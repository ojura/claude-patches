#!/usr/bin/env python3
import struct
import unittest

import bun_handler as h
from test_bun_handler import _build_bun_blob, _module


def align(v, a):
    return (v + a - 1) // a * a


def build_bun_pe(source=b'console.log("(Claude Code)");', *, cert=True, trailing_section=False, modern=False, section_header_size=8, checksum=0x12345678, entry_name='B:/~BUN/root/claude.exe'):
    origin = b'B:/~BUN/root/src/entrypoints/cli.js' if entry_name.endswith('/cli') else b''
    mods = [_module(entry_name, source, module_format=2, bytecode=b'bytecode' if modern else b'', module_info=b'info' if modern else b'', origin=origin)]
    kwargs = dict(flags=0x3ff, builtin_bytecode=[(7,b'builtin')], bytecode_string_table=b'bst', startup_module_count=1, module_info_string_table=b'mist') if modern else {}
    runtime_base = 0x140000000 + 0x2000 + section_header_size
    blob = _build_bun_blob(mods, 52, runtime_base, trailing_pad=False, entry_point_id=0, **kwargs)
    payload = ((struct.pack('<Q', len(blob)) if section_header_size == 8 else struct.pack('<I', len(blob))) + blob)
    file_align = 0x200
    sec_align = 0x1000
    peoff = 0x80
    opt_size = 0xF0
    nsects = 3 if trailing_section else 2
    section_table = peoff + 4 + 20 + opt_size
    headers_size = align(section_table + nsects * 40, file_align)
    text_off = headers_size
    text_raw = 0x200
    text_va = 0x1000
    bun_off = text_off + text_raw
    bun_raw = align(len(payload), file_align)
    bun_va = 0x2000
    trail_off = bun_off + bun_raw
    trail_raw = 0x200 if trailing_section else 0
    trail_va = align(bun_va + len(payload), sec_align)
    cert_off = trail_off + trail_raw
    cert_bytes = (struct.pack('<IHH', 16, 0x200, 2) + b'CERTDATA') if cert else b''
    total = cert_off + len(cert_bytes)
    out = bytearray(total)
    out[:2] = b'MZ'
    struct.pack_into('<I', out, 0x3C, peoff)
    out[peoff:peoff+4] = b'PE\0\0'
    coff = peoff + 4
    struct.pack_into('<HHIIIHH', out, coff, h.PE_MACHINE_AMD64, nsects, 0, 0, 0, opt_size, 0x22)
    opt = coff + 20
    struct.pack_into('<H', out, opt, h.PE32_PLUS_MAGIC)
    struct.pack_into('<I', out, opt + 4, text_raw)  # SizeOfCode
    struct.pack_into('<I', out, opt + 8, bun_raw + (trail_raw if trailing_section else 0))
    struct.pack_into('<I', out, opt + 16, text_va)  # entry
    struct.pack_into('<I', out, opt + 20, text_va)  # BaseOfCode
    struct.pack_into('<Q', out, opt + 24, 0x140000000)
    struct.pack_into('<II', out, opt + 32, sec_align, file_align)
    struct.pack_into('<HH', out, opt + 40, 6, 0)
    struct.pack_into('<HH', out, opt + 48, 6, 0)
    size_image = align((trail_va + 1) if trailing_section else (bun_va + len(payload)), sec_align)
    struct.pack_into('<II', out, opt + 56, size_image, headers_size)
    struct.pack_into('<I', out, opt + 64, checksum)
    struct.pack_into('<H', out, opt + 68, 3)  # console subsystem
    struct.pack_into('<Q', out, opt + 72, 0x100000)
    struct.pack_into('<Q', out, opt + 80, 0x1000)
    struct.pack_into('<Q', out, opt + 88, 0x100000)
    struct.pack_into('<Q', out, opt + 96, 0x1000)
    struct.pack_into('<I', out, opt + 108, 16)
    if cert:
        struct.pack_into('<II', out, opt + 112 + 4*8, cert_off, len(cert_bytes))

    sh = opt + opt_size
    def section(i, name, vsize, va, rawsize, rawoff, chars):
        o = sh + i*40
        out[o:o+8] = name.encode()[:8].ljust(8,b'\0')
        struct.pack_into('<IIII', out, o+8, vsize, va, rawsize, rawoff)
        struct.pack_into('<I', out, o+36, chars)
    section(0,'.text',1,text_va,text_raw,text_off,0x60000020)
    section(1,'.bun',len(payload),bun_va,bun_raw,bun_off,0x40000040)
    if trailing_section:
        section(2,'.trail',1,trail_va,trail_raw,trail_off,0x40000040)
    out[text_off] = 0xC3
    out[bun_off:bun_off+len(payload)] = payload
    if trailing_section:
        out[trail_off] = 0x42
    if cert:
        out[cert_off:cert_off+len(cert_bytes)] = cert_bytes
    return bytes(out)


class WindowsPeTests(unittest.TestCase):
    def test_parse_noop_and_detect(self):
        data = build_bun_pe()
        self.assertTrue(h.can_handle(data))
        self.assertIn('PE32+', h.detect_format(data))
        self.assertEqual(h.extract_js(data), b'console.log("(Claude Code)");')
        self.assertEqual(h.repack_unchanged(data), data)
        self.assertNotEqual(h.Pe64(data).certificate_size, 0)

    def test_changed_output_strips_signature_and_roundtrips(self):
        data = build_bun_pe()
        out = h.repack_with_js(data, b'console.log("(ClauDe Code)");')
        self.assertEqual(h.extract_js(out), b'console.log("(ClauDe Code)");')
        self.assertEqual(h.Pe64(out).certificate_size, 0)
        self.assertEqual(h.repack_unchanged(out), out)
        self.assertLess(len(out), len(data))

    def test_growth_uses_raw_slack_then_file_alignment(self):
        data = build_bun_pe(cert=False)
        before = h.Pe64(data).section('.bun')
        small = h.repack_with_js(data, h.extract_js(data) + b'x')
        small_s = h.Pe64(small).section('.bun')
        self.assertEqual(small_s['raw_size'], before['raw_size'])
        large = h.repack_with_js(data, h.extract_js(data) + b'x' * 800)
        large_s = h.Pe64(large).section('.bun')
        self.assertGreater(large_s['raw_size'], before['raw_size'])
        self.assertEqual(large_s['raw_size'] % h.Pe64(large).file_alignment, 0)
        self.assertEqual(h.extract_js(large), h.extract_js(data) + b'x' * 800)

    def test_nonfinal_mapped_section_refused(self):
        data = build_bun_pe(trailing_section=True)
        self.assertFalse(h.can_handle(data))
        with self.assertRaises(h.BunFormatError):
            h.repack_with_js(data, h.extract_js(data) + b'x')

    def test_mutable_inputs(self):
        data = build_bun_pe()
        label = h.detect_format(data)
        for value in (bytearray(data), memoryview(bytearray(data))):
            self.assertTrue(h.can_handle(value))
            self.assertEqual(h.detect_format(value), label)


class WindowsPeBoundaryTests(unittest.TestCase):
    def test_four_byte_bun_wrapper_roundtrips(self):
        data = build_bun_pe(cert=False, section_header_size=4)
        image = h.BunImage(data)
        self.assertEqual(image.section_header_size, 4)
        self.assertTrue(h.can_handle(data))
        out = h.repack_with_js(data, b'console.log("four-byte wrapper");')
        self.assertEqual(h.BunImage(out).section_header_size, 4)
        self.assertEqual(h.extract_js(out), b'console.log("four-byte wrapper");')
        self.assertEqual(h.repack_unchanged(out), out)

    def test_real_edit_zeroes_optional_checksum(self):
        data = build_bun_pe(cert=True, checksum=0x89ABCDEF)
        before = h.Pe64(data)
        self.assertEqual(struct.unpack_from('<I', data, before.checksum_off)[0], 0x89ABCDEF)
        self.assertEqual(h.repack_unchanged(data), data)
        out = h.repack_with_js(data, b'console.log("unsigned");')
        after = h.Pe64(out)
        self.assertEqual(struct.unpack_from('<I', out, after.checksum_off)[0], 0)
        self.assertEqual(after.certificate_size, 0)

    def test_wrong_machine_and_pe32_are_rejected(self):
        data = bytearray(build_bun_pe(cert=False))
        pe = h.Pe64(bytes(data))
        struct.pack_into('<H', data, pe.coff_off, 0x14C)
        self.assertFalse(h.can_handle(bytes(data)))
        with self.assertRaises(NotImplementedError):
            h.BunImage(bytes(data))

        data = bytearray(build_bun_pe(cert=False))
        pe = h.Pe64(bytes(data))
        struct.pack_into('<H', data, pe.optional_off, 0x10B)
        self.assertFalse(h.can_handle(bytes(data)))
        with self.assertRaises(NotImplementedError):
            h.BunImage(bytes(data))

    def test_half_empty_certificate_directory_is_rejected(self):
        data = bytearray(build_bun_pe(cert=True))
        pe = h.Pe64(bytes(data))
        struct.pack_into('<I', data, pe.security_directory_off + 4, 0)
        self.assertFalse(h.can_handle(bytes(data)))
        with self.assertRaises(h.BunFormatError):
            h.Pe64(bytes(data))

    def test_nonzero_overlay_without_certificate_is_rejected(self):
        data = build_bun_pe(cert=False) + b'overlay'
        self.assertFalse(h.can_handle(data))
        with self.assertRaises(h.BunFormatError):
            h.repack_with_js(data, h.extract_js(data) + b'x')

    def test_duplicate_bun_section_is_rejected(self):
        data = bytearray(build_bun_pe(cert=False))
        pe = h.Pe64(bytes(data))
        text = pe.sections[0]
        data[text['hdr_off']:text['hdr_off'] + 8] = b'.bun\0\0\0\0'
        self.assertFalse(h.can_handle(bytes(data)))
        with self.assertRaises(h.BunFormatError):
            h.BunImage(bytes(data))

    def test_coff_symbol_table_after_bun_is_rejected(self):
        data = bytearray(build_bun_pe(cert=False))
        pe = h.Pe64(bytes(data))
        bun = pe.section('.bun')
        raw_end = bun['foff'] + bun['raw_size']
        struct.pack_into('<II', data, pe.coff_off + 8, raw_end, 1)
        self.assertFalse(h.can_handle(bytes(data)))

    def test_nonzero_pe_bun_raw_padding_is_rejected(self):
        data = bytearray(build_bun_pe(cert=False))
        pe = h.Pe64(bytes(data))
        bun = pe.section('.bun')
        self.assertGreater(bun['raw_size'], bun['vsize'])
        data[bun['foff'] + bun['vsize']] = 1
        self.assertFalse(h.can_handle(bytes(data)))
        with self.assertRaises(h.BunFormatError):
            h.BunImage(bytes(data))

    def test_large_shrink_reduces_raw_allocation_and_initialized_data(self):
        source = b'console.log("(Claude Code)");' + b'X' * 2400
        data = build_bun_pe(source=source, cert=False)
        before = h.Pe64(data)
        replacement = b'console.log("(Claude Code)");' + b'Y' * 100
        out = h.repack_with_js(data, replacement)
        after = h.Pe64(out)
        self.assertEqual(h.extract_js(out), replacement)
        self.assertLess(after.section('.bun')['raw_size'], before.section('.bun')['raw_size'])
        raw_delta = after.section('.bun')['raw_size'] - before.section('.bun')['raw_size']
        self.assertEqual(after.size_of_initialized_data - before.size_of_initialized_data, raw_delta)
        expected_image = h._align_up(
            after.section('.bun')['vaddr'] + after.section('.bun')['vsize'],
            after.section_alignment,
        )
        self.assertEqual(after.size_of_image, expected_image)

    def test_repeat_equal_length_edit_does_not_fatten(self):
        data = build_bun_pe(cert=True)
        first = h.repack_with_js(data, b'console.log("(ClauDe Code)");')
        second = h.repack_with_js(first, b'console.log("(CLAUDE Code)");')
        self.assertEqual(len(second), len(first))
        self.assertEqual(h.Pe64(first).certificate_size, 0)
        self.assertEqual(h.Pe64(second).certificate_size, 0)
        self.assertEqual(h.extract_js(second), b'console.log("(CLAUDE Code)");')


def rebuild_tweakcc_pe(data, replacement):
    img = h.BunImage(data)
    fields = h._FIELD_ORDER_52 if img.module_struct_size == 52 else h._FIELD_ORDER_36
    body = bytearray(); records=[]
    for m in img.modules:
        pairs=[]
        for field in fields:
            value = replacement if (m['index']==0 and field=='contents') else img.read_field(m,field)
            pairs.append((len(body),len(value))); body.extend(value); body.append(0)
        records.append((pairs,m))
    table_off=len(body)
    for pairs,m in records:
        for pair in pairs: body.extend(struct.pack('<II',*pair))
        body.extend(struct.pack('<BBBB',m['encoding'],m['loader'],m['module_format'],m['side']))
    table_len=len(records)*img.module_struct_size
    argv=bytes(img.blob[img.compile_argv_off:img.compile_argv_off+img.compile_argv_len])
    argv_off=len(body); body.extend(argv); body.append(0)
    off=len(body); body.extend(struct.pack('<QIIIIII',off,table_off,table_len,img.entry_point_id,argv_off,len(argv),img.flags)); body.extend(h.BUN_TRAILER)
    return h._repack_blob_pe(data,body,img.section_header_size)


class WindowsTweakccRecoveryTests(unittest.TestCase):
    def test_source_only_recovery_preserves_modern_plain_cli_entrypoint(self):
        original = build_bun_pe(cert=False, modern=True, entry_name='B:/~BUN/root/cli')
        rebuilt = rebuild_tweakcc_pe(original, b'console.log("modern windows");')
        recovered = h.recover_tweakcc(rebuilt)
        image = h.BunImage(recovered)
        self.assertEqual(image.entrypoint_module()['name_str'], 'B:/~BUN/root/cli')
        self.assertEqual(image.bytecode_origin_path(image.entrypoint_module()), '')
        self.assertEqual(h.extract_js(recovered), b'console.log("modern windows");')
        self.assertEqual(h.repack_unchanged(recovered), recovered)

    def test_source_only_recovery_of_current_main_graph(self):
        original=build_bun_pe(cert=False,modern=True)
        source=b'console.log("patched windows");'
        rebuilt=rebuild_tweakcc_pe(original,source)
        self.assertFalse(h.can_handle(rebuilt))
        self.assertIn('recover-tweakcc',h.detect_format(rebuilt))
        self.assertEqual(h.extract_tweakcc_js(rebuilt),source)
        recovered=h.recover_tweakcc(rebuilt)
        self.assertTrue(h.can_handle(recovered))
        self.assertEqual(h.extract_js(recovered),source)
        self.assertEqual(h.BunImage(recovered).container_kind,'PE')
        self.assertEqual(h.repack_unchanged(recovered),recovered)

class WindowsTweakccReferenceRecoveryTests(unittest.TestCase):
    def test_reference_recovery_restores_modern_metadata(self):
        original=build_bun_pe(cert=False,modern=True)
        source=b'console.log("reference windows");'
        rebuilt=rebuild_tweakcc_pe(original,source)
        recovered=h.recover_tweakcc(rebuilt,original)
        self.assertTrue(h.can_handle(recovered))
        self.assertEqual(h.extract_js(recovered),source)
        ri=h.BunImage(recovered); oi=h.BunImage(original)
        self.assertEqual(ri.flags,oi.flags)
        self.assertEqual(len(ri.builtin_bytecode),len(oi.builtin_bytecode))
        self.assertEqual(ri.modules[0]['bytecode'],(0,0))


def append_pr_pe(data, source):
    img=h.BunImage(data)
    block=source+b'\0'
    out=bytearray(img.blob[:img.offsets_off])+block+img.blob[img.offsets_off:]
    record=img.modules_off
    struct.pack_into('<II',out,record+8,img.offsets_off,len(source))
    out[record+16:record+img.module_struct_size-4]=b'\0'*(img.module_struct_size-20)
    out[record+img.module_struct_size-4]=0
    if img.source_hashes_off is not None:
        struct.pack_into('<I',out,img.source_hashes_off,0)
    offsets=img.offsets_off+len(block)
    struct.pack_into('<Q',out,offsets,offsets)
    struct.pack_into('<I',out,offsets+28,img.flags & ~h.BUN_FLAG_SOURCE_TEXT_CONTIGUOUS)
    return h._repack_blob_pe(data,out,img.section_header_size)


class WindowsAppendCompactionTests(unittest.TestCase):
    def test_pr_append_layout_can_be_edited_and_compacted(self):
        original=build_bun_pe(cert=True,modern=True)
        source=b'console.log("append windows");'
        appended=append_pr_pe(original,source)
        self.assertTrue(h.can_handle(appended))
        self.assertEqual(h.extract_js(appended),source)
        edited=h.repack_with_js(appended,b'console.log("APPEND windows");')
        self.assertEqual(len(edited),len(appended))
        compact=h.compact_with_reference(edited,original)
        self.assertEqual(h.extract_js(compact),b'console.log("APPEND windows");')
        self.assertLessEqual(len(compact),len(edited))
        self.assertEqual(h.repack_unchanged(compact),compact)


if __name__ == '__main__':
    unittest.main(verbosity=2)
