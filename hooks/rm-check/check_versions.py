#!/usr/bin/env python3
"""Run a fixed set of commands through rm-check-hook.mjs for one or more versions.

Usage: check_versions.py <version> [<version> ...]

Each version must be installed under ~/.local/share/claude/versions/. The hook
prepares it on first use. Every command runs with a new temporary directory as
both the request's working directory and the session's original directory
(CLAUDE_PROJECT_DIR); {cwd} in a command stands for that directory. Prints DENY
or pass per command with the time the hook took and the start of the check's
text, then whether every version gave the same verdicts. Use it after
re-anchoring prepare.py for a new version.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rm-check-hook.mjs")
COMMANDS = [
    'rm -f $CC_RM_A/$CC_RM_B',
    'd=work/x && index=$PWD/$d/index && rm -f "$index"',
    'index=$PWD/work/x/index && rm -f "$index"',
    'rm -rf $(cat list.txt)',
    'cd sub && rm -rf ./*',
    'rm -rf ~/',
    'rm -rf /usr',
    'rm -rf ..',
    'rm -rf *',
    'rm -rf */*',
    'bash -c \'rm -rf "$X"/y\'',
    'rm -rf "$TARGET_DIR"/etc',
    'for f in $(ls); do rm "$f"; done',
    'rm -f /tmp/cc-hooktest-file',
    'rm -rf ./build',
    'rm -f "$X"/somefile',
    'rm -rf "${BUILD_DIR:?}"/out',
    'git rm --cached file',
    'echo rm',
    'ls -la',
    # Removes the request's working directory after a cd that the check does
    # not follow: DENY whichever directory the hook's server started in.
    'cd / && rm -rf {cwd}',
]


def run(version, cwd):
    verdicts = []
    env = dict(os.environ, RMCHECK_CLAUDE_VERSION=version, CLAUDE_PROJECT_DIR=cwd)
    for cmd in COMMANDS:
        start = time.time()
        command = cmd.replace("{cwd}", cwd)
        p = subprocess.run(["bun", HOOK], text=True, capture_output=True, env=env,
                           input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                                             "cwd": cwd, "permission_mode": "bypassPermissions"}))
        took = time.time() - start
        if p.returncode != 0:
            verdict, note = "ERROR", p.stderr.strip()[:300]
        elif '"deny"' in p.stdout:
            reason = json.loads(p.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            verdict, note = "DENY", reason.split("The check's text: ", 1)[-1][:100].replace("\n", " ")
        else:
            verdict, note = "pass", ""
        verdicts.append(verdict)
        print(f"{version} {verdict:5} {took:4.2f}s  {cmd!r:55} {note}")
    return verdicts


def main(versions):
    if not versions:
        raise SystemExit(__doc__)
    with tempfile.TemporaryDirectory(prefix="rmcheck-cwd.") as cwd:
        os.mkdir(os.path.join(cwd, "sub"))
        results = {v: run(v, cwd) for v in versions}
    same = len({tuple(r) for r in results.values()}) == 1
    print("all versions gave the same verdicts" if same else "VERDICTS DIFFER between versions")
    sys.exit(0 if same else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
