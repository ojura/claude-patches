#!/usr/bin/env python3
"""Validate current-main tweakcc Bun graph helpers against a Windows PE input.

This executes the pinned TypeScript `rebuildBunData()` and `buildSectionData()`
helpers. `node-lief` is a throwing sentinel and is never called. The resulting
exact section bytes are embedded using bun_handler's PE writer, then both
recovery policies are checked. This is component/helper evidence, not a full
`tweakcc repackPE()` / LIEF file-writer test.

Usage:
    python validate_current_tweakcc_pe_component.py CLAUDE_EXE NEW_WORK_DIR REPORT_JSON
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import bun_handler as h

BASE = Path(__file__).resolve().parent
COMMIT = 'a806febde332beb471b07d53c403d8e196142da3'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    binary = Path(argv[0]).resolve()
    work = Path(argv[1]).resolve()
    report = Path(argv[2]).resolve()
    if not binary.is_file():
        print(f'not a file: {binary}', file=sys.stderr)
        return 2
    try:
        work.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f'work directory already exists: {work}', file=sys.stderr)
        return 2
    if report.exists():
        print(f'report already exists: {report}', file=sys.stderr)
        return 2
    report.parent.mkdir(parents=True, exist_ok=True)

    original = binary.read_bytes()
    image = h.BunImage(original)
    if image.container_kind != 'PE':
        raise AssertionError('input is not supported PE')
    result = {
        'scope': __doc__,
        'tweakcc_commit': COMMIT,
        'input': str(binary),
        'input_sha256': sha(original),
        'input_bytes': len(original),
        'cases': [],
    }
    ts_path = os.environ.get(
        'TYPESCRIPT_PATH',
        '/opt/nvm/versions/node/v22.16.0/lib/node_modules/typescript/lib/typescript.js',
    )
    env = dict(os.environ, TYPESCRIPT_PATH=ts_path)

    for mode in ('literal', 'unicode', 'shrink', 'dependency', 'noop'):
        section = work / f'{mode}.section'
        producer = subprocess.run(
            ['node', str(BASE / 'reference/make_tweakcc_main_pe_section.cjs'),
             str(binary), str(section), mode],
            env=env, capture_output=True, text=True, timeout=90,
        )
        if producer.returncode != 0:
            raise AssertionError((mode, producer.stdout, producer.stderr))
        producer_record = json.loads(producer.stdout)
        wrapped = section.read_bytes()
        damaged = h._repack_section_pe_parts(original, (wrapped,))
        if h.can_handle(damaged):
            raise AssertionError('current-main rebuilt graph unexpectedly passed ordinary validation')
        extracted = h.extract_tweakcc_js(damaged)
        reference = h.recover_tweakcc(damaged, original)
        source_only = h.recover_tweakcc(damaged)
        if not h.can_handle(reference) or not h.can_handle(source_only):
            raise AssertionError('recovered output is not patchable')
        if h.repack_unchanged(reference) != reference or h.repack_unchanged(source_only) != source_only:
            raise AssertionError('recovered output no-op is not byte-identical')
        if not (h.extract_js(reference) == extracted == h.extract_js(source_only)):
            raise AssertionError('recovered effective source differs')
        result['cases'].append({
            'mode': mode,
            'producer': producer_record,
            'component_pe_bytes': len(damaged),
            'component_pe_sha256': sha(damaged),
            'ordinary_can_handle': False,
            'extracted_bytes': len(extracted),
            'reference_recovery_bytes': len(reference),
            'source_only_bytes': len(source_only),
            'reference_roundtrip': True,
            'source_only_roundtrip': True,
        })
        section.unlink()

    if sha(binary.read_bytes()) != result['input_sha256']:
        raise AssertionError('validator modified the input')
    result['input_unchanged'] = True
    result['all_pass'] = True
    report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
