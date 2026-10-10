"""Observe behavioral results before downstream validators can obscure a fault.

No production guard is disabled. Sources are real immutable byte inputs, and
writer spies assert the actual buffer before forwarding it to the real writer.
Unexpected exceptions are not converted into assertion failures. Synthetic
native files are never executed.
"""
from collections import UserString, deque
import hashlib
import struct
import unittest
from unittest.mock import patch
from types import MappingProxyType

import bun_handler as h
from test_bun_handler import _module, build_bun_elf
from test_bun_interop import fixture as graph_fixture
from test_pe_backend import fixture as pe_fixture
from test_tweakcc_recovery import rebuild_fixture


class ResultOracles(unittest.TestCase):
    def test_digest_type_is_not_replaced_by_string_value_equality(self):
        data = graph_fixture()
        digest = hashlib.sha256(data).hexdigest()
        source = h.extract_js(data)
        # UserString is a normal stdlib wrapper, not a malicious equality stub.
        # It compares equal to a string but is not the documented argument type.
        wrapped = UserString(digest)
        self.assertEqual(wrapped, digest)
        self.assertNotIsInstance(wrapped, str)
        self.assertEqual(h.repack_with_js(data, source, expected_sha256=digest), data)
        with self.assertRaises(h.BunFormatError):
            h.repack_with_js(data, source, expected_sha256=wrapped)

    def test_reversed_pe_table_order_preserves_extents_but_is_unsupported(self):
        data = pe_fixture(signed=False)
        pe = h.Pe64(data)
        self.assertEqual(len(pe.sections), 2)
        first, second = pe.sections
        self.assertEqual((first['name'], second['name']), ('.text', '.bun'))
        out = bytearray(data)
        a, b = first['hdr_off'], second['hdr_off']
        out[a:a + 40], out[b:b + 40] = data[b:b + 40], data[a:a + 40]
        # Only the two descriptors move. No raw or mapped extent is changed.
        self.assertEqual(bytes(out[pe.headers_size:]), data[pe.headers_size:])
        self.assertFalse(h.can_handle(bytes(out)))
        with self.assertRaises(h.BunFormatError):
            h.Pe64(bytes(out))

    def test_latin1_source_that_also_decodes_as_utf8(self):
        stored = b'const label="\xc3\xa9";'
        data = build_bun_elf([
            _module('/$bunfs/root/cli', stored, encoding=1, module_format=1)
        ])
        expected = stored.decode('latin-1').encode('utf-8')
        self.assertNotEqual(expected, stored.decode('utf-8').encode('utf-8'))
        self.assertEqual(h.extract_js(data), expected)
        self.assertEqual(h.repack_with_js(data, expected), data)

    def test_graph_splice_respects_the_outer_elf_alignment_contract(self):
        # No surviving cache region may add its own alignment and accidentally
        # hide a missing outer-file-alignment adjustment.
        data = build_bun_elf([
            _module('/$bunfs/root/cli', b'const x=1;', module_format=1)
        ])
        image = h.BunImage(data)
        module = image.source_modules()[0]
        old = image.read_source(module)
        alignment = h._required_container_shift_alignment(
            image.container, image.bun_section['foff'] + image.bun_section['size'])
        self.assertGreater(alignment, 1)
        replacement = old + b'\n//x\n'
        self.assertNotEqual((len(replacement) - len(old)) % alignment, 0)
        new_blob = h._apply_blob_edits(image, {(module['index'], 'contents'): replacement})
        # Inspect the returned raw graph, not a later parser's reaction to an ELF
        # whose shifted tables became misaligned.
        self.assertEqual((len(new_blob) - len(image.blob)) % alignment, 0)
        self.assertGreaterEqual(len(new_blob) - len(image.blob), len(replacement) - len(old))


class ArgumentBoundaryOracles(unittest.TestCase):
    def test_binary_input_rejects_a_list_even_when_it_contains_valid_octets(self):
        data = graph_fixture()
        self.assertEqual(bytes(list(data)), data)
        self.assertEqual(h.repack_unchanged(data), data)
        with self.assertRaises(TypeError):
            h.repack_unchanged(list(data))

    def test_low_level_edits_require_dict_not_a_readonly_mapping(self):
        image = h.BunImage(graph_fixture())
        module = image.source_modules()[0]
        edits = {(module['index'], 'contents'): image.read_source(module) + b'\n// edit'}
        self.assertNotEqual(h._apply_blob_edits(image, edits), image.blob)
        wrapped = MappingProxyType(edits)
        self.assertEqual(dict(wrapped), edits)
        with self.assertRaises(TypeError):
            h._apply_blob_edits(image, wrapped)

    def test_targeted_record_requires_tuple_or_list_not_another_sequence(self):
        data = graph_fixture()
        module = h.BunImage(data).source_modules()[0]
        request = (module['index'], module['name_bytes'], b'console.log("changed");')
        self.assertNotEqual(h.repack_module_sources(data, [request], expected_sha256=None), data)
        with self.assertRaises(h.BunFormatError):
            h.repack_module_sources(data, [deque(request)], expected_sha256=None)

    def test_bool_index_cannot_alias_the_correctly_named_integer_target(self):
        data = graph_fixture()
        image = h.BunImage(data)
        module = image.modules[1]
        self.assertEqual(module['index'], 1)
        source = image.read_source(module) + b'\n// changed'
        valid = (1, module['name_bytes'], source)
        self.assertNotEqual(h.repack_module_sources(data, [valid], expected_sha256=None), data)
        with self.assertRaises(h.BunFormatError):
            h.repack_module_sources(data, [(True, module['name_bytes'], source)], expected_sha256=None)

    def test_targeted_source_api_cannot_modify_a_well_formed_ascii_asset(self):
        data = build_bun_elf([
            _module('/$bunfs/root/cli', b'console.log("original");', module_format=1),
            _module('/$bunfs/root/settings.txt', b'asset=original',
                    encoding=1, loader=13, module_format=0),
        ])
        image = h.BunImage(data)
        asset = image.modules[1]
        self.assertNotIn(asset, image.source_modules())
        with self.assertRaises(h.BunFormatError):
            h.repack_module_sources(
                data, [(1, asset['name_bytes'], b'asset=changed!')], expected_sha256=None)


class DiagnosticResultOracles(unittest.TestCase):
    def test_extended_elf_numbering_remains_an_unsupported_format_result(self):
        data = build_bun_elf([_module('/$bunfs/root/cli', b'x;', module_format=1)])
        image = h.BunImage(data)
        out = bytearray(data)
        # Extended count lives in section zero; the ordinary e_shnum is zero.
        struct.pack_into('<Q', out, image.elf.e_shoff + 32, image.elf.e_shnum)
        struct.pack_into('<H', out, 60, 0)
        label = h.detect_format(bytes(out))
        self.assertIn('unsupported', label.lower())
        self.assertNotIn('malformed', label.lower())
        self.assertFalse(h.can_handle(bytes(out)))

    def test_historical_overlay_remains_an_unsupported_format_result(self):
        data = build_bun_elf([_module('/$bunfs/root/cli', b'x;', module_format=1)])
        image = h.BunImage(data)
        names = image.elf.sections[image.elf.e_shstrndx]
        name_offset = names['foff'] + image.bun_section['name_off']
        out = bytearray(data)
        out[name_offset:name_offset + 4] = b'.abc'
        out.extend(h.BUN_TRAILER + b'\0' * 8)
        label = h.detect_format(bytes(out))
        self.assertIn('unsupported', label.lower())
        self.assertNotIn('malformed', label.lower())
        self.assertFalse(h.can_handle(bytes(out)))


class RecoveryEmissionOracles(unittest.TestCase):
    def setUp(self):
        self.original = graph_fixture()
        source = 'console.log("customized 🦆 café");'.encode('utf-8')
        self.rebuilt = rebuild_fixture(self.original, {(0, 'contents'): source})
        self.current = h._TweakccRebuildImage(self.rebuilt)
        self.assertGreater(self.current.flags, 15)
        self.expected_sources = {
            m['index']: self.current.read_source(m) for m in self.current.source_modules()
        }

    def observe_recovery(self, verify):
        real_writer = h._repack_blob
        observations = []

        def observed_writer(original, blob, width):
            # Assert directly on the buffer produced by the real source-only
            # builder. Do not substitute output or bypass any parser/guard.
            self.assertEqual(bytes(blob[-len(h.BUN_TRAILER):]), h.BUN_TRAILER)
            offset = len(blob) - len(h.BUN_TRAILER) - 32
            header = struct.unpack_from('<QIIIIII', blob, offset)
            verify(blob, header)
            observations.append(header)
            return real_writer(original, blob, width)

        with patch.object(h, '_repack_blob', side_effect=observed_writer):
            result = h.recover_tweakcc(self.rebuilt)
        self.assertEqual(len(observations), 1)
        self.assertEqual(h.extract_js(result), h.extract_tweakcc_js(self.rebuilt))

    def test_source_only_emits_policy_bits_without_compiled_table_flags(self):
        def check(blob, header):
            self.assertEqual(header[-1], self.current.flags & 15)
        self.observe_recovery(check)

    def test_source_only_emits_utf8_tags_for_its_utf8_source_bytes(self):
        def check(blob, header):
            table_offset, table_length = header[1:3]
            width = self.current.module_struct_size
            self.assertEqual(table_length, len(self.current.modules) * width)
            for index, expected in self.expected_sources.items():
                at = table_offset + index * width
                source_at, length = struct.unpack_from('<II', blob, at + 8)
                self.assertEqual(bytes(blob[source_at:source_at + length]), expected)
                self.assertEqual(blob[at + width - 4], 0)
        self.observe_recovery(check)


if __name__ == '__main__':
    unittest.main(verbosity=2)
