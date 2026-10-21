// Loading and running the dangerous-removal check, shared by the hook and the
// server. Importing a version's prepared chunk takes about 0.45 s because it
// pulls in a large part of the bundle; one check then takes 2 to 50 ms.
import { readFileSync } from "node:fs";
import { join } from "node:path";

// The format of the prepared directory, which prepare.py writes and loadCheck
// reads. The hook puts this number in the prepared directory's name and in the
// server's socket name, so when it changes, new hooks prepare a new directory
// and start new servers, while a server running the previous code gets no more
// requests and exits when its idle time runs out.
export const LAYOUT = 2;

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
export async function loadCheck(dir) {
  const manifest = JSON.parse(readFileSync(join(dir, "manifest.json"), "utf8"));
  const load = async ({ chunk, export: name }) => {
    const fn = (await import(join(dir, chunk)))[name];
    if (typeof fn !== "function") throw new Error(`${chunk} does not export ${name}`);
    return fn;
  };
  const check = await load(manifest.check);
  const runInCwd = await load(manifest.runInCwd);
  const setOriginalCwd = await load(manifest.setOriginalCwd);
  const home = process.env.HOME ?? "/";
  return async ({ command, cwd, originalCwd, mode }) => {
    setOriginalCwd(originalCwd || cwd || home);
    const toolPermissionContext = {
      mode: mode ?? "default",
      additionalWorkingDirectories: new Map(),
      alwaysAllowRules: {}, alwaysDenyRules: {}, alwaysAskRules: {},
      isBypassPermissionsModeAvailable: true,
      mcpPermissionModeOverrides: {},
    };
    const result = await runInCwd(cwd || home, () => check({ command }, {
      getAppState: () => ({ toolPermissionContext }),
      abortController: new AbortController(),
    }));
    return dangerousRemovalMessage(result) ?? null;
  };
}
