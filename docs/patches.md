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

### Locate

In `extension.js`, find the `Wz4` (or its drift-renamed equivalent)
loader function. It's where Patch J's fixed-point loop terminates.
The K block goes between J's loop tail (`if(_newPrepend.length===0)
break;_parsed=[..._newPrepend,..._parsed];}`) and the trailing
`return dl(_parsed,K)` call.

In `webview/index.js`, find the user-message render path: the only
`Z.type==="user"` branch that creates `XR0` after the
`parentToolUseId`/`isSynthetic` early-return guards. Wrap the
returned element with a colored container when `Z.uuid` starts with
`pfgk-`.

### Patch (extension.js)

See SKILL.md Step 13 for the full splice. The K block has four
synthesis steps (see "Why" above). Three ghost types are emitted
into `_parsed`: `pfgk-bookend-…` (red, chain root), `pfgk-seam-…`
(orange, in-file orphan reattachment at phantom-lpu boundary),
`pfgk-bridge-…` (orange-red, redirection from cross-file shortcut
back into the in-file orphan).

### Patch (webview/index.js)

Wrap the user-message bubble with a colored `<div>` (red bookend,
orange seam, orange-red bridge) and inject a header bar showing
`MARKER N OF M · CLICK FOR NEXT ↓` (or `· CYCLE TO TOP ↺` for the
last marker) computed from the session's `messages.peek()`. Click
handler cycles to the next marker in document order via
`document.querySelectorAll("[data-pfgk-role]")`. Inject a `<style>`
inside the wrapper that suppresses the truncation gradient, the
"Show more / Show less" collapse buttons, and the edit/fork action
button, none of which make sense on a synthetic message. The visual
intensity (loud color stripe, large ⚠️, dashed border under header,
all-caps position counter in role color) is intentional: data-
corruption events deserve attention-grabbing markers, not muted ones.

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
- **The renderer treats user-message content as plain text** (no
  markdown, no inline HTML). The visual punch comes from the wrapper
  + emoji + colored bg, not from anything inside the message body.

### Test

Open a session in a conversation family with at least one phantom-lpu
boundary. The chat panel should:

1. Show a red **bookend** at the very top with header `MARKER 1 OF N · CLICK FOR NEXT ↓`,
   followed immediately by the conversation's true first user message
   (the canonical origin recovered via cross-conversation backfill).
2. Show an orange **seam** at each compaction event whose lpu was
   phantom (in-file orphan reattachment).
3. Show an orange-red **bridge** at each compaction event whose lpu
   was resolved cross-file by Patch J (the in-file orphan was kept,
   not bypassed).
4. The last marker's header reads `· CYCLE TO TOP ↺`.
5. Clicking any marker cycles to the next in document order, wrapping
   from the last back to the bookend.

For the canonical test, open the most-compaction-impacted session in
the family (e.g. one whose JSONL starts with a `compact_boundary` at
line 1) and verify that the original user prompt at the top matches
what appears at the top of any other session in the same family. The
backfill should produce identical recovered origins across the tree.

### Background: walker constraints + recovery topology

The renderer's chain walker (`rO4` in 2.1.132, formerly `Ez4` in
2.1.126) is **single-leaf, max-by-_parsed-index, traversing
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
