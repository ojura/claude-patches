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
                                  ⤷ contains all its splices as
                                    literal Python find-and-replace
                                  ⤷ self-contained, stdlib only
                                  ⤷ idempotent, signature-detected
                                  ⤷ supports --force restore
```

## Steps

1. **Apply all patches locally first.**

   Either via the Claude Code skill (`/patch-claude`) or by hand
   following [`skill/SKILL.md`](skill/SKILL.md). End state: your
   extension dir has the patched `extension.js`, `webview/index.js`,
   `webview/index.css` plus pristine `*.bak` backups of each.

   Verify the patches actually work: reload VSCode, do a sidebar
   pencil rename, switch sessions, fork a session, open a session
   with a known dangling-lpu boundary (Patch K should render the seam
   + bookend ghosts). The prebuilt will be byte-identical to whatever
   you applied here, so anything wrong now ships to every downstream
   user.

   For the full CDP-based iteration loop (edit live file, reload
   via CDP key events, re-discover iframe target, DOM-verify
   `[data-pfgk-role]` counts), see
   [`docs/debugging.md` "Patch development iteration loop"](docs/debugging.md)
   for the general method, and
   [`docs/patches.md` "Verifying and debugging the recovery markers"](docs/patches.md#verifying-and-debugging-the-recovery-markers)
   for the marker fixtures (`gen_demo`), the rendered-marker probes, the
   marker-specific gotchas, and the worked Patch K case studies.

2. **Synthesize the prebuilt.**

   ```sh
   git clone https://github.com/ojura/claude-patches
   cd claude-patches
   python3 util/build-prebuilt.py \
     ~/.<ide>/extensions/anthropic.claude-code-<VER>-linux-x64 \
     prebuilt/<VER>
   ```

   What the tool does:

   - Calls `util/extract_splices.py` against each of the three target
     files, diffing patched vs `.bak` and emitting minimal-context
     splice pairs widened until each `old` is unique-1 in the
     unpatched bundle.
   - Aggregates the splices into a self-contained Python script
     using a fixed template (no external dependencies, stdlib only).
   - **Validates byte-stability**: applies the synthesized script to
     fresh copies of the `.bak` files in a tempdir and confirms the
     output matches the live patched files exactly (byte-for-byte
     compare). Refuses to write `prebuilt/<VER>/apply.py` if anything
     diverges.

3. **Commit and push.**

   ```sh
   git add prebuilt/<VER>/apply.py
   git commit -m "prebuilt/<VER>: synthesized $(date +%Y-%m-%d)"
   git push origin main
   ```

## Sanity checks before push

- `prebuilt/<VER>/apply.py` is ~38KB at v1.8 (was ~24KB at v1.5, ~22KB
  at v1.4, ~17KB pre-K). Trends upward as patches accumulate. If it's
  huge (>60KB) something drifted (probably a non-deterministic field
  like a timestamp or random UUID got captured into a splice;
  those should never be in diff regions).
- The splice count is roughly 19 in `extension.js`, 4 in
  `webview/index.js`, 1 in `webview/index.css` (~24 total at v1.8).
  Counts can drift slightly across bundles because adjacent diff hunks
  sometimes merge or split depending on the surrounding context window
  in the new minified output. A *large*
  proportional drop (e.g. losing a third or more of `extension.js`'s
  splices) means the bundle restructured or the patches applied
  wrong; investigate before pushing. The byte-stability check is the
  authoritative correctness signal; the counts are a soft heuristic.
- `python3 prebuilt/<VER>/apply.py --help`-equivalent: just run the
  script with no args; it should auto-detect the extension dir and
  say `Already patched (signature <SIG> present). Nothing to do.`
  where `<SIG>` matches the output of `python3 version.py`. If it
  tries to apply, your live install isn't patched correctly. Back
  out before pushing.

## Bumping the patchset version

If a future patch is added or an existing patch's behavior is
materially changed, bump the patchset version (semver-style minor for
add-ons like H/I/J or K, major for breaking redesigns).

Precondition: your live extension already carries the new patch
behavior (you developed it locally). The bump procedure below preserves
that working state while migrating the on-disk signature.

1. **Edit ONE line in `skill/SKILL.md`**: change
   the `**Patchset version**` line to the new version. That line is
   the single source of truth (see "version.py" below).

2. **Re-apply patches locally as a stability check.** Run
   `apply-patch-fg.py` against your live extension; the *only* diff vs
   the pre-bump live extension should be the signature tag itself
   (`/*pfg-vOLD*/` → `/*pfg-vNEW*/`). If anything else moves, the
   script's regex anchors drifted relative to the bundle and need
   investigation before you ship a prebuilt.

3. **Archive stale prebuilts.** Move every `prebuilt/<VER>/apply.py`
   whose embedded `SIGNATURE` is from the *old* patchset version into
   `prebuilt/archive/vOLD/<VER>/apply.py`. This is critical: a stale
   prebuilt left in `prebuilt/<VER>/` will be fetched by the skill,
   detect its own old signature as "Already patched", and exit with a
   false-positive `PATCHES_APPLIED`, and the user never gets the new
   patches. Archive layout: `prebuilt/archive/v1.6/2.1.132/apply.py`.

4. **Synthesize the new prebuilt(s).** For every extension version you
   have installed: `python3 util/build-prebuilt.py <ext_dir> prebuilt/<VER>`.
   This captures the new signature into `prebuilt/<VER>/apply.py` and
   byte-validates against the live patched files. If you can't
   synthesize for a version you don't have installed, just archive it
   (Step 3). The skill will fall through to manual application for
   those users until someone publishes a prebuilt.

5. **Add a CHANGELOG entry.** `CHANGELOG.md` is a per-version
   historical log. Newest first; describe what changed and why.
   Critically, `CHANGELOG.md` is *deliberately excluded* from the
   sync allowlist (Step 7): its `pfg-vN` mentions must stay frozen
   to the version they describe.

6. **Stage everything**: `git add -A`. Doing this *before* sync makes
   the sync's effect visible as the only unstaged delta in Step 7.

7. **Sync README.md**:
   `python3 util/sync-version-mentions.py`. Rewrites `pfg-vX[.Y]`
   mentions in `README.md` (the only file in `SYNC_TARGETS`).
   Everything else either resolves the version dynamically
   (SKILL.md, MAINTAINER.md, and the Python consumers all reach into
   `version.py`) or is deliberately frozen (`CHANGELOG.md`,
   `docs/debugging.md`, `prebuilt/archive/*`). The script exists for
   the one file where a literal version genuinely belongs in the
   text (the public-facing README).

8. **Review the unstaged diff**: `git diff`. The only changes here
   should be the sync rewrites. If anything outside the allowlist
   was touched, the script is operating on a stale list (a doc file
   gained a current-state claim that needs to be added to
   `SYNC_TARGETS`, or vice versa). If the diff inside the allowlist
   touched a line you didn't expect (e.g., a historical reference
   that ended up there), promote that line out of the allowlisted
   file or rewrite it to not match the regex.

9. **Stage and commit**: `git add -A && git commit ...`.

End-users running the new prebuilt against an older-version-patched
extension will get: *"Stale patchset (file has vX, current is vY);
restoring … from .bak"*. The `.bak` from their original run is the
pre-patch baseline, reusable as the restore point indefinitely
(until the extension itself updates).

This is why the comprehensive prebuilt covers every patch in one script: a
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
  rewrites the `SYNC_TARGETS` allowlist on demand. Run as Step 7 above.
- `skill/SKILL.md` and `MAINTAINER.md` resolve the signature
  dynamically: bash blocks invoke `python3 "$REPO_ROOT/version.py"`
  inline; prose uses abstract language ("the patchset signature").
  SKILL.md is the *holder* of the SSOT line, and MAINTAINER.md has
  the same audience and is always read at HEAD, so neither benefits
  from a hardcoded version. Net effect:
  both are authoritative top-to-bottom and *deliberately not* in
  `SYNC_TARGETS`. Only `README.md` is, because that's the public-
  facing file where a literal version earns its keep.

Drift mode that motivated this design: bumping required editing three
places (apply-patch-fg.py constant, build-prebuilt.py template SIGNATURE,
build-prebuilt.py template docstring), plus chasing scattered `pfg-vN`
mentions in README/docs. Forgetting one of those left the prebuilt's
idempotency check looking for an old signature while the splices applied
new content, causing silent confusion. Now there's one line to edit.

### Why sync runs separately from build-prebuilt

`util/sync-version-mentions.py` is a standalone script, not a
side-effect of `build-prebuilt.py`. It can technically run any time
after Step 1 (the sync only depends on `version.py` → `SKILL.md`, not
on the prebuilt), but the recommended order puts it as Step 7 so
its diff is reviewable in isolation in Step 8. If sync ran inside
build-prebuilt, an accidental rewrite outside the intended
SYNC_TARGETS (e.g., due to a bug in the script, or an unrelated file
that gained a `pfg-v` mention) would get buried in the same commit as
the prebuilt regeneration. Separating them surfaces sync's blast
radius as its own visible delta.

## Why the byte-stability check matters

The prebuilt's `(old, new)` pairs are derived from the diff. If the
diff captures something non-deterministic (a timestamp, a UUID, a
locale-dependent format), the synthesized script's `old` won't match
on someone else's clean install. The byte-stability check catches
this: if the synthesized script doesn't reproduce the live patched
file, something captured into the diff that shouldn't have.

In practice this hasn't happened (upstream bundles are deterministic),
but the check is the cheap insurance against it ever shipping.

### Byte-stability does NOT imply correctness

Byte-stability proves the splice is **deterministic against the .bak
you have**. It does not prove the splice produces correct *behavior*
when applied to a different .bak (e.g., a fresh extension install).

If `.bak` isn't pristine (typically because you iteratively developed
the patch in place and never re-baked from a fresh install), the
synthesis only captures the **last incremental hop**, not the full
pristine→post-patch transformation. Earlier transformations are
present in both `.bak` and live, so the diff doesn't see them.
Translating the resulting prebuilt to a fresh bundle then leaves
those earlier transformations missing, often producing dead code or
silent no-ops while passing every byte-stability check.

A K webview wrap captured from an install with a non-pristine `.bak`
can omit an earlier transformation (e.g. `return createElement(...)` →
`let _ws=createElement(...)`, introduced in a prior K iteration and
already baked into that `.bak`). Applying such a splice to a fresh
install leaves the wrap as dead code after the `return` statement while
still passing every byte-stability check. See
[`docs/patches.md`](docs/patches.md#byte-stability-check-is-necessary-but-not-sufficient-for-splice-correctness) "Byte-stability check is
necessary but not sufficient for splice correctness" gotcha.

**Invariant: `.bak` is always the pristine pre-patch baseline.**

This is the rule that makes `build-prebuilt.py`'s diff
(`live - .bak`) always capture the full pristine→patched
transformation, never an incremental hop. Maintain it strictly:

- **Never overwrite `.bak` once it exists.** SKILL.md Step 2 already
  says "skip the backup if a `.bak` already exists" for end-users;
  the same discipline applies to maintainers iterating a patch.
- **Per-patch checkpoints are named after the settled patch they
  contain.** When patch X is finalized, snapshot the live file as
  `<file>.patchX.bak`. That name self-documents what's in the
  snapshot ("the settled state of patch X"). Use `.patchY.bak` after
  Y is finalized, and so on. Don't use `.pre-patchY.bak`; it only
  says what comes next, forcing the reader to know the patch
  ordering to figure out what's actually in there.
- During iteration of patch Y, work directly on live (which is
  already at `.patchX.bak`'s state). When Y is finalized, save
  `.patchY.bak` as the new checkpoint. `.patchX.bak` stays around as
  the rollback target if Y turns out wrong.
- If `.bak` somehow drifted off pristine (re-patched atop already-
  patched live, or the file was overwritten before this rule was
  formalized), recover by reinstalling the extension from scratch
  and re-baking from clean before any further patch work.
- After publishing the prebuilt, verify it against a fresh install
  before declaring it shipped: download to a clean test extension
  dir and confirm the resulting code actually runs (DOM probe for
  the rendered K marker, not just `node --check`).

(`apply-patch-fg.py` uses `<file>.pre-patchFG.bak` as a working
checkpoint; that's a script-internal artifact rather than a
maintainer-curated checkpoint, and the script reads/writes it
itself, so the existing name is grandfathered.)

### Verifying a published prebuilt before declaring shipped

Byte-stability + `node --check` + manual smoke (open a session, see
markers) are necessary but easy to fool yourself with. The
authoritative test is the user-facing one: **does the rendered DOM
contain the wrap elements the patch is supposed to produce?**

Recipe (assumes the prebuilt has been applied to a fresh install,
window has been reloaded, and a session known to trigger Patch K is
open in a chat panel):

```sh
# Find the chat panel's webview iframe target
PAGE_ID=$(curl -s http://127.0.0.1:9222/json/list | python3 -c '
import sys, json
d = json.load(sys.stdin)
# Look for an iframe whose parent page title matches the target window
for t in d:
    if t.get("type") == "iframe":
        # Filter further by URL/parent; see docs/debugging.md
        ...')

# DOM probe inside the inner active-frame
WS="ws://127.0.0.1:9222/devtools/page/$PAGE_ID"
node /tmp/eval_in_inner_frame.mjs "$WS" 'JSON.stringify({
  pfgkAlert: document.querySelectorAll(".pfgkAlert").length,
  bookend: document.querySelectorAll("[data-pfgk-role=\"bookend\"]").length,
  seam: document.querySelectorAll("[data-pfgk-role=\"seam\"]").length,
  bridge: document.querySelectorAll("[data-pfgk-role=\"bridge\"]").length
})'
```

For a session known to have phantom-lpu boundaries the result should
be `{pfgkAlert: ≥1, bookend: 1, seam: ≥1, bridge: ≥0}`; non-zero
wrap counts confirm the wrap React node is actually rendering, not
just the K message text.

If the wrap counts are zero but the message text is in the DOM
(`document.body.textContent.includes("PATCH K · ")` returns true),
the splice produced dead code. See [`docs/patches.md`'s
"Case study: dead K wrap from non-pristine .bak synthesis"](docs/patches.md#case-study-dead-k-wrap-from-non-pristine-bak-synthesis-2132)
for the empirical diagnosis walkthrough.

The full CDP toolkit + recipes live in
[`docs/debugging.md`](docs/debugging.md). Read the
"⚠️ Read this BEFORE improvising" banner there before reaching for
ad-hoc reload mechanisms; several common ones (`location.reload()`,
`Page.reload` on iframes) are documented gotchas that break the
install state.

### When a published prebuilt turns out broken

If user-visible verification reveals an already-published prebuilt
is broken (and a fixed re-synthesis isn't going to happen for that
extension version, e.g., it's been superseded by a newer one):

1. **Move it to `prebuilt/archive/broken/v<patchset>/<VER>/`**
   rather than deleting. The patchset version is the outer dir
   because the same bundle version can have a broken prebuilt for
   one patchset and a working prebuilt for another future fix, and
   we want both archived without collision. Example:
   `prebuilt/archive/broken/v1.4/2.1.126/apply.py`. Keeps the
   historical record so future investigators can see exactly what
   shipped.
2. **Document the diagnosis** in
   [`prebuilt/archive/broken/README.md`](prebuilt/archive/broken/README.md)'s
   per-version notes section: what the bug was, why it shipped
   (what synthesis pitfall), what supersedes it, what the
   workaround is.
3. **End-users running the broken prebuilt's old URL** will get a
   404 from raw.githubusercontent.com after the move. The skill's
   Step 0 falls through to manual application in that case, which
   is the intended fallback.

The current contents of `prebuilt/archive/broken/` document the
2.1.126 K-wrap-dead-code case as a worked example.

## util/ scripts: what they don't do

- They don't validate that your patches are *correct*, only that
  they're *byte-stable* against the .bak in the install dir. If you
  applied Patch D wrong (only one of two walkers patched), the
  prebuilt will be byte-stable but functionally broken. Test the live
  patches before synthesizing.
- They don't auto-locate `.bak` files anywhere other than alongside
  each target. If your backups are named `.pre-patchA.bak` or live in
  another directory, copy them to `extension.js.bak` etc. before
  running.
- They don't infer the version from the bundle contents, only from
  the directory name (`anthropic.claude-code-<VER>-linux-x64`). If
  upstream ever changes that naming convention, update
  `util/build-prebuilt.py` to extract the version differently.
