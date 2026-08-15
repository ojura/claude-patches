// The core user-facing fix, tested on the REAL i1e (src/i1e.js composed with the
// real $pfg block): a healthy chain that walks back across an lpu to a
// continuation-preamble root must fire the loud "root not found" marker, NEVER
// the green "origin reconstructed" bookend. This is the 4044 lie.
//
//   run:  node --test src/i1e.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import * as nodePath from "node:path";

const HERE = nodePath.dirname(new URL(import.meta.url).pathname);

let _factory;
function i1eWith(deps) {
  if (!_factory) {
    const src = readFileSync(nodePath.join(HERE, "i1e.js"), "utf8");
    const pfgBlock = execFileSync("python3", [nodePath.join(HERE, "..", "util", "pfg-codegen.py")], {
      encoding: "utf8",
    });
    _factory = new Function("_pv_o1e", "_pv_s1e", pfgBlock + "\n" + src + "\nreturn i1e;");
  }
  return _factory(deps.o1e, deps.s1e);
}

const deps = {
  o1e: (t, u, d) => u.slice(), // render rows = the walked chain; markers unshift on top
  s1e: () => false, // no tool-result carriers in these fixtures
};

// The 4044 shape. Structurally a clean origin (null parent, no lpu, not a
// summary), content is a resume summary. The chain reaches it by crossing an lpu,
// so _recovered is true and the OLD code fired a green success bookend.
const PREAMBLE = {
  uuid: "P", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1",
  message: { role: "user", content: "This session is being continued from a previous conversation that ran out of context. Summary: ..." },
};
const MID = {
  uuid: "M", type: "assistant", parentUuid: null, logicalParentUuid: "P", sessionId: "s", timestamp: "t2",
  message: { role: "assistant", content: "a middle turn that bridges via the logical parent" },
};
const LEAF = {
  uuid: "L", type: "user", parentUuid: "M", sessionId: "s", timestamp: "t3",
  message: { role: "user", content: "the latest turn" },
};
const TEL = { timing: null, siblingsScanned: 0, phantomsBackfilled: 0, phantomsCouldNotBackfill: 0, provBasenames: {} };

function pfgk(rec) {
  if (!rec || !rec.message || typeof rec.message.content !== "string") return null;
  if (!rec.message.content.startsWith("PFGK1:")) return null;
  return JSON.parse(rec.message.content.slice("PFGK1:".length));
}

test("i1e: a preamble terminus fires the loud marker, never the green bookend", async () => {
  const i1e = i1eWith(deps);
  const ren = await i1e([PREAMBLE, MID, LEAF], TEL);
  const top = pfgk(ren[0]);

  assert.ok(top, "a PFGK1 marker was unshifted onto the render");
  assert.ok(
    typeof top.badge === "string" && /TRANSCRIPT INCOMPLETE/.test(top.badge),
    "the one canonical loud root-not-found marker fired",
  );
  // The whole point: NO false success anywhere in the render.
  const whole = JSON.stringify(ren);
  assert.doesNotMatch(whole, /reconstructed/i, "no green 'origin reconstructed' bookend on a preamble");
  // And the marker is honestly scoped, not an overclaim.
  assert.match(top.body, /archived or earlier session/, "the marker leaves the archived-session door open");
  assert.doesNotMatch(top.badge, /unrecoverable/i, "verdict is 'not found', never the unproven 'unrecoverable'");
});

test("i1e: an origin still gets the green reconstructed bookend", async () => {
  // The mirror: same shape, but the root is a REAL first prompt. The success
  // bookend must still fire, so the fix is not just 'always fail'.
  const realOrigin = { ...PREAMBLE, message: { role: "user", content: "Fix the login bug in the auth handler" } };
  const i1e = i1eWith(deps);
  const ren = await i1e([realOrigin, MID, LEAF], TEL);
  const top = pfgk(ren[0]);
  assert.ok(top, "a bookend was unshifted");
  assert.match(top.headline || "", /reconstructed/i, "an origin reached across a fork -> green bookend");
  assert.doesNotMatch(JSON.stringify(ren), /TRANSCRIPT INCOMPLETE/, "no failure marker on a real origin");
});

test("i1e: a team-only transcript keeps its turns and recognizes its first prompt", async () => {
  const identity = { teamName: "session-deadbeef", agentName: "researcher", sessionId: "team-session" };
  const root = { ...identity, uuid: "TR", type: "user", parentUuid: null, timestamp: "t1", message: { role: "user", content: "the teammate assignment" } };
  const reply = { ...identity, uuid: "TA", type: "assistant", parentUuid: "TR", timestamp: "t2", message: { role: "assistant", content: "the teammate reply" } };
  const i1e = i1eWith(deps);
  const ren = await i1e([root, reply], TEL);
  assert.deepEqual(ren.map((r) => r.uuid), ["TR", "TA"], "team ownership does not empty or reroot the transcript");
  assert.doesNotMatch(JSON.stringify(ren), /TRANSCRIPT INCOMPLETE|CHAIN CORRUPT/, "the team-local root is accepted");
});

// --- BREACH 1 (council invariant-adversary, run-confirmed): a disjoint side-tree
// --- must not suppress the marker when the live leaf is rootless. The old
// --- corrupt-path verdict keyed on u[0] (oldest across ALL fragments), so a
// --- complete side tree whose origin sorts first passed classifyRoot and silenced
// --- the marker while the live lineage dead-ended. The verdict keys on the live leaf.
test("i1e: a disjoint side-tree does not suppress the marker when the live leaf is rootless", async () => {
  // {A1,A2}: a COMPLETE side conversation rooted at an origin A1.
  // {B1,B2}: the LIVE lineage. B2 is the latest turn; B1's parent (B0) is off-disk,
  // so the live leaf never reaches a root. A1 sorts first by write order.
  const A1 = { uuid: "A1", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1", message: { role: "user", content: "an unrelated side conversation's first prompt" } };
  const A2 = { uuid: "A2", type: "assistant", parentUuid: "A1", sessionId: "s", timestamp: "t2", message: { role: "assistant", content: "side reply" } };
  const B1 = { uuid: "B1", type: "user", parentUuid: "B0-OFF-DISK", sessionId: "s", timestamp: "t3", message: { role: "user", content: "live lineage; parent predates the saved files" } };
  const B2 = { uuid: "B2", type: "assistant", parentUuid: "B1", sessionId: "s", timestamp: "t4", message: { role: "assistant", content: "the latest turn" } };
  const i1e = i1eWith(deps);
  const ren = await i1e([A1, A2, B1, B2], TEL);
  const whole = JSON.stringify(ren);
  assert.match(whole, /TRANSCRIPT INCOMPLETE/, "live leaf dead-ends at an off-disk parent -> loud marker fires");
  assert.doesNotMatch(whole, /reconstructed/i, "no green bookend for a rootless live leaf");
});

// --- BREACH 2 (council invariant-adversary, run-confirmed): a K2 seam reattaches
// --- the chain to an in-file predecessor by POSITION, reaching that predecessor's
// --- own origin, not this conversation's true (off-disk) root. Firing a green
// --- "reconstructed" success there is the most-forbidden output. Crossing a
// --- pfgk-seam- ghost forces the loud marker.
test("i1e: a K2 seam reattachment fires the loud marker, never a green bookend", async () => {
  const G1 = { uuid: "G1", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1", message: { role: "user", content: "an in-file predecessor's own first prompt" } };
  const SEAM = { uuid: "pfgk-seam-BND01234", type: "user", parentUuid: "G1", sessionId: "s", timestamp: "t2", message: { role: "user", content: "PFGK1:" + JSON.stringify({ rows: [], body: "reattached by position" }) } };
  const BND = { uuid: "BND01234", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "pfgk-seam-BND01234", sessionId: "s", timestamp: "t3", message: { role: "user", content: "" } };
  const POST = { uuid: "POST", type: "user", parentUuid: "BND01234", sessionId: "s", timestamp: "t4", message: { role: "user", content: "the latest turn, after the compaction" } };
  const i1e = i1eWith(deps);
  const ren = await i1e([G1, SEAM, BND, POST], TEL);
  const whole = JSON.stringify(ren);
  assert.match(whole, /TRANSCRIPT INCOMPLETE/, "an unverified positional reattachment -> loud marker");
  assert.doesNotMatch(whole, /reconstructed/i, "no green 'reconstructed in-file' bookend on a seam");
});

// --- The mirror for BREACH 2: a VERIFIED K3 bridge (lpu resolved, marked with a
// --- pfgk-seamClean- ghost) is NOT a seam and must still earn its green bookend,
// --- so the fix distinguishes unverified (seam) from verified, not "always fail".
test("i1e: a verified K3 seamClean bridge still gets the green reconstructed bookend", async () => {
  const G1 = { uuid: "G1", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1", message: { role: "user", content: "the genuine, resolved pre-compaction origin" } };
  const SEAMCLEAN = { uuid: "pfgk-seamClean-BND01234", type: "user", parentUuid: "G1", sessionId: "s", timestamp: "t2", message: { role: "user", content: "PFGK1:" + JSON.stringify({ badge: "◇ IN-FILE COMPACTION", body: "bridged in-file" }) } };
  const BND = { uuid: "BND01234", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "pfgk-seamClean-BND01234", sessionId: "s", timestamp: "t3", message: { role: "user", content: "" } };
  const POST = { uuid: "POST", type: "user", parentUuid: "BND01234", sessionId: "s", timestamp: "t4", message: { role: "user", content: "the latest turn" } };
  const i1e = i1eWith(deps);
  const ren = await i1e([G1, SEAMCLEAN, BND, POST], TEL);
  const whole = JSON.stringify(ren);
  assert.doesNotMatch(whole, /TRANSCRIPT INCOMPLETE/, "a resolved in-file bridge is verified -> no failure marker");
  assert.match(whole, /reconstructed/i, "the green bookend still fires for a verified reconstruction");
});

// --- Banding count excludes ghosts (council: i1e chain-builder seat). A corrupt
// --- (disjoint) corpus that also carries a planted pfgk- ghost. Ghosts are
// --- type:"user" and pass isMain, so the banding loop used to count them and
// --- inflate the CHAIN CORRUPT banner's "messages shown". Only real turns count.
test("i1e: the CHAIN CORRUPT banner counts exclude planted ghosts", async () => {
  const A1 = { uuid: "A1", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1", message: { role: "user", content: "side origin" } };
  const A2 = { uuid: "A2", type: "assistant", parentUuid: "A1", sessionId: "s", timestamp: "t2", message: { role: "assistant", content: "side reply" } };
  const GHOST = { uuid: "pfgk-seamClean-deadbeef", type: "user", parentUuid: "A2", sessionId: "s", timestamp: "t2b", message: { role: "user", content: "PFGK1:" + JSON.stringify({ badge: "◇ IN-FILE COMPACTION", body: "ghost" }) } };
  const B1 = { uuid: "B1", type: "user", parentUuid: "B0-OFF-DISK", sessionId: "s", timestamp: "t3", message: { role: "user", content: "live lineage, rootless" } };
  const B2 = { uuid: "B2", type: "assistant", parentUuid: "B1", sessionId: "s", timestamp: "t4", message: { role: "assistant", content: "latest turn" } };
  const i1e = i1eWith(deps);
  const ren = await i1e([A1, A2, GHOST, B1, B2], TEL);
  const banner = ren.map(pfgk).find((p) => p && /CHAIN CORRUPT/.test(p.badge || ""));
  assert.ok(banner, "the CHAIN CORRUPT banner rendered");
  const shown = banner.rows.find((r) => r[0] === "messages shown");
  assert.equal(shown[1], "4", "4 real main turns counted; the pfgk- ghost is excluded");
});

// --- Fork-root, UNRESOLVABLE case (architect steer): a structural origin carrying
// --- forkedFrom whose source d1e could not read (off-disk) is a hard failure, the true
// --- origin lives in a session not on disk. A soft "forked from..." note here would be a
// --- soft note for a hard failure (forbidden middle); it fires the LOUD MARKER instead.
test("i1e: an unresolvable forkedFrom origin fires the loud marker (off-disk is a hard failure)", async () => {
  const FORK_ORIGIN = { uuid: "FO", type: "user", parentUuid: null, forkedFrom: { sessionId: "src12345-aaaa", messageUuid: "FO" }, sessionId: "s", timestamp: "t1", message: { role: "user", content: "the copied opening turn" } };
  const MIDF = { uuid: "MF", type: "assistant", parentUuid: null, logicalParentUuid: "FO", sessionId: "s", timestamp: "t2", message: { role: "assistant", content: "a turn bridging via lpu" } };
  const LEAFF = { uuid: "LF", type: "user", parentUuid: "MF", sessionId: "s", timestamp: "t3", message: { role: "user", content: "latest turn" } };
  const i1e = i1eWith(deps);
  const ren = await i1e([FORK_ORIGIN, MIDF, LEAFF], TEL);
  const whole = JSON.stringify(ren);
  const top = pfgk(ren[0]);
  assert.ok(top, "a marker was unshifted");
  assert.match(top.badge || "", /TRANSCRIPT INCOMPLETE/, "an unresolvable off-disk fork is a hard failure -> loud marker");
  assert.match(whole, /src12345/, "the off-disk source session is named in the marker");
  assert.doesNotMatch(whole, /reconstructed/i, "no green bookend");
  assert.doesNotMatch(whole, /forked from an earlier session/i, "no soft provenance note (that would be the forbidden middle)");
});

// --- The mirror: when d1e's fork backfill CONFIRMED the branch was taken at the
// --- source's start (a complete fork, flagged __pfgkForkComplete), the origin IS
// --- confirmed and must render GREEN, not the hedged note. So the note fires only for
// --- the unresolved (off-disk) case; the two tests are mutually non-vacuous.
test("i1e: a forkedFrom origin flagged complete renders green, not the fork-note", async () => {
  const COMPLETE = { uuid: "FC", type: "user", parentUuid: null, forkedFrom: { sessionId: "src99", messageUuid: "FC" }, __pfgkForkComplete: true, sessionId: "s", timestamp: "t1", message: { role: "user", content: "the copied opening turn, a complete branch" } };
  const MC = { uuid: "MC", type: "assistant", parentUuid: null, logicalParentUuid: "FC", sessionId: "s", timestamp: "t2", message: { role: "assistant", content: "a turn bridging via lpu" } };
  const LC = { uuid: "LC", type: "user", parentUuid: "MC", sessionId: "s", timestamp: "t3", message: { role: "user", content: "latest turn" } };
  const i1e = i1eWith(deps);
  const ren = await i1e([COMPLETE, MC, LC], TEL);
  const whole = JSON.stringify(ren);
  assert.doesNotMatch(whole, /forked from an earlier session/i, "a confirmed complete branch does NOT get the hedged note");
  assert.doesNotMatch(whole, /TRANSCRIPT INCOMPLETE/, "and not the loud marker");
  assert.match(whole, /reconstructed/i, "it reaches its origin across the lpu -> the green bookend");
});

// --- Task 30 (principal directive): the wall-clock timing must render on ALL THREE
// --- terminal cards, not just the green bookend: how long the reconstruction took,
// --- visible whatever the outcome (most useful on failure). This pins the payload
// --- SHAPE (tm present) so a field can never silently drop again. The prior tests all
// --- pass timing:null, so _tmStr was "" and a missing tm was invisible; this feeds a
// --- populated timing so tm is non-empty and asserted on each card.
const TEL_TIMED = { timing: { parseMs: 5, crossFileMs: 3, siblingBackfillMs: 2, bookendMs: 1 }, siblingsScanned: 4, phantomsBackfilled: 0, phantomsCouldNotBackfill: 0, provBasenames: {} };
test("i1e: the wall-clock tm renders on all three terminal cards (bookend, marker, CHAIN CORRUPT)", async () => {
  const i1e = i1eWith(deps);
  // 1. green bookend: a real origin reached across an lpu
  const realOrigin = { ...PREAMBLE, message: { role: "user", content: "Fix the login bug in the auth handler" } };
  const bk = pfgk((await i1e([realOrigin, MID, LEAF], TEL_TIMED))[0]);
  assert.match(bk.headline || "", /reconstructed/i, "fixture sanity: this yields the green bookend");
  assert.match(bk.tm || "", /wall-clock/, "green bookend carries the wall-clock tm");
  // 2. TRANSCRIPT INCOMPLETE marker: a continuation-preamble terminus (healthy path)
  const mk = pfgk((await i1e([PREAMBLE, MID, LEAF], TEL_TIMED))[0]);
  assert.match(mk.badge || "", /TRANSCRIPT INCOMPLETE/, "fixture sanity: preamble yields the marker");
  assert.match(mk.tm || "", /wall-clock/, "root-not-found marker carries the wall-clock tm");
  // 3. CHAIN CORRUPT banner: a disjoint corpus (rootless live leaf B + side tree A)
  const A1 = { uuid: "A1", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1", message: { role: "user", content: "side origin" } };
  const A2 = { uuid: "A2", type: "assistant", parentUuid: "A1", sessionId: "s", timestamp: "t2", message: { role: "assistant", content: "side reply" } };
  const B1 = { uuid: "B1", type: "user", parentUuid: "B0-OFF-DISK", sessionId: "s", timestamp: "t3", message: { role: "user", content: "live rootless" } };
  const B2 = { uuid: "B2", type: "assistant", parentUuid: "B1", sessionId: "s", timestamp: "t4", message: { role: "assistant", content: "latest" } };
  const banner = (await i1e([A1, A2, B1, B2], TEL_TIMED)).map(pfgk).find((p) => p && /CHAIN CORRUPT/.test(p.badge || ""));
  assert.ok(banner, "fixture sanity: disjoint corpus yields the CHAIN CORRUPT banner");
  assert.match(banner.tm || "", /wall-clock/, "CHAIN CORRUPT banner carries the wall-clock tm");
});

// --- Task 28 (seam-trust proof-promotion): a K2 seam that PROVABLY rejoins THIS
// --- conversation (d1e stamped __pfgkSeam.proven=true: the reattach target walks to the
// --- file's UNIQUE own-origin crossing no forkedFrom and no other unproven seam) promotes
// --- from unverified seam to trusted bridge, green bookend, no marker. The mirror of
// --- BREACH 2: an UNPROVEN seam (no evidence, or proven=false) still fires the loud marker.
// --- Binary AT proof, never a soft middle: the two tests are mutually non-vacuous.
test("i1e: a PROVEN K2 seam promotes to a trusted bridge (green bookend, no marker)", async () => {
  const G1 = { uuid: "G1", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1", message: { role: "user", content: "the genuine, sole origin of this conversation" } };
  const SEAM = { uuid: "pfgk-seam-BND01234", type: "user", parentUuid: "G1", __pfgkSeam: { reattachTarget: "G1", proven: true }, sessionId: "s", timestamp: "t2", message: { role: "user", content: "PFGK1:" + JSON.stringify({ rows: [], body: "reattached by position, proven same-conversation" }) } };
  const BND = { uuid: "BND01234", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "pfgk-seam-BND01234", sessionId: "s", timestamp: "t3", message: { role: "user", content: "" } };
  const POST = { uuid: "POST", type: "user", parentUuid: "BND01234", sessionId: "s", timestamp: "t4", message: { role: "user", content: "the latest turn" } };
  const i1e = i1eWith(deps);
  const whole = JSON.stringify(await i1e([G1, SEAM, BND, POST], TEL));
  assert.doesNotMatch(whole, /TRANSCRIPT INCOMPLETE/, "a proven seam is a verified bridge -> no failure marker");
  assert.match(whole, /reconstructed/i, "the green bookend fires for the proven reattachment");
});

test("i1e: an explicitly UNPROVEN K2 seam (proven=false) still fires the loud marker", async () => {
  const G1 = { uuid: "G1", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1", message: { role: "user", content: "an in-file predecessor's own origin" } };
  const SEAM = { uuid: "pfgk-seam-BND01234", type: "user", parentUuid: "G1", __pfgkSeam: { reattachTarget: "G1", proven: false }, sessionId: "s", timestamp: "t2", message: { role: "user", content: "PFGK1:" + JSON.stringify({ rows: [], body: "reattached by position, NOT proven" }) } };
  const BND = { uuid: "BND01234", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "pfgk-seam-BND01234", sessionId: "s", timestamp: "t3", message: { role: "user", content: "" } };
  const POST = { uuid: "POST", type: "user", parentUuid: "BND01234", sessionId: "s", timestamp: "t4", message: { role: "user", content: "the latest turn" } };
  const i1e = i1eWith(deps);
  const whole = JSON.stringify(await i1e([G1, SEAM, BND, POST], TEL));
  assert.match(whole, /TRANSCRIPT INCOMPLETE/, "proven=false stays an unverified seam -> loud marker");
  assert.doesNotMatch(whole, /reconstructed/i, "no green bookend on an unproven seam");
});

// --- Task 28 weakest-link (architect): a set with TWO seams on the live path, one proven
// --- one not, must STILL mark. rootTrusted is !path.some(unproven-seam), so a single
// --- unprovable seam poisons the whole promotion; an anded-wrong fold would false-green here.
test("i1e: two seams on the path, one proven one not, still mark (weakest-link)", async () => {
  const G1 = { uuid: "G1", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1", message: { role: "user", content: "the sole origin" } };
  const S1 = { uuid: "pfgk-seam-AAAAAAAA", type: "user", parentUuid: "G1", __pfgkSeam: { reattachTarget: "G1", proven: true }, sessionId: "s", timestamp: "t2", message: { role: "user", content: "PFGK1:" + JSON.stringify({ rows: [], body: "proven seam" }) } };
  const BND1 = { uuid: "BND1", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "pfgk-seam-AAAAAAAA", sessionId: "s", timestamp: "t3", message: { role: "user", content: "" } };
  const MID = { uuid: "MID", type: "user", parentUuid: "BND1", sessionId: "s", timestamp: "t4", message: { role: "user", content: "a middle turn" } };
  const S2 = { uuid: "pfgk-seam-BBBBBBBB", type: "user", parentUuid: "MID", __pfgkSeam: { reattachTarget: "MID", proven: false }, sessionId: "s", timestamp: "t5", message: { role: "user", content: "PFGK1:" + JSON.stringify({ rows: [], body: "UNproven seam" }) } };
  const BND2 = { uuid: "BND2", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "pfgk-seam-BBBBBBBB", sessionId: "s", timestamp: "t6", message: { role: "user", content: "" } };
  const LEAF = { uuid: "LEAF", type: "user", parentUuid: "BND2", sessionId: "s", timestamp: "t7", message: { role: "user", content: "the latest turn" } };
  const i1e = i1eWith(deps);
  const whole = JSON.stringify(await i1e([G1, S1, BND1, MID, S2, BND2, LEAF], TEL));
  assert.match(whole, /TRANSCRIPT INCOMPLETE/, "one unproven seam on the path poisons the whole chain -> loud marker");
  assert.doesNotMatch(whole, /reconstructed/i, "no green bookend when any seam on the path is unproven");
});

// --- Task 22 (never degrade silently): the same uuid re-appended with two DIFFERENT
// --- logicalParentUuids is a corrupt collision. last-wins byUuid would silently keep the
// --- last one, and here that one reaches a real origin O, so it would FALSE-GREEN. The loud
// --- detect fails the verdict instead. Non-vacuous: without the detect this renders green.
test("i1e: a divergent logicalParentUuid (corrupt uuid collision) fires the loud marker, not a false green", async () => {
  const O = { uuid: "O", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1", message: { role: "user", content: "a real origin" } };
  const M1 = { uuid: "M", type: "assistant", parentUuid: null, logicalParentUuid: "DANGLE-OFF-DISK", sessionId: "s", timestamp: "t2", message: { role: "assistant", content: "M with one lpu" } };
  const M2 = { uuid: "M", type: "assistant", parentUuid: null, logicalParentUuid: "O", sessionId: "s", timestamp: "t3", message: { role: "assistant", content: "M re-appended with a DIFFERENT lpu that reaches O" } };
  const LEAF = { uuid: "LEAF", type: "user", parentUuid: "M", sessionId: "s", timestamp: "t4", message: { role: "user", content: "the latest turn" } };
  const i1e = i1eWith(deps);
  const whole = JSON.stringify(await i1e([O, M1, M2, LEAF], TEL));
  assert.match(whole, /TRANSCRIPT INCOMPLETE|CHAIN CORRUPT/, "a divergent-lpu uuid collision -> loud, not the false green last-wins would give");
  assert.doesNotMatch(whole, /reconstructed/i, "never a green bookend on a divergent-lpu corruption");
});

// --- Task 12A: the in-context band must key on the boundary the MODEL resumes from (the
// --- first compaction up from the live leaf), NOT the physically-last one, which can be a
// --- re-appended older duplicate. The live chain M->LEAF sits AFTER the real compaction
// --- c9a6635f but BEFORE the re-appended 586fd2dc in file order; keying on physical-last
// --- mis-classifies the whole live chain as pre-boundary "summarized" (in-context 0),
// --- keying on the resume boundary counts M+LEAF live (2). (No preservedMessages, so the
// --- vendor reparent stays out of it; the boundary PICK alone drives the pre/post split.)
test("i1e band: 'in resume context' keys on the resume boundary, not a re-appended older duplicate", async () => {
  const O = { uuid: "O", type: "user", parentUuid: null, sessionId: "s", timestamp: "t1", message: { role: "user", content: "a disjoint older-branch origin" } };
  const BND_A = { uuid: "c9a6635f", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "OFF-DISK", sessionId: "s", timestamp: "t2" };
  const MIDT = { uuid: "MIDT", type: "assistant", parentUuid: "c9a6635f", sessionId: "s", timestamp: "t3", message: { role: "assistant", content: "a post-compaction turn, in context" } };
  const LEAF = { uuid: "LEAF", type: "user", parentUuid: "MIDT", sessionId: "s", timestamp: "t4", message: { role: "user", content: "the latest turn" } };
  const BND_R = { uuid: "586fd2dc", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "OFF-DISK2", sessionId: "s", timestamp: "t5" };
  const i1e = i1eWith(deps);
  // file order: O, BND_A(resume), MIDT, LEAF, BND_R(re-append, physically last).
  const ren = await i1e([O, BND_A, MIDT, LEAF, BND_R], TEL);
  const banner = ren.map(pfgk).find((p) => p && /CHAIN CORRUPT/.test(p.badge || ""));
  assert.ok(banner, "the disjoint older branch O makes the corpus corrupt -> CHAIN CORRUPT banner");
  const inCtx = banner.rows.find((row) => row[0] === "in resume context");
  assert.equal(inCtx[1], "2", "post-resume-boundary live chain (MIDT, LEAF) is in context; physical-last would misclassify to 0");
});
