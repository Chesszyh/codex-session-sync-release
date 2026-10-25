#!/usr/bin/env python3
"""Measure or rewrite archive object representations without changing content identities."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from replica.store import OBJECT_HEADER, Store


def repack(store, apply=False, uncompressed=False, limit=None):
    if apply and store.lock is None:
        raise ValueError("applying object changes requires the archive writer lock")
    result = {"checked": 0, "candidates": 0, "rewritten": 0, "before_bytes": 0, "after_bytes": 0}
    for directory in sorted(store.objects.iterdir()):
        if not directory.is_dir() or len(directory.name) != 2:
            continue
        for path in sorted(directory.iterdir()):
            identity = directory.name + path.name
            if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
                continue
            if limit is not None and result["checked"] >= limit:
                return result
            before = path.read_bytes()
            plain = store.get(identity)
            after = store.pack(identity, plain, compress=not uncompressed)
            replace = ((len(after) != len(before) or before.startswith(OBJECT_HEADER)) if uncompressed
                       else len(after) < len(before))
            result["checked"] += 1
            result["before_bytes"] += len(before)
            result["after_bytes"] += len(after) if replace else len(before)
            if replace:
                result["candidates"] += 1
                if apply:
                    store.write_object(identity, after)
                    result["rewritten"] += 1
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Rewrite objects atomically; default only measures")
    parser.add_argument("--uncompressed", action="store_true", help="Write the original nonce/ciphertext format for older readers")
    parser.add_argument("--limit", type=int, help="Check at most this many objects")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    try:
        store = Store(args.store, readonly=not args.apply)
    except BlockingIOError:
        parser.exit(1, "Archive writer is running; stop it before applying object changes.\n")
    try:
        print(json.dumps(repack(store, args.apply, args.uncompressed, args.limit)))
    finally:
        store.close()


if __name__ == "__main__":
    main()
