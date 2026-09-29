#!/usr/bin/env python3
"""Generate shared candidate inputs and independent transcript expectations."""
import copy
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent
THREAD = "00000000-0000-4000-8000-000000000001"
PARENT = "00000000-0000-4000-8000-000000000002"
TIME = "2026-09-22T00:00:00.000Z"


def envelope(kind, payload, ordinal=None):
    value = {"timestamp": TIME, "type": kind, "payload": payload}
    if ordinal is not None:
        value["ordinal"] = ordinal
    return value


def meta(mode="legacy", identity=THREAD, **extra):
    return envelope("session_meta", {"id": identity, "session_id": identity, "timestamp": TIME,
        "cwd": "/fixture/workspace", "originator": "codex_cli_rs", "cli_version": "0.155.0-alpha.9.2",
        "source": "cli", "model_provider": "openai", "history_mode": mode, **extra}, 0)


def message(role, text, phase=None):
    value = {"type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}
    if phase:
        value["phase"] = phase
    return envelope("response_item", value)


def encode(records):
    return b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode() for r in records)


def generate(output):
    output.mkdir(parents=True, exist_ok=True)
    cases = []
    def add(name, records, expected, *, tail=b"", compressed=False, warning=None, extra_files=None, selected=None, archived=False, forbidden=None):
        directory = output / name
        home = directory / "home"
        filename = "rollout-2026-09-22T00-00-00-" + THREAD + ".jsonl"
        relative = ("archived_sessions/" if archived else "sessions/2026/09/22/") + filename
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = encode(records) + tail
        path.write_bytes(raw)
        if extra_files:
            for rel, content in extra_files.items():
                p = home / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(content)
        if compressed:
            subprocess.run(["zstd", "-q", "-f", str(path), "-o", str(path) + ".zst"], check=True)
            path.unlink()
            path = Path(str(path) + ".zst")
        case = {"name": name, "home": str(home.resolve()), "path": str(path.resolve()), "thread_id": THREAD,
                "expected_text": expected, "forbidden_text": forbidden or [], "expected_warning": warning,
                "source_kind": "structural derivative of real records" if name.startswith("real-") else "synthetic",
                "complete_end": len(encode(records))}
        (directory / "metadata.json").write_text(json.dumps({"thread_id": THREAD, "selected_rollout": selected or str(path.relative_to(home)), "name": "M0_DATABASE_TITLE", "git": None}, indent=2) + "\n")
        (directory / "expected.json").write_text(json.dumps(case, indent=2) + "\n")
        cases.append(case)
        return case
    legacy = [meta(), envelope("turn_context", {"turn_id": "turn-1", "cwd": "/fixture/workspace"}),
              message("user", "M0_USER"), message("assistant", "M0_COMMENTARY", "commentary"),
              message("assistant", "M0_FINAL", "final_answer")]
    expected = ["M0_USER", "M0_COMMENTARY", "M0_FINAL"]
    add("legacy", legacy, expected)
    add("archived", legacy, expected, archived=True)
    add("metadata-only", legacy, expected)
    tools = [envelope("response_item", {"type": "function_call", "name": "exec_command", "call_id": "tool-1", "arguments": '{"cmd":"printf M0_TOOL"}'}),
             envelope("response_item", {"type": "function_call_output", "call_id": "tool-1", "output": "M0_TOOL_RESULT"})]
    add("legacy-tools", legacy + tools, expected + ["M0_TOOL_RESULT"])
    add("partial-utf8", legacy, expected, tail=b'{"type":"response_item","payload":{"text":"\xe4\xb8', warning="incomplete tail")
    add("unknown-record", legacy + [envelope("m0_future_record", {"value": "M0_UNKNOWN"})], expected, warning="unknown record")
    add("invalid-complete-line", legacy, expected, tail=b'{bad json}\n', warning="invalid JSON")
    add("compressed", legacy, expected, compressed=True)
    add("large-record", legacy + [message("assistant", "M0_LARGE_" + "x" * (8 * 1024 * 1024), "final_answer")], expected + ["M0_LARGE_"])
    for host in ("local", "macmini"):
        real = [json.loads(line) for line in (ROOT / "fixtures/shapes" / (host + ".jsonl")).read_text().splitlines()]
        records = [meta("paginated"), envelope("event_msg", {"type": "task_started", "turn_id": "turn-1", "model_context_window": 100000}, 1)] + real
        records.append(envelope("event_msg", {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "M0_AgentMessage_final"}, len(records)))
        add("real-" + host, records, ["M0_UserMessage", "M0_AgentMessage_commentary", "M0_AgentMessage_final", "M0_TOOL"])
    parent = [meta("paginated", PARENT)] + copy.deepcopy(real)
    for ordinal, record in enumerate(parent):
        record["ordinal"] = ordinal
    for record in parent[1:]:
        record["payload"]["thread_id"] = PARENT
    parent_bytes = encode(parent)
    parent_rel = "sessions/2026/09/22/rollout-2026-09-22T00-00-00-" + PARENT + ".jsonl"
    child = [meta("paginated", history_base={"thread_id": PARENT, "end_ordinal_exclusive": len(parent), "end_byte_offset": len(parent_bytes)})]
    child[0]["ordinal"] = len(parent)
    add("referenced-fork", child, ["M0_UserMessage", "M0_AgentMessage_final"], extra_files={parent_rel: parent_bytes})
    add("missing-parent", child, [], warning="missing dependency")
    newer = "sessions/2026/09/22/rollout-2026-09-22T00-00-01-" + THREAD + "_00000000-0000-4000-8000-000000000003.jsonl"
    add("selected-head", legacy, ["M0_SELECTED_HEAD"], extra_files={newer: encode([meta(), message("user", "M0_SELECTED_HEAD")])}, selected=newer, forbidden=expected)
    update = copy.deepcopy(real)
    agent = next(r for r in update if r["payload"]["item"]["type"] == "AgentMessage")
    changed = copy.deepcopy(agent)
    changed["payload"]["item"]["content"][0]["text"] = "M0_UPDATED_ITEM"
    records = [meta("paginated")] + update + [changed]
    for ordinal, record in enumerate(records):
        record["ordinal"] = ordinal
    add("item-update", records, ["M0_UPDATED_ITEM"], forbidden=[agent["payload"]["item"]["content"][0]["text"]])
    (output / "cases.json").write_text(json.dumps(cases, indent=2) + "\n")
    return cases


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({"cases": len(generate(args.out))}))
