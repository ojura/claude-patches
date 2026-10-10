"""Extra mutation-derived contract tests. Synthetic executables are never run.

Every rejection starts with a separately accepted fixture; malformed byte edits
are local. Original production/test files are untouched. No mutant IDs or source
inspection are used by these tests, only behavior and explicit format fields.
"""
import contextlib,io,os,struct,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import bun_handler as h
from test_bun_handler import _module,build_bun_elf
from test_bun_interop import fixture as graph_fixture,append_sources
from test_bun_windows import build_bun_pe
from test_pe_backend import fixture as pe_fixture


def change(data,at,fmt,*values):
    out=bytearray(data);struct.pack_into(fmt,out,at,*values);return bytes(out)


class ElfMutationCoverage(unittest.TestCase):
    def setUp(self):
        self.data=graph_fixture();self.image=h.BunImage(self.data);self.elf=self.image.elf
        self.assertTrue(h.can_handle(self.data))
    def reject(self,data):
        self.assertFalse(h.can_handle(data))
        with self.assertRaises((h.BunFormatError,NotImplementedError)):
            h.BunImage(data)
        # The separately exposed container parser must reject malformed headers,
        # not rely on a later absence/invalidity of the Bun graph.
        with self.assertRaises((h.BunFormatError,NotImplementedError)):
            h.Elf64(data)
    def test_elf_identification(self):
        for label,offset,fmt,value in [
            ('magic',0,'<4s',b'BAD!'),('big-endian-tag',5,'<B',2),
            ('identification-version',6,'<B',0),('object-not-executable',16,'<H',1),
            ('header-version',20,'<I',0),('short-declared-header',52,'<H',63),
            ('small-section-records',58,'<H',63),('extended-section-count',60,'<H',0),
            ('missing-section-table',40,'<Q',0),('out-of-file-section-table',40,'<Q',len(self.data)),
            ('section-name-index',62,'<H',self.elf.e_shnum),
            ('small-program-records',54,'<H',55),('missing-program-headers',56,'<H',0),
            ('missing-program-table',32,'<Q',0),('out-of-file-program-table',32,'<Q',len(self.data)),
        ]:
            with self.subTest(label=label):self.reject(change(self.data,offset,fmt,value))
    def test_unmapped_section_payload_bounds(self):
        trail=self.elf.section('.trail');self.assertIsNotNone(trail)
        for offset,value in [(24,len(self.data)),(32,len(self.data))]:
            with self.subTest(offset=offset):self.reject(change(self.data,trail['hdr_off']+offset,'<Q',value))
    def test_section_string_table_must_be_file_backed(self):
        tab=self.elf.sections[self.elf.e_shstrndx]
        self.reject(change(self.data,tab['hdr_off']+4,'<I',8))
    def test_section_names_are_bounded_terminated_and_utf8(self):
        tab=self.elf.sections[self.elf.e_shstrndx];trail=self.elf.section('.trail')
        self.reject(change(self.data,trail['hdr_off'],'<I',tab['size']))
        # Grow no ranges: put an unterminated invalid suffix in the existing name table.
        at=tab['foff']+tab['size']-1
        data=change(self.data,trail['hdr_off'],'<I',tab['size']-1)
        self.reject(change(data,at,'<B',ord('X')))
        # Original string termination stays intact for a separate decoding check.
        data=change(self.data,trail['hdr_off'],'<I',tab['size']-2)
        self.reject(change(data,at-1,'<B',0xff))
    def test_program_alignment_power_of_two_independent_of_congruence(self):
        seg=next(x for x in self.elf.segments if x['type']==1)
        # Keep offset and VA congruent so the independent congruence guard cannot
        # mask deletion of the power-of-two check.
        data=change(self.data,seg['hdr_off']+16,'<Q',seg['foff'])
        data=change(data,seg['hdr_off']+48,'<Q',3)
        with self.assertRaisesRegex(h.BunFormatError,'power of two'):h.Elf64(data)
    def test_file_backed_segment_cannot_extend_past_eof(self):
        seg=next(x for x in self.elf.segments if x['type']==1)
        data=change(self.data,seg['hdr_off']+32,'<Q',len(self.data)+1)
        data=change(data,seg['hdr_off']+40,'<Q',len(self.data)+1)
        self.reject(data)
    def test_load_segment_bounds_and_alignment(self):
        seg=next(x for x in self.elf.segments if x['type']==1)
        for label,at,fmt,value in [
            ('filesize-over-memsize',seg['hdr_off']+40,'<Q',max(0,seg['filesz']-1)),
            
            ('incongruent-virtual-address',seg['hdr_off']+16,'<Q',seg['vaddr']+1),
        ]:
            with self.subTest(label=label):self.reject(change(self.data,at,fmt,value))


class PeMutationCoverage(unittest.TestCase):
    def setUp(self):
        self.data=pe_fixture();self.pe=h.Pe64(self.data);self.assertTrue(h.can_handle(self.data))
    def reject(self,data):
        self.assertFalse(h.can_handle(data))
        with self.assertRaises((h.BunFormatError,NotImplementedError)):
            h.BunImage(data)
        with self.assertRaises((h.BunFormatError,NotImplementedError)):
            h.Pe64(data)
    def test_signatures_and_header_directory_limits(self):
        p=self.pe
        for label,at,fmt,value in [
            ('PE signature',p.pe_off,'<4s',b'PExx'),('DOS signature',0,'<2s',b'XX'),
            ('optional directory overflow',p.optional_off+108,'<I',17),
            ('missing Security Directory slot',p.optional_off+108,'<I',4),
            ('section relocations',p.sections[0]['hdr_off']+24,'<I',512),
            ('section line numbers',p.sections[0]['hdr_off']+28,'<I',512),
            ('section relocation count',p.sections[0]['hdr_off']+32,'<H',1),
            ('section line count',p.sections[0]['hdr_off']+34,'<H',1),
            ('initialized size too small',p.optional_off+8,'<I',0),
            ('entry in non-executable bun',p.optional_off+16,'<I',p.bun_section['rva']),
        ]:
            with self.subTest(label=label):self.reject(change(self.data,at,fmt,value))
    def test_file_alignment_minimum_is_a_separate_check(self):
        # All existing offsets still divide 256; no other alignment check should
        # conceal dropping the supported-minimum restriction.
        self.reject(change(self.data,self.pe.optional_off+36,'<I',256))
    def test_extended_optional_header_does_not_allow_unknown_directory_slots(self):
        p=self.pe;data=bytearray(self.data)
        table=data[p.section_table_off:p.section_table_off+80]
        data[p.section_table_off:p.section_table_off+8]=b'\0'*8
        data[p.section_table_off+8:p.section_table_off+88]=table
        struct.pack_into('<H',data,p.coff_off+16,248)
        struct.pack_into('<I',data,p.optional_off+108,17)
        self.reject(bytes(data))
    def with_zero_section(self):
        original=pe_fixture(signed=False);p=h.Pe64(original);data=bytearray(original)
        first=p.section_table_off;data[first+80:first+120]=data[first+40:first+80]
        data[first+40:first+80]=b'\0'*40;data[first+40:first+46]=b'.extra'
        struct.pack_into('<H',data,p.coff_off+2,3)
        struct.pack_into('<I',data,first+40+12,0x2000)
        struct.pack_into('<I',data,first+40+36,0x40000040)
        struct.pack_into('<I',data,first+80+12,0x3000)
        struct.pack_into('<I',data,p.optional_off+56,0x4000)
        self.assertTrue(h.can_handle(bytes(data)));return bytes(data),first
    def test_overlap_between_non_bun_sections(self):
        data,first=self.with_zero_section()
        # These overlaps do not touch .bun, and .bun is still the last section.
        raw=change(data,first+40+8,'<IIII',1,0x2000,512,512)
        self.reject(raw)
        virtual=change(data,first+40+8,'<IIII',1,0x1000,0,0)
        self.reject(virtual)
    def test_section_alignment_not_masked_by_neighbor_overlap(self):
        data,first=self.with_zero_section()
        data=change(data,first+12,'<I',0x1001)
        data=change(data,self.pe.optional_off+16,'<I',0x1001)
        self.reject(data)
    def test_raw_size_alignment_not_masked_by_overlap(self):
        self.reject(change(self.data,self.pe.sections[0]['hdr_off']+16,'<I',511))
    def test_image_size_cannot_exceed_declared_final_section_extent(self):
        self.reject(change(self.data,self.pe.optional_off+56,'<I',self.pe.image_size+4096))
    def test_certificate_record_types_and_padding(self):
        p=self.pe;co=p.certificate_off
        for label,at,fmt,value in [('revision',co+4,'<H',0x100),('kind',co+6,'<H',1)]:
            with self.subTest(label=label):self.reject(change(self.data,at,fmt,value))
        # A 14-byte cert body owns two zero padding bytes in its 16-byte allocation.
        valid=bytearray(change(self.data,co,'<I',14));valid[-2:]=b'\0\0'
        self.assertTrue(h.can_handle(bytes(valid)))
        self.reject(change(bytes(valid),len(valid)-1,'<B',1))
    def test_certificate_gap_is_not_free_overlay_space(self):
        p=self.pe
        data=self.data[:p.raw_end]+b'\0'*8+self.data[p.certificate_off:]
        data=change(data,p.security_off,'<I',p.certificate_off+8)
        self.reject(data)
    def test_two_certificate_records_noop_then_removal(self):
        p=self.pe;second=struct.pack('<IHH',16,0x200,2)+b'NEXTCERT'
        data=change(self.data,p.security_off+4,'<I',32)+second
        self.assertEqual(h.repack_unchanged(data),data)
        changed=h.repack_with_js(data,h.extract_js(data)+b'/*edit*/')
        self.assertEqual(h.Pe64(changed).certificate_size,0)
        self.assertEqual(h.extract_js(changed),h.extract_js(data)+b'/*edit*/')
    def debug_fixture(self):
        p=self.pe;s=p.sections[0]
        data=change(self.data,s['hdr_off']+8,'<I',256)
        data=change(data,p.optional_off+112+6*8,'<II',s['rva']+64,28)
        # IMAGE_DEBUG_DIRECTORY: SizeOfData, AddressOfRawData, PointerToRawData.
        data=change(data,s['foff']+64+16,'<III',16,s['rva']+144,s['foff']+144)
        self.assertTrue(h.can_handle(data));return data,s
    def test_pe_debug_record_boundaries(self):
        data,section=self.debug_fixture();p=self.pe
        for label,at,fmt,values in [
            ('incomplete debug record',p.optional_off+112+6*8+4,'<I',(27,)),
            ('raw debug payload in Bun',section['foff']+64+16,'<III',(16,0,p.bun_section['foff'])),
            ('inconsistent debug RVA',section['foff']+64+20,'<I',(section['rva']+145,)),
        ]:
            with self.subTest(label=label):self.reject(change(data,at,fmt,*values))
    def test_pe_reference_cannot_change_native_initialized_sum(self):
        original=build_bun_pe(cert=False,modern=True,source=b'console.log("old");'+b' '*2048)
        from test_bun_windows import append_pr_pe
        appended=append_pr_pe(original,b'console.log("new");')
        pe=h.Pe64(appended);bad=change(appended,pe.optional_off+8,'<I',pe.initialized_size+512)
        self.assertTrue(h.can_handle(bad))
        with self.assertRaisesRegex(h.BunFormatError,'initialized'):
            h.compact_with_reference(bad,original)


class CompactionMutationCoverage(unittest.TestCase):
    def test_reverting_source_does_not_restore_a_larger_signed_file(self):
        from test_bun_windows import append_pr_pe
        original=build_bun_pe(cert=True,source=b'console.log("old");')
        first=append_pr_pe(original,b'console.log("new");')
        reverted=append_pr_pe(first,b'console.log("old");')
        self.assertLess(len(reverted),len(original))
        result=h.compact_with_reference(reverted,original)
        self.assertEqual(result,reverted)
        self.assertEqual(h.extract_js(result),h.extract_js(original))
    def test_append_origin_override_not_silently_discarded(self):
        original=graph_fixture();old=h.BunImage(original)
        appended=append_sources(original,{0:b'console.log("new");'})
        image=h.BunImage(appended);out=bytearray(appended)
        record=image.bun_section['foff']+image.section_header_size+image.modules_off
        # A valid already-owned name range would alias, so use a previously
        # detached origin belonging to this same target but change its contents.
        off,n=old.modules[0]['bytecodeOriginPath']
        self.assertGreater(n,0)
        absolute=image.bun_section['foff']+image.section_header_size+off
        out[absolute:absolute+n]=b'x'*n
        struct.pack_into('<II',out,record+40,off,n)
        with self.assertRaises(h.BunFormatError):h.compact_with_reference(bytes(out),original)


class ApiMutationCoverage(unittest.TestCase):
    def test_low_level_edit_input_contract(self):
        image=h.BunImage(graph_fixture())
        bad=[[],{0:b'foo'},{(0,):b'foo'},{(0,'missing'):b'foo'},{(0,'contents'):1}]
        for edits in bad:
            with self.subTest(edits=edits),self.assertRaises((h.BunFormatError,TypeError)):
                h._apply_blob_edits(image,edits)
    def test_range_helper_negative_or_out_of_bounds(self):
        image=h.BunImage(graph_fixture())
        for off,length in [(-1,0),(-1,1),(0,-1),(len(image.blob),1)]:
            with self.subTest(off=off,length=length),self.assertRaises(h.BunFormatError):
                image._require_range('test-region',off,length)
    def test_source_hash_index_bounds(self):
        image=h.BunImage(graph_fixture())
        for index in (-1,len(image.modules)):
            with self.subTest(index=index),self.assertRaises(h.BunFormatError):image.source_hash(index)
    def test_read_source_rejects_assets(self):
        image=h.BunImage(graph_fixture())
        with self.assertRaises(h.BunFormatError):image.read_source(image.modules[2])
    def test_envelope_identity_includes_names_and_order(self):
        image=h.BunImage(graph_fixture());source=image.extract_js()
        records=h.split_extracted_js(source)
        from test_bun_handler import _rebuild_extracted
        changed=[*records];idx,name,body=changed[0];changed[0]=(idx,b'/$bunfs/root/not-the-same',body)
        with self.assertRaises(h.BunFormatError):h.changed_js_modules(source,_rebuild_extracted(changed))
        with self.assertRaises(h.BunFormatError):h.repack_with_js(image.data,_rebuild_extracted(changed))
    def test_startup_hint_is_bounded_by_module_count(self):
        original=graph_fixture();image=h.BunImage(original)
        at=image.bun_section['foff']+image.section_header_size+image.startup_module_count_record_off
        valid=change(original,at,'<I',len(image.modules))
        self.assertTrue(h.can_handle(valid))
        invalid=change(original,at,'<I',len(image.modules)+1)
        self.assertFalse(h.can_handle(invalid))
        with self.assertRaises(h.BunFormatError):h.BunImage(invalid)
    def test_empty_argv_pointer_must_still_own_a_nul(self):
        data=build_bun_elf([_module('/$bunfs/root/cli',b'console.log("x");',module_format=1)])
        image=h.BunImage(data);at=image.bun_section['foff']+image.section_header_size+image.compile_argv_off
        bad=change(data,at,'<B',1)
        self.assertFalse(h.can_handle(bad))
        with self.assertRaises(h.BunFormatError):h.BunImage(bad)
    def test_fsync_failure_does_not_publish_or_leak_temporary_file(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'new-output'
            with patch.object(h.os,'fsync',side_effect=OSError('test fsync failure')) as call:
                with self.assertRaises(OSError):h._write_new_file(out,b'complete output')
            call.assert_called_once()
            self.assertFalse(out.exists());self.assertEqual(list(Path(td).iterdir()),[])
    def test_publication_fsync_precedes_link(self):
        events=[];fsync=os.fsync;link=os.link
        def sync(fd):events.append('fsync');return fsync(fd)
        def publish(src,dst):events.append('link');return link(src,dst)
        with tempfile.TemporaryDirectory() as td:
            with patch.object(h.os,'fsync',side_effect=sync),patch.object(h.os,'link',side_effect=publish):
                h._write_new_file(Path(td)/'new',b'durable')
            self.assertEqual(events,['fsync','link'])
    def test_link_failure_preserves_existing_output_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'output';out.write_bytes(b'original')
            with self.assertRaises(FileExistsError):h._write_new_file(out,b'replacement')
            self.assertEqual(out.read_bytes(),b'original');self.assertEqual(list(Path(td).iterdir()),[out])



class AdditionalGraphAndRecoveryCoverage(unittest.TestCase):
    def test_explicit_target_record_shape_errors_are_controlled(self):
        data=graph_fixture()
        for row in (None,123,(),(0,),(0,b'name'),(0,b'name',b'x;',b'extra')):
            with self.subTest(row=row),self.assertRaises(h.BunFormatError):
                h.repack_module_sources(data,[row],expected_sha256=None)

    def test_raw_edit_index_and_type_contracts(self):
        image=h.BunImage(graph_fixture())
        for index in (True,-1,len(image.modules),'0'):
            with self.subTest(index=index),self.assertRaises(h.BunFormatError):
                h._apply_blob_edits(image,{(index,'contents'):b'changed();'})
        for value in (2,False,'text',None):
            with self.subTest(value=value),self.assertRaises(TypeError):
                h._apply_blob_edits(image,{(0,'contents'):value})

    def test_raw_asset_edits_do_not_silently_transcode(self):
        data=build_bun_elf([
            _module('/$bunfs/root/cli',b'hello();',module_format=1),
            _module('/$bunfs/root/asset.txt',b'old',encoding=1,loader=5,module_format=0)])
        image=h.BunImage(data)
        with self.assertRaises(NotImplementedError):
            h._apply_blob_edits(image,{(1,'contents'):'caf\u00e9'.encode('utf-8')})
        image=h.BunImage(graph_fixture())
        with self.assertRaises(NotImplementedError):
            h._apply_blob_edits(image,{(2,'contents'):b'new ASCII bytes'})

    def test_envelope_malformed_metadata_has_no_incidental_exception(self):
        header,marker=h._MULTI_MODULE_HEADER,h._MULTI_MODULE_MARKER
        name=b'/$bunfs/root/cli'.hex().encode()
        valid=marker+b'0 '+name+b'\nhello();'
        self.assertEqual(h.split_extracted_js(header+valid),[(0,b'/$bunfs/root/cli',b'hello();')])
        for label,tail in [('no-records',b''),('unterminated',marker+b'0 '+name),
            ('missing-name',marker+b'0\n'),('empty-name',marker+b'0 \n'),
            ('negative-index',marker+b'-1 '+name+b'\n'),('duplicate-index',valid+valid),
            ('non-integer',marker+b'x '+name+b'\n'),('bad-hex',marker+b'0 xx\n')]:
            with self.subTest(label=label),self.assertRaises(h.BunFormatError):
                h.split_extracted_js(header+tail)

    def test_identity_mask_coordinates_must_be_in_bounds(self):
        for ranges in ([(-1,1)],[(0,4)],[(2,1)]):
            with self.subTest(ranges=ranges),self.assertRaises(h.BunFormatError):
                h._equal_except(b'abc',b'abc',ranges)
        self.assertTrue(h._equal_except(b'abc',b'aBc',[(1,2)]))
        self.assertFalse(h._equal_except(b'abc',b'xbc',[(1,2)]))

    def test_nonempty_argv_terminator_is_owned(self):
        data=graph_fixture();image=h.BunImage(data)
        at=image.bun_section['foff']+image.section_header_size+image.compile_argv_off+image.compile_argv_len
        with self.assertRaises(h.BunFormatError):h.BunImage(change(data,at,'<B',88))

    def test_elf_allocated_and_file_virtual_mapping_are_distinct(self):
        data=graph_fixture();i=h.BunImage(data);s=i.bun_section
        for label,at,fmt,value in [('unallocated',s['hdr_off']+8,'<Q',s['flags']&~2),
             ('VA-file-offset-mismatch',s['hdr_off']+16,'<Q',s['vaddr']+128)]:
            bad=change(data,at,fmt,value)
            with self.subTest(label=label):
                self.assertFalse(h.can_handle(bad))
                with self.assertRaises(h.BunFormatError):h.repack_with_js(bad,h.extract_js(data)+b'\n//edit\n')

    def test_non_bun_allocated_virtual_overlap_even_without_file_payload(self):
        data=graph_fixture();i=h.BunImage(data);trail=i.elf.section('.trail')
        bad=change(data,trail['hdr_off']+4,'<I',8)
        bad=change(bad,trail['hdr_off']+8,'<Q',2)
        bad=change(bad,trail['hdr_off']+16,'<Q',i.bun_section['vaddr']+16)
        self.assertFalse(h.can_handle(bad))
        with self.assertRaises(h.BunFormatError):h.repack_with_js(bad,h.extract_js(data)+b'\n//edit\n')

    def test_source_only_recovery_rejects_digest_without_reference(self):
        from test_tweakcc_recovery import rebuild_fixture
        rebuilt=rebuild_fixture(graph_fixture())
        with self.assertRaisesRegex(h.BunFormatError,'requires reference'):
            h.recover_tweakcc(rebuilt,reference_sha256='0'*64)

    def test_recovery_compares_compile_arguments_to_reference(self):
        from test_tweakcc_recovery import rebuild_fixture
        original=graph_fixture();rebuilt=rebuild_fixture(original);i=h._TweakccRebuildImage(rebuilt)
        at=i.bun_section['foff']+i.section_header_size+i.compile_argv_off
        modified=change(rebuilt,at,'<B',ord('X'))
        self.assertNotEqual(h.extract_tweakcc_js(modified),b'')
        with self.assertRaisesRegex(h.BunFormatError,'argv'):
            h.recover_tweakcc(modified,original)

    def test_recovery_rejects_interior_origin_nul(self):
        from test_tweakcc_recovery import rebuild_fixture
        original=graph_fixture();rebuilt=rebuild_fixture(original);i=h._TweakccRebuildImage(rebuilt)
        off,n=i.modules[0]['bytecodeOriginPath'];self.assertGreater(n,1)
        bad=change(rebuilt,i.bun_section['foff']+i.section_header_size+off+1,'<B',0)
        with self.assertRaises(h.BunFormatError):h.extract_tweakcc_js(bad)

    def test_recovery_invalid_discriminants_in_non_source_asset(self):
        from test_tweakcc_recovery import rebuild_fixture
        rebuilt=rebuild_fixture(graph_fixture());i=h._TweakccRebuildImage(rebuilt)
        at=i.bun_section['foff']+i.section_header_size+i.modules_off+2*i.module_struct_size+i.module_struct_size-4
        for index,value in [(0,3),(1,255),(2,3),(3,2)]:
            with self.subTest(index=index),self.assertRaises(h.BunFormatError):
                h.extract_tweakcc_js(change(rebuilt,at+index,'<B',value))

    def test_utf16_assets_require_even_storage_in_source_only_recovery(self):
        from test_tweakcc_recovery import rebuild_fixture
        original=build_bun_elf([
            _module('/$bunfs/root/cli',b'hello();',module_format=1),
            _module('/$bunfs/root/asset',b'x\0',encoding=2,loader=5,module_format=0)])
        rebuilt=rebuild_fixture(original,{(1,'contents'):b'x'})
        with self.assertRaisesRegex(h.BunFormatError,'odd byte length'):h.recover_tweakcc(rebuilt)



class RemainingSemanticCoverage(unittest.TestCase):
    def test_utf16_replacement_removes_both_old_terminator_bytes(self):
        # No other live source/cache imposes a padding requirement here; the
        # meaningfully sized PE graph must shrink by the exact storage delta.
        original='console.log("original UTF16 source");'.encode('utf-16-le')
        data=pe_fixture(signed=False,source=original,encoding=2)
        before=h.BunImage(data);source=b'console.log("new");'
        out=h.repack_with_js(data,source);after=h.BunImage(out)
        expected=len(source)+1-(len(original)+2)
        self.assertEqual(after.bun_section['size']-before.bun_section['size'],expected)
        self.assertEqual(h.extract_js(out),source)

    def test_pe_image_extent_changes_when_crossing_virtual_pages(self):
        data=pe_fixture(signed=False);before=h.Pe64(data)
        larger=h.extract_js(data)+b'\n/*'+b'A'*12000+b'*/'
        out=h.repack_with_js(data,larger)
        raw=struct.unpack_from('<I',out,before.bun_section['hdr_off']+16)[0]
        expected=h._align_up(before.bun_section['rva']+raw,before.section_alignment)
        self.assertEqual(struct.unpack_from('<I',out,before.optional_off+56)[0],expected)
        self.assertEqual(struct.unpack_from('<I',out,before.optional_off+8)[0],
                         before.initialized_size+raw-before.bun_section['raw_size'])
        after=h.Pe64(out)
        self.assertGreater(after.image_size,before.image_size)
        self.assertEqual(h.extract_js(out),larger)
        smaller=h.repack_with_js(out,b'hello();');shrunk=h.Pe64(smaller)
        self.assertLess(shrunk.image_size,after.image_size)
        self.assertEqual(h.extract_js(smaller),b'hello();')

    def test_source_only_recovery_retains_complete_argv(self):
        from test_tweakcc_recovery import rebuild_fixture
        original=graph_fixture();before=h.BunImage(original)
        rebuilt=rebuild_fixture(original,{(0,'contents'):b'console.log("changed");'})
        out=h.recover_tweakcc(rebuilt);after=h.BunImage(out)
        expected=bytes(before.blob[before.compile_argv_off:before.compile_argv_off+before.compile_argv_len])
        actual=bytes(after.blob[after.compile_argv_off:after.compile_argv_off+after.compile_argv_len])
        self.assertEqual(expected,b'--smol')
        self.assertEqual(actual,expected)



class TightWriterOracles(unittest.TestCase):
    def test_same_size_edit_invalidates_only_changed_metadata(self):
        data=build_bun_elf([
            _module('/$bunfs/root/cli',b'console.log("original");',module_format=1,
                    bytecode=b'cached-main',module_info=b'info-main',sourcemap=b'map-main'),
            _module('/$bunfs/root/other.js',b'export let x=1;',module_format=1,
                    bytecode=b'cached-other',module_info=b'info-other',sourcemap=b'map-other')],flags=0x2f)
        before=h.BunImage(data)
        out=h.repack_module_sources(data,[(0,b'/$bunfs/root/cli',b'console.log("ORIGINAL");')],expected_sha256=None)
        self.assertEqual(len(out),len(data))
        base=before.bun_section['foff']+before.section_header_size
        record=base+before.modules_off
        # Raw field oracles intentionally run BEFORE constructing the output
        # parser. A stale pointer is diagnosed as that pointer, not indirectly.
        for name,offset in [('sourcemap',16),('bytecode',24),('moduleInfo',32)]:
            with self.subTest(field=name):
                self.assertEqual(struct.unpack_from('<II',out,record+offset),(0,0))
        hashes=base+before.source_hashes_off
        self.assertEqual(struct.unpack_from('<I',out,hashes)[0],0)
        self.assertEqual(out[hashes+4:hashes+8],data[hashes+4:hashes+8])
        self.assertEqual(out[record+52:record+104],data[record+52:record+104])
        for module in before.modules:
            for field in ('bytecode','moduleInfo','sourcemap'):
                off,length=module[field]
                self.assertEqual(out[base+off:base+off+length],data[base+off:base+off+length])
        self.assertEqual(h.BunImage(out).read_source(h.BunImage(out).modules[0]),b'console.log("ORIGINAL");')

    def test_pe_edit_clears_exact_signature_header_fields(self):
        data=pe_fixture();before=h.Pe64(data)
        out=h.repack_with_js(data,h.extract_js(data).replace(b'hello',b'HELLO'))
        self.assertNotEqual(out,data)
        self.assertEqual(struct.unpack_from('<II',out,before.security_off),(0,0))
        self.assertEqual(struct.unpack_from('<I',out,before.checksum_off)[0],0)
        self.assertEqual(len(out),before.raw_end)



class ReferenceAndMappingCoverage(unittest.TestCase):
    def test_pe_reference_must_preserve_native_code_bytes(self):
        from test_bun_windows import append_pr_pe,rebuild_tweakcc_pe
        original=build_bun_pe(cert=False,modern=True)
        for label,current in [('append',append_pr_pe(original,b'console.log("new");')),
                              ('rebuild',rebuild_tweakcc_pe(original,b'console.log("new");'))]:
            pe=h.Pe64(current);code=pe.sections[0]['foff']
            modified=change(current,code,'<B',current[code]^1)
            with self.subTest(label=label),self.assertRaisesRegex(h.BunFormatError,'native PE|PE native'):
                if label=='append':h.compact_with_reference(modified,original)
                else:h.recover_tweakcc(modified,original)

    def test_reference_rejects_modified_pe_retaining_certificate(self):
        from test_bun_windows import append_pr_pe
        original=build_bun_pe(cert=True,modern=True);ref=h.Pe64(original)
        current=append_pr_pe(original,b'console.log("new");');pe=h.Pe64(current)
        signed=bytearray(current)+original[ref.certificate_off:ref.certificate_off+ref.certificate_size]
        struct.pack_into('<II',signed,pe.security_off,len(current),ref.certificate_size)
        # Certificate structure is valid. Trust/authenticity is NOT asserted.
        self.assertTrue(h.can_handle(bytes(signed)))
        with self.assertRaisesRegex(h.BunFormatError,'certificate'):
            h.compact_with_reference(bytes(signed),original)

    def test_reference_recovery_must_not_discard_asset_encoding_change(self):
        from test_tweakcc_recovery import rebuild_fixture
        original=graph_fixture();current=rebuild_fixture(original)
        image=h._TweakccRebuildImage(current)
        at=image.bun_section['foff']+image.section_header_size+image.modules_off+2*52+48
        self.assertEqual(image.modules[2]['encoding'],0)
        modified=change(current,at,'<B',1)
        self.assertEqual(h._TweakccRebuildImage(modified).modules[2]['encoding'],1)
        with self.assertRaisesRegex(h.BunFormatError,'identity/encoding'):
            h.recover_tweakcc(modified,original)

    def test_elf_mapping_relation_with_both_extents_still_inside_load(self):
        data=graph_fixture();image=h.BunImage(data);section=image.bun_section
        # Keep all runtime alignment and segment-containment constraints valid,
        # but break VA/file-offset correspondence by a multiple of 128.
        modified=change(data,section['hdr_off']+16,'<Q',section['vaddr']-128)
        self.assertEqual(len(h._find_containing_load_segments(h.Elf64(modified),h.Elf64(modified).section('.bun'))),1)
        with self.assertRaisesRegex(h.BunFormatError,'file offset and virtual address'):
            h.repack_with_js(modified,h.extract_js(data)+b'\n//edit\n')

    def test_anonymous_mapped_tail_must_remain_zero(self):
        data=graph_fixture();image=h.BunImage(data);elf=image.elf;section=image.bun_section
        end=section['foff']+section['size'];n=8
        out=bytearray(data[:end])+b'\0'*n+data[end:]
        table=elf.e_shoff+n;struct.pack_into('<Q',out,40,table)
        for s in elf.sections:
            at=table+s['index']*elf.e_shentsize
            if s['foff']>=end and h._section_has_payload(s):
                struct.pack_into('<Q',out,at+24,s['foff']+n)
        load=next(x for x in elf.segments if x['type']==1)
        struct.pack_into('<QQ',out,load['hdr_off']+32,load['filesz']+n,load['memsz']+n)
        self.assertTrue(h.can_handle(bytes(out)))
        out[end]=88
        self.assertFalse(h.can_handle(bytes(out)))
        with self.assertRaisesRegex(h.BunFormatError,'nonzero unsectioned'):
            h.repack_with_js(bytes(out),h.extract_js(data)+b'\n//edit\n')



def raw_graph_result(before, output, test):
    """Bounded raw result oracle: no production output parser is called."""
    base=before.bun_section['foff']+before.section_header_size
    fmt='<Q' if before.section_header_size==8 else '<I'
    size=struct.unpack_from(fmt,output,before.bun_section['foff'])[0]
    test.assertLessEqual(base+size,len(output))
    graph=output[base:base+size]
    trailer=h.BUN_TRAILER
    tail=len(graph)-len(trailer)-(1 if graph.endswith(trailer+b'\0') else 0)
    test.assertEqual(graph[tail:tail+len(trailer)],trailer)
    at=tail-32;test.assertGreaterEqual(at,0)
    offsets=struct.unpack_from('<QIIIIII',graph,at)
    test.assertEqual(offsets[0],at,'serialized byte_count must identify its new offsets structure')
    test.assertLessEqual(offsets[1]+offsets[2],at)
    return graph,offsets


class RawRelocationMutationCoverage(unittest.TestCase):
    def setUp(self):
        self.data=graph_fixture();self.before=h.BunImage(self.data)
        self.growth=b'/*'+b'X'*124+b'*/'
        self.assertEqual(len(self.growth),128)
    def edited(self):
        m=self.before.modules[0]
        return h.repack_module_sources(self.data,[(0,m['name_bytes'],self.before.read_source(m)+self.growth)],expected_sha256=None)
    def test_elf_growth_updates_each_native_size_and_offset(self):
        out=self.edited();before=self.before;elf=before.elf
        self.assertEqual(len(out)-len(self.data),128)
        table=elf.e_shoff+128
        self.assertEqual(struct.unpack_from('<Q',out,40)[0],table,'ELF section-header-table relocation')
        for section in elf.sections:
            at=table+section['index']*elf.e_shentsize
            with self.subTest(section=section['name']):
                if section['index']==before.bun_section['index']:
                    self.assertEqual(struct.unpack_from('<Q',out,at+32)[0],section['size']+128,'.bun logical size')
                elif h._section_has_payload(section) and section['foff']>=before.bun_section['foff']+before.bun_section['size']:
                    self.assertEqual(struct.unpack_from('<Q',out,at+24)[0],section['foff']+128,'non-Bun tail payload offset')
        load=next(s for s in elf.segments if s['type']==1)
        self.assertEqual(struct.unpack_from('<Q',out,load['hdr_off']+32)[0],load['filesz']+128,'PT_LOAD file size')
        self.assertEqual(struct.unpack_from('<Q',out,load['hdr_off']+40)[0],load['memsz']+128,'PT_LOAD memory size')
    def test_graph_growth_remaps_argv_and_optional_table_pointers(self):
        out=self.edited();before=self.before;blob,offsets=raw_graph_result(before,out,self)
        self.assertEqual(offsets[1],before.modules_off+128,'module-table pointer')
        self.assertEqual(offsets[4],before.compile_argv_off+128,'compile argv pointer')
        self.assertEqual(offsets[5],before.compile_argv_len,'compile argv length')
        for e in before.builtin_bytecode:
            at=e['record_off']+128
            self.assertEqual(struct.unpack_from('<III',blob,at),(e['id'],e['bytes'][0]+128,e['bytes'][1]),'builtin-bytecode pointer')
        for pointer,slot in [(before.bytecode_string_table,before.bytecode_string_table_record_off),
                             (before.module_info_string_table,before.module_info_string_table_record_off)]:
            self.assertEqual(struct.unpack_from('<II',blob,slot+128),(pointer[0]+128,pointer[1]),'shared-table pointer')
    def test_one_byte_growth_preserves_live_compiled_alignment(self):
        before=self.before;m=before.modules[0]
        out=h.repack_module_sources(self.data,[(0,m['name_bytes'],before.read_source(m)+b' ')],expected_sha256=None)
        blob,offsets=raw_graph_result(before,out,self);table=offsets[1]
        pointer,length=struct.unpack_from('<II',blob,table+52+24)
        self.assertGreater(length,0)
        self.assertEqual((before._runtime_blob_vaddr()+pointer)%128,0,'unchanged module bytecode alignment')
    def test_elf_metadata_alignment_without_live_compiled_regions(self):
        data=build_bun_elf([_module('/$bunfs/root/cli',b'hello();',module_format=1)])
        before=h.BunImage(data);out=h.repack_with_js(data,b'hello(); ')
        after_shoff=struct.unpack_from('<Q',out,40)[0]
        self.assertEqual(after_shoff%8,before.elf.e_shoff%8,'natural section-header-table alignment')
    def test_field_starting_exactly_at_splice_end_is_remapped(self):
        data=build_bun_elf([
            _module('/$bunfs/root/cli',b'hello();',module_format=1),
            _module('/$bunfs/root/next.js',b'export {};',module_format=1)])
        before=h.BunImage(data);m=before.modules[0]
        self.assertEqual(before.modules[1]['name'][0],sum(m['contents'])+1)
        out=h.repack_module_sources(data,[(0,m['name_bytes'],before.read_source(m)+self.growth)],expected_sha256=None)
        blob,offsets=raw_graph_result(before,out,self)
        self.assertEqual(struct.unpack_from('<II',blob,offsets[1]+52),(before.modules[1]['name'][0]+128,before.modules[1]['name'][1]))
    def test_new_empty_source_allocation_has_its_own_terminator(self):
        data=build_bun_elf([_module('/$bunfs/root/cli',b'',module_format=1)])
        before=h.BunImage(data);out=h.repack_with_js(data,b'hello();')
        blob,offsets=raw_graph_result(before,out,self)
        pointer,length=struct.unpack_from('<II',blob,offsets[1]+8)
        self.assertEqual(blob[pointer:pointer+length],b'hello();')
        self.assertEqual(blob[pointer+length],0,'allocated source terminator must not alias the module table')
    def test_publication_contains_every_requested_byte(self):
        with tempfile.TemporaryDirectory() as td:
            output=Path(td)/'new-file';data=b'bounded test payload\xff\x00'
            h._write_new_file(output,data)
            self.assertEqual(output.read_bytes(),data)
    def test_digest_is_of_executable_not_extracted_source(self):
        import hashlib
        digest,source=h.extract_js_with_digest(self.data)
        self.assertEqual(digest,hashlib.sha256(self.data).hexdigest())
        self.assertNotEqual(digest,hashlib.sha256(source).hexdigest())

if __name__=='__main__':unittest.main(verbosity=2)
