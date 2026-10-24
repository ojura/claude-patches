// Keeps one Claude Code version's dangerous-removal check loaded and answers
// the hook over a Unix socket, so a hook call does not pay for the 0.45 s
// import and the 0.2 s self-test. When the self-test fails, the server exits.
// The hook then runs the check in its own process, where the self-test fails
// the same way, and fails open with the self-test's message.
//
// Usage: rm-check-server.mjs <prepared dir> <socket path>
//
// The hook starts a server when it does not get an answer on the socket. A
// request is one JSON line {command, cwd, originalCwd, mode}. The reply is one
// JSON line: {message} with the check's text, {message: null} when the check
// does not flag the command, or {error}. Requests run one at a time, because
// each check sets the session's original working directory, which the bundle
// keeps in process-wide state (see rm-check-lib.mjs). When the server has not
// had a request for RMCHECK_IDLE_SECONDS seconds (default 600), it removes its
// socket and exits, and so frees the memory of the loaded bundle.
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

// When the socket path is in use, this server connects to it. A socket file
// left by a server that died refuses the connection, so this server removes the
// file and listens again. When a live server accepts the connection, this one
// exits, because the live server answers the hooks.
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
