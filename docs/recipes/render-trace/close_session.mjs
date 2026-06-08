// close_session.mjs <wb-ws> <needle>  -- close the editor tab whose label contains <needle> (destroys its webview)
const ws = new WebSocket(process.argv[2]); const NEEDLE = process.argv[3] || '';
let id = 1; const p = new Map(); const sleep = ms => new Promise(r => setTimeout(r, ms));
ws.addEventListener('message', e => { const m = JSON.parse(e.data); if (m.id && p.has(m.id)) { p.get(m.id)(m.result); p.delete(m.id); } });
const call = (meth, par = {}) => new Promise(r => { const i = id++; p.set(i, r); ws.send(JSON.stringify({ id: i, method: meth, params: par })); });
await new Promise(r => ws.addEventListener('open', r));
await call('Runtime.enable');
const v = (await call('Runtime.evaluate', { expression:
  `(()=>{const tabs=[...document.querySelectorAll('.tabs-container .tab')];`
  + `const t=tabs.find(x=>((x.getAttribute('aria-label')||x.textContent||'')).includes(${JSON.stringify(NEEDLE)}));`
  + `if(!t)return 'NO_TAB';const c=t.querySelector('.codicon-close')||t.querySelector('.tab-actions .action-label');`
  + `if(!c)return 'NO_CLOSE';c.click();return 'CLOSED:'+((t.getAttribute('aria-label')||t.textContent||'').trim().slice(0,40));})()`,
  returnByValue: true })).result.value;
console.log('close:', v);
await sleep(500); ws.close();
