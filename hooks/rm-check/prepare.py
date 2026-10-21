#!/usr/bin/env python3
"""Prepare a Claude Code version's Bash permission check for rm-check-hook.mjs.

Usage: prepare.py <claude-executable> <version> <modules directory>
       prepare.py --discover-only <extracted.js>

The hook names the modules directory: ~/.cache/rm-check/<version>/modules-v<N>
(under $XDG_CACHE_HOME when it is set), where N is LAYOUT in rm-check-lib.mjs.
prepare.py extracts the bundle to claude.js next to that directory with
bun_handler/bun_handler.py, unless that file already exists, writes the
bundle's modules into the directory, and exports three functions from their
chunks:

  - the Bash tool's permission function, as __ccRmCheckBashPermission;
  - the function with which Claude Code runs a session's or a subagent's work
    in its working directory, as __ccRmCheckRunInCwd;
  - the function that sets the session's original working directory, which
    the permission check counts as a working directory of the session, as
    __ccRmCheckSetOriginalCwd.

All three are found by structure with the pfg matcher, so the minified names of
each new version are resolved without editing this file:

  - the Bash tool's `async checkPermissions(input, ctx)` starts by reading
    `input.command` and passes `<shell context>.shellPermission` to NAME; this
    anchor must match exactly once in the whole bundle (pfg.discovery._one);
  - the tool's `searchHint:"execute shell commands"` must be in the same module;
  - `function NAME(` must be defined exactly once in that module, and its body,
    brace-matched by pfg.jslex, must contain "dangerousRemoval";
  - `function RUN(dir,fn){return STORE.run({cwd:NORMALIZE(dir)},fn)}` must
    match exactly once in the whole bundle, and its module must define exactly
    one `function GET(){return STORE.getStore()?.cwd??SESSION_DIR()}` with the
    same STORE. RUN runs fn with dir in an AsyncLocalStorage store; GET returns
    the store's directory, or the session's when no store is set, and the
    permission check reads its working directory through it;
  - `function SET(dir){UPDATE({originalCwd:dir})}` must match exactly once in
    the whole bundle, and its module must define exactly one
    `function GET(){return OVERRIDES()?.originalCwd??PROJECT(SESSION()).originalCwd}`,
    the function through which the permission check reads it.

The permission function's anchors matched 2.1.281 to 2.1.296 and 2.1.303 to
2.1.305, the two directory functions' 2.1.303 to 2.1.305. 2.1.263 has a
different checkPermissions and is refused.

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
CHECK_EXPORT = "__ccRmCheckBashPermission"
# Claude Code runs a session's work, and a subagent's, inside RUN(dir, fn),
# which keeps {cwd: dir} in an AsyncLocalStorage store for fn and everything it
# calls. GET, through which the permission check reads its working directory,
# returns that cwd, or the session's directory when no store is set. In 2.1.305:
#   function hg(e,o){return n.run({cwd:Un(e)},o)}
#   function F_n(){return n.getStore()?.cwd??xk()}
CWD_RUN = (
    rf"function (?P<fn>{ID})\(({ID}),({ID})\)\{{return (?P<store>{ID})\.run\("
    rf"\{{cwd:{ID}\(\2\)\}},\3\)\}}"
)
# Filled in with ID and the store's name.
CWD_GET = r"function {id}\(\)\{{return {store}\.getStore\(\)\?\.cwd\?\?{id}\(\)\}}"
CWD_EXPORT = "__ccRmCheckRunInCwd"
# The session state holds the session's original working directory, which the
# permission check counts as a working directory of the session, together with
# the --add-dir directories. The bundle sets it from process.cwd() when it
# loads; Claude Code changes it with SET when it moves a session to another
# directory, such as a worktree. In 2.1.305:
#   function MP(e){vbn({originalCwd:e})}
#   function Ee(){return m()?.originalCwd??To(n()).originalCwd}
ORIGINAL_CWD_SET = rf"function (?P<fn>{ID})\(({ID})\)\{{{ID}\(\{{originalCwd:\2\}}\)\}}"
ORIGINAL_CWD_GET = (
    rf"function {ID}\(\)\{{return {ID}\(\)\?\.originalCwd\?\?{ID}\({ID}\(\)\)\.originalCwd\}}"
)
ORIGINAL_CWD_EXPORT = "__ccRmCheckSetOriginalCwd"
# Every layout's modules directory: "modules" before layouts were numbered,
# then "modules-v<LAYOUT>".
MODULES_DIR = re.compile(r"modules(?:-v\d+)?")
VERSION = re.compile(r"\d+\.\d+\.\d+")
BUNFS = b"/$bunfs/root/"


def module_name(name):
    return name.decode().split("/$bunfs/root/", 1)[-1]


def modules_containing(records, texts):
    """For each text, the (module name, source) of every module that contains it."""
    found = {t: [] for t in texts}
    for _idx, name, contents in records:
        src = contents.decode("utf-8", errors="surrogateescape")
        for t in texts:
            if t in src:
                found[t].append((module_name(name), src))
    return found


def discover(data):
    """Find the permission function and the two directory functions in an
    extracted bundle.

    Returns ({"check", "runInCwd", "setOriginalCwd"} -> {"chunk": module,
    "fn": name}, the bundle's module records)."""
    records = bun_handler.split_extracted_js(data)
    js = data.decode("utf-8", errors="surrogateescape")
    try:
        m = _one(js, CHECK_PERMISSIONS, "Bash checkPermissions -> permission function")
        _one(js, SEARCH_HINT, "Bash tool searchHint")
        run = _one(js, CWD_RUN, "function that runs work in a working directory")
        origin = _one(js, ORIGINAL_CWD_SET, "function that sets the original working directory")
    except SystemExit as e:
        raise SystemExit(f"{e}\n               (these anchors are CHECK_PERMISSIONS, SEARCH_HINT, "
                         f"CWD_RUN and ORIGINAL_CWD_SET in {os.path.abspath(__file__)})") from None
    # Each anchor matched once in the whole bundle, so its text is in exactly
    # one module.
    anchors = ((m.group(0), "Bash checkPermissions"), (run.group(0), "CWD_RUN"),
               (origin.group(0), "ORIGINAL_CWD_SET"))
    found = modules_containing(records, [text for text, _what in anchors])
    for text, what in anchors:
        if len(found[text]) != 1:
            raise SystemExit(f"[rmcheck prepare] the {what} anchor's text is in "
                             f"{len(found[text])} modules")
    fn = m.group("fn")
    chunk, src = found[m.group(0)][0]
    if 'searchHint:"execute shell commands"' not in src:
        raise SystemExit("[rmcheck prepare] the Bash checkPermissions and the Bash "
                         "searchHint are in different modules; re-anchor")
    s, e = jslex.find_function_span(src, fn)
    if '"dangerousRemoval"' not in src[s:e]:
        raise SystemExit(f"[rmcheck prepare] function {fn} has no \"dangerousRemoval\" "
                         "test; the check moved, re-anchor")
    run_chunk, run_src = found[run.group(0)][0]
    store = run.group("store")
    getters = re.findall(CWD_GET.format(id=ID, store=re.escape(store)), run_src)
    if len(getters) != 1:
        raise SystemExit(f"[rmcheck prepare] {run_chunk}, which defines {run.group('fn')}, "
                         f"defines {len(getters)} functions returning "
                         f"{store}.getStore()?.cwd; expected 1, re-anchor CWD_GET")
    origin_chunk, origin_src = found[origin.group(0)][0]
    getters = re.findall(ORIGINAL_CWD_GET, origin_src)
    if len(getters) != 1:
        raise SystemExit(f"[rmcheck prepare] {origin_chunk}, which defines {origin.group('fn')}, "
                         f"defines {len(getters)} functions returning the session's "
                         f"originalCwd; expected 1, re-anchor ORIGINAL_CWD_GET")
    return ({"check": {"chunk": chunk, "fn": fn},
             "runInCwd": {"chunk": run_chunk, "fn": run.group("fn")},
             "setOriginalCwd": {"chunk": origin_chunk, "fn": origin.group("fn")}}, records)


def server_dirs():
    """The prepared directories that running rm-check servers were started with."""
    dirs = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        for i, arg in enumerate(argv[:-1]):
            if arg.endswith(b"/rm-check-server.mjs"):
                dirs.add(os.fsdecode(argv[i + 1]))
    return dirs


def prune(final):
    """Remove module caches the hook no longer needs, other than those a running
    rm-check server was started with:

      - ~/.cache/rm-check/<version> for each version that is not final's, not
        installed under ~/.local/share/claude/versions/, and not the executable
        of a running process (an update deletes the binary of sessions that
        keep running it);
      - in the versions that stay, the modules directories of other layouts
        than final's;
      - the /tmp/claude-<version>-rmcheck directories that earlier revisions
        wrote."""
    vdir = os.path.dirname(final)
    base = os.path.dirname(vdir)
    needed = {os.path.basename(vdir)}
    installed = os.path.expanduser("~/.local/share/claude/versions")
    if os.path.isdir(installed):
        needed.update(os.listdir(installed))
    for pid in os.listdir("/proc"):
        try:
            link = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        m = re.search(r"/claude/versions/(\d+\.\d+\.\d+)(?: \(deleted\))?$", link)
        if m:
            needed.add(m.group(1))
    serving = server_dirs()

    def unused(p):
        return not any(d == p or d.startswith(p + os.sep) for d in serving)

    for v in os.listdir(base):
        vp = os.path.join(base, v)
        if not VERSION.fullmatch(v) or not os.path.isdir(vp):
            continue
        if v not in needed:
            if unused(vp):
                shutil.rmtree(vp, ignore_errors=True)
            continue
        for name in os.listdir(vp):
            p = os.path.join(vp, name)
            if MODULES_DIR.fullmatch(name) and name != os.path.basename(final) and unused(p):
                shutil.rmtree(p, ignore_errors=True)
    for name in os.listdir("/tmp"):
        p = os.path.join("/tmp", name)
        if (re.fullmatch(r"claude-\d+\.\d+\.\d+-rmcheck", name)
                and not os.path.islink(p) and os.stat(p).st_uid == os.getuid()):
            shutil.rmtree(p, ignore_errors=True)


def extract(exe, vdir):
    js_path = os.path.join(vdir, "claude.js")
    if os.path.exists(js_path):
        with open(js_path, "rb") as f:
            return f.read()
    with open(exe, "rb") as f:
        data = bun_handler.extract_js(f.read())
    fd, tmp = tempfile.mkstemp(prefix="claude.js.", dir=vdir)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.replace(tmp, js_path)
    return data


def write_modules(records, exports, write_dir, final_dir):
    """Write every module into write_dir, with bundle paths pointing at final_dir.

    exports maps a module name to the [(function, export name)] it gets."""
    absdir = final_dir.encode() + b"/"
    for _idx, name, contents in records:
        base = module_name(name)
        path = os.path.join(write_dir, base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = contents.replace(BUNFS, absdir)
        if base in exports:
            names = ",".join(f"{fn} as {export}" for fn, export in exports[base])
            body += f"\nexport{{{names}}};\n".encode()
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
            places, _ = discover(f.read())
        print(json.dumps(places))
        return
    exe, version, final = argv
    if not VERSION.fullmatch(version):
        raise SystemExit(f"[rmcheck prepare] bad version {version!r}")
    vdir = os.path.dirname(final)
    # prune removes directories next to and above final, so final must be where
    # the hook keeps prepared modules.
    if (not os.path.isabs(final) or not MODULES_DIR.fullmatch(os.path.basename(final))
            or os.path.basename(vdir) != version
            or os.path.basename(os.path.dirname(vdir)) != "rm-check"):
        raise SystemExit(f"[rmcheck prepare] {final!r} is not an absolute "
                         f".../rm-check/{version}/modules-v<N> directory")
    if os.path.exists(os.path.join(final, "manifest.json")):
        return
    os.makedirs(vdir, exist_ok=True)
    data = extract(exe, vdir)
    places, records = discover(data)
    names = {"check": CHECK_EXPORT, "runInCwd": CWD_EXPORT, "setOriginalCwd": ORIGINAL_CWD_EXPORT}
    exports = {}
    for key, place in places.items():
        exports.setdefault(place["chunk"], []).append((place["fn"], names[key]))
    work = tempfile.mkdtemp(prefix="modules.", dir=vdir)
    try:
        # Import paths point at the final directory, so the tree only loads once
        # renamed there; the rename also makes a half-written tree invisible.
        write_modules(records, exports, work, final)
        with open(os.path.join(work, "manifest.json"), "w") as f:
            json.dump({"version": version, "exe": exe,
                       **{key: dict(place, export=names[key]) for key, place in places.items()}}, f)
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
    # A new version or layout is when older caches become unused.
    prune(final)


if __name__ == "__main__":
    main(sys.argv[1:])
