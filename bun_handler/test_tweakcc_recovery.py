"""Stdlib-only recovery/guardrail tests; synthetic fixtures are NEVER executed.

rebuild_fixture models main's format, not an execution of upstream. Native tests
use the separate make_tweakcc_main_binary.cjs adapter and the pinned real source.
"""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import random
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

import bun_handler as h
from test_bun_interop import fixture
from test_bun_handler import _module, build_bun_elf


def replace_blob_fixture(data, blob):
    """Synthetic ELF splice; outside-blob padding keeps header tables aligned."""
    elf = h.Elf64(data)
    bs = elf.section('.bun')
    old_end = bs['foff'] + bs['size']
    wrapped = struct.pack('<Q', len(blob)) + blob
    delta = len(wrapped) - bs['size']
    pad = -delta % h._required_file_shift_alignment(elf, old_end)
    growth = delta + pad
    out = bytearray(data[:bs['foff']]) + wrapped + b'\0' * pad + data[old_end:]
    new_shoff = elf.e_shoff + growth
    struct.pack_into('<Q', out, 40, new_shoff)
    for s in elf.sections:
        at = new_shoff + s['index'] * elf.e_shentsize
        if s['index'] == bs['index']:
            struct.pack_into('<Q', out, at + 32, len(wrapped))
        elif h._section_has_payload(s) and s['foff'] >= old_end:
            struct.pack_into('<Q', out, at + 24, s['foff'] + growth)
    load = h._validate_elf_patch_layout(elf, bs, for_resize=True)
    struct.pack_into('<QQ', out, load['hdr_off'] + 32,
                     load['filesz'] + growth, load['memsz'] + growth)
    return bytes(out)


def rebuild_fixture(data, replacements=None):
    """Independent fixture construction of the legacy sequential wire format."""
    try:
        img = h.BunImage(data)
    except h.BunFormatError:
        img = h._TweakccRebuildImage(data)
    replacements = replacements or {}
    fields = h._FIELD_ORDER_52 if img.module_struct_size == 52 else h._FIELD_ORDER_36
    out = bytearray()
    records = []
    for m in img.modules:
        pairs = []
        for field in fields:
            value = replacements.get((m['index'], field), img.read_field(m, field))
            pairs.append((len(out), len(value)))
            out.extend(value); out.append(0)
        records.append((pairs, m))
    table_off = len(out)
    for pairs, m in records:
        for pair in pairs: out.extend(struct.pack('<II', *pair))
        out.extend(struct.pack('<BBBB', m['encoding'], m['loader'], m['module_format'], m['side']))
    table_len = len(records) * img.module_struct_size
    argv = bytes(img.blob[img.compile_argv_off:img.compile_argv_off + img.compile_argv_len])
    argv_off = len(out); out.extend(argv); out.append(0)
    offset = len(out)
    out.extend(struct.pack('<QIIIIII', offset, table_off, table_len,
                           img.entry_point_id, argv_off, len(argv), img.flags))
    out.extend(h.BUN_TRAILER)
    return replace_blob_fixture(data, out)


def change(data, relative, content):
    image = h._TweakccRebuildImage(data)
    absolute = image.bun_section['foff'] + image.section_header_size + relative
    out = bytearray(data); out[absolute:absolute + len(content)] = content
    return bytes(out)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.original = fixture()
        self.source = 'console.log("patched 🦆 čaj");'.encode()
        self.rebuilt = rebuild_fixture(self.original, {(0, 'contents'): self.source})

    def test_recovery_is_not_permissive_normal_parsing(self):
        self.assertFalse(h.can_handle(self.rebuilt))
        self.assertIn('recover-tweakcc', h.detect_format(self.rebuilt))
        with self.assertRaises(h.BunFormatError): h.repack_unchanged(self.rebuilt)

    def test_rejects_non_rebuilt_original(self):
        with self.assertRaises(h.BunFormatError): h.recover_tweakcc(self.original)

    def test_extraction_understands_stale_latin1_tag(self):
        self.assertIn(self.source, h.extract_tweakcc_js(self.rebuilt))

    def test_strict_entry_points_name_a_rebuild(self):
        refusal = r'^tweakcc full rebuild; run recover-tweakcc to make it patchable \(strict reader: '
        for name, call in (('extract_js', lambda: h.extract_js(self.rebuilt)),
                           ('repack_unchanged', lambda: h.repack_unchanged(self.rebuilt)),
                           ('repack_with_js', lambda: h.repack_with_js(self.rebuilt, b'x'))):
            with self.subTest(name), self.assertRaisesRegex(h.BunFormatError, refusal):
                call()
        with self.assertRaisesRegex(h.BunFormatError, '^file too small to be ELF64$'):
            h.extract_js(b'not a bun executable')

    def test_extract_and_modules_read_a_rebuild_unless_strict(self):
        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = h.main(list(argv))
            return code, out.getvalue(), err.getvalue()
        note = 'note: tweakcc full rebuild, read with the tweakcc reader'
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            binary, original = p / 'rebuilt', p / 'original'
            binary.write_bytes(self.rebuilt)
            original.write_bytes(self.original)
            code, _, err = run('extract', str(binary), str(p / 'source.js'))
            self.assertEqual(code, 0)
            self.assertTrue(err.startswith(note), err)
            self.assertEqual((p / 'source.js').read_bytes(), h.extract_tweakcc_js(self.rebuilt))
            self.assertIn(self.source, (p / 'source.js').read_bytes())
            code, out, err = run('modules', str(binary))
            self.assertEqual(code, 0)
            self.assertTrue(err.startswith(note), err)
            self.assertEqual(out.splitlines()[0], '# record size 52, 3 modules, entry point 0')
            for argv in (('extract', '--strict', str(binary), str(p / 'strict.js')),
                         ('modules', str(binary), '--strict')):
                with self.subTest(argv=argv[:2]):
                    code, out, err = run(*argv)
                    self.assertEqual(code, 1)
                    self.assertIn('failed: tweakcc full rebuild; run recover-tweakcc', err)
                    self.assertNotIn(note, err)
            self.assertFalse((p / 'strict.js').exists())
            code, _, err = run('extract', str(original), str(p / 'original.js'))
            self.assertEqual((code, err), (0, ''))

    def test_source_only_recovery_preserves_assets_policy_and_source(self):
        out = h.recover_tweakcc(self.rebuilt)
        img = h.BunImage(out); original = h.BunImage(self.original)
        self.assertTrue(h.can_handle(out))
        self.assertEqual(img.flags, original.flags & 0xf)
        self.assertEqual(img.extract_js(), h.extract_tweakcc_js(self.rebuilt))
        for m, old in zip(img.modules, original.modules):
            self.assertEqual(m['name_bytes'], old['name_bytes'])
            self.assertTrue(all(m[f] == (0, 0) for f in ('bytecode','moduleInfo','sourcemap','bytecodeOriginPath')))
            if old['loader'] not in h._JS_LOADERS:
                self.assertEqual(img.read_field(m,'contents'), original.read_field(old,'contents'))
        self.assertEqual(h.repack_unchanged(out), out)
        self.assertEqual(h.repack_with_js(out,h.extract_js(out)),out)
        self.assertEqual(h.recover_tweakcc(self.rebuilt),out)

    def test_reference_restores_omitted_optional_tables(self):
        out = h.recover_tweakcc(self.rebuilt, self.original)
        expected = h.repack_module_sources(self.original,[(0,b'/$bunfs/root/cli',self.source)],expected_sha256=None)
        self.assertEqual(out,expected)
        img = h.BunImage(out); orig = h.BunImage(self.original)
        self.assertEqual(img.flags, orig.flags)
        for pointer in ('bytecode_string_table','module_info_string_table'):
            a,b=getattr(img,pointer),getattr(orig,pointer)
            self.assertEqual(bytes(img.blob[a[0]:sum(a)]),bytes(orig.blob[b[0]:sum(b)]))
        self.assertEqual(len(img.builtin_bytecode),1)
        self.assertNotEqual(img.modules[1]['bytecode'][1],0)
        self.assertEqual(img.modules[0]['bytecode'],(0,0))

    def test_identity_rebuild_can_restore_identical_original(self):
        rebuilt = rebuild_fixture(self.original)
        self.assertEqual(h.recover_tweakcc(rebuilt,self.original),self.original)

    def test_equal_length_repatches_never_accumulate_bodies(self):
        for reference in (None, self.original):
            out = h.recover_tweakcc(self.rebuilt, reference)
            size=len(out)
            for _ in range(8):
                source=h.extract_js(out).replace(b'patched',b'PATCHED')
                out=h.repack_with_js(out,source)
                source=h.extract_js(out).replace(b'PATCHED',b'patched')
                out=h.repack_with_js(out,source)
                self.assertEqual(len(out),size)

    def test_empty_source_with_cache_needs_reference(self):
        old=build_bun_elf([_module('/$bunfs/root/cli',b'',module_format=1,bytecode=b'opaque')])
        rebuilt=rebuild_fixture(old)
        with self.assertRaisesRegex(h.BunFormatError,'reference'):
            h.recover_tweakcc(rebuilt)
        self.assertEqual(h.recover_tweakcc(rebuilt,old),old)

    def test_empty_source_without_cache(self):
        old=build_bun_elf([_module('/$bunfs/root/cli',b'',module_format=1)])
        rebuilt=rebuild_fixture(old)
        out=h.recover_tweakcc(rebuilt)
        self.assertEqual(h.extract_js(out),b'')

    def test_reference_rejects_unrelated_source_changes(self):
        rebuilt=rebuild_fixture(self.original,{(1,'contents'):b'export let n=999;'})
        with self.assertRaisesRegex(h.BunFormatError,'not a tweakcc source target'):
            h.recover_tweakcc(rebuilt,self.original)

    def test_reference_rejects_asset_changes(self):
        rebuilt=rebuild_fixture(self.original,{(2,'contents'):b'DIFFERENT'})
        with self.assertRaisesRegex(h.BunFormatError,'not a tweakcc source target'):
            h.recover_tweakcc(rebuilt,self.original)

    def test_reference_rejects_changed_cache_or_origin(self):
        for field in ('bytecode','moduleInfo','sourcemap','bytecodeOriginPath'):
            rebuilt=rebuild_fixture(self.original,{(0,field):b'changed'})
            with self.subTest(field=field),self.assertRaises(h.BunFormatError):
                h.recover_tweakcc(rebuilt,self.original)

    def test_reference_rejects_runtime_bytes(self):
        changed=bytearray(self.rebuilt);changed[24] ^= 1
        with self.assertRaisesRegex(h.BunFormatError,'native executable'):
            h.recover_tweakcc(changed,self.original)

    def test_reference_rejects_tail_changes(self):
        out=bytearray(self.rebuilt);sec=h.Elf64(out).section('.trail');out[sec['foff']]^=1
        with self.assertRaises(h.BunFormatError):h.recover_tweakcc(out,self.original)

    def test_rejects_bad_entrypoint(self):
        image=h._TweakccRebuildImage(self.rebuilt)
        out=change(self.rebuilt,image.offsets_off+16,struct.pack('<I',0xffffffff))
        with self.assertRaises(h.BunFormatError):h.recover_tweakcc(out)

    def test_rejects_unknown_flags(self):
        image=h._TweakccRebuildImage(self.rebuilt)
        out=change(self.rebuilt,image.offsets_off+28,struct.pack('<I',1<<31))
        with self.assertRaises(NotImplementedError):h.recover_tweakcc(out)

    def test_reference_rejects_policy_changes(self):
        image=h._TweakccRebuildImage(self.rebuilt)
        out=change(self.rebuilt,image.offsets_off+28,struct.pack('<I',image.flags^1))
        with self.assertRaisesRegex(h.BunFormatError,'flags'):
            h.recover_tweakcc(out,self.original)

    def test_reference_rejects_argv_changes(self):
        image=h._TweakccRebuildImage(self.rebuilt)
        out=change(self.rebuilt,image.compile_argv_off,b'X')
        with self.assertRaisesRegex(h.BunFormatError,'argv'):
            h.recover_tweakcc(out,self.original)

    def test_rejects_invalid_utf8_nul_and_marker(self):
        for source in (b'\xff',b'x\0y',b'x'+h._MULTI_MODULE_MARKER+b' comment'):
            out=rebuild_fixture(self.original,{(0,'contents'):source})
            for reference in (None,self.original):
                with self.subTest(source=source,reference=reference is not None),self.assertRaises(h.BunFormatError):
                    h.recover_tweakcc(out,reference)

    def test_rejects_missing_terminator(self):
        image=h._TweakccRebuildImage(self.rebuilt)
        out=change(self.rebuilt,sum(image.modules[0]['contents']),b'X')
        with self.assertRaises(h.BunFormatError):h.recover_tweakcc(out)

    def test_rejects_gaps_aliases_and_changed_empty_pointers(self):
        image=h._TweakccRebuildImage(self.rebuilt)
        for field_offset,value in ((8,0),(16,0),(24,0xffffffff)):
            out=change(self.rebuilt,image.modules_off+field_offset,struct.pack('<I',value))
            with self.subTest(field=field_offset),self.assertRaises(h.BunFormatError):
                h.recover_tweakcc(out)

    def test_digests_and_buffer_types(self):
        digest=hashlib.sha256(self.rebuilt).hexdigest()
        out=h.recover_tweakcc(self.rebuilt,expected_sha256=digest)
        for value in (bytearray(self.rebuilt),memoryview(bytearray(self.rebuilt))):
            self.assertEqual(h.recover_tweakcc(value,expected_sha256=digest),out)
        with self.assertRaises(h.BunFormatError):h.recover_tweakcc(self.rebuilt,expected_sha256='0'*64)
        with self.assertRaises(h.BunFormatError):h.recover_tweakcc(self.rebuilt,self.original,reference_sha256='0'*64)
        with self.assertRaises(h.BunFormatError):h.recover_tweakcc(self.rebuilt,reference_sha256='0'*64)
        with self.assertRaises(TypeError):h.recover_tweakcc(123)

    def test_utf16_tagged_targets_require_reference(self):
        text='console.log("café");'
        original=build_bun_elf([_module('/$bunfs/root/cli',text.encode('utf-16-le'),module_format=1,encoding=2)])
        unchanged=rebuild_fixture(original)
        self.assertEqual(h.recover_tweakcc(unchanged,original),original)
        src='console.log("čaj🦆");'.encode()
        changed=rebuild_fixture(original,{(0,'contents'):src})
        with self.assertRaisesRegex(h.BunFormatError,'ambiguous'):
            h.recover_tweakcc(changed)
        result=h.recover_tweakcc(changed,original)
        self.assertEqual(h.extract_js(result),src)

    def test_utf16_asset_retains_raw_storage(self):
        raw='asset čaj 🦆'.encode('utf-16-le')
        original=build_bun_elf([_module('/$bunfs/root/cli',b'foo();',module_format=1),
                               _module('/$bunfs/root/text.txt',raw,module_format=0,encoding=2,loader=13)])
        out=h.recover_tweakcc(rebuild_fixture(original))
        img=h.BunImage(out)
        self.assertEqual(img.modules[1]['encoding'],2)
        self.assertEqual(img.read_field(img.modules[1],'contents'),raw)

    def test_both_record_widths_and_seeded_edits(self):
        rng=random.Random(90264)
        for case in range(120):
            ms=rng.choice((36,52));count=rng.choice((3,9,13,18));original=fixture(ms,count)
            source=('console.log("'+rng.choice(('čaj','🦆','plain','中'))+'_'+'x'*rng.randrange(30)+'");').encode()
            rebuilt=rebuild_fixture(original,{(0,'contents'):source})
            for reference in (None,original):
                out=h.recover_tweakcc(rebuilt,reference)
                self.assertEqual(h.BunImage(out).read_source(h.BunImage(out).modules[0]),source)
                self.assertEqual(h.repack_unchanged(out),out)
                edited=h.repack_with_js(out,h.extract_js(out).replace(b'console.log',b'console.dir'))
                self.assertEqual(len(edited),len(out))

    def test_recovery_cli_exclusive_and_permissions(self):
        script=str(Path(h.__file__).resolve())
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);original=p/'broken';original.write_bytes(self.rebuilt)
            ref=p/'trusted-original';ref.write_bytes(self.original)
            for kind in ('same','symlink','hardlink'):
                target=p/kind
                if kind=='same':target=original
                elif kind=='symlink':target.symlink_to(original)
                else:os.link(original,target)
                r=subprocess.run([sys.executable,script,'recover-tweakcc',str(original),str(target)],capture_output=True)
                self.assertNotEqual(r.returncode,0);self.assertEqual(original.read_bytes(),self.rebuilt)
            for mode in ('cachefree','reference'):
                args=[sys.executable,script,'recover-tweakcc',str(original),str(p/mode)]
                if mode=='reference':args+=['--reference',str(ref)]
                r=subprocess.run(args,capture_output=True)
                self.assertEqual(r.returncode,0,r.stderr)
                self.assertEqual(stat.S_IMODE((p/mode).stat().st_mode),0o700)
                self.assertEqual((p/mode).read_bytes(),h.recover_tweakcc(self.rebuilt,self.original if mode=='reference' else None))
            r=subprocess.run([sys.executable,script,'extract',str(original),str(p/'source.js')],capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertIn(b'note: tweakcc full rebuild',r.stderr)
            self.assertEqual((p/'source.js').read_bytes(),h.extract_tweakcc_js(self.rebuilt))
            self.assertEqual(stat.S_IMODE((p/'source.js').stat().st_mode),0o600)
            self.assertFalse(list(p.glob('.bun-handler-*')))


class HarnessBoundaryTests(unittest.TestCase):
    def run_suite(self, strict='', waived='', args=(), env_path=None):
        with tempfile.TemporaryDirectory() as home:
            env={**os.environ,'HOME':home,'CLAUDE_PFG_STRICT_GATES':strict,
                 'CLAUDE_PFG_STRICT_GATES_WAIVE':waived}
            env.pop('CLAUDE_NATIVE_BINARY',None)
            if env_path is not None:env['CLAUDE_NATIVE_BINARY']=env_path
            return subprocess.run([sys.executable,str(Path(h.__file__).with_name('test_bun_handler.py')),*args],
                                  capture_output=True,env=env,timeout=30)

    def test_optional_run_without_binary(self):self.assertEqual(self.run_suite().returncode,0)
    def test_required_run_without_explicit_path(self):self.assertNotEqual(self.run_suite('gate7').returncode,0)
    def test_waived_run_without_binary(self):self.assertEqual(self.run_suite('gate7','gate7').returncode,0)
    def test_misspelled_gate_is_error(self):self.assertNotEqual(self.run_suite('gate77').returncode,0)
    def test_invalid_explicit_path_even_nonstrict(self):self.assertNotEqual(self.run_suite(args=('/no/such/binary',)).returncode,0)
    def test_invalid_environment_path(self):self.assertNotEqual(self.run_suite(env_path='/no/such/binary').returncode,0)


if __name__=='__main__':unittest.main()
