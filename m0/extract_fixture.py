#!/usr/bin/env python3
"""Extract a content-free structural fixture from recent paginated records."""
import argparse
import json
from pathlib import Path

ALLOWED = {"type": {"session_meta", "event_msg", "item_completed", "UserMessage", "AgentMessage", "CommandExecution", "Text", "text", "unknown"},
           "phase": {"commentary", "final_answer"}, "status": {"completed", "in_progress", "failed"},
           "history_mode": {"paginated", "legacy"}, "source": {"cli", "exec", "vscode", "agent", "unified_exec_startup", "unified_exec_interaction", "user_shell"}}


def sanitize(value, key="", identities=None):
    if identities is None:
        identities = {}
    if isinstance(value, dict):
        return {k: sanitize(v, k, identities) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, key, identities) for v in value]
    if isinstance(value, str):
        if value in ALLOWED.get(key, set()):
            return value
        if key in ("id", "session_id", "thread_id", "turn_id", "client_id", "process_id"):
            if value not in identities:
                identities[value] = "00000000-0000-4000-8000-%012d" % (len(identities) + 1)
            return identities[value]
        if key == "timestamp":
            return "2026-09-22T00:00:00.000Z"
        if key in ("cwd", "runtime_workspace_roots"):
            return "file:///fixture/workspace" if value.startswith("file:") else "/fixture/workspace"
        if key == "cli_version":
            return "0.155.0-alpha.9.2"
        if key == "model_provider":
            return "openai"
        return "M0_REDACTED"
    return value


def extract(home):
    found = {}
    identities = {}
    for path in sorted((home / "sessions").rglob("*.jsonl"), reverse=True)[:40]:
        with path.open("rb") as source:
            for _ in range(200):
                line = source.readline(2 * 1024 * 1024)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload", {})
                if record.get("type") != "event_msg" or payload.get("type") != "item_completed":
                    continue
                item = payload.get("item", {})
                kind = item.get("type")
                if kind not in ("UserMessage", "AgentMessage", "CommandExecution"):
                    continue
                label = kind + (":" + item.get("phase", "unknown") if kind == "AgentMessage" else "")
                if label in found:
                    continue
                # Retain only known structural fields: arbitrary extension dictionaries can contain private keys.
                fields = {"type", "id", "client_id", "content", "phase", "process_id", "command", "cwd", "parsed_cmd", "source", "status", "stdout", "stderr", "aggregated_output", "exit_code", "duration", "formatted_output"}
                selected = {k: v for k, v in item.items() if k in fields}
                if "content" in selected:
                    selected["content"] = [{k: v for k, v in c.items() if k in ("type", "text")} for c in selected["content"] if c.get("type") in ("text", "Text")]
                clean = sanitize({"timestamp": record["timestamp"], "type": "event_msg", "payload": {"type": "item_completed", "thread_id": payload["thread_id"], "turn_id": payload["turn_id"], "item": selected, "started_at_ms": 0, "completed_at_ms": 1}}, identities=identities)
                found[label] = clean
        if len(found) == 4:
            break
    if len(found) != 4:
        raise RuntimeError("Need user, commentary, final and command records; found " + str(sorted(found)))
    for ordinal, (label, record) in enumerate(found.items(), 2):
        record["ordinal"] = ordinal
        record["payload"]["thread_id"] = "00000000-0000-4000-8000-000000000001"
        record["payload"]["turn_id"] = "turn-1"
        item = record["payload"]["item"]
        item["id"] = label
        if "content" in item:
            item["content"] = [{"type": "text" if label == "UserMessage" else "Text", "text": "M0_" + label.replace(":", "_")}]
            if label == "UserMessage":
                item["content"][0]["text_elements"] = []
        else:
            item["command"] = ["printf", "M0_TOOL"]
            item["parsed_cmd"] = [{"type": "unknown", "cmd": "printf M0_TOOL"}]
            item["stdout"] = item["aggregated_output"] = item["formatted_output"] = "M0_TOOL"
            item["stderr"] = ""
        yield record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    for record in extract(parser.parse_args().home):
        print(json.dumps(record, ensure_ascii=False))
