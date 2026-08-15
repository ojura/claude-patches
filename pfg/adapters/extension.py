"""
extension adapter: the file + node-check backend for the engine on a VS Code-style
extension.js. The engine is I/O-free; this does the target I/O: read the pristine
source, run engine.apply, node --check the result, then write it. A .bak backup
plus the embedded patchset SIGNATURE give idempotency, stale-detect, and restore,
the same skeleton as skill/apply-patch-fg.py.
"""
import os
import shutil
import subprocess
import tempfile

from ..engine import apply, patch_state


def _node_check(js_text):
    fd, tmp = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(js_text)
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
    finally:
        os.unlink(tmp)
    if r.returncode != 0:
        raise SystemExit(f"[pfg] node --check FAILED on the patched output:\n{r.stderr}")


def apply_to_file(path, force=False):
    """Patch extension.js in place. Idempotency/staleness is decided by the engine's
    per-site patch_state (NOT presence of one signature): a fully-patched current file is
    a no-op, a stale one re-patches from the pristine backup, and a PARTIAL one fails loud
    rather than being silently skipped or blindly re-patched. Always applies on the
    pristine .pfg.bak, so re-patching never stacks."""
    if not os.path.exists(path):
        raise SystemExit(f"[pfg] not found: {path}")
    with open(path, encoding="utf-8") as f:
        js = f.read()
    state = patch_state(js, "extension")
    if state == "patched" and not force:
        print(f"[pfg] {path}: already patched (all sites current); nothing to do.")
        return
    if state == "partial" and not force:
        raise SystemExit(
            f"[pfg] {path}: inconsistent patch state (some pfg sites present, others "
            f"missing or at a different version). Restore the pristine .pfg.bak and "
            f"re-apply, or re-run with force.")
    # clean / stale / (patched|partial + force): apply on the PRISTINE .bak, never the
    # current file, so a stale or partial state re-patches cleanly instead of stacking.
    bak = path + ".pfg.bak"
    if not os.path.exists(bak):
        if state != "clean":
            raise SystemExit(
                f"[pfg] {path}: state '{state}' but no pristine backup at {bak} to restore "
                f"from; refusing to (re)apply onto an already-touched file.")
        shutil.copy2(path, bak)  # first patch: the current (clean) file IS the pristine source
        print(f"[pfg] backup -> {bak}")
    pristine = open(bak, encoding="utf-8").read()
    if patch_state(pristine, "extension") != "clean":
        raise SystemExit(
            f"[pfg] {bak}: backup is not pristine (it carries pfg sites); it cannot be the "
            f"restore source. Recover a clean extension.js before patching.")
    patched = apply(pristine)          # engine: pristine JS text -> patched JS text
    _node_check(patched)               # abort before writing if it does not parse
    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
    print(f"[pfg] patched {path} (+{len(patched) - len(pristine)} bytes, node --check OK)")
