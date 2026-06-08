// reopen.mjs <wb-ws>  -- focus the workbench, then Ctrl+Shift+T (reopenClosedEditor)
const ws = new WebSocket(process.argv[2]); let id = 1; const p = new Map(); const sleep = ms => new Promise(r => setTimeout(r, ms));
ws.addEventListener('message', e => { const m = JSON.parse(e.data); if (m.id && p.has(m.id)) { p.get(m.id)(m.result); p.delete(m.id); } });
const call = (meth, par = {}) => new Promise(r => { const i = id++; p.set(i, r); ws.send(JSON.stringify({ id: i, method: meth, params: par })); });
await new Promise(r => ws.addEventListener('open', r));
await call('Runtime.enable');
const rc = JSON.parse((await call('Runtime.evaluate', { expression: '(()=>{const s=document.querySelector(".part.titlebar")||document.querySelector(".statusbar");const r=s?s.getBoundingClientRect():{left:300,top:6,width:0,height:0};return JSON.stringify({x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)})})()', returnByValue: true })).result.value);
await call('Input.dispatchMouseEvent', { type: 'mousePressed', x: rc.x, y: rc.y, button: 'left', clickCount: 1 });
await call('Input.dispatchMouseEvent', { type: 'mouseReleased', x: rc.x, y: rc.y, button: 'left', clickCount: 1 });
await sleep(150);
await call('Input.dispatchKeyEvent', { type: 'rawKeyDown', modifiers: 10, key: 'T', code: 'KeyT', keyCode: 84, windowsVirtualKeyCode: 84 });
await call('Input.dispatchKeyEvent', { type: 'keyUp', modifiers: 10, key: 'T', code: 'KeyT', keyCode: 84, windowsVirtualKeyCode: 84 });
console.log('REOPEN_DISPATCHED');
await sleep(300); ws.close();
