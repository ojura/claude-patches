# Changelog

Per-version notes for the `pfg` patchset signature embedded in
`extension.js` after `updateSessionState(V,K,B){`. Newest first.

`pfg` = Persistent Forking Glitches. The signature is the only
on-disk record of which patchset version is live in a given install
— end-users running a newer prebuilt against an older signature get
auto-restore + reapply (no `--force` needed).

> Maintainers: keep this file up to date when bumping
> `**Patchset version**` in `skill/SKILL.md`. Each version's entry
> here is **historical** and must not be auto-rewritten by the sync
> mechanism in `util/build-prebuilt.py` — that's why CHANGELOG.md is
> deliberately excluded from `SYNC_TARGETS`.

## v1.4 — 2026-05-04 — Patch K cross-conversation backfill

Patch K becomes fully topology-driven. The rendered chain spans the
entire conversation tree — every persisted message, with markers at
each compaction event.

K now has four synthesis steps:

1. **Phantom-lpu sibling backfill (NEW).** For each phantom lpu in
   this file, scan siblings for one that ALSO has it as an lpu
   (= shares the same compaction's missing predecessor = is a fork
   of the same conversation tree) AND has pre-content before its
   first phantom-lpu boundary. Prepend that sibling's pre-content.
   Recovers the canonical origin (typically a real user message at
   chain root in the eldest sibling fork) for sessions whose own
   first line is a `compact_boundary`.
2. **Seam ghosts (existing).** For phantom-lpu boundaries, plant
   `pfgk-seam-…` parented to the in-file predecessor; rewrite the
   boundary's lpu to point at the seam.
3. **Bridge ghosts (NEW).** For boundaries whose lpu was resolved
   cross-file by Patch J, plant `pfgk-bridge-…` between the in-file
   orphan chain's leaf and the boundary's first child. Walker now
   traverses the in-file orphan instead of the cross-file shortcut.
   Cross-file content stays reachable via the seam path.
4. **Bookend ghost (refined).** Two predicates: (a) original "first
   non-system msg with `parent==null && !lpu`"; (b) relaxed "first
   user/asst whose parent chain dead-ends in a phantom-lpu boundary"
   for cases where chain root is parented to a system boundary.

Result: the panel for any session in a conversation family renders
the same canonical origin at top, then the full tree in chronological
order with seam/bridge/bookend ghosts marking the structural
discontinuities. v1.3's `orphannotice` ghost is removed (subsumed by
the bridge mechanism).

## v1.3 — 2026-05-04 — Patch K orphannotice for unreachable seam

Mitigation for sessions where the seam goes unrendered. The seam is
planted on the orphan in-file chain; when a session also has a
compact_boundary whose lpu resolves cross-file via Patch J's
sibling-prepend, `Ez4`'s single-chain max-by-index walker picks the
live-chain leaf and never traverses the orphan branch — so the seam
exists in `_parsed` but never reaches the DOM.

Detection signal: seam was planted but bookend didn't fire (the
bookend fails when the chain root sits in the sibling content Patch J
prepended). Synthesize a `pfgk-orphannotice-…` ghost on the LIVE
chain, between the cross-file-resolved boundary and that boundary's
first child. The seam stays planted on the orphan chain (semantically
correct for that branch); the orphannotice provides the user-facing
signal in the live chain.

Render wrapper extended: orphannotice = amber `#ffc107`, vs orange
seam `#ff9f1c` and red bookend `#dc3545`. No click-jump (no
counterpart to scroll to).

## v1.2 — 2026-05-03 — Add Patch K (lost+found-style orphan recovery)

Read-side mitigation for sessions whose `compact_boundary`
`logicalParentUuid` points at a uuid that was never persisted to
disk. Upstream write-side bug at `compact.ts:598` (filed at #55818,
root caused at #46603). Patches D and J resolve in-file and
cross-file boundary pointers; K handles the residual case where no
JSONL anywhere contains the target uuid by synthesizing a seam ghost
(parented to the in-file predecessor) plus a chain-root bookend,
both visibly bracketing the recovered span as colored bubbles.

Two-file patch:
- `extension.js`: K block in the loader, after Patch J's fixed-point
  loop.
- `webview/index.js`: render wrap on the user-message path,
  dispatching on the `pfgk-bookend-…` / `pfgk-seam-…` uuid prefix
  (out-of-band signaling via uuid, not via message content).

Render wrap injects a one-off `<style>` that suppresses the
truncation gradient + collapse buttons + edit/fork action — none make
sense on a synthetic message — and centers a 42px ⚠️ banner above
the bubble. Click anywhere on the colored container scrolls to the
matching counterpart via `[data-pfgk-role="…"]` queries.

Critical implementation note: ghosts must NOT set `isMeta: true` —
the upstream `Sz4` filter strips those. Compact summaries get away
with being synthetic-ish via `isCompactSummary` (which `Sz4` doesn't
check); we don't have that backdoor, so the synthetic flag stays off.

## v1.1 — 2026-05-03 — Cover H/I/J in prebuilt + stale-sig migration

Patches H, I, J had landed without bumping the signature, so v1
prebuilt users never re-applied and were silently missing the
cap-bypass / precompact-skip / cross-file lpu fixes. v1.1 introduces
the version-aware migration path: prebuilt detects any prior `pfg-vN`
signature (regex widened to support dotted minors), restores from
`.bak`, and re-applies — no `--force` needed for end users on the
upgrade path.

- `util/build-prebuilt.py`: template now does stale-sig detection +
  version-aware restore message.
- `prebuilt/archive/v1/{2.1.121,2.1.122,2.1.123,2.1.126}/apply.py`:
  prior v1 prebuilts archived to keep `prebuilt/` tidy without
  losing history (URL stability via the archive path).

## v1 — 2026-04-28 → 2026-05-03 — Initial patchset

Patches A through J. Initial signature scheme with single-version
tag (`/*pfg-v1*/`, no minor).

- **A** — fork session writes a `custom-title` rescue entry when the
  fork JSONL's head 64KB would otherwise be unparseable for metadata.
- **B** — drop `position: sticky` on tall message headers (linear
  scroll instead of occluding the assistant reply).
- **C** — disable the broken `isSlashCommand` heuristic
  (`text.startsWith("/")` false-positives on pasted Unix paths).
- **D** — chain walker bridges compaction boundaries via
  `logicalParentUuid` (read-side fix; the API path is bounded
  independently by `getMessagesAfterCompactBoundary`).
- **E** — sidebar pencil rename + session-title resolver order fixes.
- **F** — sticky title cache invalidation across session switch.
- **G** — sessionPanels destructure / claude --resume subprocess
  wiring for fork/rewind UI.
- **H** — bypass `CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP` env gate (the
  precompact-skip behavior is the desired default).
- **I** — remove the render cap that hides messages past a fixed
  count.
- **J** — cross-file `logicalParentUuid` resolution (sibling-prepend
  before the chain walker runs).

Patches F and G use a regex-anchored `apply-patch-fg.py` script
(version-tolerant across minified renamings); the rest are literal
splices captured into the per-version prebuilt.
