// Loads and runs the dangerous-removal check for the hook and the server.
// Importing a version's prepared modules takes about 0.45 s, because they import
// a large part of the bundle. After that, one check takes 1 to 50 ms.
import { readFileSync } from "node:fs";
import { join } from "node:path";

// The number of the format of the prepared directory that prepare.py writes and
// loadCheck reads. The hook puts this number in the prepared directory's name
// and in the server's socket name. So when it changes, new hooks prepare a new
// directory and start new servers, and a server that runs the previous code
// stops getting requests and exits when its idle time runs out.
export const LAYOUT = 4;

// Finds the first result whose decisionReason is a safetyCheck with
// circuitBreaker dangerousRemoval, or with dangerousRemoval in its also list,
// and returns its message, or the safetyCheck's reason when the result does not
// have a message. For a compound command, it looks at the results of the
// subcommands (subcommandResults) before the command's own result. Returns
// undefined when it does not find such a result.
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

// The parts of a tool-use context that the Bash tool's checkPermissions reads
// for a command: the permission settings, options.isNonInteractiveSession
// (which 2.1.309 reads), the abort controller, the tool-use id, and the
// session's writePermissionStash, which 2.1.303 to 2.1.305 call when the
// command edits a file with sed.
function toolUseContext(mode) {
  const toolPermissionContext = {
    mode,
    additionalWorkingDirectories: new Map(),
    alwaysAllowRules: {}, alwaysDenyRules: {}, alwaysAskRules: {},
    isBypassPermissionsModeAvailable: true,
    mcpPermissionModeOverrides: {},
  };
  return {
    getAppState: () => ({ toolPermissionContext }),
    options: { isNonInteractiveSession: false },
    abortController: new AbortController(),
    toolUseId: "rm-check",
    session: { writePermissionStash: { stash() {} } },
  };
}

// The self-test's directories. The check only compares and resolves these
// paths, so they do not have to exist.
const PROBE_SESSION = "/nonexistent-rm-check-probe";
const PROBE_CWD = `${PROBE_SESSION}/child`;
const PROBE_COMMAND = `cd / && rm -rf ${PROBE_CWD}`;
// PROBE_COMMAND removes PROBE_CWD after `cd /`. The check follows a cd only
// into the session's working directories, which here are the session's original
// directory alone, so each probe's verdict depends on both directories.
const PROBES = [
  { originalCwd: PROBE_SESSION, cwd: PROBE_CWD, flagged: true,
    why: "the cd leaves the session's directories, so the check does not follow it and the rm " +
      "removes the working directory" },
  { originalCwd: PROBE_SESSION, cwd: PROBE_SESSION, flagged: false,
    why: "the rm removes a subdirectory of the working directory" },
  { originalCwd: "/", cwd: PROBE_CWD, flagged: false,
    why: "the check follows the cd to /, which becomes the working directory, and the rm removes a " +
      "subdirectory of it" },
];
const PROBE_TIMEOUT_MS = 5000;

// Calls fn and returns {value} with its result, or {error} when fn throws or
// takes longer than PROBE_TIMEOUT_MS.
async function attempt(fn) {
  let timer;
  const late = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`timed out after ${PROBE_TIMEOUT_MS} ms`)), PROBE_TIMEOUT_MS);
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
// session that started in originalCwd. That function returns the check's text
// when the check flags the command, and null otherwise.
//
// The check reads two directories:
//   - the working directory, from the AsyncLocalStorage store that Claude Code
//     sets with runInCwd(dir, fn) around the work of a session or a subagent,
//     or from the session state when the check runs outside runInCwd;
//   - the session's original working directory (originalCwd), from the session
//     state. Together with the --add-dir directories, it is one of the
//     session's working directories, and Claude Code changes it with
//     setOriginalCwd when it moves a session to another directory.
// When the bundle loads, it sets both in the session state to the directory the
// process started in, and process.chdir does not change them. So each check
// first sets originalCwd and then runs inside runInCwd with the request's
// working directory. originalCwd is process-wide, so calls must not overlap.
//
// prepare.py finds the candidates for the Bash tool and the two directory
// functions with pfg.bundle and lists them in manifest.json. The check is the
// checkPermissions of the one candidate tool that flags `rm -rf /`, and exactly
// one pair of directory functions must make it give the verdicts in PROBES. In
// every other case loadCheck throws an error that says what each candidate did,
// and the hook fails open with that message.
export async function loadCheck(dir) {
  const manifest = JSON.parse(readFileSync(join(dir, "manifest.json"), "utf8"));
  const candidates = async (role, usable) => {
    const found = [];
    for (const { chunk, name, export: exported } of manifest[role] ?? []) {
      const value = (await import(join(dir, chunk)))[exported];
      if (usable(value)) found.push({ name: `${name} in ${chunk}`, value });
    }
    if (found.length === 0) {
      throw new Error(`the ${manifest.version} bundle offers no usable ${role}: prepare.py found ` +
        `${JSON.stringify(manifest[role])}`);
    }
    return found;
  };
  const isFunction = (v) => typeof v === "function";
  const tools = await candidates("tool", (v) => isFunction(v?.checkPermissions));
  const runs = await candidates("runInCwd", isFunction);
  const origins = await candidates("setOriginalCwd", isFunction);

  const rootFlaggers = [];
  const rootReport = [];
  for (const t of tools) {
    const r = await attempt(() => t.value.checkPermissions({ command: "rm -rf /" }, toolUseContext("bypassPermissions")));
    const message = r.error === undefined ? dangerousRemovalMessage(r.value) : undefined;
    rootReport.push(`${t.name} ${r.error !== undefined ? `threw: ${r.error}` : message !== undefined ? "flagged it" : "did not flag it"}`);
    if (message !== undefined) rootFlaggers.push(t);
  }
  if (rootFlaggers.length !== 1) {
    throw new Error(`the ${manifest.version} check could not be identified: of the Bash tools prepare.py ` +
      `found, ${rootFlaggers.length} flag "rm -rf /" as a dangerous removal, where exactly 1 should: ` +
      rootReport.join("; "));
  }
  const tool = rootFlaggers[0].value;

  const run = async (runInCwd, setOriginalCwd, { command, cwd, originalCwd, mode }) => {
    setOriginalCwd(originalCwd);
    const result = await runInCwd(cwd, () => tool.checkPermissions({ command }, toolUseContext(mode)));
    return dangerousRemovalMessage(result) ?? null;
  };
  const passing = [];
  const pairReport = [];
  for (const r of runs) {
    for (const o of origins) {
      const wrong = [];
      for (const [i, p] of PROBES.entries()) {
        const got = await attempt(() => run(r.value, o.value, { command: PROBE_COMMAND, cwd: p.cwd,
          originalCwd: p.originalCwd, mode: "bypassPermissions" }));
        if (got.error !== undefined) wrong.push(`probe ${i + 1} threw: ${got.error}`);
        else if ((got.value !== null) !== p.flagged) {
          wrong.push(`probe ${i + 1} (session ${p.originalCwd}, cwd ${p.cwd}) was ` +
            `${got.value !== null ? "" : "not "}flagged, although ${p.why}`);
        }
      }
      pairReport.push(`${r.name} with ${o.name}: ${wrong.length === 0 ? "passed" : wrong.join("; ")}`);
      if (wrong.length === 0) passing.push([r.value, o.value]);
    }
  }
  if (passing.length !== 1) {
    throw new Error(`the ${manifest.version} directory functions could not be identified: exactly one pair ` +
      `of prepare.py's runInCwd and setOriginalCwd candidates should make the check give the expected ` +
      `verdicts for "${PROBE_COMMAND}", and ${passing.length} pairs pass: ${pairReport.join(". ")}`);
  }
  const [runInCwd, setOriginalCwd] = passing[0];
  const home = process.env.HOME ?? "/";
  return ({ command, cwd, originalCwd, mode }) => run(runInCwd, setOriginalCwd, {
    command, cwd: cwd || home, originalCwd: originalCwd || cwd || home, mode: mode ?? "default",
  });
}
