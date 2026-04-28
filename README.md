# claude-patches

Out-of-tree patches for the Antigravity build of the Anthropic Claude Code
extension (`anthropic.claude-code-*-linux-x64`).

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
│   │                        # used by patch-antigravity skill at ~/.claude/skills/
│   └── apply-patch-fg.py    # version-tolerant apply script for Patches F and G;
│                            # discovers bundle-globals (storage class, fs/path/
│                            # projectRoot resolver) by structural anchors so the
│                            # same script applies to 2.1.120, 2.1.121, and future
│                            # releases that don't restructure the relevant code.
├── prebuilt/
│   └── 2.1.121/
│       └── apply.py         # version-pinned Patches F+G apply script,
│                            # verified byte-stable against this exact bundle.
│                            # Other versions get added as they're verified.
├── docs/
│   └── patches.md           # detailed per-patch documentation
└── README.md                # this file
```

## Usage

### As a Claude Code skill

Symlink the skill into your local skill directory:

```sh
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skill" ~/.claude/skills/patch-antigravity
```

Then in Claude Code: `/patch-antigravity` and follow along.

### Manual, after a fresh extension install

Patches A–E are structural-locator patches and live in `skill/SKILL.md` —
follow the instructions there or invoke the skill.

Patches F and G live as a single Python script:

```sh
# If your installed version has a prebuilt:
python3 prebuilt/2.1.121/apply.py     # auto-detects extension path

# Otherwise, the version-tolerant script discovers anchors on the fly:
python3 skill/apply-patch-fg.py
```

Both are idempotent. Both back up the original bundle to
`extension.js.pre-patchFG.bak` before writing.

After applying, reload the VSCode window for the patches to take effect.

## What if my version isn't in `prebuilt/`?

Run `skill/apply-patch-fg.py` instead — it discovers symbols structurally.
If it succeeds, please open a PR adding `prebuilt/<version>/apply.py` (just
copy `skill/apply-patch-fg.py`) so the next user with that version doesn't
need the discovery step.

If the script reports anchor counts that aren't 1, the bundle has shifted
structurally — see `skill/SKILL.md` for how to relocate the anchors and
update the regexes.

## Caveats

- Patches modify Anthropic's distributed bundle. Use at your own risk.
  No warranty.
- The patches are tied to specific structural shapes; sufficiently
  extensive Antigravity refactoring will break them. The version-tolerant
  script will refuse to write rather than corrupt the bundle.
- Reload VSCode after applying. The extension host caches `extension.js`
  and won't pick up changes mid-session.
