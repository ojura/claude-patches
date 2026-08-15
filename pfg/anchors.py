"""
anchors: the structural anchor registry. The SINGLE home for each vendor dep's
IDENTITY (abstract name + kind + semantic label + per-target locator). The core
(src/) names deps abstractly and does NOT restate their meaning, so the core and
this registry cannot silently drift (the fix for the semantic-double-definition,
the NJ-desync disease one level up).

  Dep.kind:
    "bind"    the core CALLS it    -> alias-prologue binding (const _pv_X = <name>)
    "locate"  the core REPLACES it -> wholesale target, located not bound
    "builtin" bound by fresh require(), never discovered (module id is stable)
    "literal" a DISCOVERED CONSTANT the core references (e.g. a CSS-module hash): the
              alias prologue bakes it as a STRING LITERAL (const _pv_X = "<value>") after
              shape-validating it, distinct from a "bind" symbol REFERENCE. Coverage counts
              it (like bind/builtin), so an unanchored discovered-constant _pv_ fails loud.
    "capture" a vendor LOCAL at a splice site (a render param, a factory), threaded
              into a rule's injection text. Stored in `names` (so the rule composes
              + cross-checks the site), but NOT alias-bound (unlike "bind") and NOT
              a wholesale target (unlike "locate"), and DELIBERATELY not
              coverage-checked: coverage counts only bind/builtin/literal, because a capture
              is consumed by the RULE, not referenced in the injected src. The
              webview wrap rule reads these to build its `pfgkRenderWrap(...)` call.

One core surface, N target anchor sets: Anchor.regexes is keyed by target.
"extension" is the built target; "webview" (the K render-wrap splice site in
webview/index.js) is added below. When the CLI lands, add "cli" regexes here
(measured: 4 of 7 of the pfg-core anchors need a CLI-specific variant) and the
core does not move.
"""
import re
from collections import namedtuple

ID = r"[A-Za-z_$][\w$]*"

# A vendor dependency the core references; its identity owned here, once.
Dep = namedtuple("Dep", "pv kind label")
# One structural match resolving one or more deps' current names.
#   deps: list of (Dep, capture_group);  regexes: {target: pattern}
Anchor = namedtuple("Anchor", "deps regexes")

# node builtins: bound by fresh require(), not discovered. Each carries the set of
# targets whose core actually requires it, so a browser-side target (webview) is not
# handed node:path / node:fs and then charged with a DEAD-builtin coverage failure
# for not referencing them. The module string stays at index 1 for engine's
# _alias_prologue (BUILTINS[pv][1]).
BUILTINS = {
    "_pv_zn": (Dep("_pv_zn", "builtin", "node:path"), "path", frozenset({"extension", "cli"})),
    "_pv_MY": (Dep("_pv_MY", "builtin", "node:fs/promises"), "fs/promises", frozenset({"extension", "cli"})),
}

ANCHORS = [
    Anchor([(Dep("_pv_qAe", "bind", "session-ref -> {filePath,fileSize} resolver"), 1)],
           {"extension":
                rf'async function ({ID})\(({ID}),{ID}\)\{{let {ID}=`\$\{{\2\}}\.jsonl`'}),
    Anchor([(Dep("_pv_r1e", "bind", "head-buffer reader"), 1)],
           {"extension":
                rf'async function ({ID})\({ID},{ID}\)\{{try\{{if\({ID}>{ID}'
                rf'.{{0,40}}?CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP'}),
    Anchor([(Dep("_pv_n1e", "bind", "JSONL transcript parser"), 1)],
           {"extension":
                rf'async function ({ID})\({ID}\)\{{let {ID}=\[\];try\{{'
                rf'.{{0,60}}?parseTranscriptEntries'}),
    Anchor([(Dep("_pv_o1e", "bind", "renderer / tool-result reattach"), 1)],
           {"extension":
                rf'function ({ID})\({ID},({ID}),{ID}\)\{{let {ID}=\2\.filter\('
                rf'\({ID}\)=>{ID}\.type==="assistant"\)'}),
    Anchor([(Dep("_pv_s1e", "bind", "tool-result carrier guard"), 1)],
           {"extension":
                rf'function ({ID})\(({ID})\)\{{if\(\2\.type!=="user"\|\|!\2\.parentUuid\)return!1'}),
    # The final transcript-output predicate. Anthropic excludes `teamName` here so a
    # leader view does not render child-agent records. A teammate's OWN JSONL carries
    # teamName on every turn, however, so the same unconditional guard empties that
    # transcript. Capture only that guard for a one-site deletion; the surrounding
    # type/system/meta/sidechain grammar pins the intended predicate uniquely.
    Anchor([(Dep("_pv_teamNameVisibilityGuard", "capture", "final transcript teamName rejection"), 4)],
           {"extension":
                rf'function ({ID})\(({ID}),({ID})\)\{{'
                rf'if\(\2\.type==="user"\|\|\2\.type==="assistant"\);'
                rf'else if\(\2\.type==="system"&&\3\);else return!1;'
                rf'if\(\2\.isMeta\)return!1;if\(\2\.isSidechain\)return!1;'
                rf'(if\(\2\.teamName\)return!1;)return!0\}}'}),
    # GY + i1e co-discovered from GY's body: they MUST resolve to the SAME
    # chain-builder or the _tel splice threads into one function while i1e-wholesale
    # replaces another. This ONE grammar is ALSO what engine.py's _tel splice reuses
    # (via anchor_for), so GY's shape is defined once: the splice reads the captured
    # params and rewrites group 1. Group layout (relied on by the engine):
    #   1 = the signature prefix the splice replaces (async function GY(e,t){let r=await i1e(e),)
    #   2 = GY   3 = e (1st param)   4 = t (2nd param)   5 = r (i1e result var)   6 = i1e
    # Tolerates an already-threaded ,_tel so discovery stays idempotent pre/post patch.
    Anchor([(Dep("_pv_GY", "bind", "chain-builder caller / render entry"), 2),
            (Dep("_pv_i1e", "locate", "the chain-builder (wholesale target)"), 6)],
           {"extension":
                rf'(async function ({ID})\(({ID}),({ID})(?:,_tel)?\)\{{'
                rf'let ({ID})=await ({ID})\(\3(?:,_tel)?\),){ID}=\4\?\.includeSystemMessages'}),
    # d1e + Nk co-discovered from the loader's first statements: Nk's own def shape
    # is shared by a second validator (verify-once caught 2), so it is resolved
    # from where d1e uniquely calls it.
    Anchor([(Dep("_pv_d1e", "locate", "the loader (wholesale target)"), 1),
            (Dep("_pv_Nk", "bind", "session-ref string guard"), 4)],
           {"extension":
                rf'async function ({ID})\(({ID}),({ID})\)\{{if\(!({ID})\(\2\)\)return\[\];let {ID}=await '}),
    # ---- webview target (webview/index.js): the K render-wrap splice site ----
    # The user-message render branch (inside the per-message dispatcher). ONE
    # structural anchor over the whole branch tail, so (a) the two edits (binding
    # conversion + card return) are provably the SAME site, and (b) the locals the
    # injected `pfgkRenderWrap(message,session,factory,_ws)` call needs are captured in
    # one match. The "capture" deps are those locals; the wrap rule threads them into
    # the call, never alias-bound. The inject rule's at_anchor points at THIS anchor, so
    # the render body lands at this branch, inside the per-message dispatcher (render.js
    # carries module-level statics, so a future dispatcher-start at_anchor would place it
    # once rather than per render). Group layout the rules rely on:
    #   1 = message var (the `.type`/`.parentToolUseId`/`.isSynthetic`/`message:` spine)
    #   2 = the `return ` token -> `let _ws=`            (binding-conversion edit)
    #   3 = JSX factory  4 = user-message component  5 = session  6 = index (also key)
    #   7 = the branch-close `}` -> `;return pfgkRenderWrap(<1>,<5>,<3>,_ws)}` (card edit)
    # `[^}]{0,400}` tolerates the volatile middle props (isHighlighted, thinking-block
    # setters, ...); it cannot cross a brace, so a future prop with an object default
    # fails the match LOUD (find-one -> re-anchor) rather than binding a wrong site.
    # Matches the modern bare-factory JSX-runtime call `b(Comp,cfg,key)`; a classic
    # `X.default.createElement` bundle (<=2.1.173) correctly gets 0 matches here and
    # must add its own regex variant rather than silently mis-binding.
    Anchor([(Dep("_pv_wvFactory", "capture", "webview JSX element factory (b in b(Comp,cfg,key))"), 3),
            (Dep("_pv_wvUserMsg", "capture", "webview user-message component"), 4),
            (Dep("_pv_wvSession", "capture", "webview render session param"), 5),
            (Dep("_pv_wvMessage", "capture", "webview render message param"), 1),
            (Dep("_pv_wvIndex", "capture", "webview render index/key param"), 6)],
           {"webview":
                rf'if\(({ID})\.type==="user"\)\{{'
                rf'if\(\1\.parentToolUseId\)return null;'
                rf'if\(\1\.isSynthetic\)return null;'
                rf'(return )({ID})\(({ID}),\{{session:({ID}),message:\1,index:({ID}),'
                rf'[^}}]{{0,400}}\}},\6\)(\}})if\(\1\.type==="assistant"\)'}),
    # ---- webview target: the ASSISTANT-side decorate site (bands gutter, task 12B) ----
    # The same dispatcher's assistant branch, parallel to the user branch above:
    # `if(t.type==="assistant"){<hidden-tool-use guard>return null;return b(z8t,{session:e,
    # message:t,index:i,...},i)}if(t.type==="meta")`. The gutter must paint respliced
    # assistant turns too, so this is a second wv-wrap site inside the SAME enclosing fn.
    # It captures only the NEW local, the assistant-message component (z8t); message,
    # session, factory and index are U8t's params, identical to the user branch, so the
    # gutter rule reads those from `names` (bound by the user anchor) and only needs this
    # anchor's own edit positions (group 2 = `return `, group 7 = the branch-close `}`).
    # `[\s\S]{0,220}?` skips the hidden-tool-use guard; the `return ({ID})\(` that follows
    # forces the match onto `return b(z8t,...` rather than the guard's `return null;`.
    # Group layout mirrors the user anchor: 1=message, 2=`return `, 3=factory,
    # 4=assistant component, 5=session, 6=index, 7=branch-close `}`.
    Anchor([(Dep("_pv_wvAsstMsg", "capture", "webview assistant-message component"), 4)],
           {"webview":
                rf'if\(({ID})\.type==="assistant"\)\{{'
                rf'[\s\S]{{0,220}}?(return )({ID})\(({ID}),\{{session:({ID}),message:\1,index:({ID}),'
                rf'[^}}]{{0,400}}\}},\6\)(\}})if\(\1\.type==="meta"\)'}),
]


def deps_for(target):
    """Every Dep this registry declares for `target` (bind + locate + builtin),
    keyed by abstract name. The single source of each dep's kind + label."""
    out = {}
    for a in ANCHORS:
        if target in a.regexes:
            for dep, _group in a.deps:
                out[dep.pv] = dep
    for pv, (dep, _mod, tgts) in BUILTINS.items():
        if target in tgts:
            out[pv] = dep
    for dep, _members in CSS_MODULE_DEPS.get(target, []):
        out[dep.pv] = dep
    return out


def anchor_for(pv):
    """The single anchor that resolves `pv` (the registry is 1:1 per dep), so a
    caller like the engine's _tel splice can REUSE an anchor's grammar (GY's) to
    locate + capture instead of re-encoding the same function shape a second time."""
    for a in ANCHORS:
        if any(dep.pv == pv for dep, _g in a.deps):
            return a
    raise SystemExit(f"[pfg anchors] no anchor resolves {pv}")


# Webview CSS-module hashes, DISCOVERED by co-occurrence (never hardcoded). The bundler
# hashes every CSS-module class as `<name>_<hash>`; the hash drifts per bundle, but the
# co-occurrence is a stable structural anchor, so this is the same discover-the-drifting-
# name discipline as the _pv_ anchors above. Hardcoding a hash (content_xGDvVg) and
# asserting it exists would re-key on the drifting bundler emit, the exact anti-pattern
# this system kills. render.js's card un-collapse <style> interpolates two discovered
# module hashes:
#   - _pv_cmBodyHash: the message-body module hash, shared by content / collapsed /
#     truncationGradient / buttonContainer; resolved as the unique intersection of three
#     of them (collapsed is redundant for discovery, so it is not a co-member here).
#   - _pv_actionButtonHash: the action-button module hash, resolved via its co-members
#     optionText / popupOption / popupHeader (never the literal hash).
# These are "literal" deps: a DISCOVERED CONSTANT (the bundler's per-module hash), a
# distinct kind from a "bind" vendor-symbol reference. Coverage counts "literal" in its
# called-set, so render.js and this registry still move in lockstep (an unanchored css
# _pv_ fails loud); discovery resolves each by co-occurrence and returns the BARE hash,
# which the alias prologue shape-validates (^[A-Za-z0-9]+$) and bakes as a string literal
# `const _pv_cmBodyHash = "xGDvVg"`.
CSS_MODULE_DEPS = {
    "webview": [
        (Dep("_pv_cmBodyHash", "literal", "message-body CSS-module hash"),
         ("content", "truncationGradient", "buttonContainer")),
        (Dep("_pv_actionButtonHash", "literal", "action-button CSS-module hash"),
         ("optionText", "popupOption", "popupHeader")),
    ],
}


def _css_module_hash(js, co_members):
    """The single CSS-module hash shared by every `co_members` base (the intersection of
    their `<base>_<hash>` matches). Order-independent and duplicate-tolerant (sets)."""
    inter = None
    for base in co_members:
        hs = set(re.findall(rf"\b{base}_([A-Za-z0-9]+)\b", js))
        inter = hs if inter is None else (inter & hs)
    return inter or set()


def discover_css_hashes(js, target="extension"):
    """Resolve `target`'s CSS-module hashes by co-occurrence -> {_pv_: "hash"} (the BARE
    hash). These are "literal" deps: the alias prologue shape-validates the bare value and
    bakes it as a string literal (`const _pv_X = "hash"`), so the quotes are added once at
    emit, never carried in the discovered value. Exactly one hash per module, or die loud:
    0 shared = a base class name drifted (re-anchor the co-members); >1 = the co-members no
    longer pin a single module (add another). This IS both the discovery and the loud
    guard; the old hardcode-then-assert-presence check is retired."""
    out = {}
    for dep, members in CSS_MODULE_DEPS.get(target, []):
        h = _css_module_hash(js, members)
        if len(h) != 1:
            raise SystemExit(
                f"[pfg discover] {dep.pv}: co-members {members} resolved {len(h)} shared "
                f"hash(es) {sorted(h)}, expected exactly 1. A base class name drifted (0) or "
                f"the co-members no longer pin one module (>1); re-verify against the bundle.")
        out[dep.pv] = next(iter(h))
    return out
