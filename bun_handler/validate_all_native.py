#!/usr/bin/env python3
"""Run every strict gate serially against explicit native executables.

The report directory must not exist. Originals are never overwritten. Run without
Python's -O option: the underlying native validators use assertion checks.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
STRICT_GATES = "gate1,gate2,gate3,gate5,gate6,gate7,gate8,gate9"


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binaries", nargs="+", type=Path)
    parser.add_argument("--report-dir", type=Path, default=Path("validation-run"))
    args = parser.parse_args()
    paths = [path.resolve() for path in args.binaries]
    for path in paths:
        if not path.is_file():
            parser.error(f"native binary does not exist or is not a file: {path}")
    try:
        args.report_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        parser.error(f"cannot create new report directory: {exc}")
    environment = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CLAUDE_PFG_STRICT_GATES": STRICT_GATES,
        "CLAUDE_PFG_STRICT_GATES_WAIVE": "",
    }
    results = []
    for index, path in enumerate(paths):
        name = path.name.replace("(", "_").replace(")", "_")
        log = args.report_dir / f"strict_{index}_{name}.log"
        before = digest(path)
        start = time.monotonic()
        # Do not let an inherited discovery path override the requested input.
        environment["CLAUDE_NATIVE_BINARY"] = str(path)
        try:
            with log.open("x") as stream:
                run = subprocess.run(
                    [sys.executable, str(BASE / "test_bun_handler.py"), str(path)],
                    stdout=stream, stderr=subprocess.STDOUT,
                    env=environment, timeout=240,
                )
            status = run.returncode
            error = None
        except (OSError, subprocess.TimeoutExpired) as exc:
            status, error = 1, str(exc)
        text = log.read_text() if log.exists() else ""
        after = digest(path)
        success = (status == 0 and "SUITE PASS" in text
                   and "SKIP" not in text and ": FAIL" not in text
                   and before == after)
        item = {
            "binary": path.name, "input_sha256": before,
            "input_unchanged": before == after,
            "exit_status": status, "seconds": round(time.monotonic() - start, 3),
            "strict_all_pass": success, "log": log.name,
        }
        if error is not None:
            item["error"] = error
        results.append(item)
        print(json.dumps(item), flush=True)
        (args.report_dir / "native_matrix.json").write_text(
            json.dumps(results, indent=2) + "\n")
        if not success:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
