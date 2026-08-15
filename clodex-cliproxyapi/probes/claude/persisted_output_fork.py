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
PORT = 47678
PARENT = "55555555-5555-4555-8555-555555555555"
CATALOG = (Path(__file__).resolve().parents[2] / "fixtures" / "cliproxy-claude-models.json").read_bytes()


def sse(text, message_id):
    events = [
        ("message_start", {"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": "gpt-5.6-luna", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


class State:
    wrapper = ""
    requests = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_bytes(self, status, content_type, data):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self.send_bytes(200, "application/json", CATALOG)
        else:
            self.send_bytes(404, "application/json", b"{}")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw or b"{}")
        if self.path.startswith("/v1/messages/count_tokens"):
            self.send_bytes(200, "application/json", b'{"input_tokens":1}')
            return
        State.requests += 1
        body_text = json.dumps(body)
        response_text = State.wrapper if "parent turn" in body_text else "FORK_OK"
        self.send_bytes(200, "text/event-stream", sse(response_text, f"msg_{State.requests}"))


def run(binary, args, env):
    return subprocess.run([binary, *args], cwd="/tmp", env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)


root = Path(tempfile.mkdtemp(prefix="claude-persisted-fork-"))
config = root / "config"
config.mkdir()
(config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "theme": "dark", "lastOnboardingVersion": "2.1.220", "projects": {"/tmp": {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True, "projectOnboardingSeenCount": 1}}}))
(config / "settings.json").write_text(json.dumps({"theme": "dark", "syntaxHighlightingDisabled": True}))
project_dir = config / "projects" / "-tmp"
artifact = project_dir / PARENT / "tool-results" / "result.txt"
artifact.parent.mkdir(parents=True)
artifact.write_text("PARENT_OWNED_FULL_OUTPUT")
State.wrapper = f"<persisted-output>\nFull output saved to: {artifact}\nUse Read to view.\n</persisted-output>"

env = os.environ.copy()
for key in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS"):
    env.pop(key, None)
env.update({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{PORT}", "ANTHROPIC_API_KEY": "test", "ANTHROPIC_MODEL": "gpt-5.6-luna", "CLAUDE_CONFIG_DIR": str(config), "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1", "CLAUDE_CODE_DISABLE_FAST_MODE": "1"})
server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    common = ["-p", "--safe-mode", "--model", "gpt-5.6-luna", "--effort", "low", "--permission-mode", "dontAsk", "--tools", "", "--output-format", "json"]
    parent_run = run(BINARY, [*common, "--session-id", PARENT, "parent turn"], env)
    before = {p.stem for p in project_dir.glob("*.jsonl")}
    fork_run = run(BINARY, [*common, "--resume", PARENT, "--fork-session", "fork turn"], env)
    after_paths = list(project_dir.glob("*.jsonl"))
    child_paths = [p for p in after_paths if p.stem not in before and p.stem != PARENT]
    child = child_paths[0] if len(child_paths) == 1 else None
    child_text = child.read_text(errors="replace") if child else ""
    wrapper_copied = str(artifact) in child_text
    child_session_dir = project_dir / child.stem if child else None
    child_artifact_copy = child_session_dir / "tool-results" / "result.txt" if child_session_dir else None
    child_has_copy = bool(child_artifact_copy and child_artifact_copy.exists())
    shutil.rmtree(project_dir / PARENT)
    result = {
        "parent_rc": parent_run.returncode,
        "fork_rc": fork_run.returncode,
        "parent_output_tail": parent_run.stdout.decode(errors="replace")[-500:],
        "fork_output_tail": fork_run.stdout.decode(errors="replace")[-500:],
        "parent_session": PARENT,
        "child_transcript": str(child) if child else None,
        "wrapper_copied_verbatim": wrapper_copied,
        "child_owned_artifact_copy": child_has_copy,
        "artifact_exists_after_parent_delete": artifact.exists(),
        "child_transcript_exists_after_parent_delete": bool(child and child.exists()),
        "root": str(root),
    }
    Path("/tmp/claude-persisted-output-fork-result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
finally:
    server.shutdown()
    server.server_close()
