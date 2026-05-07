# Maintainer guide

For people with push access to this repo who want to add a prebuilt for
a new extension release.

## When to synthesize a new prebuilt

A new release of `anthropic.claude-code-*-linux-x64` lands, you want to
patch your local install, and `prebuilt/<NEW_VER>/apply.py` doesn't
exist yet. Synthesizing it takes seconds and unblocks every other user
with the same version.

## The pipeline

```
patched live extension dir          maintainer tool             new prebuilt
───────────────────────────  ──→  build-prebuilt.py  ──→  prebuilt/<VER>/apply.py
  extension.js                       │
  extension.js.bak                   │  diffs against .bak
  webview/index.js                   │  extracts unique-anchored
  webview/index.js.bak               │    (old, new) splice pairs
  webview/index.css                  │  validates byte-stability
  webview/index.css.bak              ↓
                                  apply.py
                                  ⤷ contains all 19 splices as
                                    literal Python find-and-replace
                                  ⤷ self-contained, stdlib only
                                  ⤷ idempotent, signature-detected
                                  ⤷ supports --force restore
```

## Steps

1. **Apply patches A–K locally first.**

   Either via the Claude Code skill (`/patch-claude`) or by hand
   following [`skill/SKILL.md`](skill/SKILL.md). End state: your
   extension dir has the patched `extension.js`, `webview/index.js`,
   `webview/index.css` plus pristine `*.bak` backups of each.

   Verify the patches actually work — reload VSCode, do a sidebar
   pencil rename, switch sessions, fork a session, open a session
   with a known dangling-lpu boundary (Patch K should render the seam
   + bookend ghosts). The prebuilt will be byte-identical to whatever
   you applied here, so anything wrong now ships to every downstream
   user.

2. **Synthesize the prebuilt.**

   ```sh
   git clone https://github.com/ojura/claude-patches
   cd claude-patches
   python3 util/build-prebuilt.py \
     ~/.<ide>/extensions/anthropic.claude-code-<VER>-linux-x64 \
     prebuilt/<VER>
   ```

   What the tool does:

   - Calls `util/extract-splices.py` against each of the three target
     files, diffing patched vs `.bak` and emitting minimal-context
     splice pairs widened until each `old` is unique-1 in the
     unpatched bundle.
   - Aggregates the splices into a self-contained Python script
     using a fixed template (no external dependencies, stdlib only).
   - **Validates byte-stability**: applies the synthesized script to
     fresh copies of the `.bak` files in a tempdir and confirms the
     output matches the live patched files exactly (md5 / byte
     compare). Refuses to write `prebuilt/<VER>/apply.py` if anything
     diverges.

3. **Commit and push.**

   ```sh
   git add prebuilt/<VER>/apply.py
   git commit -m "prebuilt/<VER>: synthesized $(date +%Y-%m-%d)"
   git push origin main
   ```

## Sanity checks before push

- `prebuilt/<VER>/apply.py` is ~22KB. If it's huge (>50KB) something
  drifted (probably a non-deterministic field like a timestamp or
  random UUID got captured into a splice — those should never be in
  diff regions).
- The splice count is roughly 14 in `extension.js`, 3–4 in
  `webview/index.js`, 1 in `webview/index.css` (~18–19 total at v1.4).
  Counts can drift slightly across bundles because adjacent diff hunks
  sometimes merge or split depending on the surrounding context window
  in the new minified output (observed: 19 in 2.1.126, 18 in 2.1.132,
  no functional difference). A *large* count change (e.g. 14 → 7 in
  `extension.js`) means the bundle restructured or the patches applied
  wrong — investigate before pushing. The byte-stability check is the
  authoritative correctness signal.
- `python3 prebuilt/<VER>/apply.py --help`-equivalent: just run the
  script with no args; it should auto-detect the extension dir and
  say `Already patched (signature <SIG> present). Nothing to do.`
  where `<SIG>` matches the output of `python3 version.py`. If it
  tries to apply, your live install isn't patched correctly — back
  out before pushing.

## Bumping the patchset version

If a future patch is added or an existing patch's behavior is
materially changed, bump the patchset version (semver-style minor for
add-ons like H/I/J or K, major for breaking redesigns).

Precondition: your live extension already carries the new patch
behavior (you developed it locally). The bump procedure below preserves
that working state while migrating the on-disk signature.

1. **Edit ONE line in `skill/SKILL.md`**: change
   `**Patchset version**: \`1.4\`` to the new version. That line is
   the single source of truth (see "version.py" below).

2. **Re-apply patches locally as a stability check.** Run
   `apply-patch-fg.py` against your live extension; the *only* diff vs
   the pre-bump live extension should be the signature tag itself
   (`/*pfg-vOLD*/` → `/*pfg-vNEW*/`). If anything else moves, the
   script's regex anchors drifted relative to the bundle and need
   investigation before you ship a prebuilt.

3. **Synthesize the new prebuilt(s).** For every supported extension
   version: `python3 util/build-prebuilt.py <ext_dir> prebuilt/<VER>`.
   This captures the new signature into `prebuilt/<VER>/apply.py` and
   byte-validates against the live patched files.

4. **Add a CHANGELOG entry.** `CHANGELOG.md` is a per-version
   historical log. Newest first; describe what changed and why.
   Critically, `CHANGELOG.md` is *deliberately excluded* from the
   sync allowlist (Step 6) — its `pfg-vN` mentions must stay frozen
   to the version they describe.

5. **Stage everything**: `git add -A`. Doing this *before* sync makes
   the sync's effect visible as the only unstaged delta in Step 7.

6. **Sync README.md**:
   `python3 util/sync-version-mentions.py`. Rewrites `pfg-vX[.Y]`
   mentions in `README.md` (the only file in `SYNC_TARGETS`).
   Everything else either resolves the version dynamically
   (SKILL.md, MAINTAINER.md, the Python consumers — all reach into
   `version.py`) or is deliberately frozen (`CHANGELOG.md`,
   `docs/debugging.md`, `prebuilt/archive/*`). The script exists for
   the one file where a literal version genuinely belongs in the
   text (the public-facing README).

7. **Review the unstaged diff**: `git diff`. The only changes here
   should be the sync rewrites. If anything outside the allowlist
   was touched, the script is operating on a stale list (a doc file
   gained a current-state claim that needs to be added to
   `SYNC_TARGETS`, or vice versa). If the diff inside the allowlist
   touched a line you didn't expect (e.g., a historical reference
   that ended up there), promote that line out of the allowlisted
   file or rewrite it to not match the regex.

8. **Stage and commit**: `git add -A && git commit ...`.

End-users running the new prebuilt against an older-version-patched
extension will get: *"Stale patchset (file has vX, current is vY);
restoring … from .bak"*. The `.bak` from their original run is the
pre-patch baseline — reusable as the restore point indefinitely
(until the extension itself updates).

This is why the comprehensive prebuilt covers A–K in one script: a
patchset bump can include changes anywhere, and a self-contained
prebuilt guarantees the user ends up at exactly the new state without
needing the per-patch skill walkthrough.

### Single source of truth: `version.py`

The version lives in exactly one human-readable place: a marked line
near the top of `skill/SKILL.md`. `version.py` (repo root) extracts it
via regex (`**Patchset version**: \`(\d+(?:\.\d+)?)\``) and exports
`PATCHSET_VERSION` and `SIGNATURE` constants. When invoked as a
script (`python3 version.py`) it prints the live signature on stdout,
so shell code can fetch it without hardcoding.

Consumers:
- `skill/apply-patch-fg.py` imports `PATCHSET_VERSION` and `SIGNATURE`
  from `version.py` (resolves the patch-claude symlink via
  `os.path.realpath` so the import works whether the skill is run from
  the repo or from `~/.claude/skills/patch-claude/`).
- `util/build-prebuilt.py` imports the same constants and substitutes
  them into the prebuilt template at synthesis time (no more hardcoded
  signatures in the template).
- `util/sync-version-mentions.py` imports `PATCHSET_VERSION` and
  rewrites the `SYNC_TARGETS` allowlist on demand. Run as Step 6 above.
- `skill/SKILL.md` and `MAINTAINER.md` resolve the signature
  dynamically: bash blocks invoke `python3 "$REPO_ROOT/version.py"`
  inline; prose uses abstract language ("the patchset signature").
  SKILL.md is the *holder* of the SSOT line, and MAINTAINER.md has
  the same audience-and-currency profile (internal, always read at
  HEAD), so neither benefits from a hardcoded version. Net effect:
  both are authoritative top-to-bottom and *deliberately not* in
  `SYNC_TARGETS` — only `README.md` is, because that's the public-
  facing file where a literal version earns its keep.

Drift mode that motivated this design: bumping required editing three
places (apply-patch-fg.py constant, build-prebuilt.py template SIGNATURE,
build-prebuilt.py template docstring), plus chasing scattered `pfg-vN`
mentions in README/docs. Forgetting one of those left the prebuilt's
idempotency check looking for an old signature while the splices applied
new content — silent confusion. Now there's one line to edit.

### Why sync runs separately from build-prebuilt

`util/sync-version-mentions.py` is a standalone script, not a
side-effect of `build-prebuilt.py`. It can technically run any time
after Step 1 (the sync only depends on `version.py` → `SKILL.md`, not
on the prebuilt), but the recommended order puts it as Step 6 so
its diff is reviewable in isolation in Step 7. If sync ran inside
build-prebuilt, an accidental rewrite outside the intended
SYNC_TARGETS (e.g., due to a bug in the script, or an unrelated file
that gained a `pfg-v` mention) would get buried in the same commit as
the prebuilt regeneration. Separating them surfaces sync's blast
radius as its own visible delta.

## Why the byte-stability check matters

The prebuilt's `(old, new)` pairs are derived from the diff. If the
diff captures something non-deterministic — a timestamp, a UUID, a
locale-dependent format — the synthesized script's `old` won't match
on someone else's clean install. The byte-stability check catches
this: if the synthesized script doesn't reproduce the live patched
file, something captured into the diff that shouldn't have.

In practice this hasn't happened (upstream bundles are deterministic),
but the check is the cheap insurance against it ever shipping.

### Byte-stability does NOT imply correctness

Byte-stability proves the splice is **deterministic against the .bak
you have**. It does not prove the splice produces correct *behavior*
when applied to a different .bak (e.g., a fresh extension install).

If `.bak` isn't pristine — typically because you iteratively developed
the patch in place and never re-baked from a fresh install — the
synthesis only captures the **last incremental hop**, not the full
pristine→post-patch transformation. Earlier transformations are
present in both `.bak` and live, so the diff doesn't see them.
Translating the resulting prebuilt to a fresh bundle then leaves
those earlier transformations missing, often producing dead code or
silent no-ops while passing every byte-stability check.

This bit us during 2.1.132's first synthesis: the v1.4 K webview
wrap captured from a 2.1.126 install with non-pristine `.bak` was
missing the `return createElement(...)` → `let _ws=createElement(...)`
transformation (which had been introduced in v1.2 or v1.3 K iteration
and was already in the post-iteration `.bak`). Applying the splice to
a fresh 2.1.132 install left the K wrap as dead code after the
`return` statement. Diagnosed by CDP DOM probe; fixed by adding the
missing transformation as an explicit splice. See
[`docs/debugging.md`](docs/debugging.md) "Byte-stability check is
necessary but not sufficient" gotcha.

**Maintainer rule of thumb when iterating a patch:**

- Either re-bake from a pristine extension install before final
  prebuilt synthesis (delete `<file>.bak`, reinstall extension,
  re-apply patches, then run `build-prebuilt.py`).
- Or maintain a separate immutable checkpoint like `.pre-patchK.bak`
  or `.pristine.bak` that's never overwritten across iterations.
- After publishing the prebuilt, verify it against a pristine fresh
  install before declaring it shipped: download to a clean test
  extension dir and confirm the resulting code actually runs (DOM
  probe for the rendered K marker, not just `node --check`).

## util/ scripts: what they don't do

- They don't validate that your patches are *correct* — only that
  they're *byte-stable* against the .bak in the install dir. If you
  applied Patch D wrong (only one of two walkers patched), the
  prebuilt will be byte-stable but functionally broken. Test the live
  patches before synthesizing.
- They don't auto-locate `.bak` files anywhere other than alongside
  each target. If your backups are named `.pre-patchA.bak` or live in
  another directory, copy them to `extension.js.bak` etc. before
  running.
- They don't infer the version from the bundle contents — only from
  the directory name (`anthropic.claude-code-<VER>-linux-x64`). If
  upstream ever changes that naming convention, update
  `util/build-prebuilt.py` to extract the version differently.
