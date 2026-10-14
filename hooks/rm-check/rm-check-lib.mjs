// Loading and running the dangerous-removal check, shared by the hook and the
// server. Importing a version's prepared chunk takes about 0.45 s because it
// pulls in a large part of the bundle; one check then takes 2 to 50 ms.
import { readFileSync } from "node:fs";
import { join } from "node:path";

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
// it for one command in the given working directory and permission mode. It
// returns the check's text when the command is flagged, and null otherwise.
// The check reads the process's working directory, so calls must not overlap.
export async function loadCheck(dir) {
  const manifest = JSON.parse(readFileSync(join(dir, "manifest.json"), "utf8"));
  const mod = await import(join(dir, manifest.chunk));
  const check = mod[manifest.export];
  if (typeof check !== "function") throw new Error(`${manifest.chunk} does not export ${manifest.export}`);
  const home = process.env.HOME ?? "/";
  return async ({ command, cwd, mode }) => {
    try { process.chdir(cwd || home); } catch { process.chdir(home); }
    const toolPermissionContext = {
      mode: mode ?? "default",
      additionalWorkingDirectories: new Map(),
      alwaysAllowRules: {}, alwaysDenyRules: {}, alwaysAskRules: {},
      isBypassPermissionsModeAvailable: true,
      mcpPermissionModeOverrides: {},
    };
    const result = await check({ command }, {
      getAppState: () => ({ toolPermissionContext }),
      abortController: new AbortController(),
    });
    return dangerousRemovalMessage(result) ?? null;
  };
}
