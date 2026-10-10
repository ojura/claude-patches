#!/usr/bin/env python3
"""Two focused mutation controls for the newly retained compaction contracts.

Only disposable Python variants and synthetic data are involved. No native
mutant or malformed synthetic executable is run. A detector must be an explicit
unittest assertion with no unexpected test error; process failure alone is not
credited. These two controls are not added to the old 274-mutant score.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

MUTATIONS = [
    ('accept_unknown_lineage',
     'raise BunFormatError("binary is neither a verified append lineage nor canonical replay")',
     'return data',
     'test_exact_review_reproduction_has_typed_refusal'),
    ('replay_when_not_smaller',
     'return candidate if len(candidate) < len(data) else data',
     'return candidate',
     'test_equal_size_replay_is_not_implicit_cache_restoration'),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    source = (root / 'bun_handler.py').read_text()
    runner = '''import hashlib,json,sys,unittest
sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2])
import bun_handler
assert hashlib.sha256(open(bun_handler.__file__,"rb").read()).hexdigest()==sys.argv[4]
suite=unittest.defaultTestLoader.loadTestsFromName("test_review_v5_3.ReviewedCompactionBoundaryTests."+sys.argv[3])
r=unittest.TextTestRunner(verbosity=2).run(suite)
print("RESULT="+json.dumps({"tests":r.testsRun,"failures":[[t.id(),s] for t,s in r.failures],"errors":[[t.id(),s] for t,s in r.errors]}))
'''
    cases = []
    with tempfile.TemporaryDirectory(prefix='bun-reviewed-boundary-mutations-') as td:
        for name, before, after, test in MUTATIONS:
            if source.count(before) != 1:
                raise RuntimeError(f'nonunique mutation anchor: {name}')
            changed = source.replace(before, after)
            compile(changed, name, 'exec')
            directory = Path(td) / name
            directory.mkdir()
            (directory / 'bun_handler.py').write_text(changed)
            digest = hashlib.sha256(changed.encode()).hexdigest()
            run = subprocess.run([sys.executable, '-c', runner, str(directory), str(root), test, digest],
                                 capture_output=True, text=True, timeout=30,
                                 env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            lines = [line for line in run.stdout.splitlines() if line.startswith('RESULT=')]
            if len(lines) != 1:
                raise RuntimeError((name, run.returncode, run.stdout, run.stderr))
            observed = json.loads(lines[0][len('RESULT='):])
            detected = (run.returncode == 0 and observed['tests'] == 1
                        and bool(observed['failures']) and not observed['errors'])
            cases.append({'mutation': name, 'before': before, 'after': after,
                          'mutant_sha256': digest, 'detector': test,
                          'assertion_detected': detected, **observed})
    report = {'production_sha256': hashlib.sha256(source.encode()).hexdigest(),
              'native_mutants_executed': False, 'cases': cases,
              'all_assertion_detected': all(case['assertion_detected'] for case in cases)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'cases': len(cases), 'all_assertion_detected': report['all_assertion_detected']}))
    return 0 if report['all_assertion_detected'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
