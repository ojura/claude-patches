#!/usr/bin/env python3
"""Prepare a Claude Code version's Bash permission check for rm-check-hook.mjs.

Usage: prepare.py <claude-executable> <version>
       prepare.py --discover-only <extracted.js>

Extracts the bundle to /tmp/claude-<version>.js with bun_handler/bun_handler.py
(unless that file already exists, belongs to this user, and only this user can
write to it), writes its modules to
/tmp/claude-<version>-rmcheck/, and exports the Bash tool's permission
function from its chunk as __ccRmCheckBashPermission. The function is found by
structure with the pfg matcher, so the minified names of each new version are
resolved without editing this file:

  - the Bash tool's `async checkPermissions(input, ctx)` starts by reading
    `input.command` and passes `<shell context>.shellPermission` to NAME; this
    anchor must match exactly once in the whole bundle (pfg.discovery._one);
  - the tool's `searchHint:"execute shell commands"` must be in the same module;
  - `function NAME(` must be defined exactly once in that module, and its body,
    brace-matched by pfg.jslex, must contain "dangerousRemoval".

Checked on 2.1.281 to 2.1.296; 2.1.263 has a different checkPermissions and is
refused.

Any mismatch exits non-zero with pfg's message naming the anchor, so a vendor
refactor is reported instead of producing a wrong binding.
"""
import json
import os
import re
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "bun_handler"))

import bun_handler  # noqa: E402
from pfg import jslex  # noqa: E402
from pfg.discovery import _one  # noqa: E402

ID = r"[A-Za-z_$][\w$]*"
# The Bash tool's method starts `let s=X(e,ctx),h=Y(input.command);if(h){`, which
# the Monitor tool's checkPermissions (also passing .shellPermission) lacks.
# 2.1.292 and later end with `return NAME(input,s.shellPermission,void 0,cb)`;
# 2.1.281 to 2.1.288 with `let r=await NAME(input,s.shellPermission);`.
CHECK_PERMISSIONS = (
    rf"async checkPermissions\(({ID}),({ID})\)\{{let ({ID})={ID}\({ID},\2\),"
    rf"({ID})={ID}\(\1\.command\);if\(\4\)\{{"
    rf"(?:(?!async |function ).){{0,300}}?(?:return |let {ID}=await )"
    rf"(?P<fn>{ID})\(\1,\3\.shellPermission[,)]"
)
SEARCH_HINT = r'searchHint:"execute shell commands"'
EXPORT_NAME = "__ccRmCheckBashPermission"
BUNFS = b"/$bunfs/root/"


def distrust_reason(p):
    """Return why p cannot be trusted, or None when this user owns it and only
    this user can write to it."""
    st = os.stat(p)
    if st.st_uid != os.getuid():
        return f"{p} belongs to uid {st.st_uid}, not to this user (uid {os.getuid()})"
    if st.st_mode & 0o022:
        return (f"{p} has mode {st.st_mode & 0o777:o}, so users other than its owner "
                "can write to it")
    return None


def discover(data):
    """Return (module_name, function_name, records) for the Bash permission function."""
    records = bun_handler.split_extracted_js(data)
    js = data.decode("utf-8", errors="surrogateescape")
    try:
        m = _one(js, CHECK_PERMISSIONS, "Bash checkPermissions -> permission function")
        _one(js, SEARCH_HINT, "Bash tool searchHint")
    except SystemExit as e:
        raise SystemExit(f"{e}\n               (these anchors are CHECK_PERMISSIONS and "
                         f"SEARCH_HINT in {os.path.abspath(__file__)})") from None
    fn = m.group("fn")
    # The anchor matched once in the whole bundle, so its text is in exactly one
    # module; the tool's searchHint must be in that module too.
    owners = []
    for _idx, name, contents in records:
        src = contents.decode("utf-8", errors="surrogateescape")
        if m.group(0) in src:
            owners.append((name, src))
    if len(owners) != 1:
        raise SystemExit(f"[rmcheck prepare] the anchor text is in {len(owners)} modules")
    name, src = owners[0]
    if 'searchHint:"execute shell commands"' not in src:
        raise SystemExit("[rmcheck prepare] the Bash checkPermissions and the Bash "
                         "searchHint are in different modules; re-anchor")
    s, e = jslex.find_function_span(src, fn)
    if '"dangerousRemoval"' not in src[s:e]:
        raise SystemExit(f"[rmcheck prepare] function {fn} has no \"dangerousRemoval\" "
                         "test; the check moved, re-anchor")
    return name.decode().split("/$bunfs/root/", 1)[-1], fn, records


def extract(exe, version):
    js_path = f"/tmp/claude-{version}.js"
    # The file is only a cache. Another tool may have written it with a umask
    # that leaves it group-writable; such a copy is extracted again and replaced.
    if os.path.exists(js_path) and distrust_reason(js_path) is None:
        with open(js_path, "rb") as f:
            return f.read()
    with open(exe, "rb") as f:
        data = bun_handler.extract_js(f.read())
    fd, tmp = tempfile.mkstemp(prefix=f"claude-{version}.js.", dir="/tmp")
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    try:
        os.replace(tmp, js_path)
    except PermissionError:
        # /tmp is sticky, so another user's file cannot be replaced; the
        # extracted data is still used, without a cache.
        os.unlink(tmp)
    return data


def write_modules(records, chunk, fn, write_dir, final_dir):
    """Write every module into write_dir, with bundle paths pointing at final_dir."""
    absdir = final_dir.encode() + b"/"
    for _idx, name, contents in records:
        base = name.decode().split("/$bunfs/root/", 1)[-1]
        path = os.path.join(write_dir, base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = contents.replace(BUNFS, absdir)
        if base == chunk:
            body += f"\nexport{{{fn} as {EXPORT_NAME}}};\n".encode()
        with open(path, "wb") as f:
            f.write(body)
    # Assets the bundle keeps outside its source modules (text, docs, wasm) are
    # referenced by path; empty files let the module graph load.
    ref = re.compile(re.escape(absdir) + rb"([A-Za-z0-9_./@+-]+)")
    for root, _dirs, files in os.walk(write_dir):
        for f in files:
            with open(os.path.join(root, f), "rb") as fh:
                text = fh.read()
            for m in ref.finditer(text):
                p = os.path.join(write_dir, m.group(1).decode())
                if not os.path.exists(p):
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    open(p, "wb").close()


def main(argv):
    if argv[:1] == ["--discover-only"]:
        with open(argv[1], "rb") as f:
            chunk, fn, _ = discover(f.read())
        print(json.dumps({"chunk": chunk, "fn": fn}))
        return
    exe, version = argv
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"[rmcheck prepare] bad version {version!r}")
    final = f"/tmp/claude-{version}-rmcheck"
    if os.path.exists(os.path.join(final, "manifest.json")):
        reason = distrust_reason(final)
        if reason is not None:
            raise SystemExit(f"[rmcheck prepare] refusing to load modules from {final}: "
                             f"{reason}. Delete it and the hook prepares it again.")
        return
    data = extract(exe, version)
    chunk, fn, records = discover(data)
    work = tempfile.mkdtemp(prefix=f"claude-{version}-rmcheck.", dir="/tmp")
    try:
        # Import paths point at the final directory, so the tree only loads once
        # renamed there; the rename also makes a half-written tree invisible.
        write_modules(records, chunk, fn, work, final)
        with open(os.path.join(work, "manifest.json"), "w") as f:
            json.dump({"version": version, "exe": exe, "chunk": chunk, "fn": fn,
                       "export": EXPORT_NAME}, f)
        try:
            os.rename(work, final)
        except OSError:
            if not os.path.exists(os.path.join(final, "manifest.json")):
                raise
            shutil.rmtree(work)  # another hook prepared it first
    except BaseException:
        if os.path.exists(work):
            shutil.rmtree(work)
        raise


if __name__ == "__main__":
    main(sys.argv[1:])
