"""
engine: the I/O-free driver. discover -> verify-all -> write-all over the
target-applicable rules. It performs NO target file I/O (that is the adapters'
job); the only files it reads are its own patch SOURCE (the src/ bodies + the
$pfg codegen). Verify-all-then-write-all: every locate/splice anchor must resolve
before ANY edit is applied, so a single drifted anchor lands nothing.
"""
import os
import re
import subprocess
import sys

from .anchors import BUILTINS, anchor_for, deps_for
from .discovery import discover, coverage, derive_deps, references_pfg, _one
from .jslex import find_function_span, enclosing_function_start
from .rules import RULES

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "src")
sys.path.insert(0, REPO)
from version import SIGNATURE, PATCHSET_VERSION  # noqa: E402

# Targets whose bundle CONTAINS the vendor's preamble-writing code (the agent that
# emits "This session is being continued..."). The extension only READS the
# preamble from saved sessions, so the string is not in its bundle; its dependency
# is covered by the behavioral gate (current wording) and, once the CLI lands,
# transitively by this write-side guard (both share isContinuationPreamble).
EMITS_PREAMBLE = {"cli"}

# Any pfg site signature, capturing its version. The engine stamps SIGNATURE at EVERY
# site it writes (inject / wholesale / each splice edit), so the patch state is judged by
# counting sites at the right version, never by mere presence of one.
SIG_RE = re.compile(r"/\*pfg-v(\d+(?:\.\d+)?)\*/")


def _expected_sites(target):
    """How many signed sites `target`'s rules write: one per inject and per wholesale
    body, plus one per splice EDIT (a multi-edit splice signs each of its edits). A static
    property of the rules, so the patch-state check needs no discovery (the anchors would
    not resolve against already-patched code anyway)."""
    n = 0
    for r in RULES:
        if target not in r.targets:
            continue
        if r.kind in ("inject", "wholesale"):
            n += 1
        elif r.kind == "splice":
            n += len(r.spec["edits"])
    return n


def _patch_scan(js, target):
    """(verdict, present, expected) for `target`, judged by the per-site signatures, NOT
    by presence of one (docs objective: never degrade silently on a partial patch):
      "clean"   - no sites            -> APPLY.
      "patched" - all M sites, CURRENT version -> SKIP (idempotent no-op).
      "stale"   - a complete OLDER patchset. It may have fewer signed sites than the
                  current version added; restore .bak + re-apply on pristine.
      "partial" - a CURRENT-version site is missing / extra, an older patchset has more
                  sites than expected, or versions are mixed -> inconsistent patch state;
                  the caller must FAIL LOUD (never silent-skip, never blind re-apply /
                  double-patch)."""
    expected = _expected_sites(target)
    versions = SIG_RE.findall(js)
    present = len(versions)
    if present == 0:
        return "clean", present, expected
    uniq = set(versions)
    if present == expected and uniq == {PATCHSET_VERSION}:
        return "patched", present, expected
    # A new patchset may add a signed site. The complete older install then has N<M
    # signatures, all carrying one older version. Treat it as stale so the adapter
    # restores the pristine backup and applies the new set. A current-version N<M file
    # remains partial, as does any mixed-version or surplus-site file.
    if len(uniq) == 1 and PATCHSET_VERSION not in uniq and present <= expected:
        return "stale", present, expected
    return "partial", present, expected


def patch_state(js, target="extension"):
    """The verdict from _patch_scan (clean / patched / stale / partial). The I/O adapters
    call this to decide skip / apply / restore+reapply / fail-loud without re-running the
    engine."""
    return _patch_scan(js, target)[0]


def _read_src(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as f:
        return f.read()


def _pfg_block(target):
    """The per-target $pfg block (readable), emitted by the codegen from pfg-core.js: only
    the members this target uses (its $pfg.<member> refs) plus their transitive deps."""
    return subprocess.check_output(
        [sys.executable, os.path.join(REPO, "util", "pfg-codegen.py"), "--target", target],
        text=True,
    )


def _alias_prologue(names, target):
    """`const _pv_X = <binding>, ...;` binding exactly the deps the target's source
    references (grep-derived), each once, keyed on the dep's KIND so the collision-prone
    discovered names / values live ONLY here, never in the body:
      builtin        -> require("<module>")
      bind / capture -> a REFERENCE to the discovered vendor local (const _pv_x = <name>)
      literal        -> a discovered CONSTANT baked as a STRING LITERAL
                        (const _pv_h = "<value>"), e.g. a CSS-module hash. The value is
                        shape-validated ^[A-Za-z0-9]+$ before baking, so a mis-discovered
                        token fails loud here, not at a downstream node --check.
    Target-scoped, so it binds the deps of the target being built. A param-based target
    with no _pv_ deps gets the empty string, never the invalid `const ;`."""
    deps = sorted(derive_deps(target))
    if not deps:
        return ""
    registry = deps_for(target)
    parts = []
    for pv in deps:
        dep = registry.get(pv)
        kind = dep.kind if dep else None
        if kind == "builtin":
            parts.append(f'{pv} = require("{BUILTINS[pv][1]}")')
        elif kind == "literal":
            val = names.get(pv, "")
            if not re.fullmatch(r"[A-Za-z0-9]+", val):
                raise SystemExit(
                    f"[pfg engine] discovered constant {pv} = {val!r} is not "
                    f"^[A-Za-z0-9]+$; refusing to bake a malformed token into generated code.")
            parts.append(f'{pv} = "{val}"')
        elif pv in names:   # bind / capture: a reference to the discovered vendor local
            parts.append(f"{pv} = {names[pv]}")
        else:
            raise SystemExit(f"[pfg engine] no binding for source dep {pv}")
    return "const " + ", ".join(parts) + ";\n"


def _render_body(name):
    """A readable src/ body prepared for injection at the bundle's scope: strip the ESM
    `export ` keyword off each `export <decl> NAME`, so the declarations become plain
    names the wrap splice calls (webview render.js -> pfgkRenderWrap). Mirrors the $pfg
    codegen's export-strip, without the IIFE namespace (the wrap calls the render fn by
    bare name, not through a namespace object). export default / export {...} / export *
    are not injectable as bare declarations and fail loud rather than mangle."""
    body = _read_src(name)
    stripped = re.sub(r"^export\s+(?=(?:const|let|var|function|class)\b)", "", body, flags=re.M)
    if re.search(r"^export\b", stripped, flags=re.M):
        raise SystemExit(
            f"[pfg engine] {name}: an `export` survives declaration-export stripping "
            f"(export default / export {{...}} / export * are not injectable as bare "
            f"declarations). Rewrite as `export const/function NAME`.")
    return stripped.rstrip("\n") + "\n"


# The PINNED inject-spec schema. The ENGINE owns this contract; rules.py conforms.
#   "src":       a readable src/ body to inject (export-stripped), e.g. render.js.
#   "pfg":       include the shared $pfg block? Defaults to "unless a src body is given"
#                (extension {} -> True; a src target -> False). Set explicitly to override.
#   "at_anchor": PLACEMENT (consumed by apply()): pin the block to a named anchor's m.start
#                rather than the earliest edit.
# Any other key trips the loud guard below: that is the tripwire, not a shape to negotiate.
_INJECT_KEYS = {"src", "pfg", "at_anchor"}


def _inject_block(spec, names, target):
    """Build the ONE module-scope block an inject rule contributes, plus injected_pfg
    (whether it includes the shared $pfg block, so apply() runs the $pfg-ordering
    assertion). Composed from the pinned spec, in a fixed order so a later body can
    reference $pfg defined above it:

      $pfg block: included when injected_pfg is true, i.e. an explicit spec["pfg"], else
        the default "on unless a src body is present". The extension's {} defaults it ON;
        the webview render fn (self-contained, inlines its own "pfgk-" role detection) is
        $pfg-free.
      "src": a readable src/ body (export-stripped) whose splice calls it by name
        (webview render.js -> pfgkRenderWrap).
      alias prologue: appended only when the target HAS grep-derived _pv_ deps to bind
        (the extension's wholesale bodies); a param-based target gets the empty string.

    The per-site SIGNATURE is stamped by apply() uniformly over every edit, not here. An
    off-schema key fails loud: the schema is the pinned rules<->engine contract, not an
    ongoing negotiation."""
    unknown = set(spec) - _INJECT_KEYS
    if unknown:
        raise SystemExit(
            f"[pfg engine] inject rule for {target}: unknown spec key(s) {sorted(unknown)}; "
            f"the pinned inject-spec schema is {sorted(_INJECT_KEYS)}. The engine OWNS this "
            f"contract; conform rules.py to it (do not widen the schema here).")
    src = spec.get("src")
    # injected_pfg: an explicit spec["pfg"] wins; else USAGE-DERIVED, inject the $pfg block
    # iff the target's source actually references $pfg. So a target that starts touching
    # $pfg (webview render.js reading $pfg.PFGK1_PREFIX) auto-gets its minimal core subset
    # with no per-target flag to flip; one that doesn't gets none.
    injected_pfg = spec["pfg"] if "pfg" in spec else references_pfg(target)
    parts = []
    if injected_pfg:
        pfg = _pfg_block(target)
        if pfg:
            parts.append(pfg)
        else:
            # Empty tree-shake: the target's source references no $pfg member, so the linker
            # emits nothing. That is a clean NO-OP, not a dead block, so drop injected_pfg
            # and apply() skips the $pfg-ordering check (which would otherwise abort "no
            # $pfg.* usage" on the empty block). This makes an explicit pfg:True harmless
            # while the subset is empty (e.g. wv-3 flipping the flag before wv-1's $pfg
            # refs land); a GENUINE dead block (a non-empty block whose members go unused)
            # still trips the check, because the linker never emits one.
            injected_pfg = False
    if src:
        parts.append(_render_body(src))
    alias = _alias_prologue(names, target)  # "" when the target has no _pv_ deps to bind
    if alias:
        parts.append(alias)
    if not parts:
        raise SystemExit(f"[pfg engine] inject rule for {target}: empty block (no $pfg, no src, no deps)")
    return "\n" + "".join(parts), injected_pfg


def _continuation_preamble():
    """The one vendor content string the invariant rests on, read from its SOT
    (src/pfg-core.js) so it is not re-hardcoded here."""
    with open(os.path.join(SRC, "pfg-core.js"), encoding="utf-8") as f:
        m = re.search(r'CONTINUATION_PREAMBLE\s*=\s*"([^"]+)"', f.read())
    if not m:
        raise SystemExit("[pfg engine] could not read CONTINUATION_PREAMBLE from pfg-core.js")
    return m.group(1)


def _guard_preamble(js):
    """The invariant rests on ONE vendor content string (isContinuationPreamble).
    If the vendor rewords its resume preamble, forbidden-middle #1 (false success
    on a preamble) silently returns and no test catches it, because the tests
    hardcode the same literal. Guard the seam: assert the vendor still emits the
    string, so a reword fails LOUD here at apply, never silently at run time."""
    preamble = _continuation_preamble()
    if preamble not in js:
        raise SystemExit(
            f"[pfg engine] the vendor no longer emits the continuation preamble\n"
            f'  "{preamble[:50]}..."\n'
            f"  The invariant's content dependency drifted: isContinuationPreamble would\n"
            f"  silently fail. Re-verify src/pfg-core.js CONTINUATION_PREAMBLE against the bundle.")


def apply(js, target="extension"):
    """Vendor JS text in, patched JS text out. The patch state is judged by the per-site
    signatures (patch_state), never by presence of one: a fully-patched current file is a
    no-op; a stale or PARTIAL one fails loud rather than being silently skipped or blindly
    re-patched (docs objective: never degrade silently)."""
    verdict, present, expected = _patch_scan(js, target)
    if verdict == "patched":
        return js
    if verdict == "stale":
        raise SystemExit(
            f"[pfg engine] {target}: stale patch, all {expected} pfg sites present at an "
            f"older version (current is pfg-v{PATCHSET_VERSION}); restore .bak and re-apply "
            f"on the pristine source.")
    if verdict == "partial":
        raise SystemExit(
            f"[pfg engine] {target}: {present} of {expected} pfg-v{PATCHSET_VERSION} sites "
            f"present, inconsistent patch state; restore .bak and re-apply.")
    # verdict == "clean": patch it.
    names = discover(js, target)   # verify-once per symbol, or abort
    coverage(target)               # bijective core<->registry contract
    if target in EMITS_PREAMBLE:
        _guard_preamble(js)        # invariant's content dependency (write-side targets only)
    # The webview CSS-module hashes are DISCOVERED by co-occurrence (discover -> coverage),
    # so a drift fails loud there; there is no hardcode-then-assert-presence guard here.

    rules = [r for r in RULES if target in r.targets]
    edits = []  # (start, end, new_text); verify-all first, then write-all
    for r in rules:
        if r.kind == "wholesale":
            start, end = find_function_span(js, names[r.spec["locate"]])
            body = re.sub(rf"async function {r.spec['decl']}\(",
                          f"async function {names[r.spec['locate']]}(",
                          _read_src(r.spec["src"]), count=1)
            edits.append((start, end, body))
        elif r.kind == "splice":
            # ONE spec-driven splice mechanism for every structural in-place edit (the GY
            # _tel thread AND the webview render-wrap call site): the rule DATA names the
            # anchor to locate, an optional capture<->discovery consistency check, and a
            # list of edits, each a (capture group, replacement TEMPLATE). The template is
            # %-style: %(gN)s interpolates capture group N of the match, %(_pv_X)s a
            # discovered vendor name; JS braces are literal (a literal % would need %%). So
            # the edit SHAPE is data on the rule, not code here: a new splice is a rules.py
            # entry, never a new engine branch. Every edit lands INSIDE the one match, so a
            # multi-edit splice (the wrap's bind + return) is all-or-nothing by construction.
            anc = anchor_for(r.spec["anchor"])
            m = _one(js, anc.regexes[target], r.label)
            for pv, grp in r.spec.get("verify", {}).items():
                if names.get(pv) != m.group(grp):
                    raise SystemExit(
                        f"[pfg engine] {r.label}: capture group {grp} ({m.group(grp)!r}) != "
                        f"discovered {pv} ({names.get(pv)!r}); splice and discovery disagree.")
            ctx = {f"g{i}": (m.group(i) or "") for i in range(1, m.re.groups + 1)}
            ctx.update(names)
            for ed in r.spec["edits"]:
                g = ed["group"]
                edits.append((m.start(g), m.end(g), ed["template"] % ctx))
        # "inject" is resolved below, once the target spans are known

    if not edits:
        raise SystemExit(f"[pfg engine] target {target}: no applicable rules produced an edit")

    # The optional inject rule contributes one module-scope block placed before the
    # earliest target span, so every patched body can see it at run time (the extension's
    # $pfg + alias prologue; a target whose splice calls a lifted src body). At most one
    # per target; a target may also be pure-splice with no inject rule.
    inject_rules = [r for r in rules if r.kind == "inject"]
    if len(inject_rules) > 1:
        raise SystemExit(f"[pfg engine] target {target}: {len(inject_rules)} inject rules, expected at most 1")
    injected_pfg = False
    if inject_rules:
        spec = inject_rules[0].spec
        block, injected_pfg = _inject_block(spec, names, target)
        # Placement: by default before the earliest target span (extension: module level,
        # ahead of the wholesale bodies so they see $pfg). A rule may instead pin the block
        # to a named anchor via "at_anchor", HOISTED to module scope: the block lands before
        # the function whose body the anchor sits in, so the injected src (webview
        # render.js: PFTOK, the helper fns, the render fn) is defined ONCE at module scope,
        # not re-allocated on every call into the per-message render dispatcher. If the
        # anchor is already module-level (no enclosing function), use its own m.start.
        if "at_anchor" in spec:
            anc = anchor_for(spec["at_anchor"])
            at = _one(js, anc.regexes[target], f"inject at_anchor {spec['at_anchor']}").start()
            inject_at = enclosing_function_start(js, at)
            if inject_at is None:
                inject_at = at
        else:
            inject_at = min(s for s, _e, _t in edits)
        edits.append((inject_at, inject_at, block))

    # Stamp the ONE central signature into EVERY written site (the inject block, each
    # wholesale body, each splice edit), uniformly, so the patch state is verifiable
    # per-site (patch_state): a partial or mixed-version apply is DETECTABLE, never
    # silently mistaken for patched. The sig is an APPLY artifact placed here, never in
    # src/ (render.js / d1e / i1e stay clean source).
    edits = [(s, e, SIGNATURE + txt) for (s, e, txt) in edits]

    edits.sort(key=lambda x: (x[0], x[1]))
    out, pos = [], 0
    for s, e, txt in edits:
        if s < pos:
            raise SystemExit(f"[pfg engine] overlapping edits near offset {s}")
        out.append(js[pos:s]); out.append(txt); pos = e
    out.append(js[pos:])
    patched = "".join(out)

    # The output must be in the exact "patched" state: M current-version sites, no more and
    # no fewer. This proves the per-site stamping landed on every edit (so a re-apply
    # detects it) AND that no stray / old signature leaked in. A stamping bug is a loud
    # internal error here, never a silently under-signed ship.
    out_verdict, out_present, out_expected = _patch_scan(patched, target)
    if out_verdict != "patched":
        raise SystemExit(
            f"[pfg engine] internal: patched {target} output is '{out_verdict}' "
            f"({out_present} of {out_expected} current sites), not fully patched; the "
            f"per-site signature stamping is wrong.")
    sig_at = patched.find(SIGNATURE)

    # $pfg-ordering invariant (node-check cannot see scope): only when a $pfg block was
    # injected. The block must be referenced (else the injected bodies bound nothing: an
    # empty/broken src read would pass a find()==-1 ordering check silently) and must
    # precede every $pfg usage. A target that injects a plain src body (webview) has no
    # $pfg to order, so this check does not apply to it.
    if injected_pfg:
        use_at = patched.find("$pfg.")
        if use_at < 0:
            raise SystemExit("[pfg engine] no $pfg.* usage in patched output: the injected bodies did not reference the shared block (empty or broken src read?)")
        if use_at < sig_at:
            raise SystemExit("[pfg engine] $pfg block positioned after a $pfg usage")
    return patched
