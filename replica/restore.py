"""Restore a proven paginated history into a new home using pinned official code."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess

from .normalize import ROOT, DECODER, ancestry, archived_chunks, decode
from .native_verify import RestoreRefused, runtime_version, verify_native, protect_output
from .store import Store, durable_write, encode, ensure_directory

SPEC = json.loads((ROOT / "adapters/official/source.json").read_text())
SCHEMA = json.loads((ROOT / "adapters/official/restore-schema.json").read_text())
IMPORTER = DECODER.with_name("replica_restore")



def source_plan(archive, origin, thread_id):
    home = archive.db.execute("SELECT * FROM homes WHERE id=?", (origin,)).fetchone()
    thread = archive.db.execute("SELECT * FROM threads WHERE home_id=? AND thread_id=?", (origin, thread_id)).fetchone()
    if not home or not thread:
        raise RestoreRefused("source home or authoritative thread metadata is missing")
    inventory = archive.json(home["inventory_object"])
    metadata = inventory.get("metadata_after_capture", inventory["metadata"])
    for table, fields in metadata["schema"].items():
        if table not in SCHEMA or set(fields) != set(SCHEMA[table]):
            raise RestoreRefused("unsupported source schema: " + table)
    if set(metadata["schema"].get("threads", [])) != set(SCHEMA["threads"]):
        raise RestoreRefused("thread schema is incomplete")
    row = archive.json(thread["metadata_object"])
    if set(row) != set(SCHEMA["threads"]) or row["history_mode"] != "paginated":
        raise RestoreRefused("native restore currently supports the allowlisted paginated schema")
    selected = archive.db.execute(
        "SELECT * FROM files WHERE home_id=? AND (path=? OR path=?) ORDER BY encoding='jsonl' DESC LIMIT 1",
        (origin, thread["head_path"], (thread["head_path"] or "") + ".zst")).fetchone()
    if not selected:
        raise RestoreRefused("selected head is missing; no filename or time-based fallback is allowed")
    manifests = {r["id"]: archive.json(r["manifest_object"]) for r in archive.db.execute(
        "SELECT * FROM generations WHERE home_id=? AND rollout_id<>'session_index'", (origin,))}
    head = manifests[selected["generation"]]
    if head["thread_id"] != thread_id:
        raise RestoreRefused("selected head belongs to a different thread")
    chain, issues = ancestry(archive, manifests, selected["generation"])
    if issues:
        raise RestoreRefused("unproven dependency closure: " + ", ".join(issues))
    rollouts = []
    seen = set()
    for generation, end in chain:
        manifest = manifests[generation]
        if manifest["schema_version"] != 1 or manifest["history_mode"] != "paginated":
            raise RestoreRefused("unsupported raw manifest or history mode")
        if manifest["rollout_id"] in seen:
            raise RestoreRefused("dependency graph contains multiple generations of the same rollout")
        seen.add(manifest["rollout_id"])
        relative = Path(manifest["path"].removesuffix(".zst"))
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] not in ("sessions", "archived_sessions"):
            raise RestoreRefused("rollout path is outside the supported roots")
        if not relative.name.endswith(manifest["rollout_id"] + ".jsonl"):
            raise RestoreRefused("rollout filename does not identify its native rollout ID")
        rollouts.append({"generation": generation, "rollout_id": manifest["rollout_id"], "relative_path": str(relative),
                         "end": end, "manifest": manifest})
    tables = {}
    source_tables = metadata["tables"]
    for name, field, selected_ids in (
        ("thread_sections", "id", [row["thread_section_id"]]),
        ("projects", "id", [row["project_id"]]),
        ("project_roots", "project_id", [row["project_id"]]),
        ("thread_attachments", "thread_id", [thread_id]),
    ):
        tables[name] = [r for r in source_tables.get(name, []) if r[field] in selected_ids]
    for field, table in (("thread_section_id", "thread_sections"), ("project_id", "projects")):
        if row[field] is not None and len(tables[table]) != 1:
            raise RestoreRefused("selected thread refers to missing " + table)
    if row["project_id"] is not None and "project_roots" not in metadata["schema"]:
        raise RestoreRefused("project roots were not captured; collect this source again")
    tables["project_roots"].sort(key=lambda r: r["position"])
    return {"origin": origin, "thread_id": thread_id, "metadata": row, "tables": tables, "schema": metadata["schema"],
            "rollouts": rollouts, "source_state": selected["state"]}


def project_expected(archive, plan, output):
    db = sqlite3.connect(output / "expected.sqlite")
    os.chmod(output / "expected.sqlite", 0o600)
    db.executescript("CREATE TABLE items(turn TEXT,id TEXT,position INTEGER,item TEXT,PRIMARY KEY(turn,id));"
                     "CREATE TABLE turns(id TEXT PRIMARY KEY,data TEXT);")
    records, position = 0, 0
    try:
        for segment in plan["rollouts"]:
            manifest = {**segment["manifest"], "committed_end": str(segment["end"])}
            first = archive.read_range(manifest, 0, min(segment["end"], 1048576)).split(b"\n", 1)[0]
            try:
                header = json.loads(first)
                meta = header["payload"]
                if header["type"] != "session_meta" or meta["id"] != manifest["thread_id"] or meta["history_mode"] != "paginated":
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise RestoreRefused("raw header does not match the archived identity") from None
            base = meta.get("history_base")
            if base != manifest.get("history_base"):
                raise RestoreRefused("raw lineage differs from the manifest")
            ordinal = int(base["end_ordinal_exclusive"]) if base else 0
            end = 0
            for event in decode(archive, manifest, 0, DECODER):
                if event["kind"] != "record" or event["ordinal"] is None or int(event["ordinal"]) != ordinal:
                    raise RestoreRefused("unsupported raw record or non-contiguous ordinal")
                records += 1
                ordinal += 1
                end = int(event["end"])
                if meta.get("subagent_history_start_ordinal") is not None and int(event["ordinal"]) < int(meta["subagent_history_start_ordinal"]):
                    continue
                for turn in event["removedTurns"]:
                    db.execute("DELETE FROM items WHERE turn=?", (turn,))
                    db.execute("DELETE FROM turns WHERE id=?", (turn,))
                for turn in event["turns"]:
                    db.execute("INSERT OR REPLACE INTO turns VALUES (?,?)", (turn["id"], encode(turn).decode()))
                for change in event["items"]:
                    item = change["item"]
                    db.execute("INSERT INTO items VALUES (?,?,?,?) ON CONFLICT(turn,id) DO UPDATE SET item=excluded.item",
                               (change["turnId"], item["id"], position, encode(item).decode()))
                    position += 1
            if end != segment["end"]:
                raise RestoreRefused("dependency cutoff is not a complete record boundary")
            segment["end_ordinal"] = ordinal
        for parent, child in zip(plan["rollouts"], plan["rollouts"][1:]):
            base = child["manifest"]["history_base"]
            if base["thread_id"] != parent["rollout_id"] or int(base["end_byte_offset"]) != parent["end"] or int(base["end_ordinal_exclusive"]) != parent["end_ordinal"]:
                raise RestoreRefused("dependency byte/ordinal cutoff is unproven")
        db.commit()
        return records
    finally:
        db.close()



def restore(root, origin, thread_id, output, binary, page_size=50):
    output = Path(output).resolve()
    if output.exists():
        raise RestoreRefused("restore requires a new directory; existing native homes are never merged or overwritten")
    if page_size < 1:
        raise ValueError("page size must be positive")
    version = runtime_version(binary)
    if not IMPORTER.is_file() or not DECODER.is_file():
        raise RestoreRefused("build the pinned official adapters first")
    archive = Store(root, readonly=True)
    try:
        archive.db.execute("BEGIN")
        source = source_plan(archive, origin, thread_id)
        ensure_directory(output.parent)
        output.mkdir(mode=0o700)
        durable_write(output / "report.json", encode({"read_verified": False, "execution_started": False}))
        records = project_expected(archive, source, output)
        for name in ("home", "sqlite"):
            (output / name).mkdir(mode=0o700)
        rollouts = []
        for segment in source["rollouts"]:
            dest = output / "home" / segment["relative_path"]
            ensure_directory(dest.parent)
            with os.fdopen(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as target:
                for data in archived_chunks(archive, segment["manifest"], 0, segment["end"]):
                    target.write(data)
            rollouts.append({k: segment[k] for k in ("generation", "rollout_id", "relative_path", "end", "end_ordinal")} | {"path": str(dest)})
        plan = {"origin": origin, "thread_id": thread_id, "schema": source["schema"], "tables": source["tables"], "rollouts": rollouts,
                "threads": [{"metadata": source["metadata"], "path": rollouts[-1]["path"]}]}
        durable_write(output / "plan.json", encode(plan))
        with (output / "import.log").open("w") as log:
            result = subprocess.run([str(IMPORTER), str(output / "plan.json")], stdout=subprocess.PIPE, stderr=log, check=True, umask=0o077)
        durable_write(output / "import.json", result.stdout)
        report = verify_native(output, binary, page_size)
        for segment, restored in zip(source["rollouts"], rollouts):
            with Path(restored["path"]).open("rb") as snapshot:
                for data in archived_chunks(archive, segment["manifest"], 0, segment["end"]):
                    if snapshot.read(len(data)) != data:
                        raise RuntimeError("raw bytes changed during native import/read")
                if snapshot.read(1):
                    raise RuntimeError("native runtime appended to restored raw history")
        report.update(runtime=version, official_revision=SPEC["revision"], raw_unchanged=True, records=records,
                      rollouts=len(rollouts), raw_bytes=sum(r["end"] for r in rollouts), source_state=source["source_state"],
                      resource_files_restored=False, credentials_restored=False, workspace_exists=Path(source["metadata"]["cwd"]).is_dir(),
                      metadata_sidecar_only=["threads.has_user_event", "threads.is_pinned", "projects.position",
                                             "projects.created_at_ms", "projects.updated_at_ms", "thread_attachments.created_at"],
                      metadata_id_mapping="officially allocated section/project/attachment identities; original metadata retained in plan.json")
        durable_write(output / "report.json", encode(report))
        return report
    finally:
        archive.close()
        if output.is_dir():
            protect_output(output)
