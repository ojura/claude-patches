#!/usr/bin/env python3
"""
Surgical, byte-exact, stdlib-only handler for Bun-packed native Claude CLI builds.

Anthropic ships native Claude executables produced by `bun build --compile`: a
native container with embedded JavaScript modules, JavaScriptCore bytecode, and
assets in a `.bun` section. This module extracts the complete patchable source,
applies length-changing UTF-8 edits, and repacks supported Linux x86-64 ELF and
Windows x86-64 PE32+ binaries without reserializing unrelated opaque payloads.

Design constraints (requirements, not preferences):
  - Pure Python standard library only. No node-lief, tweakcc, pyelftools, Wine,
    Node, or Bun dependency in the production reader/writer.
  - The handler is the canonical writer. We require deterministic output,
    byte-identical no-ops, structurally valid native containers, and runtime
    behavior where an executable runtime is available.
  - Surgical editing, not whole-graph reserialization. Splice changed source,
    remap known graph pointers, detach only changed modules' stale compiled
    views, and preserve unrelated opaque bytes. Ordinary edits do not append a
    duplicate copy of each changed module. Actual source growth, encoding
    conversion, graph alignment, and PE FileAlignment can still change size.
  - Repair/compaction is explicit. The recognized current-main tweakcc rebuild
    can be recovered with a matching original or as a cache-free source/asset
    graph. Compatible append-writer overhead can be compacted with a reference.

Supported native containers:
  - Linux x86-64 ELF64 with a writable, final-mapped `.bun` section.
  - Windows x86-64 PE32+ with `.bun` as the final raw/mapped section and the
    validated 4- or 8-byte Bun wrapper. PE raw FileAlignment padding is preserved as
    zero-fill. A byte-identical no-op preserves Authenticode exactly; any real
    edit invalidates the signature, so the writer removes the final certificate
    table, clears its Security Directory entry, and emits an intentionally
    unsigned executable with CheckSum zero.
  - Bun source encodings 0 (UTF-8), 1 (Latin-1), and 2 (UTF-16LE) are decoded to
    a strict UTF-8 editing surface. Identity edits preserve original storage;
    changed non-ASCII source is stored as encoding 0.
  - Mach-O and legacy trailing-overlay ELF remain unsupported.

Why changed modules must shed compiled state:
  Bun stores source bytes, a precomputed source hash, JavaScriptCore bytecode,
  and module-analysis metadata that can become stale independently. Every
  changed source module therefore clears its source hash, bytecode, module-info,
  and sourcemap pointers. Unchanged modules retain their compiled state
  byte-for-byte.
"""
import hashlib
import os
import struct
import sys
import tempfile


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

PE_MACHINE_AMD64 = 0x8664
PE32_PLUS_MAGIC = 0x20B
PE_SCN_CNT_INITIALIZED_DATA = 0x40
PE_SECURITY_DIRECTORY_INDEX = 4

# Current standalone-module-graph flags. Bits 0-4 affect runtime policy but add
# no records. Bits 5-9 append records immediately after the module table, in
# bit order. Bit 10 is descriptive only; bits 11-12 add a prelinked graph/map
# and an eight-byte runtime-options record. Unknown higher bits remain refused.
# Schema reference: Bun StandaloneModuleGraph.rs Flags/from_bytes/to_bytes.
BUN_FLAG_SOURCE_TEXT_CONTIGUOUS = 1 << 4
BUN_FLAG_HAS_SOURCE_HASHES = 1 << 5
BUN_FLAG_HAS_BUILTIN_BYTECODE = 1 << 6
BUN_FLAG_HAS_BYTECODE_STRING_TABLE = 1 << 7
BUN_FLAG_HAS_STARTUP_MODULE_COUNT = 1 << 8
BUN_FLAG_HAS_MODULE_INFO_STRING_TABLE = 1 << 9
BUN_FLAG_CROSS_COMPILED_BYTECODE = 1 << 10
BUN_FLAG_HAS_PRELINKED_MODULE_GRAPH = 1 << 11
BUN_FLAG_HAS_RUNTIME_OPTIONS = 1 << 12
BUN_KNOWN_FLAGS = (1 << 13) - 1

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
    """The binary is not a supported, well-formed Bun .bun-section image."""


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

        self.data = _immutable_bytes(data, "binary")
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




# ---------------------------------------------------------------------------
# PE32+ AMD64: last-section surgery, no third-party dependencies.
# See Microsoft PE/COFF specification (PE Format, Windows SDK).
# ---------------------------------------------------------------------------

def _align_up(value, alignment):
    return (value + alignment - 1) & -alignment


class Pe64:
    """Bounded PE32+ reader. Patch support is deliberately last-.bun-section only.

    RVAs, file positions and logical Bun payload sizes remain distinct. Other
    sections need not have unique names; only .bun must be unique. A certificate
    is validated as a bounded table, not authenticated. Editing strips that table;
    no-op writing preserves every byte. Arbitrary overlays are not guessed at.
    """

    def __init__(self, data):
        self.data = _immutable_bytes(data, "binary")
        data = self.data
        def require(offset, size, label):
            if offset < 0 or size < 0 or offset + size > len(data):
                raise BunFormatError(f"PE {label} is outside the file")
        require(0, 64, "DOS header")
        if data[:2] != b"MZ":
            raise BunFormatError("missing PE DOS magic")
        self.pe_off = struct.unpack_from("<I", data, 60)[0]
        if self.pe_off < 64:
            raise BunFormatError("PE signature overlaps DOS header")
        require(self.pe_off, 24, "COFF header")
        if data[self.pe_off:self.pe_off + 4] != b"PE\0\0":
            raise BunFormatError("missing PE signature")
        self.coff_off = self.pe_off + 4
        machine, count, _timestamp, symbols, symbol_count, optional_size, flags = (
            struct.unpack_from("<HHIIIHH", data, self.coff_off))
        self.machine = machine
        self.number_of_sections = count
        self.optional_size = optional_size
        self.characteristics = flags
        self.pointer_to_symbol_table = symbols
        self.number_of_symbols = symbol_count
        if machine != PE_MACHINE_AMD64:
            raise NotImplementedError("PE support requires AMD64 machine 0x8664")
        if not 1 <= count <= 96:
            raise BunFormatError("PE section count must be in 1..96")
        if not flags & 2 or flags & 0x2000:
            raise NotImplementedError("PE support requires an executable, not a DLL")
        if symbols or symbol_count:
            raise NotImplementedError("PE COFF symbol tables are not supported")
        self.optional_off = self.coff_off + 20
        require(self.optional_off, optional_size, "optional header")
        if optional_size < 112:
            raise BunFormatError("PE32+ optional header is too small")
        o = self.optional_off
        self.optional_magic = struct.unpack_from("<H", data, o)[0]
        if self.optional_magic != PE32_PLUS_MAGIC:
            raise NotImplementedError("only PE32+ is supported")
        self.image_base = struct.unpack_from("<Q", data, o + 24)[0]
        if self.image_base % 65536:
            raise BunFormatError("PE ImageBase is not 64-KiB aligned")
        self.section_alignment, self.file_alignment = struct.unpack_from("<II", data, o + 32)
        sa, fa = self.section_alignment, self.file_alignment
        if (sa < 4096 or sa & (sa - 1) or not 512 <= fa <= 65536
                or fa & (fa - 1) or sa < fa):
            raise NotImplementedError("unsupported PE section/file alignment")
        self.image_size, self.headers_size = struct.unpack_from("<II", data, o + 56)
        self.initialized_size = struct.unpack_from("<I", data, o + 8)[0]
        self.checksum_off = o + 64
        directory_count = struct.unpack_from("<I", data, o + 108)[0]
        self.number_of_rva_and_sizes = directory_count
        self.data_directories_off = o + 112
        if directory_count > 16 or 112 + directory_count * 8 > optional_size:
            raise BunFormatError("PE data-directory count exceeds supported header bounds")
        if directory_count < 5:
            raise NotImplementedError("PE header lacks Security Directory slot")
        self.directories = [struct.unpack_from("<II", data, o + 112 + i * 8)
                            for i in range(directory_count)]
        self.security_off = o + 112 + 4 * 8
        self.certificate_off, self.certificate_size = self.directories[4]
        self.section_table_off = o + optional_size
        require(self.section_table_off, count * 40, "section table")
        if (self.headers_size < self.section_table_off + count * 40
                or self.headers_size > len(data) or self.headers_size % fa
                or self.image_size % sa):
            raise BunFormatError("invalid PE SizeOfHeaders/SizeOfImage")
        self.sections = []
        for i in range(count):
            at = self.section_table_off + i * 40
            raw_name = data[at:at + 8].split(b"\0", 1)[0]
            try:
                name = raw_name.decode("ascii")
            except UnicodeError as exc:
                raise BunFormatError("non-ASCII PE section name") from exc
            vs, rva, rs, ro, reloc, line, nr, nl, sc = struct.unpack_from("<IIIIIIHHI", data, at + 8)
            if reloc or line or nr or nl:
                raise NotImplementedError("PE section relocation/line tables are unsupported")
            if rs:
                require(ro, rs, f"section[{i}] raw data")
                if ro < self.headers_size or ro % fa or rs % fa:
                    raise BunFormatError(f"PE section[{i}] has invalid raw alignment")
            if (rva < _align_up(self.headers_size, sa) or rva % sa
                    or rva + max(vs, rs) > self.image_size):
                raise BunFormatError(f"PE section[{i}] has invalid virtual extent")
            self.sections.append(dict(index=i, name=name, foff=ro, raw_size=rs,
                virtual_size=vs, rva=rva, vaddr=rva,
                size=vs, flags=sc, type=1, hdr_off=at, addralign=fa,
                vsize=vs, characteristics=sc))
        for key, length_key in (("foff", "raw_size"), ("rva", None)):
            spans = []
            for s in self.sections:
                length = s[length_key] if length_key else _align_up(max(s["virtual_size"], s["raw_size"]), sa)
                if length:
                    spans.append((s[key], s[key] + length))
            spans.sort()
            if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
                raise BunFormatError(f"PE sections overlap in {key} coordinates")
        bun = [s for s in self.sections if s["name"] == ".bun"]
        if not bun:
            raise NotImplementedError("PE has no Bun .bun section")
        if len(bun) != 1:
            raise BunFormatError("PE must contain exactly one .bun section")
        self.bun_section = bs = bun[0]
        if (bs["raw_size"] < 8 or not bs["flags"] & 0x40000000
                or not bs["flags"] & 0x40 or bs["flags"] & (0x20000000 | 0x20)):
            raise BunFormatError("PE .bun must be readable initialized data, not code")
        if bs["index"] != len(self.sections) - 1:
            raise BunFormatError("PE .bun must be the final section-table entry")
        if any(s is not bs and (s["rva"] >= bs["rva"] or
                (s["raw_size"] and s["foff"] + s["raw_size"] > bs["foff"]))
                for s in self.sections):
            raise NotImplementedError("PE .bun must be last in raw and virtual layout")
        # v5 supported both wrapper widths; retain that capability without
        # weakening v5.1's raw/virtual bounds. A candidate must end at the Bun
        # trailer (optionally followed by its one-byte pad). Raw section slack
        # is not part of the graph. Never guess if both forms validate.
        candidates = []
        limit = min(bs["raw_size"], bs["virtual_size"])
        start = bs["foff"]
        for width, fmt in ((8, "<Q"), (4, "<I")):
            blob_size = struct.unpack_from(fmt, data, start)[0]
            total = width + blob_size
            if blob_size < 32 + len(BUN_TRAILER) or total > limit:
                continue
            end = start + total
            at_trailer = data[end - len(BUN_TRAILER):end] == BUN_TRAILER
            before_pad = (data[end - 1:end] == b"\0" and
                data[end - len(BUN_TRAILER) - 1:end - 1] == BUN_TRAILER)
            if at_trailer or before_pad:
                candidates.append((width, total))
        if len(candidates) != 1:
            raise BunFormatError("PE .bun wrapper/trailer is invalid or ambiguous")
        self.section_header_size, payload_size = candidates[0]
        if any(memoryview(data)[start + payload_size:start + bs["raw_size"]]):
            raise BunFormatError("PE .bun contains nonzero bytes beyond its declared payload")
        # Shared graph reader sees the logical payload, not PE allocation padding.
        bs["size"] = payload_size
        self.raw_end = bs["foff"] + bs["raw_size"]
        self.certificate_gap = 0
        co, cs = self.certificate_off, self.certificate_size
        if bool(co) != bool(cs):
            raise BunFormatError("partial PE Security Directory")
        if cs:
            if co % 8 or co < self.raw_end or co + cs != len(data):
                raise NotImplementedError("only a final PE certificate table is supported")
            self.certificate_gap = co - self.raw_end
            if self.certificate_gap > 7 or any(data[self.raw_end:co]):
                raise BunFormatError("unrecognized bytes before PE certificate")
            at = co
            while at < co + cs:
                require(at, 8, "WIN_CERTIFICATE header")
                length, revision, kind = struct.unpack_from("<IHH", data, at)
                end = at + _align_up(length, 8)
                if length < 8 or end > co + cs or revision != 0x200 or kind != 2:
                    raise BunFormatError("unsupported or malformed WIN_CERTIFICATE record")
                if any(data[at + length:end]):
                    raise BunFormatError("nonzero PE certificate record padding")
                at = end
        elif self.raw_end != len(data):
            raise BunFormatError("unsigned PE overlay is unsupported")
        expected_image = _align_up(bs["rva"] + max(bs["virtual_size"], bs["raw_size"]), sa)
        if expected_image != self.image_size:
            raise BunFormatError("PE SizeOfImage disagrees with final section extent")
        if self.initialized_size < bs["raw_size"]:
            raise BunFormatError("PE SizeOfInitializedData is smaller than .bun allocation")
        for i, (rva, size) in enumerate(self.directories):
            if i == 4 or not size:
                continue
            offset = self.rva_to_file(rva, size)
            if offset + size > bs["foff"]:
                raise NotImplementedError("a PE data directory intersects the editable .bun tail")
        entry = struct.unpack_from("<I", data, o + 16)[0]
        if not any(s["rva"] <= entry < s["rva"] + s["virtual_size"]
                   and s["flags"] & 0x20000000 for s in self.sections):
            raise BunFormatError("PE entrypoint does not reference executable code")
        # Debug entries are a rare source of raw-file pointers outside sections.
        if len(self.directories) > 6 and self.directories[6][1]:
            dr, ds = self.directories[6]
            if ds % 28:
                raise BunFormatError("PE debug directory size is not a record multiple")
            do = self.rva_to_file(dr, ds)
            for at in range(do, do + ds, 28):
                size, address, pointer = struct.unpack_from("<III", data, at + 16)
                if size:
                    require(pointer, size, "debug payload")
                    if pointer + size > bs["foff"]:
                        raise NotImplementedError("PE debug payload in editable tail")
                    if address and self.rva_to_file(address, size) != pointer:
                        raise BunFormatError("PE debug RVA/raw pointer disagree")

    @property
    def size_of_image(self):
        return self.image_size

    @property
    def size_of_headers(self):
        return self.headers_size

    @property
    def size_of_initialized_data(self):
        return self.initialized_size

    @property
    def security_directory_off(self):
        return self.security_off

    def section(self, name):
        return next((s for s in self.sections if s["name"] == name), None)

    def rva_to_file(self, rva, size):
        if rva < self.headers_size and rva + size <= self.headers_size:
            return rva
        matches = [s for s in self.sections if s["rva"] <= rva
                   and rva + size <= s["rva"] + min(s["virtual_size"], s["raw_size"])]
        if len(matches) != 1:
            raise BunFormatError("PE directory RVA is not uniquely file-backed")
        return matches[0]["foff"] + rva - matches[0]["rva"]


def _read_container(data):
    return Pe64(data) if data[:2] == b"MZ" else Elf64(data)


def _valid_bun_name(name, container):
    prefixes = (b"B:/~BUN/", b"B:\\~BUN\\") if isinstance(container, Pe64) else (b"/$bunfs/",)
    return name.startswith(prefixes)


def _validate_container_patch_layout(container, section, *, for_resize):
    if isinstance(container, Pe64):
        # Pe64's constructor validated its entire supported patch layout.
        return dict(index=section["index"], foff=section["foff"],
                    filesz=section["raw_size"], memsz=section["virtual_size"])
    return _validate_elf_patch_layout(container, section, for_resize=for_resize)


def _required_container_shift_alignment(container, bun_end):
    # PE FileAlignment belongs outside the logical Bun payload. Pad the raw
    # allocation in the PE writer, not the graph, so same-size repatches do not grow.
    return 1 if isinstance(container, Pe64) else _required_file_shift_alignment(container, bun_end)


def _repack_blob_pe(orig_bytes, blob, section_header_size):
    pe = Pe64(orig_bytes)
    if section_header_size != pe.section_header_size:
        raise BunFormatError("PE wrapper width must match the input image")
    bs = pe.bun_section
    old_blob = memoryview(orig_bytes)[bs["foff"] + section_header_size:bs["foff"] + bs["size"]]
    if _buffers_equal(old_blob, blob):
        return orig_bytes
    logical = section_header_size + len(blob)
    raw_size = _align_up(logical, pe.file_alignment)
    image_size = _align_up(bs["rva"] + raw_size, pe.section_alignment)
    initialized_size = pe.initialized_size + raw_size - bs["raw_size"]
    if any(not 0 <= x <= 0xffffffff for x in (logical, raw_size, image_size, initialized_size)):
        raise BunFormatError("resized PE fields exceed u32 capacity")
    output = bytearray(bs["foff"] + raw_size)
    output[:bs["foff"]] = memoryview(orig_bytes)[:bs["foff"]]
    fmt = "<Q" if section_header_size == 8 else "<I"
    struct.pack_into(fmt, output, bs["foff"], len(blob))
    output[bs["foff"] + section_header_size:bs["foff"] + logical] = blob
    struct.pack_into("<I", output, bs["hdr_off"] + 8, logical)
    struct.pack_into("<I", output, bs["hdr_off"] + 16, raw_size)
    struct.pack_into("<I", output, pe.optional_off + 8, initialized_size)
    struct.pack_into("<I", output, pe.optional_off + 56, image_size)
    struct.pack_into("<I", output, pe.checksum_off, 0)
    struct.pack_into("<II", output, pe.security_off, 0, 0)
    return bytes(output)


def _repack_section_pe_parts(orig_bytes, payload_parts):
    """Compatibility entry point for an already-wrapped replacement section.

    Like the existing low-level ELF writer, this validates the outer container,
    not whether a deliberately supplied inner graph is runnable. Public source
    edits and explicit recovery use their own graph validation before this stage.
    """
    original = _immutable_bytes(orig_bytes, "binary")
    pe = Pe64(original)
    parts = [_immutable_bytes(part, "PE payload part") for part in payload_parts]
    payload = b"".join(parts)
    width = pe.section_header_size
    if len(payload) < width:
        raise BunFormatError("replacement PE section wrapper is truncated")
    length = struct.unpack_from("<Q" if width == 8 else "<I", payload)[0]
    if width + length != len(payload):
        raise BunFormatError("replacement PE section length does not match its wrapper")
    return _repack_blob_pe(original, memoryview(payload)[width:], width)


def _repack_blob_native(img, blob):
    """v5 helper alias, delegated to the single native writer dispatcher."""
    return _repack_blob(img.data, blob, img.section_header_size)


def _validate_pe_patch_layout(pe, section, *, for_resize):
    """v5 helper alias; Pe64 already validated the entire supported layout."""
    if not isinstance(pe, Pe64) or section is not pe.bun_section:
        raise BunFormatError("PE layout check requires its parsed .bun section")
    return section


def _repack_blob(orig_bytes, blob, section_header_size):
    if orig_bytes[:2] == b"MZ":
        return _repack_blob_pe(orig_bytes, blob, section_header_size)
    return _repack_blob_elf(orig_bytes, blob, section_header_size)


def _verify_reference_container(current, reference):
    if type(current.container) is not type(reference.container):
        raise BunFormatError("reference executable container type differs")
    if not isinstance(current.container, Pe64):
        return _verify_reference_elf(current, reference)
    c, r = current.container, reference.container
    cb, rb = current.bun_section, reference.bun_section
    # Wrapper width is part of the reference identity, not resize bookkeeping.
    if current.section_header_size != reference.section_header_size:
        raise BunFormatError("reference PE Bun wrapper width differs")
    if (cb["foff"], cb["rva"], cb["hdr_off"], c.section_table_off,
            len(c.sections), c.optional_off, c.image_base) != (
            rb["foff"], rb["rva"], rb["hdr_off"], r.section_table_off,
            len(r.sections), r.optional_off, r.image_base):
        raise BunFormatError("reference PE native placement differs")
    if c.initialized_size - cb["raw_size"] != r.initialized_size - rb["raw_size"]:
        raise BunFormatError("reference PE initialized data outside .bun differs")
    # Geometry is separately validated; no other native/header difference is allowed.
    masks = [(c.optional_off + 8, c.optional_off + 12),
             (c.optional_off + 56, c.optional_off + 60),
             (c.checksum_off, c.checksum_off + 4),
             (c.security_off, c.security_off + 8),
             (cb["hdr_off"] + 8, cb["hdr_off"] + 12),
             (cb["hdr_off"] + 16, cb["hdr_off"] + 20)]
    if not _equal_except(current._data_view[:cb["foff"]],
                         reference._data_view[:rb["foff"]], masks):
        raise BunFormatError("reference PE native bytes differ")
    if c.certificate_size and current.data != reference.data:
        raise BunFormatError("modified PE lineage must not retain a stale certificate")


class BunImage:
    """A parsed Bun graph in an ELF or PE32+ .bun section.

    Holds the raw file bytes, the located bun data blob, the offsets struct, and
    the module table, with enough metadata to apply surgical in-place edits and
    repack into a working ELF.
    """

    def __init__(self, data):
        self.data = _immutable_bytes(data, "binary")
        self.container = _read_container(self.data)
        self.elf = self.container if isinstance(self.container, Elf64) else None
        self.pe = self.container if isinstance(self.container, Pe64) else None
        self.container_kind = "PE" if self.pe is not None else "ELF"

        bun_sections = [s for s in self.container.sections if s["name"] == ".bun"]
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
        if not 0 <= self.entry_point_id < len(self.modules):
            raise BunFormatError("entry_point_id is outside the module table")
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

                # Original payloads precede the table. A compatible external
                # writer may append *source* after it. The full validator checks
                # that this source misses optional records, argv and other data.
                for field_index, (off, length) in enumerate(ranges):
                    if length == 0:
                        continue
                    end = off + length
                    limit = self.offsets_off if field_index == 1 else self.modules_off
                    if end < off or end > limit:
                        return False

                # The Linux serializer writes each key as a NUL-terminated path
                # below /$bunfs/. Check the terminator too: StringPointer.length
                # excludes it, and the runtime reads the field with slice_to_z.
                name_off, name_len = ranges[0]
                name_end = name_off + name_len
                if name_len == 0 or name_end >= self.modules_off:
                    return False
                name = bytes(self.blob[name_off:name_end])
                if not _valid_bun_name(name, self.container) or self.blob[name_end] != 0:
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

        self.prelinked_module_graph_record_off = None
        self.prelinked_module_graph = (0, 0)
        self.prelinked_module_files = ()
        self.prelinked_module_graph_record_end = None
        self.runtime_options_record_off = None
        self.runtime_options = None
        self.runtime_options_bytes = b""

        if self.flags & BUN_FLAG_HAS_PRELINKED_MODULE_GRAPH:
            self._need_optional_bytes(at, 12, "prelinked-module-graph header")
            self.prelinked_module_graph_record_off = at
            off, length, count = struct.unpack_from("<III", self.blob, at)
            self._require_range("prelinked-module-graph", off, length)
            at += 12
            if count > module_count:
                raise BunFormatError("prelinked-module file count exceeds module count")
            self._need_optional_bytes(at, count * 4, "prelinked-module file map")
            files = tuple(struct.unpack_from("<I", self.blob, at + i * 4)[0]
                          for i in range(count))
            if any(index >= module_count for index in files):
                raise BunFormatError("prelinked-module file index is out of range")
            if len(set(files)) != count:
                raise BunFormatError("prelinked-module file map contains duplicate indices")
            # An empty record is the disabled-cache representation. Leaving a
            # map for an absent graph would attach indices to a nonexistent cache.
            if bool(length) != bool(count) or (not length and off):
                raise BunFormatError("prelinked-module graph and file map disagree")
            self.prelinked_module_graph = (off, length)
            self.prelinked_module_files = files
            at += count * 4
            self.prelinked_module_graph_record_end = at

        if self.flags & BUN_FLAG_HAS_RUNTIME_OPTIONS:
            self._need_optional_bytes(at, 8, "runtime-options")
            self.runtime_options_record_off = at
            self.runtime_options_bytes = bytes(self.blob[at:at + 8])
            self.runtime_options = struct.unpack("<II", self.runtime_options_bytes)
            # These are scalar flags/value bits, not StringPointers. Preserve
            # the exact bytes, including options this editor need not interpret.
            at += 8

        self.optional_tail_off = self.modules_off + self.modules_len
        self.optional_tail_end = at

    def _runtime_blob_vaddr(self):
        # PE section VirtualAddress is an RVA (v5 compatibility); runtime
        # alignment uses the absolute loaded address. ELF already uses VAs.
        base = self.pe.image_base if self.pe is not None else 0
        return base + self.bun_section["vaddr"] + self.section_header_size

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

        def add_region(label, off, length, terminator=0, alignment=None,
                       allow_after_table=False):
            if length == 0:
                return
            end = off + length
            owned_end = end + terminator
            before_table = owned_end <= self.modules_off
            after_table = (allow_after_table and off >= self.optional_tail_end
                           and owned_end <= self.offsets_off)
            if off < 0 or owned_end < end or not (before_table or after_table):
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
            if not _valid_bun_name(name, self.container):
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
                f"module[{i}].contents", *m["contents"], terminator=contents_term,
                allow_after_table=(m["loader"] in _JS_LOADERS
                                   and m["module_format"] in (1, 2)))
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
        add_region("prelinked-module-graph", *self.prelinked_module_graph,
                   alignment=BUN_BYTECODE_ALIGNMENT)

        if self.compile_argv_len:
            regions.append((self.compile_argv_off,
                            self.compile_argv_off + self.compile_argv_len + 1,
                            "compile-exec-argv"))
        elif self.compile_argv_off and self.compile_argv_off < self.offsets_off:
            # Even an empty, allocated argv terminator must not be overwritten
            # by an appended source region. Placeholder at offsets_off owns none.
            if self.blob[self.compile_argv_off] != 0:
                raise BunFormatError("empty compile-exec-argv is not NUL-terminated")
            regions.append((self.compile_argv_off, self.compile_argv_off + 1,
                            "empty compile-exec-argv"))

        if len({entry["id"] for entry in self.builtin_bytecode}) != len(self.builtin_bytecode):
            raise BunFormatError("duplicate builtin-bytecode id")
        self.regions = tuple(sorted(regions, key=lambda r: (r[0], r[1], r[2])))
        regions = list(self.regions)
        for previous, current in zip(regions, regions[1:]):
            if previous[1] > current[0]:
                raise BunFormatError(
                    f"embedded regions overlap: {previous[2]} [{previous[0]}, "
                    f"{previous[1]}) and {current[2]} [{current[0]}, {current[1]})")

        if self.flags & BUN_FLAG_SOURCE_TEXT_CONTIGUOUS:
            contents = [r for r in regions
                        if r[2].startswith("module[") and r[2].endswith(".contents")]
            if contents:
                lo = min(r[0] for r in contents)
                hi = max(r[1] for r in contents)
                protected = [(self.modules_off, self.optional_tail_end, "module tables")]
                content_labels = {r[2] for r in contents}
                protected += [r for r in regions if r[2] not in content_labels]
                if any(_ranges_overlap(lo, hi, a, b) for a, b, _ in protected):
                    raise BunFormatError("SOURCE_TEXT_CONTIGUOUS spans non-source data")

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
        """Recognize Claude's packed entrypoint names across Bun generations.

        Modern native builds pack the bootstrap as plain `cli` (under either
        `/$bunfs/root/` or `B:/~BUN/root/`). Older builds used `claude`,
        `claude.exe`, or the source path. The offsets-struct entry_point_id is
        still authoritative; this name check is only a consistency check.
        """
        return (
            name == "cli"
            or name.endswith("/cli")
            or name == "claude"
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
        the wrong module and still pass superficial smoke tests. Out-of-range
        indices are invalid, even when a recognizable module exists elsewhere.
        """
        if 0 <= self.entry_point_id < len(self.modules):
            indexed = self.modules[self.entry_point_id]
            if (self.is_entrypoint_record(indexed)
                    or (indexed["name_str"].replace("\\", "/") in ("/$bunfs/root/cli", "B:/~BUN/root/cli")
                        and indexed["loader"] in _JS_LOADERS
                        and indexed["module_format"] in (1, 2))):
                return indexed
            raise BunFormatError(
                f"entry_point_id and the entrypoint checks disagree: bun says "
                f"module {self.entry_point_id} ({indexed['name_str']!r}, source "
                f"path {self.bytecode_origin_path(indexed)!r}) is the "
                f"entrypoint, but neither its name nor its recorded source path "
                f"identifies it as the Claude entrypoint. Refusing to silently "
                f"patch a different module.")
        raise BunFormatError("entry_point_id is outside the module table")

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

        for m in modules:
            source = self.read_source(m)  # strict decoding, including NUL rejection
            if len(modules) > 1 and _MULTI_MODULE_MARKER in source:
                raise BunFormatError(
                    f"module {m['index']} contains the reserved bundle marker")
        return modules

    def read_source(self, rec):
        """Return strict UTF-8 source without newline or Unicode normalization.

        read_field(..., "contents") remains the raw-storage accessor. UTF-16
        with unpaired surrogates is refused, rather than replacing characters.
        """
        if rec["loader"] not in _JS_LOADERS or rec["module_format"] not in (1, 2):
            raise BunFormatError(f"module[{rec['index']}] is not executable JavaScript")
        raw = self.read_field(rec, "contents")
        encoding = rec["encoding"]
        try:
            codec = {0: "utf-8", 1: "latin-1", 2: "utf-16-le"}[encoding]
            if encoding == 1 and raw.isascii():
                source = raw
            else:
                source = raw.decode(codec, "strict").encode("utf-8", "strict")
        except (KeyError, UnicodeError) as exc:
            raise BunFormatError(
                f"module[{rec['index']}] has invalid source encoding {encoding}") from exc
        if b"\x00" in source:
            raise BunFormatError(f"module[{rec['index']}] source contains an interior NUL")
        return source

    def extract_js(self):
        """Return the complete patchable JavaScript source.

        Source is returned as UTF-8, without Unicode or newline normalization.
        A single-module build returns its source directly. Multi-module CJS/ESM
        builds add reversible comment markers; repack_with_js() maps each edit
        back to its original record. Use read_field() for original storage bytes.
        """
        modules = self.source_modules()
        if len(modules) == 1:
            return self.read_source(modules[0])
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
    An effective edit also detaches the source-dependent prelinked graph and
    its file map. Fixed-size runtime options are preserved byte-for-byte.

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
    source_encodings = {}
    multiple_sources = sum(m["loader"] in _JS_LOADERS and m["module_format"] in (1, 2)
                           for m in img.modules) > 1
    edited_values = {}
    for key, replacement in edits.items():
        if not (isinstance(key, tuple) and len(key) == 2):
            raise BunFormatError("edit keys must be (module_index, field_name)")
        mi, field = key
        if type(mi) is not int or not 0 <= mi < len(img.modules):
            raise BunFormatError(f"edit module index {mi!r} is out of range")
        if field not in field_order:
            raise BunFormatError(
                f"field {field!r} is not present in the {ms}-byte module record")
        if not isinstance(replacement, (bytes, bytearray, memoryview)):
            raise TypeError(f"replacement for module[{mi}].{field} is not bytes-like")
        new_bytes = bytes(replacement)
        if len(new_bytes) > 0xFFFFFFFF:
            raise BunFormatError(f"replacement for module[{mi}].{field} exceeds u32 length")

        rec = img.modules[mi]
        off, length = rec[field]
        if field == "contents":
            # Public and low-level source replacements are UTF-8, not raw
            # storage. This semantic comparison preserves an original UTF-16
            # or Latin-1 body byte-for-byte on a no-op.
            is_source = rec["loader"] in _JS_LOADERS and rec["module_format"] in (1, 2)
            if is_source:
                _validate_utf8_source(new_bytes, f"module[{mi}] replacement")
                if multiple_sources and _MULTI_MODULE_MARKER in new_bytes:
                    raise BunFormatError(
                        f"module[{mi}] replacement contains the reserved bundle marker")
                if img.read_source(rec) == new_bytes:
                    continue
                source_encodings[mi] = 1 if rec["encoding"] == 1 and new_bytes.isascii() else 0
            else:
                # The private raw-field API is also used for ASCII text assets.
                # It must not silently transcode a non-source binary field.
                if blob[off:off + length] == new_bytes:
                    continue
                if rec["encoding"] != 1 or not new_bytes.isascii():
                    raise NotImplementedError("non-source contents edits require ASCII Latin-1")
            changed_source_modules.add(mi)
        elif blob[off:off + length] == new_bytes:
            continue
        edited_values[(mi, field)] = new_bytes
        if length == 0 and new_bytes:
            if field != "contents":
                raise BunFormatError(
                    f"cannot grow zero-length field module[{mi}].{field} "
                    f"(offset={off}): the field reserves no bytes in the blob")
            zero_length_contents.append((mi, new_bytes))
            continue
        if field == "contents":
            # Own both bytes and terminator. UTF-16 uses two terminator bytes;
            # the new UTF-8/ASCII representation uses one, with no dead copy.
            old_term = 2 if rec["encoding"] == 2 else 1
            edit_list.append((off, length + old_term, mi, field, new_bytes + b"\0"))
        else:
            edit_list.append((off, length, mi, field, new_bytes))

    # Empty source fields own no bytes to replace. Allocate their new UTF-8
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
        if (m["encoding"] == 2 and m["contents"][1]
                and m["index"] not in changed_source_modules):
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
    disable_prelinked = bool(edited_values) and img.prelinked_module_graph_record_off is not None
    if img.prelinked_module_graph[1] and not disable_prelinked:
        alignment_targets.append(
            (img.prelinked_module_graph[0], BUN_BYTECODE_ALIGNMENT,
             "prelinked-module-graph"))

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
    file_alignment = _required_container_shift_alignment(img.container, bun_end)
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
    edit_lookup = edited_values

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
            source_encodings.get(m["index"], m["encoding"]),
            m["loader"], m["module_format"], m["side"])

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

    if img.prelinked_module_graph_record_off is not None:
        record_off = remap(img.prelinked_module_graph_record_off)
        if disable_prelinked:
            # The cached link graph depends on source imports/exports. Detach
            # BOTH the graph pointer and its file-index map after any effective
            # edit. Keep the physical cache/map bytes as inert padding so an
            # equal-size edit does not reflow or duplicate module bodies.
            struct.pack_into("<III", out, record_off, 0, 0, 0)
            if img.runtime_options_record_off is not None:
                # Record order is flag-defined: runtime options now immediately
                # follow the empty 12-byte prelink record. The old map bytes and
                # old options copy remain unreferenced; argv stays where remapped.
                out[record_off + 12:record_off + 20] = img.runtime_options_bytes
        else:
            off, length = img.prelinked_module_graph
            struct.pack_into("<II", out, record_off,
                             remap(off) if length else off, length)

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
        contents = img.read_source(module)
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


def split_extracted_js(extracted, *, single_module=False):
    """Split extract_js() output into (index, name, contents) records.

    Legacy single-module output returns one record with index and name set to
    None. Malformed multi-module markers raise BunFormatError instead of letting
    repacking assign edited source to the wrong embedded module.
    """
    data = _immutable_bytes(extracted, "extracted source")
    if single_module or not data.startswith(_MULTI_MODULE_HEADER):
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


def changed_js_modules(original, modified, *, single_module=False):
    """Return records whose contents changed between two extraction results."""
    before = split_extracted_js(original, single_module=single_module)
    after = split_extracted_js(modified, single_module=single_module)
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

def _immutable_bytes(value, label):
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{label} must be bytes-like")
    return value if isinstance(value, bytes) else bytes(value)


def _validate_utf8_source(source, label):
    if b"\0" in source:
        raise BunFormatError(f"{label} contains an interior NUL")
    try:
        source.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise BunFormatError(f"{label} is not valid UTF-8") from exc


def _check_source_digest(data, expected):
    if expected is None:
        return
    if (not isinstance(expected, str) or len(expected) != 64
            or any(c not in "0123456789abcdef" for c in expected)):
        raise BunFormatError("expected_sha256 must be 64 lowercase hexadecimal characters")
    if hashlib.sha256(data).hexdigest() != expected:
        raise BunFormatError("binary changed since source extraction (SHA-256 mismatch)")


def extract_js_with_digest(data):
    """Return (executable_sha256, UTF8_envelope) from one immutable snapshot.

    Pass the digest as expected_sha256 when repacking. This binds edits to bytes,
    not a pathname, and is not an installation lock or signature verification.
    """
    data = _immutable_bytes(data, "binary")
    return hashlib.sha256(data).hexdigest(), extract_js(data)


def repack_module_sources(data, replacements, *, expected_sha256):
    """Replace explicit (index, full_name_bytes, UTF8_source_bytes) records.

    All targets are validated before output allocation. The required digest
    prevents stale work; callers with known in-memory snapshots can explicitly
    pass None to opt out. Inputs are never written or executed by this API.
    """
    data = _immutable_bytes(data, "binary")
    _check_source_digest(data, expected_sha256)
    img = BunImage(data)
    allowed = {m["index"] for m in img.source_modules()}
    edits = {}
    for replacement in replacements:
        if not isinstance(replacement, (tuple, list)) or len(replacement) != 3:
            raise BunFormatError("replacement must be (index, name_bytes, source_bytes)")
        mi, name, contents = replacement
        if type(mi) is not int or mi not in allowed:
            raise BunFormatError(f"replacement module index {mi!r} is not executable JavaScript")
        key = (mi, "contents")
        if key in edits:
            raise BunFormatError(f"duplicate replacement module index {mi}")
        name = _immutable_bytes(name, "module name")
        if img.modules[mi]["name_bytes"] != name:
            raise BunFormatError(f"replacement name does not match module index {mi}")
        contents = _immutable_bytes(contents, "replacement source")
        _validate_utf8_source(contents, f"module[{mi}] replacement")
        edits[key] = contents
    new_blob = _apply_blob_edits(img, edits)
    return _repack_blob(img.data, new_blob, img.section_header_size)


def _buffers_equal(a, b):
    """Bound temporary comparison copies to 1 MiB (inputs may be views)."""
    if len(a) != len(b):
        return False
    for pos in range(0, len(a), 1 << 20):
        if bytes(a[pos:pos + (1 << 20)]) != bytes(b[pos:pos + (1 << 20)]):
            return False
    return True


def _equal_except(a, b, allowed_ranges):
    if len(a) != len(b):
        return False
    cursor = 0
    for start, end in sorted(allowed_ranges):
        if not 0 <= start <= end <= len(a):
            raise BunFormatError("invalid identity-comparison range")
        if start > cursor and not _buffers_equal(a[cursor:start], b[cursor:start]):
            return False
        cursor = max(cursor, end)
    return _buffers_equal(a[cursor:], b[cursor:])


def _verify_reference_elf(current, reference):
    """Verify a tail-resized ELF differs only in known geometry, not native code.

    Also checks all non-Bun tail bytes, including section headers and assets.
    Nothing is identified by scanning for apparent padding or a version string.
    """
    c, r = current.elf, reference.elf
    cb, rb = current.bun_section, reference.bun_section
    if (cb["foff"], cb["vaddr"], cb["index"], current.section_header_size) != (
            rb["foff"], rb["vaddr"], rb["index"], reference.section_header_size):
        raise BunFormatError("reference has a different Bun placement/header")
    cl = _validate_elf_patch_layout(c, cb, for_resize=True)
    rl = _validate_elf_patch_layout(r, rb, for_resize=True)
    for key in ("e_phoff", "e_phentsize", "e_phnum", "e_shentsize", "e_shnum", "e_shstrndx"):
        if getattr(c, key) != getattr(r, key):
            raise BunFormatError(f"reference ELF differs in {key}")
    if cl["index"] != rl["index"] or cl["hdr_off"] != rl["hdr_off"]:
        raise BunFormatError("reference has a different containing segment")
    ce, re = cl["foff"] + cl["filesz"], rl["foff"] + rl["filesz"]
    if c.e_phoff + c.e_phnum * c.e_phentsize > cb["foff"]:
        raise BunFormatError("reference compaction requires program headers before .bun")
    if c.e_shoff < ce or r.e_shoff < re or c.e_shoff - ce != r.e_shoff - re:
        raise BunFormatError("reference compaction requires matching file-only tails")
    if len(current.data) - ce != len(reference.data) - re:
        raise BunFormatError("reference has different non-Bun tail length")

    prefix_mask = [(40, 48), (cl["hdr_off"] + 32, cl["hdr_off"] + 48)]
    if not _equal_except(current._data_view[:cb["foff"]],
                         reference._data_view[:rb["foff"]], prefix_mask):
        raise BunFormatError("reference native executable bytes do not match")

    # Validate each field before masking the locations that a tail resize edits.
    masks = []
    delta = ce - re
    for cs, rs in zip(c.sections, r.sections):
        co = cs["hdr_off"] - ce
        if co < 0:
            raise BunFormatError("section header is outside the file-only tail")
        if cs["index"] == cb["index"]:
            masks.append((co + 32, co + 40))  # validated .bun extent
        elif _section_has_payload(rs) and rs["foff"] >= re:
            if cs["foff"] != rs["foff"] + delta:
                raise BunFormatError("reference tail section offset does not match")
            masks.append((co + 24, co + 32))
    if not _equal_except(current._data_view[ce:], reference._data_view[re:], masks):
        raise BunFormatError("reference non-Bun tail bytes do not match")


def compact_with_reference(data, reference_data, *, expected_sha256=None,
                           reference_sha256=None):
    """Recover append-writer overhead using a verified matching original.

    This is separate from ordinary repacking: a no-op never compacts implicitly.
    Accepted lineage keeps the original native executable, original Bun prefix,
    assets, cached payloads and optional records byte-exact, except for explicit
    JS source pointers/encoding and their cache tombstones. The prelinked cache
    may be disabled, with identical runtime options moved after its empty record.
    All new live sources
    must be after the original offsets structure; their compiled views must be
    detached. Only after that proof are the current UTF-8 sources replayed
    surgically onto the original. No input is written or executed.

    Reverted sources can regain their matching original caches. Unknown native,
    asset, argv, policy, or prefix changes are rejected instead of discarded.
    Unsupported old relocation/rebuild writers are not guessed at. There is no
    blind gap sweeping and no attempt to strip every historical cache payload.
    The original still must be trusted by the caller; hashes bind bytes but are
    not authenticity verification. Returns the input when canonical already or
    when replay would not make the file smaller.
    """
    data = _immutable_bytes(data, "binary")
    reference_data = _immutable_bytes(reference_data, "reference binary")
    _check_source_digest(data, expected_sha256)
    _check_source_digest(reference_data, reference_sha256)
    current, reference = BunImage(data), BunImage(reference_data)
    current_sources, reference_sources = current.source_modules(), reference.source_modules()
    _verify_reference_container(current, reference)
    if (current.module_struct_size, current.modules_len, current.entry_point_id) != (
            reference.module_struct_size, reference.modules_len, reference.entry_point_id):
        raise BunFormatError("reference module graph identity does not match")
    if [(m["index"], m["name_bytes"]) for m in current_sources] != [
            (m["index"], m["name_bytes"]) for m in reference_sources]:
        raise BunFormatError("reference executable module identities do not match")
    if (current.flags & ~BUN_FLAG_SOURCE_TEXT_CONTIGUOUS
            != reference.flags & ~BUN_FLAG_SOURCE_TEXT_CONTIGUOUS):
        raise BunFormatError("reference runtime policy/optional flags differ")
    if current.runtime_options_bytes != reference.runtime_options_bytes:
        raise BunFormatError("reference runtime options differ")
    if any(m["contents"][1] and m["contents"][0] >= reference.modules_off
           for m in reference.modules):
        raise BunFormatError("reference is not an original pre-table source layout")
    if data == reference_data:
        return data

    # A canonical output of this function is already compact. Exact regeneration
    # proves idempotence without accepting arbitrary changes to original regions.
    appended = [m for m in current_sources
                if m["contents"][0] >= current.optional_tail_end]
    replacements = [(m["index"], m["name_bytes"], current.read_source(m))
                    for m in current_sources]
    if not appended:
        candidate = repack_module_sources(reference_data, replacements, expected_sha256=None)
        if candidate == data:
            return data
        raise BunFormatError("binary is neither a verified append lineage nor canonical replay")
    if current.modules_off != reference.modules_off:
        raise BunFormatError("appended lineage moved the original module table")
    if current.offsets_off < reference.offsets_off:
        raise BunFormatError("binary truncated the reference Bun prefix")
    expected_optional_end = reference.optional_tail_end
    prelink_disabled = (bool(reference.prelinked_module_graph[1])
                        and current.prelinked_module_graph == (0, 0)
                        and not current.prelinked_module_files)
    masks = []
    if prelink_disabled:
        at = reference.prelinked_module_graph_record_off
        if current.prelinked_module_graph_record_off != at:
            raise BunFormatError("reference prelinked record placement differs")
        expected_optional_end = at + 12
        masks.append((at, at + 12))  # validated empty pointer/count
        if reference.runtime_options_record_off is not None:
            if current.runtime_options_record_off != at + 12:
                raise BunFormatError("reference runtime-options placement differs")
            expected_optional_end += 8
            masks.append((at + 12, at + 20))  # exact same scalar bytes, checked above
    if (current.compile_argv_off, current.compile_argv_len,
            current.optional_tail_off, current.optional_tail_end) != (
            reference.compile_argv_off, reference.compile_argv_len,
            reference.optional_tail_off, expected_optional_end):
        raise BunFormatError("reference argv/optional record placement differs")
    source_indices = {m["index"] for m in current_sources}
    ms = current.module_struct_size
    for cm, rm in zip(current.modules, reference.modules):
        i = cm["index"]
        at = current.modules_off + i * ms
        if _buffers_equal(current.blob[at:at + ms], reference.blob[at:at + ms]):
            if current.source_hash(i) != reference.source_hash(i):
                raise BunFormatError(f"unmodified module[{i}] has a changed source hash")
            continue
        if i not in source_indices:
            raise BunFormatError(f"non-JavaScript module[{i}] changed")
        for field in ("name", "loader", "module_format", "side"):
            if cm[field] != rm[field]:
                raise BunFormatError(f"module[{i}] identity/discriminants changed")
        off, length = cm["contents"]
        if off < reference.offsets_off:
            raise BunFormatError(f"module[{i}] was not appended after the original graph")
        if off + length > current.offsets_off:
            raise BunFormatError(f"module[{i}] appended source is out of bounds")
        if any(cm[field] != (0, 0) for field in ("bytecode", "moduleInfo", "sourcemap")):
            raise BunFormatError(f"appended module[{i}] retains compiled state")
        if cm["bytecodeOriginPath"] not in ((0, 0), rm["bytecodeOriginPath"]):
            raise BunFormatError(f"appended module[{i}] has an unknown origin override")
        if current.source_hash(i) != 0:
            raise BunFormatError(f"appended module[{i}] retains a source hash")
        masks.append((at, at + ms))
        if current.source_hashes_off is not None:
            masks.append((current.source_hashes_off + i * 4,
                          current.source_hashes_off + i * 4 + 4))
    if not _equal_except(current.blob[:reference.offsets_off],
                         reference.blob[:reference.offsets_off], masks):
        raise BunFormatError("reference Bun prefix differs outside allowed source metadata")

    # All retained opaque bytecode is identical to the original and therefore
    # cannot depend on appended bytes that did not exist when it was compiled.
    # Current sources are replayed; assets and runtime are verified above.
    candidate = repack_module_sources(reference_data, replacements, expected_sha256=None)
    return candidate if len(candidate) < len(data) else data


# ---------------------------------------------------------------------------
# Explicit import of tweakcc's shipped full-graph reserializer
# ---------------------------------------------------------------------------


def _tweakcc_entrypoint_name(name):
    """The exact POSIX name predicate in tweakcc a806feb's isClaudeModule()."""
    return (name in (b"claude", b"claude.exe", b"cli", b"src/entrypoints/cli.js")
            or name.endswith((b"/claude", b"/claude.exe", b"/cli",
                              b"/src/entrypoints/cli.js")))


class _TweakccRebuildImage(BunImage):
    """Bounded recovery reader, NOT a declaration that the input is runnable.

    Recognize the exact rebuildBunData packing used at a806feb: every field,
    including empty fields, owns one trailing NUL and has the next sequential
    offset; records follow the fields, followed immediately by argv and offsets.
    It has no optional records even when flags advertise them. Compiled payloads
    are read ONLY as opaque bounded bytes, never executed or treated as caches.
    Ordinary BunImage remains strict. This reader is used only by explicit
    recovery/extraction APIs, never as a permissive fallback in normal repacking.
    """

    def _parse_optional_records(self):
        # The known writer omits these records. Do not read their advertised
        # locations, which can alias argv, offsets, or lie outside the section.
        self.optional_tail_off = self.modules_off + self.modules_len
        self.optional_tail_end = self.optional_tail_off
        self.source_hashes_off = None
        self.builtin_bytecode = []
        self.bytecode_string_table = (0, 0)
        self.bytecode_string_table_record_off = None
        self.module_info_string_table = (0, 0)
        self.module_info_string_table_record_off = None
        self.startup_module_count = 0
        self.startup_module_count_off = None
        self.prelinked_module_graph_record_off = None
        self.prelinked_module_graph_record_end = None
        self.prelinked_module_graph = (0, 0)
        self.prelinked_module_files = ()
        self.runtime_options_record_off = None
        self.runtime_options = None
        self.runtime_options_bytes = b""

    def _validate_structure(self):
        fields = _FIELD_ORDER_52 if self.module_struct_size == 52 else _FIELD_ORDER_36
        cursor = 0
        names = set()
        for m in self.modules:
            for field in fields:
                off, length = m[field]
                # Checking *empty* pointers is essential: the known writer
                # serializes an allocated NUL, not an arbitrary tombstone.
                if off != cursor or off + length >= self.modules_off:
                    raise BunFormatError("not the recognized tweakcc sequential-field layout")
                if self.blob[off + length] != 0:
                    raise BunFormatError("tweakcc rebuilt field lacks its NUL terminator")
                cursor += length + 1
            name = m["name_bytes"]
            if not _valid_bun_name(name, self.container) or b"\0" in name or name in names:
                raise BunFormatError("invalid or duplicate tweakcc rebuilt module name")
            names.add(name)
            if (m["encoding"] not in (0, 1, 2)
                    or not 0 <= m["loader"] <= BUN_MAX_LOADER
                    or m["module_format"] not in (0, 1, 2)
                    or m["side"] not in (0, 1)):
                raise BunFormatError("invalid tweakcc rebuilt module discriminant")
            origin = self.read_field(m, "bytecodeOriginPath")
            if b"\0" in origin:
                raise BunFormatError("tweakcc rebuilt origin contains NUL")
            try:
                origin.decode("utf-8", "strict")
            except UnicodeError as exc:
                raise BunFormatError("tweakcc rebuilt origin is not UTF-8") from exc
        if cursor != self.modules_off:
            raise BunFormatError("tweakcc rebuilt fields do not end at the module table")
        table_end = self.modules_off + self.modules_len
        if (self.compile_argv_off != table_end
                or table_end + self.compile_argv_len + 1 != self.offsets_off
                or self.blob[self.offsets_off - 1] != 0):
            raise BunFormatError("not the recognized tweakcc table/argv/offsets layout")
        if self.offsets_off + 32 + len(BUN_TRAILER) != len(self.blob):
            raise BunFormatError("unexpected bytes after tweakcc rebuild trailer")
        if b"\0" in bytes(self.blob[table_end:self.offsets_off - 1]):
            raise BunFormatError("tweakcc rebuilt argv contains NUL")
        self.entrypoint_module()
        _validate_container_patch_layout(self.container, self.bun_section, for_resize=True)

    def read_source(self, rec):
        # The shipped string-based writer emits modifiedClaudeJs as UTF-8 but
        # leaves the original encoding tag unchanged. Only its name-selected
        # modules have that contract; unselected source keeps stored semantics.
        if _tweakcc_entrypoint_name(rec["name_bytes"]):
            if rec["loader"] not in _JS_LOADERS or rec["module_format"] not in (1, 2):
                raise BunFormatError("tweakcc target is not executable JavaScript")
            if rec["encoding"] == 2:
                raise BunFormatError(
                    "ambiguous UTF-16-tagged tweakcc target; reference recovery required")
            raw = self.read_field(rec, "contents")
            _validate_utf8_source(raw, f"tweakcc module[{rec['index']}] source")
            return raw
        return super().read_source(rec)


def extract_tweakcc_js(data, *, expected_sha256=None):
    """Recover UTF-8 editing text from a recognized full-rebuild output.

    This validates bounded recovery structure, NOT executable viability. Source
    customizations written by tweakcc's UTF-8 string API are recovered even when
    stale Latin-1 tags would make the damaged executable decode them differently.
    Native code, assets, and opaque caches are never executed during extraction.
    """
    data = _immutable_bytes(data, "binary")
    _check_source_digest(data, expected_sha256)
    return _TweakccRebuildImage(data).extract_js()


def _rebuild_without_caches(current):
    """Create a source/asset-only graph after recognizing the broken serializer.

    This exceptional repair intentionally rebuilds the graph. No live compiled
    data is relocated: all compilation artifacts are dropped, while module
    identities, source text, asset contents and runtime policy bits are retained.
    The normal source-edit path remains surgical and byte-identical on no-ops.
    """
    if current.flags & BUN_FLAG_HAS_RUNTIME_OPTIONS:
        raise BunFormatError(
            "tweakcc discarded runtime options; a matching reference is required")
    source_modules = current.source_modules()
    if any(not m["contents"][1] and m["bytecode"][1] for m in source_modules):
        raise BunFormatError("empty source with active bytecode is ambiguous; reference required")
    sources = {m["index"]: current.read_source(m) for m in source_modules}
    fields = _FIELD_ORDER_52 if current.module_struct_size == 52 else _FIELD_ORDER_36
    out = bytearray()
    records = []

    def put(value, terminator=1, alignment=1):
        if not value:
            return (0, 0)
        out.extend(b"\0" * (-(current._runtime_blob_vaddr() + len(out)) % alignment))
        offset = len(out)
        if offset + len(value) + terminator > 0xFFFFFFFF:
            raise BunFormatError("recovered Bun graph exceeds u32 offsets")
        out.extend(value)
        out.extend(b"\0" * terminator)
        return offset, len(value)

    for m in current.modules:
        record = {field: (0, 0) for field in fields}
        record["name"] = put(m["name_bytes"])
        if m["index"] in sources:
            record["contents"] = put(sources[m["index"]])
            encoding = 0  # valid across old/new runtimes; no UTF-16 ambiguity
        else:
            encoding = m["encoding"]
            raw = current.read_field(m, "contents")
            if encoding == 2 and len(raw) % 2:
                raise BunFormatError("UTF-16 asset has an odd byte length")
            record["contents"] = put(raw, 2 if encoding == 2 else 1,
                                     2 if encoding == 2 else 1)
        records.append((record, encoding, m))
    table_off = len(out)
    table_len = len(records) * current.module_struct_size
    for record, encoding, m in records:
        for field in fields:
            out.extend(struct.pack("<II", *record[field]))
        out.extend(struct.pack("<BBBB", encoding, m["loader"], m["module_format"], m["side"]))
    argv = bytes(current.blob[current.compile_argv_off:
                              current.compile_argv_off + current.compile_argv_len])
    argv_off = len(out)
    out.extend(argv)
    out.append(0)
    alignment = _required_container_shift_alignment(
        current.container, current.bun_section["foff"] + current.bun_section["size"])
    projected_size = current.section_header_size + len(out) + 32 + len(BUN_TRAILER)
    out.extend(b"\0" * (-(projected_size - current.bun_section["size"]) % alignment))
    offsets_off = len(out)
    if offsets_off > 0xFFFFFFFF:
        raise BunFormatError("recovered Bun graph exceeds u32 offsets")
    # Preserve all four autoload-policy bits. All compiled flags/tables, source
    # layout hints, and the cross-compiled-bytecode indicator are now obsolete.
    out.extend(struct.pack("<QIIIIII", offsets_off, table_off, table_len,
                           current.entry_point_id, argv_off, len(argv), current.flags & 0xF))
    out.extend(BUN_TRAILER)
    result = _repack_blob(current.data, out, current.section_header_size)
    recovered = BunImage(result)
    if recovered.extract_js() != current.extract_js():
        raise BunFormatError("recovered source does not match imported customizations")
    # No opaque bytecode or native asset is decoded/rebuilt as JavaScript.
    for before, after in zip(current.modules, recovered.modules):
        if before["index"] not in sources and not _buffers_equal(
                current.read_field(before, "contents"), recovered.read_field(after, "contents")):
            raise BunFormatError("recovery altered an asset")
    return result


def recover_tweakcc(data, reference_data=None, *, expected_sha256=None,
                    reference_sha256=None):
    """Import the recognized current-main tweakcc full-rebuild format.

    With a trusted original: validate native ELF/tail identity, all module
    identities, argv, flags and every surviving non-source payload; only the
    current writer's name-selected UTF-8 source replacements may differ. Replay
    those sources onto the reference using the normal surgical writer, restoring
    omitted optional tables and preserving caches for unmodified source.

    Without a reference: explicitly discard ALL stale compilation artifacts,
    build a source/asset-only graph and clear their flag bits. This can reduce
    size substantially but loses precompiled startup performance. Missing source,
    unknown layouts, unsafe ELF geometry and ambiguous target encodings fail.
    A newer runtime-options record lost by this serializer cannot be reconstructed
    without a trusted reference; source-only recovery refuses to guess defaults.

    This is repair, not a no-op or a promise to shrink. Reference restoration may
    increase size because missing metadata/caches are restored. No files are
    written and neither damaged input nor output is executed. The caller must
    trust native code and, when supplied, the matching reference's authenticity.
    Supported ELF lineage is tail replacement at the same .bun base; historical
    relocation/BUN_COMPILED rewrites are not accepted by reference recovery.
    """
    data = _immutable_bytes(data, "binary")
    _check_source_digest(data, expected_sha256)
    current = _TweakccRebuildImage(data)
    if reference_data is None:
        if reference_sha256 is not None:
            raise BunFormatError("reference_sha256 requires reference_data")
        return _rebuild_without_caches(current)
    reference_data = _immutable_bytes(reference_data, "reference binary")
    _check_source_digest(reference_data, reference_sha256)
    reference = BunImage(reference_data)
    reference_sources = {m["index"] for m in reference.source_modules()}
    _verify_reference_container(current, reference)
    if (current.module_struct_size, current.modules_len, current.entry_point_id,
            current.flags) != (reference.module_struct_size, reference.modules_len,
                               reference.entry_point_id, reference.flags):
        raise BunFormatError("reference module graph identity or flags differ")
    if not _buffers_equal(
            current.blob[current.compile_argv_off:current.compile_argv_off + current.compile_argv_len],
            reference.blob[reference.compile_argv_off:reference.compile_argv_off + reference.compile_argv_len]):
        raise BunFormatError("reference compile-exec-argv differs")
    load = _validate_container_patch_layout(current.container, current.bun_section, for_resize=True)
    end = current.bun_section["foff"] + current.bun_section["size"]
    # A recognized tail replacement zero-fills the rest of the allocation. Do
    # not silently discard unrelated payload in slack outside .bun during repair.
    slack = current._data_view[end:load["foff"] + load["filesz"]]
    if any(slack):
        raise BunFormatError("unrecognized nonzero bytes after rebuilt .bun")
    replacements = []
    for cm, rm in zip(current.modules, reference.modules):
        i = cm["index"]
        if any(cm[k] != rm[k] for k in ("index", "name_bytes", "encoding", "loader",
                                        "module_format", "side")):
            raise BunFormatError(f"reference module[{i}] identity/encoding differs")
        for field in ("sourcemap", "bytecode", "moduleInfo", "bytecodeOriginPath"):
            if not _buffers_equal(current.read_field(cm, field), reference.read_field(rm, field)):
                raise BunFormatError(f"reference module[{i}].{field} differs")
        raw = current.read_field(cm, "contents")
        old_raw = reference.read_field(rm, "contents")
        if raw == old_raw:
            continue
        if i not in reference_sources or not _tweakcc_entrypoint_name(cm["name_bytes"]):
            raise BunFormatError(f"reference module[{i}] is not a tweakcc source target")
        # This branch also resolves a stale UTF-16 tag: a changed Buffer emitted
        # by the current string writer must be UTF-8, regardless of its tag.
        _validate_utf8_source(raw, f"tweakcc replacement module[{i}]")
        replacements.append((i, cm["name_bytes"], raw))
    # Exact sequential field ownership + byte comparison of every protected
    # field covers the complete rewritten graph. Its only omitted data are the
    # known optional tables, recovered from the caller's trusted original.
    return repack_module_sources(reference_data, replacements, expected_sha256=None)



def can_handle(data):
    """Return True if `data` is a bun .bun-section ELF64 we can patch."""
    try:
        image = BunImage(data)
        image.source_modules()
        _validate_container_patch_layout(image.container, image.bun_section, for_resize=True)
        return True
    except (BunFormatError, NotImplementedError):
        return False


def extract_js(data):
    """Extract the complete patchable JavaScript source from a bun binary."""
    return BunImage(data).extract_js()


def repack_with_js(data, new_js, *, expected_sha256=None):
    """Return a binary with source edits from extract_js() applied.

    Single-module builds replace the entrypoint contents. Multi-module CJS/ESM
    builds map each edited region back to its original record using the
    extraction markers.
    Changed modules have stale source hashes, bytecode, module-info and
    sourcemaps detached; unchanged modules retain those regions. Module and
    optional-tail pointers plus ELF metadata are adjusted for the combined
    source-length delta.
    """
    data = _immutable_bytes(data, "binary")
    _check_source_digest(data, expected_sha256)
    img = BunImage(data)
    modules = img.source_modules()
    records = split_extracted_js(new_js, single_module=len(modules) == 1)

    if len(modules) == 1:
        if len(records) != 1 or records[0][0] is not None:
            raise BunFormatError("single-module binary received multi-module source")
        if records[0][2] == img.read_source(modules[0]):
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
            if contents != img.read_source(module)
        }
        if not edits:
            return img.data

    new_blob = _apply_blob_edits(img, edits)
    return _repack_blob(img.data, new_blob, img.section_header_size)


def repack_unchanged(data):
    """Run the full identity-edit and ELF-rewrite pipeline.

    The result must equal the input exactly for both single-module and split ESM
    builds. This checks every source-module range that the public API can edit.
    """
    img = BunImage(data)
    edits = {
        (module["index"], "contents"): img.read_source(module)
        for module in img.source_modules()
    }
    new_blob = _apply_blob_edits(img, edits)
    return _repack_blob(img.data, new_blob, img.section_header_size)


def detect_format(data):
    """Describe actual container support; do not equate recognition with patchability."""
    data = _immutable_bytes(data, "binary")
    kind = "PE/COFF" if data[:2] == b"MZ" else "ELF"
    other = _detect_non_elf_format(data)
    if other and kind != "PE/COFF":
        return other
    try:
        container = _read_container(data)
    except NotImplementedError as exc:
        return f"{kind} (unsupported: {exc})"
    except BunFormatError as exc:
        return f"{kind} (malformed: {exc})" if kind == "PE/COFF" else "unknown"
    try:
        image = BunImage(data)
        image.source_modules()
        _validate_container_patch_layout(container, image.bun_section, for_resize=True)
    except NotImplementedError as exc:
        return f"bun {kind} (unsupported: {exc})"
    except BunFormatError as exc:
        try:
            _TweakccRebuildImage(data)
        except (BunFormatError, NotImplementedError):
            return f"bun {kind} (malformed: {exc})"
        return f"bun {kind} (tweakcc full rebuild; explicit recover-tweakcc required)"
    return f"bun {kind} (PE32+ x64, .bun section, supported)" if kind == "PE/COFF" else f"bun {kind} (.bun section, supported)"


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

    # Gate 2: UTF-8 editing length and stored length are not identical when a
    # changed Latin-1/UTF-16 module is converted to encoding 0. Account for its
    # old terminator too, then allow only nonnegative alignment overhead.
    expected_delta = len(patched_js) - len(js)
    actual_delta = len(out_a) - len(data)
    after = BunImage(out_a)
    re_extracted = after.extract_js()
    gate2_roundtrip = re_extracted == patched_js
    changed_records = changed_js_modules(js, patched_js, single_module=len(source_modules) == 1)
    if len(source_modules) == 1:
        changed_indices = [source_modules[0]["index"]] if changed_records else []
    else:
        changed_indices = [index for index, _name, _contents in changed_records]
    storage_delta = 0
    for index, (_mi, _name, source) in zip(changed_indices, changed_records):
        old = img.modules[index]
        length = old["contents"][1]
        old_size = length + (2 if old["encoding"] == 2 else 1) if length else 0
        storage_delta += len(source) + 1 - old_size
    if img.pe is not None:
        raw_delta = after.pe.bun_section["raw_size"] - img.pe.bun_section["raw_size"]
        gate2_size = (expected_delta > 0
                      and len(after.blob) - len(img.blob) >= storage_delta
                      and actual_delta == raw_delta - img.pe.certificate_size - img.pe.certificate_gap)
    else:
        gate2_size = expected_delta > 0 and actual_delta >= storage_delta
    gate2_invalidated = bool(changed_indices) and all(
        after.source_hash(index) == 0
        and after.modules[index]["bytecode"] == (0, 0)
        and after.modules[index]["moduleInfo"] == (0, 0)
        and after.modules[index]["sourcemap"] == (0, 0)
        for index in changed_indices
    )
    geometry_detail = ("PE raw alignment/certificate policy" if img.pe is not None
                       else f"{actual_delta - storage_delta} alignment bytes")
    print(f"GATE 2 length-changing edit: UTF-8 source delta {expected_delta}; "
          f"storage delta {storage_delta}; file delta {actual_delta} "
          f"({geometry_detail}); "
          f"size-valid={gate2_size}; re-extract={gate2_roundtrip}; "
          f"compiled-state-invalidated={gate2_invalidated}")
    print("  (runtime behavior is exercised by the standalone test harness)")

    ok = gate1 and gate2_size and gate2_roundtrip and gate2_invalidated and gate3
    print(f"SELFTEST {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _write_new_file(path, data, *, mode=0o600):
    """Publish a complete new file; never overwrite an existing path or inode.

    A same-directory temporary file is fsynced before exclusive hard-link
    publication. Failure leaves existing files/symlinks alone. This is not an
    in-place installer or a lock against independent installation updates.
    """
    data = _immutable_bytes(data, "output bytes")
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".bun-handler-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            # POSIX modes do not express Windows ACLs. mkstemp uses an exclusive
            # new file on both hosts; leave the Windows directory ACL in charge.
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def _write_new_executable(path, data):
    """Publish a complete new mode-0700 executable without overwriting anything."""
    _write_new_file(path, data, mode=0o700)


def _usage():
    print("usage: bun_handler.py <command> <binary> [args]", file=sys.stderr)
    print("  detect <binary>                 print detected format", file=sys.stderr)
    print("  extract <binary> [out.js]       extract complete patchable JS", file=sys.stderr)
    print("  extract-tweakcc <binary> [new-out.js]  read recognized rebuild source", file=sys.stderr)
    print("  selftest <binary>               run byte-stability gates", file=sys.stderr)
    print("  recover-tweakcc <binary> <new-output> [--reference <original>]", file=sys.stderr)
    print("  compact <binary> <reference> <new-output>  recover verified append overhead", file=sys.stderr)


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
    if cmd in ("extract", "extract-tweakcc"):
        if len(argv) not in (2, 3):
            _usage()
            return 2
        try:
            with open(path, "rb") as f:
                js = (extract_js if cmd == "extract" else extract_tweakcc_js)(f.read())
        except (OSError, BunFormatError, NotImplementedError) as exc:
            print(f"{cmd} failed: {exc}", file=sys.stderr)
            return 1
        if len(argv) >= 3:
            try:
                _write_new_file(argv[2], js)
            except OSError as exc:
                print(f"extract failed: {exc}", file=sys.stderr)
                return 1
            print(f"wrote {argv[2]} ({len(js)} bytes)")
        else:
            sys.stdout.buffer.write(js)
        return 0
    if cmd == "selftest":
        return _selftest(path)
    if cmd == "recover-tweakcc":
        if len(argv) not in (3, 5) or (len(argv) == 5 and argv[3] != "--reference"):
            _usage()
            return 2
        try:
            with open(path, "rb") as stream:
                original = stream.read()
            reference = None
            if len(argv) == 5:
                with open(argv[4], "rb") as stream:
                    reference = stream.read()
            result = recover_tweakcc(original, reference)
            _write_new_executable(argv[2], result)
        except (OSError, BunFormatError, NotImplementedError) as exc:
            print(f"recover-tweakcc failed: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {argv[2]}: {len(original)} -> {len(result)} bytes")
        print("policy: " + ("verified reference replay" if reference is not None
                            else "source/asset-only; all compiled caches discarded"))
        print(f"sha256: {hashlib.sha256(result).hexdigest()}")
        return 0
    if cmd == "compact":
        if len(argv) != 4:
            _usage()
            return 2
        try:
            with open(path, "rb") as stream:
                original = stream.read()
            with open(argv[2], "rb") as stream:
                reference = stream.read()
            result = compact_with_reference(original, reference)
            _write_new_executable(argv[3], result)
        except (OSError, BunFormatError, NotImplementedError) as exc:
            print(f"compact failed: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {argv[3]}: {len(original)} -> {len(result)} bytes; "
              f"recovered {len(original) - len(result)} bytes")
        print(f"sha256: {hashlib.sha256(result).hexdigest()}")
        return 0
    _usage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
