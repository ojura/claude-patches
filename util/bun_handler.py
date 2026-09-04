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
    place, remap the module and optional-record pointers, invalidate only changed
    modules' compiled views, and update the offsets struct plus ELF geometry.
    This is what makes the no-op round-trip byte-identical: nothing moves that
    does not have to.

Scope of THIS module:
  - linux-x64 ELF, `.bun`-section storage form only (the layout shipped since
    roughly native build 2.1.83), with ASCII source stored as Encoding::Latin1.
    Non-ASCII edits require Bun's UTF-16 re-encoding path and are rejected.
  - Mach-O, PE/COFF, and the pre-2.1.83 ELF trailing-overlay form are detected
    and rejected with a clear NotImplementedError. They are later milestones.

Why changed modules must shed compiled state:
  Bun stores four representations that can become stale independently: source
  bytes, a precomputed source hash, JavaScriptCore bytecode, and ESM module-info
  (imports/exports). Merely replacing source can therefore produce a binary that
  re-extracts perfectly while still executing or analysing the old program. For
  every module whose source changes, this writer preserves the module identity but
  clears its source hash, bytecode, module-info, and sourcemap pointers. Unchanged
  modules retain their compiled state byte-for-byte.
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
ELF_SHF_WRITE = 0x1
ELF_SHF_ALLOC = 0x2
ELF_PF_WRITE = 0x2
ELF_EM_X86_64 = 0x3E

# Current standalone-module-graph flags. Bits 0-4 affect runtime policy but add
# no records. Bits 5-9 append records immediately after the module table, in
# bit order. Bit 10 is descriptive only. Unknown higher bits are unsafe because
# they may insert records whose pointers this writer would fail to remap.
BUN_FLAG_SOURCE_TEXT_CONTIGUOUS = 1 << 4
BUN_FLAG_HAS_SOURCE_HASHES = 1 << 5
BUN_FLAG_HAS_BUILTIN_BYTECODE = 1 << 6
BUN_FLAG_HAS_BYTECODE_STRING_TABLE = 1 << 7
BUN_FLAG_HAS_STARTUP_MODULE_COUNT = 1 << 8
BUN_FLAG_HAS_MODULE_INFO_STRING_TABLE = 1 << 9
BUN_FLAG_CROSS_COMPILED_BYTECODE = 1 << 10
BUN_KNOWN_FLAGS = (1 << 11) - 1

# Bun's Loader enum is append-only and currently occupies discriminants 0..21.
# Invalid Rust enum discriminants are not merely unsupported values; reading
# them as `Loader` is undefined behaviour in the runtime.
BUN_MAX_LOADER = 21

# `append_bytecode_aligned` arranges every module/builtin bytecode payload and
# the shared bytecode string table on a 128-byte runtime address.
BUN_BYTECODE_ALIGNMENT = 128

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

# Bun can store executable JavaScript as one module, as a bundled CJS entrypoint
# plus small CJS wrappers, or as many split ESM chunks. The public API still
# returns one byte string so patchers can search it globally. These markers
# preserve module identity and boundaries when more than one executable source
# record is present.
_MULTI_MODULE_HEADER = b"// bun_handler multi-module bundle v1"
_MULTI_MODULE_MARKER = b"\n// bun_handler module 6e0d7c9f5a3b4d2184f176c2 "

_JS_LOADERS = frozenset((0, 1, 2, 3))  # JSX, JS, TS, TSX


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
        ei_version = data[6]
        ei_osabi = data[7]
        if ei_class != 2:
            # ELFCLASS32; we only handle 64-bit native Claude builds.
            raise NotImplementedError("only ELF64 is supported (native Claude is x64)")
        if ei_data != 1:
            raise NotImplementedError("only little-endian ELF is supported")
        if ei_version != 1:
            raise BunFormatError(f"unsupported ELF identification version {ei_version}")
        if ei_osabi not in (0, 3):  # ELFOSABI_NONE/SYSV or ELFOSABI_GNU
            raise NotImplementedError(
                f"only Linux-compatible ELF OSABI values are supported ({ei_osabi})")

        e_type = struct.unpack_from("<H", data, 16)[0]
        if e_type not in (2, 3):  # ET_EXEC or ET_DYN (PIE)
            raise NotImplementedError(f"unsupported ELF file type {e_type}")
        e_machine = struct.unpack_from("<H", data, 18)[0]
        if e_machine != ELF_EM_X86_64:
            raise NotImplementedError(
                f"only linux-x64 ELF is supported (e_machine=0x{e_machine:x})")
        e_version = struct.unpack_from("<I", data, 20)[0]
        if e_version != 1:
            raise BunFormatError(f"unsupported ELF header version {e_version}")

        self.data = data if isinstance(data, bytes) else bytes(data)
        self.e_phoff = struct.unpack_from("<Q", data, 32)[0]
        self.e_shoff = struct.unpack_from("<Q", data, 40)[0]
        self.e_ehsize = struct.unpack_from("<H", data, 52)[0]
        self.e_phentsize = struct.unpack_from("<H", data, 54)[0]
        self.e_phnum = struct.unpack_from("<H", data, 56)[0]
        self.e_shentsize = struct.unpack_from("<H", data, 58)[0]
        self.e_shnum = struct.unpack_from("<H", data, 60)[0]
        self.e_shstrndx = struct.unpack_from("<H", data, 62)[0]
        if self.e_ehsize < 64:
            raise BunFormatError("ELF header size is smaller than ELF64_Ehdr")
        self._read_sections()
        self._read_segments()

    def _read_sections(self):
        if self.e_shentsize < 64:
            raise BunFormatError("ELF section header entries too small")
        if self.e_shnum == 0:
            raise NotImplementedError("extended ELF section numbering is unsupported")
        table_end = self.e_shoff + self.e_shentsize * self.e_shnum
        if self.e_shoff <= 0 or table_end > len(self.data):
            raise BunFormatError("ELF section header table out of range")
        if self.e_shstrndx >= self.e_shnum:
            raise BunFormatError("ELF section name string table index out of range")

        self.sections = []
        for i in range(self.e_shnum):
            o = self.e_shoff + i * self.e_shentsize
            section = {
                "index": i,
                "name_off": struct.unpack_from("<I", self.data, o)[0],
                "type": struct.unpack_from("<I", self.data, o + 4)[0],
                "flags": struct.unpack_from("<Q", self.data, o + 8)[0],
                "vaddr": struct.unpack_from("<Q", self.data, o + 16)[0],
                "foff": struct.unpack_from("<Q", self.data, o + 24)[0],
                "size": struct.unpack_from("<Q", self.data, o + 32)[0],
                "addralign": struct.unpack_from("<Q", self.data, o + 48)[0],
                "hdr_off": o,
            }
            align = section["addralign"]
            if align not in (0, 1) and align & (align - 1):
                raise BunFormatError(
                    f"ELF section {i} sh_addralign is not a power of two")
            if (section["type"] != ELF_SHT_NOBITS and section["size"] > 0
                    and section["foff"] + section["size"] > len(self.data)):
                raise BunFormatError(f"ELF section {i} payload runs past end of file")
            self.sections.append(section)

        shstr = self.sections[self.e_shstrndx]
        if shstr["type"] == ELF_SHT_NOBITS:
            raise BunFormatError("ELF section name string table has no file payload")
        if shstr["foff"] + shstr["size"] > len(self.data):
            raise BunFormatError("ELF section name string table out of range")
        names = self.data[shstr["foff"]:shstr["foff"] + shstr["size"]]
        for s in self.sections:
            if s["name_off"] >= len(names):
                raise BunFormatError(
                    f"ELF section {s['index']} name offset is outside .shstrtab")
            end = names.find(b"\x00", s["name_off"])
            if end < 0:
                raise BunFormatError(
                    f"ELF section {s['index']} name is not NUL-terminated")
            try:
                s["name"] = names[s["name_off"]:end].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BunFormatError(
                    f"ELF section {s['index']} name is not valid UTF-8") from exc

    def _read_segments(self):
        if self.e_phentsize < 56:
            raise BunFormatError("ELF program header entries too small")
        if self.e_phnum == 0:
            raise BunFormatError("ELF has no program headers")
        table_end = self.e_phoff + self.e_phentsize * self.e_phnum
        if self.e_phoff <= 0 or table_end > len(self.data):
            raise BunFormatError("ELF program header table out of range")
        self.segments = []
        for i in range(self.e_phnum):
            o = self.e_phoff + i * self.e_phentsize
            segment = {
                "index": i,
                "type": struct.unpack_from("<I", self.data, o)[0],
                "flags": struct.unpack_from("<I", self.data, o + 4)[0],
                "foff": struct.unpack_from("<Q", self.data, o + 8)[0],
                "vaddr": struct.unpack_from("<Q", self.data, o + 16)[0],
                "filesz": struct.unpack_from("<Q", self.data, o + 32)[0],
                "memsz": struct.unpack_from("<Q", self.data, o + 40)[0],
                "align": struct.unpack_from("<Q", self.data, o + 48)[0],
                "hdr_off": o,
            }
            if segment["filesz"] > 0 and segment["foff"] + segment["filesz"] > len(self.data):
                raise BunFormatError(f"ELF segment {i} payload runs past end of file")
            if segment["type"] == ELF_PT_LOAD:
                if segment["filesz"] > segment["memsz"]:
                    raise BunFormatError(f"PT_LOAD segment {i} has p_filesz > p_memsz")
                align = segment["align"]
                if align not in (0, 1) and align & (align - 1):
                    raise BunFormatError(f"PT_LOAD segment {i} p_align is not a power of two")
                if align not in (0, 1) and (segment["foff"] - segment["vaddr"]) % align:
                    raise BunFormatError(
                        f"PT_LOAD segment {i} violates p_offset/p_vaddr congruence")
            self.segments.append(segment)

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
        self.data = data if isinstance(data, bytes) else bytes(data)
        self.elf = Elf64(self.data)

        bun_sections = [s for s in self.elf.sections if s["name"] == ".bun"]
        if not bun_sections:
            # No .bun section. Distinguish the trailing-overlay form (later
            # milestone) from "this is not a bun binary at all".
            if self._looks_like_overlay():
                raise NotImplementedError(
                    "this binary uses the bun trailing-overlay form (pre-2.1.83); "
                    "only the .bun-section form is supported in this milestone"
                )
            raise BunFormatError("no .bun section found; not a supported bun ELF")
        if len(bun_sections) != 1:
            raise BunFormatError("ELF contains more than one .bun section")
        self.bun_section = bun_sections[0]
        bs = self.bun_section
        if bs["type"] == ELF_SHT_NOBITS or bs["size"] == 0:
            raise BunFormatError(".bun section has no file payload")

        # Keep read-only views instead of copying the 100+ MB section twice.
        self._data_view = memoryview(self.data)
        sec = self._data_view[bs["foff"]:bs["foff"] + bs["size"]]
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
        if bytes(self.blob[-len(BUN_TRAILER):]) == BUN_TRAILER:
            trailer_end = len(self.blob)
        elif bytes(self.blob[-len(BUN_TRAILER) - 1:-1]) == BUN_TRAILER:
            trailer_end = len(self.blob) - 1
        else:
            raise BunFormatError("bun trailer bytes not found at end of blob")

        # The 32-byte offsets struct sits immediately before the trailer.
        self.offsets_off = trailer_end - len(BUN_TRAILER) - 32
        (self.byte_count, self.modules_off, self.modules_len, self.entry_point_id,
         self.compile_argv_off, self.compile_argv_len, self.flags) = struct.unpack_from(
            "<QIIIIII", self.blob, self.offsets_off)

        unknown_flags = self.flags & ~BUN_KNOWN_FLAGS
        if unknown_flags:
            raise NotImplementedError(
                f"bun module graph uses unknown flag bits 0x{unknown_flags:x}; "
                "their optional-record layout cannot be remapped safely")
        if self.byte_count != self.offsets_off:
            raise BunFormatError(
                f"bun byte_count {self.byte_count} does not equal offsets position "
                f"{self.offsets_off}")

        # Bounds-check the module table and compile-argv range BEFORE any reads.
        # A crafted (modules_off, modules_len) could otherwise let struct.error
        # leak out of _read_modules; fail closed with BunFormatError instead.
        self._require_range("module table", self.modules_off, self.modules_len)
        self._require_range("compile-exec-argv", self.compile_argv_off, self.compile_argv_len)
        if self.modules_len == 0:
            raise BunFormatError("bun module table is empty")
        if self.modules_off + self.modules_len > self.offsets_off:
            raise BunFormatError("bun module table overlaps the offsets structure")

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
        self._parse_optional_records()
        self._validate_structure()

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
                # excludes it, and the runtime reads the field with slice_to_z.
                name_off, name_len = ranges[0]
                name_end = name_off + name_len
                if name_len == 0 or name_end >= self.modules_off:
                    return False
                name = bytes(self.blob[name_off:name_end])
                if not name.startswith(b"/$bunfs/") or self.blob[name_end] != 0:
                    return False

                encoding, loader, module_format, side = struct.unpack_from(
                    "<BBBB", self.blob, base + tail)
                if (encoding > 2 or loader > BUN_MAX_LOADER
                        or module_format > 2 or side > 1):
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

        Zero-length ranges reserve no bytes and may use offset 0 or a placeholder
        offset. Non-empty ranges must be fully inside the blob.
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
            for field in ("name", "contents", "sourcemap", "bytecode",
                          "moduleInfo", "bytecodeOriginPath"):
                f_off, f_len = rec[field]
                self._require_range(f"module[{i}].{field}", f_off, f_len)
            name_off, name_len = rec["name"]
            name_bytes = bytes(self.blob[name_off:name_off + name_len])
            try:
                rec["name_str"] = name_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BunFormatError(f"module[{i}].name is not valid UTF-8") from exc
            rec["name_bytes"] = name_bytes
            mods.append(rec)
        return mods

    def _need_optional_bytes(self, at, size, label):
        end = at + size
        if at < 0 or size < 0 or end < at or end > self.offsets_off:
            raise BunFormatError(f"{label} record runs past the offsets structure")
        return end

    def _parse_optional_records(self):
        """Parse the flag-ordered records immediately following the module table.

        Record locations and every nested StringPointer are retained so the
        writer can remap them after a source splice. Treating this area as opaque
        is what originally left source hashes and shared-table pointers stale.
        """
        at = self.modules_off + self.modules_len
        module_count = len(self.modules)

        self.source_hashes_off = None
        self.source_hashes_len = 0
        self.builtin_bytecode = []
        self.bytecode_string_table_record_off = None
        self.bytecode_string_table = (0, 0)
        self.startup_module_count_record_off = None
        self.startup_module_count = 0
        self.module_info_string_table_record_off = None
        self.module_info_string_table = (0, 0)

        if self.flags & BUN_FLAG_HAS_SOURCE_HASHES:
            size = module_count * 4
            self._need_optional_bytes(at, size, "source-hash table")
            self.source_hashes_off = at
            self.source_hashes_len = size
            at += size

        if self.flags & BUN_FLAG_HAS_BUILTIN_BYTECODE:
            self._need_optional_bytes(at, 4, "builtin-bytecode count")
            count = struct.unpack_from("<I", self.blob, at)[0]
            count_off = at
            at += 4
            if count > (self.offsets_off - at) // 12:
                raise BunFormatError("builtin-bytecode count exceeds optional-record space")
            for i in range(count):
                self._need_optional_bytes(at, 12, f"builtin-bytecode[{i}]")
                builtin_id, off, length = struct.unpack_from("<III", self.blob, at)
                self._require_range(f"builtin-bytecode[{i}].bytes", off, length)
                self.builtin_bytecode.append({
                    "index": i,
                    "id": builtin_id,
                    "record_off": at,
                    "pointer_record_off": at + 4,
                    "bytes": (off, length),
                    "count_record_off": count_off,
                })
                at += 12

        if self.flags & BUN_FLAG_HAS_BYTECODE_STRING_TABLE:
            self._need_optional_bytes(at, 8, "bytecode-string-table pointer")
            self.bytecode_string_table_record_off = at
            self.bytecode_string_table = struct.unpack_from("<II", self.blob, at)
            self._require_range(
                "bytecode-string-table", *self.bytecode_string_table)
            at += 8

        if self.flags & BUN_FLAG_HAS_STARTUP_MODULE_COUNT:
            self._need_optional_bytes(at, 4, "startup-module count")
            self.startup_module_count_record_off = at
            self.startup_module_count = struct.unpack_from("<I", self.blob, at)[0]
            if self.startup_module_count > module_count:
                raise BunFormatError(
                    f"startup-module count {self.startup_module_count} exceeds "
                    f"module count {module_count}")
            at += 4

        if self.flags & BUN_FLAG_HAS_MODULE_INFO_STRING_TABLE:
            self._need_optional_bytes(at, 8, "module-info-string-table pointer")
            self.module_info_string_table_record_off = at
            self.module_info_string_table = struct.unpack_from("<II", self.blob, at)
            self._require_range(
                "module-info-string-table", *self.module_info_string_table)
            at += 8

        self.optional_tail_off = self.modules_off + self.modules_len
        self.optional_tail_end = at

    def _runtime_blob_vaddr(self):
        return self.bun_section["vaddr"] + self.section_header_size

    def _validate_structure(self):
        """Validate the serializer invariants the surgical writer depends on."""
        modules_end = self.modules_off + self.modules_len
        if self.optional_tail_off != modules_end or self.optional_tail_end > self.offsets_off:
            raise BunFormatError("optional records overlap the offsets structure")

        # compile_exec_argv is serialized after the optional records. Its
        # StringPointer length excludes the NUL terminator. A zero-length pointer
        # may be the (0,0) tombstone or point at the serializer's empty NUL.
        if self.compile_argv_len:
            end = self.compile_argv_off + self.compile_argv_len
            if self.compile_argv_off < self.optional_tail_end or end >= self.offsets_off:
                raise BunFormatError("compile-exec-argv is not after the optional records")
            if self.blob[end] != 0:
                raise BunFormatError("compile-exec-argv is not NUL-terminated")
            if 0 in self.blob[self.compile_argv_off:end]:
                raise BunFormatError("compile-exec-argv contains an interior NUL")
        elif self.compile_argv_off != 0:
            if not self.optional_tail_end <= self.compile_argv_off <= self.offsets_off:
                raise BunFormatError("empty compile-exec-argv placeholder is out of order")

        regions = []
        names = set()
        runtime_base = self._runtime_blob_vaddr()

        def add_region(label, off, length, terminator=0, alignment=None):
            if length == 0:
                return
            end = off + length
            owned_end = end + terminator
            if off < 0 or owned_end < end or owned_end > self.modules_off:
                raise BunFormatError(
                    f"{label} range (including terminator/padding) intersects "
                    "the module table or metadata tail")
            if terminator and any(self.blob[end:end + terminator]):
                raise BunFormatError(f"{label} is not correctly NUL-terminated")
            if alignment and (runtime_base + off) % alignment:
                raise BunFormatError(
                    f"{label} runtime address is not {alignment}-byte aligned")
            regions.append((off, owned_end, label))

        for m in self.modules:
            i = m["index"]
            if not 0 <= m["encoding"] <= 2:
                raise BunFormatError(f"module[{i}] has invalid Encoding {m['encoding']}")
            if not 0 <= m["loader"] <= BUN_MAX_LOADER:
                raise BunFormatError(f"module[{i}] has invalid Loader {m['loader']}")
            if not 0 <= m["module_format"] <= 2:
                raise BunFormatError(
                    f"module[{i}] has invalid ModuleFormat {m['module_format']}")
            if not 0 <= m["side"] <= 1:
                raise BunFormatError(f"module[{i}] has invalid FileSide {m['side']}")

            name = m["name_bytes"]
            if not name.startswith(b"/$bunfs/"):
                raise BunFormatError(f"module[{i}].name is not below /$bunfs/")
            if b"\x00" in name:
                raise BunFormatError(f"module[{i}].name contains an interior NUL")
            if name in names:
                raise BunFormatError(f"duplicate embedded module name {name!r}")
            names.add(name)

            add_region(f"module[{i}].name", *m["name"], terminator=1)
            contents_term = 2 if m["encoding"] == 2 else 1
            if m["encoding"] == 2:
                off, length = m["contents"]
                if length and ((runtime_base + off) % 2 or length % 2):
                    raise BunFormatError(
                        f"module[{i}].contents is invalid UTF-16 storage")
            add_region(
                f"module[{i}].contents", *m["contents"], terminator=contents_term)
            add_region(f"module[{i}].sourcemap", *m["sourcemap"])
            add_region(
                f"module[{i}].bytecode", *m["bytecode"],
                alignment=BUN_BYTECODE_ALIGNMENT)
            add_region(f"module[{i}].moduleInfo", *m["moduleInfo"])
            add_region(
                f"module[{i}].bytecodeOriginPath", *m["bytecodeOriginPath"],
                terminator=1)
            origin_off, origin_len = m["bytecodeOriginPath"]
            if origin_len and 0 in self.blob[origin_off:origin_off + origin_len]:
                raise BunFormatError(
                    f"module[{i}].bytecodeOriginPath contains an interior NUL")

        for entry in self.builtin_bytecode:
            add_region(
                f"builtin-bytecode[{entry['index']}].bytes", *entry["bytes"],
                alignment=BUN_BYTECODE_ALIGNMENT)
        add_region(
            "bytecode-string-table", *self.bytecode_string_table,
            alignment=BUN_BYTECODE_ALIGNMENT)
        add_region("module-info-string-table", *self.module_info_string_table)

        regions.sort(key=lambda r: (r[0], r[1], r[2]))
        for previous, current in zip(regions, regions[1:]):
            if previous[1] > current[0]:
                raise BunFormatError(
                    f"embedded regions overlap: {previous[2]} [{previous[0]}, "
                    f"{previous[1]}) and {current[2]} [{current[0]}, {current[1]})")

    def source_hash(self, module):
        """Return a module's stored source hash, or 0 when the table is absent."""
        index = module if isinstance(module, int) else module["index"]
        if not 0 <= index < len(self.modules):
            raise BunFormatError(f"source-hash module index {index} is out of range")
        if self.source_hashes_off is None:
            return 0
        return struct.unpack_from("<I", self.blob, self.source_hashes_off + index * 4)[0]

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
        try:
            return bytes(self.blob[off:off + length]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BunFormatError(
                f"module[{rec.get('index', '?')}].bytecodeOriginPath is not UTF-8") from exc

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
        return bytes(self.blob[off:off + length])

    def source_modules(self):
        """Return the embedded modules represented by extract_js().

        Older Claude binaries use one bundled CJS entrypoint plus separate CJS
        native-addon wrappers. Newer Bun code splitting stores the program as
        many ESM chunks. Both forms contain independently executable JavaScript
        records, so omitting the non-entrypoint records would make the advertised
        editing surface incomplete. Include every JS-like ESM/CJS module and let
        the reversible multi-module envelope preserve their identities.
        """
        entrypoint = self.entrypoint_module()
        modules = [
            m for m in self.modules
            if m["loader"] in _JS_LOADERS and m["module_format"] in (1, 2)
        ]
        if not any(m["index"] == entrypoint["index"] for m in modules):
            raise BunFormatError(
                f"entrypoint module {entrypoint['index']} is not an executable "
                f"JavaScript record (Loader={entrypoint['loader']}, "
                f"ModuleFormat={entrypoint['module_format']})")
        if len(modules) == 1:
            modules = [entrypoint]

        # The extraction format is a byte-preserving editing surface. Bun's
        # UTF-16 storage requires decode/re-encode and offset/alignment handling
        # that this milestone deliberately does not guess at. Restrict only the
        # source modules; a supported binary may still contain UTF-16 text assets.
        for m in modules:
            if m["encoding"] != 1:
                raise NotImplementedError(
                    f"patchable JavaScript module {m['index']} uses Encoding "
                    f"{m['encoding']}; only ASCII Encoding::Latin1 source is supported")
            off, length = m["contents"]
            if not bytes(self.blob[off:off + length]).isascii():
                raise NotImplementedError(
                    f"patchable JavaScript module {m['index']} contains non-ASCII "
                    "Latin-1 bytes; UTF-16 re-encoding is not implemented")
            if m["loader"] not in _JS_LOADERS:
                raise BunFormatError(
                    f"patchable JavaScript module {m['index']} has non-JS Loader "
                    f"{m['loader']}")
        return modules

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
    """Apply owned-region edits and return a remapped bun blob.

    Public callers edit module ``contents``. The lower-level mapping remains
    useful to tests, but every changed source module is handled specially:
    source hash, bytecode, module-info and sourcemap are detached so neither JSC
    evaluation nor Bun's import/export analysis can silently use stale state.

    All StringPointers in the module table and optional tail are remapped. The
    original physical cache bytes stay in the blob as unreachable padding; this
    keeps the operation surgical and avoids a full graph reserialization.
    """
    blob = img.blob
    ms = img.module_struct_size
    field_order = _FIELD_ORDER_52 if ms == 52 else _FIELD_ORDER_36

    if not isinstance(edits, dict):
        raise TypeError("edits must be a dict keyed by (module_index, field_name)")

    # Resolve and normalize each effective edit. Identity replacements are
    # deliberately discarded: they must not invalidate compiled state, which is
    # what keeps the no-op round trip byte-identical.
    edit_list = []
    zero_length_contents = []
    changed_source_modules = set()
    for key, replacement in edits.items():
        if not (isinstance(key, tuple) and len(key) == 2):
            raise BunFormatError("edit keys must be (module_index, field_name)")
        mi, field = key
        if not isinstance(mi, int) or not 0 <= mi < len(img.modules):
            raise BunFormatError(f"edit module index {mi!r} is out of range")
        if field not in field_order:
            raise BunFormatError(
                f"field {field!r} is not present in the {ms}-byte module record")
        try:
            new_bytes = bytes(replacement)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"replacement for module[{mi}].{field} is not bytes-like") from exc
        if len(new_bytes) > 0xFFFFFFFF:
            raise BunFormatError(f"replacement for module[{mi}].{field} exceeds u32 length")

        off, length = img.modules[mi][field]
        if blob[off:off + length] == new_bytes:
            continue
        if field == "contents":
            if img.modules[mi]["encoding"] != 1:
                raise NotImplementedError(
                    f"editing module[{mi}] contents with Encoding "
                    f"{img.modules[mi]['encoding']} is unsupported")
            if not new_bytes.isascii():
                raise NotImplementedError(
                    f"editing module[{mi}] with non-ASCII source requires Bun's "
                    "UTF-16 encoding path")
            changed_source_modules.add(mi)
        if length == 0 and new_bytes:
            if field != "contents":
                raise BunFormatError(
                    f"cannot grow zero-length field module[{mi}].{field} "
                    f"(offset={off}): the field reserves no bytes in the blob")
            zero_length_contents.append((mi, new_bytes))
            continue
        edit_list.append((off, length, mi, field, new_bytes))

    # Empty source fields own no bytes to replace. Allocate their new ASCII
    # source (plus NUL) immediately before the module table, then point the
    # record at that inserted island. Clear SOURCE_TEXT_CONTIGUOUS because the
    # new source may no longer belong to the serializer's original source run.
    zero_length_lookup = {}
    zero_length_block = bytearray()
    for mi, new_bytes in sorted(zero_length_contents, key=lambda item: item[0]):
        relative = len(zero_length_block)
        zero_length_block.extend(new_bytes)
        zero_length_block.append(0)
        zero_length_lookup[(mi, "contents")] = (relative, len(new_bytes))
    if zero_length_block:
        edit_list.append((
            img.modules_off, 0, -1, "zero-length-source-allocation",
            bytes(zero_length_block),
        ))

    edit_list.sort(key=lambda e: (e[0], e[1], e[2], e[3]))
    for a, b in zip(edit_list, edit_list[1:]):
        if a[0] + a[1] > b[0]:
            raise BunFormatError(
                f"overlapping blob edits are not supported: module[{a[2]}].{a[3]} "
                f"and module[{b[2]}].{b[3]}")

    def make_deltas(splices):
        return [
            (off + length, len(new_bytes) - length)
            for (off, length, _mi, _field, new_bytes) in splices
        ]

    def remap_with(pos, deltas):
        shift = 0
        for end, delta in deltas:
            if pos >= end:
                shift += delta
        value = pos + shift
        if value < 0 or value > 0xFFFFFFFF:
            raise BunFormatError(f"remapped bun offset {value} does not fit StringPointer")
        return value

    # Length-changing source edits can move later UTF-16 bodies or bytecode.
    # Insert the minimum zero padding immediately before each affected region,
    # in original-offset order, so its runtime address remains valid. Padding
    # is unreferenced and therefore does not change extracted JavaScript.
    runtime_base = img._runtime_blob_vaddr()
    alignment_targets = []
    for m in img.modules:
        if m["encoding"] == 2 and m["contents"][1]:
            alignment_targets.append(
                (m["contents"][0], 2, f"module[{m['index']}].contents"))
    for m in img.modules:
        if m["index"] not in changed_source_modules:
            off, length = m["bytecode"]
            if length:
                alignment_targets.append(
                    (off, BUN_BYTECODE_ALIGNMENT,
                     f"module[{m['index']}].bytecode"))
    for entry in img.builtin_bytecode:
        off, length = entry["bytes"]
        if length:
            alignment_targets.append(
                (off, BUN_BYTECODE_ALIGNMENT,
                 f"builtin-bytecode[{entry['index']}].bytes"))
    if img.bytecode_string_table[1]:
        alignment_targets.append(
            (img.bytecode_string_table[0], BUN_BYTECODE_ALIGNMENT,
             "bytecode-string-table"))

    splice_list = list(edit_list)
    padding_serial = 0
    for off, alignment, label in sorted(
            alignment_targets, key=lambda item: (item[0], -item[1], item[2])):
        current_deltas = make_deltas(splice_list)
        new_off = remap_with(off, current_deltas)
        padding = (-(runtime_base + new_off)) % alignment
        if padding:
            # Internal insertions sort before an owned edit at the same offset,
            # making remap(off) point to the real region after its padding.
            splice_list.append(
                (off, 0, -1, f"alignment-padding-{padding_serial}:{label}",
                 b"\x00" * padding))
            padding_serial += 1
            splice_list.sort(key=lambda e: (e[0], 0 if e[1] == 0 else 1, e[2], e[3]))

    # The ELF metadata after `.bun` has its own modular-alignment contract. A
    # source edit of (say) +30 bytes must not move an 8-byte-aligned .symtab by
    # +30. Add unreferenced bytes immediately before the Bun offsets struct so
    # the complete section delta preserves every shifted object's original
    # residue without disturbing any source/cache region.
    current_delta = sum(delta for _end, delta in make_deltas(splice_list))
    bun_end = img.bun_section["foff"] + img.bun_section["size"]
    file_alignment = _required_file_shift_alignment(img.elf, bun_end)
    file_padding = (-current_delta) % file_alignment
    if file_padding:
        splice_list.append((
            img.offsets_off, 0, -1, "elf-file-alignment-padding",
            b"\x00" * file_padding,
        ))
        splice_list.sort(key=lambda e: (e[0], 0 if e[1] == 0 else 1, e[2], e[3]))

    deltas = make_deltas(splice_list)

    def remap(pos):
        return remap_with(pos, deltas)

    # One mutable output buffer; memoryview slices avoid materializing every
    # unchanged source/bytecode run as an intermediate bytes object.
    new_size = len(blob) + sum(delta for _end, delta in deltas)
    if new_size < 0:
        raise BunFormatError("edits would produce a negative bun blob size")
    out = bytearray(new_size)
    source_cursor = 0
    dest_cursor = 0
    for off, length, _mi, _field, new_bytes in splice_list:
        unchanged = blob[source_cursor:off]
        out[dest_cursor:dest_cursor + len(unchanged)] = unchanged
        dest_cursor += len(unchanged)
        out[dest_cursor:dest_cursor + len(new_bytes)] = new_bytes
        dest_cursor += len(new_bytes)
        source_cursor = off + length
    tail = blob[source_cursor:]
    out[dest_cursor:dest_cursor + len(tail)] = tail

    new_modules_off = remap(img.modules_off)
    new_offsets_off = remap(img.offsets_off)
    # Unlike module tombstones, compile_exec_argv's zero-length pointer normally
    # names a real NUL after the optional records, so keep it in the same place
    # relative to shifted metadata.
    new_compile_argv_off = remap(img.compile_argv_off)
    edit_lookup = {
        (mi, field): new_bytes
        for (_off, _length, mi, field, new_bytes) in edit_list
    }

    # Rebuild each fixed-size record in place. Only pointers/lengths and stale
    # cache fields change; enum bytes and record order are preserved.
    for m in img.modules:
        rc = new_modules_off + m["index"] * ms
        changed_source = m["index"] in changed_source_modules
        for field in field_order:
            off, length = m[field]
            edited = edit_lookup.get((m["index"], field))
            zero_alloc = zero_length_lookup.get((m["index"], field))
            if changed_source and field in ("sourcemap", "bytecode", "moduleInfo"):
                new_off, new_length = 0, 0
            elif zero_alloc is not None:
                relative, new_length = zero_alloc
                new_off = remap(img.modules_off) - len(zero_length_block) + relative
            elif edited is not None:
                new_off = remap(off) if length else off
                new_length = len(edited)
            else:
                new_off = remap(off) if length else off
                new_length = length
            struct.pack_into("<II", out, rc, new_off, new_length)
            rc += 8
        struct.pack_into(
            "<BBBB", out, rc,
            m["encoding"], m["loader"], m["module_format"], m["side"])

    # Optional-tail records are not part of modules_ptr.length. They still carry
    # pointers into the pre-table data and therefore need the same coordinate
    # transform as module fields.
    if img.source_hashes_off is not None:
        new_hashes_off = remap(img.source_hashes_off)
        for mi in changed_source_modules:
            struct.pack_into("<I", out, new_hashes_off + mi * 4, 0)

    for entry in img.builtin_bytecode:
        record_off = remap(entry["pointer_record_off"])
        off, length = entry["bytes"]
        struct.pack_into("<II", out, record_off, remap(off) if length else off, length)

    if img.bytecode_string_table_record_off is not None:
        record_off = remap(img.bytecode_string_table_record_off)
        off, length = img.bytecode_string_table
        struct.pack_into("<II", out, record_off, remap(off) if length else off, length)

    if img.module_info_string_table_record_off is not None:
        record_off = remap(img.module_info_string_table_record_off)
        off, length = img.module_info_string_table
        struct.pack_into("<II", out, record_off, remap(off) if length else off, length)

    # Rewrite the fixed trailer struct. byte_count is exactly the offset of this
    # struct in the serialized graph.
    struct.pack_into("<Q", out, new_offsets_off, new_offsets_off)
    struct.pack_into("<I", out, new_offsets_off + 8, new_modules_off)
    struct.pack_into("<I", out, new_offsets_off + 12, img.modules_len)
    struct.pack_into("<I", out, new_offsets_off + 16, img.entry_point_id)
    struct.pack_into("<I", out, new_offsets_off + 20, new_compile_argv_off)
    struct.pack_into("<I", out, new_offsets_off + 24, img.compile_argv_len)
    new_flags = img.flags
    if zero_length_block:
        new_flags &= ~BUN_FLAG_SOURCE_TEXT_CONTIGUOUS
    struct.pack_into("<I", out, new_offsets_off + 28, new_flags)
    return out


def _wrap_section(blob, section_header_size):
    """Materialize a wrapped section payload for low-level callers/tests."""
    if section_header_size == 8:
        header = struct.pack("<Q", len(blob))
    else:
        header = struct.pack("<I", len(blob))
    wrapped = bytearray(len(header) + len(blob))
    wrapped[:len(header)] = header
    wrapped[len(header):] = blob
    return wrapped


# ---------------------------------------------------------------------------
# ELF repack for a .bun-section size change
# ---------------------------------------------------------------------------

def _section_has_payload(s):
    return s["type"] != ELF_SHT_NOBITS and s["size"] > 0


def _required_file_shift_alignment(elf, bun_end):
    """Modular alignment every file object shifted after `.bun` must retain.

    A shift divisible by ``sh_addralign`` preserves each section's original
    file-offset residue (including producer-specific layouts that are not at
    residue zero). Preserve natural alignment of a relocated ELF header table
    as well. Every admitted alignment is a power of two, so the maximum is
    their least common multiple.
    """
    required = 1
    for section in elf.sections:
        if _section_has_payload(section) and section["foff"] >= bun_end:
            required = max(required, section["addralign"] or 1)
    if elf.e_shoff >= bun_end:
        required = max(required, 8)
    if elf.e_phoff >= bun_end:
        required = max(required, 8)
    return required


def _ranges_overlap(a_start, a_end, b_start, b_end):
    return max(a_start, b_start) < min(a_end, b_end)


def _find_containing_load_segments(elf, sec):
    sec_file_end = sec["foff"] + sec["size"]
    sec_virt_end = sec["vaddr"] + sec["size"]
    result = []
    for seg in elf.segments:
        if seg["type"] != ELF_PT_LOAD:
            continue
        contains_file = (
            seg["foff"] <= sec["foff"]
            and sec_file_end <= seg["foff"] + seg["filesz"]
        )
        contains_virt = (
            seg["vaddr"] <= sec["vaddr"]
            and sec_virt_end <= seg["vaddr"] + seg["memsz"]
        )
        if contains_file and contains_virt:
            result.append(seg)
    return result


def _validate_elf_patch_layout(elf, bs, *, for_resize):
    """Validate ownership and, optionally, the nonzero-resize geometry.

    Returns the unique PT_LOAD containing `.bun`. `for_resize=True` enforces
    the narrower canonical-writer scope: `.bun` is writable and is the final
    allocated/mapped payload, so changing its size cannot trample a later
    virtual mapping whose addresses we do not rewrite.
    """
    bun_off = bs["foff"]
    bun_end = bun_off + bs["size"]
    bun_vaddr = bs["vaddr"]
    bun_vend = bun_vaddr + bs["size"]

    if not (bs["flags"] & ELF_SHF_ALLOC):
        raise BunFormatError(".bun section is not allocated into memory")
    if not (bs["flags"] & ELF_SHF_WRITE):
        raise BunFormatError(".bun section is not writable")

    overlapping = []
    for section in elf.sections:
        if section["index"] == bs["index"] or not _section_has_payload(section):
            continue
        if _ranges_overlap(
                bun_off, bun_end,
                section["foff"], section["foff"] + section["size"]):
            overlapping.append(section)
    if overlapping:
        raise BunFormatError(
            ".bun overlaps other section payloads: "
            + ", ".join(s["name"] or f"<{s['index']}>" for s in overlapping))

    ph_end = elf.e_phoff + elf.e_phentsize * elf.e_phnum
    sh_end = elf.e_shoff + elf.e_shentsize * elf.e_shnum
    if _ranges_overlap(bun_off, bun_end, elf.e_phoff, ph_end):
        raise BunFormatError(".bun overlaps the ELF program-header table")
    if _ranges_overlap(bun_off, bun_end, elf.e_shoff, sh_end):
        raise BunFormatError(".bun overlaps the ELF section-header table")

    containing_loads = _find_containing_load_segments(elf, bs)
    if len(containing_loads) != 1:
        raise BunFormatError(
            f".bun must be covered by exactly one PT_LOAD (found {len(containing_loads)})")
    containing = containing_loads[0]
    if not (containing["flags"] & ELF_PF_WRITE):
        raise BunFormatError("the PT_LOAD containing .bun is not writable")
    if (bs["foff"] - containing["foff"]
            != bs["vaddr"] - containing["vaddr"]):
        raise BunFormatError(
            ".bun file offset and virtual address disagree within its PT_LOAD")

    intersecting_segments = []
    for seg in elf.segments:
        if seg["index"] == containing["index"] or seg["filesz"] == 0:
            continue
        if _ranges_overlap(
                bun_off, bun_end,
                seg["foff"], seg["foff"] + seg["filesz"]):
            intersecting_segments.append(seg)
    if intersecting_segments:
        raise BunFormatError(
            ".bun intersects unrelated file-backed segments: "
            + ", ".join(f"{seg['index']}:{seg['type']}" for seg in intersecting_segments))

    if not for_resize:
        return containing

    # Extending a segment with a pre-existing zero-fill tail would move the
    # virtual start of that tail without relocating symbols that may point into
    # it. The shipped section-form Claude layout has no such tail: Bun's final
    # writable mapping is fully file-backed after injection.
    if containing["filesz"] != containing["memsz"]:
        raise BunFormatError(
            "cannot resize .bun inside a PT_LOAD with a zero-fill memory tail")

    containing_file_end = containing["foff"] + containing["filesz"]
    mapped_tail = memoryview(elf.data)[bun_end:containing_file_end]
    if any(mapped_tail):
        raise BunFormatError(
            "cannot resize .bun before nonzero unsectioned bytes in its PT_LOAD")

    # A section or auxiliary program header that names bytes after `.bun` has
    # its own file/virtual-address relationship. The writer shifts file offsets
    # but intentionally does not rewrite arbitrary sh_addr/p_vaddr consumers,
    # so admit only anonymous zero padding in the containing mapping's tail and
    # no independently-described payload after the section.
    described_tail_sections = [
        section for section in elf.sections
        if section["index"] != bs["index"]
        and _section_has_payload(section)
        and bun_end <= section["foff"] < containing_file_end
    ]
    if described_tail_sections:
        raise BunFormatError(
            "cannot resize .bun before section payloads in its PT_LOAD tail: "
            + ", ".join(s["name"] or f"<{s['index']}>"
                        for s in described_tail_sections))

    later_aux_segments = [
        seg for seg in elf.segments
        if seg["index"] != containing["index"]
        and seg["type"] != ELF_PT_LOAD
        and seg["filesz"] > 0
        and seg["foff"] >= bun_end
    ]
    if later_aux_segments:
        raise BunFormatError(
            "cannot resize .bun before later file-backed segments: "
            + ", ".join(f"{seg['index']}:{seg['type']}"
                        for seg in later_aux_segments))

    # The writer changes neither sh_addr nor any later p_vaddr. Require `.bun`
    # to be the final allocated section, including SHT_NOBITS sections that have
    # no file payload but do occupy virtual memory.
    later_allocated = []
    overlapping_allocated_vaddr = []
    for section in elf.sections:
        if section["index"] == bs["index"] or not (section["flags"] & ELF_SHF_ALLOC):
            continue
        section_vend = section["vaddr"] + section["size"]
        if section["size"] and _ranges_overlap(
                bun_vaddr, bun_vend, section["vaddr"], section_vend):
            overlapping_allocated_vaddr.append(section)
        elif (section["vaddr"] >= bun_vend
              or (_section_has_payload(section) and section["foff"] >= bun_end)):
            later_allocated.append(section)
    if overlapping_allocated_vaddr:
        raise BunFormatError(
            ".bun overlaps allocated virtual sections: "
            + ", ".join(s["name"] or f"<{s['index']}>"
                        for s in overlapping_allocated_vaddr))
    if later_allocated:
        raise BunFormatError(
            "cannot resize .bun before later allocated sections: "
            + ", ".join(s["name"] or f"<{s['index']}>" for s in later_allocated))

    load_segments = [seg for seg in elf.segments if seg["type"] == ELF_PT_LOAD]
    later_loads = [
        seg for seg in load_segments
        if seg["index"] != containing["index"]
        and (
            seg["index"] > containing["index"]
            or seg["foff"] + seg["filesz"] > containing["foff"] + containing["filesz"]
            or seg["vaddr"] + seg["memsz"] > containing["vaddr"] + containing["memsz"]
        )
    ]
    if later_loads:
        raise BunFormatError(
            "cannot resize .bun unless its containing PT_LOAD is final: "
            + ", ".join(str(seg["index"]) for seg in later_loads))
    return containing


def _repack_section_elf_parts(orig_bytes, payload_parts):
    """Replace `.bun` from bytes-like parts without first joining the payload."""
    orig_bytes = orig_bytes if isinstance(orig_bytes, bytes) else bytes(orig_bytes)
    parts = tuple(memoryview(part) for part in payload_parts)
    payload_size = sum(len(part) for part in parts)
    elf = Elf64(orig_bytes)
    bun_sections = [section for section in elf.sections if section["name"] == ".bun"]
    if len(bun_sections) != 1:
        raise BunFormatError("expected exactly one .bun section during repack")
    bs = bun_sections[0]

    bun_off = bs["foff"]
    orig_size = bs["size"]
    bun_end = bun_off + orig_size
    growth = payload_size - orig_size
    new_file_size = len(orig_bytes) + growth
    if new_file_size < 0:
        raise BunFormatError(".bun resize would produce a negative file size")

    required_alignment = _required_file_shift_alignment(elf, bun_end)
    if growth % required_alignment:
        raise BunFormatError(
            f".bun resize delta {growth} violates required file alignment "
            f"{required_alignment}")

    containing = _validate_elf_patch_layout(elf, bs, for_resize=bool(growth))
    if growth:
        new_filesz = containing["filesz"] + growth
        new_memsz = containing["memsz"] + growth
        if new_filesz < 0 or new_memsz < 0 or new_filesz > new_memsz:
            raise BunFormatError(".bun resize would make the containing PT_LOAD invalid")

    # Allocate exactly one mutable final-size file buffer. The caller may pass
    # `[length_header, blob_bytearray]`, avoiding a second 100+ MB wrapped copy.
    new_bytes = bytearray(new_file_size)
    new_bytes[:bun_off] = orig_bytes[:bun_off]
    write_at = bun_off
    for part in parts:
        new_bytes[write_at:write_at + len(part)] = part
        write_at += len(part)
    new_bun_end = write_at
    new_bytes[new_bun_end:] = orig_bytes[bun_end:]

    def shift_if_after(value):
        return value + growth if value >= bun_end else value

    new_phoff = shift_if_after(elf.e_phoff)
    new_shoff = shift_if_after(elf.e_shoff)
    struct.pack_into("<Q", new_bytes, 32, new_phoff)
    struct.pack_into("<Q", new_bytes, 40, new_shoff)

    for section in elf.sections:
        header_off = new_shoff + section["index"] * elf.e_shentsize
        if section["index"] == bs["index"]:
            struct.pack_into("<Q", new_bytes, header_off + 32, payload_size)
        elif _section_has_payload(section) and section["foff"] >= bun_end:
            struct.pack_into("<Q", new_bytes, header_off + 24, section["foff"] + growth)

    for seg in elf.segments:
        header_off = new_phoff + seg["index"] * elf.e_phentsize
        if seg["index"] == containing["index"]:
            struct.pack_into("<Q", new_bytes, header_off + 32, seg["filesz"] + growth)
            struct.pack_into("<Q", new_bytes, header_off + 40, seg["memsz"] + growth)
        elif seg["filesz"] > 0 and seg["foff"] >= bun_end:
            struct.pack_into("<Q", new_bytes, header_off + 8, seg["foff"] + growth)

    return bytes(new_bytes)


def _repack_section_elf(orig_bytes, wrapped):
    """Replace `.bun` with one already-wrapped bytes-like payload."""
    return _repack_section_elf_parts(orig_bytes, (wrapped,))


def _repack_blob_elf(orig_bytes, blob, section_header_size):
    """Wrap and repack a blob without materializing a full wrapped copy."""
    if section_header_size == 8:
        header = struct.pack("<Q", len(blob))
    else:
        header = struct.pack("<I", len(blob))
    return _repack_section_elf_parts(orig_bytes, (header, blob))


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
        image = BunImage(data)
        image.source_modules()
        _validate_elf_patch_layout(image.elf, image.bun_section, for_resize=True)
        return True
    except (BunFormatError, NotImplementedError):
        return False


def extract_js(data):
    """Extract the complete patchable JavaScript source from a bun binary."""
    return BunImage(data).extract_js()


def repack_with_js(data, new_js):
    """Return a binary with source edits from extract_js() applied.

    Single-module builds replace the entrypoint contents. Multi-module CJS/ESM
    builds map each edited region back to its original record using the
    extraction markers.
    Changed modules have stale source hashes, bytecode, module-info and
    sourcemaps detached; unchanged modules retain those regions. Module and
    optional-tail pointers plus ELF metadata are adjusted for the combined
    source-length delta.
    """
    img = BunImage(data)
    modules = img.source_modules()
    records = split_extracted_js(new_js)

    if len(modules) == 1:
        if len(records) != 1 or records[0][0] is not None:
            raise BunFormatError("single-module binary received multi-module source")
        if records[0][2] == img.read_field(modules[0], "contents"):
            return img.data
        edits = {(modules[0]["index"], "contents"): records[0][2]}
    else:
        if len(records) == 1 and records[0][0] is None:
            raise BunFormatError(
                "multi-module binary requires the envelope returned by extract_js()")
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
    return _repack_blob_elf(img.data, new_blob, img.section_header_size)


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
    return _repack_blob_elf(img.data, new_blob, img.section_header_size)


def detect_format(data):
    """Return a short label describing the binary and patchability boundary."""
    non_elf = _detect_non_elf_format(data)
    if non_elf:
        return non_elf
    try:
        elf = Elf64(data)
    except NotImplementedError as exc:
        return f"ELF (unsupported variant: {exc})"
    except BunFormatError:
        return "unknown"

    try:
        image = BunImage(data)
    except NotImplementedError as exc:
        return f"bun ELF (unsupported: {exc})"
    except BunFormatError as exc:
        if elf.section(".bun") is None:
            return "ELF (no bun .bun section)"
        return f"bun ELF (malformed: {exc})"

    try:
        image.source_modules()
        _validate_elf_patch_layout(
            image.elf, image.bun_section, for_resize=True)
    except (BunFormatError, NotImplementedError) as exc:
        return f"bun ELF (unsupported patch layout: {exc})"
    return "bun ELF (.bun section, supported)"


# ---------------------------------------------------------------------------
# Self-test / CLI
# ---------------------------------------------------------------------------

def _selftest(path):
    """Run byte-stability checks against a real binary at `path` (read-only):
      1. no-op repack is byte-identical
      2. a length-growing edit round-trips and invalidates stale compiled views
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
    print(f"bytecode: {sum(m['bytecode'][1] for m in source_modules)} bytes "
          f"(retained only for unchanged modules)")

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

    # Gate 2: the source delta is exact; the file may need a small positive
    # padding delta to keep later bytecode/UTF-16 regions aligned.
    expected_delta = len(patched_js) - len(js)
    actual_delta = len(out_a) - len(data)
    gate2_size = actual_delta >= expected_delta > 0
    after = BunImage(out_a)
    re_extracted = after.extract_js()
    gate2_roundtrip = re_extracted == patched_js
    changed_records = changed_js_modules(js, patched_js)
    if len(source_modules) == 1:
        changed_indices = [source_modules[0]["index"]] if changed_records else []
    else:
        changed_indices = [index for index, _name, _contents in changed_records]
    gate2_invalidated = bool(changed_indices) and all(
        after.source_hash(index) == 0
        and after.modules[index]["bytecode"] == (0, 0)
        and after.modules[index]["moduleInfo"] == (0, 0)
        and after.modules[index]["sourcemap"] == (0, 0)
        for index in changed_indices
    )
    print(f"GATE 2 length-changing edit: source delta {expected_delta}; "
          f"file delta {actual_delta} ({actual_delta - expected_delta} alignment bytes); "
          f"size-valid={gate2_size}; re-extract={gate2_roundtrip}; "
          f"compiled-state-invalidated={gate2_invalidated}")
    print("  (runtime behavior is exercised by the standalone test harness)")

    ok = gate1 and gate2_size and gate2_roundtrip and gate2_invalidated and gate3
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
