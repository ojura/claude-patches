#!/usr/bin/env python3
"""Execute disposable outputs only. Requires explicit original and work paths.

The work directory must already contain tweakcc-once, tweakcc-twice and
 tweakcc-unicode produced by reference/make_tweakcc_binary.cjs.
This probe never executes synthetic malformed input or edits the originals.
"""
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import bun_handler as h


def run(data, path, stdout_contains, stderr_contains=None):
    path.write_bytes(data)
    path.chmod(0o700)
    result = subprocess.run([str(path), '--version'], capture_output=True,
                            encoding='utf-8', timeout=90)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert stdout_contains in result.stdout, result.stdout
    if stderr_contains:
        assert stderr_contains in result.stderr, result.stderr
    tools = {}
    for executable, args in [('readelf', ['-aW']), ('objdump', ['-h'])]:
        check = subprocess.run([executable, *args, str(path)], capture_output=True, timeout=60)
        assert check.returncode == 0 and not check.stderr, (executable, check.stderr)
        tools[executable] = {'exit_status': check.returncode, 'stderr': check.stderr.decode()}
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'stdout': result.stdout.strip(), 'stderr': result.stderr.strip(),
            'elf_tools': tools}


def payload_check(before, after):
    a, b = h.BunImage(before), h.BunImage(after)
    assert len(a.modules) == len(b.modules)
    changed = []
    source_ids = {m['index'] for m in a.source_modules()}
    for am, bm in zip(a.modules, b.modules):
        if am['index'] in source_ids and a.read_source(am) != b.read_source(bm):
            changed.append(am['index'])
            for field in ('bytecode', 'moduleInfo', 'sourcemap'):
                assert bm[field] == (0, 0)
            assert b.source_hash(bm) == 0
        else:
            for field in ('name', 'contents', 'bytecode', 'moduleInfo', 'sourcemap', 'bytecodeOriginPath'):
                assert a.read_field(am, field) == b.read_field(bm, field), (am['index'], field)
            assert a.source_hash(am) == b.source_hash(bm)
    for ae, be in zip(a.builtin_bytecode, b.builtin_bytecode):
        assert ae['id'] == be['id']
        ao, al = ae['bytes']; bo, bl = be['bytes']
        assert al == bl and a.blob[ao:ao+al] == b.blob[bo:bo+bl]
    assert len(a.builtin_bytecode) == len(b.builtin_bytecode)
    for field in ('bytecode_string_table', 'module_info_string_table'):
        ao, al = getattr(a, field); bo, bl = getattr(b, field)
        assert al == bl and a.blob[ao:ao+al] == b.blob[bo:bo+bl]
    return {'changed_modules': changed, 'builtin_payloads_preserved': len(a.builtin_bytecode),
            'both_shared_tables_preserved': True, 'unchanged_module_payloads_preserved': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('original', type=Path)
    parser.add_argument('work', type=Path)
    parser.add_argument('report', type=Path)
    parser.add_argument('--legacy', type=Path)
    args = parser.parse_args()
    start = time.monotonic()
    results = {'handler_sha256': hashlib.sha256(Path(h.__file__).read_bytes()).hexdigest(),
               'upstream_commit': 'db7f183751603d856b589ba9666355ea1a3a8c8f', 'cases': {}}
    def save(label, record):
        results['cases'][label] = record
        args.report.write_text(json.dumps(results, indent=2, ensure_ascii=False)+'\n')
        print(label, json.dumps(record, ensure_ascii=False), flush=True)
    original = args.original.read_bytes()
    once = (args.work/'tweakcc-once').read_bytes()
    twice = (args.work/'tweakcc-twice').read_bytes()
    save('upstream_two_append_generations', {
        'original_bytes': len(original), 'once_bytes': len(once), 'twice_bytes': len(twice),
        'growth_bytes': len(twice)-len(original), 'accepted_by_handler': h.can_handle(twice),
        'no_op_byte_identical': h.repack_unchanged(twice) == twice})
    del once
    digest, extracted = h.extract_js_with_digest(twice)
    patched = extracted.replace(b'(CLAUDE Code)', b'(OURNEW Code)')
    assert patched != extracted and len(patched) == len(extracted)
    edited = h.repack_with_js(twice, patched, expected_sha256=digest)
    assert len(edited) == len(twice) and h.extract_js(edited) == patched
    record = run(edited, args.work/'ours-after-tweakcc', '(OURNEW Code)')
    record['growth_bytes'] = len(edited)-len(twice)
    record['payload_audit'] = payload_check(twice, edited)
    save('surgical_repatch_of_upstream_output', record)
    del twice, extracted, patched; gc.collect()
    output = h.compact_with_reference(edited, original,
        expected_sha256=hashlib.sha256(edited).hexdigest(),
        reference_sha256=hashlib.sha256(original).hexdigest())
    assert len(output) == len(original)
    assert h.extract_js(output) == h.extract_js(edited)
    assert h.compact_with_reference(output, original) == output
    record = run(output, args.work/'compacted', '(OURNEW Code)')
    record.update(recovered_bytes=len(edited)-len(output), sources_preserved=True, idempotent=True)
    save('reference_assisted_compaction', record)
    del edited, output; gc.collect()
    unicode_input = (args.work/'tweakcc-unicode').read_bytes()
    extracted = h.extract_js(unicode_input)
    assert '🦆 café'.encode() in extracted
    assert h.repack_with_js(unicode_input, extracted) == unicode_input
    patched = extracted.replace('🦆 café'.encode(), '🐧 čaj!'.encode())
    output = h.repack_with_js(unicode_input, patched)
    assert h.extract_js(output) == patched
    record = run(output, args.work/'unicode-repatched', '(Claude Code)', 'interop:🐧 čaj!')
    record.update(no_op_preserves_upstream_storage=True,
                  growth_bytes=len(output)-len(unicode_input),
                  payload_audit=payload_check(unicode_input, output))
    save('upstream_utf8_unicode_repatch', record)
    del unicode_input, extracted, patched, output; gc.collect()
    # Make a runtime-backed UTF-16LE input by appending to the native graph.
    # This is a deliberately derived format fixture, not a vendor-shipped file.
    image = h.BunImage(original)
    index = next(m['index'] for m in image.source_modules()
                 if b'}.BUILD_REF_NAME){return""}' in image.read_source(m))
    module = image.modules[index]
    source = image.read_source(module).decode('utf-8')
    source += '\nconst __v3Utf16 = await Promise.resolve("🦆 UTF16"); export { __v3Utf16 }; console.error(__v3Utf16);\n'
    encoded = source.encode('utf-16-le')
    import struct
    align = (-(image._runtime_blob_vaddr() + image.offsets_off)) % 2
    block = b'\0'*align + encoded + b'\0\0'
    file_alignment = h._required_file_shift_alignment(image.elf, image.bun_section['foff'] + image.bun_section['size'])
    block += b'\0' * (-len(block) % file_alignment)
    blob = bytearray(image.blob[:image.offsets_off]) + block + image.blob[image.offsets_off:]
    at = image.modules_off + index*image.module_struct_size
    struct.pack_into('<II', blob, at+8, image.offsets_off+align, len(encoded))
    blob[at+16:at+48] = b'\0'*32
    blob[at+48] = 2
    struct.pack_into('<I', blob, image.source_hashes_off+index*4, 0)
    struct.pack_into('<Q', blob, image.offsets_off+len(block), image.offsets_off+len(block))
    struct.pack_into('<I', blob, image.offsets_off+len(block)+28, image.flags & ~16)
    derived = h._repack_blob_elf(original, blob, image.section_header_size)
    del blob, block, image; gc.collect()
    assert h.repack_unchanged(derived) == derived
    extracted = h.extract_js(derived)
    assert h.repack_with_js(derived, extracted) == derived
    before_run = run(derived, args.work/'derived-utf16', '(Claude Code)', '🦆 UTF16')
    edited = h.repack_with_js(derived, extracted.replace('🦆 UTF16'.encode(), '🐧 UTF8!'.encode()))
    assert h.extract_js(edited) == extracted.replace('🦆 UTF16'.encode(), '🐧 UTF8!'.encode())
    assert h.BunImage(edited).modules[index]['encoding'] == 0
    record = run(edited, args.work/'derived-utf16-edited', '(Claude Code)', '🐧 UTF8!')
    record.update(derived_fixture=True, before=before_run, no_op_byte_identity=True,
                  edited_encoding=0, stored_file_delta=len(edited)-len(derived))
    save('native_runtime_utf16_decode_edit_utf8_write', record)
    del derived, extracted, edited, original; gc.collect()
    if args.legacy:
        original = args.legacy.read_bytes()
        js = h.extract_js(original)
        edited = h.repack_with_js(original, js.replace(b'(Claude Code)', '(Claude 🦆)'.encode()))
        assert h.extract_js(edited) == js.replace(b'(Claude Code)', '(Claude 🦆)'.encode())
        record = run(edited, args.work/'legacy-unicode', '2.1.234 (Claude 🦆)')
        record['source_modules'] = len(h.BunImage(edited).source_modules())
        record['payload_audit'] = payload_check(original, edited)
        save('legacy_cjs_unicode_write', record)
    results['all_pass'] = True
    results['seconds'] = round(time.monotonic()-start,3)
    args.report.write_text(json.dumps(results, indent=2, ensure_ascii=False)+'\n')
    print('ALL PASS', flush=True)

if __name__ == '__main__': main()
