"""Known graph records 0x800/0x1000, with raw-byte result assertions.

Synthetic prelinked payloads are opaque test bytes, never executed. Header and
record construction uses struct, not the new parser's optional-record reader.
Existing ELF/PE builders supply only the previously supported base containers.
"""
import hashlib
import struct
import unittest

import bun_handler as h
from test_bun_handler import _module, build_bun_elf
from test_bun_windows import build_bun_pe
from test_bun_interop import append_sources
from test_tweakcc_recovery import rebuild_fixture

PRELINK = 0x800
OPTIONS = 0x1000
OPTIONS_BYTES = struct.pack('<II', 2, 0x41000000)  # known flag plus f32 8.0 bits
CACHE = b'PLMG-fixture-not-executed\0' + b'G' * 231


def base_elf(width=52, wrapper=8, argv=b'--smol'):
    return build_bun_elf([
        _module('/$bunfs/root/cli', b'console.log("original");',
                bytecode=b'old-main-cache', module_info=b'old-info' if width == 52 else b'',
                module_format=1),
        _module('/$bunfs/root/helper.js', b'export const answer = 42;',
                bytecode=b'helper-cache', module_info=b'helper-info' if width == 52 else b'',
                module_format=1),
        _module('/$bunfs/root/asset.txt', b'ascii-asset', module_format=0, loader=5),
    ], module_struct_size=width, section_header_size=wrapper,
       flags=0x3ef, builtin_bytecode=[(1,b'builtin-cache')],
       bytecode_string_table=b'string-table',module_info_string_table=b'info-table',
       startup_module_count=1,compile_argv=argv)


def add_records(data, *, mapping=(0, 1), cache=CACHE, options=OPTIONS_BYTES,
                prelink=True):
    """Insert one opaque pre-table cache and flag-ordered metadata by construction."""
    i = h.BunImage(data)
    pad = (-(i._runtime_blob_vaddr() + i.modules_off)) % 128
    block = b'\0' * pad + cache if prelink and cache else b''
    graph_ptr = (i.modules_off + pad, len(cache)) if prelink and cache else (0,0)
    record = struct.pack('<III', *graph_ptr, len(mapping)) if prelink else b''
    if prelink:
        record += b''.join(struct.pack('<I', n) for n in mapping)
    if options is not None:
        record += options
    new_table = i.modules_off + len(block)
    optional_start = i.optional_tail_end + len(block)
    blob = (bytearray(i.blob[:i.modules_off]) + block
            + i.blob[i.modules_off:i.optional_tail_end] + record
            + i.blob[i.optional_tail_end:i.offsets_off])
    alignment = h._required_container_shift_alignment(i.container, i.bun_section['foff'] + i.bun_section['size'])
    delta = len(block) + len(record)
    blob.extend(b'\0' * (-delta % alignment))
    off = len(blob)
    flags = i.flags | (PRELINK if prelink else 0) | (OPTIONS if options is not None else 0)
    blob.extend(struct.pack('<QIIIIII', off, new_table, i.modules_len,
        i.entry_point_id, i.compile_argv_off + len(block) + len(record), i.compile_argv_len, flags))
    blob.extend(i.blob[i.offsets_off + 32:])
    return h._repack_blob(data, blob, i.section_header_size), dict(
        record=optional_start, map=optional_start+12,
        options=optional_start+(12+4*len(mapping) if prelink else 0),
        graph=graph_ptr, options_bytes=options)


def change(data, relative, fmt, *values):
    i = h.BunImage(data)
    out = bytearray(data)
    struct.pack_into(fmt, out, i.bun_section['foff'] + i.section_header_size + relative, *values)
    return bytes(out)


def read_raw_record(out, original, record_offset):
    # Same-size test edits retain graph/section placement; do not reparse output
    # before checking the specific writer bytes that the test is about.
    i=h.BunImage(original)
    return struct.unpack_from('<III',out,i.bun_section['foff']+i.section_header_size+record_offset)


class GraphFlags278Tests(unittest.TestCase):
    def setUp(self):
        self.data,self.meta=add_records(base_elf())
        self.image=h.BunImage(self.data)

    def test_new_flags_have_exact_known_values(self):
        self.assertEqual(h.BUN_FLAG_HAS_PRELINKED_MODULE_GRAPH,0x800)
        self.assertEqual(h.BUN_FLAG_HAS_RUNTIME_OPTIONS,0x1000)
        self.assertEqual(h.BUN_KNOWN_FLAGS,0x3fff)

    def test_active_cache_and_map_are_read(self):
        i=self.image
        self.assertEqual(i.prelinked_module_graph,self.meta['graph'])
        self.assertEqual(i.prelinked_module_files,(0,1))
        self.assertEqual(i.runtime_options,(2,0x41000000))
        self.assertEqual(i.runtime_options_bytes,OPTIONS_BYTES)
        self.assertEqual(i.runtime_options_record_off,self.meta['options'])

    def test_active_prelink_noop_is_exact(self):
        self.assertEqual(h.repack_unchanged(self.data),self.data)
        self.assertEqual(h.repack_with_js(self.data,h.extract_js(self.data)),self.data)
        self.assertEqual(h.repack_module_sources(self.data,[],expected_sha256=None),self.data)

    def test_equal_edit_detaches_pointer_and_map_before_reparsing(self):
        edited=h.repack_with_js(self.data,h.extract_js(self.data).replace(b'original',b'ORIGINAL'))
        self.assertEqual(read_raw_record(edited,self.data,self.meta['record']),(0,0,0))
        self.assertEqual(len(edited),len(self.data))
        after=h.BunImage(edited)
        self.assertEqual(after.prelinked_module_graph,(0,0))
        self.assertEqual(after.prelinked_module_files,())
        self.assertEqual(after.flags,self.image.flags)

    def test_runtime_options_follow_tombstone_not_old_map(self):
        out=h.repack_with_js(self.data,h.extract_js(self.data).replace(b'original',b'ORIGINAL'))
        start=self.image.bun_section['foff']+self.image.section_header_size
        self.assertEqual(out[start+self.meta['record']+12:start+self.meta['record']+20],OPTIONS_BYTES)
        self.assertEqual(h.BunImage(out).runtime_options_bytes,OPTIONS_BYTES)

    def test_graph_cache_payload_remains_physically_identical(self):
        out=h.repack_with_js(self.data,h.extract_js(self.data).replace(b'original',b'ORIGINAL'))
        p,n=self.meta['graph'];start=self.image.bun_section['foff']+self.image.section_header_size
        self.assertEqual(out[start+p:start+p+n],CACHE)

    def test_unchanged_module_cache_and_hash_are_preserved(self):
        out=h.repack_with_js(self.data,h.extract_js(self.data).replace(b'original',b'ORIGINAL'))
        j=h.BunImage(out)
        for field in ('contents','bytecode','moduleInfo','sourcemap','name'):
            self.assertEqual(j.read_field(j.modules[1],field),self.image.read_field(self.image.modules[1],field))
        self.assertEqual(j.source_hash(1),self.image.source_hash(1))
        self.assertEqual(j.modules[0]['bytecode'],(0,0))
        self.assertEqual(j.modules[0]['moduleInfo'],(0,0))

    def test_second_equal_edit_does_not_grow_or_move_options(self):
        first=h.repack_with_js(self.data,h.extract_js(self.data).replace(b'original',b'ORIGINAL'))
        second=h.repack_with_js(first,h.extract_js(first).replace(b'ORIGINAL',b'original'))
        self.assertEqual(len(first),len(second))
        self.assertEqual(h.BunImage(second).runtime_options_bytes,OPTIONS_BYTES)
        self.assertEqual(h.repack_unchanged(second),second)

    def test_growth_and_shrink_keep_runtime_and_argv(self):
        for text in (b'x',b'long-new-source'*70,b''):
            with self.subTest(size=len(text)):
                o=h.repack_module_sources(self.data,[(0,self.image.modules[0]['name_bytes'],text)],expected_sha256=None)
                j=h.BunImage(o)
                self.assertEqual(j.read_source(j.modules[0]),text)
                self.assertEqual(j.runtime_options_bytes,OPTIONS_BYTES)
                self.assertEqual(bytes(j.blob[j.compile_argv_off:j.compile_argv_off+j.compile_argv_len]),b'--smol')
                self.assertEqual(j.prelinked_module_files,())

    def test_unicode_and_empty_sources(self):
        for text in ('čaj 🦆',''):
            o=h.repack_module_sources(self.data,[(1,self.image.modules[1]['name_bytes'],text.encode())],expected_sha256=None)
            j=h.BunImage(o)
            self.assertEqual(j.read_source(j.modules[1]),text.encode())
            self.assertEqual(j.runtime_options_bytes,OPTIONS_BYTES)
            self.assertEqual(j.prelinked_module_graph,(0,0))

    def test_runtime_options_without_prelinked_graph(self):
        data,_=add_records(base_elf(),prelink=False)
        self.assertEqual(h.repack_unchanged(data),data)
        out=h.repack_with_js(data,h.extract_js(data).replace(b'original',b'ORIGINAL'))
        self.assertEqual(h.BunImage(out).runtime_options_bytes,OPTIONS_BYTES)
        self.assertFalse(h.BunImage(out).flags&PRELINK)

    def test_prelinked_graph_without_runtime_options(self):
        data,meta=add_records(base_elf(),options=None)
        out=h.repack_with_js(data,h.extract_js(data).replace(b'original',b'ORIGINAL'))
        self.assertEqual(read_raw_record(out,data,meta['record']),(0,0,0))
        self.assertEqual(h.BunImage(out).runtime_options,None)
        self.assertEqual(len(out),len(data))

    def test_scalar_options_bits_are_preserved_without_reinterpretation(self):
        raw=struct.pack('<II',0x80000002,0xdeadbeef)
        data,_=add_records(base_elf(),options=raw)
        out=h.repack_with_js(data,h.extract_js(data).replace(b'original',b'ORIGINAL'))
        self.assertEqual(h.BunImage(out).runtime_options_bytes,raw)

    def test_empty_prelinked_record_is_valid(self):
        data,_=add_records(base_elf(),cache=b'',mapping=())
        self.assertEqual(h.repack_unchanged(data),data)
        out=h.repack_with_js(data,h.extract_js(data).replace(b'original',b'ORIGINAL'))
        self.assertEqual(h.BunImage(out).runtime_options_bytes,OPTIONS_BYTES)

    def test_one_entry_map_relocates_options_without_extra_space(self):
        data,meta=add_records(base_elf(),mapping=(0,))
        out=h.repack_with_js(data,h.extract_js(data).replace(b'original',b'ORIGINAL'))
        self.assertEqual(read_raw_record(out,data,meta['record']),(0,0,0))
        self.assertEqual(h.BunImage(out).runtime_options_bytes,OPTIONS_BYTES)
        self.assertEqual(len(out),len(data))

    def test_zero_source_allocation_with_prelink(self):
        data,_=add_records(build_bun_elf([_module('/$bunfs/root/cli',b'',module_format=1)]),mapping=(0,))
        out=h.repack_with_js(data,b'export const value=1;')
        j=h.BunImage(out)
        self.assertEqual(j.extract_js(),b'export const value=1;')
        self.assertEqual(j.runtime_options_bytes,OPTIONS_BYTES)
        self.assertEqual(j.prelinked_module_files,())

    def test_non_source_private_edit_also_detaches_global_cache(self):
        blob=h._apply_blob_edits(self.image,{(2,'contents'):b'ASCII-ASSET'})
        self.assertEqual(struct.unpack_from('<III',blob,self.meta['record']),(0,0,0))

    def test_count_is_bounded_before_map_allocation(self):
        bad=change(self.data,self.meta['record']+8,'<I',0xffffffff)
        with self.assertRaisesRegex(h.BunFormatError,'count exceeds module count'):
            h.BunImage(bad)

    def test_map_index_out_of_range(self):
        bad=change(self.data,self.meta['map']+4,'<I',3)
        with self.assertRaisesRegex(h.BunFormatError,'file index is out of range'):
            h.BunImage(bad)

    def test_duplicate_map_index(self):
        bad=change(self.data,self.meta['map']+4,'<I',0)
        with self.assertRaisesRegex(h.BunFormatError,'duplicate indices'):
            h.BunImage(bad)

    def test_empty_pointer_cannot_have_live_map(self):
        bad=change(self.data,self.meta['record'],'<II',0,0)
        with self.assertRaisesRegex(h.BunFormatError,'graph and file map disagree'):
            h.BunImage(bad)

    def test_empty_graph_tombstone_has_zero_offset(self):
        data,meta=add_records(base_elf(),cache=b'',mapping=())
        bad=change(data,meta['record'],'<I',128)
        with self.assertRaisesRegex(h.BunFormatError,'graph and file map disagree'):
            h.BunImage(bad)

    def test_nonempty_graph_cannot_have_empty_map(self):
        # options are deliberately absent so only the graph/count relation matters.
        data,meta=add_records(base_elf(),options=None)
        bad=change(data,meta['record']+8,'<I',0)
        with self.assertRaisesRegex(h.BunFormatError,'graph and file map disagree'):
            h.BunImage(bad)

    def test_prelinked_payload_out_of_blob(self):
        bad=change(self.data,self.meta['record'],'<II',len(self.image.blob)-1,128)
        with self.assertRaisesRegex(h.BunFormatError,'prelinked-module-graph.*outside'):
            h.BunImage(bad)

    def test_prelinked_payload_must_be_runtime_aligned(self):
        p,n=self.meta['graph'];bad=change(self.data,self.meta['record'],'<II',p+1,n-1)
        with self.assertRaisesRegex(h.BunFormatError,'prelinked-module-graph.*128-byte aligned'):
            h.BunImage(bad)

    def test_prelinked_payload_cannot_alias_bytecode(self):
        pointer=self.image.modules[0]['bytecode']
        bad=change(self.data,self.meta['record'],'<II',*pointer)
        with self.assertRaisesRegex(h.BunFormatError,'embedded regions overlap'):
            h.BunImage(bad)

    def test_prelinked_payload_cannot_own_table_bytes(self):
        bad=change(self.data,self.meta['record'],'<II',self.image.modules_off,128)
        with self.assertRaisesRegex(h.BunFormatError,'prelinked-module-graph.*module table'):
            h.BunImage(bad)

    def test_runtime_options_truncation_is_bounded(self):
        data=base_elf(argv=b'')
        i=h.BunImage(data);bad=change(data,i.offsets_off+28,'<I',i.flags|OPTIONS)
        with self.assertRaisesRegex(h.BunFormatError,'runtime-options record runs past'):
            h.BunImage(bad)

    def test_prelink_header_truncation_is_bounded(self):
        data=base_elf(argv=b'')
        i=h.BunImage(data);bad=change(data,i.offsets_off+28,'<I',i.flags|PRELINK)
        with self.assertRaisesRegex(h.BunFormatError,'prelinked-module-graph header record runs past'):
            h.BunImage(bad)

    def test_still_refuses_unknown_outer_flag(self):
        bad=change(self.data,self.image.offsets_off+28,'<I',self.image.flags|0x4000)
        with self.assertRaisesRegex(NotImplementedError,'unknown flag bits 0x4000'):
            h.BunImage(bad)

    def test_elf_width_matrix(self):
        for wrapper in (4,8):
            for width in (36,52):
                with self.subTest(wrapper=wrapper,width=width):
                    data,_=add_records(base_elf(width,wrapper))
                    out=h.repack_with_js(data,h.extract_js(data).replace(b'original',b'ORIGINAL'))
                    j=h.BunImage(out)
                    self.assertEqual(j.section_header_size,wrapper)
                    self.assertEqual(j.module_struct_size,width)
                    self.assertEqual(j.runtime_options_bytes,OPTIONS_BYTES)
                    self.assertEqual(h.repack_unchanged(out),out)

    def test_pe_width_matrix(self):
        for wrapper in (4,8):
            with self.subTest(wrapper=wrapper):
                base=build_bun_pe(cert=False,modern=True,section_header_size=wrapper)
                data,_=add_records(base,mapping=(0,))
                out=h.repack_with_js(data,h.extract_js(data).replace(b'Claude Code',b'ClauDe Code'))
                j=h.BunImage(out)
                self.assertEqual(j.section_header_size,wrapper)
                self.assertEqual(j.runtime_options_bytes,OPTIONS_BYTES)
                self.assertEqual(len(out),len(data))
                self.assertEqual(h.repack_unchanged(out),out)

    def test_compact_canonical_output_is_idempotent(self):
        edited=h.repack_with_js(self.data,h.extract_js(self.data).replace(b'original',b'ORIGINAL'))
        self.assertEqual(h.compact_with_reference(edited,self.data),edited)

    def test_append_repatch_and_compaction(self):
        appended=append_sources(self.data,{0:b'console.log("patched!");'})
        edited=h.repack_with_js(appended,h.extract_js(appended).replace(b'patched!',b'PATCHED!'))
        compact=h.compact_with_reference(edited,self.data)
        self.assertEqual(h.extract_js(edited),h.extract_js(compact))
        self.assertLess(len(compact),len(edited))
        self.assertEqual(h.BunImage(compact).runtime_options_bytes,OPTIONS_BYTES)
        self.assertEqual(h.compact_with_reference(compact,self.data),compact)

    def test_compaction_rejects_changed_runtime_policy(self):
        edited=h.repack_with_js(self.data,h.extract_js(self.data).replace(b'original',b'ORIGINAL'))
        i=h.BunImage(edited);bad=change(edited,i.runtime_options_record_off+4,'<I',0x3f800000)
        with self.assertRaisesRegex(h.BunFormatError,'reference runtime options differ'):
            h.compact_with_reference(bad,self.data)

    def test_append_compaction_rejects_changed_runtime_policy(self):
        appended=append_sources(self.data,{0:b'console.log("patched!");'})
        edited=h.repack_with_js(appended,h.extract_js(appended).replace(b'patched!',b'PATCHED!'))
        i=h.BunImage(edited);bad=change(edited,i.runtime_options_record_off+4,'<I',0x3f800000)
        with self.assertRaisesRegex(h.BunFormatError,'reference runtime options differ'):
            h.compact_with_reference(bad,self.data)

    def test_compaction_rejects_nonmasked_old_mapping_changes(self):
        appended=append_sources(self.data,{0:b'console.log("patched!");'})
        edited=h.repack_with_js(appended,h.extract_js(appended).replace(b'patched!',b'PATCHED!'))
        # Two map entries are overwritten by relocated options. The old options
        # remain inert but byte-verified, so unrelated changes there must fail.
        bad=change(edited,self.meta['options']+4,'<I',1)
        with self.assertRaisesRegex(h.BunFormatError,'Bun prefix differs'):
            h.compact_with_reference(bad,self.data)

    def test_reference_recovery_restores_options_and_detaches_link_cache(self):
        damaged=rebuild_fixture(self.data,{(0,'contents'):b'console.log("updated!");'})
        out=h.recover_tweakcc(damaged,self.data)
        j=h.BunImage(out)
        self.assertEqual(j.runtime_options_bytes,OPTIONS_BYTES)
        self.assertEqual(j.prelinked_module_graph,(0,0))
        self.assertEqual(j.read_source(j.modules[0]),b'console.log("updated!");')

    def test_source_only_recovery_must_not_guess_discarded_options(self):
        damaged=rebuild_fixture(self.data,{(0,'contents'):b'console.log("updated!");'})
        with self.assertRaisesRegex(h.BunFormatError,'discarded runtime options.*reference is required'):
            h.recover_tweakcc(damaged)

    def test_source_only_recovery_can_drop_prelink_when_no_options_lost(self):
        data,_=add_records(base_elf(),options=None)
        damaged=rebuild_fixture(data,{(0,'contents'):b'console.log("updated!");'})
        out=h.recover_tweakcc(damaged);j=h.BunImage(out)
        self.assertFalse(j.flags & (PRELINK|OPTIONS))
        self.assertEqual(j.read_source(j.modules[0]),b'console.log("updated!");')

    def test_recovery_noop_with_reference_preserves_active_graph(self):
        damaged=rebuild_fixture(self.data)
        self.assertEqual(h.recover_tweakcc(damaged,self.data),self.data)


if __name__=='__main__':
    unittest.main(verbosity=2)
