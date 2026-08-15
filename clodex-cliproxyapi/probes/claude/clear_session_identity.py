#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BINARY = "/home/juraj/.local/share/claude/versions/2.1.220"
PORT = 47656
SESSION = "11111111-1111-4111-8111-111111111111"
CATALOG_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "cliproxy-claude-models.json"
CATALOG = CATALOG_PATH.read_bytes() if CATALOG_PATH.exists() else json.dumps({"data": [{"id": "gpt-5.6-luna", "type": "model", "display_name": "GPT 5.6 Luna", "max_input_tokens": 372000, "max_tokens": 128000}]}).encode()

class State:
    requests = []

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(CATALOG)))
            self.end_headers()
            self.wfile.write(CATALOG)
        else:
            self.reply_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        State.requests.append({
            "path": self.path,
            "session_header": self.headers.get("X-Claude-Code-Session-Id"),
            "headers": dict(self.headers),
            "body": json.loads(body or b"{}"),
        })
        if self.path.startswith("/v1/messages/count_tokens"):
            self.reply_json(200, {"input_tokens": 1})
            return
        if not self.path.startswith("/v1/messages"):
            self.reply_json(404, {"error": {"message": "not found"}})
            return
        msg_id = f"msg_probe_{len(State.requests)}"
        events = [
            ("message_start", {"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "model": "gpt-5.6-luna", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        payload = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def user_line(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}, "parent_tool_use_id": None, "session_id": SESSION}) + "\n"


def run(custom_header=None):
    State.requests = []
    config = Path(tempfile.mkdtemp(prefix="claude-clear-probe-"))
    work = config / "work"
    work.mkdir()
    env = os.environ.copy()
    for key in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(key, None)
    env.update({
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{PORT}",
        "ANTHROPIC_API_KEY": "test",
        "ANTHROPIC_MODEL": "gpt-5.6-luna",
        "CLAUDE_CONFIG_DIR": str(config / "config"),
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "372000",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000",
    })
    if custom_header:
        env["ANTHROPIC_CUSTOM_HEADERS"] = custom_header
    else:
        env.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    cmd = [
        BINARY, "-p", "--bare", "--safe-mode", "--model", "gpt-5.6-luna", "--effort", "low",
        "--input-format", "stream-json", "--output-format", "stream-json", "--replay-user-messages", "--verbose",
        "--session-id", SESSION, "--permission-mode", "dontAsk", "--tools", "",
        "--settings", '{"autoCompactEnabled":true}',
    ]
    stdin = user_line("first") + user_line("/clear") + user_line("second")
    proc = subprocess.run(cmd, input=stdin.encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=work, timeout=90)
    output = proc.stdout.decode("utf-8", "replace")
    result = {"rc": proc.returncode, "output_tail": output[-5000:], "requests": State.requests}
    shutil.rmtree(config, ignore_errors=True)
    return result

if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        normal = run()
        pinned = run(f"X-Claude-Code-Session-Id: {SESSION}")
        Path("/tmp/claude-clear-probe-result.json").write_text(json.dumps({"normal": normal, "pinned": pinned}, indent=2))
        print(json.dumps({
            "normal_rc": normal["rc"],
            "normal_requests": [(r["path"], r["session_header"], r["body"].get("metadata", {}).get("user_id")) for r in normal["requests"]],
            "pinned_rc": pinned["rc"],
            "pinned_requests": [(r["path"], r["session_header"], r["body"].get("metadata", {}).get("user_id")) for r in pinned["requests"]],
            "normal_output_tail": normal["output_tail"][-1000:],
            "pinned_output_tail": pinned["output_tail"][-1000:],
        }, indent=2))
    finally:
        server.shutdown()
        server.server_close()
