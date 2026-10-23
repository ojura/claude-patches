#!/usr/bin/env python3
"""Prepare a Claude Code version's Bash permission check for rm-check-hook.mjs.

Usage: prepare.py <claude-executable> <version> <modules directory>
       prepare.py --discover-only <extracted.js>

The hook names the modules directory: ~/.cache/rm-check/<version>/modules-v<N>
(under $XDG_CACHE_HOME when it is set), where N is LAYOUT in rm-check-lib.mjs.
prepare.py extracts the bundle to claude.js next to that directory with
bun_handler/bun_handler.py, unless that file already exists, finds three things
in it with pfg.bundle, the structural matcher, writes the bundle's modules into
the directory, and exports what it found from the chunks that hold it:

  - tool (TOOL_HINT, TOOL_NAME): the Bash tool, a module-level
    `var T = F({name: N, ..., searchHint: "execute shell commands", ...})`
    whose N is the string "Bash", read through an import where there is one.
    The hook calls T.checkPermissions, the method Claude Code calls for the
    tool's permission verdict, with every wrapper Claude Code puts around the
    check;
  - runInCwd (CWD): a module-level `function F(dir, fn) {return S.run({cwd:
    dir}, fn)}`, with or without a call around dir, which runs fn with
    {cwd: dir} in an AsyncLocalStorage store. Claude Code runs a session's and
    a subagent's work inside it, and the check reads its working directory
    from that store;
  - setOriginalCwd (ORIGINAL_CWD): a module-level
    `function F(dir) {U({originalCwd: dir})}`, which sets the session's
    original working directory. With the --add-dir directories it makes up
    the session's working directories, and the check follows a cd only into
    those.

The text in NOMINATE picks the modules to parse, and the matcher decides from
their syntax and bindings. On 2.1.309 that parses 5 modules in about 4 s; the
whole bundle takes 11 s. rm-check-lib.mjs then verifies by behaviour, on
synthetic directories, that the three work together, so a match that only
looks right is refused there.

Every anchor is tried, and each one that finds nothing, or more than
MAX_CANDIDATES, is reported by name. pfg.bundle runs its helper on the node
that npm runs under, or else the first node on PATH. The anchors found their
matches on 2.1.303, 2.1.304, 2.1.305 and 2.1.309.
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
# The text that picks the modules to parse for each role.
NOMINATE = {"tool": f'"{TOOL_HINT}"', "runInCwd": ".run({cwd:", "setOriginalCwd": "({originalCwd:"}
NEEDLES = [pb.Needle("tool", "string", TOOL_HINT), pb.Needle("runInCwd", "property", "cwd"),
           pb.Needle("setOriginalCwd", "property", "originalCwd")]
ANCHORS = {
    "tool": f"TOOL_HINT: a module-level `var T = F({{name: N, ..., searchHint: \"{TOOL_HINT}\"}})` "
            f"with N the string \"{TOOL_NAME}\"",
    "runInCwd": "CWD: a module-level `function F(dir, fn) {return S.run({cwd: dir}, fn)}`",
    "setOriginalCwd": "ORIGINAL_CWD: a module-level `function F(dir) {U({originalCwd: dir})}`",
}
# More matches than this for one role means an anchor matches more than it is
# meant for. rm-check-lib.mjs calls every tool once and runs every pair of
# directory functions through its self-test, so the number also bounds that.
MAX_CANDIDATES = 8
EXPORT = "__ccRmCheck"
# Every layout's modules directory: "modules" before layouts were numbered,
# then "modules-v<LAYOUT>".
MODULES_DIR = re.compile(r"modules(?:-v\d+)?")
VERSION = re.compile(r"\d+\.\d+\.\d+")
BUNFS = b"/$bunfs/root/"


def module_name(name):
    return name.split("/$bunfs/root/", 1)[-1]


def up(handle, steps):
    """The handle of the node steps levels above the node at handle."""
    return ".".join(handle.split(".")[:-steps])


def kinds(chain, n):
    return [step["k"] for step in chain[:n]]


def is_module_statement(handle):
    return handle != "" and "." not in handle


def module_function(b, index, handle, params):
    """The syntax of the function declaration at handle when it is a statement
    of its module, takes params parameters and has one statement; else None."""
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
    """(module index, variable, syntax of the name property's value) of each
    `var T = F({..., searchHint: TOOL_HINT, ...})` statement in modules."""
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
    `function F(dir, fn) {return S.run({cwd: dir}, fn)}` in modules; dir may
    sit in a call."""
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
        # A parameter that is a destructuring pattern has no binding id, and a
        # free name has a null one, so each id must be present to compare.
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
    """The string a module-level name's initializer holds: ("value", text), or
    ("import", module name, exported name) when the name is imported, or None."""
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
    """("value", the string the module named spec exports as exported, when
    that module declares it with a string), or ("refused", why)."""
    index = next((i for i, (name, _) in texts.items() if name == spec), None)
    if index is None:
        return ("refused", f"there is no module {spec}")
    with pb.Bundle.from_sources([(index, spec, texts[index][1].decode("utf-8"))]) as b:
        try:
            target = b.export(index, exported)
        except pb.Unresolved as e:
            return ("refused", f"its name is reached through an import that prepare.py follows only into "
                               f"the module that declares it: {e}")
        if target[0] != "binding":
            return ("refused", f"{module_name(spec)} exports {exported} as a {target[0]}")
        declarations = b.scope_binding(target[1], target[2]).get("declarations", [])
        if len(declarations) != 1 or declarations[0].get("init") is None:
            return ("refused", f"{module_name(spec)} does not initialize {exported} where it declares it")
        value = string_value(b, target[1], b.syntax(target[1], declarations[0]["init"]["h"], 0))
        if value and value[0] == "value":
            return value
        return ("refused", f"{module_name(spec)}'s {exported} is not a string")


def discover(data):
    """Find the tool, runInCwd and setOriginalCwd in an extracted bundle.

    Returns ({"tool", "runInCwd", "setOriginalCwd"} -> [{"chunk": module,
    "name": local name}], the bundle's module records). Exits naming every
    anchor that found nothing, or too many."""
    records = bun_handler.split_extracted_js(data)
    texts = {index: (name.decode(), contents) for index, name, contents in records}
    failures = []
    taken = [module_name(name) for name, contents in texts.values() if EXPORT.encode() in contents]
    if taken:
        failures.append(f"EXPORT: the name {EXPORT}, which prepare.py adds to chunks, already occurs "
                        f"in {', '.join(taken)}")
    picked = {role: sorted(i for i, (_, contents) in texts.items() if text.encode() in contents)
              for role, text in NOMINATE.items()}
    for role, indexes in picked.items():
        if not indexes:
            failures.append(f"{ANCHORS[role]}: no module holds the text {NOMINATE[role]}")
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
                    why = name[1] if name and name[0] == "refused" else f"name {name[1]!r}" if name else "no name"
                    notes.append(f"{variable} in {module_name(texts[index][0])}: {why}")
        except pb.BackendError as e:
            raise SystemExit(f"[rmcheck prepare] pfg.bundle could not parse the bundle: {e}") from None
        for role, matches in roles.items():
            if not matches or len(matches) > MAX_CANDIDATES:
                places = ", ".join(f"{name} in {module_name(texts[index][0])}" for index, name in matches)
                if role == "tool" and notes:
                    places = ", ".join(([places] if places else []) + [f"refused {n}" for n in notes])
                modules = ", ".join(module_name(texts[i][0]) for i in picked[role])
                failures.append(f"{ANCHORS[role]}: found {len(matches)} in {modules}; expected 1 to "
                                f"{MAX_CANDIDATES}" + (f" ({places})" if places else ""))
    if failures:
        raise SystemExit("[rmcheck prepare] these anchors did not find what they look for:\n  "
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


def export_name(name):
    return f"{EXPORT}_{name}"


def export_statement(names):
    """The export appended to a chunk for its module-level names."""
    return ("\nexport{" + ",".join(f"{n} as {export_name(n)}" for n in names) + "};\n").encode()


def write_modules(records, exports, write_dir, final_dir):
    """Write every module into write_dir, with bundle paths pointing at final_dir.

    exports maps a module name to the module-level names it exports."""
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
        # Import paths point at the final directory, so the tree only loads once
        # renamed there; the rename also makes a half-written tree invisible.
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
    # A new version or layout is when older caches become unused.
    prune(final)


if __name__ == "__main__":
    main(sys.argv[1:])
