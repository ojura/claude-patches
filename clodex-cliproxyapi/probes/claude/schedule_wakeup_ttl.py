#!/usr/bin/env python3
import json, os, subprocess, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT=47663
CATALOG=(Path(__file__).resolve().parents[2] / 'fixtures' / 'cliproxy-claude-models.json').read_bytes()
class State: requests=[]

def sse():
    events=[
      ('message_start',{'type':'message_start','message':{'id':'msg_ttl','type':'message','role':'assistant','model':'gpt-5.6-luna','content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':0,'output_tokens':0}}}),
      ('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),
      ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'OK'}}),
      ('content_block_stop',{'type':'content_block_stop','index':0}),
      ('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}}),
      ('message_stop',{'type':'message_stop'})]
    return ''.join(f'event: {n}\ndata: {json.dumps(d)}\n\n' for n,d in events).encode()
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def sendb(self,status,ct,data):
        self.send_response(status); self.send_header('Content-Type',ct); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self): self.sendb(200,'application/json',CATALOG) if self.path.startswith('/v1/models') else self.sendb(404,'application/json',b'{}')
    def do_POST(self):
        raw=self.rfile.read(int(self.headers.get('Content-Length','0'))); body=json.loads(raw or b'{}')
        State.requests.append({'path':self.path,'body':body})
        if self.path.startswith('/v1/messages/count_tokens'): self.sendb(200,'application/json',b'{"input_tokens":1}')
        else: self.sendb(200,'text/event-stream',sse())

def run(label,binary,extra):
    State.requests=[]
    root=Path(tempfile.mkdtemp(prefix='claude-ttl-'+label+'-')); cfg=root/'config'; work=root/'work'; cfg.mkdir(); work.mkdir()
    env=os.environ.copy()
    for k in ('CLAUDECODE','CLAUDE_CODE_CHILD_SESSION','CLAUDE_CODE_SESSION_ID','CLAUDE_PID','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS','FORCE_PROMPT_CACHING_5M','ENABLE_PROMPT_CACHING_1H','ENABLE_PROMPT_CACHING_1H_BEDROCK'):
        env.pop(k,None)
    env.update({'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{PORT}','ANTHROPIC_API_KEY':'test','ANTHROPIC_MODEL':'gpt-5.6-luna','CLAUDE_CONFIG_DIR':str(cfg),'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS':'1','CLAUDE_CODE_DISABLE_FAST_MODE':'1','CLAUDE_CODE_MAX_CONTEXT_TOKENS':'372000','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'128000',**extra})
    p=subprocess.run([binary,'-p','check','--safe-mode','--model','gpt-5.6-luna','--effort','low','--permission-mode','dontAsk','--tools','ScheduleWakeup'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,cwd=work,timeout=90)
    msgs=[r for r in State.requests if r['path'].startswith('/v1/messages')]
    body=msgs[-1]['body'] if msgs else {}
    desc='\n'.join(t.get('description','') for t in body.get('tools',[]) if t.get('name')=='ScheduleWakeup')
    result={'label':label,'rc':p.returncode,'message_requests':len(msgs),'tool_count':len(body.get('tools',[])),'schedule_count':sum(t.get('name')=='ScheduleWakeup' for t in body.get('tools',[])),'has_5m':"default 5-minute Anthropic prompt-cache TTL" in desc,'has_1h':"1-hour Anthropic prompt-cache TTL" in desc,'has_generic':"default 5-minute" not in desc and "1-hour Anthropic" not in desc,'description_sha256':__import__('hashlib').sha256(desc.encode()).hexdigest(),'description_bytes':len(desc),'description':desc,'output_tail':p.stdout.decode('utf-8','replace')[-500:]}
    Path(f'/tmp/claude-ttl-{label}-requests.json').write_text(json.dumps(State.requests,indent=2))
    return result

if __name__=='__main__':
    srv=ThreadingHTTPServer(('127.0.0.1',PORT),H); threading.Thread(target=srv.serve_forever,daemon=True).start()
    try:
        results=[
          run('five','/home/juraj/.local/share/claude/versions/2.1.220',{'FORCE_PROMPT_CACHING_5M':'1'}),
          run('one','/home/juraj/.local/share/claude/versions/2.1.220',{'ENABLE_PROMPT_CACHING_1H':'1'}),
          run('mixed',os.environ.get('CLAUDE_TTL_MIXED_BINARY','/tmp/claude-2.1.220-ttl-mixed'),{}),
        ]
        Path('/tmp/claude-schedule-ttl-probe-result.json').write_text(json.dumps(results,indent=2))
        print(json.dumps([{k:v for k,v in r.items() if k!='description'} for r in results],indent=2))
    finally: srv.shutdown(); srv.server_close()
