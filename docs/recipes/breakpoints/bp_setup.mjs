// bp_setup.mjs <exthost-ws-url> <hold-seconds>
// Side-effect BP on the Patch-K loader return; captures _parsed's planted
// ghosts to globalThis.__l4caps without pausing. Holds the WS open so the BP
// survives across the trigger+read window.
const wsUrl = process.argv[2];
const hold = (parseInt(process.argv[3]) || 25) * 1000;
const ws = new WebSocket(wsUrl);
let nextId = 1; const pending = new Map(); const scripts = [];
const sleep = ms => new Promise(r => setTimeout(r, ms));
ws.addEventListener('message', ev => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { const { res, rej } = pending.get(msg.id); pending.delete(msg.id); msg.error ? rej(msg.error) : res(msg.result); }
  else if (msg.method === 'Debugger.scriptParsed' && (msg.params.url || '').includes('extension.js')) scripts.push(msg.params);
});
function call(method, params = {}) { const id = nextId++; return new Promise((res, rej) => { pending.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params })); }); }
await new Promise(r => ws.addEventListener('open', r));
await call('Debugger.enable');
await sleep(5000); // drain scriptParsed
const SUB = 'return Xa(_parsed,V,_pfgkTel)';
let tgt = null;
for (const s of scripts) {
  try { const r = await call('Debugger.getScriptSource', { scriptId: s.scriptId }); if (r.scriptSource && r.scriptSource.includes(SUB)) { tgt = { scriptId: s.scriptId, src: r.scriptSource }; break; } } catch (e) {}
}
if (!tgt) { console.log('SCRIPT_NOT_FOUND n=' + scripts.length); ws.close(); process.exit(1); }
const i = tgt.src.indexOf(SUB);
let line = 0, col = 0; for (let k = 0; k < i; k++) { if (tgt.src[k] === '\n') { line++; col = 0; } else col++; }
const cond = '((function(){try{(globalThis.__l4caps=globalThis.__l4caps||[]).push({n:_parsed.length,ghosts:_parsed.filter(function(m){return String(m&&m.uuid).indexOf("pfgk-")===0}).map(function(m){return String(m.uuid).replace(/^(pfgk-[a-z]+)-.*/,"$1")+"<-"+String(m.logicalParentUuid).slice(0,8)}),nonce:JSON.stringify(_parsed).indexOf("PFGKL4bc512c4282")>=0});}catch(e){(globalThis.__l4err=globalThis.__l4err||[]).push(String(e))}return false})())';
const bp = await call('Debugger.setBreakpoint', { location: { scriptId: tgt.scriptId, lineNumber: line, columnNumber: col }, condition: cond });
console.log('BP_SET line=' + line + ' actual=' + JSON.stringify(bp.actualLocation || {}));
await sleep(hold);
ws.close();
