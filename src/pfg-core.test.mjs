// Executable contract for the Patch J+K base invariant (see docs/invariant.md).
//
// The guard: a preamble is NOT the root, so classifyRoot must return
// reachedRoot=false for it (a specific failure reason, never a soft peer
// of "origin"), and the crossedLpu(path) policy must report the recovery so the display tells a
// plain root from one recovered across a fork. A content-blind origin check or a
// parentUuid-only reachability walk (the two live bugs this whole change fixes)
// cannot make these assertions pass. The contract bites in code, not just prose.
//
//   run:  node --test src/pfg-core.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  isOrigin,
  isMain,
  isBoundary,
  isGhost,
  isGhostUuid,
  ghostKind,
  GHOST_PREFIX,
  byUuid,
  isContinuationPreamble,
  classifyRoot,
  walkToRoot,
  crossedLpu,
  crossedUnprovenSeam,
  rootTrusted,
} from "./pfg-core.js";

// A first prompt: structural origin, real content.
const genuineOrigin = {
  uuid: "root",
  type: "user",
  parentUuid: null,
  message: { content: "Fix the login bug in the auth handler" },
};

// A continuation preamble: STRUCTURALLY identical to an origin (null parent, no
// lpu, not a compact summary), but its content is a resume summary, so the true
// first prompt predates it and is off disk. This is the masquerade the whole
// invariant exists to catch: it passes every structural test for an origin.
const preamble = {
  uuid: "pre",
  type: "user",
  parentUuid: null,
  message: {
    content:
      "This session is being continued from a previous conversation that ran " +
      "out of context. Summary: the user asked to ...",
  },
};

// A compaction boundary: never an origin.
const boundary = {
  uuid: "bnd",
  type: "system",
  subtype: "compact_boundary",
  parentUuid: null,
  logicalParentUuid: "root",
};

test("primitives: a preamble is a STRUCTURAL origin but a detectable continuation summary", () => {
  // Structure alone cannot separate them: that is precisely the trap.
  assert.equal(isOrigin(genuineOrigin), true);
  assert.equal(isOrigin(preamble), true);
  // Content is the one decision structure can't make; the primitive must make it.
  assert.equal(isContinuationPreamble(genuineOrigin), false);
  assert.equal(isContinuationPreamble(preamble), true);
  assert.equal(isBoundary(boundary), true);
});

test("primitives: team ownership does not disqualify a file-local turn or origin", () => {
  const teamOrigin = {
    ...genuineOrigin,
    uuid: "team-root",
    teamName: "session-deadbeef",
    agentName: "researcher",
    sessionId: "team-session",
  };
  assert.equal(isMain(teamOrigin), true, "teamName identifies this transcript's owner, not a foreign turn");
  assert.equal(isOrigin(teamOrigin), true, "a teammate's first prompt is the teammate transcript's origin");
  assert.equal(isMain({ ...teamOrigin, isMeta: true }), false, "meta records remain excluded");
  assert.equal(isMain({ ...teamOrigin, isSidechain: true }), false, "sidechain records remain excluded");
});

test("primitives: ghost detection by record and by bare uuid", () => {
  assert.equal(GHOST_PREFIX, "pfgk-");
  assert.equal(isGhostUuid("pfgk-seam-abc"), true);
  assert.equal(isGhostUuid("0223d19d-not-a-ghost"), false);
  assert.equal(isGhost({ uuid: "pfgk-broken-x" }), true);
  assert.equal(isGhost(genuineOrigin), false);
});

test("primitives: byUuid indexes records last-wins and skips the uuid-less", () => {
  const index = byUuid([genuineOrigin, { uuid: "x", v: 1 }, { uuid: "x", v: 2 }, { noUuid: true }]);
  assert.equal(index.get("root"), genuineOrigin);
  assert.equal(index.get("x").v, 2, "duplicate uuid: last record wins");
  assert.equal(index.size, 2, "the record with no uuid is skipped");
});

test("INVARIANT: only an origin reaches the root (verdict is a boolean, not a reason)", () => {
  const g = classifyRoot(genuineOrigin);
  const p = classifyRoot(preamble);
  const b = classifyRoot(boundary);

  assert.equal(g.reachedRoot, true, "an origin IS the root");
  assert.equal(g.reason, "origin");

  // The point of this test: a preamble is a FAILURE with a specific reason,
  // never a soft third state that a renderer can treat as neither.
  assert.equal(
    p.reachedRoot,
    false,
    "a continuation preamble does NOT reach the root: hard failure, not success",
  );
  assert.equal(p.reason, "preamble");

  assert.equal(b.reachedRoot, false, "a compaction boundary is not an origin");
});

test("forbidden middle: preamble cannot be a soft peer of origin", () => {
  const p = classifyRoot(preamble);
  assert.equal(typeof p, "object", "classifyRoot returns a verdict object, not a bare string");
  assert.notEqual(p.reachedRoot, true);
});

// A fork copy is structurally an origin (null parent, no lpu, real content), but its
// ancestry lives in an off-disk SOURCE session named by forkedFrom. Until the loader
// resolves that source (setting __pfgkForkComplete), the copy is a dead end, not the
// origin: the same masquerade as the preamble, on a different axis. Both fields the
// verdict keys on sit on this one record, so classifyRoot can make the call.
test("INVARIANT: an unresolved fork copy is reason 'fork', a resolved one is 'origin'", () => {
  const forkCopy = {
    uuid: "forkroot",
    type: "user",
    parentUuid: null,
    forkedFrom: { sessionId: "src-session", messageUuid: "forkroot" },
    message: { content: "the branch-point turn" },
  };
  assert.equal(isOrigin(forkCopy), true, "a fork copy passes every STRUCTURAL origin test");

  const unresolved = classifyRoot(forkCopy);
  assert.equal(
    unresolved.reachedRoot,
    false,
    "an unresolved fork does NOT reach the root: its origin is off disk",
  );
  assert.equal(unresolved.reason, "fork");

  // Once the loader confirms the copy itself IS the source's origin, it passes.
  const complete = classifyRoot({ ...forkCopy, __pfgkForkComplete: true });
  assert.equal(complete.reachedRoot, true, "a complete fork's copy IS the origin");
  assert.equal(complete.reason, "origin");
});

test("walkToRoot carries the lpu-crossed signal its own caller needs", () => {
  const viaLpu = byUuid([
    genuineOrigin,
    {
      uuid: "child",
      type: "assistant",
      parentUuid: null,
      logicalParentUuid: "root",
      message: { content: "the reply" },
    },
  ]);
  const r1 = walkToRoot("child", viaLpu);
  assert.equal(r1.terminus.uuid, "root");
  assert.equal(r1.reachedRoot, true);
  assert.equal(crossedLpu(r1.path), true, "followed a logicalParent edge, must be flagged as recovered");

  const viaParent = byUuid([
    genuineOrigin,
    {
      uuid: "kid",
      type: "assistant",
      parentUuid: "root",
      message: { content: "the reply" },
    },
  ]);
  const r2 = walkToRoot("kid", viaParent);
  assert.equal(r2.terminus.uuid, "root");
  assert.equal(crossedLpu(r2.path), false, "pure parentUuid chain, not a recovery");
});

test("walkToRoot end to end: a preamble terminus is reached but is NOT the root", () => {
  // The shape of the whole fix: the walk reaches the deepest on-disk root, it is
  // a preamble, so reachedRoot is false and the caller must fire the
  // loud marker, not a green bookend.
  const index = byUuid([
    preamble,
    {
      uuid: "leaf",
      type: "assistant",
      parentUuid: null,
      logicalParentUuid: "pre",
      message: { content: "a later turn" },
    },
  ]);
  const r = walkToRoot("leaf", index);
  assert.equal(r.terminus.uuid, "pre");
  assert.equal(r.reason, "preamble");
  assert.equal(r.reachedRoot, false, "deepest on-disk root is a preamble: hard failure");
  assert.equal(crossedLpu(r.path), true);
});

test("walkToRoot: a K2 seam on the path is an unproven crossing and is in the visited path", () => {
  // leaf -> pfgk-seam-x -> root. The walk reaches a structural origin, but it crossed an
  // unverified positional reattachment (a "seam" ghost with no proof stamped), so
  // crossedUnprovenSeam(path) is true. reachedRoot stays structural; the trust policy
  // rootTrusted folds no-unproven-seam into the binary verdict.
  const index = byUuid([
    genuineOrigin,
    { uuid: "pfgk-seam-x", type: "user", parentUuid: "root", message: { content: "reattached by position" } },
    { uuid: "leaf", type: "user", parentUuid: "pfgk-seam-x", message: { content: "latest" } },
  ]);
  const r = walkToRoot("leaf", index);
  assert.equal(r.terminus.uuid, "root");
  assert.equal(r.reachedRoot, true, "structurally reached the origin");
  assert.equal(crossedUnprovenSeam(r.path), true, "but crossed an unproven seam: root unverified");
  assert.deepEqual(
    r.path.map((m) => m.uuid),
    ["leaf", "pfgk-seam-x", "root"],
    "path is leaf -> terminus in walk order",
  );
});

test("walkToRoot: a cycle is reason 'cycle', never a false root", () => {
  // a <-> b via parentUuid. The walk must detect the loop, not spin or mislabel a
  // cycle node as an origin. (Coverage gap flagged by the primitives seat.)
  const cyc = byUuid([
    { uuid: "a", type: "user", parentUuid: "b", message: { content: "x" } },
    { uuid: "b", type: "assistant", parentUuid: "a", message: { content: "y" } },
  ]);
  const r = walkToRoot("a", cyc);
  assert.equal(r.reason, "cycle");
  assert.equal(r.reachedRoot, false, "a cycle never reaches the root");
  assert.equal(
    r.terminus.uuid,
    "b",
    "cycle terminus is the last DISTINCT node reached (path end), not the looped-to node",
  );
});

test("walkToRoot: an edge into a missing record is reason 'dangling' with the off-file ref", () => {
  const idx = byUuid([{ uuid: "leaf", type: "user", parentUuid: "GONE", message: { content: "x" } }]);
  const r = walkToRoot("leaf", idx);
  assert.equal(r.reason, "dangling");
  assert.equal(r.ref, "GONE", "the off-file uuid is reported as a backfill candidate");
  assert.equal(r.terminus.uuid, "leaf", "terminus is the last present record");
  assert.equal(r.reachedRoot, false);
});

test("walkToRoot: a corrupt boundary with a resolvable parentUuid recovers via it, not the off-disk lpu", () => {
  // The mirror shape: the ONLY non-null-parent boundaries the corpus produces
  // (8ea367b6 / 0087065e / 41bbb60d) have a parentUuid that RESOLVES to a real in-file
  // turn and an off-disk lpu. edge() = parentUuid ?? lpu picks the resolvable parent, so
  // the off-disk lpu is never followed and the chain recovers. REGRESSION GUARD: a
  // boundary-aware "prefer lpu" edge would send these to a dangling lpu and fire a
  // spurious marker on sessions that recover today.
  const index = byUuid([
    { uuid: "root", type: "user", parentUuid: null, message: { content: "the real first prompt" } },
    { uuid: "bnd", type: "system", subtype: "compact_boundary", parentUuid: "root", logicalParentUuid: "OFFDISK" },
    { uuid: "leaf", type: "user", parentUuid: "bnd", message: { content: "latest" } },
  ]);
  const r = walkToRoot("leaf", index);
  assert.equal(r.terminus.uuid, "root", "followed the resolvable parentUuid to the in-file origin");
  assert.equal(r.reachedRoot, true);
  assert.equal(r.reason, "origin");
});

test("walkToRoot: a corrupt boundary whose parentUuid dangles fires the marker, does not silently follow lpu", () => {
  // The hypothetical gap-#4 shape (0 corpus instances): parentUuid points at a missing
  // record, lpu is present. edge() picks the (dangling) parentUuid, so the walk dangles
  // and reports the bad ref as the backfill candidate: the honest outcome, since a
  // boundary whose primary edge dangles is a corruption signal. We deliberately do NOT
  // add boundary-scoped retry-on-dangle for a shape the corpus never produces.
  const index = byUuid([
    { uuid: "realO", type: "user", parentUuid: null, message: { content: "an in-file origin the lpu points at" } },
    { uuid: "bnd", type: "system", subtype: "compact_boundary", parentUuid: "BOGUS", logicalParentUuid: "realO" },
    { uuid: "leaf", type: "user", parentUuid: "bnd", message: { content: "latest" } },
  ]);
  const r = walkToRoot("leaf", index);
  assert.equal(r.reason, "dangling");
  assert.equal(r.ref, "BOGUS", "reports the dangling parentUuid as the backfill candidate");
  assert.equal(r.reachedRoot, false);
  assert.equal(crossedLpu(r.path), false, "did not follow the lpu: parentUuid was non-null, just dangling");
});

test("walkToRoot: an empty-string logicalParentUuid does not set a false crossedLpu", () => {
  // A non-origin record (compaction summary) with parentUuid null and lpu "". edge()
  // nullish-coalesces to "", so the walk dead-ends at reason "none"; it must NOT be
  // miscounted as a crossed logical-parent edge (regression: crossedLpu was set
  // before the !ref guard, so an empty lpu tripped it).
  const idx = byUuid([
    { uuid: "leaf", type: "user", parentUuid: null, logicalParentUuid: "", isCompactSummary: true, message: { content: "x" } },
  ]);
  const r = walkToRoot("leaf", idx);
  assert.equal(r.reason, "none", "empty lpu dead-ends at none, not a phantom edge");
  assert.equal(crossedLpu(r.path), false, "an empty lpu is not a real cross-boundary hop");
});

test("walkToRoot: an absent start uuid gives a non-root 'none', never a crash", () => {
  const r = walkToRoot("nope", byUuid([genuineOrigin]));
  assert.equal(r.reachedRoot, false);
  assert.equal(r.terminus, null);
});

test("ghostKind: parses the kind from a ghost uuid, distinguishing seam from seamClean", () => {
  // BREACH 2's fix keys on this distinction: an unverified seam vs a verified bridge.
  assert.equal(ghostKind({ uuid: "pfgk-seam-abc123" }), "seam");
  assert.equal(ghostKind({ uuid: "pfgk-seamClean-abc123" }), "seamClean");
  assert.equal(ghostKind({ uuid: "pfgk-bridge-abc123" }), "bridge");
  assert.equal(ghostKind({ uuid: "pfgk-resplice-0223d19d-uuid-tail" }), "resplice");
  assert.equal(ghostKind({ uuid: "0223d19d-not-a-ghost" }), null);
  assert.equal(ghostKind({ uuid: null }), null);
});
