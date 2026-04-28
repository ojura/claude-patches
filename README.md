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

### Quickest path: prebuilt exists for your version

```sh
# Look up your installed extension version, e.g. 2.1.121
python3 prebuilt/2.1.121/apply.py
```

That's it. The script auto-detects the extension dir, applies all 14
splices (A through G), and validates with `node --check`. Idempotent
(re-running is a no-op via signature detection). Use `--force` to
restore from `.bak` files and re-apply unconditionally.

### Your version isn't in `prebuilt/` yet

You have two options:

**Option A — version-tolerant fallback for F+G only**:

```sh
python3 skill/apply-patch-fg.py
```

Discovers anchors structurally for Patches F and G. Patches A–E still
need the manual skill walk-through (see `skill/SKILL.md`).

**Option B — file an issue or wait for a maintainer**:

When a maintainer with push access patches the new version, they synthesize
a prebuilt via `util/build-prebuilt.py` (see *For maintainers* below) and
push it to this repo. After that, you get the quickest path above.

### As a Claude Code skill

Symlink the skill into your local skill directory:

```sh
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skill" ~/.claude/skills/patch-claude
```

Then in Claude Code: `/patch-claude`. The skill checks the repo
for a prebuilt matching your installed version; if found it just runs
that. Otherwise it walks through the structural-locator instructions
in `SKILL.md`.

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
