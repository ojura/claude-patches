#!/usr/bin/env python3
import json, os, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pexpect

BINARY=os.environ.get('CLAUDE_PHYSICAL_GC_BINARY','/tmp/claude-2.1.220-force-physical-gc-v2'); PORT=47676; SESSION='44444444-4444-4444-8444-444444444444'; CATALOG=(Path(__file__).resolve().parents[2] / 'fixtures' / 'cliproxy-claude-models.json').read_bytes()
class S: requests=[]; cv=threading.Condition()
def sse(text,mid):
 ev=[('message_start',{'type':'message_start','message':{'id':mid,'type':'message','role':'assistant','model':'gpt-5.6-luna','content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':0,'output_tokens':0}}}),('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}}),('content_block_stop',{'type':'content_block_stop','index':0}),('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}}),('message_stop',{'type':'message_stop'})]
 return ''.join(f'event: {n}\ndata: {json.dumps(d)}\n\n' for n,d in ev).encode()
class H(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def sendb(self,status,ct,data): self.send_response(status); self.send_header('Content-Type',ct); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
 def do_GET(self): self.sendb(200,'application/json',CATALOG) if self.path.startswith('/v1/models') else self.sendb(404,'application/json',b'{}')
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length','0'))); body=json.loads(raw or b'{}')
  with S.cv: S.requests.append({'path':self.path,'header':self.headers.get('X-Claude-Code-Session-Id'),'body':body}); S.cv.notify_all()
  if self.path.startswith('/v1/messages/count_tokens'): self.sendb(200,'application/json',b'{"input_tokens":1}'); return
  rawtext=json.dumps(body).lower(); compact=any(x in rawtext for x in ['compact_boundary','conversation summary','summarize the conversation','detailed summary'])
  self.sendb(200,'text/event-stream',sse('COMPACT_SUMMARY_MARKER' if compact else 'OK',f'msg_{len(S.requests)}'))
def wait_requests(n,timeout=30):
 end=time.time()+timeout
 with S.cv:
  while len([r for r in S.requests if r['path'].startswith('/v1/messages')])<n and time.time()<end: S.cv.wait(end-time.time())
 return len([r for r in S.requests if r['path'].startswith('/v1/messages')])>=n
root=Path(tempfile.mkdtemp(prefix='claude-interactive-compact-')); cfg=root/'config'; cfg.mkdir();
(cfg/'.claude.json').write_text(json.dumps({'hasCompletedOnboarding':True,'theme':'dark','lastOnboardingVersion':'2.1.220','projects':{'/tmp':{'hasTrustDialogAccepted':True,'hasCompletedProjectOnboarding':True,'projectOnboardingSeenCount':1}}}))
(cfg/'settings.json').write_text(json.dumps({'autoCompactEnabled':True,'theme':'dark','syntaxHighlightingDisabled':True}))
env=os.environ.copy()
for k in ('CLAUDECODE','CLAUDE_CODE_CHILD_SESSION','CLAUDE_CODE_SESSION_ID','CLAUDE_PID','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS'): env.pop(k,None)
env.update({'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{PORT}','ANTHROPIC_API_KEY':'test','ANTHROPIC_MODEL':'gpt-5.6-luna','CLAUDE_CONFIG_DIR':str(cfg),'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS':'1','CLAUDE_CODE_DISABLE_FAST_MODE':'1','CLAUDE_CODE_MAX_CONTEXT_TOKENS':'372000','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'128000','TERM':'xterm-256color'})
server=ThreadingHTTPServer(('127.0.0.1',PORT),H); threading.Thread(target=server.serve_forever,daemon=True).start(); transcript='/tmp/claude-physical-gc-forced-screen.txt'
def drain(child, seconds):
 try:
  child.expect(pexpect.TIMEOUT, timeout=seconds)
 except pexpect.EOF:
  pass

try:
 args=['--safe-mode','--model','gpt-5.6-luna','--effort','low','--permission-mode','dontAsk','--tools','','--session-id',SESSION,'--settings','{"autoCompactEnabled":true}']
 child=pexpect.spawn(BINARY,args,env=env,cwd='/tmp',encoding='utf-8',timeout=30,dimensions=(40,160)); child.logfile=open(transcript,'w')
 drain(child,3); child.send('\x1b[A'); child.send('\r'); drain(child,4)
 child.send('first'); child.send('\r'); drain(child,2); first=wait_requests(1,30); drain(child,2)
 child.send('second'); child.send('\r'); drain(child,2); wait_requests(2,30); drain(child,2)
 child.send('third'); child.send('\r'); drain(child,2); wait_requests(3,30); drain(child,2)
 child.send('/compact'); child.send('\r'); drain(child,3); compact=wait_requests(4,60); drain(child,4)
 child.send('fourth'); child.send('\r'); drain(child,2); second=wait_requests(5,30); drain(child,2)
 child.send('/exit'); child.send('\r'); drain(child,2); child.close(force=True)
 resume_args=[BINARY,'-p','--safe-mode','--model','gpt-5.6-luna','--effort','low','--permission-mode','dontAsk','--tools','','--resume',SESSION,'after restart','--settings','{"autoCompactEnabled":true}']
 resumed=subprocess.run(resume_args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,cwd='/tmp',timeout=90)
 Path('/tmp/claude-physical-gc-forced-requests.json').write_text(json.dumps(S.requests,indent=2))
 jsonls=list(cfg.rglob('*.jsonl')); jt='\n'.join(p.read_text(errors='replace') for p in jsonls)
 print(json.dumps({'first_request':first,'compact_request':compact,'second_request':second,'message_request_count':len([r for r in S.requests if r['path'].startswith('/v1/messages')]),'message_counts':[len(r['body'].get('messages',[])) for r in S.requests if r['path'].startswith('/v1/messages')],'wire_summary_marker':['COMPACT_SUMMARY_MARKER' in json.dumps(r['body']) for r in S.requests if r['path'].startswith('/v1/messages')],'jsonls':[str(p) for p in jsonls],'jsonl_compact_boundary':'compact_boundary' in jt,'jsonl_summary_marker':'COMPACT_SUMMARY_MARKER' in jt,'resume_rc':resumed.returncode,'resume_output_tail':resumed.stdout.decode('utf-8','replace')[-500:],'screen_tail':Path(transcript).read_text(errors='replace')[-2000:],'root':str(root)},indent=2))
finally: server.shutdown(); server.server_close()
