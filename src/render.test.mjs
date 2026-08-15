// Faithfulness + structure tests for the webview render-wrap lift (Patch K cards).
//
//   run:  node --test src/render.test.mjs
//
// TWO arms, driven on IDENTICAL inputs:
//   - REFERENCE arm: the shipped MINIFIED render-wrap, extracted byte-for-byte
//     from prebuilt/2.1.195/apply.py by util/extract_render_wrap.py. This is the
//     ground truth the readable source must reproduce.
//   - READABLE arm: src/render.js (wv-1's lift). Activates the moment it lands.
//
// The faithfulness check renders each PFGK1 ghost kind through BOTH arms with the
// SAME mock element factory `b` (each arm builds its own `_h` shim over it, so a
// shim that flattens children differently is caught) and deep-equals the element
// trees. Functions (the onClick nav closure) can't be structurally compared, so
// the tree diff treats them as equal-by-kind and a separate BEHAVIORAL test drives
// onClick against a mock document. `_pfDiagram(kind,theme)` is a pure
// (kind,theme)->string, so it is diffed by direct `===`.
//
// SCOPE (measured in the prebuilt): the shipped wrap recognizes exactly five roles
// -- bookend, broken, seam, seamClean, bridge -- so those are the only kinds with a
// minified reference to diff against. The `pfgk-resplice-` CHAIN CORRUPT banner and
// the task-12B four-tone gutter are GREENFIELD (src/i1e.js emits them; the prebuilt
// predates both), so they have no reference and are covered by explicit assertion.

import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import * as nodePath from "node:path";

const HERE = nodePath.dirname(new URL(import.meta.url).pathname);
const REPO = nodePath.join(HERE, "..");

// ---------------------------------------------------------------------------
// Reference arm: pull the shipped minified wrap body + isolated _pfDiagram.
// ---------------------------------------------------------------------------
const REF = JSON.parse(
  execFileSync("python3", [nodePath.join(REPO, "util", "extract_render_wrap.py"), "--what", "json"], {
    encoding: "utf8",
  }),
);
// REF.body    : the runnable card block, references only t / e / b / document,
//               ends in `return _ws`.
// REF.diagram : the isolated `function _pfDiagram(kind, T){...}` text.
const referenceWrap = new Function("t", "e", "b", "_ws", REF.body);
const referenceDiagram = new Function(REF.diagram + "\nreturn _pfDiagram;")();

// The shipped un-collapse CSS literal, pulled from the reference wrap's <style> child.
// Single source for Option A's CSS leg: render.js's UNCOLLAPSE_CSS (with its _pv_ hash
// deps filled) must equal this byte-for-byte.
const REF_UNCOLLAPSE_CSS = (() => {
  const m = REF.body.match(/_h\("style",\s*\{key:"_s"\},\s*("(?:[^"\\]|\\.)*")\)/);
  return m ? JSON.parse(m[1]) : null;
})();

// ---------------------------------------------------------------------------
// Readable arm: src/render.js (wv-1). Absent until it lands -> its tests skip.
// The harness expects an entry that wraps (message, session, factory, default)
// and a standalone SVG builder. Names are resolved leniently across a few
// plausible exports so a small naming choice by wv-1 does not silently skip the
// whole faithfulness suite; if render.js exists but neither shape is found, fail
// LOUD rather than skip (a present-but-unwired lift is the thing we must catch).
// ---------------------------------------------------------------------------
// render.js references the discovered CSS-module hashes as _pv_ deps (Option A):
// _pv_cmBodyHash = the message-body module hash (content_/truncationGradient_/
// buttonContainer_), _pv_actionButtonHash = the action-button module hash. render.js
// stays export-const/-function with no imports and no $pfg, so we EVAL it with those two
// bound to the known 2.1.195 values -- a bare import would ReferenceError on the free
// _pv_ names. Same idiom as i1e.test.mjs. The filled UNCOLLAPSE_CSS must then match the
// shipped literal byte-for-byte (asserted below): Option A's CSS faithfulness leg.
const PV_CM_BODY_HASH = "xGDvVg";
const PV_ACTION_BUTTON_HASH = "v2CdxQ";
let readable = null;
const RENDER_JS = nodePath.join(HERE, "render.js");
if (existsSync(RENDER_JS)) {
  const src = readFileSync(RENDER_JS, "utf8");
  // render.js references $pfg.isGhostUuid (out-of-band ghost detection on the uuid) and
  // $pfg.PFGK1_PREFIX (the payload envelope constant). Prepend the codegen's WEBVIEW $pfg
  // subset -- the exact {isGhostUuid, PFGK1_PREFIX, GHOST_PREFIX} closure wv-2's engine injects
  // into the webview at apply time -- so render.js links against the REAL primitives, not a
  // stub. Same idiom as i1e.test.mjs prepending the pfg block.
  const pfgBlock = execFileSync("python3", [nodePath.join(REPO, "util", "pfg-codegen.py"), "--target", "webview"], { encoding: "utf8" });
  const exportNames = [...src.matchAll(/^export\s+(?:const|function)\s+([A-Za-z_$][\w$]*)/gm)].map((m) => m[1]);
  if (exportNames.length === 0) throw new Error("src/render.js: no `export const/function` declarations found to eval");
  const factoryBody = pfgBlock + "\n" + src.replace(/^export\s+/gm, "") + "\nreturn { " + exportNames.join(", ") + " };";
  const mod = new Function("_pv_cmBodyHash", "_pv_actionButtonHash", factoryBody)(PV_CM_BODY_HASH, PV_ACTION_BUTTON_HASH);
  const wrap = mod.pfgkRenderWrap || mod.renderWrap || mod.render || mod.pfgkWrap;
  const diagram = mod.pfDiagram || mod.pfgDiagram || mod.diagram || mod.buildDiagram; // frozen name: pfDiagram
  const gutter =
    mod.pfgkBandGutter || mod.bandGutter || mod.pfBandGutter || mod.applyBandGutter ||
    mod.renderMessageGutter || mod.gutter;
  if (typeof wrap !== "function") {
    throw new Error(
      "src/render.js is present but exports no render entry the harness recognizes " +
        "(tried pfgkRenderWrap / renderWrap / render / pfgkWrap). wv-5 needs a callable " +
        "(message, session, factory, defaultElement) -> element.",
    );
  }
  readable = {
    wrap,
    diagram: typeof diagram === "function" ? diagram : null,
    gutter: typeof gutter === "function" ? gutter : null,
    bandTones: mod.BAND_TONES || null,
    pftok: mod.PFTOK || null,
    uncollapseCss: typeof mod.UNCOLLAPSE_CSS === "string" ? mod.UNCOLLAPSE_CSS : null,
    decorate: typeof mod.pfgkDecorate === "function" ? mod.pfgkDecorate : null,
  };
}
const HAS_READABLE = readable !== null;
const skipReadable = HAS_READABLE ? false : "src/render.js not landed yet (wv-1)";
const skipGutter = !HAS_READABLE
  ? "src/render.js not landed yet (wv-1)"
  : !readable.gutter
  ? "gutter fn not exported yet (task 12 / wv-4 spec); tried pfgkBandGutter / bandGutter / pfBandGutter / applyBandGutter / renderMessageGutter / gutter"
  : false;
const skipDiagram = !HAS_READABLE
  ? "src/render.js not landed yet (wv-1)"
  : !readable.diagram
  ? "src/render.js exports no standalone diagram builder (pfgDiagram); SVG covered via the tree diff"
  : false;

// ---------------------------------------------------------------------------
// Mocks. The SAME factory `b` feeds both arms; each arm's own `_h` shim wraps it.
// ---------------------------------------------------------------------------
// Vendor factory modeled as the JSX RUNTIME: b(type, config, key). Children live in
// config.children and the 3rd positional is the KEY (config.key wins if present).
// Deliberately NOT classic createElement(type, props, ...children): were it classic,
// a regression that passed the element as the 3rd positional would land it as a
// child and MASK the bug wv-4 caught; here it lands as `key`, and the gutter's
// "el is the single child / key = i" assertions catch it. Both faithfulness arms
// share this exact factory (their _h shims put children + key into config).
const b = (type, config, key) => {
  const cfg = config || {};
  const { children, key: ckey, ...props } = cfg;
  return { type, props, key: ckey !== void 0 ? ckey : key, children };
};

// Session signal the counter reads (e.messages.peek()); include the fixture uuid so
// the "Marker N of M" counter renders deterministically for both arms.
const makeSession = (uuids) => ({ messages: { peek: () => uuids.map((uuid) => ({ uuid })) } });

// A minimal global document so the wrap's onClick closure can be *constructed*
// during render (it is only *invoked* in the behavioral test, which installs its
// own richer document).
globalThis.document = { querySelectorAll: () => [] };

// ---------------------------------------------------------------------------
// Fixtures: one PFGK1 ghost per kind. Payload fields mirror what src/i1e.js emits.
// Content is delivered in the ASSEMBLED shape (t.content = [{content:{text}}]) that
// the IDE's message assembler produces at render time (SKILL.md data-channel
// contract); a second fixture exercises the raw t.message.content string shape.
// ---------------------------------------------------------------------------
const assembled = (uuid, payload) => ({
  uuid,
  type: "user",
  content: [{ content: { text: "PFGK1:" + JSON.stringify(payload) } }],
});
const rawString = (uuid, payload) => ({
  uuid,
  type: "user",
  message: { role: "user", content: "PFGK1:" + JSON.stringify(payload) },
});

const TM = "K stitching wall-clock: parse 3ms · cross-file 1ms · sibling 4ms · bookend 0ms";

const PAYLOADS = {
  bookend: {
    kind: "bookend", // i1e/d1e stamp kind in every ghost; render.js keys role/theme on it (post-swap)
    headline: "Conversation origin · reconstructed in-file",
    rows: [["bridged across", "in-file compaction(s)"], ["walk terminus", "abc12345"]],
    body: "The walk bridged back across one or more in-file compactions to a legitimate root, so the view is complete.",
    tm: TM,
  },
  broken: {
    kind: "broken",
    badge: "⛔ TRANSCRIPT INCOMPLETE · Conversation root not found",
    glyph: "⛔",
    headline: "Conversation root not found in the saved files",
    rows: [["walk terminus", "abc12345 (user)"], ["parentUuid", "none"], ["lpu", "none"], ["root reached", "NO"]],
    body: "The deepest saved message is a continuation summary, so the conversation's first prompt predates the saved files.",
    tm: TM,
  },
  seam: {
    kind: "seam",
    // no badge/glyph/headline -> falls back to the seam theme tokens
    rows: [["missing phantom", "def67890 ✗"], ["reattached to", "abc12345"], ["bug origin", "compact.ts:598 (write-side)"]],
    body: "Claude Code compacted the conversation here. Patch K reconnected the chain via the in-file predecessor.",
  },
  seamClean: {
    kind: "seamClean",
    badge: "◇ IN-FILE COMPACTION",
    headline: "Compaction event · crossed in-file",
    rows: [["boundary", "abc12345"], ["bridged via", "in-file predecessor"]],
    body: "Claude Code compacted the conversation here; the messages above were bridged across this boundary in-file.",
  },
  bridge: {
    kind: "bridge",
    badge: "↻ CROSS-FILE BRIDGE",
    headline: "Compaction origin · bridged from a sibling conversation",
    rows: [["phantom (J-resolved)", "def67890 ✗"], ["cross-file source", "sibling.jsonl"], ["boundary", "abc12345"]],
    body: "This compaction's pre-boundary lineage lives in a sibling .jsonl. Patch J resolved it cross-file and Patch K bridges it in.",
  },
};

// The greenfield banner (no minified reference): CHAIN CORRUPT + per-message bands.
const RESPLICE_PAYLOAD = {
  kind: "resplice",
  badge: "⚠️ CHAIN CORRUPT · RESPLICED", // amber tier (lead ruling); mirrors i1e.js's actual emit
  glyph: "⚠️",
  headline: "Transcript respliced · chain corruption",
  rows: [["messages shown", "42"], ["in resume context", "8"], ["summarized away", "30"], ["damage", "stranded history"]],
  body: "The saved conversation chain is corrupt, so the normal lineage walk could not show the whole transcript. Every saved message is shown here in write order.",
  bands: { live: ["u1", "u2"], sev: ["u3"], unc: ["u4"] },
  tm: TM,
};

const KINDS = ["bookend", "broken", "seam", "seamClean", "bridge"];
const uuidFor = (kind) => "pfgk-" + kind + "-" + "abc12345";
// A stable session listing three markers; the fixture ghost is the 2nd -> "Marker 02 of 03".
const SESSION_UUIDS = ["pfgk-bookend-zzz", uuidFor("__self__"), "pfgk-seam-yyy"];
const sessionFor = (kind) => makeSession(["pfgk-bookend-zzz", uuidFor(kind), "pfgk-seam-yyy"]);

const themeFor = (kind) => {
  // The _PFTOK tokens _pfDiagram needs (accent/text/textDim). Pulled from the
  // reference wrap by rendering once and reading back the resolved style is
  // overkill; the diagram only reads these three, so supply them directly.
  const T = {
    bookend: { accent: "#7ddcff", text: "#fff", textDim: "#b8d8e8", panel: "#0c1c25" },
    broken: { accent: "#ff6b6b", text: "#fff", textDim: "#e8b8b8", panel: "#2a1010" },
    seam: { accent: "#ffc168", text: "#fff", textDim: "#e8d4a8", panel: "#2a1f0c" },
    bridge: { accent: "#ff9c5e", text: "#fff", textDim: "#e8c4a8", panel: "#2a1810" },
    seamClean: { accent: "#93a6c4", text: "#fff", textDim: "#c2cad8", panel: "#0f141d" },
  };
  return T[kind];
};

// ---------------------------------------------------------------------------
// Tree helpers.
// ---------------------------------------------------------------------------
// Every string that renders as visible text or markup, gathered from children
// (text nodes) and the svg __html prop. Deliberately excludes style values.
function textOf(node, acc = []) {
  if (node == null) return acc;
  if (typeof node === "string") {
    acc.push(node);
    return acc;
  }
  if (Array.isArray(node)) {
    for (const c of node) textOf(c, acc);
    return acc;
  }
  if (typeof node === "object") {
    const html = node.props && node.props.dangerouslySetInnerHTML && node.props.dangerouslySetInnerHTML.__html;
    if (typeof html === "string") acc.push(html);
    textOf(node.children, acc);
  }
  return acc;
}
const rendered = (node) => textOf(node).join("\n");

// Order-insensitive on object keys, order-SENSITIVE on children arrays (visual
// order matters), function-blind (onClick tested behaviorally). Throws a
// path-precise message on the first divergence.
function assertTreeEqual(ref, got, path = "$") {
  if (typeof ref === "function" || typeof got === "function") {
    assert.equal(typeof got, typeof ref, `${path}: function-ness differs`);
    return;
  }
  if (ref === null || got === null || typeof ref !== "object" || typeof got !== "object") {
    assert.deepEqual(got, ref, `${path}: leaf mismatch`);
    return;
  }
  if (Array.isArray(ref) || Array.isArray(got)) {
    assert.ok(Array.isArray(ref) && Array.isArray(got), `${path}: array-ness differs`);
    assert.equal(got.length, ref.length, `${path}: children count differs`);
    for (let i = 0; i < ref.length; i++) assertTreeEqual(ref[i], got[i], `${path}[${i}]`);
    return;
  }
  assert.deepEqual(Object.keys(got).sort(), Object.keys(ref).sort(), `${path}: prop keys differ`);
  for (const k of Object.keys(ref).sort()) assertTreeEqual(ref[k], got[k], `${path}.${k}`);
}

// ===========================================================================
// 1. Reference-arm structure. Validates the mocks/fixtures against the shipped
//    bytes AND documents the card contract the readable lift must reproduce.
// ===========================================================================
for (const kind of KINDS) {
  test(`reference: ${kind} ghost renders a pfgkAlert card with the expected structure`, () => {
    const t = assembled(uuidFor(kind), PAYLOADS[kind]);
    const node = referenceWrap(t, sessionFor(kind), b, { type: "PLAIN_BUBBLE" });

    assert.equal(node.type, "div", "root is a div");
    assert.equal(node.props.className, "pfgkAlert pfgk-" + kind, "className carries the role");
    assert.equal(node.props["data-pfgk-role"], kind, "data-pfgk-role set (used by onClick nav)");
    assert.equal(typeof node.props.onClick, "function", "onClick nav handler present");
    assert.ok(node.props.style && node.props.style.background, "inline theme applied");

    const text = rendered(node);
    assert.match(text, /PATCH K/, "PATCH K tag in header");
    assert.match(text, /Marker 02 of 03/, "zero-padded marker counter from e.messages.peek()");
    const expectBadge =
      PAYLOADS[kind].badge ||
      { seam: "⚠ IN-FILE REATTACH" }[kind]; // seam has no payload badge -> theme fallback
    if (expectBadge) assert.ok(text.includes(expectBadge), `badge "${expectBadge}" rendered`);
    for (const [k, v] of PAYLOADS[kind].rows) {
      assert.ok(text.includes(String(k)), `row key "${k}" rendered`);
      assert.ok(text.includes(String(v)), `row value "${v}" rendered`);
    }
    assert.ok(text.includes(PAYLOADS[kind].body), "body paragraph rendered");
    assert.match(text, /<svg/, "SVG diagram injected");
    // un-collapse <style> so the long essays are not truncated
    assert.match(text, /\.pfgkAlert .*max-height:none/, "un-collapse style injected");
  });
}

test("reference: tm timing line renders on the terminal cards when supplied", () => {
  for (const kind of ["bookend", "broken"]) {
    const t = assembled(uuidFor(kind), PAYLOADS[kind]);
    const node = referenceWrap(t, sessionFor(kind), b, { type: "PLAIN_BUBBLE" });
    assert.ok(rendered(node).includes(TM), `${kind} card keeps the tm wall-clock line`);
  }
});

test("reference: a card with no tm omits the timing line (no empty node)", () => {
  const t = assembled(uuidFor("seam"), PAYLOADS.seam); // seam payload has no tm
  const node = referenceWrap(t, sessionFor("seam"), b, { type: "PLAIN_BUBBLE" });
  assert.ok(!rendered(node).includes("wall-clock"), "no stray timing line when tm absent");
});

test("reference: the raw message.content string shape parses the same as assembled", () => {
  const raw = referenceWrap(rawString(uuidFor("bookend"), PAYLOADS.bookend), sessionFor("bookend"), b, null);
  const asm = referenceWrap(assembled(uuidFor("bookend"), PAYLOADS.bookend), sessionFor("bookend"), b, null);
  assertTreeEqual(asm, raw); // both content channels must yield the identical card
});

test("reference: a non-pfgk message is passed through untouched", () => {
  const plain = { uuid: "not-a-ghost", type: "user", message: { role: "user", content: "hello" } };
  const sentinel = { type: "PLAIN_BUBBLE" };
  const out = referenceWrap(plain, makeSession([]), b, sentinel);
  assert.equal(out, sentinel, "non-ghost returns the default element unchanged");
});

// ===========================================================================
// 2. INVARIANT (docs/invariant.md): never soften a marker / paint a false green.
// ===========================================================================
test("invariant: the broken card is red and never speaks the bookend's success language", () => {
  const node = referenceWrap(assembled(uuidFor("broken"), PAYLOADS.broken), sessionFor("broken"), b, null);
  assert.equal(node.props.className, "pfgkAlert pfgk-broken");
  assert.match(node.props.style.background, /^#3a1818$/i, "broken uses the red panel background, not cyan");
  const text = rendered(node);
  assert.ok(text.includes("TRANSCRIPT INCOMPLETE"), "loud failure badge present");
  assert.doesNotMatch(text, /reconstructed/i, "a failure card never says 'reconstructed'");
});

test("invariant: only the bookend card carries the green 'reconstructed' headline", () => {
  const bookend = rendered(referenceWrap(assembled(uuidFor("bookend"), PAYLOADS.bookend), sessionFor("bookend"), b, null));
  assert.match(bookend, /reconstructed/i, "bookend is the success card");
  for (const kind of ["broken", "seam"]) {
    const text = rendered(referenceWrap(assembled(uuidFor(kind), PAYLOADS[kind]), sessionFor(kind), b, null));
    assert.doesNotMatch(text, /origin · reconstructed/i, `${kind} must not mimic the green bookend headline`);
  }
});

// ===========================================================================
// 3. _pfDiagram: pure (kind,theme)->SVG string. Determinism now; faithfulness
//    `===` when the readable builder lands.
// ===========================================================================
for (const kind of KINDS) {
  test(`reference: _pfDiagram(${kind}) is a deterministic non-empty SVG`, () => {
    const svg = referenceDiagram(kind, themeFor(kind));
    assert.match(svg, /^<svg/, "starts with <svg");
    assert.match(svg, /<\/svg>$/, "ends with </svg>");
    assert.equal(svg, referenceDiagram(kind, themeFor(kind)), "same inputs -> same string");
  });
}

// ===========================================================================
// 4. FAITHFULNESS: readable src/render.js must reproduce the reference byte-for-byte
//    on every kind that has a minified reference. Activates when render.js lands.
// ===========================================================================
for (const kind of KINDS) {
  test(`faithfulness: ${kind} card tree matches the shipped wrap`, { skip: skipReadable }, () => {
    const t = assembled(uuidFor(kind), PAYLOADS[kind]);
    const ref = referenceWrap(t, sessionFor(kind), b, { type: "PLAIN_BUBBLE" });
    const got = readable.wrap(t, sessionFor(kind), b, { type: "PLAIN_BUBBLE" });
    assertTreeEqual(ref, got);
  });
}

for (const kind of KINDS) {
  test(`faithfulness: _pfDiagram(${kind}) SVG is byte-identical`, { skip: skipDiagram }, () => {
    assert.equal(readable.diagram(kind, themeFor(kind)), referenceDiagram(kind, themeFor(kind)));
  });
}

test("faithfulness: _pfDiagram(<no-diagram kind>) returns the same empty string in both arms", { skip: skipDiagram }, () => {
  // resplice + any unrecognized/future kind produce no card diagram; the minified original
  // and render.js must agree it is exactly "" (not a stray SVG, not undefined).
  for (const garbage of ["resplice", "futurekind", "", "xyz"]) {
    assert.equal(
      readable.diagram(garbage, themeFor("bookend")),
      referenceDiagram(garbage, themeFor("bookend")),
      `_pfDiagram(${JSON.stringify(garbage)}) matches (both empty)`,
    );
  }
});

test("faithfulness: filled UNCOLLAPSE_CSS is byte-identical to the shipped <style> literal (Option A CSS leg)", { skip: skipReadable }, () => {
  assert.ok(REF_UNCOLLAPSE_CSS, "extracted the shipped un-collapse CSS from the reference wrap");
  assert.ok(readable.uncollapseCss, "render.js exports UNCOLLAPSE_CSS");
  assert.equal(
    readable.uncollapseCss,
    REF_UNCOLLAPSE_CSS,
    "render.js UNCOLLAPSE_CSS (its _pv_ hash deps filled with the 2.1.195 values) == the shipped <style> literal",
  );
});

test("faithfulness: onClick cycles to the next marker identically in both arms", { skip: skipReadable }, () => {
  // Build N mock role-elements; clicking element i must scroll (i+1)%N into view.
  const makeEls = () => Array.from({ length: 3 }, (_v, i) => ({ i, scrolledWith: null, scrollIntoView(opt) { this.scrolledWith = opt; } }));
  const drive = (wrap) => {
    const els = makeEls();
    globalThis.document = { querySelectorAll: () => els };
    const node = wrap(assembled(uuidFor("bookend"), PAYLOADS.bookend), sessionFor("bookend"), b, null);
    node.props.onClick({ currentTarget: els[1] });
    globalThis.document = { querySelectorAll: () => [] }; // restore
    return els.map((e) => e.scrolledWith);
  };
  assert.deepEqual(drive(readable.wrap), drive(referenceWrap), "same element scrolled, same options");
  const scrolled = drive(referenceWrap);
  assert.ok(scrolled[2] && scrolled[1] === null && scrolled[0] === null, "clicking index 1 scrolled index 2");
});

test("faithfulness: a minimal payload (empty rows, no body/tm) matches", { skip: skipReadable }, () => {
  // Exercises the empty-collection paths (_rows=[], _body="", no tm) where a lift
  // easily diverges: an omitted vs null child slot, or a dropped empty rows panel.
  const t = assembled(uuidFor("bookend"), { kind: "bookend", headline: "origin reconstructed" });
  assertTreeEqual(referenceWrap(t, sessionFor("bookend"), b, null), readable.wrap(t, sessionFor("bookend"), b, null));
});

test("faithfulness: the last marker renders '↺ cycle' identically", { skip: skipReadable }, () => {
  // The nav affordance flips on _last (_mi===_tot-1); the mid-list fixtures only
  // exercise "↓ next", so place the fixture ghost last to cover the cycle branch.
  const t = assembled(uuidFor("bookend"), PAYLOADS.bookend);
  const lastSession = makeSession(["pfgk-seam-yyy", "pfgk-bridge-xxx", uuidFor("bookend")]);
  const ref = referenceWrap(t, lastSession, b, null);
  assertTreeEqual(ref, readable.wrap(t, lastSession, b, null));
  assert.match(rendered(ref), /↺ cycle/, "last marker shows the cycle affordance, not next");
});

// ===========================================================================
// 5. GREENFIELD (no minified reference): the CHAIN CORRUPT resplice banner card
//    and the task-12B four-tone gutter. Explicit-assertion coverage.
// ===========================================================================
// The resplice role is greenfield (no minified reference in the prebuilt wrap), so
// this is assertion-only, NOT part of the faithfulness diff. Hard now that render.js
// maps the pfgk-resplice- role (task 12).
test("resplice: the CHAIN CORRUPT banner renders as a card, not a bare bubble", { skip: skipReadable }, () => {
  const t = assembled("pfgk-resplice-abc12345", RESPLICE_PAYLOAD);
  const node = readable.wrap(t, makeSession(["pfgk-resplice-abc12345"]), b, { type: "PLAIN_BUBBLE" });
  assert.notEqual(node && node.type, "PLAIN_BUBBLE", "resplice must map to a role (prebuilt drops it to a plain bubble -- the #12 bug)");
  assert.equal(node.props["data-pfgk-role"], "resplice", "resplice role assigned");
  const text = rendered(node);
  assert.ok(text.includes("CHAIN CORRUPT · RESPLICED"), "loud corrupt badge rendered");
  assert.ok(text.includes(TM), "resplice keeps its tm wall-clock line");
  assert.doesNotMatch(text, /<svg/, "resplice renders NO card topology diagram (deliberate: the per-message gutter below carries the corruption structure)");
  // invariant (docs/invariant.md): a corrupt-chain banner is a failure card -- red,
  // never the green bookend. Distinctiveness is the badge/headline, so assert not-green
  // rather than a specific red (resplice may reuse broken's palette or carry its own).
  assert.doesNotMatch(text, /reconstructed/i, "resplice never speaks the bookend's success language");
  assert.notEqual(node.props.style.background, "#142a35", "resplice is not the bookend (cyan/green) card");
  // AMBER tier, NOT broken-red (lead ruling): resplice is a distinct, softer failure tier
  // than broken (all messages shown, just disordered, vs origin gone). The two red/amber
  // tiers must never collapse to the same palette. Companion to the "broken is red" test.
  if (readable.pftok) {
    const R = readable.pftok.resplice, K = readable.pftok.broken;
    const hue = (hex) => { const n = parseInt(hex.slice(1), 16); return { g: (n >> 8) & 255, b: n & 255 }; };
    assert.equal(node.props.style.background, R.bg, "resplice card paints with the resplice (amber) background");
    assert.notEqual(R.bg, K.bg, "resplice bg differs from broken bg -- the two tiers do not collapse");
    assert.notEqual(R.accent, K.accent, "resplice accent differs from broken accent -- the two tiers do not collapse");
    assert.ok(hue(R.accent).g - hue(R.accent).b > 20, `resplice accent ${R.accent} is amber/orange (green >> blue), not red`);
    assert.ok(Math.abs(hue(K.accent).g - hue(K.accent).b) <= 10, `broken accent ${K.accent} is red (green ~= blue) -- the companion tier it must not match`);
  }
});

test("resplice: wears the warning sign, never broken's stop sign (severity-tier split)", { skip: skipReadable }, () => {
  // The SIGN must match the tier: resplice = irregularity (warning sign, amber), while
  // broken / INCOMPLETE = hard failure (the stop sign, red). A stop sign on the amber
  // resplice card is the contradictory-sign bug the lead re-corrected. Render-side default
  // via a glyph-less payload (falls back to PFTOK.resplice), so this is non-circular.
  const t = assembled("pfgk-resplice-abc12345", { kind: "resplice", headline: "H", rows: [["k", "v"]], body: "b", tm: TM });
  const text = rendered(readable.wrap(t, makeSession(["pfgk-resplice-abc12345"]), b, { type: "MSG" }));
  assert.ok(text.includes("⚠"), "resplice falls back to the warning sign (U+26A0)");
  assert.ok(!text.includes("⛔"), "resplice never shows the stop sign (U+26D4) that broken/INCOMPLETE use");
  if (readable.pftok) {
    const R = readable.pftok.resplice, K = readable.pftok.broken;
    assert.ok(R.glyph.includes("⚠") && !R.glyph.includes("⛔"), `resplice glyph ${R.glyph} is the warning sign, not the stop sign`);
    assert.ok(K.glyph.includes("⛔"), `broken glyph ${K.glyph} is the stop sign -- the companion tier`);
    assert.notEqual(R.glyph, K.glyph, "the two tier signs do not collapse");
    assert.ok(R.badge.startsWith("⚠") && !R.badge.includes("⛔"), "resplice badge uses the warning sign, not the stop sign");
  }
});

test("drift-guard: docs/invariant.md banner signs match the impl tier glyphs (doc<->impl link)", { skip: skipReadable }, () => {
  // Machine-checks the exact silent divergence class wv-5 caught: the written contract
  // (docs/invariant.md) must not disagree with the impl (render.js PFTOK) on the two
  // severity-tier signs. Scoped to the two banner glyph chars; a wording change that trips
  // this is a loud re-check signal, which is the intent. FE0F (the emoji variation selector)
  // is normalized so only the BASE sign (warning vs stop) is compared, not presentation.
  const inv = readFileSync(nodePath.join(REPO, "docs", "invariant.md"), "utf8");
  const baseSign = (s) => (s || "").replace(/\uFE0F/g, "");
  const signBefore = (phrase) => {
    const m = inv.match(new RegExp("([^\\s`\"]+)\\s+" + phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    return m ? baseSign(m[1]) : null;
  };
  const respliceSign = signBefore("CHAIN CORRUPT · RESPLICED");
  const brokenSign = signBefore("TRANSCRIPT INCOMPLETE · Conversation root not found");
  assert.ok(respliceSign, "found the CHAIN CORRUPT banner sign in invariant.md");
  assert.ok(brokenSign, "found the TRANSCRIPT INCOMPLETE banner sign in invariant.md");
  assert.ok(readable.pftok, "render.js exports PFTOK");
  assert.equal(respliceSign, baseSign(readable.pftok.resplice.glyph), "invariant.md CHAIN CORRUPT sign == PFTOK.resplice.glyph (amber irregularity tier)");
  assert.equal(brokenSign, baseSign(readable.pftok.broken.glyph), "invariant.md TRANSCRIPT INCOMPLETE sign == PFTOK.broken.glyph (red hard-failure tier)");
  assert.notEqual(respliceSign, brokenSign, "the two tier signs stay distinct in the contract too (warning vs stop)");
});

test("drift-guard (24B): EVERY pfgk-<kind>- ghost renders a card, never a bare bubble", { skip: skipReadable }, () => {
  // The resplice miss (a bare bubble for pfgk-resplice-) was one instance of a general
  // hazard: a NEW i1e ghost kind that the render role-detection does not recognize falls
  // through to the plain _ws bubble, silently, with no color/badge/nav. render.js parses
  // the role GENERICALLY and falls back to DEFAULT_THEME, so this pins that EVERY pfgk-
  // ghost -- the five lifted kinds, resplice, AND an unknown/future kind -- renders a
  // pfgkAlert card. No future i1e kind can regress to a passthrough without failing here.
  const sentinel = { type: "PLAIN_BUBBLE", props: {}, children: void 0 };
  for (const kind of ["bookend", "broken", "seam", "seamClean", "bridge", "resplice", "futurekind", "kindNotYetInvented"]) {
    const uuid = "pfgk-" + kind + "-abc12345";
    // payload carries `kind` (i1e/d1e stamp it in every ghost; lead's 24B option-B ruling).
    // Harmless under the current uuid-parse; forward-compatible with envelope+kind detection.
    const payload = { kind, badge: "◆ SOME BADGE", headline: "H", rows: [["k", "v"]], body: "b", tm: TM };
    const node = readable.wrap(assembled(uuid, payload), makeSession([uuid]), b, sentinel);
    assert.notEqual(node, sentinel, `${kind}: MUST render a card, never return _ws (the resplice-class bare-bubble regression)`);
    assert.equal(node.type, "div", `${kind}: card is a div`);
    assert.ok(String(node.props.className).startsWith("pfgkAlert pfgk-" + kind), `${kind}: className carries the generic role`);
    assert.equal(node.props["data-pfgk-role"], kind, `${kind}: data-pfgk-role is the generic kind segment`);
    const text = rendered(node);
    assert.ok(text.includes("SOME BADGE") && text.includes(payload.tm), `${kind}: the (default) card renders the payload badge + tm`);
  }
});

// The gutter (wv-4 spec) paints the NORMAL respliced messages (not the card): each
// message's uuid is looked up in the CHAIN CORRUPT banner's bands:{live,sev,unc}
// sets (read from e.messages.peek() -> _ren); an in-spine message in NO set is
// "summarized" (dim); a healthy transcript (no banner) and pfgk- ghosts get none.
// Greenfield: no minified reference, so assertion-only. Runs the moment render.js
// exports the gutter fn.
//
// ASSUMPTIONS wv-5 is holding until wv-4/wv-1 confirm (flagged to the lead): the
// gutter returns an element carrying data-pfgk-band at top level; the band-value
// strings mirror i1e's __pfgkBand (live / severed / uncertain / summarized); the
// tone COLORS are asserted only for DISTINCTNESS pending wv-4's palette.

// Local i1e factory (same idiom as i1e.test.mjs) to drive a REAL respliced _ren.
let _i1eFactory;
function i1eWith(d) {
  if (!_i1eFactory) {
    const src = readFileSync(nodePath.join(HERE, "i1e.js"), "utf8");
    const pfgBlock = execFileSync("python3", [nodePath.join(REPO, "util", "pfg-codegen.py")], { encoding: "utf8" });
    _i1eFactory = new Function("_pv_o1e", "_pv_s1e", pfgBlock + "\n" + src + "\nreturn i1e;");
  }
  return _i1eFactory(d.o1e, d.s1e);
}
const i1eDeps = { o1e: (t, u) => u.slice(), s1e: () => false };
const I1E_TEL = { timing: { parseMs: 5, crossFileMs: 3, siblingBackfillMs: 2, bookendMs: 1 }, siblingsScanned: 4, phantomsBackfilled: 0, phantomsCouldNotBackfill: 0, provBasenames: {} };
const parseGhost = (rec) => {
  const c = rec && rec.message && rec.message.content;
  if (typeof c !== "string" || !c.startsWith("PFGK1:")) return null;
  try { return JSON.parse(c.slice(6)); } catch { return null; }
};

const M = (uuid) => ({ uuid, type: "user", message: { role: "user", content: "a turn" } });
const BANDS = { live: ["m-live-1", "m-live-2"], sev: ["m-sev-1"], unc: ["m-unc-1"] };
const respliceBanner = {
  uuid: "pfgk-resplice-leaf",
  type: "user",
  message: {
    role: "user",
    content: "PFGK1:" + JSON.stringify({
      kind: "resplice", // bandTone finds the banner via isGhostUuid + payload.kind==="resplice"
      badge: "⚠️ CHAIN CORRUPT · RESPLICED", glyph: "⚠️", // amber tier (matches i1e's emit)
      headline: "Transcript respliced · chain corruption",
      rows: [["messages shown", "5"]], body: "corrupt", bands: BANDS, tm: TM,
    }),
  },
};
const RESPLICED_REN = [respliceBanner, M("m-live-1"), M("m-live-2"), M("m-sev-1"), M("m-unc-1"), M("m-sum-1")];
const resplicedSession = { messages: { peek: () => RESPLICED_REN } };
const healthySession = { messages: { peek: () => [M("h1"), M("h2")] } };
const bandOf = (node) => (node && node.props ? node.props["data-pfgk-band"] : undefined);
// pfgkBandGutter(el, t, e, b, i): element FIRST, then message, session, factory, index.
const gut = (t, session) => readable.gutter({ type: "MSG", props: {}, children: void 0 }, t, session, b, 0);

test("gutter: live/severed/uncertain messages get the right, distinct bands", { skip: skipGutter }, () => {
  const bl1 = bandOf(gut(M("m-live-1"), resplicedSession));
  const bl2 = bandOf(gut(M("m-live-2"), resplicedSession));
  const bs = bandOf(gut(M("m-sev-1"), resplicedSession));
  const bu = bandOf(gut(M("m-unc-1"), resplicedSession));
  assert.ok(bl1 && bs && bu, "each banded message carries a data-pfgk-band");
  assert.equal(bl1, bl2, "same band set -> same tone");
  // four-tone distinctness: a live<->severed swap would fail here even if strings drift
  assert.notEqual(bs, bl1, "severed differs from live");
  assert.notEqual(bu, bl1, "uncertain differs from live");
  assert.notEqual(bu, bs, "uncertain differs from severed");
  // exact value convention (mirrors i1e __pfgkBand); loosen if wv-4 chose other strings
  assert.equal(bl1, "live");
  assert.equal(bs, "severed");
  assert.equal(bu, "uncertain");
  // and the right border tone per band (exact palette from the exported BAND_TONES)
  if (readable.bandTones) {
    const border = (t) => gut(t, resplicedSession).props.style.borderLeft;
    assert.ok(border(M("m-live-1")).includes(readable.bandTones.live.color), "live gutter uses the live tone color");
    assert.ok(border(M("m-sev-1")).includes(readable.bandTones.severed.color), "severed gutter uses the severed tone color");
    assert.ok(border(M("m-unc-1")).includes(readable.bandTones.uncertain.color), "uncertain gutter uses the uncertain tone color");
  }
});

test("gutter: border STYLE codes the tone -- uncertain dashed, the rest solid (legibility)", { skip: skipGutter }, () => {
  // severed (orange) and uncertain (amber) sit close in hue, so uncertain is DASHED to separate
  // them by style AND signal "tentative"; live/severed/summarized stay solid. Non-vacuous: a
  // regression to all-solid fails the dashed assert.
  const style = (t) => gut(t, resplicedSession).props.style.borderLeft;
  assert.match(style(M("m-unc-1")), /^3px dashed /, "uncertain gutter is dashed");
  assert.match(style(M("m-sev-1")), /^3px solid /, "severed gutter is solid");
  assert.match(style(M("m-live-1")), /^3px solid /, "live gutter is solid");
  assert.match(style(M("m-sum-1")), /^3px solid /, "summarized gutter is solid");
});

test("gutter: wraps with the vendor list key and el as its single child (jsx-runtime, not classic createElement)", { skip: skipGutter }, () => {
  // The bug wv-4 caught: a classic createElement(type, props, el) wrap would land el
  // in the 3rd positional = KEY (jsx runtime), leaving children undefined -> the message
  // vanishes. The correct wrap puts el in config.children and the vendor index in
  // config.key. Both asserts below fail under the classic-createElement regression.
  const el = { type: "MSG", props: { id: "orig" }, children: void 0 };
  const w = readable.gutter(el, M("m-live-1"), resplicedSession, b, 7);
  assert.equal(bandOf(w), "live", "a banded message is wrapped");
  assert.equal(w.key, 7, "the vendor list index rides as the element key (config.key), not dropped");
  assert.equal(w.children, el, "el is the SINGLE child (config.children), never the 3rd-positional key");
});

test("gutter: an in-spine message in NO band set is summarized/dim", { skip: skipGutter }, () => {
  const bsum = bandOf(gut(M("m-sum-1"), resplicedSession));
  assert.ok(bsum, "an unlisted in-spine message still gets a (dim) gutter");
  assert.equal(bsum, "summarized");
  assert.notEqual(bsum, bandOf(gut(M("m-live-1"), resplicedSession)), "summarized differs from live");
});

test("gutter: a HEALTHY transcript (no CHAIN CORRUPT banner) paints zero gutters", { skip: skipGutter }, () => {
  const el = { type: "MSG", props: {}, children: void 0 };
  assert.equal(readable.gutter(el, M("h1"), healthySession, b, 0), el, "no banner -> element passes through unwrapped (identity), no data-pfgk-band");
});

test("gutter: pfgk- ghosts get no gutter and pass through by identity", { skip: skipGutter }, () => {
  const el = { type: "MSG", props: {}, children: void 0 };
  assert.equal(readable.gutter(el, respliceBanner, resplicedSession, b, 0), el, "the banner ghost itself passes through unwrapped");
  const cardGhost = { uuid: "pfgk-bookend-x", type: "user", message: { role: "user", content: "PFGK1:{}" } };
  assert.equal(readable.gutter(el, cardGhost, resplicedSession, b, 0), el, "a card ghost passes through unwrapped");
});

test("gutter: integration -- REAL i1e (wv-4 cycle fixture) emits the bands keys the gutter reads", { skip: skipGutter }, async () => {
  // wv-4's canonical repro: a 3-node cycle L->A->B->L forces the corrupt/resplice path.
  // This pins the emit->consume KEY contract end to end: i1e emits bands.{live,sev,unc}
  // as arrays, and the gutter reads those exact keys (a rename on either side breaks here).
  const i1e = i1eWith(i1eDeps);
  const L = { uuid: "L", type: "user", parentUuid: "A", sessionId: "s", timestamp: "t3", message: { role: "user", content: "leaf" } };
  const A = { uuid: "A", type: "assistant", parentUuid: "B", sessionId: "s", timestamp: "t2", message: { role: "assistant", content: "mid" } };
  const B = { uuid: "B", type: "user", parentUuid: "A", sessionId: "s", timestamp: "t1", message: { role: "user", content: "old" } };
  const ren = await i1e([B, A, L], I1E_TEL);
  const bannerRec = ren.find((r) => String(r.uuid).startsWith("pfgk-resplice-"));
  const banner = bannerRec && parseGhost(bannerRec);
  assert.ok(banner && banner.bands, "i1e emitted a pfgk-resplice- banner carrying bands");
  for (const k of ["live", "sev", "unc"]) {
    assert.ok(Array.isArray(banner.bands[k]), `banner.bands.${k} is an array (the exact key the gutter reads)`);
  }
  assert.ok(banner.bands.live.length > 0, "this cycle populates live");
  const session = { messages: { peek: () => ren } };
  for (const uuid of banner.bands.live) {
    const rec = ren.find((r) => r.uuid === uuid);
    if (rec) assert.equal(bandOf(gut(rec, session)), "live", `real live uuid ${uuid} -> live gutter`);
  }
});

test("gutter: integration -- real i1e severed + uncertain bands map to the right gutter tones", { skip: skipGutter }, async () => {
  // _postReliable is one bool per run, so severed and uncertain cannot co-occur; cover
  // each with its own real-i1e fixture (band assignment is i1e's emit-side territory --
  // this asserts the gutter CONSUMES i1e's real sev/unc uuids, closing the end-to-end loop).
  const i1e = i1eWith(i1eDeps);
  const find = (ren, u) => ren.find((r) => r.uuid === u);
  const bandsFrom = (ren) => parseGhost(ren.find((r) => String(r.uuid).startsWith("pfgk-resplice-"))).bands;

  // Fixture A: the live leaf walks to a boundary (reliable terminus) -> a post-boundary
  // orphan is SEVERED; the preserved message + leaf are LIVE.
  const BND = { uuid: "BND", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "OFFDISK", sessionId: "s", timestamp: "t0", compactMetadata: { preservedMessages: { uuids: ["PLIVE"], anchorUuid: "BND" } } };
  const PLIVE = { uuid: "PLIVE", type: "user", parentUuid: "BND", sessionId: "s", timestamp: "t0b", message: { role: "user", content: "preserved" } };
  const ORPH = { uuid: "ORPH", type: "assistant", parentUuid: "GONE", sessionId: "s", timestamp: "t1", message: { role: "assistant", content: "orphan" } };
  const LEAF = { uuid: "LEAF", type: "user", parentUuid: "BND", sessionId: "s", timestamp: "t2", message: { role: "user", content: "leaf" } };
  const renA = await i1e([BND, PLIVE, ORPH, LEAF], I1E_TEL);
  const bandsA = bandsFrom(renA);
  assert.ok(bandsA.sev.length > 0, "fixture A: i1e emits a non-empty severed band");
  const sessA = { messages: { peek: () => renA } };
  for (const u of bandsA.sev) assert.equal(bandOf(gut(find(renA, u), sessA)), "severed", `real severed ${u} -> severed gutter`);
  for (const u of bandsA.live) assert.equal(bandOf(gut(find(renA, u), sessA)), "live", `real live ${u} -> live gutter`);

  // Fixture B: the live leaf dead-ends off-disk (unreliable terminus) -> post-boundary
  // orphans are UNCERTAIN.
  const BND2 = { uuid: "BND", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "OFFDISK", sessionId: "s", timestamp: "t0" };
  const O2 = { uuid: "ORPH", type: "user", parentUuid: "BND", sessionId: "s", timestamp: "t1", message: { role: "user", content: "orphan" } };
  const Lf2 = { uuid: "LEAF", type: "assistant", parentUuid: "ORPH", sessionId: "s", timestamp: "t2", message: { role: "assistant", content: "leaf" } };
  const STR = { uuid: "STR", type: "user", parentUuid: "ALSO-GONE", sessionId: "s", timestamp: "t3", message: { role: "user", content: "stranded" } };
  const renB = await i1e([BND2, O2, Lf2, STR], I1E_TEL);
  const bandsB = bandsFrom(renB);
  assert.ok(bandsB.unc.length > 0, "fixture B: i1e emits a non-empty uncertain band");
  const sessB = { messages: { peek: () => renB } };
  for (const u of bandsB.unc) assert.equal(bandOf(gut(find(renB, u), sessB)), "uncertain", `real uncertain ${u} -> uncertain gutter`);
});

test("gutter: bandInfo memoizes per _ren -- banner reads are O(1), constant in N", { skip: skipGutter }, () => {
  // bandInfo is cached on the peek() result (_ren): the resplice banner is found + its bands
  // compiled ONCE per list, then every message's band is a map lookup -- so the banner content
  // is read a CONSTANT number of times regardless of how many messages share the _ren, not once
  // per message. Prove O(1) by measuring at N and 2N and asserting the read count is IDENTICAL (a
  // per-message re-parse would scale with N and the two would differ); the small-constant check
  // catches a gross regression. Robust to parsePayload's exact .content-access count (no magic 2).
  const runFor = (N) => {
    let reads = 0;
    const payload = JSON.stringify({ kind: "resplice", badge: "⚠️ CHAIN CORRUPT · RESPLICED", bands: { live: ["row-0"], sev: [], unc: [] }, body: "c" });
    const banner = { uuid: "pfgk-resplice-N", type: "user", message: { role: "user", get content() { reads++; return "PFGK1:" + payload; } } };
    const big = [banner, ...Array.from({ length: N }, (_v, i) => M("row-" + i))];
    const session = { messages: { peek: () => big } };
    for (const rec of big) if (!String(rec.uuid).startsWith("pfgk-")) gut(rec, session);
    return reads;
  };
  const atN = runFor(500), at2N = runFor(1000);
  assert.equal(atN, at2N, `banner reads must be constant in N (O(1) per-_ren memo): ${atN} at N=500 vs ${at2N} at N=1000`);
  assert.ok(atN <= 8, `banner read a small constant number of times (${atN}), not per-message`);
});

test("pfgkDecorate: composes wrap+gutter -- ghost -> card, ordinary -> banded, healthy -> passthrough", { skip: skipReadable }, () => {
  // pfgkDecorate(t,e,b,_ws,i) = pfgkBandGutter(pfgkRenderWrap(...), t, e, b, i): the single
  // entry both U8t branches call. A ghost gets the card (gutter no-ops on a pfgk- uuid); an
  // ordinary respliced message gets the gutter; a healthy message passes through.
  if (!readable.decorate) { assert.ok(true, "pfgkDecorate not exported yet"); return; }
  // 1. ghost -> the bare card (function-tolerant compare vs pfgkRenderWrap alone).
  const ghost = assembled(uuidFor("bookend"), PAYLOADS.bookend);
  assertTreeEqual(
    readable.wrap(ghost, sessionFor("bookend"), b, { type: "MSG" }),
    readable.decorate(ghost, sessionFor("bookend"), b, { type: "MSG" }, 3),
  );
  // 2. ordinary respliced message -> gutter-wrapped: key === i (the vendor index, NOT the uuid),
  //    the vendor element is the single child.
  const el = { type: "MSG", props: {}, children: void 0 };
  const w = readable.decorate(M("m-live-1"), resplicedSession, b, el, 9);
  assert.equal(bandOf(w), "live", "ordinary respliced message is gutter-banded");
  assert.equal(w.key, 9, "gutter carries the vendor index i as the React key, not the uuid");
  assert.equal(w.children, el, "the vendor element is the gutter's single child");
  // 3. healthy transcript (no resplice banner) -> passthrough unchanged.
  assert.equal(readable.decorate(M("h1"), healthySession, b, el, 0), el, "healthy -> passthrough, no gutter");
});

// ===========================================================================
// 7. OUT-OF-BAND detection guards (lead + architect ruling). Detection is by the
//    UUID PREFIX ("pfgk-", GHOST_PREFIX), never by the "PFGK1:" content envelope
//    (PFGK1_PREFIX), which is used ONLY to PARSE a confirmed ghost's payload. These
//    pass today (roleFromUuid is already out-of-band) AND fail loud if detection ever
//    regresses to in-band content-parsing -- the guard on the isGhostUuid swap.
// ===========================================================================
test("out-of-band: a NON-pfgk-uuid message carrying PFGK1: content is NOT a ghost (anti-spoof)", { skip: skipReadable }, () => {
  // The spoof vector wv-4 caught: a real user message (non-pfgk uuid) whose CONTENT happens to
  // lead with "PFGK1:" -- a paste, or a message discussing the format -- must render as the plain
  // user element, never a fake marker card. In-band content-detection would mis-card it.
  const sentinel = { type: "USER_BUBBLE" };
  const spoof = {
    uuid: "7f3a-9c21-real-user-uuid",
    type: "user",
    message: { role: "user", content: "PFGK1:" + JSON.stringify({ kind: "resplice", badge: "FAKE", note: "discussing the format" }) },
  };
  assert.equal(readable.wrap(spoof, makeSession([]), b, sentinel), sentinel, "non-pfgk uuid + PFGK1 content -> passthrough, never a pfgkAlert card");
  // and in a respliced transcript it gets a REAL band (ordinary message), not skipped as a ghost.
  if (readable.gutter) {
    const session = { messages: { peek: () => [respliceBanner, spoof] } };
    const w = readable.gutter({ type: "USER_BUBBLE" }, spoof, session, b, 0);
    assert.equal(bandOf(w), "summarized", "the spoof message gets a real (summarized) band, not null-skipped as a ghost");
  }
});

test("out-of-band: a pfgk- ghost with a MISSING kind still renders the DEFAULT card (no bare bubble)", { skip: skipReadable }, () => {
  // A ghost is confirmed out-of-band by its pfgk- uuid; a missing/unknown kind falls back to the
  // neutral DEFAULT card, never a bare bubble. (Under the isGhostUuid swap: kind comes from
  // payload.kind, absent here -> DEFAULT; today via the uuid parse -> same DEFAULT card.)
  const sentinel = { type: "USER_BUBBLE" };
  const ghost = { uuid: "pfgk-frobnicate-abc", type: "user", message: { role: "user", content: "PFGK1:" + JSON.stringify({ badge: "X", headline: "H", rows: [], body: "b" }) } };
  const node = readable.wrap(ghost, makeSession(["pfgk-frobnicate-abc"]), b, sentinel);
  assert.notEqual(node, sentinel, "a pfgk- ghost with a missing kind must NOT bare-bubble");
  assert.equal(node.type, "div", "renders a card");
  assert.ok(String(node.props.className).startsWith("pfgkAlert"), "the neutral pfgkAlert default card");
});

test("out-of-band: KIND comes from payload.kind, not the uuid (divergent discriminator)", { skip: skipReadable }, () => {
  // The definitive proof that render.js reads the kind from the PAYLOAD (i1e stamps it), not by
  // parsing the uuid: a ghost whose uuid says "seam" but whose payload.kind says "bridge" MUST
  // render as bridge. A uuid-parse would render "seam" (this test was red pre-swap by design);
  // payload.kind renders "bridge". Is-ghost stays out-of-band (the pfgk- prefix is present), so
  // this isolates the kind-SOURCE axis without touching the is-ghost gate.
  const t = assembled("pfgk-seam-DIVERGENT", { kind: "bridge", badge: "B", headline: "H", rows: [["k", "v"]], body: "b" });
  const node = readable.wrap(t, makeSession(["pfgk-seam-DIVERGENT"]), b, { type: "MSG" });
  assert.equal(node.props["data-pfgk-role"], "bridge", "role from payload.kind (bridge), NOT the uuid (seam)");
  assert.ok(String(node.props.className).includes("pfgk-bridge"), "className keys on payload.kind");
});

test("emit-side: every i1e-planted ghost payload stamps a kind (24B correct-theme half)", async () => {
  // Out-of-band detection cards ANY pfgk- ghost (the no-bare-bubble half); the CORRECT theme then
  // depends on i1e/d1e stamping `kind` in every ghost payload. Drive real i1e on a corrupt corpus
  // (-> CHAIN CORRUPT banner + a broken marker, multiple ghost kinds) and assert every planted
  // ghost carries a non-empty kind.
  const i1e = i1eWith(i1eDeps);
  const A1 = { uuid: "A1", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1", message: { role: "user", content: "side origin" } };
  const A2 = { uuid: "A2", type: "assistant", parentUuid: "A1", sessionId: "s", timestamp: "t2", message: { role: "assistant", content: "side" } };
  const B1 = { uuid: "B1", type: "user", parentUuid: "OFF-DISK", sessionId: "s", timestamp: "t3", message: { role: "user", content: "rootless" } };
  const B2 = { uuid: "B2", type: "assistant", parentUuid: "B1", sessionId: "s", timestamp: "t4", message: { role: "assistant", content: "latest" } };
  const ren = await i1e([A1, A2, B1, B2], I1E_TEL);
  const ghosts = ren.filter((r) => String(r.uuid).startsWith("pfgk-"));
  assert.ok(ghosts.length > 0, "i1e planted at least one ghost");
  for (const g of ghosts) {
    const pd = parseGhost(g);
    assert.ok(pd && typeof pd.kind === "string" && pd.kind.length > 0, `ghost ${g.uuid} stamps a kind (got ${pd && JSON.stringify(pd.kind)})`);
  }
});
