#!/usr/bin/env python3
"""Export an explicit public file list from a committed revision, without Git history."""
import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess

ROOT = Path(__file__).resolve().parent.parent


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def export(repo, revision, output):
    commit = git(repo, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    names = json.loads(git(repo, 'show', commit + ':config/public-files.json'))
    files = {}
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '.git' in path.parts:
            raise ValueError(f'Invalid public path: {name}')
        entry = git(repo, 'ls-tree', commit, '--', name).decode().split()
        if not entry or entry[0] not in ('100644', '100755') or entry[1] != 'blob':
            raise ValueError(f'Public file is not a regular committed file: {name}')
        files[name] = (git(repo, 'show', commit + ':' + name), int(entry[0][-3:], 8))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for name, (data, mode) in files.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
    return {'private_source_commit': commit, 'public_files': len(files), 'output': str(output.resolve())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', default='HEAD', help='Committed source revision; uncommitted changes are not exported')
    parser.add_argument('--output', required=True, type=Path, help='New local candidate directory; must not exist')
    args = parser.parse_args()
    print(json.dumps(export(ROOT, args.ref, args.output), indent=2))


if __name__ == '__main__':
    main()
