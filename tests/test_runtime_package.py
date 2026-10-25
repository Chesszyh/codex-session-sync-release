import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class RuntimePackageTests(unittest.TestCase):
    def test_restore_imports_with_only_product_modules_and_adapter_configuration(self):
        repo = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            shutil.copytree(repo / "replica", root / "replica", ignore=shutil.ignore_patterns("__pycache__"))
            config = root / "adapters/official"
            config.mkdir(parents=True)
            for name in ("source.json", "restore-schema.json"):
                shutil.copyfile(repo / "adapters/official" / name, config / name)
            result = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys; sys.path.insert(0, " + json.dumps(str(root)) + "); from replica.restore import restore"],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
