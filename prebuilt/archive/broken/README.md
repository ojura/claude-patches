# Broken-prebuilt archive

Prebuilts that were published but later identified as functionally
broken (the splices apply cleanly and pass the byte-stability check
but produce dead code or otherwise don't deliver the intended
behavior). Kept here rather than deleted so a future investigator
can see exactly what shipped.

**Don't run anything in this directory.** A working prebuilt for the
same extension version may exist under `prebuilt/<VER>/` if the
patchset still supports that version; otherwise apply patches
manually via the SKILL.md fallback.

## Path structure

```
prebuilt/archive/broken/v<patchset-version>/<bundle-version>/apply.py
```

Both the patchset version (e.g. `v1.4`) AND the bundle version
(e.g. `2.1.126`) participate in the path. The patchset version is
the outer dir because the same bundle version can have a broken
prebuilt for one patchset and a working prebuilt for another
(future fix), and we want both archived without collision.

## Criteria for landing here

A prebuilt belongs in `prebuilt/archive/broken/v<patchset>/<VER>/` if:

1. It was once published at `prebuilt/<VER>/apply.py`.
2. Empirical user-visible verification (DOM probe, behavior test,
   etc.) revealed it doesn't deliver the patchset's intended
   behavior on a fresh install. Typically this is because the splice was
   synthesized off a non-pristine `.bak` and missed an earlier
   pristine→post-patch transformation. See
   [`docs/debugging.md`](../../../docs/debugging.md) "Case study:
   dead K wrap from non-pristine .bak synthesis" for the canonical
   example.
3. We're not going to re-synthesize a fixed version (e.g., the
   extension version is no longer actively distributed, or the fix
   has been folded into a newer prebuilt that supersedes it).

If a fresh fixed prebuilt is available for the same version,
publish it under `prebuilt/<VER>/` and leave the broken one here as
historical record. Update this README's per-version notes section
below to document the diagnosis.

## Per-version notes

### v1.4 / 2.1.126

- **Bug**: K webview wrap is dead code on fresh installs. The
  splice's OLD anchor matches `})}if(Z.type==="assistant"...` but
  doesn't strip the preceding `return ` keyword, so
  `return n1.default.createElement(GR0,{...});` runs first and
  everything after (the wrap branch) is unreachable.
- **Why it shipped**: K was developed iteratively in 2.1.126 across
  v1.2 → v1.3 → v1.4. By the time the v1.4 prebuilt was synthesized
  (commit 248665f), the install's `.bak` already had
  `let _ws=createElement(...)` from an earlier K iteration. The
  diff `live - .bak` only captured the wrap-internal v1.3→v1.4
  changes, missing the pristine→post-K
  `return → let _ws=` transformation that was equally present in
  both files.
- **Byte-stability passed** because the splice is deterministic
  against that non-pristine `.bak`, but the splice doesn't produce
  a working patch on a pristine install.
- **Fixed in**: not fixed for 2.1.126 directly; the lesson was
  applied to 2.1.132's prebuilt (commit 126a095), which adds the
  missing `return → let _ws=` transformation as an explicit splice.
  2.1.126 isn't actively distributed anymore (2.1.132 superseded it).
- **Workaround if you must use 2.1.126**: apply patches manually via
  the SKILL.md per-step instructions; do not run this archived
  prebuilt.

### v1.7 / 2.1.148

- **Bug**: the J+K combined loader splice rewrites the Patch H
  read-fn call `pE0` into bare `0`. The live loader becomes
  `async function tE0(z,V){...let x=await 0(K.filePath,K.fileSize)...}`
  with no surrounding try/catch, so it throws
  `TypeError: 0 is not a function` the moment the loader runs, i.e.
  on every session content load. The Patch H read fn itself
  (`async function pE0(...)`) is patched correctly; only the
  loader's *call* to it lost its name.
- **Why it shipped**: when the J+K loader head was applied manually
  to 2.1.148, the loader-head rewrite `let x=await <READ_BUF>(...)`
  dropped the `pE` of `pE0`, leaving `await 0(...)`. Byte-stability
  passed (the splice is deterministic against `.bak`) and
  `node --check` passed (`0(...)` is a syntactically valid call
  expression that only fails at runtime), so neither guard caught
  it.
- **Why nobody noticed**: Antigravity loads the highest-versioned
  extension on disk, so once 2.1.152 landed the broken 2.1.148
  loader went dormant and never executed.
- **Detected**: 2026-05-27, while synthesizing the 2.1.152 prebuilt.
  Translating the 2.1.148 J+K splice surfaced an OLD anchor of
  `let x=await pE0(...)` against a NEW of `let x=await 0(...)`.
- **Fixed in**: not fixed for 2.1.148 (no longer actively
  distributed; superseded by 2.1.152). The 2.1.152 prebuilt
  reconstructs the loader head correctly as
  `let x=await pE0(N.filePath,N.fileSize)`, so the typo is not
  carried forward.
- **Second, independent bug in the same prebuilt**: 2.1.148 also
  carries the dead-K-render-wrap regression described under
  2.1.152 below (webview splices dropped from 4 to 3; the
  `return -> let _ws=` binding conversion is missing, so the pfgk
  wrap is unreachable). 2.1.148 is the origin of that regression;
  2.1.152/2.1.158/2.1.159 inherited it by copying this render-wrap
  verbatim.
- **Workaround if you must use 2.1.148**: apply patches manually via
  the SKILL.md per-step instructions; do not run this archived
  prebuilt.

### v1.7 / 2.1.152

- **Bug**: dead K render-wrap. The webview render-wrap is missing the
  `return <X>.createElement(<userComponent>,...)` ->
  `let _ws=<X>.createElement(<userComponent>,...)` binding conversion,
  so on a fresh (pristine) bundle the user element is `return`ed
  immediately and the entire pfgk wrap block that follows
  (`;if(typeof Z.uuid==="string"){...;_ws=createElement("div",{className:"pfgkAlert ..."},...,_ws)}}return _ws}`)
  is unreachable dead code. The seam/bookend/bridge/broken ghosts the
  loader inserts still render, but as plain collapsed user bubbles:
  no color, no warning emoji, no click-to-navigate, and the
  un-collapse `<style>` never fires, so the long diagnostic essays
  stay truncated. This is a recurrence of the 2.1.126 bug (see v1.4
  section), which had been fixed for 2.1.132 through 2.1.146.
- **Why it shipped**: the 2.1.148 prebuilt synthesis collapsed the
  webview render-wrap from 4 splices to 3, dropping the
  binding-conversion splice (almost certainly synthesized from an
  already-`let _ws=`-patched base, so the conversion was identical on
  both sides of the diff and fell out, exactly the 2.1.126 mechanism).
  2.1.152 was then built by copying 2.1.148's 3-splice render-wrap
  verbatim, and 2.1.158/2.1.159 copied it from 2.1.152. Byte-stability
  passed (deterministic), `node --check` passed (dead code is valid
  syntax), and the `grep -c 'pfgkAlert pfgk-'` verification passed
  because it checks *presence*, not *reachability*: the literal is in
  the file, just after a `return`.
- **Why nobody noticed**: same dormancy as the loader bug plus the
  presence-not-reachability blind spot above.
- **Detected**: 2026-06-01, by an independent instance patching 2.1.159
  that triangulated against the installed 2.1.139 (correct `let _ws=`
  shape) instead of trusting the 2.1.152 prebuilt, then confirmed by
  re-synthesis showing 4 webview splices where the published ones had 3.
- **Status**: retired, not re-synthesized. 2.1.152 is superseded by
  2.1.158/2.1.159 (both fixed), and the local 2.1.152 install was
  garbage-collected by Antigravity, so the cheap path (re-synthesize
  from a live `.bak`) is gone. A corrected 2.1.152 prebuilt is still
  reachable if anyone wants one: re-fetch the 2.1.152 bundle from Open
  VSX / the VS Code marketplace, add the binding-conversion splice
  (and confirm the loader head is intact), then re-validate. It just
  was not worth doing for a superseded bundle. 2.1.158 and 2.1.159 are
  fixed (re-synthesized with the 4th binding-conversion splice
  restored, `bind_conv=YES`).
- **Workaround if you must use 2.1.152**: apply patches manually via
  the SKILL.md per-step instructions (the render-wrap step now documents
  the required binding conversion); do not run this archived prebuilt.
