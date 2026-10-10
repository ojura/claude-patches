"""PE parser/writer regression fixtures. Synthetic bytecode is never executed."""
import hashlib
import struct
import unittest
import bun_handler as h
from test_bun_handler import _module, _build_bun_blob


def fixture(*, signed=True, source=b'console.log("hello");', encoding=1, ms=52, flags=0xf):
    base = 0x140000000
    modules = [_module('B:/~BUN/root/cli', source, bytecode=b'FAKE-CACHE',
        module_format=2, module_info=b'FAKE-INFO' if ms == 52 else b'',
        origin='B:/~BUN/root/src/entrypoints/cli.js' if ms == 52 else b'', encoding=encoding)]
    blob = _build_bun_blob(modules, ms, base + 0x2008, flags=flags)
    logical = 8 + len(blob); raw = (logical + 511) // 512 * 512
    data = bytearray(1024 + raw)
    data[:2] = b'MZ'; struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'; struct.pack_into('<HHIIIHH', data, 132, 0x8664, 2, 0, 0, 0, 240, 0x22)
    o = 152
    struct.pack_into('<H', data, o, 0x20b)
    struct.pack_into('<II', data, o+4, 512, raw)
    struct.pack_into('<I', data, o+16, 0x1000)
    struct.pack_into('<Q', data, o+24, base)
    struct.pack_into('<II', data, o+32, 4096, 512)
    struct.pack_into('<II', data, o+56, (0x2000+raw+4095)//4096*4096, 512)
    struct.pack_into('<I', data, o+64, 0x12345678)
    struct.pack_into('<H', data, o+68, 3)
    struct.pack_into('<I', data, o+108, 16)
    for index, name, vs, rva, rs, fo, sc in [
        (0,b'.text',1,0x1000,512,512,0x60000020),
        (1,b'.bun',logical,0x2000,raw,1024,0x40000040)]:
        at=o+240+index*40;data[at:at+len(name)]=name
        struct.pack_into('<IIIIIIHHI',data,at+8,vs,rva,rs,fo,0,0,0,0,sc)
    data[512] = 0xc3
    struct.pack_into('<Q',data,1024,len(blob));data[1032:1032+len(blob)]=blob
    if signed:
        struct.pack_into('<II',data,o+112+32,len(data),16)
        data += struct.pack('<IHH',16,0x200,2)+b'FAKECERT'
    return bytes(data)


def mutate(data, offset, fmt, *values):
    buf=bytearray(data);struct.pack_into(fmt,buf,offset,*values);return bytes(buf)


class PeTests(unittest.TestCase):
    def setUp(self):
        self.data=fixture(); self.pe=h.Pe64(self.data)

    def reject(self, data):
        self.assertFalse(h.can_handle(data))
        with self.assertRaises((h.BunFormatError,NotImplementedError)):
            h.repack_unchanged(data)

    def test_signed_noop_preserves_every_byte(self):
        self.assertEqual(h.repack_unchanged(self.data),self.data)
        self.assertEqual(h.repack_with_js(self.data,h.extract_js(self.data)),self.data)

    def test_unsigned_noop(self):
        data=fixture(signed=False);self.assertEqual(h.repack_unchanged(data),data)

    def test_same_size_removes_certificate_only_from_size(self):
        out=h.repack_with_js(self.data,b'console.log("HELLO");')
        self.assertEqual(len(out),len(self.data)-16)
        self.assertEqual(h.Pe64(out).certificate_size,0)
        i=h.BunImage(out);self.assertEqual(i.modules[0]['bytecode'],(0,0))
        self.assertEqual(h.repack_unchanged(out),out)

    def test_repatch_does_not_append(self):
        a=h.repack_with_js(self.data,b'console.log("HELLO");')
        b=h.repack_with_js(a,b'console.log("world");')
        self.assertEqual(len(a),len(b))

    def test_growth_crosses_raw_boundary(self):
        source=h.extract_js(self.data)+b'\n/*'+b'X'*1500+b'*/'
        out=h.repack_with_js(self.data,source);p=h.Pe64(out)
        self.assertEqual(h.extract_js(out),source)
        self.assertEqual(p.bun_section['raw_size']%512,0)
        self.assertGreater(p.bun_section['raw_size'],self.pe.bun_section['raw_size'])

    def test_shrink_preserves_prefix(self):
        out=h.repack_with_js(self.data,b'x;');self.assertEqual(h.extract_js(out),b'x;')
        self.assertEqual(out[512:1024],self.data[512:1024])

    def test_unicode_write(self):
        source='console.log("čaj 🦆");'.encode();out=h.repack_with_js(self.data,source)
        self.assertEqual(h.extract_js(out),source)
        self.assertEqual(h.BunImage(out).modules[0]['encoding'],0)

    def test_utf16_identity_and_edit(self):
        data=fixture(source='console.log("čaj");'.encode('utf-16-le'),encoding=2)
        self.assertEqual(h.repack_with_js(data,h.extract_js(data)),data)
        out=h.repack_with_js(data,b'console.log("changed");')
        self.assertEqual(h.BunImage(out).modules[0]['encoding'],0)

    def test_36_byte_records(self):
        data=fixture(ms=36);self.assertEqual(h.repack_unchanged(data),data)

    def test_source_hash_invalidation(self):
        data=fixture(flags=0x2f);out=h.repack_with_js(data,b'changed();')
        self.assertEqual(h.BunImage(out).source_hash(0),0)

    def test_digest_binding(self):
        digest,source=h.extract_js_with_digest(self.data)
        self.assertEqual(h.repack_with_js(self.data,source,expected_sha256=digest),self.data)
        with self.assertRaises(h.BunFormatError):
            h.repack_with_js(self.data,source,expected_sha256='0'*64)

    def test_mutable_buffers(self):
        for data in (bytearray(self.data),memoryview(bytearray(self.data))):
            self.assertEqual(h.detect_format(data),h.detect_format(self.data))
            self.assertTrue(h.can_handle(data))

    def test_truncated_headers_and_raw(self):
        for size in (0,1,63,128,150,240,450,len(self.data)-1):
            with self.subTest(size=size):self.reject(self.data[:size])

    def test_wrong_machine(self):self.reject(mutate(self.data,132,'<H',0xaa64))
    def test_unaligned_image_base(self):self.reject(mutate(self.data,176,'<Q',0x140000001))
    def test_pe32(self):self.reject(mutate(self.data,152,'<H',0x10b))
    def test_dll(self):self.reject(mutate(self.data,150,'<H',0x2022))
    def test_section_count_bounds(self):self.reject(mutate(self.data,134,'<H',97))
    def test_optional_header_bounds(self):self.reject(mutate(self.data,148,'<H',8))
    def test_bad_file_alignment(self):self.reject(mutate(self.data,188,'<I',513))
    def test_section_header_in_raw_data(self):self.reject(mutate(self.data,212,'<I',256))
    def test_bad_image_size(self):self.reject(mutate(self.data,208,'<I',4096))
    def test_partial_security_directory(self):self.reject(mutate(self.data,self.pe.security_off,'<I',0))
    def test_certificate_not_at_eof(self):self.reject(self.data+b'JUNK')
    def test_bad_certificate_length(self):self.reject(mutate(self.data,self.pe.certificate_off,'<I',7))
    def test_raw_overlap(self):self.reject(mutate(self.data,self.pe.bun_section['hdr_off']+20,'<I',512))
    def test_virtual_overlap(self):self.reject(mutate(self.data,self.pe.bun_section['hdr_off']+12,'<I',4096))
    def test_extra_overlay(self):self.reject(fixture(signed=False)+b'X'*8)
    def test_nonzero_bun_slack(self):
        p=self.pe.bun_section;self.reject(mutate(self.data,p['foff']+p['size'],'<B',1))
    def test_directory_points_into_bun(self):
        self.reject(mutate(self.data,152+112,'<II',0x2000,16))
    def test_bun_contains_code_flag(self):
        self.reject(mutate(self.data,self.pe.bun_section['hdr_off']+36,'<I',0x60000060))
    def test_no_bun(self):
        out=bytearray(self.data);at=self.pe.bun_section['hdr_off'];out[at:at+8]=b'.other\0\0'
        self.reject(bytes(out))
    def test_two_bun_sections(self):
        out=bytearray(self.data);out[392:400]=b'.bun\0\0\0\0';self.reject(bytes(out))
    def test_oversized_length_prefix(self):self.reject(mutate(self.data,1024,'<Q',2**63))



class WineCaptureTests(unittest.TestCase):
    def test_pe_capture_uses_files_and_retains_exit_and_text(self):
        import os, subprocess, tempfile
        from pathlib import Path
        from unittest.mock import patch
        import test_bun_handler as gates
        with tempfile.TemporaryDirectory() as td:
            wine=Path(td)/"wine";wine.touch()
            def run(command, **kwargs):
                self.assertNotIn("capture_output",kwargs)
                kwargs["stdout"].write(b"version\r\n")
                kwargs["stderr"].write(b"diagnostic\r\n")
                return subprocess.CompletedProcess(command,7)
            with patch.dict(os.environ,{"BUN_HANDLER_WINE":str(wine)}), patch.object(gates.subprocess,"run",side_effect=run):
                result,skip=gates._run_binary(b"MZ parser-only fixture","--version")
            self.assertIsNone(skip)
            self.assertEqual((result.returncode,result.stdout,result.stderr),(7,"version\n","diagnostic\n"))

    @unittest.skipIf(__import__("os").name=="nt", "native Windows needs no Wine")
    def test_missing_wine_does_not_execute_a_fixture(self):
        import os
        from unittest.mock import patch
        import test_bun_handler as gates
        with patch.dict(os.environ,{"BUN_HANDLER_WINE":""}), patch.object(gates.subprocess,"run") as run:
            result,skip=gates._run_binary(b"MZ parser-only fixture","--version")
        self.assertIsNone(result)
        self.assertIn("requires",skip)
        run.assert_not_called()

if __name__=='__main__':unittest.main()
