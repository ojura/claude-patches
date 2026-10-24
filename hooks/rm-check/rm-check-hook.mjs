// PreToolUse hook for Bash: deny the removals that Claude Code's dangerous-removal check flags.
//
// Claude Code asks a person to approve any rm/rmdir that its "dangerousRemoval"
// safety check flags, even in bypass mode. In an interactive session, the
// prompt's auto-deny timer stops at the first keypress. A background subagent's
// prompt does not start that timer at all, so the agent waits until someone
// answers. This hook runs the same check before that prompt, by calling the
// Bash tool's checkPermissions from the Claude Code version that runs the
// session. When the check flags the command, the hook denies it with the
// check's text, so the agent at once gets the check's advice on how to rewrite
// it. For every other command, including rm on ordinary literal paths, the hook
// exits without a decision.
//
// prepare.py (next to this file) runs once per version. It extracts the
// version's bundle, uses pfg.bundle to find the Bash tool and the two functions
// that set the check's directories, and exports them from their modules.
// Importing them and running the self-test on them (rm-check-lib.mjs) take
// about 0.7 s, so rm-check-server.mjs keeps one version's check loaded and
// answers the hook over a Unix socket. The hook starts a server when its
// request over the socket is not answered, and the server exits once it has
// been idle for RMCHECK_IDLE_SECONDS. When the server that the hook started
// does not answer either, the hook runs the check in its own process. When the
// hook itself fails, it fails open. It warns in the session, rings the terminal
// bell, sends a desktop notification (at most one every 10 minutes) and exits 0
// without a decision. Claude Code then runs its own permission check on the
// command and asks a person to approve a flagged rm.
//
// The check runs with the request's working directory (the hook input's cwd)
// and the session's original working directory, which rm-check-lib.mjs sets
// with the same functions that Claude Code uses. Directories added with
// --add-dir or /add-dir are not in the hook input, so the hook lets through a
// command that removes one of them or a parent of one, and Claude Code then
// asks for approval.
import { readFileSync, readlinkSync, statSync, existsSync, mkdirSync, writeFileSync, renameSync } from "node:fs";
import { spawn, spawnSync } from "node:child_process";
import net from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { LAYOUT, loadCheck } from "./rm-check-lib.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

// While the hook is broken, it fails open on every command it checks, in every
// session, and a critical notification stays on screen until someone dismisses
// it. So the hook posts at most one desktop notification every
// NOTIFY_INTERVAL_MS across all sessions, and keeps the time of the last one in
// a file in the runtime directory. Two hooks that fail within the same few
// milliseconds can both post one. When the file cannot be read or written, the
// hook posts.
const NOTIFY_INTERVAL_MS = 10 * 60 * 1000;
function notificationDue() {
  try {
    const dir = join(process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid()}`, "rm-check");
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    const stamp = join(dir, "last-notification");
    let last = 0;
    try { last = Number(readFileSync(stamp, "utf8")) || 0; } catch { /* missing until the first notification */ }
    const now = Date.now();
    // A recorded time later than now, left by a clock set back, lets the hook post.
    if (now >= last && now - last < NOTIFY_INTERVAL_MS) return false;
    const tmp = `${stamp}.${process.pid}`;
    writeFileSync(tmp, String(now));
    renameSync(tmp, stamp);
  } catch { /* post this one */ }
  return true;
}

// Warn as loudly as a hook can and let the command through. Claude Code shows
// systemMessage in the session and writes terminalSequence, here a BEL, to the
// terminal for the hook. notify-send posts a critical desktop notification over
// D-Bus, which works in any terminal, also inside tmux, where OSC notifications
// do not get through. The session warning and the bell come with every failed
// check, and the desktop notification at most once every NOTIFY_INTERVAL_MS.
function failOpen(msg) {
  const text = String(msg);
  process.stderr.write(`rm-check hook: ${text}\n`);
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  const brief = (lines.length > 1 ? `${lines[0]} ${lines.at(-1)}` : lines[0] ?? "").slice(0, 300);
  if (notificationDue()) {
    try {
      spawnSync("notify-send", ["-u", "critical", "-a", "Claude Code rm-check", "-i", "dialog-warning",
        "rm-check hook failed", `${brief}\nClaude Code's own permission check still runs. Further ` +
        "failures in the next 10 minutes show only in the Claude Code sessions."],
        { timeout: 3000, stdio: "ignore" });
    } catch { /* the session warning below still reaches the user */ }
  }
  process.stdout.write(JSON.stringify({
    systemMessage:
      "⚠ The rm-check hook failed and did not check this command. Claude Code's own " +
      "permission check still runs, and asks for approval when it flags an rm. " +
      `To fix the hook, see ~/claude-patches/hooks/rm-check/README.md.\n${text}`,
    terminalSequence: "\x07",
  }));
  process.exit(0);
}
process.on("uncaughtException", (e) => failOpen(`unexpected error: ${e?.stack ?? e}`));
process.on("unhandledRejection", (e) => failOpen(`unexpected error: ${e?.stack ?? e}`));

// The Claude Code process, executable and version: the nearest ancestor process
// whose executable is .../claude/versions/<version> (a native install), or is
// named claude and prints its version with --version. Returns null when the walk
// up the ancestors does not find one. The walk stops after 10 levels, and at the
// first process whose executable link it cannot read.
// An update deletes the binary that running sessions started from, and the
// kernel then reports their executable as "<path> (deleted)". The file stays
// readable through /proc/<pid>/exe for as long as the process runs, so for a
// deleted binary the hook uses that link as the executable, both to run it with
// --version and to pass it to prepare.py.
function claudeExecutable() {
  let pid = process.ppid;
  for (let i = 0; i < 10 && pid > 1; i++) {
    let link;
    try { link = readlinkSync(`/proc/${pid}/exe`); } catch { break; }
    const deleted = link.endsWith(" (deleted)");
    const name = deleted ? link.slice(0, -" (deleted)".length) : link;
    const exe = deleted ? `/proc/${pid}/exe` : link;
    const v = /\/claude\/versions\/(\d+\.\d+\.\d+)$/.exec(name);
    if (v) return { pid, exe, version: v[1] };
    if (/\/claude$/.test(name)) {
      const out = spawnSync(exe, ["--version"], { encoding: "utf8", timeout: 20000 });
      const m = /(\d+\.\d+\.\d+)/.exec(out.stdout ?? "");
      if (m) return { pid, exe, version: m[1] };
    }
    const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
    pid = Number(stat.slice(stat.lastIndexOf(")") + 2).split(" ")[1]);
  }
  return null;
}

// Sends one request to the server at sockPath and returns its reply, or null
// when the connection fails, the reply is not JSON, or the server does not
// answer within timeoutMs.
function ask(sockPath, request, timeoutMs) {
  return new Promise((resolve) => {
    let buf = "";
    const sock = net.createConnection(sockPath);
    const done = (v) => { clearTimeout(timer); sock.destroy(); resolve(v); };
    const timer = setTimeout(() => done(null), timeoutMs);
    sock.setEncoding("utf8");
    sock.on("connect", () => sock.write(JSON.stringify(request) + "\n"));
    sock.on("data", (d) => { buf += d; });
    sock.on("end", () => { try { done(JSON.parse(buf)); } catch { done(null); } });
    sock.on("error", () => done(null));
  });
}

const input = JSON.parse(readFileSync(0, "utf8"));
if (input.tool_name !== "Bash") process.exit(0);
const command = String(input.tool_input?.command ?? "");
// The check can flag only commands that mention rm or rmdir, and shell -c
// scripts that it cannot fully check when a word in command position could be
// rm: a word that starts with $, or a brace expansion. The check's text for such
// a script says that it "runs rm", even when the script does not contain rm. An
// example is an awk program with `&& \$5 !~ ...`, where the check reads $5 as the
// name of a command. The hook skips the check for every other command, because
// a check costs about 0.04 s with a running server and about 0.8 s when the hook
// has to start one.
const SHELL_C = /(?:^|[^\w.-])(?:(?:ba|z|da|mk|k|a|hu|rba)?sh|busybox|toybox|su|runuser)\b[^;&|\n]*?\s-[A-Za-z]*c(?![\w-])/;
if (!/\brm(?:dir)?\b/i.test(command) && !SHELL_C.test(command)) process.exit(0);

// With RMCHECK_CLAUDE_VERSION set, the hook runs the check of the installed
// version that it names (for testing).
const forced = process.env.RMCHECK_CLAUDE_VERSION;
const found = forced
  ? { exe: join(process.env.HOME ?? "", ".local/share/claude/versions", forced), version: forced }
  : claudeExecutable();
if (found === null) failOpen("the hook could not find the Claude Code executable among its parent processes");
// The names of the prepared directory and of the server's socket contain LAYOUT
// (see rm-check-lib.mjs). prepare.py writes into the directory chosen here.
const cacheBase = process.env.XDG_CACHE_HOME || join(process.env.HOME ?? "", ".cache");
const dir = join(cacheBase, "rm-check", found.version, `modules-v${LAYOUT}`);
if (!existsSync(join(dir, "manifest.json"))) {
  const prep = spawnSync("python3", ["-I", join(HERE, "prepare.py"), found.exe, found.version, dir],
    { encoding: "utf8", timeout: 300000 });
  if (prep.status !== 0) failOpen(`prepare.py failed for ${found.version}:\n${prep.stderr}`);
}
// The session's original working directory. Claude Code sets it to its own
// working directory when it starts, and in most places where it moves the
// session to another directory (into or out of a worktree, on resume, for
// Remote Control), it changes its own working directory in the same step. So
// the hook reads the working directory of the Claude Code process. With
// RMCHECK_CLAUDE_VERSION set, the hook does not look for that process, and it
// takes CLAUDE_PROJECT_DIR, which Claude Code sets for hooks to the session's
// project directory, or else the request's cwd. It does the same when it
// cannot read the process's working directory.
function originalCwd(pid) {
  if (pid !== undefined) {
    try { return readlinkSync(`/proc/${pid}/cwd`).replace(/ \(deleted\)$/, ""); } catch { /* below */ }
  }
  return process.env.CLAUDE_PROJECT_DIR || input.cwd;
}
const request = {
  command, cwd: input.cwd, originalCwd: originalCwd(found.pid),
  mode: input.permission_mode ?? "default",
};

// The server's socket lives in a directory only this user can enter.
const sockDir = join(process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid()}`, "rm-check");
mkdirSync(sockDir, { recursive: true, mode: 0o700 });
const sd = statSync(sockDir);
if (sd.uid !== process.getuid() || (sd.mode & 0o077) !== 0)
  failOpen(`the socket directory ${sockDir} belongs to uid ${sd.uid} and has mode ` +
    `${(sd.mode & 0o777).toString(8)}, and the hook uses it only when it belongs to this user ` +
    `(uid ${process.getuid()}) and only its owner has permissions on it (mode 700)`);
const sockPath = join(sockDir, `${found.version}-v${LAYOUT}.sock`);

let reply = await ask(sockPath, request, 5000);
if (reply === null) {
  // Start a server and wait for it to load the check (about 0.7 s). When two
  // hooks start a server at the same moment, the second server finds the first
  // one answering and exits. When the server that this hook started exits, the
  // hook sends one more request, which a server started by another hook may
  // answer.
  const child = spawn(process.execPath, [join(HERE, "rm-check-server.mjs"), dir, sockPath],
    { detached: true, stdio: "ignore" });
  let exited = false;
  child.on("exit", () => { exited = true; });
  child.unref();
  for (let waited = 0; reply === null && !exited && waited < 10000; waited += 100) {
    await new Promise((r) => setTimeout(r, 100));
    reply = await ask(sockPath, request, 5000);
  }
  if (reply === null && exited) reply = await ask(sockPath, request, 5000);
}
if (reply === null) {
  try { reply = { message: await (await loadCheck(dir))(request) }; }
  catch (e) { failOpen(`the ${found.version} permission check could not be loaded or run: ${e?.stack ?? e}`); }
}
if (reply.error !== undefined) failOpen(`the ${found.version} rm-check server failed: ${reply.error}`);

const message = reply.message;
if (message !== null && message !== undefined) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason:
        "This command was not run. A PreToolUse hook ran Claude Code's dangerous-removal " +
        "check before the permission prompt, and the check flagged the command. Without the " +
        "hook, the command would wait for a person to approve it. Rewrite the removal so that " +
        "the check passes, as the check's text below advises (usually with a literal absolute " +
        "path or a ${VAR:?} guard), and run it again. You can also delete with python3 -I " +
        "(pathlib's unlink, shutil.rmtree) when the target is a literal absolute path that you " +
        "have checked. When the check flags a shell -c script that it could not fully check, write " +
        "the script to a file and run it with sh <file>, or run its commands without sh -c.\n\n" +
        "The check's text: " + message,
    },
  }));
}
process.exit(0);
