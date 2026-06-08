#!/bin/bash
# Full proto trace of a genuine fresh mount: close the session tab (destroy the
# retained webview), start trace, reopen (Ctrl+Shift+T), SELECT it (lazy render),
# detect the fresh iframe, poll to render-done, drain.
cd /tmp
rm -f /tmp/STOP_TRACE
BROWSER=$(curl -s http://127.0.0.1:9222/json/version | python3 -c 'import sys,json;print(json.load(sys.stdin)["webSocketDebuggerUrl"])')
WB=$(curl -s http://127.0.0.1:9222/json | python3 -c "import sys,json
for t in json.load(sys.stdin):
  if t.get('type')=='page' and 'Antigravity IDE' in (t.get('title') or ''): print(t['webSocketDebuggerUrl']); break")
echo "BROWSER=$BROWSER"
echo "WB=$WB"
echo '(function(){return JSON.stringify({len:(document.body.innerText||"").length});})()' > /tmp/blen.js
blen(){ node eval_in_inner_frame.mjs "ws://127.0.0.1:9222/devtools/page/$1" @/tmp/blen.js 2>/dev/null | python3 -c "import sys,json
try: print(json.loads(json.load(sys.stdin)['result']['value'])['len'])
except: print(-1)"; }
chats(){ curl -s -m3 http://127.0.0.1:9222/json/list 2>/dev/null | python3 -c "import sys,json
try:
  for t in json.load(sys.stdin):
    u=t.get('url') or ''
    if t.get('type')=='iframe' and 'vscode-webview' in u and 'purpose=webviewView' not in u: print(t['id'])
except: pass"; }
# 0) close the session tab (destroy the retained webview)
echo "=== close session tab ==="
node /tmp/close_session.mjs "$WB" "claude-patches"
sleep 2
BEFORE=$(chats | tr '\n' ' ')
echo "BEFORE iframes (after close): [$BEFORE]"
# 1) start proto trace
node /tmp/tracer8.mjs "$BROWSER" /tmp/render.perfetto-trace 320 600000 &
TP=$!
sleep 4
# 2) reopen the closed tab
echo "=== reopen (Ctrl+Shift+T) ==="
node /tmp/reopen.mjs "$WB"
sleep 2
# 3) select the reopened tab -> lazy render
echo "=== select session tab ==="
node /tmp/select_tab.mjs "$WB" "claude-patches"
# 4) find FRESH iframe
TARGET=""; for i in $(seq 1 30); do sleep 1; for cid in $(chats); do case " $BEFORE " in *" $cid "*) continue;; esac; TARGET="$cid"; break; done; [ -n "$TARGET" ] && break; done
if [ -z "$TARGET" ]; then echo "NO FRESH IFRAME in 30s"; touch /tmp/STOP_TRACE; wait $TP; echo FRESH_FAIL; exit 1; fi
echo "fresh TARGET=$TARGET start_len=$(blen "$TARGET"); capturing render to completion"
# 5) poll to render-done
LAST=-1; STABLE=0
for i in $(seq 1 240); do L=$(blen "$TARGET")
  if [ "$L" -gt 500000 ] && [ "$L" = "$LAST" ]; then STABLE=$((STABLE+1)); else STABLE=0; fi
  if [ "$STABLE" -ge 2 ]; then echo ">>> RENDER DONE len=$L (i=$i)"; break; fi
  LAST="$L"; sleep 1
done
touch /tmp/STOP_TRACE
wait $TP; echo "tracer8 exit=$?"
ls -la /tmp/render.perfetto-trace 2>/dev/null | awk '{print "proto bytes:", $5}'
echo FRESH_CAPTURE_DONE
