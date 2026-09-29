"""Generated, non-private data for browser behavior and layout verification."""
import argparse
from pathlib import Path

from replica.store import Store
from replica.view import View, session_key

SID = session_key("Macmini:/fixture", "fixture-thread")


def populate(root, update=False):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    Store(root).close()
    view = View(root)
    try:
        with view.db:
            if not update:
                for host in ("Macmini", "Fedora"):
                    view.set_entity("host-" + host, "host", "", {
                        "id": host + ":/fixture", "status": "online",
                        "last_complete": "2026-09-22T00:00:00+00:00", "last_scan": {"complete": True},
                    })
                for identity, title, host, count, archived in [
                    (SID, "让每一次思考，都有迹可循", "Macmini", 4, False),
                    ("second-session", "周末，整理一下工作台", "Fedora", 0, False),
                    ("third-session", "九月阅读笔记", "Fedora", 0, True)]:
                    view.set_entity("thread-"+identity, "thread", identity, {"id": identity, "origin": host+":/fixture", "thread_id": identity,
                        "title": title, "archived": archived, "updated_at": 1790049600, "cwd": "/workspace/session-replica", "items": count,
                        "coverage": {"status": "complete", "issues": []}})
                items = [
                    {"type": "userMessage", "id": "user-1", "content": [{"type": "text", "text": "想把不同电脑上的会话保存下来，断网也能继续阅读。应该从哪里开始？"}]},
                    {"type": "agentMessage", "id": "agent-1", "phase": "commentary", "text": "先确认历史保存在哪里，以及哪些信息需要一起保留。接下来检查分段归档和断点恢复。"},
                    {"type": "commandExecution", "id": "command-1", "command": "python -m unittest discover -s tests", "aggregatedOutput": "Ran 15 tests\nOK", "status": "completed"},
                    {"type": "agentMessage", "id": "agent-final", "phase": "final_answer", "text": "## 历史已经保存好了\n\n原始记录和会话信息会一起保留。每一条记录都能找到它来自哪个文件、哪一段内容。\n\n- **增量保存**：文件没变就跳过，变化后只补上新增部分。\n- **离线阅读**：已同步的内容保存在浏览器里。\n- **可追溯**：缺少的历史会明确显示，不会悄悄变成空白。\n\n```python\narchive.capture(source)\nviewer.read_from_cache()\n```\n\n<script>window.__injected = true</script>\n![remote](https://invalid.example/private-image.png)"}]
                for position, item in enumerate(items):
                    from replica.normalize import text_content
                    view.set_entity("item-"+item["id"], "item", SID, {"session": SID, "position": position, "turn": "turn-1", "item": item,
                        "text": text_content(item), "source": {"generation": "fixture-generation", "start": str(position*100), "end": str((position+1)*100)}})
            else:
                from replica.normalize import text_content
                item = {"type": "agentMessage", "id": "agent-final", "phase": "final_answer", "text": "## 已补齐最新记录\n\n离线期间的新内容已经到达。重复同步不会产生第二份答复。"}
                view.set_entity("item-agent-final", "item", SID, {"session": SID, "position": 3, "turn": "turn-1", "item": item,
                    "text": text_content(item), "source": {"generation": "fixture-generation", "start": "400", "end": "500"}})
    finally:
        view.close()


def live_phase(root, phase):
    from replica.live import LiveJournal
    journal = LiveJournal(root)
    origin, epoch = "Macmini:/fixture", "fixture-epoch"
    def event(method, **params):
        journal.append(origin, epoch, "event", {"method": method, "params": {"threadId": "fixture-thread", "turnId": "live-turn", **params}})
    try:
        if phase == "start":
            journal.append(origin, epoch, "connected", {"scope": "fixture"})
            event("item/started", item={"type": "agentMessage", "id": "stream-item", "text": ""})
            event("item/agentMessage/delta", itemId="stream-item", delta="STREAM ")
        elif phase == "delta":
            event("item/agentMessage/delta", itemId="stream-item", delta="one ")
            event("item/agentMessage/delta", itemId="stream-item", delta="two")
        elif phase == "complete":
            for _ in range(2):
                event("item/completed", item={"type": "agentMessage", "id": "stream-item", "text": "STREAM one two DONE"})
        elif phase == "gap":
            event("item/started", item={"type": "agentMessage", "id": "unfinished", "text": "unfinished"})
            journal.append(origin, epoch, "gap", {"reason": "connection_closed"})
        elif phase == "prune":
            journal.prune(journal.seq())
    finally:
        journal.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--live-phase", choices=["start", "delta", "complete", "gap", "prune"])
    args = parser.parse_args()
    if args.live_phase:
        live_phase(args.root, args.live_phase)
    else:
        populate(args.root, args.update)
