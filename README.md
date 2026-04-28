# claude-patches

Out-of-tree patches for the Anthropic Claude Code VS Code extension
(`anthropic.claude-code-*-linux-x64`, distributed via the
[Open VSX Registry](https://open-vsx.org/extension/Anthropic/claude-code)).

These patches address seven user-visible bugs that, as of the current
public releases, ship with the bundled extension. Each patch has a
corresponding upstream issue on
[anthropics/claude-code](https://github.com/anthropics/claude-code/issues?q=is%3Aissue+author%3Aojura);
this repo is where the *workaround* lives until upstream fixes ship.

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

See [`docs/patches.md`](docs/patches.md) for the full per-patch breakdown
(why, locate, patch, verify, test).

## Layout

```
claude-patches/
├── skill/
│   ├── SKILL.md             # Anthropic-style skill instructions for Claude Code,
│   │                        # used by the patch-claude skill at ~/.claude/skills/
│   └── apply-patch-fg.py    # version-tolerant apply script for Patches F and G;
│                            # discovers bundle-globals (storage class, fs/path/
│                            # projectRoot resolver) by structural anchors. Used
│                            # as a fallback when no prebuilt exists for the
│                            # current extension version.
├── prebuilt/
│   └── 2.1.121/
│       └── apply.py         # version-pinned, self-contained apply script
│                            # covering ALL patches A through G. Built by
│                            # diffing the live patched bundle against its
│                            # pristine .bak files, byte-stable verified.
├── docs/
│   └── patches.md           # detailed per-patch documentation
├── util/                    # maintainer tools — see MAINTAINER.md
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
- **Auto-detects which extension install to patch** — when invoked
  from inside an IDE-hosted Claude Code session, `CLAUDE_CODE_EXECPATH`
  points directly at the running install (works regardless of which
  IDE: VS Code, Antigravity, Cursor, VSCodium, etc.). Falls back to
  globbing across `~/.<ide>/extensions/` if the env var isn't set.
- **Fetches and runs the prebuilt** for your installed version from
  this repo. The prebuilt is idempotent (detects an embedded
  `pfg-v1` signature and no-ops if patches are already applied) and
  byte-stable verified at synthesis time.
- **Falls back to manual per-patch synthesis** if no prebuilt exists
  yet for your version. After successful manual application, the
  skill prompts to synthesize a new prebuilt and push it back to the
  repo so the next user with that version skips synthesis.

After the skill prints `PATCHES_APPLIED`, reload your VS Code window
and you're done.

The symlink layout means `git pull` in `~/claude-patches` updates the
skill in-place. Multi-machine: clone the repo on each box and symlink
identically — the self-update step keeps them all in sync.

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
applies all 14 splices (Patches A–G), and validates with `node --check`.
Idempotent. Use `--force` to restore from `.bak` files and re-apply
unconditionally.

If your version isn't in `prebuilt/<VER>/`, file an issue and either
wait for a maintainer to synthesize one or use the Claude Code skill
above (which can synthesize per-version on the fly).

## For maintainers (push access)

If you have push access and need to add a prebuilt for a new extension
release, see [MAINTAINER.md](MAINTAINER.md). The short version: apply
patches A–G locally first, then
`python3 util/build-prebuilt.py <ext_dir> prebuilt/<VER>` synthesizes
and byte-stability-validates a prebuilt apply script from the diff.

End-users never need `util/` — the prebuilt `apply.py` is
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
