#!/usr/bin/env python3
"""
Surgical, byte-exact, stdlib-only handler for the bun-packed native Claude CLI.

Anthropic ships the native Claude CLI as a single-file executable produced by
`bun build --compile`: a host ELF executable with embedded JavaScript modules,
JavaScriptCore bytecode, and assets stored in a `.bun` ELF section. This module
extracts the complete patchable JavaScript, applies length-changing text edits,
and repacks the binary so it still runs.

Design constraints (requirements, not preferences):
  - Pure Python standard library only. No node-lief, no tweakcc, no pyelftools.
    The same code runs at maintainer synthesis time AND in the end-user apply
    path, so it must be self-contained and dependency-free.
  - The handler is the single canonical writer. We do NOT byte-match any external
    tool; we require (a) deterministic self-consistency (same input + same edit
    always yields the same output bytes) and (b) the output binary actually runs.
  - Surgical in-place editing, never full re-serialization. A bun blob rebuild
    reflows every region and breaks the JavaScriptCore bytecode's absolute source
    offsets (and also bloats the file). Instead we splice edited region bytes in
    place and only recompute the module table, the offsets struct, and the ELF
    headers/segment sizes for the byte delta. This is what makes the no-op
    round-trip byte-identical: nothing moves that does not have to.

Scope of THIS module:
  - linux-x64 ELF, `.bun`-section storage form only (the layout shipped since
    roughly native build 2.1.83).
  - Mach-O, PE/COFF, and the pre-2.1.83 ELF trailing-overlay form are detected
    and rejected with a clear NotImplementedError. They are later milestones.

Why source edits take effect even when modules are bytecode-compiled:
  JSC bytecode reads strings and source-backed data from each module's live source
  buffer. Editing those source bytes changes runtime behavior with bytecode left
  intact, as long as each edited module keeps its identity and every shifted range
  in the module table is updated. We keep the bytecode untouched.
"""
import hashlib
import struct
import sys


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUN_TRAILER = b"\n---- Bun! ----\n"

ELF_PT_LOAD = 1
ELF_SHT_NOBITS = 8
ELF_SHF_ALLOC = 0x2

# Module record field order, as bun lays out the per-module struct. The 52-byte
# (new) form carries two extra ranges (moduleInfo, bytecodeOriginPath) that the
# 36-byte (old) form omits.
_FIELD_ORDER_52 = ["name", "contents", "sourcemap", "bytecode", "moduleInfo", "bytecodeOriginPath"]
_FIELD_ORDER_36 = ["name", "contents", "sourcemap", "bytecode"]

# Tail of the pre-compile source path bun records for the Claude entrypoint in
# each module's bytecodeOriginPath field. Claude 2.1.232 renamed the packed
# module itself from src/entrypoints/cli.js to plain cli, but left this path
# reading /$bunfs/root/src/entrypoints/cli.js.
ENTRYPOINT_ORIGIN_SUFFIX = "entrypoints/cli.js"

# Bun 1.4 can compile an ESM program as many embedded chunks. The public API
# still returns one byte string so existing patchers can search it globally.
# These markers preserve module boundaries for repacking after length changes.
_MULTI_MODULE_HEADER = b"// bun_handler multi-module bundle v1"
_MULTI_MODULE_MARKER = b"\n// bun_handler module 6e0d7c9f5a3b4d2184f176c2 "


class BunFormatError(Exception):
    """The binary is not a bun .bun-section ELF we can handle."""


# ---------------------------------------------------------------------------
# ELF64 parsing (little-endian only)
# ---------------------------------------------------------------------------

class Elf64:
    """Minimal little-endian ELF64 reader: program headers, section headers,
    section names. Read-only; the writer works on a fresh byte buffer."""

    def __init__(self, data):
        if len(data) < 64:
            raise BunFormatError("file too small to be ELF64")
        if data[:4] != b"\x7fELF":
            raise BunFormatError("missing ELF magic")
        ei_class = data[4]
        ei_data = data[5]
        if ei_class != 2:
            # ELFCLASS32; we only handle 64-bit native Claude builds.
            raise NotImplementedError("only ELF64 is supported (native Claude is x64)")
        if ei_data != 1:
            raise NotImplementedError("only little-endian ELF is supported")

        self.data = data
        self.e_phoff = struct.unpack_from("<Q", data, 32)[0]
        self.e_shoff = struct.unpack_from("<Q", data, 40)[0]
        self.e_phentsize = struct.unpack_from("<H", data, 54)[0]
        self.e_phnum = struct.unpack_from("<H", data, 56)[0]
        self.e_shentsize = struct.unpack_from("<H", data, 58)[0]
        self.e_shnum = struct.unpack_from("<H", data, 60)[0]
        self.e_shstrndx = struct.unpack_from("<H", data, 62)[0]
        self._read_sections()
        self._read_segments()

    def _read_sections(self):
        if self.e_shentsize < 64:
            raise BunFormatError("ELF section header entries too small")
        table_end = self.e_shoff + self.e_shentsize * self.e_shnum
        if self.e_shoff <= 0 or table_end > len(self.data):
            raise BunFormatError("ELF section header table out of range")
        if self.e_shstrndx >= self.e_shnum:
            raise BunFormatError("ELF section name string table index out of range")

        self.sections = []
        for i in range(self.e_shnum):
            o = self.e_shoff + i * self.e_shentsize
            self.sections.append({
                "index": i,
                "name_off": struct.unpack_from("<I", self.data, o)[0],
                "type": struct.unpack_from("<I", self.data, o + 4)[0],
                "flags": struct.unpack_from("<Q", self.data, o + 8)[0],
                "vaddr": struct.unpack_from("<Q", self.data, o + 16)[0],
                "foff": struct.unpack_from("<Q", self.data, o + 24)[0],
                "size": struct.unpack_from("<Q", self.data, o + 32)[0],
                "hdr_off": o,
            })

        shstr = self.sections[self.e_shstrndx]
        if shstr["type"] == ELF_SHT_NOBITS:
            raise BunFormatError("ELF section name string table has no file payload")
        if shstr["foff"] + shstr["size"] > len(self.data):
            raise BunFormatError("ELF section name string table out of range")
        names = self.data[shstr["foff"]:shstr["foff"] + shstr["size"]]
        for s in self.sections:
            end = names.find(b"\x00", s["name_off"])
            if end < 0:
                end = len(names)
            s["name"] = names[s["name_off"]:end].decode("utf-8", "replace")

    def _read_segments(self):
        if self.e_phentsize < 56:
            raise BunFormatError("ELF program header entries too small")
        table_end = self.e_phoff + self.e_phentsize * self.e_phnum
        if self.e_phoff <= 0 or table_end > len(self.data):
            raise BunFormatError("ELF program header table out of range")
        self.segments = []
        for i in range(self.e_phnum):
            o = self.e_phoff + i * self.e_phentsize
            self.segments.append({
                "index": i,
                "type": struct.unpack_from("<I", self.data, o)[0],
                "foff": struct.unpack_from("<Q", self.data, o + 8)[0],
                "vaddr": struct.unpack_from("<Q", self.data, o + 16)[0],
                "filesz": struct.unpack_from("<Q", self.data, o + 32)[0],
                "memsz": struct.unpack_from("<Q", self.data, o + 40)[0],
                "align": struct.unpack_from("<Q", self.data, o + 48)[0],
                "hdr_off": o,
            })

    def section(self, name):
        for s in self.sections:
            if s["name"] == name:
                return s
        return None


def _detect_non_elf_format(data):
    """Return a human label if this is a recognizably non-ELF executable, else None.
    Used to give a precise NotImplementedError instead of a vague parse failure."""
    if data[:4] == b"MZ\x90\x00" or data[:2] == b"MZ":
        return "PE/COFF (Windows)"
    macho_magics = {
        b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64 (LE)
        b"\xce\xfa\xed\xfe",  # MH_MAGIC (LE)
        b"\xca\xfe\xba\xbe",  # FAT_MAGIC
        b"\xbe\xba\xfe\xca",  # FAT_MAGIC swapped
    }
    if data[:4] in macho_magics:
        return "Mach-O (macOS)"
    return None


# ---------------------------------------------------------------------------
# Bun blob parsing
# ---------------------------------------------------------------------------

def _detect_module_struct_size(modules_table_len):
    """Bun's module table is N records of either 36 or 52 bytes. Return the
    candidate size when only one divides the table length; return None when
    both divide (callers must validate both complete record interpretations) or
    neither does. Silent fallback ("just pick 52") would patch the wrong field offsets
    for a 36-byte build and look superficially fine, so we refuse it here.
    """
    div52 = (modules_table_len % 52) == 0
    div36 = (modules_table_len % 36) == 0
    if div52 and not div36:
        return 52
    if div36 and not div52:
        return 36
    return None  # ambiguous (both divide) or impossible (neither divides)




class BunImage:
    """A parsed bun-on-ELF (.bun section form) image.

    Holds the raw file bytes, the located bun data blob, the offsets struct, and
    the module table, with enough metadata to apply surgical in-place edits and
    repack into a working ELF.
    """

    def __init__(self, data):
        self.data = bytes(data)
        self.elf = Elf64(self.data)

        bs = self.elf.section(".bun")
        if bs is None:
            # No .bun section. Distinguish the trailing-overlay form (later
            # milestone) from "this is not a bun binary at all".
            if self._looks_like_overlay():
                raise NotImplementedError(
                    "this binary uses the bun trailing-overlay form (pre-2.1.83); "
                    "only the .bun-section form is supported in this milestone"
                )
            raise BunFormatError("no .bun section found; not a supported bun ELF")
        self.bun_section = bs

        sec = self.data[bs["foff"]:bs["foff"] + bs["size"]]
        if len(sec) != bs["size"]:
            raise BunFormatError(".bun section runs past end of file")

        # The section content is an N-byte length header followed by the bun data
        # blob, where header + blob == section size. Bun uses a u64 header in the
        # 64-bit form and a u32 header in the 32-bit form; detect by which makes
        # the arithmetic close.
        self.section_header_size = self._detect_section_header_size(sec)
        if self.section_header_size == 8:
            blob_len = struct.unpack_from("<Q", sec, 0)[0]
        else:
            blob_len = struct.unpack_from("<I", sec, 0)[0]
        if self.section_header_size + blob_len != len(sec):
            raise BunFormatError(".bun section length header inconsistent with section size")
        self.blob = sec[self.section_header_size:self.section_header_size + blob_len]

        if len(self.blob) < 32 + len(BUN_TRAILER):
            raise BunFormatError("bun blob too small to hold offsets and trailer")
        # The trailer is normally the last bytes of the blob. Some builds append a
        # single pad byte after it; locate the trailer so the offsets struct (which
        # sits immediately before the trailer) is found correctly in both cases.
        if self.blob[-len(BUN_TRAILER):] == BUN_TRAILER:
            trailer_end = len(self.blob)
        elif self.blob[-len(BUN_TRAILER) - 1:-1] == BUN_TRAILER:
            trailer_end = len(self.blob) - 1
        else:
            raise BunFormatError("bun trailer bytes not found at end of blob")

        # The 32-byte offsets struct sits immediately before the trailer.
        self.offsets_off = trailer_end - len(BUN_TRAILER) - 32
        (self.byte_count, self.modules_off, self.modules_len, self.entry_point_id,
         self.compile_argv_off, self.compile_argv_len, self.flags) = struct.unpack_from(
            "<QIIIIII", self.blob, self.offsets_off)

        # Bounds-check the module table and compile-argv range BEFORE any reads.
        # A crafted (modules_off, modules_len) could otherwise let struct.error
        # leak out of _read_modules; fail closed with BunFormatError instead.
        self._require_range("module table", self.modules_off, self.modules_len)
        self._require_range("compile-exec-argv", self.compile_argv_off, self.compile_argv_len)

        size_from_length = _detect_module_struct_size(self.modules_len)
        if size_from_length is None:
            # Either both 36 and 52 divide (ambiguous) or neither does (corrupt).
            if self.modules_len % 36 != 0 and self.modules_len % 52 != 0:
                raise BunFormatError(
                    f"module table length {self.modules_len} divides neither 36 nor 52")
            # When both divide, validate every record under both interpretations.
            # Record 0 cannot distinguish the layouts because its name range is
            # at offset 0 in both. Subsequent record boundaries differ, so the
            # wrong interpretation reads source/table bytes as ranges and names.
            self.module_struct_size = self._disambiguate_module_struct_size()
        else:
            self.module_struct_size = size_from_length
        if self.modules_len % self.module_struct_size != 0:
            raise BunFormatError("module table length not a multiple of the record size")
        self.modules = self._read_modules()

    def _disambiguate_module_struct_size(self):
        """Return the one record size whose complete table validates.

        Record 0 has the same name-range offset in both layouts, so inspecting
        it alone cannot distinguish 36 from 52. The record boundaries diverge
        after it. Validate every candidate record, including every range and
        every name, and accept a size only when the other interpretation fails.
        """
        def candidate_valid(ms):
            if self.modules_len < ms or self.modules_len % ms != 0:
                return False
            field_count = 6 if ms == 52 else 4
            tail = 48 if ms == 52 else 32
            for i in range(self.modules_len // ms):
                base = self.modules_off + i * ms
                ranges = [
                    struct.unpack_from("<II", self.blob, base + field * 8)
                    for field in range(field_count)
                ]

                # Bun writes every per-module region before modules_ptr, then
                # appends the complete record array. A wrong record size soon
                # reads table/source bytes as offsets; blob-wide bounds checks
                # alone admit many of those accidental ranges.
                for off, length in ranges:
                    if length == 0:
                        continue
                    end = off + length
                    if end < off or end > self.modules_off:
                        return False

                # The Linux serializer writes each key as a NUL-terminated path
                # below /$bunfs/. Check the terminator too: StringPointer.length
                # excludes it, and the runtime reads the field with sliceToZ.
                name_off, name_len = ranges[0]
                name_end = name_off + name_len
                if name_len == 0 or name_end >= self.modules_off:
                    return False
                name = self.blob[name_off:name_end]
                if not name.startswith(b"/$bunfs/") or self.blob[name_end] != 0:
                    return False

                encoding, _loader, module_format, side = struct.unpack_from(
                    "<BBBB", self.blob, base + tail)
                if encoding > 2 or module_format > 2 or side > 1:
                    return False
            return True

        valid = [ms for ms in (36, 52) if candidate_valid(ms)]
        if len(valid) == 1:
            return valid[0]
        if len(valid) == 2:
            raise BunFormatError(
                "ambiguous module table layout: both 36-byte and 52-byte records validate")
        raise BunFormatError(
            "module table matches neither 36-byte nor 52-byte record layout")

    def _looks_like_overlay(self):
        """Heuristic for the trailing-overlay form: the trailer near EOF with an
        8-byte total-byte-count after it."""
        tail = self.data[-(8 + len(BUN_TRAILER)):-8] if len(self.data) > 8 + len(BUN_TRAILER) else b""
        return tail == BUN_TRAILER

    @staticmethod
    def _detect_section_header_size(sec):
        if len(sec) < 8:
            raise BunFormatError(".bun section too small")
        as_u64 = struct.unpack_from("<Q", sec, 0)[0]
        if 8 + as_u64 == len(sec):
            return 8
        as_u32 = struct.unpack_from("<I", sec, 0)[0]
        if 4 + as_u32 == len(sec):
            return 4
        raise BunFormatError("could not determine .bun section length header form")

    def _require_range(self, label, offset, length):
        """Validate that [offset, offset+length) sits inside the bun blob.

        Treats negative offsets/lengths and addition overflow as out-of-range.
        Zero-length ranges are always allowed even when the offset is nonzero;
        bun uses zero-length fields with placeholder offsets (e.g. compile-argv
        often has offset > 0, length 0 pointing just past the data) and the
        parser must accept them since no bytes are actually read.
        """
        if length == 0:
            if offset < 0:
                raise BunFormatError(f"{label} has negative offset")
            return
        if offset < 0 or length < 0:
            raise BunFormatError(f"{label} has negative offset/length")
        end = offset + length
        if end < offset or end > len(self.blob):
            raise BunFormatError(
                f"{label} range [{offset}, {end}) is outside the bun blob (size {len(self.blob)})")

    def _read_modules(self):
        ms = self.module_struct_size
        table = self.blob[self.modules_off:self.modules_off + self.modules_len]
        mods = []
        for i in range(self.modules_len // ms):
            base = i * ms

            def rng(field_off):
                return struct.unpack_from("<II", table, base + field_off)

            rec = {
                "index": i,
                "name": rng(0),
                "contents": rng(8),
                "sourcemap": rng(16),
                "bytecode": rng(24),
            }
            if ms == 52:
                rec["moduleInfo"] = rng(32)
                rec["bytecodeOriginPath"] = rng(40)
                tail = 48
            else:
                rec["moduleInfo"] = (0, 0)
                rec["bytecodeOriginPath"] = (0, 0)
                tail = 32
            enc, ldr, fmt, side = struct.unpack_from("<BBBB", table, base + tail)
            rec.update(encoding=enc, loader=ldr, module_format=fmt, side=side)
            # Bound-check every field range against the blob. A crafted record
            # with garbage offsets must raise BunFormatError, not let an out-of-
            # range slice produce a silently truncated name_str or contents.
            for field in ("name", "contents", "sourcemap", "bytecode",
                          "moduleInfo", "bytecodeOriginPath"):
                f_off, f_len = rec[field]
                self._require_range(f"module[{i}].{field}", f_off, f_len)
            name_off, name_len = rec["name"]
            rec["name_str"] = self.blob[name_off:name_off + name_len].decode("utf-8", "replace")
            mods.append(rec)
        return mods

    # -- module lookup ------------------------------------------------------

    @staticmethod
    def is_entrypoint_name(name):
        return (
            name == "claude"
            or name.endswith("/claude")
            or name == "claude.exe"
            or name.endswith("/claude.exe")
            or name == "src/entrypoints/cli.js"
            or name.endswith("/src/entrypoints/cli.js")
        )

    def bytecode_origin_path(self, rec):
        """Return the module's recorded pre-compile source path, or '' when it
        has none: the 36-byte record form has no such field, and a module built
        without bytecode leaves it empty."""
        off, length = rec.get("bytecodeOriginPath", (0, 0))
        if length == 0:
            return ""
        self._require_range(
            f"module[{rec.get('index', '?')}].bytecodeOriginPath", off, length)
        return self.blob[off:off + length].decode("utf-8", "replace")

    def is_entrypoint_record(self, rec):
        """True if `rec` is recognizable as the Claude entrypoint, by either of
        two checks. Both are needed because each one goes blind on builds the
        other still reads:

          - The module name is the path bun packed the file under, and it gets
            renamed: 2.1.232 changed it from src/entrypoints/cli.js to plain
            cli, which the name check does not match.
          - bytecodeOriginPath is the source path bun records alongside the
            bytecode. It survived that rename, but it is absent in the 36-byte
            record form and empty in any module compiled without bytecode.
        """
        if self.bytecode_origin_path(rec).endswith(ENTRYPOINT_ORIGIN_SUFFIX):
            return True
        return self.is_entrypoint_name(rec["name_str"])

    def entrypoint_module(self):
        """Return the entrypoint module record.

        Prefer the offsets-struct `entry_point_id` (the value bun itself uses to
        dispatch at startup); if the module at that index is also recognizable
        as the entrypoint by name or by recorded source path, return it. If
        neither check recognizes it, refuse with BunFormatError rather than
        silently falling back to a search, since a silent fallback could patch
        the wrong module and still pass superficial smoke tests. The fallback
        search is only used when entry_point_id is out of range (older builds
        that did not record a useful index there).
        """
        if 0 <= self.entry_point_id < len(self.modules):
            indexed = self.modules[self.entry_point_id]
            if self.is_entrypoint_record(indexed):
                return indexed
            raise BunFormatError(
                f"entry_point_id and the entrypoint checks disagree: bun says "
                f"module {self.entry_point_id} ({indexed['name_str']!r}, source "
                f"path {self.bytecode_origin_path(indexed)!r}) is the "
                f"entrypoint, but neither its name nor its recorded source path "
                f"identifies it as the Claude entrypoint. Refusing to silently "
                f"patch a different module.")
        # entry_point_id out of range: fall back to a search over all modules.
        for m in self.modules:
            if self.is_entrypoint_record(m):
                return m
        raise BunFormatError("could not locate the Claude entrypoint module")

    def read_field(self, rec, field):
        off, length = rec[field]
        # Records produced by _read_modules are already range-checked; this is a
        # defensive guard for callers that hand in synthesized records.
        self._require_range(f"module[{rec.get('index', '?')}].{field}", off, length)
        return self.blob[off:off + length]

    def source_modules(self):
        """Return the embedded modules represented by extract_js().

        Older compiled binaries use a CJS entrypoint containing the complete
        program, plus a few small native-addon wrappers. Bun 1.4 code splitting
        uses an ESM entrypoint and stores every generated chunk as a separate ESM
        module. Include all ESM modules for that form so extraction does not
        mistake the small bootstrap for the complete program.
        """
        entrypoint = self.entrypoint_module()
        if entrypoint["module_format"] != 1:
            return [entrypoint]
        modules = [m for m in self.modules if m["module_format"] == 1]
        return modules if len(modules) > 1 else [entrypoint]

    def extract_js(self):
        """Return the complete patchable JavaScript source.

        A single-module build returns its entrypoint bytes unchanged. A split
        ESM build returns all JavaScript chunks with reversible comment markers
        between them; repack_with_js() uses those markers to map edits back to
        the original module records.
        """
        modules = self.source_modules()
        if len(modules) == 1:
            return self.read_field(modules[0], "contents")
        return _join_source_modules(self, modules)


# ---------------------------------------------------------------------------
# Surgical in-place edit of the bun blob
# ---------------------------------------------------------------------------

def _apply_blob_edits(img, edits):
    """Apply a set of in-place region edits to the bun blob and return the new
    blob bytes.

    edits: dict mapping (module_index, field_name) -> new_bytes.

    Model: the blob is a flat buffer; each module field occupies [off, off+len).
    Changing a field's content by delta D shifts every byte at position >= the
    field's original end by D. We splice the edited regions in increasing offset
    order, then rewrite the module table and the offsets struct in the new
    coordinate space. Empty (offset 0, length 0) fields keep offset 0.
    """
    blob = img.blob
    ms = img.module_struct_size
    field_order = _FIELD_ORDER_52 if ms == 52 else _FIELD_ORDER_36

    # Resolve each edit to (orig_off, orig_len, new_bytes), sorted by offset.
    edit_list = []
    for (mi, field), new_bytes in edits.items():
        off, length = img.modules[mi][field]
        # Zero-length fields come in two flavours, both refusing the same way
        # for the same reason:
        #   (0, 0)       bun's tombstone for an absent field.
        #   (N>0, 0)     placeholder offset pointing past data (e.g. how the
        #                real binary records compile-exec-argv).
        # In either case the field reserves no bytes inside the blob, so a
        # grow would have to splice the new bytes at `off` and shift every
        # downstream region; the splice/remap path treats `off` as the start
        # of a region we already own and trusts it not to overlap anything,
        # which only holds when the field is non-empty. Refuse a non-empty
        # replacement for any zero-length source field rather than corrupt
        # downstream offsets. Shrinks of zero-length to empty are no-ops and
        # remain allowed.
        if length == 0 and len(new_bytes) > 0:
            raise BunFormatError(
                f"cannot grow zero-length field module[{mi}].{field} "
                f"(offset={off}): the field reserves no bytes in the blob, "
                f"so growing it would shift downstream regions without a "
                f"matching offset remap")
        edit_list.append((off, length, mi, field, new_bytes))
    edit_list.sort(key=lambda e: e[0])

    for a, b in zip(edit_list, edit_list[1:]):
        if a[0] + a[1] > b[0]:
            raise BunFormatError("overlapping blob edits are not supported")

    # Build the remap from original byte position to new byte position.
    deltas = [(off + length, len(new_bytes) - length) for (off, length, _, _, new_bytes) in edit_list]

    def remap(pos):
        shift = 0
        for end, d in deltas:
            if pos >= end:
                shift += d
        return pos + shift

    # Splice edited regions in increasing offset order.
    out = bytearray()
    cursor = 0
    for (off, length, _mi, _field, new_bytes) in edit_list:
        out += blob[cursor:off]
        out += new_bytes
        cursor = off + length
    out += blob[cursor:]

    # Rewrite the module table at its remapped location.
    new_modules_off = remap(img.modules_off)
    new_offsets_off = remap(img.offsets_off)
    new_compile_argv_off = remap(img.compile_argv_off) if img.compile_argv_len > 0 else img.compile_argv_off

    edit_lookup = {(off_mi, off_field): nb for (_o, _l, off_mi, off_field, nb) in edit_list}

    for m in img.modules:
        rc = new_modules_off + m["index"] * ms
        for field in field_order:
            off, length = m[field]
            edited = edit_lookup.get((m["index"], field))
            if edited is not None:
                struct.pack_into("<II", out, rc, remap(off) if length > 0 else off, len(edited))
            else:
                struct.pack_into("<II", out, rc, remap(off) if length > 0 else off, length)
            rc += 8
        struct.pack_into("<BBBB", out, rc,
                         m["encoding"], m["loader"], m["module_format"], m["side"])

    # Rewrite the 32-byte offsets struct. Bun stores the struct's own offset in
    # the leading byte_count field; we preserve that convention.
    struct.pack_into("<Q", out, new_offsets_off, new_offsets_off)
    struct.pack_into("<I", out, new_offsets_off + 8, new_modules_off)
    struct.pack_into("<I", out, new_offsets_off + 12, img.modules_len)
    struct.pack_into("<I", out, new_offsets_off + 16, img.entry_point_id)
    struct.pack_into("<I", out, new_offsets_off + 20, new_compile_argv_off)
    struct.pack_into("<I", out, new_offsets_off + 24, img.compile_argv_len)
    struct.pack_into("<I", out, new_offsets_off + 28, img.flags)
    return bytes(out)


def _wrap_section(blob, section_header_size):
    if section_header_size == 8:
        return struct.pack("<Q", len(blob)) + blob
    return struct.pack("<I", len(blob)) + blob


# ---------------------------------------------------------------------------
# ELF repack for a .bun-section size change
# ---------------------------------------------------------------------------

def _section_has_payload(s):
    return s["type"] != ELF_SHT_NOBITS and s["size"] > 0


def _find_containing_load_segment(elf, sec):
    sec_file_end = sec["foff"] + sec["size"]
    sec_virt_end = sec["vaddr"] + sec["size"]
    for seg in elf.segments:
        if seg["type"] != ELF_PT_LOAD:
            continue
        contains_file = seg["foff"] <= sec["foff"] and sec_file_end <= seg["foff"] + seg["filesz"]
        contains_virt = seg["vaddr"] <= sec["vaddr"] and sec_virt_end <= seg["vaddr"] + seg["memsz"]
        if contains_file and contains_virt:
            return seg["index"]
    return None


def _repack_section_elf(orig_bytes, wrapped):
    """Replace the `.bun` section payload with `wrapped` and fix up every ELF
    field that depends on the section's size, producing a working binary.

    Fixed up, in order:
      - the `.bun` section header size,
      - e_shoff and e_phoff (if they sit after `.bun`),
      - the file offset of every section whose payload sits after `.bun`,
      - the file offset of every segment that starts after `.bun`,
      - the containing LOAD segment's filesz and memsz.

    Safety guards (fail closed, symmetric for grow AND shrink):
      - The rule the dynamic loader itself enforces: for every PT_LOAD segment
        whose file offset is shifted by the resize, the new offset must still
        satisfy `(p_offset - p_vaddr) mod p_align == 0`. If it would not,
        refuse. This rule covers any segment-remapping case we have not
        implemented (we never adjust p_vaddr) and subsumes naive grow/shrink
        special cases into one invariant.
      - Defense in depth: also refuse to shift a later allocated (SHF_ALLOC)
        section or a later/spanning loadable segment.

    The native Claude layout keeps only non-allocated metadata (`.comment`,
    `.symtab`, `.strtab`, `.shstrtab`, notes) plus the section-header table
    after `.bun`, with no later PT_LOAD, so normal patches pass all guards.
    """
    elf = Elf64(orig_bytes)
    bs = elf.section(".bun")
    if bs is None:
        raise BunFormatError(".bun section not found during repack")

    bun_off = bs["foff"]
    orig_size = bs["size"]
    bun_end = bun_off + orig_size
    growth = len(wrapped) - orig_size

    # Refuse if any payload-bearing section starts strictly inside the old .bun
    # span (would indicate an overlapping/garbled layout we must not touch).
    overlapping = [
        s for s in elf.sections
        if s["name"] != ".bun" and _section_has_payload(s)
        and bun_off < s["foff"] < bun_end
    ]
    if overlapping:
        raise BunFormatError(
            ".bun overlaps later section payloads: "
            + ", ".join(s["name"] or f"<{s['index']}>" for s in overlapping))

    shifted = [
        s for s in elf.sections
        if s["name"] != ".bun" and _section_has_payload(s) and s["foff"] >= bun_end
    ]
    shifted_alloc = [s for s in shifted if s["flags"] & ELF_SHF_ALLOC]
    # Defense in depth: refuse to shift later allocated sections at all. The
    # alignment check below subsumes this for properly-aligned
    # shifts, but allocated section shifts are unusual enough that we keep the
    # narrower refusal too. Symmetric for grow AND shrink, since either changes
    # foff without vaddr and breaks foff =~ vaddr mod p_align for those sections.
    if growth != 0 and shifted_alloc:
        raise BunFormatError(
            "cannot resize .bun before later allocated sections: "
            + ", ".join(s["name"] or f"<{s['index']}>" for s in shifted_alloc))

    containing = _find_containing_load_segment(elf, bs)

    spanning = [
        seg for seg in elf.segments
        if seg["index"] != containing and seg["filesz"] > 0
        and seg["foff"] < bun_end < seg["foff"] + seg["filesz"]
    ]
    if growth != 0 and spanning:
        raise BunFormatError(
            "cannot resize .bun inside unrelated segments: "
            + ", ".join(f"{seg['index']}:{seg['type']}" for seg in spanning))

    # PT_LOAD invariant check: for every PT_LOAD segment whose
    # file offset is shifted by the resize, the new offset must still satisfy
    # the loader's congruence `(p_offset - p_vaddr) mod p_align == 0`. This is
    # the direct invariant that keeps the dynamic loader happy; phrasing the
    # check this way (instead of "delta divides p_align") collapses grow,
    # shrink, and any future case where we might also adjust p_vaddr into one
    # rule. The defense-in-depth ALLOC-section and spanning-segment refusals
    # above remain, but this is the fundamental check.
    if growth != 0:
        for seg in elf.segments:
            if seg["type"] != ELF_PT_LOAD:
                continue
            if seg["foff"] < bun_end:
                continue  # not shifted by the resize
            align = seg["align"]
            if align in (0, 1):
                continue  # no alignment constraint
            new_foff = seg["foff"] + growth
            if (new_foff - seg["vaddr"]) % align != 0:
                raise BunFormatError(
                    f"resizing .bun by {growth} would break PT_LOAD invariant "
                    f"(p_offset - p_vaddr) mod p_align == 0 for segment "
                    f"{seg['index']} (p_align={align}); this segment-remapping "
                    f"case is not implemented")

    # Splice the new payload in: [0, bun_off) + wrapped + [bun_end, EOF).
    new_bytes = bytearray(orig_bytes[:bun_off]) + bytearray(wrapped) + bytearray(orig_bytes[bun_end:])

    def shift_if_after(value):
        return value + growth if value >= bun_end else value

    new_phoff = shift_if_after(elf.e_phoff)
    new_shoff = shift_if_after(elf.e_shoff)
    struct.pack_into("<Q", new_bytes, 32, new_phoff)
    struct.pack_into("<Q", new_bytes, 40, new_shoff)

    for s in elf.sections:
        ho = new_shoff + s["index"] * elf.e_shentsize
        if s["index"] == bs["index"]:
            struct.pack_into("<Q", new_bytes, ho + 32, len(wrapped))
            continue
        if _section_has_payload(s) and s["foff"] >= bun_end:
            struct.pack_into("<Q", new_bytes, ho + 24, s["foff"] + growth)

    for seg in elf.segments:
        ho = new_phoff + seg["index"] * elf.e_phentsize
        if seg["index"] == containing:
            struct.pack_into("<Q", new_bytes, ho + 32, seg["filesz"] + growth)
            struct.pack_into("<Q", new_bytes, ho + 40, seg["memsz"] + growth)
            continue
        if seg["filesz"] > 0 and seg["foff"] >= bun_end:
            struct.pack_into("<Q", new_bytes, ho + 8, seg["foff"] + growth)

    return bytes(new_bytes)


# ---------------------------------------------------------------------------
# Multi-module source representation
# ---------------------------------------------------------------------------

def _join_source_modules(img, modules):
    out = bytearray(_MULTI_MODULE_HEADER)
    for module in modules:
        contents = img.read_field(module, "contents")
        if _MULTI_MODULE_MARKER in contents:
            raise BunFormatError(
                f"module {module['index']} contains the reserved bundle marker")
        name = img.read_field(module, "name")
        out += _MULTI_MODULE_MARKER
        out += str(module["index"]).encode("ascii")
        out += b" "
        out += name.hex().encode("ascii")
        out += b"\n"
        out += contents
    return bytes(out)


def split_extracted_js(extracted):
    """Split extract_js() output into (index, name, contents) records.

    Legacy single-module output returns one record with index and name set to
    None. Malformed multi-module markers raise BunFormatError instead of letting
    repacking assign edited source to the wrong embedded module.
    """
    data = bytes(extracted)
    if not data.startswith(_MULTI_MODULE_HEADER):
        return [(None, None, data)]

    records = []
    seen = set()
    pos = len(_MULTI_MODULE_HEADER)
    while pos < len(data):
        if not data.startswith(_MULTI_MODULE_MARKER, pos):
            raise BunFormatError("malformed multi-module bundle marker")
        metadata_start = pos + len(_MULTI_MODULE_MARKER)
        metadata_end = data.find(b"\n", metadata_start)
        if metadata_end < 0:
            raise BunFormatError("unterminated multi-module bundle marker")
        metadata = data[metadata_start:metadata_end].split(b" ", 1)
        if len(metadata) != 2:
            raise BunFormatError("malformed multi-module bundle metadata")
        try:
            index = int(metadata[0])
            name = bytes.fromhex(metadata[1].decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            raise BunFormatError("malformed multi-module bundle metadata") from None
        if index < 0 or index in seen or not name:
            raise BunFormatError("invalid multi-module bundle index or name")
        seen.add(index)

        contents_start = metadata_end + 1
        next_pos = data.find(_MULTI_MODULE_MARKER, contents_start)
        contents_end = len(data) if next_pos < 0 else next_pos
        records.append((index, name, data[contents_start:contents_end]))
        if next_pos < 0:
            pos = len(data)
        else:
            pos = next_pos

    if not records:
        raise BunFormatError("multi-module bundle contains no modules")
    return records


def changed_js_modules(original, modified):
    """Return records whose contents changed between two extraction results."""
    before = split_extracted_js(original)
    after = split_extracted_js(modified)
    before_ids = [(index, name) for index, name, _ in before]
    after_ids = [(index, name) for index, name, _ in after]
    if before_ids != after_ids:
        raise BunFormatError("multi-module bundle markers changed during patching")
    return [
        new
        for old, new in zip(before, after)
        if old[2] != new[2]
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def can_handle(data):
    """Return True if `data` is a bun .bun-section ELF64 we can patch."""
    try:
        BunImage(data)
        return True
    except (BunFormatError, NotImplementedError):
        return False


def extract_js(data):
    """Extract the complete patchable JavaScript source from a bun binary."""
    return BunImage(data).extract_js()


def repack_with_js(data, new_js):
    """Return a binary with source edits from extract_js() applied.

    Single-module builds replace the entrypoint contents. Split ESM builds map
    each edited region back to its original module using the extraction markers.
    Bytecode remains unchanged; module offsets and ELF metadata are adjusted for
    the combined source-length delta.
    """
    img = BunImage(data)
    modules = img.source_modules()
    records = split_extracted_js(new_js)

    if len(modules) == 1:
        if len(records) != 1 or records[0][0] is not None:
            raise BunFormatError("single-module binary received multi-module source")
        edits = {(modules[0]["index"], "contents"): records[0][2]}
    else:
        if len(records) == 1 and records[0][0] is None:
            raise BunFormatError(
                "split ESM binary requires the multi-module output from extract_js()")
        expected = [
            (module["index"], img.read_field(module, "name"))
            for module in modules
        ]
        received = [(index, name) for index, name, _ in records]
        if received != expected:
            raise BunFormatError("multi-module bundle markers do not match the binary")
        edits = {
            (module["index"], "contents"): contents
            for module, (_, _, contents) in zip(modules, records)
            if contents != img.read_field(module, "contents")
        }
        if not edits:
            return img.data

    new_blob = _apply_blob_edits(img, edits)
    wrapped = _wrap_section(new_blob, img.section_header_size)
    return _repack_section_elf(img.data, wrapped)


def repack_unchanged(data):
    """Run the full identity-edit and ELF-rewrite pipeline.

    The result must equal the input exactly for both single-module and split ESM
    builds. This checks every source-module range that the public API can edit.
    """
    img = BunImage(data)
    edits = {
        (module["index"], "contents"): img.read_field(module, "contents")
        for module in img.source_modules()
    }
    new_blob = _apply_blob_edits(img, edits)
    wrapped = _wrap_section(new_blob, img.section_header_size)
    return _repack_section_elf(img.data, wrapped)


def detect_format(data):
    """Return a short label describing the binary format, for diagnostics."""
    non_elf = _detect_non_elf_format(data)
    if non_elf:
        return non_elf
    try:
        Elf64(data)
    except NotImplementedError as exc:
        return f"ELF (unsupported variant: {exc})"
    except BunFormatError:
        return "unknown"
    try:
        BunImage(data)
        return "bun ELF (.bun section, supported)"
    except NotImplementedError as exc:
        return f"bun ELF (unsupported: {exc})"
    except BunFormatError:
        return "ELF (no bun .bun section)"


# ---------------------------------------------------------------------------
# Self-test / CLI
# ---------------------------------------------------------------------------

def _selftest(path):
    """Run the gate checks against a real binary at `path` (read-only):
      1. no-op repack is byte-identical
      2. a length-changing edit produces a different binary of the expected size
      3. determinism: two identical edits produce identical bytes
    Does NOT execute the produced binary (callers can do that). Returns 0 on pass.
    """
    with open(path, "rb") as f:
        data = f.read()

    fmt = detect_format(data)
    print(f"format: {fmt}")
    if not can_handle(data):
        print("FAIL: handler cannot parse this binary")
        return 1

    img = BunImage(data)
    ep = img.entrypoint_module()
    source_modules = img.source_modules()
    js = img.extract_js()
    print(f"entrypoint: {ep['name_str']}")
    print(f"module struct size: {img.module_struct_size}  section header size: {img.section_header_size}")
    print(f"JS source: {len(js)} bytes across {len(source_modules)} module(s)  "
          f"sha256 {hashlib.sha256(js).hexdigest()[:16]}")
    print(f"bytecode: {sum(m['bytecode'][1] for m in source_modules)} bytes (left intact)")

    # Gate 1: no-op byte identity.
    noop = repack_unchanged(data)
    gate1 = noop == data
    print(f"GATE 1 no-op byte-identical: {gate1}")
    if not gate1:
        for i in range(min(len(noop), len(data))):
            if noop[i] != data[i]:
                print(f"  first diff at {i} (0x{i:x}); lengths {len(noop)} vs {len(data)}")
                break
        return 1

    # Gate 3 (determinism, done before gate 2 so we can reuse one build): two
    # independent length-changing edits must be byte-identical.
    needle = b"(Claude Code)"
    insert = b"\n(patched)"
    i = js.find(needle)
    if i < 0:
        print("WARN: '(Claude Code)' not found; using a generic length-changing edit")
        patched_js = js + b"\n//selftest-marker"
    else:
        patched_js = js[:i + len(needle)] + insert + js[i + len(needle):]

    out_a = repack_with_js(data, patched_js)
    out_b = repack_with_js(data, patched_js)
    gate3 = out_a == out_b
    print(f"GATE 3 determinism (identical bytes across runs): {gate3} "
          f"sha256 {hashlib.sha256(out_a).hexdigest()[:16]}")

    # Gate 2: length-changing edit produces the expected size delta and round-trips.
    expected_delta = len(patched_js) - len(js)
    actual_delta = len(out_a) - len(data)
    gate2_size = actual_delta == expected_delta
    re_extracted = BunImage(out_a).extract_js()
    gate2_roundtrip = re_extracted == patched_js
    print(f"GATE 2 length-changing edit: size delta {actual_delta} "
          f"(expected {expected_delta}) match={gate2_size}; "
          f"re-extract round-trip={gate2_roundtrip}")
    print("  (run the produced binary with --version to confirm the edit shows; "
          "that step needs an executable host and is done by the test harness)")

    ok = gate1 and gate2_size and gate2_roundtrip and gate3
    print(f"SELFTEST {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _usage():
    print("usage: bun_handler.py <command> <binary> [args]", file=sys.stderr)
    print("  detect <binary>                 print detected format", file=sys.stderr)
    print("  extract <binary> [out.js]       extract complete patchable JS", file=sys.stderr)
    print("  selftest <binary>               run byte-stability gates", file=sys.stderr)


def main(argv):
    if len(argv) < 2:
        _usage()
        return 2
    cmd = argv[0]
    path = argv[1]
    if cmd == "detect":
        with open(path, "rb") as f:
            print(detect_format(f.read()))
        return 0
    if cmd == "extract":
        with open(path, "rb") as f:
            js = extract_js(f.read())
        if len(argv) >= 3:
            with open(argv[2], "wb") as f:
                f.write(js)
            print(f"wrote {argv[2]} ({len(js)} bytes)")
        else:
            sys.stdout.buffer.write(js)
        return 0
    if cmd == "selftest":
        return _selftest(path)
    _usage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
