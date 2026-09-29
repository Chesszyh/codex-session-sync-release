#!/usr/bin/env python3
"""Build the pinned official decoder and isolated projection importer."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SOURCE = ROOT / ".m0/upstream/codex-official"
SPEC = json.loads((HERE / "source.json").read_text())
BRIDGE = """impl LocalThreadStore {
    pub async fn replica_materialize(&self, rollout_id: ThreadId, path: &std::path::Path) -> ThreadStoreResult<()> {
        thread_history_materialization::materialize_to_sqlite(self, rollout_id, path).await
    }
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Download the pinned official source if absent")
    args = parser.parse_args()
    marker = SOURCE / ".replica-source-revision"
    if not SOURCE.exists():
        if not args.download:
            parser.error("source absent; use --download")
        SOURCE.mkdir(parents=True)
        archive = SOURCE.parent / ("codex-" + SPEC["revision"] + ".tar.gz")
        with archive.open("wb") as output:
            subprocess.run(["gh", "api", "repos/" + SPEC["repository"] + "/tarball/" + SPEC["revision"]], stdout=output, check=True)
        subprocess.run(["tar", "-xzf", str(archive), "--strip-components=1", "-C", str(SOURCE)], check=True)
        marker.write_text(SPEC["revision"] + "\n")
    if not marker.is_file() or marker.read_text().strip() != SPEC["revision"]:
        raise RuntimeError("source revision marker does not match adapters/official/source.json")
    module = SOURCE / "codex-rs/thread-store/src/local/mod.rs"
    content = module.read_text()
    if BRIDGE not in content:
        needle = "impl LocalThreadStore {\n"
        if content.count(needle) != 1 or "replica_materialize" in content:
            raise RuntimeError("official bridge insertion point changed")
        module.write_text(content.replace(needle, BRIDGE, 1))
    for package, example in (("app-server-protocol", "replica_read"), ("thread-store", "replica_materialize"), ("thread-store", "replica_restore")):
        dest = SOURCE / "codex-rs" / package / "examples" / (example + ".rs")
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(HERE / (example + ".rs"), dest)
        env = os.environ.copy()
        env.update(CARGO_TARGET_DIR=str(ROOT / ".m0/official-target"), CARGO_BUILD_JOBS="4", CARGO_HTTP_MULTIPLEXING="false")
        subprocess.run(["cargo", "+" + SPEC["rust_toolchain"], "build", "--manifest-path", str(SOURCE / "codex-rs/Cargo.toml"), "-p", "codex-" + package, "--example", example, "--locked"], env=env, check=True)


if __name__ == "__main__":
    main()
