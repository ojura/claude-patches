// Set BP at Ez4 RETURN with side-effect that captures V/U/Z/H to a global ring buffer.
const wsUrl = process.argv[2];
const holdSecs = parseInt(process.argv[3] || '30', 10);
const ws = new WebSocket(wsUrl);
let nextId=1, pending=new Map();
const events=[];
ws.addEventListener('message', ev=>{
  const m=JSON.parse(ev.data);
  if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);if(m.error)p.reject(m.error);else p.resolve(m.result);}
  else if(m.method==='Debugger.scriptParsed') events.push(m);
});
function call(method,params={}){const id=nextId++;return new Promise((resolve,reject)=>{pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});}
await new Promise(r=>ws.addEventListener('open',r));
await call('Debugger.enable');
await call('Runtime.enable');
await new Promise(r=>setTimeout(r,5000));
const ext=events.find(e=>e.params.url?.endsWith('extension.js'));
const sid=String(ext.params.scriptId);
console.log('scriptId:', sid);

// Find Ez4 return site: search for 'return H.reverse(),Pz4(K,H,D)}'
const src = (await call('Debugger.getScriptSource',{scriptId:sid})).scriptSource;
const target = 'return H.reverse(),Pz4(K,H,D)';
const i = src.indexOf(target);
let line=0, col=0;
for(let k=0;k<i;k++){ if(src[k]==='\n'){line++;col=0;} else col++; }
console.log('Ez4 return at byte', i, '→ line/col:', line, col);

// Reset capture buffer
await call('Runtime.evaluate', { expression: 'globalThis.__ez4Caps = []', returnByValue:true, includeCommandLineAPI:true });

// Build the condition: capture V/U/Z/H minimally then return false (no pause)
const condition = `((function(){
  try {
    var cap = {
      ts: Date.now(),
      Vlen: V.length,
      Glen: G.length,
      Ulen: U.length,
      U: U.slice(0,40).map(function(t){return {u:String(t.uuid).slice(0,12), i: B.get(t.uuid), t: t.type}}),
      Zuuid: Z ? String(Z.uuid).slice(0,12) : null,
      Zidx: Z ? B.get(Z.uuid) : null,
      Hlen: H.length,
      pfgkInH: H.filter(function(m){return String(m.uuid).startsWith('pfgk-')}).map(function(m){return String(m.uuid).slice(0,20)}),
      pfgkInV: V.filter(function(m){return String(m.uuid).startsWith('pfgk-')}).map(function(m){return String(m.uuid).slice(0,20)}),
      sessionIds: Array.from(new Set(V.slice(0,200).map(function(m){return m.sessionId}).filter(Boolean))).slice(0,5)
    };
    (globalThis.__ez4Caps = globalThis.__ez4Caps || []).push(cap);
  } catch(e) { (globalThis.__ez4CapsErr = globalThis.__ez4CapsErr || []).push(String(e)); }
  return false;
})())`;

const r = await call('Debugger.setBreakpoint', {
  location: { scriptId: sid, lineNumber: line, columnNumber: col },
  condition,
});
console.log('BP set:', JSON.stringify(r));

console.log(`waiting ${holdSecs}s: trigger an Ez4 invocation (close+reopen panel or send webview RPC)...`);
await new Promise(r => setTimeout(r, holdSecs*1000));

const final = await call('Runtime.evaluate', {
  expression: 'JSON.stringify({caps: globalThis.__ez4Caps||[], errs: globalThis.__ez4CapsErr||[]})',
  returnByValue:true, includeCommandLineAPI:true
});
console.log('CAPTURED:', final.result.value);
ws.close();
