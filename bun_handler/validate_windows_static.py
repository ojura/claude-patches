#!/usr/bin/env python3
"""Static PE validation for one supplied Windows Claude executable.

Usage:
    python validate_windows_static.py CLAUDE_EXE NEW_REPORT_DIR

No generated PE is executed. The input is hashed before and after. The report
separates structural evidence from the Wine runtime validator.
"""

import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile

import bun_handler as h


def digest(data):
    return hashlib.sha256(data).hexdigest()


def alignment_bad(image):
    base = image._runtime_blob_vaddr()
    bad = []
    for m in image.modules:
        off, length = m['bytecode']
        if length and (base + off) % h.BUN_BYTECODE_ALIGNMENT:
            bad.append(f"module[{m['index']}].bytecode")
        if m['encoding'] == 2 and m['contents'][1] and (base + m['contents'][0]) % 2:
            bad.append(f"module[{m['index']}].contents")
    for entry in image.builtin_bytecode:
        off, length = entry['bytes']
        if length and (base + off) % h.BUN_BYTECODE_ALIGNMENT:
            bad.append(f"builtin[{entry['index']}]")
    if image.bytecode_string_table[1]:
        if (base + image.bytecode_string_table[0]) % h.BUN_BYTECODE_ALIGNMENT:
            bad.append('bytecode-string-table')
    return bad


def payload_bytes(image, pointer):
    off, length = pointer
    return bytes(image.blob[off:off + length])


def replace_module(extracted, target_index, new_contents):
    records = h.split_extracted_js(extracted)
    if len(records) == 1 and records[0][0] is None:
        return new_contents
    out = bytearray(h._MULTI_MODULE_HEADER)
    found = False
    for index, name, contents in records:
        if index == target_index:
            contents = new_contents
            found = True
        out += h._MULTI_MODULE_MARKER
        out += str(index).encode('ascii') + b' ' + name.hex().encode('ascii') + b'\n'
        out += contents
    if not found:
        raise AssertionError(f'module {target_index} not in extraction')
    return bytes(out)


def changed_indices(before, after):
    records = h.changed_js_modules(before, after)
    if records and records[0][0] is None:
        return None
    return [x[0] for x in records]


def objdump_check(data, directory, label):
    tool = shutil.which('objdump')
    if not tool:
        return {'available': False}
    path = directory / f'{label}.exe'
    path.write_bytes(data)
    try:
        run = subprocess.run([tool, '-x', str(path)], capture_output=True, timeout=60)
        return {
            'available': True,
            'returncode': run.returncode,
            'stderr': run.stderr.decode('utf-8', 'replace'),
        }
    finally:
        path.unlink(missing_ok=True)


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    binary = Path(argv[0]).resolve()
    report_dir = Path(argv[1]).resolve()
    if not binary.is_file():
        print(f'not a file: {binary}', file=sys.stderr)
        return 2
    try:
        report_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f'report directory already exists: {report_dir}', file=sys.stderr)
        return 2

    data = binary.read_bytes()
    before_sha = digest(data)
    image = h.BunImage(data)
    if image.container_kind != 'PE':
        raise AssertionError('input is not supported PE')
    pe, bun = image.pe, image.bun_section
    source = h.extract_js(data)
    result = {
        'scope': __doc__,
        'input': {
            'path': str(binary), 'bytes': len(data), 'sha256': before_sha,
            'format': h.detect_format(data),
        },
        'pe': {
            'sections': pe.number_of_sections,
            'file_alignment': pe.file_alignment,
            'section_alignment': pe.section_alignment,
            'size_of_image': pe.size_of_image,
            'size_of_initialized_data': pe.size_of_initialized_data,
            'certificate_offset': pe.certificate_off,
            'certificate_size': pe.certificate_size,
            'bun': bun,
        },
        'graph': {
            'records': len(image.modules),
            'source_modules': len(image.source_modules()),
            'extracted_bytes': len(source),
            'entrypoint': image.entrypoint_module()['name_str'],
            'module_struct_size': image.module_struct_size,
            'section_header_size': image.section_header_size,
            'flags': hex(image.flags),
            'builtin_count': len(image.builtin_bytecode),
            'active_alignment_bad': alignment_bad(image),
        },
    }
    assert not result['graph']['active_alignment_bad']

    noop = h.repack_unchanged(data)
    assert noop == data
    result['noop_identical'] = True

    literal = source.replace(b'(Claude Code)', b'(ClauDe Code)')
    assert literal != source and len(literal) == len(source)
    equal = h.repack_with_js(data, literal)
    eq = h.BunImage(equal)
    assert h.extract_js(equal) == literal
    assert h.repack_unchanged(equal) == equal
    cidx = changed_indices(source, literal)
    assert cidx
    assert all(eq.source_hash(i) == 0 and eq.modules[i]['bytecode'] == (0, 0)
               and eq.modules[i]['moduleInfo'] == (0, 0)
               and eq.modules[i]['sourcemap'] == (0, 0) for i in cidx)
    result['equal_literal'] = {
        'bytes': len(equal), 'file_delta': len(equal) - len(data),
        'sha256': digest(equal), 'certificate_size': eq.pe.certificate_size,
        'checksum': struct.unpack_from('<I', equal, eq.pe.checksum_off)[0],
        'changed_modules': cidx, 'changed_cache_detached': True,
        'source_roundtrip': True, 'noop_after_identical': True,
        'alignment_bad': alignment_bad(eq),
        'objdump': objdump_check(equal, report_dir, 'equal'),
    }
    assert not result['equal_literal']['alignment_bad']

    repeat_source = literal.replace(b'(ClauDe Code)', b'(CLAUDE Code)')
    repeat = h.repack_with_js(equal, repeat_source)
    ri = h.BunImage(repeat)
    assert len(repeat) == len(equal)
    assert h.extract_js(repeat) == repeat_source
    result['repeat_equal_literal'] = {
        'bytes': len(repeat), 'delta_from_first_edit': 0,
        'certificate_size': ri.pe.certificate_size, 'source_roundtrip': True,
        'objdump': objdump_check(repeat, report_dir, 'repeat'),
    }

    small_source = source.replace(b'(Claude Code)', b'(Claude Code)12345678')
    small = h.repack_with_js(data, small_source)
    si = h.BunImage(small)
    assert h.extract_js(small) == small_source
    result['growth_small'] = {
        'source_delta': len(small_source)-len(source), 'bytes': len(small),
        'file_delta': len(small)-len(data), 'bun_vsize': si.bun_section['vsize'],
        'bun_raw_size': si.bun_section['raw_size'],
        'certificate_size': si.pe.certificate_size,
        'alignment_bad': alignment_bad(si), 'source_roundtrip': True,
        'objdump': objdump_check(small, report_dir, 'grow-small'),
    }
    assert not result['growth_small']['alignment_bad']

    # Force crossing multiple FileAlignment blocks while retaining valid source.
    records = h.split_extracted_js(source)
    target = next((r for r in records if b'(Claude Code)' in r[2]), None)
    assert target is not None
    large_module = target[2] + (b'\n/*' + b'X' * 1495 + b'*/')
    large_source = replace_module(source, target[0], large_module)
    large = h.repack_with_js(data, large_source)
    li = h.BunImage(large)
    assert h.extract_js(large) == large_source
    raw_delta = li.bun_section['raw_size'] - bun['raw_size']
    result['growth_large'] = {
        'source_delta': len(large_source)-len(source), 'bytes': len(large),
        'file_delta': len(large)-len(data), 'raw_delta': raw_delta,
        'bun_vsize': li.bun_section['vsize'], 'bun_raw_size': li.bun_section['raw_size'],
        'size_init_delta': li.pe.size_of_initialized_data - pe.size_of_initialized_data,
        'certificate_size': li.pe.certificate_size,
        'alignment_bad': alignment_bad(li), 'source_roundtrip': True,
        'objdump': objdump_check(large, report_dir, 'grow-large'),
    }
    assert raw_delta > 0 and raw_delta % pe.file_alignment == 0
    assert result['growth_large']['size_init_delta'] == raw_delta
    assert not result['growth_large']['alignment_bad']

    shrink_source = source.replace(b'(Claude Code)', b'(CC)')
    shrink = h.repack_with_js(data, shrink_source)
    shi = h.BunImage(shrink)
    assert h.extract_js(shrink) == shrink_source
    result['shrink_valid'] = {
        'source_delta': len(shrink_source)-len(source), 'bytes': len(shrink),
        'file_delta': len(shrink)-len(data), 'certificate_size': shi.pe.certificate_size,
        'alignment_bad': alignment_bad(shi), 'source_roundtrip': True,
        'objdump': objdump_check(shrink, report_dir, 'shrink'),
    }
    assert not result['shrink_valid']['alignment_bad']

    # Opaque optional payloads survive the large resize byte-for-byte.
    large_before = h.BunImage(data); large_after = h.BunImage(large)
    assert len(large_before.builtin_bytecode) == len(large_after.builtin_bytecode)
    builtins_ok = all(
        payload_bytes(large_before, a['bytes']) == payload_bytes(large_after, b['bytes'])
        for a, b in zip(large_before.builtin_bytecode, large_after.builtin_bytecode)
    )
    bst_ok = payload_bytes(large_before, large_before.bytecode_string_table) == payload_bytes(large_after, large_after.bytecode_string_table)
    mist_ok = payload_bytes(large_before, large_before.module_info_string_table) == payload_bytes(large_after, large_after.module_info_string_table)
    assert builtins_ok and bst_ok and mist_ok
    result['opaque_preservation_large_growth'] = {
        'builtin_count': len(large_before.builtin_bytecode),
        'builtins_identical': builtins_ok,
        'bytecode_string_table_identical': bst_ok,
        'module_info_string_table_identical': mist_ok,
    }

    # Equal-length control-flow source proof (static only on this host).
    anchor = b'}.BUILD_REF_NAME){return""}'
    control_records = [r for r in h.split_extracted_js(source) if anchor in r[2]]
    assert len(control_records) == 1
    c = control_records[0]
    changed = c[2].replace(anchor, b'}.BUILD_REF_NAME){return 0}', 1)
    assert len(changed) == len(c[2])
    control_source = replace_module(source, c[0], changed)
    control = h.repack_with_js(data, control_source); ci = h.BunImage(control)
    assert h.extract_js(control) == control_source
    cidx2 = changed_indices(source, control_source)
    result['control_flow_static'] = {
        'source_delta': 0, 'changed_modules': cidx2,
        'source_roundtrip': True,
        'cache_detached': all(ci.modules[i]['bytecode'] == (0,0) and ci.modules[i]['moduleInfo'] == (0,0) for i in cidx2),
    }
    assert result['control_flow_static']['cache_detached']

    # Dependency-changing source proof (runtime expectation belongs to Wine test).
    missing = b'B:/~BUN/root/pfg-definitely-missing.js'
    dep_stmt = b'import"' + missing + b'";'
    dep_module = c
    dep_changed = dep_stmt + dep_module[2]
    dep_source = replace_module(source, dep_module[0], dep_changed)
    dependency = h.repack_with_js(data, dep_source); di = h.BunImage(dependency)
    assert h.extract_js(dependency) == dep_source
    didx = changed_indices(source, dep_source)
    result['dependency_static'] = {
        'source_roundtrip': True, 'certificate_size': di.pe.certificate_size,
        'cache_detached': all(di.modules[i]['bytecode'] == (0,0) and di.modules[i]['moduleInfo'] == (0,0) for i in didx),
    }
    assert result['dependency_static']['cache_detached']

    after_sha = digest(binary.read_bytes())
    result['input']['sha256_after'] = after_sha
    result['input']['unchanged'] = after_sha == before_sha
    assert result['input']['unchanged']
    result['all_pass'] = True
    (report_dir / 'windows_static_native.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
