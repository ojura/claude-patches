import http from 'node:http';
import fs from 'node:fs';
const extract=`(function(){var els=document.querySelectorAll('[data-pfgk-role]');var seen={},out=[];for(var i=0;i<els.length;i++){var e=els[i],role=e.getAttribute('data-pfgk-role'),svg=e.querySelector('svg'),t=svg?svg.outerHTML:'';var cap=(t.match(/(in-file reattach|in-file link|cross-file link)/)||[''])[0];var key=role+'|'+cap;if(!seen[key]){seen[key]=1;out.push({role:role,cap:cap,svg:t});}}return out;})()`;
const getJSON=p=>new Promise((res,rej)=>{http.get('http://localhost:9222'+p,r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>res(JSON.parse(d)));}).on('error',rej);});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function withWS(wsUrl,fn){return new Promise((resolve,reject)=>{const ws=new WebSocket(wsUrl);let nid=1;const pend=new Map();const ctx=[];ws.addEventListener('message',ev=>{const m=JSON.parse(ev.data);if(m.id&&pend.has(m.id)){const{res,rej}=pend.get(m.id);pend.delete(m.id);m.error?rej(m.error):res(m.result);}else if(m.method==='Runtime.executionContextCreated')ctx.push(m.params.context);});const call=(method,params={})=>{const id=nid++;return new Promise((res,rej)=>{pend.set(id,{res,rej});ws.send(JSON.stringify({id,method,params}));});};ws.addEventListener('open',async()=>{try{const r=await fn(call,ctx);ws.close();resolve(r);}catch(e){ws.close();reject(e);}});ws.addEventListener('error',e=>reject(e));setTimeout(()=>{try{ws.close();}catch(_){}reject(new Error('ws timeout'));},8000);});}
const list=await getJSON('/json/list');
const ifs=list.filter(t=>t.type==='iframe'&&(t.url||'').includes('index'));
let arr=[];const _seen={};
for(const wv of ifs){
  const r=await withWS(wv.webSocketDebuggerUrl,async(call,ctx)=>{
    await call('Page.enable');await call('Runtime.enable');await sleep(700);
    const tree=await call('Page.getFrameTree');const frames=[];(function w(n){if(n.frame)frames.push(n.frame);(n.childFrames||[]).forEach(w);})(tree.frameTree);
    const inner=frames.find(f=>f.name==='active-frame');if(!inner)return [];
    const c=ctx.find(x=>x.auxData&&x.auxData.frameId===inner.id&&!x.name&&x.origin);if(!c)return [];
    const r=await call('Runtime.evaluate',{expression:extract,returnByValue:true,contextId:c.id});
    return r.result.value||[];
  }).catch(()=>[]);
  for(const it of (r||[])){const k=it.role+'|'+(it.cap||'');if(!_seen[k]){_seen[k]=1;arr.push(it);}}
}
const bgs={bookend:'#142a35',broken:'#3a1818',seam:'#3a2c14',seamClean:'#181d28',bridge:'#3a2418'};
let html='<html><body style="margin:0;background:#0a0a0c;font-family:monospace">';
for(const it of arr){
  let bg=it.cap==='in-file link'?bgs.seamClean:it.cap==='cross-file link'?bgs.bridge:it.cap==='in-file reattach'?bgs.seam:(bgs[it.role]||'#222');
  let svg=it.svg.replace('width:100%;height:124px','width:860px;height:auto');
  html+='<div style="color:#999;padding:11px 24px 3px;font-size:13px">live: '+it.role+' / '+(it.cap||'(no caption)')+'</div><div style="background:'+bg+';margin:0 24px 14px;padding:16px;border-radius:8px">'+svg+'</div>';
}
html+='</body></html>';
fs.writeFileSync('/tmp/live_extracted.html',html);
console.log('extracted '+arr.length+' unique live SVGs: '+arr.map(a=>a.role+'/'+(a.cap||'-')).join(', '));
