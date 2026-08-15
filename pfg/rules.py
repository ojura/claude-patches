"""
rules: the patch rule set as DATA. Each rule states its kind and which targets it
applies to; the engine runs only the target-applicable subset, so an
extension-only rule SKIPS on the CLI instead of its zero-count anchor aborting the
whole apply, while verify-once-or-abort still holds for every rule that IS run.

  kind "wholesale": replace a whole vendor function (located by its _pv_ name) with
                    a readable src/ body, renamed to the discovered name.
  kind "splice":    one or more structural in-place edits off a SINGLE anchor match,
                    declared as DATA: {anchor, [verify], edits:[{group, template}]}.
                    The template is %-style (%(gN)s = capture group N of the match,
                    %(_pv_X)s = a discovered vendor name; JS braces are literal). ONE
                    mechanism for BOTH the GY _tel thread (rewrite the signature group)
                    and the webview render-wrap call site (bind the element in one
                    group, return the wrapped element at the branch-close group), so
                    there is no per-splice engine branch. Every edit lands inside the
                    one match, so a multi-edit splice is all-or-nothing.
  kind "inject":    insert the shared block before the earliest target (or at a rule's
                    "at_anchor" m.start) so the injected bodies can reference it. For
                    the extension that block is $pfg + the alias prologue; for the
                    webview it is the param-based render.js body (spec["src"]), no $pfg
                    and no alias prologue.

The pfg-core rules are {extension}; the render-wrap rules are {webview}. The CLI
(v2, bun_handler) gets added to each pfg-core rule's target set, together with its
regexes in anchors.py, once the extension work lands. The CLI scaffolding that
already exists (the "when the CLI lands" anchor notes, engine.EMITS_PREAMBLE's cli
entry) stays as forward roadmap; only the rule target set drops cli, so the
declared-but-unanchored contradiction is gone and applying cli aborts loud instead.
"""
from collections import namedtuple

Rule = namedtuple("Rule", "label kind targets spec")

_EXT = frozenset({"extension"})
_WV = frozenset({"webview"})
# The v2 target set, once the CLI (bun_handler) anchors land in anchors.py (measured:
# 4 of 7 need a cli-specific variant). DELIBERATELY not wired yet: re-enabling cli is
# a one-line flip of the pfg-core rules below from _EXT to _BOTH, scaffolding
# preserved not re-derived. Kept defined on purpose, do not delete as "unused".
_BOTH = frozenset({"extension", "cli"})

RULES = [
    Rule("inject $pfg block + alias prologue", "inject", _EXT, {}),
    Rule("replace d1e (loader)", "wholesale", _EXT,
         {"locate": "_pv_d1e", "src": "d1e.js", "decl": "d1e"}),
    Rule("replace i1e (chain-builder)", "wholesale", _EXT,
         {"locate": "_pv_i1e", "src": "i1e.js", "decl": "i1e"}),
    Rule("splice GY (+_tel thread)", "splice", _EXT, {
        "anchor": "_pv_GY",
        # co-discovered from GY's body: assert the captured GY/i1e names match discovery,
        # or the _tel thread would splice into a different function than i1e-wholesale
        # replaces. Group layout (owned by the GY anchor): 1 = the signature prefix
        # replaced, 2 = GY, 3 = e (param 1), 4 = t (param 2), 5 = r (i1e result var),
        # 6 = i1e. Rewrite group 1 to thread _tel through both the signature and the call.
        "verify": {"_pv_GY": 2, "_pv_i1e": 6},
        "edits": [
            {"group": 1,
             "template": "async function %(g2)s(%(g3)s,%(g4)s,_tel){let %(g5)s=await %(g6)s(%(g3)s,_tel),"},
        ],
    }),
    Rule("allow team-owned turns in transcript output", "splice", _EXT, {
        "anchor": "_pv_teamNameVisibilityGuard",
        "edits": [
            {"group": 4, "template": ""},
        ],
    }),
    # ---- webview target: lift the K render-wrap into src/render.js ----
    # render.js is a "src" inject. The engine USAGE-DERIVES the $pfg block (no explicit pfg
    # flag): none while render.js references no $pfg member (it inlines its own "pfgk-" role
    # detection today), and its minimal core subset (e.g. {PFGK1_PREFIX}) the instant
    # render.js reads a $pfg member. `at_anchor` names the wv-wrap render site, but the
    # engine HOISTS the inject to that function's module scope, so render.js's module-level
    # statics (PFTOK, BAND_TONES, the helper fns) and any $pfg subset land ONCE at module
    # scope, not re-created per render (the earlier per-render follow-up is done). The site
    # is then wrapped by the splice off that one anchor match.
    Rule("inject render.js (K render-wrap fn) into webview", "inject", _WV,
         {"src": "render.js", "at_anchor": "_pv_wvFactory"}),
    # ---- webview target: wrap BOTH render-dispatcher branches with the K gutter ----
    # pfgkDecorate(t,e,b,_ws,i) = pfgkBandGutter(pfgkRenderWrap(t,e,b,_ws), t,e,b,i): the OUTER
    # band gutter over the card logic, keyed on i (the vendor list index, NOT t.uuid: the other
    # U8t branches keep index keys, so uuid-keying just these two would give inconsistent
    # sibling keys). It self-gates (bandTone returns null on a healthy transcript and on ghosts
    # -> passthrough), so wrapping both branches unconditionally is safe. Both branches take the
    # SAME shape: bind the vendor element to _ws (edit on group 2, `return ` -> `let _ws=`), then
    # return pfgkDecorate(...) before the branch close (edit on group 7, `}` preserved via
    # %(g7)s). Both edits land inside the one anchor match, so bind + return are all-or-nothing:
    # the dead-code-after-return ship bug is structurally impossible. Group layout is owned by
    # the two wv anchors (user: _pv_wvFactory site; assistant: _pv_wvAsstMsg site; both 2=`return
    # `, 7=branch-close `}`). message/session/factory/index are U8t's shared params, bound by the
    # user anchor's captures, so the assistant splice reuses those discovered names.
    Rule("splice webview user-branch render-wrap + gutter", "splice", _WV, {
        "anchor": "_pv_wvFactory",
        "edits": [
            {"group": 2, "template": "let _ws="},
            {"group": 7,
             "template": ";return pfgkDecorate(%(_pv_wvMessage)s,%(_pv_wvSession)s,%(_pv_wvFactory)s,_ws,%(_pv_wvIndex)s)%(g7)s"},
        ],
    }),
    Rule("splice webview assistant-branch gutter", "splice", _WV, {
        "anchor": "_pv_wvAsstMsg",
        "edits": [
            {"group": 2, "template": "let _ws="},
            {"group": 7,
             "template": ";return pfgkDecorate(%(_pv_wvMessage)s,%(_pv_wvSession)s,%(_pv_wvFactory)s,_ws,%(_pv_wvIndex)s)%(g7)s"},
        ],
    }),
]
