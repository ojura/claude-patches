#!/usr/bin/env python3
import json,os,shutil,signal,subprocess,tempfile,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
B=os.environ.get('CLAUDE_TOMBSTONE_PROBE_BINARY','/tmp/claude-2.1.220-remove-probe');P=47675;CAT=(Path(__file__).resolve().parents[2] / 'fixtures' / 'cliproxy-claude-models.json').read_bytes()
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def sendb(self,s,c,d):self.send_response(s);self.send_header('Content-Type',c);self.send_header('Content-Length',str(len(d)));self.end_headers();self.wfile.write(d)
 def do_GET(self):self.sendb(200,'application/json',CAT) if self.path.startswith('/v1/models') else self.sendb(404,'application/json',b'{}')
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
  if self.path.startswith('/v1/messages/count_tokens'):self.sendb(200,'application/json',b'{"input_tokens":1}');return
  ev=[('message_start',{'type':'message_start','message':{'id':'m','type':'message','role':'assistant','model':'gpt-5.6-luna','content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':0,'output_tokens':0}}}),('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'OK'}}),('content_block_stop',{'type':'content_block_stop','index':0}),('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}}),('message_stop',{'type':'message_stop'})];d=''.join(f'event: {n}\ndata: {json.dumps(x)}\n\n' for n,x in ev).encode();self.sendb(200,'text/event-stream',d)
def base_copy():
 src=Path(json.loads(Path('/tmp/claude-rewind-successor-100.json').read_text())['root']);dst=Path(tempfile.mkdtemp(prefix='claude-remove-case-'));shutil.copytree(src/'config',dst/'config');return src,dst,next((dst/'config').rglob('*.jsonl'))
def env(cfg,uuid,pause=''):
 e=os.environ.copy()
 for k in ('CLAUDECODE','CLAUDE_CODE_CHILD_SESSION','CLAUDE_CODE_SESSION_ID','CLAUDE_PID','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS'):e.pop(k,None)
 e.update({'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{P}','ANTHROPIC_API_KEY':'test','ANTHROPIC_MODEL':'gpt-5.6-luna','CLAUDE_CONFIG_DIR':str(cfg),'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS':'1','CLAUDE_CODE_DISABLE_FAST_MODE':'1','PROBE_REMOVE_UUID':uuid})
 if pause:e['PROBE_REMOVE_PAUSE_MS']=pause
 return e
def run_skip():
 src,dst,j=base_copy();sid=j.stem;target='remove-over-50m-target';
 with j.open('ab') as f:
  f.write((json.dumps({'type':'progress','uuid':target,'sessionId':sid,'data':{'kind':'target'}},separators=(',',':'))+'\n').encode())
  chunk='X'*100000
  for i in range(530):f.write((json.dumps({'type':'progress','uuid':f'pad-{i}','sessionId':sid,'data':{'text':chunk}})+'\n').encode())
 before=j.stat().st_size;p=subprocess.run([B,'-p','--safe-mode','--resume',sid,'check','--permission-mode','dontAsk','--tools',''],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env(dst/'config',target),cwd=src/'work',timeout=180);text=j.read_text(errors='replace');return {'before':before,'after':j.stat().st_size,'target_remains':target in text,'rc':p.returncode,'root':str(dst),'tail':p.stdout.decode(errors='replace')[-500:]}
def run_interrupt():
 src,dst,j=base_copy();sid=j.stem;target='remove-pause-target';suffix='SUFFIX_MUST_SURVIVE';
 with j.open('ab') as f:f.write((json.dumps({'type':'progress','uuid':target,'sessionId':sid,'data':{'kind':'target'}},separators=(',',':'))+'\n').encode());f.write((json.dumps({'type':'progress','uuid':'suffix-id','sessionId':sid,'data':{'text':suffix+'Y'*20000}},separators=(',',':'))+'\n').encode())
 before=j.stat().st_size;p=subprocess.Popen([B,'-p','--safe-mode','--resume',sid,'check','--permission-mode','dontAsk','--tools',''],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env(dst/'config',target,'10000'),cwd=src/'work',start_new_session=True)
 end=time.time()+60; truncated=False
 while time.time()<end:
  current=j.read_text(errors='replace')
  if target not in current and suffix not in current:truncated=True;break
  time.sleep(.02)
 if truncated:os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=10)
 else:p.terminate();p.wait(timeout=10)
 text=j.read_text(errors='replace');return {'before':before,'after':j.stat().st_size,'truncated_before_kill':truncated,'target_remains':target in text,'suffix_remains':suffix in text,'root':str(dst)}
srv=ThreadingHTTPServer(('127.0.0.1',P),H);threading.Thread(target=srv.serve_forever,daemon=True).start()
try:
 r={'interrupted_tail_removal':run_interrupt()} if os.environ.get('ONLY_INTERRUPT') else {'over_50m':run_skip(),'interrupted_tail_removal':run_interrupt()};Path('/tmp/claude-tombstone-remove-interrupt-result.json' if os.environ.get('ONLY_INTERRUPT') else '/tmp/claude-tombstone-remove-result.json').write_text(json.dumps(r,indent=2));print(json.dumps(r,indent=2))
finally:srv.shutdown();srv.server_close()
