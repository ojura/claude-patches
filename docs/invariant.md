# Patch J+K base invariant

This is the single written contract for what the J+K patch complex (cross-file
lineage recovery plus the read-side markers) must guarantee. It is defined once,
here; `patches.md`, `SKILL.md`, and any test reference this file rather than
restating it. It existed only as oral tradition of a review thread before this,
which is exactly why it was compromised repeatedly. An invariant not in an
artifact cannot be tested, reviewed, or defended.

## Origin, root, dead-end: the vocabulary

**Origin == root.** It is the first turn of the conversation, and it is binary: a
record either is the origin or it is not. There is no "authentic" or "true"
origin; that qualifier would fake a spectrum where there is a bright line (the
same category error as "most optimal"). Code carries the verdict as
`reachedRoot: boolean`.

A record is the origin only if it is a canonical (`isMain`) turn with **no
canonical parent reachable by any edge**: not by `parentUuid`, not by the
compaction `logicalParentUuid`, and not by `forkedFrom` (which points at a parent
in another session). `isMain` means a user/assistant turn that is neither meta
nor sidechain. `teamName` is not an exclusion: in a teammate-owned JSONL it is
the local conversation identity carried by every genuine turn. Being
structurally parentless is necessary but not sufficient; a record can be
null-parent and lpu-less and still have a parent that one of those other edges
names.

Everything the walk can stop at that is **not** the origin is a **dead-end**, a
different category, never a lesser origin. The code's `reason` field names them:
`origin` is the one success; `preamble`, `fork`, `dangling`, `cycle`, `none` are
dead-ends. (A K2 seam is not a `reason` but a separate path flag, `crossedSeam`,
that poisons an otherwise-structural origin; see the axis note below.) A
continuation preamble in particular is a dead-end (its content is a
resume summary, so the first prompt predates it and is off disk), not an
"inauthentic origin."

## The invariant

Every session load that J+K touches terminates in **exactly one** of two states,
never a third:

1. **Root reached.** The rendered transcript's lineage reaches the origin, having
   **first genuinely exhausted recovery** across every edge: every
   `logicalParentUuid` hop, every `forkedFrom` cross-session hop, every
   sibling/ancestor `.jsonl` on disk, and the preserved-message set. Only then may
   the transcript render as complete, with no failure marker.

2. **Loud hard failure.** If, after recovery is genuinely exhausted, the origin is
   not reachable, the load renders the **single canonical** hard-failure marker
   (`⛔ TRANSCRIPT INCOMPLETE · Conversation root not found`), as final as the UI
   allows. Under-alarming is the injury: the real harm is a user who does not
   notice the beginning is missing and acts on a truncated transcript.

   The verdict is loud **and** honest about its own boundary. "Not found" is a
   scoped, falsifiable negative ("we searched X and did not find the root"), not
   the unproven universal "unrecoverable". The backfill scans only the session's
   own project directory; it does not look in other project directories, backups,
   or `~/claude-archive`, where the fork-archiving skill deliberately moves
   ancestor sessions, i.e. the single most likely home of a genuinely missing root
   or an off-disk `forkedFrom` source. "Unrecoverable" would assert the recovery
   space is empty without having searched it: a false closure in the
   confident-negative direction. So the badge stays maximally loud and final, and
   the open door lives in the body: "not found in the saved files searched; it may
   still exist in an archived or earlier session." Loud verdict, honest boundary,
   no overclaim.

## Detecting a dead-end that is shaped like an origin

Two kinds of record are structurally parentless (null `parentUuid`, no lpu, not
flagged a summary) yet are **not** the origin. Each needs its own detector, and
they differ in kind.

**The continuation preamble** is caught only by **in-band detection**: reading the
message text and matching the resume-summary opener. This is a **forced necessary
evil**, not a feature. The vendor writes no out-of-band flag distinguishing a
resume summary from a real first prompt, so the content read is the only signal
there is. It is fragile (a vendor reword silently breaks it and forbidden-middle
#1 returns) and therefore **guarded**: the apply step asserts the vendor still
emits the exact string and aborts loud if it drifts, and the string lives once as
a single constant. Name it the liability it is, never dress it up as
"content-aware."

**The fork point** is caught **out-of-band**, cleanly. `forkedFrom {sessionId,
messageUuid}` is an explicit field that says outright "this was copied from
another session; the real parent lives there." No sniffing, no fragility.
`forkedFrom` is a cross-session **edge to walk**, structurally the same as a
dangling lpu (follow the pointer, resolve it if the target is on disk, fail loud
if it is not), but more precise: it names the exact session and message, so
resolution is a direct lookup, not a sibling search. Walking it has three
outcomes:

1. source on disk, and something precedes the fork point in it, a canonical turn,
   OR a compaction boundary (not a canonical turn, but it carries off-disk
   ancestry through its lpu), OR a further fork: the record is a dead-end, the real
   origin is upstream, recover it or the marker.
2. source on disk, and the fork point is itself the origin of the source (the
   recursive origin test one session up: nothing precedes it there by ANY edge):
   the record is the origin. "No canonical turn before the fork point" is NOT the
   test; a source that begins with a compaction boundary is not empty, its true
   origin is off disk through that boundary's lpu.
3. source off disk: undecidable, so the loud marker; never assume the fork
   contains the origin.

The definition is **probability-blind**: all three branches must be implemented
correctly. Outcome 2 is rare (you almost always fork from a conversation already
underway), but "rare" is no license to drop a branch, that would be false
closure. The prose may note the base rate; the coverage may not.

An unresolved fork point carries `reason: fork`, a dead-end alongside `preamble`.
Like the preamble's, that verdict lives in the shared classifier so no caller
re-derives it, and `isOrigin` stays purely structural: the loader's fork
discovery keys on `isOrigin`, so excluding fork copies from it would break the
very step that resolves them.

An in-file LPU target is not automatically a valid ancestor. If the target's
walk returns to the boundary that names it, the lineage is cyclic even though
no UUID is missing. The only automatic local repair currently permitted is the
fully described preserved-tail case: the complete preserved UUID order is on
the boundary, the LPU is that list's tail, the walk returns to that boundary,
and an earlier non-preserved record exists. Rebuilding that recorded order is a
mechanical correction; connecting its head to the preceding record is trusted
only when that predecessor reaches the file's one fork-free origin. Otherwise
the repair carries an unproven seam and cannot satisfy `rootTrusted`.

## Forbidden middles (each is a bug, by name)

- **False success on a non-root.** A green / "origin reconstructed" bookend, or
  simply the absence of a failure marker, on a terminus that is not the origin: a
  continuation preamble, a fork point, a compaction boundary, a summary. This is
  the worst failure mode because it is under-alarming, and it is the real false
  closure in this system: it closes the question quietly and dodges scrutiny. Two
  blind spots exhibit it. A **content-blind** check renders a preamble as success,
  because a preamble passes every structural test for an origin (null parent, no
  lpu, not a summary). A **forkedFrom-blind** check renders a fork point as
  success for the same structural reason.

- **Premature failure marker.** Firing the loud marker before recovery is
  genuinely exhausted: giving up at a dead-end that one more `logicalParentUuid`
  hop, one more `forkedFrom` hop, or one more sibling file would have resolved.
  This is false closure on the recovery space. It is the only valid kernel of "a
  truthful failure marker is not good enough": the objection was never to the
  marker, it was to declaring it too early. It is also why the verdict is "not
  found in what we searched" and never the unproven universal "unrecoverable".

- **A soft note for a hard failure.** Routing a non-origin terminus to a quiet
  "note the off-disk residual, do not claim completeness" instead of the loud
  marker. A preamble is a failure and renders as the loud marker, not an aside;
  so is an off-disk `forkedFrom` source. A fork-provenance note is legitimate only
  when the pre-fork ancestry is actually present in the file; when it is off disk,
  the note is this forbidden middle.

## The load-bearing consequences

- Reaching the **deepest on-disk root-shaped record** is **not** reaching the
  origin when that record is a continuation preamble or an unresolved fork point.
  The origin is off disk; that is a clause-2 hard failure, declared loudly. "We
  recovered N ancestor sessions" is context inside the failure marker, never a
  substitute for it.

- The success/failure axis and the reason axis are **separate**. Code carries
  `reachedRoot: boolean` (true only for the origin) alongside `reason`
  (`origin | preamble | fork | none | cycle | dangling`), so it is structurally
  impossible to render a non-origin as anything but failure. `reason` is data; the
  boolean is the verdict. Two **path flags** ride alongside, set by traversal
  rather than by the terminus: `crossedLpu` (the walk recovered across a
  compaction/fork boundary, so the bookend reads "reconstructed") and `crossedSeam`
  (the walk passed through an unverified K2 positional reattachment, so any origin
  it reached is unproven, and the caller ANDs `!crossedSeam` into its trust
  verdict). A seam is a path property, never a `reason` value: a record can be a
  structural `origin` and still be untrusted because the walk to it crossed a seam.

- **Recovery follows every edge and trusts none blindly.** Exhaustion is over
  `parentUuid`, `logicalParentUuid`, and `forkedFrom` together. And a corrupt edge
  must not be trusted over the real one: a compaction boundary is always written
  `parentUuid: null`, so a non-null `parentUuid` on a boundary is itself the
  corruption signal, not a parent to follow. Where an out-of-band attestation
  exists (`forkedFrom` names the true source), it is preferred over an in-band
  proxy (a volume heuristic that picks the sibling contributing the most records).
  These are described here but not yet implemented (`edge` still returns
  `parentUuid ?? lpu`, the gates still key on `!parentUuid`), and the corrupt-parent
  case is a LATENT FALSE SUCCESS, not a safe under-recovery: `edge` follows the
  corrupt parent, and if that parent reaches a spurious in-file origin the walk
  renders it green with no marker. That the one known corrupt boundary "fails
  loud" is that corpus cycling on its bad parent, not a structural guarantee, so
  the boundary-aware `edge` is false-success hardening, not a nice-to-have. The
  volume pick and last-wins dedup, by contrast, do not false-succeed on genuine
  data (a forked lineage shares its origin, over-recovery is safe); there
  "genuinely exhausted" is simply not honestly met while recovery guesses the
  source by volume instead of following the attestation.

- One canonical string **per fact**, not one total. The root-outcome verdict
  (`⛔ TRANSCRIPT INCOMPLETE · Conversation root not found`) and the render-mode
  banner (`⚠️ CHAIN CORRUPT · RESPLICED`, meaning "the chain was broken, here is
  every message in write order") are **orthogonal** facts: both can be true and
  they stack. They also sit on **two deliberate severity tiers**: the root verdict
  is the red hard failure (⛔, the origin is genuinely gone and unrecoverable); the
  resplice banner is the amber irregularity (⚠️, a lesser severity, because every
  message is still present, only its lineage is unreliable and its order is
  write-order). Each is defined once and always renders at its own tier: the string
  is never weakened to a softer wording, the red never drops to amber, and the
  amber never drops to a plain note or a green card. The two-tier split is
  deliberate (distinct colors let a stacked pair read at once); the forbidden move
  is the silent under-alarm, a real verdict rendered as success or as an unstyled
  aside. They stay two strings because they are two claims, not one.

## Determinism

The recovered transcript is a pure function of the input corpus bytes. The same
set of `.jsonl` files, byte for byte, must produce the same reconstruction on any
machine, in any order. Concretely:

- The ancestor pick (which sibling backfills a dangling boundary) must never
  depend on `readdir` order (filesystem-dependent) or file mtime (not an input at
  all). Sibling iteration is sorted by filename, so ties resolve identically
  everywhere. Resolving a `forkedFrom` edge is a direct session+message lookup,
  order-independent by construction.
- The render spine is ordered by first-occurrence write index, itself derived from
  the deterministic backfill order, never by wall-clock timestamp. Sorting a spine
  by timestamp is strictly less deterministic (equal or skewed clocks across
  resumed sessions) and is not allowed.
- Wall-clock (`Date.now`) may feed only display-only timing telemetry, never the
  set or the order of recovered records.

A reconstruction that varies with mtime, readdir order, or wall-clock is a bug of
the same class as a false root: it makes the transcript unreproducible.

## Test obligation

At least one executable test feeds a synthetic continuation-preamble transcript
(structurally a clean origin: null parent, no lpu, `isCompactSummary` unset,
content beginning with the continuation preamble) and asserts the loud
hard-failure verdict fires and no success bookend renders. A content-blind origin
check or a parentUuid-only reachability walk must fail this test.

At least one test covers each `forkedFrom` outcome: a fork point whose source is
off disk fires the loud marker, not a provenance note; a fork point whose source
is on disk with canonical content before the fork point resolves to a dead-end and
recovers or fails loud upstream; and the complete fork (source on disk, and
the source's fork point is itself an origin, nothing preceding it there by any
edge) renders the fork point as the origin. In particular a fork whose source
BEGINS with a compaction boundary must fire the marker, not be flagged complete:
the boundary is non-`isMain` but carries off-disk ancestry, so an
`isMain`-position "nothing canonical before" check wrongly passes it (the fork
analogue of the preamble trap). A forkedFrom-blind origin check must fail the
first of these.
