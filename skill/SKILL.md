---
name: patch-claude
description: Reapply Juraj's ten local patches to a newly updated anthropic.claude-code VS Code extension. Auto-detects which IDE-hosted install is the running one via CLAUDE_CODE_EXECPATH. Use when the user says "the extension updated, reapply patches" or similar. Backs up files, reapplies all ten patches, verifies each one.
---

# Reapply anthropic.claude-code extension patches

**Patchset version**: `1.4`

Eleven patches live out-of-tree and need to be reapplied every time the bundled
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
   self-validating (detect `pfg-v1.2` signature, idempotent, byte-stable
   verified at synthesis time) — so this step either applies the
   patches cleanly OR no-ops because they're already applied.

```sh
set -u

# --- Step 0a: self-update via fast-forward, if symlinked-clone setup ---
# Discover by repo origin URL, not by skill directory name — the user is
# free to install the skill under any name (~/.claude/skills/patch-claude,
# patch-antigravity, foo, ...). We scan all entries under ~/.claude/skills/
# and pick whichever one resolves into a clone of ojura/claude-patches.
REPO_ROOT=
for entry in ~/.claude/skills/*; do
  [ -e "$entry" ] || continue
  target=$(readlink -f "$entry" 2>/dev/null) || continue
  [ -d "$target" ] || continue
  candidate=$(git -C "$target" rev-parse --show-toplevel 2>/dev/null) || continue
  remote=$(git -C "$candidate" remote get-url origin 2>/dev/null) || continue
  case "$remote" in
    *ojura/claude-patches*|*ojura/claude-patches.git)
      REPO_ROOT="$candidate"
      break
      ;;
  esac
done
if [ -n "$REPO_ROOT" ]; then
  echo "Self-update: fetching $REPO_ROOT..."
  # Fetch via the configured remote first. If that fails (common in
  # headless/sandboxed shells where ssh-askpass is unavailable) and the
  # remote is github/gitlab SSH, fall back to HTTPS — public-repo reads
  # don't need auth. Stays generic: works for any fork, no hardcoded
  # owner/repo.
  if ! git -C "$REPO_ROOT" fetch --quiet origin 2>/dev/null; then
    remote_url="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
    https_url=
    case "$remote_url" in
      git@github.com:*|git@gitlab.com:*)
        host="${remote_url#git@}"; host="${host%%:*}"
        path="${remote_url#git@*:}"; path="${path%.git}"
        https_url="https://${host}/${path}.git"
        ;;
    esac
    if [ -n "$https_url" ]; then
      echo "  configured fetch failed; retrying via $https_url"
      if ! git -C "$REPO_ROOT" fetch --quiet "$https_url" \
            main:refs/remotes/origin/main 2>&1; then
        echo "  WARNING: HTTPS fallback also failed — origin/main is stale."
      fi
    else
      echo "  WARNING: fetch failed and remote isn't a recognized github/gitlab SSH URL — origin/main may be stale."
    fi
  fi
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
  echo "Self-update skipped: no symlink under ~/.claude/skills/ resolves to a clone of ojura/claude-patches."
  echo "  (Optional) install via: git clone https://github.com/ojura/claude-patches ~/claude-patches;"
  echo "  ln -s ~/claude-patches/skill ~/.claude/skills/<any-name>"
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

# --- Step 0c: try the prebuilt (covers all patches A–J in one shot) ---
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
  prebuilt-applied code. The `pfg-v1.2` signature embedded in
  `extension.js` is the authoritative check, and the prebuilt verifies
  it itself.
- `ABORT: ...` → stop and surface the message to the user; don't try
  to "fix" the abort condition automatically.
- `No prebuilt for $VER` → apply the patches manually as follows:
    1. **Patches A–E**: follow Steps 3–7 (per-splice manual application).
    2. **Patches F and G**: do NOT splice manually. Run
       `skill/apply-patch-fg.py` — it locates anchors via regex (so it
       handles variable-name drift across releases automatically) and
       embeds the `/*pfg-v1.2*/` signature comment that the prebuilt
       relies on for idempotency. Steps 8 and 9 below describe the
       splices structurally for reference only.
    3. **Patches H, I, J**: follow Steps 10–12 (per-splice manual
       application). All three are short, single-anchor splices.
    4. **Patch K**: follow Step 13 (extension.js loader splice + a
       webview/index.js render wrap). This is `lost+found`-style
       recovery for sessions whose `compact_boundary.logicalParentUuid`
       points at a never-persisted uuid (write-side bug at upstream
       `compact.ts:598`).
    5. If the F+G script reports anchors not matching uniquely, the
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
# Uses $REPO_ROOT discovered in Step 0a.
git -C "$REPO_ROOT" push --dry-run origin main 2>&1 | head -1
```

A successful dry-run (`Everything up-to-date` or a list of refs that
would advance) means you can publish. An auth error means you can't —
stop here.

If `apply-patch-fg.py` succeeded as-is (preferred path):

1. Verify the signature is in live:
   `grep -c '/\*pfg-v1.2\*/' $EXT/extension.js` — must be `1`.
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
grep -q '/\*pfg-v1.2\*/' "$EXT/extension.js" || \
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
python3 "$REPO_ROOT/skill/apply-patch-fg.py" "$EXT/extension.js"
```

The script handles both F (+F.2 +F.3) and G (+G.1 +G.2). It locates
anchors via regex with named captures, so renamings like `m1`→`c1`
(storage class) or `[z,L]`→`[z,A]` (sessionPanels destructure) are
absorbed automatically. It also embeds the `/*pfg-v1.2*/` signature
into `extension.js` after `updateSessionState(V,K,B){`, which
`build-prebuilt.py` will then capture into the synthesized prebuilt.

If the script reports anchors not matching uniquely, the bundle
structure has shifted enough to break the regexes. Fall back to
the manual splice descriptions in Step 8 (Patch F) and Step 9
(Patch G) below to get the patches applied locally, then stop.
Updating the script and synthesizing a refreshed prebuilt is a
maintainer task — see the "Maintainer-only" subsection under
Step 0.

After the script runs successfully, jump to Step 10 (Patch H).

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

## Step 10 — Patch H: bypass the 5 MB precompact-skip optimization

### Why

The bundled session loader skips parsing pre-compact-boundary content for any JSONL > 5 MB
([leaked source `sessionStoragePortable.ts:480`](https://github.com/yasasbanukaofficial/claude-code/blob/main/src/utils/sessionStoragePortable.ts#L480) defines the `SKIP_PRECOMPACT_THRESHOLD = 5 * 1024 * 1024` constant; the gate lives in
[`sessionStorage.ts:3536-3556`](https://github.com/yasasbanukaofficial/claude-code/blob/main/src/utils/sessionStorage.ts#L3536-L3556)).
Pre-boundary content is *never parsed*, so the chain walker, fork picker, rewind UI, and
chat-panel render all only see post-most-recent-`compact_boundary` messages on big files.
Patch D's `parentUuid → logicalParentUuid` fallback can't help — the predecessor messages
aren't in the parsed array.

There's an env-var kill switch (`CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP`) but requiring users
to set environment variables is fragile; better to disable the optimization at the
read-site itself.

Filed as [#55700](https://github.com/anthropics/claude-code/issues/55700).

### Locate and patch

In `extension.js`, find the loader function that branches on file size against the
`SKIP_PRECOMPACT_THRESHOLD` constant. In 2.1.126 it's bundled as `Rz4`:

```js
function Rz4(V,K){try{if(K>Hz4&&!M2(process.env.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP))return(await jz4(V,K)).postBoundaryBuf;return await ml.readFile(V)}catch{return null}}
```

`Hz4` is the 5 MB constant. `M2(...)` evaluates the env var. Locate via:

```
grep -oE 'function\s[A-Za-z_$0-9]+\([^)]*\)\{try\{if\([A-Za-z_$0-9]+>[A-Za-z_$0-9]+&&![A-Za-z_$0-9]+\(process\.env\.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP\)\)return\(await' $EXT/extension.js
```

Replace the `!M2(process.env.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP)` clause with `!(!0||M2(...))`.
The OR-with-true short-circuits to `true`, the negation makes it `false`, the `&&` makes the
whole condition `false`, the optimization never fires. Original env-var read kept around the
`||` for forensic clarity (no functional effect):

Old:
```
if(K>Hz4&&!M2(process.env.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP))
```
New:
```
if(K>Hz4&&!(!0||M2(process.env.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP)))
```

Confirm exactly one occurrence before replacing.

### Verify

```
grep -c '!(!0||M2(process\.env\.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP))' $EXT/extension.js
```

Expect `1`. The original `&&!M2(...)` form must no longer appear.

### Test

After reload, open any session whose JSONL is > 5 MB. The chat panel should populate with
the full in-file chain back to the most recent stitch with cross-file `logicalParentUuid`
(which is then the genuine ceiling — addressed by Patch J). Cache_read on the next API turn
should remain bounded — `getMessagesAfterCompactBoundary` does its own boundary slicing
independent of what the loader returns.

## Step 11 — Patch I: neutralize the webview's 500-message render cap

### Why

The webview hardcodes a cap on how many messages can live in the React state — anything
beyond ~600 is silently truncated to the most recent 500. There's no UI feedback, no
"load more" affordance. This negates the work of Patches D + H (and the upstream chain
walker fixes in #46603 / #48937) once the chain walks back beyond 500 messages.

Filed as [#55701](https://github.com/anthropics/claude-code/issues/55701).

### Locate and patch

In `webview/index.js`, find the cap function. In 2.1.126 it's bundled as `OD`:

```js
function OD($){if($.length>g20){let Z=$.length-u20;return $.slice(Z)}return $}
```

Where `g20 = 600` and `u20 = 500`. Discover via:

```
grep -oE 'function [A-Za-z_$]+\(\$\)\{if\(\$\.length>[a-z0-9]+\)\{let [A-Za-z_$]+=\$\.length-[a-z0-9]+;return \$\.slice\([A-Za-z_$]+\)\}return \$\}' $EXT/webview/index.js
```

Replace with the identity function:

Old:
```
function OD($){if($.length>g20){let Z=$.length-u20;return $.slice(Z)}return $}
```
New:
```
function OD($){return $}
```

Substitute the actual function name (e.g. `OD`) in the new form. Confirm exactly one
occurrence before replacing.

### Verify

```
grep -c 'function OD(\$){return \$}' $EXT/webview/index.js
```

Expect `1` (with the function name observed during locate).

### Test

After reload, open a session with > 500 chain-walkable messages. The full chain should
render. Note: at 10K+ messages, initial render takes a few seconds — the bottleneck is
React rendering, not the patch. If the UI lags unacceptably, partial mitigation: use
`if($.length>10000)return $.slice(-10000); return $` instead of the pure identity.

## Step 12 — Patch J: cross-file logicalParentUuid resolution at session load

### Why

Patch D + Patch H restore visibility back to the most recent in-file `compact_boundary`
stitch. But fork-from-compact creates stitches whose `logicalParentUuid` points to a
message in a **different** JSONL (the source session). The chain walker's
`parentUuid → logicalParentUuid` fallback then misses, because the in-memory map only
contains the current session's messages. The chain stops at the cross-file stitch.

Patch J resolves the cross-file pointers at load time: scan the parsed message array for
compact-boundary stitches with unresolved `logicalParentUuid`, look up which sibling JSONL
in the project dir owns that uuid, take the slice of that file from index 0 through (and
including) the lpu's target message, and prepend to the parsed array. Loop with a
fixed-point until no dangling lpus remain (capped at 10 iterations as a safety).

Closes the read-side half of [#48937 secondary](https://github.com/anthropics/claude-code/issues/48937)
and [#46603](https://github.com/anthropics/claude-code/issues/46603) for the cross-file case.

### Locate and patch

In `extension.js`, find the loader (`Wz4` in 2.1.126). The pre-patch shape is:

```js
async function Wz4(V,K){if(!uz(V))return[];let B=await qz4(V,K?.dir);if(!B)return[];let x=await Rz4(B.filePath,B.fileSize);if(!x)return[];return dl(Yz4(x),K)}
```

Identify by role (names will drift):

- `<LOADER>` — the function name (e.g. `Wz4`)
- `<EXIST>` — the existence check (e.g. `uz`)
- `<FIND_FILE>` — finds path/size given session id (e.g. `qz4`)
- `<READ_BUF>` — Patch H's read function (e.g. `Rz4`)
- `<PARSE>` — JSONL → message array (e.g. `Yz4`)
- `<DL>` — the chain-walk + filter pipeline (e.g. `dl`)
- `<PATH>` — node `path` module under bundler-assigned name (e.g. `jK`)
- `<FS_PROMISES>` — `fs.promises` under bundler-assigned name (e.g. `Y8`)
- `<FS_RAW>` — `fs` module with `.readFile` used by `<READ_BUF>` (e.g. `ml`)

Replace the function body. The new shape (using the 2.1.126 names; substitute as needed):

```js
async function Wz4(V,K){if(!uz(V))return[];let B=await qz4(V,K?.dir);if(!B)return[];let x=await Rz4(B.filePath,B.fileSize);if(!x)return[];let _parsed=Yz4(x);let _seen=new Set(_parsed.map(_m=>_m.uuid));let _dir=jK.dirname(B.filePath);let _entries=await Y8.readdir(_dir);let _filesParsed=new Map();for(let _pass=0;_pass<10;_pass++){let _dangling=[];for(let _m of _parsed)if(_m.type==="system"&&_m.subtype==="compact_boundary"&&!_m.parentUuid&&_m.logicalParentUuid&&!_seen.has(_m.logicalParentUuid))_dangling.push(_m.logicalParentUuid);if(_dangling.length===0)break;let _maxByFile=new Map();for(let _lpu of _dangling){for(let _name of _entries){if(!_name.endsWith(".jsonl"))continue;let _path=jK.join(_dir,_name);if(_path===B.filePath)continue;let _siblingMsgs=_filesParsed.get(_path);if(!_siblingMsgs){let _buf=await ml.readFile(_path);let _str=_buf.toString("utf-8");if(!_str.includes(`"uuid":"${_lpu}"`))continue;_siblingMsgs=Yz4(_buf);_filesParsed.set(_path,_siblingMsgs)}let _found=-1;for(let _i=0;_i<_siblingMsgs.length;_i++)if(_siblingMsgs[_i].uuid===_lpu){_found=_i;break}if(_found===-1)continue;let _prev=_maxByFile.get(_path);if(_prev===void 0||_found>_prev)_maxByFile.set(_path,_found);break}}if(_maxByFile.size===0)break;let _newPrepend=[];for(let[_path,_maxIdx]of _maxByFile){let _siblingMsgs=_filesParsed.get(_path);for(let _i=0;_i<=_maxIdx;_i++){let _m=_siblingMsgs[_i];if(_m&&!_seen.has(_m.uuid)){_newPrepend.push(_m);_seen.add(_m.uuid)}}}if(_newPrepend.length===0)break;_parsed=[..._newPrepend,..._parsed]}return dl(_parsed,K)}
```

Key behaviors:

- **Per-file max-index slicing**: when multiple dangling lpus point into the same sibling
  file, take the slice through the *furthest* lpu's target index (covers all of them).
- **Fixed-point**: if the prepended sibling's own messages introduce *new* dangling lpus
  pointing to yet another file, the loop runs another pass.
- **No try/catch**: errors propagate up. A malformed sibling or unreadable file fails the
  whole load loudly, instead of silently giving back the un-extended chain.

`jK` (path), `Y8` (fs.promises), `ml` (fs) are already in scope from `qz4`, `xU`, `Rz4`
respectively. Substitute the actual bundler-assigned names if they differ in your version.

### Verify

```
grep -c '_dangling.push(_m.logicalParentUuid)' $EXT/extension.js
```

Expect `1`. Also verify the function still parses:

```
node --check $EXT/extension.js && echo "syntax OK"
```

### Test

After reload, open a session that was forked from a compacted parent (parent stitches with
cross-file lpus). Scrollback should reach back through the parent session's pre-fork
content. First-load latency increases by the parse time of the matched sibling files
(typically 1–2 s for a 56 MB sibling).

For diagnostic logging, the maintainer's `cdp_instrument.mjs`-style approach (described in
the project's NOTES) attaches via the `--inspect-extensions` port and logs each pass's
`{dangling, files, prepend}` to a side-channel file.

## Step 13 — Patch K: lost+found-style recovery for dangling logicalParentUuid

### Why

Auto-compaction can write a `compact_boundary` whose `logicalParentUuid` references a uuid
that never gets persisted to disk — the upstream write-side bug at `compact.ts:598`
(see `findLast(m => m.type !== 'progress')` filter missing on the auto-compact path; same
filter is present on the partial-compact path at L1014). After Patches D + J fail to resolve
such a pointer (no parent in any sibling JSONL), the chain walker stops at the boundary and
the entire pre-compaction transcript becomes invisible despite being intact on disk.

Patch K is the read-side mitigation: at session-load time, detect dangling boundaries and
splice a synthetic seam ghost (claiming the missing lpu) parented to the in-file
predecessor, plus a bookend ghost at chain root. Both render as visibly-marked colored
bubbles so the user knows the recovered span may not all belong to this conversation.

Filed upstream as [#55818](https://github.com/anthropics/claude-code/issues/55818).

### Locate (extension.js loader)

The Patch J site (Step 12) ends with `if(_newPrepend.length===0)break;_parsed=[..._newPrepend,..._parsed];}return dl(_parsed,K)` — the closing brace ends J's fixed-point loop, then `dl()` is called. K's block goes between those two: after the loop, before the `dl()` call.

### Patch (extension.js)

Insert immediately after Patch J's loop tail. The block scans for boundaries with
unresolved `logicalParentUuid`, synthesizes a seam ghost claiming a `pfgk-seam-…`
prefixed uuid (rewriting the boundary's lpu to it), then if K fired anywhere, prepends
a `pfgk-bookend-…` ghost at chain root by reparenting the original first chain-participant
to it.

```js
let _kFired=!1;
for(let _i=0;_i<_parsed.length;_i++){
  let _m=_parsed[_i];
  if(_m.type==="system"&&_m.subtype==="compact_boundary"&&!_m.parentUuid&&_m.logicalParentUuid&&!_seen.has(_m.logicalParentUuid)){
    let _predUuid=null;
    for(let _j=_i-1;_j>=0;_j--){if(_parsed[_j].uuid){_predUuid=_parsed[_j].uuid;break}}
    if(!_predUuid)continue;
    let _seamUuid="pfgk-seam-"+_m.uuid.slice(0,8);
    let _origLpu=_m.logicalParentUuid;
    let _ghost={type:"user",uuid:_seamUuid,parentUuid:_predUuid,sessionId:_m.sessionId,timestamp:_m.timestamp,
      message:{role:"user",content:"\u{1F53A} Orphaned compaction pointer (seam)\n\nThe compactor referenced a chain predecessor uuid ("+_origLpu.slice(0,8)+"…) that was never persisted to disk — a Claude Code bug. Pre-compaction history above this notice has been reattached via the in-file predecessor by Patch K. Click to jump to the start of the recovered chain."}};
    _parsed.splice(_i,0,_ghost);
    _m.logicalParentUuid=_seamUuid;
    _seen.add(_ghost.uuid);
    _kFired=!0;
    _i++
  }
}
if(_kFired){
  for(let _i=0;_i<_parsed.length;_i++){
    let _r=_parsed[_i];
    if(_r.uuid&&_r.parentUuid==null&&!_r.logicalParentUuid&&_r.type!=="system"){
      let _bid="pfgk-bookend-"+_r.uuid;
      let _be={type:"user",uuid:_bid,parentUuid:null,sessionId:_r.sessionId,timestamp:_r.timestamp,
        message:{role:"user",content:"\u{1F53B} Recovered orphan chain (start)\n\nThe content below this notice was orphaned by a Claude Code compaction bug. The compact boundary further down referenced a chain predecessor that was never persisted to disk; the in-file pre-compaction history was reattached as a best-effort fallback by Patch K. Click to jump to the seam at the end of the recovered section."}};
      _parsed.splice(_i,0,_be);
      _r.parentUuid=_bid;
      _seen.add(_bid);
      break
    }
  }
}
```

(Use `🔺` / `🔻` surrogate-pair form for the emoji in JS source if
the literal `\u{...}` form isn't accepted.)

### Patch (webview/index.js)

The chain walker output flows to the user-message renderer. Bare ghost messages render as
plain user bubbles (no markdown — the renderer treats content as plain text inside a
`<span>`). To make the seam/bookend visually distinct, wrap them with a colored container
+ click-to-scroll handler — detected out-of-band by the `pfgk-` uuid prefix.

Anchor (the user-message render path):

```
if(Z.type==="user"){if(Z.parentToolUseId)return null;if(Z.isSynthetic)return null;return n1.default.createElement(XR0,{session:$,message:Z,index:J,context:Y,key:J,isHighlighted:X,areThinkingBlocksExpanded:Q,setAreThinkingBlocksExpanded:G,setInputError:q,onCreateNewSession:z})}
```

Variable names will drift; identify by structure: this is the only `Z.type==="user"`
branch that creates `XR0` after the two early-return guards. Replace the `return`
expression with a wrap-on-prefix block:

```js
let _ws=n1.default.createElement(XR0,{...same args...});
if(typeof Z.uuid==="string"){
  let _r=Z.uuid.startsWith("pfgk-bookend")?"bookend":Z.uuid.startsWith("pfgk-seam-")?"seam":null;
  if(_r){
    let _o=_r==="seam"?"bookend":"seam";
    let _bg=_r==="seam"?"rgba(255,159,28,0.20)":"rgba(220,53,69,0.18)";
    let _bd=_r==="seam"?"#ff9f1c":"#dc3545";
    let _emoji="⚠️";  // ⚠️
    _ws=n1.default.createElement("div",{
      className:"pfgkAlert pfgk-"+_r,
      "data-pfgk-role":_r,
      style:{background:_bg,borderLeft:"4px solid "+_bd,borderRadius:"6px",padding:"6px 12px 12px",margin:"6px 0",cursor:"pointer"},
      title:"Click to jump to "+_o,
      onClick:function(){var _t=document.querySelector("[data-pfgk-role=\""+_o+"\"]");if(_t)_t.scrollIntoView({behavior:"smooth",block:"center"})}
    },
      n1.default.createElement("style",{key:"_pfgks"},".pfgkAlert .content_xGDvVg.collapsed_xGDvVg{max-height:none!important}.pfgkAlert .truncationGradient_xGDvVg{display:none}.pfgkAlert .buttonContainer_xGDvVg{display:none}.pfgkAlert .actionButton_v2CdxQ{display:none}"),
      n1.default.createElement("div",{key:"_pfgkemoji",style:{fontSize:"42px",textAlign:"center",lineHeight:1.1,padding:"6px 0 4px",userSelect:"none"}},_emoji),
      _ws
    )
  }
}
return _ws;
```

The injected `<style>` rule suppresses the `Show more`/`Show less` collapse button, the
truncation gradient, and the edit/fork action button — none of which make sense on a
synthetic message. The rule-class names (`content_xGDvVg`, `collapsed_xGDvVg`, etc.) come
from the bundle's CSS modules and may drift between releases — locate by inspecting the
DOM around a real user-message bubble if any rule stops applying.

### Critical: don't set `isMeta:true` on the ghosts

The chain walker's render filter (`Sz4` in 2.1.126) drops messages with `isMeta` truthy.
We rejected setting it on the synthetic ghosts because that hides them. Compact summary
messages render despite being functionally synthetic because they don't set `isMeta`
(only `isCompactSummary`, which Sz4 doesn't check).

### Verify

```
grep -c '_kFired=!0' $EXT/extension.js
grep -c 'pfgk-bookend' $EXT/extension.js
grep -c 'pfgkAlert pfgk-' $EXT/webview/index.js
node --check $EXT/extension.js && node --check $EXT/webview/index.js
```

Each grep should be ≥ 1.

### Test

Open a session known to have a dangling lpu (search for a `compact_boundary` whose
`logicalParentUuid` resolves to no `"uuid":"…"` line anywhere on disk). Reload VSCode.
The chat panel should render the bookend at the top of the recovered span and the seam
at the boundary, both as colored bubbles with a ⚠️ banner. Clicking either should
smooth-scroll to the other.

## Step 14 — summary to the user

Report which version was patched and which files were touched, using
markdown relative links. Remind the user to reload the VSCode window for the
patches to take effect.

Also summarize any drift observations from any earlier step (0 through 13):
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
  patch ID (A–K), and a minimal repro grep.

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
