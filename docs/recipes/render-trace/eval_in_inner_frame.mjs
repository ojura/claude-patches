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
