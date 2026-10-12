# rm-check: deny flagged removals before Claude Code's prompt

Claude Code's Bash permission check flags an `rm` or `rmdir` that can remove far
more than intended: a target built from a variable that may be empty, the output
of a command substitution, a relative glob after `cd`, or a system, home or
workspace directory. A flagged call always asks a person, even in bypass mode.
In an interactive session the prompt's auto-deny timer stops at the first
keypress, and a background subagent's prompt has no timer, so the agent waits
until someone answers.

`rm-check-hook.mjs` is a PreToolUse hook that runs the same check first and
denies the call with the check's own text, so the agent gets its rewrite advice
at once. It runs the check from the installed Claude Code version itself, so its
verdicts are the harness's verdicts. Every command the check does not flag,
including `rm` on ordinary literal paths, passes through untouched.

## Install

In `~/.claude/settings.json`:

```json
"hooks": {
  "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
    "command": "/home/juraj/.local/bin/bun /home/juraj/claude-patches/hooks/rm-check/rm-check-hook.mjs",
    "timeout": 120}]}]
}
```

It needs `bun`, `python3` and this repository. It finds the Claude Code version
by walking up its parent processes to the executable under
`~/.local/share/claude/versions/`. An update deletes the binary that running
sessions started from, and the kernel then reports their executable as
`<path> (deleted)`. The hook strips that suffix to read the version, and
`prepare.py` reads the binary through `/proc/<pid>/exe`, which stays readable
while the process runs.

## How it finds the check

The first time the hook sees a version, `prepare.py`:

1. extracts the bundle to `/tmp/claude-<version>.js` with `bun_handler/bun_handler.py`,
   unless that file exists;
2. finds the Bash tool's permission function by structure with the pfg matcher:
   the anchor on the tool's `checkPermissions` method must match exactly once
   (`pfg.discovery._one`), and the function's body, brace-matched by
   `pfg.jslex`, must test for `"dangerousRemoval"`;
3. writes the bundle's modules to `/tmp/claude-<version>-rmcheck/` and exports
   that function from its chunk.

The hook then imports the chunk with bun, calls the function on the command,
and denies when the result carries the `dangerousRemoval` safety check. A new
version with the same structure needs no edit. If the vendor changes it,
`prepare.py` fails naming the anchor, the hook exits 1, Claude Code reports a
hook error and the harness prompt applies as before.

The anchors match 2.1.281 through 2.1.296. After re-anchoring for a new
version, run `check_versions.py <new> <old>`: it runs 20 commands through the
hook for each version and reports whether the verdicts agree.

## Cost and limits

- A command that mentions `rm` or `rmdir` takes about 0.5 s longer; other
  commands about 0.04 s. Preparing a version takes 1 to 2 s, once.
- Only the session's working directory is passed to the check. A directory
  added with `--add-dir` or `/add-dir` is unknown to the hook, so removing it or
  one of its parents still gets the harness prompt.
- Commands the check flags wrongly are denied too, for example
  `d=x && index=$PWD/$d/index && rm -f "$index"`. The agent then rewrites the
  command instead of waiting for an answer.

Checked live on 2.1.296 in bypass mode: a flagged `rm` from the main agent and
from a background subagent was denied in about 0.5 s with no prompt, and an
`rm` on an ordinary literal path ran.
