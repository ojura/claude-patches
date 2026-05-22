# Changelog

Per-version notes for the `pfg` patchset signature embedded in
`extension.js` after `updateSessionState(V,K,B){`. Newest first.

`pfg` = Persistent Forking Glitches. The signature is the only
on-disk record of which patchset version is live in a given install.
End-users running a newer prebuilt against an older signature get
auto-restore + reapply (no `--force` needed).

> Maintainers: keep this file up to date when bumping
> `**Patchset version**` in `skill/SKILL.md`. Each version's entry
> here is **historical** and must not be auto-rewritten by the sync
> mechanism in `util/build-prebuilt.py` (that's why CHANGELOG.md is
> deliberately excluded from `SYNC_TARGETS`).

## v1.7 (2026-05-17): Patch L, force `--thinking-display summarized` on IDE-spawned CLI

Restores thinking summaries in the VS Code / Antigravity chat panel
for `claude-opus-4-7[1m]` (and presumably any 4.7+ model). Closes
the same gap that #49902 / #49322 / #49268 / #8477 / #56984 and
several more upstream tickets describe.

Upstream-fix proposal filed at [anthropics/claude-code#59844](https://github.com/anthropics/claude-code/issues/59844)
with two options: a one-line CLI fix (drop the
`!getIsNonInteractiveSession()` gate from the CLI's
`K3.display = "summarized"` assignment) or this same one-line
extension fix as the fallback. When/if upstream lands either, Patch
L can be retired.

### Root cause

For Opus 4.7, Anthropic flipped the API default for
`thinking.display` from `"summarized"` to `"omitted"` (documented in
their migration guide). With `display: "omitted"`, the API returns
thinking content blocks with an empty `thinking` field and a
multi-KB `signature` only. The webview renders these as the static
`<div class="thinkingStatic">Thinking</div>` stub because its
`thinking.length > 0` branch can't fire.

The bundled CLI HAS a gate that sets `display = "summarized"` when
`settings.json` has `showThinkingSummaries: true`, but the gate is
`!getIsNonInteractiveSession() && showThinkingSummaries === true`.
The IDE spawns the CLI subprocess with `--print --input-format
stream-json --output-format stream-json`, which makes the session
non-interactive, so the gate never fires for IDE chat panels. The
user's setting is silently ignored where it matters most.

The CLI also accepts an explicit `--thinking-display <mode>` flag
that bypasses the non-interactive gate. The IDE's SDK-side spawn
code already knows how to pass it, but only when
`thinkingConfig.display` is set on the spawn-time options. The
chat-panel caller never sets it, so the flag is never pushed to
argv.

### Patch L (one-line splice in `extension.js`)

```
- if(U.type!=="disabled"&&U.display)i.push("--thinking-display",U.display)
+ if(U.type!=="disabled")i.push("--thinking-display",U.display||"summarized")
```

Drops the `&&U.display` guard (which was suppressing the flag) and
adds `||"summarized"` as the fallback value. Every IDE-spawned CLI
subprocess now receives `--thinking-display summarized` in argv, the
CLI's first display-gate branch fires regardless of interactive
state, the API request body includes `thinking.display:
"summarized"`, and the API returns thinking with content.

End-to-end DOM-verified pre-publish: post-patch chat-panel turns
produce thinking blocks with `text_len > 0` on disk (sampled at
209, 511, 612, 2117, 115 chars in the verification session). The
expandable `<details>` render path is back. Old (pre-patch) blocks
on disk stay empty because their data was never persisted.

### WebSearch / WebFetch safety

#56984 reports that the `CLAUDE_CODE_EXTRA_BODY` workaround for
this same bug breaks WebSearch (`400 Thinking may not be enabled
when tool_choice forces tool use`) and WebFetch (`400 adaptive
thinking is not supported on this model`). Those failures are about
`thinking.type` being forced into requests where the CLI's per-call
logic would have left it disabled. Patch L is structurally distinct:
it sets `display` only, not `type`. The CLI's per-request gate is
`q.type !== "disabled"`, so when WebSearch / WebFetch / auto-mode
classifier paths build their own per-request thinking config with
`type` disabled (or with a thinking-incompatible model), the entire
`thinking` field is dropped regardless of `q.display`.

Verified empirically pre-publish: `claude --print --thinking-display
summarized --allowed-tools WebSearch "search ..."` produced 5+ API
requests, all 200 OK, no 400 errors. The forced-tool-use path is
unaffected.

### Splice count and signature

20 total splices now (was 19): 15 in `extension.js` (was 14, +1 for
L), 4 in `webview/index.js`, 1 in `webview/index.css`. Signature
bumped from `/*pfg-v1.6*/` to `/*pfg-v1.7*/`. End-users on v1.6 will
get auto-restore + reapply on the next `apply.py` run.

## v1.6 (2026-05-07): Patch K silent-failure fix + marker enrichment

Fixes a silent-data-loss bug introduced (or unmasked) by v1.5, plus
substantially enriches marker content with concrete diagnostic data.
Behavioral compatibility: all v1.5 cases still render identically;
v1.6 only adds a fallback marker for the bug case + extra text in
existing markers.

### Bug: K silently rendered ZERO markers when chain begins with phantom-lpu boundary

Symptom (DOM-observed on a real session): chat panel reconstructs
the chain via Patch J's cross-file prepend, the prepended sibling
itself begins with a phantom-lpu compaction boundary (i.e., the
boundary is at index 0 of `_parsed` after J prepend), but K renders
ZERO markers (no bookend, no seam, no broken). The user sees a
chain that's missing upstream lineage with no visual signal that
data is missing.

Root cause: the post-K bookend / broken / bridge planting block was
gated on `if (_kFired)`, where `_kFired` becomes true only when K2
*successfully plants* a seam. K2 needs an in-file predecessor in
`_parsed` to anchor the seam to (so the walker following the
rewritten lpu has somewhere to go). For a boundary at index 0 of
`_parsed`, no predecessor exists → K2 `continue`s → `_kFired`
stays false → all fallback markers (including the broken-marker
predicate (b) explicitly designed for this case) are skipped.

Fix: introduce a separate `_kAttempted` flag set whenever K2
*detects* a phantom-lpu boundary it would want to fix (regardless
of whether the seam plant succeeded). Gate the fallback block on
`_kAttempted` instead of `_kFired`. The broken-marker predicate (b)
now fires correctly when reconstruction failed.

Generalized as a playbook design rule:
[`docs/debugging.md`](docs/debugging.md) "K detected vs K
succeeded: gate downstream logic on attempt, not effect".

### Marker informativeness: surface concrete data instead of generic prose

K v1.5's marker text was prose-heavy with truncated 8-12 character
uuid prefixes, sufficient to identify a marker AS a marker but
not actionable for cross-referencing or bug reports. v1.6 adds
per-marker concrete data:

- **Bookend (a)**: now lists each K1 backfill source per phantom-lpu:
  `phantom <full-uuid>: backfilled <N> msgs from <sibling-filename>
  (chosen from <K> candidates)` if ambiguous. Plus the wall-clock
  breakdown (see below).
- **Seam**: shows `missing phantom uuid: <full-uuid>` and
  `reattached to in-file predecessor: <full-uuid>`.
- **Bridge**: shows `boundary uuid: <full-uuid>`, `J-resolved
  predecessor uuid: <full-uuid> (lives in sibling: <filename>)`,
  `K bridge points at in-file predecessor: <full-uuid>`.
- **Broken**: shows `dead-end phantom uuid: <full-uuid>`, `sibling
  .jsonls examined in project dir: <N>`, `phantoms successfully
  backfilled: <N>`, `phantoms K could not backfill: <N>`. Plus the
  wall-clock breakdown.

UUIDs are surfaced full (no truncation); they're the primary value
for cross-referencing across files. The truncation/collapse `<style>`
already in the wrap handles overflow visually.

### K stitching wall-clock instrumentation

`Date.now()` checkpoints at four stages of `Bz4`: parse boundary,
post-J-prepend boundary, post-K1 boundary, and the implicit pre-zi
checkpoint. Surfaced in bookend (a) and broken marker text:

```
K stitching wall-clock: parse 15ms, J cross-file prepend 2785ms,
K1 sibling backfill 649ms, K2/K3/bookend 3ms
```

Empirically: J's cross-file prepend dominates (typically 2-3s on
multi-MB siblings); K1 is hundreds of ms; K2/K3/bookend is single-
digit ms. The numbers identify J as the bottleneck for any future
K perf work without needing extra instrumentation.

### Red-on-red broken-marker header readability fix

The shared header style (`color: _bd, borderBottom: "2px dashed
"+_bd`) renders fine for low-saturation roles (bookend `rgba(220,
53,69,0.18)`, seam `rgba(255,159,28,0.20)`, bridge `rgba(255,107,
28,0.20)`) but produces near-invisible header text on the broken
role's high-saturation `rgba(180,0,0,0.50)` bg. Fix: override
header `color:` and `borderBottom:` to white for the broken role
specifically. DOM-verified `getComputedStyle(brokenHeaderDiv).color
=== "rgb(255, 255, 255)"` post-fix.

Generalized as a playbook gotcha:
[`docs/debugging.md`](docs/debugging.md) "Red-on-red (and other
role-specific bg-color clashes)".

### End-to-end verification

All four changes DOM-verified pre-push (per the playbook's absolute
DOM-verification rule):

- Baseline (no fixture) on a session previously showing 2 markers:
  still 2 markers (bookend + seam), no broken, no AMBIG.
  Bookend's K1 source line + wall-clock are present, seam's full
  uuids are visible.
- Test #5 (rename K1 source jsonl to break reconstruction):
  `[data-pfgk-role="broken"]` count = 1 (was 0 in v1.5 with same
  fixture, the silent failure mode), bodyHasINCOMPLETE === true,
  full phantom uuid visible in marker text, header `getComputedStyle
  .color === "rgb(255, 255, 255)"` (readable on saturated red bg),
  `MARKER 1 OF 1 · CYCLE TO TOP ↺` confirms broken is in the
  click-cycle navigation list (closes the v1.5 click-cycle gap
  too).

## v1.5 (2026-05-07): Patch K reconstruction-quality signaling

Patch K gains two new signals to surface reconstruction quality at
the marker level. Behavior of the previous v1.4 mechanism unchanged
when reconstruction is unambiguous and complete; the new signals
fire only in degenerate cases.

### Non-uniqueness warning (`AMBIGUOUS RECONSTRUCTION`)

K1's sibling-backfill loop now counts how many sibling .jsonls
structurally qualify for a given phantom-lpu (shared phantom-lpu
+ pre-content before their own first phantom-lpu boundary). If
more than one sibling qualifies, the chosen "canonical pre-content"
is by definition ambiguous: different filesystems / readdir
orderings could pick a different sibling and produce a different
chain root.

Behavior change: K1 no longer `break`s after the first qualifying
sibling. The full count is captured in `_ambigPhLpus` (set of
phantom-lpus with >1 candidates), and:

- The bookend ghost's content prepends a "⚠ AMBIGUOUS
  RECONSTRUCTION: N phantom-lpu compaction event(s) had multiple
  sibling-file candidates for backfill..." prefix when
  `_ambigPhLpus.size > 0`.
- Each seam ghost whose underlying phantom-lpu is ambiguous gets a
  matching prefix on its content.

Verified end-to-end via DOM probe: clone a sibling .jsonl in the
project dir to induce non-uniqueness, reload, observe
`bodyHasAMBIG === true`, bookend rendered with the warning text +
ambient styling unchanged (warning is content-side only, not
visual).

### Reconstruction-failed marker (`pfgk-broken-`)

Bookend predicate (b), the relaxed predicate that fires when
predicate (a) finds no clean parent==null user/assistant chain
root, now plants a NEW marker variant with uuid prefix
`pfgk-broken-` (was: `pfgk-bookend-`). The webview render wrap
recognizes this as a fourth role with a deliberately stronger
visual style than the regular bookend so the user can't miss it
at a glance:

- Background `rgba(180,0,0,0.50)` (saturated red, 50% alpha vs
  the regular bookend's 18%).
- Full border `4px solid #990000` (was: only border-left;
  broken gets all-around).
- Border-left still `6px solid #990000` (preserves the
  side-stripe accent shared with other markers).
- Box-shadow `0 0 12px rgba(180,0,0,0.6)` (red glow).
- Emoji `⛔` (was: `⚠️` shared across all roles).
- Content text: "⛔ INCOMPLETE TRANSCRIPT: RECONSTRUCTION
  FAILED..." (was: "PATCH K · Conversation origin (chain root
  recovered)..."). Critical signal that upstream lineage is
  missing from this view.

This handles the case where K can't make the rendered chain reach
a true canonical root despite trying, typically because the
sibling .jsonl that originally held the canonical pre-compaction
content has been deleted / moved / renamed, and no other sibling
shares the phantom-lpu.

Verified end-to-end via DOM probe: rename a sibling .jsonl
(the one K1 backfilled from) so reconstruction fails, reload,
observe `[data-pfgk-role="broken"]` count = 1, `[data-pfgk-role
="bookend"]` count = 0, `bodyHasINCOMPLETE === true`,
`getComputedStyle(brokenEl).backgroundColor === "rgba(180, 0, 0,
0.35)"`.

### Why a separate marker variant rather than a flag on the bookend

Visual differentiation matters: `bookend` is a *positive* signal
("we successfully reached the conversation origin"). When that's
not actually true, we need a marker that visually stands apart at
a glance, not just by reading the message text. Different uuid
prefix → different role → different colors and header → user
can't miss it.

## v1.4 (2026-05-04): Patch K cross-conversation backfill

Patch K becomes fully topology-driven. The rendered chain spans the
entire conversation tree, every persisted message, with markers at
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

## v1.3 (2026-05-04): Patch K orphannotice for unreachable seam

Mitigation for sessions where the seam goes unrendered. The seam is
planted on the orphan in-file chain; when a session also has a
compact_boundary whose lpu resolves cross-file via Patch J's
sibling-prepend, `Ez4`'s single-chain max-by-index walker picks the
live-chain leaf and never traverses the orphan branch, so the seam
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

## v1.2 (2026-05-03): Add Patch K (lost+found-style orphan recovery)

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
truncation gradient + collapse buttons + edit/fork action (none make
sense on a synthetic message) and centers a 42px ⚠️ banner above
the bubble. Click anywhere on the colored container scrolls to the
matching counterpart via `[data-pfgk-role="…"]` queries.

Critical implementation note: ghosts must NOT set `isMeta: true`,
because the upstream `Sz4` filter strips those. Compact summaries get away
with being synthetic-ish via `isCompactSummary` (which `Sz4` doesn't
check); we don't have that backdoor, so the synthetic flag stays off.

## v1.1 (2026-05-03): Cover H/I/J in prebuilt + stale-sig migration

Patches H, I, J had landed without bumping the signature, so v1
prebuilt users never re-applied and were silently missing the
cap-bypass / precompact-skip / cross-file lpu fixes. v1.1 introduces
the version-aware migration path: prebuilt detects any prior `pfg-vN`
signature (regex widened to support dotted minors), restores from
`.bak`, and re-applies, with no `--force` needed for end users on the
upgrade path.

- `util/build-prebuilt.py`: template now does stale-sig detection +
  version-aware restore message.
- `prebuilt/archive/v1/{2.1.121,2.1.122,2.1.123,2.1.126}/apply.py`:
  prior v1 prebuilts archived to keep `prebuilt/` tidy without
  losing history (URL stability via the archive path).

## v1 (2026-04-28 → 2026-05-03): Initial patchset

Patches A through J. Initial signature scheme with single-version
tag (`/*pfg-v1*/`, no minor).

- **A**: fork session writes a `custom-title` rescue entry when the
  fork JSONL's head 64KB would otherwise be unparseable for metadata.
- **B**: drop `position: sticky` on tall message headers (linear
  scroll instead of occluding the assistant reply).
- **C**: disable the broken `isSlashCommand` heuristic
  (`text.startsWith("/")` false-positives on pasted Unix paths).
- **D**: chain walker bridges compaction boundaries via
  `logicalParentUuid` (read-side fix; the API path is bounded
  independently by `getMessagesAfterCompactBoundary`).
- **E**: title resolver order: put `firstPrompt` ahead of
  `lastPrompt` in the resolver chain, so session titles don't drift
  to "whatever the user most recently typed". Resolver-order fix
  only; the rename-propagation fix is F.
- **F**: sidebar pencil-rename propagation through the
  `sessionStates` Map, so renamed titles don't flip back on session
  switch / broadcast. Three coordinated splices: `updateSessionState`
  preserves missing fields; `q8.renameSession` invokes
  `onSessionStateChanged` after success; sidebar `q8` ctor wires
  the callback. Plus F.2 (drop title at the `update_session_state`
  boundary so panel reactives can't clobber the Map) and F.3
  (manager writes `panel.title` directly so tab title updates
  cross-webview).
- **G**: fork-conversation handler pushes a `sessionStates` Map
  entry for the new fork immediately, so it appears in the sidebar
  without requiring a first message. Two splices: G.1 widens the
  panel ctor callback with a skip-bookkeeping flag; G.2 makes
  `fork_conversation` push a Map entry derived from the source's
  `custom-title`/`ai-title`.
- **H**: disable the 5 MB precompact-skip optimization in the loader
  (rewriting the env-var-gated condition so the optimization never
  fires). Scrollback before the compact boundary for sessions > 5 MB
  becomes visible to the chain walker.
- **I**: disable the webview's hard 500-message cap
  (rewrite the cap function to identity), so sessions with > 600
  messages don't silently truncate to the last 500 in the chat
  panel.
- **J**: cross-file `logicalParentUuid` resolution (sibling-prepend
  before the chain walker runs).

Patches F and G use a regex-anchored `apply-patch-fg.py` script
(version-tolerant across minified renamings); the rest are literal
splices captured into the per-version prebuilt.
