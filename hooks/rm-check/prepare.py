#!/usr/bin/env python3
"""Prepare a Claude Code version's Bash permission check for rm-check-hook.mjs.

Usage: prepare.py <claude-executable> <version> <modules directory>
       prepare.py --discover-only <extracted.js>

The hook names the modules directory: ~/.cache/rm-check/<version>/modules-v<N>
(under $XDG_CACHE_HOME when it is set), where N is LAYOUT in rm-check-lib.mjs.
prepare.py extracts the bundle to claude.js next to that directory with
bun_handler/bun_handler.py, unless that file already exists, and writes the
bundle's modules into the directory. Each chunk that holds a candidate for one
of three roles gets an object __ccRmCheck with its candidates:

  - check: the function that decides a Bash command's permission. The Bash
    tool's module is the one module that holds the tool's searchHint
    "execute shell commands" (SEARCH_HINT), and the candidates are the
    functions that module passes an input and a context's shellPermission to,
    `F(input,ctx.shellPermission` (SHELL_PERMISSION_CALL);
  - runInCwd: the functions that run a callback with {cwd: dir} in an
    AsyncLocalStorage store, `function F(dir,fn){return S.run({cwd:dir},fn)}`
    with or without a call around dir (CWD_RUN);
  - setOriginalCwd: the functions that set the session's original working
    directory, `function F(dir){U({originalCwd:dir})}` (ORIGINAL_CWD_SET).

These anchors name only the facts the hook's use of the check depends on. A
role can have several candidates: on 2.1.309 the Bash tool passes its context
to the permission function, to a crash guard and to a sandbox-override step.
rm-check-lib.mjs picks the check among them by what each one does with
`rm -rf /`, and verifies the three together on synthetic directories when it
loads them, so a candidate that only looks right is refused there.

All anchors are tried on every run, and every anchor that finds nothing is
reported by name, so a vendor refactor shows each place to re-anchor at once.
They found their candidates on 2.1.303, 2.1.304, 2.1.305 and 2.1.309.
"""
import json
import os
import re
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "bun_handler"))

import bun_handler  # noqa: E402

ID = r"[A-Za-z_$][\w$]*"
SEARCH_HINT = 'searchHint:"execute shell commands"'
# The Bash tool passes its input and its shell permission context to the
# function that decides the permission. 2.1.305: return Lvr(n,s.shellPermission,
# void 0,cb); 2.1.309: return bJn(n,s.shellPermission,await _Rr(n,
# s.shellPermission,void 0,cb),"chosenVerdict"), where bJn handles a request to
# run outside the sandbox.
SHELL_PERMISSION_CALL = rf"(?<![\w$.])(?P<fn>{ID})\({ID},{ID}\.shellPermission[,)]"
# Claude Code runs a session's work, and a subagent's, inside F(dir, fn), which
# keeps {cwd: dir} in an AsyncLocalStorage store for fn and everything it calls;
# the permission check reads its working directory from that store. 2.1.305:
# function hg(e,o){return n.run({cwd:Un(e)},o)}; 2.1.309: function Dg(e,o){return
# n.run({cwd:On(e)},o)}.
CWD_RUN = (
    rf"function (?P<fn>{ID})\(({ID}),({ID})\)\{{return {ID}\.run\("
    rf"\{{cwd:(?:{ID}\()?\2\)?(?:,[^{{}}]*)?\}},\3\)\}}"
)
# The session's original working directory counts, with the --add-dir
# directories, as the session's working directories, and the check follows a cd
# only into those. The bundle sets it from process.cwd() when it loads; Claude
# Code changes it with F when it moves a session to another directory, such as
# a worktree. 2.1.305: function MP(e){vbn({originalCwd:e})}; 2.1.309: function
# mI(e){DEn({originalCwd:e})}.
ORIGINAL_CWD_SET = rf"function (?P<fn>{ID})\(({ID})\)\{{(?:return )?{ID}\(\{{originalCwd:\2\}}\)\}}"
# More candidates than this for one role means an anchor matches more than the
# functions it is meant for. rm-check-lib.mjs calls every check candidate once
# and runs every pair of directory functions through its self-test, so the
# number also bounds that work.
MAX_CANDIDATES = 8
EXPORT = "__ccRmCheck"
# Every layout's modules directory: "modules" before layouts were numbered,
# then "modules-v<LAYOUT>".
MODULES_DIR = re.compile(r"modules(?:-v\d+)?")
VERSION = re.compile(r"\d+\.\d+\.\d+")
BUNFS = b"/$bunfs/root/"


def module_name(name):
    return name.decode().split("/$bunfs/root/", 1)[-1]


def discover(data):
    """Find the candidates of the three roles in an extracted bundle.

    Returns ({"check", "runInCwd", "setOriginalCwd"} -> [{"chunk": module,
    "fn": name}], the bundle's module records). Exits naming every anchor that
    found no candidate, or too many."""
    records = bun_handler.split_extracted_js(data)
    hint_modules = []
    taken = []
    roles = {"check": [], "runInCwd": [], "setOriginalCwd": []}
    for _idx, name, contents in records:
        src = contents.decode("utf-8", errors="surrogateescape")
        chunk = module_name(name)
        if EXPORT in src:
            taken.append(chunk)
        if SEARCH_HINT in src:
            hint_modules.append(chunk)
            names = sorted({m.group("fn") for m in re.finditer(SHELL_PERMISSION_CALL, src)})
            roles["check"] += [{"chunk": chunk, "fn": fn} for fn in names]
        roles["runInCwd"] += [{"chunk": chunk, "fn": m.group("fn")} for m in re.finditer(CWD_RUN, src)]
        roles["setOriginalCwd"] += [{"chunk": chunk, "fn": m.group("fn")}
                                    for m in re.finditer(ORIGINAL_CWD_SET, src)]
    failures = []
    if taken:
        failures.append(f"EXPORT: the name {EXPORT}, which prepare.py appends to chunks, already "
                        f"occurs in {', '.join(taken)}")
    if len(hint_modules) != 1:
        failures.append(f"SEARCH_HINT: {len(hint_modules)} modules hold the Bash tool's "
                        f"{SEARCH_HINT}; expected exactly 1 {hint_modules}")
    anchors = {"check": ("SHELL_PERMISSION_CALL", "calls in the Bash tool's module that pass "
                         "an input and a context's shellPermission"),
               "runInCwd": ("CWD_RUN", "functions that run a callback with {cwd: dir} in "
                            "an AsyncLocalStorage store"),
               "setOriginalCwd": ("ORIGINAL_CWD_SET", "functions that set {originalCwd: dir}")}
    for role, (anchor, what) in anchors.items():
        found = roles[role]
        if role == "check" and len(hint_modules) != 1:
            continue
        if not found or len(found) > MAX_CANDIDATES:
            places = ", ".join(f"{c['fn']} in {c['chunk']}" for c in found)
            failures.append(f"{anchor}: found {len(found)} {what}; expected 1 to {MAX_CANDIDATES}"
                            + (f" ({places})" if places else ""))
    if failures:
        raise SystemExit("[rmcheck prepare] these anchors did not find their candidates:\n  "
                         + "\n  ".join(failures)
                         + f"\n(the anchors are constants in {os.path.abspath(__file__)})")
    return roles, records


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


def export_statement(names):
    """The statements appended to a chunk to put its candidates in __ccRmCheck.
    Each reference sits in its own try, so a name that is not bound at module
    level fails alone, when the chunk runs, instead of making the chunk fail to
    link; rm-check-lib.mjs then finds that name missing. A name that is not bound
    at module level but is a global, such as Map, gives the global, which the
    self-test then refuses like any other function that does not behave."""
    sets = "".join(f"try{{{EXPORT}[{json.dumps(n)}]={n}}}catch{{}}" for n in names)
    return f"\nexport const {EXPORT}={{}};{sets}\n".encode()


def write_modules(records, exports, write_dir, final_dir):
    """Write every module into write_dir, with bundle paths pointing at final_dir.

    exports maps a module name to the names of the candidates it holds."""
    absdir = final_dir.encode() + b"/"
    for _idx, name, contents in records:
        base = module_name(name)
        path = os.path.join(write_dir, base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = contents.replace(BUNFS, absdir)
        if base in exports:
            body += export_statement(exports[base])
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
            roles, _ = discover(f.read())
        print(json.dumps(roles))
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
    roles, records = discover(data)
    exports = {}
    for candidates in roles.values():
        for c in candidates:
            names = exports.setdefault(c["chunk"], [])
            if c["fn"] not in names:
                names.append(c["fn"])
    work = tempfile.mkdtemp(prefix="modules.", dir=vdir)
    try:
        # Import paths point at the final directory, so the tree only loads once
        # renamed there; the rename also makes a half-written tree invisible.
        write_modules(records, exports, work, final)
        with open(os.path.join(work, "manifest.json"), "w") as f:
            json.dump({"version": version, "exe": exe, "export": EXPORT, **roles}, f)
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
