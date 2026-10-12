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
// prepare.py (next to this file) extracts the version's bundle and exports the
// function; it runs once per version. When the hook itself fails, it fails open:
// it warns in the session, rings the terminal bell and sends a desktop
// notification, then exits 0 without a decision, so the command goes on to
// Claude Code's own permission check and a flagged rm gets the harness prompt.
//
// The hook passes only the session's working directory. Directories added with
// --add-dir or /add-dir are unknown to it, so removing one of those (or a parent
// of one) passes here and still gets the harness prompt.
import { readFileSync, readlinkSync, statSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

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
      "rm-check hook failed", `${brief}\nThe command went on to Claude Code's own check.`],
      { timeout: 3000, stdio: "ignore" });
  } catch { /* the session warning below still reaches the user */ }
  process.stdout.write(JSON.stringify({
    systemMessage:
      "⚠ rm-check hook FAILED; this command was not checked by it and goes on to " +
      "Claude Code's own permission check, where a flagged rm shows the approval " +
      `prompt again. Fix it in ~/claude-patches/hooks/rm-check (see its README).\n${text}`,
    terminalSequence: "\x07",
  }));
  process.exit(0);
}
process.on("uncaughtException", (e) => failOpen(`unexpected error: ${e?.stack ?? e}`));
process.on("unhandledRejection", (e) => failOpen(`unexpected error: ${e?.stack ?? e}`));

// The Claude Code executable and version: the nearest ancestor process whose
// executable is .../claude/versions/<version> (native install) or is named claude.
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
    if (v) return { exe, version: v[1] };
    if (/\/claude$/.test(name)) {
      const out = spawnSync(exe, ["--version"], { encoding: "utf8", timeout: 20000 });
      const m = /(\d+\.\d+\.\d+)/.exec(out.stdout ?? "");
      if (m) return { exe, version: m[1] };
    }
    const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
    pid = Number(stat.slice(stat.lastIndexOf(")") + 2).split(" ")[1]);
  }
  return null;
}

// The result carries a dangerousRemoval safety check somewhere in its decision
// reason (compound commands nest them under subcommandResults). Returns the
// message of the innermost result that carries it.
function dangerousRemovalMessage(result) {
  const isDanger = (r) => r?.type === "safetyCheck" &&
    (r.circuitBreaker === "dangerousRemoval" || (r.also ?? []).includes("dangerousRemoval"));
  const walk = (res) => {
    const r = res?.decisionReason;
    if (r?.type === "subcommandResults" && r.reasons instanceof Map) {
      for (const sub of r.reasons.values()) {
        const found = walk(sub);
        if (found !== undefined) return found;
      }
    }
    if (isDanger(r)) return String(res.message ?? r.reason ?? "");
    return undefined;
  };
  return walk(result);
}

const input = JSON.parse(readFileSync(0, "utf8"));
if (input.tool_name !== "Bash") process.exit(0);
const command = String(input.tool_input?.command ?? "");
// The check itself only looks at commands that mention rm or rmdir.
if (!/\brm(?:dir)?\b/i.test(command)) process.exit(0);

// RMCHECK_CLAUDE_VERSION runs another installed version's check (for testing).
const forced = process.env.RMCHECK_CLAUDE_VERSION;
const found = forced
  ? { exe: join(process.env.HOME ?? "", ".local/share/claude/versions", forced), version: forced }
  : claudeExecutable();
if (found === null) failOpen("could not find the Claude Code executable among the parent processes");
const dir = `/tmp/claude-${found.version}-rmcheck`;
if (!existsSync(join(dir, "manifest.json"))) {
  const prep = spawnSync("python3", ["-I", join(HERE, "prepare.py"), found.exe, found.version],
    { encoding: "utf8", timeout: 300000 });
  if (prep.status !== 0) failOpen(`prepare.py failed for ${found.version}:\n${prep.stderr}`);
}
const st = statSync(dir);
if (st.uid !== process.getuid() || (st.mode & 0o022) !== 0)
  failOpen(`${dir} is not owned by this user or is writable by others`);
const manifest = JSON.parse(readFileSync(join(dir, "manifest.json"), "utf8"));

try {
  if (input.cwd) process.chdir(input.cwd);
} catch { /* keep the hook's own cwd */ }

let result;
try {
  const mod = await import(join(dir, manifest.chunk));
  const check = mod[manifest.export];
  if (typeof check !== "function") failOpen(`${manifest.chunk} does not export ${manifest.export}`);
  const toolPermissionContext = {
    mode: input.permission_mode ?? "default",
    additionalWorkingDirectories: new Map(),
    alwaysAllowRules: {}, alwaysDenyRules: {}, alwaysAskRules: {},
    isBypassPermissionsModeAvailable: true,
    mcpPermissionModeOverrides: {},
  };
  result = await check({ command }, {
    getAppState: () => ({ toolPermissionContext }),
    abortController: new AbortController(),
  });
} catch (e) {
  failOpen(`the ${found.version} permission check threw: ${e?.stack ?? e}`);
}

const message = dangerousRemovalMessage(result);
if (message !== undefined) {
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
        "shutil.rmtree) on a literal absolute path you have checked also works.\n\n" +
        "The check's text: " + message,
    },
  }));
}
process.exit(0);
