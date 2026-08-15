# Artifact index

This index records the durable corpus retained from the 2026-08-05 Claude Code
2.1.220 → CLIProxyAPI → Codex investigation, including evidence corrections, the
direct-path steering additions completed on 2026-08-10, and the automated permission
reviewer findings completed on 2026-08-11.
The current system architecture and decisions live in
[`ARCHITECTURE.md`](ARCHITECTURE.md); focused permission-reviewer findings live in
[`CLASSIFIER.md`](CLASSIFIER.md). This document records provenance, evidence class,
retention gaps, and reproduction constraints.

Exact hashes for every durable project-local file except the manifest itself are in
[`SHA256SUMS`](SHA256SUMS). Temporary consolidation working files are not part of the
durable corpus or integrity manifest.

## Reading order and status

1. [`ARCHITECTURE.md`](ARCHITECTURE.md) is the current system conclusion and decision
   record.
2. [`CLASSIFIER.md`](CLASSIFIER.md) is the current focused account of Claude auto mode,
   Codex Guardian, direct reviewer translation, cache behavior, and implementation limits.
3. [`NATIVE_COMPACTION.md`](NATIVE_COMPACTION.md) is a provisional investigation. Its
   source-confirmed observations may be cited by evidence class; its process drafts are not
   accepted architecture.
4. [`reports/corpus-absorption-checklist.md`](reports/corpus-absorption-checklist.md)
   maps each consolidation correction to its durable destination or explicit open item.
5. [`reports/midturn-steering-direct-path.md`](reports/midturn-steering-direct-path.md)
   is an absorbed focused narrative. Its current conclusions are carried independently by
   the architecture and the two steering evidence files.
6. The two `workflow12-*` reports are dated experiment snapshots. Read later findings
   and this index before treating their scoped or provisional wording as current.
7. Evidence captures establish only the fields they retain. Report prose, source
   analysis, and composed controls are not interchangeable with a raw executable
   capture.

## Selection policy

Retained:

- synthesized reports that classify source and executable evidence;
- small focused result captures that establish or narrow a finding;
- compact logs that show exact transport transitions;
- localhost reproduction scripts and focused Go tests;
- the captured cloaked model-list fixture used by Claude probes;
- source/binary provenance pins and hashes of surviving temporary instruments.

Excluded or lost:

- real credentials and auth material;
- temporary patched Claude binaries, including the 275-MiB tombstone probe binary;
- temporary CLIProxy source copies and worktrees;
- per-run config/session directories;
- giant redundant raw request captures where a focused result preserved the required
  facts;
- raw auto-compaction capture (`/tmp/claude-auto-compact-requests.json`, 3.3 MiB);
- raw context-hint request history (`/tmp/claude-context-hint-all-requests.json`,
  1.2 MiB);
- ScheduleWakeup per-policy request captures
  (`/tmp/claude-ttl-{five,one,mixed}-requests.json`);
- raw same-UUID and cross-model precompute request histories, including the ordering
  needed to make the retained Luna→Terra summary independently self-contained;
- `/tmp/cliproxy-ws-probe.log` from the late-frame run;
- per-delay raw replacement-crash-window captures;
- the custom-base exact-function raw harness;
- the localhost admission stub required by `admission_boundary.py`;
- a distinct 0-ms rewind result file;
- full logs that established why the forced physical-GC attempt missed the real
  bytecode path;
- full mid-turn request/child-stream captures and response matrices under
  `/tmp/midturn-steering-research`; focused project-authored summaries retain the needed
  order and aggregate outcomes without full harness text or large model signatures;
- copied upstream source trees and unrelated `/tmp` data.

The exclusions are not uniformly “redundant.” Several are explicit evidence gaps. The
founding-evidence report and probe README identify where a conclusion depends on report
prose, surviving source, composed controls, or an excluded raw ordering.

All retained API keys, bearer values, UUIDs, and tokens are synthetic fixture values
such as `test`, `sk-test`, `token-one`, and fixed UUIDs. A credential-pattern scan found
no real key-shaped material before or after corpus consolidation.

## Focused design documents

| Local artifact | Original source | Evidence role |
|---|---|---|
| [`CLASSIFIER.md`](CLASSIFIER.md) | Project-authored from the pinned Claude bundle, readable Claude reconstruction, pinned CLIProxyAPI and Codex source, and direct port-8317 cache experiments | Current source-confirmed and executable-informed account of automated permission reviewers; distinguishes native client implementation from direct protocol translation and marks the raw cache histories as unretained |
| [`NATIVE_COMPACTION.md`](NATIVE_COMPACTION.md) | Project-authored from the pinned Claude 2.1.220 bundle, readable Claude reconstruction, pinned CLIProxyAPI and Codex source, and the append-only JSONL project constraint | Provisional investigation only; labels source-confirmed observations separately from unexecuted carrier, interception, replay, and recursive-compaction drafts |

## Reports

| Local artifact | Original source | Evidence role |
|---|---|---|
| [`reports/workflow12-local-state-checks.md`](reports/workflow12-local-state-checks.md) | `/tmp/workflow12-local-state-checks.md` | First executable-pass snapshot: `/clear` split, replay/count findings, replacement crash window, compact controls, and 128k/32k telemetry contradiction; later work narrows several evidence classes |
| [`reports/workflow12-outstanding5-local-checks.md`](reports/workflow12-outstanding5-local-checks.md) | `/tmp/workflow12-outstanding5-local-checks.md` | Follow-up snapshot: TTL materialization, hint durability, stale precompute, WebSocket boundaries, tombstones, fork ownership, physical-GC activation, and SDK/remote-session gaps |
| [`reports/founding-experiments-and-evidence-gaps.md`](reports/founding-experiments-and-evidence-gaps.md) | Project-authored from the session record and surviving probes | Durable classification of admission, custom-base, and replacement-crash founding results, including what raw evidence was not retained |
| [`reports/corpus-absorption-checklist.md`](reports/corpus-absorption-checklist.md) | Project-authored consolidation record | Finding-by-finding map of architecture, evidence-class, provenance, completeness, reproduction, and documentation corrections to durable destinations or explicit open items |
| [`reports/midturn-steering-direct-path.md`](reports/midturn-steering-direct-path.md) | Project-authored from the 2026-08-10 bundle/source audit and localhost captures | Absorbed focused narrative retained for readable sequencing; current conclusions and evidence classifications live independently in `ARCHITECTURE.md` and the two steering evidence files |

## Provenance

| Local artifact | Role |
|---|---|
| [`evidence/provenance/source-pins.json`](evidence/provenance/source-pins.json) | Claude binary/source, CLIProxy v7.2.116 Go lane, CLIProxy 7.2.112 late-frame binary lane, Codex source, and audited CCS file pins |
| [`evidence/provenance/instrumented-binary-hashes.txt`](evidence/provenance/instrumented-binary-hashes.txt) | Hashes of temporary instrumented binaries that survived during consolidation; identifies bytes only and does not reconstruct their patch semantics |

The installed Claude binary was already locally patched; its exact pre-existing patch
delta was not retained. Executable observations identify the hash-pinned installed
bytes, not pristine upstream independently of source confirmation.

## Claude Code evidence

| Local artifact | Original source | Classification | Interpretation / limitation |
|---|---|---|---|
| [`evidence/claude/clear-session-identity.json`](evidence/claude/clear-session-identity.json) | `/tmp/claude-clear-probe-result.json` | Executable-reproduced | Normal `/clear` rotates header/body; a custom header pins the old proxy-visible session while body metadata rotates |
| [`evidence/claude/midturn-steering-order.json`](evidence/claude/midturn-steering-order.json) | Focused summary of captured request/child-stream JSONL under `/tmp/midturn-steering-research/clean-instance` and the pinned bundle | Executable request order plus source-confirmed queue lifecycle | Self-contained steering evidence: exact snapshot exclusions and selection filters, tool-result → later system order, capability and wrapper rules, final-response limit, removal/abort/cancellation semantics, and chair-sermon fallback details |
| [`evidence/claude/schedule-wakeup-ttl-variants.json`](evidence/claude/schedule-wakeup-ttl-variants.json) | `/tmp/claude-schedule-ttl-probe-result.json` | Executable-reproduced | Three ScheduleWakeup descriptions with hashes; historical `description_bytes` fields are Python character counts, not UTF-8 bytes |
| [`evidence/claude/schedule-wakeup-token-counts.json`](evidence/claude/schedule-wakeup-token-counts.json) | `/tmp/claude-ttl-count-results.json` | Executable-reproduced; generator not retained | The three materialized descriptions produced distinct local Count Tokens totals |
| [`evidence/claude/context-hint-durability.json`](evidence/claude/context-hint-durability.json) | `/tmp/claude-context-hint-durability-result.json` | Executable-reproduced | 422 retry uses wrappers; fresh resume restores raw results and offers the hint again |
| [`evidence/claude/context-hint-precompute-stale.json`](evidence/claude/context-hint-precompute-stale.json) | `/tmp/claude-hint-precompute-stale-result.json` | Executable-reproduced | Hint wrappers are not retained across turns and a boundary-compatible pre-hint summary is later consumed |
| [`evidence/claude/precompute-cross-model.json`](evidence/claude/precompute-cross-model.json) | `/tmp/claude-precompute-cross-model-result.json` | Executable-reproduced with retained-ordering gap | Luna→Terra stale-summary reuse was verified in the original raw sequence; the focused JSON alone does not disambiguate the later Terra compact |
| [`evidence/claude/replacement-crash-window-summary.json`](evidence/claude/replacement-crash-window-summary.json) | Project-authored from retained report prose | Report-derived summary, not raw capture | Records the 0–50 ms omission / 100–500 ms reconstruction sweep and explicitly marks missing per-delay raw results |
| [`evidence/claude/rewind-successor-100ms.json`](evidence/claude/rewind-successor-100ms.json) | `/tmp/claude-rewind-successor-100.json` | Executable control | Once the new branch record commits, fresh resume follows it; delay exists only in the filename |
| [`evidence/claude/rewind-successor-500ms.json`](evidence/claude/rewind-successor-500ms.json) | `/tmp/claude-rewind-successor-500.json` | Executable control | Same control at 500 ms; delay exists only in the filename |
| [`evidence/claude/rewind-successor-1000ms.json`](evidence/claude/rewind-successor-1000ms.json) | `/tmp/claude-rewind-successor-1000.json` | Executable control | Same control at 1,000 ms; the separately reported 0-ms omission had no distinct durable result left to copy |
| [`evidence/claude/physical-gc-live-negative.json`](evidence/claude/physical-gc-live-negative.json) | `/tmp/claude-physical-gc-live-result.json` | Negative executable diagnostic | Normal local env setup did not prune raw rows; source later explained the missing SDK/remote-session initializer; timing makes top-level `compact_seen` misleading without the request array/probe |
| [`evidence/claude/physical-gc-forced-negative.json`](evidence/claude/physical-gc-forced-negative.json) | `/tmp/claude-physical-gc-forced-v2-output.json` | Semantic-compaction control; negative interpretation not self-contained | The retained fields do not independently establish that the real physical-GC bytecode path was missed; unretained logs supplied that diagnosis |
| [`evidence/claude/semantic-auto-compact-control.json`](evidence/claude/semantic-auto-compact-control.json) | `/tmp/claude-physical-gc-output.json` | Executable control; generator not retained | Semantic auto-compaction writes and reuses a boundary/summary independently of physical GC |
| [`evidence/claude/manual-compact-requests.json`](evidence/claude/manual-compact-requests.json) | `/tmp/claude-interactive-compact-requests.json` | Executable control; generator not retained | Focused manual `/compact` request sequence |
| [`evidence/claude/tombstone-removal.json`](evidence/claude/tombstone-removal.json) | `/tmp/claude-tombstone-remove-result.json` | Executable-reproduced plus control | Greater-than-50-MiB target remains with success; also retains the first non-truncating interruption control |
| [`evidence/claude/tombstone-interruption.json`](evidence/claude/tombstone-interruption.json) | `/tmp/claude-tombstone-remove-interrupt-result.json` | Executable fault injection | Kill after truncate removes the target and legitimate suffix; an instrumented 10-second pause widened the window |
| [`evidence/claude/persisted-output-fork-ownership.json`](evidence/claude/persisted-output-fork-ownership.json) | `/tmp/claude-persisted-output-fork-result.json` | Executable simulated-behavior control | Stub-authored wrapper and probe-planted artifact demonstrate row-copy/dangling ownership; genuine replacement-ledger fork handling remains untested |

## CLIProxy evidence

| Local artifact | Original source | Classification | Interpretation / limitation |
|---|---|---|---|
| [`evidence/cliproxy/websocket-late-frame-crossover.json`](evidence/cliproxy/websocket-late-frame-crossover.json) | `/tmp/cliproxy-websocket-late-frame-result.json` | Executable-reproduced against 7.2.112 / `a63da8ae` | A late response-A terminal frame is delivered as request B's terminal result; proxy-side log was not retained |
| [`evidence/cliproxy/direct-midturn-translation.json`](evidence/cliproxy/direct-midturn-translation.json) | Focused summary of the pinned v7.2.116 translator and `/tmp/midturn-steering-research` matrices | Source-confirmed translation/cache key plus executable response checks | Self-contained direct-path evidence: translator source locations, developer/user/function-output ordering, tail-prefix cache property, 21/21 short corrections, and the 12-trial long-history acknowledgement/action split; full responses and large signatures are excluded |
| [`evidence/cliproxy/credential-refresh-inconclusive-control.json`](evidence/cliproxy/credential-refresh-inconclusive-control.json) | `/tmp/cliproxy-websocket-credential-refresh-result.json` | Superseded control with premise gap | Fresh connections still showed token-one; the artifact itself does not record that or when a token-two edit occurred |
| [`evidence/cliproxy/credential-refresh-stale-handshake.log`](evidence/cliproxy/credential-refresh-stale-handshake.log) | `/tmp/cliproxy-credential-refresh-focused.log` | Two executable controls at v7.2.116 | Watcher same-ID token update and executor retained-socket reuse; service glue is source-confirmed, not one end-to-end fixture |
| [`evidence/cliproxy/partial-write-full-resend.log`](evidence/cliproxy/partial-write-full-resend.log) | `/tmp/cliproxy-partial-write-focused.log` | Executable-reproduced at v7.2.116 | Upstream read 65,536 bytes of a 32-MiB request, then CLIProxy reconnected and resent the full request |
| [`evidence/cliproxy/executor-suite.log`](evidence/cliproxy/executor-suite.log) | `/tmp/cliproxy-executor-suite.log` | Executable package control, non-verbose | Three device-profile expectations fail because the configured macOS profile differs from expected Linux headers; passing replay tests are not named |

## Fixture

| Local artifact | Original source | Purpose |
|---|---|---|
| [`fixtures/cliproxy-claude-models.json`](fixtures/cliproxy-claude-models.json) | `/tmp/cliproxy-claude-models.json` | Faithful captured CLIProxy cloaked `/v1/models` response used by retained probes; IDs are prefix+reversal transformed, not plain model IDs |

## Claude reproduction scripts

These are project-local copies of the original `/tmp` probes. References to the shared
catalog were normalized to the project fixture, and binary/port overrides were made
configurable where practical. See [`probes/README.md`](probes/README.md) for the exact
patch, dependency, and retained-evidence gaps.

| Local probe | Original source | Primary result / limitation |
|---|---|---|
| [`probes/claude/admission_boundary.py`](probes/claude/admission_boundary.py) | `/tmp/bisect-luna-admission.py` | Admission/output boundary search; required `ADMISSION_PASSED_STUB` server is not retained |
| [`probes/claude/clear_session_identity.py`](probes/claude/clear_session_identity.py) | `/tmp/claude_runtime_clear_probe.py` | `/clear` header/body identity split |
| [`probes/claude/schedule_wakeup_ttl.py`](probes/claude/schedule_wakeup_ttl.py) | `/tmp/claude_schedule_ttl_probe.py` | Three dynamic ScheduleWakeup descriptions; size field is character count |
| [`probes/claude/context_hint_durability.py`](probes/claude/context_hint_durability.py) | `/tmp/claude_context_hint_durability_probe.py` | Same-process and fresh-resume hint durability |
| [`probes/claude/precompute_cross_model.py`](probes/claude/precompute_cross_model.py) | `/tmp/claude_precompute_cross_model_probe.py` | Cross-model precompute reuse; raw request ordering excluded |
| [`probes/claude/context_hint_precompute_stale.py`](probes/claude/context_hint_precompute_stale.py) | `/tmp/claude_hint_precompute_stale_probe.py` | Same-UUID hint/precompute incompatibility |
| [`probes/claude/replacement_crash_window.py`](probes/claude/replacement_crash_window.py) | `/tmp/claude_replacement_crash_probe.py` | Single-delay replacement crash fixture; per-delay outputs not retained |
| [`probes/claude/rewind_successor.py`](probes/claude/rewind_successor.py) | `/tmp/claude_rewind_successor_probe.py` | Rewind write-window controls; one fixed result filename requires manual per-delay preservation |
| [`probes/claude/physical_gc_live.py`](probes/claude/physical_gc_live.py) | `/tmp/claude_physical_gc_live_probe.py` | Normal local physical-GC negative diagnostic |
| [`probes/claude/physical_gc_forced_diagnostic.py`](probes/claude/physical_gc_forced_diagnostic.py) | `/tmp/claude_physical_gc_forced_probe.py` | Forced-branch diagnostic whose retained JSON does not establish physical-GC activation |
| [`probes/claude/tombstone_removal.py`](probes/claude/tombstone_removal.py) | `/tmp/claude_tombstone_remove_probe.py` | Large-file silent failure and instrumented interrupted tail loss; depends on preserved rewind `/tmp` state |
| [`probes/claude/persisted_output_fork.py`](probes/claude/persisted_output_fork.py) | `/tmp/claude_persisted_output_fork_probe.py` | Simulated wrapper/artifact fork-ownership behavior |

## CLIProxy reproduction scripts and tests

| Local probe | Original source | Primary result |
|---|---|---|
| [`probes/cliproxy/websocket_late_frame.py`](probes/cliproxy/websocket_late_frame.py) | `/tmp/cliproxy_websocket_late_frame_probe.py` | Late terminal frame crosses retained-request boundary; 7.2.112 binary lane |
| [`probes/cliproxy/executor/replay_and_count_regressions_test.go`](probes/cliproxy/executor/replay_and_count_regressions_test.go) | `/tmp/cliproxy-runtime-probe/internal/runtime/executor/z_workflow_runtime_probe_test.go` | Six replay/count behaviors at v7.2.116; future rerun needs verbose output and fingerprint non-degeneracy guard |
| [`probes/cliproxy/executor/credential_reuse_regression_test.go`](probes/cliproxy/executor/credential_reuse_regression_test.go) | `/tmp/cliproxy-runtime-probe/internal/runtime/executor/z_workflow_credential_reuse_probe_test.go` | Same auth ID and URL reuse the old handshake despite new headers |
| [`probes/cliproxy/executor/partial_write_regression_test.go`](probes/cliproxy/executor/partial_write_regression_test.go) | `/tmp/cliproxy-runtime-probe/internal/runtime/executor/z_workflow_partial_write_probe_test.go` | Partial first transmission followed by full automatic resend |
| [`probes/cliproxy/watcher/auth_refresh_regression_test.go`](probes/cliproxy/watcher/auth_refresh_regression_test.go) | `/tmp/cliproxy-runtime-probe/internal/watcher/z_workflow_auth_refresh_probe_test.go` | Auth-file token update emits Modify while preserving synthesized auth ID |

## Integrity and provenance notes

- `SHA256SUMS` hashes the durable project-local corpus, including provenance, reports,
  evidence, fixtures, and probes. Temporary consolidation working files are excluded.
- At the original consolidation, the retained evidence/report files with surviving
  `/tmp` originals were byte-identical. The two workflow reports now carry a
  project-authored historical-status banner; later documentation edits are not
  described as current byte copies of `/tmp`.
- Probe hashes differ from their `/tmp` originals where model-catalog paths, host
  binding, port settings, or binary overrides were normalized.
- Temporary binary hashes identify surviving bytes only. Future maintained patches
  must recreate edits structurally through `../util/bun_handler.py` and verify extracted
  source, rather than relying on `/tmp` filenames.
- Source and binary identities are pinned in `ARCHITECTURE.md` and
  `evidence/provenance/source-pins.json`.
- The two mid-turn steering JSON files are project-authored focused summaries. Their
  classification fields separate executable request/response observations from
  source-confirmed lifecycle and translation details; they are not represented as byte
  copies of the excluded temporary captures.
