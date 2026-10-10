#!/usr/bin/env python3
"""Stdlib regression tests for encodings, append-layout input and reference recovery.

append_sources models PR #987's wire layout; it is NOT an upstream execution.
The separate reference/make_tweakcc_binary.cjs probe runs the exact pinned helpers.
No synthetic executable is executed by this test file.
"""
import contextlib
import hashlib
import io
import os
import random
import tempfile
import struct
import unittest

import bun_handler as h
from test_bun_handler import _module, build_bun_elf


def fixture(ms=52, count=3):
    modules = [
        _module('/$bunfs/root/cli', 'console.log("original");',
                bytecode=b'opaque-bytecode' * 13,
                origin='/$bunfs/root/src/entrypoints/cli.js', module_format=1),
        _module('/$bunfs/root/other.js', 'export let n=1;',
                bytecode=b'other-cache' * 17, module_format=1),
        _module('/$bunfs/root/asset.bin', b'\x00\x01\xff',
                module_format=0, loader=5, encoding=0),
    ]
    for i in range(count - 3):
        modules.append(_module(f'/$bunfs/root/extra{i}.js', 'export {};', module_format=1))
    return build_bun_elf(
        modules, module_struct_size=ms, flags=0x3ef,
        builtin_bytecode=[(7, b'builtin-cache' * 13)],
        bytecode_string_table=b'bytecode-string-table',
        module_info_string_table=b'module-info-string-table',
        startup_module_count=1, compile_argv=b'--smol',
        trailing_section={'flags': 0, 'size': 16, 'vaddr': 0})


def append_sources(data, sources):
    """Wire-format fixture builder retaining original offsets and dead copies."""
    img = h.BunImage(data)
    block = b''.join(source + b'\0' for _, source in sorted(sources.items()))
    align = h._required_file_shift_alignment(img.elf, img.bun_section['foff'] + img.bun_section['size'])
    block += b'\0' * (-len(block) % align)
    out = bytearray(img.blob[:img.offsets_off]) + block + img.blob[img.offsets_off:]
    cursor = img.offsets_off
    for index, source in sorted(sources.items()):
        record = img.modules_off + index * img.module_struct_size
        struct.pack_into('<II', out, record + 8, cursor, len(source))
        out[record + 16:record + img.module_struct_size - 4] = b'\0' * (img.module_struct_size - 20)
        out[record + img.module_struct_size - 4] = 0
        if img.source_hashes_off is not None:
            struct.pack_into('<I', out, img.source_hashes_off + index * 4, 0)
        cursor += len(source) + 1
    offsets = img.offsets_off + len(block)
    struct.pack_into('<Q', out, offsets, offsets)
    struct.pack_into('<I', out, offsets + 28, img.flags & ~h.BUN_FLAG_SOURCE_TEXT_CONTIGUOUS)
    return h._repack_blob_elf(data, out, img.section_header_size)


def mutate_blob(data, offset, value):
    img = h.BunImage(data)
    out = bytearray(data)
    start = img.bun_section['foff'] + img.section_header_size + offset
    out[start:start + len(value)] = value
    return bytes(out)


class InteropTests(unittest.TestCase):
    def setUp(self):
        self.original = fixture()
        self.image = h.BunImage(self.original)
        self.source = b'console.log("modified");'
        self.appended = append_sources(self.original, {0: self.source})

    def test_accepts_appended_encoding_zero(self):
        image = h.BunImage(self.appended)
        self.assertTrue(h.can_handle(self.appended))
        self.assertEqual(image.read_source(image.modules[0]), self.source)
        self.assertEqual(image.entrypoint_module()['index'], 0)
        self.assertEqual(image.modules[0]['bytecodeOriginPath'], (0, 0))

    def test_external_noop_byte_identity(self):
        self.assertEqual(h.repack_unchanged(self.appended), self.appended)
        self.assertEqual(h.repack_with_js(self.appended, h.extract_js(self.appended)), self.appended)

    def test_external_same_size_edit_does_not_append(self):
        digest, js = h.extract_js_with_digest(self.appended)
        changed = js.replace(b'modified', b'MODIFIED')
        out = h.repack_with_js(self.appended, changed, expected_sha256=digest)
        self.assertEqual(len(out), len(self.appended))
        self.assertEqual(h.extract_js(out), changed)
        for _ in range(5):
            out = h.repack_with_js(out, h.extract_js(out).replace(b'MODIFIED', b'modified'))
            out = h.repack_with_js(out, h.extract_js(out).replace(b'modified', b'MODIFIED'))
        self.assertEqual(len(out), len(self.appended))

    def test_edit_original_and_appended_source_together(self):
        js = h.extract_js(self.appended)
        modified = js.replace(b'modified', b'a much larger edit').replace(b'export let n=1;', b'export {};')
        out = h.repack_with_js(self.appended, modified)
        self.assertEqual(h.extract_js(out), modified)
        self.assertTrue(h.can_handle(out))
        self.assertEqual(h.repack_unchanged(out), out)

    def test_growing_and_shrinking_appended_unicode(self):
        for text in ('café🦆中', 'x', ''):
            with self.subTest(text=text):
                src = f'console.log("{text}");'.encode()
                out = h.repack_module_sources(self.appended, [(0, b'/$bunfs/root/cli', src)], expected_sha256=None)
                img = h.BunImage(out)
                self.assertEqual(img.read_source(img.modules[0]), src)
                self.assertTrue(h.can_handle(out))

    def test_utf16_noop_and_changed_storage(self):
        src = 'console.log("café🦆");'
        data = build_bun_elf([_module('/$bunfs/root/cli', src.encode('utf-16-le'), encoding=2, module_format=1)])
        self.assertEqual(h.extract_js(data), src.encode())
        self.assertEqual(h.repack_unchanged(data), data)
        self.assertEqual(h.repack_with_js(data, src.encode()), data)
        new = 'console.log("中");'.encode()
        out = h.repack_with_js(data, new)
        self.assertEqual(h.extract_js(out), new)
        image = h.BunImage(out)
        self.assertEqual(image.modules[0]['encoding'], 0)
        self.assertEqual(h.repack_unchanged(out), out)

    def test_latin1_is_not_misread_as_utf8(self):
        data = build_bun_elf([_module('/$bunfs/root/cli', b'console.log("caf\xe9");', module_format=1)])
        js = h.extract_js(data)
        self.assertEqual(js, 'console.log("café");'.encode())
        self.assertEqual(h.repack_with_js(data, js), data)

    def test_invalid_utf8_input_is_rejected(self):
        data = append_sources(self.original, {0: b'const a="\xff";'})
        self.assertFalse(h.can_handle(data))
        with self.assertRaises(h.BunFormatError):
            h.extract_js(data)

    def test_unpaired_utf16_rejected_without_replacement_characters(self):
        data = build_bun_elf([_module('/$bunfs/root/cli', b'\x00\xd8', encoding=2, module_format=1)])
        self.assertFalse(h.can_handle(data))

    def test_invalid_replacement_utf8_and_nul(self):
        for value in (b'\xff', b'const x=0;\0'):
            with self.subTest(value=value), self.assertRaises(h.BunFormatError):
                h.repack_module_sources(self.original, [(0, b'/$bunfs/root/cli', value)], expected_sha256=None)

    def test_source_digest_rejects_stale_binary(self):
        digest, js = h.extract_js_with_digest(self.original)
        with self.assertRaisesRegex(h.BunFormatError, 'SHA-256 mismatch'):
            h.repack_with_js(self.appended, js, expected_sha256=digest)
        with self.assertRaises(h.BunFormatError):
            h.repack_module_sources(self.appended, [], expected_sha256=digest)

    def test_digest_checks_precede_even_noop(self):
        with self.assertRaises(h.BunFormatError):
            h.repack_with_js(self.original, h.extract_js(self.original), expected_sha256='0' * 64)
        with self.assertRaises(h.BunFormatError):
            h.compact_with_reference(self.original, self.original, reference_sha256='0' * 64)

    def test_invalid_digest_syntax(self):
        for digest in ('', 'A' * 64, 'z' * 64, 10):
            with self.subTest(digest=digest), self.assertRaises(h.BunFormatError):
                h.repack_module_sources(self.original, [], expected_sha256=digest)

    def test_module_targets_are_explicit_and_unique(self):
        cases = [
            [(0, b'wrong', b'foo();')],
            [(0, b'/$bunfs/root/cli', b'foo();')] * 2,
            [(100, b'/$bunfs/root/cli', b'foo();')],
            [(True, b'/$bunfs/root/other.js', b'foo();')],
            [(2, b'/$bunfs/root/asset.bin', b'foo();')],
        ]
        for replacements in cases:
            with self.subTest(replacements=replacements), self.assertRaises(h.BunFormatError):
                h.repack_module_sources(self.original, replacements, expected_sha256=None)

    def test_integer_bytes_coercion_rejected(self):
        with self.assertRaises(TypeError): h.BunImage(1 << 62)
        with self.assertRaises(TypeError): h.split_extracted_js(1 << 62)
        with self.assertRaises(TypeError):
            h.repack_module_sources(self.original, [(0, b'/$bunfs/root/cli', 12)], expected_sha256=None)

    def test_snapshot_is_of_one_immutable_input(self):
        mutable = bytearray(self.original)
        digest, js = h.extract_js_with_digest(mutable)
        mutable[-1] ^= 1
        self.assertEqual(digest, hashlib.sha256(self.original).hexdigest())
        self.assertEqual(h.repack_with_js(self.original, js, expected_sha256=digest), self.original)
        with self.assertRaises(h.BunFormatError):
            h.repack_with_js(mutable, js, expected_sha256=digest)

    def test_posttable_source_cannot_overlap_optional_records(self):
        img = h.BunImage(self.appended)
        bad = mutate_blob(self.appended, img.modules_off + 8, struct.pack('<II', img.optional_tail_off, 1))
        self.assertFalse(h.can_handle(bad))

    def test_posttable_source_cannot_overlap_argv(self):
        img = h.BunImage(self.appended)
        bad = mutate_blob(self.appended, img.modules_off + 8, struct.pack('<II', img.compile_argv_off, img.compile_argv_len))
        self.assertFalse(h.can_handle(bad))

    def test_posttable_alias_rejected(self):
        data = append_sources(self.original, {0: self.source, 1: b'export let n=2;'})
        img = h.BunImage(data)
        data = mutate_blob(data, img.modules_off + img.module_struct_size + 8, struct.pack('<II', *img.modules[0]['contents']))
        self.assertFalse(h.can_handle(data))

    def test_false_source_contiguous_flag_rejected(self):
        img = h.BunImage(self.appended)
        bad = mutate_blob(self.appended, img.offsets_off + 28, struct.pack('<I', img.flags | 16))
        self.assertFalse(h.can_handle(bad))

    def test_duplicate_builtin_id_rejected(self):
        data = build_bun_elf([_module('/$bunfs/root/cli', b'foo();', module_format=1)],
                             flags=0x40, builtin_bytecode=[(1, b'cache-A'), (1, b'cache-B')])
        self.assertFalse(h.can_handle(data))

    def test_ambiguous_width_with_appended_source(self):
        for ms, count in ((36, 13), (52, 9)):
            with self.subTest(ms=ms):
                data = fixture(ms, count)
                appended = append_sources(data, {0: b'console.log("appended");'})
                self.assertEqual(h.BunImage(appended).module_struct_size, ms)
                self.assertTrue(h.can_handle(appended))
                self.assertEqual(h.repack_unchanged(appended), appended)

    def test_compact_multiple_append_generations(self):
        data = append_sources(self.appended, {0: b'console.log("current!");', 1: b'export let n=2;'})
        result = h.compact_with_reference(data, self.original)
        self.assertLess(len(result), len(data))
        self.assertEqual(h.extract_js(result), h.extract_js(data))
        expected = h.repack_module_sources(self.original,
            [(0, b'/$bunfs/root/cli', b'console.log("current!");'),
             (1, b'/$bunfs/root/other.js', b'export let n=2;')], expected_sha256=None)
        self.assertEqual(result, expected)
        self.assertEqual(h.compact_with_reference(result, self.original), result)

    def test_compaction_original_and_reverted_source(self):
        self.assertEqual(h.compact_with_reference(self.original, self.original), self.original)
        reverted = append_sources(self.appended, {0: self.image.read_source(self.image.modules[0])})
        self.assertEqual(h.compact_with_reference(reverted, self.original), self.original)

    def test_compact_unicode_then_repeat(self):
        data = append_sources(self.original, {0: 'console.log("very long 🦆 café 字");'.encode()})
        out = h.compact_with_reference(data, self.original)
        self.assertEqual(h.extract_js(out), h.extract_js(data))
        self.assertEqual(h.compact_with_reference(out, self.original), out)

    def test_compact_empty_source(self):
        data = append_sources(self.original, {0: b''})
        out = h.compact_with_reference(data, self.original)
        self.assertEqual(h.extract_js(out), h.extract_js(data))
        self.assertLess(len(out), len(data))

    def test_compact_36_byte_record(self):
        data = fixture(36)
        out = append_sources(data, {0: b'console.log("different!");'})
        compacted = h.compact_with_reference(out, data)
        self.assertEqual(h.extract_js(compacted), h.extract_js(out))

    def test_compaction_rejects_wrong_original(self):
        wrong = fixture(52, 4)
        with self.assertRaises(h.BunFormatError): h.compact_with_reference(self.appended, wrong)

    def test_compaction_rejects_native_change(self):
        bad = bytearray(self.appended); bad[24] ^= 1  # ELF entrypoint
        with self.assertRaisesRegex(h.BunFormatError, 'native executable'):
            h.compact_with_reference(bad, self.original)

    def test_compaction_rejects_non_bun_tail_change(self):
        img = h.BunImage(self.appended)
        tail = img.elf.section('.trail')
        bad = bytearray(self.appended); bad[tail['foff']] ^= 1
        with self.assertRaisesRegex(h.BunFormatError, 'non-Bun tail'):
            h.compact_with_reference(bad, self.original)

    def test_compaction_rejects_original_asset_and_unreferenced_prefix_changes(self):
        img = h.BunImage(self.appended)
        for offset in (0, img.modules[2]['contents'][0], img.builtin_bytecode[0]['bytes'][0]):
            bad = mutate_blob(self.appended, offset, bytes([img.blob[offset] ^ 1]))
            with self.subTest(offset=offset), self.assertRaisesRegex(h.BunFormatError, 'Bun prefix'):
                h.compact_with_reference(bad, self.original)

    def test_compaction_rejects_policy_flag_change(self):
        img = h.BunImage(self.appended)
        bad = mutate_blob(self.appended, img.offsets_off + 28, struct.pack('<I', img.flags ^ 1))
        with self.assertRaisesRegex(h.BunFormatError, 'policy'):
            h.compact_with_reference(bad, self.original)

    def test_compaction_rejects_appended_source_with_active_cache(self):
        img = h.BunImage(self.appended)
        bad = mutate_blob(self.appended, img.modules_off + 24, struct.pack('<II', *self.image.modules[0]['bytecode']))
        with self.assertRaisesRegex(h.BunFormatError, 'retains compiled'):
            h.compact_with_reference(bad, self.original)

    def test_compaction_rejects_changed_unmodified_hash(self):
        img = h.BunImage(self.appended)
        bad = mutate_blob(self.appended, img.source_hashes_off + 4, struct.pack('<I', 0))
        with self.assertRaisesRegex(h.BunFormatError, 'changed source hash'):
            h.compact_with_reference(bad, self.original)

    def test_reference_must_not_itself_be_append_layout(self):
        twice = append_sources(self.appended, {0: b'console.log("another!");'})
        with self.assertRaisesRegex(h.BunFormatError, 'original pre-table'):
            h.compact_with_reference(twice, self.appended)

    def test_utf16_selftest_uses_physical_storage_delta(self):
        text = 'console.log("(Claude Code) café");' + '/*' + 'long '*100 + '*/'
        data = build_bun_elf([_module('/$bunfs/root/cli', text.encode('utf-16-le'), encoding=2, module_format=1)])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'fixture')
            with open(path, 'wb') as stream:
                stream.write(data)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(h._selftest(path), 0)
            self.assertIn('storage delta -', output.getvalue())

    def test_exclusive_output_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, 'result')
            h._write_new_executable(output, b'complete bytes')
            with open(output, 'rb') as stream:
                self.assertEqual(stream.read(), b'complete bytes')
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                h._write_new_executable(output, b'new bytes')
            with open(output, 'rb') as stream:
                self.assertEqual(stream.read(), b'complete bytes')
            self.assertEqual(os.listdir(directory), ['result'])

    def test_output_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, 'target')
            output = os.path.join(directory, 'result')
            with open(target, 'wb') as stream: stream.write(b'keep')
            os.symlink(target, output)
            with self.assertRaises(FileExistsError): h._write_new_executable(output, b'changed')
            with open(target, 'rb') as stream: self.assertEqual(stream.read(), b'keep')
            self.assertEqual(sorted(os.listdir(directory)), ['result', 'target'])

    def test_compact_cli_new_output_and_existing_path_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            current, reference, output = [os.path.join(directory, name) for name in ('current', 'reference', 'output')]
            for path, data in ((current, self.appended), (reference, self.original)):
                with open(path, 'wb') as stream: stream.write(data)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(h.main(['compact', current, reference, output]), 0)
            with open(output, 'rb') as stream:
                self.assertEqual(h.extract_js(stream.read()), h.extract_js(self.appended))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(h.main(['compact', current, reference, reference]), 1)
            with open(reference, 'rb') as stream: self.assertEqual(stream.read(), self.original)

    def test_compaction_digest_checks_both_inputs(self):
        digest = hashlib.sha256(self.appended).hexdigest()
        reference_digest = hashlib.sha256(self.original).hexdigest()
        self.assertLess(len(h.compact_with_reference(self.appended, self.original,
            expected_sha256=digest, reference_sha256=reference_digest)), len(self.appended))
        for kwargs in ({'expected_sha256': '0'*64}, {'reference_sha256': '0'*64}):
            with self.subTest(kwargs=kwargs), self.assertRaises(h.BunFormatError):
                h.compact_with_reference(self.appended, self.original, **kwargs)

    def test_compaction_does_not_increase_size_for_large_replacement(self):
        source = b'console.log("large");/*' + b'x'*5000 + b'*/'
        appended = append_sources(self.original, {0: source})
        out = h.compact_with_reference(appended, self.original)
        self.assertLessEqual(len(out), len(appended))
        self.assertEqual(h.extract_js(out), h.extract_js(appended))
        self.assertEqual(h.compact_with_reference(out, self.original), out)

    def test_utf16_odd_length_and_unpaired_surrogate_refused(self):
        for content in (b'a', b'\x00\xd8'):
            with self.subTest(content=content):
                data = build_bun_elf([_module('/$bunfs/root/cli', content, encoding=2, module_format=1)])
                self.assertFalse(h.can_handle(data))

    def test_seeded_unicode_append_edit_compact_properties(self):
        rng = random.Random(263987)
        characters = 'abcDEF123 café🦆中🙂e\u0301'
        for case in range(200):
            text = ''.join(rng.choice(characters) for _ in range(rng.randrange(100)))
            source = ('console.log("' + text + '");').encode()
            once = append_sources(self.original, {0: source})
            twice = append_sources(once, {0: source + b'/*second*/', 1: b'export let n=77;'})
            digest, extracted = h.extract_js_with_digest(twice)
            changed = extracted.replace(b'/*second*/', b'/*THIRD!*/')
            edited = h.repack_with_js(twice, changed, expected_sha256=digest)
            with self.subTest(case=case):
                self.assertEqual(len(edited), len(twice))
                self.assertEqual(h.extract_js(edited), changed)
                self.assertEqual(h.repack_unchanged(edited), edited)
                result = h.compact_with_reference(edited, self.original)
                self.assertLessEqual(len(result), len(edited))
                self.assertEqual(h.extract_js(result), changed)
                self.assertEqual(h.compact_with_reference(result, self.original), result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
