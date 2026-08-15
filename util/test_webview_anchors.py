"""
Webview call-site splice guards (task 24, wv-3's half): the COMMITTED webview anchors
(anchors.py) and the CSS-module hash discovery (anchors.discover_css_hashes), exercised
against a representative render-dispatcher fixture and, when this box has one installed,
the real pristine bundle.

Distinct from util/test_target_aware.py: that test drives the engine plumbing over a
TOY anchor (it monkeypatches engine.anchor_for), so it never exercises the real anchor
grammar or the co-occurrence hash discovery. This does exactly that: the grammar that has
to survive per-bundle minifier drift, and the discovery that resolves the drifting
CSS-module hashes structurally instead of hardcoding them.
"""
import glob
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from pfg import anchors, engine  # noqa: E402
from pfg.discovery import _one  # noqa: E402

# A representative per-message render dispatcher: both decorate branches (user + assistant)
# plus the `meta` branch the assistant anchor keys its terminator on. Real 2.1.195 local
# names so the fixture reads like the bundle it must match.
DISPATCH = (
    'function U8t(e,t,i,n,o=!1,r,s,a,l){if(t.isEmpty)return null;'
    'if(t.type==="user"){if(t.parentToolUseId)return null;if(t.isSynthetic)return null;'
    'return b(V8t,{session:e,message:t,index:i,context:n,setInputError:a,onCreateNewSession:l},i)}'
    'if(t.type==="assistant"){if(t.content.every((d)=>d.content.type==="tool_use"&&_1(d.content.name,n).hidden))return null;'
    'return b(z8t,{session:e,message:t,index:i,context:n,status:W8t(t,e.busy.value)},i)}'
    'if(t.type==="meta")return b(j8t,{message:t},i);return null}'
)

# A representative bundle slice for CSS-module hash DISCOVERY: the body module's classes
# share hash ABC123 (with a decoy `content_OTHER9` that only `content_` carries, so the
# 3-way intersection must exclude it); the action module shares XYZ789.
CSS_FIXTURE = (
    ".content_ABC123.collapsed_ABC123{}.truncationGradient_ABC123{}.buttonContainer_ABC123{}"
    ".content_OTHER9{}"
    ".optionText_XYZ789{}.popupOption_XYZ789{}.popupHeader_XYZ789{}.actionButton_XYZ789{}"
)


def _rx(pv):
    return anchors.anchor_for(pv).regexes["webview"]


# ---- the committed anchor grammars, against a representative site -------------

def test_user_anchor_matches_once_and_captures_locals():
    anc = anchors.anchor_for("_pv_wvFactory")
    m = _one(DISPATCH, anc.regexes["webview"], "user")
    caps = {dep.pv: m.group(g) for dep, g in anc.deps}
    assert caps == {
        "_pv_wvFactory": "b", "_pv_wvUserMsg": "V8t", "_pv_wvSession": "e",
        "_pv_wvMessage": "t", "_pv_wvIndex": "i",
    }
    # the two edit positions the wv-wrap rule rewrites
    assert m.group(2) == "return " and m.group(7) == "}"


def test_assistant_anchor_matches_once_and_captures_z8t():
    anc = anchors.anchor_for("_pv_wvAsstMsg")
    m = _one(DISPATCH, anc.regexes["webview"], "assistant")
    # only the new local is declared; message/session/factory/index come from the user anchor
    assert {dep.pv: m.group(g) for dep, g in anc.deps} == {"_pv_wvAsstMsg": "z8t"}
    # the `[\s\S]{0,220}?return ({ID})\(` must skip the hidden-tool-use guard's `return null;`
    # and land on `return b(z8t,`, not the guard.
    assert m.group(2) == "return " and m.group(3) == "b" and m.group(7) == "}"


def test_both_anchors_are_unique_in_one_dispatcher():
    # find-one discipline: each site resolves exactly once, so discovery never binds a
    # unique-but-wrong or aborts ambiguous.
    for pv in ("_pv_wvFactory", "_pv_wvAsstMsg"):
        assert len(list(re.finditer(_rx(pv), DISPATCH))) == 1


def test_anchors_do_not_cross_match_each_others_branch():
    # the user anchor keys on `...==="assistant"` as its terminator and the assistant anchor
    # on `...==="meta"`, so neither matches the other's branch in isolation.
    user_only = DISPATCH[:DISPATCH.index('if(t.type==="assistant")')] + 'if(t.type==="assistant"){}'
    assert len(list(re.finditer(_rx("_pv_wvAsstMsg"), user_only))) == 0


# ---- the CSS-module hash DISCOVERY (co-occurrence, never hardcoded) -----------

def test_css_hashes_discovered_by_cooccurrence():
    got = anchors.discover_css_hashes(CSS_FIXTURE, "webview")
    # each module's shared hash, as the RAW value (a "literal" dep-kind; the two-form alias
    # prologue quotes it into `= "hash"`); the decoy content_OTHER9 is excluded by the
    # 3-way body-module intersection.
    assert got == {"_pv_cmBodyHash": "ABC123", "_pv_actionButtonHash": "XYZ789"}


def test_css_hash_discovery_fires_loud_on_drift():
    # a base class name drifted -> its `<base>_H` set is empty -> the intersection is empty
    # -> loud, never a silently-wrong hash (the anti-pattern the discovery replaces).
    drifted = CSS_FIXTURE.replace("truncationGradient_", "fadeGradient_")
    with pytest.raises(SystemExit) as ei:
        anchors.discover_css_hashes(drifted, "webview")
    assert "_pv_cmBodyHash" in str(ei.value)


def test_css_hashes_are_webview_only():
    # the CSS-module deps are webview-scoped; the extension target has none.
    assert anchors.discover_css_hashes(CSS_FIXTURE, "extension") == {}


# ---- the real installed bundle, when present (strongest check) ---------------

_V195 = glob.glob(os.path.expanduser(
    "~/.*/extensions/anthropic.claude-code-2.1.195-linux-x64/webview/index.js.bak"))


@pytest.mark.skipif(not _V195, reason="no installed pristine 2.1.195 webview bundle")
def test_real_2195_bundle_anchors_unique_and_guard_passes():
    js = open(_V195[0], encoding="utf-8", errors="surrogateescape").read()
    for pv, comp in (("_pv_wvFactory", "V8t"), ("_pv_wvAsstMsg", "z8t")):
        ms = list(re.finditer(_rx(pv), js))
        assert len(ms) == 1, f"{pv}: expected 1 match in the real 2.1.195 bundle, got {len(ms)}"
        assert ms[0].group(4) == comp
    # the two CSS-module hashes resolve uniquely by co-occurrence on the real bundle (raw
    # values; the two-form prologue quotes them at bind time)
    assert anchors.discover_css_hashes(js, "webview") == {
        "_pv_cmBodyHash": "xGDvVg", "_pv_actionButtonHash": "v2CdxQ"}


@pytest.mark.skipif(not _V195, reason="no installed pristine 2.1.195 webview bundle")
def test_real_2195_gutter_wraps_both_branches_no_dead_code():
    """End-to-end on the real bundle: apply(webview) wraps BOTH dispatcher branches (user
    V8t + assistant z8t) with pfgkDecorate, and NEITHER still returns the raw vendor element.
    A raw `return b(V8t/z8t,...)` surviving is the dead-code-after-return ship bug
    (MAINTAINER.md: shipped twice); this pins it to zero. Idempotent re-apply."""
    from pfg import engine
    js = open(_V195[0], encoding="utf-8", errors="surrogateescape").read()
    out = engine.apply(js, "webview")
    assert out.count("return pfgkDecorate(") == 2                 # user + assistant, both wrapped
    assert out.count("return b(V8t,{session") == 0               # no dead user-branch return
    assert out.count("return b(z8t,{session") == 0               # no dead assistant-branch return
    assert engine.apply(out, "webview") == out                   # idempotent
