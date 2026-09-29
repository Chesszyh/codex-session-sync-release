#!/usr/bin/env python3
"""Run pinned candidate probes on generated fixtures; no production home is read."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from fixtures import generate

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".m0"


def run(command, name, *, cwd=ROOT, env=None):
    with (WORK / "evidence" / (name + ".log")).open("w") as log:
        subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)


def assess(case, row):
    text = row.get("text", "")
    missing = [token for token in case["expected_text"] if token not in text]
    unexpected = [token for token in case["forbidden_text"] if token in text]
    warning = None
    if case["expected_warning"]:
        if case["expected_warning"] == "invalid JSON":
            warning = "supported" if row.get("malformed", 0) > 0 or row.get("error") else "unsupported"
        elif case["expected_warning"] == "incomplete tail":
            warning = "untested"
        else:
            warning = "unsupported" if not row.get("error") else "untested"
    return {"case": case["name"], "transcript": "unsupported" if missing or unexpected or row.get("error") else ("untested" if not case["expected_text"] else "supported"),
            "missing": missing, "unexpected": unexpected, "messages": row.get("messages"),
            "discovered_files": row.get("discovered"), "error": row.get("error"),
            "warning_coverage": warning,
            "metadata_title": "untested" if case["name"] == "metadata-only" else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="Fetch pinned source and build dependencies into .m0")
    parser.add_argument("--output", type=Path, default=WORK / "evidence" / "capabilities.json")
    args = parser.parse_args()
    (WORK / "evidence").mkdir(parents=True, exist_ok=True)
    sources = json.loads((ROOT / "m0/sources.json").read_text())
    for name in ("agentsview", "happier"):
        path = WORK / "upstream" / name
        if args.prepare and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "clone", "--no-checkout", "--filter=blob:none", sources[name]["url"], str(path)], name + "-clone")
            run(["git", "checkout", "--detach", sources[name]["commit"]], name + "-checkout", cwd=path)
        revision = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
        if revision != sources[name]["commit"]:
            raise RuntimeError(name + " revision differs from m0/sources.json")
    if args.prepare:
        run(["npm", "install", "--prefix", str(WORK / "tools"), "--ignore-scripts", "--no-audit", "--no-fund", "esbuild@" + sources["esbuild"]], "npm-install")
        run(["make", "pricing-snapshot"], "agentsview-prepare", cwd=WORK / "upstream/agentsview")
    cases = generate(WORK / "fixtures")
    shutil.copyfile(ROOT / "m0/agentsview_probe_test.go", WORK / "upstream/agentsview/internal/parser/m0_probe_test.go")
    run([str(WORK / "tools/node_modules/.bin/esbuild"), "m0/happier_probe.ts", "--bundle", "--platform=node", "--format=esm", "--alias:@=./.m0/upstream/happier/apps/cli/src", "--outfile=.m0/happier-probe.mjs"], "happier-build")
    before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in (WORK / "fixtures").rglob("*") if p.is_file()}
    results = {}
    for name in ("agentsview", "happier"):
        env = os.environ.copy()
        env.update(M0_CASES=str(WORK / "fixtures/cases.json"), M0_OUTPUT=str(WORK / "evidence" / (name + "-results.json")))
        if name == "agentsview":
            run(["go", "test", "./internal/parser", "-run", "^TestM0SharedFixtures$", "-count=1"], "agentsview-probe", cwd=WORK / "upstream/agentsview", env=env)
        else:
            run(["node", str(WORK / "happier-probe.mjs")], "happier-probe", env=env)
        rows = json.loads(Path(env["M0_OUTPUT"]).read_text())
        by_name = {row["name"]: row for row in rows}
        if len(rows) != len(cases) or set(by_name) != {c["name"] for c in cases}:
            raise RuntimeError(name + " did not return each fixture exactly once")
        results[name] = [assess(case, by_name[case["name"]]) for case in cases]
    after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in (WORK / "fixtures").rglob("*") if p.is_file()}
    if before != after:
        raise RuntimeError("Candidate changed fixture files")
    follow = json.loads((WORK / "evidence/happier-follow.json").read_text())
    results["happier_follow"] = {
        "legacy_append": "supported" if len(follow["after"]["items"]) == 1 and "M0_FOLLOW" in json.dumps(follow["after"]) else "unsupported",
        "repeat_cursor": "supported" if not follow["repeat"]["items"] else "unsupported",
        "atomic_replacement_detection": "supported" if follow["replaced"].get("truncationReason") == "source_discontinuity" else "unsupported"}
    av_follow = json.loads((WORK / "evidence/agentsview-follow.json").read_text())
    results["agentsview_follow"] = {"legacy_append": "supported" if av_follow["append_found_once"] else "unsupported",
                                    "repeat_cursor": "supported" if av_follow["repeat_count"] == 0 else "unsupported"}
    report = {"sources": sources, "fixture_count": len(cases), "fixture_size_mtime_unchanged": True,
              "scope": "Candidate parser/discovery/Direct pager probes; not deployed UI, server persistence, native restore or GB benchmark", "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"fixture_count": len(cases), "report": str(args.output), "completed": True}))


if __name__ == "__main__":
    main()
