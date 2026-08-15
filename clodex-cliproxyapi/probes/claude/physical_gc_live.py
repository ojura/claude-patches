#!/usr/bin/env python3
import json, os, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
BINARY='/home/juraj/.local/share/claude/versions/2.1.220'; PORT=47672; SESSION='99999999-9999-4999-8999-999999999999'; CATALOG=(Path(__file__).resolve().parents[2] / 'fixtures' / 'cliproxy-claude-models.json').read_bytes()
class S: requests=[]; cv=threading.Condition()
def sse(text,mid):
 es=[('message_start',{'type':'message_start','message':{'id':mid,'type':'message','role':'assistant','model':'gpt-5.6-luna','content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':0,'output_tokens':0}}}),('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}}),('content_block_stop',{'type':'content_block_stop','index':0}),('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}}),('message_stop',{'type':'message_stop'})]
 return ''.join(f'event: {n}\ndata: {json.dumps(d)}\n\n' for n,d in es).encode()
class H(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def sendb(self,status,ct,data): self.send_response(status); self.send_header('Content-Type',ct); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
 def do_GET(self): self.sendb(200,'application/json',CATALOG) if self.path.startswith('/v1/models') else self.sendb(404,'application/json',b'{}')
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length','0'))); body=json.loads(raw or b'{}'); text=json.dumps(body).lower(); compact='critical: respond with text only' in text
  with S.cv:S.requests.append({'path':self.path,'compact':compact,'body_bytes':len(raw)}); S.cv.notify_all()
  if self.path.startswith('/v1/messages/count_tokens'): self.sendb(200,'application/json',b'{"input_tokens":1}'); return
  self.sendb(200,'text/event-stream',sse('GC_SUMMARY' if compact else 'OK',f'msg_{len(S.requests)}'))
def line(text):return json.dumps({'type':'user','message':{'role':'user','content':[{'type':'text','text':text}]},'parent_tool_use_id':None,'session_id':SESSION})+'\n'
def wait_compact(timeout=300):
 end=time.time()+timeout
 with S.cv:
  while not any(r['compact'] for r in S.requests) and time.time()<end:S.cv.wait(end-time.time())
 return any(r['compact'] for r in S.requests)
root=Path(tempfile.mkdtemp(prefix='claude-gc-live-')); cfg=root/'config'; work=root/'work'; cfg.mkdir(); work.mkdir(); env=os.environ.copy()
for k in ('CLAUDECODE','CLAUDE_CODE_CHILD_SESSION','CLAUDE_CODE_SESSION_ID','CLAUDE_PID','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS'):env.pop(k,None)
env.update({'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{PORT}','ANTHROPIC_API_KEY':'test','ANTHROPIC_MODEL':'gpt-5.6-luna','CLAUDE_CONFIG_DIR':str(cfg),'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS':'1','CLAUDE_CODE_DISABLE_FAST_MODE':'1','CLAUDE_CODE_MAX_CONTEXT_TOKENS':'372000','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'128000','CLAUDE_CODE_TRANSCRIPT_LOCAL_GC':'true'})
srv=ThreadingHTTPServer(('127.0.0.1',PORT),H);threading.Thread(target=srv.serve_forever,daemon=True).start()
try:
 cmd=[BINARY,'-p','--safe-mode','--model','gpt-5.6-luna','--effort','low','--input-format','stream-json','--output-format','stream-json','--verbose','--session-id',SESSION,'--permission-mode','dontAsk','--tools','','--settings','{"autoCompactEnabled":true}']
 p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,cwd=work)
 p.stdin.write((line('one')+line('x '*3000000+' finish')).encode());p.stdin.flush(); compact=wait_compact();time.sleep(8)
 jsonl=next(cfg.rglob('*.jsonl')); before={'bytes':jsonl.stat().st_size,'raw':'x '*1000 in jsonl.read_text(errors='replace')}
 p.stdin.write(line('KEEPALIVE').encode());p.stdin.flush();time.sleep(3); after={'bytes':jsonl.stat().st_size,'raw':'x '*1000 in jsonl.read_text(errors='replace')}
 p.stdin.close();p.wait(timeout=120)
 result={'compact_seen':compact,'before':before,'after':after,'requests':S.requests,'root':str(root)};Path('/tmp/claude-physical-gc-live-result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
finally:srv.shutdown();srv.server_close()
