# rm-check: deny flagged removals before Claude Code's permission prompt

Claude Code's Bash permission check flags an `rm` or `rmdir` that can remove far
more than intended: a target built from a variable that may be empty, the output
of a command substitution, a relative glob after `cd`, or a system, home or
workspace directory. For a flagged call, Claude Code always asks a person, even
in bypass mode. In an interactive session, the prompt's auto-deny timer stops at
the first keypress. A background subagent's prompt does not start that timer at
all, so the agent waits until someone answers.

`rm-check-hook.mjs` is a PreToolUse hook that runs the same check before Claude
Code does. When the check flags a command, the hook denies it with the check's
message, so the agent at once gets the check's advice on how to rewrite the
command. The hook takes the check from the Claude Code version that runs the
session, so its verdicts are Claude Code's own, with the exceptions listed under
"Cost and limits". For every command that the check does not flag, including
`rm` on ordinary literal paths, the hook exits without a decision, and Claude
Code handles the command as it would without the hook.

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
2. finds three things with `pfg.bundle`, which parses the modules with the
   TypeScript parser and answers questions about their syntax and bindings:
   - the Bash tool: a module-level `var T = F({name: N, ..., searchHint:
     "execute shell commands", ...})` where `N` is the string `"Bash"`, or a
     variable initialized with it, either in the same module or in the module
     that `N` is imported from. The hook calls `T.checkPermissions`, the method
     that Claude Code calls for the tool's permission verdict, so the steps
     that Claude Code wraps around the check run too;
   - runInCwd: a module-level `function F(dir, fn) {return S.run({cwd: dir},
     fn)}`, also with `dir` passed through a call, as in `{cwd: G(dir)}`. It
     runs `fn` with `{cwd: dir}` as the store of the AsyncLocalStorage `S`.
     Claude Code runs the work of the session and of each subagent inside this
     function, and the check reads its working directory from that store;
   - setOriginalCwd: a module-level `function F(dir) {U({originalCwd:
     dir})}`, which sets the session's original working directory;
3. writes the bundle's modules to `~/.cache/rm-check/<version>/modules-v4/`,
   appends an export statement to each module that declares one of the names
   it found, and lists the names and their modules in `manifest.json`.

`prepare.py` parses only the modules that contain one of the texts in
`NOMINATE`. On 2.1.309 these are 5 modules, which take about 4 s to parse,
where the whole bundle takes 11 s. `prepare.py` tries every anchor, and when one
fails, the error names each anchor that found 0 matches or more than 8.
`pfg.bundle` runs its helper on `node`.

When `rm-check-lib.mjs` loads the modules that the manifest lists, it calls
what `prepare.py` found and checks the results. Exactly one tool's
`checkPermissions` must flag `rm -rf /`, and exactly one pair of directory
functions must make that check give the expected verdicts on three probes
(`PROBES` in `rm-check-lib.mjs`), which use made-up paths under
`/nonexistent-rm-check-probe`. Otherwise `rm-check-lib.mjs` throws an error
that says what each candidate did, and the hook fails open with that message.

The anchors match only the code that the hook relies on, because the code
around it changes between releases. 2.1.309, for example, removed the
statements that the Bash tool's `checkPermissions` began with. The method now
wraps the permission function in a sandbox-override step, and the permission
function runs the check again on a retry.

The hook imports and runs code from the extracted bundle and the prepared
modules, so they are kept under your home directory, where other accounts cannot
create or change them.

After it prepares a version, `prepare.py` removes:

- the cache directory of each version that is neither installed under
  `~/.local/share/claude/versions/` nor the executable of a running process;
- the modules directories of other layouts (see below);
- the `/tmp/claude-<version>-rmcheck/` directories that earlier revisions of
  the hook wrote.

It keeps every directory that a running server was started with.

Importing the modules that the manifest lists takes about 0.45 s, because they
import a large part of the bundle. The self-test takes about 0.2 s, and after
that one check takes 1 to 50 ms. `rm-check-server.mjs` keeps a version's check
loaded and answers the hook over the Unix socket
`$XDG_RUNTIME_DIR/rm-check/<version>-v4.sock`, in a directory that only this
user can enter. The hook sends its request to that server. When it does not get
an answer, it starts a server and waits for it, and when that server does not
answer either, the hook imports the modules and runs the check itself.
`rm-check-lib.mjs` contains the code that both use. A server runs one check at
a time, because each check sets the session's original working directory,
which the bundle keeps in process-wide state. When a server has not had a
request for `RMCHECK_IDLE_SECONDS` seconds (default 600), it removes its socket
and exits.

The hook denies a command when the decisionReason of the check's result, or of
the result for one of its subcommands, is a safetyCheck whose circuitBreaker is
`dangerousRemoval` or whose `also` list contains `dangerousRemoval`. On a new
version, the hook works unchanged as long as the anchors find their code and the
self-test passes. When one of them fails, `prepare.py` names the anchors that
failed, or the self-test says what each candidate did.

The check's verdict depends on two directories. It resolves relative targets
against the working directory, and it flags an `rm` that would remove the
working directory or one of its parents. The session's original working
directory is the directory that the session started in. Together with the
`--add-dir` directories, it forms the session's working directories, and the
check follows a `cd` in the command only when it leads into one of them. For
example, in a session that started in `/tmp` and whose working directory was
`/tmp/cpa-deploy-7237e8a8`, Claude Code flagged
`cd /home/juraj && rm -rf /tmp/cpa-deploy-7237e8a8` as "Dangerous rm operation
on working directory or its ancestor". `/home/juraj` is outside `/tmp`, so the
check did not follow the `cd` and treated the `rm` as one that removes the
working directory.

When the bundle loads, it sets both directories to the directory that the
process started in, and `process.chdir` does not change them. A server that did
not set them would check every command with the directory it started in. So
before each check, `rm-check-lib.mjs` sets the session's original directory
with setOriginalCwd, and it runs the check inside runInCwd with the request's
working directory, the hook input's `cwd`.

The hook takes the session's original directory from the working directory of
the Claude Code process among its parents. Claude Code sets the session's
original directory to its own working directory when it starts. In most places
where it moves the session to another directory (into or out of a worktree, on
resume, for Remote Control), it changes its working directory and the
session's original directory in the same step. When `RMCHECK_CLAUDE_VERSION`
selects a version for testing, the hook does not look for a Claude Code
process. It then takes `CLAUDE_PROJECT_DIR`, which Claude Code sets for hooks to
the session's project directory, or, when that is not set, the request's `cwd`.

A running server keeps the code it loaded. A change to `rm-check-lib.mjs` or
`rm-check-server.mjs` takes effect for a version's checks once that version's
server has exited. A change to the prepared files or to the manifest must come
with a higher `LAYOUT` in `rm-check-lib.mjs` (now 4). `LAYOUT` is part of the
name of the modules directory (`modules-v4`) and of the socket, so new hooks
at once prepare a new directory and start a new server, and the old server
exits when its idle time runs out.

The hook fails open on every failure, including a `prepare.py` failure when an
anchor no longer finds its code, and unexpected exceptions. It shows the
warning "⚠ The rm-check hook failed" with the cause in the session
(`systemMessage`), rings the terminal bell (`terminalSequence`), posts a
critical desktop notification with `notify-send`, and exits 0 without a
decision. Claude Code then runs its own permission check on the command and
asks a person to approve a flagged rm, as it would without the hook. The
session warning and the bell repeat on every command that the hook checks,
until the hook is fixed. The hook posts the desktop notification at most once
every 10 minutes across all sessions, and keeps the time of the last one in
`$XDG_RUNTIME_DIR/rm-check/last-notification`.

The hook runs the check on commands that mention `rm` or `rmdir`, and on
commands that pass a script with `-c` to `sh`, `bash`, `zsh`, `dash`, `ksh`,
`mksh`, `ash`, `hush`, `rbash`, `busybox`, `toybox`, `su` or `runuser`. The
check also flags a `-c` script that it cannot fully check when a word in
command position could be `rm`: a word that starts with `$`, or a brace
expansion. Its message then says that the script "runs rm", even when the
script does not contain `rm`. An awk program with `&& \$5 !~ ...` inside
`sh -c '...'` gets this message, because the check reads `$5` as the name of a
command.

The anchors find their matches, and the self-test passes, on 2.1.303, 2.1.304,
2.1.305 and 2.1.309. After changing the anchors for a new version, run
`check_versions.py <new> <old>`. It runs a fixed list of commands through the
hook for each version, among them one that removes the request's working
directory after a `cd`, and reports whether the versions gave the same
verdicts. `util/test_rm_check_hook.py` repeats the `/tmp/cpa-deploy-7237e8a8`
example above with new directories on the newest installed version, and checks
that the verdicts are the same whichever directory the hook and its server
started in. It also runs the self-test on a fake module that contains the
three things together with look-alikes of them, and runs the anchors on small
synthetic modules.

## Cost and limits

- A command that the hook checks takes about 0.04 s with a running server and
  about 0.8 s when the hook has to start the server. Other commands take about
  0.03 s. A server uses about 190 MB of memory until it exits. On 2026-10-14
  the hook ran the check on 164 of 6,972 Bash calls. Preparing a version takes
  about 6 s and happens once per version.
- The check gets the request's working directory and the session's original
  directory. It does not get the additional working directories, because a
  directory added with `--add-dir` or `/add-dir` is not in the hook input. So
  the hook lets through a command that removes such a directory or one of its
  parents, and Claude Code then asks for approval. The check in the hook also
  does not follow a `cd` into such a directory.
- When Claude Code moves a session into or out of a worktree and changes only
  one of two directories, the working directory of its process or the
  session's original directory, the hook still takes the process's working
  directory, and it can judge a command that contains a `cd` differently from
  Claude Code.
- The hook also denies commands that the check flags wrongly, for example
  `d=x && index=$PWD/$d/index && rm -f "$index"`. The agent then rewrites the
  command instead of waiting for an answer.

In a live test on 2.1.296 in bypass mode, the hook denied a flagged `rm` from
the main agent and one from a background subagent, and Claude Code did not
prompt for either. An `rm` on an ordinary literal path ran. When five hooks
started at the same moment with the server stopped, all five returned the
expected verdict, and one server was left running.
