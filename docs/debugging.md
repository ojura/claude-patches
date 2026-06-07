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
> - **Reload mechanisms**: see "Refresh / reload playbook" for five
>   verified mechanisms ordered by precision. `location.reload()` on
>   webview iframes BREAKS the iframe (chrome-error). `Page.reload`
>   is rejected on iframe targets. Don't try them. Use mechanism 3
>   (`Input.dispatchKeyEvent` for `workbench.action.reloadWindow`)
>   or mechanism 5 (disk-edit + Reload Window). **Bare `Ctrl+R` is
>   unreliable** (keybinding-context-sensitive); the canonical
>   reload is `Ctrl+Shift+P` → type `Reload Window` → `Enter`.
> - **Verifying a code change took effect**: see the Patch K case study in
>   [`patches.md`](patches.md#verifying-and-debugging-the-recovery-markers) (its "End-to-end verification of the fix" paragraph). The exact recipe
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
> sanity checks but **none of them substitute for a query against the actual
> rendered iframe** counting the elements your change renders, plus a
> `document.body.textContent.includes(...)` text check.
> When the change is exercised by a fixture, embed a unique **nonce** in
> that fixture's payload and assert the nonce appears in the rendered DOM:
> a stale or rehydrated tab will not contain it, so it cannot pass as a
> fresh render (byte-size or resource-timing guesses do not prove this).
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
>   count: document.querySelectorAll('<the selector your change renders>').length,
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

Use them, not an ad-hoc client. The DevTools WS endpoint rejects any
handshake carrying an `Origin` header with `403 Forbidden`; Node's native
`WebSocket` sends none, which is the entire reason these work where a
hand-rolled connection does not. Python's `websocket-client`, for instance,
403s unless you pass `suppress_origin=True`. That is a workaround, not a
license to deviate; the Node scripts are the path.

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

The webview-side session manager (class `Wn`; minified name drifts between releases):

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
DOM-verify your change's output in the active-frame
        ↓
iterate
```

The make-or-break step is the fresh walk (Step 6). A bare reload restores the
old rendered DOM from serialized state; it does not re-run the walker. See Step
6 for why, and the two triggers that actually re-mount the webview.

This loop is the general method. The claude-patches marker specifics, the
copy-paste one-shot marker cycle, the rendered-marker probes, the fixture
generator (`gen_demo`), the marker gotchas, and the diagnostic case studies, all
live in [`patches.md`](patches.md#verifying-and-debugging-the-recovery-markers).
The Steps below are the reusable pieces; Step 6 (a fresh walk via the
conversations panel) is the make-or-break one.

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
// FOCUS RECOVERY first: a broken chrome-error iframe (from an earlier botched
// reload) captures focus and silently swallows the later Enter, so the reload
// no-ops. Escape, then a REAL mouse click on workbench chrome (titlebar or
// statusbar rect, never an iframe), restores focus to the workbench.
await call('Input.dispatchKeyEvent', {type:'rawKeyDown', key:'Escape', code:'Escape', keyCode:27, windowsVirtualKeyCode:27});
await call('Input.dispatchKeyEvent', {type:'keyUp', key:'Escape', code:'Escape', keyCode:27, windowsVirtualKeyCode:27});
await sleep(120);
const _rc = JSON.parse((await call('Runtime.evaluate', {expression:
  '(()=>{const s=document.querySelector(".part.titlebar")||document.querySelector(".statusbar");const r=s?s.getBoundingClientRect():{left:300,top:6,width:0,height:0};return JSON.stringify({x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)})})()',
  returnByValue:true})).result.value);
await call('Input.dispatchMouseEvent', {type:'mousePressed', x:_rc.x, y:_rc.y, button:'left', clickCount:1});
await call('Input.dispatchMouseEvent', {type:'mouseReleased', x:_rc.x, y:_rc.y, button:'left', clickCount:1});
await sleep(200);
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
// Verify the SELECTED row (.monaco-list-row.focused), not just the first row,
// IS the command, AND that the palette input still holds focus. A broken
// chrome-error iframe steals focus and silently swallows the Enter, so the
// reload no-ops (see the Focus-lost-to-broken-iframe gotcha; recover focus
// with a real mouse click on workbench chrome first).
const st = JSON.parse((await call('Runtime.evaluate', {expression:
  '(()=>{const w=document.querySelector(".quick-input-widget");const f=w&&w.querySelector(".quick-input-list .monaco-list-row.focused");const a=document.activeElement;return JSON.stringify({active:a?a.tagName+"/"+(a.className||"").slice(0,30):null,selected:f?f.textContent.slice(0,60).trim():null})})()',
  returnByValue:true})).result.value);
if (!/INPUT/.test(st.active||'')) {
  console.log('ABORT: focus on', st.active, '(broken iframe; recover then retry)'); ws.close(); process.exit(1);
}
if (!st.selected || !st.selected.startsWith('Developer: Reload Window')) {
  console.log('ABORT: selected row is', st.selected); ws.close(); process.exit(1);
}
console.log('selected row:', st.selected);
try {
  await call('Input.dispatchKeyEvent', {type:'rawKeyDown', key:'Enter',
    code:'Enter', keyCode:13, windowsVirtualKeyCode:13});
  await call('Input.dispatchKeyEvent', {type:'keyUp', key:'Enter',
    code:'Enter', keyCode:13, windowsVirtualKeyCode:13});
  // Enter was DISPATCHED. That is NOT confirmation the window reloaded: if a
  // broken iframe still had focus, or the row was not actually selected, this
  // is a silent no-op. Confirm separately (Step 5: the chat iframe target id
  // changes; Step 3: the exthost comes back fresh). Never treat this as success.
  console.log('ENTER DISPATCHED (NOT confirmed; verify via Step 5 + Step 3)');
} catch (e) { console.log('enter dispatch error:', e && e.message); }
ws.close();
EOF
node /tmp/reload_go.mjs "$PAGE"
```

If the palette opens but typing fails (focus stuck on a broken iframe), run a
body-area mouse click first. See the "Focus-lost-to-broken-iframe" gotcha.

Do not read the script's exit code as success. On a successful reload the 9222
page tears down mid-script, the post-Enter `await`s never settle, and Node exits
non-zero (13, "unsettled top-level await"). A dispatched Enter is not proof of a
reload regardless; confirm only via Step 3 (exthost pid change) and Step 5 (a new
chat iframe id).

### Step 3: wait for the exthost to RESTART (not merely respond)

Reload Window kills the old exthost process and spawns a new one (confirmed:
`process.pid` changes and `process.uptime()` resets to 0 on every reload; the old
pid is gone, the Antigravity main process re-parents the fresh one). The naive
"poll 9229 until it answers" FALSE-POSITIVES: for ~1.7s after you start driving
the palette the old host is still alive and 9229 still serves it, so a poll that
begins right after triggering catches the OLD host (with a large uptime) and
declares success without ever seeing the restart. Watch the PID, not reachability.

```sh
# BEFORE triggering the reload, record the current exthost pid:
OLD=$(curl -s --max-time 1 http://127.0.0.1:9229/json \
      | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["webSocketDebuggerUrl"])')
OLD_PID=$(node /tmp/cdp-eval.mjs "$OLD" 'process.pid' \
      | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["result"]["value"])')
echo "old exthost pid: $OLD_PID"
# ... now trigger the reload (Step 2) ...

# Then poll for a DIFFERENT pid with a small uptime (race-free):
for i in $(seq 1 40); do
  sleep 1
  WS=$(curl -s --max-time 1 http://127.0.0.1:9229/json 2>/dev/null \
       | python3 -c 'import sys,json
try: print(json.load(sys.stdin)[0]["webSocketDebuggerUrl"])
except Exception: pass')
  [ -z "$WS" ] && continue                       # old host gone, new not up yet
  R=$(node /tmp/cdp-eval.mjs "$WS" 'JSON.stringify({pid:process.pid,up:Math.round(process.uptime())})' 2>/dev/null \
       | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["result"]["result"]["value"])
except Exception: pass')
  echo "$R" | python3 -c "import sys,json
try: d=json.loads(sys.stdin.read()); sys.exit(0 if (d['pid']!=$OLD_PID and d['up']<60) else 1)
except Exception: sys.exit(1)" && { echo "exthost restarted: $R (was $OLD_PID)"; break; }
done
```

Conditions, by importance: (1) `pid != OLD_PID` is ground truth that a new
process exists, and kills the "old host still alive" false positive outright;
(2) `uptime < 60` is belt-and-suspenders; (3) treat HTTP-refused, empty `/json`,
and transient `Promise was collected` eval errors (the new host's first ~1s of
inspector init) as keep-polling, never as terminal.

Measured timing (the old "12-16s" figure is stale): the new exthost HTTP-answers
~1.6-2.2s after Enter, and the chat iframe re-mounts ~1s after that. Total reload
is ~5s, not 15; keep a ~40s ceiling but expect ~5s.

Cheaper renderer-side signal: snapshot the `vscode-webview` chat-iframe id(s)
from 9222 `/json/list` before the reload, then poll until a NEW id appears that
was not in the snapshot (~3s after Enter). No exthost eval needed; it is the
better default for the iteration loop because what you verify next lives in that
iframe (it dovetails with Step 6's fresh-mount requirement). Use the pid check to
prove the *exthost* reloaded your `extension.js`; use the iframe-id change when
you need the *renderer*. The workbench PAGE id never changes; do not use it.

Caveat if you (or any tool) hold an exthost inspector socket OPEN across the
reload: the old Node process will not exit until the debugger detaches (Node
`--inspect` blocks at exit waiting for the inspector), so it leaks and stays in
the process table, indefinitely while the socket is held. A liveness check like
"is the old pid gone?" then falsely reports the reload as incomplete. The served
pid on `9229` and the new iframe id flip on schedule regardless, so they remain
the correct signals; never use "old pid still in `ps`" as a reload-incomplete
test when a socket is held. One-shot connect/eval/close tooling (`cdp-eval.mjs`)
closes within milliseconds and never hits this.

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

Iframe CDP target IDs change on every reload. Do not reuse old IDs. The
workbench PAGE id, by contrast, persists across a reload (the BrowserWindow
is reused; only its contents reload), so an unchanged page id does NOT mean
the reload failed. The renderer-side signal that the reload landed is the
chat iframe target id changing, not the page id.

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

The panel re-reads the project dir on each search, so a fixture you just
wrote to disk appears without any manual refresh and without a reload; you do
not need to reload the window for the panel to list a brand-new session.

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
poll (the probe returns nothing until the React app has painted). This loop uses
a render-check probe to spot the painted iframe; the claude-patches one is
`/tmp/pfg_markers.js` (see `patches.md`):

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

After the fresh walk, probe the inner active-frame for the output your change
produces, not the fixture file (which base Claude Code mutates on open). Use
`eval_in_inner_frame.mjs` to query the active-frame's main-world DOM and assert
on the elements and text your edit adds or removes; for a workbench screenshot,
`Page.captureScreenshot`. The claude-patches marker probes (role counts,
`markerText`, the field notes, the per-marker SVG extraction, and `shot.mjs`)
are in the "Probing the rendered markers" subsection of
[`patches.md`](patches.md#verifying-and-debugging-the-recovery-markers).

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
case studies in `patches.md` use it.

### `Page.reload` is rejected on iframe targets

`Page.reload` on a vscode-webview iframe target returns:

```
{ code: -32000, message: 'Command can only be executed on top-level targets' }
```

CDP only allows `Page.reload` on top-level pages. The webview iframe
inherits its lifecycle from its parent workbench page; reload that
instead (or use the refresh playbook's mechanism 3/5).

The `-32000` is not guaranteed. In practice `Page.reload` on a
vscode-webview iframe can instead return `ok` and break the frames anyway:
they go `chrome-error://chromewebdata/` and drop out of `/json/list` (e.g.
the count falls 3 to 1). A protocol `ok` is therefore not a safe signal.
Never `Page.reload` an iframe, whatever it returns.

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

---

The marker-specific verification (the `gen_demo` fixture generator, the rendered-
marker probes, the marker-specific gotchas, and the worked Patch K case studies)
lives in [`patches.md`](patches.md#verifying-and-debugging-the-recovery-markers).
This playbook keeps the project-agnostic method.

