# Patches: detail

Each patch is described with:

- **Symptom**: what the user sees without the patch
- **Root cause**: the architectural reason the bug exists
- **Fix shape**: what the patch changes, structurally (variable names will
  drift between releases; locate by structure)
- **Upstream issue**: where the bug is reported

For the literal patch text and step-by-step locator instructions, see
[`../skill/SKILL.md`](../skill/SKILL.md).

---

## Patch A: `forkSession` writes a `custom-title` rescue

**Symptom**: forking a session that's been auto-compacted produces a fork
that appears blank in the session list, or that listSessions skips
entirely. The fork's JSONL exists on disk but its title can't be resolved
because the head 64KB starts with `isCompactSummary: true` followed by
long tool results, so the metadata parser's `firstPrompt` extractor returns
`null` and the fork is filtered out.

**Root cause**: `forkSession` doesn't emit any metadata entries for the
new fork's JSONL. The session-list metadata parser only resolves a title
if either (a) an explicit `custom-title` / `ai-title` entry exists, or
(b) the head 64KB contains a parseable first user prompt. Compacted
sessions fail (b) and have no (a), so the fork is invisible.

**Fix shape**: at the tail of `forkSession`, after the JSONL body has been
written and the messages have been registered in `this.sessionMessages`
and `this.summaries`, conditionally append a `custom-title` entry to the
fork's JSONL:

1. Read the source's JSONL, scan for the latest `custom-title` and
   `ai-title` entries; remember whichever is more recent.
2. Walk the fork's messages forward, find the first valid user prompt
   (skipping `isCompactSummary` / `isMeta` / tool-result-only content),
   track the byte offset.
3. Decide:
   - **Source has explicit title** → write that as the fork's
     `customTitle` (always). This keeps the fork visually grouped with
     its source, including post-`/rename`.
   - **Else head 64KB parser would resolve a valid prompt** → no write
     (the firstPrompt extractor handles it).
   - **Else** → write a rescue `customTitle` derived from the first user
     message anywhere in the chain, or `"Forked conversation"` as
     last-resort.

**Upstream issue**: [#48937](https://github.com/anthropics/claude-code/issues/48937)

---

## Patch B: sticky message header to linear scroll

**Symptom**: a tall user message at the top of a turn is rendered with
`position: sticky`, so when the assistant reply scrolls past, the user
message remains pinned to the top and visually occludes the reply.

**Root cause**: pure CSS layout choice in
`webview/index.css`. The `.message_<S>.stickyHeader_<S>` rule sets
`position: sticky; z-index: 2; background-image: linear-gradient(...);
top: 0`.

**Fix shape**: replace the sticky positioning with linear flow:
`position: relative; z-index: auto`, drop the gradient and `top: 0`. Also
update the `[aria-expanded=true]` variant rule's `z-index: 3` → `z-index:
auto`.

**Upstream issue**: [#49114](https://github.com/anthropics/claude-code/issues/49114)

---

## Patch C: disable broken `isSlashCommand` detection

**Symptom**: any user message that begins with `/` (e.g. a pasted Unix
path, a compiler error message, a literal slash-command someone wrote out)
loses its `userMessageContainer` wrapper in the rendered chat, which
removes the fork/rewind action button.

**Root cause**: in-band signalling from message text. The webview infers
"this user message is a slash command" via
`text.startsWith("/")`. The actual slash-command dispatch happens
elsewhere (it's wired up before the message is committed to the
transcript), so this `startsWith` check is purely cosmetic, and wrong
in principle. It false-positives on any path-like or `/`-prefixed
content.

**Fix shape**: replace the `startsWith` check with literal `false`. The
slash-command render path stops being used; user messages render with
their normal wrapper and action buttons.

**Upstream issue**: [#49155](https://github.com/anthropics/claude-code/issues/49155)

---

## Patch D: chain walker bridges compaction boundaries via `logicalParentUuid`

**Symptom**: `claude --resume <session-id>` and the in-VSCode rewind UI
display messages from days ago instead of the most recent conversation.
Hours of recent work appear lost, even though the JSONL on disk contains
them.

**Root cause**: the compact stitch (`type:"system",
subtype:"compact_boundary"`) is written with `parentUuid: null` and the
actual pre-compact predecessor stored in `logicalParentUuid`. The
chain-walking code in `extension.js` follows `parentUuid` only, so any
read path that reconstructs the conversation by walking backwards from
the last message stops at the boundary, even though the pre-compact
transcript is intact on disk.

The architecture justifies a read-side workaround: the API context is
bounded independently of `parentUuid` topology by
`getMessagesAfterCompactBoundary`, which scans for the boundary marker.
So adding a `logicalParentUuid` fallback to the chain walker is safe:
it doesn't blow up the API context, only restores UI/fork visibility.

**Fix shape**: there are **two near-identical inline parentUuid walkers**
in `extension.js`. Both need the same bridge:

```diff
- <x> = <x>.parentUuid ? <map>.get(<x>.parentUuid) : void 0
+ <x> = <x>.parentUuid ? <map>.get(<x>.parentUuid)
+                       : (<x>.logicalParentUuid ? <map>.get(<x>.logicalParentUuid) : void 0)
```

Do **not** patch `getTranscript` (a method on the session class). It
already has the `K=!1` opt-in fallback used by `forkSession`.

### v1.8: cycle gate + data-loss verdict

- **Rewrite-gate**: skip the upstream `compactMetadata.preservedMessages`
  rewrite for any `compact_boundary` whose `logicalParentUuid` resolves
  in-file (`if(D.logicalParentUuid&&V.has(D.logicalParentUuid))continue`).
  That rewrite re-points the preserved window onto the post-boundary
  summary; with the lpu fallback above also active, the boundary lpu
  pointed into the rewritten window and the walk cycled, truncating the
  transcript. Skipping it keeps the up-links so the walk reaches origin.
- **Post-walk verdict**: key on the terminal node. Origin reached via
  bridging gives `pfgk-bookend-` reconstructed; reached cleanly gives no
  marker; not reached gives `pfgk-broken-` INCOMPLETE. The marker raises
  whenever the walk stops short of a real root. Marker vocabulary and
  rendering are Patch K's.

**Upstream issue**: [#46603](https://github.com/anthropics/claude-code/issues/46603)

---

## Patch E: title resolver puts `firstPrompt` ahead of `lastPrompt`

**Symptom**: a session's displayed title drifts over time to "whatever
the user most recently typed" instead of "what the conversation is
about." Long-running sessions accumulate titles like
`"thanks"` or `"can you check this"` even though the conversation was
originally about a totally different topic.

**Root cause**: the session-list metadata parser resolves a title via
the chain:

    customTitle || aiTitle || lastPrompt || summary || firstPrompt(head)

`lastPrompt` outranking `firstPrompt(head)` causes title drift. The
correct order has `firstPrompt(head)` ahead, with `lastPrompt` as a
last-resort.

**Fix shape**: there are **two near-identical resolver chains** in
`extension.js`. Swap the order at each so `firstPrompt` appears before
`lastPrompt`:

```diff
- <H> || <X>(src,"lastPrompt") || <X>(src,"summary") || <FP>
+ <H> || <FP> || <X>(src,"summary") || <X>(src,"lastPrompt")
```

(One site has `firstPrompt` as a precomputed variable; the other has it
as a direct call. Both flip the same way.)

**Upstream issue**: [#32150](https://github.com/anthropics/claude-code/issues/32150)

---

## Patch F: session rename writes propagate through `sessionStates` Map

**Symptom**: renaming a session via the sidebar pencil icon shows the new
title briefly, then it flips back to the previous title, typically when
the user switches sessions, or any other event that triggers a
session-states broadcast.

**Root cause** (traced via CDP signal-write spies): the extension's
`sessionStates` Map is the source of truth for `broadcastSessionStates()`
payloads. Its only writer is `updateSessionState(sessionId, state, title)`
on the manager class, which the chat panel's per-session reactive
triggers via the `update_session_state` message. The rename path doesn't
update the Map: `q8.renameSession` (the message handler) just delegates
to `m1.renameSession` (storage class) → JSONL write → return. Neither
layer touches `sessionStates`, calls `broadcastSessionStates()`, or
notifies any other component.

For panel-triggered renames the per-session reactive happens to fire on
`summary.value` change and re-aligns the Map as a side effect.
**Sidebar-driven renames have no such reactive**, and the sidebar's own
`q8` instance is constructed in `resolveSessionListView` with `void 0`
for the `onSessionStateChanged` callback slot, so even if the sidebar's
webview *did* send `update_session_state`, the manager would never
receive it.

After a sidebar rename, the Map still holds the previous title. Any
subsequent `broadcastSessionStates()` (focus change via `setActivePanel`,
busy-state flip on any session, etc.) sends a `session_states_update`
carrying the stale title. The sidebar's update handler in the webview
unconditionally writes `N.summary.value = O.title` if `O.title` is
truthy, overwriting the just-renamed local signal.

**Fix shape** (five splices in `extension.js`):

1. **Site 1+3 (`updateSessionState` preserves missing fields + writes
   `panel.title` directly)**: the manager method becomes:

   ```diff
     updateSessionState(sessionId, state, title) {
   +   const prev = this.sessionStates.get(sessionId);
       this.sessionStates.set(sessionId, {
         sessionId,
   -     state, title
   +     state: state != null ? state : (prev?.state ?? "idle"),
   +     title: title != null ? title : prev?.title,
       });
       this.broadcastSessionStates();
   +   if (title != null) {
   +     const pnl = this.sessionPanels.get(sessionId);
   +     if (pnl) pnl.title = title;        // bypass webview-reactive plumbing
   +   }
     }
   ```

2. **Site 2 (`q8.renameSession` invokes `onSessionStateChanged`)**: after
   the storage-layer write succeeds, push the new title into the
   manager's Map and trigger a broadcast:

   ```diff
     async renameSession(sessionId, title, isAi) {
   -   return {
   -     type: "rename_session_response",
   -     skipped: await (await m1.load(this.cwd, this.logger))
   -       .renameSession(sessionId, title, isAi),
   -   };
   +   const skipped = await (await m1.load(this.cwd, this.logger))
   +     .renameSession(sessionId, title, isAi);
   +   if (!skipped) this.onSessionStateChanged?.(sessionId, undefined, title);
   +   return { type: "rename_session_response", skipped };
     }
   ```

3. **Sidebar `q8` ctor wires `onSessionStateChanged`**: the sidebar
   instance is constructed without the callback. Wire a minimal
   forwarder:

   ```diff
     new q8(/* … */, /*panel*/ undefined, () => this.broadcastUsageUpdate(),
   -        // sidebar omits isFullEditor + onSessionStateChanged
   +        /*isFullEditor*/ false,
   +        (id, state, title) => this.updateSessionState(id, state, title));
   ```

4. **F.2, drop title at the `update_session_state` boundary**: the
   webview reactive sends a title field, but the title channel should
   only be authoritative on the rename path. Drop it at the message
   handler so panel reactives can no longer clobber the Map with stale
   panel-local `summary.value`:

   ```diff
     if (V.request.type === "update_session_state")
       return this.onSessionStateChanged?.(
         V.request.sessionId, V.request.state,
   -     V.request.title
   +     void 0
       ), { type: "update_session_state_response" };
   ```

   After F.2, only `q8.renameSession` (Site 2) ever pushes a title into
   the Map.

**Why F.2 and the direct `panel.title` write are needed**: panels don't
have the `sessionStates → summary.value` bridge that the sidebar's `Te1`
component has. Panel `summary.value` for any session it isn't actively
displaying stays at the initial-load value. The panel's per-session
reactive `K4(()=>{Z.updateSessionState(sessionId, state, Y.summary.value)})`
fires on busy-state flips and sends `update_session_state` carrying that
**stale** title. F.2 drops the title at the boundary so this doesn't
clobber. F.3 (the `panel.title` direct write) compensates for the same
gap on the read side: with no panel-bridge, the `renameTab` reactive
never fires for sidebar-driven renames, so the manager has to update
`panel.title` itself.

**Upstream issue**: [#53942](https://github.com/anthropics/claude-code/issues/53942)

---

## Patch G: forked session appears in sidebar without sending a message

**Symptom**: forking a session creates the new JSONL on disk, but the
new session doesn't appear in the sidebar list until the user sends a
message in it. Sending a message triggers the new session's busy state,
which propagates through the `sessionStates` Map to the sidebar, which
then re-fetches the session list.

**Root cause**: same shape as Patch F. The sidebar's
`Te1` component re-fetches the session list (`$.listSessions()`) only
when its `G.length > 0` `useEffect` fires, where `G` is built from
broadcast Map entries that aren't in `Vn.sessions`. The fork has no Map
entry until the new session's first state change.

**Fix shape** (two splices in `extension.js`):

1. **G.1, panel ctor callback supports skip-bookkeeping flag**: the
   panel callback wired in `setupPanel` does panel-mapping bookkeeping
   (`sessionPanels.set(forkSid, panel); sessionPanels.delete(sourceSid)`)
   that's wrong when called at fork-handler time: the panel still owns
   the source session at that point, the fork hasn't been activated yet.
   Add a 4th arg to skip bookkeeping:

   ```diff
   - (sid, state, title) => {
   + (sid, state, title, skipMapping) => {
       this.updateSessionState(sid, state, title);
   +   if (skipMapping) return;
       for (const [z, L] of this.sessionPanels)
         if (L === panel && z !== sid) this.sessionPanels.delete(z);
       if (this.sessionPanels.set(sid, panel), panel.active)
         this.activeSessionId = sid;
     }
   ```

2. **G.2, `fork_conversation` handler pushes Map entry with source's
   title**: the wrinkle is that the Map title must agree with what the
   sidebar will load from JSONL via the title resolver, otherwise the
   sidebar's `sessionStates → summary.value` bridge overwrites the
   JSONL-derived title with the placeholder. Patch A's fork-time
   `custom-title` injection inherits the source's `customTitle` /
   `aiTitle` to the fork's JSONL, so we read the source's metadata and
   use that:

   ```diff
   - case "fork_conversation":
   -   return {
   -     type: "fork_conversation_response",
   -     sessionId: await (await m1.load(this.cwd, this.logger))
   -       .forkSession(req.forkedFromSession, req.resumeSessionAt),
   -   };
   + case "fork_conversation": {
   +   const storage = await m1.load(this.cwd, this.logger);
   +   const sourceSid = req.forkedFromSession;
   +   const newSid = await storage.forkSession(sourceSid, req.resumeSessionAt);
   +   let title = "";
   +   try {
   +     const lines = (await fs.promises.readFile(
   +       path.join(d5(storage.projectRoot), `${sourceSid}.jsonl`), "utf8"
   +     )).split("\n");
   +     let custom = "", ai = "";
   +     for (const line of lines) {
   +       if (!line) continue;
   +       try {
   +         const m = JSON.parse(line);
   +         if (m.type === "custom-title" && m.customTitle) custom = m.customTitle;
   +         if (m.type === "ai-title" && m.aiTitle) ai = m.aiTitle;
   +       } catch (_) {}
   +     }
   +     title = custom || ai;
   +   } catch (_) {}
   +   if (!title) title = "Forked conversation";
   +   this.onSessionStateChanged?.(newSid, "idle", title, true /* skipMapping */);
   +   return { type: "fork_conversation_response", sessionId: newSid };
   + }
   ```

   The `fs`, `path`, and `projectRoot` resolver names drift between
   releases (e.g. `R1`/`O1`/`d5` on 2.1.120 → `W1`/`O1`/`n5` on 2.1.121).
   The version-tolerant apply script discovers them from the storage
   class's own `renameSession` (which has a fixed structural shape
   exposing all three).

**Upstream issue**: [#53942 (follow-up comment)](https://github.com/anthropics/claude-code/issues/53942#issuecomment-4332593160)

---

## Why F and G share a root cause

Patches F.2, F.3, G.1, and G.2 are all symptoms of the same gap: chat
panel webviews don't have the `sessionStates → summary.value` bridge that
the sidebar's `Te1` component has. The bridge is a one-`useEffect`
fragment that iterates incoming `sessionStates` broadcast entries and
syncs the Vn-instance's per-session `summary.value` signals to match.

Adding the bridge to the panel webview would obsolete most of F and G.
The panel's `renameTab` reactive would fire correctly on cross-webview
renames, the panel's per-session `update_session_state` reactive would
read fresh titles instead of stale local copies, and forks could be
discovered via the standard placeholder-mechanism without wrestling with
panel-mapping bookkeeping.

Until that happens upstream, F.2/F.3/G are extension-side bypasses for
the missing webview bridge.

---

## Patch H: bypass the 5 MB precompact-skip optimization

The bundled session loader applies a "precompact skip" optimization for
files > 5 MB: instead of reading the full JSONL, it scans for the most
recent `compact_boundary` system message and returns only the
post-boundary buffer. Pre-boundary content is never parsed into the
messages array that downstream consumers (chain walker, fork picker,
rewind UI, chat panel render) operate on.

The leaked source documents this as an optimization, justified by the
observation that the API context is bounded by `compact_boundary`
anyway. But the loader is *also* what feeds the chat-panel render and
the chain walker's input, so the optimization breaks every read path
that wants to see content before the compact boundary.

**Threshold**: `SKIP_PRECOMPACT_THRESHOLD = 5 * 1024 * 1024` ([leaked source `sessionStoragePortable.ts:480`](https://github.com/yasasbanukaofficial/claude-code/blob/main/src/utils/sessionStoragePortable.ts#L480)).

**Gate**: [`sessionStorage.ts:3536-3556`](https://github.com/yasasbanukaofficial/claude-code/blob/main/src/utils/sessionStorage.ts#L3536-L3556), the
`if (size > SKIP_PRECOMPACT_THRESHOLD) buf = scan.postBoundaryBuf` branch.

**Env-var kill switch**: `CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP` already
exists upstream. Patch H makes it permanent at the read-site instead of
relying on the user setting an env var.

### Patch shape

In `extension.js`, find the bundled equivalent of:

```js
function Rz4(V, K) {
  try {
    if (K > Hz4 && !M2(process.env.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP))
      return (await jz4(V, K)).postBoundaryBuf;
    return await ml.readFile(V);
  } catch { return null; }
}
```

`Hz4` is the 5 MB constant; `M2(env)` is the env-var truthy check. Replace:

```diff
- if (K > Hz4 && !M2(process.env.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP))
+ if (K > Hz4 && !(!0 || M2(process.env.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP)))
```

The `!0 || ...` short-circuits to `true`; the negation makes it `false`;
the `&&` makes the whole condition `false`; the optimization never
fires. The original env-var read is kept around the `||` for forensic
clarity (no functional effect; this confirms the splice point lines up
with the pre-existing kill switch).

**Empirical**: on a 49 MB session, `getSession` returned 27 messages
pre-patch (post-boundary slice) and 1857 post-patch (full in-file chain
back to the next dangling lpu). Cache_read on the next API turn stayed
bounded at ~30 K tokens. `getMessagesAfterCompactBoundary` does its
own boundary slicing on the way to the API, independent of what the
loader returns.

**Upstream issue**: [#55700](https://github.com/anthropics/claude-code/issues/55700)

---

## Patch I: neutralize the webview's 500-message render cap

The webview hardcodes a cap on the messages array assigned to React
state: anything > 600 is silently truncated to the last 500. There's no
"load more" affordance, no UI indication.

Once Patch D restores chain-walker visibility past the most recent
`compact_boundary`, sessions with > 500 historical messages still cap
out at 500 in the chat panel. The whole point of D + H is undone by
this single function unless I is also applied.

### Patch shape

In `webview/index.js`, find:

```js
function OD($) {
  if ($.length > g20) {        // g20 = 600
    let Z = $.length - u20;    // u20 = 500
    return $.slice(Z);
  }
  return $;
}
```

Replace with the identity function:

```js
function OD($) { return $; }
```

The bundled function name (`OD`) and the constants (`g20`, `u20`) drift
between releases, so locate by structure (the `slice($.length - u20)`
shape is distinctive).

**Caveat**: rendering 10K+ messages takes a few seconds at first paint.
The bottleneck is React reconciliation, not the patch. If first-paint
latency becomes painful, partial mitigation: keep a generous cap (e.g.
`if ($.length > 10000) return $.slice(-10000)`, much higher than 500
but still bounded).

**Upstream issue**: [#55701](https://github.com/anthropics/claude-code/issues/55701)

---

## Patch J: cross-file logicalParentUuid resolution at session load

Patches D + H restore visibility back to the most recent in-file
`compact_boundary` stitch. But fork-from-compact creates stitches whose
`logicalParentUuid` points to a uuid in a **different** JSONL: the
parent session that was compacted. The chain walker's
`parentUuid → logicalParentUuid` fallback (Patch D) misses, because the
in-memory map only has the current session's messages.

Patch J resolves cross-file lpu pointers at load time by scanning
sibling JSONLs in the project dir.

### Algorithm

1. Parse current JSONL via `Yz4` → `_parsed`. Build `_seen` set of all
   uuids in `_parsed`.
2. Iterate (fixed-point, capped at 10 passes):
   a. Scan `_parsed` for compact_boundary stitches with
      `parentUuid: null` and `logicalParentUuid` not in `_seen`. These
      are the dangling lpus.
   b. For each dangling lpu, scan `*.jsonl` files in the project dir
      (other than the current one). Substring-prefilter via
      `"uuid":"<lpu>"`. Parse matching files via `Yz4`, cache.
   c. Track per-file the **maximum** index across all dangling lpus
      found in that file. (Multiple dangling lpus into the same parent
      session is the common case, and they all need the slice up to
      the furthest one.)
   d. For each matched file, take slice `[0..maxIdx]` (inclusive). For
      each message in the slice not already in `_seen`, push to
      `_newPrepend` and add uuid to `_seen`.
   e. If `_newPrepend` is empty, break. Otherwise prepend to `_parsed`
      and loop, because the just-added messages may themselves contain
      compact_boundary stitches whose lpus point further back.
3. Pass extended `_parsed` to `dl()` as before.

### Patch shape

In `extension.js`, find the bundled `Wz4` (or equivalent: the loader
that calls `Yz4(buf)` then `dl(parsed, K)`):

```js
async function Wz4(V, K) {
  if (!uz(V)) return [];
  let B = await qz4(V, K?.dir);
  if (!B) return [];
  let x = await Rz4(B.filePath, B.fileSize);
  if (!x) return [];
  return dl(Yz4(x), K);
}
```

Replace with the iterative resolver. See [`SKILL.md`](../skill/SKILL.md)
Step 12 for the full minified body and per-version variable mapping
(loader role names: `<LOADER>`, `<EXIST>`, `<FIND_FILE>`, `<READ_BUF>`,
`<PARSE>`, `<DL>`, `<PATH>`, `<FS_PROMISES>`, `<FS_RAW>`).

### Design notes

- **Per-file max-index slicing**, not per-lpu separate slices. Multiple
  dangling lpus into the same parent file is the common case (a session
  compacted N times before being forked produces N stitches all pointing
  at uuids in the parent file at increasing indices). Loading per-lpu
  would re-parse the same file N times. Per-file with max-index reads
  the file once and takes the longest slice that covers all of them.
- **Fixed-point iteration**, not single-pass. The prepended sibling can
  itself contain compact_boundary stitches with their own dangling lpus
  pointing to grandparent sessions. Loop until either no more dangling
  remain or no new prepends were produced (fixpoint converged).
- **No try/catch around the resolver**. If a sibling read fails or its
  parse throws, fail loudly so the user sees an error in the chat panel
  rather than silently getting a less-extended chain.

### Performance

First open of an affected session: load latency increases by the parse
time of matched siblings. For a 56 MB sibling parsed once, that's about
1–2 seconds. Subsequent re-opens of the same session are fast (V8 has
the parsed result cached in module-scope `_filesParsed` for the lifetime
of the extension host process. No, that's per-call inside `Wz4`; the
benefit is from OS filesystem cache only).

**Upstream issue**: addresses the cross-file half of [#48937 secondary](https://github.com/anthropics/claude-code/issues/48937) and [#46603](https://github.com/anthropics/claude-code/issues/46603).

## Patch K: lost+found-style recovery for dangling logicalParentUuid

### Why

Patches D and J resolve compact-boundary `logicalParentUuid` pointers
when the target message is somewhere on disk (in-file via D, in a
sibling JSONL via J). They can't help when the target uuid is a
*phantom* (never persisted anywhere), which happens when the
upstream compactor at `compact.ts:598` captures `messages.at(-1)?.uuid`
without filtering out non-loggable types (e.g. `progress` messages,
or in-flight assistant turns whose uuid was allocated before the auto-
compact interrupted them).

Symptom: chain walker hits the boundary and stops. The user sees a
silently truncated transcript even though the pre-compaction JSONL
content is intact on disk. No error, no UI feedback.

Patch K runs at session-load time, after D + J have done their work.
The K block has four synthesis steps, each addressing a different
shape of compaction corruption:

1. **Phantom-lpu sibling backfill** (cross-conversation recovery):
   For each compact_boundary whose `logicalParentUuid` is phantom
   (no sibling has it as a uuid AS WELL), check if any sibling has
   the same uuid AS A logicalParentUuid (= they share the same
   compaction's missing predecessor). If so, the conversation tree
   continues across that sibling. Find the sibling's content before the
   compact boundary (lines from chain root to its first phantom-lpu boundary)
   and prepend that into `_parsed`. This recovers the conversation's
   true origin from a forked sibling whose chain root is a real user
   message.
2. **Seam ghosts** (in-file orphan recovery):
   For each compact_boundary whose lpu is still phantom after step 1,
   synthesize a `pfgk-seam-…` ghost parented to the in-file predecessor
   (the message immediately before the boundary in `_parsed`), and
   rewrite the boundary's lpu to point at the seam. The chain walker
   bridges through the seam.
3. **Bridge ghosts** (cross-file → in-file redirection):
   For each compact_boundary whose lpu was resolved cross-file by
   Patch J (the live chain takes the cross-file shortcut), synthesize
   a `pfgk-bridge-…` ghost between the in-file orphan chain's leaf
   and the boundary's first child (live chain head). The chain walker
   now traverses the in-file orphan instead of the cross-file shortcut.
   The cross-file content is still in `_parsed` (J prepended it) and
   reachable via the seam path → its content stays rendered.
4. **Bookend ghost** (chain-root marker):
   Prepend a `pfgk-bookend-…` ghost at the chain root. Two predicates,
   tried in order: (a) original, first non-system message with
   `parent==null && !lpu`; (b) relaxed, first user/assistant whose
   parent chain dead-ends in a phantom-lpu compact_boundary (covers
   the case where the chain root is parented to a system boundary, as
   happens after step 1's backfill).

After all four steps, the renderer's chain walker traverses the full
conversation tree in chronological order: bookend → backfilled origin
→ seam at first compaction → in-file pre-content → bridge at second
compaction → live chain → leaf. **Zero persisted message is dropped
from the rendered transcript.**

The recovered topology isn't *guaranteed* to be canonical. In
pathological cases (e.g., two separate conversations that happened
to land in the same JSONL), the seam/bridge brackets could fudge
unrelated content. The visible markers exist to make the
recovery-vs-fabrication boundary legible to the user.

### v1.8: verdict moved to the walker; in-file seams; render fix

The bookend/broken verdict no longer runs as per-cause loops in the
loader (step 4 above). It is a single 3-state verdict computed in the
Patch D walker after the up-walk, keyed on whether the walk reached a
real root. The loader's `pfgk-bookend-`/`pfgk-broken-` loops are removed;
the seam (step 2) and bridge (step 3) loops stay.

- **Seam now also marks in-file compactions.** Step 2 planted a seam only
  for phantom boundaries; v1.8 also plants one at each `compact_boundary`
  the walker crosses in-file (the native boundary is a filtered system
  message and never renders, so the crossing was previously invisible).
- **Render-wrap `pfgk-broken-` case.** The role detector matched
  `pfgk-bookend`/`pfgk-seam-`/`pfgk-bridge-` but not `pfgk-broken-`, so
  the broken marker rendered unstyled. Added the case.

### Locate

In `extension.js`, find the `Wz4` (or its drift-renamed equivalent)
loader function. It's where Patch J's fixed-point loop terminates.
The K block goes between J's loop tail (`if(_newPrepend.length===0)
break;_parsed=[..._newPrepend,..._parsed];}`) and the trailing
`return dl(_parsed,K)` call.

In `webview/index.js`, find the user-message render path: the only
`Z.type==="user"` branch that creates the user-message component (`<USER_MSG_COMPONENT>`, drifts per bundle) after the
`parentToolUseId`/`isSynthetic` early-return guards. Wrap the
returned element with a colored container when `Z.uuid` starts with
`pfgk-`.

### Patch (extension.js)

See SKILL.md Step 13 for the full splice. The K block has four
synthesis steps (see "Why" above). Five marker kinds are emitted: `pfgk-bookend-…` (cyan, marks the reconstructed
chain root of an intact transcript), `pfgk-broken-…` (red, marks a
chain that dead-ended at a missing ancestor), `pfgk-seam-…` (amber,
marks an in-file compaction crossed via phantom-lpu reattachment),
`pfgk-bridge-…` (orange, marks a compaction whose lpu lives in a
sibling `.jsonl`; the ghost is inserted between that cross-file lpu
and the boundary), plus a slate-toned variant of seam (payload field
`dg:"seamClean"`) for clean in-file compactions where the walk
bridges in-file without phantoms.

### Patch (webview/index.js)

When a message's uuid begins `pfgk-`, replace the user-message bubble
entirely with a structured card. The card has:

- A header bar: `PATCH K` tag, a per-role state badge (`◆ RECONSTRUCTED
  · INFO`, `⛔ UNRECOVERABLE`, `⚠ IN-FILE REATTACH`, `↻
  CROSS-FILE BRIDGE`, or `◇ IN-FILE COMPACTION`), a zero-padded
  counter (`MARKER 03 OF 08`) and `↓ NEXT` / `↺ CYCLE` navigation.
  Counter and nav are computed from `$.messages.peek()`. Clicking
  cycles to the next marker via
  `document.querySelectorAll("[data-pfgk-role]")`, wrapping from the
  last back to the first.
- A per-role glyph and headline.
- An SVG topology diagram, built by `_pfDiagram(dg, T)` using helpers
  `L` (line), `TX` (text), `D` (chain dot). One case per role:
  bookend (chain reconstructed), broken (dead-end), seam (phantom
  reattachment), seamClean (clean in-file link), bridge (the cross-file
  link arc crosses a dashed file-boundary divider into the boundary).
- A rows table from the payload's `rows` field (`[[key, value], ...]`).
  Rows whose counts are zero are omitted by the loader.
- A body paragraph (`body`).
- A separate monospace timing line (`tm`) showing K stitching
  wall-clock.

Per-role tone tokens live in `_PFTOK`: bookend cyan, broken red, seam
amber, bridge orange, healthy (clean-seam) slate. The render-wrap also
injects CSS that suppresses bubble truncation, the "Show more / Show
less" collapse buttons, and the edit/fork action buttons. None of these
apply to a synthetic marker. Payload is parsed from `block.content.text`
after the IDE's message assembler reshapes the ghost into a content
block array (see SKILL.md's data-channel contract bullet).

### Critical implementation notes

- **`isMeta` must NOT be set on ghosts.** The render filter (`Sz4`)
  drops `isMeta:true` messages. Compact summaries get away with being
  synthetic-ish because they only set `isCompactSummary` (which `Sz4`
  doesn't check). Setting `isMeta:true` on our ghosts hides them
  entirely.
- **Out-of-band signaling.** The colored wrapper detects ghost-ness
  via the `pfgk-` uuid prefix, not via the message content. In-band
  detection (parsing message text for marker strings) is fragile:
  any real user message could spoof it. The uuid prefix is structural
  metadata that the renderer can dispatch on without reading content.
- **The card is built structurally, not from message text.** The
  render-wrap parses the `PFGK1:` payload out of the assembled content
  block and constructs the card via `createElement` (header, glyph,
  SVG diagram, rows, body, timing line). The ghost's message body is
  never shown as prose; the uuid prefix selects the card, the payload
  fills it.

### Test

Open a session in a conversation family with at least one phantom-lpu
boundary. The chat panel should:

1. Show a cyan **bookend** card at the very top, badge `◆ RECONSTRUCTED
   · INFO`, counter `MARKER 01 OF N` with `↓ NEXT`, followed
   immediately by the conversation's true first user message (the
   canonical origin recovered via cross-conversation backfill).
2. Show an amber **seam** card (badge `⚠ IN-FILE REATTACH`) at each
   compaction whose lpu was a phantom reattached in-file, or a slate
   **clean-seam** card (badge `◇ IN-FILE COMPACTION`) where the
   in-file bridge needed no phantom.
3. Show an orange **bridge** card (badge `↻ CROSS-FILE BRIDGE`) at
   each compaction whose lpu lives in a sibling `.jsonl`. The card's
   "cross-file source" row names that sibling; the diagram's cross-file
   link arc crosses a file-boundary divider into the boundary.
4. If the walk dead-ends at an unreachable ancestor, the top card is a
   red **broken** card (badge `⛔ UNRECOVERABLE`)
   instead of a bookend.
5. Each card's counter is zero-padded (`MARKER 03 OF 08`); the last
   card reads `↺ CYCLE`. Clicking any card cycles to the next in
   document order, wrapping from the last back to the first.

For the canonical test, open the most-compaction-impacted session in
the family (e.g. one whose JSONL starts with a `compact_boundary` at
line 1) and verify that the original user prompt at the top matches
what appears at the top of any other session in the same family. The
backfill should produce identical recovered origins across the tree.

### Background: walker constraints + recovery topology

The renderer's chain walker (the Patch D up-walk function; the minified name drifts every bundle, `xE0` in 2.1.159) is **single-leaf, max-by-_parsed-index, traversing
parentUuid (with logicalParentUuid fallback per Patch D) backward**:

1. Build `K`: uuid → msg map from `V` (= `_parsed` after J's prepend
   + K's modifications).
2. Compute `B`: uuid → original index in `V` (positions in the
   pre-walker array).
3. Find all **leaves**: uuids that *aren't* used as anyone's
   `parentUuid`. These are messages with no children in `V`:
   typically the most recent user message, the most recent assistant
   reply, plus any sibling tip in K's prepended content.
4. For each leaf, walk up via `parentUuid` (with `logicalParentUuid`
   fallback) to the first `user`/`assistant` ancestor. These become
   candidates `U`. Filter out sidechain / teamName / isMeta tips →
   `q`.
5. **Pick the single leaf with the highest index in `B`** (i.e. the
   one written latest into `_parsed`). Call it `Z`.
6. Walk back from `Z` only via `parentUuid`/`lpu` fallback,
   collecting `H`. Reverse `H`. Return.

Three properties of this design that constrain K's recovery work:

- **Only ONE leaf's chain renders.** Other leaves' walks are computed
  in step 4 but discarded in step 5. Adding content to `_parsed`
  introduces new leaves, but their leaves get pruned out unless
  they're connected (via parentUuid/lpu) into the chain rooted at
  the chosen `Z`.
- **The "root" of the rendered chain is wherever the walk
  terminates**: not selected, just emerges. Could be a
  `parent==null` user msg (clean canonical root), or could dead-end
  at a system compact_boundary with unresolvable lpu (the case
  predicate (b) catches as "broken").
- **The leaf selection is index-based, not topology-based.** "Max
  index in `_parsed`" = "latest written" ≈ "most recent message in
  the live conversation". K can't change which leaf wins by
  prepending content; prepended content's leaves have low indices
  and lose to the live leaf.

This design forces K's recovery to be topology-driven: K can't
short-circuit to "render multiple chains"; it has to ensure the
walker starting from the live leaf reaches every persisted message
in this branch via parentUuid/lpu links. The four synthesis steps
(backfill / seam / bridge / bookend) work together to satisfy that
single-leaf-walk constraint:

- **Backfill** (K1) prepends a forked sibling's pre-compaction
  content into `_parsed` so the walker has somewhere to walk back
  to (when the in-file chain root is a system boundary).
- **Seam ghosts** (K2) give the walker a `uuid` to follow when
  boundaries have phantom lpus, by rewriting `boundary.lpu =
  seam.uuid` and parenting the seam to the in-file predecessor.
- **Bridge ghosts** (K3) redirect the walker from Patch J's
  cross-file shortcut back into the in-file orphan chain, by
  reparenting the boundary's first child onto the bridge ghost.
- **Bookend / broken** at the chain root marks where the recovered
  chain begins (or, in `pfgk-broken-` case, where it failed to).

**Common pitfall when designing K extensions**: "I added content to
`_parsed`" ≠ "the user sees it rendered". You have to ensure
parentUuid/lpu connectivity from the live leaf BACK through your
prepended content to a true root. If you just prepend without
linking, the walker picks the live leaf, walks back through
existing links only, and your new content sits in `_parsed` as a
disconnected sub-graph, pruned by step 5's max-by-index leaf
selection.

A simpler `rO4`-side fix would collect content from ALL tips (multi-
leaf rendering, concatenating the walks from every leaf) instead of
just max-by-index `Z`, eliminating the need for bridges and
relaxing the connectivity-from-live-leaf constraint. That's a much
bigger change and not currently attempted.

### How the cross-conversation backfill works

The phantom-lpu (`logicalParentUuid` that no sibling has as a `uuid`)
is the smoking gun left by `compact.ts:598`. Critically: when a
conversation forks, ALL forks of that conversation inherit the same
phantom lpu in their first compact_boundary (the missing predecessor
is missing from EVERY fork). So a sibling sharing the phantom lpu IS
a fork of the same conversation tree.

K's backfill exploits this: for each phantom lpu, scan siblings and
find one that ALSO has it as an lpu AND has pre-content before its
first phantom-lpu boundary (a real user message at chain root, not
itself a boundary). Prepend that sibling's pre-content into `_parsed`.

The result: even sessions whose own first line is a compact_boundary
(no recoverable in-file origin) can now display the conversation's
true canonical origin, sourced from a sibling fork that retained it.

**Upstream issue**: [#55818](https://github.com/anthropics/claude-code/issues/55818) (read-side mitigation) + [#46603](https://github.com/anthropics/claude-code/issues/46603) (write-side root cause at `compact.ts:598`).

---

## Patch L: force `--thinking-display summarized` on the IDE-spawned CLI

For `claude-opus-4-7[1m]` (and any 4.7+ model), Anthropic flipped the API
default for `thinking.display` from `"summarized"` to `"omitted"`
(documented in their Opus 4.7 migration guide). With `display: "omitted"`,
the API returns thinking content blocks with an empty `thinking` field and
a multi-KB `signature` only, so the webview renders the static
`<div class="thinkingStatic">Thinking</div>` stub, since its `thinking.length > 0`
branch can never fire. Thinking summaries vanish from the IDE chat panel.

The bundled CLI *has* a gate that sets `display = "summarized"` when
`settings.json` carries `showThinkingSummaries: true`, but the gate is
`!getIsNonInteractiveSession() && showThinkingSummaries === true`. The IDE
spawns the CLI subprocess with `--print --input-format stream-json
--output-format stream-json`, which makes the session non-interactive, so
the gate never fires for IDE chat panels, so the user's setting is silently
ignored where it matters most.

The CLI also accepts an explicit `--thinking-display <mode>` flag that
bypasses the non-interactive gate. The IDE's SDK-side spawn code already
knows how to pass it, but only when `thinkingConfig.display` is set on the
spawn-time options, and the chat-panel caller never sets it, so the flag
never reaches argv.

### Patch shape

In `extension.js`, find the argv assembly that gates the flag on `U.display`
being truthy:

```js
if(U.type!=="disabled"&&U.display)i.push("--thinking-display",U.display)
```

Replace with an unconditional push that defaults the mode to `summarized`:

```js
if(U.type!=="disabled")i.push("--thinking-display",U.display||"summarized")
```

Dropping `&&U.display` stops the empty-display case from suppressing the
flag; `||"summarized"` supplies the fallback value. Every IDE-spawned CLI
subprocess now receives `--thinking-display summarized`, the CLI's first
display-gate branch fires regardless of interactive state, and the API
returns real thinking summaries again. The local symbols (`U`, `i`) drift
between releases; locate by the distinctive `"--thinking-display"` string
literal.

If/when upstream lands either option in [#59844](https://github.com/anthropics/claude-code/issues/59844)
(dropping the `!getIsNonInteractiveSession()` gate from the CLI, or this
same extension splice as the fallback), Patch L can be retired.

**Upstream issue**: [#59844](https://github.com/anthropics/claude-code/issues/59844) (fix proposal); it closes the gap described by [#49902](https://github.com/anthropics/claude-code/issues/49902) / [#49322](https://github.com/anthropics/claude-code/issues/49322) / [#49268](https://github.com/anthropics/claude-code/issues/49268) / [#8477](https://github.com/anthropics/claude-code/issues/8477) and several more.

## Verifying and debugging the recovery markers

The general CDP method (mental model, tooling, the BP/RPC/fiber recipes, the
refresh mechanisms, and the universal gotchas) lives in [`debugging.md`](debugging.md).
This section is the claude-patches-specific part: fixtures that trigger the
Patch K markers, marker-specific gotchas, and worked diagnoses. Where the text
below says "Recipe N", "mechanism N", "Step N", or "this doc", it means
`debugging.md`.

### Synthetic demo sessions: when no real session triggers your marker

Some markers (seam, broken, ambiguous) only fire on rare chain topologies
(phantom-lpu compaction, dangling root, multi-sibling backfill) that may not be
present in any of your real sessions. Rather than hunt, CRAFT a minimal `.jsonl`
that triggers exactly the case you want. The "PFGK DEMO seam" / "PFGK DEMO
broken" tabs were made this way.

Two hard requirements learned empirically:

1. **The file must live in the project dir of an OPEN workspace folder.** The
   conversations panel lists sessions via the extension's SessionStore, scoped to
   the folders in the currently open workspace; it does NOT live-scan arbitrary
   dirs, and it is NOT enough for the dir to be anywhere under `~/.claude/projects`.
   A `.jsonl` dropped into a brand-new project dir (`-tmp-foo/`, or a fresh
   `-home-juraj-foo/` for a folder you do not have open) will NOT appear, even
   after reload. Empirically: `/tmp`-rooted and a fresh `/home/juraj/pfgk-demo`
   both showed zero rows; only placing the file under the open workspace's project
   (e.g. with `~/CDVIEWER/PNGS` open, the dir
   `~/.claude/projects/-home-juraj-CDVIEWER-PNGS/`) surfaced it. The project-dir
   name is the workspace path with `/` replaced by `-`. Use a **unique** dangling
   / phantom uuid (`deadbeef-0000-4000-8000-0000000000NN`) so none of the real
   siblings in that dir accidentally resolve it and defuse the trigger.

2. **Reload the window to re-scan after writing the file.** The panel caches its
   list; a freshly written `.jsonl` only surfaces after `Developer: Reload
   Window` (Step 2). Then open it via the panel (Step 6a) for a fresh `Wz4` walk.

Generator: the maintained script is **`util/gen_demo.py`** (run it from inside the
open workspace folder so it targets that project dir, or set `PFG_DEMO_CWD` /
`PFG_DEMO_PROJ`). It writes one seam-triggering and one broken-triggering session;
records use the real on-disk JSONL shape, `ai-title` gives the panel a searchable
title, `last-prompt` marks the leaf. The same logic, condensed, inline:

```python
# Minimal inline copy; the maintained version is util/gen_demo.py. Seam + broken.
import json, uuid, os
def u(): return str(uuid.uuid4())
TS="2026-06-04T11:30:00.000Z"; VER="2.1.159"
CWD=os.environ.get("PFG_DEMO_CWD") or os.getcwd()                 # run from the OPEN workspace folder
PROJ_DIR=os.environ.get("PFG_DEMO_PROJ") or CWD.replace("/","-")  # Claude Code project-dir encoding
def user_rec(uid,parent,sid,text):
    return {"parentUuid":parent,"isSidechain":False,"type":"user","message":{"role":"user","content":text},
            "isMeta":False,"uuid":uid,"timestamp":TS,"cwd":CWD,"sessionId":sid,"version":VER,"gitBranch":""}
def asst_rec(uid,parent,sid,text):
    return {"parentUuid":parent,"isSidechain":False,"type":"assistant","uuid":uid,"timestamp":TS,"cwd":CWD,
            "sessionId":sid,"version":VER,"gitBranch":"","message":{"model":"claude-opus-4-8","id":"msg_"+uid.replace('-','')[:24],
            "type":"message","role":"assistant","content":[{"type":"text","text":text}],"stop_reason":"end_turn"}}
def title_rec(sid,t): return {"type":"ai-title","aiTitle":t,"sessionId":sid}
def leaf_rec(sid,leaf): return {"type":"last-prompt","leafUuid":leaf,"sessionId":sid}
def compact_rec(uid,lpu,sid,preserved):
    return {"parentUuid":None,"logicalParentUuid":lpu,"isSidechain":False,"type":"system","subtype":"compact_boundary",
            "content":"Conversation compacted","isMeta":False,"timestamp":TS,"uuid":uid,"level":"info",
            "compactMetadata":{"trigger":"auto","preTokens":900000,"preservedMessages":{"anchorUuid":preserved[0],"uuids":preserved}},"sessionId":sid}
def write(sid,recs):
    d=os.path.expanduser("~/.claude/projects/"+PROJ_DIR); os.makedirs(d,exist_ok=True)
    p=os.path.join(d,sid+".jsonl")
    with open(p,"w") as f:
        for r in recs: f.write(json.dumps(r)+"\n")
    return p

# BROKEN: root parentUuid dangles at a unique missing uuid (isolated => unresolvable => INCOMPLETE TRANSCRIPT)
sb=u(); DANGLING="deadbeef-0000-4000-8000-000000000001"; b1,b2,b3,b4=u(),u(),u(),u()
broken=[title_rec(sb,"PFGK DEMO broken: incomplete-transcript marker (synthetic)"),
  user_rec(b1,DANGLING,sb,"PFGK DEMO (broken). Root points at a missing upstream message."),
  asst_rec(b2,b1,sb,"Acknowledged."), user_rec(b3,b2,sb,"Continue."), asst_rec(b4,b3,sb,"Continuing."),
  leaf_rec(sb,b4)]
# SEAM: compact_boundary whose logicalParentUuid is a unique missing phantom (Loop A reattaches in-file)
ss=u(); PHANTOM="deadbeef-0000-4000-8000-000000000002"; s1,s2,scb,s3,s4=u(),u(),u(),u(),u()
seam=[title_rec(ss,"PFGK DEMO seam: in-file phantom reattach marker (synthetic)"),
  user_rec(s1,None,ss,"PFGK DEMO (seam). Compaction predecessor was never persisted."),
  asst_rec(s2,s1,ss,"Acknowledged."), compact_rec(scb,PHANTOM,ss,[s1,s2]),
  user_rec(s3,scb,ss,"Continue after the compaction."), asst_rec(s4,s3,ss,"Continuing."),
  leaf_rec(ss,s4)]
print("BROKEN:",sb,"->",write(sb,broken)); print("SEAM:  ",ss,"->",write(ss,seam))
```

```sh
python3 util/gen_demo.py | tee /tmp/demo_sids.txt   # run from the workspace; note the two sessionIds
# Reload (Step 2), then open via the panel (Step 6a, search "PFGK DEMO"), verify (Step 7).
```

Pitfalls when crafting or reusing a fixture:

- The seam fixture plants `dg:"seam"` only while its phantom `logicalParentUuid`
  (a uuid no record in the dir defines) stays unresolved. If a sibling defines it
  (a prior run, a clone), the crossing renders `dg:"seamClean"` or `dg:"bridge"`
  instead, a DIFFERENT marker. Clear leftover demo files before generating.
- A same-titled inert stub left in the dir makes `click_convo` (Step 6a) open the
  WRONG row. Move it aside, or give your fixture a unique title, so the search
  matches exactly one session.
- A fixture with zero message records (an `ai-title`+`mode` stub and nothing else)
  plants no markers: the walker has no boundary or chain to fire on. It must
  contain the actual messages, not just a title.
- Opening a session is not idempotent on disk. Base Claude Code (the `claude`
  subprocess, via `generate_session_title` with `persist`, NOT our patches)
  appends a compact-JSON `ai-title` record to the session's own `.jsonl` on open,
  so a 7-line seam fixture becomes 8 lines after one open and re-opening keeps
  adding duplicate `ai-title` lines (harmless to the render, the file just grows).
  Verify markers from the rendered DOM (Step 7) or the wire `H` of a fresh walk,
  NEVER by re-reading or diffing the fixture file after opening it. The `ai-title`
  is also what the panel lists and searches, so the fixture needs one (gen_demo
  writes it); real seam sessions often have none and cannot be opened by panel
  title-search.

Cleanup when done (delete by the exact sessionIds, leaving the real siblings
untouched), then reload once to drop the stale panel rows:

```sh
DIR="$HOME/.claude/projects/$(pwd | tr / -)"   # run from the same workspace folder
rm -f "$DIR/<broken-sid>.jsonl" "$DIR/<seam-sid>.jsonl"
grep -l "PFGK DEMO" "$DIR"/*.jsonl 2>/dev/null | wc -l   # expect 0
```

(For inducing edge cases on an EXISTING real session instead of crafting one
from scratch, see the "Patch K verification recipes" gotcha:
clone-a-sibling for ambiguity, rename-the-source-away for reconstruction
failure.)


### Byte-stability check is necessary but not sufficient for splice correctness

`util/build-prebuilt.py` validates the synthesized splice by re-applying
it to a fresh copy of `.bak` and confirming the result is byte-identical
to the live patched file. **This proves the splice is deterministic; it
does not prove the splice is correct.**

If `.bak` itself isn't pristine (e.g., the patch was iteratively
developed in place and `.bak` was captured *between* iterations), the
splice synthesis only captures the diff between *the latest .bak state*
and *the current live state*, missing transformations that were
introduced by an earlier patch iteration and are now equally present
in both `.bak` and live.

Concrete instance: Patch K's webview wrap was developed iteratively
across v1.2 → v1.3 → v1.4 in the 2.1.126 install. The `let _ws=`
prefix on the `createElement(GR0,...)` call was introduced in an
earlier K iteration (v1.2 or v1.3). By the time the v1.4 prebuilt was
synthesized, `.bak` already had `let _ws=` (because no one re-baked
from pristine after the iteration). The captured splice therefore
covered only the v1.3→v1.4 wrap-internal change, NOT the pristine
`return createElement(...)` → `let _ws=createElement(...)`
transformation that was needed for the wrap to do anything at all.

Translating that splice to a fresh bundle (e.g. 2.1.132, where `.bak`
IS pristine) applied cleanly via grep, byte-stability passed, but the
result was dead code: the K wrap branch lived after a `return
createElement(...)` statement and never executed.

Mitigations:
- **Treat `.bak` as the pristine pre-patch baseline, always.** Never
  overwrite it once it exists, for end-users and maintainers alike.
  This invariant makes `build-prebuilt.py`'s `live - .bak` diff
  always capture the full pristine→patched transformation, not an
  incremental hop.
- **Per-patch checkpoints are named after the settled patch they
  contain** (`.patchX.bak` = "settled state of patch X"), not after
  the next patch in line. Self-documenting; doesn't require knowing
  the patch ordering to interpret.
- If `.bak` ever drifted off pristine, recover by reinstalling the
  extension from scratch and re-baking from clean before any further
  patch work. See [`MAINTAINER.md`](../MAINTAINER.md) for the
  detailed invariant + recovery procedure.
- The user-visible verification (DOM probe for `.pfgkAlert` after
  Reload Window) catches this regardless; the byte-stability check
  alone does not.


### Patch K verification recipes (clone-to-induce-non-uniqueness, rename-to-induce-breakage)

For testing K's edge cases (v1.5+ ambiguity warning + reconstruction-
failed marker), the JSONL filesystem is the test fixture:

**Test #1, induce sibling-backfill non-uniqueness** (verifies the
`AMBIGUOUS RECONSTRUCTION` warning fires on the bookend + relevant
seam markers):

```sh
PROJ=~/.claude/projects/-<workspace>
# Pick a session you know is the K1-backfill source for the target.
# Clone it to a new uuid filename; same content, structurally
# qualifies for backfill, makes count > 1.
cp $PROJ/<source-sid>.jsonl $PROJ/<source-sid>-clone-aaaa-bbbb-cccccccccccc.jsonl
# Reload Window via palette → re-fetch chain walker for target session
# DOM-verify [data-pfgk-role="bookend"] count = 1, bodyHasAMBIG === true
# Cleanup: rm the clone
```

**Test #5, induce reconstruction failure** (verifies the
`pfgk-broken-` marker variant fires with critical styling):

```sh
PROJ=~/.claude/projects/-<workspace>
# Rename the K1-backfill source so K1 finds no qualifying sibling.
# Suffix with .test-disabled (also rename .bak to keep the pair).
mv $PROJ/<source-sid>.jsonl     $PROJ/<source-sid>.jsonl.test-disabled
mv $PROJ/<source-sid>.jsonl.bak $PROJ/<source-sid>.jsonl.test-disabled.bak
# Reload Window via palette
# DOM-verify [data-pfgk-role="broken"] count = 1, bodyHasINCOMPLETE === true
# Cleanup: rename back
```

Both tests target the SAME session (the one whose chain walker
output you're inspecting). The fixture changes its sibling
environment, not its own content.

**End-to-end DOM verification template** (used after each fixture
change + reload):

```js
JSON.stringify({
  pfgkAlert: document.querySelectorAll('.pfgkAlert').length,
  bookend:  document.querySelectorAll('[data-pfgk-role="bookend"]').length,
  seam:     document.querySelectorAll('[data-pfgk-role="seam"]').length,
  bridge:   document.querySelectorAll('[data-pfgk-role="bridge"]').length,
  broken:   document.querySelectorAll('[data-pfgk-role="broken"]').length,
  bodyHasAMBIG:      document.body.textContent.includes('AMBIGUOUS RECONSTRUCTION'),
  bodyHasUNRECOVERABLE: document.body.textContent.includes('UNRECOVERABLE'),
  msgs: document.querySelectorAll('[class*=message_]').length,
})
```

Predictions:
- Baseline (no fixture): 4 markers (1 bookend + 2 seams + 1 bridge),
  0 broken, no AMBIG, no INCOMPLETE.
- Test #1 (clone present): SAME marker counts but `bodyHasAMBIG ===
  true`, bookend + at least one seam contain "AMBIGUOUS
  RECONSTRUCTION:" prefix.
- Test #5 (source renamed away): `bookend` count = 0, `broken`
  count = 1, `bodyHasINCOMPLETE === true`. Total `msgs` drops by
  ~size of the missing source's pre-content.

### "K detected" vs "K succeeded": gate downstream logic on attempt, not effect

A subtle K design lesson learned the hard way: when downstream
logic (bookend planting, broken-marker fallback) needs to react to
the *presence* of a problem, gate on whether K *detected* the
problem, not whether K *fixed* it.

Concrete instance from v1.5 → v1.6: the post-K "plant bookend or
broken marker" block was gated on `if(_kFired)`, where `_kFired`
became true only when K2 successfully planted a seam. But K2 has
its own preconditions (needs an in-file predecessor in `_parsed`
to anchor the seam to). For a session whose chain begins with a
phantom-lpu compaction boundary at index 0 of `_parsed` after J's
prepend (i.e., the boundary is at the very start), K2 has nothing
to anchor to and `continue`s; `_kFired` stays false. The bookend/
broken/bridge block is then skipped entirely and the rendered chain
shows **zero markers despite having phantom-lpu data loss**: silent
failure, exactly the bug the broken marker was supposed to prevent.

Fix: introduce a separate `_kAttempted` flag set whenever K detects
a phantom-lpu boundary it would WANT to fix (regardless of whether
the seam plant succeeded). Gate downstream "plant a marker"
fallback on `_kAttempted`. The bookend(b) → broken-marker predicate
then fires correctly even when K2 couldn't anchor a seam.

Rule of thumb: any K stage that has multiple preconditions for
side-effect success (plant a ghost) should also set a "detection"
flag with weaker preconditions (just "saw the problem"). The
detection flag is what user-facing fallback markers gate on.

### Marker informativeness: surface concrete data, not prose

When designing user-facing diagnostic markers (bookend, seam, bridge,
broken, or any analogous synthesized message), include the concrete
data the marker is acting on. Generic prose ("a compaction event
happened here") is far less useful than the specific identifiers
involved:

- **Phantom uuid** that was unresolvable (full uuid, not truncated;
  users may grep for it across files).
- **Sibling filename** that K1 backfilled from (so user can correlate
  with their own session list / fork structure).
- **Predecessor uuid** the seam stitched to (for chain-walking
  verification).
- **Counts** (number of qualifying siblings, msgs prepended,
  phantoms attempted vs backfilled).
- **Wall-clock timing** of K stages (parse / J prepend / K1 / K2-3-
  bookend): surfaces perf hotspots and helps the user judge whether
  slow chain rendering is K's fault or downstream React's.

Don't truncate uuids in marker text just for display compactness;
the value of full uuids for cross-referencing exceeds the cost of
a slightly longer string. The truncation/collapse `<style>` already
in the wrap handles overflow visually.

K v1.5 marker text was prose-heavy with truncated uuid prefixes;
v1.6 added per-marker data tracking + full uuids + wall-clock
timing. The v1.6 markers are usable as standalone diagnostic
artifacts (paste into a bug report, the data is all there); v1.5
markers required the user to also dump the chain walker output
elsewhere.

### Red-on-red (and other role-specific bg color clashes)

Webview render wrap that uses the SAME header text/border color
(`color:_bd, borderBottom:"2px dashed "+_bd`) across all marker
roles works fine when bg colors are subtle (rgba alpha 0.18-0.20),
because the dark border-color stands out enough on the lighter bg.

But when a role bumps bg to higher saturation (broken role: `rgba(180,
0,0,0.50)`) AND the same role uses a dark border color (`#990000`),
the header text rendered in `color:_bd = #990000` becomes
near-invisible on the dark-red bg. Same can happen for any role
whose bg is high-saturation and bd matches the bg's hue.

Fix: detect the high-saturation role in the header style and
override `color:` (and `borderBottom:`) to a contrasting value:

```js
color: _r==="broken" ? "#ffffff" : _bd,
borderBottom: "2px dashed " + (_r==="broken" ? "#ffffff" : _bd),
```

Verify empirically via DOM probe:

```js
JSON.stringify({
  bg: getComputedStyle(brokenEl).backgroundColor,
  header_color: getComputedStyle(brokenHeaderDiv).color,
})
```

Header color should be sufficiently distant in lab-distance from
bg color. White vs dark-red is fine; dark-red vs dark-red is not.

When adding any new role with elevated bg saturation, manually
verify header readability before considering the wrap change
shipped.

---

## Case study: empirically diagnosing Patch K's "seam not rendering"

This walks through using all the mechanics in `debugging.md` to diagnose a real
bug. Patch K (lost+found recovery for sessions with phantom
`logicalParentUuid` after compaction) was suspected of failing on some
sessions: the seam ghost it inserts wasn't appearing in the rendered
chat panel for one specific session, while it appeared correctly for
another. This case study shows the introspection that confirmed the
structural cause empirically.

### Symptom

User opens two chat panels in the same Antigravity window:

- Session **A** (`0727164e-...`): Patch K's seam ghost renders
  correctly in the panel.
- Session **B** (`61974011-...`): Patch K's seam ghost is absent
  from the panel.

Both sessions have the same patches loaded (signature `/*pfg-v1.4*/`
present in `extension.js`). Both panels are in the same exthost (same
window). What's different?

### Step 1: precheck patches are loaded

```sh
# Path stem was ~/.antigravity/ at the time; current installs use ~/.antigravity-ide/.
node /tmp/cdp-eval.mjs "$WS_TARGET" '(function(){
  var s = require("fs").readFileSync(
    "/home/juraj/.antigravity/extensions/anthropic.claude-code-2.1.126-linux-x64/extension.js","utf-8");
  return JSON.stringify({sig: ["/*pfg-v1.4*/"].filter(t=>s.includes(t)),
                         hasK: s.includes("Orphaned compaction pointer")});
})()'
// {"sig":["/*pfg-v1.4*/"], "hasK":true}: patches are live
```

### Step 2: simulate Ez4 on the broken session's JSONL outside the running extension

To verify Patch K is *supposed* to fire, replicate the loader chain
against Session B's JSONL on disk:

```js
// Read 61974011's JSONL, parse, walk for compact_boundaries with
// phantom lpu, simulate K's logic.
```

Result: simulator reports K should produce 1 seam ghost in Session B.
So K is firing logically, but the rendered messages don't contain
it. Discrepancy is downstream of K.

### Step 3: BP at Ez4 entry and capture all locals

Use Recipe 1 (BP-with-side-effect-condition) to capture
`V`/`U`/`Z`/`H` at Ez4's RETURN site:

```js
// /tmp/bp_ez4_capture.mjs
condition: `((function(){
  try {
    (globalThis.__caps = globalThis.__caps || []).push({
      ts: Date.now(),
      Vlen: V.length,
      U: U.slice(0,40).map(t => ({u: String(t.uuid).slice(0,12), i: B.get(t.uuid), t: t.type})),
      Zuuid: Z ? String(Z.uuid).slice(0,12) : null,
      Zidx: Z ? B.get(Z.uuid) : null,
      Hlen: H.length,
      pfgkInH: H.filter(m => String(m.uuid).startsWith('pfgk-')).map(m => String(m.uuid).slice(0,20)),
      pfgkInV: V.filter(m => String(m.uuid).startsWith('pfgk-')).map(m => String(m.uuid).slice(0,20)),
    });
  } catch(e) { (globalThis.__capsErr = globalThis.__capsErr || []).push(String(e)); }
  return false;
})())`
```

### Step 4: trigger via webview RPC (Recipe 2)

Use the bash orchestration pattern: BP script in background, post
`get_session_request` for Session B in foreground, read `__caps` after.

```sh
node /tmp/bp_ez4_capture.mjs "$WS_TARGET" 25 > /tmp/cap.log 2>&1 &
sleep 8
node /tmp/cdp-eval.mjs "$WS_RENDERER_PAGE" \
  'window.__vscode_post_message__("onmessage", {
    message: {type:"request", channelId:"probe", requestId:"r1",
              request:{type:"get_session_request", sessionId:"61974011-..."}},
    transfer: undefined});
   "posted"'
sleep 2
node /tmp/cdp-eval.mjs "$WS_TARGET" 'JSON.stringify(globalThis.__caps||[])'
wait
```

### Step 5: read the result

```json
{
  "Vlen": 5060,
  "U": [
    {"u": "pfgk-seam-32", "i": 2375, "t": "user"},
    {"u": "d2558225-074", "i": 4750, "t": "user"},
    {"u": "5fda77a3-9d3", "i": 4772, "t": "user"},
    {"u": "9fc43c52-903", "i": 5059, "t": "assistant"}
  ],
  "Zuuid": "9fc43c52-903",
  "Zidx": 5059,
  "Hlen": 2683,
  "pfgkInV": ["pfgk-seam-32ff69a5"],
  "pfgkInH": []
}
```

### Diagnosis (empirically confirmed)

- `pfgkInV: ["pfgk-seam-32ff69a5"]`: the seam IS in the input. K
  inserted it correctly.
- `U` has 4 leaf-walk tips, INCLUDING `pfgk-seam-32` at idx 2375.
- `Zuuid: 9fc43c52-903` at idx **5059**: Ez4 picks Z by `max(B.get(t.uuid))`;
  the chain-B leaf at 5059 wins over the seam at 2375.
- `pfgkInH: []`: the rendered chain (walked back from Z at 5059)
  never crosses the seam. Seam is unreachable.

The bug isn't in K's insertion. It's in Ez4's single-leaf max-by-index
walker: when a session has TWO disjoint chains in the file (orphan +
live), the walker picks the live-chain leaf and ignores the orphan.
K's seam, planted on the orphan chain, becomes topologically invisible.

Session A (`0727164e-...`) doesn't trigger the asymmetry because its
second compaction's `lpu` resolves SAME-FILE (a `local_command stderr`
uuid captured by `compact.ts:598`), keeping the live chain stitched
through the orphan; Session B's second-compaction `lpu` resolves
CROSS-FILE (a `tool_result` uuid in a sibling JSONL), bypassing the
orphan entirely.

### Mitigation (added in pfg-v1.3, later subsumed by the bridge mechanism in v1.4)

Once the empirical diagnosis was nailed, the fix was straightforward:
detect the unreachable scenario at K-time (signal: a seam was planted
but no bookend was; bookend fails to fire when the chain root is the
sibling-file content Patch J prepended), and synthesize a **third
ghost type**, `pfgk-orphannotice-…`, on the LIVE chain. Insert it
between the cross-file-resolved boundary and that boundary's first
child, so the walker traverses through it on the way back from Z.
The seam stays planted on the orphan chain (semantically correct for
that branch); the orphannotice provides the user-facing signal in the
live chain. Render wrapper colours it amber.

End-to-end verification of the fix used the same recipes in `debugging.md`:
disk-edit `extension.js` + `webview/index.js` (mechanism 5 from the
refresh playbook), `Input.dispatchKeyEvent` for "Reload Window"
(mechanism 3), then BP-free verification via fiber-walk to the manager
`__mgr.activeSession.value.messages.peek()`, filtering for
`pfgk-orphannotice-` prefix uuids and DOM probing for the
`.pfgk-orphannotice` wrapper class.

### What this case study demonstrates

Each recipe in `debugging.md` was used:

- Mental model (mine/target/renderer) to pick the right ports
- Precheck to confirm patches are live
- Recipe 1 (BP-with-side-effect-condition) to capture Ez4's locals
- Recipe 2 (RPC trigger) to invoke `Wz4` → `Ez4` without UI
- Bash orchestration to hold the BP across trigger+read
- Field-shape knowledge (camelCase locals at Ez4) to read fields right

Without the BP-with-side-effect-condition recipe this would have been
hard to confirm empirically; `setScriptSource` injection of the same
trace failed silently, per the gotcha. The mechanic generalises beyond
Patch K. It's the answer for any "rendered messages don't match
on-disk JSONL" question.

---

## Case study: dead K wrap from non-pristine `.bak` synthesis (2.1.132)

A second case study, demonstrating how a prebuilt can pass byte-stability
checks while shipping dead code, and the empirical workflow to diagnose +
recover.

### Symptom

Antigravity upgrades to `2.1.132`. `/patch-claude` runs the manual-fallback
path (no prebuilt exists for this version yet). Maintainer translates the
v1.4 K splices from the `2.1.126` prebuilt with bundle-var renames
(`XR0` → `GR0` etc.), `node --check` passes, `build-prebuilt.py`
synthesizes a new prebuilt for `2.1.132`, byte-stability check passes,
prebuilt pushed.

User reports K rendering is broken: "ancient version of Patch K. Where
is the circular navigation?"

### Step 1: precheck patches are loaded

```sh
WS=$(curl -s http://127.0.0.1:9229/json | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["webSocketDebuggerUrl"])')
# Path stem was ~/.antigravity/ at the time; current installs use ~/.antigravity-ide/.
node /tmp/cdp-eval.mjs "$WS" '(function(){
  var s = require("fs").readFileSync(
    "/home/juraj/.antigravity/extensions/anthropic.claude-code-2.1.132-linux-x64/extension.js","utf-8");
  return JSON.stringify({sigs: ["/*pfg-v1.4*/"].filter(t=>s.includes(t)),
                         hasK: s.includes("_kFired"), hasBridge: s.includes("pfgk-bridge-")});
})()'
// {"sigs":["/*pfg-v1.4*/"], "hasK":true, "hasBridge":true}: extension.js patch is live.
```

### Step 2: probe the rendered DOM in the chat panel

Find the chat panel's webview iframe target via `/json/list` (filter for
`type:"iframe"` whose parent page is the target window's). Then drill into
the inner `active-frame` and probe for K wrap markers:

```sh
WS_CHAT="ws://127.0.0.1:9222/devtools/page/<chat-iframe-id>"
node /tmp/eval_in_inner_frame.mjs "$WS_CHAT" 'JSON.stringify({
  pfgkAlert: document.querySelectorAll(".pfgkAlert").length,
  bookend: document.querySelectorAll("[data-pfgk-role=\"bookend\"]").length,
  hasPatchKMarker: document.body.textContent.includes("PATCH K · ")
})'
// {"pfgkAlert":0,"bookend":0,"hasPatchKMarker":true}
```

The K message text is in the DOM (`PATCH K · Conversation origin...`)
but `pfgkAlert: 0`: the wrap div isn't being rendered. The K bookend
is showing as a plain user message bubble.

### Step 3: walk DOM up from the K text node to identify where the wrap *should* be

```sh
node /tmp/eval_in_inner_frame.mjs "$WS_CHAT" '(function(){
  var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  var n;
  while (n = w.nextNode()) if (n.nodeValue.includes("PATCH K · Conversation")) break;
  if (!n) return "not found";
  var path = []; var c = n.parentElement;
  for (var i=0; i<10 && c; i++) { path.push({tag: c.tagName, cls: (c.className||"").slice(0,80)}); c = c.parentElement; }
  return JSON.stringify(path);
})()'
```

Result shows `SPAN → DIV.content_xGDvVg → DIV.contentWrapper_xGDvVg → DIV.userMessage_07S1Yg → ...`: the K message is inside a normal
`userMessage` bubble, with NO `pfgkAlert` parent. The wrap React node
that's supposed to wrap the user-message createElement isn't there.

### Step 4: inspect the patched `webview/index.js` source

```sh
grep -A 1 'pfgkAlert' "$EXT/webview/index.js" | head
```

The wrap code IS present in the source:

```js
if(Z.type==="user"){
  if(Z.parentToolUseId)return null;
  if(Z.isSynthetic)return null;
  return n1.default.createElement(GR0,{...});       // ← returns immediately
  if(typeof Z.uuid==="string"){...; _ws=createElement("div", ..., _ws); ...}
  return _ws                                          // ← UNREACHABLE
}
```

The `return n1.default.createElement(GR0,...)` makes everything after it
dead code. The wrap branch never runs.

### Step 5: diagnose the synthesis pitfall

The `2.1.126` prebuilt's K webview splice OLD anchor was
`G,setInputError:q,onCreateNewSession:z})}if(Z.type==="assistant"...`
(80 chars). The corresponding NEW string changed `})}` to
`});<wrap code>; return _ws}`. For this to produce a wrapped result,
the createElement call must NOT be preceded by `return `; instead
preceded by `let _ws=`.

**In `2.1.126`**, the patch was developed iteratively (v1.2 → v1.3 → v1.4).
By the time the v1.4 prebuilt was synthesized, an earlier iteration had
already transformed `return createElement(...)` to `let _ws=createElement(...)`.
That transformation was equally present in `.bak` (post-iteration) and
live (post-iteration), so `build-prebuilt.py`'s diff didn't capture it.
The synthesized splice covered only the wrap-internal v1.3→v1.4 changes.
Byte-stability passed because re-applying the splice to that `.bak`
reproduces live exactly, but the .bak wasn't pristine.

**In `2.1.132`** (fresh install, `.bak` IS pristine `return createElement(...)`),
the splice's grep anchor matched, `node --check` passed, byte-stability
passed, but the result was dead code.

### Step 6: fix and re-synthesize

Add an explicit `return → let _ws=` transformation as a separate splice:

```py
old = "return n1.default.createElement(GR0,{session:$,...,onCreateNewSession:z});"
new = "let _ws=n1.default.createElement(GR0,{session:$,...,onCreateNewSession:z});"
```

After applying, `build-prebuilt.py` synthesis now captures 4
`webview/index.js` splices (was 3): the new transformation + the original
3. Byte-stability passes (deterministic against the new pristine `.bak`).

### Step 7: reload via mechanism 3 and verify in DOM

This is the recovery + verify path. The maintainer first improvised
with `location.reload()` on the iframe (DON'T; see gotcha) which left
the iframe in `chrome-error://chromewebdata/`. Then `Page.reload` on
the iframe target (DON'T; rejected as not top-level). Then command
palette typing failed because focus was held by the broken iframe.

The recovery path that actually works:

```js
// 1. Body-area mouse click to take focus from the broken iframe
await call('Input.dispatchMouseEvent', {type:'mousePressed', x:5, y:5, button:'left', clickCount:1});
await call('Input.dispatchMouseEvent', {type:'mouseReleased', x:5, y:5, button:'left', clickCount:1});

// 2. Verify document.activeElement is now BODY, not IFRAME
//    (skip this and you'll waste turns wondering why typing doesn't reach the palette)

// 3. Open command palette: Ctrl+Shift+P sets the ">" command-mode prefix
await call('Input.dispatchKeyEvent', {type:'rawKeyDown', modifiers:10, key:'P', code:'KeyP', keyCode:80, windowsVirtualKeyCode:80});
await call('Input.dispatchKeyEvent', {type:'keyUp', modifiers:10, key:'P', code:'KeyP', keyCode:80, windowsVirtualKeyCode:80});

// 4. CRITICAL: do NOT clear the input. Clearing drops the ">" prefix and
//    puts the palette into file-picker mode, where Enter opens a file
//    instead of running a command. Type APPEND-style.

// 5. Type "Reload Window" via char events
for (const ch of "Reload Window") {
  await call('Input.dispatchKeyEvent', {type:'char', text:ch, unmodifiedText:ch});
}

// 6. Verify firstSugg matches "Developer: Reload Window..."

// 7. Enter, wait ~10s for reload
await call('Input.dispatchKeyEvent', {type:'rawKeyDown', key:'Enter', code:'Enter', keyCode:13, windowsVirtualKeyCode:13});
await call('Input.dispatchKeyEvent', {type:'keyUp', key:'Enter', code:'Enter', keyCode:13, windowsVirtualKeyCode:13});
```

After reload, re-discover the new chat-panel iframe target (the ID
changes on reload), and re-probe:

```sh
WS_CHAT_NEW="ws://127.0.0.1:9222/devtools/page/<new-iframe-id>"
node /tmp/eval_in_inner_frame.mjs "$WS_CHAT_NEW" 'JSON.stringify({
  pfgkAlert: document.querySelectorAll(".pfgkAlert").length,
  bookend: document.querySelectorAll("[data-pfgk-role=\"bookend\"]").length,
  seam: document.querySelectorAll("[data-pfgk-role=\"seam\"]").length,
  bridge: document.querySelectorAll("[data-pfgk-role=\"bridge\"]").length
})'
// {"pfgkAlert":4,"bookend":1,"seam":2,"bridge":1}: fix verified end-to-end.
```

### What this case study demonstrates

- **Byte-stability ≠ correctness**. The check validates determinism
  against the .bak you have, not behavioral correctness against a
  pristine .bak. (Gotcha.)
- **`.bak` is the pristine pre-patch baseline; never overwrite it.**
  Per-patch checkpoints are `.patchX.bak` (= settled state of patch
  X), named after the patch they contain rather than the next patch
  in line. This invariant makes the diff-based prebuilt synthesis
  correct by construction. (MAINTAINER rule.)
- **DOM probing is the user-visible verification.** `node --check` and
  byte-stability are necessary but not sufficient.
- **Don't improvise reload mechanisms.** `location.reload()` on a
  webview iframe leaves it in chrome-error; `Page.reload` is rejected
  on iframe targets. Use mechanism 3 (Reload Window via key events) or
  mechanism 5 (disk + Reload Window). (Gotcha.)
- **Focus-stuck-to-iframe blocks command palette typing.** Body-area
  mouse click first; verify `document.activeElement` before typing.
  (Gotcha.)
- **Don't clear the `>` prefix when the palette is in command mode.**
  Append, don't replace. (Pitfall.)

The first three points are the substantive lessons; the last three are
recovery paths from cowboying-when-the-playbook-already-said-not-to.
