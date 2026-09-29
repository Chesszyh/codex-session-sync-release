#!/usr/bin/env python3
"""Run backend and browser checks; --full requires official decoding and restore."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="Require official adapters and REPLICA_TEST_BINARY; reject skipped tests")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.full:
        from replica.normalize import DECODER
        from replica.restore import IMPORTER
        from replica.native_verify import runtime_version
        if not DECODER.is_file() or not IMPORTER.is_file() or not os.environ.get("REPLICA_TEST_BINARY"):
            parser.error("--full needs official adapters and REPLICA_TEST_BINARY (see docs/Restore-Usage.md)")
        runtime_version(Path(os.environ["REPLICA_TEST_BINARY"]).resolve())
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover("tests"))
    if not result.wasSuccessful() or (args.full and result.skipped):
        return 1
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    for command in (["npm", "--prefix", "viewer", "run", "build"],
                    ["npm", "--prefix", "viewer", "run", "check"],
                    ["npm", "--prefix", "viewer", "test"]):
        subprocess.run(command, env=env, check=True)
    print("Full checks passed (no skips)." if args.full else f"Available checks passed; backend skips: {len(result.skipped)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
