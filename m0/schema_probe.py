#!/usr/bin/env python3
"""Generate a runtime schema in a temporary home and report the read API surface."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def inspect(binary):
    with tempfile.TemporaryDirectory(prefix="codex-replica-m0-schema-") as directory:
        root = Path(directory)
        for name in ("home", "sqlite", "schema"):
            (root / name).mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": str(root / "home"), "CODEX_HOME": str(root / "home"), "CODEX_SQLITE_HOME": str(root / "sqlite")}
        version = subprocess.check_output([binary, "--version"], text=True).strip()
        subprocess.run([binary, "app-server", "generate-json-schema", "--out", str(root / "schema")], env=env, check=True, stdout=subprocess.DEVNULL)
        request = json.loads((root / "schema/ClientRequest.json").read_text())
        methods = sorted({v for variant in request["oneOf"] for v in variant.get("properties", {}).get("method", {}).get("enum", [])})
        return {"binary": binary, "version": version, "request_count": len(methods),
                "thread_methods": [m for m in methods if m.startswith("thread/")],
                "explicit_subscribe_methods": [m for m in methods if "subscribe" in m and "unsubscribe" not in m],
                "schema_generated_in_isolation": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    print(json.dumps(inspect(parser.parse_args().binary), indent=2))
