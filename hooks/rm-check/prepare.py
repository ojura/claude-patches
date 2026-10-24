#!/usr/bin/env python3
"""Prepare a Claude Code version's Bash permission check for rm-check-hook.mjs.

Usage: prepare.py <claude-executable> <version> <modules directory>
       prepare.py --discover-only <extracted.js>

The hook passes the modules directory as the third argument:
~/.cache/rm-check/<version>/modules-v<N> (under $XDG_CACHE_HOME when it is set),
where N is LAYOUT in rm-check-lib.mjs.
prepare.py extracts the bundle with bun_handler/bun_handler.py to claude.js
next to that directory, unless that file already exists. It finds three things
in the bundle with pfg.bundle, writes the bundle's modules into the directory,
and appends an export statement to each module that declares one of the names
it found. Error messages refer to each anchor by the label in parentheses:

  - tool (TOOL_HINT): the Bash tool, a module-level
    `var T = F({name: N, ..., searchHint: "execute shell commands", ...})`
    where N is the string "Bash" (TOOL_NAME), or a variable initialized with
    it, either in the same module or in the module that N is imported from.
    The hook calls T.checkPermissions, the method that Claude Code calls for
    the tool's permission verdict, so the steps that Claude Code wraps around
    the check run too;
  - runInCwd (CWD): a module-level `function F(dir, fn) {return S.run({cwd:
    dir}, fn)}`, also with dir passed through a call, as in {cwd: G(dir)}. It
    runs fn with {cwd: dir} as the store of the AsyncLocalStorage S. Claude
    Code runs the work of the session and of each subagent inside this
    function, and the check reads its working directory from that store;
  - setOriginalCwd (ORIGINAL_CWD): a module-level
    `function F(dir) {U({originalCwd: dir})}`, which sets the session's
    original working directory. Together with the --add-dir directories, that
    directory forms the session's working directories, and the check follows
    a cd only into them.

prepare.py parses only the modules that contain one of the texts in NOMINATE,
and the anchors test the syntax and bindings that pfg.bundle reports for them.
On 2.1.309 these are 5 modules, which take about 4 s to parse, where the whole
bundle takes 11 s. rm-check-lib.mjs then calls the three on made-up
directories and checks the verdicts, so it refuses a match that looks right
but gives the wrong verdicts.

prepare.py tries every anchor, and when one fails, the error names each anchor
that found 0 matches or more than MAX_CANDIDATES. pfg.bundle runs its helper on
the node that npm runs under, or else the first node on PATH. The anchors found
their matches on 2.1.303, 2.1.304, 2.1.305 and 2.1.309.
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
from pfg import bundle as pb  # noqa: E402

TOOL_HINT = "execute shell commands"
TOOL_NAME = "Bash"
# For each role, the text that a module must contain for prepare.py to parse it.
NOMINATE = {"tool": f'"{TOOL_HINT}"', "runInCwd": ".run({cwd:", "setOriginalCwd": "({originalCwd:"}
NEEDLES = [pb.Needle("tool", "string", TOOL_HINT), pb.Needle("runInCwd", "property", "cwd"),
           pb.Needle("setOriginalCwd", "property", "originalCwd")]
ANCHORS = {
    "tool": f"TOOL_HINT: a module-level `var T = F({{name: N, ..., searchHint: \"{TOOL_HINT}\"}})` "
            f"with N the string \"{TOOL_NAME}\"",
    "runInCwd": "CWD: a module-level `function F(dir, fn) {return S.run({cwd: dir}, fn)}`",
    "setOriginalCwd": "ORIGINAL_CWD: a module-level `function F(dir) {U({originalCwd: dir})}`",
}
# More matches than this for one role means that its anchor also matches code
# it is not meant for. rm-check-lib.mjs calls every tool once and runs its
# self-test on every pair of directory functions, so this number also limits
# the self-test to 8 tools and 64 pairs.
MAX_CANDIDATES = 8
EXPORT = "__ccRmCheck"
# The name of the modules directory in every layout: "modules" from before the
# layouts had numbers, and "modules-v<LAYOUT>" since.
MODULES_DIR = re.compile(r"modules(?:-v\d+)?")
VERSION = re.compile(r"\d+\.\d+\.\d+")
BUNFS = b"/$bunfs/root/"


def module_name(name):
    return name.split("/$bunfs/root/", 1)[-1]


def up(handle, steps):
    """The handle of the ancestor that is steps levels above the node at handle."""
    return ".".join(handle.split(".")[:-steps])


def kinds(chain, n):
    return [step["k"] for step in chain[:n]]


def is_module_statement(handle):
    return handle != "" and "." not in handle


def module_function(b, index, handle, params):
    """The syntax of the function declaration at handle when it is a statement
    of its module, has params parameters and a body of one statement, and None
    otherwise."""
    if not is_module_statement(handle):
        return None
    fn = b.syntax(index, handle, 4)
    if (fn["k"] != "FunctionDeclaration" or len(fn.get("parameters", [])) != params
            or len(fn["body"].get("statements", [])) != 1):
        return None
    return fn


def parameter_bindings(fn):
    return [p["name"].get("b") for p in fn["parameters"]]


def tool_matches(b, modules):
    """(module index, variable name, syntax of the name property's value) of
    each module-level `var T = F({..., searchHint: TOOL_HINT, ...})` statement
    in modules. The third element is None unless the object has exactly one
    name property."""
    found = []
    for index, hit in b.hits("tool"):
        chain = hit["chain"]
        if index not in modules:
            continue
        if kinds(chain, 6) != ["PropertyAssignment", "ObjectLiteralExpression", "CallExpression",
                               "VariableDeclaration", "VariableDeclarationList", "VariableStatement"]:
            continue
        if (chain[0].get("key") != "searchHint" or chain[2]["f"] != "arguments"
                or not is_module_statement(up(hit["h"], 6))):
            continue
        variable = b.syntax(index, up(hit["h"], 4), 1)["name"]["text"]
        names = [p for p in b.syntax(index, up(hit["h"], 2), 2)["properties"] if p.get("key") == "name"]
        found.append((index, variable, names[0].get("initializer") if len(names) == 1 else None))
    return found


def run_matches(b, modules):
    """(module index, name) of each module-level
    `function F(dir, fn) {return S.run({cwd: dir}, fn)}` in modules. The value
    of cwd may also be dir passed to a one-argument call, as in {cwd: G(dir)}."""
    found = []
    for index, hit in b.hits("runInCwd"):
        chain = hit["chain"]
        if index not in modules:
            continue
        if kinds(chain, 4) != ["PropertyAssignment", "ObjectLiteralExpression", "CallExpression",
                               "ReturnStatement"]:
            continue
        if chain[0]["f"] != "name" or chain[2]["f"] != "arguments" or chain[2].get("i") != 0 \
                or chain[2].get("argc") != 2:
            continue
        fn = module_function(b, index, up(hit["h"], 6), 2)
        if fn is None:
            continue
        directory, callback = parameter_bindings(fn)
        call = b.syntax(index, up(hit["h"], 3), 3)
        if call["expression"]["k"] != "PropertyAccessExpression" or call["expression"]["name"]["text"] != "run":
            continue
        # A parameter that is a destructuring pattern does not have a binding
        # id, and a free name has a null one. Both read as None here and would
        # compare equal, so a match also requires the parameter to have an id.
        second = call["arguments"][1]
        if second["k"] != "Identifier" or not callback or second.get("b") != callback:
            continue
        value = b.syntax(index, up(hit["h"], 1), 3)["initializer"]
        if value["k"] == "CallExpression" and len(value.get("arguments", [])) == 1:
            value = value["arguments"][0]
        if value["k"] == "Identifier" and directory and value.get("b") == directory:
            found.append((index, fn["name"]["text"]))
    return found


def origin_matches(b, modules):
    """(module index, name) of each module-level
    `function F(dir) {U({originalCwd: dir})}` in modules."""
    found = []
    for index, hit in b.hits("setOriginalCwd"):
        chain = hit["chain"]
        if index not in modules:
            continue
        if kinds(chain, 4) != ["PropertyAssignment", "ObjectLiteralExpression", "CallExpression",
                               "ExpressionStatement"]:
            continue
        if chain[0]["f"] != "name" or chain[1].get("n") != 1 or chain[2]["f"] != "arguments" \
                or chain[2].get("argc") != 1:
            continue
        fn = module_function(b, index, up(hit["h"], 6), 1)
        if fn is None:
            continue
        value = b.syntax(index, up(hit["h"], 1), 2)["initializer"]
        directory = parameter_bindings(fn)[0]
        if value["k"] == "Identifier" and directory and value.get("b") == directory:
            found.append((index, fn["name"]["text"]))
    return found


def string_value(b, index, node):
    """The string that node resolves to: ("value", text) for a string literal,
    or for a module-level name declared once with an initializer that resolves
    to a string; ("import", module name, exported name) for a module-level name
    that is imported; None otherwise."""
    if node is None:
        return None
    if node["k"] == "StringLiteral":
        return ("value", node.get("value"))
    if node["k"] != "Identifier" or not node.get("b"):
        return None
    declarations = b.binding(index, node["b"]).get("declarations", [])
    if len(declarations) != 1 or not declarations[0].get("module"):
        return None
    declaration = declarations[0]
    if declaration["kind"] == "import":
        return ("import", declaration["import"]["spec"], declaration["import"]["imported"])
    init = declaration.get("init")
    if declaration["kind"] not in ("var", "let", "const") or init is None:
        return None
    return string_value(b, index, b.syntax(index, init["h"], 0))


def exported_string(texts, spec, exported):
    """("value", text) when the module named spec exports the name exported and
    declares it itself with a string as its initializer, and ("refused", the
    reason) otherwise."""
    index = next((i for i, (name, _) in texts.items() if name == spec), None)
    if index is None:
        return ("refused", f"the bundle does not contain a module {spec}")
    with pb.Bundle.from_sources([(index, spec, texts[index][1].decode("utf-8"))]) as b:
        try:
            target = b.export(index, exported)
        except pb.Unresolved as e:
            return ("refused", f"prepare.py follows an imported name only into the module it is imported "
                               f"from, and {exported} cannot be resolved within {module_name(spec)} alone: {e}")
        if target[0] != "binding":
            return ("refused", f"the export {exported} of {module_name(spec)} is of kind {target[0]}, and "
                               "prepare.py reads only variables")
        declarations = b.scope_binding(target[1], target[2]).get("declarations", [])
        if len(declarations) != 1 or declarations[0].get("init") is None:
            return ("refused", f"{module_name(spec)} declares {exported} more than once, or without an initializer")
        value = string_value(b, target[1], b.syntax(target[1], declarations[0]["init"]["h"], 0))
        if value and value[0] == "value":
            return value
        return ("refused", f"{exported} in {module_name(spec)} is not initialized with a string")


def discover(data):
    """Find the tool, runInCwd and setOriginalCwd in an extracted bundle.

    Returns ({"tool", "runInCwd", "setOriginalCwd"} -> [{"chunk": module,
    "name": local name}], the bundle's module records). Raises SystemExit with
    a message that names every anchor that found 0 matches or more than
    MAX_CANDIDATES, and EXPORT when a module already contains that name."""
    records = bun_handler.split_extracted_js(data)
    texts = {index: (name.decode(), contents) for index, name, contents in records}
    failures = []
    taken = [module_name(name) for name, contents in texts.values() if EXPORT.encode() in contents]
    if taken:
        failures.append(f"EXPORT: prepare.py exports the names it finds as {EXPORT}_<name>, and the text "
                        f"{EXPORT} already occurs in {', '.join(taken)}")
    picked = {role: sorted(i for i, (_, contents) in texts.items() if text.encode() in contents)
              for role, text in NOMINATE.items()}
    for role, indexes in picked.items():
        if not indexes:
            failures.append(f"{ANCHORS[role]}: the text {NOMINATE[role]} does not occur in any module")
    roles = {role: [] for role in NOMINATE}
    if all(picked.values()):
        sources = [(i, texts[i][0], texts[i][1].decode("utf-8")) for i in sorted(set().union(*picked.values()))]
        try:
            with pb.Bundle.from_sources(sources, NEEDLES) as b:
                tools = [(index, variable, string_value(b, index, name))
                         for index, variable, name in tool_matches(b, picked["tool"])]
                roles["runInCwd"] = run_matches(b, picked["runInCwd"])
                roles["setOriginalCwd"] = origin_matches(b, picked["setOriginalCwd"])
            notes = []
            for index, variable, name in tools:
                if name and name[0] == "import":
                    name = exported_string(texts, name[1], name[2])
                if name == ("value", TOOL_NAME):
                    roles["tool"].append((index, variable))
                else:
                    why = name[1] if name and name[0] == "refused" else f"name {name[1]!r}" if name else "name unknown"
                    notes.append(f"{variable} in {module_name(texts[index][0])}: {why}")
        except pb.BackendError as e:
            raise SystemExit(f"[rmcheck prepare] pfg.bundle's node helper failed: {e}") from None
        for role, matches in roles.items():
            if not matches or len(matches) > MAX_CANDIDATES:
                places = ", ".join(f"{name} in {module_name(texts[index][0])}" for index, name in matches)
                if role == "tool" and notes:
                    places = ", ".join(([places] if places else []) + [f"refused {n}" for n in notes])
                modules = ", ".join(module_name(texts[i][0]) for i in picked[role])
                failures.append(f"{ANCHORS[role]}: found {len(matches)} in {modules}, where it should find 1 "
                                f"to {MAX_CANDIDATES}" + (f" ({places})" if places else ""))
    if failures:
        raise SystemExit("[rmcheck prepare] these anchors failed:\n  "
                         + "\n  ".join(failures)
                         + f"\n(the anchors are in {os.path.abspath(__file__)})")
    return ({role: [{"chunk": module_name(texts[index][0]), "name": name} for index, name in matches]
             for role, matches in roles.items()}, records)


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
        of a running process (a session keeps running its version after an
        update deletes that version's binary);
      - in the versions that stay, the modules directories of layouts other
        than final's;
      - the /tmp/claude-<version>-rmcheck directories that earlier revisions
        of the hook wrote."""
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


def export_name(name):
    return f"{EXPORT}_{name}"


def export_statement(names):
    """The export statement that prepare.py appends to a module to export the
    module-level names in names."""
    return ("\nexport{" + ",".join(f"{n} as {export_name(n)}" for n in names) + "};\n").encode()


def write_modules(records, exports, write_dir, final_dir):
    """Write every module into write_dir, with each /$bunfs/root/ path in it
    pointing at final_dir instead.

    exports maps a module name to the module-level names to export from it."""
    absdir = final_dir.encode() + b"/"
    for _idx, name, contents in records:
        base = module_name(name.decode())
        path = os.path.join(write_dir, base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = contents.replace(BUNFS, absdir)
        if base in exports:
            body += export_statement(exports[base])
        with open(path, "wb") as f:
            f.write(body)
    # The modules refer by path to assets that the bundle keeps outside its
    # source modules (text, docs, wasm). An empty file at each such path is
    # enough for the modules to load.
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
    for found in roles.values():
        for place in found:
            names = exports.setdefault(place["chunk"], [])
            if place["name"] not in names:
                names.append(place["name"])
    manifest = {"version": version, "exe": exe,
                **{role: [dict(place, export=export_name(place["name"])) for place in found]
                   for role, found in roles.items()}}
    work = tempfile.mkdtemp(prefix="modules.", dir=vdir)
    try:
        # The import paths in the modules point at final, so the tree only loads
        # once it is renamed there. Because the rename is atomic, other hooks see
        # the tree at final only when it is complete.
        write_modules(records, exports, work, final)
        with open(os.path.join(work, "manifest.json"), "w") as f:
            json.dump(manifest, f)
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
    # A cache becomes unused when a new version or layout replaces it, so prune
    # runs after each preparation.
    prune(final)


if __name__ == "__main__":
    main(sys.argv[1:])
