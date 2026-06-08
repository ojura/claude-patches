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
