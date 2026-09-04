#!/usr/bin/env python3
"""
Standalone adversarial test harness for util/bun_handler.py.

The real-binary gates exercise the supported linux-x64 Claude artifact through
public APIs (`extract_js`, `repack_with_js`, `repack_unchanged`, `can_handle`,
`detect_format`). Synthetic fixtures additionally call lower-level helpers so
malformed ownership, optional-tail pointers, alignment, and ELF geometry can be
constructed directly.

Real-binary gates (skipped cleanly when no native binary is available):
  1. Byte-exact no-op identity.
  2. Length-growing source patch runs and is visible.
  3. Deterministic output for identical input and edit.
  5. Length-shrinking source patch round-trips and remains patchable.
  6. Multiple source modules edited in one public-API repack.
  7. Equal-length control-flow edit changes runtime output.
  8. Equal-length string-literal edit changes runtime output.
  9. A newly inserted dependency (ESM static import or CJS require) is parsed
     and rejected instead of being hidden by stale compiled state.

Gates 7-9 are the stale-compiled-state regression proof: changed modules must
have their source hash, bytecode, module-info, and sourcemap pointers detached.
Gate 4 and the synthetic suite always run. They cover 36/52-byte records,
4/8-byte section headers, current flag-ordered optional records, grow/shrink and
multi-edit remapping, ownership/terminator/discriminant checks, bytecode and
UTF-16 alignment, x86-64 scope, and fail-closed ELF mapping geometry.

Binary discovery, first hit wins:
  - CLAUDE_NATIVE_BINARY environment variable,
  - first file-valued CLI argument,
  - highest version under ~/.local/share/claude/versions.

Exit code is 0 only when every executed check passes. A skipped real gate is
permissive by default and can be promoted to failure with
CLAUDE_PFG_STRICT_GATES=gateN[,gateN...].
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


def _run_binary(binary_bytes, *args, prefix="pfg-bun-"):
    """Execute a produced binary and return (CompletedProcess, skip_detail)."""
    with tempfile.NamedTemporaryFile(prefix=prefix, suffix=".bin", delete=False) as tf:
        tf.write(binary_bytes)
        tmp = tf.name
    try:
        os.chmod(tmp, 0o755)
        try:
            return subprocess.run(
                [tmp, *args], capture_output=True, text=True, timeout=90
            ), None
        except OSError as exc:
            return None, f"cannot exec produced binary on this host ({exc})"
        except subprocess.TimeoutExpired:
            return None, "produced binary timed out"
    finally:
        os.unlink(tmp)


def _rebuild_extracted(records):
    """Inverse of split_extracted_js for test-generated module replacements."""
    if len(records) == 1 and records[0][0] is None:
        return records[0][2]
    out = bytearray(bun_handler._MULTI_MODULE_HEADER)
    for index, name, contents in records:
        if index is None or name is None:
            raise AssertionError("mixed single/multi-module extraction records")
        out += bun_handler._MULTI_MODULE_MARKER
        out += str(index).encode("ascii")
        out += b" "
        out += name.hex().encode("ascii")
        out += b"\n"
        out += contents
    return bytes(out)


def _replace_extracted_modules(extracted, replacements):
    records = bun_handler.split_extracted_js(extracted)
    rebuilt = []
    seen = set()
    for index, name, contents in records:
        key = index
        if key in replacements:
            contents = bytes(replacements[key])
            seen.add(key)
        rebuilt.append((index, name, contents))
    missing = set(replacements) - seen
    if missing:
        raise AssertionError(f"replacement module ids not present: {sorted(missing)!r}")
    return _rebuild_extracted(rebuilt)


def _changed_module_indices(data, patched_js):
    img = bun_handler.BunImage(data)
    original = img.extract_js()
    changed = bun_handler.changed_js_modules(original, patched_js)
    modules = img.source_modules()
    if len(modules) == 1:
        return [modules[0]["index"]] if changed else []
    return [index for index, _name, _contents in changed]


def _check_changed_modules_invalidated(data, out, patched_js):
    """Verify that every changed module is detached from stale compiled views."""
    before = bun_handler.BunImage(data)
    after = bun_handler.BunImage(out)
    changed = _changed_module_indices(data, patched_js)
    if not changed:
        return False, "patch changed no source module"

    exercised = []
    for index in changed:
        old = before.modules[index]
        new = after.modules[index]
        old_state = {
            "hash": before.source_hash(index),
            "bytecode": old["bytecode"][1],
            "moduleInfo": old["moduleInfo"][1],
            "sourcemap": old["sourcemap"][1],
        }
        if any(old_state.values()):
            exercised.append(index)
        if after.source_hash(index) != 0:
            return False, f"module[{index}] source hash was not cleared"
        for field in ("bytecode", "moduleInfo", "sourcemap"):
            if new[field] != (0, 0):
                return False, f"module[{index}] stale {field} pointer remains {new[field]}"
    if not exercised:
        return False, "changed modules carried no compiled state, so invalidation was not exercised"
    return True, f"invalidated modules {changed} (compiled state existed on {exercised})"


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
    patched = js.replace(needle, needle + marker)
    out = bun_handler.repack_with_js(data, patched)

    source_delta = len(patched) - len(js)
    file_delta = len(out) - len(data)
    if source_delta <= 0 or file_delta < source_delta:
        return False, f"unexpected source/file delta {source_delta}/{file_delta}"
    if bun_handler.extract_js(out) != patched:
        return False, "re-extract round-trip mismatch"
    invalidated, detail = _check_changed_modules_invalidated(data, out, patched)
    if not invalidated:
        return False, detail

    result, skip = _run_binary(out, "--version", prefix="pfg-grow-")
    if result is None:
        return None, f"skip: {skip}"
    if result.returncode != 0:
        return False, f"produced binary exited {result.returncode}: {result.stderr.strip()[:160]}"
    if "(pfg-selftest)" not in result.stdout:
        return False, f"edit not visible in --version output: {result.stdout.strip()!r}"
    alignment_padding = file_delta - source_delta
    return True, (f"runs and shows edit; source delta +{source_delta}, file delta "
                  f"+{file_delta} ({alignment_padding} alignment byte(s)); {detail}")


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
    """Exercise a negative source delta without pretending arbitrary cut JS runs."""
    js = bun_handler.extract_js(data)
    needle = b"(Claude Code)"
    i = js.find(needle)
    if i < 0 or len(js) < 2000:
        return None, "skip: cannot find a region to shrink"
    cut_start = i + len(needle)
    shrunk = js[:cut_start] + js[cut_start + 100:]
    out = bun_handler.repack_with_js(data, shrunk)
    if len(out) >= len(data):
        return False, f"negative source delta did not shrink file ({len(out) - len(data):+d})"
    if bun_handler.extract_js(out) != shrunk:
        return False, "re-extract round-trip mismatch after shrink"
    if not bun_handler.can_handle(out):
        return False, "shrunk binary no longer parses as patchable"
    invalidated, detail = _check_changed_modules_invalidated(data, out, shrunk)
    if not invalidated:
        return False, detail
    return True, (f"source delta -100 round-trips; file delta "
                  f"{len(out) - len(data):+d}; {detail}")


def gate6_multi_edit(data):
    """Edit multiple source modules through the public extraction/repack API."""
    js = bun_handler.extract_js(data)
    records = bun_handler.split_extracted_js(js)
    if len(records) < 2:
        return None, "skip: binary exposes only one patchable source module"

    needle = b"(Claude Code)"
    replacements = {}
    for index, _name, contents in records:
        if needle in contents:
            replacements[index] = contents.replace(needle, needle + b"\n(multi-A)")
    if not replacements:
        return None, "skip: '(Claude Code)' anchor not present in any source module"

    extra = next((record for record in records if record[0] not in replacements), None)
    if extra is None:
        return None, "skip: no independent module available for the second edit"
    replacements[extra[0]] = extra[2] + b"\n/*pfg-multi-B*/"
    patched = _replace_extracted_modules(js, replacements)

    out = bun_handler.repack_with_js(data, patched)
    if bun_handler.extract_js(out) != patched:
        return False, "multi-module re-extract round-trip mismatch"
    invalidated, detail = _check_changed_modules_invalidated(data, out, patched)
    if not invalidated:
        return False, detail

    result, skip = _run_binary(out, "--version", prefix="pfg-multi-")
    if result is None:
        return None, f"skip: {skip}"
    if result.returncode != 0:
        return False, f"multi-edit binary exited {result.returncode}: {result.stderr.strip()[:160]}"
    if "(multi-A)" not in result.stdout:
        return False, f"multi-edit not visible: {result.stdout.strip()!r}"
    out2 = bun_handler.repack_with_js(data, patched)
    if out2 != out:
        return False, "multi-edit not deterministic"
    return True, f"edited {len(replacements)} modules, runs, deterministic; {detail}"


def gate7_equal_length_control_flow(data):
    """Equal-length control-flow edit: stale bytecode must not win."""
    js = bun_handler.extract_js(data)
    anchor = b'}.BUILD_REF_NAME){return""}'
    replacement = b'}.BUILD_REF_NAME){return 0}'
    if len(anchor) != len(replacement):
        raise AssertionError("equal-length control-flow fixture drifted")
    if js.count(anchor) != 1:
        return None, f"skip: control-flow anchor count is {js.count(anchor)}, expected 1"

    patched = js.replace(anchor, replacement)
    out = bun_handler.repack_with_js(data, patched)
    if len(out) != len(data):
        return False, f"equal-length source edit changed file size by {len(out) - len(data)}"
    if bun_handler.extract_js(out) != patched:
        return False, "equal-length control-flow edit did not round-trip"
    invalidated, detail = _check_changed_modules_invalidated(data, out, patched)
    if not invalidated:
        return False, detail

    baseline, baseline_skip = _run_binary(data, "--version", prefix="pfg-cf-base-")
    result, skip = _run_binary(out, "--version", prefix="pfg-cf-equal-")
    if baseline is None or result is None:
        return None, f"skip: {baseline_skip or skip}"
    if baseline.returncode != 0:
        return False, f"baseline binary exited {baseline.returncode}"
    if result.returncode != 0:
        return False, f"patched binary exited {result.returncode}: {result.stderr.strip()[:160]}"
    expected = baseline.stdout.rstrip("\n") + "0\n"
    if result.stdout != expected:
        return False, (f"equal-length branch edit produced {result.stdout!r}; "
                       f"expected {expected!r}")
    return True, f"same-size return expression changes output; {detail}"


def gate8_equal_length_literal(data):
    """Equal-length literal edit: stale source hash/bytecode must not win."""
    js = bun_handler.extract_js(data)
    before = b"(Claude Code)"
    after = b"(ClauDe Code)"
    if before not in js:
        return None, "skip: version literal anchor not present"
    if len(before) != len(after):
        raise AssertionError("equal-length literal fixture drifted")
    patched = js.replace(before, after)
    out = bun_handler.repack_with_js(data, patched)
    if len(out) != len(data):
        return False, f"equal-length literal edit changed file size by {len(out) - len(data)}"
    if bun_handler.extract_js(out) != patched:
        return False, "equal-length literal edit did not round-trip"
    invalidated, detail = _check_changed_modules_invalidated(data, out, patched)
    if not invalidated:
        return False, detail

    result, skip = _run_binary(out, "--version", prefix="pfg-literal-")
    if result is None:
        return None, f"skip: {skip}"
    if result.returncode != 0:
        return False, f"patched binary exited {result.returncode}: {result.stderr.strip()[:160]}"
    if "(ClauDe Code)" not in result.stdout or "(Claude Code)" in result.stdout:
        return False, f"equal-length literal remained stale: {result.stdout.strip()!r}"
    return True, f"same-size literal is visible at runtime; {detail}"


def gate9_dependency_reanalysis(data):
    """Insert a missing dependency through the target module's real syntax.

    Split ESM builds receive a static import, which specifically proves stale
    moduleInfo was detached. Older CJS builds wrap the program in a function
    whose `require` parameter is only in scope inside the wrapper, so inject a
    require call immediately after that opening brace. In both forms, stale
    bytecode would silently ignore the new dependency.
    """
    js = bun_handler.extract_js(data)
    records = bun_handler.split_extracted_js(js)
    anchor = b'}.BUILD_REF_NAME){return""}'
    targets = [record for record in records if anchor in record[2]]
    if len(targets) != 1:
        return None, f"skip: import target anchor matched {len(targets)} modules"
    target_index, _name, contents = targets[0]
    image = bun_handler.BunImage(data)
    if target_index is None:
        target_module = image.source_modules()[0]
    else:
        target_module = image.modules[target_index]

    missing = b"/$bunfs/root/pfg-definitely-missing.js"
    if target_module["module_format"] == 1:
        dependency_stmt = b'import"' + missing + b'";'
        patched_contents = dependency_stmt + contents
        dependency_kind = "static import"
    elif target_module["module_format"] == 2:
        cjs_wrapper = b"(function(exports, require, module, __filename, __dirname) {"
        if contents.count(cjs_wrapper) != 1:
            return None, ("skip: CJS wrapper prologue count is "
                          f"{contents.count(cjs_wrapper)}, expected 1")
        dependency_stmt = b'require("' + missing + b'");'
        patched_contents = contents.replace(
            cjs_wrapper, cjs_wrapper + dependency_stmt, 1)
        dependency_kind = "CJS require"
    else:
        return None, ("skip: target module has unsupported ModuleFormat "
                      f"{target_module['module_format']}")

    patched = _replace_extracted_modules(
        js, {target_index: patched_contents})
    out = bun_handler.repack_with_js(data, patched)
    if bun_handler.extract_js(out) != patched:
        return False, f"{dependency_kind} edit did not round-trip"
    invalidated, detail = _check_changed_modules_invalidated(data, out, patched)
    if not invalidated:
        return False, detail

    result, skip = _run_binary(out, "--version", prefix="pfg-import-")
    if result is None:
        return None, f"skip: {skip}"
    combined = result.stdout + result.stderr
    missing_text = missing.decode("ascii")
    if result.returncode == 0:
        return False, (f"inserted missing {dependency_kind} was ignored "
                       "(stale compiled state likely remained)")
    if missing_text not in combined or "Cannot find module" not in combined:
        return False, (f"patched binary failed, but not on inserted "
                       f"{dependency_kind}: "
                       f"{combined.strip()[:240]!r}")
    return True, f"inserted {dependency_kind} is parsed and rejected; {detail}"



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
# The real binary exercises the happy path but cannot trigger every
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


def _build_bun_blob(modules, module_struct_size, runtime_blob_vaddr,
                    trailing_pad=True, entry_point_id=0, flags=0xF,
                    builtin_bytecode=None, bytecode_string_table=b"",
                    startup_module_count=None, module_info_string_table=b"",
                    compile_argv=b""):
    """Build a structurally faithful standalone-module-graph blob.

    `runtime_blob_vaddr` is the mapped address of blob byte zero (after the
    section's 4/8-byte length header), allowing bytecode and UTF-16 regions to
    receive the same alignment Bun's serializer requires. Optional records are
    emitted in flag order immediately after the fixed module table.
    """
    ms = module_struct_size
    field_order = (["name", "contents", "sourcemap", "bytecode", "moduleInfo", "bytecodeOriginPath"]
                   if ms == 52 else ["name", "contents", "sourcemap", "bytecode"])
    builtin_bytecode = list(builtin_bytecode or [])

    out = bytearray(128)  # offset 0 remains the empty-field tombstone

    def align(alignment):
        padding = (-(runtime_blob_vaddr + len(out))) % alignment
        if padding:
            out.extend(b"\x00" * padding)

    def place(data, *, alignment=1, terminator=0):
        data = bytes(data)
        if not data:
            return (0, 0)
        align(alignment)
        off = len(out)
        out.extend(data)
        if terminator:
            out.extend(b"\x00" * terminator)
        return (off, len(data))

    placed = []
    for m in modules:
        offs = {}
        for field in field_order:
            data = m[field]
            if field == "bytecode":
                offs[field] = place(data, alignment=128)
            elif field == "contents" and m["encoding"] == 2:
                offs[field] = place(data, alignment=2, terminator=2)
            elif field in ("name", "contents", "bytecodeOriginPath"):
                offs[field] = place(data, terminator=1)
            else:
                offs[field] = place(data)
        for field in ["name", "contents", "sourcemap", "bytecode", "moduleInfo", "bytecodeOriginPath"]:
            offs.setdefault(field, (0, 0))
        placed.append(offs)

    builtin_pointers = []
    for builtin_id, payload in builtin_bytecode:
        builtin_pointers.append((builtin_id, place(payload, alignment=128)))
    bytecode_table_ptr = place(bytecode_string_table, alignment=128)
    module_info_table_ptr = place(module_info_string_table)

    modules_off = len(out)
    modules_len = len(modules) * ms
    out.extend(b"\x00" * modules_len)

    # Fill the module table after every pointer target has its final offset.
    for mi, (m, offs) in enumerate(zip(modules, placed)):
        rc = modules_off + mi * ms
        for field in field_order:
            o, length = offs[field]
            _struct.pack_into("<II", out, rc, o, length)
            rc += 8
        _struct.pack_into("<BBBB", out, rc,
                          m["encoding"], m["loader"], m["module_format"], m["side"])

    if flags & bun_handler.BUN_FLAG_HAS_SOURCE_HASHES:
        for m in modules:
            out.extend(_struct.pack("<I", m.get("source_hash", 0x12345678)))

    if flags & bun_handler.BUN_FLAG_HAS_BUILTIN_BYTECODE:
        out.extend(_struct.pack("<I", len(builtin_pointers)))
        for builtin_id, (off, length) in builtin_pointers:
            out.extend(_struct.pack("<III", builtin_id, off, length))

    if flags & bun_handler.BUN_FLAG_HAS_BYTECODE_STRING_TABLE:
        out.extend(_struct.pack("<II", *bytecode_table_ptr))

    if flags & bun_handler.BUN_FLAG_HAS_STARTUP_MODULE_COUNT:
        count = len(modules) if startup_module_count is None else startup_module_count
        out.extend(_struct.pack("<I", count))

    if flags & bun_handler.BUN_FLAG_HAS_MODULE_INFO_STRING_TABLE:
        out.extend(_struct.pack("<II", *module_info_table_ptr))

    compile_argv = bytes(compile_argv)
    compile_argv_off = len(out)
    out.extend(compile_argv)
    out.append(0)

    offsets_off = len(out)
    out.extend(b"\x00" * 32)
    out.extend(_TRAILER)
    if trailing_pad:
        out.append(0)

    _struct.pack_into("<Q", out, offsets_off, offsets_off)
    _struct.pack_into("<I", out, offsets_off + 8, modules_off)
    _struct.pack_into("<I", out, offsets_off + 12, modules_len)
    _struct.pack_into("<I", out, offsets_off + 16, entry_point_id)
    _struct.pack_into("<I", out, offsets_off + 20, compile_argv_off)
    _struct.pack_into("<I", out, offsets_off + 24, len(compile_argv))
    _struct.pack_into("<I", out, offsets_off + 28, flags)
    return bytes(out)


def _module(name, contents, bytecode=b"", ms=52, origin=b"", module_format=None,
            module_info=b"", sourcemap=b"", encoding=1, loader=1, side=0,
            source_hash=0x12345678):
    name_bytes = name.encode() if isinstance(name, str) else name
    if module_format is None:
        # Most synthetic secondary records are inert fixtures, not executable
        # JavaScript modules. Infer CJS only for the recognizable Claude
        # entrypoint; tests that intentionally model extra CJS wrappers pass 2
        # explicitly. This keeps single-module fixture expectations explicit.
        name_text = name_bytes.decode("ascii", "ignore")
        origin_bytes = origin.encode() if isinstance(origin, str) else origin
        is_entry = (
            bun_handler.BunImage.is_entrypoint_name(name_text)
            or origin_bytes.endswith(bun_handler.ENTRYPOINT_ORIGIN_SUFFIX.encode("ascii"))
        )
        module_format = 2 if is_entry else 0
    return {
        "name": name_bytes,
        "contents": contents.encode() if isinstance(contents, str) else contents,
        "sourcemap": sourcemap,
        "bytecode": bytecode,
        "moduleInfo": module_info,
        "bytecodeOriginPath": origin.encode() if isinstance(origin, str) else origin,
        "encoding": encoding, "loader": loader, "module_format": module_format,
        "side": side, "source_hash": source_hash,
    }


def build_bun_elf(modules, module_struct_size=52, section_header_size=8,
                  trailing_pad=True, trailing_section=None,
                  trailing_pt_load=None, entry_point_id=0, flags=0xF,
                  builtin_bytecode=None, bytecode_string_table=b"",
                  startup_module_count=None, module_info_string_table=b"",
                  compile_argv=b""):
    """Assemble a minimal valid bun-on-ELF64 binary (little-endian).

    trailing_section: optional dict {flags, sht, vaddr, size, addralign} placed in the file
    AFTER .bun, used to test the growth guards. If its SHF_ALLOC flag is set the
    repack guard must refuse to grow .bun.

    trailing_pt_load: optional dict {p_align, p_filesz, p_vaddr} adding a second
    PT_LOAD segment whose file offset sits AFTER .bun. Used to test the unified
    alignment gate: any nonzero .bun delta whose magnitude is not a multiple of
    this segment's p_align must be refused.

    Returns the binary bytes. Layout is compact but consistent: at least one
    LOAD segment covering .bun, .bun aligned, section header table at EOF.
    """
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
    seg_vaddr = 0x400000
    bun_vaddr = seg_vaddr + bun_off
    runtime_blob_vaddr = bun_vaddr + section_header_size
    blob = _build_bun_blob(
        modules,
        module_struct_size,
        runtime_blob_vaddr,
        trailing_pad=trailing_pad,
        entry_point_id=entry_point_id,
        flags=flags,
        builtin_bytecode=builtin_bytecode,
        bytecode_string_table=bytecode_string_table,
        startup_module_count=startup_module_count,
        module_info_string_table=module_info_string_table,
        compile_argv=compile_argv,
    )
    if section_header_size == 8:
        bun_payload = _struct.pack("<Q", len(blob)) + blob
    else:
        bun_payload = _struct.pack("<I", len(blob)) + blob
    cursor = bun_off + len(bun_payload)

    trailing_off = 0
    trailing_size = 0
    if trailing_section is not None:
        trailing_size = trailing_section.get("size", 16)
        if trailing_section.get("sht", _SHT_PROGBITS) != _SHT_NOBITS:
            trailing_align = trailing_section.get("addralign", 1)
            cursor = (cursor + trailing_align - 1) // trailing_align * trailing_align
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
    def write_sh(idx, name_off, sht, flags, vaddr, off, size, addralign=0):
        ho = shoff + idx * shentsize
        _struct.pack_into("<I", buf, ho + 0, name_off)
        _struct.pack_into("<I", buf, ho + 4, sht)
        _struct.pack_into("<Q", buf, ho + 8, flags)
        _struct.pack_into("<Q", buf, ho + 16, vaddr)
        _struct.pack_into("<Q", buf, ho + 24, off)
        _struct.pack_into("<Q", buf, ho + 32, size)
        _struct.pack_into("<Q", buf, ho + 48, addralign)

    write_sh(0, 0, _SHT_NULL, 0, 0, 0, 0)
    # .bun section: PROGBITS, WRITE+ALLOC, vaddr inside the LOAD segment.
    write_sh(1, bun_name_off, _SHT_PROGBITS, _SHF_WRITE | _SHF_ALLOC,
             bun_vaddr, bun_off, len(bun_payload), 16)
    idx = 2
    if trailing_section is not None:
        write_sh(idx, trailing_name_off,
                 trailing_section.get("sht", _SHT_PROGBITS),
                 trailing_section.get("flags", 0),
                 trailing_section.get("vaddr", 0),
                 trailing_off, trailing_size,
                 trailing_section.get("addralign", 1))
        idx += 1
    write_sh(idx, shstr_name_off, _SHT_STRTAB, 0, 0, shstr_off, len(names), 1)

    return bytes(buf)


def _check(name, ok, detail=""):
    SYNTH_RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


SYNTH_RESULTS = []


def synthetic_tests():
    """Tests that need controllable fixtures the real binary cannot provide."""
    print("SYNTHETIC FIXTURE TESTS")
    bh = bun_handler

    def blob_abs(binary, image, relative_offset):
        section = bh.Elf64(binary).section(".bun")
        return section["foff"] + image.section_header_size + relative_offset

    def pointer_bytes(image, pointer):
        off, length = pointer
        return bytes(image.blob[off:off + length])

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
        _module(
            "/$bunfs/root/src/entrypoints/cli.js", "X();", ms=36,
            bytecode=b"legacy-bytecode" * 16,
            sourcemap=b"legacy-sourcemap",
        ),
        _module("/$bunfs/root/two.js", "Y();", ms=36),
    ]
    fx36 = build_bun_elf(mods36, module_struct_size=36, section_header_size=8)
    img36 = bh.BunImage(fx36)
    _check("36-byte module struct detected", img36.module_struct_size == 36,
           f"got {img36.module_struct_size}")
    _check("36-byte form extract_js works", bh.extract_js(fx36) == b"X();")
    _check("36-byte form no-op byte-identical", bh.repack_unchanged(fx36) == fx36)
    fx36_edit = bh.repack_with_js(fx36, b"Z();")
    img36_edit = bh.BunImage(fx36_edit)
    _check("36-byte changed source clears legacy bytecode/sourcemap",
           img36_edit.modules[0]["bytecode"] == (0, 0)
           and img36_edit.modules[0]["sourcemap"] == (0, 0))
    _check("36-byte equal-length edit round-trips", bh.extract_js(fx36_edit) == b"Z();")

    # --- zero-length field grow is refused regardless of offset ---
    # Two flavours of zero-length field: (0, 0) tombstone and (N>0, 0)
    # placeholder. Both reserve no bytes in the blob, so growing either would
    # shift downstream regions without a matching offset remap. _apply_blob_edits
    # must refuse both with the same BunFormatError shape.
    mods00 = [
        _module("/$bunfs/root/src/entrypoints/cli.js",
                "console.log('(Claude Code)');", bytecode=b"\xd4zFT" + b"\x00" * 40),
        _module("/$bunfs/root/helper.js", "module.exports={};"),  # bytecode is (0,0)
    ]
    fx00 = build_bun_elf(mods00)
    img00 = bh.BunImage(fx00)
    helper = [m for m in img00.modules if not bh.BunImage.is_entrypoint_name(m["name_str"])][0]
    # Confirm the field really is (0, 0) before we test growing it.
    _check("(0,0) test fixture has empty bytecode field on helper module",
           helper["bytecode"] == (0, 0), f"got {helper['bytecode']}")
    raised = False
    try:
        bh._apply_blob_edits(img00, {(helper["index"], "bytecode"): b"new bytecode data"})
    except bh.BunFormatError as exc:
        raised = "cannot grow zero-length field" in str(exc)
    _check("growing a (0,0) field raises BunFormatError", raised)

    # --- (N>0, 0) placeholder grow is also refused ---
    # Build a fresh fixture and hand-edit the helper module's bytecode field
    # to (offset=8, length=0). offset 8 sits inside the blob's zero prefix
    # (valid but reserves no bytes), so _require_range admits it during parse,
    # and the only thing standing between us and a silent corruption is the
    # zero-length grow guard.
    fxN0 = bytearray(build_bun_elf(mods00))
    bun_sec_N0 = bh.Elf64(bytes(fxN0)).section(".bun")
    imgN0_pre = bh.BunImage(bytes(fxN0))
    helperN0 = [m for m in imgN0_pre.modules if not bh.BunImage.is_entrypoint_name(m["name_str"])][0]
    # Record layout: in the 52-byte struct the bytecode field sits at offset
    # 24 inside the record. Compute the absolute file offset and overwrite.
    table_abs = bun_sec_N0["foff"] + imgN0_pre.section_header_size + imgN0_pre.modules_off
    helper_record_abs = table_abs + helperN0["index"] * imgN0_pre.module_struct_size
    _struct.pack_into("<II", fxN0, helper_record_abs + 24, 8, 0)  # bytecode -> (8, 0)
    imgN0 = bh.BunImage(bytes(fxN0))
    helperN0_after = [m for m in imgN0.modules if not bh.BunImage.is_entrypoint_name(m["name_str"])][0]
    _check("(N>0, 0) test fixture has bytecode field at (8, 0)",
           helperN0_after["bytecode"] == (8, 0), f"got {helperN0_after['bytecode']}")
    raised = False
    try:
        bh._apply_blob_edits(imgN0, {(helperN0_after["index"], "bytecode"): b"new bytecode data"})
    except bh.BunFormatError as exc:
        raised = "cannot grow zero-length field" in str(exc)
    _check("growing a (N>0, 0) field raises BunFormatError", raised)

    # Empty source modules can be populated: the writer allocates a new owned
    # NUL-terminated region before the module table and clears stale caches.
    empty_flags = 0xF | bh.BUN_FLAG_SOURCE_TEXT_CONTIGUOUS | bh.BUN_FLAG_HAS_SOURCE_HASHES
    empty_mods = [
        _module(
            "/$bunfs/root/src/entrypoints/cli.js", b"",
            bytecode=b"empty-source-bytecode" * 8,
            sourcemap=b"empty-source-map",
            source_hash=0xAABBCCDD,
        )
    ]
    fx_empty = build_bun_elf(empty_mods, flags=empty_flags)
    _check("empty source fixture is patchable", bh.can_handle(fx_empty))
    populated = b"console.log('(Claude Code)');"
    out_populated = bh.repack_with_js(fx_empty, populated)
    img_populated = bh.BunImage(out_populated)
    _check("zero-length source can grow and round-trip",
           bh.extract_js(out_populated) == populated)
    _check("zero-length source allocation is NUL-terminated",
           img_populated.blob[
               img_populated.modules[0]["contents"][0]
               + img_populated.modules[0]["contents"][1]
           ] == 0)
    _check("zero-length source growth clears hash and compiled state",
           img_populated.source_hash(0) == 0
           and img_populated.modules[0]["bytecode"] == (0, 0)
           and img_populated.modules[0]["sourcemap"] == (0, 0))
    _check("zero-length source growth clears contiguous-source promise",
           not (img_populated.flags & bh.BUN_FLAG_SOURCE_TEXT_CONTIGUOUS))
    emptied_again = bh.repack_with_js(out_populated, b"")
    regrown = bh.repack_with_js(emptied_again, b"export{};")
    _check("source can be emptied and populated again",
           bh.extract_js(emptied_again) == b""
           and bh.extract_js(regrown) == b"export{};"
           and bh.can_handle(regrown))

    # Multiple empty ESM chunks are allocated as distinct owned islands in one
    # splice. This catches pointer arithmetic that accidentally makes every
    # zero-length source point at the last inserted module.
    empty_split_mods = [
        _module(
            "/$bunfs/root/src/entrypoints/cli.js", b"",
            bytecode=b"empty-entry-bytecode" * 8,
            source_hash=0x11111111,
            module_format=1,
        ),
        _module(
            "/$bunfs/root/empty-chunk.js", b"",
            bytecode=b"empty-chunk-bytecode" * 8,
            source_hash=0x22222222,
            module_format=1,
        ),
    ]
    fx_empty_split = build_bun_elf(empty_split_mods, flags=empty_flags)
    extracted_empty_split = bh.extract_js(fx_empty_split)
    populated_empty_split = _replace_extracted_modules(
        extracted_empty_split,
        {0: b'import"/$bunfs/root/empty-chunk.js";',
         1: b"export const populated=1;"},
    )
    out_empty_split = bh.repack_with_js(
        fx_empty_split, populated_empty_split)
    img_empty_split = bh.BunImage(out_empty_split)
    ranges_empty_split = [
        (m["contents"][0], m["contents"][0] + m["contents"][1])
        for m in img_empty_split.source_modules()
    ]
    _check("multiple zero-length source modules grow independently",
           bh.extract_js(out_empty_split) == populated_empty_split
           and ranges_empty_split[0][1] <= ranges_empty_split[1][0])
    _check("multiple zero-length source modules are NUL-terminated",
           all(img_empty_split.blob[end] == 0
               for _start, end in ranges_empty_split))
    _check("multiple zero-length source modules all shed compiled state",
           all(img_empty_split.source_hash(i) == 0
                   and img_empty_split.modules[i]["bytecode"] == (0, 0)
               for i in (0, 1)))

    # --- entry_point_id is preferred over a search by name ---
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

    # And a HARD FAIL when entry_point_id points at a module that neither
    # entrypoint check recognizes: silent fallback would patch the wrong module
    # and still pass smoke tests.
    fx_mismatch = build_bun_elf(mods_idx, entry_point_id=0)  # module 0 is "aux.js"
    img_mm = bh.BunImage(fx_mismatch)
    raised = False
    try:
        img_mm.entrypoint_module()
    except bh.BunFormatError as exc:
        raised = "disagree" in str(exc)
    _check("entry_point_id pointing at non-entrypoint module hard-fails", raised)

    # --- 2.1.232 shape: packed module renamed to "cli", source path unchanged ---
    # Claude 2.1.232 packs its entrypoint as /$bunfs/root/cli, which the name
    # check does not match; bytecodeOriginPath still reads
    # /$bunfs/root/src/entrypoints/cli.js and is what resolves the module.
    mods232 = [
        _module("/$bunfs/root/cli", "console.log('(Claude Code)');",
                bytecode=b"\xd4zFT" + b"\x00" * 40,
                origin="/$bunfs/root/src/entrypoints/cli.js"),
        _module("/$bunfs/root/helper.js", "module.exports={};"),
    ]
    fx232 = build_bun_elf(mods232)
    img232 = bh.BunImage(fx232)
    _check("2.1.232 module name alone is not recognized",
           not bh.BunImage.is_entrypoint_name("/$bunfs/root/cli"))
    _check("2.1.232 shape resolves via bytecodeOriginPath",
           img232.entrypoint_module()["index"] == 0)
    _check("2.1.232 shape extract_js works",
           bh.extract_js(fx232) == b"console.log('(Claude Code)');")
    _check("2.1.232 shape no-op byte-identical", bh.repack_unchanged(fx232) == fx232)

    # A growing edit must leave bytecodeOriginPath readable. That field sits
    # after contents in the record layout, so a wrong remap would shift it and
    # the NEXT patch pass over the same binary would fail to find the entrypoint.
    grown232 = bh.repack_with_js(fx232, b"console.log('(Claude Code) patched');")
    img232g = bh.BunImage(grown232)
    origin_after = img232g.bytecode_origin_path(img232g.modules[0])
    _check("origin path survives a growing edit",
           origin_after == "/$bunfs/root/src/entrypoints/cli.js",
           f"got {origin_after!r}")
    _check("grown 2.1.232 fixture still resolves its entrypoint",
           img232g.entrypoint_module()["index"] == 0)

    # --- a source path pointing elsewhere does not rescue a wrong module ---
    mods_wrong = [
        _module("/$bunfs/root/aux.js", "a();", bytecode=b"\xd4zFT" + b"\x00" * 40,
                origin="/$bunfs/root/src/entrypoints/other.js"),
        _module("/$bunfs/root/helper.js", "h();"),
    ]
    img_wrong = bh.BunImage(build_bun_elf(mods_wrong, entry_point_id=0))
    raised = False
    try:
        img_wrong.entrypoint_module()
    except bh.BunFormatError as exc:
        raised = "disagree" in str(exc)
    _check("non-entrypoint source path still hard-fails", raised)

    # --- the 36-byte record form carries no source path at all ---
    _check("36-byte form reports an empty origin path",
           img36.bytecode_origin_path(img36.modules[0]) == "",
           f"got {img36.bytecode_origin_path(img36.modules[0])!r}")

    # --- older bundled-CJS builds expose the entrypoint AND CJS wrappers ---
    # Claude 2.1.234 has this shape: one large CJS entrypoint, three small CJS
    # native-addon wrappers, and unrelated file assets. The editing surface must
    # include every executable JS record without sweeping in those assets.
    cjs_prologue = (
        b"// @bun @bytecode @bun-cjs\n"
        b"(function(exports, require, module, __filename, __dirname) {"
    )
    cjs_mods = [
        _module(
            "/$bunfs/root/cli",
            cjs_prologue + b'console.log("(Claude Code)");})',
            bytecode=b"cjs-entry-bytecode" * 10,
            origin="/$bunfs/root/src/entrypoints/cli.js",
            module_format=2,
            source_hash=0x11111111,
        ),
        _module(
            "/$bunfs/root/image-processor.js",
            cjs_prologue
            + b'module.exports=require("/$bunfs/root/image-processor.node");})',
            bytecode=b"cjs-wrapper-bytecode" * 10,
            module_format=2,
            source_hash=0x22222222,
        ),
        _module(
            "/$bunfs/root/audio-capture.js",
            cjs_prologue
            + b'module.exports=require("/$bunfs/root/audio-capture.node");})',
            module_format=2,
            source_hash=0x33333333,
        ),
        _module(
            "/$bunfs/root/chart.umd.min.js", b"/* file asset, not a module */",
            module_format=0, loader=5, encoding=0, source_hash=0,
        ),
    ]
    cjs_flags = 0xF | bh.BUN_FLAG_HAS_SOURCE_HASHES
    fx_cjs = build_bun_elf(cjs_mods, flags=cjs_flags)
    img_cjs = bh.BunImage(fx_cjs)
    extracted_cjs = bh.extract_js(fx_cjs)
    cjs_records = bh.split_extracted_js(extracted_cjs)
    _check("bundled CJS extraction includes entrypoint and wrappers",
           [(index, name) for index, name, _ in cjs_records] == [
               (0, b"/$bunfs/root/cli"),
               (1, b"/$bunfs/root/image-processor.js"),
               (2, b"/$bunfs/root/audio-capture.js"),
           ])
    _check("bundled CJS extraction excludes file assets",
           all(index != 3 for index, _name, _contents in cjs_records))
    _check("bundled CJS no-op repack is byte-identical",
           bh.repack_unchanged(fx_cjs) == fx_cjs)

    old_wrapper = cjs_records[1][2]
    new_wrapper = old_wrapper.replace(
        b"image-processor.node", b"image-processor-v2.node")
    patched_cjs = _replace_extracted_modules(
        extracted_cjs, {1: new_wrapper})
    changed_cjs = bh.changed_js_modules(extracted_cjs, patched_cjs)
    _check("bundled CJS changed-module detection finds only wrapper",
           [(index, name) for index, name, _ in changed_cjs]
           == [(1, b"/$bunfs/root/image-processor.js")])
    out_cjs = bh.repack_with_js(fx_cjs, patched_cjs)
    img_cjs_after = bh.BunImage(out_cjs)
    _check("bundled CJS wrapper edit maps to its own module",
           img_cjs_after.read_field(img_cjs_after.modules[1], "contents")
           == new_wrapper
           and img_cjs_after.read_field(img_cjs_after.modules[0], "contents")
           == img_cjs.read_field(img_cjs.modules[0], "contents"))
    _check("bundled CJS changed wrapper sheds compiled state",
           img_cjs_after.source_hash(1) == 0
           and img_cjs_after.modules[1]["bytecode"] == (0, 0))
    _check("bundled CJS untouched entrypoint retains compiled state",
           img_cjs_after.source_hash(0) == img_cjs.source_hash(0)
           and img_cjs_after.read_field(img_cjs_after.modules[0], "bytecode")
           == img_cjs.read_field(img_cjs.modules[0], "bytecode"))

    # --- Bun 1.4 split ESM builds expose all JavaScript chunks ---
    split_mods = [
        _module("/$bunfs/root/cli",
                'import{answer}from"/$bunfs/root/chunk-a.js";console.log(answer);',
                bytecode=b"entry-bytecode", origin="/$bunfs/root/src/entrypoints/cli.js",
                module_format=1),
        _module("/$bunfs/root/chunk-a.js", "export let answer=1;",
                bytecode=b"chunk-a-bytecode", origin="/$bunfs/root/chunk-a.js",
                module_format=1),
        _module("/$bunfs/root/chunk-b.js", "export let unused=2;",
                bytecode=b"chunk-b-bytecode", origin="/$bunfs/root/chunk-b.js",
                module_format=1),
    ]
    fx_split = build_bun_elf(split_mods, entry_point_id=0)
    img_split = bh.BunImage(fx_split)
    extracted_split = bh.extract_js(fx_split)
    split_records = bh.split_extracted_js(extracted_split)
    _check("split ESM extraction includes every JavaScript module",
           [(index, name) for index, name, _ in split_records] == [
               (0, b"/$bunfs/root/cli"),
               (1, b"/$bunfs/root/chunk-a.js"),
               (2, b"/$bunfs/root/chunk-b.js"),
           ])
    _check("split ESM extraction is larger than the bootstrap",
           len(extracted_split) > img_split.entrypoint_module()["contents"][1])
    _check("split ESM no-op repack is byte-identical",
           bh.repack_unchanged(fx_split) == fx_split)

    patched_split = extracted_split.replace(b"export let answer=1;", b"export let answer=1000;")
    changed_split = bh.changed_js_modules(extracted_split, patched_split)
    _check("changed-module detection identifies only the edited chunk",
           [(index, name) for index, name, _ in changed_split]
           == [(1, b"/$bunfs/root/chunk-a.js")])
    out_split = bh.repack_with_js(fx_split, patched_split)
    img_split_after = bh.BunImage(out_split)
    _check("split ESM edit maps back to the correct module",
           img_split_after.read_field(img_split_after.modules[1], "contents")
           == b"export let answer=1000;")
    _check("split ESM untouched entrypoint stays byte-identical",
           img_split_after.read_field(img_split_after.modules[0], "contents")
           == img_split.read_field(img_split.modules[0], "contents"))
    _check("split ESM extraction round-trips after edit",
           bh.extract_js(out_split) == patched_split)

    damaged_markers = patched_split.replace(
        b"bun_handler module 6e0d7c9f5a3b4d2184f176c2",
        b"bun_handler module damaged",
        1,
    )
    raised = False
    try:
        bh.repack_with_js(fx_split, damaged_markers)
    except bh.BunFormatError as exc:
        raised = "marker" in str(exc)
    _check("split ESM repack refuses changed module markers", raised)

    # --- current optional-tail layout and changed-module invalidation ---
    cache_flags = (
        0xF
        | bh.BUN_FLAG_HAS_SOURCE_HASHES
        | bh.BUN_FLAG_HAS_BUILTIN_BYTECODE
        | bh.BUN_FLAG_HAS_BYTECODE_STRING_TABLE
        | bh.BUN_FLAG_HAS_STARTUP_MODULE_COUNT
        | bh.BUN_FLAG_HAS_MODULE_INFO_STRING_TABLE
    )
    cache_mods = [
        _module(
            "/$bunfs/root/cli",
            'import{answer}from"/$bunfs/root/chunk.js";console.log(answer);',
            bytecode=b"entry-bytecode" * 10,
            module_info=b"entry-module-info",
            sourcemap=b"entry-sourcemap",
            origin="/$bunfs/root/src/entrypoints/cli.js",
            module_format=1,
            source_hash=0x11111111,
        ),
        _module(
            "/$bunfs/root/chunk.js",
            "export let answer=1;",
            bytecode=b"chunk-bytecode" * 10,
            module_info=b"chunk-module-info",
            sourcemap=b"chunk-sourcemap",
            origin="/$bunfs/root/chunk.js",
            module_format=1,
            source_hash=0x22222222,
        ),
    ]
    fx_cache = build_bun_elf(
        cache_mods,
        flags=cache_flags,
        builtin_bytecode=[(7, b"builtin-cache" * 20)],
        bytecode_string_table=b"bytecode-string-table" * 20,
        startup_module_count=1,
        module_info_string_table=b"module-info-string-table",
        compile_argv=b"--smol",
    )
    before_cache = bh.BunImage(fx_cache)
    _check("optional-tail fixture is patchable", bh.can_handle(fx_cache))
    _check("optional-tail no-op remains byte-identical",
           bh.repack_unchanged(fx_cache) == fx_cache)
    _check("source hashes parse in module order",
           [before_cache.source_hash(i) for i in range(2)]
           == [0x11111111, 0x22222222])
    _check("startup-module count parses", before_cache.startup_module_count == 1)
    _check("compile argv parses",
           bytes(before_cache.blob[
               before_cache.compile_argv_off:
               before_cache.compile_argv_off + before_cache.compile_argv_len
           ]) == b"--smol")

    cache_js = bh.extract_js(fx_cache)
    cache_patched = cache_js.replace(
        b"export let answer=1;", b"export let answer=1000;")
    cache_out = bh.repack_with_js(fx_cache, cache_patched)
    after_cache = bh.BunImage(cache_out)
    _check("optional-tail source edit round-trips",
           bh.extract_js(cache_out) == cache_patched)
    _check("changed module source hash is cleared",
           after_cache.source_hash(1) == 0)
    _check("changed module compiled pointers are detached",
           all(after_cache.modules[1][field] == (0, 0)
               for field in ("bytecode", "moduleInfo", "sourcemap")))
    _check("unchanged module source hash is retained",
           after_cache.source_hash(0) == 0x11111111)
    _check("unchanged module compiled payloads are retained",
           all(after_cache.read_field(after_cache.modules[0], field)
               == before_cache.read_field(before_cache.modules[0], field)
               for field in ("bytecode", "moduleInfo", "sourcemap")))
    _check("builtin bytecode pointer remaps and payload survives",
           after_cache.builtin_bytecode[0]["bytes"]
               != before_cache.builtin_bytecode[0]["bytes"]
           and pointer_bytes(after_cache, after_cache.builtin_bytecode[0]["bytes"])
               == pointer_bytes(before_cache, before_cache.builtin_bytecode[0]["bytes"]))
    _check("bytecode string-table pointer remaps and payload survives",
           after_cache.bytecode_string_table != before_cache.bytecode_string_table
           and pointer_bytes(after_cache, after_cache.bytecode_string_table)
               == pointer_bytes(before_cache, before_cache.bytecode_string_table))
    _check("module-info string-table pointer remaps and payload survives",
           after_cache.module_info_string_table != before_cache.module_info_string_table
           and pointer_bytes(after_cache, after_cache.module_info_string_table)
               == pointer_bytes(before_cache, before_cache.module_info_string_table))
    _check("compile argv remaps and survives",
           after_cache.compile_argv_off != before_cache.compile_argv_off
           and bytes(after_cache.blob[
               after_cache.compile_argv_off:
               after_cache.compile_argv_off + after_cache.compile_argv_len
           ]) == b"--smol")
    _check("alignment padding keeps remapped cache regions valid",
           bh.can_handle(cache_out), f"file delta={len(cache_out) - len(fx_cache)}")

    # --- 36-vs-52 disambiguation when the table length divides both ---
    # 13 * 36 == 9 * 52 == 468. Record 0 is identical through its first four
    # ranges, but later record boundaries differ. Validating every record picks
    # the real layout without relying on a binary version.
    mods36_amb = [_module("/$bunfs/root/src/entrypoints/cli.js", "X();", ms=36)]
    for n in range(12):
        mods36_amb.append(_module(f"/$bunfs/root/mod{n}.js", f"m{n}();", ms=36))
    fx36_amb = build_bun_elf(mods36_amb, module_struct_size=36, section_header_size=8)
    img36_amb = bh.BunImage(fx36_amb)
    _check("ambiguous-length 36-byte table is identified from all records",
           img36_amb.module_struct_size == 36, f"got {img36_amb.module_struct_size}")
    _check("ambiguous-length 36-byte table extracts its entrypoint",
           img36_amb.extract_js() == b"X();")
    _check("ambiguous-length 36-byte table no-op round-trips",
           bh.repack_unchanged(fx36_amb) == fx36_amb)

    mods52_amb = [
        _module("/$bunfs/root/src/entrypoints/cli.js",
                "console.log('(Claude Code)');",
                bytecode=b"\xd4zFT" + b"\x00" * 40),
    ]
    for n in range(8):
        mods52_amb.append(_module(f"/$bunfs/root/m{n}.js", f"e{n}();"))
    fx52_amb = build_bun_elf(mods52_amb, module_struct_size=52, section_header_size=8)
    img52_amb = bh.BunImage(fx52_amb)
    _check("ambiguous-length 52-byte table is identified from all records",
           img52_amb.module_struct_size == 52, f"got {img52_amb.module_struct_size}")
    _check("ambiguous-length 52-byte table extracts its entrypoint",
           img52_amb.extract_js() == b"console.log('(Claude Code)');")
    _check("ambiguous-length 52-byte table no-op round-trips",
           bh.repack_unchanged(fx52_amb) == fx52_amb)

    # A deliberately constructed table can satisfy both interpretations. Every
    # candidate name points to the same valid bunfs path, and all remaining
    # accidental ranges end before the table. The parser must still refuse.
    both_mods = [
        _module("/$bunfs/root/src/entrypoints/cli.js", "X" * 1024,
                bytecode=b"\xd4zFT" + b"\x00" * 40),
    ]
    for n in range(8):
        both_mods.append(_module(f"/$bunfs/root/b{n}.js", f"b{n}();"))
    both_seed = build_bun_elf(both_mods, module_struct_size=52, section_header_size=8)
    both_seed_img = bh.BunImage(both_seed)
    both_fx = bytearray(both_seed)
    both_bun = bh.Elf64(both_seed).section(".bun")
    both_blob_abs = both_bun["foff"] + both_seed_img.section_header_size
    both_table_abs = both_blob_abs + both_seed_img.modules_off
    both_fx[both_table_abs:both_table_abs + both_seed_img.modules_len] = (
        b"\x00" * both_seed_img.modules_len)
    shared_name = b"/$bunfs/" + b"a" * (257 - len("/$bunfs/"))
    both_fx[both_blob_abs + 257:both_blob_abs + 257 + len(shared_name)] = shared_name
    both_fx[both_blob_abs + 257 + len(shared_name)] = 0
    for ms in (36, 52):
        for i in range(both_seed_img.modules_len // ms):
            _struct.pack_into("<II", both_fx, both_table_abs + i * ms, 257, 257)
    raised = None
    try:
        bh.BunImage(bytes(both_fx))
    except bh.BunFormatError as exc:
        raised = "both 36-byte and 52-byte records validate" in str(exc)
    _check("table valid under both layouts refuses as ambiguous",
           raised is True, f"got raised={raised}")

    # If the common record-0 name is malformed, neither interpretation is
    # acceptable and the error must say that rather than choosing by length.
    neither_fx = bytearray(fx36_amb)
    neither_seed = bh.BunImage(fx36_amb)
    neither_bun = bh.Elf64(fx36_amb).section(".bun")
    neither_table_abs = (neither_bun["foff"] + neither_seed.section_header_size
                         + neither_seed.modules_off)
    _struct.pack_into("<I", neither_fx, neither_table_abs + 4, 0)
    raised = None
    try:
        bh.BunImage(bytes(neither_fx))
    except bh.BunFormatError as exc:
        raised = "matches neither 36-byte nor 52-byte record layout" in str(exc)
    _check("table invalid under both layouts refuses as malformed",
           raised is True, f"got raised={raised}")

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

    # A one-byte source growth before an 8-byte-aligned metadata section must
    # acquire seven bytes of anonymous Bun padding. Runtime bytecode alignment
    # is not the only alignment contract in the file.
    fx_file_align = build_bun_elf(
        mods,
        trailing_section={"flags": 0, "sht": _SHT_PROGBITS,
                          "vaddr": 0, "size": 32, "addralign": 8},
    )
    js_file_align = bh.extract_js(fx_file_align)
    out_file_align = bh.repack_with_js(fx_file_align, js_file_align + b"X")
    elf_file_align = bh.Elf64(out_file_align)
    trail_file_align = elf_file_align.section(".trail")
    _check("ELF metadata alignment padding rounds +1 source delta to +8",
           len(out_file_align) - len(fx_file_align) == 8)
    _check("shifted section and section-header table remain aligned",
           trail_file_align["foff"] % trail_file_align["addralign"] == 0
           and elf_file_align.e_shoff % 8 == 0)

    # The low-level ELF splice is guarded too; callers cannot bypass the Bun
    # edit planner and produce a misaligned metadata tail.
    raw_wrapped = bytearray(
        fx_file_align[
            bh.Elf64(fx_file_align).section(".bun")["foff"]:
            bh.Elf64(fx_file_align).section(".bun")["foff"]
            + bh.Elf64(fx_file_align).section(".bun")["size"]
        ]
    )
    raw_wrapped.append(0)
    misaligned_low_level_rejected = False
    try:
        bh._repack_section_elf(fx_file_align, raw_wrapped)
    except bh.BunFormatError as exc:
        misaligned_low_level_rejected = "file alignment" in str(exc)
    _check("low-level ELF repack rejects misaligned section delta",
           misaligned_low_level_rejected)

    fx_bad_addralign = bytearray(fx_file_align)
    bad_addralign_trail = bh.Elf64(bytes(fx_bad_addralign)).section(".trail")
    _struct.pack_into(
        "<Q", fx_bad_addralign, bad_addralign_trail["hdr_off"] + 48, 3)
    _check("non-power-of-two sh_addralign is rejected",
           not bh.can_handle(bytes(fx_bad_addralign)))

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

    # --- SAFETY GUARD: an unrelated file-backed segment intersecting .bun ---
    # Keep both LOAD headers individually valid, but move the second segment's
    # file view inside .bun. The writer must reject the overlapping ownership.
    fx_span = bytearray(build_bun_elf(
        mods, trailing_pt_load={"p_align": 1, "p_filesz": 64}))
    elf_s = bh.Elf64(bytes(fx_span))
    bun_s = elf_s.section(".bun")
    seg2 = elf_s.segments[1]
    _struct.pack_into("<Q", fx_span, seg2["hdr_off"] + 8, bun_s["foff"] + 8)
    raised = False
    try:
        img_span = bh.BunImage(bytes(fx_span))
        bh._repack_section_elf(
            img_span.data,
            bh._wrap_section(bytes(img_span.blob), img_span.section_header_size),
        )
    except bh.BunFormatError as exc:
        raised = "intersects unrelated" in str(exc)
    _check("guard: refuse unrelated segment intersecting .bun", raised)

    # --- adversarial structure acceptance: every writer precondition ---
    # byte_count must identify the offsets structure exactly; accepting another
    # value used to make a no-op rewrite silently canonicalize the input.
    fx_byte_count = bytearray(fx)
    img_byte_count = bh.BunImage(fx)
    _struct.pack_into(
        "<Q", fx_byte_count,
        blob_abs(fx, img_byte_count, img_byte_count.offsets_off),
        img_byte_count.offsets_off + 1,
    )
    _check("mismatched byte_count is rejected", not bh.can_handle(bytes(fx_byte_count)))

    fx_unknown_flags = bytearray(fx)
    _struct.pack_into(
        "<I", fx_unknown_flags,
        blob_abs(fx, img_byte_count, img_byte_count.offsets_off + 28),
        img_byte_count.flags | (1 << 31),
    )
    _check("unknown optional-record flag bits are rejected",
           not bh.can_handle(bytes(fx_unknown_flags)))

    table_abs = blob_abs(fx, img_byte_count, img_byte_count.modules_off)
    first_name = img_byte_count.modules[0]["name"]
    first_contents = img_byte_count.modules[0]["contents"]

    fx_duplicate_name = bytearray(fx)
    _struct.pack_into(
        "<II", fx_duplicate_name,
        table_abs + img_byte_count.module_struct_size,
        *first_name,
    )
    _check("duplicate module names are rejected",
           not bh.can_handle(bytes(fx_duplicate_name)))

    fx_aliased_contents = bytearray(fx)
    _struct.pack_into(
        "<II", fx_aliased_contents,
        table_abs + img_byte_count.module_struct_size + 8,
        *first_contents,
    )
    _check("aliased module contents are rejected",
           not bh.can_handle(bytes(fx_aliased_contents)))

    fx_table_contents = bytearray(fx)
    _struct.pack_into(
        "<II", fx_table_contents,
        table_abs + img_byte_count.module_struct_size + 8,
        img_byte_count.modules_off, 1,
    )
    _check("module contents intersecting the module table are rejected",
           not bh.can_handle(bytes(fx_table_contents)))

    fx_misaligned_bytecode = bytearray(fx)
    bytecode_off, bytecode_len = img_byte_count.modules[0]["bytecode"]
    _struct.pack_into(
        "<II", fx_misaligned_bytecode,
        table_abs + 24,
        bytecode_off + 1, bytecode_len - 1,
    )
    _check("misaligned bytecode pointers are rejected",
           not bh.can_handle(bytes(fx_misaligned_bytecode)))

    fx_bad_terminator = bytearray(fx)
    name_end = blob_abs(
        fx, img_byte_count, first_name[0] + first_name[1])
    fx_bad_terminator[name_end] = ord("X")
    _check("missing module-name terminator is rejected",
           not bh.can_handle(bytes(fx_bad_terminator)))

    fx_interior_name_nul = bytearray(fx)
    interior_name_at = blob_abs(fx, img_byte_count, first_name[0] + 10)
    fx_interior_name_nul[interior_name_at] = 0
    _check("interior NUL in a module name is rejected",
           not bh.can_handle(bytes(fx_interior_name_nul)))

    fx_interior_argv_nul = build_bun_elf(
        mods, compile_argv=b"--smol\x00--inspect")
    _check("interior NUL in compile argv is rejected",
           not bh.can_handle(fx_interior_argv_nul))

    fx_bad_loader = bytearray(fx)
    fx_bad_loader[table_abs + 49] = bh.BUN_MAX_LOADER + 1
    _check("invalid Loader discriminants are rejected",
           not bh.can_handle(bytes(fx_bad_loader)))

    fx_arm64 = bytearray(fx)
    _struct.pack_into("<H", fx_arm64, 18, 0xB7)
    _check("non-x86-64 ELF is rejected", not bh.can_handle(bytes(fx_arm64)))

    fx_freebsd = bytearray(fx)
    fx_freebsd[7] = 9  # ELFOSABI_FREEBSD
    _check("non-Linux ELF OSABI is rejected", not bh.can_handle(bytes(fx_freebsd)))

    utf16_mods = [
        _module(
            "/$bunfs/root/src/entrypoints/cli.js",
            "console.log('(Claude Code)');".encode("utf-16le"),
            encoding=2,
        )
    ]
    fx_utf16 = build_bun_elf(utf16_mods)
    utf16_parses = False
    utf16_extract_rejected = False
    try:
        bh.BunImage(fx_utf16)
        utf16_parses = True
        bh.extract_js(fx_utf16)
    except NotImplementedError:
        utf16_extract_rejected = True
    _check("UTF-16 graph structure parses", utf16_parses)
    _check("UTF-16 patchable source is explicitly unsupported",
           utf16_extract_rejected and not bh.can_handle(fx_utf16))

    latin1_non_ascii = build_bun_elf([
        _module("/$bunfs/root/src/entrypoints/cli.js", b"x=\xe9;")
    ])
    _check("non-ASCII Latin-1 source is rejected pending UTF-16 support",
           not bh.can_handle(latin1_non_ascii))
    non_ascii_edit_rejected = False
    try:
        bh.repack_with_js(fx, bh.extract_js(fx) + "é".encode("utf-8"))
    except NotImplementedError:
        non_ascii_edit_rejected = True
    _check("non-ASCII source edits are rejected instead of mis-encoded",
           non_ascii_edit_rejected)

    # Full interval checks cover equal-start and containing sections, not only a
    # second section whose start lies strictly inside `.bun`.
    for mode in ("same-start", "contains"):
        candidate = bytearray(build_bun_elf(
            mods, trailing_section={"flags": 0, "sht": _SHT_PROGBITS,
                                    "vaddr": 0, "size": 32}))
        candidate_elf = bh.Elf64(bytes(candidate))
        candidate_bun = candidate_elf.section(".bun")
        candidate_trail = candidate_elf.section(".trail")
        if mode == "same-start":
            new_off, new_size = candidate_bun["foff"], 8
        else:
            new_off = candidate_bun["foff"] - 8
            new_size = candidate_bun["size"] + 16
        _struct.pack_into("<Q", candidate, candidate_trail["hdr_off"] + 24, new_off)
        _struct.pack_into("<Q", candidate, candidate_trail["hdr_off"] + 32, new_size)
        _check(f"{mode} section overlap is rejected",
               not bh.can_handle(bytes(candidate)))

    fx_nobits_alloc = build_bun_elf(
        mods,
        trailing_section={"flags": _SHF_ALLOC, "sht": _SHT_NOBITS,
                          "vaddr": 0x900000, "size": 64},
    )
    _check("later allocated NOBITS section makes resize unsupported",
           not bh.can_handle(fx_nobits_alloc))

    fx_readonly_section = bytearray(fx)
    readonly_elf = bh.Elf64(bytes(fx_readonly_section))
    readonly_bun = readonly_elf.section(".bun")
    _struct.pack_into(
        "<Q", fx_readonly_section, readonly_bun["hdr_off"] + 8,
        readonly_bun["flags"] & ~bh.ELF_SHF_WRITE,
    )
    _check("read-only .bun section is rejected",
           not bh.can_handle(bytes(fx_readonly_section)))

    fx_readonly_segment = bytearray(fx)
    readonly_seg = bh.Elf64(bytes(fx_readonly_segment)).segments[0]
    _struct.pack_into(
        "<I", fx_readonly_segment, readonly_seg["hdr_off"] + 4,
        readonly_seg["flags"] & ~bh.ELF_PF_WRITE,
    )
    _check("read-only containing PT_LOAD is rejected",
           not bh.can_handle(bytes(fx_readonly_segment)))

    fx_bad_mapping_relation = bytearray(fx)
    relation_elf = bh.Elf64(bytes(fx_bad_mapping_relation))
    relation_bun = relation_elf.section(".bun")
    _struct.pack_into(
        "<Q", fx_bad_mapping_relation, relation_bun["hdr_off"] + 16,
        relation_bun["vaddr"] + 1,
    )
    _check(".bun sh_addr must match its PT_LOAD file mapping",
           not bh.can_handle(bytes(fx_bad_mapping_relation)))

    fx_zero_fill_tail = bytearray(fx)
    zero_fill_segment = bh.Elf64(bytes(fx_zero_fill_tail)).segments[0]
    _struct.pack_into(
        "<Q", fx_zero_fill_tail, zero_fill_segment["hdr_off"] + 40,
        zero_fill_segment["memsz"] + 64,
    )
    _check("PT_LOAD zero-fill tail makes resize unsupported",
           not bh.can_handle(bytes(fx_zero_fill_tail)))

    # Extend the containing PT_LOAD over a zeroed non-ALLOC section after
    # `.bun`. Even though the bytes are harmless padding-like zeros, the section
    # independently describes their file position, whose virtual relation this
    # writer does not rewrite.
    fx_described_tail = bytearray(build_bun_elf(
        mods, trailing_section={"flags": 0, "sht": _SHT_PROGBITS,
                                "vaddr": 0, "size": 32}))
    described_elf = bh.Elf64(bytes(fx_described_tail))
    described_bun = described_elf.section(".bun")
    described_trail = described_elf.section(".trail")
    described_segment = described_elf.segments[0]
    fx_described_tail[
        described_trail["foff"]:
        described_trail["foff"] + described_trail["size"]
    ] = b"\x00" * described_trail["size"]
    described_end = described_trail["foff"] + described_trail["size"]
    _struct.pack_into(
        "<Q", fx_described_tail, described_segment["hdr_off"] + 32,
        described_end - described_segment["foff"],
    )
    _struct.pack_into(
        "<Q", fx_described_tail, described_segment["hdr_off"] + 40,
        described_end - described_segment["foff"],
    )
    _check("section-described PT_LOAD tail makes resize unsupported",
           not bh.can_handle(bytes(fx_described_tail)))

    # A later non-LOAD program header also carries its own p_offset/p_vaddr
    # relationship. Turn the fixture's second LOAD into PT_NOTE to prove the
    # generic auxiliary-segment guard, not only the later-LOAD guard.
    fx_aux_segment = bytearray(build_bun_elf(
        mods, trailing_pt_load={"p_align": 4, "p_filesz": 64}))
    aux_segment = bh.Elf64(bytes(fx_aux_segment)).segments[1]
    _struct.pack_into("<I", fx_aux_segment, aux_segment["hdr_off"], 4)  # PT_NOTE
    _check("later auxiliary file-backed segment makes resize unsupported",
           not bh.can_handle(bytes(fx_aux_segment)))

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

    # --- resize geometry: any later PT_LOAD makes a nonzero resize unsafe ---
    # Page congruence alone is insufficient: growing the containing mapping can
    # overlap a later mapping even when the later p_offset stays congruent to its
    # p_vaddr. Reject aligned and unaligned grow/shrink symmetrically.
    def _try_resize(fx_bytes, contents_delta):
        img_loc = bh.BunImage(fx_bytes)
        js_loc = bh.extract_js(fx_bytes)
        new_js = (js_loc + b"P" * contents_delta
                  if contents_delta > 0 else js_loc[:contents_delta])
        try:
            out = bh.repack_with_js(fx_bytes, new_js)
            return None, len(out)
        except bh.BunFormatError as exc:
            return str(exc), None

    big_js = b"console.log('(Claude Code)');" + b"X" * 0x2000
    big_mods = [
        _module("/$bunfs/root/src/entrypoints/cli.js", big_js,
                bytecode=b"\xd4zFT" + b"\x00" * 40),
        _module("/$bunfs/root/helper.js", "module.exports={};"),
    ]
    fx_load = build_bun_elf(
        big_mods, trailing_pt_load={"p_align": 0x1000, "p_filesz": 64})
    for delta, label in (
        (+7, "unaligned grow +7"),
        (-9, "unaligned shrink -9"),
        (+0x1000, "page-aligned grow +0x1000"),
        (-0x1000, "page-aligned shrink -0x1000"),
    ):
        error, _size = _try_resize(fx_load, delta)
        _check(
            f"later PT_LOAD rejected for {label}",
            error is not None and "containing PT_LOAD is final" in error,
            repr(error),
        )

    # A five-byte source shrink is exactly absorbed by five bytes of metadata
    # alignment padding, so the ELF section does not resize and no later
    # segment moves. This is safe even though general nonzero resizing of the
    # layout is unsupported.
    zero_delta_error, zero_delta_size = _try_resize(fx_load, -5)
    _check("alignment padding may turn a source shrink into ELF delta zero",
           zero_delta_error is None and zero_delta_size == len(fx_load),
           repr(zero_delta_error))

    _check("later PT_LOAD does not block delta == 0",
           bh.repack_unchanged(fx_load) == fx_load)

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
        print("REAL GATES 1-3,5-9: SKIPPED (no native binary found; set "
              "CLAUDE_NATIVE_BINARY, pass a path, or install under "
              "~/.local/share/claude/versions)")
        ok = all(results)
        print(f"\nSUITE {'PASS' if ok else 'FAIL'} (synthetic + gate 4 only; real gates skipped)")
        return 0 if ok else 1

    print(f"binary: {binary}")
    with open(binary, "rb") as f:
        data = f.read()
    print(f"format: {bun_handler.detect_format(data)}  ({len(data)} bytes)")
    if not bun_handler.can_handle(data):
        print("REAL GATES: cannot run; handler does not support this binary")
        print("\nSUITE FAIL")
        return 1

    # Strict-mode gate runner: CLAUDE_PFG_STRICT_GATES=gate7[,gateN...] promotes
    # SKIP -> FAIL for the listed gate ids; CLAUDE_PFG_STRICT_GATES_WAIVE=gate7
    # overrides strict mode for that gate (SKIP stays SKIP). Intended for CI
    # release synthesis (per plan section 6 step 11b) so the gates that prove
    # the most (especially gates 7-9's stale-cache proofs) cannot silently degrade when
    # a future Anthropic bundle bump moves the anchor. Default with no env var is
    # unchanged: skip-permissive for local dev.
    strict_gates = set(g.strip() for g in os.environ.get(
        "CLAUDE_PFG_STRICT_GATES", "").split(",") if g.strip())
    waived_gates = set(g.strip() for g in os.environ.get(
        "CLAUDE_PFG_STRICT_GATES_WAIVE", "").split(",") if g.strip())
    for gate_id, label, fn in [
        ("gate1", "GATE 1 no-op byte-identical", gate1_noop),
        ("gate2", "GATE 2 length-changing + runs + shows edit", gate2_length_change),
        ("gate3", "GATE 3 determinism", gate3_determinism),
        ("gate5", "GATE 5 shrinking edit (negative delta)", gate5_shrink),
        ("gate6", "GATE 6 public multi-module edit + runs", gate6_multi_edit),
        ("gate7", "GATE 7 equal-length control-flow edit", gate7_equal_length_control_flow),
        ("gate8", "GATE 8 equal-length literal edit", gate8_equal_length_literal),
        ("gate9", "GATE 9 dependency re-analysis", gate9_dependency_reanalysis),
    ]:
        ok, detail = fn(data)
        if ok is None:
            if gate_id in strict_gates and gate_id not in waived_gates:
                print(f"{label}: FAIL (strict mode: {gate_id} required to PASS, "
                      f"got SKIP; detail: {detail!r}; set "
                      f"CLAUDE_PFG_STRICT_GATES_WAIVE={gate_id} to override)")
                results.append(False)
            else:
                print(f"{label}: SKIP ({detail})")
        else:
            print(f"{label}: {'PASS' if ok else 'FAIL'} ({detail})")
            results.append(ok)

    ok = all(results)
    print(f"\nSUITE {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
