"""
discovery: resolve each abstract vendor dep (_pv_X) to its current minified name
on a given target, verify-once-or-abort, and enforce the core<->registry contract
BIJECTIVELY. Target-agnostic engine over the per-target anchor DATA (anchors.py):
one core surface, N anchor sets, each must cover the surface.
"""
import os
import re

from . import anchors

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "src")


def _one(js, pattern, label):
    """find_one discipline: exactly one structural match, or die loud. 0 = the
    vendor reshaped this site (re-anchor); >1 = ambiguous (tighten). Never bind a
    unique-but-wrong, which is the silent hazard the wholesale rules carry."""
    ms = list(re.finditer(pattern, js))
    if len(ms) != 1:
        raise SystemExit(
            f"[pfg discover] {label}: expected 1 structural match, got {len(ms)}.\n"
            f"               Vendor drifted here; re-anchor (anchors.py) for this target."
        )
    return ms[0]


def discover(js, target="extension"):
    """Every vendor name this target's anchors resolve, verify-once each. Returns
    {_pv_: minified_name} for bind deps AND locate targets. Builtins are not
    discovered (they bind by fresh require, module ids being stable)."""
    names = {}
    for a in anchors.ANCHORS:
        if target not in a.regexes:
            continue  # rule not applicable to this target: skip, never abort
        label = " + ".join(dep.pv for dep, _g in a.deps)
        m = _one(js, a.regexes[target], label)
        for dep, group in a.deps:
            names[dep.pv] = m.group(group)
    # CSS-module hash deps are resolved BY CO-OCCURRENCE, not a single-regex anchor (the
    # bundler hashes a whole module, so no one class pins it; the intersection of a few does).
    # They land in `names` as quoted string literals, alias-prologue-bound like any dep.
    names.update(anchors.discover_css_hashes(js, target))
    return names


# The src/ bodies that reference vendor deps, per target. The dep surface is grepped
# from these (never hand-listed), so each target greps ITS OWN source: the extension's
# loader + chain-builder, the webview's render-wrap. Keyed by target so adding the
# second target does not make the extension re-scan webview source or vice versa.
SRC_FILES = {
    "extension": ("d1e.js", "i1e.js"),
    "webview": ("render.js",),
}


def derive_deps(target="extension"):
    """The dep surface for `target`, grepped from that target's src/ bodies (never
    hand-listed). A _pv_ in a body with no registry entry is an unbindable surface ->
    loud (via coverage)."""
    try:
        files = SRC_FILES[target]
    except KeyError:
        raise SystemExit(
            f"[pfg discover] no src file list for target {target!r}; "
            f"add it to discovery.SRC_FILES"
        )
    deps = set()
    for fn in files:
        path = os.path.join(SRC, fn)
        if not os.path.exists(path):
            raise SystemExit(
                f"[pfg discover] target {target!r} declares src/{fn}, which does not "
                f"exist yet: the readable source for this target has not landed, so its "
                f"vendor-dep surface cannot be derived."
            )
        with open(path, encoding="utf-8") as f:
            deps |= set(re.findall(r"_pv_\w+", f.read()))
    return deps


def references_pfg(target="extension"):
    """Does `target`'s source reference any $pfg.<member>? A cheap grep of the target's
    src files, so the engine can USAGE-DERIVE whether to inject the $pfg block: a target
    that touches $pfg gets its minimal core subset, one that does not gets none, with no
    per-target flag to keep in sync (the webview lights up the instant render.js reads
    $pfg.PFGK1_PREFIX). Missing src files count as no reference (not-landed-yet is fine
    here; derive_deps is the loud check for a declared-but-absent body)."""
    for fn in SRC_FILES.get(target, ()):
        path = os.path.join(SRC, fn)
        try:
            with open(path, encoding="utf-8") as f:
                if re.search(r"\$pfg\.", f.read()):
                    return True
        except OSError:
            continue
    return False


def coverage(target="extension"):
    """BIJECTIVE per-target contract between the core surface and the registry:
      - every _pv_ the core CALLS must be resolvable                        -> else UNCOVERED
      - every alias-bound dep the registry declares must be called          -> else DEAD
    "Called" = the deps the alias prologue binds: bind + builtin (references) AND literal
    (discovered CONSTANTS baked as string literals, e.g. the CSS-module hashes). Locate
    deps are wholesale TARGETS, not source references, and capture deps are threaded into a
    rule at its splice site (not alias-bound), so both are excluded from the source side.
    Adding OR removing a core dep is forced to move the registry in lockstep, loud on
    either side, so an unanchored _pv_ (css hash included) fails loud."""
    derived = derive_deps(target)
    registry = anchors.deps_for(target)
    called = {pv for pv, d in registry.items() if d.kind in ("bind", "builtin", "literal")}
    missing = derived - called
    if missing:
        raise SystemExit(
            f"[pfg coverage/{target}] UNCOVERED: src/ references {sorted(missing)} "
            f"with no anchor or builtin. Add each to anchors.py.")
    dead = called - derived
    if dead:
        raise SystemExit(
            f"[pfg coverage/{target}] DEAD: anchors.py binds {sorted(dead)} that "
            f"src/ no longer references. Remove the dead anchor.")
    return derived
