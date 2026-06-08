# Recipe: capture + decode a CDP proto trace of the webview render

Runnable scripts for the "Performance profiling a render via CDP" section of
[../../debugging.md](../../debugging.md). The playbook holds the timeless method;
these are the disposable scripts that implement it, kept here (version-controlled,
next to the playbook) because they were lost once when `/tmp` was cleared on a
reboot. Recover them from here, not from memory.

## What it does
Captures a full Chrome proto trace of the webview rendering a large chat session,
then decodes the `Layout`-slice parentage to prove the render is *forced
synchronous layout* (JS-triggered reflows), not natural Blink growth.

## The trigger insight (the hard-won part)
The session tab's webview is RETAINED once rendered: deselect/reselect and
`Page.reload` do NOT re-render it (bodyLen stays ~9.9M, you capture an idle
trace). The ONLY trigger that forces a genuine fresh mount + render is:

  close the tab (destroys the webview) -> Ctrl+Shift+T reopen -> SELECT the tab

SELECT is required because the reopened tab is lazy (it renders on selection).
`catch_fresh.sh` does exactly this, inside the trace. A fresh mount shows bodyLen
climbing 20 -> ~9,900,944; an idle/retained capture shows it flat at ~9.9M.

## Run
1. IDE up with CDP on 9222 (`.../antigravity-ide/bin/antigravity-ide --remote-debugging-port=9222`).
2. The target session tab is open (it may be inactive).
3. `cp docs/recipes/render-trace/* /tmp/ && cd /tmp && bash catch_fresh.sh`
   writes `/tmp/render.perfetto-trace` (proto, ~200MB for a 16.8k-message session).
   Scripts hardcode `/tmp` paths and the `"claude-patches"` tab needle; edit the
   needle in `catch_fresh.sh` for another session.

| script | role |
|---|---|
| `catch_fresh.sh` | orchestrator: close, reopen, select, poll bodyLen to render-done, drain |
| `tracer8.mjs` | browser-socket proto tracer (survives the blocked renderer; stall-detect; buffer heartbeat is the only liveness signal during the freeze) |
| `close_session.mjs` | close the tab matching a needle (destroys the retained webview) |
| `reopen.mjs` | focus-recover + Ctrl+Shift+T (`reopenClosedEditor`) |
| `select_tab.mjs` | real-mouse-click the tab matching a needle (the lazy-render trigger) |
| `eval_in_inner_frame.mjs` | eval an expression in the webview's active inner frame (the bodyLen poll) |

`blen.js` (the `document.body.innerText.length` probe) is generated inline by the
orchestrator, so it is not stored here.

## Decode (needs perfetto trace_processor)
```sh
curl -sL https://get.perfetto.dev/trace_processor -o /tmp/trace_processor && chmod +x /tmp/trace_processor
/tmp/trace_processor /tmp/render.perfetto-trace -q QUERY.sql
```
The first run network-fetches the real binary (the curl gets a launcher shim).

The cause-level query (forced vs natural layout), the one that settles it:
```sql
select p.name parent, count(*) n, sum(s.dur)/1e9 sec
from slice s join slice p on s.parent_id = p.id
where s.name = 'Layout' group by p.name order by sec desc;
```
`Layout` under `FunctionCall` = JS-forced (thrashing); under
`ThreadControllerImpl::RunTask` = natural Blink lifecycle.

Which JS forces it (FunctionCall definition site):
```sql
select extract_arg(fc.arg_set_id,'debug.data.url') url,
       extract_arg(fc.arg_set_id,'debug.data.lineNumber') line,
       count(*) n, sum(l.dur)/1e9 sec
from slice l join slice fc on l.parent_id = fc.id
where l.name = 'Layout' and fc.name = 'FunctionCall'
group by url, line order by sec desc limit 20;
```
Engine-level witness (per-reflow scope): `Layout` slice args carry
`debug.beginData.totalObjects`, `.partialLayout`, `.layoutRoots[0].nodeName`
(`#document` + `partialLayout=0` = full-document reflow).

## The finding (2.1.159, this session)
Forced synchronous layout: ~8,850 JS-triggered reflows, ~155 s of ~164 s
style+layout, each a full `#document` relayout (~404k objects). Dominant forcing
site is VS Code's `requestAnimationFrame` DOM-scheduler flush draining queued
geometry measures during React's synchronous commit; plus the React reconciler
and per-code-block Monaco self-measurement. Two captures agreed within ~3%
(Layout 92.3 s / 95.3 s; max single reflow 2.8 s / 2.9 s).
