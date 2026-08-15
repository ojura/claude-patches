#!/usr/bin/env python3
import asyncio, json, os, socket, subprocess, tempfile, time
from pathlib import Path
import websockets
UP_PORT=47665; PROXY_PORT=18417; BIN=os.environ.get('CLIPROXY_BIN','/home/juraj/.ccs/cliproxy/bin/original/cli-proxy-api')
state={'up_requests':[]}

def completed(rid):
    return json.dumps({'type':'response.completed','response':{'id':rid,'object':'response','status':'completed','output':[],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}})
async def upstream(ws):
    idx=0
    async for raw in ws:
        idx+=1; state['up_requests'].append(json.loads(raw))
        if idx==1:
            await ws.send(completed('resp_A'))
        elif idx==2:
            await ws.send(completed('resp_A_LATE'))
            await asyncio.sleep(0.15)
            await ws.send(completed('resp_B'))
async def wait_port(port,timeout=10):
    end=time.time()+timeout
    while time.time()<end:
        try:
            r,w=await asyncio.open_connection('127.0.0.1',port); w.close(); await w.wait_closed(); return
        except OSError: await asyncio.sleep(.1)
    raise RuntimeError('proxy port not ready')
async def receive_terminal(ws,timeout=10):
    got=[]
    while True:
        raw=await asyncio.wait_for(ws.recv(),timeout); obj=json.loads(raw); got.append(obj)
        if obj.get('type') in ('response.completed','response.incomplete','error'): return got
async def main():
    root=Path(tempfile.mkdtemp(prefix='cliproxy-ws-probe-')); auth=root/'auth'; auth.mkdir(); cfg=root/'config.yaml'
    cfg.write_text(f'''host: 127.0.0.1\nport: {PROXY_PORT}\ndebug: true\napi-keys:\n  - local-test\nauth-dir: "{auth}"\nrequest-retry: 0\nlogging-to-file: false\nrequest-log: false\ncodex-api-key:\n  - api-key: upstream-test\n    base-url: "http://127.0.0.1:{UP_PORT}/v1"\n    websockets: true\n    models:\n      - name: gpt-5.6-luna\n        alias: gpt-5.6-luna\n        max-context-length: 372000\n''')
    up=await websockets.serve(upstream,'127.0.0.1',UP_PORT)
    log=open('/tmp/cliproxy-ws-probe.log','wb'); p=subprocess.Popen([BIN,'--config',str(cfg),'--local-model'],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    try:
        await wait_port(PROXY_PORT)
        async with websockets.connect(f'ws://127.0.0.1:{PROXY_PORT}/v1/responses',additional_headers={'Authorization':'Bearer local-test'}) as ws:
            await ws.send(json.dumps({'type':'response.create','model':'gpt-5.6-luna','input':'A'}))
            a=await receive_terminal(ws)
            await ws.send(json.dumps({'type':'response.create','model':'gpt-5.6-luna','input':'B'}))
            b=await receive_terminal(ws)
            # Drain briefly in case the real B terminal arrives after stale A ended the turn.
            extra=[]
            try:
                while True: extra.append(json.loads(await asyncio.wait_for(ws.recv(),.5)))
            except Exception: pass
        result={'a_events':a,'b_events':b,'extra_events':extra,'up_request_count':len(state['up_requests']),'up_requests':state['up_requests'],'root':str(root)}
        Path('/tmp/cliproxy-websocket-late-frame-result.json').write_text(json.dumps(result,indent=2)); print(json.dumps(result,indent=2))
    finally:
        p.terminate()
        try:p.wait(timeout=5)
        except: p.kill()
        log.close(); up.close(); await up.wait_closed()
asyncio.run(main())
