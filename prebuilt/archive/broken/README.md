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
- **Workaround if you must use 2.1.148**: apply patches manually via
  the SKILL.md per-step instructions; do not run this archived
  prebuilt.
