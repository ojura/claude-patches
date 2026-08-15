#!/usr/bin/env python3
"""Apply the connoisseur display patches to a native Claude Code binary.

Reuses the upstream patch logic verbatim (vendor/connoisseur/patch-claude-display.ts,
run under node -- it is plain CommonJS despite the .ts extension) and drives the
bun-binary extract/repack through our own bun_handler.py instead of the repo's
tweakcc/vendored-ELF path.

Pipeline: pristine binary -> extract JS (bun_handler) -> patch JS (node) ->
repack (bun_handler) -> verify `--version` shows "(patched)" -> install in place.

Re-runs are safe and deterministic: the patch always starts from the
`<bin>.orig-unpatched` backup, which is captured once from the first pristine
binary and never overwritten.

Usage:
  apply_connoisseur.py <version>          # e.g. 2.1.220 -> ~/.local/share/claude/versions/2.1.220
  apply_connoisseur.py <path-to-binary>
  apply_connoisseur.py <target> --no-install   # build + verify only, leave live binary alone
  apply_connoisseur.py <target> --patcher <patch-claude-display.ts>
  apply_connoisseur.py <target> --keep <out-binary-path>
"""
import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_PATCHES = os.path.dirname(HERE)
DEFAULT_PATCHER = os.path.join(CLAUDE_PATCHES, "vendor", "connoisseur", "patch-claude-display.ts")
VERSIONS_DIR = os.path.expanduser("~/.local/share/claude/versions")
BACKUP_SUFFIX = ".orig-unpatched"

sys.path.insert(0, HERE)
import bun_handler  # noqa: E402


def resolve_binary(target):
    """A path is used as-is; anything else is treated as a version under VERSIONS_DIR."""
    if os.path.sep in target or os.path.exists(target):
        return os.path.abspath(target)
    return os.path.join(VERSIONS_DIR, target)


def pristine_source(bin_path):
    """Return the bytes to patch FROM, capturing a one-time backup if needed.

    If a backup already exists it is authoritative (the live binary may already be
    patched from a previous run). Otherwise the live binary is pristine: back it up,
    then use it.
    """
    backup = bin_path + BACKUP_SUFFIX
    if os.path.exists(backup):
        print(f"using existing backup as pristine source: {backup}")
        with open(backup, "rb") as f:
            return f.read(), backup
    with open(bin_path, "rb") as f:
        data = f.read()
    with open(backup, "wb") as f:
        f.write(data)
    os.chmod(backup, 0o755)
    print(f"captured backup: {backup} ({len(data)} bytes)")
    return data, backup


def run_patcher(patcher, js_path):
    """Run the upstream patcher over an extracted JS file, in place."""
    proc = subprocess.run(
        ["node", patcher, "--file", js_path],
        capture_output=True, text=True,
    )
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"patcher failed (exit {proc.returncode})")


def install(bin_path, out_bytes):
    """Replace the live binary. Unlink first so a running process (which keeps its
    own inode) doesn't cause ETXTBSY on the write."""
    if os.path.exists(bin_path):
        os.unlink(bin_path)
    with open(bin_path, "wb") as f:
        f.write(out_bytes)
    os.chmod(bin_path, 0o755)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="version (e.g. 2.1.220) or path to a native claude binary")
    ap.add_argument("--patcher", default=DEFAULT_PATCHER, help=f"patch-claude-display.ts (default: {DEFAULT_PATCHER})")
    ap.add_argument("--no-install", action="store_true", help="build and verify only; do not touch the live binary")
    ap.add_argument("--keep", metavar="PATH", help="also write the patched binary to PATH")
    args = ap.parse_args(argv)

    bin_path = resolve_binary(args.target)
    if not os.path.exists(bin_path):
        raise SystemExit(f"binary not found: {bin_path}")
    if not os.path.exists(args.patcher):
        raise SystemExit(f"patcher not found: {args.patcher}")

    print(f"target binary: {bin_path}")
    fmt = bun_handler.detect_format(open(bin_path, "rb").read())
    print(f"format: {fmt}")

    data, backup = pristine_source(bin_path)
    if not bun_handler.can_handle(data):
        raise SystemExit("bun_handler cannot parse this binary")

    orig_js = bun_handler.extract_js(data)
    print(f"extracted JS: {len(orig_js)} bytes")

    with tempfile.TemporaryDirectory() as td:
        js_path = os.path.join(td, "content.js")
        with open(js_path, "wb") as f:
            f.write(orig_js)
        run_patcher(args.patcher, js_path)
        with open(js_path, "rb") as f:
            new_js = f.read()

    if new_js == orig_js:
        raise SystemExit("patcher made no changes -- aborting (unexpected)")

    out = bun_handler.repack_with_js(data, new_js)
    assert bun_handler.extract_js(out) == new_js, "repack round-trip mismatch"
    print(f"repacked: {len(out)} bytes (delta {len(out) - len(data):+d}, js delta {len(new_js) - len(orig_js):+d})")

    # Verify by executing the produced binary out-of-tree.
    with tempfile.NamedTemporaryFile(suffix="-claude-patched", delete=False) as tf:
        tf.write(out)
        probe = tf.name
    try:
        os.chmod(probe, 0o755)
        ver = subprocess.run([probe, "--version"], capture_output=True, text=True, timeout=120)
        print("--- patched --version ---")
        print(ver.stdout.strip())
        if "(patched)" not in ver.stdout:
            raise SystemExit("verification failed: '(patched)' not in --version output")
    finally:
        os.unlink(probe)

    if args.keep:
        with open(args.keep, "wb") as f:
            f.write(out)
        os.chmod(args.keep, 0o755)
        print(f"wrote: {args.keep}")

    if args.no_install:
        print("--no-install: live binary left unchanged.")
        return 0

    install(bin_path, out)
    live = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    print(f"installed. live `claude --version`: {live.stdout.strip()}")
    print(f"revert with: rm -f {bin_path} && cp {backup} {bin_path} && chmod 755 {bin_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
