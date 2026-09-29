#!/usr/bin/env python3
"""Register a Codex history source on the collector, optionally over SSH."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile


def probe(home=None, sqlite_home=None):
    home = Path(home or Path.home() / '.codex').expanduser().resolve()
    sqlite_home = Path(sqlite_home).expanduser().resolve() if sqlite_home else home
    if not (home / 'sessions').is_dir():
        raise ValueError(f'{home}/sessions does not exist; specify --home for the native Codex directory')
    with closing(sqlite3.connect((sqlite_home / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        count = db.execute('SELECT count(*) FROM threads').fetchone()[0]
    return {'home': str(home), 'sqlite_home': str(sqlite_home), 'threads': count}


def register(config, source):
    config = Path(config).expanduser()
    old = config.read_bytes() if config.exists() else None
    data = json.loads(old) if old is not None else {'sources': []}
    for existing in data['sources']:
        if (existing['host'], existing['home']) == (source['host'], source['home']):
            if existing != source:
                raise ValueError('This source identity already has different settings; inspect the existing config before changing it')
            return False
    data['sources'].append(source)
    config.parent.mkdir(parents=True, exist_ok=True)
    if old is not None:
        with tempfile.NamedTemporaryFile(prefix=config.name + '.before-add-', dir=config.parent, delete=False) as backup:
            backup.write(old)
    with tempfile.NamedTemporaryFile(prefix=config.name + '.', dir=config.parent, delete=False) as output:
        output.write((json.dumps(data, indent=2) + '\n').encode())
        name = output.name
    os.replace(name, config)
    return True


def reload_collector():
    if sys.platform == 'darwin':
        target = f'gui/{os.getuid()}/xyz.chesszyh.codex-session-replica'
        check = ['launchctl', 'print', target]
        restart = ['launchctl', 'kill', 'SIGTERM', target]
    elif sys.platform.startswith('linux'):
        check = ['systemctl', '--user', 'is-active', '--quiet', 'codex-session-replica.service']
        restart = ['systemctl', '--user', 'restart', 'codex-session-replica.service']
    else:
        return False
    try:
        active = subprocess.run(check, capture_output=True).returncode == 0
    except FileNotFoundError:
        return False
    if not active:
        return False
    subprocess.run(restart, check=True)
    return True


def remote(target, args, source):
    return subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                           target, shlex.join(['python3', '-', *args])],
                          input=source, text=True, capture_output=True, check=True).stdout


def main(argv=None, script=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', help='Collector SSH target; omit when running on the collector')
    parser.add_argument('--ssh', help='Source SSH target, reachable from the collector')
    parser.add_argument('--host', help='Stable display/identity name; defaults to the SSH target or local')
    parser.add_argument('--home', help='Source Codex home; defaults to the source account ~/.codex')
    parser.add_argument('--sqlite-home', help='Source database directory; defaults to --home')
    parser.add_argument('--config', default='~/.config/codex-session-sync/sources.json')
    parser.add_argument('--check', action='store_true', help='Probe only; do not change config or restart services')
    parser.add_argument('--probe', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    forwarded = []
    for flag in ('ssh', 'host', 'home', 'sqlite_home', 'config'):
        value = getattr(args, flag)
        if value is not None:
            forwarded += ['--' + flag.replace('_', '-'), value]
    if args.check:
        forwarded.append('--check')
    try:
        if args.server:
            # Embed the script so stdin execution can forward the same probe to the source.
            program = 'exec(compile(' + repr(script) + ', "add_source.py", "exec"))'
            print(remote(args.server, forwarded, 'SCRIPT = ' + repr(script) + '\n' + program), end='')
            return
        if args.probe:
            print(json.dumps(probe(args.home, args.sqlite_home)))
            return
        if args.ssh:
            options = ['--probe']
            for name in ('home', 'sqlite_home'):
                if getattr(args, name):
                    options += ['--' + name.replace('_', '-'), getattr(args, name)]
            info = json.loads(remote(args.ssh, options, script))
        else:
            info = probe(args.home, args.sqlite_home)
        source = {'host': args.host or args.ssh or 'local', 'home': info['home'], 'sqlite_home': info['sqlite_home']}
        if args.ssh:
            source['ssh'] = args.ssh
        print(f"Source reachable: {source['host']}; {info['threads']} registered threads; {info['home']}")
        if args.check:
            return
        changed = register(args.config, source)
        print('Source added.' if changed else 'Source already registered; config unchanged.')
        if not changed:
            return
        default_config = Path.home() / '.config/codex-session-sync/sources.json'
        if Path(args.config).expanduser().resolve() == default_config.resolve() and reload_collector():
            print('Collector restart requested; inspect its next scan to confirm history was collected.')
        else:
            print('No running managed collector found. Start or restart your watch command with --config ' + args.config)
    except (OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        parser.exit(1, detail + '\n')


if __name__ == '__main__':
    main(script=globals().get('SCRIPT') or (Path(__file__).read_text() if __file__ != '<stdin>' else None))
