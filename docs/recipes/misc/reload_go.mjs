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
