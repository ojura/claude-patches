// Keeps one Claude Code version's dangerous-removal check loaded and answers
// the hook over a Unix socket, so a hook call does not pay the 0.45 s import.
//
// Usage: rm-check-server.mjs <prepared dir> <socket path>
//
// The hook starts it when no server answers. A request is one JSON line
// {command, cwd, originalCwd, mode}; the reply is one JSON line {message} (the
// check's text, or null when the command is not flagged) or {error}. Requests
// run one at a time, because each check sets the session's original working
// directory, which the bundle keeps in process-wide state (see
// rm-check-lib.mjs). After RMCHECK_IDLE_SECONDS (default 600) without a
// request the server removes its socket and exits, which frees the loaded
// bundle.
import net from "node:net";
import { unlinkSync } from "node:fs";
import { loadCheck } from "./rm-check-lib.mjs";

const [dir, sockPath] = process.argv.slice(2);
const idleMs = 1000 * (Number(process.env.RMCHECK_IDLE_SECONDS) || 600);
const runCheck = await loadCheck(dir);

let queue = Promise.resolve();
let idleTimer;
const server = net.createServer((sock) => {
  armIdle();
  let buf = "";
  sock.setEncoding("utf8");
  sock.on("data", (chunk) => {
    buf += chunk;
    const nl = buf.indexOf("\n");
    if (nl < 0) return;
    const line = buf.slice(0, nl);
    buf = "";
    queue = queue.then(async () => {
      let reply;
      try { reply = { message: await runCheck(JSON.parse(line)) }; }
      catch (e) { reply = { error: String(e?.stack ?? e) }; }
      sock.end(JSON.stringify(reply) + "\n");
      armIdle();
    });
  });
  sock.on("error", () => {});
});

function armIdle() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(shutdown, idleMs);
}

function shutdown() {
  server.close();
  try { unlinkSync(sockPath); } catch { /* already gone */ }
  process.exit(0);
}

// A socket file left by a server that died refuses connections; a live server
// answers, and then this one is not needed.
function listen() {
  server.once("error", (e) => {
    if (e.code !== "EADDRINUSE") throw e;
    const probe = net.createConnection(sockPath);
    probe.on("connect", () => { probe.destroy(); process.exit(0); });
    probe.on("error", () => { try { unlinkSync(sockPath); } catch {} listen(); });
  });
  server.listen(sockPath, armIdle);
}
process.on("SIGTERM", shutdown);
listen();
