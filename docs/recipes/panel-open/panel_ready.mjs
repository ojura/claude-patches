import http from 'node:http';
const WB=process.argv[2];
const NEEDLE=process.argv[3]||'';
const getJSON=p=>new Promise((res,rej)=>{http.get('http://127.0.0.1:9222'+p,r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>res(JSON.parse(d)));}).on('error',rej);});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function withWS(wsUrl,fn){return new Promise((resolve,reject)=>{const ws=new WebSocket(wsUrl);let nid=1;const pend=new Map();const ctx=[];ws.addEventListener('message',ev=>{const m=JSON.parse(ev.data);if(m.id&&pend.has(m.id)){const{res,rej}=pend.get(m.id);pend.delete(m.id);m.error?rej(m.error):res(m.result);}else if(m.method==='Runtime.executionContextCreated')ctx.push(m.params.context);});const call=(method,params={})=>{const id=nid++;return new Promise((res,rej)=>{pend.set(id,{res,rej});ws.send(JSON.stringify({id,method,params}));});};ws.addEventListener('open',async()=>{try{const r=await fn(call,ctx);ws.close();resolve(r);}catch(e){ws.close();reject(e);}});ws.addEventListener('error',e=>reject(e));setTimeout(()=>{try{ws.close();}catch(_){}reject(new Error('to'));},7000);});}
const wbEval=expr=>withWS(WB,async call=>{await call('Runtime.enable');const r=await call('Runtime.evaluate',{expression:expr,returnByValue:true});return r.result.value;});
async function inActive(wsUrl,expr){return withWS(wsUrl,async(call,ctx)=>{await call('Page.enable');await call('Runtime.enable');await sleep(600);const tree=await call('Page.getFrameTree');const fr=[];(function w(n){if(n.frame)fr.push(n.frame);(n.childFrames||[]).forEach(w);})(tree.frameTree);const inner=fr.find(f=>f.name==='active-frame');if(!inner)return {__noframe:1};const c=ctx.find(x=>x.auxData&&x.auxData.frameId===inner.id&&!x.name&&x.origin);if(!c)return {__noctx:1};const r=await call('Runtime.evaluate',{expression:expr,returnByValue:true,contextId:c.id});return r.result.value;}).catch(e=>({__err:String(e.message||e)}));}
let ready=false;
for(let i=0;i<20;i++){try{if(await wbEval(`document.querySelectorAll('.activitybar [aria-label="Claude Code"]').length`)>0){ready=true;break;}}catch(e){}await sleep(1000);}
if(!ready){console.log('WB_NOT_READY');process.exit(1);}
// Open the panel ONLY if not already open: the activity-bar click TOGGLES, so
// clicking it while the view is active CLOSES it. Probe for the panel first.
const findPanel=async()=>{
  for(const f of (await getJSON('/json/list')).filter(t=>t.type==='iframe'&&(t.url||'').includes('index'))){
    if(await inActive(f.webSocketDebuggerUrl,`!!document.querySelector('input[placeholder*="Search" i]')`)===true) return f.webSocketDebuggerUrl;
  }
  return null;
};
let panelWs=await findPanel();
if(!panelWs){
  await wbEval(`(function(){var t=document.querySelector('.activitybar .action-item a[aria-label="Claude Code"],.activitybar [aria-label="Claude Code"]');if(t)t.click();return !!t;})()`);
  await sleep(2000);
  for(let i=0;i<12 && !panelWs;i++){ panelWs=await findPanel(); if(!panelWs) await sleep(1200); }
}
if(!panelWs){console.log('PANEL_NOT_FOUND');process.exit(1);}
await inActive(panelWs,`(function(){var inp=document.querySelector('input.search,.filterInput_90gk3A,input[placeholder*="Search" i]');var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(inp,'');inp.dispatchEvent(new Event('input',{bubbles:true}));s.call(inp,${JSON.stringify(NEEDLE)});inp.dispatchEvent(new Event('input',{bubbles:true}));inp.focus();return inp.value;})()`);
await sleep(900);
console.log('PANEL_WS='+panelWs);
