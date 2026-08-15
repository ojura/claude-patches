// Determinism contract for the loader (src/d1e.js): the recovered transcript is
// a pure function of the input corpus BYTES, never of readdir order or mtime
// (see docs/invariant.md "Determinism").
//
// This runs the REAL shipped d1e: it loads src/d1e.js, prepends the real $pfg
// block emitted by util/pfg-codegen.py (exactly as the bundle composes them),
// and injects mock vendor deps over an in-memory corpus. No paraphrase.
//
//   run:  node --test src/d1e.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import * as nodePath from "node:path";
import { byUuid, walkToRoot, rootTrusted } from "./pfg-core.js";

const HERE = nodePath.dirname(new URL(import.meta.url).pathname);

// Build the loader once: [ $pfg block ] + [ d1e source ] + `return d1e`, with the
// vendor symbols as injected parameters. Exactly the shape the bundle presents.
let _factory;
function d1eWith(deps) {
  if (!_factory) {
    const d1eSrc = readFileSync(nodePath.join(HERE, "d1e.js"), "utf8");
    const pfgBlock = execFileSync("python3", [nodePath.join(HERE, "..", "util", "pfg-codegen.py")], {
      encoding: "utf8",
    });
    _factory = new Function(
      "_pv_Nk", "_pv_qAe", "_pv_r1e", "_pv_n1e", "_pv_zn", "_pv_MY", "_pv_GY",
      pfgBlock + "\n" + d1eSrc + "\nreturn d1e;",
    );
  }
  return _factory(deps.Nk, deps.qAe, deps.r1e, deps.n1e, deps.zn, deps.MY, deps.GY);
}

// A minimal corpus with a deliberate TIE. main.jsonl's compaction boundary
// dangles on lpu "P"; two siblings both carry "P" with equal-length, DIFFERENT
// prefixes (origin A vs origin B). Which one wins must not depend on file order.
const CORPUS = {
  "/corpus/main.jsonl": [
    { type: "system", subtype: "compact_boundary", uuid: "bnd", parentUuid: null, logicalParentUuid: "P", sessionId: "s", timestamp: "t" },
    { uuid: "leaf", type: "user", parentUuid: "bnd", message: { role: "user", content: "latest turn" } },
  ],
  "/corpus/aaa.jsonl": [
    { uuid: "oa", type: "user", parentUuid: null, message: { role: "user", content: "origin A" } },
    { uuid: "P", type: "assistant", parentUuid: "oa", message: { role: "assistant", content: "P via A" } },
  ],
  "/corpus/bbb.jsonl": [
    { uuid: "ob", type: "user", parentUuid: null, message: { role: "user", content: "origin B" } },
    { uuid: "P", type: "assistant", parentUuid: "ob", message: { role: "assistant", content: "P via B" } },
  ],
};
const toJsonl = (recs) => recs.map((r) => JSON.stringify(r)).join("\n") + "\n";

function mocks(readdirOrder) {
  return {
    Nk: () => true,
    qAe: async () => ({ filePath: "/corpus/main.jsonl", fileSize: 1 }),
    r1e: async () => Buffer.from(toJsonl(CORPUS["/corpus/main.jsonl"]), "utf8"),
    n1e: async (buf) => buf.toString("utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)),
    zn: nodePath,
    MY: {
      readdir: async () => readdirOrder.slice(),
      readFile: async (p) => Buffer.from(toJsonl(CORPUS[p]), "utf8"),
    },
    GY: (parsed) => parsed, // return the reconstruction itself for inspection
  };
}

async function reconstruct(readdirOrder) {
  const d1e = d1eWith(mocks(readdirOrder));
  const out = await d1e({}, { dir: "/corpus" });
  return out.map((r) => r.uuid);
}

test("determinism: readdir order does not change the reconstruction", async () => {
  const forward = await reconstruct(["aaa.jsonl", "bbb.jsonl"]);
  const reverse = await reconstruct(["bbb.jsonl", "aaa.jsonl"]);
  assert.deepEqual(
    forward,
    reverse,
    "same corpus bytes, reversed readdir order, must reconstruct identically",
  );
  // And it must be the deterministic winner (lexicographically-first tied sibling
  // aaa -> origin A), not merely stable: origin A present, origin B never spliced.
  assert.ok(forward.includes("oa"), "picked aaa (origin A) on the tie");
  assert.ok(!forward.includes("ob"), "did not splice the losing sibling bbb (origin B)");
});

test("the tie is real: both siblings independently qualify as backfill sources", async () => {
  // If this ever fails because only one sibling qualifies, the determinism test
  // above is vacuous (no tie to break). Guard against that rotting silently.
  const viaA = await reconstruct(["aaa.jsonl"]);
  const viaB = await reconstruct(["bbb.jsonl"]);
  assert.ok(viaA.includes("oa") && !viaA.includes("ob"), "aaa alone backfills origin A");
  assert.ok(viaB.includes("ob") && !viaB.includes("oa"), "bbb alone backfills origin B");
});

// --- K1 ambiguous-phantom selection (council: d1e loader seat). The boundary's
// --- logical parent "PH" is a PHANTOM (on no record); two siblings each carry it
// --- as their own boundary's lpu and SHARE pre-compaction records s1/s2. The old
// --- K1 mutated _seen WHILE comparing candidates, so the first-scanned sibling
// --- claimed the shared records and shrank the second's count below its own,
// --- electing the shallower sibling and stranding the loser's records in _seen.
const K1_CORPUS = {
  "/k1/main.jsonl": [
    { type: "system", subtype: "compact_boundary", uuid: "bndm", parentUuid: null, logicalParentUuid: "PH", sessionId: "s", timestamp: "t" },
    { uuid: "leaf", type: "user", parentUuid: "bndm", message: { role: "user", content: "latest turn" } },
  ],
  // aaa: 3 unique pre-boundary records (oa, s1, s2).
  "/k1/aaa.jsonl": [
    { uuid: "oa", type: "user", parentUuid: null, message: { role: "user", content: "origin A" } },
    { uuid: "s1", type: "user", parentUuid: "oa", message: { role: "user", content: "shared 1" } },
    { uuid: "s2", type: "user", parentUuid: "s1", message: { role: "user", content: "shared 2" } },
    { type: "system", subtype: "compact_boundary", uuid: "ba", parentUuid: null, logicalParentUuid: "PH", sessionId: "s", timestamp: "t" },
    { uuid: "posta", type: "user", parentUuid: "ba", message: { role: "user", content: "post A" } },
  ],
  // bbb: 4 unique pre-boundary records (ob, s1, s2, s3), genuinely deeper, and it
  // SHARES s1/s2 with aaa. It must win the length comparison on the merits.
  "/k1/bbb.jsonl": [
    { uuid: "ob", type: "user", parentUuid: null, message: { role: "user", content: "origin B" } },
    { uuid: "s1", type: "user", parentUuid: "ob", message: { role: "user", content: "shared 1" } },
    { uuid: "s2", type: "user", parentUuid: "s1", message: { role: "user", content: "shared 2" } },
    { uuid: "s3", type: "user", parentUuid: "s2", message: { role: "user", content: "unique 3" } },
    { type: "system", subtype: "compact_boundary", uuid: "bb", parentUuid: null, logicalParentUuid: "PH", sessionId: "s", timestamp: "t" },
    { uuid: "postb", type: "user", parentUuid: "bb", message: { role: "user", content: "post B" } },
  ],
};

async function reconstructK1(readdirOrder) {
  const d1e = d1eWith({
    Nk: () => true,
    qAe: async () => ({ filePath: "/k1/main.jsonl", fileSize: 1 }),
    r1e: async () => Buffer.from(toJsonl(K1_CORPUS["/k1/main.jsonl"]), "utf8"),
    n1e: async (buf) => buf.toString("utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)),
    zn: nodePath,
    MY: { readdir: async () => readdirOrder.slice(), readFile: async (p) => Buffer.from(toJsonl(K1_CORPUS[p]), "utf8") },
    GY: (parsed) => parsed,
  });
  const out = await d1e({}, { dir: "/k1" });
  return out.map((r) => r.uuid);
}

test("K1 ambiguous phantom: the genuinely-deeper sibling wins, not the first scanned", async () => {
  // Entries are sorted, so aaa is always scanned before bbb. The old mutation let
  // aaa claim shared s1/s2, dropping bbb's count to 2 (< aaa's 3) so aaa won. bbb
  // contributes 4 unique pre-records to aaa's 3, so bbb must win on the merits.
  const out = await reconstructK1(["aaa.jsonl", "bbb.jsonl"]);
  assert.ok(out.includes("ob") && out.includes("s3"), "deeper sibling bbb backfilled (origin B plus its unique s3)");
  assert.ok(!out.includes("oa"), "shallower sibling aaa did not win the length comparison");
});

function pfgkBody(rec) {
  if (!rec || !rec.message || typeof rec.message.content !== "string" || !rec.message.content.startsWith("PFGK1:")) return "";
  try { return JSON.parse(rec.message.content.slice("PFGK1:".length)).body || ""; } catch { return ""; }
}

// --- J divergent-fork ambiguity disclosure (design-continuity seats; live: 6 of 18
// --- trunk lpus are held by >1 sibling). Two siblings carry the same dangling lpu L
// --- but DIVERGE (each holds a pre-L record the other lacks). J picks by volume; the
// --- K3 bridge ghost must disclose that the pick is one of several reconstructions.
const J_DIVERGENT = {
  "/jd/main.jsonl": [
    { type: "system", subtype: "compact_boundary", uuid: "bnd", parentUuid: null, logicalParentUuid: "L", sessionId: "s", timestamp: "t" },
    { uuid: "leaf", type: "user", parentUuid: "bnd", message: { role: "user", content: "latest" } },
  ],
  "/jd/aaa.jsonl": [
    { uuid: "oA", type: "user", parentUuid: null, message: { role: "user", content: "origin A" } },
    { uuid: "xA", type: "user", parentUuid: "oA", message: { role: "user", content: "A-only pre-compaction turn" } },
    { uuid: "L", type: "assistant", parentUuid: "xA", message: { role: "assistant", content: "the shared lpu record" } },
    { uuid: "postA", type: "user", parentUuid: "L", message: { role: "user", content: "post A" } },
  ],
  "/jd/bbb.jsonl": [
    { uuid: "oB", type: "user", parentUuid: null, message: { role: "user", content: "origin B" } },
    { uuid: "yB", type: "user", parentUuid: "oB", message: { role: "user", content: "B-only pre-compaction turn (divergent)" } },
    { uuid: "L", type: "assistant", parentUuid: "yB", message: { role: "assistant", content: "the shared lpu record" } },
    { uuid: "postB", type: "user", parentUuid: "L", message: { role: "user", content: "post B" } },
  ],
};

// The mirror: NESTED candidates (aaa's pre-L is a subset of bbb's), so the volume
// winner bbb IS the complete superset. Not a divergent fork -> must NOT warn.
const J_NESTED = {
  "/jn/main.jsonl": [
    { type: "system", subtype: "compact_boundary", uuid: "bnd", parentUuid: null, logicalParentUuid: "L", sessionId: "s", timestamp: "t" },
    { uuid: "leaf", type: "user", parentUuid: "bnd", message: { role: "user", content: "latest" } },
  ],
  "/jn/aaa.jsonl": [
    { uuid: "o", type: "user", parentUuid: null, message: { role: "user", content: "shared origin" } },
    { uuid: "L", type: "assistant", parentUuid: "o", message: { role: "assistant", content: "the shared lpu record" } },
    { uuid: "postA", type: "user", parentUuid: "L", message: { role: "user", content: "post A" } },
  ],
  "/jn/bbb.jsonl": [
    { uuid: "o", type: "user", parentUuid: null, message: { role: "user", content: "shared origin" } },
    { uuid: "x", type: "user", parentUuid: "o", message: { role: "user", content: "deeper shared pre-turn" } },
    { uuid: "L", type: "assistant", parentUuid: "x", message: { role: "assistant", content: "the shared lpu record" } },
    { uuid: "postB", type: "user", parentUuid: "L", message: { role: "user", content: "post B" } },
  ],
};

async function reconstructFrom(corpus, dir) {
  const d1e = d1eWith({
    Nk: () => true,
    qAe: async () => ({ filePath: dir + "/main.jsonl", fileSize: 1 }),
    r1e: async () => Buffer.from(toJsonl(corpus[dir + "/main.jsonl"]), "utf8"),
    n1e: async (buf) => buf.toString("utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)),
    zn: nodePath,
    MY: { readdir: async () => ["aaa.jsonl", "bbb.jsonl"], readFile: async (p) => Buffer.from(toJsonl(corpus[p]), "utf8") },
    GY: (parsed) => parsed,
  });
  return d1e({}, { dir });
}

test("J divergent fork: the bridge ghost discloses the ambiguous reconstruction", async () => {
  const out = await reconstructFrom(J_DIVERGENT, "/jd");
  const bridge = out.find((r) => String(r.uuid).startsWith("pfgk-bridge-"));
  assert.ok(bridge, "a cross-file bridge ghost was planted");
  assert.match(pfgkBody(bridge), /AMBIGUOUS RECONSTRUCTION/, "divergent siblings -> the bridge discloses one of several reconstructions");
});

test("J nested candidates: the winner subsumes the losers, so NO false ambiguity warning", async () => {
  const out = await reconstructFrom(J_NESTED, "/jn");
  const bridge = out.find((r) => String(r.uuid).startsWith("pfgk-bridge-"));
  assert.ok(bridge, "a cross-file bridge ghost was planted");
  assert.doesNotMatch(pfgkBody(bridge), /AMBIGUOUS/, "nested (superset) candidates are not a divergent fork");
});

// --- Divergent-edge resolution: a uuid re-appended with two different parentUuids.
// --- last-wins would keep whichever was written last (here a dead-ending edge). The
// --- resolver instead walks each candidate and keeps the one reaching an
// --- origin. Investigated, not guessed.
const DIVERGENT_EDGE = {
  "/de/main.jsonl": [
    { uuid: "O", type: "user", parentUuid: null, message: { role: "user", content: "the origin" } },
    { uuid: "U", type: "assistant", parentUuid: "O", message: { role: "assistant", content: "a turn, first written with the correct parent" } },
    { uuid: "leaf", type: "user", parentUuid: "U", message: { role: "user", content: "latest" } },
    { uuid: "U", type: "assistant", parentUuid: "DEAD-OFF-DISK", message: { role: "assistant", content: "the same turn, re-appended with a dead parent (last-wins would keep this)" } },
  ],
};

test("divergent-edge: resolves a re-appended uuid to the parent that reaches an origin", async () => {
  const d1e = d1eWith({
    Nk: () => true,
    qAe: async () => ({ filePath: "/de/main.jsonl", fileSize: 1 }),
    r1e: async () => Buffer.from(toJsonl(DIVERGENT_EDGE["/de/main.jsonl"]), "utf8"),
    n1e: async (buf) => buf.toString("utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)),
    zn: nodePath,
    MY: { readdir: async () => [], readFile: async () => Buffer.from("", "utf8") },
    GY: (parsed) => parsed,
  });
  const out = await d1e({}, { dir: "/de" });
  const us = out.filter((r) => r.uuid === "U");
  assert.ok(us.length >= 1, "U is present in the reconstruction");
  assert.ok(us.every((r) => r.parentUuid === "O"), "U's edge resolved to O (reaches the origin), not the dead-end that last-wins picked");
  assert.ok(!us.some((r) => r.parentUuid === "DEAD-OFF-DISK"), "the dead-ending edge was not kept");
});

test("d1e->i1e precondition: the reconstruction handed to i1e has NO divergent parentUuid", async () => {
  // i1e's last-wins $pfg.byUuid keeps ONE record per uuid, so it is only safe because
  // d1e's divergent-edge pass already collapsed every uuid to a single parentUuid before
  // the handoff. This pins that precondition at the boundary: were i1e ever driven WITHOUT
  // d1e (a future CLI target, or a direct i1e test), a residual divergent parentUuid would
  // let last-wins pick an arbitrary edge. SCOPE (documented, 0-observed, not yet guarded):
  // the pass resolves parentUuid divergence only, NOT logicalParentUuid divergence.
  const d1e = d1eWith({
    Nk: () => true,
    qAe: async () => ({ filePath: "/de/main.jsonl", fileSize: 1 }),
    r1e: async () => Buffer.from(toJsonl(DIVERGENT_EDGE["/de/main.jsonl"]), "utf8"),
    n1e: async (buf) => buf.toString("utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)),
    zn: nodePath,
    MY: { readdir: async () => [], readFile: async () => Buffer.from("", "utf8") },
    GY: (parsed) => parsed,
  });
  const out = await d1e({}, { dir: "/de" });
  const parentsByUuid = new Map();
  for (const r of out) {
    if (!r || !r.uuid) continue;
    if (!parentsByUuid.has(r.uuid)) parentsByUuid.set(r.uuid, new Set());
    parentsByUuid.get(r.uuid).add(r.parentUuid ?? null);
  }
  const divergent = [...parentsByUuid].filter(([, ps]) => ps.size > 1).map(([u]) => u);
  assert.deepEqual(divergent, [], "every uuid handed to i1e collapses to a single parentUuid (no residual divergence)");
});

// --- Task 28 (seam-trust evidence): d1e stamps __pfgkSeam.proven on the K2 seam. The
// --- Option-2 guard promotes (proven=true) ONLY when the file has EXACTLY ONE
// --- forkedFrom-free own-origin, so the positional reattach cannot land on the wrong
// --- own-branch. These two cases are the mutually non-vacuous halves.
const _k2Deps = (corpus, dir) => ({
  Nk: () => true,
  qAe: async () => ({ filePath: dir + "/main.jsonl", fileSize: 1 }),
  r1e: async () => Buffer.from(toJsonl(corpus), "utf8"),
  n1e: async (buf) => buf.toString("utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)),
  zn: nodePath,
  MY: { readdir: async () => [], readFile: async () => Buffer.from("", "utf8") },
  GY: (parsed) => parsed,
});

function preservedCycleFixture({ secondOrigin = false, boundaryFirst = false } = {}) {
  const team = { teamName: "session-deadbeef", agentName: "researcher", sessionId: "team-session" };
  const before = [
    { ...team, uuid: "O", type: "user", parentUuid: null, message: { role: "user", content: "the teammate assignment" } },
    { ...team, uuid: "PRE", type: "assistant", parentUuid: "O", message: { role: "assistant", content: "the pre-compaction reply" } },
  ];
  if (secondOrigin) before.push({ ...team, uuid: "O2", type: "user", parentUuid: null, message: { role: "user", content: "a second possible origin" } });
  const boundary = {
    ...team,
    uuid: "BND01234",
    type: "system",
    subtype: "compact_boundary",
    parentUuid: null,
    logicalParentUuid: "TAIL",
    compactMetadata: {
      preservedSegment: { headUuid: "HEAD", anchorUuid: "SUM", tailUuid: "TAIL" },
      preservedMessages: { anchorUuid: "SUM", uuids: ["HEAD", "MIDP", "TAIL"], allUuids: ["HEAD", "MIDP", "TAIL"] },
    },
  };
  const post = [
    { ...team, uuid: "SUM", type: "user", parentUuid: "BND01234", isCompactSummary: true, message: { role: "user", content: "compact summary" } },
    { ...team, uuid: "HEAD", type: "assistant", parentUuid: "SUM", message: { role: "assistant", content: "preserved head" } },
    { ...team, uuid: "MIDP", type: "assistant", parentUuid: "HEAD", message: { role: "assistant", content: "preserved middle" } },
    { ...team, uuid: "TAIL", type: "user", parentUuid: "MIDP", message: { role: "user", content: "preserved tail" } },
    { ...team, uuid: "LEAF", type: "assistant", parentUuid: "TAIL", message: { role: "assistant", content: "latest" } },
  ];
  return boundaryFirst ? [boundary, ...post] : [...before, boundary, ...post];
}

test("d1e: repairs an in-file lpu-into-preserved-tail cycle and reaches the team origin", async () => {
  const d1e = d1eWith(_k2Deps(preservedCycleFixture(), "/pc"));
  const out = await d1e({}, { dir: "/pc" });
  const ghost = out.find((r) => r.uuid === "pfgk-seamClean-BND01234");
  assert.ok(ghost, "a verified preserved-cycle repair plants the existing seamClean marker");
  assert.equal(ghost.parentUuid, "PRE", "the marker reconnects to the record immediately before the boundary");
  assert.equal(out.find((r) => r.uuid === "HEAD").parentUuid, ghost.uuid, "preserved head follows the repair marker");
  assert.equal(out.find((r) => r.uuid === "MIDP").parentUuid, "HEAD");
  assert.equal(out.find((r) => r.uuid === "TAIL").parentUuid, "MIDP");
  const walk = walkToRoot("LEAF", byUuid(out));
  assert.equal(walk.reason, "origin");
  assert.equal(walk.terminus.uuid, "O", "the repaired teammate chain reaches its first assignment");
  assert.equal(rootTrusted(walk), true);
  assert.equal(out.find((r) => r.uuid === "BND01234").__pfgkPreservedCycleRepair.proven, true);
});

test("d1e: a preserved-tail repair with multiple possible origins stays unproven", async () => {
  const d1e = d1eWith(_k2Deps(preservedCycleFixture({ secondOrigin: true }), "/pc2"));
  const out = await d1e({}, { dir: "/pc2" });
  const ghost = out.find((r) => r.uuid === "pfgk-seam-BND01234");
  assert.ok(ghost, "ambiguous positional repair uses the warning seam, not seamClean");
  assert.equal(ghost.__pfgkSeam.proven, false);
  assert.equal(rootTrusted(walkToRoot("LEAF", byUuid(out))), false, "the restored path is visible but not trusted to one origin");
});

test("d1e: a preserved-tail cycle at file start is not guessed onto an absent predecessor", async () => {
  const d1e = d1eWith(_k2Deps(preservedCycleFixture({ boundaryFirst: true }), "/pc3"));
  const out = await d1e({}, { dir: "/pc3" });
  assert.ok(!out.some((r) => String(r.uuid).startsWith("pfgk-seamClean-BND01234")), "no clean repair without a pre-boundary record");
  assert.equal(walkToRoot("TAIL", byUuid(out)).reason, "cycle", "the unresolved cycle remains for i1e's loud fallback");
});

test("d1e: a K2 seam in a single-own-origin file is stamped proven=true (promotable)", async () => {
  // O -> pre -> boundary(phantom lpu) -> leaf, no sibling carries the lpu, so K2 reattaches
  // the boundary to `pre`. One own-origin O, and `pre` walks to it cleanly -> proven.
  const d1e = d1eWith(_k2Deps([
    { uuid: "O", type: "user", parentUuid: null, message: { role: "user", content: "the sole origin" } },
    { uuid: "pre", type: "assistant", parentUuid: "O", message: { role: "assistant", content: "a pre-compaction turn" } },
    { uuid: "BND", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "PHANTOM-OFF-DISK", sessionId: "s", timestamp: "t" },
    { uuid: "leaf", type: "user", parentUuid: "BND", message: { role: "user", content: "latest" } },
  ], "/k2a"));
  const out = await d1e({}, { dir: "/k2a" });
  const seam = out.find((r) => typeof r.uuid === "string" && r.uuid.startsWith("pfgk-seam-"));
  assert.ok(seam, "a K2 seam was planted for the phantom-lpu boundary");
  assert.equal(seam.__pfgkSeam.proven, true, "single own-origin + clean reattach walk -> proven");
});

test("d1e: a K2 seam in a MULTI-own-origin file is stamped proven=false (architect condition c)", async () => {
  // Two forkedFrom-free own-origins A and B. The seam's positional reattach could be the
  // wrong branch (the live leaf's true origin might be B, not A), so the guard refuses to
  // promote: proven=false -> the seam still fires the loud marker downstream in i1e.
  const d1e = d1eWith(_k2Deps([
    { uuid: "A", type: "user", parentUuid: null, message: { role: "user", content: "own-origin A" } },
    { uuid: "a1", type: "assistant", parentUuid: "A", message: { role: "assistant", content: "A's branch turn (positional predecessor)" } },
    { uuid: "BND", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "PHANTOM-OFF-DISK", sessionId: "s", timestamp: "t" },
    { uuid: "leaf", type: "user", parentUuid: "BND", message: { role: "user", content: "latest (its true origin B is off the seam's reach)" } },
    { uuid: "B", type: "user", parentUuid: null, message: { role: "user", content: "own-origin B" } },
  ], "/k2b"));
  const out = await d1e({}, { dir: "/k2b" });
  const seam = out.find((r) => typeof r.uuid === "string" && r.uuid.startsWith("pfgk-seam-"));
  assert.ok(seam, "a K2 seam was planted");
  assert.equal(seam.__pfgkSeam.proven, false, "two own-origins -> ambiguous reattach -> not proven");
});

// --- Fork source backfill (forkedFrom, investigate-don't-guess): a forkedFrom origin
// --- is a copy from a source session. d1e reads the source and resolves it three ways.
async function reconstructFork(mainPath, dir, files) {
  const d1e = d1eWith({
    Nk: () => true,
    qAe: async () => ({ filePath: mainPath, fileSize: 1 }),
    r1e: async () => Buffer.from(toJsonl(files[mainPath]), "utf8"),
    n1e: async (buf) => buf.toString("utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)),
    zn: nodePath,
    MY: {
      readdir: async () => [],
      readFile: async (p) => { if (!files[p]) throw new Error("ENOENT " + p); return Buffer.from(toJsonl(files[p]), "utf8"); },
    },
    GY: (parsed) => parsed,
  });
  return d1e({}, { dir });
}

test("fork backfill: a mid-conversation branch is re-rooted onto its source ancestry", async () => {
  const files = {
    "/fk/main.jsonl": [
      { uuid: "FO", type: "user", parentUuid: null, forkedFrom: { sessionId: "src", messageUuid: "FO" }, message: { role: "user", content: "the copied fork point" } },
      { uuid: "leaf", type: "user", parentUuid: "FO", forkedFrom: { sessionId: "src", messageUuid: "leaf" }, message: { role: "user", content: "latest" } },
    ],
    "/fk/src.jsonl": [
      { uuid: "srcO", type: "user", parentUuid: null, message: { role: "user", content: "the source's origin" } },
      { uuid: "pre1", type: "user", parentUuid: "srcO", message: { role: "user", content: "a pre-fork turn" } },
      { uuid: "FO", type: "user", parentUuid: "pre1", message: { role: "user", content: "the fork point in the source" } },
      { uuid: "srcpost", type: "user", parentUuid: "FO", message: { role: "user", content: "source post-fork" } },
    ],
  };
  const out = await reconstructFork("/fk/main.jsonl", "/fk", files);
  const uuids = out.map((r) => r.uuid);
  assert.ok(uuids.includes("srcO") && uuids.includes("pre1"), "the source's pre-fork ancestry was prepended");
  assert.ok(out.some((r) => r.uuid === "FO" && r.parentUuid === "pre1"), "the copied origin was re-rooted onto its real source parent");
});

test("fork backfill: a branch taken at the source's start is flagged complete", async () => {
  const files = {
    "/fc/main.jsonl": [
      { uuid: "FO2", type: "user", parentUuid: null, forkedFrom: { sessionId: "src2", messageUuid: "FO2" }, message: { role: "user", content: "the copied origin (branch at source start)" } },
      { uuid: "leaf2", type: "user", parentUuid: "FO2", message: { role: "user", content: "latest" } },
    ],
    "/fc/src2.jsonl": [
      { uuid: "FO2", type: "user", parentUuid: null, message: { role: "user", content: "the source's origin, same as the fork's" } },
      { uuid: "s2post", type: "user", parentUuid: "FO2", message: { role: "user", content: "source content" } },
    ],
  };
  const out = await reconstructFork("/fc/main.jsonl", "/fc", files);
  const fo = out.find((r) => r.uuid === "FO2");
  assert.equal(fo.__pfgkForkComplete, true, "complete fork flagged so i1e renders it green, not a hedged note");
});

test("fork backfill: an off-disk source leaves the origin unresolved (i1e then fires the marker)", async () => {
  const files = {
    "/fo/main.jsonl": [
      { uuid: "FO3", type: "user", parentUuid: null, forkedFrom: { sessionId: "gone", messageUuid: "FO3" }, message: { role: "user", content: "copied origin; source not in this folder" } },
      { uuid: "leaf3", type: "user", parentUuid: "FO3", message: { role: "user", content: "latest" } },
    ],
  };
  const out = await reconstructFork("/fo/main.jsonl", "/fo", files);
  const fo = out.find((r) => r.uuid === "FO3");
  assert.ok(fo.forkedFrom, "forkedFrom preserved (unresolved)");
  assert.ok(!fo.__pfgkForkComplete, "NOT flagged complete: the source could not be investigated");
  assert.equal(fo.parentUuid, null, "not re-rooted (source off-disk)");
});

test("fork backfill: a boundary-headed source is NOT complete (off-disk ancestry via the boundary lpu)", async () => {
  // The source session was ITSELF compacted before the fork point: it opens on a
  // compact_boundary whose lpu points off disk. The old isMain-position scan saw no
  // main-line turn before the fork point and wrongly flagged the copy complete (green);
  // a compact_boundary is non-isMain yet carries off-disk ancestry via its lpu, so the
  // copy is NOT the origin. classifyRoot(fork point) is false here, so d1e prepends the
  // boundary and re-roots, and i1e then fires the marker on the off-disk lpu.
  const files = {
    "/bh/main.jsonl": [
      { uuid: "FO4", type: "user", parentUuid: null, forkedFrom: { sessionId: "src4", messageUuid: "FO4" }, message: { role: "user", content: "copied fork point; source was pre-compacted" } },
      { uuid: "leaf4", type: "user", parentUuid: "FO4", message: { role: "user", content: "latest" } },
    ],
    "/bh/src4.jsonl": [
      { uuid: "bnd4", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "OFFDISK" },
      { uuid: "FO4", type: "user", parentUuid: "bnd4", message: { role: "user", content: "the fork point, parented on the source's own compaction boundary" } },
      { uuid: "s4post", type: "user", parentUuid: "FO4", message: { role: "user", content: "source post-fork" } },
    ],
  };
  const out = await reconstructFork("/bh/main.jsonl", "/bh", files);
  const fo = out.find((r) => r.uuid === "FO4");
  assert.ok(!fo.__pfgkForkComplete, "NOT complete: the source's own origin is off disk via the boundary lpu");
  assert.ok(out.some((r) => r.uuid === "bnd4"), "the source's compaction boundary was prepended");
  assert.equal(fo.parentUuid, "bnd4", "the copy was re-rooted onto its real source parent (the boundary)");
});

test("fork backfill: F keys the source lookup on forkedFrom.messageUuid, not the copy's own uuid", async () => {
  // On all real data messageUuid === uuid, so this is defensive: if a branch copy ever
  // got a FRESH uuid, F must still find the fork point in the source by the ATTESTED
  // messageUuid. Keying on the copy's own uuid would silently no-op into a premature marker.
  const files = {
    "/mu/main.jsonl": [
      { uuid: "COPY", type: "user", parentUuid: null, forkedFrom: { sessionId: "src5", messageUuid: "SRCPT" }, message: { role: "user", content: "copy with a fresh uuid" } },
      { uuid: "leaf5", type: "user", parentUuid: "COPY", message: { role: "user", content: "latest" } },
    ],
    "/mu/src5.jsonl": [
      { uuid: "s5O", type: "user", parentUuid: null, message: { role: "user", content: "the source origin" } },
      { uuid: "SRCPT", type: "user", parentUuid: "s5O", message: { role: "user", content: "the fork point in the source (uuid != the copy's)" } },
    ],
  };
  const out = await reconstructFork("/mu/main.jsonl", "/mu", files);
  const uuids = out.map((r) => r.uuid);
  assert.ok(uuids.includes("s5O"), "F found the fork point by forkedFrom.messageUuid and prepended the source ancestry");
  const copy = out.find((r) => r.uuid === "COPY");
  assert.equal(copy.parentUuid, "s5O", "the copy was re-rooted onto its real source parent (found via messageUuid)");
});

test("fork backfill: a two-level on-disk fork chain resolves to the grand-source origin, not a premature marker", async () => {
  // copy -> source (itself a fork) -> grand-source origin, all on disk. A single F pass
  // resolves level 1, sees the source's origin is itself a fork, and would fire a
  // premature marker (forbidden-middle #2). F's fixed point must CHAIN: redirect the copy
  // at the grand-source and re-investigate, until the chain reaches a real origin.
  const files = {
    "/nf/main.jsonl": [
      { uuid: "COPY", type: "user", parentUuid: null, forkedFrom: { sessionId: "src", messageUuid: "SF" }, message: { role: "user", content: "copy of the source's fork origin" } },
      { uuid: "nleaf", type: "user", parentUuid: "COPY", message: { role: "user", content: "latest" } },
    ],
    "/nf/src.jsonl": [
      { uuid: "SF", type: "user", parentUuid: null, forkedFrom: { sessionId: "grand", messageUuid: "GO" }, message: { role: "user", content: "source's origin, itself forked from grand" } },
      { uuid: "sfpost", type: "user", parentUuid: "SF", message: { role: "user", content: "source content" } },
    ],
    "/nf/grand.jsonl": [
      { uuid: "GO", type: "user", parentUuid: null, message: { role: "user", content: "the grand-source's real first prompt" } },
      { uuid: "gopost", type: "user", parentUuid: "GO", message: { role: "user", content: "grand content" } },
    ],
  };
  const out = await reconstructFork("/nf/main.jsonl", "/nf", files);
  const copy = out.find((r) => r.uuid === "COPY");
  assert.equal(copy.__pfgkForkComplete, true, "the two-level chain resolved to the grand-source's real origin: complete, not a marker");
  assert.ok(copy.forkedFrom && copy.forkedFrom.sessionId === "grand", "the copy was chained to the grand-source, not left unresolved at the intermediate source");
});

// Like reconstructFork, but readdir returns siblings so J's cross-file backfill runs
// (the fork source is still read by sessionId via readFile). Exercises the F<->J boundary.
async function reconstructJF(mainPath, dir, files, readdirNames) {
  const d1e = d1eWith({
    Nk: () => true,
    qAe: async () => ({ filePath: mainPath, fileSize: 1 }),
    r1e: async () => Buffer.from(toJsonl(files[mainPath]), "utf8"),
    n1e: async (buf) => buf.toString("utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)),
    zn: nodePath,
    MY: {
      readdir: async () => readdirNames.slice(),
      readFile: async (p) => { if (!files[p]) throw new Error("ENOENT " + p); return Buffer.from(toJsonl(files[p]), "utf8"); },
    },
    GY: (parsed) => parsed,
  });
  return d1e({}, { dir });
}

test("F+J joint fixed point: a fork origin revealed only by a J backfill is investigated, not a premature marker", async () => {
  // main dangles on an lpu (SIBX) carried by a sibling whose OWN origin (SIBFORK) is a
  // fork copy to an on-disk grand-source (GO). F runs BEFORE J, so a sequential F-then-J
  // never re-investigates the fork origin J reveals: SIBFORK stays unresolved and fires a
  // premature marker even though the grand-source is on disk (forbidden-middle #2). The
  // joint fixed point re-runs F after J and resolves SIBFORK to the grand-source origin.
  const files = {
    "/jf/main.jsonl": [
      { uuid: "mbnd", type: "system", subtype: "compact_boundary", parentUuid: null, logicalParentUuid: "SIBX" },
      { uuid: "mleaf", type: "user", parentUuid: "mbnd", message: { role: "user", content: "latest" } },
    ],
    "/jf/sib.jsonl": [
      { uuid: "SIBFORK", type: "user", parentUuid: null, forkedFrom: { sessionId: "grand", messageUuid: "GO" }, message: { role: "user", content: "the sibling's own origin, itself a fork" } },
      { uuid: "SIBX", type: "user", parentUuid: "SIBFORK", message: { role: "user", content: "the boundary's lpu target" } },
    ],
    "/jf/grand.jsonl": [
      { uuid: "GO", type: "user", parentUuid: null, message: { role: "user", content: "the grand-source's real first prompt" } },
      { uuid: "gpost", type: "user", parentUuid: "GO", message: { role: "user", content: "grand content" } },
    ],
  };
  const out = await reconstructJF("/jf/main.jsonl", "/jf", files, ["sib.jsonl", "grand.jsonl"]);
  const sf = out.find((r) => r.uuid === "SIBFORK");
  assert.ok(sf, "the sibling's fork origin was backfilled into the reconstruction by J");
  assert.equal(sf.__pfgkForkComplete, true, "and F, re-run in the joint loop, resolved it to the grand-source origin rather than leaving a premature marker");
});
