# Probe corpus

These probes are the reusable parts of the localhost experiments summarized in
[`../reports/`](../reports/) and indexed by [`../ARTIFACTS.md`](../ARTIFACTS.md).
They intentionally exclude temporary config/session directories and copied source
worktrees. Temporary instrumented binaries are not part of the durable corpus; hashes
of the copies that still survived during consolidation are retained for provenance,
not as a substitute for structural patch descriptions.

## Safety and isolation

Every retained probe is designed for localhost fixtures. All retained tokens/API keys
are synthetic. Do not point these scripts at a real provider.

The Claude probes create an isolated `CLAUDE_CONFIG_DIR` and scrub inherited Claude
auth/session variables before launching the binary. Result files and temporary roots
are written under `/tmp`. Review fixed ports before running probes concurrently.

The retained WebSocket fixture now binds both the fake upstream and CLIProxy listener
to `127.0.0.1`.

## Python dependencies

Most probes use only the Python standard library. Exceptions:

- `claude/physical_gc_forced_diagnostic.py` imports `pexpect`;
- `cliproxy/websocket_late_frame.py` imports `websockets`.

## Shared fixture

Claude probes that need a model catalog read:

```text
fixtures/cliproxy-claude-models.json
```

under the project root. The file is a captured CLIProxy cloaked `/v1/models` response,
not a plain-ID synthetic catalog: IDs use CLIProxy's `claude-fable-5-dd-` plus reversed
model-ID transform. Display names, limits, credentials, and IDs used elsewhere in the
fixtures remain non-secret. Results are still written to `/tmp` so reruns do not dirty
the repository.

## Claude probes using the installed binary directly

- `claude/admission_boundary.py`
- `claude/clear_session_identity.py`
- `claude/replacement_crash_window.py`
- `claude/rewind_successor.py`
- `claude/physical_gc_live.py`
- `claude/persisted_output_fork.py`
- `claude/schedule_wakeup_ttl.py` for its `five` and `one` branches

They default to:

```text
/home/juraj/.local/share/claude/versions/2.1.220
```

That installed binary is hash-pinned in `ARCHITECTURE.md`, but it already carried a
local patch whose exact delta was not retained. Results describe those bytes; they do
not establish pristine-upstream behavior independently of source analysis.

`admission_boundary.py` additionally requires the localhost admission stub that emits
`ADMISSION_PASSED_STUB`. That stub was not retained and must be reconstructed before a
full rerun.

## Claude probes requiring temporary instrumented binaries

Recreate instruments from a known pinned 2.1.220 input through
[`../../util/bun_handler.py`](../../util/bun_handler.py), then point the probe at the
result with the listed environment variable. Do not silently trust a surviving `/tmp`
binary merely because its filename matches; compare its hash with
[`../evidence/provenance/instrumented-binary-hashes.txt`](../evidence/provenance/instrumented-binary-hashes.txt)
and verify the structural edit.

| Probe | Environment variable | Historical default | Forced behavior / limitation |
|---|---|---|---|
| `schedule_wakeup_ttl.py` mixed branch | `CLAUDE_TTL_MIXED_BINARY` | `/tmp/claude-2.1.220-ttl-mixed` | Forced mixed selector result for the dynamic ScheduleWakeup description |
| `context_hint_durability.py` | `CLAUDE_CONTEXT_HINT_BINARY` | `/tmp/claude-2.1.220-force-context-hint` | Forced context-hint activation against a localhost 422 fixture |
| `precompute_cross_model.py` | `CLAUDE_PRECOMPUTE_BINARY` | `/tmp/claude-2.1.220-force-precompute-always` | Forced precompute availability; retained summary omits the raw request ordering needed for self-contained Luna→Terra attribution |
| `context_hint_precompute_stale.py` | `CLAUDE_HINT_PRECOMPUTE_BINARY` | `/tmp/claude-2.1.220-hint-precompute-stale` | Forced hint and precompute paths for same-UUID incompatibility |
| `physical_gc_forced_diagnostic.py` | `CLAUDE_PHYSICAL_GC_BINARY` | `/tmp/claude-2.1.220-force-physical-gc-v2` | Diagnostic attempt only; retained JSON does not independently establish execution of the real physical-GC bytecode path |
| `tombstone_removal.py` | `CLAUDE_TOMBSTONE_PROBE_BINARY` | `/tmp/claude-2.1.220-remove-probe` | `PROBE_REMOVE_UUID` triggers removal; interruption uses an instrumented `PROBE_REMOVE_PAUSE_MS=10000` pause to widen the truncate→rewrite window |

The exact patch recipes were not retained. The founding-evidence report and architecture
therefore distinguish executable observations, source confirmation, and missing raw
harnesses rather than treating these filenames as the source of an evidence claim.

## Probe-specific reproduction gaps

- `claude/replacement_crash_window.py` survives, but its per-delay raw outputs do not;
  the retained JSON is a report-derived summary.
- `claude/persisted_output_fork.py` simulates the wrapper/artifact behavior: the stub
  authors wrapper text and the probe plants the parent-owned artifact. It does not run
  Claude's genuine persisted-output reduction/content-replacement path.
- `claude/tombstone_removal.py` currently depends on
  `/tmp/claude-rewind-successor-100.json` and the still-existing root referenced by that
  manually named result. `claude/rewind_successor.py` itself writes
  `/tmp/claude-rewind-successor-result.json`; reproduce the 100-ms control, preserve its
  root, and copy/rename the result before invoking the tombstone probe. Relevant knobs
  include `KILL_DELAY_MS`, `PROBE_PORT`, `PROBE_SESSION`, and `ONLY_INTERRUPT`.
- No retained generator exists for `schedule-wakeup-token-counts.json`,
  `manual-compact-requests.json`, or `semantic-auto-compact-control.json`.
- The six replay/count Go results were reported as passing, but the retained package log
  is non-verbose and does not name those passing tests. Future reruns should retain
  `go test -v` output.

## CLIProxy probes

### Python WebSocket fixture

`cliproxy/websocket_late_frame.py` launches the local CLIProxy binary and a fake
upstream WebSocket. The historical default is the separately pinned 7.2.112 / `a63da8ae`
binary lane. Override it explicitly with:

```sh
CLIPROXY_BIN=/path/to/cli-proxy-api python3 probes/cliproxy/websocket_late_frame.py
```

The Go/source evidence lane is CLIProxy `v7.2.116` at commit
`a88197f845c979132c8978ea223c6af05cc81536`; do not conflate it with the 7.2.112
late-frame binary lane.

### Focused Go tests

The Go files retain their original package names and use unexported implementation
symbols. To rerun them, use a checkout at the pinned source revision and copy each file
into the matching package:

```text
probes/cliproxy/executor/*.go
    → internal/runtime/executor/

probes/cliproxy/watcher/*.go
    → internal/watcher/
```

Then run the named tests with `go test -v`. The tests describe historical undesirable
behavior; a pass in the audited version means the behavior was reproduced. After a
fix, invert the assertions into ordinary regression expectations. The semantic-
fingerprint test also needs a non-empty/non-degenerate digest assertion before it can
serve as a durable post-fix regression check.

## Provenance

The retained copies came from `/tmp` experiment outputs produced on 2026-08-05, plus
project-authored summaries and reports created during corpus consolidation.
[`../SHA256SUMS`](../SHA256SUMS) hashes the project-local corpus.
[`../ARTIFACTS.md`](../ARTIFACTS.md) records source paths, evidence classification,
retention gaps, and interpretation.
