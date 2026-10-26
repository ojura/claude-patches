"""Linked bytecode descriptors and overlapping entry views (graph flag 0x2000).

The fixtures use byte strings as payloads. Tests inspect their bytes and pointers;
they do not execute the synthetic binaries. Descriptor construction is independent
of the parser under test.
"""
import itertools
import struct
import unittest

import bun_handler as h
from test_bun_handler import _module, _build_bun_blob, build_bun_elf
from test_bun_windows import build_bun_pe
from test_bun_interop import append_sources
from test_graph_flags_278 import add_records, OPTIONS_BYTES
from test_tweakcc_recovery import rebuild_fixture

LINKED = 0x2000
PAYLOAD = bytes(range(256)) * 2
ENDS = (32, 160, 160, 255, 400, len(PAYLOAD))
ENTRIES = (7, 131)
BUILTIN_ENTRY = 29


def linked_fixture(*, pe=False, width=52, wrapper=8, prelink=True, options=True):
    modules = [
        _module('/$bunfs/root/cli', b'console.log("original");',
                bytecode=b'old-main', module_format=1),
        _module('/$bunfs/root/helper.js', b'export const answer = 42;',
                bytecode=b'old-helper', module_format=1),
        _module('/$bunfs/root/asset.txt', b'asset', module_format=0, loader=5),
    ]
    kwargs = dict(flags=0x3ef, builtin_bytecode=[(1, b'old-builtin')],
                  bytecode_string_table=b'strings', module_info_string_table=b'info',
                  startup_module_count=1, compile_argv=b'--smol')
    if pe:
        for module in modules:
            module['name'] = module['name'].replace(b'/$bunfs/root/', b'B:/~BUN/root/')
        base = build_bun_pe(cert=False, section_header_size=wrapper)
        image = h.BunImage(base)
        blob = _build_bun_blob(modules, width, image._runtime_blob_vaddr(),
                               trailing_pad=False, entry_point_id=0, **kwargs)
        data = h._repack_blob(base, blob, wrapper)
    else:
        data = build_bun_elf(modules, module_struct_size=width,
                             section_header_size=wrapper, **kwargs)
    data, _ = add_records(data, prelink=prelink,
                          options=OPTIONS_BYTES if options else None)
    image = h.BunImage(data)
    padding = (-(image._runtime_blob_vaddr() + image.modules_off)) % 128
    block = b'\0' * padding + PAYLOAD
    payload_off = image.modules_off + padding
    record = struct.pack('<II6I', payload_off, len(PAYLOAD), *ENDS)
    table_off = image.modules_off + len(block)
    record_off = image.optional_tail_end + len(block)
    blob = (bytearray(image.blob[:image.modules_off]) + block
            + image.blob[image.modules_off:image.optional_tail_end] + record
            + image.blob[image.optional_tail_end:image.offsets_off])
    for index, entry in enumerate(ENTRIES):
        struct.pack_into('<II', blob, table_off + index * width + 24,
                         payload_off + entry, len(PAYLOAD) - entry)
    builtin_record = image.builtin_bytecode[0]['pointer_record_off'] + len(block)
    struct.pack_into('<II', blob, builtin_record,
                     payload_off + BUILTIN_ENTRY, len(PAYLOAD) - BUILTIN_ENTRY)
    alignment = h._required_container_shift_alignment(
        image.container, image.bun_section['foff'] + image.bun_section['size'])
    delta = len(block) + len(record)
    blob.extend(b'\0' * (-delta % alignment))
    offsets_off = len(blob)
    blob.extend(struct.pack('<QIIIIII', offsets_off, table_off, image.modules_len,
                            image.entry_point_id, image.compile_argv_off + delta,
                            image.compile_argv_len, image.flags | LINKED))
    blob.extend(image.blob[image.offsets_off + 32:])
    return h._repack_blob(data, blob, wrapper)


def mutate(data, offset, fmt, *values):
    image = h.BunImage(data)
    out = bytearray(data)
    struct.pack_into(fmt, out, image.bun_section['foff'] + image.section_header_size + offset,
                     *values)
    return bytes(out)


def replace_source(data, index, source):
    image = h.BunImage(data)
    return h.repack_module_sources(data, [(index, image.modules[index]['name_bytes'], source)],
                                   expected_sha256=None)


class LinkedBytecodeTests(unittest.TestCase):
    def setUp(self):
        self.data = linked_fixture()
        self.image = h.BunImage(self.data)

    def assert_payload(self, image):
        off, size = image.linked_bytecode_payload
        self.assertEqual(bytes(image.blob[off:off + size]), PAYLOAD)
        self.assertEqual(image.linked_bytecode_region_ends, ENDS)
        self.assertEqual((image._runtime_blob_vaddr() + off) % 128, 0)
        for m in image.modules:
            start, length = m['bytecode']
            if length:
                self.assertEqual(start - off, ENTRIES[m['index']])
                self.assertEqual(start + length, off + size)
        for entry in image.builtin_bytecode:
            start, length = entry['bytes']
            self.assertEqual(start - off, BUILTIN_ENTRY)
            self.assertEqual(start + length, off + size)

    def test_flag_and_descriptor_sizes(self):
        self.assertEqual(h.BUN_FLAG_HAS_LINKED_BYTECODE_PAYLOAD, LINKED)
        self.assertEqual(h.BUN_LINKED_BYTECODE_REGION_COUNT, 6)
        self.assertEqual(h.BUN_LINKED_BYTECODE_RECORD_SIZE, 32)
        self.assertEqual(h.BUN_KNOWN_FLAGS, 0x3fff)

    def test_overlapping_unaligned_entries_belong_to_one_payload(self):
        self.assert_payload(self.image)
        self.assertNotEqual((self.image._runtime_blob_vaddr()
                             + self.image.modules[0]['bytecode'][0]) % 128, 0)
        regions = [r for r in self.image.regions if 'bytecode' in r[2]]
        self.assertEqual({r[2] for r in regions},
                         {'linked-bytecode payload', 'bytecode-string-table'})

    def test_noop_preserves_every_byte(self):
        self.assertEqual(h.repack_unchanged(self.data), self.data)
        self.assertEqual(h.repack_with_js(self.data, h.extract_js(self.data)), self.data)
        self.assertEqual(h.repack_module_sources(self.data, [], expected_sha256=None), self.data)

    def test_descriptor_follows_options_and_precedes_argv(self):
        image = self.image
        self.assertEqual(image.linked_bytecode_record_off, image.runtime_options_record_off + 8)
        self.assertEqual(image.optional_tail_end, image.linked_bytecode_record_off + 32)
        self.assertEqual(image.compile_argv_off, image.optional_tail_end)

    def test_edit_moves_descriptor_after_disabled_prelinked_map(self):
        result = replace_source(self.data, 0, b'console.log("ORIGINAL");')
        after = h.BunImage(result)
        off = after.prelinked_module_graph_record_off
        self.assertEqual(struct.unpack_from('<III', after.blob, off), (0, 0, 0))
        self.assertEqual(bytes(after.blob[off + 12:off + 20]), OPTIONS_BYTES)
        self.assertEqual(after.linked_bytecode_record_off, off + 20)
        self.assertEqual(struct.unpack_from('<II6I', after.blob, off + 20),
                         (*after.linked_bytecode_payload, *ENDS))
        self.assertEqual(len(result), len(self.data))
        self.assertEqual(after.modules[0]['bytecode'], (0, 0))
        self.assertEqual(after.source_hash(0), 0)
        self.assertEqual(after.source_hash(1), self.image.source_hash(1))
        self.assert_payload(after)

    def test_growth_shrink_unicode_empty_and_regrowth(self):
        data = self.data
        for source in (b'x' * 901, b'x', 'čaj 🦆'.encode(), b'', b'export {};'):
            with self.subTest(source=source[:30]):
                data = replace_source(data, 0, source)
                after = h.BunImage(data)
                self.assertEqual(after.read_source(after.modules[0]), source)
                self.assertEqual(after.modules[0]['bytecode'], (0, 0))
                self.assertEqual(after.runtime_options_bytes, OPTIONS_BYTES)
                self.assertEqual(bytes(after.blob[after.compile_argv_off:
                                                 after.compile_argv_off + after.compile_argv_len]),
                                 b'--smol')
                self.assert_payload(after)
                self.assertEqual(h.repack_unchanged(data), data)

    def test_editing_second_module_preserves_first_entry(self):
        result = replace_source(self.data, 1, b'export const answer = 99;')
        after = h.BunImage(result)
        self.assert_payload(after)
        self.assertEqual(after.modules[1]['bytecode'], (0, 0))
        self.assertEqual(after.modules[0]['bytecode'][1], self.image.modules[0]['bytecode'][1])
        self.assertEqual(after.source_hash(0), self.image.source_hash(0))

    def test_all_module_caches_can_be_detached_while_builtins_remain(self):
        result = replace_source(replace_source(self.data, 0, b'export {};'),
                                1, b'export {};')
        after = h.BunImage(result)
        self.assertTrue(all(m['bytecode'] == (0, 0) for m in after.modules))
        self.assert_payload(after)

    def test_repacking_is_deterministic(self):
        source = b'export const answer = 99;' * 31
        self.assertEqual(replace_source(self.data, 1, source),
                         replace_source(self.data, 1, source))

    def test_payload_alone_gets_alignment_padding(self):
        result = replace_source(self.data, 0, b'console.log("original");/*123*/')
        after = h.BunImage(result)
        self.assertNotEqual(after.linked_bytecode_payload[0], self.image.linked_bytecode_payload[0])
        self.assert_payload(after)
        self.assertEqual(after.modules[1]['bytecode'][1], self.image.modules[1]['bytecode'][1])

    def test_container_record_width_and_optional_record_combinations(self):
        for pe, width, wrapper, prelink, options in itertools.product(
                (False, True), (36, 52), (4, 8), (False, True), (False, True)):
            with self.subTest(pe=pe, width=width, wrapper=wrapper,
                              prelink=prelink, options=options):
                data = linked_fixture(pe=pe, width=width, wrapper=wrapper,
                                      prelink=prelink, options=options)
                self.assertEqual(h.repack_unchanged(data), data)
                for source in (b'longer source' * 31, b'', b'x'):
                    data = replace_source(data, 0, source)
                    image = h.BunImage(data)
                    self.assert_payload(image)
                    self.assertEqual(image.read_source(image.modules[0]), source)
                    self.assertEqual(image.runtime_options_bytes, OPTIONS_BYTES if options else b'')
                    self.assertEqual(h.repack_unchanged(data), data)

    def test_repeated_region_ends_are_valid(self):
        self.assertEqual(self.image.linked_bytecode_region_ends[1:3], (160, 160))

    def test_unsorted_or_wrong_final_end_is_rejected(self):
        for ends in ((32, 160, 159, 255, 400, 512), (32, 160, 160, 255, 400, 511),
                     (32, 160, 160, 255, 400, 513)):
            bad = mutate(self.data, self.image.linked_bytecode_record_off + 8, '<6I', *ends)
            with self.assertRaisesRegex(h.BunFormatError, 'linked-bytecode region ends'):
                h.BunImage(bad)

    def test_empty_payload_is_rejected(self):
        bad = mutate(self.data, self.image.linked_bytecode_record_off, '<8I', *([0] * 8))
        with self.assertRaisesRegex(h.BunFormatError, 'linked-bytecode payload is empty'):
            h.BunImage(bad)

    def test_payload_outside_blob_is_rejected(self):
        bad = mutate(self.data, self.image.linked_bytecode_record_off, '<II',
                     len(self.image.blob) - 1, len(PAYLOAD))
        with self.assertRaisesRegex(h.BunFormatError, 'linked-bytecode payload.*outside'):
            h.BunImage(bad)

    def test_payload_alignment_is_checked(self):
        off, length = self.image.linked_bytecode_payload
        bad = mutate(self.data, self.image.linked_bytecode_record_off, '<II', off - 1, length)
        with self.assertRaisesRegex(h.BunFormatError, 'linked-bytecode payload.*128-byte aligned'):
            h.BunImage(bad)

    def test_payload_cannot_intersect_module_table(self):
        bad = mutate(self.data, self.image.linked_bytecode_record_off, '<II6I',
                     self.image.modules_off, 52, 0, 0, 0, 0, 0, 52)
        with self.assertRaisesRegex(h.BunFormatError, 'linked-bytecode payload.*module table'):
            h.BunImage(bad)

    def test_payload_cannot_overlap_other_regions(self):
        # Point an asset's contents into the payload and add its terminator there.
        start = self.image.linked_bytecode_payload[0]
        out = mutate(self.data, self.image.modules_off + 2 * 52 + 8, '<II', start, 1)
        out = bytearray(out)
        out[self.image.bun_section['foff'] + self.image.section_header_size + start + 1] = 0
        with self.assertRaisesRegex(h.BunFormatError, 'embedded regions overlap'):
            h.BunImage(bytes(out))

    def test_module_and_builtin_entries_must_be_payload_suffixes(self):
        start, size = self.image.linked_bytecode_payload
        for record in (self.image.modules_off + 24,
                       self.image.builtin_bytecode[0]['pointer_record_off']):
            for off, length in ((start - 1, size + 1), (start + size, 1),
                                (start + 7, size - 8), (start + 7, size - 6)):
                with self.subTest(record=record, off=off, length=length):
                    bad = mutate(self.data, record, '<II', off, length)
                    with self.assertRaisesRegex(h.BunFormatError, 'linked-bytecode payload'):
                        h.BunImage(bad)

    def test_zero_length_builtin_entries_are_refused(self):
        start, size = self.image.linked_bytecode_payload
        record = self.image.builtin_bytecode[0]['pointer_record_off']
        for off in (0, start - 1, start, start + 400, start + size, start + size + 1):
            with self.subTest(offset=off):
                bad = mutate(self.data, record, '<II', off, 0)
                with self.assertRaisesRegex(h.BunFormatError, 'builtin-bytecode.*linked-bytecode'):
                    h.BunImage(bad)

    def test_module_can_compile_from_source(self):
        start, size = self.image.linked_bytecode_payload
        for off in (0, start + 400, start + size):
            with self.subTest(offset=off):
                data = mutate(self.data, self.image.modules_off + 24, '<II', off, 0)
                self.assertEqual(h.BunImage(data).modules[0]['bytecode'], (off, 0))
                # A source edit leaves the other zero-length module inactive,
                # even when growth moves its placeholder inside the payload.
                result = replace_source(data, 1, b'x' * 280)
                self.assertEqual(h.BunImage(result).modules[0]['bytecode'], (off, 0))

    def test_descriptor_cannot_extend_past_offsets(self):
        # Moving the offsets structure into the descriptor is invalid even if
        # the file has enough trailing bytes to read 32 bytes there.
        blob = bytearray(self.image.blob[:self.image.linked_bytecode_record_off + 31])
        at = len(blob)
        blob.extend(struct.pack('<QIIIIII', at, self.image.modules_off, self.image.modules_len,
                                self.image.entry_point_id, 0, 0, self.image.flags))
        blob.extend(h.BUN_TRAILER)
        bad = h._repack_blob(self.data, blob, self.image.section_header_size)
        with self.assertRaisesRegex(h.BunFormatError, 'linked-bytecode payload record runs past'):
            h.BunImage(bad)

    def test_private_bytecode_edits_are_refused(self):
        with self.assertRaisesRegex(h.BunFormatError, 'cannot edit an individual linked-bytecode'):
            h._apply_blob_edits(self.image, {(0, 'bytecode'): b'changed'})
        original = self.image.read_field(self.image.modules[0], 'bytecode')
        self.assertEqual(h._apply_blob_edits(self.image, {(0, 'bytecode'): original}),
                         self.image.blob)

    def test_unknown_next_flag_is_refused(self):
        bad = mutate(self.data, self.image.offsets_off + 28, '<I', self.image.flags | 0x4000)
        with self.assertRaisesRegex(NotImplementedError, 'unknown flag bits 0x4000'):
            h.BunImage(bad)

    def test_append_edit_compact_preserves_linked_payload(self):
        discarded = append_sources(self.data, {0: b'a' * 4000})
        appended = append_sources(discarded, {0: b'a' * 1000})
        edited = replace_source(appended, 0, b'b' * 1200)
        self.assert_payload(h.BunImage(edited))
        compacted = h.compact_with_reference(edited, self.data)
        self.assert_payload(h.BunImage(compacted))
        self.assertEqual(h.extract_js(compacted), h.extract_js(edited))
        self.assertLess(len(compacted), len(edited))
        self.assertEqual(h.compact_with_reference(compacted, self.data), compacted)

    def test_append_without_disabling_prelink_can_compact(self):
        appended = append_sources(self.data, {0: b'a' * 1000})
        compacted = h.compact_with_reference(appended, self.data)
        self.assertEqual(h.extract_js(compacted), h.extract_js(appended))
        self.assert_payload(h.BunImage(compacted))

    def test_compaction_rejects_changed_region_boundaries(self):
        appended = append_sources(self.data, {0: b'a' * 1000})
        edited = replace_source(appended, 0, b'b' * 1200)
        image = h.BunImage(edited)
        bad = mutate(edited, image.linked_bytecode_record_off + 8, '<I', 31)
        with self.assertRaisesRegex(h.BunFormatError, 'reference linked-bytecode descriptor differs'):
            h.compact_with_reference(bad, self.data)

    def test_compaction_rejects_changed_payload_bytes(self):
        appended = append_sources(self.data, {0: b'a' * 1000})
        edited = replace_source(appended, 0, b'b' * 1200)
        image = h.BunImage(edited)
        bad = mutate(edited, image.linked_bytecode_payload[0], '<B', 255)
        with self.assertRaisesRegex(h.BunFormatError, 'reference Bun prefix differs'):
            h.compact_with_reference(bad, self.data)

    def test_compaction_checks_unused_old_descriptor_bytes(self):
        appended = append_sources(self.data, {0: b'a' * 1000})
        edited = replace_source(appended, 0, b'b' * 1200)
        # Disabling the two-entry prelinked map moves the linked descriptor
        # eight bytes earlier. Its old last word remains outside the new record.
        old_last_word = self.image.linked_bytecode_record_off + 28
        bad = mutate(edited, old_last_word, '<I', 123)
        with self.assertRaisesRegex(h.BunFormatError, 'reference Bun prefix differs'):
            h.compact_with_reference(bad, self.data)

    def test_reference_recovery_restores_linked_record(self):
        broken = rebuild_fixture(self.data, {(0, 'contents'): b'edited source'})
        recovered = h.recover_tweakcc(broken, self.data)
        image = h.BunImage(recovered)
        self.assertEqual(image.read_source(image.modules[0]), b'edited source')
        self.assertEqual(image.modules[0]['bytecode'], (0, 0))
        self.assert_payload(image)

    def test_recovery_without_reference_drops_linked_flag(self):
        data = linked_fixture(options=False)
        broken = rebuild_fixture(data, {(0, 'contents'): b'edited source'})
        image = h.BunImage(h.recover_tweakcc(broken))
        self.assertFalse(image.flags & LINKED)
        self.assertEqual(image.linked_bytecode_payload, (0, 0))
        self.assertTrue(all(m['bytecode'] == (0, 0) for m in image.modules))


if __name__ == '__main__':
    unittest.main()
