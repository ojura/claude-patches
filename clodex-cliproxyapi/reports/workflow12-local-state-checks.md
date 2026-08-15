# Workflow 1/2 local state-consistency checks

Date: 2026-08-05

> **Historical snapshot.** This report records the first executable pass. Later checks
> and corpus maintenance narrowed several evidence classes and closed some items.
> Use [`../ARCHITECTURE.md`](../ARCHITECTURE.md) for current conclusions and
> [`../ARTIFACTS.md`](../ARTIFACTS.md) for retention limitations.

All checks were local. Claude Code used loopback HTTP stubs. CLIProxy checks used a temporary source copy or the configured localhost endpoints. No external model request was made.

## Session rotation

- Native `/clear`: header and `metadata.user_id.session_id` both rotated from `11111111-1111-4111-8111-111111111111` to a new UUID.
- With `ANTHROPIC_CUSTOM_HEADERS` pinning `X-Claude-Code-Session-Id`, the body rotated but the header remained old.
- CLIProxy replay scope selected the old header value over the new body session.
- Capture: `/tmp/claude-clear-probe-result.json`.

## CLIProxy replay/count checks

Temporary test: `/tmp/cliproxy-runtime-probe/internal/runtime/executor/z_workflow_runtime_probe_test.go`.

Confirmed current behavior:

- An unanchored marked reasoning turn ignores a mismatched stored request fingerprint.
- Distinct valid client reasoning and marked cached reasoning are both retained.
- Changing instructions, tools, and effort while retaining identical `input` leaves the replay fingerprint unchanged.
- Assistant segmentations `["ab","c"]` and `["a","bc"]` collide.
- Tool IDs `call:a` and `call/a` share a comparable sanitized alias.
- Claude header session identity overrides conflicting body metadata.
- Native Responses body `{"input":"hello"}` counts as zero.

Focused tests: 6/6 passed, meaning each current behavior was reproduced.

The upstream executor package suite failed only these three environment/profile expectations:

- `TestApplyClaudeHeaders_DisableDeviceProfileStabilization`
- `TestApplyClaudeHeaders_LegacyModePreservesConfiguredUserAgentOverrideForClaudeClients`
- `TestClaudeExecutor_NonClaudeRequestUsesClaudeCode220CLIFingerprint`

All expected Linux but received the configured MacOS device profile. Log: `/tmp/cliproxy-executor-suite.log`.

## Local CLIProxy instance

- `GET /v1/models`: HTTP 200, 14 models, `gpt-5.6-luna` present.
- `POST /v1/messages/count_tokens` for `hello`: HTTP 200, `input_tokens: 1`.
- No generation request was sent to an upstream provider.

## Replacement persistence and crash recovery

A 1.9 MiB Bash result was reduced to a 2,276-byte `<persisted-output>` wrapper and stored in `tool-results/...txt`.

Normal completion:

- Current request contained the wrapper.
- Fresh-process resume reconstructed the same wrapper.

Forced stop after the wrapped request reached the local stub:

- 0, 10, 25, and 50 ms: resume omitted the entire assistant/tool-result turn, although the model request had contained the wrapper and the artifact file existed.
- 100, 200, and 500 ms: resume reconstructed the wrapper.
- The observed inconsistency was turn omission, not restoration of the raw 1.9 MiB body.

## Manual compaction and reconstruction

After three completed turns, interactive `/compact`:

- issued a summary request;
- wrote a `compact_boundary` and summary marker to JSONL;
- used the summary in the next same-process request;
- reconstructed and used the same summary after a fresh-process `--resume`.

Capture: `/tmp/claude-interactive-compact-requests.json`; isolated session root from the successful run: `/tmp/claude-interactive-compact-f1dg_q26`.

## Automatic compaction and reconstruction

With `autoCompactEnabled: true`, a large queued prompt triggered automatic compaction:

- a summary request was issued;
- a compact boundary and summary were written;
- the final model request used the summary;
- a fresh-process resume retained the summary.

Capture: `/tmp/claude-auto-compact-requests.json`.

## Precomputed compaction

The print/stream-json setup did not produce a separately persisted background precompute result below the automatic-compaction threshold, including with local feature-cache and setting overrides. The same-UUID stale-precompute scenario was not reproduced in this pass.

## Additional observation

Claude's outbound request used `max_tokens: 128000`, while stream-json result telemetry reported `modelUsage.gpt-5.6-luna.maxOutputTokens: 32000`.

## Source state

- `/tmp/cliproxyapi-source` remained clean.
- The only Go test added was in the temporary copy `/tmp/cliproxy-runtime-probe`.
- No commits were created.
