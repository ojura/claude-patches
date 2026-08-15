#!/usr/bin/env python3
"""
Codegen: compile src/pfg-core.js (readable, unit-tested ESM source) into a per-target
MINIMAL $pfg block for the bundle. The core is the single source of truth for the
compaction/lineage primitives; the codegen is its per-target DISTRIBUTION mechanism.

WHY: Patches D/J/K re-decide "is this a compaction boundary", "which edge does the walk
follow", "is this a conversation root" inline at many sites, and the copies drift.
pfg-core.js defines each ONCE; this codegen emits it so every splice references
$pfg.<name> instead of re-deciding the concept, making a second definition unwritable.

WHY PER-TARGET + USAGE-DERIVED: a target should carry only the primitives it actually
uses. The extension (d1e/i1e) references most of the core; the webview render fn needs a
tiny subset (e.g. just the PFGK1 envelope prefix). Emitting the WHOLE core into the
webview would ship ~250 lines of lineage logic for one constant. So the codegen greps
each target's source for its $pfg.<member> refs, computes the transitive closure over the
core's own member dependency graph, and emits ONLY that closure, exposing ONLY the direct
refs. This is derive_deps-per-target applied to the core block.

DELIBERATELY THIN. It splits pfg-core.js on its top-level `const` declarations, keeps each
member's exact source span (doc comment included), strips the ESM `export ` keyword, and
wraps the needed subset in one namespaced IIFE. It does NOT parse the bundle or bind to
the minifier's churning local names. Emitted readable, not minified (house rule: injected
patch code stays reviewable).

Usage:
  python3 util/pfg-codegen.py [--target T]   # print the $pfg block for target T (default extension)
  python3 util/pfg-codegen.py --check [--target T]  # emit + validate it parses (node)
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SRC = os.path.join(REPO_ROOT, "src", "pfg-core.js")

# The consumers of $pfg per target: the source bodies the engine injects for that target,
# grepped for their $pfg.<member> refs. The extension injects d1e + i1e; the webview
# injects render.js. (Kept in lockstep with engine/discovery SRC_FILES; the two describe
# the same per-target source surface, one for $pfg, one for the _pv_ deps.)
CONSUMERS = {
    "extension": ("d1e.js", "i1e.js"),
    "webview": ("render.js",),
}

# A member declaration at column 0: `export const NAME =` or an internal `const NAME =`.
DECL_RE = re.compile(r"^(export )?const (\w+)\s*=", re.M)
# A consumer's reference to a core member.
PFG_MEMBER_RE = re.compile(r"\$pfg\.([A-Za-z_$][\w$]*)")

BEGIN = "// ==== pfg-core (GENERATED from src/pfg-core.js by util/pfg-codegen.py) ===="
END = "// ==== end pfg-core ===="


def read_src():
    with open(SRC, encoding="utf-8") as f:
        return f.read()


def _member_blocks(src):
    """Split `src` into (header, [(name, exported, text)]). Each member's `text` is its
    EXACT source span, its leading `//` doc comment through its value, with the ESM
    `export ` keyword stripped, so concatenating every block reproduces the file (minus the
    module header, which is returned separately). A member's leading comment is the run of
    `//`-comment / blank lines directly above its `const`, bounded by the previous member."""
    ms = list(DECL_RE.finditer(src))
    if not ms:
        raise SystemExit("pfg-codegen: no `const` declarations found in " + SRC)

    def block_start(i):
        decl_start = ms[i].start()
        floor = ms[i - 1].start() if i > 0 else 0
        pos = decl_start
        while pos > floor:
            line_start = src.rfind("\n", floor, pos - 1) + 1
            if line_start < floor:
                line_start = floor
            stripped = src[line_start:pos].strip()
            if not (stripped.startswith("//") or stripped == ""):
                break
            pos = line_start
        return pos

    starts = [block_start(i) for i in range(len(ms))]
    header = src[: starts[0]]
    blocks = []
    for i, m in enumerate(ms):
        s = starts[i]
        e = starts[i + 1] if i + 1 < len(ms) else len(src)
        text = re.sub(r"^export ", "", src[s:e], count=1, flags=re.M)
        blocks.append((m.group(2), bool(m.group(1)), text))
    return header, blocks


def _dep_graph(blocks):
    """member name -> set of OTHER member names its VALUE references by bare name (the core
    members call each other unqualified, e.g. walkToRoot -> edge/isOrigin/classifyRoot). The
    leading doc comment is excluded from the scan, so a member merely NAMED in prose is not
    pulled into the closure. Word-boundaried, so `isGhost` does not match `isGhostUuid`."""
    names = [b[0] for b in blocks]
    graph = {}
    for name, _exported, text in blocks:
        decl = re.search(r"^const \w+\s*=", text, re.M)
        value = text[decl.start():] if decl else text
        graph[name] = {
            other for other in names
            if other != name and re.search(rf"\b{re.escape(other)}\b", value)
        }
    return graph


def _closure(direct, graph):
    """Transitive closure of `direct` over `graph` (every member reachable from a directly
    referenced one). Downward-closed, so emitting the closure never leaves a bare reference
    unbound."""
    seen, stack = set(), list(direct)
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(graph.get(n, ()))
    return seen


def _pfg_refs(target):
    """The set of $pfg.<member> a target's consumer source references (its DIRECT usage)."""
    refs = set()
    for fn in CONSUMERS.get(target, ()):
        path = os.path.join(REPO_ROOT, "src", fn)
        try:
            with open(path, encoding="utf-8") as f:
                refs |= set(PFG_MEMBER_RE.findall(f.read()))
        except OSError:
            continue
    return refs


def check_membership(target, exported):
    """Per-target BIJECTION between the emitted subset and the target's $pfg usage:
      - referenced-but-not-exported: a $pfg.X the target calls that the core does not export
        (passes node --check + the ordering assert, then throws TypeError at run time).
      - (the emitted-but-unreferenced direction holds by construction: the exposed set IS
        the referenced set, and the check below re-derives it, so a divergence fails loud.)
    This is the $pfg-side analogue of discovery's per-target _pv_ coverage."""
    refs = _pfg_refs(target)
    unexported = sorted(refs - set(exported))
    if unexported:
        raise SystemExit(
            f"pfg-codegen[{target}]: source calls $pfg member(s) NOT exported by "
            f"src/pfg-core.js: {', '.join('$pfg.' + m for m in unexported)}. Add them to "
            f"pfg-core (or fix the call); an unexported $pfg.X passes node --check but "
            f"throws TypeError at run time.")
    return refs


def build_block(target="extension"):
    """The per-target $pfg block: the transitive closure of `target`'s $pfg refs, in source
    order, exposing exactly the direct refs. Empty string when the target references no
    $pfg member (so the engine injects no block for it)."""
    src = read_src()
    header, blocks = _member_blocks(src)
    exported = [name for name, exp, _t in blocks if exp]
    # Every `export ` line must be an `export const NAME` we captured (else a future
    # `export function`/`export {}` would silently vanish from $pfg).
    n_export = len(re.findall(r"^export\s", src, flags=re.M))
    if n_export != len(exported):
        raise SystemExit(
            f"pfg-codegen: {n_export} `export ` lines but {len(exported)} `export const "
            f"NAME` captured; every export must be an `export const NAME`.")

    refs = check_membership(target, exported)
    if not refs:
        return ""  # target references no core member: no $pfg block

    graph = _dep_graph(blocks)
    needed = _closure(refs, graph)
    body = "".join(text for name, _exp, text in blocks if name in needed).rstrip("\n")
    exposed = ", ".join(sorted(refs))
    return (
        BEGIN + "\n"
        "// Do NOT edit here. Edit src/pfg-core.js and re-run the codegen. This is the ONE\n"
        f"// injected definition of the compaction/lineage primitives (the {target} subset:\n"
        "// only the members this target uses, plus their transitive deps).\n"
        + header.rstrip("\n") + "\n"
        "const $pfg = (function () {\n"
        + body + "\n"
        "return { " + exposed + " };\n"
        "})();\n"
        + END + "\n"
    )


def check(block, target):
    if not block:
        sys.stderr.write(f"pfg-codegen[{target}]: empty $pfg block (target references no core member).\n")
        return 0
    fd, tmp = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(block)
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
    finally:
        os.unlink(tmp)
    if r.returncode != 0:
        sys.stderr.write("pfg-codegen: generated block FAILED node --check:\n" + r.stderr)
        return 1
    exposed = sorted(_pfg_refs(target))
    sys.stderr.write(
        f"pfg-codegen[{target}]: OK. Block parses; {len(exposed)} primitives exposed: "
        + ", ".join(exposed) + "\n")
    return 0


def _target_arg(argv):
    if "--target" in argv:
        i = argv.index("--target")
        if i + 1 < len(argv):
            return argv[i + 1]
        raise SystemExit("pfg-codegen: --target needs a value")
    return "extension"


def main(argv):
    target = _target_arg(argv)
    block = build_block(target)
    if "--check" in argv:
        return check(block, target)
    sys.stdout.write(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
