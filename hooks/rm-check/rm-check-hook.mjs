// PreToolUse hook for Bash: deny the removals Claude Code's dangerous-rm check flags.
//
// Claude Code asks a person to approve any rm/rmdir its "dangerousRemoval" safety
// check flags, even in bypass mode. In an interactive session the prompt's
// auto-deny timer stops at the first keypress, and a background subagent's prompt
// has no timer at all, so the agent waits until someone answers. This hook runs
// the same check before that prompt, using the running version's own Bash
// permission function, and denies with the check's text, so the agent gets the
// rewrite advice at once. Every other command, including rm on ordinary literal
// paths, passes through untouched.
//
// prepare.py (next to this file) extracts the version's bundle, finds the Bash
// tool and the two functions that set the check's directories with the
// structural matcher, and exports them; it runs once per version. Importing
// them and the self-test that checks them (rm-check-lib.mjs) take about 0.7 s,
// so rm-check-server.mjs keeps the check loaded per version and answers over
// a Unix socket; the hook starts the server when none answers, and the server
// exits after an idle period. When no server answers, the hook runs the check
// in its own process. When the hook itself fails, it fails open:
// it warns in the session, rings the terminal bell and sends a desktop
// notification, then exits 0 without a decision, so the command goes on to
// Claude Code's own permission check and a flagged rm gets the harness prompt.
//
// The check runs with the request's working directory (the hook input's cwd)
// and the session's original working directory, set the way Claude Code sets
// them (see rm-check-lib.mjs). Directories added with --add-dir or /add-dir are
// not in the hook input, so removing one of those (or a parent of one) passes
// here and still gets the harness prompt.
import { readFileSync, readlinkSync, statSync, existsSync, mkdirSync } from "node:fs";
import { spawn, spawnSync } from "node:child_process";
import net from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { LAYOUT, loadCheck } from "./rm-check-lib.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

// Warn as loudly as a hook can and let the command through. systemMessage is
// shown in the session, terminalSequence is a BEL that Claude Code emits for the
// hook, and notify-send posts a critical desktop notification over D-Bus, which
// works whatever the terminal (tmux does not pass OSC notifications on).
function failOpen(msg) {
  const text = String(msg);
  process.stderr.write(`rm-check hook: ${text}\n`);
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  const brief = (lines.length > 1 ? `${lines[0]} ${lines.at(-1)}` : lines[0] ?? "").slice(0, 300);
  try {
    spawnSync("notify-send", ["-u", "critical", "-a", "Claude Code rm-check", "-i", "dialog-warning",
      "rm-check hook failed", `${brief}\nClaude Code's own permission check still runs on the command.`],
      { timeout: 3000, stdio: "ignore" });
  } catch { /* the session warning below still reaches the user */ }
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
// whose executable is .../claude/versions/<version> (native install) or is named
// claude.
// An update deletes the binary that running sessions started from, and the
// kernel then reports their executable as "<path> (deleted)". The file stays
// readable through /proc/<pid>/exe for as long as the process runs, so that link
// is what gets read and executed in that case.
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
// when no server answers within timeoutMs.
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
// The check flags commands that mention rm or rmdir, and shell -c scripts it
// cannot fully check when a word in command position could be rm: a word that
// starts with $ or a brace expansion. Such a script is flagged with "runs rm"
// although its text never contains rm, for example an awk program whose
// `&& \$5 !~ ...` the check reads as a command named $5. Every other command
// skips the check: a check costs about 0.04 s with a running server and about
// 0.6 s when the hook has to start one.
const SHELL_C = /(?:^|[^\w.-])(?:(?:ba|z|da|mk|k|a|hu|rba)?sh|busybox|toybox|su|runuser)\b[^;&|\n]*?\s-[A-Za-z]*c(?![\w-])/;
if (!/\brm(?:dir)?\b/i.test(command) && !SHELL_C.test(command)) process.exit(0);

// RMCHECK_CLAUDE_VERSION runs another installed version's check (for testing).
const forced = process.env.RMCHECK_CLAUDE_VERSION;
const found = forced
  ? { exe: join(process.env.HOME ?? "", ".local/share/claude/versions", forced), version: forced }
  : claudeExecutable();
if (found === null) failOpen("could not find the Claude Code executable among the parent processes");
// The prepared directory and the server's socket are named after LAYOUT (see
// rm-check-lib.mjs); prepare.py writes into the directory named here.
const cacheBase = process.env.XDG_CACHE_HOME || join(process.env.HOME ?? "", ".cache");
const dir = join(cacheBase, "rm-check", found.version, `modules-v${LAYOUT}`);
if (!existsSync(join(dir, "manifest.json"))) {
  const prep = spawnSync("python3", ["-I", join(HERE, "prepare.py"), found.exe, found.version, dir],
    { encoding: "utf8", timeout: 300000 });
  if (prep.status !== 0) failOpen(`prepare.py failed for ${found.version}:\n${prep.stderr}`);
}
// The session's original working directory. Claude Code sets it to its own
// working directory when it starts, and where it moves the session to another
// directory (into or out of a worktree, on resume, for Remote Control), it
// mostly changes its working directory in the same step. So the hook reads the
// working directory of the Claude Code process. With RMCHECK_CLAUDE_VERSION
// there is no such process, and the hook takes CLAUDE_PROJECT_DIR, which Claude
// Code sets for hooks to the session's project directory, or else the
// request's cwd.
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
  failOpen(`refusing to use the socket directory ${sockDir}: it belongs to uid ${sd.uid} ` +
    `with mode ${(sd.mode & 0o777).toString(8)}, and it must belong to this user ` +
    `(uid ${process.getuid()}) with mode 700`);
const sockPath = join(sockDir, `${found.version}-v${LAYOUT}.sock`);

let reply = await ask(sockPath, request, 5000);
if (reply === null) {
  // Start a server and wait for it to load the check (about 0.5 s). Two hooks
  // starting one at the same moment is harmless: the second server finds the
  // first one answering and exits. When the server this hook started exits,
  // one more request decides whether another server answers.
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
        "check before the permission prompt, and the check flagged it; without the hook " +
        "it would wait for a person to approve it. Rewrite the removal so the check " +
        "passes, as its text below advises (usually a literal absolute path or a " +
        "${VAR:?} guard), and run it again. Deleting with python3 -I (pathlib unlink, " +
        "shutil.rmtree) on a literal absolute path you have checked also works. When " +
        "the check names a shell -c script it could not check, write the script to a " +
        "file and run it with sh <file>, or run its commands without sh -c.\n\n" +
        "The check's text: " + message,
    },
  }));
}
process.exit(0);
