#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile

BINARY = os.environ.get("CLAUDE_ADMISSION_BINARY", "/home/juraj/.local/share/claude/versions/2.1.220")
BASE_URL = os.environ.get("CLAUDE_ADMISSION_BASE_URL", "http://127.0.0.1:47654")
CONFIG_DIR = tempfile.mkdtemp(prefix="claude-admission-config-")

BASE = [
    BINARY, "-p", "--no-session-persistence", "--safe-mode",
    "--model", "gpt-5.6-luna", "--effort", "low",
    "--output-format", "json",
]

def probe(filler_units: int, max_output: int) -> str:
    env = os.environ.copy()
    for key in (
        "CLAUDECODE",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_PID",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
    ):
        env.pop(key, None)
    env["ANTHROPIC_BASE_URL"] = BASE_URL
    env["ANTHROPIC_API_KEY"] = "test"
    env["CLAUDE_CONFIG_DIR"] = CONFIG_DIR
    env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    env["CLAUDE_CODE_DISABLE_FAST_MODE"] = "1"
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output)
    prompt = (("x " * filler_units) + "\nReply exactly OK.").encode()
    try:
        p = subprocess.run(
            BASE,
            input=prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    text = p.stdout.decode("utf-8", "replace")
    if "ADMISSION_PASSED_STUB" in text:
        return "pass"
    if "Prompt is too long" in text or "Context limit reached" in text:
        return "block"
    print(f"UNKNOWN filler={filler_units} output={max_output} rc={p.returncode}", file=sys.stderr)
    print(text[-4000:], file=sys.stderr)
    return "unknown"

def boundary_for_output(max_output: int) -> tuple[int, int]:
    lo = 0
    if probe(lo, max_output) != "pass":
        raise RuntimeError(f"zero-filler probe did not pass for output={max_output}")
    hi = 100_000
    while True:
        result = probe(hi, max_output)
        print(f"bracket output={max_output} filler={hi}: {result}", flush=True)
        if result == "block":
            break
        if result != "pass":
            raise RuntimeError(f"unexpected probe result: {result}")
        lo = hi
        hi *= 2
        if hi > 1_600_000:
            raise RuntimeError("could not find blocked upper bound")
    while hi - lo > 256:
        mid = (lo + hi) // 2
        result = probe(mid, max_output)
        print(f"bisect output={max_output} filler={mid}: {result}", flush=True)
        if result == "pass":
            lo = mid
        elif result == "block":
            hi = mid
        else:
            raise RuntimeError(f"unexpected probe result: {result}")
    return lo, hi

def output_boundary(filler_units: int, low: int = 128, high: int = 128_000) -> tuple[int, int]:
    low_result = probe(filler_units, low)
    high_result = probe(filler_units, high)
    print(f"output bracket filler={filler_units}: {low}={low_result}, {high}={high_result}", flush=True)
    if low_result != "pass" or high_result != "block":
        raise RuntimeError("chosen filler does not bracket output admission")
    lo, hi = low, high
    while hi - lo > 8:
        mid = (lo + hi) // 2
        result = probe(filler_units, mid)
        print(f"bisect filler={filler_units} output={mid}: {result}", flush=True)
        if result == "pass":
            lo = mid
        elif result == "block":
            hi = mid
        else:
            raise RuntimeError(f"unexpected probe result: {result}")
    return lo, hi

if __name__ == "__main__":
    b32 = boundary_for_output(32_000)
    b128 = boundary_for_output(128)
    print(f"FILLER_BOUNDARY output=32000 pass<{b32[0]} block>={b32[1]}")
    print(f"FILLER_BOUNDARY output=128 pass<{b128[0]} block>={b128[1]}")
    chosen = (b32[1] + b128[0]) // 2
    ob = output_boundary(chosen)
    print(f"OUTPUT_BOUNDARY filler={chosen} pass<={ob[0]} block>={ob[1]}")
