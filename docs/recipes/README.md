# docs/recipes

Runnable, version-controlled copies of the disposable CDP / introspection scripts
the playbooks ([debugging.md](../debugging.md), [patches.md](../patches.md))
reference. They live here so they survive `/tmp` being cleared (a reboot wipes
`/tmp`): the playbook holds the timeless method, these are the throwaway tools.

Usage: the scripts hardcode `/tmp` paths and CDP port 9222. To use a recipe, `cp`
its scripts to `/tmp` and run them per the playbook section that references them.

## lib/ (shared helpers, used by most recipes)
- `cdp-eval.mjs`: eval an expression (or `@file`) in a CDP target's top context
- `eval_in_inner_frame.mjs`: eval an expression in the webview's active inner frame

## render-trace/ (perf: capture + decode a render trace)
The close/reopen/select capture orchestrator + proto tracer + helpers; see its own
README. Referenced by debugging.md "Performance profiling a render via CDP".

## panel-open/ (open a session via the conversations panel, Step 6a)
- `panel_ready.mjs`: open the panel, type a search needle, return its ws
- `click_convo.js`: click the conversation row matching `NEEDLE` (sed the needle in)

## breakpoints/ (conditional / side-effect breakpoints)
- `bp_setup.mjs`: set a side-effect-condition BP that captures state to a global
- `bp_ez4_capture.mjs`: capture the walker's V/U/Z/H at the Ez4 return

## markers/ (Patch K recovery-marker probing)
- `pfg_markers.js`: read the rendered `pfgk-` markers (role, text)
- `extract_live_svgs.mjs`: dump the marker SVGs from a live render

## misc/
- `reload_go.mjs`: reload-window helper with focus recovery
- `shot.mjs`: screenshot the workbench page

Not stored (generated or placeholder, not source): `click_convo.ready.js` (the
`sed` output of `click_convo.js` with `NEEDLE` filled in), `expr.js` (a scratch
file you write your own expression into), `blen.js` (generated inline by the
render-trace orchestrator).
