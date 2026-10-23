// Loading and running the dangerous-removal check, shared by the hook and the
// server. Importing a version's prepared chunks takes about 0.45 s because they
// pull in a large part of the bundle; one check then takes 2 to 50 ms.
import { readFileSync } from "node:fs";
import { join } from "node:path";

// The format of the prepared directory, which prepare.py writes and loadCheck
// reads. The hook puts this number in the prepared directory's name and in the
// server's socket name, so when it changes, new hooks prepare a new directory
// and start new servers, while a server running the previous code gets no more
// requests and exits when its idle time runs out.
export const LAYOUT = 3;

// The result carries a dangerousRemoval safety check somewhere in its decision
// reason (compound commands nest them under subcommandResults). Returns the
// message of the innermost result that carries it.
export function dangerousRemovalMessage(result) {
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

function permissionContext(mode) {
  const toolPermissionContext = {
    mode,
    additionalWorkingDirectories: new Map(),
    alwaysAllowRules: {}, alwaysDenyRules: {}, alwaysAskRules: {},
    isBypassPermissionsModeAvailable: true,
    mcpPermissionModeOverrides: {},
  };
  return { getAppState: () => ({ toolPermissionContext }), abortController: new AbortController() };
}

// The self-test's directories. The check only compares and resolves these
// paths; nothing creates them.
const PROBE_SESSION = "/nonexistent-rm-check-probe";
const PROBE_CWD = `${PROBE_SESSION}/child`;
const PROBE_COMMAND = `cd / && rm -rf ${PROBE_CWD}`;
// The check removes its working directory after a cd that it follows only into
// the session's working directories, which here are the session's original
// directory alone.
const PROBES = [
  { originalCwd: PROBE_SESSION, cwd: PROBE_CWD, flagged: true,
    why: "the cd leaves the session's directories, so the check does not follow it and the rm " +
      "removes the working directory" },
  { originalCwd: PROBE_SESSION, cwd: PROBE_SESSION, flagged: false,
    why: "the rm removes a subdirectory of the working directory" },
  { originalCwd: "/", cwd: PROBE_CWD, flagged: false,
    why: "the check follows the cd to /, and the target is neither / nor a parent of /" },
];
const PROBE_TIMEOUT_MS = 5000;

// Calls fn and returns {value} with its result, or {error} when it throws or
// does not answer within PROBE_TIMEOUT_MS.
async function attempt(fn) {
  let timer;
  const late = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`no answer within ${PROBE_TIMEOUT_MS} ms`)), PROBE_TIMEOUT_MS);
  });
  try {
    return { value: await Promise.race([Promise.resolve().then(fn), late]) };
  } catch (e) {
    return { error: String(e?.message ?? e).split("\n")[0] };
  } finally {
    clearTimeout(timer);
  }
}

// Imports the check from a prepared directory and returns a function that runs
// it for one command and permission mode, in the given working directory of a
// session that started in originalCwd. It returns the check's text when the
// command is flagged, and null otherwise.
//
// The check reads two directories:
//   - the working directory: from the AsyncLocalStorage store that Claude Code
//     sets with runInCwd(dir, fn) around a session's or a subagent's work, and
//     without the store, from the session state;
//   - the session's original working directory (originalCwd), which counts as
//     a working directory of the session together with the --add-dir
//     directories: from the session state, where Claude Code changes it with
//     setOriginalCwd when it moves a session to another directory.
// When the bundle loads, it sets both in the session state to the directory the
// process started in, and process.chdir changes neither. So each check first
// sets originalCwd, then runs inside runInCwd with the request's working
// directory. originalCwd is process-wide, so calls must not overlap.
//
// prepare.py lists candidates for the three functions, found by the structure
// the hook depends on. Here the check is the one candidate that flags
// `rm -rf /`, and exactly one pair of directory functions must make it give
// PROBES' verdicts. Anything else throws with what each candidate did, and the
// hook fails open with that text.
export async function loadCheck(dir) {
  const manifest = JSON.parse(readFileSync(join(dir, "manifest.json"), "utf8"));
  const chunks = new Map();
  const candidates = async (role) => {
    const found = [];
    for (const { chunk, fn } of manifest[role] ?? []) {
      if (!chunks.has(chunk)) chunks.set(chunk, (await import(join(dir, chunk)))[manifest.export] ?? {});
      const f = chunks.get(chunk)[fn];
      if (typeof f === "function") found.push({ name: `${fn} in ${chunk}`, f });
    }
    if (found.length === 0) {
      throw new Error(`the ${manifest.version} bundle offers no ${role} function: none of prepare.py's ` +
        `candidates ${JSON.stringify(manifest[role])} is a function at the module level of its chunk`);
    }
    return found;
  };
  const checks = await candidates("check");
  const runs = await candidates("runInCwd");
  const origins = await candidates("setOriginalCwd");

  const rootFlaggers = [];
  const rootReport = [];
  for (const c of checks) {
    const r = await attempt(() => c.f({ command: "rm -rf /" }, permissionContext("bypassPermissions")));
    const message = r.error === undefined ? dangerousRemovalMessage(r.value) : undefined;
    rootReport.push(`${c.name} ${r.error !== undefined ? `threw: ${r.error}` : message !== undefined ? "flagged it" : "did not flag it"}`);
    if (message !== undefined) rootFlaggers.push(c);
  }
  if (rootFlaggers.length !== 1) {
    throw new Error(`the ${manifest.version} check could not be identified: of the functions the Bash tool ` +
      `passes its shell permission context to, ${rootFlaggers.length} flag "rm -rf /" as a dangerous ` +
      `removal, where exactly 1 should: ${rootReport.join("; ")}`);
  }
  const check = rootFlaggers[0].f;

  const run = async (runInCwd, setOriginalCwd, { command, cwd, originalCwd, mode }) => {
    setOriginalCwd(originalCwd);
    const result = await runInCwd(cwd, () => check({ command }, permissionContext(mode)));
    return dangerousRemovalMessage(result) ?? null;
  };
  const passing = [];
  const pairReport = [];
  for (const r of runs) {
    for (const o of origins) {
      const wrong = [];
      for (const [i, p] of PROBES.entries()) {
        const got = await attempt(() => run(r.f, o.f, { command: PROBE_COMMAND, cwd: p.cwd,
          originalCwd: p.originalCwd, mode: "bypassPermissions" }));
        if (got.error !== undefined) wrong.push(`probe ${i + 1} threw: ${got.error}`);
        else if ((got.value !== null) !== p.flagged) {
          wrong.push(`probe ${i + 1} (session ${p.originalCwd}, cwd ${p.cwd}) was ` +
            `${got.value !== null ? "" : "not "}flagged, although ${p.why}`);
        }
      }
      pairReport.push(`${r.name} with ${o.name}: ${wrong.length === 0 ? "passed" : wrong.join("; ")}`);
      if (wrong.length === 0) passing.push([r.f, o.f]);
    }
  }
  if (passing.length !== 1) {
    throw new Error(`the ${manifest.version} check does not take its directories from exactly one pair of ` +
      `prepare.py's runInCwd and setOriginalCwd candidates (${passing.length} pairs pass) for ` +
      `"${PROBE_COMMAND}": ${pairReport.join(". ")}`);
  }
  const [runInCwd, setOriginalCwd] = passing[0];
  const home = process.env.HOME ?? "/";
  return ({ command, cwd, originalCwd, mode }) => run(runInCwd, setOriginalCwd, {
    command, cwd: cwd || home, originalCwd: originalCwd || cwd || home, mode: mode ?? "default",
  });
}
