const ws=new WebSocket(process.argv[2]);          // ws://127.0.0.1:9222/devtools/page/<workbench-page-id>
let id=1;const p=new Map();
ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id&&p.has(m.id)){p.get(m.id)(m.result);p.delete(m.id);}});
const call=(method,params={})=>new Promise(r=>{const i=id++;p.set(i,r);ws.send(JSON.stringify({id:i,method,params}));});
await new Promise(r=>ws.addEventListener('open',r));
await call('Page.enable');
const sc=await call('Page.captureScreenshot',{format:'png'});
const fs=await import('node:fs');fs.writeFileSync('/tmp/wb_now.png',Buffer.from(sc.data,'base64'));
console.log('saved /tmp/wb_now.png');ws.close();
