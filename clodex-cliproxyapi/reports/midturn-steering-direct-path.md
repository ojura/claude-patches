# Mid-turn steering on the direct Claude Code → CLIProxyAPI path

Date: 2026-08-10

> **Absorbed on 2026-08-11.** Current conclusions and evidence classifications now live
> in [`../ARCHITECTURE.md`](../ARCHITECTURE.md),
> [`../evidence/claude/midturn-steering-order.json`](../evidence/claude/midturn-steering-order.json),
> and [`../evidence/cliproxy/direct-midturn-translation.json`](../evidence/cliproxy/direct-midturn-translation.json).
> This file remains as the focused narrative and must not be the only destination for a
> current claim.

This report folds the Claude Code 2.1.220 mid-turn queue investigation into the
current direct request path:

```text
Claude Code → CLIProxyAPI → Codex
```

It covers queue timing, message normalization, direct protocol translation,
prompt caching, controlled response checks, and remaining tests. Request-rewriting
middleware that is not part of the current path is outside this report.

## Evidence and versions

Evidence classes follow [`../ARCHITECTURE.md`](../ARCHITECTURE.md#evidence-levels):

- **Executable-reproduced:** observed in a localhost Claude child, captured Claude
  requests, or isolated direct CLIProxy response checks.
- **Source-confirmed:** reachable in the hash-pinned Claude bundle or CLIProxy source,
  but not necessarily induced in a focused runtime fixture.
- **Runtime/provider-dependent:** not settled by local source and fixtures.

Pinned inputs:

- Claude Code 2.1.220 extracted bundle:
  `/tmp/claude-2.1.220.js`, SHA-256
  `e32e7ead0b8ec4815fb69806bfad0116bdb9b51bba927fbd172f5a4a2903ce6e`.
- CLIProxyAPI Go/source lane: `/tmp/cliproxyapi-source` at
  `a88197f845c979132c8978ea223c6af05cc81536`, tag `v7.2.116`.
- Model used by the controlled checks: `gpt-5.6-sol`.

The installed Claude binary is locally patched. The executable capture describes the
hash-pinned installed bytes; source inspection independently establishes the queue and
normalization paths described below.

## Busy-session queue and fold timing

Claude Code reads input while a query is running. A human prompt submitted during the
query enters the message queue instead of starting a concurrent outer query.

The query does not check that queue continuously. It takes a fold snapshot only after:

1. the model emitted at least one `tool_use`;
2. all tool executions finished;
3. `PostToolBatch` hooks finished; and
4. the query did not already take an abort, hook-stop, requested-end-turn,
   dynamic-loop-end, max-turn, deferred-tool, or suspended-fold path.

The snapshot selects commands through priority `next`, which includes `now` and `next`
but excludes `later`. The main-thread filter keeps commands addressed to the main agent;
a subagent keeps only task notifications addressed to that agent. Slash-like commands
are excluded unless slash processing was explicitly skipped. Of the selected entries,
only `prompt` and `task-notification` modes produce `queued_command` attachments and
enter the removal batch.

The transcript order is:

```text
assistant tool_use
user tool_result
attachment queued_command
```

The attachments are appended after the tool results, then Claude Code starts another
model request with the combined history. This preserves the required assistant-tool-call
and user-tool-result adjacency.

The focused child capture reproduced the delivery order. The tool result appeared first,
the replayed human update appeared next, and the following assistant action used the
updated request. The human input carried an earlier submission timestamp because it had
been queued while the tool was still running. The ordering is summarized in
[`../evidence/claude/midturn-steering-order.json`](../evidence/claude/midturn-steering-order.json).

### Final-response timing limit

A model response with no `tool_use` never reaches the post-tool queue snapshot. Claude
Code runs Stop hooks and completes the turn first. Input arriving during final prose,
after the last tool-boundary snapshot, or during Stop hooks cannot change that provider
response. It becomes a later top-level user turn.

This is independent of proxy translation. A proxy cannot add input to a response that
has already completed. Matching native pending-input behavior would require a Claude-side
rule that starts another model request whenever human input remains queued after the
current sample.

## Removal, abort, and cancellation

Selected queue UUIDs are marked fold-in-flight while attachments are generated. Removal
happens after attachment emission rather than before it.

Consequences established from source:

- If a selected entry produces no `queued_command` attachment, it stays queued.
- A partial attachment emission logs an error but removes the whole selected batch.
- If the outer abort signal is observed after attachment emission and before removal,
  the command remains queued. Its attachment may already be present in the aborted
  transcript, so the same text can be delivered again later.
- An abort after the removal check does not restore the command.
- `cancel_async_message` cannot remove a fold-in-flight UUID and reports
  `cancelled:false` for that case.
- `interrupt(cancel_queued:true)` is the separate operation that aborts the turn and
  also cancels fold-in-flight UUIDs.

These race cases are source-confirmed. The investigation did not retain a focused runtime
fixture that induces the abort between attachment emission and queue removal.

## Message-level system normalization

The `queued_command` attachment converter creates Claude's explanatory human-steering
text. The normalizer then decides whether that content remains ordinary user content,
is appended to a tool result, or becomes a later message-level system item.

For text-only attachment content on a model with mid-conversation-system support, the
normalizer takes the system path before the chair-sermon logic. It emits an internal
`api_system` item and sends the beta:

```text
mid-conversation-system-2026-04-07
```

The outbound Claude order is:

```text
assistant tool_use
user tool_result
system steering reminder
```

The stable SDK and harness instructions remain in the request's top-level `system`
field. Claude Code therefore uses two distinct system channels:

- top-level `system` for stable instructions;
- message-level `role: "system"` for later reminders and steering.

The captured second request reproduced both channels and the post-tool message order.

### Capability selection

In 2.1.220, the mid-conversation-system capability check applies these relevant rules:

1. HIPAA mode disables it.
2. `CLAUDE_CODE_FORCE_MID_CONVERSATION_SYSTEM=1` forces it on otherwise.
3. Per-model metadata feature `mid_conversation_system`, when present, decides it.
4. Explicit older Claude model families are denied.
5. Model capability `mid_conv_system`, Mythos 5, or an eligible provider route enables
   it.

If the endpoint rejects the role or beta, `midConvFallback` normalizes the request again
with message-level system promotion disabled.

### `tengu_chair_sermon`

The chair-sermon flag does not decide the normal text-only promoted path because that
path runs earlier.

On fallback paths:

- with the flag false, all-text attachment content following a string-valued tool result
  can already be appended to the `tool_result.content` string;
- with the flag true, Claude performs broader mixed-content relocation into a preceding
  tool result and a final cleanup pass over remaining reminder blocks;
- tool results containing `tool_reference` refuse the broader relocation;
- error and mixed-media cases can discard or move attachment blocks differently.

False therefore does not mean that steering always remains separate from tool output.
Leaving it false only avoids the broader true-path relocation when system promotion does
not consume the attachment first.

## Direct CLIProxyAPI translation

CLIProxyAPI `v7.2.116` already converts the two Claude system channels separately:

- top-level Claude `system` text becomes a Responses `developer` message;
- a Claude message-level `role: "system"` item becomes a Responses `user` message at
  the same history position;
- Claude `tool_result` becomes `function_call_output`.

For promoted mid-turn steering, the translated order is:

```text
assistant function_call
function_call_output
user steering reminder
```

This preserves the important output-then-update chronology. Native Codex uses plain user
text at that point, while the current CLIProxy helper wraps the text in one outer
`<system-reminder>` pair.

The helper does not detect a complete existing wrapper. Already wrapped message-level
system content can therefore acquire nested reminder tags. This is a formatting issue;
it does not move the item.

Source locations at the pinned revision:

- top-level developer conversion:
  `internal/translator/codex/claude/codex_claude_request.go:51-83`;
- message-level system conversion:
  `internal/translator/codex/claude/codex_claude_request.go:90-100`;
- tool-result conversion:
  `internal/translator/codex/claude/codex_claude_request.go:200-249`;
- reminder helper:
  `internal/translator/common/claude_system.go:15-27`.

The focused source and response summary is
[`../evidence/cliproxy/direct-midturn-translation.json`](../evidence/cliproxy/direct-midturn-translation.json).

## Prompt caching

CLIProxy derives a deterministic prompt-cache UUID from:

```text
model
+ Claude root session
+ Claude agent
```

The implementation is in
`internal/runtime/executor/helps/claude_code_session.go:96-105`; the Codex executor adds
that value as `prompt_cache_key` and `Session_id` in
`internal/runtime/executor/codex_executor_request.go:91-147`.

The key is a performance namespace, not proof that replay or continuation is valid. A
cache hit still requires matching request content through the provider's cache boundary.

Keeping a new steering item at the chronological tail allows the earlier request prefix
to remain identical when nothing else changed. Direct translation does not itself rewrite
an early historical item merely because a later reminder arrived. This is the desired
cache property; provider cache-hit amounts remain runtime-dependent.

## Controlled model response checks

Two isolated direct-route matrices tested whether `gpt-5.6-sol` could use the steering
content after translation.

### Short correction matrix

A 4,625-character tool result recommended one port. Later steering required another.
Six representations were tested three times with thinking disabled, plus one xhigh trial
for three representations:

- plain user text;
- Claude's third-person steering scaffold;
- a complete reminder wrapper;
- reminder text alone;
- steering appended to tool output;
- message-level system.

All 21 trials selected the corrected port.

This rejects a deterministic claim that the model cannot understand reminder tags,
third-person wording, or steering inside tool output. It does not show equal reliability
in long work.

### Long-history acknowledgement matrix

A second matrix constructed 40 tool cycles before the update. The direct-path forms
retained here account for 12 trials:

| Representation | Verbal acknowledgement | Fact included in next tool arguments |
|---|---:|---:|
| Latest plain user message | 3/3 | 3/3 |
| Latest complete wrapper | 3/3 | 3/3 |
| Appended to tool result | 3/3 | 1/3 |
| Latest message-level system | 3/3 | 1/3 |

Every response acknowledged the update, but acknowledgement did not always change the
next tool arguments. The sample is small, synthetic, and explicitly requested an
acknowledgement, so it does not estimate a production failure rate. It demonstrates that
receiving an update and applying it to the next action are separate observations.

The full matrix outputs contain large model signatures and are not copied into this
corpus. The durable aggregate, temporary source paths, and limitations are in the focused
CLIProxy evidence file.

## Current decisions

1. The request path is direct Claude Code → CLIProxyAPI. The shell/service wrapper may
   set environment variables, serialize startup, and check health; it does not rewrite
   request bodies.
2. Preserve top-level and message-level system roles until CLIProxy performs its
   source-aware conversion.
3. Preserve the post-tool position of human steering. Do not move it into an early user
   message.
4. Do not append steering to tool output when message-level system promotion is
   available. Tool output cannot prove that identical reminder text came from the human.
5. An optional future conversion may recognize Claude Code's exact human-steering
   scaffold and emit plain user text at the same position. It must preserve adjacent
   reminders and mixed text/image input.
6. Treat late-arrival follow-up as Claude-side lifecycle work. Proxy translation cannot
   repair the absence of a final queue snapshot.

## Verification additions

Future regression checks should cover:

- a stable top-level system becomes one developer item;
- a post-tool message-level system remains after `function_call_output`;
- multiple later system items retain their relative positions;
- text-plus-image queued input remains one user message with both parts;
- an already wrapped message-level system does not acquire unintended nested wrappers;
- changing only trailing steering does not rewrite the earlier translated prefix;
- abort between attachment emission and queue removal has an explicit duplicate-delivery
  result;
- `cancel_async_message` and `interrupt(cancel_queued:true)` differ as documented;
- input arriving during final prose or Stop hooks produces a later turn, or a future
  follow-up rule starts another sample.

## Retention notes

The original request capture, child event stream, and full matrix responses remain under
`/tmp/midturn-steering-research`. They are not part of the durable corpus. The two focused
JSON files preserve the fields needed for the current conclusions without copying full
harness instructions, large reasoning signatures, temporary authentication directories,
or unrelated request history.
