---
name: patch-claude
description: Reapply Juraj's twelve local patches to all anthropic.claude-code IDE extension installs. Discovers every install across VS Code, Antigravity, Cursor, VSCodium via CLAUDE_CODE_EXECPATH + glob. Use when the user says "the extension updated, reapply patches" or similar. Backs up files, reapplies all twelve patches, verifies each one.
---

# Reapply anthropic.claude-code extension patches

**Patchset version**: `1.8`

Twelve patches live out-of-tree and need to be reapplied every time a bundled
`anthropic.claude-code-*` extension updates. The minified code
changes variable names between releases, so do NOT blindly search-and-replace
literal strings from prior versions. Locate the pattern structurally, then
edit.

## Step 0: boot, self-update, locate install, try the prebuilt

This single bash block does three things in one round-trip:

1. **Self-update** the skill if it's a symlinked clone of
   `ojura/claude-patches` (fast-forward only; aborts on dirty / non-FF
   state with a helpful message).
2. **Locate all target installs** via `CLAUDE_CODE_EXECPATH` (the IDE-
   hosted Claude Code CLI sets this directly to the running install)
   plus a fallback glob across `~/.<ide>/extensions/` for every IDE
   that pulls from Open VSX or the VS Code marketplace (VS Code,
   Antigravity, Cursor, VSCodium, etc.). Antigravity's authoritative
   extensions dir is `~/.antigravity-ide/`; the older `~/.antigravity/`
   is deprecated (the `~/.*/extensions/` glob still catches a stale
   install there if one lingers, and dedup-by-realpath handles overlap).
   Collects all installs, deduplicates by realpath, and patches each one.
3. **Fetch the prebuilt for each version** and run it. Prebuilts are
   self-validating (detect the patchset signature, idempotent,
   byte-stable verified at synthesis time), so this step either
   applies the patches cleanly OR no-ops because they're already
   applied.

```sh
set -u

# NOTE: the harness may run this block under zsh (here it does). In zsh,
# `path` is a special array bidirectionally tied to `PATH`: assigning a
# scalar to `path` silently clobbers `PATH` and breaks all later command
# resolution (and the tie writes through `readonly`, so no tripwire helps).
# Never reuse a zsh special-var name as a scratch local here: path, cdpath,
# fpath, manpath, mailpath, module_path, psvar, status, argv, options,
# commands. This is why Step 0a's SSH->HTTPS fallback below uses `rpath`.

# --- Step 0a: self-update via fast-forward, if symlinked-clone setup ---
# Discover by repo origin URL, not by skill directory name. The user is
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
  # remote is github/gitlab SSH, fall back to HTTPS. Public-repo reads
  # don't need auth. Stays generic: works for any fork, no hardcoded
  # owner/repo.
  if ! git -C "$REPO_ROOT" fetch --quiet origin 2>/dev/null; then
    remote_url="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
    https_url=
    case "$remote_url" in
      git@github.com:*|git@gitlab.com:*)
        host="${remote_url#git@}"; host="${host%%:*}"
        rpath="${remote_url#git@*:}"; rpath="${rpath%.git}"
        https_url="https://${host}/${rpath}.git"
        ;;
    esac
    if [ -n "$https_url" ]; then
      echo "  configured fetch failed; retrying via $https_url"
      if ! git -C "$REPO_ROOT" fetch --quiet "$https_url" \
            main:refs/remotes/origin/main 2>&1; then
        echo "  WARNING: HTTPS fallback also failed. origin/main is stale."
      fi
    else
      echo "  WARNING: fetch failed and remote isn't a recognized github/gitlab SSH URL. origin/main may be stale."
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

# --- Step 0b: locate ALL target installs ---
# CLAUDE_CODE_EXECPATH is set to different things in different layouts:
#   - IDE-hosted: .../extensions/anthropic.claude-code-X.Y.Z[-<platform>]/resources/native-binary/claude
#   - Standalone CLI: ~/.local/share/claude/versions/X.Y.Z (no extension to patch)
# Walk the path looking for an "anthropic.claude-code-*" component with
# extension.js, so we don't get fooled by the standalone layout.
# Collect ALL installs across all IDEs, not just the first match.
# VS Code marketplace installs have no platform suffix (e.g.
# anthropic.claude-code-2.1.145); Open VSX adds one (e.g.
# anthropic.claude-code-2.1.146-linux-x64). Both are valid.
EXTS=()
_seen_real=()

# EXECPATH-derived install goes first (the running one).
if [ -n "${CLAUDE_CODE_EXECPATH:-}" ]; then
  P="$CLAUDE_CODE_EXECPATH"
  while [ "$P" != "/" ] && [ "$P" != "." ] && [ -n "$P" ]; do
    case "$(basename "$P")" in
      anthropic.claude-code-*)
        if [ -f "$P/extension.js" ]; then
          _real="$(readlink -f "$P")"
          EXTS+=("$P")
          _seen_real+=("$_real")
        fi
        break
        ;;
    esac
    P="$(dirname "$P")"
  done
fi

# Glob across all IDE extension dirs for any remaining installs.
for _ext in ~/.*/extensions/anthropic.claude-code-*; do
  [ -f "$_ext/extension.js" ] || continue
  _real="$(readlink -f "$_ext")"
  _dup=0
  for _s in "${_seen_real[@]+"${_seen_real[@]}"}"; do
    if [ "$_s" = "$_real" ]; then _dup=1; break; fi
  done
  [ "$_dup" = 1 ] && continue
  EXTS+=("$_ext")
  _seen_real+=("$_real")
done

if [ ${#EXTS[@]} -eq 0 ]; then
  echo "ABORT: could not locate any extension installs."
  exit 1
fi
echo "Found ${#EXTS[@]} install(s):"
for _ext in "${EXTS[@]}"; do echo "  $_ext"; done

# --- Step 0c: try prebuilts for each install ---
# Prefer the local clone's copy (already pulled by Step 0a) over curl.
# curl from raw.githubusercontent.com is blocked by Claude Code's
# auto-mode classifier ("code execution from external source"), so
# the local path isn't just faster, it's the only one that works
# in auto-approve mode.
_manual_count=0
_applied=0
_first_manual=

_ver_from_dir() {
  basename "$1" | sed 's/^anthropic.claude-code-//; s/-\(linux\|darwin\|win32\|alpine\)-\(x64\|arm64\)$//'
}

for EXT in "${EXTS[@]}"; do
  VER="$(_ver_from_dir "$EXT")"
  echo ""
  echo "--- $EXT (version $VER) ---"

  PREBUILT=
  if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/prebuilt/$VER/apply.py" ]; then
    PREBUILT="$REPO_ROOT/prebuilt/$VER/apply.py"
    echo "Local prebuilt for $VER: $PREBUILT"
  elif curl -fsSL -o "/tmp/apply_${VER}.py" "https://raw.githubusercontent.com/ojura/claude-patches/main/prebuilt/$VER/apply.py" 2>/dev/null; then
    PREBUILT="/tmp/apply_${VER}.py"
    echo "Remote prebuilt for $VER downloaded to $PREBUILT"
  fi
  if [ -n "$PREBUILT" ]; then
    # Stale-prebuilt guard: if local clone exists, compare the prebuilt's
    # embedded signature to version.py. A stale prebuilt (signature older than the
    # current `pfg-vX.Y`) would detect its own old signature as
    # "Already patched" and exit: a false positive that silently skips
    # the newer patches. Treat stale as "no prebuilt".
    if [ -n "$REPO_ROOT" ]; then
      CURRENT_SIG="$(python3 "$REPO_ROOT/version.py" 2>/dev/null)"
      PREBUILT_SIG="$(grep -oE '/\*pfg-v[0-9.]+\*/' "$PREBUILT" | head -1)"
      if [ -n "$CURRENT_SIG" ] && [ "$PREBUILT_SIG" != "$CURRENT_SIG" ]; then
        echo "Prebuilt is stale ($PREBUILT_SIG vs current $CURRENT_SIG); treating as no prebuilt."
        PREBUILT=
      fi
    fi
  fi
  if [ -n "$PREBUILT" ]; then
    python3 "$PREBUILT" "$EXT"
    _applied=$((_applied + 1))
  else
    echo "No prebuilt for $VER. Needs manual application."
    _manual_count=$((_manual_count + 1))
    if [ -z "$_first_manual" ]; then _first_manual="$EXT"; fi
  fi
done

echo ""
echo "=== Summary: ${_applied} applied, ${_manual_count} need manual ==="
if [ "$_manual_count" -eq 0 ]; then
  echo "PATCHES_APPLIED: all ${#EXTS[@]} install(s) patched; reload your IDE(s)."
  exit 0
else
  # Set EXT/VER for the first manual-needed install so Steps 2-14 proceed.
  EXT="$_first_manual"
  VER="$(_ver_from_dir "$EXT")"
  echo "No prebuilt for ${_manual_count} install(s); falling through to manual application."
  echo "Starting with: $EXT (version $VER)"
fi
```

**How to interpret the output**:

- `RESTART_SKILL` → stop and re-invoke the skill (the local clone got
  fast-forwarded; SKILL.md on disk is now newer than what you read).
- `PATCHES_APPLIED` → skill is complete. Tell the user to reload their IDE(s).
  Do NOT run any verification greps: they target manual-path splices
  with placeholder param names and will give false negatives on
  prebuilt-applied code. The patchset signature embedded in
  `extension.js` is the authoritative check, and the prebuilt verifies
  it itself.
  **HOWEVER**: check whether the prebuilt's signature matches the
  *current* patchset version (`python3 "$REPO_ROOT/version.py"`). If
  the prebuilt is from an older patchset (signature older than the current
  `pfg-vX.Y`), it applies stale patches and the "Already patched"
  no-op is a false positive. In that case **fall through to the
  maintainer section** to synthesize an updated prebuilt: restore from
  `.bak`, apply ALL patches (A–L) manually per Steps 3–14 below +
  apply-patch-fg.py, then run `build-prebuilt.py`, commit, and push.
  Do NOT ask the user for permission: synthesize-and-push is part of
  the skill's defined maintainer workflow.
- `ABORT: ...` → stop and surface the message to the user; don't try
  to "fix" the abort condition automatically.
- `No prebuilt for $VER` → apply the patches manually as follows:
    1. **Patches A–E**: follow Steps 3–7 (per-splice manual application).
    2. **Patches F and G**: do NOT splice manually. Run
       `skill/apply-patch-fg.py`. It locates anchors via regex (so it
       handles variable-name drift across releases automatically) and
       embeds the patchset signature comment (current value:
       `python3 "$REPO_ROOT/version.py"`) that the prebuilt relies on
       for idempotency. Steps 8 and 9 below describe the splices
       structurally for reference only.
    3. **Patches H, I**: follow Steps 10–11 (per-splice manual
       application). Both are short, single-anchor splices.
    4. **Patches J + K (combined)**: follow Step 13. The current K splice (v1.4+)
       replaces the loader's body wholesale and incorporates J's
       cross-file fixed-point loop, so apply them together; do NOT
       run Step 12 separately. Step 12 is preserved as the structural
       reference for J in isolation. Step 13 also covers the
       webview/index.js render wrap that makes K's seam/bookend/bridge
       ghosts visually distinct.
    5. **Patch L**: follow Step 14 (one-line spawn-args splice that
       forces `--thinking-display summarized` on every IDE-spawned
       claude subprocess). Required for Opus 4.7+ to render thinking
       summaries in the chat panel; without it the on-disk thinking
       blocks come back empty.
    6. If the F+G script reports anchors not matching uniquely, the
       bundle structure has shifted enough to break its regexes.
       End-user fallback: apply F+G manually from Step 8 and Step 9,
       then **stop**; do not attempt the maintainer steps below.
       Tell the user a maintainer will need to update the script
       and publish a prebuilt for this version.

### Maintainer-only: synthesize and publish a prebuilt

The steps below assume push access to the upstream `claude-patches`
repo. If you don't have that, skip this section: you have patches
applied locally and that is enough.

#### Pre-step 0: don't synthesize-and-publish on every 404

A 404 on `prebuilt/<VER>/apply.py` (Step 0c) does NOT automatically
mean "publish a new prebuilt". Three distinct cases produce 404, with
different correct actions:

1. **Genuine new version, no prebuilt yet** (e.g., upstream just
   released `2.1.<N>` and nothing has been synthesized for it).
   Action: apply patches manually via Steps 2–13 and 8/9, then if
   you have push access, synthesize and publish.

2. **Version was deliberately archived as broken.** Check
   `prebuilt/archive/broken/v*/<VER>/` (the broken archive is
   namespaced by patchset version: e.g.
   `prebuilt/archive/broken/v1.4/2.1.126/`). If any patchset's broken
   archive has this bundle version, the prior published prebuilt was
   found buggy. See `prebuilt/archive/broken/README.md` for
   per-version diagnoses.
   Action: do NOT re-synthesize from your live install if its `.bak`
   was the source of the original broken prebuilt; you'll just
   reproduce the bug. `build-prebuilt.py` has a guardrail that
   refuses to publish a byte-identical-to-archived-broken prebuilt,
   but it's a backstop, not a substitute for understanding why the
   archive entry exists. End-user fallback in this case is the same
   as for genuine new versions: manual application via Steps 2–13.

3. **Version was archived as superseded** (e.g., older patchset
   version under `prebuilt/archive/v<patchset>/`). Same handling as case 1
   if a current-patchset prebuilt is missing.

To check which case you're in:

```sh
ls "$REPO_ROOT"/prebuilt/archive/broken/*/$VER/ 2>/dev/null && \
    echo "WARNING: this bundle version has a known-broken prebuilt under some patchset; read the archive README before publishing"
ls "$REPO_ROOT"/prebuilt/archive/v*/$VER/     2>/dev/null && \
    echo "Note: superseded older-patchset-version prebuilt exists; this is fine"
```

To check publish capability without guessing at identity, dry-run a
push from the local clone (this just probes credentials; it doesn't
actually push):

```sh
# Uses $REPO_ROOT discovered in Step 0a.
git -C "$REPO_ROOT" push --dry-run origin main 2>&1 | head -1
```

A successful dry-run (`Everything up-to-date` or a list of refs that
would advance) means you can publish. An auth error means you can't;
stop here.

If `apply-patch-fg.py` succeeded as-is (preferred path):

1. Verify the signature is in live:
   `grep -cF "$(python3 "$REPO_ROOT/version.py")" "$EXT/extension.js"`
   must be `1`.
2. Run `build-prebuilt.py`, commit, push.

If `apply-patch-fg.py`'s regexes failed on this version:

1. Update the regex anchors in `skill/apply-patch-fg.py` to cover
   the new shape.
2. Restore `extension.js` from `.bak` (or `.pre-patchFG.bak` if
   present) and re-run the script; verify it now applies cleanly
   AND embeds the signature.
3. Commit the script change in the same push as the prebuilt.

```sh
# Precondition: signature must already be in live (apply-patch-fg.py ran)
SIG="$(python3 "$REPO_ROOT/version.py")"
grep -qF "$SIG" "$EXT/extension.js" || \
    { echo "ABORT: signature $SIG missing; run apply-patch-fg.py first"; exit 1; }

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
the signature) becomes part of the prebuilt, so the signature must
already be present in live at synthesis time.

---

The remaining steps below are only relevant if Step 0 reported "no
prebuilt": manual per-patch application against the located `$EXT`
install.

## Step 2: back up the three target files

```
cp $EXT/extension.js           $EXT/extension.js.bak
cp $EXT/webview/index.js       $EXT/webview/index.js.bak
cp $EXT/webview/index.css      $EXT/webview/index.css.bak
```

Skip the backup if a `.bak` already exists (don't overwrite a prior backup
with already-patched content).

## Step 3: Patch A: fork session writes a `custom-title` entry IF the head 64KB is otherwise unparseable

### Why

`forkSession` creates a new JSONL but emits no metadata entry. When the
session was previously compacted, the fork JSONL starts with
`isCompactSummary: true` followed by long tool results, and the session metadata
parser (`Pp` / `Jq4`) can't find a valid prompt in the 64KB head buffer,
returns `null`, and the fork is filtered out of `listSessions`. The webview
then falls back to a blank session.

**Two refinements over the naive fix:**

- Use `custom-title` rather than `last-prompt` for the metadata channel.
  Resolver order is `customTitle || aiTitle || lastPrompt || summary || firstPrompt(head)`.
  Writing `last-prompt` would make the fork discoverable but also poison
  the title channel (lastPrompt wins over firstPrompt, the unrelated
  #32150 / #49996 bug). `custom-title` puts the rescue in the right channel:
  highest precedence, stable, overridable by user `/rename`.
- Only write the entry when actually needed. If the fork chain has a valid
  first user prompt within head 64KB, the parser's `firstPrompt` extractor
  works on its own; no metadata write required, and the fork's title is
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

- `<SESSION_ID>`: the argument to `this.loadedSessions.add(...)` in the
  return statement.
- `<FILE_PATH>`: first argument to the earliest `<FS>.promises.appendFile(..., M)`
  in this function (the one that writes the JSONL body).
- `<SRC_PATH>`: the local that holds the **source** JSONL path. Look for
  `<X> = <pathJoin>(<dir>, \`${<sourceSessionIdParam>}.jsonl\`)`, typically
  one assignment up from where the existing `for(let M of await <loader>(<X>))`
  iterates the source file for file-history-snapshots.
- `<MESSAGES>`: the array whose `.uuid`s are put into
  `this.sessionMessages.set(<SESSION_ID>, new Set(...))`.
- `<FS>`: whatever module object `.promises.appendFile` is called on
  (`w1`, `M1`, etc).
- The splice point is the exact substring
  `this.summaries.set(<LEAF_UUID>,<SUMMARY>);return this.loadedSessions.add(<SESSION_ID>),<SESSION_ID>}}`.
  Insert the block between the first `;` and `return`.

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

1. **Read source title**: slurps `<SRC_PATH>` and scans line-by-line for
   `custom-title` / `ai-title` entries; remembers the most recent of each.
2. **Walk fork messages forward**: finds the first valid user prompt
   (skipping `isCompactSummary` / `isMeta` / tool-result-only content) and
   tracks the byte offset of that prompt's full JSONL line.
3. **Decide what to write**:
   - If source has explicit title → inherit it as the fork's `customTitle`
     (regardless of head 64KB parseability; keeps fork in sync with source's
     displayed name, including post-`/rename`).
   - Else if head 64KB parser would resolve a valid prompt → don't write
     anything (firstPrompt extractor handles it).
   - Else → write rescue `customTitle` derived from the first user message
     anywhere in the chain, or `"Forked conversation"` as last-resort.

`Buffer` is a Node global available in this context.

### Watch the `\n` inside the backticks

Each of the three template literals in the inserted block
(`split(`\n`)`, the `Buffer.byteLength(...+`\n`,"utf8")`, and the
`appendFile(..., ...+`\n`)`) contains the two-character escape
backslash-n, not a literal newline. If you apply this patch via
`python3`, write the splice's NEW string as a raw string (`r"..."`)
or double-escape (`\\n`); otherwise Python eats the backslash and
emits a real LF instead. The resulting JS is functionally identical
(either form produces a newline in a template literal), but
byte-diverges from the canonical splice, which costs you
byte-stability against the published prebuilt. This is the one spot
in the entire patchset where Steps 3-7 prose can be transcribed two
ways that both parse and run, so the trap is real even for a careful
reader.

### Verify

```
# When applied: at most 1 occurrence (could be 0 if the test fork's head
# happens to satisfy the parser without the rescue write, but the patch
# itself is in the source as a single block).
grep -c 'type:"custom-title",customTitle:_titleToWrite,sessionId:' $EXT/extension.js
```
Expect `1` (the literal injection is present once in source; the `if`
guard around it determines whether it runs at fork time).

The inserted block references several forkSession locals by their
minified names (`<FS>`, `<SRC_PATH>`, `<MSG>`, `<FPATH>`, `<SID>`), and
NONE of `<SRC_PATH>`/`<MSG>`/`<FPATH>`/`<FS>` is pinned by the OLD
anchor (the anchor is only the `summaries.set/return` tail). So they
are not validated by the match and must be re-derived against the
target's forkSession, per habit 2 in Step 13. The trap that already
shipped: `<SRC_PATH>` (the `${sourceId}.jsonl` path the rescue reads)
was carried from a prior bundle as `D`, but in a later bundle `D` is a
uuid-remap `Map` and the path is `q` (`readFile(D,...)` then throws and
is swallowed by the try/catch, so source-title inheritance silently
never runs). Confirm the read targets the actual source-path local:

```
# the variable inside readFile(<X>,"utf8") must be the one assigned
# <pathJoin>(<dir>,`${<sourceId>}.jsonl`), not a Map or other local
grep -oE 'readFile\([A-Za-z_$]+,"utf8"\)' $EXT/extension.js
```

## Step 4: Patch B: sticky message header → linear scroll

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

1. Main rule: strip `position:sticky`, both `background-image` gradients,
   `z-index:2`, and `top:0`; replace with `position:relative;z-index:auto`:

   ```
   .message_<S>.stickyHeader_<S>{--sticky-bg:var(--app-primary-background);position:relative;z-index:auto;align-items:stretch;padding-top:14px;padding-bottom:12px}
   ```

2. Expanded-variant rule: `z-index:3` → `z-index:auto`:

   ```
   .message_<S>.stickyHeader_<S>:has([aria-expanded=true]){z-index:auto}
   ```

### Verify

```
grep -oE '\.message_<S>\.stickyHeader_<S>[^}]*\}' $EXT/webview/index.css
```

The first two rules should contain `position:relative` / `z-index:auto` and
no `sticky` or `background-image:linear-gradient`.

## Step 5: Patch C: disable broken `isSlashCommand` detection

### Why

The webview infers "this user message is a slash command" via
`text.startsWith("/")`, which false-positives on any message that begins
with a Unix path (e.g. compiler output pasted as a prompt). The
slash-command render path drops the `userMessageContainer` wrapper and loses
the fork/rewind action button. In-band signalling from message text is
wrong in principle; kill it.

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

## Step 6: Patch D: chain walker bridges compaction boundaries via logicalParentUuid

### Why

The compact stitch (`type:"system", subtype:"compact_boundary"`) is written
with `parentUuid:null` and the actual pre-compact predecessor stored in
`logicalParentUuid`. The chain-walking code in `extension.js` follows
`parentUuid` only, so any path that reads back the conversation (rewind UI,
fork-action discoverability, `--resume` render) sees only post-last-compaction
messages. The pre-compact transcript is on disk but invisible.

Architecture (verified against the leaked source: `buildConversationChain` at
`src/utils/sessionStorage.ts:2069`, `getMessagesAfterCompactBoundary` at
`src/utils/messages.ts:4643`): the API path is bounded independently of
the parentUuid chain shape by `getMessagesAfterCompactBoundary`, which scans for the
boundary marker. So making the chain walker follow `logicalParentUuid` is
safe; it doesn't blow up the API context, only restores UI/fork visibility.

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
on the session class); it already has the `K=!1` opt-in fallback, used correctly
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
Continuation of the source session should still work normally; the API
slice is unaffected.

## Step 7: Patch E: title resolver puts `firstPrompt` ahead of `lastPrompt`

### Why

The session-list metadata parser resolves a session's title via the chain:

    customTitle || aiTitle || lastPrompt || summary || firstPrompt(head)

`lastPrompt` outranking `firstPrompt` causes title drift on every long-enough
session: the title becomes "whatever the user most recently typed" instead
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

## Steps 8 & 9: Patches F and G: USE THE SCRIPT

```sh
python3 "$REPO_ROOT/skill/apply-patch-fg.py" "$EXT/extension.js"
```

The script handles both F (+F.2 +F.3) and G (+G.1 +G.2). It locates
anchors via regex with named captures, so renamings like `m1`→`c1`
(storage class) or `[z,L]`→`[z,A]` (sessionPanels destructure) are
absorbed automatically. It also embeds the patchset signature
into `extension.js` after the `updateSessionState` method's opening brace, which
`build-prebuilt.py` will then capture into the synthesized prebuilt.
The literal signature value comes from `version.py`, which extracts
it from this file's `**Patchset version**` line above.

If the script reports anchors not matching uniquely, the bundle
structure has shifted enough to break the regexes. Fall back to
the manual splice descriptions in Step 8 (Patch F) and Step 9
(Patch G) below to get the patches applied locally, then stop.
Updating the script and synthesizing a refreshed prebuilt is a
maintainer task; see the "Maintainer-only" subsection under
Step 0.

After the script runs successfully, jump to Step 10 (Patch H).

---

## Step 8: Patch F: rename writes propagate through `sessionStates` Map (manual reference)

### Why

Renaming a session via the sidebar pencil icon flips the new title back
to the previous one within seconds, typically when the user switches
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
renames (pencil icon) do not. The sidebar's `q8` is constructed in
`resolveSessionListView` with `void 0` and no `onSessionStateChanged`
callback, so nothing ever pushes the new title into the Map. The next
`broadcastSessionStates()` (focus change, busy-state flip on any session,
panel switch) overwrites the just-renamed `summary.value` with the
stale title.

The fix has three coordinated splices:

1. **`updateSessionState` preserves missing fields**: let callers update
   only the title (or only the state) without clobbering the other.
2. **`q8.renameSession` invokes `onSessionStateChanged` after success**:
   pushes the new title into the manager's Map and triggers a broadcast.
3. **Sidebar `q8` ctor wires `onSessionStateChanged`**: without this,
   sidebar-driven renames still wouldn't propagate (the callback is the
   only escape hatch from `q8` to the manager's Map).

### Locate and patch

All three sites are in `extension.js`. Match patterns are unique on the
2.1.120 build; variable names will change between releases. Locate by
the literal patterns `sessionStates.set(V,{sessionId:V,state:K,title:B})`,
`renameSession(V,K,B){return{type:"rename_session_response"`, and the
unique `,void 0,()=>this.<broadcastUsageUpdate>())` tail of the
`resolveSessionListView`-equivalent `q8` instantiation (the only `q8`
ctor call that passes `void 0` for the panel-reference slot).

#### Site 1: `updateSessionState`

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

#### Site 2: `q8.renameSession`

Old:
```
async renameSession(V,K,B){return{type:"rename_session_response",skipped:await(await m1.load(this.cwd,this.logger)).renameSession(V,K,B)}}
```
New:
```
async renameSession(V,K,B){let _r=await(await m1.load(this.cwd,this.logger)).renameSession(V,K,B);if(!_r)this.onSessionStateChanged?.(V,void 0,K);return{type:"rename_session_response",skipped:_r}}
```

`_r` is `m1.renameSession`'s `skipped` flag; `true` means the storage
short-circuited (e.g. `aiTitle` skipped because `customTitle` already
exists). Only invoke the callback when an actual write happened.
Optional chaining covers the case where `onSessionStateChanged` is
unwired on a particular `q8` instance (e.g. before Site 3 has been
applied; the patch is internally robust to partial application).

#### Site 3: sidebar `q8` ctor

The `,void 0,()=>this.<broadcastUsageUpdate>())` tail is unique on
2.1.120: the only `q8` instantiation passing `void 0` for the panel
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
(`this.isFullEditor=I;this.onSessionStateChanged=F`; last two params
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
traces, then trigger the rename and switch sessions. There should now
be exactly one write (from `Vn.renameSession` itself), with no later
write from `index.js:2044` carrying the old title.

### Patch F.2 + F.3: close two regressions revealed by panel-side stale summaries

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
   flips back on the next state event, even with Patch F's three
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

#### F.2: drop title at the `update_session_state` boundary

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
broadcast-supplied title; the placeholder filter
`N.title || N.state !== "idle"` only renders them when they have a
non-idle state (which they will, otherwise nothing would have called
`update_session_state` for them in the first place). In practice the
sidebar's `Vn.sessions` includes everything on disk in the cwd, so
this case is rare.

#### F.3: manager writes `panel.title` directly

Combine into the F-site-1 splice (only one match for the original
unpatched anchor; combine to keep the apply pass single-step):

Old (still the original; applies cleanly even if F site 1 hasn't been
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

## Step 9: Patch G: forked session appears in sidebar without sending a message (manual reference)

### Why

Forking a session creates a new JSONL on disk via
`m1.forkSession`, but the new session doesn't appear in the sidebar
list until the user sends a message in it. Same shape as the rename
bug: the sidebar's `Te1` component re-fetches the
session list (`$.listSessions()`) only when its
`G.length > 0` `useEffect` fires, and `G` is built from broadcast Map
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
so we can read the source's metadata and use that. Both will agree.

### G.1: panel ctor callback supports skip-bookkeeping flag

The panel callback wired in `setupPanel` does panel-mapping
bookkeeping (`sessionPanels.set(forkSid, V); sessionPanels.delete(sourceSid) if it pointed at V`)
that's wrong when the fork hasn't been activated yet. At fork-handler
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

### G.2: `fork_conversation` handler pushes Map entry with source's title

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

`R1` (fs), `O1` (path), and `d5` (project-root resolver) are bundler-assigned
names already used by `m1.renameSession` etc., visible in scope.

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

## Step 10: Patch H: bypass the 5 MB precompact-skip optimization

### Why

The bundled session loader skips parsing pre-compact-boundary content for any JSONL > 5 MB
([leaked source `sessionStoragePortable.ts:480`](https://github.com/yasasbanukaofficial/claude-code/blob/main/src/utils/sessionStoragePortable.ts#L480) defines the `SKIP_PRECOMPACT_THRESHOLD = 5 * 1024 * 1024` constant; the gate lives in
[`sessionStorage.ts:3536-3556`](https://github.com/yasasbanukaofficial/claude-code/blob/main/src/utils/sessionStorage.ts#L3536-L3556)).
Content before the boundary is *never parsed*, so the chain walker, fork picker, rewind UI, and
chat-panel render all only see post-most-recent-`compact_boundary` messages on big files.
Patch D's `parentUuid → logicalParentUuid` fallback can't help: the predecessor messages
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
grep -cE '!\(!0\|\|[A-Za-z_$0-9]+\(process\.env\.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP\)\)' "$EXT/extension.js"
```

Expect `1`. The original `&&!<checker>(...)` form must no longer appear.

### Test

After reload, open any session whose JSONL is > 5 MB. The chat panel should populate with
the full in-file chain back to the most recent stitch with cross-file `logicalParentUuid`
(which is then the genuine ceiling, addressed by Patch J). Cache_read on the next API turn
should remain bounded; `getMessagesAfterCompactBoundary` does its own boundary slicing
independent of what the loader returns.

## Step 11: Patch I: neutralize the webview's 500-message render cap

### Why

The webview hardcodes a cap on how many messages can live in the React state: anything
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
grep -oE 'function [A-Za-z_$]+\(\$\)\{if\(\$\.length>[A-Za-z0-9]+\)\{let [A-Za-z_$]+=\$\.length-[A-Za-z0-9]+;return \$\.slice\([A-Za-z_$]+\)\}return \$\}' $EXT/webview/index.js
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
grep -c 'function <FN>(\$){return \$}' $EXT/webview/index.js
```

Expect `1` (with the function name observed during locate).

### Test

After reload, open a session with > 500 chain-walkable messages. The full chain should
render. Note: at 10K+ messages, initial render takes a few seconds; the bottleneck is
React rendering, not the patch. If the UI lags unacceptably, partial mitigation: use
`if($.length>10000)return $.slice(-10000); return $` instead of the pure identity.

## Step 12: Patch J: cross-file logicalParentUuid resolution at session load

> **For the current K (v1.4+)**, skip this step and apply the combined J+K splice
> from Step 13 instead. The v1.4 prebuilt collapses J's fixed-point
> loop and K's four-stage synthesis into a single replacement of the
> loader's body. This Step 12 description is preserved as the
> structural reference for J in isolation, useful when debugging or
> if K is intentionally disabled.

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

- `<LOADER>`: the function name (e.g. `Wz4`)
- `<EXIST>`: the existence check (e.g. `uz`)
- `<FIND_FILE>`: finds path/size given session id (e.g. `qz4`)
- `<READ_BUF>`: Patch H's read function (e.g. `Rz4`)
- `<PARSE>`: JSONL → message array (e.g. `Yz4`)
- `<DL>`: the chain-walk + filter pipeline (e.g. `dl`)
- `<PATH>`: node `path` module under bundler-assigned name (e.g. `jK`)
- `<FS_PROMISES>`: `fs.promises` under bundler-assigned name (e.g. `Y8`)
- `<FS_RAW>`: `fs` module with `.readFile` used by `<READ_BUF>` (e.g. `ml`)

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
Note the `ml` (fs) label reflects the older reference bundle above; current bundles' read
function uses `require("fs/promises")` and the two fs roles may collapse to one symbol. See
Step 13's `<FS_PROMISES>`/`<FS_RAW>` resolution caveat before substituting.

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

## Step 13: Patch K: full conversation-tree recovery for dangling logicalParentUuid

### Why

Auto-compaction can write a `compact_boundary` whose `logicalParentUuid`
references a uuid that was never persisted to disk: the upstream
write-side bug at `compact.ts:598` (the `findLast(m => m.type !==
'progress')` filter is missing on the auto-compact path; same filter
is present on the partial-compact path at L1014). After Patches D + J
fail to resolve such a pointer (no parent in any sibling JSONL), the
chain walker stops at the boundary and the entire pre-compaction
transcript becomes invisible despite being intact on disk. In a fork
family where the same phantom-lpu is shared across siblings (forks of
a common compacted parent), this can hide the conversation's true
origin from every panel that loads it.

Patch K is the read-side mitigation. v1.4 has **four** synthesis
stages run after Patch J's cross-file fixed-point loop, before the
chain walker (`zi`/`dl`):

1. **Phantom-lpu sibling backfill.** For each phantom lpu still
   unresolved after J, scan siblings for one that ALSO has it as an
   lpu (= shares the same compaction's missing predecessor = is a
   fork of the same conversation tree) AND has pre-content before
   its first phantom-lpu boundary. Prepend that sibling's pre-content.
   Recovers the canonical origin (typically a real user message at
   chain root in the eldest sibling fork) for sessions whose own
   first line is a `compact_boundary`.
2. **Seam ghosts.** For phantom-lpu boundaries, plant `pfgk-seam-…`
   parented to the in-file predecessor; rewrite the boundary's lpu
   to point at the seam.
3. **Bridge ghosts.** For boundaries whose lpu was resolved cross-file
   by Patch J (live chain takes the cross-file shortcut), plant
   `pfgk-bridge-…` between the in-file orphan chain's leaf and the
   boundary's first child. Walker now traverses the in-file orphan
   instead of the cross-file shortcut. Cross-file content stays
   reachable via the seam path so it's still rendered.
4. **Bookend ghost (chain root marker).** If K fired, plant a
   `pfgk-bookend-…` ghost as the chain root and reparent the original
   first chain-participant onto it. Two predicates: (a) the original
   "first non-system msg with `parent==null && !lpu`"; (b) a relaxed
   "first user/assistant whose parent chain dead-ends in a phantom-lpu
   boundary" for cases where the chain root is parented to a system
   boundary.

Result: the panel for any session in a conversation family renders
the same canonical origin at top, then the full tree in chronological
order with seam/bridge/bookend ghosts marking the structural
discontinuities. Every persisted message is reachable.

Filed upstream as [#55818](https://github.com/anthropics/claude-code/issues/55818).

### Use the latest prebuilt as the canonical splice source

The two splices below (extension.js loader + webview/index.js render
wrap) are intricate enough that maintaining them as ASCII in this
doc has bitrotted in the past: v1.2 → v1.3 → v1.4 introduced
backfill + bridges + relaxed bookend, and the doc lagged. The
canonical, byte-stable shape lives in the latest published prebuilt:

```sh
LATEST_PREBUILT_VER="$(ls "$REPO_ROOT/prebuilt" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)"
LATEST_PREBUILT="$REPO_ROOT/prebuilt/$LATEST_PREBUILT_VER/apply.py"
echo "Reference: $LATEST_PREBUILT"
```

Read its `SPLICES` literal and look for the entries containing
`_kFired`, `pfgk-seam`, `_filesParsed` (extension.js loader splice;
combined with Patch J in v1.4) and `pfgkAlert` (webview render wrap).
The loader is a single replace pair `(old, new)`; the render wrap is
**two** pairs (the `return` -> `let _ws=` binding conversion and the
pfgk wrap, see the render-wrap Critical note below).

**Trust is not transitive across a synthesis.** The prebuilt is a
translation of once-correct code against a *prior* bundle. A
`build-prebuilt.py` run from a non-pristine `.bak` can drop a
transformation that was present on both sides of its diff, and a NEW
string copied from a prior version can carry that version's variable
names into a bundle where those names mean something else. Both ship a
splice that passes every cheap check yet is wrong on a fresh install.
This has happened repeatedly (loader-head read-fn call, render-wrap
binding conversion, loader parse-arg `BE0(x)`, Patch A `readFile(D)`;
see `prebuilt/archive/broken/`). Four habits keep it from recurring:

1. **Verify reachability, not presence.** Anchor-matches-once, literal
   present, `node --check` clean, and byte-stable are all *necessary
   and insufficient*: dead or mis-bound code passes every one. Confirm
   the inserted code actually executes (bound before use, not after a
   `return`).
2. **Re-derive every identifier the NEW introduces that is NOT in the
   OLD anchor.** The anchor only validates the tokens it contains. Any
   variable the NEW references beyond the anchor (and every variable in
   a wholesale body rewrite like the J+K loader) is unvalidated by the
   match: it may resolve to a binding of the WRONG role in the target
   (a buffer named `x` in the source bundle is `B` here; a source-path
   `D` is a `Map` here). For each such identifier, confirm against the
   target bundle that it is bound AND has the right role (a path is a
   path, a buffer is the one the read-fn just assigned). Several splices
   introduce non-anchor identifiers, not just one or two: measuring the
   2.1.159 prebuilt, the Patch A rescue (`G,H,q,s1`), the J+K loader
   (`Pa,m1`, and the buffer the parse consumes), the K render-wrap (the
   React var `n1` + inline styling), and the F/G splices (callback
   params + discovered aliases) all do; the small in-place edits
   (B/C/D/E/H/I/L, the render binding conversion) introduce nothing
   beyond their anchor. These differ in MITIGATION, not kind. F/G are
   generated by `apply-patch-fg.py`, which re-derives its identifiers
   from the target, so they self-correct; the render-wrap references
   tokens that have been stable across bundles. Patch A and the J+K
   loader are the MOST exposed (not the only exposed): their NEW is
   carried verbatim from a prior bundle and translated by hand with no
   re-derivation, which is why both shipped a drifted-variable bug.
   Don't trust a short "these are the risky ones" list; measure each
   splice's introduced identifiers (tokens in NEW not in OLD, minus its
   own `_`-locals) and check every one against the target. But split
   them into two kinds with different failure modes, or you will
   over-flag: **function-locals** (a buffer, a path/array var inside the
   patched function) can resolve to the wrong ROLE, not just a different
   name (the `x`-unbound and `D`-as-`Map` class above) so verify name AND
   role against the target function; **top-level `require` aliases**
   (path / fs / fs-promises) are NOT scoped that way. The application
   code is one flat top-level scope: esbuild's `__commonJS`/`__esm`
   closures wrap only the vendored npm modules in the file header, never
   the patch targets, so every `var X=require(...)` is hoisted and in
   scope throughout, and the bundle usually has several redundant
   aliases for the same module. Re-derive which alias NAME exists (it
   drifts across versions), but "this alias is in a different module / out
   of scope" is a FALSE alarm for app code, and any matching top-level
   alias works. (A reviewer once flagged the loader's `m1.dirname` as
   out-of-scope by assuming per-module closures; measuring brace depth
   put `DE0` and the `m1=require("path")` binding both at depth 0, same
   scope.) To settle a scope question, measure brace nesting with a
   regex-aware tokenizer: a naive brace counter goes negative on the
   bundle's regex literals and will mislead you.
3. **Triangulate against a known-good *installed* bundle**, not sibling
   prebuilts. When a splice looks structurally off (references a
   variable its own old/new never binds, etc.), find an
   already-patched install of a nearby version on disk and read the
   working shape there. Sibling prebuilts can share the same latent
   defect; a live install that renders correctly cannot.
4. **Diff the splice *count* against a known-good prior version.** A
   region that drops from N splices to N-1 between versions (e.g. the
   webview render wrap going 4 -> 3) is a red flag that a
   transformation fell out of the diff.

### Apply

The two splices are baked against the bundle var names of the
prebuilt's version. Translate them to the current target's names:

**extension.js loader (combined J + K splice).** The `old` anchor in
the prebuilt is the original loader function body
(`return <CHAIN_WALKER>(<PARSE>(x),K)}` form, no J or K applied yet).
Find the loader by its post-Patch-H signature
(`function <LOADER>(V,K){if(!<EXIST>(V))return[];let B=await <FIND_FILE>(V,K?.dir);…return <CHAIN_WALKER>(<PARSE>(x),K)}`)
and identify by role:

- `<LOADER>`: the function name
- `<EXIST>`: the existence check
- `<FIND_FILE>`: finds path/size given session id
- `<READ_BUF>`: Patch H's read function
- `<PARSE>`: JSONL → message array
- `<CHAIN_WALKER>`: chain-walk + filter pipeline
- `<PATH>`: node `path` module under bundler-assigned name
- `<FS_PROMISES>`: the module `.readdir` is called on, under bundler-assigned name
- `<FS_RAW>`: the module `<READ_BUF>` calls `.readFile` on

Resolve `<FS_PROMISES>` and `<FS_RAW>` by the *method called on them* in the
loader neighborhood (`.readdir`, and the `.readFile` inside `<READ_BUF>`), not
by their `require` target, and do NOT assume they are two distinct symbols:

- **`<FS_RAW>` is `require("fs/promises")`, not the callback-style `fs`
  module.** Its `.readFile` returns an awaitable `Promise<Buffer>` (the block
  does `await <FS_RAW>.readFile(...)`). Grepping for `require("fs")` to find it
  comes up empty; grep for what `<READ_BUF>`'s `.readFile` is called on.
- **`<FS_PROMISES>` and `<FS_RAW>` may be the SAME symbol.** The bundler
  sometimes emits two separate `fs/promises` aliases (one carrying `.readdir`,
  one carrying `.readFile`) and sometimes dedups them to one. Map both block
  members onto whatever symbol(s) the current bundle uses; one name serving
  both is correct, not a mistake.

Substitute these in the prebuilt's `new` string and apply. Confirm
exactly one occurrence of the `old` anchor before replacing. (Warning:
**don't apply Patch J as a separate splice if you're using the current K (v1.4+)**
because the v1.4 prebuilt combines J + K into one splice that replaces the
loader's original body wholesale. Skip Step 12 in this case.)

**webview/index.js render wrap.** This is **two** splices in the
prebuilt's webview SPLICES, not one (a known-good render-wrap has
*four* webview splices total: render-cap, isSlashCommand, and these
two). The user-message render branch contains
`if(Z.type==="user")…return <REACT>.default.createElement(<USER_MSG_COMPONENT>,{…})`.
Identify `<USER_MSG_COMPONENT>` (it drifts every bundle; React var
`<REACT>`, signal/prop names, and CSS module names are stable). Then
apply both: (1) the binding conversion `return … createElement(<USER_MSG_COMPONENT>,…`
-> `let _ws=… createElement(<USER_MSG_COMPONENT>,…`, and (2) the pfgk
wrap that reassigns and returns `_ws`. Omitting (1) makes (2) dead
code; see "Critical: the render-wrap needs the `return` -> `let _ws=`
binding conversion" below before applying.

### Critical: don't set `isMeta:true` on the ghosts

The chain walker's render filter (`Sz4` in 2.1.126) drops messages
with `isMeta` truthy. We rejected setting it on the synthetic ghosts
because that hides them. Compact summary messages render despite
being functionally synthetic because they don't set `isMeta` (only
`isCompactSummary`, which Sz4 doesn't check).

### Critical: Patch K depends on Patch D being co-applied

Canonical K's seam stage sets only `boundary.logicalParentUuid =
seam_uuid` and never touches `parentUuid`. The chain walker reaches
the seam only through the `parentUuid → logicalParentUuid` fallback
that Patch D installs. Apply K without D and the seam ghost is
unreachable from the walker and never renders, even though it sits in
the parsed array. Treat K and D as a single behavioral unit. If you
ever want to ship K standalone, the seam must also rewrite
`parentUuid`, which the canonical does not.

Verified empirically by an independent from-scratch reconstruction of
K from the prose alone: working on a D-less pristine file, the
reconstruction deduced this dependency and defensively wrote both
fields, exposing the silent coupling.

### Critical: the loader head has TWO drift-sensitive tokens

The J+K splice replaces the loader's body wholesale, so it must
reproduce the head exactly:
`let <buf>=await <READ_BUF>(<find>.filePath,<find>.fileSize); ... let _parsed=<PARSE>(<buf>); ...`.
Two distinct tokens here bite, and both pass `node --check` +
byte-stability + presence-greps:

1. **The `<READ_BUF>` call name.** Mangle it during translation and
   `await KE0(...)` becomes `await E0(...)` or, worst, `await 0(...)`.
   A bare `0(...)` (or any wrong name) is a valid call expression that
   throws `<x> is not a function` at runtime. (Shipped once.)
2. **The buffer local that `<PARSE>` consumes.** The body must parse
   the SAME local the head bound: `<PARSE>(<buf>)`. When the NEW string
   is carried from a prior bundle, `<buf>` is easy to leave as the
   prior bundle's name: e.g. the source bundle named it `x`, this
   bundle names it `B`, and `BE0(x)` survives. `x` is then an unbound
   free variable -> `ReferenceError: x is not defined` on every load.
   (Shipped once: `BE0(x)` where the head bound `B`.)

The loader has no try/catch, so either fault breaks every session load.
Both went unnoticed partly because the affected bundle was dormant
(Antigravity loads the highest version on disk); see
`prebuilt/archive/broken/`. Note the worked example in Step 12 names
the buffer `x` (`let x=await Rz4(...)`); that is exactly the name that
must NOT survive into a bundle whose buffer local is something else.

Guard it after applying (substitute the real names), and confirm the
parse argument is the head's buffer local, not a leftover:

```
grep -c 'await <READ_BUF>(<find>.filePath,<find>.fileSize)' $EXT/extension.js  # expect 1
grep -c 'await 0('       $EXT/extension.js                                     # expect 0
grep -c '_parsed=<PARSE>(<buf>)' $EXT/extension.js                             # expect 1 (<buf> = the head's local)
# and: the only standalone single-letter free var in the loader body should be <buf>/<find>, nothing else
```

Neither guard nor `node --check` substitutes for reading the loader
head once with your own eyes; it is the cheapest catch.

### Critical: the render-wrap needs the `return` -> `let _ws=` binding conversion

The webview render-wrap is **two** coordinated edits, not one:

1. **Binding conversion** at the user-message branch:
   `return <REACT>.createElement(<USER_MSG_COMPONENT>,{...})` must become
   `let _ws=<REACT>.createElement(<USER_MSG_COMPONENT>,{...})`.
2. **The pfgk wrap** appended after it:
   `;if(typeof Z.uuid==="string"){...;_ws=<REACT>.createElement("div",{className:"pfgkAlert ..."},...,_ws)}}return _ws}`.

Edit 2 reassigns and finally returns `_ws`, so it only works if edit 1
bound `_ws` in the first place. Skip the binding conversion and the
branch still `return`s the bare user element on its first statement,
making the whole pfgk block **unreachable dead code**. The ghosts the
loader inserts then render as plain collapsed user bubbles: no color,
no warning glyph, no click-navigation, and the un-collapse `<style>`
never fires.

This passes every cheap check: `grep -c 'pfgkAlert pfgk-'` is 1
(the literal is present, just after a `return`), `node --check`
passes (dead code is valid syntax), and byte-stability passes. Same
presence-not-reachability blind spot as the loader-head gotcha above.

**Root cause of the recurrence (this has regressed more than once):**
the binding conversion vanishes from a prebuilt when that prebuilt is
synthesized from a base whose `.bak` is **not pristine**, i.e. already
`let _ws=`-patched. `build-prebuilt.py` diffs live against `.bak`; if
both sides already say `let _ws=`, the conversion is identical on both
and falls out of the captured splices, leaving only the pfgk-wrap
splice. The next version then copies that incomplete render-wrap
verbatim and ships dead code on genuinely pristine installs. **Always
synthesize from a genuinely pristine `.bak`** (Step 2's backup, taken
before any patching), and treat a render-wrap that has 3 webview
splices where a known-good prior version had 4 as a red flag.

Guard it after applying (substitute the real names):

```
# user element must be BOUND, not returned, before the pfgk block:
grep -c 'let _ws=<REACT>.createElement(<USER_MSG_COMPONENT>,' $EXT/webview/index.js  # expect 1
grep -c 'return <REACT>.createElement(<USER_MSG_COMPONENT>,'  $EXT/webview/index.js  # expect 0
```

Reachability, not presence, is the property you are verifying. See
`prebuilt/archive/broken/` for the two times this shipped.

### Patch K's behavioral contract (not derivable from the prose above)

The K block lives only in the prebuilt; the prose describes the four
stages conceptually but withholds the contracts a from-scratch
reconstruction needs to behave correctly. If you ever modify K (or
synthesize an equivalent for a future patchset that touches the
loader shape), the canonical guarantees the following:

- **Five marker kinds.** Beyond seam / bookend / bridge there is
  `pfgk-broken-<term.uuid>`, emitted in the walker's `!_root` branch
  (the up-walk's terminal node `_term` is not a legitimate root) with a
  ⛔ "UNRECOVERABLE" payload that reports where the walk stopped (the
  terminal's short uuid, type, and unresolved `parentUuid`/`lpu`) as a
  single post-walk verdict, not a list of per-cause guesses.
  Triggered when a user/assistant's `parentUuid` walk (cycle-guarded by a visited set, otherwise
  unbounded) dead-ends at a phantom boundary K could not backfill. The fifth
  is a slate-toned clean-seam variant (`dg:"seamClean"`) for in-file
  compactions the walk bridges with no phantom.
- **`message.content` is a string at construction, but the assembler
  reshapes it.** Loader-side, each ghost is built with
  `message.content` as a string (`"PFGK1:"+JSON.stringify({...})`).
  Between loader and render, the IDE's message assembler (`DT→Mn`)
  converts that string into a single-element block array on a `wG`
  instance, and `wG` has no `.message` field. The render-wrap
  therefore recovers the payload from `block.content.text` (the
  `Array.isArray(Z.content)` path), not `Z.message.content`. A
  from-scratch reconstruction that reads `.message.content` off the
  assembled message gets `undefined` and renders blank cards.
- **uuid prefix is the only discriminator.** The render-wrap keys
  entirely off `Z.uuid.startsWith("pfgk-seam-")` etc.; there are no
  marker fields on the ghost object. Adding `isPfgkGhost` /
  `pfgkKind` / `logicalParentUuid: null` is harmless but dead
  weight.
- **Marker stages gate in the walker, not on a loader flag.** The
  loader tracks `_kFired` (a seam was planted) and `_kAttempted` (a
  phantom boundary was seen), but the current build's downstream marker
  stages read neither, so `_kAttempted` is dead weight (written, never
  read). The bookend and broken markers are emitted in the
  cycle-guarded chain-walker from one post-walk verdict on whether the
  up-walk reached a legitimate root (broken fires under `!_root`,
  bookend when a root was reached); the bridge is a loader-side
  cross-file marker. Gating bookend / broken on a loader flag was the
  superseded v1.5 -> v1.6 design; see `docs/patches.md`.
- **Ambiguity propagation.** K1 backfill tracks `_ambigCount` per
  phantom and an aggregate `_ambigPhLpus`. When >1 sibling
  qualified, K prepends a `"⚠ AMBIGUOUS RECONSTRUCTION: ..."`
  paragraph to the affected seam content and to the bookend
  content, and embeds a `_k1Sources` provenance table
  (per-phantom: source filename, prepended count, candidate count)
  inside the bookend essay.
- **Wall-clock instrumentation is surfaced to the user, not
  logged.** Canonical records `_kT0` / `_kTparse` / `_kTjprepend`
  / `_kTk1` and embeds a "K stitching wall-clock: parse Xms, J
  cross-file prepend Yms, ..." line in the bookend and broken
  essays. There is no side-channel logging.
- **Bridge detection reads `_seen`, no separate set required.** A
  boundary qualifies as a bridge if `!parentUuid &&
  logicalParentUuid && _seen.has(lpu) &&
  !String(lpu).startsWith("pfgk-")`: "in `_seen` but not a pfgk
  ghost" reliably means "J pulled it in cross-file." The `!pfgk-`
  guard prevents re-handling seams the earlier stage already
  rewrote. Same-session guard (`_prev.sessionId ===
  _m.sessionId`) skips cross-session predecessors.
- **Bridges append, seams splice.** Canonical does
  `_parsed.push(_bridge)` (append, relying on the chain walker to
  re-thread by parentUuid) but `_parsed.splice(_i, 0, _seam)`
  (in-place for seams).
- **The webview render-wrap is part of K, not a separate concern.**
  It renders `div.pfgkAlert.pfgk-<role>` with per-role tone tokens
  (defined in `_PFTOK`; see `docs/patches.md` for the live palette), a
  `MARKER n OF m` counter with `↓`/`↺` next-and-cycle navigation computed
  from `$.messages.peek()`, a per-role glyph, and a `data-pfgk-role`
  attribute used by the cyclic-scroll `onClick`. It also **injects a `<style>`
  block that un-collapses the bubbles**
  (`.content_xGDvVg.collapsed_xGDvVg{max-height:none}` plus rules
  hiding truncation and buttons). Without that style injection,
  the long diagnostic essays render collapsed and most of the
  content is hidden. Skip the render-wrap and the loader splice's
  ghosts technically render, but as truncated plain user bubbles
  with no navigation.

Empirical basis: a from-scratch reconstruction of K against this
prose (no prebuilt access) recovered the four stages, their
ordering, the no-`isMeta` constraint, and the most-pre-content
backfill heuristic, but invented every concrete contract above. The
list is the minimum set you need to keep K behaviorally equivalent
across future patchset bumps or independent reimplementations.

### Verify

```
grep -c '_kFired=!0' $EXT/extension.js
grep -c 'pfgk-bookend' $EXT/extension.js
grep -c 'pfgk-bridge' $EXT/extension.js
grep -c 'pfgkAlert pfgk-' $EXT/webview/index.js
node --check $EXT/extension.js && node --check $EXT/webview/index.js
# loader head must still call the read fn by name (see Critical note above):
grep -c 'await <READ_BUF>(<find>.filePath,<find>.fileSize)' $EXT/extension.js  # expect 1
grep -c 'await 0(' $EXT/extension.js                                           # expect 0
# render-wrap must be REACHABLE: user element bound, not returned (see Critical note):
grep -c 'let _ws=<REACT>.createElement(<USER_MSG_COMPONENT>,' $EXT/webview/index.js  # expect 1
grep -c 'return <REACT>.createElement(<USER_MSG_COMPONENT>,'  $EXT/webview/index.js  # expect 0
```

Each grep should be ≥ 1 (or, for `pfgk-bridge`, ≥ 1 if any boundary
was cross-file resolved by Patch J; embedded literal regardless),
except the two reachability guards (`await 0(` and the `return
<REACT>.createElement(<USER_MSG_COMPONENT>,` form) which must be 0.

### Test

Open a session known to have a dangling lpu (search for a
`compact_boundary` whose `logicalParentUuid` resolves to no
`"uuid":"…"` line anywhere on disk). Reload VSCode. The chat panel
should render the bookend at the top of the recovered span, seams
at each phantom-lpu boundary, and bridges at each cross-file-resolved
boundary, all as colored bubbles with a ⚠️ banner. Clicking any
ghost cycles to the next.

## Step 14: Patch L: IDE-spawned CLI always passes `--thinking-display summarized`

### Why

For `claude-opus-4-7[1m]` (and presumably any 4.7+ model), Anthropic
changed the API default for the `thinking.display` field from
`"summarized"` to `"omitted"`. Documented in their own migration
guide: [Migrating to Claude Opus 4.7](https://platform.claude.com/docs/en/about-claude/models/migration-guide#migrating-to-claude-opus-4-7).
With `display: "omitted"`, the API returns thinking content blocks
with an empty `thinking` field and a multi-KB `signature` only. The
webview renders these as the static `<div class="thinkingStatic">Thinking</div>`
stub because its `thinking.length > 0` branch can't fire.

Background tracker: [#49902](https://github.com/anthropics/claude-code/issues/49902),
[#49322](https://github.com/anthropics/claude-code/issues/49322),
[#49268](https://github.com/anthropics/claude-code/issues/49268),
[#8477](https://github.com/anthropics/claude-code/issues/8477) (and
seven more variants in the same thread). Anthropic has not shipped a
first-party fix as of v2.1.142.

Upstream-fix proposal: [#59844](https://github.com/anthropics/claude-code/issues/59844)
documents two one-line options: dropping the
`!getIsNonInteractiveSession()` gate from the CLI's
`K3.display = "summarized"` assignment (preferred) or this same
extension splice as the fallback. If/when upstream lands either,
Patch L becomes retire-able.

The bundled CLI HAS a gate that sets `display = "summarized"` when
the user's `settings.json` has `showThinkingSummaries: true`, but the
gate is `!getIsNonInteractiveSession() && showThinkingSummaries === true`.
The IDE spawns the CLI subprocess with `--print --input-format
stream-json --output-format stream-json`, which makes
`getIsNonInteractiveSession() === true`, so the auto-default never
fires for IDE chat panels. The user's setting is silently ignored
in the place it matters most.

The CLI also accepts an explicit `--thinking-display <mode>` flag
that bypasses the non-interactive gate (first branch in the gate
takes precedence). The IDE's SDK-side spawn code already KNOWS how
to pass this flag, but only when `thinkingConfig.display` is set on
the spawn-time options. The chat-panel caller never sets it, so the
flag is never pushed to argv, and the API returns redacted thinking.

This patch closes the gap by defaulting the flag to `"summarized"`
when no explicit display is configured. End-to-end DOM-verified
pre-publish: post-patch chat-panel turns produce thinking blocks
with non-empty text on disk, restoring the expandable `<details>`
render path.

### WebSearch safety

[#56984](https://github.com/anthropics/claude-code/issues/56984)
reports that the `CLAUDE_CODE_EXTRA_BODY` workaround for this same
bug breaks WebSearch (`400 Thinking may not be enabled when
tool_choice forces tool use`) and WebFetch (`400 adaptive thinking is
not supported on this model`). Those failures are about
`thinking.type` being forced into requests where the CLI's per-call
logic would have left it out or set it disabled. Patch L only sets
`display`, not `type`. The CLI's per-request gate is
`q.type !== "disabled"`, so when WebSearch / WebFetch / auto-mode
classifier paths build their own request config (often with
`q.type = "disabled"` or with a thinking-incompatible model), the
entire `thinking` field is dropped from the request regardless of
`q.display`.

Verified empirically pre-publish: ran `claude --print
--thinking-display summarized --allowed-tools WebSearch "..."` with
a forced WebSearch query and got 5+ requests, all 200 OK, no 400.

### Locate and patch

In `extension.js`, find the SDK-side spawn-args builder. The
specific anchor is the one-line gate that pushes the
`--thinking-display` flag conditionally on the config's `.display` (the variable names like `U`/`i` drift every bundle, so the greps below match any names):

```
if(U.type!=="disabled"&&U.display)i.push("--thinking-display",U.display)
```

Locate via grep:

```sh
grep -cE '[A-Za-z_$0-9]+\.type!=="disabled"&&[A-Za-z_$0-9]+\.display\)[A-Za-z_$0-9]+\.push\("--thinking-display"' "$EXT/extension.js"
```

Expect exactly 1 match.

### Splice

Old:
```
if(U.type!=="disabled"&&U.display)i.push("--thinking-display",U.display)
```

New:
```
if(U.type!=="disabled")i.push("--thinking-display",U.display||"summarized")
```

Two minimal changes:

1. Drop the `&&U.display` guard. The flag should always be pushed
   when thinking isn't disabled; the value is what varies.
2. Add `||"summarized"` as the fallback value when no explicit
   display is configured.

Effect: every IDE-spawned CLI subprocess gets
`--thinking-display summarized` in argv. The CLI's commander parser
sets `z.thinkingDisplay = "summarized"`, the K3 build site's first
branch fires (bypassing the non-interactive gate), the API request
body's `thinking.display` becomes `"summarized"`, and the API
returns thinking content blocks with text instead of the empty +
signature stub.

### Verify

```
grep -cE '[A-Za-z_$0-9]+\.type!=="disabled"\)[A-Za-z_$0-9]+\.push\("--thinking-display",[A-Za-z_$0-9]+\.display\|\|"summarized"\)' "$EXT/extension.js"
```

Expect `1`. Old form (with `&&U.display`) should be gone.

### Test

After reload: open a chat panel, ask any reasoning-heavy query
that triggers thinking, then inspect the session JSONL:

```sh
python3 -c '
import json
path = "<path-to-session.jsonl>"
for line in open(path):
    line = line.strip()
    if not line: continue
    try: m = json.loads(line)
    except: continue
    if m.get("type") != "assistant": continue
    content = (m.get("message") or {}).get("content")
    if not isinstance(content, list): continue
    for b in content:
        if isinstance(b, dict) and b.get("type") == "thinking":
            tl = len(b.get("thinking",""))
            print(f"ts={m.get(\"timestamp\")} text_len={tl}")
'
```

Post-patch turns should have `text_len > 0`. Pre-patch turns stay
at `text_len = 0` (the on-disk data can't be retroactively
recovered).

For WebSearch regression check (the residual #56984 concern): run a
real web-search query through the chat panel. The CLI should NOT
hit `400 Thinking may not be enabled when tool_choice forces tool
use`. If it does, revert the patch and use the `claudeProcessWrapper`
shim approach from #49322 comments instead, which can scope the
flag to top-level invocations only.

## Step 15: summary to the user

Report which version was patched and which files were touched, using
markdown relative links. Remind the user to reload the VSCode window for the
patches to take effect.

Also summarize any drift observations from any earlier step (0 through 14):
anchors that didn't match as written, structural shifts beyond pure
variable renaming, prebuilt-fetch / install-locate / backup quirks,
variable renames the F+G script auto-absorbed that future readers
would benefit from knowing about, and any verify-grep or other doc
bugs in this SKILL.md you noticed during application. Do this
proactively; don't wait to be asked. Then propose follow-up:

- If you have push access to `claude-patches` (the Step 0 dry-run probe
  already established this: `Everything up-to-date` or a list of
  advancing refs means yes), propose specific SKILL.md edits in the
  same response and apply on confirmation.
- If you don't, propose opening an issue at
  https://github.com/ojura/claude-patches/issues with the version, the
  patch ID, and a minimal repro grep.

If nothing drifted and nothing was wrong, say so explicitly. Silence
is ambiguous.

## Notes

- The CSS and JS files in `webview/` are minified onto a single line each;
  use `python3` for string replacement rather than the Edit tool (Read
  fails on them).
- If any of the patches cannot be located (pattern shape changed
  substantially), stop and report that one to the user rather than guessing
  (these are patches against obfuscated code and a wrong splice could be
  disruptive).
- If a `.bak` from a prior version already exists, leave it. The current
  pre-patch state is still recoverable from the VSCode extension cache /
  reinstall.
