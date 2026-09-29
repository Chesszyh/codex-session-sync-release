"""Portable verification of an isolated native restore using only the standard library."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess

from m0.rpc_probe import Client

ROOT = Path(__file__).resolve().parent.parent
SPEC = json.loads((ROOT / "adapters/official/source.json").read_text())


def protect_output(output):
    for path in output.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file():
            path.chmod(0o600)
        elif path.is_dir():
            path.chmod(0o700)


class RestoreRefused(ValueError):
    """The archive remains readable, but this input is not approved for native import."""


def runtime_version(binary):
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    if version not in SPEC["restore_tested_runtimes"]:
        raise RestoreRefused("unsupported runtime; tested restore runtimes: " + ", ".join(SPEC["restore_tested_runtimes"]))
    return version



def pages(client, method, params):
    seen = set()
    while True:
        response = client.call(method, params)
        if "error" in response:
            raise RuntimeError(method + " failed; isolated native log retained")
        page = response["result"]
        yield from page["data"]
        cursor = page.get("nextCursor")
        if cursor is None:
            return
        if cursor in seen:
            raise RuntimeError(method + " repeated a cursor")
        seen.add(cursor)
        params = {**params, "cursor": cursor}


def verify_native(output, binary, page_size=50):
    output = Path(output).resolve()
    runtime_version(binary)
    plan = json.loads((output / "plan.json").read_text())
    home, sqlite = output / "home", output / "sqlite"
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home), "CODEX_SQLITE_HOME": str(sqlite)}
    expected = sqlite3.connect((output / "expected.sqlite").as_uri() + "?mode=ro", uri=True)
    count = 0
    try:
        with (output / "native.log").open("a") as log:
            client = Client([str(binary), "app-server", "--listen", "stdio://"], env, stderr=log, umask=0o077)
            try:
                init = client.call("initialize", {"clientInfo": {"name": "codex_session_replica_restore", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
                if "error" in init or Path(init["result"]["codexHome"]).resolve() != home:
                    raise RuntimeError("runtime did not initialize in the isolated home")
                client.send({"method": "initialized", "params": {}})
                thread_id = plan["thread_id"]
                response = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
                if "error" in response:
                    raise RuntimeError("thread/read failed in the isolated home")
                thread = response["result"]["thread"]
                if Path(thread["path"]) != Path(plan["threads"][0]["path"]):
                    raise RuntimeError("official read selected a different history head")
                turns = list(pages(client, "thread/turns/list", {"threadId": thread_id, "itemsView": "notLoaded", "limit": page_size, "sortDirection": "asc"}))
                expected_rows = iter(expected.execute("SELECT turn,id,item FROM items ORDER BY position"))
                for entry in pages(client, "thread/items/list", {"threadId": thread_id, "limit": page_size, "sortDirection": "asc"}):
                    key = (entry["turnId"], entry["item"]["id"])
                    row = next(expected_rows, None)
                    if not row or key != row[:2] or json.loads(row[2]) != entry["item"]:
                        raise RuntimeError("official history differs from the archived projection")
                    count += 1
                if next(expected_rows, None) is not None:
                    raise RuntimeError("official history omitted archived items")
                turn_ids = {r["id"] for r in turns}
                expected_turns = {r[0] for r in expected.execute("SELECT id FROM turns")}
                if expected_turns != turn_ids or len(turn_ids) != len(turns):
                    raise RuntimeError("official history differs from the projected turn inventory")
                for turn in turns:
                    wanted = json.loads(expected.execute("SELECT data FROM turns WHERE id=?", (turn["id"],)).fetchone()[0])
                    if any(turn.get(field) != value for field, value in wanted.items()):
                        raise RuntimeError("official turn state differs from the archived projection")
                if list(pages(client, "thread/loaded/list", {"limit": 100})):
                    raise RuntimeError("read verification unexpectedly loaded a native thread")
            finally:
                client.close()
        metadata_fields = verify_metadata(output, plan)
        return {"read_verified": True, "turns": len(turns), "items": count, "loaded_threads": 0,
                "metadata_fields_verified": metadata_fields, "execution_started": False}
    finally:
        expected.close()
        protect_output(output)


def verify_metadata(output, plan):
    db = sqlite3.connect((output / "sqlite/state_5.sqlite").as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = dict(db.execute("SELECT * FROM threads WHERE id=?", (plan["thread_id"],)).fetchone())
        source = plan["threads"][0]["metadata"]
        mappings = json.loads((output / "import.json").read_text())
        # These legacy columns have no field in the pinned native metadata model.
        derived = {"has_user_event", "is_pinned"}
        for field, value in source.items():
            if field in derived:
                continue
            if field == "rollout_path":
                value = plan["threads"][0]["path"]
            elif field == "thread_section_id" and value is not None:
                value = mappings["sections"][value]
            elif field == "project_id" and value is not None:
                value = mappings["projects"][value]
            if row[field] != value:
                raise RuntimeError("native metadata differs: " + field)
        for source_section in plan["tables"]["thread_sections"]:
            section = db.execute("SELECT * FROM thread_sections WHERE id=?", (mappings["sections"][source_section["id"]],)).fetchone()
            for field in ("name", "appearance"):
                if section[field] != source_section[field]:
                    raise RuntimeError("native section metadata differs: " + field)
        for project in plan["tables"]["projects"]:
            project_id = mappings["projects"][project["id"]]
            actual = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if actual["name"] != project["name"] or json.loads(actual["metadata"]) != json.loads(project["metadata"]):
                raise RuntimeError("native project metadata differs")
            roots = [r[0] for r in db.execute("SELECT path FROM project_roots WHERE project_id=? ORDER BY position", (project_id,))]
            if roots != [r["path"] for r in plan["tables"]["project_roots"] if r["project_id"] == project["id"]]:
                raise RuntimeError("native project roots differ")
        actual_attachments = {(r["attachment_type"], r["identity_key"]): json.loads(r["payload"]) for r in db.execute("SELECT * FROM thread_attachments WHERE thread_id=?", (plan["thread_id"],))}
        wanted_attachments = {(r["attachment_type"], r["identity_key"]): json.loads(r["payload"]) for r in plan["tables"]["thread_attachments"]}
        if actual_attachments != wanted_attachments:
            raise RuntimeError("native attachments differ")
        return len(source) - len(derived)
    finally:
        db.close()
