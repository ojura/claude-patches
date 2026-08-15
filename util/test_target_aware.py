"""
Target-awareness gate for the engine (task 23): discovery + engine can build a
SECOND target ('webview') alongside the extension, each grepping its OWN src for
the vendor-dep surface, without the extension's byte output moving.

These are hermetic unit tests over the plumbing itself. They do NOT need a pristine
bundle (the end-to-end binding proof for the extension lives in test_behavioral.py,
and the webview end-to-end proof lands with src/render.js + the webview anchors/rules).
The point here is that the generic machinery routes correctly and fails loud on the
seams a second target introduces: an unknown target, a declared-but-missing src file,
and the inject-block variants.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from pfg import discovery, engine  # noqa: E402
from pfg.engine import SIGNATURE, patch_state, _expected_sites, _inject_block  # noqa: E402
from pfg.discovery import derive_deps  # noqa: E402
from pfg.rules import Rule  # noqa: E402


# ---- derive_deps(target) ----------------------------------------------------

def test_derive_deps_default_is_extension():
    """The default arg keeps every existing caller (test_behavioral, the alias
    prologue) pointed at the extension surface, so nothing regresses."""
    assert derive_deps() == derive_deps("extension")


def test_derive_deps_extension_surface_nonempty_and_pv_only():
    deps = derive_deps("extension")
    assert deps, "extension derives an empty dep surface (src/ not found?)"
    # _pv_o1e (the renderer / tool-result reattach) is CALLED by the source bodies, so
    # it is in the grepped surface. _pv_i1e / _pv_d1e are locate TARGETS, not source
    # references, so they are deliberately NOT here (that split is the coverage contract).
    assert "_pv_o1e" in deps, sorted(deps)
    assert all(d.startswith("_pv_") for d in deps), sorted(deps)


def test_derive_deps_unknown_target_raises_loud():
    with pytest.raises(SystemExit) as ei:
        derive_deps("nosuchtarget")
    assert "SRC_FILES" in str(ei.value)


def test_derive_deps_missing_src_file_raises_loud(monkeypatch):
    """A target that declares a src file which has not landed yet fails loud with a
    'has not landed' message, not a bare FileNotFoundError. This is exactly the
    webview state until src/render.js exists; asserted via a synthetic target so the
    test stays green once render.js DOES land."""
    monkeypatch.setitem(discovery.SRC_FILES, "toytarget", ("does_not_exist_xyz.js",))
    with pytest.raises(SystemExit) as ei:
        derive_deps("toytarget")
    msg = str(ei.value)
    assert "does_not_exist_xyz.js" in msg and "has not landed" in msg


# ---- _render_body(name): export-strip for bundle injection -------------------

def test_render_body_strips_declaration_exports(monkeypatch):
    """`export const/function NAME` -> bare `const/function NAME` so the wrap splice can
    call the render fn by name at the bundle's scope."""
    monkeypatch.setattr(engine, "_read_src",
                        lambda name: "export const A = 1;\nexport function f(){ return A; }\n")
    out = engine._render_body("render.js")
    assert out == "const A = 1;\nfunction f(){ return A; }\n"
    assert "export" not in out


def test_render_body_rejects_non_declaration_export(monkeypatch):
    """export default / export {...} / export * cannot be injected as bare decls: fail
    loud rather than mangle `export default x` into a stray `default x`."""
    monkeypatch.setattr(engine, "_read_src", lambda name: "const x=1;\nexport default x;\n")
    with pytest.raises(SystemExit) as ei:
        engine._render_body("render.js")
    assert "export" in str(ei.value)


# ---- _inject_block(spec, names, target) -------------------------------------

def test_inject_block_extension_empty_spec_is_pfg_plus_alias(monkeypatch):
    """The extension's {} spec: $pfg block + alias prologue, injected_pfg True. The block
    carries NO signature: apply() stamps the sig per-site uniformly, so src/ stays clean.
    Stub the two builders so this tests ROUTING, not the codegen."""
    monkeypatch.setattr(engine, "_pfg_block", lambda target: "PFG_CORE_BODY\n")
    monkeypatch.setattr(engine, "_alias_prologue", lambda names, target: "const _pv_x=y;\n")
    block, injected_pfg = _inject_block({}, {"_pv_x": "y"}, "extension")
    assert injected_pfg is True
    assert block == "\nPFG_CORE_BODY\nconst _pv_x=y;\n"
    assert SIGNATURE not in block   # unsigned here; apply() stamps it


def test_inject_block_webview_src_pfg_false_is_body_only(monkeypatch):
    """The committed webview inject spec {"src":...,"pfg":False}: just the export-stripped
    render body (unsigned; apply() stamps the sig per-site). No $pfg, no alias prologue
    (render.js is param-based and $pfg-free)."""
    monkeypatch.setattr(engine, "_render_body", lambda name: "function pfgkRenderWrap(){}\n")
    monkeypatch.setattr(engine, "derive_deps", lambda target="extension": set())
    block, injected_pfg = _inject_block({"src": "render.js", "pfg": False}, {}, "webview")
    assert injected_pfg is False
    assert block == "\nfunction pfgkRenderWrap(){}\n"
    assert SIGNATURE not in block and "$pfg" not in block and "const _pv_" not in block


def test_inject_block_off_schema_block_key_is_rejected():
    """The old {"block":...} shape is OFF the pinned schema {src,pfg,at_anchor}: it trips
    the loud guard rather than being silently honored. This is the tripwire the lead kept,
    not a negotiated shape."""
    with pytest.raises(SystemExit) as ei:
        _inject_block({"block": "src", "src": "render.js"}, {}, "webview")
    assert "pinned inject-spec schema" in str(ei.value)


def test_inject_block_unknown_spec_key_raises_loud():
    """A truly unknown inject-spec key fails loud (the engine owns the schema; rules.py
    conforms) rather than being silently ignored."""
    with pytest.raises(SystemExit) as ei:
        _inject_block({"bogus_key": 1, "src": "render.js"}, {}, "webview")
    assert "unknown spec key" in str(ei.value)


def test_inject_pfg_is_usage_derived_when_flag_absent(monkeypatch):
    """No explicit "pfg" flag -> the engine injects the $pfg block IFF the target's source
    references $pfg (references_pfg). So a webview whose render.js starts reading
    $pfg.PFGK1_PREFIX auto-carries its minimal core subset, no per-target flag to flip; one
    that references no $pfg member gets none. This is what unblocks wv-1's render.js swap."""
    monkeypatch.setattr(engine, "_read_src",
                        lambda name: "export function pfgkRenderWrap(t,e,b,_ws){return $pfg.PFGK1_PREFIX;}\n")
    monkeypatch.setattr(engine, "_pfg_block",
                        lambda target: 'const $pfg=(function(){const PFGK1_PREFIX="PFGK1:";return { PFGK1_PREFIX };})();\n')
    monkeypatch.setattr(engine, "derive_deps", lambda target="extension": set())
    # render.js references $pfg -> auto-inject the subset (no explicit pfg flag)
    monkeypatch.setattr(engine, "references_pfg", lambda target="extension": True)
    block, injected_pfg = _inject_block({"src": "render.js", "at_anchor": "_pv_wvFactory"}, {}, "webview")
    assert injected_pfg is True
    assert 'const $pfg=(function(){const PFGK1_PREFIX' in block            # the subset is present
    assert "return $pfg.PFGK1_PREFIX" in block                            # the render body uses it
    assert block.index("const $pfg") < block.index("return $pfg.PFGK1_PREFIX")  # defined before used
    # render.js references NO $pfg (consistent: a $pfg-free body) -> no block, no dead $pfg
    monkeypatch.setattr(engine, "references_pfg", lambda target="extension": False)
    monkeypatch.setattr(engine, "_read_src",
                        lambda name: "export function pfgkRenderWrap(t,e,b,_ws){return _ws;}\n")
    block2, injected_pfg2 = _inject_block({"src": "render.js", "at_anchor": "_pv_wvFactory"}, {}, "webview")
    assert injected_pfg2 is False and "$pfg" not in block2


def test_inject_pfg_true_but_empty_subset_is_noop_not_abort(monkeypatch):
    """Explicit pfg:True while the linked $pfg subset is EMPTY (the target references no
    $pfg member yet) is a clean no-op, not a dead block: injected_pfg drops to False so
    apply()'s $pfg-ordering check does not abort ("no $pfg.* usage"). This lets a flag be
    flipped pfg:True ahead of the target's $pfg refs landing without a half-land abort."""
    monkeypatch.setattr(engine, "_pfg_block", lambda target: "")            # empty tree-shake
    monkeypatch.setattr(engine, "_render_body", lambda name: "function pfgkRenderWrap(){}\n")
    monkeypatch.setattr(engine, "derive_deps", lambda target="extension": set())
    block, injected_pfg = _inject_block({"src": "render.js", "pfg": True}, {}, "webview")
    assert injected_pfg is False           # dropped, so apply() skips the $pfg-ordering check
    assert "$pfg" not in block             # no dead block injected


def test_alias_prologue_no_deps_is_empty_not_const_semicolon(monkeypatch):
    """A target with no _pv_ deps yields the empty string, never `const ;` (a SyntaxError).
    Regression: a webview inject mis-routed through the $pfg branch shipped `const ;` into
    webview/index.js and failed node --check."""
    monkeypatch.setattr(engine, "derive_deps", lambda target="extension": set())
    assert engine._alias_prologue({}, "webview") == ""


# ---- the unified spec-driven splice (GY thread + webview call site) ---------

def test_splice_verify_mismatch_raises(monkeypatch):
    """The splice's optional `verify`: a captured group that disagrees with discovery fails
    loud. This is the GY/i1e co-discovery guard, now generic on the unified splice."""
    from pfg.anchors import Anchor
    toy_anchor = Anchor([], {"extension": r"(foo) (bar)"})
    monkeypatch.setattr(engine, "discover", lambda js, target: {"_pv_x": "NOTfoo"})
    monkeypatch.setattr(engine, "coverage", lambda target: None)
    monkeypatch.setattr(engine, "anchor_for", lambda pv: toy_anchor)
    monkeypatch.setattr(engine, "RULES", [
        Rule("toy splice", "splice", frozenset({"extension"}), {
            "anchor": "_pv_x", "verify": {"_pv_x": 1},
            "edits": [{"group": 2, "template": "BAZ"}],
        }),
    ])
    with pytest.raises(SystemExit) as ei:
        engine.apply("foo bar", target="extension")
    assert "splice and discovery disagree" in str(ei.value)


def test_apply_webview_splice_injects_binds_and_calls_render(monkeypatch):
    """The webview target end-to-end through apply(), testing the ENGINE MECHANICS of the
    UNIFIED spec-driven "splice" kind + the src/at_anchor inject. Uses a CONTROLLED toy
    anchor (8 groups: 4 = the return token, 8 = the branch-close) so this stays stable
    while wv-3 iterates the real render-site grammar; the real grammar's fidelity to
    webview/index.js is role 3's / role 5's to validate.

    apply() must: HOIST the export-stripped render body to module scope via "at_anchor"
    (before the function whose body the anchor sits in, so it lands once, not per call);
    apply the two spec-declared edits (group 4 -> "let _ws=", group 8 -> the return-wrap
    with the discovered capture names %-templated in and the branch-close preserved via
    %(g8)s); carry no $pfg; and be idempotent."""
    from pfg.anchors import Anchor
    # 8 capture groups: 1 dispatcher, 2 session, 3 message, 4 "return ", 5 factory,
    # 6 user-msg component, 7 index, 8 branch-close "}". The render site sits INSIDE a
    # `function WRAP(...)` body so the at_anchor placement HOISTS the block out to module
    # scope (before `function WRAP`), not inside the per-call dispatcher body.
    toy = "head;function WRAP(a){DISP(SS){M return B UC IX }}tail"
    toy_anchor = Anchor([], {"webview": r"(DISP)\((SS)\)\{(M) (return )(B) (UC) (IX) (\})"})
    caps = {"_pv_wvDispatch": "DISP", "_pv_wvSession": "SS", "_pv_wvMessage": "M",
            "_pv_wvFactory": "B", "_pv_wvUserMsg": "UC", "_pv_wvIndex": "IX"}
    monkeypatch.setattr(engine, "discover", lambda js, target: dict(caps))
    monkeypatch.setattr(engine, "coverage", lambda target: None)
    monkeypatch.setattr(engine, "anchor_for", lambda pv: toy_anchor)
    monkeypatch.setattr(engine, "derive_deps", lambda target="extension": set())
    monkeypatch.setattr(engine, "_read_src",
                        lambda name: "export function pfgkRenderWrap(t,e,b,_ws){return _ws;}\n")
    _WV = frozenset({"webview"})
    monkeypatch.setattr(engine, "RULES", [
        Rule("inject render body", "inject", _WV,
             {"src": "render.js", "at_anchor": "_pv_wvFactory", "pfg": False}),
        Rule("splice render site", "splice", _WV, {
            "anchor": "_pv_wvFactory",
            "edits": [
                {"group": 4, "template": "let _ws="},
                {"group": 8,
                 "template": ";return pfgkRenderWrap(%(_pv_wvMessage)s,%(_pv_wvSession)s,%(_pv_wvFactory)s,_ws)%(g8)s"},
            ],
        }),
    ])
    out = engine.apply(toy, target="webview")
    assert SIGNATURE in out                               # the one central sig, shared by every target's file
    assert "function pfgkRenderWrap(t,e,b,_ws)" in out    # injected render body (export-stripped)
    assert out.index(SIGNATURE) < out.index("function WRAP")  # HOISTED to module scope, before the enclosing fn
    assert out.index("function pfgkRenderWrap") < out.index("function WRAP")  # render fn defined ONCE, above WRAP
    assert "let _ws=B" in out                             # group 4 edit: binding conversion
    assert ";return pfgkRenderWrap(M,SS,B,_ws)}" in out   # group 8 edit: capture names templated, "}" preserved
    assert "$pfg" not in out                              # no $pfg block injected
    assert patch_state(out, target="webview") == "patched"  # all 3 webview sites stamped (inject + 2 splice edits)
    assert engine.apply(out, target="webview") == out       # idempotent: patch_state == "patched" -> no-op


# ---- per-site signatures + patch_state decision table ------------------------

def test_expected_sites_per_target():
    # extension: inject + d1e wholesale + i1e wholesale + GY splice + teamName-filter splice = 5
    assert _expected_sites("extension") == 5
    # webview: inject + user-branch render-wrap+gutter splice (2 edits) + assistant-branch
    # gutter splice (2 edits) = 5
    assert _expected_sites("webview") == 5


def test_patch_state_decision_table():
    """The engine judges the patch state by counting per-site sigs at the right version,
    NOT by presence of one (docs objective: never degrade silently). M = expected sites."""
    M = _expected_sites("extension")
    OLD = "/*pfg-v0.1*/"
    assert patch_state("", "extension") == "clean"                            # no sites -> APPLY
    assert patch_state(SIGNATURE * M, "extension") == "patched"               # all M current -> SKIP
    assert patch_state(OLD * M, "extension") == "stale"                       # all M, one old ver -> restore+reapply
    assert patch_state(OLD * (M - 1), "extension") == "stale"                 # older complete set had fewer sites
    assert patch_state(SIGNATURE * (M - 1), "extension") == "partial"         # current site missing -> FAIL LOUD
    assert patch_state(SIGNATURE * (M + 1), "extension") == "partial"         # a site extra -> FAIL LOUD
    assert patch_state(SIGNATURE * (M - 1) + OLD, "extension") == "partial"   # mixed versions -> FAIL LOUD


def test_apply_fails_loud_on_partial_state():
    """apply() refuses a partial file rather than silently skipping or double-patching."""
    partial = "x" + SIGNATURE + "y"   # 1 of 5 extension sites present
    with pytest.raises(SystemExit) as ei:
        engine.apply(partial, target="extension")
    assert "inconsistent patch state" in str(ei.value)
