# claude-patches

Out-of-tree patches for the Anthropic Claude Code VS Code extension
(`anthropic.claude-code-*-linux-x64`, distributed via the
[Open VSX Registry](https://open-vsx.org/extension/Anthropic/claude-code)).

These patches address eleven user-visible bugs that, as of the current
public releases, ship with the bundled extension. Each patch has a
corresponding upstream issue on
[anthropics/claude-code](https://github.com/anthropics/claude-code/issues?q=is%3Aissue+author%3Aojura);
this repo is where the *workaround* lives until upstream fixes ship.

The patchset signature `/*pfg-vN*/` stands for **Persistent Forking
Glitches**, backronymed from the original `patch-fg` script (Patches
F + G); the rest just kept getting added.

## What's patched

| | Patch | Symptom | Upstream issue |
|---|---|---|---|
| **A** | `forkSession` writes a `custom-title` rescue when the head 64KB is unparseable | Forks of compacted sessions appear blank or lose their title | [#48937](https://github.com/anthropics/claude-code/issues/48937) |
| **B** | Sticky message header → linear scroll | Tall user message at top of turn occludes the assistant reply | [#49114](https://github.com/anthropics/claude-code/issues/49114) |
| **C** | Disable broken `isSlashCommand` detection | Messages starting with `/` (Unix paths, etc.) lose the fork/rewind action button | [#49155](https://github.com/anthropics/claude-code/issues/49155) |
| **D** | Chain walker bridges compaction boundaries via `logicalParentUuid` | `--resume` and rewind UI show stale messages from days ago after compaction | [#46603](https://github.com/anthropics/claude-code/issues/46603) |
| **E** | Title resolver puts `firstPrompt` ahead of `lastPrompt` | Session title drifts to "whatever the user most recently typed" | [#32150](https://github.com/anthropics/claude-code/issues/32150) |
| **F** | Session rename propagation through `sessionStates` Map | Pencil rename flips back to old title on session switch | [#53942](https://github.com/anthropics/claude-code/issues/53942) |
| **G** | Forked session enters sidebar list immediately after fork | New fork doesn't appear in sidebar until first message is sent | [#53942 (follow-up)](https://github.com/anthropics/claude-code/issues/53942) |
| **H** | Bypass the 5 MB precompact-skip optimization in the loader | Sessions > 5 MB only show post-most-recent-compactSummary content; scrollback, fork picker, and rewind don't show anything before the most recent compact boundary | [#55700](https://github.com/anthropics/claude-code/issues/55700) |
| **I** | Neutralize the webview's 500-message render cap | Sessions with > 600 messages silently truncate to last 500 in the chat panel; no UI feedback | [#55701](https://github.com/anthropics/claude-code/issues/55701) |
| **J** | Resolve cross-file `logicalParentUuid` at session load (load sibling JSONLs to bridge fork boundaries) | Forks of compacted sessions can't scroll back into the source session's history; chain walker stops at the cross-file stitch | [#48937 (cross-file)](https://github.com/anthropics/claude-code/issues/48937) / [#46603](https://github.com/anthropics/claude-code/issues/46603) |
| **K** | `lost+found`-style read-side recovery for sessions with dangling `logicalParentUuid`: add visible markers in the chat that recover lost earlier messages from the same file | Sessions where the compactor's lpu was never persisted (write-side bug at `compact.ts:598`) silently truncate the chain at the boundary; user thinks pre-compact work was lost | [#55818](https://github.com/anthropics/claude-code/issues/55818) / [#46603](https://github.com/anthropics/claude-code/issues/46603) |

See [`docs/patches.md`](docs/patches.md) for the full per-patch breakdown
(why, locate, patch, verify, test). For *how* to introspect the running
extension via CDP (set conditional breakpoints, walk the React fiber
tree, dispatch RPCs from outside, etc.), see
[`docs/debugging.md`](docs/debugging.md). The Patch K case study at the
end of that doc walks through using all the recipes end-to-end.

## Layout

```
claude-patches/
├── skill/
│   ├── SKILL.md             # Anthropic-style skill instructions for Claude Code,
│   │                        # used by the patch-claude skill at ~/.claude/skills/
│   └── apply-patch-fg.py    # version-tolerant apply script for Patches F and G;
│                            # finds the globals it needs in the bundle (storage
│                            # class, fs/path/projectRoot resolver) by code shape,
│                            # not by name. Used as a fallback when no prebuilt
│                            # exists for the current extension version.
├── prebuilt/
│   ├── <VER>/
│   │   └── apply.py         # version-pinned, self-contained apply script
│   │                        # covering ALL patches A through K. Built by
│   │                        # diffing the live patched bundle against its
│   │                        # pristine .bak files, byte-stable verified.
│   └── archive/
│       ├── v1/              # superseded by current patchset version
│       └── broken/          # known-broken prebuilts (don't run);
│           └── v<patchset>/ # outer dir: which patchset version
│               └── <VER>/   # inner dir: which bundle version
│                            # see the README in archive/broken/ for
│                            # criteria and per-version diagnoses
├── docs/
│   ├── patches.md           # detailed per-patch documentation
│   └── debugging.md         # CDP introspection reference for the bundled
│                            # extension: port roles, BP-with-condition,
│                            # fiber walk, RPC dispatch, gotchas, case study
├── util/                    # maintainer tools; see MAINTAINER.md
├── MAINTAINER.md            # how to synthesize and push a new prebuilt
└── README.md                # this file
```

## Usage

### Install as a Claude Code skill (recommended)

One-time setup (~30 seconds):

```sh
git clone https://github.com/ojura/claude-patches ~/claude-patches
mkdir -p ~/.claude/skills
ln -s ~/claude-patches/skill ~/.claude/skills/patch-claude
```

Then in any Claude Code session: `/patch-claude`. One slash-command
applies the patches, end to end. The skill:

- **Self-updates** on each invocation (fast-forwards your local clone
  from `origin/main`; aborts cleanly if you have uncommitted local
  changes or non-FF state, instead of silently merging).
- **Auto-detects which extension install to patch**: when invoked
  from inside an IDE-hosted Claude Code session, `CLAUDE_CODE_EXECPATH`
  points directly at the running install (works regardless of which
  IDE: VS Code, Antigravity, Cursor, VSCodium, etc.). Falls back to
  globbing across `~/.<ide>/extensions/` if the env var isn't set.
- **Fetches and runs the prebuilt** for your installed version from
  this repo. The prebuilt is idempotent (detects an embedded
  `pfg-v1.7` signature and no-ops if patches are already applied) and
  byte-stable verified at synthesis time.
- **Falls back to manual per-patch synthesis** if no prebuilt exists
  yet for your version.

After the skill prints `PATCHES_APPLIED`, reload your VS Code window
and you're done.

The symlink layout means `git pull` in `~/claude-patches` updates the
skill in-place. Multi-machine: clone the repo on each box and symlink
identically. The self-update step keeps them all in sync.

### Manual application (no Claude Code installed)

If your installed extension version has a prebuilt in this repo, you
can run it directly:

```sh
VER=$(ls -d ~/.*/extensions/anthropic.claude-code-*-linux-x64 \
        | sort -V | tail -1 \
        | sed 's/.*anthropic.claude-code-//; s/-linux-x64$//')
curl -fsSL "https://raw.githubusercontent.com/ojura/claude-patches/main/prebuilt/$VER/apply.py" \
  | python3
```

The script auto-detects your extension dir (across IDE variants),
applies all 19 splices (Patches A–K), and validates with `node --check`.
Idempotent. Use `--force` to restore from `.bak` files and re-apply
unconditionally.

If your version isn't in `prebuilt/<VER>/`, file an issue and either
wait for a maintainer to synthesize one or use the Claude Code skill
above (which can synthesize per-version on the fly).

## Uninstall

To restore the unpatched extension, copy each `.bak` back over its
patched counterpart. The apply script writes a backup of every file it
touches before patching, so this is always reversible:

```sh
EXT=$(ls -d ~/.*/extensions/anthropic.claude-code-*-linux-x64 \
        | sort -V | tail -1)
cp "$EXT/extension.js.bak"        "$EXT/extension.js"
cp "$EXT/webview/index.js.bak"    "$EXT/webview/index.js"
cp "$EXT/webview/index.css.bak"   "$EXT/webview/index.css"
```

Reload VS Code. The extension is now back to whatever Open VSX shipped.

To also remove the Claude Code skill installation:

```sh
rm ~/.claude/skills/patch-claude
rm -rf ~/claude-patches              # optional: delete the local clone too
```

(VS Code auto-updates the extension occasionally. When it does, your
`.bak` files stay in the old version's directory, which gets removed
when the IDE garbage-collects old extensions. The new version's directory
has no `.bak` until you patch it.)

## For maintainers (push access)

If you have push access and need to add a prebuilt for a new extension
release, see [MAINTAINER.md](MAINTAINER.md). The short version: apply
patches A–K locally first, then
`python3 util/build-prebuilt.py <ext_dir> prebuilt/<VER>` synthesizes
and byte-stability-validates a prebuilt apply script from the diff.

End-users never need `util/`: the prebuilt `apply.py` is
self-contained (Python stdlib only) and curl-installable:

```sh
curl -fsSL https://raw.githubusercontent.com/ojura/claude-patches/main/prebuilt/<VER>/apply.py | python3
```

## Caveats

- Patches modify Anthropic's distributed bundle. Use at your own risk.
  No warranty.
- The patches are tied to specific structural shapes; sufficiently
  extensive upstream refactoring will break them. The version-tolerant
  script will refuse to write rather than corrupt the bundle.
- Reload VS Code after applying. The extension host caches `extension.js`
  and won't pick up changes mid-session.
