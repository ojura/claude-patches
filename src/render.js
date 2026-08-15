/*
 * render: the webview render-wrap (Patch K's read side). Readable, faithful
 * replacement of the minified block that the apply step splices into the vendor's
 * user-message render path in webview/index.js.
 *
 * WHAT IT DOES: the loader (d1e) plants marker ghost records whose uuid is
 * "pfgk-<kind>-<slice>" and whose message content is "PFGK1:" + JSON payload. The
 * vendor would render those as plain (collapsed) user bubbles. This wrap detects a
 * ghost by its uuid prefix and renders a structured "pfgkAlert" card instead: a
 * PATCH K header with a marker counter and next/cycle nav, a per-role glyph +
 * headline, an SVG topology diagram, a rows table, an essay body, and a timing line.
 *
 * FAITHFULNESS CONTRACT (docs/invariant.md, MAINTAINER.md): the rendered output
 * must be byte-identical to the minified original. So every value that reaches the
 * DOM or the React tree is preserved VERBATIM here: class names, data attributes,
 * every inline style value (including number-vs-string types, e.g. fontWeight 700
 * and lineHeight 1.55 are numbers, "12.5px" is a string), the React `key` strings,
 * titles, glyphs, badges, headlines, colors, px, font strings, the whole SVG, and
 * the inline un-collapse CSS. Only local variable names, comments, and structure are
 * made readable. The lift must NEVER soften a marker or paint a false green: the
 * three terminal cards (bookend, broken, resplice banner) each keep their `tm` line,
 * and broken/resplice never carry the bookend's cyan accent or "reconstructed" text.
 *
 * SCOPE: five roles (bookend, broken, seam, bridge, seamClean) are a FAITHFUL lift of
 * the shipped wrap, held byte-identical by the old-vs-new element-tree diff. The
 * sixth role, `resplice`, is NEW (task 12): the prebuilt wrap had no resplice branch,
 * so i1e's "⛔ CHAIN CORRUPT · RESPLICED" banner fell through to a bare user bubble.
 * This role renders it as a distinct red terminal-failure card. It has no minified
 * reference, so it is covered by explicit structural assertions, not the diff.
 *
 * Two real ship bugs this file's callers must avoid (see SKILL.md): the wrap only
 * works if the vendor's `return <factory>(<component>,...)` is first converted to
 * `let _ws=<factory>(...)` (else the card is dead code after a return), and the wrap
 * reads TWO vendor locals, the message AND the session (session.messages.peek() for
 * the counter), so both must be remapped at the splice site.
 *
 * DEPS: the wrap takes the vendor message record `t`, the session `e`, the JSX factory
 * `b`, and the default element `_ws`; the onClick handler reads the ambient `document`
 * global (only on click, never at render time). Beyond those, the module binds two
 * engine-discovered `_pv_` deps for the un-collapse <style>, `_pv_cmBodyHash` and
 * `_pv_actionButtonHash` (the drift-prone CSS-module class hashes; see UNCOLLAPSE_CSS),
 * via the alias prologue like d1e/i1e's vendor deps. It ALSO references two $pfg members
 * (the usage-derived webview subset): $pfg.isGhostUuid for OUT-OF-BAND ghost detection on the
 * uuid, and $pfg.PFGK1_PREFIX for the payload envelope. Because of the `_pv_` + `$pfg` deps
 * this module is NOT directly importable: tests eval-with-deps (strip `export`, `new Function`
 * with $pfg + the two `_pv_` hashes bound), exactly like i1e.test.mjs.
 */

// Per-role visual theme tokens. One entry per marker role; each supplies the card's
// colors plus the default badge / glyph / headline (a payload may override the last
// three). Cyan bookend (info), red broken (unrecoverable), amber seam (in-file
// reattach), orange bridge (cross-file), slate seamClean (clean in-file crossing).
export const PFTOK = {
  bookend: { accent: "#7ddcff", accentDim: "#246680", bg: "#142a35", panel: "#0c1c25", text: "#fff", textDim: "#b8d8e8", badge: "◆ RECONSTRUCTED · INFO", glyph: "◆", headline: "Conversation origin · reconstructed" },
  broken: { accent: "#ff6b6b", accentDim: "#8b2424", bg: "#3a1818", panel: "#2a1010", text: "#fff", textDim: "#e8b8b8", badge: "⛔ UNRECOVERABLE", glyph: "⛔", headline: "Incomplete transcript · reconstruction failed" },
  seam: { accent: "#ffc168", accentDim: "#8b6824", bg: "#3a2c14", panel: "#2a1f0c", text: "#fff", textDim: "#e8d4a8", badge: "⚠ IN-FILE REATTACH", glyph: "⚠", headline: "Compaction event · in-file orphan reattached" },
  bridge: { accent: "#ff9c5e", accentDim: "#8b4d24", bg: "#3a2418", panel: "#2a1810", text: "#fff", textDim: "#e8c4a8", badge: "↻ CROSS-FILE BRIDGE", glyph: "↻", headline: "Compaction origin · bridged from a sibling conversation" },
  seamClean: { accent: "#93a6c4", accentDim: "#3a4a64", bg: "#181d28", panel: "#0f141d", text: "#fff", textDim: "#c2cad8", badge: "◇ IN-FILE COMPACTION", glyph: "◇", headline: "Compaction event · crossed in-file" },
  // Task 12: the CHAIN CORRUPT · RESPLICED banner. This is the IRREGULARITY tier, NOT a
  // hard failure: the data is all present, only the threading was unreliable and got
  // respliced. So it takes an AMBER/ORANGE tone (the seam/bridge tier), deliberately NOT
  // broken's red. Red is reserved for broken (root-not-found: the beginning is GONE). The
  // two can co-occur, and the distinct red-vs-orange lets both severities read at once
  // (see the tier invariant in the tests). A deep orange, set apart from seam's amber and
  // bridge's lighter orange. Never green/cyan; keeps its tm line. The badge/glyph here use
  // ⚠️ (warning), NOT the ⛔ hard-failure sign that broken/INCOMPLETE carry, so the two tiers
  // stay distinct at the glyph level too, not just the accent. These are defensive defaults;
  // i1e's resplice payload provides them (its glyph is also ⚠️).
  resplice: { accent: "#ff8c42", accentDim: "#8b4a1e", bg: "#3a2614", panel: "#2a1a0c", text: "#fff", textDim: "#e8ccab", badge: "⚠️ CHAIN CORRUPT · RESPLICED", glyph: "⚠️", headline: "Transcript respliced · chain corruption" },
};

// Neutral fallback theme for any pfgk- ghost whose kind has no specific PFTOK entry
// (task 24B): an unknown or future kind still renders a readable card (payload-driven
// badge/rows/body/tm on a neutral slate) instead of a bare bubble. Deliberately gray,
// distinct from the five role tones, so an unstyled kind reads as a generic Patch K marker.
const DEFAULT_THEME = { accent: "#b8c0cc", accentDim: "#3a4250", bg: "#1e2129", panel: "#161920", text: "#fff", textDim: "#aab2c0", badge: "◆ PATCH K MARKER", glyph: "◆", headline: "Patch K marker" };

// (No local uuid->role parse. Ghost detection is OUT-OF-BAND via $pfg.isGhostUuid on the
// uuid: unspoofable, since the vendor assigns uuids and a user can't set a "pfgk-" one. The
// render KIND comes from payload.kind (data, stamped by i1e), read only for a uuid-confirmed
// ghost. A hand-copied "pfgk-" grammar here would be an SSOT dup of pfg-core.ghostKind AND,
// if used for detection, the in-band spoof vector docs/patches.md rejects. 24B still holds:
// any $pfg.isGhostUuid-true record renders a card, unknown/missing kind -> DEFAULT_THEME.)

// Recover the payload from a ghost record: the JSON after the $pfg.PFGK1_PREFIX envelope,
// or NULL on any miss (no envelope / malformed). NOTE: this reads CONTENT, so it is NOT the
// ghost detector (a user could paste PFGK1: content and spoof a card): is-ghost is decided
// OUT-OF-BAND by $pfg.isGhostUuid on the uuid; parsePayload only fills the data for an
// already-confirmed ghost (and in bandTone, reads a confirmed ghost's kind). The IDE's
// message assembler reshapes the ghost's string content into a single-element content-block
// array before render, so the payload can arrive three ways: t.message.content (string),
// t.content (string), or t.content[i].content.text (the assembled-block path). Returning
// null (not {}) lets callers use `parsePayload(t) || {}` and `parsePayload(m)?.kind`.
export const parsePayload = (t) => {
  let payload = null;
  try {
    let raw = null;
    if (t.message && typeof t.message.content === "string") raw = t.message.content;
    else if (typeof t.content === "string") raw = t.content;
    else if (Array.isArray(t.content)) {
      for (let block of t.content) {
        let text = block && block.content && block.content.text;
        if (typeof text === "string" && text.startsWith($pfg.PFGK1_PREFIX)) { raw = text; break; }
      }
    }
    if (typeof raw === "string" && raw.startsWith($pfg.PFGK1_PREFIX)) payload = JSON.parse(raw.slice($pfg.PFGK1_PREFIX.length));
  } catch (_) {}
  return payload;
};

// The createElement-shim FACTORY over the vendor JSX factory `b`. Defined ONCE at
// module scope and shared by the card (pfgkRenderWrap) and the 12B per-message gutter
// (_pfBandGutter), so there is a single element-builder and a single payload parser
// with no drift between the two render paths. The vendor factory is the jsx-runtime
// form b(type, config, key): children live in config.children and the third positional
// is the key. This shim is BYTE-EXACT to the injected original: it puts children INTO
// the config and passes NO third positional, so `key` rides in the config (props.key).
// That is the original wrap's exact behavior; it is preserved verbatim, not "corrected"
// to hoist a positional key, because wv-5 diffs the element tree against the original.
export const makeH = (b) => (type, props, ...children) =>
  b(type, { ...(props || {}), children: children.length === 0 ? void 0 : children.length === 1 ? children[0] : children });

// The SVG topology diagram for a marker: a PURE (kind, theme) -> SVG string
// function, no DOM, so it is diffable by literal string equality. `kind` selects one
// of five layouts; `theme` supplies the three colors it draws with (accent A, text X,
// textDim Dm). The nested helpers close over those colors and the layout constants.
// Kept close to the original (dense coordinate geometry with layout comments) on
// purpose: it is byte-sensitive string output, so minimal churn = minimal lift risk.
export function pfDiagram(kind, theme) {
  // ---- SVG primitives ----
  function L(x1, y1, x2, y2, c, w, da, op) {
    return '<line x1="' + x1 + '" y1="' + y1 + '" x2="' + x2 + '" y2="' + y2 +
      '" stroke="' + c + '" stroke-width="' + (w || 2) + '"' +
      (da ? ' stroke-dasharray="' + da + '"' : '') +
      (op != null ? ' opacity="' + op + '"' : '') + '/>';
  }
  function TX(x, y, str, c, o) {
    o = o || {};
    return '<text x="' + x + '" y="' + y + '" text-anchor="' + (o.a || 'start') +
      '" style="font:' + (o.f || '500 9px monospace') + ';fill:' + c +
      (o.ls ? ';letter-spacing:' + o.ls : '') +
      (o.it ? ';font-style:italic' : '') +
      (o.op != null ? ';opacity:' + o.op : '') +
      (o.tt ? ';text-transform:' + o.tt : '') + '">' + str + '</text>';
  }
  // a node: optional accent ring (pivot), a solid or ghost dot, optional label above and sub below
  function D(x, y, o) {
    o = o || {};
    var c = o.color, gr = o.gr || 6, xa = (gr * 0.5833).toFixed(2);
    var g = '<g transform="translate(' + x + ',' + y + ')"' + (o.op != null ? ' opacity="' + o.op + '"' : '') + '>';
    if (o.ring) g += '<circle r="14" fill="none" stroke="' + c + '" stroke-width="1" opacity="0.35"/>';
    g += '<circle r="' + gr + '" fill="' + (o.ghost ? 'none' : c) + '" stroke="' + c + '" stroke-width="2"' +
      (o.ghost ? ' stroke-dasharray="2 2"' : '') + '/>';
    if (o.ghost) g += '<line x1="-' + xa + '" y1="-' + xa + '" x2="' + xa + '" y2="' + xa + '" stroke="' + c + '" stroke-width="1.3"/>' +
      '<line x1="-' + xa + '" y1="' + xa + '" x2="' + xa + '" y2="-' + xa + '" stroke="' + c + '" stroke-width="1.3"/>';
    if (o.label) g += '<text x="0" y="' + (o.ldy || -17) + '" text-anchor="middle" ' +
      'style="font:700 9px monospace;letter-spacing:0.16em;fill:' + (o.lc || c) + '">' + o.label + '</text>';
    if (o.sub) g += '<text x="0" y="26" text-anchor="middle" style="font:500 9px monospace;fill:' + (o.sc || c) + ';opacity:0.75">' + o.sub + '</text>';
    return g + '</g>';
  }

  // Local color + style aliases (A = accent, X = text, Dm = textDim) and layout
  // constants, matching the original author's terse convention for this coordinate code.
  var A = theme.accent, X = theme.text, Dm = theme.textDim;
  var H = '700 9px monospace', LS = '0.16em';
  var Y = 74, G = 4, CL = 24, XL = 62; // chain baseline, line-to-node gap, continuation-stub length, left margin

  // chain line between two nodes, ending the same gap G from each node's outer edge (so every node is centred between its lines)
  function seg(ax, ar, bx, br, c, w, da, op) { return L(ax + ar + G, Y, bx - br - G, Y, c, w, da, op); }
  function lead(fx, fr, c, w, da, op) { var x2 = fx - fr - G; return L(x2 - CL, Y, x2, Y, c, w, da, op); }   // short continuation stub, left
  function trail(lx, lr, c, w, da, op) { var x1 = lx + lr + G; return L(x1, Y, x1 + CL, Y, c, w, da, op); }   // short continuation stub, right
  // compact_boundary marker: rounded rect + glyph + label
  function BND(cx) {
    return '<rect x="' + (cx - 9) + '" y="65" width="18" height="18" fill="none" stroke="' + A + '" stroke-width="1.5" rx="2"/>' +
      TX(cx, 78, '⊜', A, {a: 'middle', f: '700 11px monospace'}) +
      TX(cx, 100, 'compact_boundary', Dm, {a: 'middle', f: '700 9px monospace', ls: '0.12em'});
  }

  // seam / seamClean / bridge share this skeleton. arcHigh raises the recovery arc (and its caption) to clear the
  // seam's centred PHANTOM label; the low arc rides lower, so its caption drops to keep the same 12px gap above the apex.
  function cc(left, predSub, arc, mid, arcHigh) {
    var u1 = 62, a1 = 128, P = 194, M = 256, B = 318, u2 = 396, a2 = 462;
    var s = '<svg viewBox="0 0 524 124" style="width:100%;height:124px;display:block">';
    s += TX(20, 15, left, Dm, {f: H, ls: LS}) + TX(322, 15, '↳ POST-COMPACTION CHAIN', A, {f: H, ls: LS});
    // pre-compaction chain into the predecessor
    s += lead(u1, 6, X) + D(u1, Y, {color: X, sub: 'user', sc: Dm}) +
      seg(u1, 6, a1, 6, X) + D(a1, Y, {color: X, sub: 'assistant', sc: Dm}) +
      seg(a1, 6, P, 14, X);
    s += D(P, Y, {color: A, ring: 1, label: 'PRED', sub: predSub, sc: Dm, ldy: -22}) + mid + BND(B);
    // recovery arc PRED -> boundary; caption sits a fixed 12px above the apex on both arc heights
    var cy = arcHigh ? 16 : 30;
    s += '<path d="M ' + P + ' 64 Q ' + M + ' ' + cy + ' ' + B + ' 64" stroke="' + A + '" stroke-width="2" fill="none" stroke-dasharray="5 4"/>' +
      TX(M, arcHigh ? 28 : 35, arc, A, {a: 'middle', f: H, ls: '0.14em'});
    // post-compaction chain
    s += seg(B, 9, u2, 6, X) + D(u2, Y, {color: X, sub: 'user', sc: Dm}) +
      seg(u2, 6, a2, 6, X) + D(a2, Y, {color: X, sub: 'assistant', sc: Dm}) +
      trail(a2, 6, X);
    return s + '</svg>';
  }

  // origin reached, chain whole: ROOT pivot + a mid-chain stitch point marking where a recovery reattachment sits
  function bookendCard() {
    var R = 62, u1 = 142, a1 = 222, ST = 302, u2 = 382, a2 = 462;
    var s = '<svg viewBox="0 0 524 124" style="width:100%;height:124px;display:block">';
    s += TX(20, 15, 'FULLY RECOVERED CHAIN', A, {f: H, ls: LS});
    s += D(R, Y, {color: A, ring: 1, label: 'ROOT', sub: 'origin', sc: Dm, lc: A});
    s += seg(R, 14, u1, 6, X) + D(u1, Y, {color: X, sub: 'user', sc: Dm}) +
      seg(u1, 6, a1, 6, X) + D(a1, Y, {color: X, sub: 'assistant', sc: Dm}) +
      seg(a1, 6, ST, 11, X);
    s += '<g transform="translate(' + ST + ',74)">' +
      '<path d="M 0,-9 L 11,0 L 0,9 L -11,0 Z" fill="none" stroke="' + A + '" stroke-width="1.5" stroke-dasharray="3 2"/>' +
      '<text x="0" y="3" text-anchor="middle" style="font:700 9px monospace;fill:' + A + '">↻</text></g>' +
      TX(ST, 100, 'stitch point', Dm, {a: 'middle', f: '500 9px monospace', op: 0.75});
    s += seg(ST, 11, u2, 6, X) + D(u2, Y, {color: X, sub: 'user', sc: Dm}) +
      seg(u2, 6, a2, 6, X) + D(a2, Y, {color: X, sub: 'assistant', sc: Dm}) +
      trail(a2, 6, X);
    return s + '</svg>';
  }

  // data loss: the lost run trails off the left edge through faint ghosts to a missing phantom, then a severed edge into the dead-end
  function brokenCard() {
    function sg(x) { // a small faint ghost = an older lost ancestor
      return '<g transform="translate(' + x + ',74)" opacity="0.4">' +
        '<circle r="5.6" fill="none" stroke="' + A + '" stroke-width="1.5" stroke-dasharray="2 2"/>' +
        '<line x1="-3.3" y1="-3.3" x2="3.3" y2="3.3" stroke="' + A + '" stroke-width="1"/>' +
        '<line x1="-3.3" y1="3.3" x2="3.3" y2="-3.3" stroke="' + A + '" stroke-width="1"/></g>';
    }
    var g1 = 54, g2 = 82, g3 = 110, PH = 140, DV = 184, DE = 228, u1 = 306, a1 = 384, u2 = 462;
    var s = '<svg viewBox="0 0 524 124" style="width:100%;height:124px;display:block">';
    s += TX(20, 15, 'MISSING UPSTREAM', Dm, {f: H, ls: LS}) + TX(300, 15, '↳ VISIBLE TRANSCRIPT', A, {f: H, ls: LS});
    s += L(XL - 6 - G - CL, Y, PH - 8 - G, Y, A, 1, '2 2', 0.32); // lost run, dashed, off the left edge (stub fixed to the layout margin, not the ghosts)
    s += sg(g1) + sg(g2) + sg(g3);
    s += D(PH, Y, {color: A, ghost: 1, label: 'PHANTOM', lc: A, gr: 8});
    s += TX((g1 + PH) / 2, 100, 'unrecoverable', Dm, {a: 'middle', f: '500 9px sans-serif', it: 1, op: 0.85});
    s += L(DV, 40, DV, 104, Dm, 1, '3 4', 0.55); // region boundary
    s += seg(PH, 8, DE, 14, A, 1.5, '4 4', 0.85); // severed parent edge: phantom -> dead-end
    s += D(DE, Y, {color: A, ring: 1, label: 'DEAD-END', sub: 'missing parent', sc: Dm});
    s += seg(DE, 14, u1, 6, X) + D(u1, Y, {color: X, sub: 'user', sc: Dm}) +
      seg(u1, 6, a1, 6, X) + D(a1, Y, {color: X, sub: 'assistant', sc: Dm}) +
      seg(a1, 6, u2, 6, X) + D(u2, Y, {color: X, sub: 'user', sc: Dm}) +
      trail(u2, 6, X);
    return s + '</svg>';
  }

  if (kind === 'bookend') return bookendCard();
  if (kind === 'broken') return brokenCard();
  if (kind === 'seam') return cc('IN-FILE PRE-COMPACTION', 'in-file', 'in-file reattach',
    D(256, 74, {color: A, ghost: 1, label: 'PHANTOM', lc: A, gr: 8}), true);
  if (kind === 'seamClean') return cc('IN-FILE PRE-COMPACTION', 'in-file', 'in-file link', '', false);
  if (kind === 'bridge') return cc('CROSS-FILE PRE-COMPACTION', 'sibling', 'cross-file link',
    L(256, 40, 256, 104, Dm, 1, '3 4', 0.55), false);
  // 'resplice' (task 12) and any unrecognized kind produce no card diagram. For
  // resplice this is deliberate: the per-message four-tone gutter on the respliced
  // messages below the banner carries the corruption structure, so a card topology
  // would duplicate it.
  return '';
}

// The un-collapse <style> the card injects: it lifts the vendor's collapsed max-height
// and hides the truncation gradient / show-more / edit-fork action buttons on the marker
// bubble, so the long diagnostic essays render in full. The vendor CSS-module class-name
// hashes DRIFT per bundle, so they are NOT hardcoded here: the engine DISCOVERS the two
// module hashes by co-occurrence and binds them as _pv_ deps (like d1e/i1e's vendor deps),
// and this template interpolates them. So a bundle that regenerates the hashes still
// un-collapses, instead of a stale literal silently re-collapsing the essays.
//   _pv_cmBodyHash      = the shared hash of the message-body CSS module
//                         (content / collapsed / truncationGradient / buttonContainer)
//   _pv_actionButtonHash = the action-button CSS module hash
// Faithfulness target: filled with the current bundle's hashes, this equals byte-for-byte
// the string the original wrap shipped inline.
export const UNCOLLAPSE_CSS =
  ".pfgkAlert .content_" + _pv_cmBodyHash + ".collapsed_" + _pv_cmBodyHash + "{max-height:none!important}" +
  ".pfgkAlert .truncationGradient_" + _pv_cmBodyHash + "{display:none}" +
  ".pfgkAlert .buttonContainer_" + _pv_cmBodyHash + "{display:none}" +
  ".pfgkAlert .actionButton_" + _pv_actionButtonHash + "{display:none}";

// The wrap. Called for every user message; returns the default element `_ws`
// unchanged unless `t` is a Patch-K ghost, in which case it returns the pfgkAlert
// card. `b` is the vendor JSX factory; `e` is the session (read for the marker
// counter); the onClick closes over the ambient `document`.
export function pfgkRenderWrap(t, e, b, _ws) {
  // IS-GHOST out-of-band: the uuid prefix via $pfg.isGhostUuid, never the content envelope, so
  // a user pasting "PFGK1:..." can't fake a marker card. Any-kind ghost renders a card (24B).
  if (!$pfg.isGhostUuid(t.uuid)) return _ws;

  // The shared createElement-shim over this call's vendor factory (see makeH).
  const h = makeH(b);

  // KIND comes from the payload (data, stamped by i1e), read now that the uuid confirmed a
  // ghost. Theme AND diagram key on kind; an unknown/missing kind falls back to DEFAULT_THEME
  // (24B: never a bare bubble). PFTOK[kind] subsumes the old seamClean theme special-case.
  const payload = parsePayload(t) || {};
  const kind = payload.kind || "unknown";
  const theme = PFTOK[kind] || DEFAULT_THEME;

  // Payload fields, each falling back to the theme default / empty.
  const badge = payload.badge || theme.badge,
    glyph = payload.glyph || theme.glyph,
    headline = payload.headline || theme.headline,
    rows = Array.isArray(payload.rows) ? payload.rows : [],
    body = payload.body || "";

  // Marker counter + next/cycle nav: enumerate every pfgk ghost in the session in
  // order and find where this one sits. Guarded: e.messages.peek() may be absent on
  // some render paths, and a throw just leaves the counter hidden (markerTotal 0).
  const ghostUuids = [];
  try {
    const all = e.messages.peek();
    for (const m of all) if ($pfg.isGhostUuid(m.uuid)) ghostUuids.push(String(m.uuid));
  } catch (_) {}
  const markerIndex = ghostUuids.indexOf(String(t.uuid)),
    markerTotal = ghostUuids.length,
    isLast = markerIndex === markerTotal - 1;
  const pad2 = (n) => { n = String(n); return n.length < 2 ? "0" + n : n; };
  const MONO = '"JetBrains Mono", ui-monospace, monospace';

  return h(
    "div",
    {
      className: "pfgkAlert pfgk-" + kind,
      // attribute NAME "data-pfgk-role" preserved (onClick selector + wv-5's diff); value now payload.kind
      "data-pfgk-role": kind,
      style: { background: theme.bg, border: "3px solid " + theme.accentDim, borderRadius: "4px", cursor: "pointer", color: theme.text, margin: "8px 0", fontFamily: "Inter, sans-serif" },
      title: "Click to jump to next Patch K marker",
      // Clicking any marker scrolls to the next one, wrapping from the last back to the first.
      onClick: function (ev) {
        var all = Array.from(document.querySelectorAll("[data-pfgk-role]"));
        var i = all.indexOf(ev.currentTarget);
        if (i < 0) return;
        var next = all[(i + 1) % all.length];
        if (next) next.scrollIntoView({ behavior: "smooth", block: "center" });
      },
    },
    // Injected style: un-collapse the marker bubble and hide the vendor truncation
    // gradient / show-more / edit-fork action buttons (none apply to a synthetic marker).
    // The class hashes are discovered, not hardcoded; see UNCOLLAPSE_CSS above.
    h("style", { key: "_s" }, UNCOLLAPSE_CSS),
    // Header bar: PATCH K tag + role badge on the left, the "Marker N of M" counter,
    // and the next/cycle glyph on the right.
    h(
      "div",
      { key: "_hd", style: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "8px 16px", borderBottom: "1px dashed " + theme.accentDim, fontFamily: MONO, fontSize: "11px", fontWeight: 700, letterSpacing: "0.14em", color: theme.accent, textTransform: "uppercase", gap: "16px" } },
      h(
        "span",
        { key: "_l", style: { display: "flex", alignItems: "center", gap: "10px" } },
        h("span", { key: "_pk", style: { background: theme.accent, color: theme.bg, padding: "2px 6px", borderRadius: "2px", fontSize: "9px", letterSpacing: "0.18em" } }, "PATCH K"),
        h("span", { key: "_b" }, badge)
      ),
      markerTotal > 0 ? h("span", { key: "_ct", style: { color: theme.textDim } }, "Marker " + pad2(markerIndex + 1) + " of " + pad2(markerTotal)) : null,
      h("span", { key: "_nx" }, isLast ? "↺ cycle" : "↓ next")
    ),
    // Body: the glyph + headline row, the SVG diagram (via dangerouslySetInnerHTML),
    // then one panel box (_rp) that stacks the rows table, the essay body, and the
    // timing line together. The body and tm render INSIDE the rows panel, sharing its
    // background and padding; they are NOT siblings of it.
    h(
      "div",
      { key: "_bd", style: { padding: "14px 18px 16px" } },
      h(
        "div",
        { key: "_hl", style: { display: "flex", alignItems: "center", gap: "12px", marginBottom: "12px" } },
        h("span", { key: "_g", style: { fontSize: "22px" } }, glyph),
        h("span", { key: "_h", style: { fontSize: "15px", fontWeight: 700, color: theme.text } }, headline)
      ),
      (function () {
        var svg = pfDiagram(kind, theme);
        return svg ? h("div", { key: "_dgp", style: { background: theme.panel, borderRadius: "3px", padding: "8px 12px", marginBottom: "12px", overflow: "hidden" }, dangerouslySetInnerHTML: { __html: svg } }) : null;
      })(),
      h(
        "div",
        { key: "_rp", style: { background: theme.panel, borderRadius: "3px", padding: "6px 16px 12px", fontSize: "12.5px", lineHeight: 1.55 } },
        rows.map(function (row, ri) {
          return h(
            "div",
            { key: ri, style: { display: "flex", alignItems: "baseline", gap: "12px", padding: "8px 0", borderBottom: ri === rows.length - 1 && !body ? "none" : "1px dotted " + theme.textDim + "40", fontFamily: MONO, fontSize: "11.5px" } },
            h("span", { key: "_k", style: { color: theme.textDim, flex: "0 0 38%", textTransform: "uppercase", letterSpacing: "0.08em", fontSize: "10px", fontWeight: 600 } }, String(row && row[0])),
            h("span", { key: "_v", style: { color: theme.text, flex: "1 1 auto", minWidth: 0, wordBreak: "break-all" } }, String(row && row[1]))
          );
        }),
        body ? h("div", { key: "_by", style: { color: theme.textDim, marginTop: "12px", paddingTop: "12px", borderTop: "1px dotted " + theme.accentDim + "80", fontSize: "12.5px", fontFamily: "Inter, sans-serif" } }, body) : null,
        payload.tm ? h("div", { key: "_tm", style: { color: theme.textDim, opacity: 0.8, fontSize: "11px", marginTop: "8px", fontFamily: MONO } }, payload.tm) : null
      )
    )
  );
}

// ---- Patch K · 12B four-tone context gutter (consume side) ----
// The CHAIN CORRUPT resplice banner lists, per compaction context, which respliced
// messages are live / severed / uncertain; "summarized" is the implicit default. This
// paints that classification as a colored left border on each ordinary (non-ghost)
// message, read from the banner's bands in the session's own message set. INVARIANT:
// renders i1e's classification only; never green on a severed/uncertain turn, never
// suppresses a marker.
//
// live/severed/uncertain rhyme with the marker roles, so pull their hues from PFTOK (one
// palette source); "summarized" is the condensed-away state, a band-specific muted slate.
// severed uses the RESPLICE tone, not broken's red: "red exclusive to broken" is an
// invariant, and severed is a per-message CONTEXT status (proven not-in-context, orphaned),
// not the conversation-level hard failure. So it harmonizes with the CHAIN CORRUPT banner it
// always sits under (the severed messages ARE part of the corruption the banner announced).
export const BAND_TONES = {
  live:       { color: PFTOK.bookend.accent,  label: "in resume context" },
  summarized: { color: "#5a6270",             label: "condensed into summary" },
  severed:    { color: PFTOK.resplice.accent, label: "post-compaction · not in context" },
  uncertain:  { color: PFTOK.seam.accent,     label: "post-compaction · context uncertain" },
};

// The uuid->band map for one message list (_ren = e.messages.peek()), memoized on that
// array via WeakMap: the resplice banner is found ONCE and its bands compiled ONCE per
// list, not re-parsed per message. banner may be null (a healthy list has none); we cache
// that null result too, so a bannerless transcript is not re-scanned for every message.
// This turns the per-render banner lookup from O(N) content reads into O(1)-amortized
// (the find used to re-parse per message post-swap; wv-5's gutter test covers the bound).
const _bandCache = new WeakMap();
const bandInfo = (msgs) => {
  let info = _bandCache.get(msgs);
  if (info) return info;
  let banner = null, bp = null;
  for (const m of msgs) {
    const p = $pfg.isGhostUuid(m.uuid) ? parsePayload(m) : null;
    if (p?.kind === "resplice") { banner = m; bp = p; break; }
  }
  const map = new Map();
  const bands = (bp || {}).bands || {};
  for (const u of bands.live || []) map.set(String(u), "live");
  for (const u of bands.sev  || []) map.set(String(u), "severed");
  for (const u of bands.unc  || []) map.set(String(u), "uncertain");
  info = { banner, map };
  _bandCache.set(msgs, info);
  return info;
};
export const bandTone = (t, e) => {
  if (!t || $pfg.isGhostUuid(t.uuid)) return null; // out-of-band: skip ghosts (cards), band only ordinary messages
  let msgs;
  try { msgs = e.messages.peek(); } catch { return null; }
  if (!msgs) return null;
  const { banner, map } = bandInfo(msgs);
  if (!banner) return null;               // healthy list: no resplice banner, no gutter
  return map.get(t.uuid) || "summarized"; // banner present: known tone, else summarized
};

// Wrap an ordinary message element with its context gutter (colored left border, dimmed
// for summarized). No-op for ghosts (the card passes through), malformed records, and
// healthy transcripts. The gutter div is a vendor-style list item, so it carries the vendor
// index `i` as the POSITIONAL key (3rd jsx arg), matching how the dispatcher keys its own
// items (b(V8t,{...},i), b(z8t,{...},i)); that avoids a key-in-props warning. `el` rides in
// config.children (NOT as the 3rd positional, which would land it as the key). The CARD, by
// contrast, stays keyless/makeH (faithful to the shipped wrap): the two legitimately differ.
// Only this gutter wrapper carries key i.
export const pfgkBandGutter = (el, t, e, b, i) => {
  const tone = bandTone(t, e);
  if (!tone) return el;
  const tk = BAND_TONES[tone];
  return b("div", {
    className: "pfgkBand pfgkBand-" + tone,
    "data-pfgk-band": tone,
    title: "Patch K context: " + tk.label,
    children: el,
    // uncertain gets a DASHED border (tentative), the rest SOLID: this separates severed
    // (orange) from uncertain (amber) by style, not just their close hue, and dash-codes the
    // "tentative" meaning of uncertain. Lead-approved; tune the exact px/style if it reads
    // choppy in the DOM, the semantic (uncertain distinguished + tentative) is the point.
    style: { borderLeft: "3px " + (tone === "uncertain" ? "dashed" : "solid") + " " + tk.color, paddingLeft: "10px", marginLeft: "2px", opacity: tone === "summarized" ? 0.72 : 1 },
  }, i);
};

// The single decorate entry both U8t branches (user + assistant) call, same 5-arg shape:
// render the pfgk card (or pass the vendor element through for a non-ghost), then apply the
// per-message band gutter. The React key stays `i`, the vendor list index, for BOTH the card
// path and ordinary messages, so sibling keys are consistent across all branches (only
// user/assistant route through here; uuid-keying just these two would desync sibling keys
// and risk reconciliation). The band LOOKUP still keys on t.uuid inside bandTone; only the
// React key is `i`. For a pfgk ghost the gutter no-ops (null tone), so the card is returned
// unchanged and the faithfulness diff holds.
export const pfgkDecorate = (t, e, b, _ws, i) => pfgkBandGutter(pfgkRenderWrap(t, e, b, _ws), t, e, b, i);
