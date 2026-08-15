#!/usr/bin/env python3
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BINARY = os.environ.get("CLAUDE_REPLACEMENT_BINARY", "/home/juraj/.local/share/claude/versions/2.1.220")
PORT = int(os.environ.get("PROBE_PORT", "47657"))
SESSION = "22222222-2222-4222-8222-222222222222"
CRASH = os.environ.get("PROBE_CRASH", "1") == "1"
KILL_DELAY_MS = int(os.environ.get("PROBE_KILL_DELAY_MS", "0"))
CATALOG_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "cliproxy-claude-models.json"
CATALOG = CATALOG_PATH.read_bytes()

class State:
    mode = "initial"
    requests = []
    tool_sent = False
    second_seen = threading.Event()
    release_second = threading.Event()


def sse(events):
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def text_response(msg_id):
    return sse([
        ("message_start", {"type":"message_start","message":{"id":msg_id,"type":"message","role":"assistant","model":"gpt-5.6-luna","content":[],"stop_reason":None,"stop_sequence":None,"usage":{"input_tokens":1,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0}}}),
        ("content_block_start", {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}),
        ("content_block_delta", {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}),
        ("content_block_stop", {"type":"content_block_stop","index":0}),
        ("message_delta", {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":None},"usage":{"output_tokens":1}}),
        ("message_stop", {"type":"message_stop"}),
    ])


def tool_response(msg_id):
    command = "python3 -c 'import sys;sys.stdout.write(\"Z\"*2000000)'"
    return sse([
        ("message_start", {"type":"message_start","message":{"id":msg_id,"type":"message","role":"assistant","model":"gpt-5.6-luna","content":[],"stop_reason":None,"stop_sequence":None,"usage":{"input_tokens":1,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0}}}),
        ("content_block_start", {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_crashprobe","name":"Bash","input":{}}}),
        ("content_block_delta", {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":json.dumps({"command":command})}}),
        ("content_block_stop", {"type":"content_block_stop","index":0}),
        ("message_delta", {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":None},"usage":{"output_tokens":1}}),
        ("message_stop", {"type":"message_stop"}),
    ])

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass
    def send_bytes(self, status, content_type, data):
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path.startswith('/v1/models'): self.send_bytes(200,'application/json',CATALOG)
        else: self.send_bytes(404,'application/json',b'{}')
    def do_POST(self):
        n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n)
        try: body=json.loads(raw or b'{}')
        except Exception: body={}
        State.requests.append({"mode":State.mode,"path":self.path,"header":self.headers.get('X-Claude-Code-Session-Id'),"body":body})
        if self.path.startswith('/v1/messages/count_tokens'):
            self.send_bytes(200,'application/json',b'{"input_tokens":1}'); return
        tool_names = {t.get('name') for t in body.get('tools', []) if isinstance(t, dict)}
        if State.mode == 'initial' and not State.tool_sent and 'Bash' in tool_names:
            State.tool_sent = True
            self.send_bytes(200,'text/event-stream',tool_response('msg_tool')); return
        if State.mode == 'initial' and State.tool_sent and any(
            isinstance(block, dict) and block.get('type') == 'tool_result'
            for message in body.get('messages', []) if isinstance(message, dict)
            for block in (message.get('content') if isinstance(message.get('content'), list) else [])
        ):
            State.second_seen.set()
            if CRASH:
                State.release_second.wait(30)
            self.send_bytes(200,'text/event-stream',text_response('msg_after_tool')); return
        self.send_bytes(200,'text/event-stream',text_response('msg_aux')); return
        self.send_bytes(200,'text/event-stream',text_response('msg_resume'))


def env_for(config):
    env=os.environ.copy()
    for k in ('CLAUDECODE','CLAUDE_CODE_CHILD_SESSION','CLAUDE_CODE_SESSION_ID','CLAUDE_PID','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS'): env.pop(k,None)
    env.update({
        'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{PORT}','ANTHROPIC_API_KEY':'test','ANTHROPIC_MODEL':'gpt-5.6-luna',
        'CLAUDE_CONFIG_DIR':str(config),'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS':'1','CLAUDE_CODE_DISABLE_FAST_MODE':'1',
        'CLAUDE_CODE_MAX_CONTEXT_TOKENS':'372000','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'128000',
    })
    return env


def base_args():
    return [BINARY,'-p','--safe-mode','--model','gpt-5.6-luna','--effort','low','--permission-mode','bypassPermissions','--dangerously-skip-permissions','--allow-dangerously-skip-permissions','--allowedTools','Bash','--settings','{"autoCompactEnabled":true}']

if __name__=='__main__':
    root=Path(tempfile.mkdtemp(prefix='claude-replacement-crash-')); config=root/'config'; work=root/'work'; config.mkdir(); work.mkdir()
    server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler); th=threading.Thread(target=server.serve_forever,daemon=True); th.start()
    try:
        p=subprocess.Popen(base_args()+['--session-id',SESSION,'generate a large output'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env_for(config),cwd=work,start_new_session=True)
        if not State.second_seen.wait(60):
            out=p.communicate(timeout=5)[0].decode('utf-8','replace')
            raise RuntimeError('second request not observed: '+out[-3000:])
        initial_second=[r for r in State.requests if r['mode']=='initial' and r['path'].startswith('/v1/messages')][-1]
        initial_json=json.dumps(initial_second['body'])
        Path('/tmp/claude-replacement-initial-body.json').write_text(json.dumps(initial_second['body'], indent=2))
        initial_has_wrapper='<persisted-output>' in initial_json
        initial_has_raw='Z'*1000 in initial_json
        if CRASH:
            if KILL_DELAY_MS > 0:
                time.sleep(KILL_DELAY_MS / 1000)
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=10)
            State.release_second.set()
        else:
            State.release_second.set()
            p.communicate(timeout=60)
        time.sleep(0.5)
        State.mode='resume'
        q=subprocess.run(base_args()+['--resume',SESSION,'after restart'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env_for(config),cwd=work,timeout=90)
        resume_requests=[r for r in State.requests if r['mode']=='resume' and r['path'].startswith('/v1/messages')]
        resume_json=json.dumps(resume_requests[-1]['body']) if resume_requests else ''
        Path('/tmp/claude-replacement-all-requests.json').write_text(json.dumps(State.requests, indent=2))
        result={
            'initial_wrapper':initial_has_wrapper,'initial_raw_sample':initial_has_raw,'initial_body_bytes':len(initial_json),
            'resume_rc':q.returncode,'resume_requests':len(resume_requests),'resume_wrapper':'<persisted-output>' in resume_json,
            'resume_raw_sample':'Z'*1000 in resume_json,'resume_body_bytes':len(resume_json),
            'resume_output_tail':q.stdout.decode('utf-8','replace')[-2000:],
            'jsonls':[str(x) for x in config.rglob('*.jsonl')],
            'tool_result_files':[str(x) for x in config.rglob('*') if x.is_file() and 'tool-results' in str(x)],
        }
        Path('/tmp/claude-replacement-crash-result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result,indent=2))
    finally:
        State.release_second.set(); server.shutdown(); server.server_close()
        # preserve root for forensic inspection; path is included below
        print('probe_root='+str(root))
