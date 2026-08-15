#!/usr/bin/env python3
import json,os,subprocess,tempfile,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
B=os.environ.get('CLAUDE_HINT_PRECOMPUTE_BINARY','/tmp/claude-2.1.220-hint-precompute-stale');P=47674;SID='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';CAT=(Path(__file__).resolve().parents[2] / 'fixtures' / 'cliproxy-claude-models.json').read_bytes()
class S: req=[];cv=threading.Condition();tools=False;hint=False;ptl=False
def sse(text,mid,model='gpt-5.6-luna'):
 es=[('message_start',{'type':'message_start','message':{'id':mid,'type':'message','role':'assistant','model':model,'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':0,'output_tokens':0}}}),('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}}),('content_block_stop',{'type':'content_block_stop','index':0}),('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}}),('message_stop',{'type':'message_stop'})];return ''.join(f'event: {n}\ndata: {json.dumps(d)}\n\n' for n,d in es).encode()
def tools():
 es=[('message_start',{'type':'message_start','message':{'id':'msg_tools','type':'message','role':'assistant','model':'gpt-5.6-luna','content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':0,'output_tokens':0}}})]
 for i in range(10):
  cmd=f'''python3 -c 'import sys;sys.stdout.write("{chr(65+i)}"*30000)' '''.strip();es += [('content_block_start',{'type':'content_block_start','index':i,'content_block':{'type':'tool_use','id':f'toolu_stale_{i}','name':'Bash','input':{}}}),('content_block_delta',{'type':'content_block_delta','index':i,'delta':{'type':'input_json_delta','partial_json':json.dumps({'command':cmd})}}),('content_block_stop',{'type':'content_block_stop','index':i})]
 es += [('message_delta',{'type':'message_delta','delta':{'stop_reason':'tool_use','stop_sequence':None},'usage':{'output_tokens':10}}),('message_stop',{'type':'message_stop'})];return ''.join(f'event: {n}\ndata: {json.dumps(d)}\n\n' for n,d in es).encode()
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def sendb(self,s,c,d):self.send_response(s);self.send_header('Content-Type',c);self.send_header('Content-Length',str(len(d)));self.end_headers();self.wfile.write(d)
 def do_GET(self):self.sendb(200,'application/json',CAT) if self.path.startswith('/v1/models') else self.sendb(404,'application/json',b'{}')
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length','0')));body=json.loads(raw or b'{}');txt=json.dumps(body);compact='CRITICAL: Respond with TEXT ONLY' in txt;title='Write the title in the predominant language' in txt
  with S.cv:S.req.append({'path':self.path,'compact':compact,'title':title,'body':body});S.cv.notify_all()
  if self.path.startswith('/v1/messages/count_tokens'):self.sendb(200,'application/json',b'{"input_tokens":1}');return
  names={t.get('name') for t in body.get('tools',[]) if isinstance(t,dict)}
  if 'TOOLS' in txt and not title and not S.tools and 'Bash' in names:S.tools=True;self.sendb(200,'text/event-stream',tools());return
  if body.get('context_hint',{}).get('enabled') and not S.hint:S.hint=True;self.sendb(422,'application/json',json.dumps({'type':'error','error':{'type':'invalid_request_error','message':'local target requests client reduction'}}).encode());return
  if 'TRIGGER_PTL' in txt and not title and not compact and not S.ptl:S.ptl=True;self.sendb(413,'application/json',json.dumps({'type':'error','error':{'type':'invalid_request_error','message':'prompt is too long: 400000 > 372000'}}).encode());return
  self.sendb(200,'text/event-stream',sse('PREHINT_SUMMARY_MARKER' if compact else ('TITLE' if title else 'OK'),f'msg_{len(S.req)}'))
def line(t):return json.dumps({'type':'user','message':{'role':'user','content':[{'type':'text','text':t}]},'parent_tool_use_id':None,'session_id':SID})+'\n'
def wait(pred,to=120):
 end=time.time()+to
 with S.cv:
  while not pred() and time.time()<end:S.cv.wait(end-time.time())
 return pred()
root=Path(tempfile.mkdtemp(prefix='claude-hint-precompute-'));cfg=root/'config';work=root/'work';cfg.mkdir();work.mkdir();env=os.environ.copy()
for k in ('CLAUDECODE','CLAUDE_CODE_CHILD_SESSION','CLAUDE_CODE_SESSION_ID','CLAUDE_PID','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS','CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS'):env.pop(k,None)
env.update({'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{P}','ANTHROPIC_API_KEY':'test','ANTHROPIC_MODEL':'gpt-5.6-luna','CLAUDE_CONFIG_DIR':str(cfg),'CLAUDE_CODE_DISABLE_FAST_MODE':'1','CLAUDE_CODE_MAX_CONTEXT_TOKENS':'372000','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'128000'})
srv=ThreadingHTTPServer(('127.0.0.1',P),H);threading.Thread(target=srv.serve_forever,daemon=True).start()
try:
 cmd=[B,'-p','--safe-mode','--model','gpt-5.6-luna','--effort','low','--input-format','stream-json','--output-format','stream-json','--replay-user-messages','--verbose','--session-id',SID,'--permission-mode','bypassPermissions','--dangerously-skip-permissions','--allow-dangerously-skip-permissions','--allowedTools','Bash','--settings','{"autoCompactEnabled":true,"precomputeCompactionEnabled":true}']
 p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,cwd=work)
 p.stdin.write(line('WARMUP').encode());p.stdin.flush();wait(lambda:any('WARMUP' in json.dumps(r['body']) and not r['title'] for r in S.req));time.sleep(.4)
 p.stdin.write(line('TOOLS').encode());p.stdin.flush();wait(lambda:S.tools);wait(lambda:any(sum('tool_result' in json.dumps(m) for m in r['body'].get('messages',[]))>=1 for r in S.req),120);ready=wait(lambda:any(r['compact'] for r in S.req),120);time.sleep(.8)
 p.stdin.write(line('CLEAR_NOW').encode());p.stdin.flush();hint=wait(lambda:S.hint,120);hint_retry=wait(lambda:sum('CLEAR_NOW' in json.dumps(r['body']) and not r['title'] for r in S.req)>=2,120);time.sleep(.5)
 p.stdin.write(line('TRIGGER_PTL').encode());p.stdin.flush();ptl=wait(lambda:S.ptl,120);retry=wait(lambda:sum('TRIGGER_PTL' in json.dumps(r['body']) and not r['title'] and not r['compact'] for r in S.req)>=2,120);time.sleep(.5);p.stdin.close();p.wait(timeout=120)
 msgs=[r for r in S.req if r['path'].startswith('/v1/messages')];clear=[r for r in msgs if 'CLEAR_NOW' in json.dumps(r['body']) and not r['title']];tr=[r for r in msgs if 'TRIGGER_PTL' in json.dumps(r['body']) and not r['title'] and not r['compact']]
 def feat(r):
  t=json.dumps(r['body']);return {'bytes':len(t),'hint':r['body'].get('context_hint'),'wrappers':t.count('<persisted-output>'),'raw':t.count('A'*1000),'marker':'PREHINT_SUMMARY_MARKER' in t}
 result={'precompute_request':ready,'hint_rejected':hint,'hint_retry':hint_retry,'ptl_rejected':ptl,'ptl_retry':retry,'clear_requests':[feat(x) for x in clear],'trigger_requests':[feat(x) for x in tr],'compact_count':sum(r['compact'] for r in msgs),'root':str(root)};Path('/tmp/claude-hint-precompute-stale-result.json').write_text(json.dumps(result,indent=2));Path('/tmp/claude-hint-precompute-stale-requests.json').write_text(json.dumps(S.req,indent=2));print(json.dumps(result,indent=2))
finally:srv.shutdown();srv.server_close()
