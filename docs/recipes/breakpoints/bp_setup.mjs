// bp_setup.mjs <ws-url> <hold-seconds> <target-substring> <condition|@file> [url-filter]
//
// General non-pausing side-effect breakpoint (docs/debugging.md Recipe 1).
// Locates <target-substring> in a parsed script whose url contains <url-filter>
// (default "extension.js"; pass "index.js" for the webview), converts the byte
// offset to line/col, and sets a BP whose <condition> V8-evaluates in the live
// frame's scope at each hit. The condition must mutate globalThis and end by
// returning false, so the BP captures locals WITHOUT pausing the process. Holds
// the WS open <hold-seconds> so the BP survives the trigger+read window.
//
// <condition> is taken literally, or read from a file when prefixed with "@"
// (use a file for anything with shell-hostile quoting). Example:
//   node bp_setup.mjs "$WS" 30 'if(!_root){let _bkPh' '@/tmp/cond.js'
// where /tmp/cond.js is:  ((function(){try{(globalThis.__caps=globalThis.__caps||[]).push({root:_root});}catch(e){}return false})())
import { readFileSync } from 'node:fs';
const wsUrl = process.argv[2];
const hold = (parseInt(process.argv[3]) || 25) * 1000;
const SUB = process.argv[4];
let COND = process.argv[5] || '';
const URLF = process.argv[6] || 'extension.js';
if (!wsUrl || !SUB || !COND) {
  console.log('usage: bp_setup.mjs <ws-url> <hold-seconds> <target-substring> <condition|@file> [url-filter]');
  process.exit(2);
}
if (COND.startsWith('@')) COND = readFileSync(COND.slice(1), 'utf8').trim();
const ws = new WebSocket(wsUrl);
let nextId = 1; const pending = new Map(); const scripts = [];
const sleep = ms => new Promise(r => setTimeout(r, ms));
ws.addEventListener('message', ev => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { const { res, rej } = pending.get(msg.id); pending.delete(msg.id); msg.error ? rej(msg.error) : res(msg.result); }
  else if (msg.method === 'Debugger.scriptParsed' && (msg.params.url || '').includes(URLF)) scripts.push(msg.params);
});
function call(method, params = {}) { const id = nextId++; return new Promise((res, rej) => { pending.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params })); }); }
await new Promise(r => ws.addEventListener('open', r));
await call('Debugger.enable');
await sleep(5000); // drain scriptParsed
let tgt = null;
for (const s of scripts) {
  try { const r = await call('Debugger.getScriptSource', { scriptId: s.scriptId }); if (r.scriptSource && r.scriptSource.includes(SUB)) { tgt = { scriptId: s.scriptId, src: r.scriptSource }; break; } } catch (e) {}
}
if (!tgt) { console.log('SCRIPT_NOT_FOUND (substring absent) scripts_scanned=' + scripts.length + ' url-filter=' + URLF); ws.close(); process.exit(1); }
const i = tgt.src.indexOf(SUB);
let line = 0, col = 0; for (let k = 0; k < i; k++) { if (tgt.src[k] === '\n') { line++; col = 0; } else col++; }
const bp = await call('Debugger.setBreakpoint', { location: { scriptId: tgt.scriptId, lineNumber: line, columnNumber: col }, condition: COND });
console.log('BP_SET line=' + line + ' col=' + col + ' actual=' + JSON.stringify(bp.actualLocation || {}));
await sleep(hold);
ws.close();
