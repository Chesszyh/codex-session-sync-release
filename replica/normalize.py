"""Project archived records with the pinned official decoder and resolve selected history."""
import fcntl
import json
from pathlib import Path
import subprocess
import threading

from .store import Store, encode
from .view import View, session_key

ROOT = Path(__file__).resolve().parent.parent
DECODER = ROOT / ".m0/official-target/debug/examples/replica_read"
PROVENANCE = json.loads((ROOT / "adapters/official/source.json").read_text())["revision"]


def archived_chunks(archive, manifest, start, end):
    for segment in manifest["segments"]:
        lo, hi = int(segment["start"]), int(segment["end"])
        if lo < end and hi > start:
            yield archive.get(segment["object"])[max(0, start-lo):min(hi, end)-lo]


def decode(archive, manifest, start, decoder):
    mode = manifest["history_mode"] or "legacy"
    process = subprocess.Popen([str(decoder), "-", str(start), mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    errors = []
    def write():
        try:
            for data in archived_chunks(archive, manifest, start, int(manifest["committed_end"])):
                process.stdin.write(data)
        except Exception as error:
            errors.append(error)
        finally:
            process.stdin.close()
    producer = threading.Thread(target=write, daemon=True)
    producer.start()
    try:
        for line in process.stdout:
            yield json.loads(line)
        producer.join()
        if process.wait() or errors:
            raise RuntimeError("official decoder did not complete")
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
        process.wait()
        producer.join()


def normalize_generation(archive, view, manifest, decoder):
    generation = manifest["generation"]
    end = int(manifest["committed_end"])
    state = view.db.execute("SELECT * FROM decoded WHERE generation=?", (generation,)).fetchone()
    if state and state["end"] == end:
        return 0
    mode = manifest["history_mode"] or "legacy"
    if mode not in ("legacy", "paginated"):
        with view.db:
            view.db.execute("INSERT OR IGNORE INTO issues VALUES (?,0,'unsupported_history_mode')", (generation,))
        return 0
    start = state["end"] if state and mode == "paginated" else 0
    count = state["records"] if start else 0
    first = archive.read_range(manifest, 0, min(end, 1048576)).split(b"\n", 1)[0]
    try:
        meta = json.loads(first)["payload"]
        if not isinstance(meta, dict):
            meta = {}
    except (ValueError, TypeError, KeyError):
        meta = {}
    base = meta.get("history_base") or {}
    initial = base.get("end_ordinal_exclusive", 0) if isinstance(base, dict) else 0
    expected = state["next_ordinal"] if start else int(initial)
    subagent_start = meta.get("subagent_history_start_ordinal")
    with view.db:
        if not start:
            view.db.execute("DELETE FROM events WHERE generation=?", (generation,))
            view.db.execute("DELETE FROM issues WHERE generation=?", (generation,))
        for row in decode(archive, manifest, start, decoder):
            count += 1
            kind = row["kind"]
            row_end = int(row["end"])
            if kind != "record":
                view.db.execute("INSERT OR IGNORE INTO issues VALUES (?,?,?)", (generation, row_end, kind))
                continue
            if mode == "paginated":
                ordinal = row["ordinal"]
                if ordinal is None or int(ordinal) < expected:
                    view.db.execute("INSERT OR IGNORE INTO issues VALUES (?,?,?)", (generation, row_end, "missing_or_regressed_ordinal"))
                    continue
                if int(ordinal) > expected:
                    view.db.execute("INSERT OR IGNORE INTO issues VALUES (?,?,?)", (generation, row_end, "ordinal_gap"))
                expected = int(ordinal) + 1
                if subagent_start is not None and int(ordinal) < int(subagent_start):
                    continue
            if row["turns"] or row["items"] or row["removedTurns"] or row.get("context"):
                view.db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?)", (generation, int(row["start"]), row_end, encode(row).decode()))
        view.db.execute("INSERT OR REPLACE INTO decoded VALUES (?,?,?,?)", (generation, end, expected, count))
    return end-start


def ancestry(archive, manifests, generation, cutoff=None, seen=()):
    if generation in seen:
        return [], ["lineage_cycle"]
    manifest = manifests[generation]
    end = int(manifest["committed_end"]) if cutoff is None else cutoff
    if end > int(manifest["committed_end"]):
        return [], ["missing_parent_prefix"]
    chain, issues = [], []
    if manifest.get("history_base"):
        edge = archive.db.execute("SELECT * FROM lineage_edges WHERE child=?", (generation,)).fetchone()
        if not edge or edge["parent"] not in manifests:
            issues.append("missing_or_unproven_parent")
        else:
            chain, issues = ancestry(archive, manifests, edge["parent"], int(edge["end_byte"]), (*seen, generation))
    return [*chain, (generation, end)], issues


def text_content(item):
    kind = item.get("type")
    if kind == "userMessage":
        return "\n".join(c.get("text", "") for c in item.get("content", []) if c.get("type") == "text")
    if kind in ("agentMessage", "plan"):
        return item.get("text", "")
    if kind == "commandExecution":
        return item.get("command", "") + "\n" + (item.get("aggregatedOutput") or "")
    if kind == "reasoning":
        return "\n".join(item.get("summary", []) + item.get("content", []))
    if kind == "rawResponse":
        record = item["record"]
        if record.get("type") == "message":
            return "\n".join(c.get("text", "") for c in record.get("content", []) if isinstance(c, dict))
        return json.dumps(record, ensure_ascii=False)
    return json.dumps(item, ensure_ascii=False)


def materialize_thread(archive, view, home, identity, metadata, generation, manifests, file_state):
    session = session_key(home, identity)
    chain, issues = ancestry(archive, manifests, generation) if generation else ([], ["selected_head_missing"])
    version = encode([metadata, [(g, end) for g, end in chain], issues, file_state, PROVENANCE]).decode()
    previous = view.db.execute("SELECT version FROM thread_versions WHERE session=?", (session,)).fetchone()
    if previous and previous[0] == version:
        return False
    view.db.executescript("""
      CREATE TEMP TABLE IF NOT EXISTS work_items(turn TEXT,id TEXT,position INTEGER,data TEXT,PRIMARY KEY(turn,id));
      CREATE TEMP TABLE IF NOT EXISTS work_turns(id TEXT PRIMARY KEY,data TEXT);
      CREATE TEMP TABLE IF NOT EXISTS work_keys(key TEXT PRIMARY KEY);
      DELETE FROM work_items; DELETE FROM work_turns; DELETE FROM work_keys;
    """)
    position = 0
    for parent, end in chain:
        issues.extend(row[0] for row in view.db.execute("SELECT kind FROM issues WHERE generation=? AND end<=?", (parent, end)))
        has_canonical = view.db.execute("SELECT 1 FROM events WHERE generation=? AND end<=? AND json_array_length(payload,'$.items')>0 LIMIT 1", (parent, end)).fetchone()
        has_context = view.db.execute("SELECT 1 FROM events WHERE generation=? AND end<=? AND json_type(payload,'$.context')='object' LIMIT 1", (parent, end)).fetchone()
        if has_context and not has_canonical:
            issues.append("legacy_response_context")
        for record in view.db.execute("SELECT start,end,payload FROM events WHERE generation=? AND end<=? ORDER BY end", (parent, end)):
            event = json.loads(record["payload"])
            if event.get("context") and not has_canonical:
                event["items"].append({"turnId": "legacy-context", "item": {"type": "rawResponse", "id": "raw-" + str(record["start"]), "record": event["context"]}})
            for turn in event["removedTurns"]:
                view.db.execute("DELETE FROM work_items WHERE turn=?", (turn,))
                view.db.execute("DELETE FROM work_turns WHERE id=?", (turn,))
            for turn in event["turns"]:
                view.db.execute("INSERT OR REPLACE INTO work_turns VALUES (?,?)", (turn["id"], encode(turn).decode()))
            for change in event["items"]:
                turn, item = change["turnId"], change["item"]
                value = {"session": session, "turn": turn, "item": item, "text": text_content(item),
                         "startedAtMs": change.get("startedAtMs"), "completedAtMs": change.get("completedAtMs"),
                         "source": {"generation": parent, "start": str(record["start"]), "end": str(record["end"]),
                                    "ordinal": event["ordinal"], "parser": PROVENANCE}}
                view.db.execute("INSERT INTO work_items VALUES (?,?,?,?) ON CONFLICT(turn,id) DO UPDATE SET data=excluded.data", (turn, item["id"], position, encode(value).decode()))
                position += 1
    with view.db:
        for row in view.db.execute("SELECT * FROM work_items ORDER BY position"):
            key = encode(["item", session, row["turn"], row["id"]]).decode()
            value = json.loads(row["data"])
            value["position"] = row["position"]
            view.set_entity(key, "item", session, value)
            view.db.execute("INSERT INTO work_keys VALUES (?)", (key,))
        for row in view.db.execute("SELECT * FROM work_turns"):
            key = encode(["turn", session, row["id"]]).decode()
            view.set_entity(key, "turn", session, json.loads(row["data"]))
            view.db.execute("INSERT INTO work_keys VALUES (?)", (key,))
        stale = view.db.execute("SELECT key FROM entities WHERE session=? AND kind IN ('item','turn') AND key NOT IN (SELECT key FROM work_keys)", (session,)).fetchall()
        for row in stale:
            view.delete_entity(row["key"])
        header = {"id": session, "origin": home, "thread_id": identity, "generation": generation,
                  "title": metadata.get("thread_name") or metadata.get("title") or identity, "archived": bool(metadata.get("archived")),
                  "updated_at": metadata.get("updated_at"), "cwd": metadata.get("cwd"), "metadata": metadata,
                  "items": view.db.execute("SELECT count(*) FROM work_items").fetchone()[0],
                  "coverage": {"status": "partial" if issues else "complete", "issues": sorted(set(issues)),
                               "issue_count": len(issues), "parser": PROVENANCE, "source_state": file_state,
                               "scope": "officially projected items; opaque context remains in raw archive"}}
        view.set_entity(encode(["thread", session]).decode(), "thread", session, header)
        view.db.execute("INSERT OR REPLACE INTO thread_versions VALUES (?,?)", (session, version))
    return True


def sync(root, decoder=DECODER):
    with (Path(root) / "normalizer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        archive, view = Store(root, readonly=True), View(root)
        try:
            archive.db.execute("BEGIN")
            source_seq = archive.status()["seq"]
            manifests = {r["id"]: archive.json(r["manifest_object"]) for r in archive.db.execute("SELECT * FROM generations WHERE rollout_id<>'session_index'")}
            decoded_bytes = sum(normalize_generation(archive, view, m, decoder) for m in manifests.values())
            changed, identities = 0, set()
            for thread in archive.db.execute("SELECT * FROM threads"):
                identities.add((thread["home_id"], thread["thread_id"]))
                selected = archive.db.execute("SELECT * FROM files WHERE home_id=? AND (path=? OR path=?) ORDER BY state='present' DESC,encoding='jsonl' DESC LIMIT 1", (thread["home_id"], thread["head_path"], (thread["head_path"] or "") + ".zst")).fetchone()
                changed += materialize_thread(archive, view, thread["home_id"], thread["thread_id"], archive.json(thread["metadata_object"]), selected["generation"] if selected else None, manifests, selected["state"] if selected else "missing")
            for generation, manifest in manifests.items():
                if manifest["thread_id"] is None:
                    continue
                pair = (manifest["origin"], manifest["thread_id"])
                if pair not in identities:
                    # A raw-only thread has no selected-head evidence when multiple rollouts exist.
                    candidates = [m for m in manifests.values() if (m["origin"], m["thread_id"]) == pair]
                    metadata = {"title": manifest["thread_id"], "raw_only": True}
                    changed += materialize_thread(archive, view, *pair, metadata, generation if len(candidates) == 1 else None, manifests, "raw_only")
                    identities.add(pair)
            with view.db:
                for home in archive.status()["homes"]:
                    view.set_entity(encode(["host", home["id"]]).decode(), "host", "", home)
                view.db.execute("INSERT OR REPLACE INTO state VALUES ('source_seq',?)", (str(source_seq),))
            return {**view.status(), "decoded_bytes": decoded_bytes, "changed_threads": changed}
        finally:
            archive.close()
            view.close()
