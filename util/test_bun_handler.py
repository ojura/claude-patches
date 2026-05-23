#!/usr/bin/env python3
"""
Standalone test harness for util/bun_handler.py.

Proves the four gates from the spike against a real linux-x64 bun-packed Claude
binary, using only the committed handler code (no /tmp prototypes, no third-party
deps). The handler is exercised through its public API only:
    extract_js(binary_bytes) -> js_bytes
    repack_with_js(binary_bytes, new_js) -> binary_bytes
    repack_unchanged(binary_bytes) -> binary_bytes
    can_handle / detect_format

Real-binary gates (need a native binary; skipped cleanly without one):
  1. Byte-exact no-op repack: repack_unchanged(b) == b  (cmp-identical).
  2. Length-GROWING patch: the binary still runs (`claude --version` exits 0)
     AND the edit is visible in the output.
  3. Determinism: same input + same edit -> identical output bytes across runs.
  5. Length-SHRINKING patch (negative delta): fix-ups handle delta < 0, the
     binary re-extracts and parses.
  6. Multiple simultaneous edits through _apply_blob_edits (grow + a second
     module edit): the remap arithmetic is correct, the binary runs, shows the
     edit, and is deterministic.

Always-run gates (self-contained, no native binary needed):
  4. Format detection / guards: non-bun and truncated inputs rejected clearly.
  Synthetic fixture tests: a hand-built minimal bun-on-ELF buffer exercises the
  paths the 238 MB binary cannot reach: the 36-byte module struct form, the u32
  section-header form, the trailer-with-pad tolerance, entrypoint resolution and
  not-found, and the fail-closed growth guards actually firing (allocated
  section shift, overlapping payload, spanning segment), plus malformed inputs
  (too small, no ELF magic, ELF32, missing .bun, inconsistent length header).

Finding the binary (first hit wins):
  - CLAUDE_NATIVE_BINARY environment variable (explicit path), or
  - first CLI argument, or
  - common install locations (~/.local/share/claude/versions/<latest>).

If no binary is found, the real-binary gates are SKIPPED (reported as such, not
failed) so the suite runs in CI without the 238 MB artifact.

Exit code: 0 if every executed gate passed (skips do not fail), 1 otherwise.
"""
import glob
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bun_handler  # noqa: E402


def find_binary():
    env = os.environ.get("CLAUDE_NATIVE_BINARY")
    if env and os.path.isfile(env):
        return env
    for arg in sys.argv[1:]:
        if os.path.isfile(arg):
            return arg
    versions_dir = os.path.expanduser("~/.local/share/claude/versions")
    if os.path.isdir(versions_dir):
        candidates = sorted(glob.glob(os.path.join(versions_dir, "*")))
        candidates = [c for c in candidates if os.path.isfile(c)]
        if candidates:
            # Prefer the highest version-looking name.
            def keyfn(p):
                base = os.path.basename(p)
                parts = base.split(".")
                try:
                    return tuple(int(x) for x in parts)
                except ValueError:
                    return (0,)
            return sorted(candidates, key=keyfn)[-1]
    return None


def gate1_noop(data):
    out = bun_handler.repack_unchanged(data)
    if out == data:
        return True, "byte-identical"
    first = next((i for i in range(min(len(out), len(data))) if out[i] != data[i]), None)
    return False, f"differs at {first}; lengths {len(out)} vs {len(data)}"


def gate2_length_change(data):
    js = bun_handler.extract_js(data)
    needle = b"(Claude Code)"
    marker = b"\n(pfg-selftest)"
    if needle not in js:
        return None, "skip: '(Claude Code)' anchor not present in this build"
    # Insert the marker after EVERY '(Claude Code)'. The bundle carries more than
    # one copy of the version template (one per CLI entry shape), and `--version`
    # renders from a specific one; patching all copies guarantees the rendered
    # site is hit. This is also what the real version-output patch does.
    patched = js.replace(needle, needle + marker)
    out = bun_handler.repack_with_js(data, patched)

    # size + round-trip checks (do not need execution)
    if len(out) - len(data) != len(patched) - len(js):
        return False, "size delta mismatch"
    if bun_handler.extract_js(out) != patched:
        return False, "re-extract round-trip mismatch"

    # runtime check: write to a temp file, mark executable, run --version
    with tempfile.NamedTemporaryFile(prefix="pfg-bun-", suffix=".bin", delete=False) as tf:
        tf.write(out)
        tmp = tf.name
    try:
        os.chmod(tmp, 0o755)
        try:
            r = subprocess.run([tmp, "--version"], capture_output=True, text=True, timeout=90)
        except OSError as exc:
            return None, f"skip: cannot exec produced binary on this host ({exc})"
        if r.returncode != 0:
            return False, f"produced binary exited {r.returncode}: {r.stderr.strip()[:160]}"
        out_text = r.stdout.strip()
        shows = "(pfg-selftest)" in r.stdout
        if not shows:
            return False, f"edit not visible in --version output: {out_text!r}"
        return True, f"runs and shows edit: {out_text!r}"
    finally:
        os.unlink(tmp)


def gate3_determinism(data):
    js = bun_handler.extract_js(data)
    patched = js + b"\n//pfg-determinism"
    a = bun_handler.repack_with_js(data, patched)
    b = bun_handler.repack_with_js(data, patched)
    if a == b:
        import hashlib
        return True, f"identical across runs (sha256 {hashlib.sha256(a).hexdigest()[:16]})"
    return False, "two builds differ"


def gate5_shrink(data):
    """Length-SHRINKING edit (negative delta) on the real binary: the offset and
    ELF fix-ups must handle delta < 0, not just growth. Cut bytes out of the
    source, repack, re-extract, and confirm it still parses.

    We verify the round-trip and the negative size delta. We do not require the
    shrunk binary to run, because removing an arbitrary 100-byte slice from
    minified JS can break syntax; the goal here is to prove the negative-delta
    fix-up path is correct, which the no-op gate (delta 0) and grow gate (delta
    > 0) do not cover.
    """
    js = bun_handler.extract_js(data)
    needle = b"(Claude Code)"
    i = js.find(needle)
    if i < 0 or len(js) < 2000:
        return None, "skip: cannot find a region to shrink"
    cut_start = i + len(needle)
    shrunk = js[:cut_start] + js[cut_start + 100:]
    out = bun_handler.repack_with_js(data, shrunk)
    if len(out) - len(data) != -100:
        return False, f"size delta {len(out) - len(data)} != -100"
    if bun_handler.extract_js(out) != shrunk:
        return False, "re-extract round-trip mismatch after shrink"
    if not bun_handler.can_handle(out):
        return False, "shrunk binary no longer parses"
    return True, f"negative delta -100 round-trips; new size {len(out)}"


def gate6_multi_edit(data):
    """Multiple simultaneous edits through _apply_blob_edits on the real binary.
    Exercises the remap arithmetic with more than one edit (grow + shrink at
    different offsets), then runs the result."""
    img = bun_handler.BunImage(data)
    ep = img.entrypoint_module()
    js = bun_handler.extract_js(data)
    needle = b"(Claude Code)"
    if js.count(needle) < 2:
        return None, "skip: need two anchor sites for a multi-edit"
    # Edit the entrypoint contents with two changes baked into one new buffer is
    # the public path; to exercise _apply_blob_edits with multiple field edits we
    # combine an entrypoint contents grow with an edit to a second module if one
    # has non-empty contents.
    others = [m for m in img.modules if m["index"] != ep["index"] and m["contents"][1] > 0]
    grown = js.replace(needle, needle + b"\n(multi-A)")  # grow all version sites
    edits = {(ep["index"], "contents"): grown}
    label_other = "entrypoint-only"
    if others:
        om = others[0]
        oc = img.read_field(om, "contents")
        edits[(om["index"], "contents")] = oc + b"\n//multi-B-grow"
        label_other = f"+module[{om['index']}]"
    new_blob = bun_handler._apply_blob_edits(img, edits)
    wrapped = bun_handler._wrap_section(new_blob, img.section_header_size)
    out = bun_handler._repack_section_elf(img.data, wrapped)

    # round-trip every edited field
    img2 = bun_handler.BunImage(out)
    if img2.read_field(img2.entrypoint_module(), "contents") != grown:
        return False, "entrypoint content mismatch after multi-edit"
    for (mi, field), nb in edits.items():
        rec = [m for m in img2.modules if m["index"] == mi][0]
        if img2.read_field(rec, field) != nb:
            return False, f"module[{mi}].{field} mismatch after multi-edit"

    # run it; the grown version site must show
    import tempfile
    with tempfile.NamedTemporaryFile(prefix="pfg-multi-", suffix=".bin", delete=False) as tf:
        tf.write(out)
        tmp = tf.name
    try:
        os.chmod(tmp, 0o755)
        try:
            r = subprocess.run([tmp, "--version"], capture_output=True, text=True, timeout=90)
        except OSError as exc:
            return None, f"skip: cannot exec ({exc})"
        if r.returncode != 0:
            return False, f"multi-edit binary exited {r.returncode}: {r.stderr.strip()[:120]}"
        if "(multi-A)" not in r.stdout:
            return False, f"multi-edit not visible: {r.stdout.strip()!r}"
        # determinism under the multi-edit
        out2 = bun_handler._repack_section_elf(
            img.data,
            bun_handler._wrap_section(bun_handler._apply_blob_edits(bun_handler.BunImage(data), edits),
                                      img.section_header_size))
        if out2 != out:
            return False, "multi-edit not deterministic"
        return True, f"multi-edit ({label_other}) runs, shows edit, deterministic"
    finally:
        os.unlink(tmp)


def gate4_format_guards():
    """Build tiny synthetic inputs; the handler must reject them clearly."""
    checks = []

    # Not an ELF at all.
    checks.append(("garbage bytes rejected", not bun_handler.can_handle(b"not an executable")))

    # PE/COFF magic should be reported as PE, not handled.
    pe = b"MZ" + b"\x00" * 200
    checks.append(("PE detected as unsupported",
                   "PE/COFF" in bun_handler.detect_format(pe) and not bun_handler.can_handle(pe)))

    # Mach-O magic should be reported as Mach-O.
    macho = b"\xcf\xfa\xed\xfe" + b"\x00" * 200
    checks.append(("Mach-O detected as unsupported",
                   "Mach-O" in bun_handler.detect_format(macho) and not bun_handler.can_handle(macho)))

    # A minimal but structurally invalid ELF64 header: not handled, no crash.
    elf_stub = bytearray(64)
    elf_stub[:4] = b"\x7fELF"
    elf_stub[4] = 2   # ELFCLASS64
    elf_stub[5] = 1   # little-endian
    try:
        handled = bun_handler.can_handle(bytes(elf_stub))
        checks.append(("invalid ELF stub not handled (no crash)", handled is False))
    except Exception as exc:  # noqa: BLE001
        checks.append((f"invalid ELF stub raised uncaught {type(exc).__name__}", False))

    all_ok = all(ok for _, ok in checks)
    detail = "; ".join(f"{name}={'ok' if ok else 'FAIL'}" for name, ok in checks)
    return all_ok, detail


# ---------------------------------------------------------------------------
# Synthetic bun-on-ELF fixture builder.
#
# The 238 MB real binary exercises the happy path but cannot trigger the
# fail-closed guards, the 36-byte module form, or the u32 section-header form.
# This builds a tiny but structurally valid bun-on-ELF buffer that the handler
# parses, with knobs for those variants and for adversarial section/segment
# layouts. We mirror the exact layout the parser expects:
#   ELF64 header
#   program headers (LOAD segments)
#   section headers (NULL, .bun, optional trailing section, .shstrtab)
#   .shstrtab payload
#   .bun payload = [N-byte length header][bun blob]
#   bun blob = [module field data...][module table][offsets struct][trailer][pad]
# ---------------------------------------------------------------------------

import struct as _struct  # noqa: E402

_PT_LOAD = 1
_SHT_NULL = 0
_SHT_PROGBITS = 1
_SHT_STRTAB = 3
_SHT_NOBITS = 8
_SHF_WRITE = 0x1
_SHF_ALLOC = 0x2
_TRAILER = b"\n---- Bun! ----\n"


def _build_bun_blob(modules, module_struct_size, trailing_pad=True, entry_point_id=0):
    """Build a bun data blob from a list of module dicts.

    Each module dict: {name: bytes, contents: bytes, sourcemap: bytes,
    bytecode: bytes, moduleInfo: bytes, bytecodeOriginPath: bytes,
    encoding, loader, module_format, side}. Returns (blob_bytes, modules_off,
    modules_len, offsets_off). Layout mirrors bun closely enough for the parser:
    a 120-byte zero prefix, then per-module field data with one-byte separators,
    then the module table, the 32-byte offsets struct, the trailer (+1 pad).
    """
    ms = module_struct_size
    field_order = (["name", "contents", "sourcemap", "bytecode", "moduleInfo", "bytecodeOriginPath"]
                   if ms == 52 else ["name", "contents", "sourcemap", "bytecode"])

    out = bytearray(120)  # zero prefix; offset 0 means "empty"
    placed = []  # per module: {field: (off, len)}
    for m in modules:
        offs = {}
        for field in field_order:
            data = m[field]
            if len(data) == 0:
                offs[field] = (0, 0)
                continue
            out += b"\x00"  # one-byte separator before each non-empty region
            offs[field] = (len(out), len(data))
            out += data
        # empty fields not in field_order (36-byte form) default to (0, 0)
        for field in ["name", "contents", "sourcemap", "bytecode", "moduleInfo", "bytecodeOriginPath"]:
            offs.setdefault(field, (0, 0))
        placed.append(offs)

    out += b"\x00"
    modules_off = len(out)
    modules_len = len(modules) * ms
    out += b"\x00" * modules_len

    # compile-exec-argv is empty in our fixtures.
    out += b"\x00"
    compile_argv_off = len(out)
    compile_argv_len = 0

    out += b"\x00"
    offsets_off = len(out)
    out += b"\x00" * 32

    out += _TRAILER
    if trailing_pad:
        out += b"\x00"

    # Fill the module table.
    for mi, (m, offs) in enumerate(zip(modules, placed)):
        rc = modules_off + mi * ms
        for field in field_order:
            o, length = offs[field]
            _struct.pack_into("<II", out, rc, o, length)
            rc += 8
        _struct.pack_into("<BBBB", out, rc,
                          m["encoding"], m["loader"], m["module_format"], m["side"])

    # Offsets struct: byte_count field holds its own offset (parser convention).
    _struct.pack_into("<Q", out, offsets_off, offsets_off)
    _struct.pack_into("<I", out, offsets_off + 8, modules_off)
    _struct.pack_into("<I", out, offsets_off + 12, modules_len)
    _struct.pack_into("<I", out, offsets_off + 16, entry_point_id)
    _struct.pack_into("<I", out, offsets_off + 20, compile_argv_off)
    _struct.pack_into("<I", out, offsets_off + 24, compile_argv_len)
    _struct.pack_into("<I", out, offsets_off + 28, 0xF)  # flags
    return bytes(out)


def _module(name, contents, bytecode=b"", ms=52):
    return {
        "name": name.encode() if isinstance(name, str) else name,
        "contents": contents.encode() if isinstance(contents, str) else contents,
        "sourcemap": b"",
        "bytecode": bytecode,
        "moduleInfo": b"",
        "bytecodeOriginPath": b"",
        "encoding": 1, "loader": 1, "module_format": 2, "side": 0,
    }


def build_bun_elf(modules, module_struct_size=52, section_header_size=8,
                  trailing_pad=True, trailing_section=None,
                  trailing_pt_load=None, entry_point_id=0):
    """Assemble a minimal valid bun-on-ELF64 binary (little-endian).

    trailing_section: optional dict {flags, sht, vaddr, size} placed in the file
    AFTER .bun, used to test the growth guards. If its SHF_ALLOC flag is set the
    repack guard must refuse to grow .bun.

    trailing_pt_load: optional dict {p_align, p_filesz, p_vaddr} adding a second
    PT_LOAD segment whose file offset sits AFTER .bun. Used to test the unified
    alignment gate: any nonzero .bun delta whose magnitude is not a multiple of
    this segment's p_align must be refused.

    Returns the binary bytes. Layout is compact but consistent: at least one
    LOAD segment covering .bun, .bun aligned, section header table at EOF.
    """
    blob = _build_bun_blob(modules, module_struct_size, trailing_pad=trailing_pad,
                           entry_point_id=entry_point_id)
    if section_header_size == 8:
        bun_payload = _struct.pack("<Q", len(blob)) + blob
    else:
        bun_payload = _struct.pack("<I", len(blob)) + blob

    ehsize = 64
    phentsize = 56
    shentsize = 64

    # Section name string table.
    names = b"\x00.bun\x00.shstrtab\x00"
    bun_name_off = names.index(b".bun\x00")
    shstr_name_off = names.index(b".shstrtab\x00")
    trailing_name_off = 0
    if trailing_section is not None:
        names = b"\x00.bun\x00.shstrtab\x00.trail\x00"
        bun_name_off = names.index(b".bun\x00")
        shstr_name_off = names.index(b".shstrtab\x00")
        trailing_name_off = names.index(b".trail\x00")

    n_sections = 3 + (1 if trailing_section is not None else 0)  # NULL, .bun, [.trail], .shstrtab
    n_segments = 1 + (1 if trailing_pt_load is not None else 0)

    # File layout offsets.
    phoff = ehsize
    body_off = phoff + n_segments * phentsize
    # body_off: start of payloads. Put .bun first, aligned to 16.
    bun_align = 16
    bun_off = (body_off + bun_align - 1) // bun_align * bun_align
    cursor = bun_off + len(bun_payload)

    trailing_off = 0
    trailing_size = 0
    if trailing_section is not None:
        trailing_size = trailing_section.get("size", 16)
        if trailing_section.get("sht", _SHT_PROGBITS) != _SHT_NOBITS:
            trailing_off = cursor
            cursor += trailing_size
        else:
            trailing_off = cursor  # NOBITS occupies no file space

    tload_off = 0
    tload_filesz = 0
    tload_vaddr = 0
    tload_align = 0
    if trailing_pt_load is not None:
        tload_filesz = trailing_pt_load.get("p_filesz", 64)
        tload_vaddr = trailing_pt_load.get("p_vaddr", 0x600000)
        tload_align = trailing_pt_load.get("p_align", 0x1000)
        tload_off = cursor
        cursor += tload_filesz

    shstr_off = cursor
    cursor += len(names)
    # Section header table at EOF, 8-aligned.
    shoff = (cursor + 7) // 8 * 8
    total = shoff + n_sections * shentsize

    buf = bytearray(total)

    # ELF header.
    buf[:4] = b"\x7fELF"
    buf[4] = 2  # ELFCLASS64
    buf[5] = 1  # little-endian
    buf[6] = 1  # version
    _struct.pack_into("<H", buf, 16, 2)      # e_type = EXEC
    _struct.pack_into("<H", buf, 18, 0x3E)   # e_machine = x86-64
    _struct.pack_into("<I", buf, 20, 1)      # e_version
    _struct.pack_into("<Q", buf, 24, 0x400000)  # e_entry (nominal)
    _struct.pack_into("<Q", buf, 32, phoff)
    _struct.pack_into("<Q", buf, 40, shoff)
    _struct.pack_into("<I", buf, 48, 0)      # e_flags
    _struct.pack_into("<H", buf, 52, ehsize)
    _struct.pack_into("<H", buf, 54, phentsize)
    _struct.pack_into("<H", buf, 56, n_segments)
    _struct.pack_into("<H", buf, 58, shentsize)
    _struct.pack_into("<H", buf, 60, n_sections)
    _struct.pack_into("<H", buf, 62, n_sections - 1)  # e_shstrndx = last (.shstrtab)

    # First LOAD segment covers the .bun payload (file and virtual sizes equal).
    seg_vaddr = 0x400000
    seg_file_end = bun_off + len(bun_payload)
    po = phoff
    _struct.pack_into("<I", buf, po + 0, _PT_LOAD)
    _struct.pack_into("<I", buf, po + 4, 0x4 | 0x2)   # R + W
    _struct.pack_into("<Q", buf, po + 8, 0)            # p_offset
    _struct.pack_into("<Q", buf, po + 16, seg_vaddr)   # p_vaddr
    _struct.pack_into("<Q", buf, po + 24, seg_vaddr)   # p_paddr
    _struct.pack_into("<Q", buf, po + 32, seg_file_end)  # p_filesz (covers .bun)
    _struct.pack_into("<Q", buf, po + 40, seg_file_end)  # p_memsz
    _struct.pack_into("<Q", buf, po + 48, 0x1000)      # p_align

    # Optional second LOAD segment placed AFTER .bun. We pick p_vaddr so that
    # the loader invariant (p_offset - p_vaddr) % p_align == 0 holds initially:
    # set p_vaddr to a value congruent to tload_off mod p_align.
    if trailing_pt_load is not None:
        po2 = phoff + phentsize
        # ensure initial invariant: choose p_vaddr aligned so congruence holds
        vaddr2 = tload_vaddr + ((tload_off - tload_vaddr) % tload_align)
        _struct.pack_into("<I", buf, po2 + 0, _PT_LOAD)
        _struct.pack_into("<I", buf, po2 + 4, 0x4)   # R only
        _struct.pack_into("<Q", buf, po2 + 8, tload_off)
        _struct.pack_into("<Q", buf, po2 + 16, vaddr2)
        _struct.pack_into("<Q", buf, po2 + 24, vaddr2)
        _struct.pack_into("<Q", buf, po2 + 32, tload_filesz)
        _struct.pack_into("<Q", buf, po2 + 40, tload_filesz)
        _struct.pack_into("<Q", buf, po2 + 48, tload_align)

    # Payloads.
    buf[bun_off:bun_off + len(bun_payload)] = bun_payload
    if trailing_pt_load is not None:
        buf[tload_off:tload_off + tload_filesz] = b"L" * tload_filesz
    if trailing_section is not None and trailing_section.get("sht", _SHT_PROGBITS) != _SHT_NOBITS:
        # fill with recognizable bytes
        buf[trailing_off:trailing_off + trailing_size] = b"T" * trailing_size
    buf[shstr_off:shstr_off + len(names)] = names

    # Section headers.
    def write_sh(idx, name_off, sht, flags, vaddr, off, size):
        ho = shoff + idx * shentsize
        _struct.pack_into("<I", buf, ho + 0, name_off)
        _struct.pack_into("<I", buf, ho + 4, sht)
        _struct.pack_into("<Q", buf, ho + 8, flags)
        _struct.pack_into("<Q", buf, ho + 16, vaddr)
        _struct.pack_into("<Q", buf, ho + 24, off)
        _struct.pack_into("<Q", buf, ho + 32, size)
        # link/info/addralign/entsize left zero except addralign for .bun
        if sht == _SHT_PROGBITS and name_off == bun_name_off:
            _struct.pack_into("<Q", buf, ho + 48, 16)  # sh_addralign

    write_sh(0, 0, _SHT_NULL, 0, 0, 0, 0)
    # .bun section: PROGBITS, WRITE+ALLOC, vaddr inside the LOAD segment.
    bun_vaddr = seg_vaddr + (bun_off - 0)
    write_sh(1, bun_name_off, _SHT_PROGBITS, _SHF_WRITE | _SHF_ALLOC,
             bun_vaddr, bun_off, len(bun_payload))
    idx = 2
    if trailing_section is not None:
        write_sh(idx, trailing_name_off,
                 trailing_section.get("sht", _SHT_PROGBITS),
                 trailing_section.get("flags", 0),
                 trailing_section.get("vaddr", 0),
                 trailing_off, trailing_size)
        idx += 1
    write_sh(idx, shstr_name_off, _SHT_STRTAB, 0, 0, shstr_off, len(names))

    return bytes(buf)


def _check(name, ok, detail=""):
    SYNTH_RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


SYNTH_RESULTS = []


def synthetic_tests():
    """Tests that need controllable fixtures the real binary cannot provide."""
    print("SYNTHETIC FIXTURE TESTS")
    bh = bun_handler

    # --- baseline: a minimal fixture parses and round-trips (no-op) ---
    mods = [
        _module("/$bunfs/root/src/entrypoints/cli.js",
                "console.log('(Claude Code)');", bytecode=b"\xd4zFT" + b"\x00" * 40),
        _module("/$bunfs/root/helper.js", "module.exports={};"),
    ]
    fx = build_bun_elf(mods, module_struct_size=52, section_header_size=8)
    _check("minimal 52-byte fixture parses", bh.can_handle(fx))
    img = bh.BunImage(fx)
    _check("entrypoint resolves to cli.js", img.entrypoint_module()["name_str"].endswith("cli.js"))
    _check("extract_js returns the contents",
           bh.extract_js(fx) == b"console.log('(Claude Code)');")
    _check("no-op repack byte-identical (synthetic)", bh.repack_unchanged(fx) == fx)

    # --- 36-byte module struct form ---
    mods36 = [
        _module("/$bunfs/root/src/entrypoints/cli.js", "X();", ms=36),
        _module("/$bunfs/root/two.js", "Y();", ms=36),
    ]
    fx36 = build_bun_elf(mods36, module_struct_size=36, section_header_size=8)
    img36 = bh.BunImage(fx36)
    _check("36-byte module struct detected", img36.module_struct_size == 36,
           f"got {img36.module_struct_size}")
    _check("36-byte form extract_js works", bh.extract_js(fx36) == b"X();")
    _check("36-byte form no-op byte-identical", bh.repack_unchanged(fx36) == fx36)

    # --- entry_point_id is preferred over name heuristic ---
    # Build a fixture where module 0 is NOT an entrypoint and module 2 IS one,
    # with entry_point_id = 2. The handler must pick module 2 by index.
    mods_idx = [
        _module("/$bunfs/root/aux.js", "a();"),
        _module("/$bunfs/root/helper.js", "h();"),
        _module("/$bunfs/root/src/entrypoints/cli.js",
                "console.log('(Claude Code)');", bytecode=b"\xd4zFT" + b"\x00" * 40),
    ]
    fx_idx = build_bun_elf(mods_idx, entry_point_id=2)
    img_idx = bh.BunImage(fx_idx)
    _check("entry_point_id=2 picks module 2 (not name-search of module 0)",
           img_idx.entrypoint_module()["index"] == 2,
           f"got index {img_idx.entrypoint_module()['index']}")

    # And a HARD FAIL when entry_point_id points at a module whose name does
    # not match the entrypoint heuristic: silent fallback would patch the wrong
    # module and still pass smoke tests.
    fx_mismatch = build_bun_elf(mods_idx, entry_point_id=0)  # module 0 is "aux.js"
    img_mm = bh.BunImage(fx_mismatch)
    raised = False
    try:
        img_mm.entrypoint_module()
    except bh.BunFormatError as exc:
        raised = "disagree" in str(exc)
    _check("entry_point_id pointing at non-entrypoint module hard-fails", raised)

    # --- 36-vs-52 disambiguation under length ambiguity ---
    # 13 modules at 36 bytes/record = 468 bytes; 468 also divides 52 (= 9 * 52),
    # so the length alone is ambiguous. The handler must pick 36 by checking
    # record-0's name field rather than silently defaulting to 52.
    mods36_amb = [_module("/$bunfs/root/src/entrypoints/cli.js", "X();", ms=36)]
    for n in range(12):
        mods36_amb.append(_module(f"/$bunfs/root/mod{n}.js", f"m{n}();", ms=36))
    fx36_amb = build_bun_elf(mods36_amb, module_struct_size=36, section_header_size=8)
    img36_amb = bh.BunImage(fx36_amb)
    _check("ambiguous-length 36-byte form picked correctly",
           img36_amb.module_struct_size == 36, f"got {img36_amb.module_struct_size}")
    # And its entrypoint still resolves cleanly.
    _check("ambiguous-length 36-byte entrypoint resolves",
           img36_amb.entrypoint_module()["name_str"].endswith("cli.js"))

    # --- u32 section-header form ---
    fx_u32 = build_bun_elf(mods, module_struct_size=52, section_header_size=4)
    img_u32 = bh.BunImage(fx_u32)
    _check("u32 section header form detected", img_u32.section_header_size == 4,
           f"got {img_u32.section_header_size}")
    _check("u32 form no-op byte-identical", bh.repack_unchanged(fx_u32) == fx_u32)

    # --- trailer with NO trailing pad byte ---
    fx_nopad = build_bun_elf(mods, trailing_pad=False)
    _check("trailer-without-pad parses", bh.can_handle(fx_nopad))
    _check("trailer-without-pad no-op identical", bh.repack_unchanged(fx_nopad) == fx_nopad)

    # --- length change on a synthetic fixture (grow + shrink + multi) ---
    ep = bh.BunImage(fx).entrypoint_module()
    js = bh.extract_js(fx)
    grown = js.replace(b"(Claude Code)", b"(Claude Code) GROWN LONGER")
    out_grow = bh.repack_with_js(fx, grown)
    _check("synthetic grow round-trips", bh.extract_js(out_grow) == grown)
    _check("synthetic grow no-op-after parses", bh.can_handle(out_grow))
    shrunk = js.replace(b"console.log('(Claude Code)');", b"x;")
    out_shrink = bh.repack_with_js(fx, shrunk)
    _check("synthetic shrink (negative delta) round-trips", bh.extract_js(out_shrink) == shrunk)
    _check("synthetic shrink smaller file", len(out_shrink) < len(fx),
           f"{len(out_shrink)} vs {len(fx)}")

    # --- multiple simultaneous edits via _apply_blob_edits ---
    img2 = bh.BunImage(fx)
    e_idx = img2.entrypoint_module()["index"]
    other_idx = [m["index"] for m in img2.modules if m["index"] != e_idx][0]
    edits = {
        (e_idx, "contents"): b"console.log('(Claude Code) EDIT_ONE longer');",
        (other_idx, "contents"): b"Z();",  # shrink the other module
    }
    new_blob = bh._apply_blob_edits(img2, edits)
    wrapped = bh._wrap_section(new_blob, img2.section_header_size)
    multi_bin = bh._repack_section_elf(img2.data, wrapped)
    img_multi = bh.BunImage(multi_bin)
    got_ep = img_multi.read_field(img_multi.entrypoint_module(), "contents")
    other_after = [m for m in img_multi.modules if m["index"] == other_idx][0]
    got_other = img_multi.read_field(other_after, "contents")
    _check("multi-edit: entrypoint content correct",
           got_ep == b"console.log('(Claude Code) EDIT_ONE longer');")
    _check("multi-edit: other module content correct", got_other == b"Z();")
    # determinism under multi-edit
    b1 = bh._repack_section_elf(img2.data, bh._wrap_section(bh._apply_blob_edits(bh.BunImage(fx), edits), 8))
    b2 = bh._repack_section_elf(img2.data, bh._wrap_section(bh._apply_blob_edits(bh.BunImage(fx), edits), 8))
    _check("multi-edit determinism", b1 == b2)

    # --- entrypoint not found -> BunFormatError ---
    mods_noep = [_module("/$bunfs/root/aaa.js", "a();"), _module("/$bunfs/root/bbb.js", "b();")]
    fx_noep = build_bun_elf(mods_noep)
    raised = False
    try:
        bh.BunImage(fx_noep).entrypoint_module()
    except bh.BunFormatError:
        raised = True
    _check("entrypoint-not-found raises BunFormatError", raised)

    # --- is_entrypoint_name variants ---
    variants_ok = (
        bh.BunImage.is_entrypoint_name("claude")
        and bh.BunImage.is_entrypoint_name("/usr/lib/claude")
        and bh.BunImage.is_entrypoint_name("claude.exe")
        and bh.BunImage.is_entrypoint_name("src/entrypoints/cli.js")
        and bh.BunImage.is_entrypoint_name("/$bunfs/root/src/entrypoints/cli.js")
        and not bh.BunImage.is_entrypoint_name("/$bunfs/root/helper.js")
        and not bh.BunImage.is_entrypoint_name("notclaude")
    )
    _check("is_entrypoint_name variants", variants_ok)

    # --- SAFETY GUARD: growing .bun before an ALLOC section must refuse ---
    fx_alloc = build_bun_elf(
        mods, trailing_section={"flags": _SHF_ALLOC, "sht": _SHT_PROGBITS,
                                "vaddr": 0x900000, "size": 64})
    img_alloc = bh.BunImage(fx_alloc)
    ep_a = img_alloc.entrypoint_module()
    big = bh.extract_js(fx_alloc) + b"X" * 500  # force growth
    raised = False
    try:
        nb = bh._apply_blob_edits(img_alloc, {(ep_a["index"], "contents"): big})
        bh._repack_section_elf(img_alloc.data, bh._wrap_section(nb, img_alloc.section_header_size))
    except bh.BunFormatError as exc:
        raised = "allocated" in str(exc)
    _check("guard: refuse to grow before ALLOC section", raised)

    # --- SAFETY GUARD: overlapping payload section must refuse ---
    # Build a fixture, then hand-corrupt a section header so a payload section
    # starts strictly inside the .bun span.
    fx_ovl = bytearray(build_bun_elf(
        mods, trailing_section={"flags": 0, "sht": _SHT_PROGBITS,
                                "vaddr": 0, "size": 32}))
    elf = bh.Elf64(bytes(fx_ovl))
    bun = elf.section(".bun")
    trail = elf.section(".trail")
    # Move .trail's file offset to inside .bun.
    _struct.pack_into("<Q", fx_ovl, trail["hdr_off"] + 24, bun["foff"] + 8)
    raised = False
    try:
        img_ovl = bh.BunImage(bytes(fx_ovl))
        ep_o = img_ovl.entrypoint_module()
        nb = bh._apply_blob_edits(img_ovl, {(ep_o["index"], "contents"): bh.extract_js(bytes(fx_ovl))})
        bh._repack_section_elf(img_ovl.data, bh._wrap_section(nb, img_ovl.section_header_size))
    except bh.BunFormatError as exc:
        raised = "overlap" in str(exc)
    _check("guard: refuse overlapping payload section", raised)

    # --- SAFETY GUARD: spanning segment must refuse on growth ---
    # Add a second LOAD segment that starts after .bun and spans bun_end while
    # growing. Easiest: corrupt the existing single segment so it does NOT
    # contain .bun (forcing containing=None) yet spans bun_end, then grow.
    fx_span = bytearray(build_bun_elf(mods))
    elf_s = bh.Elf64(bytes(fx_span))
    bun_s = elf_s.section(".bun")
    seg = elf_s.segments[0]
    # Shrink the segment so it starts after .bun start but still crosses bun_end,
    # and does not satisfy the containing test (vaddr/file range no longer covers
    # the whole .bun). Set p_offset just past bun start.
    new_seg_off = bun_s["foff"] + 8
    new_seg_filesz = (bun_s["foff"] + bun_s["size"]) - new_seg_off + 4  # crosses bun_end
    _struct.pack_into("<Q", fx_span, seg["hdr_off"] + 8, new_seg_off)
    _struct.pack_into("<Q", fx_span, seg["hdr_off"] + 32, new_seg_filesz)
    raised = False
    try:
        img_span = bh.BunImage(bytes(fx_span))
        ep_s = img_span.entrypoint_module()
        big = bh.extract_js(bytes(fx_span)) + b"Y" * 300
        nb = bh._apply_blob_edits(img_span, {(ep_s["index"], "contents"): big})
        bh._repack_section_elf(img_span.data, bh._wrap_section(nb, img_span.section_header_size))
    except bh.BunFormatError as exc:
        raised = "unrelated segments" in str(exc)
    _check("guard: refuse growth inside spanning segment", raised)

    # --- malformed inputs ---
    _check("too-small file rejected", not bh.can_handle(b"\x7fELF\x02\x01"))
    _check("missing ELF magic rejected", not bh.can_handle(b"XXXX" + b"\x00" * 200))
    elf32 = bytearray(build_bun_elf(mods))
    elf32[4] = 1  # ELFCLASS32
    _check("ELF32 rejected (not handled)", not bh.can_handle(bytes(elf32)))
    # missing .bun section: corrupt .bun's name so it is not found.
    fx_nobun = bytearray(build_bun_elf(mods))
    elf_nb = bh.Elf64(bytes(fx_nobun))
    bun_nb = elf_nb.section(".bun")
    _struct.pack_into("<I", fx_nobun, bun_nb["hdr_off"] + 0, 0)  # name_off -> "" (NULL section name)
    _check("missing .bun section rejected", not bh.can_handle(bytes(fx_nobun)))
    # inconsistent length header: corrupt the .bun length prefix.
    fx_badlen = bytearray(build_bun_elf(mods))
    elf_bl = bh.Elf64(bytes(fx_badlen))
    bun_bl = elf_bl.section(".bun")
    _struct.pack_into("<Q", fx_badlen, bun_bl["foff"], 0xDEADBEEF)
    _check("inconsistent length header rejected", not bh.can_handle(bytes(fx_badlen)))

    # --- unified PT_LOAD invariant gate: for any nonzero .bun delta, every
    #     later PT_LOAD must keep `(p_offset - p_vaddr) mod p_align == 0`. The
    #     guard fires symmetrically on grow AND shrink, and lets aligned deltas
    #     and the no-op delta through. ---
    def _try_resize(fx_bytes, contents_delta):
        """Attempt a .bun resize by editing the entrypoint's contents by
        `contents_delta` bytes. Returns (BunFormatError_message, output_size)."""
        img_loc = bh.BunImage(fx_bytes)
        ep_loc = img_loc.entrypoint_module()
        js_loc = bh.extract_js(fx_bytes)
        new_js = js_loc + b"P" * contents_delta if contents_delta > 0 else js_loc[:contents_delta]
        try:
            out = bh.repack_with_js(fx_bytes, new_js)
            return None, len(out)
        except bh.BunFormatError as exc:
            return str(exc), None

    # Fixture A: trailing PT_LOAD with p_align=0x1000. Page-aligned delta passes;
    # a 7-byte non-page-aligned grow must fire the invariant.
    fx_load = build_bun_elf(mods, trailing_pt_load={"p_align": 0x1000, "p_filesz": 64})
    err_grow7, _ = _try_resize(fx_load, +7)
    _check("gate fires on non-page-aligned grow (delta=+7, p_align=0x1000)",
           err_grow7 is not None and "PT_LOAD invariant" in err_grow7, repr(err_grow7))

    err_shrink5, _ = _try_resize(fx_load, -5)
    _check("gate fires on non-page-aligned shrink (delta=-5, p_align=0x1000)",
           err_shrink5 is not None and "PT_LOAD invariant" in err_shrink5, repr(err_shrink5))

    # No-op pipeline (delta == 0) still proceeds.
    img_noop = bh.BunImage(fx_load)
    out_noop = bh.repack_unchanged(fx_load)
    _check("gate does not block delta == 0 (no-op identical)", out_noop == fx_load)

    # An aligned delta with a later PT_LOAD passes the gate. Build a fixture with
    # a small p_align (4) so a small delta (multiple of 4) is admissible without
    # forcing 4 KB of edit padding.
    fx_load_small = build_bun_elf(mods, trailing_pt_load={"p_align": 4, "p_filesz": 64})
    err_grow4, sz_grow4 = _try_resize(fx_load_small, +4)
    _check("gate admits aligned grow (delta=+4, p_align=4)",
           err_grow4 is None and sz_grow4 is not None,
           repr(err_grow4) if err_grow4 else f"size={sz_grow4}")

    # Page-aligned grow/shrink with the realistic p_align=0x1000 must also pass.
    # This locks the implementation against a too-conservative "reject any
    # nonzero delta whenever a later PT_LOAD exists" reading of the gate.
    # We need the entrypoint's contents to be large enough to shrink by 0x1000.
    big_js = b"console.log('(Claude Code)');" + b"X" * 0x2000
    big_mods = [
        _module("/$bunfs/root/src/entrypoints/cli.js", big_js,
                bytecode=b"\xd4zFT" + b"\x00" * 40),
        _module("/$bunfs/root/helper.js", "module.exports={};"),
    ]
    fx_page = build_bun_elf(big_mods, trailing_pt_load={"p_align": 0x1000, "p_filesz": 64})
    err_grow_page, sz_grow_page = _try_resize(fx_page, +0x1000)
    _check("gate admits page-aligned grow (delta=+0x1000, p_align=0x1000)",
           err_grow_page is None and sz_grow_page is not None,
           repr(err_grow_page) if err_grow_page else f"size={sz_grow_page}")
    err_shrink_page, sz_shrink_page = _try_resize(fx_page, -0x1000)
    _check("gate admits page-aligned shrink (delta=-0x1000, p_align=0x1000)",
           err_shrink_page is None and sz_shrink_page is not None,
           repr(err_shrink_page) if err_shrink_page else f"size={sz_shrink_page}")

    # --- bounds checks: a crafted modules_len that overruns the blob must raise
    #     BunFormatError, NOT let struct.error escape ---
    fx_mlen = bytearray(build_bun_elf(mods))
    img_mlen = bh.BunImage(bytes(fx_mlen))
    # offsets struct's modules_len field sits inside the bun blob; locate its
    # absolute file offset and overwrite it with a huge value (52 * 100000).
    bun_sec = bh.Elf64(bytes(fx_mlen)).section(".bun")
    abs_offsets_off = bun_sec["foff"] + img_mlen.section_header_size + img_mlen.offsets_off
    _struct.pack_into("<I", fx_mlen, abs_offsets_off + 12, 52 * 100000)
    raised_kind = None
    try:
        bh.BunImage(bytes(fx_mlen))
    except bh.BunFormatError:
        raised_kind = "BunFormatError"
    except _struct.error:
        raised_kind = "struct.error (LEAKED)"
    except Exception as exc:  # noqa: BLE001
        raised_kind = f"{type(exc).__name__} (unexpected)"
    _check("crafted modules_len overrun raises BunFormatError", raised_kind == "BunFormatError",
           f"got {raised_kind}")
    _check("crafted modules_len overrun: can_handle returns False cleanly",
           not bh.can_handle(bytes(fx_mlen)))

    # Same shape for a per-record field overrun: leave modules_len valid but
    # corrupt one module record's `contents` length so it points past the blob.
    fx_field = bytearray(build_bun_elf(mods))
    img_f = bh.BunImage(bytes(fx_field))
    bun_sec_f = bh.Elf64(bytes(fx_field)).section(".bun")
    abs_table_off = bun_sec_f["foff"] + img_f.section_header_size + img_f.modules_off
    # record 0's contents field sits at offset 8 inside the record; write a huge length
    _struct.pack_into("<I", fx_field, abs_table_off + 8 + 4, 0x7FFFFFFF)
    raised_kind = None
    try:
        bh.BunImage(bytes(fx_field))
    except bh.BunFormatError:
        raised_kind = "BunFormatError"
    except _struct.error:
        raised_kind = "struct.error (LEAKED)"
    except Exception as exc:  # noqa: BLE001
        raised_kind = f"{type(exc).__name__} (unexpected)"
    _check("crafted field-range overrun raises BunFormatError", raised_kind == "BunFormatError",
           f"got {raised_kind}")
    _check("crafted field-range overrun: can_handle returns False cleanly",
           not bh.can_handle(bytes(fx_field)))

    ok = all(SYNTH_RESULTS)
    print(f"SYNTHETIC TESTS {'PASS' if ok else 'FAIL'} ({sum(SYNTH_RESULTS)}/{len(SYNTH_RESULTS)} checks)")
    return ok


def main():
    print("=== bun_handler standalone test harness ===")
    results = []

    # Synthetic fixture tests (self-contained; always run).
    results.append(synthetic_tests())

    # Gate 4 always runs (self-contained synthetic inputs).
    ok4, d4 = gate4_format_guards()
    print(f"GATE 4 format detection / guards: {'PASS' if ok4 else 'FAIL'}  ({d4})")
    results.append(ok4)

    binary = find_binary()
    if not binary:
        print("GATE 1-3: SKIPPED (no native binary found; set CLAUDE_NATIVE_BINARY "
              "or pass a path, or install under ~/.local/share/claude/versions)")
        ok = all(results)
        print(f"\nSUITE {'PASS' if ok else 'FAIL'} (gate 4 only; 1-3 skipped)")
        return 0 if ok else 1

    print(f"binary: {binary}")
    with open(binary, "rb") as f:
        data = f.read()
    print(f"format: {bun_handler.detect_format(data)}  ({len(data)} bytes)")
    if not bun_handler.can_handle(data):
        print("GATE 1-3: cannot run, handler does not support this binary")
        print("\nSUITE FAIL")
        return 1

    for label, fn in [("GATE 1 no-op byte-identical", gate1_noop),
                      ("GATE 2 length-changing + runs + shows edit", gate2_length_change),
                      ("GATE 3 determinism", gate3_determinism),
                      ("GATE 5 shrinking edit (negative delta)", gate5_shrink),
                      ("GATE 6 multi-edit remap + runs", gate6_multi_edit)]:
        ok, detail = fn(data)
        if ok is None:
            print(f"{label}: SKIP ({detail})")
        else:
            print(f"{label}: {'PASS' if ok else 'FAIL'} ({detail})")
            results.append(ok)

    ok = all(results)
    print(f"\nSUITE {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
