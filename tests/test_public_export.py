import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.prepare_public import export


class PublicExportTests(unittest.TestCase):
    def test_only_selected_committed_files_export_without_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'private'
            repo.mkdir()
            def git(*args):
                subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
            git('init')
            (repo / 'config').mkdir()
            (repo / 'config/public-files.json').write_text(json.dumps(['README.md']))
            (repo / 'README.md').write_text('public version')
            (repo / 'private.txt').write_text('private fixture')
            git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'private message fixture')
            (repo / 'README.md').write_text('uncommitted private change')
            output = root / 'candidate'
            export(repo, 'HEAD', output)
            self.assertEqual([p.name for p in output.iterdir()], ['README.md'])
            self.assertEqual((output / 'README.md').read_text(), 'public version')
            with self.assertRaises(FileExistsError):
                export(repo, 'HEAD', output)
