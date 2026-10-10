#!/usr/bin/env python3
"""Run an unchanged submitted review probe against this source tree.

The property probe uses a private diagnostic directory. The runtime probe needs
an explicit Linux 2.1.278 input and a new report directory, and executes derived
programs through the real Bun runtime. Node/TypeScript are test dependencies only.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='probe', required=True)
    prop = commands.add_parser('property')
    prop.add_argument('--fixtures', type=int, default=1200)
    runtime = commands.add_parser('runtime')
    runtime.add_argument('binary', type=Path)
    runtime.add_argument('report', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}
    if args.probe == 'property':
        if args.fixtures < 1:
            parser.error('--fixtures must be positive')
        with tempfile.TemporaryDirectory(prefix='bun-review-property-') as td:
            temp = Path(td)
            script = temp / 'new_record_property.py'
            shutil.copyfile(root / 'review_probes/new_record_property.py', script)
            (temp / 'evidence').mkdir()
            run = subprocess.run([sys.executable, str(script), str(root),
                                  '--fixtures', str(args.fixtures)], env=env)
            if run.returncode:
                # Retain both the unchanged probe and any emitted fixtures. A
                # failed run must not erase the evidence needed to reproduce it.
                retained = Path(tempfile.mkdtemp(prefix='review-property-failure-', dir=Path.cwd()))
                shutil.copytree(temp, retained, dirs_exist_ok=True)
                print(f'Failure evidence retained at {retained}', file=sys.stderr)
            return run.returncode
    binary, report = args.binary.resolve(), args.report.resolve()
    if not binary.is_file():
        parser.error('runtime binary does not exist')
    if report.exists():
        parser.error('runtime report path already exists')
    if not report.parent.is_dir():
        parser.error('runtime report parent directory does not exist')
    return subprocess.run([sys.executable, str(root / 'review_probes/extra_278_runtime.py'),
                           str(root), str(binary), str(report)], env=env).returncode


if __name__ == '__main__':
    raise SystemExit(main())
