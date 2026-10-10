"""Reviewed compaction boundaries, with valid controls and exact refusal checks.

The source fixtures are synthetic and never executed. This module deliberately
preserves the existing policy: empty appended source followed by regrowth may
move source before the module table and is not an accepted append lineage.
It does not authorize dropping those checks to make compaction succeed.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

import bun_handler as h
from test_bun_handler import _build_bun_blob, _module
from test_bun_interop import fixture, append_sources
from test_bun_windows import build_bun_pe
from test_graph_flags_278 import add_records, base_elf, OPTIONS_BYTES

REFUSAL = "binary is neither a verified append lineage nor canonical replay"


def image_fixture(kind, wrapper, record, new_records):
    if kind == "ELF":
        data = base_elf(width=record, wrapper=wrapper)
    else:
        outer = build_bun_pe(cert=False, section_header_size=wrapper,
                             entry_name="B:/~BUN/root/cli")
        base = h.BunImage(outer)._runtime_blob_vaddr()
        modules = [
            _module("B:/~BUN/root/cli", b'console.log("original");',
                    bytecode=b'opaque-main-cache', module_info=b'opaque-info',
                    module_format=1),
            _module("B:/~BUN/root/helper.js", b'export const answer=42;',
                    bytecode=b'opaque-helper-cache', module_format=1),
            _module("B:/~BUN/root/asset.bin", b'\x00\xffASSET',
                    encoding=0, loader=5, module_format=0),
        ]
        blob = _build_bun_blob(modules, record, base, flags=0x3ef,
                              compile_argv=b'--smol',
                              builtin_bytecode=[(1, b'builtin')],
                              bytecode_string_table=b'strings',
                              module_info_string_table=b'info-strings',
                              startup_module_count=1)
        data = h._repack_blob(outer, blob, wrapper)
    return add_records(data)[0] if new_records else data


def append_fixture(data, source):
    """Construct the append format independently of the production edit engine."""
    image = h.BunImage(data)
    block = source + b'\0'
    alignment = h._required_container_shift_alignment(
        image.container, image.bun_section['foff'] + image.bun_section['size'])
    block += b'\0' * (-len(block) % alignment)
    out = bytearray(image.blob[:image.offsets_off]) + block + image.blob[image.offsets_off:]
    record = image.modules_off
    struct.pack_into('<II', out, record + 8, image.offsets_off, len(source))
    out[record + 16:record + image.module_struct_size - 4] = b'\0' * (image.module_struct_size - 20)
    out[record + image.module_struct_size - 4] = 0
    if image.source_hashes_off is not None:
        struct.pack_into('<I', out, image.source_hashes_off, 0)
    offsets = image.offsets_off + len(block)
    struct.pack_into('<Q', out, offsets, offsets)
    struct.pack_into('<I', out, offsets + 28, image.flags & ~16)
    return h._repack_blob(data, out, image.section_header_size)


def edit_first(data, source):
    image = h.BunImage(data)
    return h.repack_module_sources(data, [(0, image.modules[0]['name_bytes'], source)],
                                   expected_sha256=hashlib.sha256(data).hexdigest())


class ReviewedCompactionBoundaryTests(unittest.TestCase):
    def cases(self):
        for kind in ('ELF', 'PE'):
            for wrapper in (4, 8):
                for record in (36, 52):
                    for new_records in (False, True):
                        yield kind, wrapper, record, new_records

    def test_exact_review_reproduction_has_typed_refusal(self):
        original = fixture()
        empty = append_sources(original, {0: b''})
        regrown = edit_first(empty, b'console.log("regrown");')
        self.assertTrue(h.can_handle(regrown))
        with self.assertRaisesRegex(h.BunFormatError, '^' + REFUSAL + '$'):
            h.compact_with_reference(regrown, original)

    def test_refusal_matrix_and_source_allocation_position(self):
        for case in self.cases():
            with self.subTest(case=case):
                original = image_fixture(*case)
                empty = append_fixture(original, b'')
                ei = h.BunImage(empty)
                self.assertGreaterEqual(ei.modules[0]['contents'][0], ei.optional_tail_end)
                regrown = edit_first(empty, b'console.log("regrown");')
                ri = h.BunImage(regrown)
                self.assertLess(ri.modules[0]['contents'][0], ri.modules_off)
                self.assertTrue(h.can_handle(regrown))
                self.assertEqual(ri.read_source(ri.modules[0]), b'console.log("regrown");')
                with self.assertRaisesRegex(h.BunFormatError, '^' + REFUSAL + '$'):
                    h.compact_with_reference(regrown, original)

    def test_regrown_image_remains_editable_and_noop_stable(self):
        for case in self.cases():
            with self.subTest(case=case):
                original = image_fixture(*case)
                regrown = edit_first(append_fixture(original, b''), b'export const value=1;')
                self.assertEqual(h.repack_unchanged(regrown), regrown)
                self.assertEqual(h.repack_with_js(regrown, h.extract_js(regrown)), regrown)
                again = edit_first(regrown, 'export const value="čaj 🦆";'.encode())
                image = h.BunImage(again)
                self.assertEqual(image.read_source(image.modules[0]), 'export const value="čaj 🦆";'.encode())
                before = h.BunImage(original)
                self.assertEqual(image.read_source(image.modules[1]), before.read_source(before.modules[1]))
                self.assertEqual(image.read_field(image.modules[2], 'contents'), before.read_field(before.modules[2], 'contents'))
                self.assertEqual(image.runtime_options_bytes, OPTIONS_BYTES if case[3] else b'')
                self.assertEqual(image.prelinked_module_graph, (0, 0))

    def test_nonempty_append_control_compacts_and_is_idempotent(self):
        for case in self.cases():
            with self.subTest(case=case):
                original = image_fixture(*case)
                appended = append_fixture(original, b'console.log("append");')
                changed = edit_first(appended, b'console.log("APPEND");')
                compacted = h.compact_with_reference(changed, original)
                self.assertLessEqual(len(compacted), len(changed))
                self.assertEqual(h.extract_js(compacted), h.extract_js(changed))
                self.assertEqual(h.compact_with_reference(compacted, original), compacted)

    def test_original_and_canonical_replay_controls_remain_accepted(self):
        for case in self.cases():
            with self.subTest(case=case):
                original = image_fixture(*case)
                self.assertEqual(h.compact_with_reference(original, original), original)
                canonical = edit_first(original, b'console.log("canonical");')
                self.assertEqual(h.compact_with_reference(canonical, original), canonical)

    def test_reverted_append_restores_exact_active_cache_original(self):
        for case in self.cases():
            if not case[3]:
                continue
            with self.subTest(case=case):
                original = image_fixture(*case)
                oi = h.BunImage(original)
                first = append_fixture(original, b'console.log("temporary");' + b' ' * 2048)
                appended = append_fixture(first, b'console.log("latest");')
                reverted = edit_first(appended, oi.read_source(oi.modules[0]))
                self.assertGreater(len(reverted), len(original))
                self.assertEqual(h.BunImage(reverted).prelinked_module_graph, (0, 0))
                restored = h.compact_with_reference(reverted, original)
                self.assertEqual(restored, original)
                self.assertEqual(h.BunImage(restored).prelinked_module_graph, oi.prelinked_module_graph)
                self.assertEqual(h.BunImage(restored).prelinked_module_files, oi.prelinked_module_files)

    def test_equal_size_replay_is_not_implicit_cache_restoration(self):
        # This small PE append fits the original raw allocation. Compaction's
        # contract returns the input unless replay makes the file smaller.
        for wrapper in (4, 8):
            with self.subTest(wrapper=wrapper):
                original = image_fixture('PE', wrapper, 52, True)
                oi = h.BunImage(original)
                appended = append_fixture(original, b'console.log("temporary");')
                reverted = edit_first(appended, oi.read_source(oi.modules[0]))
                self.assertEqual(len(reverted), len(original))
                self.assertEqual(h.extract_js(reverted), h.extract_js(original))
                self.assertEqual(h.compact_with_reference(reverted, original), reverted)

    def test_cli_refusal_does_not_publish_or_modify_files(self):
        original = fixture()
        regrown = edit_first(append_sources(original, {0: b''}), b'console.log("regrown");')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            current, reference, output = (root / name for name in ('current', 'reference', 'output'))
            current.write_bytes(regrown)
            reference.write_bytes(original)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(h.main(['compact', str(current), str(reference), str(output)]), 1)
            self.assertIn(REFUSAL, stderr.getvalue())
            self.assertFalse(output.exists())
            self.assertEqual(current.read_bytes(), regrown)
            self.assertEqual(reference.read_bytes(), original)
            self.assertEqual({p.name for p in root.iterdir()}, {'current', 'reference'})

    def test_review_property_smoke_runs_in_default_discovery(self):
        """Run the exact submitted campaign, not a rewritten approximation."""
        root = Path(__file__).resolve().parent
        script = root / 'review_probes/new_record_property.py'
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        with tempfile.TemporaryDirectory() as td:
            # Preserve original script bytes; provide its failure-receipt folder
            # in a private directory rather than in the source checkout.
            path = Path(td) / script.name
            path.write_bytes(script.read_bytes())
            (Path(td) / 'evidence').mkdir()
            run = subprocess.run([sys.executable, str(path), str(root), '--fixtures', '48'],
                                 env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = json.loads(run.stdout)
        self.assertTrue(result['all_pass'])
        self.assertEqual((result['fixtures'], result['cycles'], result['noops']), (48, 144, 384))


if __name__ == '__main__':
    unittest.main(verbosity=2)
