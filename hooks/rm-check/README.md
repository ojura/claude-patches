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

Importing that chunk takes about 0.45 s, because it pulls in a large part of
the bundle; one check then takes 2 to 50 ms. `rm-check-server.mjs` keeps a
version's check loaded and answers over the Unix socket
`$XDG_RUNTIME_DIR/rm-check/<version>.sock`, in a directory only this user can
enter. The hook asks that server; when none answers, it starts one and waits for
it, and when no server can be started it imports the chunk itself
(`rm-check-lib.mjs` holds the code both use). A server runs one check at a time,
because the check reads the process's working directory, and exits after
`RMCHECK_IDLE_SECONDS` (default 600) without a request, removing its socket. The
hook denies when the result carries the `dangerousRemoval` safety check. A new
version with the same structure needs no edit. If the vendor changes it,
`prepare.py` fails naming the anchor.

Any failure of the hook, including that one and unexpected exceptions, fails
open: the hook shows a "⚠ rm-check hook FAILED" warning with the cause in the
session (`systemMessage`), rings the terminal bell (`terminalSequence`), posts a
critical desktop notification with `notify-send`, and exits 0 without a
decision. The command then goes on to Claude Code's own permission check, so a
flagged rm gets the harness prompt as it would without the hook. The warning
repeats on every command the hook checks until the hook is fixed.

The hook runs the check on commands that mention `rm` or `rmdir` and on shell
`-c` scripts (`sh`, `bash`, `zsh`, `dash`, `ksh`, `mksh`, `ash`, `hush`,
`rbash`, `busybox`, `toybox`, `su`, `runuser`). The check also flags a `-c`
script it cannot fully check when a word in command position could be `rm`: a
word that starts with `$`, or a brace expansion. Its message then says the
script "runs rm" although the text never contains `rm`; an awk program with
`&& \$5 !~ ...` inside `sh -c '...'` is enough.

The anchors match 2.1.281 through 2.1.296. After re-anchoring for a new
version, run `check_versions.py <new> <old>`: it runs 20 commands through the
hook for each version and reports whether the verdicts agree.

## Cost and limits

- A command the hook checks takes about 0.04 s with a running server and about
  0.6 s when the hook starts one; other commands take about 0.03 s. A loaded
  server holds about 190 MB until it exits. On 2026-10-14 the check ran for 164
  of 6,972 Bash calls. Preparing a version takes 1 to 2 s, once.
- Only the session's working directory is passed to the check. A directory
  added with `--add-dir` or `/add-dir` is unknown to the hook, so removing it or
  one of its parents still gets the harness prompt.
- Commands the check flags wrongly are denied too, for example
  `d=x && index=$PWD/$d/index && rm -f "$index"`. The agent then rewrites the
  command instead of waiting for an answer.

Checked live on 2.1.296 in bypass mode: a flagged `rm` from the main agent and
from a background subagent was denied with no prompt, and an `rm` on an
ordinary literal path ran. Five hooks started at once against no server all
returned the verdict and left one server running.
