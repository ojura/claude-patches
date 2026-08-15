#!/usr/bin/env python3
import json, os, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
BINARY=os.environ.get('CLAUDE_CONTEXT_HINT_BINARY','/tmp/claude-2.1.220-force-context-hint'); PORT=47664; SESSION='77777777-7777-4777-8777-777777777777'; CATALOG=(Path(__file__).resolve().parents[2] / 'fixtures' / 'cliproxy-claude-models.json').read_bytes()
class S:
    mode='run'; requests=[]; tool_sent=False; hint_rejected=False

def sse_text(text,mid):
    es=[('message_start',{'type':'message_start','message':{'id':mid,'type':'message','role':'assistant','model':'gpt-5.6-luna','content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':0,'output_tokens':0}}}),('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}}),('content_block_stop',{'type':'content_block_stop','index':0}),('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}}),('message_stop',{'type':'message_stop'})]
    return ''.join(f'event: {n}\ndata: {json.dumps(d)}\n\n' for n,d in es).encode()
def sse_tools():
    es=[('message_start',{'type':'message_start','message':{'id':'msg_tools','type':'message','role':'assistant','model':'gpt-5.6-luna','content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':0,'output_tokens':0}}})]
    for i in range(10):
        cmd=f'''python3 -c 'import sys;sys.stdout.write("{chr(65+i)}"*30000)' '''.strip()
        es += [('content_block_start',{'type':'content_block_start','index':i,'content_block':{'type':'tool_use','id':f'toolu_hint_{i}','name':'Bash','input':{}}}),('content_block_delta',{'type':'content_block_delta','index':i,'delta':{'type':'input_json_delta','partial_json':json.dumps({'command':cmd})}}),('content_block_stop',{'type':'content_block_stop','index':i})]
    es += [('message_delta',{'type':'message_delta','delta':{'stop_reason':'tool_use','stop_sequence':None},'usage':{'output_tokens':10}}),('message_stop',{'type':'message_stop'})]
    return ''.join(f'event: {n}\ndata: {json.dumps(d)}\n\n' for n,d in es).encode()
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def sendb(self,status,ct,data): self.send_response(status); self.send_header('Content-Type',ct); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self): self.sendb(200,'application/json',CATALOG) if self.path.startswith('/v1/models') else self.sendb(404,'application/json',b'{}')
    def do_POST(self):
        raw=self.rfile.read(int(self.headers.get('Content-Length','0'))); body=json.loads(raw or b'{}'); S.requests.append({'mode':S.mode,'path':self.path,'header':self.headers.get('X-Claude-Code-Session-Id'),'body':body})
        if self.path.startswith('/v1/messages/count_tokens'): self.sendb(200,'application/json',b'{"input_tokens":1}'); return
        tool_names={t.get('name') for t in body.get('tools',[]) if isinstance(t,dict)}
        if S.mode=='run' and not S.tool_sent and 'Bash' in tool_names:
            S.tool_sent=True; self.sendb(200,'text/event-stream',sse_tools()); return
        if S.mode=='run' and body.get('context_hint',{}).get('enabled') and not S.hint_rejected:
            S.hint_rejected=True; self.sendb(422,'application/json',json.dumps({'type':'error','error':{'type':'invalid_request_error','message':'context_hint unsupported by local target'}}).encode()); return
        self.sendb(200,'text/event-stream',sse_text('AFTER_HINT_OK' if S.mode=='run' else 'RESUME_OK',f'msg_{len(S.requests)}'))
def env(cfg):
    e=os.environ.copy()
    for k in ('CLAUDECODE','CLAUDE_CODE_CHILD_SESSION','CLAUDE_CODE_SESSION_ID','CLAUDE_PID','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS','CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS'):
        e.pop(k,None)
    e.update({'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{PORT}','ANTHROPIC_API_KEY':'test','ANTHROPIC_MODEL':'gpt-5.6-luna','CLAUDE_CONFIG_DIR':str(cfg),'CLAUDE_CODE_DISABLE_FAST_MODE':'1','CLAUDE_CODE_MAX_CONTEXT_TOKENS':'372000','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'128000'})
    return e
def args(): return [BINARY,'-p','--safe-mode','--model','gpt-5.6-luna','--effort','low','--permission-mode','bypassPermissions','--dangerously-skip-permissions','--allow-dangerously-skip-permissions','--allowedTools','Bash','--settings','{"autoCompactEnabled":true}']
root=Path(tempfile.mkdtemp(prefix='claude-context-hint-')); cfg=root/'config'; work=root/'work'; cfg.mkdir(); work.mkdir(); srv=ThreadingHTTPServer(('127.0.0.1',PORT),H); threading.Thread(target=srv.serve_forever,daemon=True).start()
try:
    p=subprocess.run(args()+['--session-id',SESSION,'make outputs'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env(cfg),cwd=work,timeout=180)
    S.mode='resume'
    q=subprocess.run(args()+['--resume',SESSION,'after restart'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env(cfg),cwd=work,timeout=120)
    msgs=[r for r in S.requests if r['path'].startswith('/v1/messages')]
    def features(r):
        text=json.dumps(r['body']); return {'mode':r['mode'],'hint':r['body'].get('context_hint'),'bytes':len(text),'wrappers':text.count('<persisted-output>'),'cleared':text.count('[Old tool result content cleared]'),'raw_A':text.count('A'*1000),'tool_results':text.count('tool_result')}
    result={'run_rc':p.returncode,'resume_rc':q.returncode,'hint_rejected':S.hint_rejected,'requests':[features(r) for r in msgs],'tool_result_files':[str(x) for x in cfg.rglob('*') if x.is_file() and 'tool-results' in str(x)],'jsonls':[str(x) for x in cfg.rglob('*.jsonl')],'run_tail':p.stdout.decode('utf-8','replace')[-1000:],'resume_tail':q.stdout.decode('utf-8','replace')[-1000:],'root':str(root)}
    Path('/tmp/claude-context-hint-durability-result.json').write_text(json.dumps(result,indent=2)); Path('/tmp/claude-context-hint-all-requests.json').write_text(json.dumps(S.requests,indent=2)); print(json.dumps(result,indent=2))
finally: srv.shutdown(); srv.server_close()
