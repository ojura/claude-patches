#!/usr/bin/env python3
"""Regression expectations from the bun_handler v3 review.

Usage:
    python review_regressions.py /path/to/bun_handler_v3

These tests intentionally FAIL on the reviewed v3 release. They exercise only
synthetic, non-executable fixtures. CLI tests operate in temporary directories.
No real Claude installation is read, overwritten, or executed.
"""
import argparse
import contextlib
import importlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('handler_dir', type=Path)
options, unittest_args = parser.parse_known_args()
HANDLER_DIR = options.handler_dir.resolve()
if not (HANDLER_DIR / 'bun_handler.py').is_file():
    parser.error('handler_dir must contain bun_handler.py and test_bun_handler.py')
sys.path.insert(0, str(HANDLER_DIR))
h = importlib.import_module('bun_handler')
helpers = importlib.import_module('test_bun_handler')


def fixture(source=b'console.log("hello");', *, count=1, entry_point_id=0):
    modules = [helpers._module('/$bunfs/root/claude', source, module_format=2)]
    for i in range(1, count):
        modules.append(helpers._module(f'/$bunfs/root/other{i}.js',
                                      b'export const value=1;', module_format=1))
    return helpers.build_bun_elf(modules, entry_point_id=entry_point_id)


class ReviewRegressions(unittest.TestCase):
    def test_extract_refuses_input_inode_aliases(self):
        for kind in ('same_path', 'symlink', 'hardlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                original = root / 'binary'
                before = fixture()
                original.write_bytes(before)
                output = original
                if kind != 'same_path':
                    output = root / 'alias'
                    if kind == 'symlink':
                        output.symlink_to(original)
                    else:
                        os.link(original, output)
                result = subprocess.run(
                    [sys.executable, str(HANDLER_DIR / 'bun_handler.py'),
                     'extract', str(original), str(output)],
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(original.read_bytes(), before,
                                 'extract overwrote its input executable')
                self.assertNotEqual(result.returncode, 0,
                                    'an input alias must be refused')

    def test_strict_native_gates_fail_when_binary_missing(self):
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td) / 'does-not-exist')
            env = dict(os.environ, HOME=td, CLAUDE_NATIVE_BINARY=missing,
                       CLAUDE_PFG_STRICT_GATES='gate1,gate2,gate3,gate5,gate6,gate7,gate8,gate9',
                       CLAUDE_PFG_STRICT_GATES_WAIVE='')
            result = subprocess.run(
                [sys.executable, str(HANDLER_DIR / 'test_bun_handler.py'), missing],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0,
                                'strict mode skipped every native gate:\n' + result.stdout[-600:])

    def test_out_of_range_startup_index_is_not_patchable(self):
        data = fixture(entry_point_id=0xFFFFFFFF)
        self.assertFalse(h.can_handle(data),
                         'startup index 0xffffffff is outside the one-record table')

    def test_single_module_envelope_header_comment_roundtrips(self):
        source = h._MULTI_MODULE_HEADER + b'\nconsole.log("hello");\n'
        data = fixture(source)
        self.assertTrue(h.can_handle(data))
        self.assertEqual(h.extract_js(data), source)
        self.assertEqual(h.repack_with_js(data, h.extract_js(data)), data)

    def test_targeted_write_does_not_silently_break_extraction(self):
        if not hasattr(h, 'repack_module_sources'):
            self.skipTest('targeted source API was introduced in v3')
        data = fixture(count=2)
        image = h.BunImage(data)
        module = image.modules[0]
        source = image.read_source(module) + h._MULTI_MODULE_MARKER + b' harmless comment\n'
        try:
            output = h.repack_module_sources(
                data, [(0, module['name_bytes'], source)], expected_sha256=None)
        except h.BunFormatError:
            # Either early, explicit rejection or a collision-safe envelope is
            # acceptable. Silently writing an unextractable binary is not.
            return
        self.assertEqual(h.repack_with_js(output, h.extract_js(output)), output)

    def test_format_detection_accepts_supported_mutable_byte_buffers(self):
        data = fixture()
        expected = h.detect_format(data)
        for kind, value in [('bytearray', bytearray(data)),
                            ('writable_memoryview', memoryview(bytearray(data)))]:
            with self.subTest(kind=kind):
                self.assertTrue(h.can_handle(value))
                self.assertEqual(h.detect_format(value), expected)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], '-v', *unittest_args])
