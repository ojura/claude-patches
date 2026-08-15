#!/usr/bin/env python3
import json, os, signal, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
BINARY='/home/juraj/.local/share/claude/versions/2.1.220'; PORT=int(os.environ.get('PROBE_PORT','47667')); SESSION=os.environ.get('PROBE_SESSION','88888888-8888-4888-8888-888888888888'); KILL_DELAY_MS=int(os.environ.get('KILL_DELAY_MS','0')); CATALOG=(Path(__file__).resolve().parents[2] / 'fixtures' / 'cliproxy-claude-models.json').read_bytes()
class S: requests=[]; new_seen=threading.Event(); release=threading.Event(); mode='run'
def sse(text,mid):
 es=[('message_start',{'type':'message_start','message':{'id':mid,'type':'message','role':'assistant','model':'gpt-5.6-luna','content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':0,'output_tokens':0}}}),('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}}),('content_block_stop',{'type':'content_block_stop','index':0}),('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}}),('message_stop',{'type':'message_stop'})]
 return ''.join(f'event: {n}\ndata: {json.dumps(d)}\n\n' for n,d in es).encode()
class H(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def sendb(self,status,ct,data): self.send_response(status); self.send_header('Content-Type',ct); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
 def do_GET(self): self.sendb(200,'application/json',CATALOG) if self.path.startswith('/v1/models') else self.sendb(404,'application/json',b'{}')
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length','0'))); body=json.loads(raw or b'{}'); text=json.dumps(body); S.requests.append({'mode':S.mode,'path':self.path,'body':body})
  if self.path.startswith('/v1/messages/count_tokens'): self.sendb(200,'application/json',b'{"input_tokens":1}'); return
  if 'NEW_BRANCH' in text and S.mode=='run': S.new_seen.set(); S.release.wait(30)
  self.sendb(200,'text/event-stream',sse('OK_'+str(len(S.requests)),f'msg_{len(S.requests)}'))
def line(text): return json.dumps({'type':'user','message':{'role':'user','content':[{'type':'text','text':text}]},'parent_tool_use_id':None,'session_id':SESSION})+'\n'
def env(cfg):
 e=os.environ.copy()
 for k in ('CLAUDECODE','CLAUDE_CODE_CHILD_SESSION','CLAUDE_CODE_SESSION_ID','CLAUDE_PID','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS'): e.pop(k,None)
 e.update({'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{PORT}','ANTHROPIC_API_KEY':'test','ANTHROPIC_MODEL':'gpt-5.6-luna','CLAUDE_CONFIG_DIR':str(cfg),'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS':'1','CLAUDE_CODE_DISABLE_FAST_MODE':'1','CLAUDE_CODE_MAX_CONTEXT_TOKENS':'372000','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'128000'})
 return e
def args(): return [BINARY,'-p','--safe-mode','--model','gpt-5.6-luna','--effort','low','--input-format','stream-json','--output-format','stream-json','--replay-user-messages','--verbose','--session-id',SESSION,'--permission-mode','dontAsk','--tools','','--settings','{"autoCompactEnabled":true}']
root=Path(tempfile.mkdtemp(prefix='claude-rewind-successor-')); cfg=root/'config'; work=root/'work'; cfg.mkdir(); work.mkdir(); srv=ThreadingHTTPServer(('127.0.0.1',PORT),H); threading.Thread(target=srv.serve_forever,daemon=True).start()
try:
 p=subprocess.Popen(args(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env(cfg),cwd=work,start_new_session=True)
 for text in ('FIRST','SECOND','THIRD'):
  p.stdin.write(line(text).encode()); p.stdin.flush();
  deadline=time.time()+30
  while time.time()<deadline:
   main=[r for r in S.requests if r['path'].startswith('/v1/messages') and text in json.dumps(r['body'])]
   if main: break
   time.sleep(.05)
  time.sleep(.4)
 jsonl=next(cfg.rglob('*.jsonl'))
 entries=[json.loads(x) for x in jsonl.read_text(errors='replace').splitlines() if x.strip().startswith('{')]
 third=next(e for e in entries if e.get('type')=='user' and 'THIRD' in json.dumps(e.get('message')))
 ctrl=json.dumps({'type':'control_request','request_id':'rewind-1','request':{'subtype':'rewind_conversation','target_message_uuid':third['uuid']}})+'\n'
 p.stdin.write(ctrl.encode()); p.stdin.flush(); time.sleep(1)
 p.stdin.write(line('NEW_BRANCH').encode()); p.stdin.flush()
 if not S.new_seen.wait(30): raise RuntimeError('new branch request not seen')
 if KILL_DELAY_MS: time.sleep(KILL_DELAY_MS/1000)
 os.killpg(p.pid,signal.SIGKILL); p.wait(timeout=10); S.release.set(); time.sleep(.5)
 S.mode='resume'
 q=subprocess.run([BINARY,'-p','--safe-mode','--model','gpt-5.6-luna','--effort','low','--resume',SESSION,'AFTER_RESTART','--permission-mode','dontAsk','--tools','','--settings','{"autoCompactEnabled":true}'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env(cfg),cwd=work,timeout=90)
 resume=[r for r in S.requests if r['mode']=='resume' and r['path'].startswith('/v1/messages')]
 rt=json.dumps(resume[-1]['body']) if resume else ''
 final_text=jsonl.read_text(errors='replace')
 result={'target_uuid':third['uuid'],'new_request_seen':True,'resume_rc':q.returncode,'resume_has_NEW_BRANCH':'NEW_BRANCH' in rt,'resume_has_THIRD':'THIRD' in rt,'resume_has_SECOND':'SECOND' in rt,'jsonl_has_NEW_BRANCH':'NEW_BRANCH' in final_text,'jsonl_last_prompt_lines':[x for x in final_text.splitlines() if '"type":"last-prompt"' in x][-4:],'resume_body_bytes':len(rt),'root':str(root),'resume_tail':q.stdout.decode('utf-8','replace')[-800:]}
 Path('/tmp/claude-rewind-successor-result.json').write_text(json.dumps(result,indent=2)); Path('/tmp/claude-rewind-successor-requests.json').write_text(json.dumps(S.requests,indent=2)); print(json.dumps(result,indent=2))
finally: S.release.set(); srv.shutdown(); srv.server_close()
