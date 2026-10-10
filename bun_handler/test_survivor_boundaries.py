"""Survivor follow-up: realistic boundary fixtures, no native executable runs.

Fixtures begin from real parsed synthetic images; bytes are changed explicitly.
No production guard is disabled during the control run. These tests neither
inspect source code nor carry mutant IDs. Unexpected exceptions remain test errors.
"""
import struct, unittest
import bun_handler as h
from test_bun_handler import _module, build_bun_elf
from test_bun_interop import fixture as graph_fixture, append_sources
from test_tweakcc_recovery import rebuild_fixture
from test_bun_windows import build_bun_pe
from test_pe_backend import fixture as pe_fixture


def packed(data, at, fmt, *values):
    out=bytearray(data);struct.pack_into(fmt,out,at,*values);return bytes(out)


def blob_change(data, at, payload):
    image=h.BunImage(data);out=bytearray(data)
    absolute=image.bun_section["foff"]+image.section_header_size+at
    out[absolute:absolute+len(payload)]=payload
    return bytes(out)


def graph(**kwargs):
    return build_bun_elf([
        _module("/$bunfs/root/cli", b"console.log('original');", module_format=1),
        _module("/$bunfs/root/mod", b"export const x=1;", module_format=1),
        _module("/$bunfs/root/dat", b"plain text asset", loader=13,module_format=0),
    ], flags=0xf, **kwargs)


def insert_empty_prefix(data, count=1024):
    """Add unowned pre-table space to a valid graph, adjusting all its pointers."""
    image=h.BunImage(data);assert image.flags==0xf
    blob=bytearray(count)+image.blob
    fields=h._FIELD_ORDER_52 if image.module_struct_size==52 else h._FIELD_ORDER_36
    for mod in image.modules:
        for j,field in enumerate(fields):
            off,n=mod[field]
            struct.pack_into("<II",blob,count+image.modules_off+mod["index"]*image.module_struct_size+j*8,
                             off+count if n else off,n)
    oo=image.offsets_off+count
    struct.pack_into("<Q",blob,oo,oo)
    struct.pack_into("<I",blob,oo+8,image.modules_off+count)
    struct.pack_into("<I",blob,oo+20,image.compile_argv_off+count)
    return h._repack_blob_elf(data,blob,image.section_header_size)


def many_section_pe(count):
    """Produce count valid disjoint PE sections; intended boundary is count limit."""
    original=pe_fixture(signed=False,source=b"x;")
    p=h.Pe64(original);old_bun=p.bun_section;header_size=((p.section_table_off+count*40+511)//512)*512
    text_off=header_size;bun_off=text_off+512
    result=bytearray(bun_off+old_bun["raw_size"])
    result[:p.section_table_off]=original[:p.section_table_off]
    struct.pack_into("<H",result,p.coff_off+2,count)
    struct.pack_into("<I",result,p.optional_off+60,header_size)
    first_rva=((header_size+4095)//4096)*4096
    struct.pack_into("<I",result,p.optional_off+16,first_rva)
    for i in range(count):
        at=p.section_table_off+i*40
        if i==0:
            name=b".text";vs=1;rs=512;fo=text_off;flags=0x60000020
        elif i==count-1:
            name=b".bun";vs=old_bun["virtual_size"];rs=old_bun["raw_size"];fo=bun_off;flags=0x40000040
        else:
            name=f".z{i}".encode();vs=0;rs=0;fo=0;flags=0x40000040
        result[at:at+8]=name.ljust(8,b"\0")
        struct.pack_into("<IIIIIIHHI",result,at+8,vs,first_rva+i*4096,rs,fo,0,0,0,0,flags)
    struct.pack_into("<I",result,p.optional_off+56,first_rva+count*4096)
    result[text_off]=0xc3
    result[bun_off:]=original[old_bun["foff"]:old_bun["foff"]+old_bun["raw_size"]]
    return bytes(result)


class SurvivorBoundaries(unittest.TestCase):
    def reject_image(self,data):
        self.assertFalse(h.can_handle(data))
        with self.assertRaises(h.BunFormatError):h.BunImage(data)

    def test_short_section_stride_with_consistent_record_positions(self):
        original=graph();image=h.BunImage(original);elf=image.elf
        out=bytearray(original)
        records=[original[s["hdr_off"]:s["hdr_off"]+64] for s in elf.sections]
        for i,rec in enumerate(records):
            out[elf.e_shoff+i*63:elf.e_shoff+i*63+64]=rec
        struct.pack_into("<H",out,58,63)
        with self.assertRaises(h.BunFormatError):h.Elf64(bytes(out))

    def test_extended_section_numbering_is_unsupported(self):
        original=graph()
        # An explicit semantic distinction: extended numbering is unsupported,
        # not silently accepted and not misdiagnosed as a corrupt name index.
        with self.assertRaises(NotImplementedError):
            h.Elf64(packed(original,60,"<H",0))

    def test_pe_header_overlapping_dos_header_with_other_fields_consistent(self):
        data=pe_fixture(signed=False);pe=h.Pe64(data)
        at=32;delta=pe.pe_off-at
        out=bytearray(data)
        out[at:at+(pe.section_table_off+len(pe.sections)*40-pe.pe_off)]=data[pe.pe_off:pe.section_table_off+len(pe.sections)*40]
        struct.pack_into("<I",out,60,at)
        self.reject_image(bytes(out))

    def test_maximum_pe_sections_with_complete_tables(self):
        self.assertTrue(h.can_handle(many_section_pe(96)))
        self.reject_image(many_section_pe(97))

    def test_certificate_pointer_without_length_on_unsigned_input(self):
        data=pe_fixture(signed=False);pe=h.Pe64(data)
        self.assertTrue(h.can_handle(data))
        self.reject_image(packed(data,pe.security_off,"<II",pe.raw_end,0))

    def test_layout_helper_requires_the_owned_section_not_an_equal_copy(self):
        data=pe_fixture(signed=False);pe=h.Pe64(data)
        self.assertIs(h._validate_pe_patch_layout(pe,pe.bun_section,for_resize=True),pe.bun_section)
        with self.assertRaises(h.BunFormatError):
            h._validate_pe_patch_layout(pe,dict(pe.bun_section),for_resize=True)

    def test_nonbun_overlay_classification(self):
        data=graph();image=h.BunImage(data)
        shstr=image.elf.sections[image.elf.e_shstrndx]
        out=bytearray(data)
        namepos=shstr["foff"]+image.bun_section["name_off"]
        out[namepos:namepos+4]=b".abc"
        out.extend(h.BUN_TRAILER+b"\0"*8)
        with self.assertRaises(NotImplementedError):h.BunImage(bytes(out))

    def test_duplicate_elf_bun_is_rejected_by_graph_reader(self):
        data=graph(trailing_section={"flags":0,"size":16})
        image=h.BunImage(data);trail=image.elf.section(".trail")
        duplicated=packed(data,trail["hdr_off"],"<I",image.bun_section["name_off"])
        with self.assertRaises(h.BunFormatError):h.BunImage(duplicated)

    def test_nobits_bun_with_plausible_file_bytes_is_rejected(self):
        data=graph();image=h.BunImage(data)
        malformed=packed(data,image.bun_section["hdr_off"]+4,"<I",8)
        with self.assertRaises(h.BunFormatError):h.BunImage(malformed)

    def test_entrypoint_index_rejected_when_constructing_graph(self):
        data=graph();image=h.BunImage(data)
        malformed=blob_change(data,image.offsets_off+16,struct.pack("<I",len(image.modules)))
        with self.assertRaises(h.BunFormatError):h.BunImage(malformed)

    def test_optional_range_helper_boundaries(self):
        image=h.BunImage(graph())
        self.assertEqual(image._need_optional_bytes(image.offsets_off,0,"zero"),image.offsets_off)
        for at,size in ((image.offsets_off,1),(-1,1),(0,-1)):
            with self.subTest(at=at,size=size), self.assertRaises(h.BunFormatError):
                image._need_optional_bytes(at,size,"test")

    def test_nonempty_argv_cannot_use_unowned_pre_table_gap(self):
        data=graph();image=h.BunImage(data)
        bad=blob_change(data,16,b"--x\0")
        bad=blob_change(bad,image.offsets_off+20,struct.pack("<II",16,3))
        self.reject_image(bad)

    def test_empty_argv_placeholder_cannot_use_pre_table_gap(self):
        data=graph();image=h.BunImage(data)
        bad=blob_change(data,image.offsets_off+20,struct.pack("<II",16,0))
        self.reject_image(bad)

    def test_invalid_encoding_on_non_source_asset(self):
        data=graph();image=h.BunImage(data);at=image.modules_off+2*52+48
        self.reject_image(blob_change(data,at,b"\xff"))

    def test_invalid_loader_on_non_entry_module(self):
        data=graph();image=h.BunImage(data);at=image.modules_off+52+49
        self.reject_image(blob_change(data,at,b"\xff"))

    def test_invalid_module_format_on_non_entry_module(self):
        data=graph();image=h.BunImage(data);at=image.modules_off+52+50
        self.reject_image(blob_change(data,at,b"\xff"))

    def test_invalid_side_on_non_entry_module(self):
        data=graph();image=h.BunImage(data);at=image.modules_off+52+51
        self.reject_image(blob_change(data,at,b"\xff"))

    def test_non_entry_name_outside_bunfs(self):
        data=graph();image=h.BunImage(data)
        self.reject_image(blob_change(data,image.modules[1]["name"][0],b"x"))

    def test_duplicate_names_with_independently_owned_storage(self):
        data=graph();image=h.BunImage(data)
        a,b=image.modules[:2]
        self.assertEqual(a["name"][1],b["name"][1])
        self.assertNotEqual(a["name"][0],b["name"][0])
        self.reject_image(blob_change(data,b["name"][0],a["name_bytes"]))

    def test_utf16_asset_requires_even_length(self):
        data=build_bun_elf([
            _module("/$bunfs/root/cli",b"x;",module_format=1),
            _module("/$bunfs/root/asset",b"x",encoding=2,module_format=0,loader=13),
        ])
        self.reject_image(data)

    def test_origin_path_embedded_nul_cannot_hide_behind_entrypoint_name(self):
        data=graph_fixture();image=h.BunImage(data)
        off,n=image.modules[0]["bytecodeOriginPath"]
        self.assertGreater(n,4)
        self.reject_image(blob_change(data,off+2,b"\0"))

    def test_nonexecutable_entrypoint_not_silently_omitted_from_multi_module_surface(self):
        data=build_bun_elf([
            _module("/$bunfs/root/cli",b"asset",module_format=0,loader=13),
            _module("/$bunfs/root/aa",b"export {};",module_format=1),
            _module("/$bunfs/root/bb",b"export {};",module_format=1),
        ])
        image=h.BunImage(data)
        with self.assertRaises(h.BunFormatError):image.source_modules()

    def test_reserved_marker_rejected_by_source_modules_api(self):
        data=build_bun_elf([
            _module("/$bunfs/root/cli",b"x;",module_format=1),
            _module("/$bunfs/root/mod",b"x;"+h._MULTI_MODULE_MARKER+b"comment",module_format=1),
        ])
        image=h.BunImage(data)
        with self.assertRaises(h.BunFormatError):image.source_modules()

    def test_ascii_asset_is_not_executable_source(self):
        image=h.BunImage(graph())
        self.assertEqual(image.read_field(image.modules[2],"contents"),b"plain text asset")
        with self.assertRaises(h.BunFormatError):image.read_source(image.modules[2])

    def test_embedded_nul_source(self):
        data=build_bun_elf([_module("/$bunfs/root/cli",b'const a="x\0y";',module_format=1)])
        image=h.BunImage(data)
        with self.assertRaises(h.BunFormatError):image.read_source(image.modules[0])

    def test_program_headers_cannot_occupy_unowned_bun_bytes(self):
        data=insert_empty_prefix(graph());image=h.BunImage(data);e=image.elf
        at=image.bun_section["foff"]+image.section_header_size+64
        header=data[e.e_phoff:e.e_phoff+e.e_phnum*e.e_phentsize]
        out=bytearray(data);out[at:at+len(header)]=header
        struct.pack_into("<Q",out,32,at)
        self.assertEqual(len(h.BunImage(bytes(out)).modules),3)
        self.assertFalse(h.can_handle(bytes(out)))

    def test_section_headers_cannot_occupy_unowned_bun_bytes(self):
        data=insert_empty_prefix(graph());image=h.BunImage(data);e=image.elf
        at=image.bun_section["foff"]+image.section_header_size+64
        header=data[e.e_shoff:e.e_shoff+e.e_shnum*e.e_shentsize]
        out=bytearray(data);out[at:at+len(header)]=header
        struct.pack_into("<Q",out,40,at)
        self.assertEqual(len(h.BunImage(bytes(out)).modules),3)
        self.assertFalse(h.can_handle(bytes(out)))

    def test_appended_module_hash_cannot_be_nonzero(self):
        original=graph_fixture()
        appended=append_sources(original,{0:b"console.log('changed');"})
        image=h.BunImage(appended);at=image.source_hashes_off
        bad=blob_change(appended,at,struct.pack("<I",0x12345678))
        self.assertTrue(h.can_handle(bad))
        with self.assertRaises(h.BunFormatError):h.compact_with_reference(bad,original)

    def test_appended_module_side_cannot_change(self):
        original=graph_fixture()
        appended=append_sources(original,{0:b"console.log('changed');"})
        image=h.BunImage(appended);at=image.modules_off+51
        bad=blob_change(appended,at,b"\x01")
        self.assertTrue(h.can_handle(bad))
        with self.assertRaises(h.BunFormatError):h.compact_with_reference(bad,original)

    def test_compaction_argv_pointer_cannot_move_to_identical_bytes(self):
        original=graph_fixture();before=h.BunImage(original)
        # Prefix is unchanged; append a second copy of argv and source.
        appended=append_sources(original,{0:b"console.log('changed');"})
        image=h.BunImage(appended)
        argv=b"--smol\0"
        blob=bytearray(image.blob[:image.offsets_off])+argv+image.blob[image.offsets_off:]
        extra=8-len(argv);blob[image.offsets_off+len(argv):image.offsets_off+len(argv)]=b"\0"*extra
        new_offsets=image.offsets_off+8
        struct.pack_into("<Q",blob,new_offsets,new_offsets)
        struct.pack_into("<II",blob,new_offsets+20,image.offsets_off,len(argv)-1)
        bad=h._repack_blob_elf(appended,blob,image.section_header_size)
        self.assertTrue(h.can_handle(bad))
        with self.assertRaises(h.BunFormatError):h.compact_with_reference(bad,original)

    def test_rebuilt_invalid_name_without_source_decoding_side_effects(self):
        original=graph();rebuilt=rebuild_fixture(original)
        image=h._TweakccRebuildImage(rebuilt)
        at=image.modules[1]["name"][0]
        out=bytearray(rebuilt);absolute=image.bun_section["foff"]+image.section_header_size+at
        out[absolute]=ord("x")
        with self.assertRaises(h.BunFormatError):h._TweakccRebuildImage(bytes(out))

    def test_rebuilt_reserved_padding_after_trailer_is_not_accepted(self):
        original=graph();rebuilt=rebuild_fixture(original)
        image=h._TweakccRebuildImage(rebuilt)
        # One graph trailer byte plus seven bytes of native load slack keeps
        # all file-only ELF objects aligned; no other guard should mask this.
        end=image.bun_section["foff"]+image.bun_section["size"]
        out=bytearray(rebuilt[:end])+bytearray(8)+bytearray(rebuilt[end:])
        struct.pack_into("<Q",out,40,image.elf.e_shoff+8)
        load=next(p for p in image.elf.segments if p["type"]==1)
        struct.pack_into("<QQ",out,load["hdr_off"]+32,load["filesz"]+8,load["memsz"]+8)
        struct.pack_into("<Q",out,image.bun_section["foff"],len(image.blob)+1)
        for sec in image.elf.sections:
            header=sec["hdr_off"]+8
            if sec["index"]==image.bun_section["index"]:
                struct.pack_into("<Q",out,header+32,sec["size"]+1)
            elif sec["size"] and sec["foff"]>=end:
                struct.pack_into("<Q",out,header+24,sec["foff"]+8)
        with self.assertRaises(h.BunFormatError):h._TweakccRebuildImage(bytes(out))

    def test_rebuilt_argv_embedded_nul(self):
        original=graph(compile_argv=b"--smol");rebuilt=rebuild_fixture(original)
        image=h._TweakccRebuildImage(rebuilt)
        out=bytearray(rebuilt);at=image.bun_section["foff"]+image.section_header_size+image.compile_argv_off
        out[at+1]=0
        with self.assertRaises(h.BunFormatError):h._TweakccRebuildImage(bytes(out))

    def test_rebuilt_name_selected_asset_read_is_not_source(self):
        original=graph();rebuilt=rebuild_fixture(original)
        image=h._TweakccRebuildImage(rebuilt)
        # The authoritative entrypoint remains module0. Module1 has another name
        # tweakcc recognizes but is an asset, which read_source must not accept.
        original2=build_bun_elf([
            _module("/$bunfs/root/cli",b"x;",module_format=1),
            _module("/$bunfs/root/claude",b"ascii",loader=13,module_format=0),
        ])
        image=h._TweakccRebuildImage(rebuild_fixture(original2))
        with self.assertRaises(h.BunFormatError):image.read_source(image.modules[1])

    def test_reference_recovery_rejects_nonzero_unmapped_load_slack(self):
        original=graph_fixture();rebuilt=rebuild_fixture(original)
        image=h._TweakccRebuildImage(rebuilt);s=image.bun_section
        load=next(p for p in image.elf.segments if p["type"]==1)
        end=s["foff"]+s["size"]
        # Insert a 16-byte gap after the logical section, include in PT_LOAD,
        # and move file-only sections/table accordingly.
        out=bytearray(rebuilt[:end])+bytearray(b"X"*16)+bytearray(rebuilt[end:])
        struct.pack_into("<Q",out,40,image.elf.e_shoff+16)
        struct.pack_into("<QQ",out,load["hdr_off"]+32,load["filesz"]+16,load["memsz"]+16)
        for sec in image.elf.sections:
            hdr=sec["hdr_off"]+16
            if sec["size"] and sec["foff"]>=end:
                struct.pack_into("<Q",out,hdr+24,sec["foff"]+16)
        with self.assertRaises(h.BunFormatError):h.recover_tweakcc(bytes(out))


class FurtherSurvivorBoundaries(unittest.TestCase):
    def test_pe_zero_sized_descriptor_may_not_follow_bun_virtual_address(self):
        data=many_section_pe(3);pe=h.Pe64(data)
        # The zero-sized section contributes no overlap span. Its placement,
        # rather than a missing raw allocation, is the rejected condition.
        changed=packed(data,pe.sections[1]["hdr_off"]+12,"<I",pe.bun_section["rva"])
        with self.assertRaises(NotImplementedError):h.repack_unchanged(changed)

    def test_low_level_elf_writer_requires_unique_bun(self):
        data=graph();image=h.BunImage(data)
        duplicate=packed(data,image.elf.sections[-1]["hdr_off"],"<I",
                         image.bun_section["name_off"])
        section=data[image.bun_section["foff"]:
                     image.bun_section["foff"]+image.bun_section["size"]]
        with self.assertRaises(h.BunFormatError):
            h._repack_section_elf(duplicate,section)

    def test_elf_reference_wrapper_width_is_identity(self):
        reference=graph();image=h.BunImage(reference);section=image.bun_section
        # Keep native section/program tables at exactly the same file positions.
        # Replace an 8-byte wrapper with a 4-byte wrapper and four zero load-slack
        # bytes. No source/cache payload depends on the shifted runtime address.
        out=bytearray(reference)
        wrapped=struct.pack("<I",len(image.blob))+image.blob
        out[section["foff"]:section["foff"]+len(wrapped)]=wrapped
        out[section["foff"]+len(wrapped):section["foff"]+section["size"]]=bytes(4)
        struct.pack_into("<Q",out,section["hdr_off"]+32,section["size"]-4)
        four=bytes(out)
        self.assertTrue(h.can_handle(four))
        self.assertEqual(h.BunImage(four).section_header_size,4)
        current=append_sources(four,{0:b"changed();"})
        with self.assertRaises(h.BunFormatError):
            h.compact_with_reference(current,reference)

    def test_reference_selects_same_section_table_not_an_identical_unused_copy(self):
        original=graph();image=h.BunImage(original);elf=image.elf
        table=original[elf.e_shoff:elf.e_shoff+elf.e_shnum*elf.e_shentsize]
        second=(len(original)+7)//8*8
        reference=original+bytes(second-len(original))+table
        current=packed(reference,40,"<Q",second)
        self.assertTrue(h.can_handle(reference))
        self.assertTrue(h.can_handle(current))
        current=append_sources(current,{0:b"changed();"})
        with self.assertRaises(h.BunFormatError):
            h.compact_with_reference(current,reference)

    def test_reference_tail_pointer_change_cannot_be_hidden_by_mask(self):
        reference=graph_fixture()
        current=append_sources(reference,{0:b"changed();"})
        image=h.BunImage(current);section=image.elf.section(".trail")
        changed=packed(current,section["hdr_off"]+24,"<Q",section["foff"]+1)
        # A one-byte-shifted file-only section is parseable; recovery must not
        # interpret its offset as a legitimate consequence of Bun resizing.
        self.assertTrue(h.can_handle(changed))
        with self.assertRaises(h.BunFormatError):
            h.compact_with_reference(changed,reference)

    def test_compaction_must_preserve_authoritative_entrypoint_index(self):
        reference=build_bun_elf([
            _module("/$bunfs/root/cli",b"original0();",module_format=1),
            _module("/$bunfs/more/cli",b"original1();",module_format=1),
        ],flags=0xf)
        current=append_sources(reference,{0:b"changed();"})
        image=h.BunImage(current)
        changed=blob_change(current,image.offsets_off+16,struct.pack("<I",1))
        self.assertTrue(h.can_handle(changed))
        self.assertEqual(h.BunImage(changed).entry_point_id,1)
        with self.assertRaises(h.BunFormatError):
            h.compact_with_reference(changed,reference)

    def test_compaction_rejects_nonappend_metadata_change_among_appended_modules(self):
        reference=graph_fixture()
        current=append_sources(reference,{0:b"changed();"})
        image=h.BunImage(current);blob=bytearray(image.blob)
        at=image.modules_off+image.module_struct_size
        blob[at+48]=0  # ASCII text reads identically as UTF-8.
        blob[at+16:at+48]=b"\0"*32
        struct.pack_into("<I",blob,image.source_hashes_off+4,0)
        changed=h._repack_blob_elf(current,blob,image.section_header_size)
        self.assertTrue(h.can_handle(changed))
        self.assertEqual(h.extract_js(changed),h.extract_js(current))
        with self.assertRaises(h.BunFormatError):
            h.compact_with_reference(changed,reference)

    def test_compaction_rejects_origin_override_even_when_old_region_is_unowned(self):
        reference=build_bun_elf([
            _module("/$bunfs/root/cli",b"original0();",module_format=1,
                    origin="/$bunfs/root/src/entrypoints/cli.js"),
            _module("/$bunfs/more/mod",b"original1();",module_format=1,
                    origin="/$bunfs/root/elsewhere.js"),
        ],flags=0x2f)
        current=append_sources(reference,{0:b"changed0();",1:b"changed1();"})
        image=h.BunImage(current);before=h.BunImage(reference)
        changed=blob_change(current,image.modules_off+40,
                            struct.pack("<II",*before.modules[1]["bytecodeOriginPath"]))
        self.assertTrue(h.can_handle(changed))
        with self.assertRaises(h.BunFormatError):
            h.compact_with_reference(changed,reference)

    def test_rebuilt_empty_field_pointer_must_name_its_own_allocated_terminator(self):
        rebuilt=rebuild_fixture(graph(compile_argv=b"--smol"))
        image=h._TweakccRebuildImage(rebuilt)
        off,length=image.modules[0]["contents"]
        changed=blob_change(rebuilt,image.modules_off+16,struct.pack("<II",off+length,0))
        # Both bytes are NUL. The failure is the serializer's sequential ownership
        # rule, not generic range or terminator validation.
        with self.assertRaises(h.BunFormatError):h.extract_tweakcc_js(changed)

    def test_rebuilt_fields_cannot_leave_gap_before_module_table(self):
        rebuilt=rebuild_fixture(graph(compile_argv=b"--smol"))
        image=h._TweakccRebuildImage(rebuilt)
        blob=bytearray(image.blob[:image.modules_off])+bytes(8)+image.blob[image.modules_off:]
        oo=image.offsets_off+8
        struct.pack_into("<Q",blob,oo,oo)
        struct.pack_into("<I",blob,oo+8,image.modules_off+8)
        struct.pack_into("<I",blob,oo+20,image.compile_argv_off+8)
        changed=h._repack_blob_elf(rebuilt,blob,image.section_header_size)
        with self.assertRaises(h.BunFormatError):h.extract_tweakcc_js(changed)

    def test_rebuilt_argv_pointer_must_match_serialized_argv_region(self):
        rebuilt=rebuild_fixture(graph(compile_argv=b"--smol"))
        image=h._TweakccRebuildImage(rebuilt)
        changed=blob_change(rebuilt,image.offsets_off+20,struct.pack("<I",0))
        with self.assertRaises(h.BunFormatError):h.extract_tweakcc_js(changed)

    def test_initialized_size_overflow_has_controlled_error_without_large_allocation(self):
        data=pe_fixture(signed=False);pe=h.Pe64(data)
        changed=packed(data,pe.optional_off+8,"<I",0xffffff00)
        self.assertTrue(h.can_handle(changed))
        with self.assertRaises(h.BunFormatError):
            h.repack_with_js(changed,b"long();"+b" "*1024)

    def test_truncated_low_level_pe_wrapper_is_a_controlled_error(self):
        original=pe_fixture(signed=False)
        with self.assertRaises(h.BunFormatError):
            h._repack_section_pe_parts(original,[b"\0"*7])

    def test_unmapped_rva_lookup_is_a_controlled_error(self):
        pe=h.Pe64(pe_fixture(signed=False))
        with self.assertRaises(h.BunFormatError):
            pe.rva_to_file(0xffffffff,1)

    def test_cross_container_reference_rejected(self):
        linux=graph();windows=pe_fixture(signed=False)
        for current,reference in [(linux,windows),(windows,linux)]:
            with self.subTest(current=current[:4]),self.assertRaises(h.BunFormatError):
                h.compact_with_reference(current,reference)


class BoundedEdgeCases(unittest.TestCase):
    def test_truncated_reference_prefix_with_empty_source_pointer_is_rejected(self):
        original=build_bun_elf([
            _module("/$bunfs/root/cli",b"original();",module_format=1),
            _module("/$bunfs/root/mod",b"",module_format=1),
        ],flags=0xf)
        image=h.BunImage(original)
        current_offsets=image.offsets_off;reference_offsets=current_offsets+16
        blob=bytearray(image.blob[:current_offsets])+bytes(16)+image.blob[current_offsets:]
        struct.pack_into("<II",blob,image.modules_off+52+8,reference_offsets,0)
        struct.pack_into("<Q",blob,reference_offsets,reference_offsets)
        current_metadata=struct.pack("<QIIIIII",current_offsets,image.modules_off,image.modules_len,
            image.entry_point_id,image.compile_argv_off,image.compile_argv_len,image.flags)
        # Unowned prefix bytes may resemble metadata; they are not proof of
        # append lineage. The empty field owns no bytes beyond the current EOF.
        blob[current_offsets:reference_offsets]=current_metadata[:16]
        reference=h._repack_blob_elf(original,blob,image.section_header_size)
        current_blob=bytes(blob[:current_offsets])+current_metadata+h.BUN_TRAILER
        if len(image.blob)==image.offsets_off+49:
            current_blob+=b"\0"
        current=h._repack_blob_elf(reference,current_blob,image.section_header_size)
        self.assertTrue(h.can_handle(current))
        self.assertTrue(h.can_handle(reference))
        self.assertLess(h.BunImage(current).offsets_off,h.BunImage(reference).offsets_off)
        with self.assertRaises(h.BunFormatError):
            h.compact_with_reference(current,reference)

    def test_envelope_serializer_itself_refuses_reserved_marker(self):
        source=b"export {};\n"+h._MULTI_MODULE_MARKER+b"not a boundary\n"
        image=h.BunImage(build_bun_elf([
            _module("/$bunfs/root/cli",b"main();",module_format=1),
            _module("/$bunfs/root/mod",source,module_format=1),
        ]))
        # Real parsed records supplied to the serializer, not mutated internal
        # objects. The serializer must not emit an ambiguous envelope on its own.
        with self.assertRaises(h.BunFormatError):
            h._join_source_modules(image,image.modules)

    def test_truncated_optional_header_returns_domain_error(self):
        original=pe_fixture(signed=False);pe=h.Pe64(original)
        short=bytearray(original[:pe.optional_off+10])
        struct.pack_into("<H",short,pe.coff_off+16,10)
        with self.assertRaises(h.BunFormatError):h.Pe64(bytes(short))

    def test_short_bun_blob_with_valid_wrapper_and_trailer_returns_domain_error(self):
        original=graph();image=h.BunImage(original);section=image.bun_section
        blob=h.BUN_TRAILER;wrapped=struct.pack("<Q",len(blob))+blob
        out=bytearray(original)
        out[section["foff"]:section["foff"]+len(wrapped)]=wrapped
        out[section["foff"]+len(wrapped):section["foff"]+section["size"]]=bytes(section["size"]-len(wrapped))
        struct.pack_into("<Q",out,section["hdr_off"]+32,len(wrapped))
        with self.assertRaises(h.BunFormatError):h.BunImage(bytes(out))

    def test_short_wrapper_decoder_returns_domain_error(self):
        image=h.BunImage(graph())
        for width in (0,1,3,4,7):
            with self.subTest(width=width),self.assertRaises(h.BunFormatError):
                image._detect_section_header_size(bytes(width))


class EmptyAssetLineageBoundary(unittest.TestCase):
    def test_compaction_must_not_mask_changes_to_empty_asset_records(self):
        reference=build_bun_elf([
            _module("/$bunfs/root/cli",b"original();",module_format=1),
            _module("/$bunfs/root/dat",b"",loader=5,module_format=0,encoding=0),
        ],flags=0xf)
        before=h.BunImage(reference)
        current=append_sources(reference,{0:b"changed();"})
        image=h.BunImage(current)
        changed=blob_change(current,image.modules_off+52+8,
                            struct.pack("<II",before.offsets_off,0))
        self.assertTrue(h.can_handle(changed))
        # Length zero owns no source bytes, but record identity still matters.
        self.assertEqual(h.BunImage(changed).read_field(h.BunImage(changed).modules[1],"contents"),b"")
        with self.assertRaises(h.BunFormatError):
            h.compact_with_reference(changed,reference)


class EmptySourceLineageBoundary(unittest.TestCase):
    def test_appended_empty_source_pointer_must_stay_within_current_graph(self):
        reference=graph_fixture()
        current=append_sources(reference,{0:b""})
        image=h.BunImage(current)
        changed=blob_change(current,image.modules_off+8,
                            struct.pack("<II",image.offsets_off+1,0))
        # General parsing accepts unowned zero-length pointers. Reference
        # compaction has a stricter bound for an appended source record.
        self.assertTrue(h.can_handle(changed))
        self.assertEqual(h.BunImage(changed).read_source(h.BunImage(changed).modules[0]),b"")
        with self.assertRaises(h.BunFormatError):
            h.compact_with_reference(changed,reference)
