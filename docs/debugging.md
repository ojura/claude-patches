# Debugging the bundled Claude Code extension

A reverse-engineer's reference for poking at a running
`anthropic.claude-code-*-linux-x64` install via Chrome DevTools Protocol
(CDP) — reading state, capturing function-internal data, dispatching RPCs,
walking React fibers, and triggering refreshes. Companion to
[`patches.md`](patches.md), which covers WHAT each patch does and WHY;
this one covers HOW to introspect the running bundle to build/verify
patches like them.

The notes here are derived from empirical work on Antigravity (Google's
VS Code fork that bundles claude-code via Open VSX). The mechanics
generalise to upstream VS Code with minor adjustments — the inspector
ports, IPC plumbing, and CommonJS module structure are the same; only
the launch flags and IDE-product paths differ.

---

## Mental model

### Three V8 processes, three CDP surfaces

Antigravity is Electron, so there are at least three distinct V8 processes
involved when a chat panel is open:

```
┌────────────────────────────────────────────────────────────┐
│  Renderer (Chrome) — port 9222                             │
│   ├─ Workbench page (one per IDE window)                   │
│   ├─ Outer vscode-webview iframe (CSP wrapper, empty body) │
│   └─ Inner active-frame iframe (the React app)             │
└────────────────────────────────────────────────────────────┘
                       ▲      ▲
                       │      │ electron IPC
                       │      ▼
┌────────────────────────────────────────────────────────────┐
│  Extension host (Node.js) — one per window                 │
│   ├─ extension.js (the bundled extension code)             │
│   ├─ Singleton manager (sessionStates, sessionPanels, ...) │
│   └─ Spawns claude --resume <sid> subprocesses             │
└────────────────────────────────────────────────────────────┘
                       │
                       │ stream-json stdio
                       ▼
┌────────────────────────────────────────────────────────────┐
│  claude --resume <sid> subprocess (Node.js) — one per chat │
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

`9229` is not "the target". It's whichever exthost spawned first —
could be your debugging target's window, could be the window that's
running this Claude Code session itself.

The roles you actually care about:

- **`mine`** — the exthost belonging to the Claude Code session running
  this debugger. Touching `mine` is touching yourself: BPs fire on your
  own activity, freezing you mid-tool-call.
- **`target`** — the exthost hosting the panel/session you want to
  inspect. The "outside" you're poking from.
- **`renderer`** — port 9222, shared by all renderers (DOM-side).

`mine` and `target` are typically two different exthost ports if you're
debugging another window's project. They're the SAME port if you're
debugging a panel in the same window as your Claude Code session — in
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
Node 22+'s built-in `WebSocket` global — no `npm install`, no Python,
no MCP servers.

### `/tmp/cdp-eval.mjs` — one-shot Runtime.evaluate

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

- `allowUnsafeEvalBlockedByCSP: true` — needed for vscode-webview iframes
  whose CSP otherwise blocks `eval`.
- `includeCommandLineAPI: true` — exposes Node's `require` in the eval
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
`ws://127.0.0.1:9222/devtools/page/<id>` — read `id` from
`http://127.0.0.1:9222/json/list`.

### `/tmp/eval_in_inner_frame.mjs` — drill into the React app

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
// — the inner's loaded URL is also index.html (same as outer), even
// though its src attribute pointed at fake.html.
const innerFrame = allFrames.find(f => f.name === 'active-frame');
if (!innerFrame) { console.error('FAIL: no name=active-frame'); process.exit(1); }

// Pick MAIN-world context (origin matches webview, name is empty).
// Reject __playwright_utility_world_* — those are isolated worlds that
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
| `&parentId=1` vs `&parentId=3` | VS Code container — correlates to area |

### Confirm an exthost's window cwd

```sh
WS=$(curl -s http://127.0.0.1:9229/json | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["webSocketDebuggerUrl"])')
node /tmp/cdp-eval.mjs "$WS" 'JSON.stringify({pid: process.pid, cwd: process.cwd()})'
```

`cwd` matches the workspace folder of the window owning that exthost.
Match against your target window's workspace path. **Cross-project
quirk**: a panel for a session in project A can be open inside a window
for project B — the exthost is the WINDOW's, not the SESSION's. Don't
assume by sessionId.

### Precheck: are the patches actually loaded?

Before BP work, verify patches are live (Reload Window or extension
auto-update wipes them):

```sh
WS=$(curl -s http://127.0.0.1:<TARGET>/json | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["webSocketDebuggerUrl"])')
node /tmp/cdp-eval.mjs "$WS" '(function(){
  var s = require("fs").readFileSync(
    "/home/juraj/.antigravity/extensions/anthropic.claude-code-2.1.126-linux-x64/extension.js","utf-8");
  return JSON.stringify({size: s.length, sig: ["/*pfg-v1*/","/*pfg-v1.1*/","/*pfg-v1.4*/"].filter(t=>s.includes(t))});
})()'
```

Empty `sig` → patches aren't installed. `/patch-claude` first or you'll
spend turns chasing phantoms in unpatched code. (Reading from disk is
fine; in-memory and on-disk are identical for active-installed
extensions, since CDP `setScriptSource` doesn't actually swap runtime
code — see Gotchas.)

---

## Recipe: capture function-internal state with a side-effect BP

This is THE pattern for "what does Ez4 see when it runs?" or "what fields
does fromClient receive?". Beats `setScriptSource` (which is broken for
runtime code; see Gotchas) and beats pure-pause BPs (which freeze the
exthost).

The mechanism: `Debugger.setBreakpoint` accepts a `condition` string
that V8 evaluates AT EACH HIT in the live frame's scope. The condition
has access to all locals. If the condition mutates `globalThis` and
returns `false`, the BP is "hit" but doesn't pause execution — you get
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
line/col by a few columns — V8 snaps to the nearest valid statement
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
`window.parent['__vscode_post_message__']` — that bridge is reachable
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
  `interrupt_claude`, `launch_claude`) — don't probe blindly. Test
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
startup and the resulting `vscode` is captured in closure — invisible to
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
it — use a generic walker that finds objects by method-name signature:

```js
(function(){
  const found = [];
  function looksLikeMgr(o) {
    if (!o || typeof o !== 'object') return false;
    for (const k of ['getSession','sendRequest','listSessions','renameSession']) {
      if (typeof o[k] === 'function') return k;
    }
    const proto = Object.getPrototypeOf(o);
    if (proto && proto !== Object.prototype) {
      for (const m of ['getSession','sendRequest','listSessions','renameSession']) {
        if (Object.getOwnPropertyNames(proto).includes(m)) return 'proto:'+m;
      }
    }
  }
  let visited = 0;
  function walk(fiber, depth, label) {
    if (!fiber || visited > 8000 || depth > 60) return;
    visited++;
    const mp = fiber.memoizedProps;
    if (mp && typeof mp === 'object') {
      for (const [k, v] of Object.entries(mp)) {
        if (v && typeof v === 'object') {
          const m = looksLikeMgr(v);
          if (m) found.push({ where: label+'/props.'+k, depth, match: m });
        }
      }
    }
    let st = fiber.memoizedState; let h = 0;
    while (st && h < 30) {
      const ms = st.memoizedState;
      if (ms && typeof ms === 'object') {
        const m = looksLikeMgr(ms);
        if (m) found.push({ where: label+'/hook['+h+']', depth, match: m });
        if ('current' in ms && ms.current && looksLikeMgr(ms.current)) {
          found.push({ where: label+'/hook['+h+'].current', depth });
        }
      }
      st = st.next; h++;
    }
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

Wrap in IIFE — `Runtime.evaluate` rejects top-level `return`. Stash
references on `globalThis` so subsequent evals can navigate without
re-walking. Empirically, on a session-list webview, the manager sits at
`host.child.child.child.memoizedProps.sessions`.

### Manager structure

The webview-side session manager (class `Wn` in v2.1.126):

- `sessions` — Preact signal; `.value` is array of session entries
- `activeSession` — signal; `.value.id` is current sessionId
- `comms` — connection POOL (class `Qn`), proto: `get(sid)`, `open`,
  `close`. NOT the conn itself
- Per-session entry has rich state (see below)

### Per-session entry state

Each item in `mgr.sessions.value` carries the full reactive state for
one session:

- `connection` — signal-wrapped conn (`class Jz1` with `getSession`,
  `sendRequest`)
- `messages` — signal; `.value` is the array the React app renders
- `assembler` — stream-event → message transformer
  (`processStreamEvent` is its only proto method)
- `busy`, `isLoading`, `pendingInput`, `error` — signals (boolean)
- `sessionId`, `gitBranch`, `cwd`, `permissionMode`, `summary`,
  `lastModifiedTime`, `fileSize`, `isExplicit`, `isRemote` — signals
- `usageData` — signal `{totalTokens, totalCost, contextWindow,
  maxOutputTokens}`
- `currentModelInfo`, `thinkingLevel`, `effortLevel`, `fastModeState`
  — signals
- `todos`, `permissionRequests`, `proactiveSuggestions`,
  `settingsErrors` — signals (arrays)

### Preact signals everywhere — even fields that look like primitives

All reactive state is `{value: T, peek(), subscribe(...)}`. To read:

```js
mgr.sessions.value           // subscribes if in reactive context (no-op outside React render)
mgr.sessions.peek()          // never subscribes
```

Forgetting to deref gives you the signal object itself, which serializes
to nonsense like `{$$typeof, type, props, ref}` — looks like a React
element, isn't.

**The equality trap.** Even fields that LOOK like primitives — `sessionId`,
`gitBranch`, `cwd`, etc. — are signal-wrapped on each session entry. Strict
equality against a string ALWAYS fails:

```js
const s = mgr.sessions.value[0];
typeof s.sessionId                              // "object", NOT "string"
s.sessionId.constructor.name                    // "$3" (Preact signal)
s.sessionId === "61974011-6e11-..."             // false — signal !== string
String(s.sessionId) === "61974011-6e11-..."     // true — toString derefs
s.sessionId.peek() === "61974011-6e11-..."      // true — explicit deref
```

So `mgr.sessions.value.find(s => s.sessionId === sid)` always returns
`undefined` even when sid IS in the list. Use `.find(s => s.sessionId.peek() === sid)`
or `.find(s => String(s.sessionId) === sid)`. Index access (`arr[0]`)
works because no comparison is involved — but if you find yourself
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

- `this.sessionStates: Map<sessionId, {sessionId, state, title}>` —
  the title cache. **Patch F's target.** `updateSessionState(V, K, B)`
  is the mutator (`V`=sessionId, `K`=state, `B`=title); the
  `/*pfg-v1.4*/` signature is just upstream of it. Pencil rename in
  sidebar updates this; without Patch F, the rename was applied to
  `sessionPanels[sid].title` but the `sessionStates` entry's title
  wasn't refreshed, so on session-switch the broadcast resent the stale
  title and the sidebar flipped back.
- `this.sessionPanels: Map<sessionId, WebviewPanel>` — VS Code
  WebviewPanel objects, one per open chat panel. `panel.title` is
  mutated alongside `sessionStates` on rename.
- `this.activeSessionId: string` — currently focused session.
- `this.allComms: Set<Comm>` — all live comm instances; iterated for
  broadcasts.
- `this.webviews: Map` — comm → webview lookup.

### Session-content manager

Populated by `Wz4` (the loader path).

- `this.sessionMessages: Map<sessionId, Set<uuid>>` — uuid set per
  session. Presence in this Map is the "session is loaded" check.
- `this.messages: Map<uuid, msg>` — flat uuid → message map across all
  loaded sessions.
- `this.summaries: Map<uuid, summary>` — compact summaries.
- `this.customTitles: Map<sessionId, string>` — title from
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
on `get_session_request` — empirically verified, every RPC invokes
`Wz4` directly through the `Qo` indirection. No intermediate cache
exists between `getSession` and `Wz4`.)

---

## Field shapes across the layers

Different layers carry different shapes. Mismatched-key lookups return
`undefined` silently — common waste pattern.

| Layer | Convention | Sample fields |
|---|---|---|
| Message TYPES (everywhere) | snake_case | `get_session_request`, `compact_boundary`, `tool_use`, `tool_result` |
| JSONL on disk | camelCase, **rich** | `parentUuid`, `logicalParentUuid`, `sessionId`, `compactMetadata`, `isMeta`, `isSidechain`, `cwd`, `gitBranch`, … |
| `Yz4` parse / `Wz4` internals / V passed to Ez4 | camelCase, rich | parse pass-through |
| `bz4` transformer (last step in `dl`) | snake_case, **lossy** | only emits `{type, uuid, session_id, message, parent_tool_use_id:null, timestamp}` — drops `parentUuid`, `logicalParentUuid`, `compactMetadata`, etc. |
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
  ASSEMBLER shape. `sessionId` and `session_id` are BOTH absent —
  session is implicit (the messages array hangs off the session entry).
- JSONL on disk via `require('fs').readFileSync` → camelCase, full
  JSONL shape (raw).

Common gotchas:

- Looking for `sessionId` or `session_id` on
  `__mgr...messages.peek()[N]` — neither is there. Owner-of-array, not
  field-on-element.
- Looking for `parentUuid` / `logicalParentUuid` on the React side —
  gone. The assembler discards them. To debug compaction-chain
  stitching, you MUST read at Ez4 (or earlier), not at the React side.
- Looking for `parent_tool_use_id` on the React side — renamed to
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
btn?.click();   // simple synthetic — works for most React onClick
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
// Modifiers bitfield: Alt=1, Ctrl=2, Meta=4, Shift=8 — OR them.
// Ctrl+Shift+P opens command palette:
await call('Input.dispatchKeyEvent', { type: 'rawKeyDown', modifiers: 10,
  key: 'P', code: 'KeyP', keyCode: 80, windowsVirtualKeyCode: 80 });
await call('Input.dispatchKeyEvent', { type: 'keyUp', modifiers: 10,
  key: 'P', code: 'KeyP', keyCode: 80, windowsVirtualKeyCode: 80 });
// Verify by checking if .quick-input-widget appeared in DOM.
```

Verified: opens the command palette ("Type the name of a command to
run.") and ESC closes it. Note: only works on real workbench pages —
the Antigravity Launchpad page has no command palette.

### 4. Direct method calls on the manager singleton

After capturing the manager via the BP-stash trick:

```js
globalThis.__mgr.updateSessionState(sid, "idle", "New Title");  // bypass RPC
globalThis.__mgr.broadcastSessionStates();                       // re-send to all webviews
```

Useful for fine-grained state tweaks. (To force a fresh load you don't
need to clear any cache — `get_session_request` already invokes `Wz4`
every call, verified empirically.)

### 5. The nuclear option

Edit `extension.js` on disk + `Developer: Reload Window`. The ONLY way
to truly hot-swap function implementations (since `setScriptSource`
doesn't, per the gotcha). Slow (~5–15s for the reload). This is what
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
  the JSONL file for *display* in the panel is exthost-side via `Wz4` —
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
  `extension.exports.openTabs()` via Runtime.evaluate — `tabsCount`
  returned correctly (function ran), but the marker stayed `undefined`.
- **Module-internal name resolution**: patched `Ez4` (called by name
  from `dl` inside the same IIFE) to set `globalThis.__ez4_marker++`.
  Triggered Wz4 via webview RPC (we know this calls Ez4 — separately
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
connection — even though the underlying script is the same. Re-fetch
every time. Caching `sid="368"` and reusing it after a new connection
silently sets BPs against a non-existent script and they never fire.

### Pure-pause BPs freeze the exthost

A BP without a condition pauses the exthost on hit. If the exthost is
yours (mine = target) you freeze yourself. If it's a target exthost,
you freeze user activity in that window — tabs, sidebar, everything.

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
no React app — you're in an isolated world. Pick the **main-world**
context: origin matches the page, name is empty (the inner-frame helper
script enforces this).

### `await import('fs')` fails in the exthost

Returns `ERR_VM_DYNAMIC_IMPORT_CALLBACK_MISSING`. V8's dynamic-import
callback isn't wired into the inspector eval context. Use the
synchronous `require('fs')` form via `includeCommandLineAPI: true`.

### vscode-webview is double-nested

`/json/list` shows the OUTER iframe. The actual React app is in a
nested `<iframe id="active-frame">`. The inner frame's URL also ends
in `index.html` (not `fake.html` as the outer's `src` suggests) —
trust the frame `name`, not the URL.

### Top-level `return` rejected in `Runtime.evaluate`

```
SyntaxError: Illegal return statement
```

Wrap the script in an IIFE: `(function(){ ...; return ...; })()`.

### Preact signals serialize as nonsense

`signal.value` and `signal.peek()` return the unwrapped value. The
signal object itself (a Preact internal) serializes to
`{$$typeof, type, props, ref, ...}` — looks like a React element, isn't.
Always deref before serializing.

### `extension.exports` is sparse

Only `activate`, `deactivate`, `openTabs`. The session manager and other
internal singletons are NOT reachable via `require.cache`. Use the
BP-stash trick.

### Inspector ports change on exthost restart

`Developer: Reload Window` restarts the exthost; ephemeral ports
reshuffle. `9229` itself can change hands — if the original
first-spawned window closes and another spawns, the new one captures
`9229`. Re-identify `mine` and `target` after any window-state change.

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

- Session **A** (`0727164e-...`) — Patch K's seam ghost renders
  correctly in the panel.
- Session **B** (`61974011-...`) — Patch K's seam ghost is absent
  from the panel.

Both sessions have the same patches loaded (signature `/*pfg-v1.4*/`
present in `extension.js`). Both panels are in the same exthost (same
window). What's different?

### Step 1: precheck patches are loaded

```sh
node /tmp/cdp-eval.mjs "$WS_TARGET" '(function(){
  var s = require("fs").readFileSync(
    "/home/juraj/.antigravity/extensions/anthropic.claude-code-2.1.126-linux-x64/extension.js","utf-8");
  return JSON.stringify({sig: ["/*pfg-v1.4*/"].filter(t=>s.includes(t)),
                         hasK: s.includes("Orphaned compaction pointer")});
})()'
// {"sig":["/*pfg-v1.4*/"], "hasK":true} — patches are live
```

### Step 2: simulate Ez4 on the broken session's JSONL outside the running extension

To verify Patch K is *supposed* to fire, replicate the loader chain
against Session B's JSONL on disk:

```js
// Read 61974011's JSONL, parse, walk for compact_boundaries with
// phantom lpu, simulate K's logic.
```

Result: simulator reports K should produce 1 seam ghost in Session B.
So K is firing logically — but the rendered messages don't contain
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

- `pfgkInV: ["pfgk-seam-32ff69a5"]` — the seam IS in the input. K
  inserted it correctly.
- `U` has 4 leaf-walk tips, INCLUDING `pfgk-seam-32` at idx 2375.
- `Zuuid: 9fc43c52-903` at idx **5059** — Ez4 picks Z by `max(B.get(t.uuid))`;
  the chain-B leaf at 5059 wins over the seam at 2375.
- `pfgkInH: []` — the rendered chain (walked back from Z at 5059)
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

### Mitigation (added in pfg-v1.4)

Once the empirical diagnosis was nailed, the fix was straightforward:
detect the unreachable scenario at K-time (signal: a seam was planted
but no bookend was — bookend fails to fire when the chain root is the
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
Patch K — it's the answer for any "rendered messages don't match
on-disk JSONL" question.
