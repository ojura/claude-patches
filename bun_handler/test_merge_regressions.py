"""Focused reconciliation tests: v5 capabilities plus v5.1 rejection/capture rules.

No generated fixture is an executable test. Native tests live in the explicit
runners; every fixture here is parsed and transformed only.
"""
import hashlib
import os
import random
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bun_handler as h
import test_bun_handler as gates
from test_bun_windows import build_bun_pe, rebuild_tweakcc_pe, append_pr_pe


class MergeTests(unittest.TestCase):
    def image(self, width=4, **kwargs):
        return build_bun_pe(section_header_size=width, **kwargs)

    def test_width_matrix_preserves_signed_noops_and_current_size(self):
        for width in (4, 8):
            for signed in (False, True):
                for modern in (False, True):
                    with self.subTest(width=width, signed=signed, modern=modern):
                        data=self.image(width,cert=signed,modern=modern)
                        img=h.BunImage(data)
                        self.assertEqual(img.section_header_size,width)
                        self.assertEqual(img.pe.section_header_size,width)
                        self.assertEqual(h.repack_unchanged(data),data)
                        self.assertEqual(h.repack_with_js(data,h.extract_js(data)),data)

    def test_both_wrappers_with_36_byte_records(self):
        from test_bun_handler import _build_bun_blob, _module
        for width in (4,8):
            with self.subTest(width=width):
                outer=self.image(width,cert=False)
                base=h.BunImage(outer)._runtime_blob_vaddr()
                modules=[_module('B:/~BUN/root/cli',b'console.log("legacy");',
                                 bytecode=b'FAKE legacy cache',module_format=2)]
                blob=_build_bun_blob(modules,36,base)
                data=h._repack_blob_pe(outer,blob,width)
                self.assertEqual(h.BunImage(data).module_struct_size,36)
                out=h.repack_with_js(data,b'console.log("edited");')
                self.assertEqual(h.extract_js(out),b'console.log("edited");')
                self.assertEqual(h.BunImage(out).section_header_size,width)
                self.assertEqual(h.repack_unchanged(out),out)

    def test_four_byte_current_tweakcc_recovery_both_policies(self):
        original=self.image(modern=True,cert=True,entry_name='B:/~BUN/root/cli')
        replacement='console.log("recovered 🦆");'.encode()
        external=rebuild_tweakcc_pe(original,replacement)
        self.assertFalse(h.can_handle(external))
        self.assertIn('recover-tweakcc',h.detect_format(external))
        for reference in (original,None):
            with self.subTest(reference=reference is not None):
                out=h.recover_tweakcc(external,reference)
                self.assertEqual(h.BunImage(out).section_header_size,4)
                self.assertEqual(h.extract_js(out),replacement)
                self.assertEqual(h.repack_unchanged(out),out)
                self.assertEqual(h.Pe64(out).certificate_size,0)

    def test_four_byte_append_compaction_and_repatch(self):
        original=self.image(modern=True,cert=True)
        appended=append_pr_pe(original,b'console.log("first");')
        appended=append_pr_pe(appended,b'console.log("latest");')
        final=h.repack_with_js(appended,b'console.log("LATEST");')
        self.assertEqual(len(appended),len(final))
        compacted=h.compact_with_reference(final,original)
        self.assertLessEqual(len(compacted),len(final))
        self.assertEqual(h.BunImage(compacted).section_header_size,4)
        self.assertEqual(h.extract_js(compacted),h.extract_js(final))
        self.assertEqual(h.compact_with_reference(compacted,original),compacted)

    def test_four_byte_growth_keeps_active_builtin_alignment(self):
        original=self.image(modern=True,cert=False)
        before=h.BunImage(original)
        updated=h.repack_with_js(original,h.extract_js(original)+b'\n/*' + b'A'*1600 + b'*/')
        after=h.BunImage(updated)
        self.assertGreater(after.pe.bun_section['raw_size'],before.pe.bun_section['raw_size'])
        for record in after.builtin_bytecode:
            off,length=record['bytes']
            if length:self.assertEqual((after._runtime_blob_vaddr()+off)%128,0)
        self.assertEqual(h.repack_unchanged(updated),updated)

    def test_wrapper_mismatch_is_not_silently_converted(self):
        for width in (4,8):
            original=self.image(width)
            graph=h.BunImage(original).blob
            with self.assertRaises(h.BunFormatError):
                h._repack_blob_pe(original,graph,12-width)

    def test_wrapped_parts_validate_length(self):
        original=self.image()
        blob=bytes(h.BunImage(original).blob)
        with self.assertRaises(h.BunFormatError):
            h._repack_section_pe_parts(original,(struct.pack('<I',len(blob)+1),blob))
        with self.assertRaises(TypeError):
            h._repack_section_pe_parts(original,(1000000000,))
        self.assertEqual(h._repack_section_pe_parts(original,(struct.pack('<I',len(blob)),blob)),original)

    def test_corrupt_u64_not_misdetected_as_u32(self):
        original=bytearray(self.image(8))
        image=h.BunImage(original)
        original[image.bun_section['foff']+4]=1
        self.assertFalse(h.can_handle(original))

    def test_four_byte_wrapper_does_not_bypass_overlap_guard(self):
        original=bytearray(self.image())
        pe=h.Pe64(original)
        struct.pack_into('<I',original,pe.bun_section['hdr_off']+20,pe.sections[0]['foff'])
        self.assertFalse(h.can_handle(original))

    def test_four_byte_wrapper_does_not_bypass_certificate_records(self):
        original=bytearray(self.image())
        pe=h.Pe64(original)
        struct.pack_into('<I',original,pe.certificate_off,7)
        self.assertFalse(h.can_handle(original))

    def test_four_byte_wrapper_does_not_bypass_directory_guard(self):
        original=bytearray(self.image())
        pe=h.Pe64(original)
        struct.pack_into('<II',original,pe.data_directories_off,pe.bun_section['rva'],16)
        self.assertFalse(h.can_handle(original))

    def test_four_byte_wrapper_does_not_bypass_image_base_guard(self):
        original=bytearray(self.image())
        pe=h.Pe64(original)
        struct.pack_into('<Q',original,pe.optional_off+24,pe.image_base+1)
        self.assertFalse(h.can_handle(original))

    def test_v5_and_v51_metadata_access_is_consistent(self):
        image=h.BunImage(self.image())
        pe=image.pe
        self.assertEqual(image.container_kind,'PE')
        self.assertIs(image.container,pe)
        self.assertEqual(pe.image_size,pe.size_of_image)
        self.assertEqual(pe.headers_size,pe.size_of_headers)
        self.assertEqual(pe.initialized_size,pe.size_of_initialized_data)
        self.assertEqual(pe.security_off,pe.security_directory_off)
        self.assertEqual(pe.bun_section['rva'],pe.bun_section['vaddr'])
        self.assertEqual(pe.bun_section['vsize'],pe.bun_section['virtual_size'])
        self.assertEqual(image._runtime_blob_vaddr(),pe.image_base+pe.bun_section['rva']+4)

    def test_original_v5_wine_environment_name_is_accepted(self):
        if os.name=='nt':self.skipTest('native Windows does not use Wine')
        with tempfile.TemporaryDirectory() as td:
            fake=Path(td)/'wine';fake.touch()
            def run(command, **kwargs):
                self.assertEqual(command[0],str(fake))
                self.assertNotIn('capture_output',kwargs)
                kwargs['stdout'].write(b'captured\r\n')
                return subprocess.CompletedProcess(command,0)
            with patch.dict(os.environ,{'BUN_HANDLER_WINE':'','WINE_BINARY':str(fake)}), patch.object(gates.subprocess,'run',side_effect=run):
                result,skip=gates._run_binary(b'MZ nonexecuted fixture','--version')
            self.assertIsNone(skip)
            self.assertEqual(result.stdout,'captured\n')

    def test_seeded_width_encoding_recovery_matrix(self):
        rng=random.Random(520051)
        for n in range(100):
            width=(4,8)[n%2]
            original=self.image(width,modern=True,cert=bool(n%3))
            value=''.join(rng.choice('abcdčž é🦆中') for _ in range(rng.randrange(50)))
            replacement=('console.log("'+value+'");').encode()
            edited=h.repack_with_js(original,replacement)
            self.assertEqual(h.extract_js(edited),replacement)
            self.assertEqual(h.repack_unchanged(edited),edited)
            self.assertEqual(h.BunImage(edited).section_header_size,width)
            appended=append_pr_pe(original,replacement)
            compact=h.compact_with_reference(appended,original)
            self.assertEqual(h.extract_js(compact),replacement)
            self.assertLessEqual(len(compact),len(appended))

if __name__=='__main__':unittest.main(verbosity=2)
