/*
 * pfg-core: single source of truth for the compaction/lineage primitives that
 * Patches D, J, and K depend on.
 *
 * WHY THIS FILE EXISTS: the patches were authored as hand-written splices into a
 * minified bundle, so "is this a compaction boundary", "which edge does the walk
 * follow", and "is this a conversation root" each got re-decided inline at many
 * sites, and they drifted apart: the live loader's reachability check follows
 * only parentUuid while the display walk and the loss bookend follow the logical
 * parent too, so they disagree about where a lineage ends. This module defines
 * each of those concepts ONCE.
 *
 * The codegen (util/pfg-codegen.py) inlines this into the bundle as one shared
 * block that both the loader (d1e) and the chain-builder (i1e) reference by
 * name. Until those call sites are redirected to it and the inline copies are
 * deleted, this module is the authoritative definition the copies must match,
 * not a second opinion competing with them.
 */

// The stable text a resumed/continued session opens with. A turn carrying this
// is a continuation summary, not the first prompt, even when its structure
// (null parent, no lpu, not flagged isCompactSummary) looks like one.
export const CONTINUATION_PREAMBLE =
  "This session is being continued from a previous conversation";

// The uuid prefix every Patch-K marker ghost carries (resplice banner, seam,
// bookend, bridge, ...). One constant so the many inline "pfgk-" string tests
// stop drifting between .slice(0,5), .indexOf(...)===0, and .startsWith.
export const GHOST_PREFIX = "pfgk-";

// The PFGK1 payload envelope. i1e/d1e stamp a marker as
// message.content = PFGK1_PREFIX + JSON.stringify(payload); the webview detects a
// ghost by this prefix and reads the payload (kind, bands, rows, ...) as data,
// never re-deriving from the uuid. One definition, both the exthost emit and the
// webview consume link to it.
export const PFGK1_PREFIX = "PFGK1:";

// A compaction boundary record.
export const isBoundary = (rec) =>
  !!rec && rec.type === "system" && rec.subtype === "compact_boundary";

// Is this uuid one of our planted marker ghosts? String form, for when only the
// uuid is in hand (e.g. a logicalParentUuid that points at a seam ghost).
export const isGhostUuid = (uuid) =>
  typeof uuid === "string" && uuid.startsWith(GHOST_PREFIX);

// A Patch-K marker ghost record.
export const isGhost = (rec) => !!rec && isGhostUuid(rec.uuid);

// The KIND of a marker ghost, parsed from its uuid ("pfgk-<kind>-<slice>"):
// "seam" | "seamClean" | "bridge" | "resplice" | "broken" | "bookend", else null.
// The one place that reads a ghost's kind, so a caller telling an UNVERIFIED seam
// reattachment (K2: the phantom logical parent was never found, so the chain is
// reattached to the in-file predecessor by position) from a VERIFIED bridge (K3:
// the logical parent was resolved in-file or cross-file) does not restate the
// prefix grammar. Construction is GHOST_PREFIX + kind + "-" + slice, so the first
// "-" after the prefix delimits the kind. Note "seam" is a strict prefix of
// "seamClean"; splitting on that first "-" keeps them distinct.
export const ghostKind = (rec) => {
  const uuid = rec && rec.uuid;
  if (typeof uuid !== "string" || !uuid.startsWith(GHOST_PREFIX)) return null;
  const rest = uuid.slice(GHOST_PREFIX.length);
  const dash = rest.indexOf("-");
  return dash === -1 ? rest : rest.slice(0, dash);
};

// THE lineage edge: the real parent, else the logical parent. A compaction writes
// the logicalParentUuid on its boundary record, pointing across the boundary at the
// pre-compaction tail; omitting it here is what makes a walk dead-end at every
// compaction boundary, so following it IS the point of Patches D/J/K. (Measured
// across the real corpus: a logicalParentUuid appears ONLY on compact_boundary
// records (lpu is boundary-only). A fork/resume ALSO stamps forkedFrom {sessionId,
// messageUuid} on every copied record (all types), naming its off-disk source;
// forkedFrom is NOT disjoint from the lpu (a rare boundary-headed fork carries both),
// and d1e's F step resolves it to a real parent edge at LOAD, so edge() itself needs
// no forkedFrom awareness. An earlier draft claimed forks write the lpu across files;
// the data does not bear that out.)
//
// `??`, not `||`: parentUuid / logicalParentUuid are each a uuid string or
// null/undefined. Nullish-coalescing falls through to the logical parent only for a
// genuinely absent parentUuid; a present-but-falsy value (e.g. "") does not silently
// reroute the lineage. There is no valid falsy uuid, so on real data this is
// identical to ||; it is only more defensive on malformed data, a choice made on
// purpose rather than inherited from the minified original.
export const edge = (rec) =>
  (rec && (rec.parentUuid ?? rec.logicalParentUuid)) ?? null;

// A file-local conversation turn: user or assistant, not meta or sidechain.
// `teamName` identifies the owner of a teammate transcript; it does NOT make that
// teammate's own turns foreign to the file. Anthropic's leader-view renderer uses
// `!teamName` only as a preference when a parent transcript mixes candidate tips.
// Promoting that preference into this shared predicate made team-only transcripts
// rootless and emptied their resplice. This is the BASE predicate that counting,
// banding, the resplice spine, and marker logic build on; each layers its own
// further filters (the resplice spine also excludes tool-result carriers and pfgk-
// ghosts). isMain is not itself the spine.
export const isMain = (rec) =>
  !!rec && (rec.type === "user" || rec.type === "assistant") &&
  !rec.isMeta && !rec.isSidechain;

// Plain-text content of a turn, for the one decision structure cannot make.
export const contentText = (rec) => {
  const content = rec && rec.message && rec.message.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content))
    for (const block of content)
      if (block && block.type === "text" && typeof block.text === "string")
        return block.text;
  return "";
};

// A STRUCTURAL conversation origin: a main-line turn with no parent and no
// logical parent, that is not itself a compaction artifact. Necessary but NOT
// sufficient to BE the root: BOTH a continuation preamble AND a forkedFrom copy
// satisfy every clause here yet are not the origin (their real first prompt is off
// disk). isOrigin deliberately does NOT exclude them, so the loader's fork-discovery
// still sees a fork copy as a structural origin to resolve; classifyRoot owns the
// preamble and fork verdicts. See isContinuationPreamble and classifyRoot.
export const isOrigin = (rec) =>
  isMain(rec) && !edge(rec) && !rec.isCompactSummary && !isGhost(rec);

// A structural origin whose CONTENT is a continuation summary: the deepest
// root recoverable from disk, but the first prompt predates it and is off
// disk, so this is a dead end, not the origin. Distinguished so we recover TO
// it without CLAIMING it is the origin.
export const isContinuationPreamble = (rec) =>
  isOrigin(rec) && contentText(rec).startsWith(CONTINUATION_PREAMBLE);

// The verdict on a single terminus, split along the two axes docs/invariant.md
// requires so they can never be conflated:
//   reachedRoot: the boolean VERDICT. true only for an origin.
//   reason: the DATA behind it; never softens the verdict.
//     "origin":   the first turn; the transcript may render as complete.
//     "preamble": deepest on-disk root is a continuation summary, a dead end;
//                 the origin is off disk. A FAILURE (reachedRoot=false),
//                 routed to the loud marker, never a soft note.
//     "fork":     structural origin whose ancestry is an off-disk SOURCE session
//                 (forkedFrom), not yet resolved complete. A FAILURE
//                 (reachedRoot=false), routed to the loud marker. Parallel to
//                 preamble, on the cross-session axis.
//     "none":     terminus is a boundary / summary / non-origin dead-end.
// Splitting the axes is the whole point: a renderer keying on the boolean cannot
// paint a preamble as success, because for a preamble the boolean is false.
export const classifyRoot = (rec) => {
  if (!isOrigin(rec)) return { reachedRoot: false, reason: "none" };
  if (isContinuationPreamble(rec))
    return { reachedRoot: false, reason: "preamble" };
  // A fork copy is a structural origin whose real ancestry is an off-disk source
  // session named by forkedFrom. It is a dead end until the loader confirms the
  // copy itself is that source's origin (__pfgkForkComplete). Same masquerade as
  // the preamble, on the cross-session axis; both fields sit on this one record,
  // so the verdict stays local. Keeping this OUT of isOrigin is deliberate: the
  // loader's fork-discovery loop needs isOrigin(rec) && rec.forkedFrom to still see
  // a fork copy as a structural origin, or it would never find forks to resolve.
  if (rec.forkedFrom && rec.forkedFrom.sessionId && !rec.__pfgkForkComplete)
    return { reachedRoot: false, reason: "fork" };
  return { reachedRoot: true, reason: "origin" };
};

// Build a uuid -> record index (last-wins on duplicate uuids), the shape both
// walkToRoot and the loader's cross-file backfill consume. One builder so the
// `_mapOf`-style rebuild stops being re-authored inline.
export const byUuid = (records) => {
  const index = new Map();
  for (const rec of records) if (rec && rec.uuid) index.set(rec.uuid, rec);
  return index;
};

// Walk from a uuid along `edge`, cycle-guarded, reporting where and how it ends.
// ONE predicate for every caller (the loader's backfill decision, the loader's
// edge pickers, and the chain-builder's main walk), so they can never disagree.
// This is the MECHANISM: it returns the raw visited path; the display/trust POLICIES
// (crossedLpu, rootTrusted, crossedUnprovenSeam) fold that path, rather than the walk
// baking an any-lpu / any-seam boolean into itself. Returns:
//   { terminus, reachedRoot, reason, path, steps, ref? }
//   reason: "origin" | "preamble" | "fork" | "none" | "cycle" | "dangling"
//     (origin / preamble / fork come from classifyRoot at the terminus; cycle and
//     dangling are walk outcomes.)
//   path: the records visited, in walk order (leaf -> terminus). EVERY crossing the
//     policies care about is IN this path: a logical-parent hop (crossedLpu) and any
//     K2 seam ghost (crossedUnprovenSeam / rootTrusted) are derived from it, so the
//     walk never pre-computes them. The chain-builder also consumes path as its
//     walked set instead of re-walking the lineage inline.
//   ref (on "dangling" only): the off-file uuid the chain points at, a backfill
//     candidate the loader looks for in sibling files.
//
// LOADER kind->action contract: cross-file backfill continues UNLESS
// reachedRoot. preamble / fork / none / cycle / dangling all keep the search
// going; a preamble must NOT stop it, or we refuse to recover a root
// sitting in a sibling file. Backfill ends only when genuinely exhausted (no
// sibling adds new records); if the terminus is then still not an origin, that
// is the invariant's clause-2 loud hard failure, never a quiet green stop.
export const walkToRoot = (startUuid, index) => {
  let cur = index.get(startUuid), seen = new Set(), steps = 0;
  const path = [];
  const buildResult = (reason, terminus, extra) => ({
    terminus,
    reachedRoot: reason === "origin",
    reason,
    path,
    steps,
    ...extra,
  });
  while (cur) {
    // The terminus on a cycle is the last DISTINCT node reached (path end), not the
    // already-visited node the edge loops back to: callers band post-boundary
    // reachability off this terminus, so it must be the node the walk ended on,
    // matching a plain leaf -> dead-end walk.
    if (seen.has(cur.uuid)) return buildResult("cycle", path[path.length - 1]);
    seen.add(cur.uuid);
    path.push(cur);
    steps++;
    if (isOrigin(cur)) return buildResult(classifyRoot(cur).reason, cur);
    const ref = edge(cur);
    if (!ref) return buildResult("none", cur);
    const next = index.get(ref);
    if (!next) return buildResult("dangling", cur, { ref });
    cur = next;
  }
  return buildResult("none", null);
};

// Display signal (formerly walkToRoot's crossedLpu boolean): did the walk traverse at
// least one logical-parent edge, i.e. was the lineage recovered ACROSS a compaction/fork
// boundary rather than by the plain parent chain? Re-derived from the returned path, so
// walkToRoot reports the raw crossings (mechanism) and this folds them (policy) instead
// of baking the fold into the walk. A path node hopped via its logical parent iff its
// parentUuid is null AND the walk continued past it (it is not the terminus): the terminus
// origin also has a null parent but is last, so it is correctly excluded. An empty-string
// parentUuid dead-ends at "none" and is not == null, so it never counts as a cross.
export const crossedLpu = (path) =>
  Array.isArray(path) &&
  path.some((rec, i) => rec && rec.parentUuid == null && i < path.length - 1);

// A K2 seam ghost whose same-conversation reattachment is NOT proven: either no evidence
// was stamped at plant time, or the evidence says the reattach target is off this
// conversation's lineage (a forkedFrom copy). The trust-BLOCKING case; a seam with
// proven:true is a verified bridge and does not block. Internal to the two folds below.
const seamUnproven = (rec) =>
  ghostKind(rec) === "seam" && !(rec.__pfgkSeam && rec.__pfgkSeam.proven === true);

// The TRUST verdict (policy over walkToRoot's path mechanism, formerly the crossedSeam
// boolean, now proof-promoting): the walk reached a genuine origin AND no unproven seam
// sits on the path. seamClean/bridge ghosts are already verified reattachments (not
// "seam"), so only the unverified positional reattach must clear the bar. With no evidence
// stamped, every seam is unproven, so this collapses to the pre-promotion rule reachedRoot
// && no-seam. The gradation lives in the boolean `proven` (binary AT proof), never a
// continuous score reaching the render.
export const rootTrusted = (walk) =>
  !!walk &&
  walk.reachedRoot === true &&
  Array.isArray(walk.path) &&
  !walk.path.some(seamUnproven);

// Did the walk cross an unproven seam? The display counterpart to rootTrusted: it names the
// specific failure (root reattached by unverified in-file position) for the marker's honest
// detail, distinct from a preamble / fork / plain non-origin dead end.
export const crossedUnprovenSeam = (path) =>
  Array.isArray(path) && path.some(seamUnproven);
