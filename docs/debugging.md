# Debugging the bundled Claude Code extension

A reverse-engineer's reference for poking at a running
`anthropic.claude-code-*-linux-x64` install via Chrome DevTools Protocol
(CDP): reading state, capturing function-internal data, dispatching RPCs,
walking React fibers, and triggering refreshes. Companion to
[`patches.md`](patches.md), which covers WHAT each patch does and WHY;
this one covers HOW to introspect the running bundle to build/verify
patches like them.

The notes here are derived from empirical work on Antigravity (Google's
VS Code fork that bundles claude-code via Open VSX). The mechanics
generalise to upstream VS Code with minor adjustments: the inspector
ports, IPC plumbing, and CommonJS module structure are the same; only
the launch flags and IDE-product paths differ.

> ## ⚠️ Read this BEFORE improvising
>
> If you found this doc because you're about to debug a running
> Antigravity / VS Code install: **read the relevant section first,
> then act**. Do not freelance reload commands, eval expressions,
> or reload mechanisms before checking whether the playbook already
> covers your case. Specifically:
>
> - **Reload mechanisms**: see "Refresh / reload playbook" for six
>   verified mechanisms ordered by precision. `location.reload()` on
>   webview iframes BREAKS the iframe (chrome-error). `Page.reload`
>   is rejected on iframe targets. Don't try them. Use mechanism 3
>   (`Input.dispatchKeyEvent` for `workbench.action.reloadWindow`)
>   or mechanism 5 (disk-edit + Reload Window). **Bare `Ctrl+R` is
>   unreliable** (keybinding-context-sensitive); the canonical
>   reload is `Ctrl+Shift+P` → type `Reload Window` → `Enter`.
> - **Verifying a code change took effect**: see the K case study
>   "End-to-end verification of the fix" paragraph. The exact recipe
>   is `Input.dispatchKeyEvent` reload + BP-free verification via
>   fiber-walk + DOM probing. `node --check` and byte-stability
>   passing are NOT verification.
> - **Capturing function-internal state**: see "Recipe: capture
>   function-internal state with a side-effect BP". Don't try
>   `setScriptSource` for code injection: it silently doesn't swap
>   running code (top gotcha).
> - **Reaching the manager singleton**: not on `extension.exports` /
>   `globalThis` / `require.cache`. Use the BP-stash trick (Recipe 1).
> - **Invoking the chain walker for a session** without BP setup:
>   `await mgr.getConnection(sid).then(c => c.sendRequest({type: 'get_session_request', sessionId: sid}))` from the inner
>   active-frame. Returns the walker's H array directly. See
>   "Trigger the chain walker without BP setup" recipe.
> - **Forcing a FRESH re-render after a code edit + reload**: a reloaded
>   webview REHYDRATES from serialized state (stale, no re-walk). Neither
>   the `probe`-channel RPC nor `conn.sendRequest` re-renders the panel. To
>   actually re-run `Wz4` and re-render, open the session as a NEW editor
>   tab via the conversations panel, or close+reopen its tab (editor-tab
>   webviews are disposed on hide). See "Patch development iteration loop"
>   Step 6. The visual loop WAS done autonomously dozens of times; it is
>   not impractical.
>
> ### ABSOLUTE RULE: DOM verification before push
>
> **Never claim "tests passed end-to-end" or push code that affects
> rendering without DOM verification.** RPC output / `messages.peek()`
> / chain walker H / `node --check` / byte-stability are all valid
> sanity checks but **none of them substitute for a
> `[data-pfgk-role=...]` count + `document.body.textContent.includes(...)` query against the actual rendered iframe**.
> The DOM is the user's view of truth; everything else is internal
> model agreement.
>
> If pushing requires action you don't have (e.g., reload to
> re-render), wait for the action and verify after. Do not push
> first and verify later. The push commits to a public artifact
> end-users will fetch; reverting after the fact loses confidence
> and forces a fix.
>
> The rule is verifiable: any attempt at `git commit` / `git push`
> for code that affects rendering must have, in immediate prior
> context, the output of a query like:
>
> ```js
> JSON.stringify({
>   pfgkAlert: document.querySelectorAll('.pfgkAlert').length,
>   bookend: document.querySelectorAll('[data-pfgk-role="bookend"]').length,
>   broken: document.querySelectorAll('[data-pfgk-role="broken"]').length,
>   bodyHas<expected_text>: document.body.textContent.includes('<expected text>'),
> })
> ```
>
> against the actual rendered iframe, with values matching the test's
> predictions. No exceptions for "but the chain walker output looked
> right" or "but the source has the change". The rendered DOM gates
> the push.
>
> The cost of pausing to skim the relevant section is seconds. The
> cost of cowboying is minutes per failed mechanism plus often
> breaking the install state (chrome-error iframes, broken focus,
> etc.). The playbook exists because every recipe in it was earned
> by failing the cowboy way first.
>
> If you find a case the playbook doesn't cover, document it here
> after solving it. If you find an outdated recipe, fix it.

---

## Mental model

### Three V8 processes, three CDP surfaces

Antigravity is Electron, so there are at least three distinct V8 processes
involved when a chat panel is open:

```
┌────────────────────────────────────────────────────────────┐
│  Renderer (Chrome), port 9222                              │
│   ├─ Workbench page (one per IDE window)                   │
│   ├─ Outer vscode-webview iframe (CSP wrapper, empty body) │
│   └─ Inner active-frame iframe (the React app)             │
└────────────────────────────────────────────────────────────┘
                       ▲      ▲
                       │      │ electron IPC
                       │      ▼
┌────────────────────────────────────────────────────────────┐
│  Extension host (Node.js), one per window                  │
│   ├─ extension.js (the bundled extension code)             │
│   ├─ Singleton manager (sessionStates, sessionPanels, ...) │
│   └─ Spawns claude --resume <sid> subprocesses             │
└────────────────────────────────────────────────────────────┘
                       │
                       │ stream-json stdio
                       ▼
┌────────────────────────────────────────────────────────────┐
│  claude --resume <sid> subprocess (Node.js), one per chat  │
│   └─ Talks to Anthropic API, streams tool-use events back  │
└────────────────────────────────────────────────────────────┘
```

The renderer is exposed at a single shared CDP port (`9222`,
fixed via `--remote-debugging-port=9222` at Antigravity launch). Every
workbench window's renderer page and every webview iframe is reachable
through `http://127.0.0.1:9222/json/list`.

Each extension host gets its OWN inspector port. The **first** exthost to
spawn captures the sticky `--inspect-extensions=9229` flag; subsequent
exthosts get OS-ephemeral ports (`11277`, `45647`, …). Same target type
in `/json` (`title: "electron/js2c/utility_init"`), different port
numbers.

The `claude --resume` subprocesses are NOT directly inspectable unless
launched with `--inspect`; you observe them indirectly via the extension
host that owns their stdio.

### Ports as roles, not numbers

`9229` is not "the target". It's whichever exthost spawned first;
could be your debugging target's window, could be the window that's
running this Claude Code session itself.

The roles you actually care about:

- **`mine`**: the exthost belonging to the Claude Code session running
  this debugger. Touching `mine` is touching yourself: BPs fire on your
  own activity, freezing you mid-tool-call.
- **`target`**: the exthost hosting the panel/session you want to
  inspect. The "outside" you're poking from.
- **`renderer`**: port 9222, shared by all renderers (DOM-side).

`mine` and `target` are typically two different exthost ports if you're
debugging another window's project. They're the SAME port if you're
debugging a panel in the same window as your Claude Code session; in
that case BPs are double-edged (you'll trigger them yourself).

**Identify `mine` first, every session.** Don't carry over assumptions
from prior runs; the mapping changes with launch order.

```sh
# Walk ppid chain from the current shell to find the parent exthost,
# then look at its --inspect=PORT flag.
P=$$
for i in 1 2 3 4 5 6; do
  ps -o pid=,ppid=,cmd= -p $P 2>/dev/null | head -c 250 | head -1; echo
  P=$(ps -o ppid= -p $P 2>/dev/null | tr -d ' ')
  [ -z "$P" ] || [ "$P" = "1" ] && break
done
# Look for --inspect=127.0.0.1:<PORT>; that's `mine`.
```

Then enumerate everything else and pick `target` by `cwd` match:

```sh
# Find all exthost-shaped inspector ports (excluding language servers)
for p in $(ss -tlnp 2>/dev/null | grep -oE '127\.0\.0\.1:[0-9]+' | sort -u); do
  R=$(curl -s --max-time 1 "http://$p/json" 2>/dev/null | head -c 60)
  case "$R" in *electron/js2c/utility_init*) echo "exthost @ ${p##*:}";; esac
done
# Confirm each is the right window: connect and read process.cwd()
```

### What lives where

| Surface | Port | Holds |
|---|---|---|
| Renderer | 9222 | Workbench DOM, all webview iframes, React app inside `name="active-frame"` |
| Extension host (per window) | 9229 + ephemeral | `extension.js`, dispatcher, singleton Maps, `require.cache`, Node `globalThis` |

A `globalThis.__caps` you set on one exthost is invisible from any other
exthost AND from the renderer. Mixing this up is the single biggest
time-waster: post on 9222, observe on `target`, read on `target`. If
`__caps` reads as `undefined` it's almost always a port mixup, not a BP
that didn't fire.

---

## Tooling

Two tiny Node scripts cover all of CDP that you'll need. Both rely on
Node 22+'s built-in `WebSocket` global: no `npm install`, no Python,
no MCP servers.

### `/tmp/cdp-eval.mjs`: one-shot Runtime.evaluate

```js
// usage: node cdp-eval.mjs <ws-url> <expression-or-@file>
const wsUrl = process.argv[2], exprArg = process.argv[3];
const expression = exprArg.startsWith('@')
  ? await (async () => { const fs = await import('node:fs/promises'); return fs.readFile(exprArg.slice(1), 'utf8'); })()
  : exprArg;
const ws = new WebSocket(wsUrl);
let nextId = 1;
const pending = new Map();
ws.addEventListener('open', () => {
  send('Runtime.enable', {}).then(() =>
    send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true,
                               allowUnsafeEvalBlockedByCSP: true, includeCommandLineAPI: true })
  ).then(r => { console.log(JSON.stringify(r, null, 2)); ws.close(); });
});
ws.addEventListener('message', ev => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(msg.error); else resolve(msg.result);
  }
});
function send(method, params) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
```

Critical flags:

- `allowUnsafeEvalBlockedByCSP: true`: needed for vscode-webview iframes
  whose CSP otherwise blocks `eval`.
- `includeCommandLineAPI: true`: exposes Node's `require` in the eval
  scope. Without it you get `require is not defined` when trying to use
  `require('fs')`.

Discovery + invocation pattern:

```sh
# WS URL for the first exthost
WS=$(curl -s http://127.0.0.1:9229/json | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["webSocketDebuggerUrl"])')

# Inline expression
node /tmp/cdp-eval.mjs "$WS" 'process.pid'

# Expression from file
node /tmp/cdp-eval.mjs "$WS" '@/tmp/expr.js'
```

For a webview iframe target, the WS URL is
`ws://127.0.0.1:9222/devtools/page/<id>`; read `id` from
`http://127.0.0.1:9222/json/list`.

### `/tmp/eval_in_inner_frame.mjs`: drill into the React app

`vscode-webview` iframes are double-nested. The outer iframe at
`/json/list` is just a CSP wrapper with an empty body; the React app
lives in a child `<iframe id="active-frame">` that is NOT itself a
top-level CDP target. To eval against it, you have to enumerate the
parent's frame tree and Run`Runtime.evaluate` against the inner frame's
main-world execution context.

```js
// usage: node eval_in_inner_frame.mjs <outer-ws-url> '<expr-or-@file>'
const wsUrl = process.argv[2];
const expression = process.argv[3].startsWith('@')
  ? await (async () => { const fs = await import('node:fs/promises'); return fs.readFile(process.argv[3].slice(1), 'utf8'); })()
  : process.argv[3];

const ws = new WebSocket(wsUrl);
let nextId = 1, pending = new Map();
const ctxEvents = [];
ws.addEventListener('message', ev => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(msg.error); else resolve(msg.result);
  } else if (msg.method === 'Runtime.executionContextCreated') {
    ctxEvents.push(msg.params.context);
  }
});
function call(method, params={}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
await new Promise(r => ws.addEventListener('open', r));
await call('Page.enable');
await call('Runtime.enable');
await new Promise(r => setTimeout(r, 800));   // drain executionContextCreated

const tree = await call('Page.getFrameTree');
function findInner(node, acc=[]) {
  if (node.frame) acc.push(node.frame);
  (node.childFrames||[]).forEach(c => findInner(c, acc));
  return acc;
}
const allFrames = findInner(tree.frameTree);

// Discriminator: name === "active-frame". URLs are NOT reliable
// (the inner's loaded URL is also index.html, same as outer, even
// though its src attribute pointed at fake.html).
const innerFrame = allFrames.find(f => f.name === 'active-frame');
if (!innerFrame) { console.error('FAIL: no name=active-frame'); process.exit(1); }

// Pick MAIN-world context (origin matches webview, name is empty).
// Reject __playwright_utility_world_*; those are isolated worlds that
// CANNOT see the page's globals or React state.
const mainCtx = ctxEvents.find(c =>
  c.auxData?.frameId === innerFrame.id && !c.name && c.origin);
if (!mainCtx) { console.error('FAIL: no main-world context'); process.exit(1); }

const r = await call('Runtime.evaluate', {
  expression, returnByValue: true, awaitPromise: true,
  contextId: mainCtx.id, includeCommandLineAPI: true,
});
console.log(JSON.stringify(r, null, 2));
ws.close();
```

The two non-obvious things this script handles:

1. **Frame discriminator is `name === "active-frame"`**, not URL. The
   inner frame's reported URL is `index.html?id=...` (same as the outer
   wrapper), even though the outer's HTML source has `<iframe
   src="fake.html?id=...">`. The src attribute and the loaded URL
   diverge; trust the frame name.

2. **Pick the main-world execution context**, not an isolated one.
   `Page.createIsolatedWorld` and tools like Playwright create sandboxed
   contexts (named `__playwright_utility_world_*`) that CANNOT see the
   page's globals or React state. The main-world context has empty
   `name` and an `origin` matching the webview.

---

## Discovery: find the right port and the right iframe

### Find the renderer page for a window

```sh
curl -s http://127.0.0.1:9222/json/list | python3 -c "
import sys, json
for t in json.load(sys.stdin):
    if t.get('type') == 'page':
        print(t['id'], '|', t.get('title','?')[:70])
"
```

The page title is `"<workspace-name> - Antigravity - <active-tab-name>"`.
Match by workspace name.

### Find the chat panel for a session

```sh
SID=61974011-6e11-45a1-a489-7ab0b496dc95
curl -s http://127.0.0.1:9222/json/list | python3 -c "
import sys, json
for t in json.load(sys.stdin):
    if t.get('type') == 'iframe' and 'session=$SID' in t.get('url',''):
        print(t['id'], '|', t['url'][:120])
"
```

Useful URL query-param filters on iframes:

| Param | Meaning |
|---|---|
| `id=<webview-uuid>` | unique per webview (matches between outer and inner) |
| `&session=<sid>` | present on chat panels for a specific session |
| `&purpose=webviewView` | present on **sidebar** webviews. Editor-tab panels lack this |
| `&parentId=1` vs `&parentId=3` | VS Code container; correlates to area |

### Confirm an exthost's window cwd

```sh
WS=$(curl -s http://127.0.0.1:9229/json | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["webSocketDebuggerUrl"])')
node /tmp/cdp-eval.mjs "$WS" 'JSON.stringify({pid: process.pid, cwd: process.cwd()})'
```

`cwd` matches the workspace folder of the window owning that exthost.
Match against your target window's workspace path. **Cross-project
quirk**: a panel for a session in project A can be open inside a window
for project B; the exthost is the WINDOW's, not the SESSION's. Don't
assume by sessionId.

### Precheck: are the patches actually loaded?

Before BP work, verify patches are live (Reload Window or extension
auto-update wipes them):

```sh
EXT=$(ls -d ~/.*/extensions/anthropic.claude-code-*-linux-x64 | sort -V | tail -1)
WS=$(curl -s http://127.0.0.1:<TARGET>/json | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["webSocketDebuggerUrl"])')
node /tmp/cdp-eval.mjs "$WS" '(function(){
  var s = require("fs").readFileSync(
    "'"$EXT"'/extension.js","utf-8");
  return JSON.stringify({size: s.length, sig: (s.match(/\/\*pfg-v[\d.]+\*\//g)||[])});
})()'
```

Empty `sig` → patches aren't installed. `/patch-claude` first or you'll
spend turns chasing phantoms in unpatched code. (Reading from disk is
fine; in-memory and on-disk are identical for active-installed
extensions, since CDP `setScriptSource` doesn't actually swap runtime
code; see Gotchas.)

---

## Recipe: capture function-internal state with a side-effect BP

This is THE pattern for "what does Ez4 see when it runs?" or "what fields
does fromClient receive?". Beats `setScriptSource` (which is broken for
runtime code; see Gotchas) and beats pure-pause BPs (which freeze the
exthost).

The mechanism: `Debugger.setBreakpoint` accepts a `condition` string
that V8 evaluates AT EACH HIT in the live frame's scope. The condition
has access to all locals. If the condition mutates `globalThis` and
returns `false`, the BP is "hit" but doesn't pause execution: you get
side-effect data capture without freezing.

```js
// 1. Connect, Debugger.enable, drain scriptParsed for 5s, find scriptId.
// 2. Locate target byte offset in the source (substring search), convert to line/col.
// 3. Set BP with side-effect condition.

const target = 'return H.reverse(),Pz4(K,H,D)';   // exact substring
const i = src.indexOf(target);
let line = 0, col = 0;
for (let k = 0; k < i; k++) { if (src[k] === '\n') { line++; col = 0; } else col++; }

await call('Debugger.setBreakpoint', {
  location: { scriptId, lineNumber: line, columnNumber: col },
  condition: `((function(){
    try {
      (globalThis.__caps = globalThis.__caps || []).push({
        ts: Date.now(),
        Vlen: V.length,
        Zuuid: Z ? String(Z.uuid).slice(0,12) : null,
        // ...whatever locals are in scope at this BP location
      });
    } catch(e) { (globalThis.__capsErr = globalThis.__capsErr || []).push(String(e)); }
    return false;     // critical: don't actually pause
  })())`,
});
```

The actualLocation in the BP-set response may differ from the requested
line/col by a few columns; V8 snaps to the nearest valid statement
boundary. Acceptable; just keep the BP set.

### Bash orchestration: hold the BP open across trigger + read

The BP is removed when its CDP session disconnects. So the BP-setting
script must HOLD its WebSocket open across the trigger-and-read window.

```sh
# 1. Background: set BP, hold connection N seconds
node /tmp/bp_setup.mjs "$WS_TARGET" 25 > /tmp/bp.log 2>&1 &
BP_PID=$!
sleep 8                          # let BP arm

# 2. Foreground: trigger via webview RPC (see next recipe)
node /tmp/cdp-eval.mjs "$WS_RENDERER_PAGE" '...trigger expression...'
sleep 2                          # let dispatch + BP fire

# 3. Read the global the BP populated
node /tmp/cdp-eval.mjs "$WS_TARGET" 'JSON.stringify(globalThis.__caps||[])'

wait $BP_PID                     # let BP script exit cleanly
```

Common failures:

- Forgot the `&` on step 1 → BP script blocks foreground, trigger
  never runs.
- Sleep too short → BP not yet armed when trigger fires.
- Reading via 9222 instead of `target` → `__caps` is `undefined`
  (different V8 process).

---

## Recipe: trigger an extension RPC from outside

Once you can post a structured message into the webview→extension
channel, you can invoke any handler. No UI interaction needed.

The webview→extension wire envelope is two-layer:

```js
// Outer envelope, what the dispatcher's outer switch matches on
{ type: "request", channelId, requestId, request: <inner> }

// Inner envelope, what readFromClient → handleRequest's inner switch matches on
{ type: "<inner_type>", ...inner_args }
```

To dispatch from outside, post via the OUTER vscode-webview iframe's
bridge function. The renderer-side shim (`acquireVsCodeApi`) wraps
`window.parent['__vscode_post_message__']`; that bridge is reachable
directly on the outer iframe:

```js
// Run this against the outer iframe's CDP target (9222 /devtools/page/<outer-id>)
window.__vscode_post_message__('onmessage', {
  message: {
    type: "request",
    channelId: "probe",                                      // any string for handlers that don't validate
    requestId: "rid" + Math.random().toString(36).slice(2),
    request: {
      type: "get_session_request",
      sessionId: "61974011-6e11-45a1-a489-7ab0b496dc95"
    }
  },
  transfer: undefined
});
```

Verified: this reaches `readFromClient` → `case "request":` →
`handleRequest(V)` → `case "get_session_request":` →
`this.getSession(V.request.sessionId)` → `Wz4` → `dl` → `Ez4`. Trace fires.

To enumerate all valid inner types:

```sh
grep -oE 'case"[a-z_]+":' /path/to/extension.js | sort -u
```

To find an inner type's exact field names, search `webview/index.js` for
how its own client constructs the request:

```sh
grep -B1 -A1 '{type:"<inner_type>"' /path/to/webview/index.js
```

Caveats:

- Some handlers require a `channelId` matching a real comm; invalid
  values may error silently. `get_session_request` and
  `list_sessions_request` accept any string.
- Destructive handlers (`delete_session`, `close_channel`,
  `interrupt_claude`, `launch_claude`): don't probe blindly. Test
  read-only RPCs first.

### Alternative: invoke the conn directly via fiber walk

If you've fiber-walked to the webview manager (next section), you can
bypass the bridge entirely:

```js
__mgr.sessions.value
  .find(s => s.sessionId === '<sid>')
  .connection.peek()
  .getSession('<sid>')
  .then(r => globalThis.__r = r);
```

`s.connection` is a Preact signal wrapping the actual conn (`class Jz1`).
`.peek()` derefs without subscribing. The conn instance has `getSession`
and `sendRequest` as instance methods.

---

## Recipe: read webview-side state (React fiber walk)

The chat panel's React app stores the session manager, conn, and per-
session state on the fiber tree. `acquireVsCodeApi` is consumed once at
startup and the resulting `vscode` is captured in closure, invisible to
`Object.keys(window)`. Fiber walking is the way in.

### Get the host root

```js
const root = document.querySelector('#root');
const fk = Object.keys(root).find(k => k.startsWith('__reactContainer$'));
const host = root[fk];                              // HostRoot Fiber
```

The container's `__reactContainer$<random>` key holds the HostRoot Fiber
directly. Its `.stateNode` is the FiberRootNode (with cycle back via
`.current`). For walking the app tree, just follow `host.child` /
`.sibling`.

### Find the manager

The depth/path from host to the React component holding the session
manager varies per panel TYPE (sidebar / editor / chat). Don't hard-code
it; use a generic walker that finds objects by method-name signature:

```js
(function(){
  const found = [];
  // Cross-origin-safe property test. fiber.memoizedProps can hold Window
  // references from cross-origin webview iframes; bare `o[k]` against those
  // throws SecurityError and aborts the walker silently (found ends up empty,
  // no stack trace, no obvious failure mode). Wrap every property access.
  function looksLikeMgr(o) {
    if (!o || typeof o !== 'object') return false;
    try {
      for (const k of ['getSession','sendRequest','listSessions','renameSession']) {
        if (typeof o[k] === 'function') return k;
      }
      const proto = Object.getPrototypeOf(o);
      if (proto && proto !== Object.prototype) {
        for (const m of ['getSession','sendRequest','listSessions','renameSession']) {
          if (Object.getOwnPropertyNames(proto).includes(m)) return 'proto:'+m;
        }
      }
    } catch { return false; }
  }
  let visited = 0;
  function walk(fiber, depth, label) {
    if (!fiber || visited > 8000 || depth > 60) return;
    visited++;
    try {
      const mp = fiber.memoizedProps;
      if (mp && typeof mp === 'object') {
        for (const k of Object.keys(mp)) {
          let v;
          try { v = mp[k]; } catch { continue; }    // cross-origin guard
          if (v && typeof v === 'object') {
            const m = looksLikeMgr(v);
            if (m) found.push({ where: label+'/props.'+k, depth, match: m });
          }
        }
      }
      let st = fiber.memoizedState; let h = 0;
      while (st && h < 30) {
        try {
          const ms = st.memoizedState;
          if (ms && typeof ms === 'object') {
            const m = looksLikeMgr(ms);
            if (m) found.push({ where: label+'/hook['+h+']', depth, match: m });
            try {
              if ('current' in ms && ms.current && looksLikeMgr(ms.current)) {
                found.push({ where: label+'/hook['+h+'].current', depth });
              }
            } catch {}
          }
        } catch {}
        st = st.next; h++;
      }
    } catch {}
    if (fiber.child) walk(fiber.child, depth+1, label+'>c');
    if (fiber.sibling) walk(fiber.sibling, depth, label+'>s');
  }
  const root = document.querySelector('#root');
  const fk = Object.keys(root).find(k => k.startsWith('__reactContainer$'));
  walk(root[fk], 0, 'h');
  globalThis.__hostRoot = root[fk];
  globalThis.__found = found;
  return JSON.stringify(found.slice(0, 10), null, 2);
})()
```

Wrap in IIFE; `Runtime.evaluate` rejects top-level `return`. Stash
references on `globalThis` so subsequent evals can navigate without
re-walking. Empirically, on a session-list webview, the manager sits at
`host.child.child.child.memoizedProps.sessions`.

**Cross-origin guard is mandatory.** Several fiber.memoizedProps slots in
the Antigravity workbench tree hold Window references from cross-origin
webview iframes (`origin="vscode-webview://<authority>"`). Touching any
property on those Windows from the wrong-origin context throws
`SecurityError: Failed to read a named property '...' from 'Window'`.
Without per-property `try`/`catch`, the walker bails on the first such
slot and silently returns zero matches; looks identical to "manager
genuinely not on this fiber tree", which is the wrong conclusion. If
`found` is empty AND the panel clearly has rendered messages, suspect
this gotcha first. Re-eval with `try`/`catch` wrappers around every
`mp[k]`, `st.memoizedState`, and `ms.current` access; the manager
typically materializes immediately.

### Manager structure

The webview-side session manager (class `Wn` in v2.1.126):

- `sessions`: Preact signal; `.value` is array of session entries
- `activeSession`: signal; `.value.id` is current sessionId
- `comms`: connection POOL (class `Qn`), proto: `get(sid)`, `open`,
  `close`. NOT the conn itself
- Per-session entry has rich state (see below)

### Per-session entry state

Each item in `mgr.sessions.value` carries the full reactive state for
one session:

- `connection`: signal-wrapped conn (`class Jz1` with `getSession`,
  `sendRequest`)
- `messages`: signal; `.value` is the array the React app renders
- `assembler`: stream-event to message transformer
  (`processStreamEvent` is its only proto method)
- `busy`, `isLoading`, `pendingInput`, `error`: signals (boolean)
- `sessionId`, `gitBranch`, `cwd`, `permissionMode`, `summary`,
  `lastModifiedTime`, `fileSize`, `isExplicit`, `isRemote`: signals
- `usageData`: signal `{totalTokens, totalCost, contextWindow,
  maxOutputTokens}`
- `currentModelInfo`, `thinkingLevel`, `effortLevel`, `fastModeState`:
  signals
- `todos`, `permissionRequests`, `proactiveSuggestions`,
  `settingsErrors`: signals (arrays)

### Preact signals everywhere, even fields that look like primitives

All reactive state is `{value: T, peek(), subscribe(...)}`. To read:

```js
mgr.sessions.value           // subscribes if in reactive context (no-op outside React render)
mgr.sessions.peek()          // never subscribes
```

Forgetting to deref gives you the signal object itself, which serializes
to nonsense like `{$$typeof, type, props, ref}`: looks like a React
element, isn't.

**The equality trap.** Even fields that LOOK like primitives (`sessionId`,
`gitBranch`, `cwd`, etc.) are signal-wrapped on each session entry. Strict
equality against a string ALWAYS fails:

```js
const s = mgr.sessions.value[0];
typeof s.sessionId                              // "object", NOT "string"
s.sessionId.constructor.name                    // "$3" (Preact signal)
s.sessionId === "61974011-6e11-..."             // false: signal !== string
String(s.sessionId) === "61974011-6e11-..."     // true: toString derefs
s.sessionId.peek() === "61974011-6e11-..."      // true: explicit deref
```

So `mgr.sessions.value.find(s => s.sessionId === sid)` always returns
`undefined` even when sid IS in the list. Use `.find(s => s.sessionId.peek() === sid)`
or `.find(s => String(s.sessionId) === sid)`. Index access (`arr[0]`)
works because no comparison is involved; but if you find yourself
"working around" `.find` returning undefined, the actual fix is to
deref the signal in the predicate.

Same hazard for ANY equality test against a fiber-walked field. Always
deref before comparing.

---

## Extension internals: state structures

Two singleton-like structures hold the per-window session state in
`extension.js`. Both are module-internal (not in `extension.exports`).

### Panel/state manager

Mutators include `updateSessionState`, `setActivePanel`,
`broadcastSessionStates`.

- `this.sessionStates: Map<sessionId, {sessionId, state, title}>`:
  the title cache. **Patch F's target.** `updateSessionState(V, K, B)`
  is the mutator (`V`=sessionId, `K`=state, `B`=title); the
  `/*pfg-vN*/` patchset signature is just upstream of it. Pencil rename in
  sidebar updates this; without Patch F, the rename was applied to
  `sessionPanels[sid].title` but the `sessionStates` entry's title
  wasn't refreshed, so on session-switch the broadcast resent the stale
  title and the sidebar flipped back.
- `this.sessionPanels: Map<sessionId, WebviewPanel>`: VS Code
  WebviewPanel objects, one per open chat panel. `panel.title` is
  mutated alongside `sessionStates` on rename.
- `this.activeSessionId: string`: currently focused session.
- `this.allComms: Set<Comm>`: all live comm instances; iterated for
  broadcasts.
- `this.webviews: Map`: comm to webview lookup.

### Session-content manager

Populated by `Wz4` (the loader path).

- `this.sessionMessages: Map<sessionId, Set<uuid>>`: uuid set per
  session. Presence in this Map is the "session is loaded" check.
- `this.messages: Map<uuid, msg>`: flat uuid to message map across all
  loaded sessions.
- `this.summaries: Map<uuid, summary>`: compact summaries.
- `this.customTitles: Map<sessionId, string>`: title from
  `custom-title` JSONL entries (Patch A writes these).
- `this.fileHistorySnapshots: Map<messageId, snapshot>`.
- `this.loadedSessions: Set<sessionId>`.

### Reaching them from outside

Both managers are module-internal. `extension.exports` is sparse (only
`activate`, `deactivate`, `openTabs`). The route in:

```js
// At ANY method on the manager, set a side-effect-condition BP that
// stashes `this` to a global. After one trigger you have the manager
// reachable for follow-up evals.
condition: '(globalThis.__mgr = this, false)'
```

Set on `updateSessionState` (frequently invoked by any session activity)
to capture quickly. Then:

```js
// target-exthost Runtime.evaluate, after globalThis.__mgr captured
globalThis.__mgr.updateSessionState(sid, "idle", "New Title");  // bypass RPC
globalThis.__mgr.broadcastSessionStates();                       // re-send
```

(There's no need to "clear a cache" to force a fresh `Wz4` invocation
on `get_session_request`: empirically verified, every RPC invokes
`Wz4` directly through the `Qo` indirection. No intermediate cache
exists between `getSession` and `Wz4`.)

---

## Field shapes across the layers

Different layers carry different shapes. Mismatched-key lookups return
`undefined` silently; common waste pattern.

| Layer | Convention | Sample fields |
|---|---|---|
| Message TYPES (everywhere) | snake_case | `get_session_request`, `compact_boundary`, `tool_use`, `tool_result` |
| JSONL on disk | camelCase, **rich** | `parentUuid`, `logicalParentUuid`, `sessionId`, `compactMetadata`, `isMeta`, `isSidechain`, `cwd`, `gitBranch`, … |
| `Yz4` parse / `Wz4` internals / V passed to Ez4 | camelCase, rich | parse pass-through |
| `bz4` transformer (last step in `dl`) | snake_case, **lossy** | only emits `{type, uuid, session_id, message, parent_tool_use_id:null, timestamp}`; drops `parentUuid`, `logicalParentUuid`, `compactMetadata`, etc. |
| Wire response (`get_session_response.messages`) | snake_case, lossy | same as bz4 output |
| Assembler output `s.messages.peek()` (what React renders) | camelCase, **re-shaped** | `type, content, uuid, timestamp, parentToolUseId, betaMessageId, isSynthetic, compactMetadata, compactSummary` |
| claude subprocess stream events | snake_case | mirrors Anthropic API |

`dl(V, K)` chain: `Ez4(V)` (camelCase rich) → `.filter(Sz4)` (camelCase) →
`.map(bz4)` (camelCase→snake_case, lossy) → `Cz4` (offset/limit). The
bz4 output is what the wire carries.

But the React side does NOT render the wire shape directly. The
webview's per-session **assembler** (each session entry's
`assembler.processStreamEvent`, plus initial-replay handling) consumes a
mix of stream events from the claude subprocess AND get_session_response
payloads, and produces a THIRD shape that's camelCase + re-flattened
(`content` instead of `message.content`, `parentToolUseId` instead of
`parent_tool_use_id`, with extra synthetic fields like `betaMessageId`,
`isSynthetic`).

`bz4` itself:

```js
function bz4(V){
  return {type: V.type, uuid: V.uuid, session_id: V.sessionId,
          message: V.message, parent_tool_use_id: null, timestamp: V.timestamp}
}
```

**Where to read what, when:**

- BP at `Ez4` / `Wz4` → locals are camelCase, full JSONL shape (rich).
- BP just AFTER `dl` returns → snake_case, lossy. Wire shape.
- `__mgr.sessions.value[N].messages.peek()` from the webview → camelCase,
  ASSEMBLER shape. `sessionId` and `session_id` are BOTH absent;
  session is implicit (the messages array hangs off the session entry).
- JSONL on disk via `require('fs').readFileSync` → camelCase, full
  JSONL shape (raw).

Common gotchas:

- Looking for `sessionId` or `session_id` on
  `__mgr...messages.peek()[N]`: neither is there. Owner-of-array, not
  field-on-element.
- Looking for `parentUuid` / `logicalParentUuid` on the React side:
  gone. The assembler discards them. To debug compaction-chain
  stitching, you MUST read at Ez4 (or earlier), not at the React side.
- Looking for `parent_tool_use_id` on the React side: renamed to
  `parentToolUseId` by the assembler.

---

## Refresh / reload playbook

Different mechanisms, ordered roughly by precision (most surgical
first):

### 1. Direct extension RPC

See "Recipe: trigger an extension RPC from outside" above. Most
surgical, no UI side effects. Verified end-to-end.

### 2. Synthetic DOM events

Simulates user UI interaction in the React app. Fires the React
component's onClick handlers; the message goes through normal channels.

```js
// In the inner active-frame's main world (use eval_in_inner_frame.mjs)
const btn = document.querySelector('[aria-label="Rename"]');
btn?.click();   // simple synthetic; works for most React onClick
// or fuller fidelity:
btn?.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
```

Verified: all three forms (`.click()`, simple `dispatchEvent`, full
`dispatchEvent`) fire React `onClick` handlers in the inner frame.

Selector discovery requires inspecting the rendered DOM in the
active-frame first.

### 3. Keyboard events via `Input.dispatchKeyEvent`

CDP renderer-side primitive that fires keys at the workbench keybinding
pipeline. Reaches Workbench commands like `workbench.action.reloadWindow`,
`workbench.action.closeActiveEditor`, etc.

```js
// Run against a top-level workbench page (NOT a webview iframe).
// Modifiers bitfield: Alt=1, Ctrl=2, Meta=4, Shift=8; OR them.
// Ctrl+Shift+P opens command palette:
await call('Input.dispatchKeyEvent', { type: 'rawKeyDown', modifiers: 10,
  key: 'P', code: 'KeyP', keyCode: 80, windowsVirtualKeyCode: 80 });
await call('Input.dispatchKeyEvent', { type: 'keyUp', modifiers: 10,
  key: 'P', code: 'KeyP', keyCode: 80, windowsVirtualKeyCode: 80 });
// Verify by checking if .quick-input-widget appeared in DOM.
```

Verified: opens the command palette ("Type the name of a command to
run.") and ESC closes it. Note: only works on real workbench pages;
the Antigravity Launchpad page has no command palette.

### 4. Direct method calls on the manager singleton

After capturing the manager via the BP-stash trick:

```js
globalThis.__mgr.updateSessionState(sid, "idle", "New Title");  // bypass RPC
globalThis.__mgr.broadcastSessionStates();                       // re-send to all webviews
```

Useful for fine-grained state tweaks. (To force a fresh load you don't
need to clear any cache; `get_session_request` already invokes `Wz4`
every call, verified empirically.)

### 5. The nuclear option

Edit `extension.js` on disk + `Developer: Reload Window`. The reliable hot-swap: it
survives JIT-cached / closure-bound call sites where `setScriptSource`
does not (per the gotcha). A lighter in-process swap that rebinds
future lookups instead of already-bound references (re-bind the export,
clear `require.cache`, a `Proxy`) is not something I have ruled out. Slow (~5–15s for the reload). This is what
the patch-claude skill uses.

### Picking which mechanism

- "Test how the extension reacts to a webview message" → mechanism 1
  (RPC).
- "Verify a UI button does what I think" → mechanism 2 (DOM click).
- "Reload the whole window or trigger a workbench command" → mechanism 3
  (key event) or 5 (disk patch + reload).
- "Clear a specific cache or twiddle internal state" → mechanism 4
  (direct method).
- "Apply a code change that actually runs" → mechanism 5. **Don't
  waste turns on `setScriptSource`.**

---

## Patch development iteration loop

The canonical loop for editing live extension files and verifying the result
in the running IDE. One cycle takes 20-40 seconds (mostly the reload wait).

```
edit extension.js / webview/index.js on disk
        ↓
trigger Developer: Reload Window via CDP        (exthost reloads patched code)
        ↓
wait ~15s for exthost restart
        ↓
re-discover iframe target (ID changes on every reload)
        ↓
FORCE A FRESH WALK: open the session as a NEW tab via the conversations
panel, or close+reopen its tab. A rehydrated tab is STALE (no re-walk).
        ↓
re-discover the new chat-panel iframe (fresh mount = new target id)
        ↓
DOM-verify [data-pfgk-role] counts + body text in active-frame
        ↓
iterate
```

The make-or-break step is the fresh walk (Step 6). A bare reload restores the
old rendered DOM from serialized state; it does not re-run the walker. See Step
6 for why, and the two triggers that actually re-mount the webview.

### One-shot runnable block (full cycle, copy-paste)

The Steps below break the cycle down for understanding, but each fenced block is
a separate shell, so shell vars (`$WB_PAGE`, `$PANEL_WS`, `$BEFORE`, `$WS_CHAT`)
do NOT persist between them, and several blocks reference vars another block set.
For an actual zero-shot run, paste THIS single block: it recreates every `/tmp`
helper inline, threads all vars through one shell, opens "PFGK DEMO seam" as a
FRESH tab via the conversations panel, re-discovers the freshly-mounted iframe by
diffing `/json/list`, and prints the marker probe (`pfg_markers.js`). Assumes you already did the disk
edit + Reload Window (Steps 1-3) so the exthost runs the new code, and that the
`PFGK DEMO seam` fixture exists in an open-workspace project (Step 8). Change
`NEEDLE` to verify a different session.

```sh
set -u
NEEDLE="PFGK DEMO seam"          # session title to open + verify
R=http://127.0.0.1:9222          # renderer CDP HTTP endpoint (DOM side)

# ---- helper: cdp-eval.mjs (one-shot Runtime.evaluate against any target) ----
cat > /tmp/cdp-eval.mjs <<'EOF'
const wsUrl = process.argv[2], exprArg = process.argv[3];
const expression = exprArg.startsWith('@')
  ? await (async () => { const fs = await import('node:fs/promises'); return fs.readFile(exprArg.slice(1), 'utf8'); })()
  : exprArg;
const ws = new WebSocket(wsUrl);
let nextId = 1;
const pending = new Map();
ws.addEventListener('open', () => {
  send('Runtime.enable', {}).then(() =>
    send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true,
                               allowUnsafeEvalBlockedByCSP: true, includeCommandLineAPI: true })
  ).then(r => { console.log(JSON.stringify(r, null, 2)); ws.close(); });
});
ws.addEventListener('message', ev => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(msg.error); else resolve(msg.result);
  }
});
function send(method, params) {
  const id = nextId++;
  return new Promise((resolve, reject) => { pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params })); });
}
EOF

# ---- helper: eval_in_inner_frame.mjs (drill into the active-frame React app) -
cat > /tmp/eval_in_inner_frame.mjs <<'EOF'
const wsUrl = process.argv[2];
const expression = process.argv[3].startsWith('@')
  ? await (async () => { const fs = await import('node:fs/promises'); return fs.readFile(process.argv[3].slice(1), 'utf8'); })()
  : process.argv[3];
const ws = new WebSocket(wsUrl);
let nextId = 1, pending = new Map();
const ctxEvents = [];
ws.addEventListener('message', ev => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(msg.error); else resolve(msg.result);
  } else if (msg.method === 'Runtime.executionContextCreated') {
    ctxEvents.push(msg.params.context);
  }
});
function call(method, params={}) {
  const id = nextId++;
  return new Promise((resolve, reject) => { pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params })); });
}
await new Promise(r => ws.addEventListener('open', r));
await call('Page.enable');
await call('Runtime.enable');
await new Promise(r => setTimeout(r, 800));
const tree = await call('Page.getFrameTree');
function findInner(node, acc=[]) { if (node.frame) acc.push(node.frame); (node.childFrames||[]).forEach(c => findInner(c, acc)); return acc; }
const allFrames = findInner(tree.frameTree);
const innerFrame = allFrames.find(f => f.name === 'active-frame');
if (!innerFrame) { console.error('FAIL: no name=active-frame'); process.exit(1); }
const mainCtx = ctxEvents.find(c => c.auxData?.frameId === innerFrame.id && !c.name && c.origin);
if (!mainCtx) { console.error('FAIL: no main-world context'); process.exit(1); }
const r = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true, contextId: mainCtx.id, includeCommandLineAPI: true });
console.log(JSON.stringify(r, null, 2));
ws.close();
EOF

# ---- helper: pfg_markers.js (general marker probe) -------------------------
cat > /tmp/pfg_markers.js <<'EOF'
JSON.stringify({
  pfgkAlert: document.querySelectorAll(".pfgkAlert").length,
  bookend:   document.querySelectorAll('[data-pfgk-role="bookend"]').length,
  seam:      document.querySelectorAll('[data-pfgk-role="seam"]').length,
  seamClean: document.querySelectorAll('[data-pfgk-role="seamClean"]').length,
  bridge:    document.querySelectorAll('[data-pfgk-role="bridge"]').length,
  broken:    document.querySelectorAll('[data-pfgk-role="broken"]').length,
  markerText:(() => { const c = document.querySelector(".pfgkAlert"); return c ? c.textContent.replace(/\s+/g, " ").trim().slice(0, 400) : null; })()
})
EOF

# ---- helper: click_convo.js (click a panel row by title needle) ------------
cat > /tmp/click_convo.js <<'EOF'
(function(){
  var needle=NEEDLE;
  var rows=[...document.querySelectorAll('div,li,a,button')],target=null;
  for(var i=0;i<rows.length;i++){
    var t=(rows[i].innerText||'').replace(/\s+/g,' ').trim();
    if(t.indexOf(needle)>=0 && t.length<220 && rows[i].children.length<=8) target=rows[i];
  }
  if(!target) return {found:false};
  var el=target;
  for(var k=0;k<6 && el && el.parentElement;k++){
    var c=String(el.className||'');
    if(/row|item|session|card|listItem/i.test(c)||(el.getAttribute&&el.getAttribute('role')==='button')) break;
    el=el.parentElement;
  }
  (el||target).click();
  return {found:true, text:(target.innerText||'').replace(/\s+/g,' ').trim().slice(0,60)};
})()
EOF

# ---- helper: panel_ready.mjs (open activity-bar panel, search, print WS) ----
cat > /tmp/panel_ready.mjs <<'EOF'
import http from 'node:http';
const WB=process.argv[2];
const NEEDLE=process.argv[3]||'';
const getJSON=p=>new Promise((res,rej)=>{http.get('http://127.0.0.1:9222'+p,r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>res(JSON.parse(d)));}).on('error',rej);});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function withWS(wsUrl,fn){return new Promise((resolve,reject)=>{const ws=new WebSocket(wsUrl);let nid=1;const pend=new Map();const ctx=[];ws.addEventListener('message',ev=>{const m=JSON.parse(ev.data);if(m.id&&pend.has(m.id)){const{res,rej}=pend.get(m.id);pend.delete(m.id);m.error?rej(m.error):res(m.result);}else if(m.method==='Runtime.executionContextCreated')ctx.push(m.params.context);});const call=(method,params={})=>{const id=nid++;return new Promise((res,rej)=>{pend.set(id,{res,rej});ws.send(JSON.stringify({id,method,params}));});};ws.addEventListener('open',async()=>{try{const r=await fn(call,ctx);ws.close();resolve(r);}catch(e){ws.close();reject(e);}});ws.addEventListener('error',e=>reject(e));setTimeout(()=>{try{ws.close();}catch(_){}reject(new Error('to'));},7000);});}
const wbEval=expr=>withWS(WB,async call=>{await call('Runtime.enable');const r=await call('Runtime.evaluate',{expression:expr,returnByValue:true});return r.result.value;});
async function inActive(wsUrl,expr){return withWS(wsUrl,async(call,ctx)=>{await call('Page.enable');await call('Runtime.enable');await sleep(600);const tree=await call('Page.getFrameTree');const fr=[];(function w(n){if(n.frame)fr.push(n.frame);(n.childFrames||[]).forEach(w);})(tree.frameTree);const inner=fr.find(f=>f.name==='active-frame');if(!inner)return {__noframe:1};const c=ctx.find(x=>x.auxData&&x.auxData.frameId===inner.id&&!x.name&&x.origin);if(!c)return {__noctx:1};const r=await call('Runtime.evaluate',{expression:expr,returnByValue:true,contextId:c.id});return r.result.value;}).catch(e=>({__err:String(e.message||e)}));}
let ready=false;
for(let i=0;i<20;i++){try{if(await wbEval(`document.querySelectorAll('.activitybar [aria-label="Claude Code"]').length`)>0){ready=true;break;}}catch(e){}await sleep(1000);}
if(!ready){console.log('WB_NOT_READY');process.exit(1);}
await wbEval(`(function(){var t=document.querySelector('.activitybar .action-item a[aria-label="Claude Code"],.activitybar [aria-label="Claude Code"]');if(t)t.click();return !!t;})()`);
await sleep(2000);
let panelWs=null;
for(let i=0;i<12 && !panelWs;i++){
  for(const f of (await getJSON('/json/list')).filter(t=>t.type==='iframe'&&(t.url||'').includes('index'))){
    if(await inActive(f.webSocketDebuggerUrl,`!!document.querySelector('input[placeholder*="Search" i]')`)===true){panelWs=f.webSocketDebuggerUrl;break;}
  }
  if(!panelWs)await sleep(1200);
}
if(!panelWs){console.log('PANEL_NOT_FOUND');process.exit(1);}
await inActive(panelWs,`(function(){var inp=document.querySelector('input.search,.filterInput_90gk3A,input[placeholder*="Search" i]');var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(inp,'');inp.dispatchEvent(new Event('input',{bubbles:true}));s.call(inp,${JSON.stringify(NEEDLE)});inp.dispatchEvent(new Event('input',{bubbles:true}));inp.focus();return inp.value;})()`);
await sleep(900);
console.log('PANEL_WS='+panelWs);
EOF

# A. workbench top-level page id (9222, NOT an iframe, NOT Launchpad)
WB_PAGE=$(curl -s "$R/json/list" | python3 -c '
import sys, json
for t in json.load(sys.stdin):
    if t.get("type")=="page" and "Launchpad" not in (t.get("title") or ""):
        print(t["id"]); break')
echo "workbench page: $WB_PAGE"
WB="ws://127.0.0.1:9222/devtools/page/$WB_PAGE"

# B. snapshot chat-iframe ids BEFORE opening (to spot the fresh mount after)
BEFORE=$(curl -s "$R/json/list" | python3 -c '
import sys, json
print(" ".join(t["id"] for t in json.load(sys.stdin)
      if t.get("type")=="iframe" and "vscode-webview" in (t.get("url") or "")))')

# C. open the conversations panel + search the title; capture PANEL_WS
PANEL_WS=$(node /tmp/panel_ready.mjs "$WB" "$NEEDLE" | sed -n 's/^PANEL_WS=//p')
echo "panel iframe WS: $PANEL_WS"
[ -z "$PANEL_WS" ] && { echo "ABORT: conversations panel not found"; exit 1; }

# D. click the matching row -> mounts a FRESH chat tab (real Wz4 re-walk)
sed "s/NEEDLE/\"$NEEDLE\"/" /tmp/click_convo.js > /tmp/click_convo.ready.js
node /tmp/eval_in_inner_frame.mjs "$PANEL_WS" @/tmp/click_convo.ready.js \
  | python3 -c "import sys,json;print('clicked:',json.load(sys.stdin).get('result',{}).get('value'))"

# E. find the fresh chat iframe (id NOT in BEFORE), poll it until markers paint
for attempt in $(seq 1 8); do
  sleep 3
  for id in $(curl -s "$R/json/list" | python3 -c '
import sys, json
print("\n".join(t["id"] for t in json.load(sys.stdin)
      if t.get("type")=="iframe" and "vscode-webview" in (t.get("url") or "")))'); do
    case " $BEFORE " in *" $id "*) continue;; esac          # skip pre-existing iframes
    out=$(node /tmp/eval_in_inner_frame.mjs "ws://127.0.0.1:9222/devtools/page/$id" @/tmp/pfg_markers.js 2>/dev/null)
    val=$(printf '%s' "$out" | python3 -c "import sys,json
try: print(json.load(sys.stdin)['result']['value'])
except Exception: pass" 2>/dev/null)
    # Require a marker actually painted (pfgkAlert>=1): the conversations-panel
    # iframe also answers the probe but with pfgkAlert:0 and is a false target.
    if printf '%s' "$val" | python3 -c "import sys,json
try: sys.exit(0 if json.load(sys.stdin).get('pfgkAlert',0)>=1 else 1)
except Exception: sys.exit(1)"; then
      echo "=== fresh chat iframe $id (attempt $attempt) ==="
      echo "$val"
      echo ">>> Inspect markerText: the string your edit REMOVED must be absent and the one it ADDED present. Pre-edit text means a stale render (Step 6 skipped) or old exthost code, not a failed edit."
      exit 0
    fi
  done
done
echo "no freshly-mounted marker iframe found (check the panel row title, or the tab did not open)"; exit 1
```

Expected: the fresh tab shows the markers your fixture or session produces (e.g.
`pfgkAlert >= 1` with the relevant role), and `markerText` reflects your edit. If
markers are missing, or `markerText` still shows the pre-edit content, the render
is stale (you skipped Step 6) or the exthost runs old code (reload, re-run), not
a failed edit. The Steps below explain each piece.

### Step 0: ensure helpers are on disk

```sh
# Write cdp-eval.mjs if not present
ls /tmp/cdp-eval.mjs /tmp/eval_in_inner_frame.mjs 2>/dev/null \
  || echo "MISSING: write them from the Tooling section above"
```

Both scripts require Node 22+ (built-in `WebSocket`). See the Tooling section
for their full source.

### Step 1: edit the live extension file(s)

```sh
EXT="$HOME/.antigravity-ide/extensions/anthropic.claude-code-2.1.159-linux-x64"
# For other IDEs, adjust the path:
#   VS Code:   ~/.vscode/extensions/anthropic.claude-code-*-linux-x64
#   Cursor:    ~/.cursor/extensions/anthropic.claude-code-*-linux-x64
#   VSCodium:  ~/.vscode-oss/extensions/anthropic.claude-code-*-linux-x64

# Edit directly (the .bak was already created on first patch apply):
#   $EXT/extension.js          (extension host logic)
#   $EXT/webview/index.js      (React renderer)
#   $EXT/webview/index.css     (styles)

# Sanity check before reloading:
node --check "$EXT/extension.js" && echo "syntax OK"
```

Never edit `.bak` files. The `.bak` is the pristine baseline; see MAINTAINER.md.

### Step 2: trigger Developer: Reload Window via CDP

Run this against a top-level workbench page (not an iframe, not Launchpad):

```sh
# Find the workbench page ID
PAGE=$(curl -s http://127.0.0.1:9222/json/list | python3 -c '
import sys, json
for t in json.load(sys.stdin):
    if t.get("type") == "page" and "Launchpad" not in (t.get("title") or ""):
        print(t["id"]); break
')
echo "workbench page: $PAGE"

# Write and run the reload script
cat > /tmp/reload_go.mjs << 'EOF'
const wsUrl = `ws://127.0.0.1:9222/devtools/page/${process.argv[2]}`;
const ws = new WebSocket(wsUrl);
let nextId = 1; const pending = new Map();
const sleep = ms => new Promise(r => setTimeout(r, ms));
ws.addEventListener('message', ev => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const {resolve, reject} = pending.get(msg.id); pending.delete(msg.id);
    if (msg.error) reject(msg.error); else resolve(msg.result);
  }
});
function call(method, params={}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, {resolve, reject});
    ws.send(JSON.stringify({id, method, params}));
  });
}
await new Promise(r => ws.addEventListener('open', r));
// Open command palette with Ctrl+Shift+P (modifiers: Ctrl=2, Shift=8 → 10)
await call('Input.dispatchKeyEvent', {type:'rawKeyDown', modifiers:10,
  key:'P', code:'KeyP', keyCode:80, windowsVirtualKeyCode:80});
await call('Input.dispatchKeyEvent', {type:'keyUp', modifiers:10,
  key:'P', code:'KeyP', keyCode:80, windowsVirtualKeyCode:80});
await sleep(400);
// Verify palette opened
const after = JSON.parse((await call('Runtime.evaluate', {expression:
  '(()=>{const w=document.querySelector(".quick-input-widget");return JSON.stringify({open:!!w,val:(w&&w.querySelector("input"))?.value})})()',
  returnByValue:true})).result.value);
if (!after.open) { console.log("ABORT: palette did not open"); ws.close(); process.exit(1); }
// Type the command without clearing the ">" prefix
await call('Input.insertText', {text: 'Developer: Reload Window'});
await sleep(400);
// Verify first suggestion is the right command
const st = JSON.parse((await call('Runtime.evaluate', {expression:
  '(()=>{const w=document.querySelector(".quick-input-widget");const f=w&&w.querySelector(".quick-input-list .monaco-list-row");return JSON.stringify({input:(w&&w.querySelector("input"))?.value,first:f?.textContent?.slice(0,60).trim()||null})})()',
  returnByValue:true})).result.value);
if (!st.first || !st.first.startsWith('Developer: Reload Window')) {
  console.log('ABORT: first result is', st.first); ws.close(); process.exit(1);
}
console.log('reloading:', st.first);
try {
  await call('Input.dispatchKeyEvent', {type:'rawKeyDown', key:'Enter',
    code:'Enter', keyCode:13, windowsVirtualKeyCode:13});
  await call('Input.dispatchKeyEvent', {type:'keyUp', key:'Enter',
    code:'Enter', keyCode:13, windowsVirtualKeyCode:13});
  console.log('RELOAD TRIGGERED');
} catch (e) { console.log('enter sent'); }
ws.close();
EOF
node /tmp/reload_go.mjs "$PAGE"
```

If the palette opens but typing fails (focus stuck on a broken iframe), run a
body-area mouse click first. See the "Focus-lost-to-broken-iframe" gotcha.

### Step 3: wait for the exthost to restart

```sh
echo "waiting for exthost..."
for i in $(seq 1 20); do
  sleep 1
  R=$(curl -s --max-time 1 http://127.0.0.1:9229/json/version 2>/dev/null)
  [ -n "$R" ] && echo "exthost up after ${i}s" && break
done
```

Typical wait: 12-16 seconds. The renderer (9222) comes up faster than the
exthost (9229). Do not proceed to verification until the exthost responds.

### Step 4: confirm the patch is in the running in-memory code

Disk edit + reload is the reliable swap, but verify the new exthost actually
loaded the edited file:

```sh
EXT="$HOME/.antigravity-ide/extensions/anthropic.claude-code-2.1.159-linux-x64"
WS=$(curl -s http://127.0.0.1:9229/json | python3 -c \
  'import sys,json; print(json.load(sys.stdin)[0]["webSocketDebuggerUrl"])')
node /tmp/cdp-eval.mjs "$WS" "(function(){
  var s = require('fs').readFileSync('$EXT/extension.js','utf-8');
  return JSON.stringify({
    sig: (s.match(/\/\*pfg-v[\d.]+\*\//g)||[]),
    hasMyMarker: s.includes('<unique substring of your edit>')
  });
})()"
```

Replace `<unique substring of your edit>` with a string that only your new
code contains (a comment, a variable name, a literal). Empty `sig` means the
patches aren't installed at all; run `/patch-claude` first.

`readFileSync` on the exthost reads the on-disk file. To confirm what the
exthost is RUNNING (in-memory), use `Debugger.getScriptSource` instead. See
the "Debugger.getScriptSource shows in-memory script, NOT disk" gotcha for
the full recipe. In practice, disk edit + Reload Window always produces
disk == in-memory; `readFileSync` is sufficient for normal iteration.

### Step 5: re-discover the chat-panel iframe target

Iframe CDP target IDs change on every reload. Do not reuse old IDs.

```sh
# All iframe targets after reload (abbreviated):
curl -s http://127.0.0.1:9222/json/list | python3 -c "
import sys, json
for t in json.load(sys.stdin):
    if t.get('type') == 'iframe':
        print(t['id'], '|', t.get('url','')[:100])
"
```

To narrow to a specific session's chat panel:

```sh
SID="<your-session-uuid>"
IFRAME=$(curl -s http://127.0.0.1:9222/json/list | python3 -c "
import sys, json
for t in json.load(sys.stdin):
    if t.get('type') == 'iframe' and 'session=$SID' in t.get('url',''):
        print(t['id']); break
")
echo "chat panel iframe: $IFRAME"
```

If no iframe matches by session ID (session wasn't visible before the reload),
open or switch to the session in the IDE and repeat the curl. The iframe
appears when the panel is visible, not before.

### Step 6: force a FRESH walk + re-render (the rehydration trap)

**This is the step the loop lives or dies on. Read it before improvising.**

A session that was visible before the reload comes back as a STALE rehydrated
webview: VS Code restores the editor's serialized DOM/state, it does NOT re-run
`Wz4`. So the markers you see after a bare reload are from the OLD code, even
though the new code is live in the exthost. Waiting for the iframe to reappear
in `/json/list` is NOT verification. The `get_session_request` RPC to a
throwaway `probe` channel (below) likewise does NOT re-render the visible panel
(no webview is subscribed to `probe`); it only runs the walker for capture.

What actually re-renders fresh: the chat panels are **editor tabs**, and
**editor-tab webviews are disposed when hidden**. Activating a fresh tab (or
closing + reopening one) disposes the stale webview and forces a brand-new
mount, which re-runs `Wz4` → `Ez4` against the on-disk JSONL with your patched
code. Two proven triggers:

**6a. Open the session fresh via the conversations panel (preferred).** The
panel is the activity-bar "Claude Code" view (NOT a programmatic RPC). Opening
a session that is not already an open tab mounts a fresh webview. Drive it from
the workbench page (port 9222), discover the panel iframe by its Search box,
type the session's title, click the row. The `/tmp/panel_ready.mjs` recovered
below does discovery → open → search → list-rows in one shot; the
`/tmp/click_convo.js` snippet clicks a row by needle.

```sh
# Open the activity-bar panel, search a title, dump matching rows.
# WB = the workbench top-level page (NOT an iframe, NOT Launchpad).
cat > /tmp/panel_ready.mjs <<'EOF'
import http from 'node:http';
const WB=process.argv[2];                       // ws://127.0.0.1:9222/devtools/page/<workbench-page-id>
const NEEDLE=process.argv[3]||'';               // title substring to search/list
const getJSON=p=>new Promise((res,rej)=>{http.get('http://127.0.0.1:9222'+p,r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>res(JSON.parse(d)));}).on('error',rej);});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function withWS(wsUrl,fn){return new Promise((resolve,reject)=>{const ws=new WebSocket(wsUrl);let nid=1;const pend=new Map();const ctx=[];ws.addEventListener('message',ev=>{const m=JSON.parse(ev.data);if(m.id&&pend.has(m.id)){const{res,rej}=pend.get(m.id);pend.delete(m.id);m.error?rej(m.error):res(m.result);}else if(m.method==='Runtime.executionContextCreated')ctx.push(m.params.context);});const call=(method,params={})=>{const id=nid++;return new Promise((res,rej)=>{pend.set(id,{res,rej});ws.send(JSON.stringify({id,method,params}));});};ws.addEventListener('open',async()=>{try{const r=await fn(call,ctx);ws.close();resolve(r);}catch(e){ws.close();reject(e);}});ws.addEventListener('error',e=>reject(e));setTimeout(()=>{try{ws.close();}catch(_){}reject(new Error('to'));},7000);});}
const wbEval=expr=>withWS(WB,async call=>{await call('Runtime.enable');const r=await call('Runtime.evaluate',{expression:expr,returnByValue:true});return r.result.value;});
async function inActive(wsUrl,expr){return withWS(wsUrl,async(call,ctx)=>{await call('Page.enable');await call('Runtime.enable');await sleep(600);const tree=await call('Page.getFrameTree');const fr=[];(function w(n){if(n.frame)fr.push(n.frame);(n.childFrames||[]).forEach(w);})(tree.frameTree);const inner=fr.find(f=>f.name==='active-frame');if(!inner)return {__noframe:1};const c=ctx.find(x=>x.auxData&&x.auxData.frameId===inner.id&&!x.name&&x.origin);if(!c)return {__noctx:1};const r=await call('Runtime.evaluate',{expression:expr,returnByValue:true,contextId:c.id});return r.result.value;}).catch(e=>({__err:String(e.message||e)}));}
// 1. wait for the workbench to be back (activity-bar present)
let ready=false;
for(let i=0;i<30;i++){try{if(await wbEval(`document.querySelectorAll('.activitybar [aria-label="Claude Code"]').length`)>0){ready=true;break;}}catch(e){}await sleep(1500);}
if(!ready){console.log('WB_NOT_READY');process.exit(1);}
// 2. click the activity-bar "Claude Code" view to open the conversations panel
await wbEval(`(function(){var t=document.querySelector('.activitybar .action-item a[aria-label="Claude Code"],.activitybar [aria-label="Claude Code"]');if(t)t.click();return !!t;})()`);
await sleep(2000);
// 3. find the panel iframe: the index.html iframe that has a Search box
let panelWs=null;
for(let i=0;i<12 && !panelWs;i++){
  for(const f of (await getJSON('/json/list')).filter(t=>t.type==='iframe'&&(t.url||'').includes('index'))){
    if(await inActive(f.webSocketDebuggerUrl,`!!document.querySelector('input[placeholder*="Search" i]')`)===true){panelWs=f.webSocketDebuggerUrl;break;}
  }
  if(!panelWs)await sleep(1200);
}
if(!panelWs){console.log('PANEL_NOT_FOUND');process.exit(1);}
// 4. set the search box (native setter + input event so React sees it)
await inActive(panelWs,`(function(){var inp=document.querySelector('input.search,.filterInput_90gk3A,input[placeholder*="Search" i]');var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(inp,'');inp.dispatchEvent(new Event('input',{bubbles:true}));s.call(inp,${JSON.stringify(NEEDLE)});inp.dispatchEvent(new Event('input',{bubbles:true}));inp.focus();return inp.value;})()`);
await sleep(900);
// 5. dump matching rows (and click the first exact-ish match)
const rows=await inActive(panelWs,`(function(){var out=[],seen={};for(var e of document.querySelectorAll('div,li,a,button')){var t=(e.innerText||'').replace(/\\s+/g,' ').trim();if(t.length>10&&t.length<170&&t.indexOf(${JSON.stringify(NEEDLE)})>=0&&e.children.length<=8&&!seen[t]){seen[t]=1;out.push(t.slice(0,95));}}return out;})()`);
console.log('PANEL_WS='+panelWs);
console.log('ROWS='+JSON.stringify(rows));
EOF

# Discover the workbench page id, then run. Capture PANEL_WS from the output
# (panel_ready.mjs PRINTS it; it is not exported), since the next block needs it:
WB_PAGE=$(curl -s http://127.0.0.1:9222/json/list | python3 -c '
import sys, json
for t in json.load(sys.stdin):
    if t.get("type")=="page" and "Launchpad" not in (t.get("title") or ""):
        print(t["id"]); break')
PANEL_WS=$(node /tmp/panel_ready.mjs "ws://127.0.0.1:9222/devtools/page/$WB_PAGE" "PFGK DEMO seam" \
           | sed -n 's/^PANEL_WS=//p')
echo "PANEL_WS=$PANEL_WS"
```

Then click the row by title needle (this opens a FRESH tab). NB: `$PANEL_WS`
must be set in the SAME shell, or re-derive it (the one-shot block above threads
it for you):

```sh
cat > /tmp/click_convo.js <<'JS'
(function(){
  var needle=NEEDLE, rows=[...document.querySelectorAll('div,li,a,button')], target=null;
  for(var e of rows){var t=(e.innerText||'').replace(/\s+/g,' ').trim();
    if(t.indexOf(needle)>=0 && t.length<220 && e.children.length<=8) target=e;}
  if(!target) return {found:false};
  var el=target;
  for(var k=0;k<6 && el && el.parentElement;k++){
    var c=String(el.className||'');
    if(/row|item|session|card|listItem/i.test(c)||(el.getAttribute&&el.getAttribute('role')==='button')) break;
    el=el.parentElement;
  }
  (el||target).click();
  return {found:true, text:(target.innerText||'').replace(/\s+/g,' ').trim().slice(0,60)};
})()
JS
sed -i 's/NEEDLE/"PFGK DEMO seam"/' /tmp/click_convo.js
# PANEL_WS printed by panel_ready.mjs above:
node /tmp/eval_in_inner_frame.mjs "$PANEL_WS" @/tmp/click_convo.js
```

**6b. Close + reopen the active editor tab (force fresh re-walk of the open
session).** Activating any other tab disposes the current one; reopening
re-mounts it fresh. From the workbench page (9222):

```sh
# Click a chat tab by title to ACTIVATE it (disposes the previously-active one,
# which then re-mounts fresh on next activation):
node /tmp/cdp-eval.mjs "ws://127.0.0.1:9222/devtools/page/$WB_PAGE" \
 '(function(){var t=[].slice.call(document.querySelectorAll(".tabs-container .tab"))
   .filter(function(e){return /PFGK DEMO seam/i.test(e.getAttribute("aria-label")||e.textContent||"")})[0];
   if(!t)return "tab not found"; t.click(); return "clicked "+(t.getAttribute("aria-label")||"").slice(0,45);})()'
# Or via palette: workbench.action.closeActiveEditor then
# workbench.action.reopenClosedEditor (Input.dispatchKeyEvent, mechanism 3).
```

After the fresh mount, the chat-panel iframe is a NEW CDP target with a NEW id;
re-discover it (Step 5) before probing. The robust discovery when several chats
are open (or the session-id URL filter is ambiguous) is to **snapshot the
`vscode-webview` iframe ids BEFORE opening, then diff after**: the id NOT in the
snapshot is the fresh mount. The mount + first render takes a few seconds, so
poll (the marker probe returns nothing until the React app has painted). This
loop uses `/tmp/pfg_markers.js`, written in Step 7 below:

```sh
# BEFORE opening the session:
BEFORE=$(curl -s http://127.0.0.1:9222/json/list | python3 -c '
import sys, json
print(" ".join(t["id"] for t in json.load(sys.stdin)
      if t.get("type")=="iframe" and "vscode-webview" in (t.get("url") or "")))')

# ... open via 6a or 6b ...

# AFTER: find the new iframe id, poll it until markers paint:
for attempt in $(seq 1 8); do
  sleep 3
  for id in $(curl -s http://127.0.0.1:9222/json/list | python3 -c '
import sys, json
print("\n".join(t["id"] for t in json.load(sys.stdin)
      if t.get("type")=="iframe" and "vscode-webview" in (t.get("url") or "")))'); do
    case " $BEFORE " in *" $id "*) continue;; esac          # skip pre-existing iframes
    out=$(node /tmp/eval_in_inner_frame.mjs "ws://127.0.0.1:9222/devtools/page/$id" @/tmp/pfg_markers.js 2>/dev/null)
    # Require a marker actually painted (pfgkAlert>=1): the conversations-panel
    # iframe also answers but with pfgkAlert:0 and would be a false target.
    val=$(printf '%s' "$out" | python3 -c "import sys,json
try: print(json.load(sys.stdin)['result']['value'])
except Exception: pass" 2>/dev/null)
    printf '%s' "$val" | python3 -c "import sys,json
try: sys.exit(0 if json.load(sys.stdin).get('pfgkAlert',0)>=1 else 1)
except Exception: sys.exit(1)" && { echo "FRESH IFRAME $id: $val"; break 2; }
  done
done
```

**Capture-only alternative (no re-render): run the walker and read its H array
directly.** When you only need to confirm what `Wz4` *produces* (not what the
panel shows), skip the render entirely. From the inner active-frame, call the
conn directly (this is the canonical invocation; see "`conn.sendRequest` is the
canonical chain-walker invocation"):

```sh
node /tmp/eval_in_inner_frame.mjs "$WS_CHAT" "(async()=>{
  const root=document.querySelector('#root');
  const fk=Object.keys(root).find(k=>k.startsWith('__reactContainer\$'));
  const mgr=root[fk].child.child.child.memoizedProps.sessions;
  const conn=await mgr.getConnection('$SID');
  const resp=await conn.sendRequest({type:'get_session_request', sessionId:'$SID'});
  const H=resp?.messages||[];
  const ghosts=H.filter(m=>String(m&&m.uuid).startsWith('pfgk-')).map(m=>String(m.uuid).replace(/^(pfgk-[a-z]+)-.*/,'$1'));
  return JSON.stringify({n:H.length, ghosts});
})()"
```

The throwaway-`probe`-channel RPC (`window.__vscode_post_message__('onmessage',
{message:{type:'request', channelId:'probe', requestId:'r'+Math.random(),
request:{type:'get_session_request', sessionId:'$SID'}}})` from the OUTER
iframe) ALSO runs the walker (useful to trigger a side-effect BP, see Recipe 1),
but its response goes nowhere visible. Neither this nor `conn.sendRequest`
re-renders the panel; for that you MUST use 6a or 6b.

### Step 7: verify the rendered DOM

After the session renders, probe the inner active-frame. Assert on this rendered
DOM (or the wire `H` from a fresh walk), never by re-reading the fixture `.jsonl`:
opening a session makes base Claude Code append an `ai-title` to that file, so it
is not byte-stable across an open (see the Step 8 fixture pitfalls).

```sh
WS_CHAT="ws://127.0.0.1:9222/devtools/page/$IFRAME"
node /tmp/eval_in_inner_frame.mjs "$WS_CHAT" 'JSON.stringify({
  pfgkAlert:  document.querySelectorAll(".pfgkAlert").length,
  bookend:    document.querySelectorAll("[data-pfgk-role=\"bookend\"]").length,
  seam:       document.querySelectorAll("[data-pfgk-role=\"seam\"]").length,
  bridge:     document.querySelectorAll("[data-pfgk-role=\"bridge\"]").length,
  broken:     document.querySelectorAll("[data-pfgk-role=\"broken\"]").length,
  msgs:       document.querySelectorAll("[class*=message_]").length,
  bodyHasK:   document.body.textContent.includes("PATCH K"),
})'
```

Expected for a session with Patch K markers: `pfgkAlert >= 1`, `bookend = 1`,
`seam >= 1`. If `bodyHasK` is true but `pfgkAlert = 0`, the React wrap node
isn't rendering. See the "dead K wrap from non-pristine .bak synthesis"
case study.

To assert on a marker's actual TEXT (e.g. confirming an edit that changed a
marker's prose took effect in the rendered panel), read `markerText`. This is the
canonical "is the new code live in the render" check used during marker
iteration. Write the probe to a file so the poll loop in Step 6
(`@/tmp/pfg_markers.js`) and ad-hoc probes share one definition:

```sh
cat > /tmp/pfg_markers.js <<'EOF'
JSON.stringify({
  pfgkAlert: document.querySelectorAll(".pfgkAlert").length,
  bookend:   document.querySelectorAll('[data-pfgk-role="bookend"]').length,
  seam:      document.querySelectorAll('[data-pfgk-role="seam"]').length,
  seamClean: document.querySelectorAll('[data-pfgk-role="seamClean"]').length,
  bridge:    document.querySelectorAll('[data-pfgk-role="bridge"]').length,
  broken:    document.querySelectorAll('[data-pfgk-role="broken"]').length,
  markerText:(() => { const c = document.querySelector(".pfgkAlert"); return c ? c.textContent.replace(/\s+/g, " ").trim().slice(0, 400) : null; })()
})
EOF
node /tmp/eval_in_inner_frame.mjs "$WS_CHAT" @/tmp/pfg_markers.js
```

The load-bearing assertion is change-specific and belongs with YOUR change, not in
this playbook: a FRESH walk (Step 6a/6b) must render the edit you made, so assert
that `markerText` no longer contains the string you removed and does contain the
one you added. If a removed string still shows (or an added one does not), you are
looking at a STALE rehydrated webview (you skipped Step 6) or the exthost is still
running old code (see the `Debugger.getScriptSource` gotcha), NOT a failed edit.
Re-walk fresh, then re-probe.

Field notes for the probe: `pfgkAlert` counts the wrap divs (structural "did it
render as a marker"); the per-role counts (`bookend`/`seam`/`seamClean`/`bridge`/
`broken`) assert the exact marker SET the walk produced; `markerText` is the first
marker card's `textContent` (whitespace-collapsed, capped at 400 chars) for
asserting on the marker PROSE. `textContent` returns the FULL body even when the
card visually collapses it (the wrap injects `.pfgkAlert
.content_xGDvVg.collapsed_xGDvVg{max-height:none}` and hides the truncation
gradient), so a removed phrase cannot hide behind a collapse. The injected
`<style>` text and the header/emoji chrome are included in `textContent`, so
assert on a specific substring, not on exact equality.

To verify a specific CSS property (e.g. marker background color):

```sh
node /tmp/eval_in_inner_frame.mjs "$WS_CHAT" '(function(){
  const el = document.querySelector("[data-pfgk-role=\"broken\"]");
  if (!el) return "not found";
  const hdr = el.querySelector("[class*=header]");
  return JSON.stringify({
    bg:          getComputedStyle(el).backgroundColor,
    headerColor: hdr ? getComputedStyle(hdr).color : "no header",
  });
})()'
```

To eyeball the marker (screenshot the whole workbench page):

```sh
cat > /tmp/shot.mjs <<'JS'
const ws=new WebSocket(process.argv[2]);          // ws://127.0.0.1:9222/devtools/page/<workbench-page-id>
let id=1;const p=new Map();
ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id&&p.has(m.id)){p.get(m.id)(m.result);p.delete(m.id);}});
const call=(method,params={})=>new Promise(r=>{const i=id++;p.set(i,r);ws.send(JSON.stringify({id:i,method,params}));});
await new Promise(r=>ws.addEventListener('open',r));
await call('Page.enable');
const sc=await call('Page.captureScreenshot',{format:'png'});
const fs=await import('node:fs');fs.writeFileSync('/tmp/wb_now.png',Buffer.from(sc.data,'base64'));
console.log('saved /tmp/wb_now.png');ws.close();
JS
node /tmp/shot.mjs "ws://127.0.0.1:9222/devtools/page/$WB_PAGE"   # then Read /tmp/wb_now.png
```

To pull the marker's own SVG out of the live DOM (crisp, panel-independent
render): iterate the chat iframes, find the active-frame that has
`[data-pfgk-role]` nodes, grab each `svg.outerHTML`, dedupe by `role|caption`,
and write them to an HTML file you can headless-screenshot.

```sh
cat > /tmp/extract_live_svgs.mjs <<'EOF'
import http from 'node:http';
import fs from 'node:fs';
const extract=`(function(){var els=document.querySelectorAll('[data-pfgk-role]');var seen={},out=[];for(var i=0;i<els.length;i++){var e=els[i],role=e.getAttribute('data-pfgk-role'),svg=e.querySelector('svg'),t=svg?svg.outerHTML:'';var cap=(t.match(/(in-file reattach|in-file link|cross-file link)/)||[''])[0];var key=role+'|'+cap;if(!seen[key]){seen[key]=1;out.push({role:role,cap:cap,svg:t});}}return out;})()`;
const getJSON=p=>new Promise((res,rej)=>{http.get('http://127.0.0.1:9222'+p,r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>res(JSON.parse(d)));}).on('error',rej);});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function withWS(wsUrl,fn){return new Promise((resolve,reject)=>{const ws=new WebSocket(wsUrl);let nid=1;const pend=new Map();const ctx=[];ws.addEventListener('message',ev=>{const m=JSON.parse(ev.data);if(m.id&&pend.has(m.id)){const{res,rej}=pend.get(m.id);pend.delete(m.id);m.error?rej(m.error):res(m.result);}else if(m.method==='Runtime.executionContextCreated')ctx.push(m.params.context);});const call=(method,params={})=>{const id=nid++;return new Promise((res,rej)=>{pend.set(id,{res,rej});ws.send(JSON.stringify({id,method,params}));});};ws.addEventListener('open',async()=>{try{const r=await fn(call,ctx);ws.close();resolve(r);}catch(e){ws.close();reject(e);}});ws.addEventListener('error',e=>reject(e));setTimeout(()=>{try{ws.close();}catch(_){}reject(new Error('ws timeout'));},8000);});}
// Loop ALL index iframes; merge + dedupe by role|caption. (After a tab switch the
// stale iframe may still be listed; only the active one returns marker nodes. For
// multiple open chats this collects markers across all of them.)
const ifs=(await getJSON('/json/list')).filter(t=>t.type==='iframe'&&(t.url||'').includes('index'));
let arr=[];const _seen={};
for(const wv of ifs){
  const r=await withWS(wv.webSocketDebuggerUrl,async(call,ctx)=>{
    await call('Page.enable');await call('Runtime.enable');await sleep(700);
    const tree=await call('Page.getFrameTree');const frames=[];(function w(n){if(n.frame)frames.push(n.frame);(n.childFrames||[]).forEach(w);})(tree.frameTree);
    const inner=frames.find(f=>f.name==='active-frame');if(!inner)return [];
    const c=ctx.find(x=>x.auxData&&x.auxData.frameId===inner.id&&!x.name&&x.origin);if(!c)return [];
    const r=await call('Runtime.evaluate',{expression:extract,returnByValue:true,contextId:c.id});
    return r.result.value||[];
  }).catch(()=>[]);
  for(const it of (r||[])){const k=it.role+'|'+(it.cap||'');if(!_seen[k]){_seen[k]=1;arr.push(it);}}
}
const bgs={bookend:'#142a35',broken:'#3a1818',seam:'#3a2c14',seamClean:'#181d28',bridge:'#3a2418'};
let html='<html><body style="margin:0;background:#0a0a0c;font-family:monospace">';
for(const it of arr){
  let bg=it.cap==='in-file link'?bgs.seamClean:it.cap==='cross-file link'?bgs.bridge:it.cap==='in-file reattach'?bgs.seam:(bgs[it.role]||'#222');
  let svg=it.svg.replace('width:100%;height:124px','width:860px;height:auto');
  html+='<div style="color:#999;padding:11px 24px 3px;font-size:13px">live: '+it.role+' / '+(it.cap||'(no caption)')+'</div><div style="background:'+bg+';margin:0 24px 14px;padding:16px;border-radius:8px">'+svg+'</div>';
}
html+='</body></html>';
fs.writeFileSync('/tmp/live_extracted.html',html);
console.log('extracted '+arr.length+' unique live SVGs: '+arr.map(a=>a.role+'/'+(a.cap||'-')).join(', '));
EOF
node /tmp/extract_live_svgs.mjs
# Render the extracted SVGs to a PNG you can Read:
google-chrome --headless=new --disable-gpu --force-device-scale-factor=2 \
  --screenshot=/tmp/live_all.png --window-size=920,900 file:///tmp/live_extracted.html
```

CROP LAW for the headless screenshot: a card at width `W` is about `W*0.24` px
tall, so the `--window-size` height must exceed `W*0.24 + 90` or the bottom
sub-labels are cropped. For a single role bump `--window-size` to e.g.
`920,360`; for all five markers stacked use `920,900` or taller.

### Step 8: synthetic demo sessions (when no real session triggers your marker)

Some markers (seam, broken, ambiguous) only fire on rare chain topologies
(phantom-lpu compaction, dangling root, multi-sibling backfill) that may not be
present in any of your real sessions. Rather than hunt, CRAFT a minimal `.jsonl`
that triggers exactly the case you want. The "PFGK DEMO seam" / "PFGK DEMO
broken" tabs were made this way.

Two hard requirements learned empirically:

1. **The file must live in the project dir of an OPEN workspace folder.** The
   conversations panel lists sessions via the extension's SessionStore, scoped to
   the folders in the currently open workspace; it does NOT live-scan arbitrary
   dirs, and it is NOT enough for the dir to be anywhere under `~/.claude/projects`.
   A `.jsonl` dropped into a brand-new project dir (`-tmp-foo/`, or a fresh
   `-home-juraj-foo/` for a folder you do not have open) will NOT appear, even
   after reload. Empirically: `/tmp`-rooted and a fresh `/home/juraj/pfgk-demo`
   both showed zero rows; only placing the file under the open workspace's project
   (e.g. with `~/CDVIEWER/PNGS` open, the dir
   `~/.claude/projects/-home-juraj-CDVIEWER-PNGS/`) surfaced it. The project-dir
   name is the workspace path with `/` replaced by `-`. Use a **unique** dangling
   / phantom uuid (`deadbeef-0000-4000-8000-0000000000NN`) so none of the real
   siblings in that dir accidentally resolve it and defuse the trigger.

2. **Reload the window to re-scan after writing the file.** The panel caches its
   list; a freshly written `.jsonl` only surfaces after `Developer: Reload
   Window` (Step 2). Then open it via the panel (Step 6a) for a fresh `Wz4` walk.

Generator: the maintained script is **`util/gen_demo.py`** (run it from inside the
open workspace folder so it targets that project dir, or set `PFG_DEMO_CWD` /
`PFG_DEMO_PROJ`). It writes one seam-triggering and one broken-triggering session;
records use the real on-disk JSONL shape, `ai-title` gives the panel a searchable
title, `last-prompt` marks the leaf. The same logic, condensed, inline:

```python
# Minimal inline copy; the maintained version is util/gen_demo.py. Seam + broken.
import json, uuid, os
def u(): return str(uuid.uuid4())
TS="2026-06-04T11:30:00.000Z"; VER="2.1.159"
CWD=os.environ.get("PFG_DEMO_CWD") or os.getcwd()                 # run from the OPEN workspace folder
PROJ_DIR=os.environ.get("PFG_DEMO_PROJ") or CWD.replace("/","-")  # Claude Code project-dir encoding
def user_rec(uid,parent,sid,text):
    return {"parentUuid":parent,"isSidechain":False,"type":"user","message":{"role":"user","content":text},
            "isMeta":False,"uuid":uid,"timestamp":TS,"cwd":CWD,"sessionId":sid,"version":VER,"gitBranch":""}
def asst_rec(uid,parent,sid,text):
    return {"parentUuid":parent,"isSidechain":False,"type":"assistant","uuid":uid,"timestamp":TS,"cwd":CWD,
            "sessionId":sid,"version":VER,"gitBranch":"","message":{"model":"claude-opus-4-8","id":"msg_"+uid.replace('-','')[:24],
            "type":"message","role":"assistant","content":[{"type":"text","text":text}],"stop_reason":"end_turn"}}
def title_rec(sid,t): return {"type":"ai-title","aiTitle":t,"sessionId":sid}
def leaf_rec(sid,leaf): return {"type":"last-prompt","leafUuid":leaf,"sessionId":sid}
def compact_rec(uid,lpu,sid,preserved):
    return {"parentUuid":None,"logicalParentUuid":lpu,"isSidechain":False,"type":"system","subtype":"compact_boundary",
            "content":"Conversation compacted","isMeta":False,"timestamp":TS,"uuid":uid,"level":"info",
            "compactMetadata":{"trigger":"auto","preTokens":900000,"preservedMessages":{"anchorUuid":preserved[0],"uuids":preserved}},"sessionId":sid}
def write(sid,recs):
    d=os.path.expanduser("~/.claude/projects/"+PROJ_DIR); os.makedirs(d,exist_ok=True)
    p=os.path.join(d,sid+".jsonl")
    with open(p,"w") as f:
        for r in recs: f.write(json.dumps(r)+"\n")
    return p

# BROKEN: root parentUuid dangles at a unique missing uuid (isolated => unresolvable => INCOMPLETE TRANSCRIPT)
sb=u(); DANGLING="deadbeef-0000-4000-8000-000000000001"; b1,b2,b3,b4=u(),u(),u(),u()
broken=[title_rec(sb,"PFGK DEMO broken: incomplete-transcript marker (synthetic)"),
  user_rec(b1,DANGLING,sb,"PFGK DEMO (broken). Root points at a missing upstream message."),
  asst_rec(b2,b1,sb,"Acknowledged."), user_rec(b3,b2,sb,"Continue."), asst_rec(b4,b3,sb,"Continuing."),
  leaf_rec(sb,b4)]
# SEAM: compact_boundary whose logicalParentUuid is a unique missing phantom (Loop A reattaches in-file)
ss=u(); PHANTOM="deadbeef-0000-4000-8000-000000000002"; s1,s2,scb,s3,s4=u(),u(),u(),u(),u()
seam=[title_rec(ss,"PFGK DEMO seam: in-file phantom reattach marker (synthetic)"),
  user_rec(s1,None,ss,"PFGK DEMO (seam). Compaction predecessor was never persisted."),
  asst_rec(s2,s1,ss,"Acknowledged."), compact_rec(scb,PHANTOM,ss,[s1,s2]),
  user_rec(s3,scb,ss,"Continue after the compaction."), asst_rec(s4,s3,ss,"Continuing."),
  leaf_rec(ss,s4)]
print("BROKEN:",sb,"->",write(sb,broken)); print("SEAM:  ",ss,"->",write(ss,seam))
```

```sh
python3 util/gen_demo.py | tee /tmp/demo_sids.txt   # run from the workspace; note the two sessionIds
# Reload (Step 2), then open via the panel (Step 6a, search "PFGK DEMO"), verify (Step 7).
```

Pitfalls when crafting or reusing a fixture:

- The seam fixture plants `dg:"seam"` only while its phantom `logicalParentUuid`
  (a uuid no record in the dir defines) stays unresolved. If a sibling defines it
  (a prior run, a clone), the crossing renders `dg:"seamClean"` or `dg:"bridge"`
  instead, a DIFFERENT marker. Clear leftover demo files before generating.
- A same-titled inert stub left in the dir makes `click_convo` (Step 6a) open the
  WRONG row. Move it aside, or give your fixture a unique title, so the search
  matches exactly one session.
- A fixture with zero message records (an `ai-title`+`mode` stub and nothing else)
  plants no markers: the walker has no boundary or chain to fire on. It must
  contain the actual messages, not just a title.
- Opening a session is not idempotent on disk. Base Claude Code (the `claude`
  subprocess, via `generate_session_title` with `persist`, NOT our patches)
  appends a compact-JSON `ai-title` record to the session's own `.jsonl` on open,
  so a 7-line seam fixture becomes 8 lines after one open and re-opening keeps
  adding duplicate `ai-title` lines (harmless to the render, the file just grows).
  Verify markers from the rendered DOM (Step 7) or the wire `H` of a fresh walk,
  NEVER by re-reading or diffing the fixture file after opening it. The `ai-title`
  is also what the panel lists and searches, so the fixture needs one (gen_demo
  writes it); real seam sessions often have none and cannot be opened by panel
  title-search.

Cleanup when done (delete by the exact sessionIds, leaving the real siblings
untouched), then reload once to drop the stale panel rows:

```sh
DIR="$HOME/.claude/projects/$(pwd | tr / -)"   # run from the same workspace folder
rm -f "$DIR/<broken-sid>.jsonl" "$DIR/<seam-sid>.jsonl"
grep -l "PFGK DEMO" "$DIR"/*.jsonl 2>/dev/null | wc -l   # expect 0
```

(For inducing edge cases on an EXISTING real session instead of crafting one
from scratch, see the "Patch K verification recipes" gotcha:
clone-a-sibling for ambiguity, rename-the-source-away for reconstruction
failure.)

### Quick-reference: one-liner port checks

```sh
# Is Antigravity's renderer reachable?
curl -s http://127.0.0.1:9222/json/version | python3 -c 'import sys,json;print(json.load(sys.stdin).get("Browser"))'

# Which exthost ports are live?
for p in $(ss -tlnp 2>/dev/null | grep -oE '127\.0\.0\.1:[0-9]+' | sort -u); do
  R=$(curl -s --max-time 1 "http://$p/json" 2>/dev/null | head -c 60)
  case "$R" in *electron/js2c/utility_init*) echo "exthost @ ${p##*:}";; esac
done

# WS URL for the first exthost (usually 9229):
curl -s http://127.0.0.1:9229/json | python3 -c \
  'import sys,json; print(json.load(sys.stdin)[0]["webSocketDebuggerUrl"])'
```

---

## Per-session subprocess

Each open chat panel spawns a dedicated `claude --resume <sid>`
subprocess as a child of the panel's window's exthost.

```sh
ps -eo pid,ppid,cmd | grep 'claude --resume'
```

- ppid = exthost pid; lookup the exthost's inspector port via
  `cat /proc/<ppid>/cmdline | grep -oE 'inspect=127\.0\.0\.1:[0-9]+'`.
- stdio between exthost ↔ subprocess uses JSON-stream protocol
  (`--output-format stream-json --input-format stream-json`).
- The subprocess does the LIVE chat (API streaming, tool use). Reading
  the JSONL file for *display* in the panel is exthost-side via `Wz4`;
  the subprocess isn't involved in sidebar-list or fork-rewind UI
  rendering.
- Killing the subprocess: behavior unclear. Don't.

This architecture is why live message updates appear in
`s.messages.value` via stream events from the subprocess (NOT via Wz4);
whereas opening or reopening a panel triggers a Wz4 read of the JSONL on
disk. That's the asymmetry behind "active session vs cold-load":
patches affecting `Wz4` (H/J/K) affect the cold-load path, not live
updates.

---

## Gotchas catalog

### `setScriptSource` updates source TEXT but does NOT swap running code

The single biggest trap. `Debugger.setScriptSource` returns
`{status: "Ok"}`, and `Debugger.getScriptSource` shows the patched
source. But the EXECUTING code is unchanged.

Verified empirically across multiple modes:

- **CJS export**: patched `il4` (= `extension.exports.openTabs`) to set
  `globalThis.__patched_il4 = Date.now()`. Same-connection
  `getScriptSource` showed the marker present. Called
  `extension.exports.openTabs()` via Runtime.evaluate: `tabsCount`
  returned correctly (function ran), but the marker stayed `undefined`.
- **Module-internal name resolution**: patched `Ez4` (called by name
  from `dl` inside the same IIFE) to set `globalThis.__ez4_marker++`.
  Triggered Wz4 via webview RPC (we know this calls Ez4, separately
  verified by BP). Marker stayed at 0.
- **`allowTopFrameEditing: true` doesn't help.** Re-tested both modes
  with the flag set; same result. The flag governs editing the
  CURRENTLY-paused top frame, not module-internal callers.
- **Class methods**: patching `fromClient` on a class created at
  startup also failed (instances keep the old prototype binding).

Why: V8 inspector's setScriptSource live-edit updates the source text
view and may even create new function objects internally, but
*pre-existing references to the old function objects continue pointing
at the old code*. In CommonJS modules with IIFE wrappers and JIT-cached
call sites, virtually every reference is "pre-existing". So:

- `module.exports.openTabs = il4` was bound at module load; the
  replacement function with the patched body is a different object.
- `dl()` calling `Ez4(V)` inside the IIFE has its name lookup either
  cached by V8's JIT or resolved via a closure-scope binding that
  isn't refreshed by source-text updates.
- Class method `fromClient` on a class created at startup: instances
  keep using the old prototype.

**For introspection, use BP-with-side-effect-condition (Recipe 1)
instead.** For actual code changes, edit `extension.js` on disk and
Reload Window.

**Always verify `status: "Ok"` with a side-effect probe.** Set a global
from inside the patched function body, trigger that function naturally,
check if the global got set. If `undefined`, the patch didn't actually
take effect at runtime.

### `scriptParsed` doesn't always replay on a fresh CDP connection

Symptom: `Debugger.enable` returns immediately, `setBreakpointByUrl`
returns `locations: []`, scripts seem invisible.

Workaround: `Debugger.enable` + drain `Debugger.scriptParsed` events
patiently for **5–10 seconds** before assuming the scripts aren't
loaded. CommonJS modules are parsed by V8 once at parse time; a fresh
inspector connection replays them but the replay isn't instant.

If still empty: `Profiler.enable` + `Profiler.startPreciseCoverage` +
trigger any extension code path → coverage report includes scriptIds
for everything that ran.

### `scriptId` is per CDP connection

A `scriptId` you got from a prior CDP connection is invalid in a new
connection, even though the underlying script is the same. Re-fetch
every time. Caching `sid="368"` and reusing it after a new connection
silently sets BPs against a non-existent script and they never fire.

### Pure-pause BPs freeze the exthost

A BP without a condition pauses the exthost on hit. If the exthost is
yours (mine = target) you freeze yourself. If it's a target exthost,
you freeze user activity in that window: tabs, sidebar, everything.

Use side-effect-condition BPs that return `false` (Recipe 1) for
data capture. Reserve pure-pause BPs for cases where you're attaching
to a real debugger UI and ready to step.

### Logpoints with `console.log` conditions silently fail

Extension code may not have global `console` in scope. A condition like
`(console.log("X"), false)` then errors silently and the BP "fires"
without producing any output. Use `globalThis` mutation instead, or use
a logger the bundle exposes (e.g. `Yc.log`).

### `Page.createIsolatedWorld` blinds you to page state

Creates a sandbox with NO access to the page's globals or React state.
If you eval and see no `__reactContainer$` keys, no `acquireVsCodeApi`,
no React app: you're in an isolated world. Pick the **main-world**
context: origin matches the page, name is empty (the inner-frame helper
script enforces this).

### `await import('fs')` fails in the exthost

Returns `ERR_VM_DYNAMIC_IMPORT_CALLBACK_MISSING`. V8's dynamic-import
callback isn't wired into the inspector eval context. Use the
synchronous `require('fs')` form via `includeCommandLineAPI: true`.

### vscode-webview is double-nested

`/json/list` shows the OUTER iframe. The actual React app is in a
nested `<iframe id="active-frame">`. The inner frame's URL also ends
in `index.html` (not `fake.html` as the outer's `src` suggests);
trust the frame `name`, not the URL.

### Top-level `return` rejected in `Runtime.evaluate`

```
SyntaxError: Illegal return statement
```

Wrap the script in an IIFE: `(function(){ ...; return ...; })()`.

### Preact signals serialize as nonsense

`signal.value` and `signal.peek()` return the unwrapped value. The
signal object itself (a Preact internal) serializes to
`{$$typeof, type, props, ref, ...}`: looks like a React element, isn't.
Always deref before serializing.

### `extension.exports` is sparse

Only `activate`, `deactivate`, `openTabs`. The session manager and other
internal singletons are NOT reachable via `require.cache`. Use the
BP-stash trick.

### Inspector ports change on exthost restart

`Developer: Reload Window` (or `Restart Extension Host`) restarts
the per-window exthost; ephemeral ports reshuffle. `9229` itself
can change hands: if the original first-spawned window closes and
another spawns, the new one captures `9229`. Re-identify `mine` and
`target` after any window-state change.

### Per-window exthosts: enumerate ALL of them, not just `9229`

If `ps -ef | grep inspect-extensions` returns ONE port (`9229`),
that does NOT mean there's only one exthost. Each window gets its
OWN exthost, but **only the first one to spawn captures the sticky
`--inspect-extensions=9229` flag**. Subsequent exthosts get
`--inspect=127.0.0.1:<ephemeral>` (e.g. `11277`, `45647`); same
process type (`utility-sub-type=node.mojom.NodeService`), different
flag form.

Failure mode this gotcha prevents: assuming "shared exthost across
windows" because `9229` is the only port your initial scan finds.
The conclusion is wrong; you missed the ephemeral inspect ports on
the other windows' exthosts.

Canonical enumeration:

```sh
# All Antigravity exthost processes (one per window)
ps -ef --no-headers | awk '$3==<antigravity_main_pid> && /node.mojom.NodeService/'

# Their inspect ports; note the TWO forms
for pid in $(ps -ef --no-headers | awk '/node.mojom.NodeService/ {print $2}'); do
  cat /proc/$pid/cmdline 2>/dev/null | tr '\0' ' ' | \
    grep -oE -- '--inspect[a-zA-Z=:-]*[0-9.:]+' | head -1
done
```

Sticky `9229` is special only in being the first; behaviorally,
ephemeral-port exthosts are identical (full CDP, full `Debugger`,
full `Runtime` etc).

### `Restart Extension Host` brings the next exthost back to `9229`

Empirically: when the per-window exthost is restarted via the
`Developer: Restart Extension Host` palette command, the killed
exthost frees its ephemeral port and the respawn binds to the
sticky `9229` (because the original sticky-holder was killed too,
or the slot is otherwise free). So a window that previously used
`11277` may now be on `9229`. Re-discover ports after every restart.

### Iframe IDs change on every Reload Window

Each `Developer: Reload Window` rebuilds the renderer's iframe
targets from scratch. The CDP target IDs you cached from
`/json/list` (e.g., `WS=ws://127.0.0.1:9222/devtools/page/<old-id>`)
are dead the moment the window reloads; connecting to them just
hangs.

After every reload, **re-discover** the chat-panel iframe by walking
`/json/list` again with whatever filter matches your panel:

```sh
curl -fsS http://127.0.0.1:9222/json/list | python3 -c "
import sys, json
for t in json.load(sys.stdin):
    if t.get('type')=='iframe' and t.get('parentId')=='<workbench-page-id>' and '1c9b69d1' in t.get('url',''):
        print(t['id']); break
"
```

The same applies to inner active-frame discovery: its frameId
inside the parent iframe also changes. The discovery walker
inside `eval_in_inner_frame.mjs` re-fetches each call so it's
naturally robust; bash scripts that cache the outer iframe ID
between reloads are not.

Wait ~14–16 seconds after triggering Reload Window before
re-fetching `/json/list`. The renderer takes a moment to bring
the new iframes up; an immediate fetch may catch a transient state
with no chat panel iframe yet.

### `Debugger.getScriptSource` shows the in-memory script, NOT disk

`fs.readFileSync('.../extension.js')` from an exthost CDP eval
returns the file's CURRENT on-disk content. That's useful for
checking what's *available*, but it does NOT tell you what code is
actually *running* in the exthost: Node modules are loaded once
at process start (or `require()`-time) and cached.

To check what code is *actually loaded* (the in-memory module
source), use the Debugger domain:

```sh
node - <<'JS'
const ws = new WebSocket('ws://127.0.0.1:<exthost-port>/<id>');
let nextId = 1; const pending = new Map(); const scripts = new Map();
ws.addEventListener('message', ev => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { const {resolve} = pending.get(msg.id); pending.delete(msg.id); resolve(msg.result); }
  else if (msg.method === 'Debugger.scriptParsed' && msg.params.url?.includes('extension.js')) scripts.set(msg.params.scriptId, msg.params.url);
});
function call(method, params={}) { const id = nextId++; return new Promise(r => { pending.set(id, {resolve: r}); ws.send(JSON.stringify({id, method, params})); }); }
await new Promise(r => ws.addEventListener('open', r));
await call('Debugger.enable');
await new Promise(r => setTimeout(r, 4000));    // drain scriptParsed events
const ext = [...scripts.entries()].find(([id, url]) => url.endsWith('extension.js'));
const src = (await call('Debugger.getScriptSource', {scriptId: ext[0]})).scriptSource;
console.log({length: src.length, has_my_marker: src.includes('<unique substring of recent edit>')});
ws.close();
JS
```

Use this when you need to confirm "did my disk-edit actually get
loaded by THIS exthost?", particularly relevant when (a) you have
multiple per-window exthosts and aren't sure which one serves the
panel you're looking at, or (b) you ran disk-edit + Reload Window
and want to verify the fresh exthost actually picked up the new
code (vs. some unexpected caching).

Concrete failure mode this catches: `readFileSync` can show the new
signature and markers on disk while the exthost serving the chat panel
still runs the old code from memory (cached from before the disk edit),
so you wrongly conclude the new K is live when it isn't. `Debugger.getScriptSource` immediately
disambiguates.

### `location.reload()` on a webview iframe leaves it in `chrome-error://chromewebdata/`

Using `Runtime.evaluate` to call `location.reload()` inside a
vscode-webview's inner active-frame does NOT cleanly reload the
webview; the iframe ends up navigating to
`chrome-error://chromewebdata/` with an empty body. The webview
content (React app, scripts, body) is GONE and doesn't recover until
the window is reloaded. This is because vscode-webview's outer CSP
wrapper expects to re-issue the load via its own RPC channel, not
via in-frame navigation.

Don't use `location.reload()` on webview frames. To reload webview
content after editing `webview/index.js` on disk, use mechanism 5
from the refresh playbook: `Input.dispatchKeyEvent` for
`workbench.action.reloadWindow` (Ctrl+R or via command palette →
"Developer: Reload Window"). Mechanism 5 is the verified path; the
case study below uses it.

### `Page.reload` is rejected on iframe targets

`Page.reload` on a vscode-webview iframe target returns:

```
{ code: -32000, message: 'Command can only be executed on top-level targets' }
```

CDP only allows `Page.reload` on top-level pages. The webview iframe
inherits its lifecycle from its parent workbench page; reload that
instead (or use the refresh playbook's mechanism 3/5).

### Focus-lost-to-broken-iframe blocks command-palette typing

Symptom: `Input.dispatchKeyEvent` for Ctrl+Shift+P opens the palette
(verified by `.quick-input-widget` appearing in DOM), but subsequent
`type:'char'` events don't reach the palette input. Reading
`document.querySelector('.quick-input-and-message input').value`
shows the palette input is unchanged or has stale text from a prior
session.

Cause: an iframe in the workbench (often a broken
`chrome-error://chromewebdata/` from an earlier botched
`location.reload()`) has captured focus. `document.activeElement`
returns `IFRAME` instead of the palette `INPUT`, so the char events
go to the iframe (where they're discarded). JS-side `i.focus()` on
the palette input fails to take focus back; the iframe re-grabs it
synchronously after the JS focus call returns.

Recovery requires fixing the iframe state: close+reopen the offending
panel via UI, or `Developer: Reload Window`. In a healthy workbench
(no chrome-error iframes), command palette typing via
`Input.dispatchKeyEvent` works fine, verified end-to-end.

Always verify the `document.activeElement` after opening the palette:

```js
JSON.stringify({focused: document.activeElement?.tagName + '/' + (document.activeElement?.className||'').slice(0,40)})
```

If it's anything other than `INPUT/quick-input-and-message ...`, the
keystrokes won't reach the palette. Stop and recover the iframe state
before attempting to type.

### Byte-stability check is necessary but not sufficient for splice correctness

`util/build-prebuilt.py` validates the synthesized splice by re-applying
it to a fresh copy of `.bak` and confirming the result is byte-identical
to the live patched file. **This proves the splice is deterministic; it
does not prove the splice is correct.**

If `.bak` itself isn't pristine (e.g., the patch was iteratively
developed in place and `.bak` was captured *between* iterations), the
splice synthesis only captures the diff between *the latest .bak state*
and *the current live state*, missing transformations that were
introduced by an earlier patch iteration and are now equally present
in both `.bak` and live.

Concrete instance: Patch K's webview wrap was developed iteratively
across v1.2 → v1.3 → v1.4 in the 2.1.126 install. The `let _ws=`
prefix on the `createElement(GR0,...)` call was introduced in an
earlier K iteration (v1.2 or v1.3). By the time the v1.4 prebuilt was
synthesized, `.bak` already had `let _ws=` (because no one re-baked
from pristine after the iteration). The captured splice therefore
covered only the v1.3→v1.4 wrap-internal change, NOT the pristine
`return createElement(...)` → `let _ws=createElement(...)`
transformation that was needed for the wrap to do anything at all.

Translating that splice to a fresh bundle (e.g. 2.1.132, where `.bak`
IS pristine) applied cleanly via grep, byte-stability passed, but the
result was dead code: the K wrap branch lived after a `return
createElement(...)` statement and never executed.

Mitigations:
- **Treat `.bak` as the pristine pre-patch baseline, always.** Never
  overwrite it once it exists, for end-users and maintainers alike.
  This invariant makes `build-prebuilt.py`'s `live - .bak` diff
  always capture the full pristine→patched transformation, not an
  incremental hop.
- **Per-patch checkpoints are named after the settled patch they
  contain** (`.patchX.bak` = "settled state of patch X"), not after
  the next patch in line. Self-documenting; doesn't require knowing
  the patch ordering to interpret.
- If `.bak` ever drifted off pristine, recover by reinstalling the
  extension from scratch and re-baking from clean before any further
  patch work. See [`MAINTAINER.md`](../MAINTAINER.md) for the
  detailed invariant + recovery procedure.
- The user-visible verification (DOM probe for `.pfgkAlert` after
  Reload Window) catches this regardless; the byte-stability check
  alone does not.

### `conn.sendRequest` is the canonical chain-walker invocation

You don't always need a BP-with-side-effect-condition + RPC dispatch
dance to capture a chain walker's H array. From the inner active-frame:

```js
let mgr = root[fk].child.child.child.memoizedProps.sessions;
let conn = await mgr.getConnection(sid);
let resp = await conn.sendRequest({type: "get_session_request", sessionId: sid});
let H = resp?.messages || [];
```

This goes through the normal webview→exthost RPC path. The exthost
runs `getSession` → `Br` → `Bz4` → `zi` and returns H over the wire.
Faster setup than BP + dispatch when you just need walker output for
inspection. Use the BP recipe when you need locals INSIDE the walker
(e.g., V before K modifies it, or intermediate state).

`conn.sendRequest` works for ANY sessionId, including hidden sessions
that aren't in `mgr.sessions.value`. The conn is sid-bound but the
RPC handler reads any sid you pass.

### Hidden sessions are filtered out of `mgr.sessions.value`

If a user has hidden a session via the trash icon in the session
panel, that session won't appear in `mgr.sessions.value` (the
sidebar list). But its JSONL file still exists on disk and the
chain walker still loads it on demand.

The hide-state lives in vscode's globalState, NOT workspaceState.
Path varies by IDE; pick whichever you're patching:

| IDE         | globalState path                                              |
|-------------|---------------------------------------------------------------|
| VS Code     | `~/.config/Code/User/globalStorage/state.vscdb`               |
| Antigravity | `~/.config/Antigravity/User/globalStorage/state.vscdb`        |
| Cursor      | `~/.config/Cursor/User/globalStorage/state.vscdb`             |
| VSCodium    | `~/.config/VSCodium/User/globalStorage/state.vscdb`           |

```sh
# Auto-find via glob (picks first match, fine for single-IDE setups)
GS_DB=$(ls ~/.config/*/User/globalStorage/state.vscdb 2>/dev/null | head -1)
python3 -c "
import sqlite3, json, sys
con = sqlite3.connect(sys.argv[1])
data = json.loads(con.execute(\"SELECT value FROM ItemTable WHERE key='Anthropic.claude-code'\").fetchone()[0])
print(data.get('hiddenSessionIds', []))
" "$GS_DB"
```

Use `mgr.getConnection(sid)` to query a hidden session's chain
walker output regardless of its visibility state.

### `conn.outstandingRequests` can leave the wire jammed after killed eval processes

Each `conn.sendRequest` adds an entry to `conn.outstandingRequests`
(Map keyed by requestId) and awaits the response promise. If the
caller process is killed before the response arrives, the entry
stays in the Map. Subsequent `sendRequest` calls **may appear to
hang** because the conn's wire processing gets degraded by the
backlog.

Symptom: `await conn.sendRequest({...})` never resolves; exthost is
idle (`pcpu` = 1.2%, `cpuTime` flat); no error logged.

Recovery from inside the inner active-frame:

```js
let conn = await mgr.getConnection(sid);
for (let k of [...conn.outstandingRequests.keys()]) {
  try { conn.cancelRequest(k); } catch(_){}
}
conn.outstandingRequests.clear();
```

After clearing, fresh `sendRequest` calls work normally.

Prevention: kill cleanly with `pkill -f eval_in_inner_frame.mjs`
before retrying; finalize-on-exit cancels the request.
Alternatively wrap `eval_in_inner_frame.mjs` invocations in
`timeout <N>` to bound their lifetime.

### Don't clear the `>` prefix when typing into command palette

`Ctrl+Shift+P` opens the palette in **command mode** with a `>`
prefix in the input. If you do `input.value = ""` (or otherwise
clear it) before typing, the palette drops to **file-picker mode**
where `Enter` opens a file matching the typed string instead of
running a command.

Always APPEND-only: type your command after the existing `>`. The
final input value should look like `>Reload Window`, not
`Reload Window`.

Verify before pressing Enter:

```js
JSON.stringify({
  text: document.querySelector('.quick-input-and-message input').value,
  firstSugg: document.querySelector('.quick-input-list .monaco-list-row')?.textContent?.slice(0, 80),
})
```

`text` should start with `>`; `firstSugg` should be the command name
you intended (e.g. "Developer: Reload Window..."), not a filename.

### Body click before Ctrl+Shift+P when iframe focus is suspect

If a webview iframe is focus-trapped (especially a broken
chrome-error one; see earlier gotcha), `Ctrl+Shift+P` may open the
palette but typing won't reach the input (focus stays on the
iframe). Body-area mouse click first to take focus from the iframe:

```js
await call('Input.dispatchMouseEvent', {type:'mousePressed',  x:5, y:5, button:'left', clickCount:1});
await call('Input.dispatchMouseEvent', {type:'mouseReleased', x:5, y:5, button:'left', clickCount:1});
```

Then verify `document.activeElement.tagName === 'BODY'` before
firing Ctrl+Shift+P. If it's still `IFRAME`, the iframe is broken
and needs explicit recovery (close the panel via UI, or
`Reload Window`).

### Patch K verification recipes (clone-to-induce-non-uniqueness, rename-to-induce-breakage)

For testing K's edge cases (v1.5+ ambiguity warning + reconstruction-
failed marker), the JSONL filesystem is the test fixture:

**Test #1, induce sibling-backfill non-uniqueness** (verifies the
`AMBIGUOUS RECONSTRUCTION` warning fires on the bookend + relevant
seam markers):

```sh
PROJ=~/.claude/projects/-<workspace>
# Pick a session you know is the K1-backfill source for the target.
# Clone it to a new uuid filename; same content, structurally
# qualifies for backfill, makes count > 1.
cp $PROJ/<source-sid>.jsonl $PROJ/<source-sid>-clone-aaaa-bbbb-cccccccccccc.jsonl
# Reload Window via palette → re-fetch chain walker for target session
# DOM-verify [data-pfgk-role="bookend"] count = 1, bodyHasAMBIG === true
# Cleanup: rm the clone
```

**Test #5, induce reconstruction failure** (verifies the
`pfgk-broken-` marker variant fires with critical styling):

```sh
PROJ=~/.claude/projects/-<workspace>
# Rename the K1-backfill source so K1 finds no qualifying sibling.
# Suffix with .test-disabled (also rename .bak to keep the pair).
mv $PROJ/<source-sid>.jsonl     $PROJ/<source-sid>.jsonl.test-disabled
mv $PROJ/<source-sid>.jsonl.bak $PROJ/<source-sid>.jsonl.test-disabled.bak
# Reload Window via palette
# DOM-verify [data-pfgk-role="broken"] count = 1, bodyHasINCOMPLETE === true
# Cleanup: rename back
```

Both tests target the SAME session (the one whose chain walker
output you're inspecting). The fixture changes its sibling
environment, not its own content.

**End-to-end DOM verification template** (used after each fixture
change + reload):

```js
JSON.stringify({
  pfgkAlert: document.querySelectorAll('.pfgkAlert').length,
  bookend:  document.querySelectorAll('[data-pfgk-role="bookend"]').length,
  seam:     document.querySelectorAll('[data-pfgk-role="seam"]').length,
  bridge:   document.querySelectorAll('[data-pfgk-role="bridge"]').length,
  broken:   document.querySelectorAll('[data-pfgk-role="broken"]').length,
  bodyHasAMBIG:      document.body.textContent.includes('AMBIGUOUS RECONSTRUCTION'),
  bodyHasUNRECOVERABLE: document.body.textContent.includes('UNRECOVERABLE'),
  msgs: document.querySelectorAll('[class*=message_]').length,
})
```

Predictions:
- Baseline (no fixture): 4 markers (1 bookend + 2 seams + 1 bridge),
  0 broken, no AMBIG, no INCOMPLETE.
- Test #1 (clone present): SAME marker counts but `bodyHasAMBIG ===
  true`, bookend + at least one seam contain "AMBIGUOUS
  RECONSTRUCTION:" prefix.
- Test #5 (source renamed away): `bookend` count = 0, `broken`
  count = 1, `bodyHasINCOMPLETE === true`. Total `msgs` drops by
  ~size of the missing source's pre-content.

### "K detected" vs "K succeeded": gate downstream logic on attempt, not effect

A subtle K design lesson learned the hard way: when downstream
logic (bookend planting, broken-marker fallback) needs to react to
the *presence* of a problem, gate on whether K *detected* the
problem, not whether K *fixed* it.

Concrete instance from v1.5 → v1.6: the post-K "plant bookend or
broken marker" block was gated on `if(_kFired)`, where `_kFired`
became true only when K2 successfully planted a seam. But K2 has
its own preconditions (needs an in-file predecessor in `_parsed`
to anchor the seam to). For a session whose chain begins with a
phantom-lpu compaction boundary at index 0 of `_parsed` after J's
prepend (i.e., the boundary is at the very start), K2 has nothing
to anchor to and `continue`s; `_kFired` stays false. The bookend/
broken/bridge block is then skipped entirely and the rendered chain
shows **zero markers despite having phantom-lpu data loss**: silent
failure, exactly the bug the broken marker was supposed to prevent.

Fix: introduce a separate `_kAttempted` flag set whenever K detects
a phantom-lpu boundary it would WANT to fix (regardless of whether
the seam plant succeeded). Gate downstream "plant a marker"
fallback on `_kAttempted`. The bookend(b) → broken-marker predicate
then fires correctly even when K2 couldn't anchor a seam.

Rule of thumb: any K stage that has multiple preconditions for
side-effect success (plant a ghost) should also set a "detection"
flag with weaker preconditions (just "saw the problem"). The
detection flag is what user-facing fallback markers gate on.

### Marker informativeness: surface concrete data, not prose

When designing user-facing diagnostic markers (bookend, seam, bridge,
broken, or any analogous synthesized message), include the concrete
data the marker is acting on. Generic prose ("a compaction event
happened here") is far less useful than the specific identifiers
involved:

- **Phantom uuid** that was unresolvable (full uuid, not truncated;
  users may grep for it across files).
- **Sibling filename** that K1 backfilled from (so user can correlate
  with their own session list / fork structure).
- **Predecessor uuid** the seam stitched to (for chain-walking
  verification).
- **Counts** (number of qualifying siblings, msgs prepended,
  phantoms attempted vs backfilled).
- **Wall-clock timing** of K stages (parse / J prepend / K1 / K2-3-
  bookend): surfaces perf hotspots and helps the user judge whether
  slow chain rendering is K's fault or downstream React's.

Don't truncate uuids in marker text just for display compactness;
the value of full uuids for cross-referencing exceeds the cost of
a slightly longer string. The truncation/collapse `<style>` already
in the wrap handles overflow visually.

K v1.5 marker text was prose-heavy with truncated uuid prefixes;
v1.6 added per-marker data tracking + full uuids + wall-clock
timing. The v1.6 markers are usable as standalone diagnostic
artifacts (paste into a bug report, the data is all there); v1.5
markers required the user to also dump the chain walker output
elsewhere.

### Red-on-red (and other role-specific bg color clashes)

Webview render wrap that uses the SAME header text/border color
(`color:_bd, borderBottom:"2px dashed "+_bd`) across all marker
roles works fine when bg colors are subtle (rgba alpha 0.18-0.20),
because the dark border-color stands out enough on the lighter bg.

But when a role bumps bg to higher saturation (broken role: `rgba(180,
0,0,0.50)`) AND the same role uses a dark border color (`#990000`),
the header text rendered in `color:_bd = #990000` becomes
near-invisible on the dark-red bg. Same can happen for any role
whose bg is high-saturation and bd matches the bg's hue.

Fix: detect the high-saturation role in the header style and
override `color:` (and `borderBottom:`) to a contrasting value:

```js
color: _r==="broken" ? "#ffffff" : _bd,
borderBottom: "2px dashed " + (_r==="broken" ? "#ffffff" : _bd),
```

Verify empirically via DOM probe:

```js
JSON.stringify({
  bg: getComputedStyle(brokenEl).backgroundColor,
  header_color: getComputedStyle(brokenHeaderDiv).color,
})
```

Header color should be sufficiently distant in lab-distance from
bg color. White vs dark-red is fine; dark-red vs dark-red is not.

When adding any new role with elevated bg saturation, manually
verify header readability before considering the wrap change
shipped.

---

## Case study: empirically diagnosing Patch K's "seam not rendering"

This walks through using all the above mechanics to diagnose a real
bug. Patch K (lost+found recovery for sessions with phantom
`logicalParentUuid` after compaction) was suspected of failing on some
sessions: the seam ghost it inserts wasn't appearing in the rendered
chat panel for one specific session, while it appeared correctly for
another. This case study shows the introspection that confirmed the
structural cause empirically.

### Symptom

User opens two chat panels in the same Antigravity window:

- Session **A** (`0727164e-...`): Patch K's seam ghost renders
  correctly in the panel.
- Session **B** (`61974011-...`): Patch K's seam ghost is absent
  from the panel.

Both sessions have the same patches loaded (signature `/*pfg-v1.4*/`
present in `extension.js`). Both panels are in the same exthost (same
window). What's different?

### Step 1: precheck patches are loaded

```sh
# Path stem was ~/.antigravity/ at the time; current installs use ~/.antigravity-ide/.
node /tmp/cdp-eval.mjs "$WS_TARGET" '(function(){
  var s = require("fs").readFileSync(
    "/home/juraj/.antigravity/extensions/anthropic.claude-code-2.1.126-linux-x64/extension.js","utf-8");
  return JSON.stringify({sig: ["/*pfg-v1.4*/"].filter(t=>s.includes(t)),
                         hasK: s.includes("Orphaned compaction pointer")});
})()'
// {"sig":["/*pfg-v1.4*/"], "hasK":true}: patches are live
```

### Step 2: simulate Ez4 on the broken session's JSONL outside the running extension

To verify Patch K is *supposed* to fire, replicate the loader chain
against Session B's JSONL on disk:

```js
// Read 61974011's JSONL, parse, walk for compact_boundaries with
// phantom lpu, simulate K's logic.
```

Result: simulator reports K should produce 1 seam ghost in Session B.
So K is firing logically, but the rendered messages don't contain
it. Discrepancy is downstream of K.

### Step 3: BP at Ez4 entry and capture all locals

Use Recipe 1 (BP-with-side-effect-condition) to capture
`V`/`U`/`Z`/`H` at Ez4's RETURN site:

```js
// /tmp/bp_ez4_capture.mjs
condition: `((function(){
  try {
    (globalThis.__caps = globalThis.__caps || []).push({
      ts: Date.now(),
      Vlen: V.length,
      U: U.slice(0,40).map(t => ({u: String(t.uuid).slice(0,12), i: B.get(t.uuid), t: t.type})),
      Zuuid: Z ? String(Z.uuid).slice(0,12) : null,
      Zidx: Z ? B.get(Z.uuid) : null,
      Hlen: H.length,
      pfgkInH: H.filter(m => String(m.uuid).startsWith('pfgk-')).map(m => String(m.uuid).slice(0,20)),
      pfgkInV: V.filter(m => String(m.uuid).startsWith('pfgk-')).map(m => String(m.uuid).slice(0,20)),
    });
  } catch(e) { (globalThis.__capsErr = globalThis.__capsErr || []).push(String(e)); }
  return false;
})())`
```

### Step 4: trigger via webview RPC (Recipe 2)

Use the bash orchestration pattern: BP script in background, post
`get_session_request` for Session B in foreground, read `__caps` after.

```sh
node /tmp/bp_ez4_capture.mjs "$WS_TARGET" 25 > /tmp/cap.log 2>&1 &
sleep 8
node /tmp/cdp-eval.mjs "$WS_RENDERER_PAGE" \
  'window.__vscode_post_message__("onmessage", {
    message: {type:"request", channelId:"probe", requestId:"r1",
              request:{type:"get_session_request", sessionId:"61974011-..."}},
    transfer: undefined});
   "posted"'
sleep 2
node /tmp/cdp-eval.mjs "$WS_TARGET" 'JSON.stringify(globalThis.__caps||[])'
wait
```

### Step 5: read the result

```json
{
  "Vlen": 5060,
  "U": [
    {"u": "pfgk-seam-32", "i": 2375, "t": "user"},
    {"u": "d2558225-074", "i": 4750, "t": "user"},
    {"u": "5fda77a3-9d3", "i": 4772, "t": "user"},
    {"u": "9fc43c52-903", "i": 5059, "t": "assistant"}
  ],
  "Zuuid": "9fc43c52-903",
  "Zidx": 5059,
  "Hlen": 2683,
  "pfgkInV": ["pfgk-seam-32ff69a5"],
  "pfgkInH": []
}
```

### Diagnosis (empirically confirmed)

- `pfgkInV: ["pfgk-seam-32ff69a5"]`: the seam IS in the input. K
  inserted it correctly.
- `U` has 4 leaf-walk tips, INCLUDING `pfgk-seam-32` at idx 2375.
- `Zuuid: 9fc43c52-903` at idx **5059**: Ez4 picks Z by `max(B.get(t.uuid))`;
  the chain-B leaf at 5059 wins over the seam at 2375.
- `pfgkInH: []`: the rendered chain (walked back from Z at 5059)
  never crosses the seam. Seam is unreachable.

The bug isn't in K's insertion. It's in Ez4's single-leaf max-by-index
walker: when a session has TWO disjoint chains in the file (orphan +
live), the walker picks the live-chain leaf and ignores the orphan.
K's seam, planted on the orphan chain, becomes topologically invisible.

Session A (`0727164e-...`) doesn't trigger the asymmetry because its
second compaction's `lpu` resolves SAME-FILE (a `local_command stderr`
uuid captured by `compact.ts:598`), keeping the live chain stitched
through the orphan; Session B's second-compaction `lpu` resolves
CROSS-FILE (a `tool_result` uuid in a sibling JSONL), bypassing the
orphan entirely.

### Mitigation (added in pfg-v1.3, later subsumed by the bridge mechanism in v1.4)

Once the empirical diagnosis was nailed, the fix was straightforward:
detect the unreachable scenario at K-time (signal: a seam was planted
but no bookend was; bookend fails to fire when the chain root is the
sibling-file content Patch J prepended), and synthesize a **third
ghost type**, `pfgk-orphannotice-…`, on the LIVE chain. Insert it
between the cross-file-resolved boundary and that boundary's first
child, so the walker traverses through it on the way back from Z.
The seam stays planted on the orphan chain (semantically correct for
that branch); the orphannotice provides the user-facing signal in the
live chain. Render wrapper colours it amber.

End-to-end verification of the fix used the same recipes in this doc:
disk-edit `extension.js` + `webview/index.js` (mechanism 5 from the
refresh playbook), `Input.dispatchKeyEvent` for "Reload Window"
(mechanism 3), then BP-free verification via fiber-walk to the manager
`__mgr.activeSession.value.messages.peek()`, filtering for
`pfgk-orphannotice-` prefix uuids and DOM probing for the
`.pfgk-orphannotice` wrapper class.

### What this case study demonstrates

Each recipe in this doc was used:

- Mental model (mine/target/renderer) to pick the right ports
- Precheck to confirm patches are live
- Recipe 1 (BP-with-side-effect-condition) to capture Ez4's locals
- Recipe 2 (RPC trigger) to invoke `Wz4` → `Ez4` without UI
- Bash orchestration to hold the BP across trigger+read
- Field-shape knowledge (camelCase locals at Ez4) to read fields right

Without the BP-with-side-effect-condition recipe this would have been
hard to confirm empirically; `setScriptSource` injection of the same
trace failed silently, per the gotcha. The mechanic generalises beyond
Patch K. It's the answer for any "rendered messages don't match
on-disk JSONL" question.

---

## Case study: dead K wrap from non-pristine `.bak` synthesis (2.1.132)

A second case study, demonstrating how a prebuilt can pass byte-stability
checks while shipping dead code, and the empirical workflow to diagnose +
recover.

### Symptom

Antigravity upgrades to `2.1.132`. `/patch-claude` runs the manual-fallback
path (no prebuilt exists for this version yet). Maintainer translates the
v1.4 K splices from the `2.1.126` prebuilt with bundle-var renames
(`XR0` → `GR0` etc.), `node --check` passes, `build-prebuilt.py`
synthesizes a new prebuilt for `2.1.132`, byte-stability check passes,
prebuilt pushed.

User reports K rendering is broken: "ancient version of Patch K. Where
is the circular navigation?"

### Step 1: precheck patches are loaded

```sh
WS=$(curl -s http://127.0.0.1:9229/json | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["webSocketDebuggerUrl"])')
# Path stem was ~/.antigravity/ at the time; current installs use ~/.antigravity-ide/.
node /tmp/cdp-eval.mjs "$WS" '(function(){
  var s = require("fs").readFileSync(
    "/home/juraj/.antigravity/extensions/anthropic.claude-code-2.1.132-linux-x64/extension.js","utf-8");
  return JSON.stringify({sigs: ["/*pfg-v1.4*/"].filter(t=>s.includes(t)),
                         hasK: s.includes("_kFired"), hasBridge: s.includes("pfgk-bridge-")});
})()'
// {"sigs":["/*pfg-v1.4*/"], "hasK":true, "hasBridge":true}: extension.js patch is live.
```

### Step 2: probe the rendered DOM in the chat panel

Find the chat panel's webview iframe target via `/json/list` (filter for
`type:"iframe"` whose parent page is the target window's). Then drill into
the inner `active-frame` and probe for K wrap markers:

```sh
WS_CHAT="ws://127.0.0.1:9222/devtools/page/<chat-iframe-id>"
node /tmp/eval_in_inner_frame.mjs "$WS_CHAT" 'JSON.stringify({
  pfgkAlert: document.querySelectorAll(".pfgkAlert").length,
  bookend: document.querySelectorAll("[data-pfgk-role=\"bookend\"]").length,
  hasPatchKMarker: document.body.textContent.includes("PATCH K · ")
})'
// {"pfgkAlert":0,"bookend":0,"hasPatchKMarker":true}
```

The K message text is in the DOM (`PATCH K · Conversation origin...`)
but `pfgkAlert: 0`: the wrap div isn't being rendered. The K bookend
is showing as a plain user message bubble.

### Step 3: walk DOM up from the K text node to identify where the wrap *should* be

```sh
node /tmp/eval_in_inner_frame.mjs "$WS_CHAT" '(function(){
  var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  var n;
  while (n = w.nextNode()) if (n.nodeValue.includes("PATCH K · Conversation")) break;
  if (!n) return "not found";
  var path = []; var c = n.parentElement;
  for (var i=0; i<10 && c; i++) { path.push({tag: c.tagName, cls: (c.className||"").slice(0,80)}); c = c.parentElement; }
  return JSON.stringify(path);
})()'
```

Result shows `SPAN → DIV.content_xGDvVg → DIV.contentWrapper_xGDvVg → DIV.userMessage_07S1Yg → ...`: the K message is inside a normal
`userMessage` bubble, with NO `pfgkAlert` parent. The wrap React node
that's supposed to wrap the user-message createElement isn't there.

### Step 4: inspect the patched `webview/index.js` source

```sh
grep -A 1 'pfgkAlert' "$EXT/webview/index.js" | head
```

The wrap code IS present in the source:

```js
if(Z.type==="user"){
  if(Z.parentToolUseId)return null;
  if(Z.isSynthetic)return null;
  return n1.default.createElement(GR0,{...});       // ← returns immediately
  if(typeof Z.uuid==="string"){...; _ws=createElement("div", ..., _ws); ...}
  return _ws                                          // ← UNREACHABLE
}
```

The `return n1.default.createElement(GR0,...)` makes everything after it
dead code. The wrap branch never runs.

### Step 5: diagnose the synthesis pitfall

The `2.1.126` prebuilt's K webview splice OLD anchor was
`G,setInputError:q,onCreateNewSession:z})}if(Z.type==="assistant"...`
(80 chars). The corresponding NEW string changed `})}` to
`});<wrap code>; return _ws}`. For this to produce a wrapped result,
the createElement call must NOT be preceded by `return `; instead
preceded by `let _ws=`.

**In `2.1.126`**, the patch was developed iteratively (v1.2 → v1.3 → v1.4).
By the time the v1.4 prebuilt was synthesized, an earlier iteration had
already transformed `return createElement(...)` to `let _ws=createElement(...)`.
That transformation was equally present in `.bak` (post-iteration) and
live (post-iteration), so `build-prebuilt.py`'s diff didn't capture it.
The synthesized splice covered only the wrap-internal v1.3→v1.4 changes.
Byte-stability passed because re-applying the splice to that `.bak`
reproduces live exactly, but the .bak wasn't pristine.

**In `2.1.132`** (fresh install, `.bak` IS pristine `return createElement(...)`),
the splice's grep anchor matched, `node --check` passed, byte-stability
passed, but the result was dead code.

### Step 6: fix and re-synthesize

Add an explicit `return → let _ws=` transformation as a separate splice:

```py
old = "return n1.default.createElement(GR0,{session:$,...,onCreateNewSession:z});"
new = "let _ws=n1.default.createElement(GR0,{session:$,...,onCreateNewSession:z});"
```

After applying, `build-prebuilt.py` synthesis now captures 4
`webview/index.js` splices (was 3): the new transformation + the original
3. Byte-stability passes (deterministic against the new pristine `.bak`).

### Step 7: reload via mechanism 3 and verify in DOM

This is the recovery + verify path. The maintainer first improvised
with `location.reload()` on the iframe (DON'T; see gotcha) which left
the iframe in `chrome-error://chromewebdata/`. Then `Page.reload` on
the iframe target (DON'T; rejected as not top-level). Then command
palette typing failed because focus was held by the broken iframe.

The recovery path that actually works:

```js
// 1. Body-area mouse click to take focus from the broken iframe
await call('Input.dispatchMouseEvent', {type:'mousePressed', x:5, y:5, button:'left', clickCount:1});
await call('Input.dispatchMouseEvent', {type:'mouseReleased', x:5, y:5, button:'left', clickCount:1});

// 2. Verify document.activeElement is now BODY, not IFRAME
//    (skip this and you'll waste turns wondering why typing doesn't reach the palette)

// 3. Open command palette: Ctrl+Shift+P sets the ">" command-mode prefix
await call('Input.dispatchKeyEvent', {type:'rawKeyDown', modifiers:10, key:'P', code:'KeyP', keyCode:80, windowsVirtualKeyCode:80});
await call('Input.dispatchKeyEvent', {type:'keyUp', modifiers:10, key:'P', code:'KeyP', keyCode:80, windowsVirtualKeyCode:80});

// 4. CRITICAL: do NOT clear the input. Clearing drops the ">" prefix and
//    puts the palette into file-picker mode, where Enter opens a file
//    instead of running a command. Type APPEND-style.

// 5. Type "Reload Window" via char events
for (const ch of "Reload Window") {
  await call('Input.dispatchKeyEvent', {type:'char', text:ch, unmodifiedText:ch});
}

// 6. Verify firstSugg matches "Developer: Reload Window..."

// 7. Enter, wait ~10s for reload
await call('Input.dispatchKeyEvent', {type:'rawKeyDown', key:'Enter', code:'Enter', keyCode:13, windowsVirtualKeyCode:13});
await call('Input.dispatchKeyEvent', {type:'keyUp', key:'Enter', code:'Enter', keyCode:13, windowsVirtualKeyCode:13});
```

After reload, re-discover the new chat-panel iframe target (the ID
changes on reload), and re-probe:

```sh
WS_CHAT_NEW="ws://127.0.0.1:9222/devtools/page/<new-iframe-id>"
node /tmp/eval_in_inner_frame.mjs "$WS_CHAT_NEW" 'JSON.stringify({
  pfgkAlert: document.querySelectorAll(".pfgkAlert").length,
  bookend: document.querySelectorAll("[data-pfgk-role=\"bookend\"]").length,
  seam: document.querySelectorAll("[data-pfgk-role=\"seam\"]").length,
  bridge: document.querySelectorAll("[data-pfgk-role=\"bridge\"]").length
})'
// {"pfgkAlert":4,"bookend":1,"seam":2,"bridge":1}: fix verified end-to-end.
```

### What this case study demonstrates

- **Byte-stability ≠ correctness**. The check validates determinism
  against the .bak you have, not behavioral correctness against a
  pristine .bak. (Gotcha.)
- **`.bak` is the pristine pre-patch baseline; never overwrite it.**
  Per-patch checkpoints are `.patchX.bak` (= settled state of patch
  X), named after the patch they contain rather than the next patch
  in line. This invariant makes the diff-based prebuilt synthesis
  correct by construction. (MAINTAINER rule.)
- **DOM probing is the user-visible verification.** `node --check` and
  byte-stability are necessary but not sufficient.
- **Don't improvise reload mechanisms.** `location.reload()` on a
  webview iframe leaves it in chrome-error; `Page.reload` is rejected
  on iframe targets. Use mechanism 3 (Reload Window via key events) or
  mechanism 5 (disk + Reload Window). (Gotcha.)
- **Focus-stuck-to-iframe blocks command palette typing.** Body-area
  mouse click first; verify `document.activeElement` before typing.
  (Gotcha.)
- **Don't clear the `>` prefix when the palette is in command mode.**
  Append, don't replace. (Pitfall.)

The first three points are the substantive lessons; the last three are
recovery paths from cowboying-when-the-playbook-already-said-not-to.
