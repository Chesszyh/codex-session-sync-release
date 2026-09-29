import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.add_source import main, probe, register


class AddSourceTests(unittest.TestCase):
    def test_command_registers_generated_source_in_custom_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sessions').mkdir()
            with contextlib.closing(sqlite3.connect(root / 'state_5.sqlite')) as db:
                db.execute('CREATE TABLE threads(id TEXT)')
            config = root / 'sources.json'
            command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/add_source.py'),
                       '--host', 'fixture', '--home', str(root), '--config', str(config)]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertIn('Source added.', result.stdout)
            self.assertEqual(json.loads(config.read_text())['sources'][0]['host'], 'fixture')
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertIn('already registered', result.stdout)
            self.assertNotIn('restart requested', result.stdout)

    def test_probe_and_register_preserve_existing_config_and_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sessions').mkdir()
            with contextlib.closing(sqlite3.connect(root / 'state_5.sqlite')) as db:
                db.execute('CREATE TABLE threads(id TEXT)')
                db.execute("INSERT INTO threads VALUES ('fixture')")
                db.commit()
            info = probe(root)
            self.assertEqual(info['threads'], 1)
            config = root / 'sources.json'
            old = {'sources': [{'host': 'old', 'home': '/old', 'sqlite_home': '/old'}], 'note': 'keep'}
            config.write_text(json.dumps(old))
            source = {'host': 'new', 'home': info['home'], 'sqlite_home': info['sqlite_home']}
            self.assertTrue(register(config, source))
            self.assertFalse(register(config, source))
            actual = json.loads(config.read_text())
            self.assertEqual(actual['sources'], old['sources'] + [source])
            self.assertEqual(actual['note'], 'keep')
            self.assertEqual(json.loads(next(root.glob('sources.json.before-add-*')).read_text()), old)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                register(config, {**source, 'ssh': 'different'})
            self.assertEqual(json.loads(config.read_text()), actual)

    def test_failed_probe_does_not_write_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'sources.json'
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(['--home', tmp, '--config', str(config)])
            self.assertFalse(config.exists())

    def test_check_does_not_register_or_restart(self):
        with patch('scripts.add_source.probe', return_value={'home': '/fixture', 'sqlite_home': '/fixture', 'threads': 1}), patch('scripts.add_source.register') as save, patch('scripts.add_source.reload_collector') as restart, contextlib.redirect_stdout(io.StringIO()):
            main(['--check'])
            save.assert_not_called()
            restart.assert_not_called()
