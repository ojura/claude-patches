// select_tab.mjs <wb-ws> <needle>
// Real-mouse-click the editor tab whose label contains <needle>, to SELECT it.
// The big session tab renders LAZILY on selection, so this is the render trigger.
const ws = new WebSocket(process.argv[2]); const NEEDLE = process.argv[3] || '';
let id = 1; const pend = new Map();
const sleep = ms => new Promise(r => setTimeout(r, ms));
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); } });
const call = (method, params = {}) => new Promise(r => { const i = id++; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
await new Promise(r => ws.addEventListener('open', r));
await call('Runtime.enable');
const v = (await call('Runtime.evaluate', { expression:
  `(()=>{const tabs=[...document.querySelectorAll('.tabs-container .tab')];`
  + `const t=tabs.find(x=>((x.getAttribute('aria-label')||x.textContent||'')).includes(${JSON.stringify(NEEDLE)}));`
  + `if(!t)return null;const r=t.getBoundingClientRect();`
  + `return JSON.stringify({x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2),label:(t.getAttribute('aria-label')||t.textContent||'').trim().slice(0,45)});})()`,
  returnByValue: true })).result.value;
if (!v) { console.log('NO_TAB'); ws.close(); process.exit(1); }
const r = JSON.parse(v);
await call('Input.dispatchMouseEvent', { type: 'mousePressed', x: r.x, y: r.y, button: 'left', clickCount: 1 });
await call('Input.dispatchMouseEvent', { type: 'mouseReleased', x: r.x, y: r.y, button: 'left', clickCount: 1 });
console.log('SELECTED tab:', r.label, '@', r.x + ',' + r.y);
await sleep(300); ws.close();
