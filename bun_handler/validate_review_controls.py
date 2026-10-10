#!/usr/bin/env python3
"""Synthetic-only negative controls for the two v5.2 review fixes.

Creates isolated copies, removes each added guard separately, and requires the
retained review TestCase to fail. No synthetic executable is ever executed and
no production source is edited. Run: python validate_review_controls.py REPORT
"""
from pathlib import Path
import json, os, shutil, subprocess, sys, tempfile
ROOT=Path(__file__).resolve().parent
GUARDS={
    'reference_width':('''    # Wrapper width is part of the reference identity, not resize bookkeeping.
    if current.section_header_size != reference.section_header_size:
        raise BunFormatError("reference PE Bun wrapper width differs")
''',6),
    'non_bun_diagnostic':('''        if not bun:
            raise NotImplementedError("PE has no Bun .bun section")
''',1),
}

def main():
    if len(sys.argv)!=2:
        raise SystemExit('usage: validate_review_controls.py NEW_REPORT.json')
    report=Path(sys.argv[1]);results=[]
    for name,(guard,expected_failures) in GUARDS.items():
        with tempfile.TemporaryDirectory(prefix='bun-review-control-') as td:
            work=Path(td)
            for filename in ('bun_handler.py','test_review_v5_2.py','test_bun_windows.py','test_bun_handler.py'):
                shutil.copyfile(ROOT/filename,work/filename)
            text=(work/'bun_handler.py').read_text();assert text.count(guard)==1
            (work/'bun_handler.py').write_text(text.replace(guard,'',1))
            run=subprocess.run([sys.executable,'-m','unittest','-v','test_review_v5_2'],cwd=work,
                capture_output=True,text=True,timeout=30,
                env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
            output=run.stdout+run.stderr
            detected=(run.returncode==1 and 'Ran 3 tests' in output
                      and f'FAILED (failures={expected_failures})' in output)
            results.append({'removed_guard':name,'regression_detected':detected,
                            'expected_failing_subcases':expected_failures,
                            'exit_status':run.returncode,'output':output})
            assert detected,output
    report.parent.mkdir(parents=True,exist_ok=True)
    with report.open('x') as stream:json.dump({'all_detected':True,'controls':results},stream,indent=2);stream.write('\n')
    print('Both negative controls detected the restored regressions.')

if __name__=='__main__':main()
