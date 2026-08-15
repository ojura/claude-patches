#!/usr/bin/env python3
import json, os, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
BINARY=os.environ.get('CLAUDE_PRECOMPUTE_BINARY','/tmp/claude-2.1.220-force-precompute-always'); PORT=47673; SESSION='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; CATALOG=(Path(__file__).resolve().parents[2] / 'fixtures' / 'cliproxy-claude-models.json').read_bytes()
class S: requests=[]; cv=threading.Condition(); ptl=False
def sse(text,mid,model):
 es=[('message_start',{'type':'message_start','message':{'id':mid,'type':'message','role':'assistant','model':model,'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':0,'output_tokens':0}}}),('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}}),('content_block_stop',{'type':'content_block_stop','index':0}),('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}}),('message_stop',{'type':'message_stop'})]
 return ''.join(f'event: {n}\ndata: {json.dumps(d)}\n\n' for n,d in es).encode()
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def sendb(self,status,ct,data):self.send_response(status);self.send_header('Content-Type',ct);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):self.sendb(200,'application/json',CATALOG) if self.path.startswith('/v1/models') else self.sendb(404,'application/json',b'{}')
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length','0')));body=json.loads(raw or b'{}');text=json.dumps(body);compact='CRITICAL: Respond with TEXT ONLY' in text;title='Write the title in the predominant language' in text
  with S.cv:S.requests.append({'path':self.path,'model':body.get('model'),'compact':compact,'title':title,'body':body});S.cv.notify_all()
  if self.path.startswith('/v1/messages/count_tokens'):self.sendb(200,'application/json',b'{"input_tokens":1}');return
  if 'TRIGGER_PTL' in text and not compact and not title and not S.ptl:
   S.ptl=True;self.sendb(413,'application/json',json.dumps({'type':'error','error':{'type':'invalid_request_error','message':'prompt is too long: 400000 tokens > 372000 maximum'}}).encode());return
  marker='PRECOMPUTED_LUNA_SUMMARY' if compact else ('TITLE' if title else 'OK')
  self.sendb(200,'text/event-stream',sse(marker,f'msg_{len(S.requests)}',body.get('model','gpt-5.6-luna')))
def line(text):return json.dumps({'type':'user','message':{'role':'user','content':[{'type':'text','text':text}]},'parent_tool_use_id':None,'session_id':SESSION})+'\n'
def wait(pred,timeout=90):
 end=time.time()+timeout
 with S.cv:
  while not pred() and time.time()<end:S.cv.wait(end-time.time())
 return pred()
root=Path(tempfile.mkdtemp(prefix='claude-precompute-cross-model-'));cfg=root/'config';work=root/'work';cfg.mkdir();work.mkdir();env=os.environ.copy()
for k in ('CLAUDECODE','CLAUDE_CODE_CHILD_SESSION','CLAUDE_CODE_SESSION_ID','CLAUDE_PID','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS'):env.pop(k,None)
env.update({'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{PORT}','ANTHROPIC_API_KEY':'test','ANTHROPIC_MODEL':'gpt-5.6-luna','CLAUDE_CONFIG_DIR':str(cfg),'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS':'1','CLAUDE_CODE_DISABLE_FAST_MODE':'1','CLAUDE_CODE_MAX_CONTEXT_TOKENS':'372000','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'128000'})
srv=ThreadingHTTPServer(('127.0.0.1',PORT),H);threading.Thread(target=srv.serve_forever,daemon=True).start()
try:
 cmd=[BINARY,'-p','--safe-mode','--model','gpt-5.6-luna','--effort','low','--input-format','stream-json','--output-format','stream-json','--replay-user-messages','--verbose','--session-id',SESSION,'--permission-mode','dontAsk','--tools','','--settings','{"autoCompactEnabled":true,"precomputeCompactionEnabled":true}']
 p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,cwd=work)
 p.stdin.write(line('WARMUP').encode());p.stdin.flush();wait(lambda:any('WARMUP' in json.dumps(r['body']) and not r['title'] for r in S.requests));time.sleep(.5)
 p.stdin.write(line('SECOND').encode());p.stdin.flush();wait(lambda:any('SECOND' in json.dumps(r['body']) and not r['title'] for r in S.requests));
 ready=wait(lambda:any(r['compact'] for r in S.requests),120);time.sleep(.5)
 ctrl=json.dumps({'type':'control_request','request_id':'model-1','request':{'subtype':'set_model','model':'gpt-5.6-terra'}})+'\n';p.stdin.write(ctrl.encode());p.stdin.flush();time.sleep(1)
 p.stdin.write(line('TRIGGER_PTL').encode());p.stdin.flush();retry=wait(lambda:sum('TRIGGER_PTL' in json.dumps(r['body']) and not r['compact'] and not r['title'] for r in S.requests)>=2,120);time.sleep(1)
 p.stdin.close();p.wait(timeout=120)
 msgs=[r for r in S.requests if r['path'].startswith('/v1/messages')];tr=[r for r in msgs if 'TRIGGER_PTL' in json.dumps(r['body']) and not r['compact'] and not r['title']]
 result={'precompute_ready_request':ready,'ptl_sent':S.ptl,'retry_seen':retry,'models':[r['model'] for r in msgs],'compact_count':sum(r['compact'] for r in msgs),'trigger_request_count':len(tr),'trigger_models':[r['model'] for r in tr],'retry_contains_luna_summary':len(tr)>1 and 'PRECOMPUTED_LUNA_SUMMARY' in json.dumps(tr[-1]['body']),'retry_body_bytes':len(json.dumps(tr[-1]['body'])) if tr else 0,'root':str(root)}
 Path('/tmp/claude-precompute-cross-model-result.json').write_text(json.dumps(result,indent=2));Path('/tmp/claude-precompute-cross-model-requests.json').write_text(json.dumps(S.requests,indent=2));print(json.dumps(result,indent=2))
finally:srv.shutdown();srv.server_close()
