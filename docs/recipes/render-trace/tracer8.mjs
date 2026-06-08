// tracer8.mjs <browser-ws> <out.perfetto-trace> <max-seconds> <bufKb>
// PROTO streamFormat (binary, fast finalize, no JSON serialize hang). cpu_profiler + timeline.
// Stuck-detection: timeouts on tracingComplete and on each drain read. Live buffer% + drain MB/s.
import fs from 'node:fs';
const ws = new WebSocket(process.argv[2]); const out = process.argv[3]; const maxs = parseInt(process.argv[4]) || 320; const bufKb = parseInt(process.argv[5]) || 500000;
const STOP = '/tmp/STOP_TRACE';
let nid = 1; const pend = new Map(); let completeResolve;
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(m.error) : res(m.result); }
  else if (m.method === 'Tracing.tracingComplete') { completeResolve && completeResolve(m.params); }
  else if (m.method === 'Tracing.bufferUsage') { const p = m.params; console.log('  capture buffer ' + (p.percentFull != null ? Math.round(p.percentFull * 100) + '%' : '?')); } });
const call = (method, params = {}) => { const id = nid++; return new Promise((res, rej) => { pend.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params })); }); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const tErr = (ms, label) => sleep(ms).then(() => { throw new Error(label); });
await new Promise(r => ws.addEventListener('open', r));
try { fs.unlinkSync(STOP); } catch (e) {}
await call('Tracing.start', { transferMode: 'ReturnAsStream', streamFormat: 'proto', bufferUsageReportingInterval: 2000, traceConfig: { recordMode: 'recordAsMuchAsPossible', traceBufferSizeInKb: bufKb, includedCategories: ['disabled-by-default-v8.cpu_profiler', 'devtools.timeline', 'v8', 'toplevel'] } });
console.log('TRACE_STARTED proto buf=' + bufKb + 'KB');
const t0 = Date.now();
while (Date.now() - t0 < maxs * 1000) { await sleep(400); if (fs.existsSync(STOP)) break; }
console.log('ending trace; awaiting tracingComplete (detect 60s)...');
const cp = new Promise(r => completeResolve = r);
await call('Tracing.end');
let params;
try { params = await Promise.race([cp, tErr(60000, 'STUCK_TRACINGCOMPLETE: no tracingComplete in 60s')]); }
catch (e) { console.log('!! ' + e.message); ws.close(); process.exit(2); }
const handle = params.stream;
const fd = fs.openSync(out, 'w'); let bytes = 0; const dStart = Date.now(); let lastPrint = 0;
try {
  while (true) {
    const r = await Promise.race([call('IO.read', { handle, size: 1 << 20 }), tErr(25000, 'STUCK_DRAIN: stalled 25s at ' + Math.round(bytes / 1e6) + 'MB')]);
    const buf = r.base64Encoded ? Buffer.from(r.data, 'base64') : Buffer.from(r.data, 'binary');
    fs.writeSync(fd, buf); bytes += buf.length;
    const now = Date.now();
    if (now - lastPrint >= 1000 || r.eof) { const s = (now - dStart) / 1000; console.log('  drained ' + Math.round(bytes / 1e6) + 'MB (' + s.toFixed(0) + 's, ' + (bytes / 1e6 / Math.max(s, 0.1)).toFixed(0) + ' MB/s)'); lastPrint = now; }
    if (r.eof) break;
  }
} catch (e) { console.log('!! ' + e.message); try { fs.closeSync(fd); } catch (_) {} ws.close(); process.exit(3); }
fs.closeSync(fd); await call('IO.close', { handle });
console.log('TRACE_DONE bytes=' + bytes + ' (' + Math.round(bytes / 1e6) + 'MB) proto -> ' + out);
ws.close();
