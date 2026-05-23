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

Gates:
  1. Byte-exact no-op repack: repack_unchanged(b) == b  (cmp-identical).
  2. Length-changing patch: repack with a longer JS, the binary still runs
     (`claude --version` exits 0) AND the edit is visible in the output.
  3. Determinism: same input + same edit -> identical output bytes across runs.
  4. Format detection / guards: non-bun and truncated inputs are rejected with
     clear errors, never a silent wrong-answer.

Finding the binary (first hit wins):
  - CLAUDE_NATIVE_BINARY environment variable (explicit path), or
  - first CLI argument, or
  - common install locations (~/.local/share/claude/versions/<latest>).

If no binary is found, gates 1-3 are SKIPPED (reported as such, not failed) so
the suite can run in CI without the 238 MB artifact; gate 4 always runs because
it builds its own tiny synthetic inputs.

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


def main():
    print("=== bun_handler standalone test harness ===")
    results = []

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
                      ("GATE 3 determinism", gate3_determinism)]:
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
