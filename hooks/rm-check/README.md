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

It needs `bun`, `python3`, `node` (for `pfg.bundle`, once per version) and this
repository. It finds the Claude Code version by walking up its parent processes
to the executable under `~/.local/share/claude/versions/`. An update deletes the
binary that running sessions started from, and the kernel then reports their
executable as `<path> (deleted)`. The hook strips that suffix to read the
version, and `prepare.py` reads the binary through `/proc/<pid>/exe`, which
stays readable while the process runs.

## How it finds the check

The first time the hook sees a version, `prepare.py`:

1. extracts the bundle to `~/.cache/rm-check/<version>/claude.js` (under
   `$XDG_CACHE_HOME` when it is set) with `bun_handler/bun_handler.py`, unless
   that file exists;
2. finds three things with `pfg.bundle`, the structural matcher, which parses
   the modules with TypeScript and answers from their syntax and bindings:
   - the Bash tool: a module-level `var T = F({name: N, ..., searchHint:
     "execute shell commands", ...})` whose `N` is the string `"Bash"`, read
     through its import. The hook calls `T.checkPermissions`, the method
     Claude Code calls for the tool's permission verdict, so the wrappers
     Claude Code puts around the check apply too;
   - runInCwd: a module-level `function F(dir, fn) {return S.run({cwd: dir},
     fn)}`, with or without a call around `dir`, which runs `fn` with
     `{cwd: dir}` in an AsyncLocalStorage store. Claude Code runs a session's
     and a subagent's work inside it, and the check reads its working
     directory from that store;
   - setOriginalCwd: a module-level `function F(dir) {U({originalCwd:
     dir})}`, which sets the session's original working directory;
3. writes the bundle's modules to `~/.cache/rm-check/<version>/modules-v4/`,
   exports what it found from the chunks that hold it, and lists it in
   `manifest.json`.

Text in the modules picks which ones to parse (`NOMINATE` in `prepare.py`):
on 2.1.309 that is 5 modules, about 4 s of parsing, where the whole bundle
takes 11 s. Every anchor is tried, and each one that finds nothing, or more
than 8 matches, is reported by name. `pfg.bundle` runs its helper on `node`.

When `rm-check-lib.mjs` loads the chunks, it checks by behaviour that they
work together: the tool's `checkPermissions` must flag `rm -rf /`, and exactly
one pair of directory functions must give it its expected verdicts on three
probes with synthetic directories under `/nonexistent-rm-check-probe`
(`PROBES` there). Otherwise it throws with what each candidate did, and the
hook fails open with that text. The anchors describe what the hook relies on
rather than the code around it, because that code changes between releases:
on 2.1.309 the Bash tool's `checkPermissions` lost the statements it began
with, wraps its permission function in a sandbox-override step, and that
function runs the check again under a retry.

The hook imports and runs code from these files, so they are kept under your
home directory, where other accounts cannot create or change them.

After preparing a version, `prepare.py` removes the cache directory of each
version that is neither installed under `~/.local/share/claude/versions/` nor
the executable of a running process, the modules directories of earlier
layouts (see below), and the `/tmp/claude-<version>-rmcheck/` directories that
earlier revisions of the hook wrote. It keeps every directory that a running
server was started with.

Importing the chunks takes about 0.45 s, because they pull in a large part of
the bundle, and the self-test about 0.2 s; one check then takes 1 to 50 ms.
`rm-check-server.mjs` keeps a version's check loaded and answers over the Unix
socket `$XDG_RUNTIME_DIR/rm-check/<version>-v4.sock`, in a directory only this
user can enter. The hook asks that server; when none answers, it starts one and
waits for it, and when no server can be started it imports the chunks itself
(`rm-check-lib.mjs` holds the code both use). A server runs one check at a
time, because each check sets the session's original working directory, which
the bundle keeps in process-wide state. It exits after `RMCHECK_IDLE_SECONDS`
(default 600) without a request, removing its socket. The hook denies when the
result carries the `dangerousRemoval` safety check. A new version with the same
structure needs no edit. If the vendor changes it, `prepare.py` fails naming
the anchors, or the self-test fails naming what each candidate did.

The check's verdict depends on two directories. The working directory is where
it resolves relative targets and the directory that an `rm` must not remove,
together with its parents. The session's original working directory is where
the session started; with the `--add-dir` directories, it makes up the
session's working directories, and the check follows a `cd` in the command only
into those. For example, in a session that started in `/tmp` and whose working
directory was `/tmp/cpa-deploy-7237e8a8`, Claude Code flagged
`cd /home/juraj && rm -rf /tmp/cpa-deploy-7237e8a8` as "Dangerous rm operation
on working directory or its ancestor": `/home/juraj` is outside `/tmp`, so the
check did not follow the `cd`.

When the bundle loads, it sets both directories to the directory the process
started in, so in a server they would be the directory the server started in,
and `process.chdir` changes neither. `rm-check-lib.mjs` therefore sets the
session's original directory with setOriginalCwd before each check, and runs
the check inside runInCwd with the request's working directory, the hook
input's `cwd`. The hook takes the original directory from the working
directory of the Claude Code process it finds among its parents: Claude Code
sets the session's original directory from it when it starts, and where it
moves the session to another directory (into or out of a worktree, on resume,
for Remote Control), it mostly changes both in the same step. With
`RMCHECK_CLAUDE_VERSION` there is no such process, and the hook takes
`CLAUDE_PROJECT_DIR`, which Claude Code sets for hooks to the session's project
directory, or else the request's `cwd`.

A running server keeps the code it loaded. A change to `rm-check-lib.mjs` or
`rm-check-server.mjs` reaches a version's checks once its server has exited. A
change to the prepared files or to the manifest must raise `LAYOUT` in
`rm-check-lib.mjs` (now 4), which names the modules directory (`modules-v4`)
and the socket; new hooks then prepare a new directory and start a new server
at once, and the old server exits when its idle time runs out.

Any failure of the hook, including a `prepare.py` failure on a changed anchor
and unexpected exceptions, fails open: the hook shows a "⚠ The rm-check hook
failed" warning with the cause in the session (`systemMessage`), rings the
terminal bell (`terminalSequence`), posts a critical desktop notification with
`notify-send`, and exits 0 without a decision. The command then goes on to
Claude Code's own permission check, so a flagged rm gets the harness prompt as
it would without the hook. The warning repeats on every command the hook checks
until the hook is fixed.

The hook runs the check on commands that mention `rm` or `rmdir` and on shell
`-c` scripts (`sh`, `bash`, `zsh`, `dash`, `ksh`, `mksh`, `ash`, `hush`,
`rbash`, `busybox`, `toybox`, `su`, `runuser`). The check also flags a `-c`
script it cannot fully check when a word in command position could be `rm`: a
word that starts with `$`, or a brace expansion. Its message then says the
script "runs rm" although the text never contains `rm`; an awk program with
`&& \$5 !~ ...` inside `sh -c '...'` is enough.

The anchors find their matches, and the self-test passes, on 2.1.303, 2.1.304,
2.1.305 and 2.1.309. After re-anchoring for a new version, run
`check_versions.py <new> <old>`: it runs a fixed set of commands through the
hook for each version, one of them removing the request's working directory,
and reports whether the verdicts agree.
`util/test_rm_check_hook.py` repeats that example with new directories on the
newest installed version, and checks that the verdicts do not depend on the
directory the hook and its server started in. It also runs the self-test on a
fake chunk with look-alike candidates, and the anchors on small synthetic
modules.

## Cost and limits

- A command the hook checks takes about 0.04 s with a running server and about
  0.8 s when the hook starts one; other commands take about 0.03 s. A loaded
  server holds about 190 MB until it exits. On 2026-10-14 the check ran for 164
  of 6,972 Bash calls. Preparing a version takes about 6 s, once.
- The check gets the request's working directory and the session's original
  directory, and no additional working directories. A directory added with
  `--add-dir` or `/add-dir` is not in the hook input, so removing it or one of
  its parents passes the hook and still gets the harness prompt, and the check
  in the hook does not follow a `cd` into it.
- When Claude Code moves a session into or out of a worktree and changes only
  one of the two, its process's working directory or the session's original
  directory, the hook checks with the process's working directory and can judge
  a command with a `cd` differently from Claude Code.
- Commands the check flags wrongly are denied too, for example
  `d=x && index=$PWD/$d/index && rm -f "$index"`. The agent then rewrites the
  command instead of waiting for an answer.

Checked live on 2.1.296 in bypass mode: a flagged `rm` from the main agent and
from a background subagent was denied with no prompt, and an `rm` on an
ordinary literal path ran. Five hooks started at once against no server all
returned the verdict and left one server running.
