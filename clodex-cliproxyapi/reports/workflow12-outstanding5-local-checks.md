# Outstanding workflow-1/2 local reliability checks

Date: 2026-08-05

> **Historical snapshot.** This report records the focused follow-up. Later corpus
> maintenance added provenance pins, disclosed simulated or instrumented controls, and
> reclassified claims whose raw ordering or end-to-end fixture was not retained. Use
> [`../ARCHITECTURE.md`](../ARCHITECTURE.md) for current conclusions and
> [`../ARTIFACTS.md`](../ARTIFACTS.md) for evidence limitations.

Scope: installed Claude Code 2.1.220, temporary byte-patched copies produced with `bun_handler.py`, temporary CLIProxy processes, the configured localhost CLIProxy count endpoint, and local HTTP/WebSocket stubs. All traffic stayed on localhost. The installed binary and `/tmp/cliproxyapi-source` were not modified.

## Corrected patch ladder status

| Rung | Validation result | Dependency effect |
|---|---|---|
| 0. Coherent GPT-5.6 profile | Existing executable captures still show bare `gpt-5.6-luna`, 372k request context, and `max_tokens: 128000`. No new failure in this pass. | None. Remains the base configuration. |
| 1. Exact-model input-only admission | The earlier binary-search result remains the executable control. This pass did not alter or retest `vSe`/`Fny`. | None. Both sites remain required. |
| 2. Additive observability | Existing run still shows request `max_tokens: 128000` while `modelUsage.maxOutputTokens` reports 32000. Dynamic ScheduleWakeup variants also produce different materialized request sizes and token counts. | Strengthens the need to show request-side output capacity separately from reported catalog/telemetry capacity and to expose prepared-request drift. |
| 3. Proxy replay correctness | Existing six focused checks remain reproduced. This pass additionally reproduced late upstream WebSocket frame crossover into the next downstream request. | Rung 3 remains a hard prerequisite for rungs 5 and 6. |
| 4. Claude durability | Forced 422 context-hint handling reduced the live retry but did not survive a new turn or fresh-process resume. Rewind continuation survived once its user record had committed. Tombstone removal now has executable confirmations for both the >50 MiB skip and truncate-before-tail-rewrite loss window. A CLI session fork copied a persisted-output wrapper without copying or owning its artifact. | Durable replacement/clear commits, crash-safe transcript mutation, and an artifact ownership manifest remain required. The rewind finding is narrowed. |
| 5. Durable hints and precompute integrity | Three-state dynamic tool materialization was reproduced. Cross-model stale precompute and same-UUID hint/precompute incompatibility were reproduced with isolated branch-forcing patches. | Confirms rung 5 must wait for rungs 3–4 and must digest the final materialized request, including dynamic tool text/model/configuration. The generic `Oe` design remains out of scope. |
| 6. Optional continuation | A late response-A terminal frame was delivered as response B's terminal event. A 32 MiB request was partially transmitted, failed with connection reset, and was automatically resent in full on a fresh socket. A watcher-supported auth-file refresh emitted a same-ID modify, while the retained WebSocket keyed only on auth ID and URL and reused the token-one handshake instead of redialing for token-two. | Rung 6 remains blocked by replay identity, frame demultiplexing/cancellation boundaries, credential-generation affinity, and an explicit unknown-outcome execution policy. |

## Rung 2 / 5 — dynamic ScheduleWakeup materialization

Three executable variants captured the final `ScheduleWakeup` tool description:

| Regime | Expected text | Description bytes | SHA-256 |
|---|---|---:|---|
| Forced 5-minute | Explicit default 5-minute wording | 2933 | `ff980d5a1a291e82e4e8b6ff791bf023fc341093b7c471e1b9da7e08a6b54e1d` |
| Forced 1-hour | Explicit 1-hour wording | 2685 | `85443c53c5fca68bf1e85620dd1fd9da3c616ebb9b8aee882dcd8ff1ee513ec0` |
| Mixed main/sdk | Generic dual-regime wording | 2734 | `b469ecead71fa8984ee2289dbe7b9384ab86456850b05f28e99676189e5ce967` |

All three requests had exactly one `ScheduleWakeup` tool and completed normally. Sending the captured, fully materialized Claude request bodies to the local CLIProxy count endpoint produced:

- 5-minute: 1849 tokens
- 1-hour: 1777 tokens
- mixed: 1804 tokens

Result: the TTL selector changes both the final semantic request bytes and Count Tokens output under an otherwise equivalent prompt. The replay/precompute digest must cover the materialized tool definition. Count Tokens does include the supplied materialized tool description; the remaining seam is whether every earlier local preparation phase materializes the same deferred definition.

Captures:

- `/tmp/claude-schedule-ttl-probe-result.json`
- `/tmp/claude-ttl-five-requests.json`
- `/tmp/claude-ttl-one-requests.json`
- `/tmp/claude-ttl-mixed-requests.json`
- `/tmp/claude-ttl-count-results.json`

## Rung 4 / 5 — 422 context-hint durability

An isolated temporary binary forced the existing context-hint controller for print/sdk requests and disabled ordinary tool-result persistence so the context-hint path could be observed independently.

Ten inline Bash results produced a 402,116-byte request with five old eligible results above the clearing threshold:

1. Initial request: raw results, `context_hint.enabled=true`, `target_tokens_saved=75000`.
2. Local stub returned HTTP 422.
3. Retry: 253,221 bytes, five `<persisted-output>` wrappers, no raw sample.
4. Fresh-process resume: 402,241 bytes, original raw results restored, `context_hint.enabled=true` again.

This directly confirms the clear is a retry-time reduction, not a durable content replacement.

The combined precompute/hint run showed an even narrower failure: after the 422 retry used five wrappers, the next user turn in the same process again sent the original raw results and another context hint. The reduced history was not retained across normal query turns in this print/sdk path.

Captures:

- `/tmp/claude-context-hint-durability-result.json`
- `/tmp/claude-context-hint-all-requests.json`
- `/tmp/claude-hint-precompute-stale-result.json`
- `/tmp/claude-hint-precompute-stale-requests.json`

## Rung 5 — precomputed compaction integrity

### Cross-model result

A temporary binary forced precompute arming while retaining the production precompute/consume implementation:

1. Background precompute request ran under `gpt-5.6-luna` and returned `PRECOMPUTED_LUNA_SUMMARY`.
2. `set_model` control request switched the active model to `gpt-5.6-terra`.
3. Local stub returned prompt-too-long for the Terra request.
4. Terra retry consumed the Luna precomputed summary.

Result:

- Precompute request observed: yes
- Prompt-too-long recovery observed: yes
- Retry model: `gpt-5.6-terra`
- Retry contained Luna summary marker: yes

This reproduces missing model compatibility in in-memory precompute consumption.

Capture: `/tmp/claude-precompute-cross-model-result.json` and `/tmp/claude-precompute-cross-model-requests.json`.

### Same-UUID context-hint result

A second temporary binary armed precompute only after raw tool results were present, then activated the existing 422 hint-reduction path:

1. Precompute summarized history containing raw tool results.
2. A later request sent those same message UUIDs with `context_hint.enabled=true`.
3. 422 retry replaced five old results with persisted-output wrappers.
4. Next turn restored the raw results.
5. Prompt-too-long recovery consumed the pre-hint summary marker while the current request again carried the raw results.

This reproduces the core compatibility failure: boundary UUID presence is insufficient after same-UUID content changes. Precompute validity must include canonical current content, final tool/system materialization, model, and compact configuration.

Capture: `/tmp/claude-hint-precompute-stale-result.json`.

## Rung 6 — WebSocket boundaries

### Late-frame crossover: reproduced

A temporary CLIProxy instance used a localhost Codex WebSocket stub:

1. Request A completed as `resp_A`.
2. Request B was sent on the same retained upstream connection.
3. Stub sent a late terminal frame `resp_A_LATE` after receiving B.
4. Downstream B received `resp_A_LATE` as its terminal response.
5. The proxy then closed the connection before the real B terminal could be delivered.

This is direct evidence that retained upstream frames are routed to the currently active request without response-ID demultiplexing.

Capture: `/tmp/cliproxy-websocket-late-frame-result.json` and `/tmp/cliproxy-ws-probe.log`.

### Same-ID credential refresh: reproduced stale retained handshake

The earlier configured-key edit remained inconclusive because that loader did not expose token-two. A focused two-part executable control used the watcher-supported auth-file path instead:

1. Rewriting `codex-account.json` from `access_token: token-one` to `token-two` emitted `AuthUpdateActionModify` with the same synthesized auth ID.
2. The retained Codex WebSocket was then offered the same auth ID and URL with a token-two Authorization header. `ensureUpstreamConn` returned the existing token-one connection, opened no second handshake, and therefore could not apply token-two.

The source path agrees with the execution: retained-session matching compares only `authID` and `wsURL`; service updates replace the auth object but close Codex WebSockets only on auth deletion, not modification.

Captures:

- `/tmp/cliproxy-credential-refresh-focused.log`
- `/tmp/cliproxy-websocket-credential-refresh-result.json` (earlier inconclusive configured-key control)
- Temporary tests:
  - `/tmp/cliproxy-runtime-probe/internal/watcher/z_workflow_auth_refresh_probe_test.go`
  - `/tmp/cliproxy-runtime-probe/internal/runtime/executor/z_workflow_credential_reuse_probe_test.go`

### Unknown-outcome write resend: reproduced

A local WebSocket server accepted a 32 MiB request, read exactly 65,536 bytes from the first frame, and reset the TCP connection. CLIProxy reported a write-side `connection reset by peer`, invalidated the socket, opened a fresh connection, and automatically sent the complete request again. The second connection received the full request and returned success.

This is an unknown-outcome boundary: once any request bytes have left the process, the client cannot infer that the upstream did not execute or buffer the request. The current unconditional retry of non-request-scoped write errors can duplicate execution.

Captures:

- `/tmp/cliproxy-partial-write-focused.log`
- `/tmp/cliproxy-runtime-probe/internal/runtime/executor/z_workflow_partial_write_probe_test.go`

### Still unexercised

- A provider-side cancellation acknowledgement and response-ID demultiplexer that could safely close the late-frame boundary.
- HTTP/SSE `previous_response_id` remains absent by source and prior focused audit; no new runtime test was needed for current behavior.

## Rung 4 — rewind, physical GC, background/fork/remote synchronization/tombstone

### Rewind correction

The remote-control `rewind_conversation` path was exercised, then a replacement branch was sent and the process was stopped while its model request was in flight.

- 0ms stop: the model had seen `NEW_BRANCH`, but the user record itself was absent from JSONL; resume omitted it.
- 100ms, 500ms, and 1000ms stops: `NEW_BRANCH` was present in JSONL and fresh-process resume included it, even though the explicit rewind `last-prompt` record remained the latest pre-resume marker.

The previously proposed post-rewind successor-marker issue was not reproduced. The executable result narrows it to the ordinary early transcript-write window: once the new user message commits, resume follows the descendant branch correctly in these fixtures.

Captures: `/tmp/claude-rewind-successor-{100,500,1000}.json` and `/tmp/claude-rewind-successor-result.json` for the 0ms case.

### Physical GC trigger conditions: source-resolved; local interactive path not activated

A 6,003,800-byte transcript (about 5.73 MiB, above the 5-MiB eligibility threshold) with `CLAUDE_CODE_TRANSCRIPT_LOCAL_GC=true` underwent semantic compaction but retained its raw pre-boundary rows. Source inspection resolves why: the physical-GC state variable starts false and is initialized from `CLAUDE_CODE_TRANSCRIPT_LOCAL_GC` only inside the `sdkUrl`/remote-session resume branch. A normal local interactive or print resume never calls that initializer, so the environment variable alone cannot activate physical transcript GC.

When initialized in the SDK/remote-session path, physical GC has three scheduling sites:

1. Insertion of a compact boundary requests compaction immediately.
2. The transcript writer runs a backstop compaction after accumulated writes cross the backstop threshold.
3. remote delta hydration requests compaction when the hydrated file exceeds the backstop threshold.

The production minimum file size is 5 MiB and the initial backstop is 20 MiB. The physical compactor keeps rows at or after the latest compact boundary, accumulated/last-wins rows required by policy, preserved-message or preserved-segment rows, and a direct pre-boundary parent referenced by a post-boundary transcript row.

The attempted local forced-branch binaries did not establish execution of this bytecode path and are not counted as confirmations. A real `sdkUrl`/remote-session transport fixture is still required for runtime retention-matrix coverage.

Captures: `/tmp/claude-physical-gc-live-result.json`, `/tmp/claude-physical-gc-output.json`, `/tmp/claude-physical-gc-forced-v2-output.json` (negative forced-branch diagnostic).

### Tombstone removal: both failure modes reproduced

The instrumented production removal wrapper produced two executable failures:

- **Over 50 MiB fallback:** before 53,069,996 bytes; after 53,071,918 bytes; target UUID remained; process returned 0. The fallback scans only the final 64 KiB and silently leaves an older target in place.
- **Interrupted fast path:** before 29,273 bytes; process was killed after truncate and before suffix rewrite; after 9,020 bytes; target absent; legitimate suffix absent. This confirms broad tail loss, not just failure to remove one row.

Captures:

- `/tmp/claude-tombstone-remove-result.json`
- `/tmp/claude-tombstone-remove-interrupt-result.json`
- `/tmp/claude_tombstone_remove_probe.py`

### Persisted-output ownership across a CLI fork: reproduced

A parent session contained a valid `<persisted-output>` wrapper pointing to a file under its session-owned `tool-results` directory. `--resume <parent> --fork-session` created a new transcript that copied the wrapper path verbatim but created no child-owned artifact copy. Deleting the parent session directory removed the file while the child transcript remained and still referenced the deleted parent path.

Result:

- Parent and fork runs returned 0.
- Wrapper copied verbatim: yes.
- Child-owned artifact copy: no.
- Parent artifact existed after parent deletion: no.
- Child transcript existed after parent deletion: yes.

Capture: `/tmp/claude-persisted-output-fork-result.json` and `/tmp/claude_persisted_output_fork_probe.py`.

### Sidechain parent pruning: source-confirmed, runtime unexercised

A child `fork-context-ref` is stored in the child agent file and names only `parentSessionId` plus `parentLastUuid`. Physical GC of the parent scans only the parent JSONL. It does not inspect child references, so a pre-boundary `parentLastUuid` can be removed unless it independently falls into the parent compactor's retained classes. Fresh child reconstruction then loads the parent transcript, fails to find the UUID, logs a warning, and returns an empty prefix.

This is a static cross-file reachability result. Runtime confirmation still requires a real SDK/remote-session physical-GC fixture plus a valid sidechain.

### Background main-session completion: source-confirmed, runtime unexercised

The background-main controller launches initial and per-event sidechain writes without awaiting them, then marks the task completed/failed and emits its notification without flushing the transcript writer. The writer has a 100 ms batching interval. This establishes from source that completion does not wait for transcript durability.

Attempts to reach the feature-gated Ctrl-B background-main path from the local interactive fixture did not produce the Yxd sidechain route and are not counted as executable confirmation.

### Remote transcript hydration (`CCR`): source-confirmed client behavior

`CCR` is the internal label used in this investigation for Claude Code's remote-session transcript synchronization; its exact acronym expansion was not established. No authenticated remote-session fixture was available, so the complete client/server behavior was not tested end to end.

Source inspection establishes the following client behavior:

- **Full hydration:** when delta hydration is disabled, remote resume replaces the local JSONL with the remote transcript. It opens the transcript in truncating `"w"` mode rather than writing a complete temporary file and atomically replacing it. A failure after truncation can leave the transcript incomplete.
- **Delta hydration:** when enabled, the client uses an accompanying metadata file recording the last known remote tip, together with checks against the end of the local transcript, to decide how to apply incoming events. It does not record a stable local-history generation or verify immediately before writing that the local transcript is still the version used for that decision. Concurrent local changes are therefore not guaranteed to survive.
- **Local replacements:** `content-replacement` records are local route-by-agent records. The client does not include them in normal remote transcript-event uploads or in the resynchronization projection. The remote transcript therefore cannot represent or restore those substitutions through these paths.

The remaining runtime and service-side unknowns are event ordering, deduplication, UUID stability and idempotency, projection rules for records the service does receive, catch-up behavior, and recovery from interrupted synchronization. Those unknowns do not change the source-confirmed omission of local `content-replacement` state.

## Controls and source state

- Installed binary SHA-256: `d6e8882dce83be22a08456f6bdf8fa9b52c8bddf97fdcc1fc4d02209f0e5244e`.
- All modified Claude binaries were temporary files under `/tmp` and were deterministic `bun_handler.py` repacks.
- `/tmp/cliproxyapi-source` remained clean.
- `/tmp/cliproxy-runtime-probe` contains four untracked temporary focused test files: the original replay/count probe plus credential-refresh, retained-socket, and partial-write probes.
- Temporary CLIProxy instances used isolated configs and localhost upstream stubs.
- All Claude fork/tombstone/context/compaction fixtures used temporary config directories and temporary repacked binaries under `/tmp`.
- No external generation or remote service was contacted.

## Dependency-order conclusion

The corrected ladder is strengthened, not reordered:

1. Rungs 0–2 remain separable profile/admission/observability work.
2. Rung 3 must establish canonical post-materialization replay identity, caller/upstream isolation, client-reasoning precedence, and injective tool IDs.
3. Rung 4 must make replacements and hint clears durable and establish crash-safe reconstruction boundaries.
4. Only then can rung 5 activate 422 context hints and precompute reuse. Rung 5 must invalidate on same-UUID content changes, model/config/tool/beta/provider changes, and must use revised anchor-partitioned admission accounting rather than generic `Oe`.
5. Rung 6 remains optional and blocked until late-frame routing, cancellation, credential affinity, and ambiguous-execution behavior are corrected.
