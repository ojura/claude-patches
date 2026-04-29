---
name: patch-claude
description: Reapply Juraj's seven local patches to a newly updated anthropic.claude-code VS Code extension. Auto-detects which IDE-hosted install is the running one via CLAUDE_CODE_EXECPATH. Use when the user says "the extension updated, reapply patches" or similar. Backs up files, reapplies all seven patches, verifies each one.
---

# Reapply anthropic.claude-code extension patches

Seven patches live out-of-tree and need to be reapplied every time the bundled
`anthropic.claude-code-*-linux-x64` extension updates. The minified code
changes variable names between releases, so do NOT blindly search-and-replace
literal strings from prior versions — locate the pattern structurally, then
edit.

## Step 0 — boot: self-update, locate install, try the prebuilt

This single bash block does three things in one round-trip:

1. **Self-update** the skill if it's a symlinked clone of
   `ojura/claude-patches` (fast-forward only; aborts on dirty / non-FF
   state with a helpful message).
2. **Locate the target install** via `CLAUDE_CODE_EXECPATH` (the IDE-
   hosted Claude Code CLI sets this directly to the running install)
   or a fallback glob across `~/.<ide>/extensions/` for any IDE that
   pulls from Open VSX (VS Code, Antigravity, Cursor, VSCodium, etc.).
3. **Fetch the prebuilt for that version** and run it. Prebuilts are
   self-validating (detect `pfg-v1` signature, idempotent, byte-stable
   verified at synthesis time) — so this step either applies the
   patches cleanly OR no-ops because they're already applied.

```sh
set -u

# --- Step 0a: self-update via fast-forward, if symlinked-clone setup ---
SKILL_DIR=$(readlink -f ~/.claude/skills/patch-claude 2>/dev/null || true)
REPO_ROOT=
if [ -n "$SKILL_DIR" ]; then
  REPO_ROOT="$(git -C "$SKILL_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
fi
if [ -n "$REPO_ROOT" ]; then
  echo "Self-update: fetching $REPO_ROOT..."
  git -C "$REPO_ROOT" fetch --quiet origin
  HEAD_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  ORIGIN_SHA="$(git -C "$REPO_ROOT" rev-parse origin/main)"
  if [ "$HEAD_SHA" != "$ORIGIN_SHA" ]; then
    if git -C "$REPO_ROOT" merge-base --is-ancestor HEAD origin/main; then
      if git -C "$REPO_ROOT" diff --quiet HEAD && git -C "$REPO_ROOT" diff --cached --quiet; then
        git -C "$REPO_ROOT" merge --ff-only --quiet origin/main
        echo "  fast-forwarded to $(git -C "$REPO_ROOT" rev-parse --short HEAD)"
        echo "RESTART_SKILL: re-read SKILL.md from disk to pick up changes."
        exit 0
      else
        echo "ABORT: clone has uncommitted local changes; resolve before retrying."
        exit 1
      fi
    else
      echo "ABORT: local clone has commits not in origin/main (would require non-FF merge)."
      echo "Resolve manually: cd $REPO_ROOT && git status; rebase or push first."
      exit 1
    fi
  else
    echo "  already up to date with origin/main"
  fi
else
  echo "Self-update skipped: skill is not a symlinked git clone."
  echo "  (Optional) install via: rm -rf ~/.claude/skills/patch-claude;"
  echo "  git clone https://github.com/ojura/claude-patches ~/claude-patches;"
  echo "  ln -s ~/claude-patches/skill ~/.claude/skills/patch-claude"
fi

# --- Step 0b: locate the target install ---
# CLAUDE_CODE_EXECPATH is set to different things in different layouts:
#   - IDE-hosted: .../extensions/anthropic.claude-code-X.Y.Z-linux-x64/resources/native-binary/claude
#   - Standalone CLI: ~/.local/share/claude/versions/X.Y.Z (no extension to patch)
# Walk the path looking for an "anthropic.claude-code-*-linux-x64" component
# so we don't get fooled by the standalone layout into picking ~/.local/share.
EXT=
if [ -n "${CLAUDE_CODE_EXECPATH:-}" ]; then
  P="$CLAUDE_CODE_EXECPATH"
  while [ "$P" != "/" ] && [ "$P" != "." ] && [ -n "$P" ]; do
    case "$(basename "$P")" in
      anthropic.claude-code-*-linux-x64)
        if [ -f "$P/extension.js" ]; then EXT="$P"; fi
        break
        ;;
    esac
    P="$(dirname "$P")"
  done
fi
if [ -z "$EXT" ]; then
  EXT="$(ls -d ~/.*/extensions/anthropic.claude-code-*-linux-x64 2>/dev/null | sort -V | tail -1)"
fi
if [ -z "$EXT" ] || [ ! -f "$EXT/extension.js" ]; then
  echo "ABORT: could not locate the extension install. EXT='$EXT'"
  exit 1
fi
VER="$(basename "$EXT" | sed 's/^anthropic.claude-code-//; s/-linux-x64$//')"
echo "Target: $EXT (version $VER)"

# --- Step 0c: try the prebuilt (covers all patches A–G in one shot) ---
URL="https://raw.githubusercontent.com/ojura/claude-patches/main/prebuilt/$VER/apply.py"
if curl -fsSL -o /tmp/apply.py "$URL"; then
  echo "Prebuilt found for $VER — applying"
  python3 /tmp/apply.py "$EXT"
  echo "PATCHES_APPLIED: skill complete; reload VSCode."
  exit 0
else
  echo "No prebuilt for $VER — falling through to manual application (Steps 2–9 below)."
fi
```

**How to interpret the output**:

- `RESTART_SKILL` → stop and re-invoke the skill (the local clone got
  fast-forwarded; SKILL.md on disk is now newer than what you read).
- `PATCHES_APPLIED` → skill is complete. Tell the user to reload VSCode.
  Do NOT run any verification greps — they target manual-path splices
  with placeholder param names and will give false negatives on
  prebuilt-applied code. The `pfg-v1` signature embedded in
  `extension.js` is the authoritative check, and the prebuilt verifies
  it itself.
- `ABORT: ...` → stop and surface the message to the user; don't try
  to "fix" the abort condition automatically.
- `No prebuilt for $VER` → apply the patches manually as follows:
    1. **Patches A–E**: follow Steps 3–7 (per-splice manual application).
    2. **Patches F and G**: do NOT splice manually. Run
       `skill/apply-patch-fg.py` — it locates anchors via regex (so it
       handles variable-name drift across releases automatically) and
       embeds the `/*pfg-v1*/` signature comment that the prebuilt
       relies on for idempotency. Steps 8 and 9 below describe the
       splices structurally for reference only.
    3. If the script reports anchors not matching uniquely, the
       bundle structure has shifted enough to break its regexes.
       End-user fallback: apply F+G manually from Step 8 and Step 9,
       then **stop** — do not attempt the maintainer steps below.
       Tell the user a maintainer will need to update the script
       and publish a prebuilt for this version.

### Maintainer-only — synthesize and publish a prebuilt

The steps below assume push access to the upstream `claude-patches`
repo. If you don't have that, skip this section: you have patches
applied locally and that is enough.

To check capability without guessing at identity, dry-run a push from
the local clone (this just probes credentials; it doesn't actually
push):

```sh
git -C "$(git -C ~/.claude/skills/patch-claude rev-parse --show-toplevel 2>/dev/null)" \
    push --dry-run origin main 2>&1 | head -1
```

A successful dry-run (`Everything up-to-date` or a list of refs that
would advance) means you can publish. An auth error means you can't —
stop here.

If `apply-patch-fg.py` succeeded as-is (preferred path):

1. Verify the signature is in live:
   `grep -c '/\*pfg-v1\*/' $EXT/extension.js` — must be `1`.
2. Run `build-prebuilt.py`, commit, push.

If `apply-patch-fg.py`'s regexes failed on this version:

1. Update the regex anchors in `skill/apply-patch-fg.py` to cover
   the new shape.
2. Restore `extension.js` from `.bak` (or `.pre-patchFG.bak` if
   present) and re-run the script — verify it now applies cleanly
   AND embeds the signature.
3. Commit the script change in the same push as the prebuilt.

```sh
# Precondition: signature must already be in live (apply-patch-fg.py ran)
grep -q '/\*pfg-v1\*/' "$EXT/extension.js" || \
    { echo "ABORT: signature missing — run apply-patch-fg.py first"; exit 1; }

git clone https://github.com/ojura/claude-patches /tmp/claude-patches
cd /tmp/claude-patches
python3 util/build-prebuilt.py "$EXT" prebuilt/$VER
git add prebuilt/$VER/apply.py
git commit -m "prebuilt/$VER: synthesized $(date +%Y-%m-%d)"
git push
```

`util/build-prebuilt.py` diffs each patched file against its `.bak`,
extracts the splice pairs, and writes a self-contained apply script
that validates byte-stable against the live patched files before being
saved. Because it diffs live-vs-`.bak`, anything in live (including
the signature) becomes part of the prebuilt — so the signature must
already be present in live at synthesis time.

---

The remaining steps below are only relevant if Step 0 reported "no
prebuilt" — manual per-patch application against the located `$EXT`
install.

## Step 2 — back up the three target files

```
cp $EXT/extension.js           $EXT/extension.js.bak
cp $EXT/webview/index.js       $EXT/webview/index.js.bak
cp $EXT/webview/index.css      $EXT/webview/index.css.bak
```

Skip the backup if a `.bak` already exists (don't overwrite a prior backup
with already-patched content).

## Step 3 — Patch A: fork session writes a `custom-title` entry IF the head 64KB is otherwise unparseable

### Why

`forkSession` creates a new JSONL but emits no metadata entry. When the
session was previously compacted, the fork JSONL starts with
`isCompactSummary: true` followed by long tool results — the session metadata
parser (`Pp` / `Jq4`) can't find a valid prompt in the 64KB head buffer,
returns `null`, and the fork is filtered out of `listSessions`. The webview
then falls back to a blank session.

**Two refinements over the naive fix:**

- Use `custom-title` rather than `last-prompt` for the metadata channel.
  Resolver order is `customTitle || aiTitle || lastPrompt || summary || firstPrompt(head)`.
  Writing `last-prompt` would make the fork discoverable but also poison
  the title channel (lastPrompt wins over firstPrompt — the unrelated
  #32150 / #49996 bug). `custom-title` puts the rescue in the right channel:
  highest precedence, stable, overridable by user `/rename`.
- Only write the entry when actually needed. If the fork chain has a valid
  first user prompt within head 64KB, the parser's `firstPrompt` extractor
  works on its own — no metadata write required, and the fork's title is
  the source's original first prompt without further intervention.

### Locate

In `extension.js`, find the `forkSession` method. The tail of the function
follows this structure (variable names WILL differ between versions):

```js
async forkSession(K,V){
  ...
  let x = HD.randomUUID();               // <SESSION_ID> = fork session id
  ...
  let U = U1.join(N, `${x}.jsonl`);      // <FILE_PATH> = fork JSONL path
  ...
  let A = [...];                          // <MESSAGES> = fork messages array
  ...
  await w1.promises.appendFile(U,M);     // <FS>.promises.appendFile
  let v = A[A.length-1]?.uuid;           // <LEAF_UUID>
  if(q&&v){...}                          // <SUMMARY>=q
  this.sessionMessages.set(x, ...);
  ...
  if(q&&v) this.summaries.set(v,q);
  return this.loadedSessions.add(x), x;
}
```

Identify the six variables by *role*, not by name:

- `<SESSION_ID>` — the argument to `this.loadedSessions.add(...)` in the
  return statement.
- `<FILE_PATH>` — first argument to the earliest `<FS>.promises.appendFile(..., M)`
  in this function (the one that writes the JSONL body).
- `<SRC_PATH>` — the local that holds the **source** JSONL path. Look for
  `<X> = <pathJoin>(<dir>, \`${<sourceSessionIdParam>}.jsonl\`)` — typically
  one assignment up from where the existing `for(let M of await <loader>(<X>))`
  iterates the source file for file-history-snapshots.
- `<MESSAGES>` — the array whose `.uuid`s are put into
  `this.sessionMessages.set(<SESSION_ID>, new Set(...))`.
- `<FS>` — whatever module object `.promises.appendFile` is called on
  (`w1`, `M1`, etc).
- The splice point is the exact substring
  `this.summaries.set(<LEAF_UUID>,<SUMMARY>);return this.loadedSessions.add(<SESSION_ID>),<SESSION_ID>}}`
  — insert the block between the first `;` and `return`.

### Patch (with placeholders)

Replace:
```
this.summaries.set(<LEAF>,<SUM>);return this.loadedSessions.add(<SID>),<SID>}}
```
With:
```
this.summaries.set(<LEAF>,<SUM>);{let _srcCustom="",_srcAi="";try{let _src=(await <FS>.promises.readFile(<SRC_PATH>,"utf8")).split(`\n`);for(let _line of _src){if(!_line)continue;try{let _M=JSON.parse(_line);if(_M.type==="custom-title"&&_M.customTitle)_srcCustom=_M.customTitle;if(_M.type==="ai-title"&&_M.aiTitle)_srcAi=_M.aiTitle}catch(_){}}}catch(_){}let _srcTitle=_srcCustom||_srcAi;let _lp="",_lpEndBytes=-1,_byteOffset=0;for(let _i=0;_i<<MSG>.length;_i++){let _m=<MSG>[_i];let _lineBytes=Buffer.byteLength(JSON.stringify(_m)+`\n`,"utf8");if(_lpEndBytes<0&&_m.type==="user"&&!_m.isCompactSummary&&!_m.isMeta){let _mc=_m.message?.content;let _txt=null;if(typeof _mc==="string"&&_mc.trim())_txt=_mc;else if(Array.isArray(_mc))for(let _c of _mc){if(_c.type==="text"&&_c.text?.trim()){_txt=_c.text;break}if(_c.type==="tool_result")break}if(_txt){_lp=_txt;_lpEndBytes=_byteOffset+_lineBytes}}_byteOffset+=_lineBytes;if(_lpEndBytes>=0&&_byteOffset>65536)break}let _titleToWrite="";if(_srcTitle)_titleToWrite=_srcTitle;else if(_lpEndBytes<0||_lpEndBytes>65536)_titleToWrite=_lp||"Forked conversation";if(_titleToWrite){if(_titleToWrite.length>200)_titleToWrite=_titleToWrite.slice(0,200);await <FS>.promises.appendFile(<FPATH>,JSON.stringify({type:"custom-title",customTitle:_titleToWrite,sessionId:<SID>})+`\n`)}}return this.loadedSessions.add(<SID>),<SID>}}
```

Substitute the six placeholders with the actual variable names observed in
the current version. Use `python3` or the Edit tool.

The injected logic does three passes and one decision:

1. **Read source title** — slurps `<SRC_PATH>` and scans line-by-line for
   `custom-title` / `ai-title` entries; remembers the most recent of each.
2. **Walk fork messages forward** — finds the first valid user prompt
   (skipping `isCompactSummary` / `isMeta` / tool-result-only content) and
   tracks the byte offset of that prompt's full JSONL line.
3. **Decide what to write**:
   - If source has explicit title → inherit it as the fork's `customTitle`
     (regardless of head 64KB parseability — keeps fork in sync with source's
     displayed name, including post-`/rename`).
   - Else if head 64KB parser would resolve a valid prompt → don't write
     anything (firstPrompt extractor handles it).
   - Else → write rescue `customTitle` derived from the first user message
     anywhere in the chain, or `"Forked conversation"` as last-resort.

`Buffer` is a Node global available in this context.

### Verify

```
# When applied: at most 1 occurrence (could be 0 if the test fork's head
# happens to satisfy the parser without the rescue write, but the patch
# itself is in the source as a single block).
grep -c 'type:"custom-title",customTitle:_titleToWrite,sessionId:' $EXT/extension.js
```
Expect `1` (the literal injection is present once in source — the `if`
guard around it determines whether it runs at fork time).

## Step 4 — Patch B: sticky message header → linear scroll

### Why

A very tall user message at the top of a turn, with `position: sticky`,
occludes the assistant reply underneath it. The fix reverts to linear
layout (`position: relative`).

### Locate and patch

In `webview/index.css` (a single massive line), find the rule
```
.message_<S>.stickyHeader_<S>{--sticky-bg:var(--app-primary-background);position:sticky;z-index:2;background-image:linear-gradient(...)...;align-items:stretch;padding-top:14px;padding-bottom:12px;top:0}
```
where `<S>` is a short suffix like `_07S1Yg`. Discover it with:

```
grep -oE 'stickyHeader_[A-Za-z0-9]+' $EXT/webview/index.css | sort -u
```

Then apply two replacements (via python; the file is too large for Read):

1. Main rule — strip `position:sticky`, both `background-image` gradients,
   `z-index:2`, and `top:0`; replace with `position:relative;z-index:auto`:

   ```
   .message_<S>.stickyHeader_<S>{--sticky-bg:var(--app-primary-background);position:relative;z-index:auto;align-items:stretch;padding-top:14px;padding-bottom:12px}
   ```

2. Expanded-variant rule — `z-index:3` → `z-index:auto`:

   ```
   .message_<S>.stickyHeader_<S>:has([aria-expanded=true]){z-index:auto}
   ```

### Verify

```
grep -oE '\.message_<S>\.stickyHeader_<S>[^}]*\}' $EXT/webview/index.css
```

The first two rules should contain `position:relative` / `z-index:auto` and
no `sticky` or `background-image:linear-gradient`.

## Step 5 — Patch C: disable broken `isSlashCommand` detection

### Why

The webview infers "this user message is a slash command" via
`text.startsWith("/")`, which false-positives on any message that begins
with a Unix path (e.g. compiler output pasted as a prompt). The
slash-command render path drops the `userMessageContainer` wrapper and loses
the fork/rewind action button. In-band signalling from message text is
wrong in principle — kill it.

### Locate and patch

In `webview/index.js`, find the pattern (variable names may change):

```
<VAR>=<TEXT>.startsWith("/");return{type:"text",text:<TEXT>,isSlashCommand:<VAR>}
```

Replace with:

```
<VAR>=!1;return{type:"text",text:<TEXT>,isSlashCommand:<VAR>}
```

Confirm exactly one occurrence before replacing.

### Verify

```
grep -oE '[A-Za-z_$]+=![01];return\{type:"text",text:[A-Za-z_$]+,isSlashCommand:[A-Za-z_$]+\}' $EXT/webview/index.js
```

Should show the patched line; the original `.startsWith("/")` form must no
longer exist in that context.

## Step 6 — Patch D: chain walker bridges compaction boundaries via logicalParentUuid

### Why

The compact stitch (`type:"system", subtype:"compact_boundary"`) is written
with `parentUuid:null` and the actual pre-compact predecessor stored in
`logicalParentUuid`. The chain-walking code in `extension.js` follows
`parentUuid` only — so any path that reads back the conversation (rewind UI,
fork-action discoverability, `--resume` render) sees only post-last-compaction
messages. The pre-compact transcript is on disk but invisible.

Architecture (verified against the leaked source — `buildConversationChain` at
`src/utils/sessionStorage.ts:2069`, `getMessagesAfterCompactBoundary` at
`src/utils/messages.ts:4643`): the API path is bounded independently of
parentUuid topology by `getMessagesAfterCompactBoundary`, which scans for the
boundary marker. So making the chain walker follow `logicalParentUuid` is
safe — it doesn't blow up the API context, only restores UI/fork visibility.

This is the read-side fix proposed in [#48937](https://github.com/anthropics/claude-code/issues/48937).
Filed with empirical verification; not yet upstream.

### Locate

In `extension.js`, find the `buildConversationChain`-equivalent function. It
contains TWO inline parentUuid walks. Discover them with:

```
grep -oE '[A-Za-z_$]{1,3}\.parentUuid\?[A-Za-z_$]{1,3}\.get\([A-Za-z_$]{1,3}\.parentUuid\):(void 0|undefined)' $EXT/extension.js | sort -u
```

You should see two walkers, each with form `<X>=<X>.parentUuid?<MAP>.get(<X>.parentUuid):void 0`,
where `<X>` differs (typically two single-letter variable names) and `<MAP>` is the
same shared messages map (typically `K`). Do NOT patch `getTranscript` (a method
on the session class) — it already has the `K=!1` opt-in fallback, used correctly
by `forkSession`. Only the inline walkers need bridging.

### Patch

For each of the two walkers, replace:

```
<X>=<X>.parentUuid?<MAP>.get(<X>.parentUuid):void 0
```

With:

```
<X>=<X>.parentUuid?<MAP>.get(<X>.parentUuid):(<X>.logicalParentUuid?<MAP>.get(<X>.logicalParentUuid):void 0)
```

Confirm exactly one occurrence of each old string before replacing (each
walker uses a distinct variable name, so the strings are unique).

### Verify

```
# Both patched walkers should match (count == 2 across both X values)
grep -oE '\.logicalParentUuid\?[A-Za-z_$]{1,3}\.get\([A-Za-z_$]{1,3}\.logicalParentUuid\)' $EXT/extension.js | wc -l
# Old form must be gone for both
grep -cE '[A-Za-z_$]{1,3}=[A-Za-z_$]{1,3}\.parentUuid\?[A-Za-z_$]{1,3}\.get\([A-Za-z_$]{1,3}\.parentUuid\):void 0' $EXT/extension.js
```

Expect 2 patched walkers and 0 remaining old forms.

### Test

After reload, open a session that has been auto-compacted. Pre-compact
messages should be visible in scrollback and have fork-action buttons.
Continuation of the source session should still work normally — the API
slice is unaffected.

## Step 7 — Patch E: title resolver puts `firstPrompt` ahead of `lastPrompt`

### Why

The session-list metadata parser resolves a session's title via the chain:

    customTitle || aiTitle || lastPrompt || summary || firstPrompt(head)

`lastPrompt` outranking `firstPrompt` causes title drift on every long-enough
session — the title becomes "whatever the user most recently typed" instead
of "what the conversation is about." Filed as
[#32150](https://github.com/anthropics/claude-code/issues/32150) (with
@ojura's resolver-chain comment); the fix is to swap `firstPrompt` ahead of
`lastPrompt`.

### Locate

There are two near-identical resolver sites in `extension.js`. Find them with:

```
grep -oE '[A-Za-z_$]{1,3}=H\|\|[A-Za-z_$0-9]+\([^)]+,"lastPrompt"\)\|\|[A-Za-z_$0-9]+\([^)]+,"summary"\)\|\|[A-Za-z_$0-9]+' $EXT/extension.js
grep -oE '\|\|[A-Za-z_$0-9]+\([^)]+,"lastPrompt"\)\|\|[A-Za-z_$0-9]+\([^)]+,"summary"\)\|\|[A-Za-z_$0-9]+\([^)]+\)' $EXT/extension.js
```

Each chain has the structure `<...>||<extractor>(<src>,"lastPrompt")||<extractor>(<src>,"summary")||<firstPromptFn>(<src>)`.
The two sites differ in their extractor name (e.g. `X5` vs `x9`) and the
firstPrompt function name (e.g. variable `D` holding a precomputed result vs
direct call to `Ca(<src>)`).

### Patch

Swap the order at each site so `firstPrompt` appears before `lastPrompt`:

Site 1 (extractor `<E1>`, firstPrompt held in variable `<FP_VAR>`):

Old: `<E1>(<src>,"lastPrompt")||<E1>(<src>,"summary")||<FP_VAR>`
New: `<FP_VAR>||<E1>(<src>,"summary")||<E1>(<src>,"lastPrompt")`

Site 2 (extractor `<E2>`, firstPrompt is a direct call `<FP_FN>(<head>)`):

Old: `||<E2>(<src>,"lastPrompt")||<E2>(<src>,"summary")||<FP_FN>(<head>)`
New: `||<FP_FN>(<head>)||<E2>(<src>,"summary")||<E2>(<src>,"lastPrompt")`

Confirm exactly one occurrence of each old string before replacing.

### Verify

```
# Both new orderings present
grep -cE '[A-Za-z_$]{1,3}=H\|\|[A-Za-z_$0-9]+\|\|[A-Za-z_$0-9]+\([^)]+,"summary"\)\|\|[A-Za-z_$0-9]+\([^)]+,"lastPrompt"\)' $EXT/extension.js
grep -cE '\|\|[A-Za-z_$0-9]+\([^)]+\)\|\|[A-Za-z_$0-9]+\([^)]+,"summary"\)\|\|[A-Za-z_$0-9]+\([^)]+,"lastPrompt"\)' $EXT/extension.js
```

Both should be `>= 1` (site 1 + site 2). Old `lastPrompt`-then-`summary`
ordering should not appear.

## Steps 8 & 9 — Patches F and G: USE THE SCRIPT

```sh
python3 ~/.claude/skills/patch-claude/apply-patch-fg.py "$EXT/extension.js"
```

The script handles both F (+F.2 +F.3) and G (+G.1 +G.2). It locates
anchors via regex with named captures, so renamings like `m1`→`c1`
(storage class) or `[z,L]`→`[z,A]` (sessionPanels destructure) are
absorbed automatically. It also embeds the `/*pfg-v1*/` signature
into `extension.js` after `updateSessionState(V,K,B){`, which
`build-prebuilt.py` will then capture into the synthesized prebuilt.

If the script reports anchors not matching uniquely, the bundle
structure has shifted enough to break the regexes. Fall back to
the manual splice descriptions in Step 8 (Patch F) and Step 9
(Patch G) below to get the patches applied locally, then stop.
Updating the script and synthesizing a refreshed prebuilt is a
maintainer task — see the "Maintainer-only" subsection under
Step 0.

After the script runs successfully, jump to Step 10.

---

## Step 8 — Patch F: rename writes propagate through `sessionStates` Map (manual reference)

### Why

Renaming a session via the sidebar pencil icon flips the new title back
to the previous one within seconds — typically when the user switches
sessions or any other broadcast trigger fires.

Root cause traced via CDP-instrumented signal-write spy. The sidebar's
`session_states_update` handler at `webview/index.js:2044` overwrote
`summary.value` with the **old** title 2.275s after the legitimate
rename, on session switch.

The extension's `sessionStates` Map (the source of `broadcastSessionStates`
payloads) is updated **only** by `update_session_state` messages forwarded
through `q8.onSessionStateChanged`. The chat panel's per-session reactive
in the webview happens to send those messages on `summary.value` change,
so panel-side renames re-align the Map as a side effect. Sidebar-side
renames (pencil icon) do not — the sidebar's `q8` is constructed in
`resolveSessionListView` with `void 0` and no `onSessionStateChanged`
callback, so nothing ever pushes the new title into the Map. The next
`broadcastSessionStates()` (focus change, busy-state flip on any session,
panel switch) overwrites the just-renamed `summary.value` with the
stale title.

The fix has three coordinated splices:

1. **`updateSessionState` preserves missing fields** — let callers update
   only the title (or only the state) without clobbering the other.
2. **`q8.renameSession` invokes `onSessionStateChanged` after success** —
   pushes the new title into the manager's Map and triggers a broadcast.
3. **Sidebar `q8` ctor wires `onSessionStateChanged`** — without this,
   sidebar-driven renames still wouldn't propagate (the callback is the
   only escape hatch from `q8` to the manager's Map).

### Locate and patch

All three sites are in `extension.js`. Match patterns are unique on the
2.1.120 build; variable names will change between releases — locate by
the structural anchors `sessionStates.set(V,{sessionId:V,state:K,title:B})`,
`renameSession(V,K,B){return{type:"rename_session_response"`, and the
unique `,void 0,()=>this.<broadcastUsageUpdate>())` tail of the
`resolveSessionListView`-equivalent `q8` instantiation (the only `q8`
ctor call that passes `void 0` for the panel-reference slot).

#### Site 1 — `updateSessionState`

Old:
```
updateSessionState(V,K,B){this.sessionStates.set(V,{sessionId:V,state:K,title:B}),this.broadcastSessionStates()}
```
New:
```
updateSessionState(V,K,B){let _p=this.sessionStates.get(V);this.sessionStates.set(V,{sessionId:V,state:K!=null?K:_p?.state??"idle",title:B!=null?B:_p?.title}),this.broadcastSessionStates()}
```

`!=null` (loose) means "neither null nor undefined", so existing callers
passing real values (including `""`) are unaffected; new callers can
pass `undefined` to mean "leave as-is". The `??"idle"` fallback handles
the not-yet-seen-before case.

#### Site 2 — `q8.renameSession`

Old:
```
async renameSession(V,K,B){return{type:"rename_session_response",skipped:await(await m1.load(this.cwd,this.logger)).renameSession(V,K,B)}}
```
New:
```
async renameSession(V,K,B){let _r=await(await m1.load(this.cwd,this.logger)).renameSession(V,K,B);if(!_r)this.onSessionStateChanged?.(V,void 0,K);return{type:"rename_session_response",skipped:_r}}
```

`_r` is `m1.renameSession`'s `skipped` flag — `true` means the storage
short-circuited (e.g. `aiTitle` skipped because `customTitle` already
exists). Only invoke the callback when an actual write happened.
Optional chaining covers the case where `onSessionStateChanged` is
unwired on a particular `q8` instance (e.g. before Site 3 has been
applied — the patch is internally robust to partial application).

#### Site 3 — sidebar `q8` ctor

The `,void 0,()=>this.<broadcastUsageUpdate>())` tail is unique on
2.1.120 — the only `q8` instantiation passing `void 0` for the panel
slot is the one in `resolveSessionListView` (or the equivalent sidebar
view resolver). Confirm uniqueness with `grep -c` before splicing.

Old:
```
,void 0,()=>this.broadcastUsageUpdate())
```
New:
```
,void 0,()=>this.broadcastUsageUpdate(),!1,(H,D,O)=>{this.updateSessionState(H,D,O)})
```

Two args added: `isFullEditor=!1` (sidebar isn't a full editor) and a
minimal `onSessionStateChanged` callback that just forwards to the
manager. The panel's callback also does panel-mapping bookkeeping
(`sessionPanels` Map maintenance, `activeSessionId`); the sidebar
doesn't need any of that.

Constructor parameter order is verified by reading the class body
(`this.isFullEditor=I;this.onSessionStateChanged=F` — last two params
are isFullEditor then onSessionStateChanged in that order).

### Verify

```
grep -c 'updateSessionState(V,K,B){let _p=this.sessionStates.get(V)' $EXT/extension.js
grep -c 'if(!_r)this.onSessionStateChanged?.(V,void 0,K)' $EXT/extension.js
grep -c ',!1,(H,D,O)=>{this.updateSessionState(H,D,O)})' $EXT/extension.js
node --check $EXT/extension.js && echo "syntax OK"
```

Each grep should be `1`. Syntax check should pass.

### Test

After reload: rename a session via the sidebar pencil. Switch to a
different session and back. The new title should remain.

For a CDP-instrumented test (faster iteration), patch the sidebar's
`Vn.sessions.value[i].summary` signal `set` to log writes with stack
traces, then trigger the rename and switch sessions — there should now
be exactly one write (from `Vn.renameSession` itself), with no later
write from `index.js:2044` carrying the old title.

### Patch F.2 + F.3 — close two regressions revealed by panel-side stale summaries

After the three core splices land, two follow-up holes show up:

1. **Stale-summary feedback loop.** Panels don't have the
   `sessionStates → summary.value` bridge that the sidebar's `Te1`
   component has (it's the only `useEffect` that writes
   `Vn.sessions[i].summary.value` from broadcast data, at offset
   ~`webview/index.js:2044`). So a panel's local `summary.value` for a
   session it isn't actively displaying stays at whatever value it had
   on initial load. When the panel's per-session reactive
   `K4(()=>{Z.updateSessionState(sessionId, state, Y.summary.value)})`
   fires for *any* state change (busy flip on this session, etc.), it
   sends `update_session_state` carrying the **stale** title. Patch F's
   manager.updateSessionState writes that stale title into the Map and
   re-broadcasts. Sidebar's bridge sees the stale title and overwrites
   the just-renamed `summary.value`. Visible as: rename works, then
   flips back on the next state event — even with Patch F's three
   splices applied.

2. **Tab title doesn't update on cross-webview rename.** The chat
   panel's tab title comes from `panelTab.title`, set only by the
   `rename_tab` message handler, sent only by the panel webview's
   `renameTab` reactive subscribed to `activeSession.value.summary.value`.
   For a sidebar-driven rename, the panel's local `summary.value` never
   changes (no bridge), the reactive never fires, and the tab stays on
   the old title even though the sidebar list shows the new one
   correctly.

Both fixed by two more splices in `extension.js`:

#### F.2 — drop title at the `update_session_state` boundary

The webview reactive sends a title field, but only the rename path
should be authoritative for titles. Drop the title at the message
handler so panel reactives can no longer clobber the Map:

Old:
```
if(V.request.type==="update_session_state")return this.onSessionStateChanged?.(V.request.sessionId,V.request.state,V.request.title),{type:"update_session_state_response"}
```
New:
```
if(V.request.type==="update_session_state")return this.onSessionStateChanged?.(V.request.sessionId,V.request.state,void 0),{type:"update_session_state_response"}
```

After F.2, only `q8.renameSession` (Patch F site 2) ever pushes a
title into the Map.

**Known cosmetic edge case.** Sidebar placeholders for sessions that
exist in the broadcast Map but not in the sidebar's `Vn.sessions`
(the `Q.filter(N => !O.has(N.sessionId))` set) lose their
broadcast-supplied title — the placeholder filter
`N.title || N.state !== "idle"` only renders them when they have a
non-idle state (which they will, otherwise nothing would have called
`update_session_state` for them in the first place). In practice the
sidebar's `Vn.sessions` includes everything on disk in the cwd, so
this case is rare.

#### F.3 — manager writes `panel.title` directly

Combine into the F-site-1 splice (only one match for the original
unpatched anchor; combine to keep the apply pass single-step):

Old (still the original — applies cleanly even if F site 1 hasn't been
applied yet):
```
updateSessionState(V,K,B){this.sessionStates.set(V,{sessionId:V,state:K,title:B}),this.broadcastSessionStates()}
```
New (F site 1 + F.3 combined):
```
updateSessionState(V,K,B){let _p=this.sessionStates.get(V);this.sessionStates.set(V,{sessionId:V,state:K!=null?K:_p?.state??"idle",title:B!=null?B:_p?.title}),this.broadcastSessionStates();if(B!=null){let _pnl=this.sessionPanels.get(V);if(_pnl)_pnl.title=B}}
```

The trailing `if(B!=null){let _pnl=this.sessionPanels.get(V);if(_pnl)_pnl.title=B}`
fires whenever a non-null title was supplied (i.e. the rename path,
since F.2 ensures `update_session_state` passes `void 0`). The
manager looks up the owning panel via `sessionPanels` (the Map
maintained in `setupPanel`) and sets its title directly, bypassing
the webview-reactive plumbing. Tab title now updates on any rename
path regardless of which webview triggered it.

### Verify (F + F.2 + F.3)

```
grep -c 'updateSessionState(V,K,B){let _p=this.sessionStates.get(V).*if(B!=null){let _pnl' $EXT/extension.js
grep -c 'this.onSessionStateChanged?.(V.request.sessionId,V.request.state,void 0)' $EXT/extension.js
grep -c 'if(!_r)this.onSessionStateChanged?.(V,void 0,K)' $EXT/extension.js
grep -c ',!1,(H,D,O)=>{this.updateSessionState(H,D,O)})' $EXT/extension.js
```
Each should be `1`.

## Step 9 — Patch G: forked session appears in sidebar without sending a message (manual reference)

### Why

Forking a session creates a new JSONL on disk via
`m1.forkSession`, but the new session doesn't appear in the sidebar
list until the user sends a message in it. Same architectural pattern
as the rename bug: the sidebar's `Te1` component re-fetches the
session list (`$.listSessions()`) only when its
`G.length > 0` `useEffect` fires — and `G` is built from broadcast Map
entries that aren't in `Vn.sessions`. The fork has no such Map entry
until the new session's first state change (typically the user
sending a message → busy state → `update_session_state` → Map entry →
broadcast → placeholder → list refresh).

The fix is the same shape as Patch F's site 2: have the extension's
`fork_conversation` handler push an entry into the Map immediately
after `m1.forkSession`. The trick: the Map title must agree with
what the sidebar will eventually load from JSONL (otherwise the
sidebar's `sessionStates → summary.value` bridge overwrites the
JSONL-derived title with the placeholder text). Patch A's fork-time
`custom-title` injection inherits the source's `customTitle`/`aiTitle`,
so we can read the source's metadata and use that — both will agree.

### G.1 — panel ctor callback supports skip-bookkeeping flag

The panel callback wired in `setupPanel` does panel-mapping
bookkeeping (`sessionPanels.set(forkSid, V); sessionPanels.delete(sourceSid) if it pointed at V`)
that's wrong when the fork hasn't been activated yet — at fork-handler
time the panel `V` is still showing the source. Add a 4th arg to skip
the bookkeeping:

Old:
```
(H,D,O)=>{this.updateSessionState(H,D,O);for(let[z,L]of this.sessionPanels)if(L===V&&z!==H)this.sessionPanels.delete(z);if(this.sessionPanels.set(H,V),V.active)this.activeSessionId=H}
```
New:
```
(H,D,O,_sk)=>{this.updateSessionState(H,D,O);if(!_sk){for(let[z,L]of this.sessionPanels)if(L===V&&z!==H)this.sessionPanels.delete(z);if(this.sessionPanels.set(H,V),V.active)this.activeSessionId=H}}
```

### G.2 — `fork_conversation` handler pushes Map entry with source's title

Reads the source's JSONL line-by-line for the latest `custom-title` /
`ai-title`, falls back to `"Forked conversation"` if no metadata.
Calls `onSessionStateChanged(forkSid, "idle", title, true)` (skip
bookkeeping flag set) so the bookkeeping doesn't corrupt the source's
panel mapping.

Old:
```
case"fork_conversation":return{type:"fork_conversation_response",sessionId:await(await m1.load(this.cwd,this.logger)).forkSession(V.request.forkedFromSession,V.request.resumeSessionAt)};
```
New:
```
case"fork_conversation":{let _m=await m1.load(this.cwd,this.logger),_src=V.request.forkedFromSession,_sid=await _m.forkSession(_src,V.request.resumeSessionAt);let _t="";try{let _lines=(await R1.promises.readFile(O1.join(d5(_m.projectRoot),`${_src}.jsonl`),"utf8")).split(`\n`),_c="",_a="";for(let _line of _lines){if(!_line)continue;try{let _M=JSON.parse(_line);if(_M.type==="custom-title"&&_M.customTitle)_c=_M.customTitle;if(_M.type==="ai-title"&&_M.aiTitle)_a=_M.aiTitle}catch(_){}}_t=_c||_a}catch(_){}if(!_t)_t="Forked conversation";this.onSessionStateChanged?.(_sid,"idle",_t,!0);return{type:"fork_conversation_response",sessionId:_sid}}
```

`R1` (fs), `O1` (path), and `d5` (project-root resolver) are bundle
globals already used by `m1.renameSession` etc. — visible in scope.

### Verify (G)

```
grep -c '(H,D,O,_sk)=>{this.updateSessionState(H,D,O);if(!_sk)' $EXT/extension.js
grep -c 'case"fork_conversation":{let _m=await m1.load' $EXT/extension.js
```
Each should be `1`.

### Test

After reload: fork an existing session via the chat UI. The new fork
should appear in the sidebar immediately (no need to send a message
first), with the source session's title (or "Forked conversation" if
the source had no `custom-title` or `ai-title`).

## Step 10 — summary to the user

Report which version was patched and which files were touched, using
markdown relative links. Remind the user to reload the VSCode window for the
patches to take effect.

Also summarize any drift observations from any earlier step (0 through 9):
anchors that didn't match as written, structural shifts beyond pure
variable renaming, prebuilt-fetch / install-locate / backup quirks,
variable renames the F+G script auto-absorbed that future readers
would benefit from knowing about, and any verify-grep or other doc
bugs in this SKILL.md you noticed during application. Do this
proactively — don't wait to be asked. Then propose follow-up:

- If you have push access to `claude-patches` (the Step 0 dry-run probe
  already established this — `Everything up-to-date` or a list of
  advancing refs means yes), propose specific SKILL.md edits in the
  same response and apply on confirmation.
- If you don't, propose opening an issue at
  https://github.com/ojura/claude-patches/issues with the version, the
  patch ID (A–G), and a minimal repro grep.

If nothing drifted and nothing was wrong, say so explicitly — silence
is ambiguous.

## Notes

- The CSS and JS files in `webview/` are minified onto a single line each;
  use `python3` for string replacement rather than the Edit tool (Read
  fails on them).
- If any of the three patches cannot be located (pattern shape changed
  substantially), stop and report that one to the user rather than guessing
  — these are patches against obfuscated code and a wrong splice could be
  disruptive.
- If a `.bak` from a prior version already exists, leave it. The current
  pre-patch state is still recoverable from the VSCode extension cache /
  reinstall.
