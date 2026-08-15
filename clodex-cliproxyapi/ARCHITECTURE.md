# Clodex + CLIProxyAPI architecture findings and decisions

This document is the durable home for the Claude Code 2.1.220 → CLIProxyAPI →
Codex integration investigation. It records the system model, findings, decisions,
dependency order, alternatives considered, and remaining validation work.

It is larger than a patch recipe because the integration crosses three stateful
systems. A change can behave correctly in one component while leaving the combined
state inconsistent. Keeping the reasoning beside the implementation helps later work
reuse the same system model instead of solving one local symptom in isolation.

The implementation scope is the Codex route. The investigation also found similar
questions in non-Codex replay paths, including xAI replay after Claude-side compaction
and CLIProxy persistent-home KV replay. Those paths were not audited to the same depth
and need separate provider-specific work before drawing broader conclusions.

## Scope and pinned sources

This document describes the state established on 2026-08-05, with direct-path
steering findings added on 2026-08-10 and automated permission-review findings added
on 2026-08-11. The focused reviewer analysis and direct translation sketch are in
[`CLASSIFIER.md`](CLASSIFIER.md).

Pinned inputs:

- Claude Code native binary:
  `/home/juraj/.local/share/claude/versions/2.1.220`
- Installed binary SHA-256:
  `d6e8882dce83be22a08456f6bdf8fa9b52c8bddf97fdcc1fc4d02209f0e5244e`
- Extracted entrypoint source size: 21,636,716 bytes
- Extracted source SHA-256:
  `e32e7ead0b8ec4815fb69806bfad0116bdb9b51bba927fbd172f5a4a2903ce6e`
- Claude Code bundle container codec:
  `../util/bun_handler.py`
- CLIProxyAPI Go/source audit lane:
  `/tmp/cliproxyapi-source` at commit
  `a88197f845c979132c8978ea223c6af05cc81536`, tag `v7.2.116`
- CLIProxyAPI retained-WebSocket Python fixture lane:
  `/home/juraj/.ccs/cliproxy/bin/original/cli-proxy-api`, version `7.2.112`,
  commit `a63da8ae`, SHA-256
  `8f4c9e1e3ddcabede29236569cb835fb160e167255ca340e9df5b6a3d910bc5a`
- Codex reference source: `/tmp/codex-source` at commit
  `d75f94a94d5cb0bbabc59b86c0427c7ad09a9d6d`
- Installed CCS examined by the audits: 8.8.1 under
  `/home/juraj/.local/lib/node_modules/@kaitranntt/ccs`; hashes of the audited
  package and route files are pinned in
  [`evidence/provenance/source-pins.json`](evidence/provenance/source-pins.json)

The CLIProxy Go/source and late-frame executable lanes are version-split. Claims must
name the lane that supports them; source confirmation at `v7.2.116` does not by itself establish
behavior in the `7.2.112` binary.

Claude Code 2.1.220 is the version examined for this project. Later version drift
does not change conclusions about 2.1.220; it is a separate maintenance concern.

The installed binary is already locally patched. New experimental edits must be
made against temporary copies, structurally located, repacked once, and executed
without treating the installed `(patched)` version suffix as evidence of the
active semantic changes.

`bun_handler.py` has passed its extraction and repack checks against the pinned
binary:

- supported `.bun`-section ELF format;
- no-op repack is byte-identical;
- length-changing source replacement produces the expected binary size delta;
- re-extraction returns the exact intended source;
- identical edits are deterministic;
- the installed binary executes and reports `2.1.220 (Claude Code) (patched)`.

Those checks validate the extraction and repack path. They do not establish the
behavior of any context-management patch.

## Architecture summary

The current request path is direct:

```text
Claude Code → CLIProxyAPI → Codex
```

A sourced shell wrapper may start one shared CLIProxyAPI process, check its health, and
export the Claude environment. It does not rewrite request bodies.

Claude auto mode also follows this direct path. Claude Code makes an ordinary separate
reviewer request, CLIProxyAPI translates its core messages and forced tool choice to OpenAI
Responses, and Claude Code applies the returned allow/deny result. Native Codex Guardian
uses the same separate-reviewer design, but its internal session manager is not required for
Claude requests. The current translator's classifier-control omissions are recorded in
[`CLASSIFIER.md`](CLASSIFIER.md).

Claude Code remains responsible for the conversation history:

```text
Claude Code manages:
    the user-visible transcript
    the model-facing history
    compact boundaries and summaries
    content replacements and hint clears
    resume, fork, rewind, and reconstruction
    context UI and local admission state

CLIProxyAPI manages:
    protocol translation
    encrypted-reasoning replay derived from requests
    prompt-cache namespaces
    provider and credential affinity
    retained transport sessions
    optional continuation optimizations
```

CLIProxy state is derived from Claude requests, while Claude Code remains the source
for conversation history. Replay, cache, route, and transport reuse therefore needs
to match the current model-facing request; a shared session ID alone is insufficient.

### Append-only transcript invariant

Claude JSONL is the authoritative append-only event log. No implementation may truncate,
rewrite, delete, or physically garbage-collect existing JSONL rows. Semantic compaction
appends a compact boundary and a new root; earlier rows remain on disk and are excluded only
when Claude reconstructs the active lineage.

All state required to reconstruct a request must be present in that append-only event log.
CLIProxyAPI may decode provider-specific state replayed from JSONL, but it must not own an
alternate conversation history. Resume, fork, rewind, and recovery must remain derivable
from JSONL alone.

## Core compatibility rule

For every model request, the proxy-derived state used to prepare or continue that
request must be compatible with the exact model-facing semantic request and with
the trusted route on which it will execute.

Formally, a request may reuse derived state only if all required dimensions match:

```text
semantic compatibility
AND trusted route compatibility
AND lineage/transport continuity where stateful continuation is involved
AND durable source history is not contradicted by the current reconstruction
```

Correctness does not depend on a cache hit, retained socket, replay item, or
`previous_response_id` being available. Each is optional and disposable.

## Two related state models

The investigation uses two views. They answer different questions and should not
be collapsed into one vague "history generation."

### State planes

These classify where a divergence lives:

| Symbol | Plane | Meaning |
|---|---|---|
| **M** | Model-visible request | The final normalized request the model actually sees |
| **D** | Durable reconstruction | What a fresh process reconstructs from persisted state |
| **R** | Retained reachability | Which branches, ancestors, compacted records, and alternate histories remain recoverable |
| **A** | Artifacts | Persisted-output ownership, digest, size, truncation, location, and availability |
| **S** | Sidechain/task state | Parent anchor, agent transcript, task output, metadata, completion, and notification state |
| **P** | Proxy-derived state | Replay, prompt cache, route affinity, transport, and trust state |

Examples:

- A model can see a persisted-output wrapper in **M** while a crash leaves no
  corresponding turn in **D**.
- Physical JSONL compaction can preserve current **M** while deleting an ancestor
  needed by a future agent resume in **R/S**.
- Relocation can preserve transcript text while breaking absolute artifact paths in
  **A**.
- A proxy can advance replay state in **P** on `response.completed` before Claude
  has durably advanced **D**.

### Compatibility and identity dimensions

These classify what a reuse decision must validate:

| Symbol | Dimension | Required content |
|---|---|---|
| **C** | Semantic-request compatibility | Canonical digest of the target model and final source-derived model-facing request |
| **U** | Trust/upstream affinity | Downstream caller, provider, endpoint, deployment, provider/model revision where observable, account/credential compatibility, selected route, transport, handshake properties |
| **E** | Continuation generation | Monotonic branch/turn generation for retained sockets and future `previous_response_id` |
| **D** | Durable commit generation | The committed reconstruction state that justified exposing a mutation |
| **R** | Reachability generation | Selected leaf and retained ancestry/branch graph |
| **A** | Artifact manifest generation | Owner, digest, actual size, truncation state, location, availability |
| **S** | Sidechain/task lineage | Parent snapshot/anchor, agent identity, transcript, output, completion barrier |

These do not need seven HTTP headers. A versioned structured envelope can encode
several dimensions. The requirement is logical independence: matching one cannot
stand in for the others.

Notation convention: unqualified **M/D/R/A/S** in prose names a state plane. A phrase
such as “durable commit generation” or “**D/R/A/S** generation” names the corresponding
compatibility dimension. The repeated letters are intentional because the dimensions
version those planes; the qualifier is required wherever the reading could be ambiguous.

Responsibilities and fallback behavior are:

| Dimension | Source | Proxy behavior when absent or incompatible |
|---|---|---|
| **C** | CLIProxy computes it from the complete post-normalization virtual request, including the target model | Skip replay or continuation reuse and execute from the full request |
| **U** | CLIProxy derives it from the caller, selected route, endpoint, credential generation, and transport | Keep derived state separate; a session ID alone does not connect routes or callers |
| **E** | CLIProxy maintains a monotonic transport generation and resets it when the request lineage or transport changes | Disable retained continuation and open or reset the transport |
| **D/R/A/S** | Claude Code produces these states; a future integration needs a versioned history/artifact/lineage record committed with the corresponding JSONL mutation | Until that record exists, these dimensions are insufficient for stateful continuation; exact request content may still support conservative content-addressed replay |

Open integration item: specify the producer, commit point, serialization, transport,
and verifier for **D/R/A/S**. The design must say whether Claude Code emits a
versioned record or digest in request metadata, how that value is committed atomically
with the JSONL mutation, and how CLIProxy rejects missing, stale, or incompatible
values. No stateful continuation may depend on these dimensions until that contract is
implemented and tested.

A proxy response can complete before Claude durably advances **D**. There is no
cross-process transaction. Each later request therefore checks **C/U** again, while
Claude-side replacements, compactions, forks, and task completion need explicit
commit points.

At minimum, CLIProxy needs two distinct compatibility checks:

- **C**, for content replay such as encrypted reasoning;
- **E**, for stateful continuation such as retained WebSockets or future
  `previous_response_id`.

An exact historical prefix after rewind might eventually permit content replay if
the provider contract permits it. It does not, by itself, permit reuse of a prior
transport turn.

Exact bytes are necessary but not sufficient for opaque replay across silent provider
revisions. Replay entries require a bounded age and, where available, a provider/model
revision or deployment generation in **U**. If no trustworthy revision identity is
available, expiry is the practical way to prevent indefinite reuse.

## Evidence levels

Every claim in this document belongs to one of three evidence classes:

1. **Executable-reproduced** — observed with the installed 2.1.220 binary, local
   CLIProxy tests, or local fake providers.
2. **Source-confirmed** — statically reachable in the pinned source, but the
   branch was not induced end to end.
3. **Runtime/provider-dependent** — cannot be closed from local source alone.

The installed 2.1.220 binary carries a pre-existing local patch whose exact delta was
not retained. Executable results against it are valid observations of that pinned
binary, not evidence that an unknown patch had no influence on the exercised
subsystem. Instrumented-binary results must additionally disclose the forcing edit;
a surviving binary hash identifies bytes but does not reconstruct semantics.

Edits must preserve these evidence classes. Source inspection is not relabeled as an
executable reproduction; separate executable controls are not presented as one
end-to-end run; and runtime uncertainty remains identified as such. The same rule also
works in the other direction: plain-language rewrites must not soften a source-confirmed
fact into a runtime possibility.

## Superseded claims

Later evidence explicitly supersedes these earlier statements. They remain listed so
an old report cannot silently reintroduce them.

| Earlier claim | Corrected conclusion |
|---|---|
| A replacement crash restores the original raw tool body | The exercised 0–50-ms window omitted the entire assistant/tool turn after the model had seen the wrapper; raw restoration was not reproduced in that fixture |
| Credential-refresh WebSocket behavior is inconclusive | Two executable controls plus source-confirmed service glue establish stale same-ID handshake reuse; one end-to-end watcher→service→request fixture is still absent |
| Unknown-outcome WebSocket resend is unexercised | After the upstream read 65,536 bytes of a 32-MiB request, the full request was resent; duplicate execution risk is executable-reproduced |
| Tombstone interruption and >50-MiB fallback are source-only | Both broad suffix truncation and silent large-file failure are executable-reproduced; the interruption used an instrumentation-widened pause |
| Fork artifact ownership is only a design concern | A simulated parent-owned wrapper/artifact reproduced the fork copy-and-dangle behavior; the genuine replacement-ledger fork path remains unexercised |
| Physical-GC activation is generally unresolved | Initialization is source-resolved to the `sdkUrl`/remote-session resume branch; this remains a historical source finding, while the accepted architecture forbids physical JSONL GC |
| Rewind requires a separate successor-marker repair | Withdrawn: once the new user record commits, the tested branch resumes correctly; only the ordinary early write window remains |
| CCS effort suffixes always override Claude `/effort` | Route-dependent: normal CodexReasoningProxy translation preserves Claude-native effort, while fallback suffix handling can override it |
| `uZc` teaches Claude Code a 372k input window | False in 2.1.220: discovered `max_input_tokens` has no production consumer; `uZc` is output-cap discovery only |
| Claude `/v1/models` emits IDs only | Obsolete: current handler emits display and numeric token fields, subject to last-writer, synthetic-default, route, and cloaking caveats |
| One nonzero `Oe` scalar can reconcile hint savings | Rejected: admission and prefix diagnostics require different anchor-aware accounting |

## Origin of the investigation: the 336k/372k failure

The motivating session showed:

```text
/context headline:             336.2k / 372k
projected category total:      approximately 358.7k
free shown by /context:        approximately 10.3k
auto-compaction at the time:   disabled
```

The tool-result distribution was extreme:

```text
Bash: 82.3k
Read: 145.8k
```

The one-character next prompt did not add 22.5k. The headline showed the latest API
input usage, while the category analysis projected the next request.

The displayed 10.3k free space came from a different boundary again. With
auto-compaction disabled, `/context` subtracts only the flat 3k manual compact buffer:

```text
displayed free = 372.0k raw - 358.7k projected - 3.0k buffer ≈ 10.3k
```

The 20k output reservation remained active in admission but was absent from that UI
branch. Operative headroom was therefore approximately -9.7k while the display showed
+10.3k: wrong sign, not merely a stale magnitude. The difference is exactly the omitted
reservation: `+10.3k - 20k = -9.7k`. The authoritative bundle implements this in `jLo`
(`/tmp/claude-2.1.220.js:16915`, relevant bytes approximately 10,337,200–10,339,000):
when auto-compaction is disabled it sets
the reserved category to `ZMu=3000` (`"Compact buffer"`) and computes free space as
`max(0, window - projected - reserved)`. The readable orientation source names the
same branch `analyzeContextUsage` and uses `MANUAL_COMPACT_BUFFER_TOKENS` at
`~/claude-code/src/utils/analyzeContext.ts:1139-1150`. With auto-compaction enabled,
`jLo` instead derives the automatic buffer from the effective compact threshold and
would show zero free for that projection.

The bundle-to-readable-source correspondence is:

| Authoritative 2.1.220 bundle | Readable orientation source |
|---|---|
| `ZMu=3000` | `MANUAL_COMPACT_BUFFER_TOKENS = 3_000` in `autoCompact.ts:65` |
| `QMu=13000` | `AUTOCOMPACT_BUFFER_TOKENS = 13_000` in `autoCompact.ts:62` |
| `se = vSe(t,f) - QMu` | `autoCompactThreshold = getEffectiveContextWindowSize(model) - AUTOCOMPACT_BUFFER_TOKENS` in `analyzeContext.ts:1001-1005` |
| auto enabled: `te = m - se`, `"Autocompact buffer"` | `reservedTokens = contextWindow - autoCompactThreshold` in `analyzeContext.ts:1131-1138` |
| auto disabled: `te = ZMu`, `"Compact buffer"` | `reservedTokens = MANUAL_COMPACT_BUFFER_TOKENS` in `analyzeContext.ts:1139-1147` |
| `he = Math.max(0, m - ve - te)` | `freeTokens = Math.max(0, contextWindow - actualUsage - reservedTokens)` in `analyzeContext.ts:1149-1150` |

This also identifies `getEffectiveContextWindowSize` as the readable counterpart of
`vSe`: it subtracts the 20k output reservation. In the standard auto-enabled branch,
the displayed automatic buffer is therefore 20k reserve plus 13k margin, or 33k. The
readable source explicitly suppresses that row in reactive-only and context-collapse
modes because proactive auto-compaction does not use it (`analyzeContext.ts:1105-1130`).
It does not suppress the auto-disabled branch, which instead displays only the flat 3k
manual buffer while admission still retains the 20k reservation.

The readable tree and bundle differ in the shape of the skip guard: the tree names two
feature-flag branches, while the bundle condenses the condition to
`!(ce && g === "auto")`. This is expected orientation-source drift. The two constants,
the enabled and disabled buffer branches, and the free-space formula still correspond
exactly, so the readable names clarify the authoritative bundle without replacing it.

Claude Code computed an effective input window by subtracting an output reserve:

```text
raw input window:            372k
output reservation:          min(max output, 20k)
effective input window:      352k
hard safety margin:          3k
hard local admission limit:  349k
```

The projected request, approximately 358.7k, therefore failed locally before
CLIProxy or Codex saw it.

### Executable boundary evidence for the reserve equation

A fresh Luna session, safe mode, no persistence, identical input, and loopback stub
produced boundary shifts consistent with the admission equation independently of
source inspection:

- with 32k configured output, the input boundary moved by approximately 20k;
- with 128 tokens configured output, the boundary moved accordingly;
- at a fixed midpoint input, output 10,062 passed and 10,070 blocked, bracketing a
  source-predicted boundary of 10,064 at eight-token resolution.

The original console capture was not retained. The surviving probe and evidence-status
report are [`probes/claude/admission_boundary.py`](probes/claude/admission_boundary.py)
and [`reports/founding-experiments-and-evidence-gaps.md`](reports/founding-experiments-and-evidence-gaps.md).
The operative reservation is independently source-confirmed as:

```text
reservation = min(configured max output, 20,000)
```

Both `vSe` and `Fny` apply this policy. Patching only one leaves the other as an
earlier boundary.

## Capacity policy

For exact models:

```text
gpt-5.6-sol
gpt-5.6-terra
gpt-5.6-luna
```

the intended contract is:

```text
raw input capacity:   372,000
output capacity:      128,000
shared-window reserve: none
hard safety margin:   retain the existing 3,000 unless separately justified
```

This does not assert that every provider request near 372k plus a requested 128k
output succeeds. The independent catalog shape and local admission policy are
separate from the live provider contract. Direct boundary experiments remain the
provider-level evidence.

The exact-model condition is essential. Do not globally remove the 20k reserve for
Claude models or arbitrary custom providers.

## Implementation order

The historical patch rungs remain useful after adding two prerequisites that the
original ladder lacked.

```text
0  coherent profile
1  exact-model input-only admission
2  additive observability
3  proxy replay compatibility
4  Claude durability
5  durable context hints + precompute integrity
6  optional transport continuation
```

Dependency order is part of the architecture:

```text
0–2
  ↓
3
  ↓
4
  ↓
5
  ↓
6
```

Rung 5 depends on Rungs 3 and 4. Rung 6 depends on replay,
transport, and credential-affinity work.

### Historical rung mapping

| Earlier rung | Current location | Decision |
|---|---|---|
| Rung 0 — profile configuration | Rung 0 | Retained |
| Rung 0.5 — model-capabilities switch | Separate capability-discovery project | Deferred; output-only, cache-identity and cloaking prerequisites |
| Rung 1 — input-only admission | Rung 1 | Retained as first independently shippable patch |
| Rung 2 — `/context` correction | Rung 2 | Retained as additive observability; scalar substitution rejected |
| Rung 3 — CLIProxy 422 handshake | Rung 5 | Retained only as controlled activation after replay and durability prerequisites |
| Rung 4 — eager pre-admission clearing/accounting | Rung 5, heavily revised | Original generic `Oe` design rejected |
| Rung 5 — `previous_response_id` | Rung 6 | Optional and last |

## Rung 0: coherent profile

Use bare Claude-facing IDs:

```text
Opus:   gpt-5.6-sol
Sonnet: gpt-5.6-terra
Haiku:  gpt-5.6-luna
```

Configure:

```text
CLAUDE_CODE_MAX_CONTEXT_TOKENS=372000
CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000
CLAUDE_CODE_AUTO_COMPACT_WINDOW=372000
```

Do not permit a 372k-configured process to fall back to a 272k GPT-5.5 route.

Auto-compaction is currently enabled. It is policy, not capacity. Enabling it does
not repair the input/output reserve bug, and disabling it is no longer the current
operating state. The explicit auto window keeps profile intent visible and avoids an
ambient default changing compact timing without an explicit profile setting; the input-only patch still controls
whether the false 20k reserve is subtracted.

### Effort handling

Bare IDs remain the default. On the direct path, CLIProxy derives Codex reasoning from
Claude-native `thinking` and `output_config.effort`, with its own recognized model-suffix
handling available only when a suffix is deliberately supplied.

The intended behavior is:

- bare + `/effort low` resolves to low;
- an explicit locked effort rewrites the Claude-native fields before translation;
- `speed: "fast"` maps to the intended priority/service tier;
- a model suffix is not the hidden control channel for Claude-target profiles.

Verification captures the final translated reasoning and service-tier values at the
CLIProxy request boundary rather than inferring them from the configured model string.

## Rung 1: exact-model input-only admission

Introduce one exact-model predicate and one policy helper for output reservation.
Apply it at both:

- `vSe` — effective automatic-compaction window;
- `Fny` — hard-input fallback.

Recheck the separate `s$t → exceeds200kTokens → LP` branch that controls the
`opusplan` plan-mode Opus upgrade. It tests total assistant usage
(input + cache creation + cache read + output), not input capacity. Any change there
requires its own policy rationale rather than mechanically reusing the input-only
predicate.

The main hard-admission guard is also deliberately bypassed on the reactive,
auto-window route. Rung 1 verification must cover both local-preflight paths and the
server prompt-too-long recovery path; `vSe`/`Fny` arithmetic does not define every
request boundary.

Expected exact-model thresholds after removing the false shared-window reserve while
retaining existing 13k/3k policy margins:

| Behavior | Before | Input-only policy |
|---|---:|---:|
| Effective input window | 352k | 372k |
| Automatic compact threshold | approximately 339k | approximately 359k |
| Hard local boundary | approximately 349k | approximately 369k |

These numbers are consequences of current Claude constants, not evidence that they are
optimal operational policy. Codex intervenes earlier. Raw capacity, warning policy,
automatic policy, and hard admission should remain independently visible and
configurable.

### Codex operational benchmark

Pinned Codex source (`d75f94a9`) provides a separate policy oracle:

- automatic compaction at approximately 90% of the model context;
- usable/effective full limit at approximately 95%;
- for 372k, approximately 334,800 automatic and 353,400 effective tokens;
- Responses requests do not carry a Claude-style `max_output_tokens` field;
- `COMPACT_USER_MESSAGE_MAX_TOKENS` is 20,000;
- tool output is bounded separately for model-visible content and retained raw
  execution output (approximately 10k tokens visible and 1 MiB retained in the audited
  lifecycle), with truncation/orphan repair;
- prompt-cache identity is session-derived;
- WebSocket continuation uses strict incremental-suffix eligibility.

These mechanisms are references, not hidden components of the Claude→CLIProxy route.
Expose a separate Codex-math oracle in tests rather than forcing Claude policy to copy
Codex constants.

### Effect of widening limits before durability work

Rungs 0–2 expose truthful 372k capacity and move Claude's current automatic/hard
boundaries later. That widens operation over native durability and stale-precompute
known behavior today. Initial rollout should therefore keep warning and automatic policy
conservative, disable precompute until Rung 5 integrity is available, keep context-hint
activation off until Rungs 3–4, and monitor transcript growth/tombstone failures. The
capacity correction is independently valid; maximum utilization is not the rollout
objective.

## Rung 2: additive observability

The one-expression proposal `T ?? ve → ve` is not used because it would make one display
look internally consistent by redefining an existing interactive,
noninteractive, remote, and transcript payload contract. The motivating 10.3k mismatch
is tied to the concrete `jLo`/`analyzeContextUsage` buffer branches and constant mapping
in [the origin analysis](#origin-of-the-investigation-the-336k372k-failure), not merely
to stale headline usage.

Expose separately:

- last API input;
- projected next request;
- raw input capacity;
- effective compaction window;
- selective-clear threshold if introduced;
- automatic compact threshold;
- hard admission threshold;
- remaining room before each boundary;
- output default;
- configured request output ceiling;
- hard output capability;
- estimated category attribution.

Category estimates are not required to sum exactly to the API usage snapshot. The
UI must label the difference rather than hide it.

### Confirmed 128k/32k contradiction

Executable capture showed:

```text
request max_tokens:            128000
modelUsage.maxOutputTokens:     32000
```

The source explains it:

- `Efo()` applies the configured output and caps it at the upper limit;
- the request builder uses `Efo()`;
- `modelUsage.maxOutputTokens` reports `lst(model).default`;
- unknown GPT models retain a 32k default with a 128k upper limit.

Capability discovery of a 128k upper limit does not change the 32k default because
the default is effectively bounded by `min(existingDefault, discoveredUpper)`.

Decision: either report the effective request ceiling or rename the field so it
unambiguously means default allocation. Errors, UI, and telemetry must use one
explicitly defined vocabulary.

## Rung 3: proxy replay

CLIProxy already carries derived semantic state. It is not a stateless translator.
Before adding any new continuation, fix the state it already injects.

### Replay results reproduced locally

Local Go checks reproduced all of the following:

1. A reasoning-only marked turn replays despite a mismatched request fingerprint.
2. Distinct valid client reasoning and cached marked reasoning are both retained.
3. Changed instructions, tools, and effort produce the same replay fingerprint when
   raw `input` is unchanged.
4. Assistant block sequences `['ab', 'c']` and `['a', 'bc']` collide.
5. Tool IDs `call:a` and `call/a` share a comparable sanitized alias.
6. Native Responses string input `{"input":"hello"}` can count as zero in the
   affected estimator.

Source-confirmed at CLIProxy `v7.2.116` (not part of the six local probe functions):
upstream tests `TestCodexExecutorReasoningReplayCacheSharesSameSessionAcrossClientKeys`
and `TestCodexExecutorReasoningReplayCacheSharesSameSessionAcrossCodexAuths`
deliberately permit sharing for the same supplied session.

The six-function result is retained as report prose plus the exact Go probe source; the
non-verbose package log does not name passing tests. A future rerun must retain `go test
-v` output and add a non-degeneracy assertion to the semantic-fingerprint probe so it
does not pass merely because both compared values collapse to an empty digest.

Evidence pointers:

- [`reports/workflow12-local-state-checks.md`](reports/workflow12-local-state-checks.md)
- [`probes/cliproxy/executor/replay_and_count_regressions_test.go`](probes/cliproxy/executor/replay_and_count_regressions_test.go)

### What the current replay fingerprint omits

The current fingerprint hashes only raw serialized Codex `input` items. It omits:

- target model and model revision/deployment identity;
- normalized instructions/system content;
- effective beta set;
- loaded tool names, descriptions, and schemas;
- tool choice and parallel-tool behavior;
- thinking, reasoning effort, and summary configuration;
- output/text mode;
- context-management result;
- service/speed tier;
- route and provider compatibility;
- final input-ID sanitation.

It is also representation-sensitive because raw JSON member order and escaping feed
the digest.

### Semantic request digest

**C** must be a versioned, typed, length-delimited digest of the effective,
post-materialization, source-derived model-facing request before proxy replay is
inserted.

It must include at minimum:

```text
resolved source protocol/version
target model identity
normalized system/instructions
canonical message/input items
effective beta set
actually loaded tools with descriptions and schemas
tool choice and parallel mode
thinking/reasoning/output configuration
context-management materialization result
max_tokens and semantically relevant request modes
```

It must exclude:

```text
cached reasoning being considered for insertion
request IDs
auth secrets
prompt-cache keys
transport-only noise
identity-confusion output
```

### When replay may be reused

A marked replay turn must have:

- a valid non-colliding assistant/tool anchor;
- exact **C** compatibility;
- compatible **U**;
- no conflicting valid client-supplied reasoning.

Reasoning-only unanchored turns must not replay.

Assistant anchors require typed boundaries. Tool-call IDs require an injective,
per-turn mapping that can be reversed exactly. Lossy sanitation aliases cannot be
identity.

### Updating replay cache entries

Replay writes and deletes must use conditional generation/snapshot semantics. An
older request that later receives an invalid-signature error must not delete a newer
cache generation written by another request.

Prompt-cache identity is only a performance namespace. It does not authorize replay,
tool restoration, or continuation.

### `/clear` rotates native identity but is not a universal proxy reset

Executable capture confirmed the normal native path rotates both
`X-Claude-Code-Session-Id` and `metadata.user_id.session_id`. When
`ANTHROPIC_CUSTOM_HEADERS` pins the old session header, the body rotates to the new
UUID while the header remains old. A focused CLIProxy test confirmed replay scope
prefers the old header over conflicting body metadata.

Therefore `/clear` creates a new default Claude namespace, but custom headers,
already-created requests, interceptor rewrites, compatibility aliases, execution
metadata, and retained downstream transport can preserve older proxy identity.
Old replay/cache entries are not deleted merely because the native UUID rotated.

Decision:

- reserve or validate Claude's session header so custom input cannot pin an
  obsolete identity;
- report and stop on header/body session disagreement before replay or route selection;
- treat `/clear` as an **E** reset and a new default **U** namespace;
- explicitly invalidate or detach retained transport rather than assuming request
  header rotation reaches connection-level state.

Evidence:

- [`evidence/claude/clear-session-identity.json`](evidence/claude/clear-session-identity.json) for native header/body rotation;
- [`probes/cliproxy/executor/replay_and_count_regressions_test.go`](probes/cliproxy/executor/replay_and_count_regressions_test.go) for header-over-body replay-scope precedence.

## Mid-turn human input on the direct path

Claude Code reads input concurrently with a running query, but it does not inspect the
busy-session queue continuously. After a model response containing at least one
`tool_use`, it waits for every tool result and `PostToolBatch` hook, then takes one queue
snapshot unless the query has already taken an abort, deferred-tool, hook-stop,
requested-end-turn, dynamic-loop-end, max-turn, or suspended-fold path.

The snapshot includes priorities `now` and `next`, not `later`. Main-thread selection
keeps entries addressed to the main agent; subagents keep targeted task notifications.
Slash-like input is excluded unless slash processing was skipped. Only selected
`prompt` and `task-notification` modes produce `queued_command` attachments and enter
the removal batch.

The transcript order is:

```text
assistant tool_use
user tool_result
attachment queued_command
```

Attachments are appended after tool results and the model is called again with the
combined history. A localhost child capture reproduced the tool-result, replayed-update,
then next-assistant-action order.

A response with no `tool_use` never reaches this snapshot. Input arriving during final
prose, after the last tool-boundary snapshot, or during Stop hooks cannot change that
response; it becomes a later top-level user turn. Proxy translation cannot repair this
because the provider response has already completed. Matching native pending-input
behavior requires a Claude-side follow-up request when human input remains queued.

### Removal, abort, and cancellation

Selected UUIDs are marked fold-in-flight while attachments are generated. Queue removal
happens after attachment emission:

- a selected entry that produces no attachment remains queued;
- partial attachment emission removes the selected batch after logging an error;
- abort after emission but before removal can leave an attachment in the aborted
  transcript while retaining the command for later delivery;
- abort after removal does not restore the command;
- `cancel_async_message` cannot remove a fold-in-flight UUID;
- `interrupt(cancel_queued:true)` also cancels fold-in-flight UUIDs while aborting the
  turn.

The abort and cancellation details are source-confirmed. A focused runtime race fixture
is still pending.

### Message-level system promotion

For text-only attachment content on a model with mid-conversation-system support, Claude
Code promotes the attachment before chair-sermon processing. The stable harness stays in
the top-level `system` field, while the new content becomes a later message-level
`role: "system"` item. Claude sends the beta
`mid-conversation-system-2026-04-07`. Only text-only attachment conversion qualifies.
Sonnet 5 retains a `<system-reminder>` wrapper inside the message-level system content;
other eligible models receive the unwrapped inner reminder text.

The outbound Claude order is:

```text
assistant tool_use
user tool_result
system steering reminder
```

The capability check considers HIPAA mode, the
`CLAUDE_CODE_FORCE_MID_CONVERSATION_SYSTEM` override, per-model feature metadata,
explicit older-family exclusions, model capability `mid_conv_system`, Mythos 5, and
eligible provider routes. If the endpoint rejects the role or beta, `midConvFallback`
normalizes again without promotion.

`tengu_chair_sermon` does not decide the promoted text-only path. On fallback paths,
false can already append all-text attachment content to a string-valued tool result.
With true, unwrapped attachment text is wrapped, mixed content can be offered to the
preceding tool result, adjacent user messages are merged, and a final pass moves remaining
top-level reminder text into the last tool result. A tool result containing
`tool_reference` refuses relocation. Error results discard non-text attachment blocks
before merging and retain the original error result if no text remains. Text
trimming/coalescing can change mixed-media placement.

### Direct CLIProxy translation and cache behavior

CLIProxyAPI `v7.2.116` preserves the important chronology:

- top-level Claude `system` becomes a Responses `developer` message;
- message-level Claude `role: "system"` becomes a Responses `user` reminder at the
  same history position;
- Claude `tool_result` becomes `function_call_output`.

The resulting promoted order is:

```text
assistant function_call
function_call_output
user steering reminder
```

The message-level-system helper adds one outer `<system-reminder>` wrapper without
checking whether the text is already fully wrapped, so nested wrappers remain a known
formatting case.

Source locations at the pinned revision:

- top-level developer conversion:
  `internal/translator/codex/claude/codex_claude_request.go:51-83`;
- message-level system conversion:
  `internal/translator/codex/claude/codex_claude_request.go:90-100`;
- tool-result conversion:
  `internal/translator/codex/claude/codex_claude_request.go:200-249`;
- reminder helper: `internal/translator/common/claude_system.go:15-27`;
- prompt-cache derivation:
  `internal/runtime/executor/helps/claude_code_session.go:96-105`;
- prompt-cache injection:
  `internal/runtime/executor/codex_executor_request.go:91-147`.

CLIProxy derives a deterministic prompt-cache key from model, Claude root session, and
agent. The key selects a performance namespace; it does not authorize reasoning replay
or continuation. Keeping the new reminder at the tail allows an unchanged earlier
request prefix to remain identical. Provider cache-hit amounts are runtime-dependent.

Controlled direct-route checks selected a later corrected value in 21/21 short trials.
In a 40-tool-cycle history, the four retained direct representations were acknowledged
in 12/12 trials, but the new fact appeared in the next tool arguments in 8/12. The small
synthetic sample establishes that receipt and next-action use are distinct observations;
it does not estimate a production miss rate.

Canonical evidence summaries:

- [`evidence/claude/midturn-steering-order.json`](evidence/claude/midturn-steering-order.json)
- [`evidence/cliproxy/direct-midturn-translation.json`](evidence/cliproxy/direct-midturn-translation.json)

Decision: preserve the two Claude system channels and the post-tool position through
direct translation. A future optional conversion may recognize the exact human-steering
scaffold and emit plain user text at the same position, but it must preserve adjacent
reminders and mixed text/image input.

## Dynamic request semantics without transcript drift

Several independently verified mechanisms change the final request while transcript,
session, agent, and model remain unchanged.

### ScheduleWakeup TTL prompt

The tool description has three model-visible variants:

| State | Description size | Local Count Tokens |
|---|---:|---:|
| Explicit 5-minute | 2,933 characters | 1,849 |
| Explicit 1-hour | 2,685 characters | 1,777 |
| Mixed/generic | 2,734 characters | 1,804 |

The retained JSON calls the field `description_bytes`, but the probe computed Python
`len(str)`: these are Unicode character counts, not UTF-8 byte lengths.

The selector always probes `eFe('repl_main_thread')` and `eFe('sdk')`; it does not
use the current query source directly.

`eFe` precedence is:

1. forced 5-minute mode;
2. forced 1-hour mode, including Bedrock override;
3. OAuth eligibility/overage fallback to 5-minute;
4. cached feature-flag allowlist.

The serialized schema is memoized by `qLo`. Policy changes do not necessarily alter
the model-visible description until the schema cache is cleared. The compatibility
input is therefore the final serialized description, not the raw policy variables.

ScheduleWakeup is conditionally deferred:

- ToolSearch disabled: included immediately;
- ToolSearch enabled and `tengu_kairos_loop_dynamic` true: forced non-deferred;
- ToolSearch enabled, flag false, undiscovered: absent;
- discovered through a tool reference: included on the next request.

Count Tokens includes the description whenever Claude supplied the materialized
tool. It does not invent a deferred tool absent from the request.

Captures: [`evidence/claude/schedule-wakeup-ttl-variants.json`](evidence/claude/schedule-wakeup-ttl-variants.json), [`evidence/claude/schedule-wakeup-token-counts.json`](evidence/claude/schedule-wakeup-token-counts.json).

Decision: hash the final loaded tool surface into **C** and use the same prepared
surface for admission, token counting, precompute compatibility, and execution.

### Custom-base classification

Source analysis and exact-function execution in the investigation (raw harness not
retained) resolved a loopback `ANTHROPIC_BASE_URL` as:

```text
Hn  = firstParty
Dc  = true
rm  = true
dj  = true
dGr = false
Yd  = false
```

This preserves Anthropic Messages transport but enables some Anthropic product and
capability assumptions. The effective request can change betas, context-management
fields, strict/deferred tool properties, output configuration, and speed without a
transcript change.

Do not globally redefine `Hn`, `rm`, or `dj`. Add a narrow host/route capability
predicate, conceptually:

```text
isDirectAnthropicAPI = Hn == firstParty AND dGr
isGenericCustomBase  = Hn == firstParty AND NOT dGr
```

Evaluate features individually after outbound capture. Do not permanently disable all
experimental betas; some are intentionally consumed by CLIProxy and context-hint
work depends on them.

Put final semantic effects into **C** and provider/base/route/account identity into
**U**.

## Rung 4: durable Claude history

A model-visible mutation is not committed until fresh-process reconstruction will
reproduce it.

### Ordinary replacement crash window

A 1.9 MiB Bash result was reduced to a 2,276-byte persisted-output wrapper and the
full artifact was written under the isolated session's `tool-results` directory.

Normal completion reconstructed the same wrapper after resume.

When the process was stopped shortly after the wrapped request reached the local
stub:

| Stop delay | Fresh-process result |
|---:|---|
| 0 ms | entire assistant/tool interaction absent |
| 10 ms | absent |
| 25 ms | absent |
| 50 ms | absent |
| 100 ms | wrapper reconstructed |
| 200 ms | wrapper reconstructed |
| 500 ms | wrapper reconstructed |

The per-delay raw captures were not retained. The surviving reproduction probe and
report-derived summary are
[`probes/claude/replacement_crash_window.py`](probes/claude/replacement_crash_window.py)
and
[`evidence/claude/replacement-crash-window-summary.json`](evidence/claude/replacement-crash-window-summary.json).

The earlier hypothesis that the original raw result would necessarily return was
not reproduced in this fixture. The executable result was different:

```text
model saw the wrapped turn
artifact existed
fresh-process transcript omitted the whole turn
```

### Replacement write order

The durable sequence must be:

```text
write uniquely identified artifact
verify actual size, digest, truncation state, and ownership
fsync artifact as required
append replacement ledger through an error-bearing durable operation
commit selected history state
update in-memory/UI/read state
only then expose reduced history to the model
```

If commit fails, either keep the original content or fail the request visibly.
Do not expose a history mutation to the model before fresh reconstruction can reproduce it.

### Transcript writer requirements

- write failures must return an error or preserve retry state rather than resolving dropped
  queue promises;
- background completion must wait for accepted transcript writes;
- first descendants after rewind are subject to the ordinary write window, not a
  separately reproduced successor-marker selection bug;
- in-place tombstone truncation must be replaced with append-only tombstones or a
  source-verified temp-file rewrite;
- duplicate-assistant recovery must require same lineage/content identity;
- a torn final JSONL line must not erase a rewind marker without an error and fall back to an
  older retained branch; recovery needs an explicit corruption/error state or a
  separately durable branch-selection record;
- Remote transcript hydration must use atomic replacement and verify immediately before writing that the local transcript has not changed.

The torn-line rewind-marker loss is source-confirmed; no focused runtime capture was
retained.

### Artifact requirements

Every persisted output needs a manifest covering:

- stable owner/session/fork semantics;
- content digest;
- actual stored size;
- declared original size;
- truncation state;
- location generation;
- availability and cleanup policy.

`EEXIST` without byte verification is not success. Absolute paths without relocation
or remote-transfer semantics are not durable ownership.

Source-confirmed Bash handoff behavior can truncate the saved artifact at 64 MiB while
the wrapper advertises the original output size. That concrete mismatch is why actual
stored size and truncation state are required manifest fields; advertised original
size alone is not integrity.

Forks must either copy artifacts, reference-count immutable shared artifacts, or
materialize an explicit portable object store. Parent deletion must not break child
history.

### Tombstone removal results

Both source-identified tombstone behaviors are now reproduced locally:

1. **Greater-than-50-MiB silent success.** A 53,069,996-byte transcript grew to
   53,071,918 bytes, the target UUID remained, and the process returned success. The
   unconditional fast path searched only the final 64 KiB; because the file exceeded
   50 MiB, the whole-file rewrite fallback was skipped, leaving the older target in
   place.
2. **Interrupted in-place fast path.** A 29,273-byte transcript was killed after
   truncate and before suffix rewrite. The resulting file was 9,020 bytes: the
   target disappeared, but so did the legitimate suffix. This fault-injection run
   used `PROBE_REMOVE_UUID` and an instrumented 10-second
   `PROBE_REMOVE_PAUSE_MS` delay to widen the truncate→rewrite window; it demonstrates
   the non-atomic consequence, not the natural production-window duration.

This supersedes the earlier source-only classification. The local fixtures reproduced
both suffix loss and a success result that left the target in place.

Decision: replace in-place mutation with an append-only tombstone interpreted by the
loader, or a source-generation-verified temporary rewrite with file and directory
sync. Removal must return an error if the target cannot be durably removed; transcript size
must not change that result.

Captures:

- [`evidence/claude/tombstone-removal.json`](evidence/claude/tombstone-removal.json)
- [`evidence/claude/tombstone-interruption.json`](evidence/claude/tombstone-interruption.json)

### Persisted-output ownership across forks

A CLI session fork copied a `<persisted-output>` wrapper verbatim into the child
transcript but created no child-owned artifact. Deleting the parent session directory
removed the file while the child transcript remained and still referenced the deleted
parent path.

The probe deliberately simulated the relevant behavior: the local stub authored the wrapper as
assistant text, the probe planted the parent-owned artifact, and Claude's genuine
persisted-output reduction/content-replacement pipeline did not run. The fork row-copy
and dangling-path behavior are executable-reproduced; fork handling of a genuine
replacement-ledger record remains unexercised.

Decision: fork semantics must be explicit. A fork must copy the immutable artifact,
reference-count a shared content-addressed object, or rewrite the wrapper to a
portable owner-independent object. Child history must remain valid after parent
deletion.

Capture: [`evidence/claude/persisted-output-fork-ownership.json`](evidence/claude/persisted-output-fork-ownership.json).

### Background completion and transcript writes

Source confirms the background-main controller launches initial and per-event
sidechain transcript writes without awaiting them, then marks the task complete or
failed and emits its notification without flushing the 100-ms-batched transcript
writer.

The local interactive fixture did not reach the feature-gated `Yxd` background-main
route. Therefore the missing completion barrier is source-confirmed, while the exact
runtime loss window remains unexercised.

Decision: task completion and terminal notification must be downstream of an
error-bearing transcript flush for the task's final semantic state.

### Compact controls

Executable controls confirmed that manual and automatic semantic compaction can:

- issue a summary request;
- persist a compact boundary and summary;
- use the summary in the next request;
- reconstruct the same summary after fresh-process resume.

Claude source also contains a separate physical transcript-GC path. Source resolves its
initializer to the `sdkUrl`/remote-session resume branch; ordinary local interactive and
print resumes did not activate it. The retained probes and source notes document that
historical behavior only.

The accepted Clodex architecture forbids physical JSONL GC. No implementation work or
runtime qualification depends on enabling this path, and no retention matrix is required
for the append-only design.

### Child references during physical GC

A child `fork-context-ref` lives in the child agent file and names only
`parentSessionId` plus `parentLastUuid`. Parent physical GC scans only the parent
JSONL and does not inspect child files. A pre-boundary parent UUID can therefore be
removed unless it independently belongs to a parent retention class. Fresh child
reconstruction then cannot resolve the UUID and returns an empty inherited prefix.

This is a source-confirmed consequence of the excluded physical-GC path. It is retained as
historical analysis and is not a pending implementation prerequisite.

### Remote transcript hydration (`CCR`)

`CCR` is the internal label used here for Claude Code's remote-session transcript
synchronization. Its exact acronym expansion has not been established. We did not
have an authenticated remote-session fixture, so the complete client/server behavior
was not tested end to end. The following client behavior is established from source
inspection:

- **Full hydration:** when delta hydration is disabled, remote resume replaces the
  local JSONL with the remote transcript. The client opens the transcript in
  truncating `"w"` mode instead of writing a complete temporary file and atomically
  replacing it. A failure after truncation can leave the local transcript incomplete.
- **Delta hydration:** when enabled, the client uses an accompanying metadata file
  recording the last known remote tip, together with checks against the end of the
  local transcript, to decide how to apply incoming remote events. It does not record
  a stable local-history generation or verify immediately before writing that the
  local transcript is still the version used for that decision. The client therefore
  does not guarantee preservation of concurrent local changes.
- **Local content replacements:** `content-replacement` records preserve substitutions
  such as `<persisted-output>` wrappers. The client does not include these records in
  normal remote transcript-event uploads or in the resynchronization projection.
  Consequently, the remote transcript cannot represent that local replacement state,
  and hydration through these paths cannot preserve it.

These are client-side, source-confirmed conclusions. The remaining runtime and
service-side unknowns are event ordering, deduplication, UUID stability and
idempotency, projection rules for records the service does receive, catch-up behavior,
and recovery from conflicts or interrupted synchronization. Those unknowns may reveal
additional behavior, but they cannot restore `content-replacement` information that
the client never sends.

## Rung 5: durable context hints and valid precompute

### 422 activation path

A route-aware local 422 remains a useful controlled activation mechanism:

```text
Claude request with context_hint
→ selected target is Codex
→ local 422 before upstream execution
→ Claude applies native context-hint replacement
→ retry without the hint
```

It is not a durability solution.

### Context-hint durability result

A forced fixture produced:

```text
initial raw request + context_hint:  402,116 bytes
retry with five wrappers:            253,221 bytes
fresh-process resume with raw data:  402,241 bytes
context hint offered again:          yes
```

The raw results also returned on the next turn in the same process after the prior
retry had used wrappers.

Captures: [`evidence/claude/context-hint-durability.json`](evidence/claude/context-hint-durability.json), [`evidence/claude/context-hint-precompute-stale.json`](evidence/claude/context-hint-precompute-stale.json).

Decision: do not enable production context-hint clearing until successful clears
are committed through the same durable replacement ledger as ordinary persisted
output and applied consistently to:

- foreground query state;
- UI;
- `readFileState`;
- background main sessions;
- SDK mutable history;
- resume, fork, and rewind;
- remote history.

### Precomputed compaction results

Executable checks reproduced:

1. Cross-model reuse:

   ```text
   precompute under gpt-5.6-luna
   switch to gpt-5.6-terra
   prompt-too-long recovery
   Terra consumes Luna summary
   ```

2. Same-UUID hint mismatch:

   ```text
   precompute raw tool-result history
   hint retry uses wrappers
   next turn restores raw results
   prompt-too-long recovery consumes pre-hint summary
   ```

Captures: [`evidence/claude/precompute-cross-model.json`](evidence/claude/precompute-cross-model.json), [`evidence/claude/context-hint-precompute-stale.json`](evidence/claude/context-hint-precompute-stale.json).

The cross-model conclusion is correct, but its retained summary does not independently
carry the request ordering that distinguishes the Luna precompute from a later Terra
compact request; that ordering survived only in an excluded raw request capture. Treat
this as an explicitly documented retention gap, not self-contained raw evidence.

Source and exact-function analysis also establish a conditional stale dynamic-tool
case: precompute under one serialized ScheduleWakeup description, clear the schema
cache through an auth transition, rematerialize a different description, and accept
the old summary by boundary UUID.

### Precompute validity rules

Precompute state must be keyed and validated against:

- session and agent;
- effective model;
- canonical semantic prefix **C**;
- trusted route/capability identity where relevant **U**;
- preserved-message UUID order and content digest;
- effective loaded tool/system schema;
- effective beta set;
- compact configuration;
- selected leaf/reachability generation.

At consumption, preserved UUIDs must be rebound to current messages. Boundary UUID
presence is necessary but radically insufficient.

Invalidate on:

- same-UUID content replacement;
- hint clear;
- model/config change;
- session switch or fork;
- rewind or relevant tombstone;
- remote transcript hydration;
- schema-cache transitions that alter the final tool surface;
- custom-base/provider capability changes.

### Pre-admission rescue and `Oe`

The old generic `Oe`/`snipTokensFreed` design is not used.

`Y0(messages)` uses the latest valid assistant API usage as a historical anchor and
estimates only the tail after it:

- post-anchor edits are already visible in `Y0(editedMessages)`;
- pre-anchor edits are hidden behind old API usage;
- with no anchor, edited content is fully visible and subtracting again double-counts.

The same scalar currently feeds both admission and prefix diagnostics, whose
correction semantics differ.

If same-turn pre-admission clearing remains necessary after durable hints are in
place, partition accounting explicitly:

```text
post-anchor savings:
    already reflected in edited Y0

pre-anchor savings:
    admission-only reconciliation

prefix diagnostics:
    recompute separately
```

Production 2.1.220 does not currently credit context-hint savings through this seam:
the production caller initializes `Oe` to zero. Do not mistake the presence of the
parameter for active savings reconciliation.

Do not revive one scalar as a shared correction.

## Rung 6: transport behavior and optional continuation

Normal Claude HTTP/SSE traffic currently does not use proxy-managed
`previous_response_id`:

- Claude→Codex translation reconstructs full `input`;
- HTTP and SSE Codex executors delete `previous_response_id`;
- no response-ID continuation store exists for those paths.

This is good. Continuation is a genuinely new feature and remains deferred.

### Executable late-frame crossover

A local retained-WebSocket fixture reproduced:

```text
request A completes
request B starts on retained socket
late terminal frame for A arrives
B receives A's terminal frame as its own
B terminates
the real B response can no longer be delivered
```

Decision: retained sockets require verified response/turn demultiplexing or must be
invalidated at every cancellation/terminal ambiguity before the request boundary is
released.

Capture: [`evidence/cliproxy/websocket-late-frame-crossover.json`](evidence/cliproxy/websocket-late-frame-crossover.json).

### Same-ID credential refresh retains a stale handshake

The earlier configured-key edit was inconclusive because that loader did not expose
the replacement token. Two focused executable controls resolved the endpoints of the
chain:

1. a watcher test rewrote `codex-account.json` from `token-one` to `token-two` and
   observed `AuthUpdateActionModify` with the same synthesized auth ID;
2. an executor test offered the retained socket the same auth ID and URL with a
   token-two Authorization header, then observed `ensureUpstreamConn` return the
   existing token-one connection without a second handshake.

Source at CLIProxy `v7.2.116` supplies the glue: modification replaces the auth object
but closes Codex sockets only on deletion, and the serving path re-offers the same auth
ID/URL. No single fixture executed file rewrite → service update → next request end to
end. The stale-handshake behavior is therefore established by two executable controls
plus source-confirmed composition, not one executable chain.

This supersedes the earlier broad "credential refresh inconclusive" statement while
preserving the narrower evidence classification above.

Decision: retained transport affinity must include a credential/handshake generation
or digest. Any semantically relevant auth modification must redial before another
request uses the socket.

Capture: [`evidence/cliproxy/credential-refresh-stale-handshake.log`](evidence/cliproxy/credential-refresh-stale-handshake.log).

### Unknown-outcome partial write is resent

A localhost WebSocket server accepted a 32-MiB request, read exactly 65,536 bytes from
the first frame, and reset the TCP connection. CLIProxy observed a write-side
`connection reset by peer`, invalidated the socket, opened a fresh connection, and
automatically sent the complete request again. The second connection returned
success.

Once request bytes have left the process, the client cannot infer that the upstream
did not buffer or execute them. Automatic resend can duplicate a generation or tool
turn.

This supersedes the earlier "unknown-outcome resend unexercised" statement. The
ambiguous resend is executable-reproduced.

Decision: after any partial or outcome-unknown send, return an explicit ambiguous
execution result unless the provider supplies a verified idempotency contract or
request acknowledgement that makes retry safe.

Capture: [`evidence/cliproxy/partial-write-full-resend.log`](evidence/cliproxy/partial-write-full-resend.log).

### Terminal responses and fresh `response.create`

CLIProxy currently treats Codex `response.incomplete` as successful terminal output in
HTTP/SSE paths while native Codex treats it as a stream failure. This is a product
policy conflict, not a harmless mapping detail. Before retry or continuation changes,
define whether incomplete output is a failed turn, an explicit partial result, or a
recoverable state; it does not implicitly seed completed-turn replay or continuation.

In downstream Responses-WebSocket HTTP/fallback mode, a fresh `response.create`
without `previous_response_id` can inherit merged prior input/output and pending tool
state. Decision: fresh create is a reset. History is inherited only through an explicit
continuation contract.

### When continuation may be reused

Continuation does not become a second source of semantic history. Claude continues to send
the complete source request. CLIProxy computes **C** over that virtual full request,
compares it to a disposable cache of the immediately preceding full request and
provider output items, and may send only the strict suffix plus `previous_response_id`.
The cache is transport state, can be discarded at any time, and is not used to
reconstruct Claude history. Any mismatch sends the full request and replaces the
cache.

Before enabling `previous_response_id` or equivalent stateful continuation:

- **C** must match;
- **U** must match;
- **E** must match and advance monotonically;
- no unresolved pending tool turn may exist;
- cancellation, clear, compact, rewind, fork, hydration, replacement, model/tool
  change, credential change, ambiguous send, and transport loss must reset **E**;
- a later byte-identical historical prefix must not resurrect an old **E**.

## Request translation and content handling

### Tool-call IDs

Claude-visible IDs derived by replacing unsupported characters are non-injective:

```text
a.b → a_b
a/b → a_b
```

Maintain a per-turn injective map from provider IDs to Claude-visible IDs and reverse
it on tool-result translation. The original is not inferred from a lossy alias.

### Unsupported content

Claude→non-Claude translators have dropped or degraded context-bearing content without
fields, including some documents, unsupported block types, cache-control metadata,
and context controls.

Decision: every unsupported semantic field must have one policy:

- verified mapping;
- typed rejection;
- explicit visible degradation marker.

Silent deletion is not a policy.

### Context-management behavior in Claude Code 2.1.220

The production Claude query path emits only:

```json
{"edits":[{"type":"clear_thinking_20251015","keep":"all"}]}
```

No production emitter was found for `clear_tool_uses_20250919` or
`compact_20260112`. The emitted `keep:"all"` form is non-shrinking from the client's
perspective; any server-side checkpoint/cache semantics remain provider-dependent.

Current CLIProxy behavior is asymmetric:

- all five Claude→non-Claude translators drop `context_management` and
  `context_hint` unless another layer re-adds them;
- cloaked direct-Anthropic paths can synthetically inject the no-op
  `clear_thinking keep:"all"` edit;
- payload rules can re-add fields after translation;
- replay can then inject opaque state after the edit was dropped or changed.

Decision: context-control policy is source+target aware and enforced **after payload
rules, immediately before replay and egress**. Native Claude targets preserve supported
fields. Unsupported targets must materialize a supported edit with explicit change
metadata or reject it; no-op forms must not invalidate replay merely because the field
is present. Unknown edit versions are rejected explicitly. This is separate from `context_hint`,
which is a client retry/local-clear protocol.

### Choosing the normalized request as the working request

CLIProxy currently retains both original and current request forms, and different
subsystems consult different ones for session identity, prompt cache, replay, tool
mapping, and response translation.

Decision: one post-normalization source-derived request is the working request. The
original is provenance only.

## Count Tokens and planning

Count Tokens and execution must share a pure count-safe preparation phase for all
source-derived semantic material.

Confirmed distinctions:

- native Responses string input can count as zero in the affected Codex estimator;
- Claude-shaped `hello` through the local count endpoint counted as one;
- supplied dynamic ScheduleWakeup descriptions change the token count;
- Count Tokens includes a materialized description when Claude supplied it;
- it does not independently load a deferred tool absent from the request;
- execution can later add replay and other proxy-derived state that must be handled
  explicitly rather than accidentally omitted.

The count contract must state whether proxy-derived replay is included, excluded, or
reported separately. It must be monotonic for source input and aligned closely enough
that Claude does not compact after the real route has already exceeded its limit.

## Model registry and route metadata

Current CLIProxy Claude `/v1/models` emits:

- `id`;
- `display_name`;
- `max_input_tokens`;
- `max_tokens`.

The prior claim that it emits IDs only is obsolete.

The metadata is not automatically the source of truth:

- the global registry is last-writer-wins when multiple routes register one ID;
- missing values can be filled with synthetic defaults;
- execution selects provider/auth route later;
- non-Claude IDs are cloaked by default in the Claude model list.

Decision: advertise route-pinned metadata or a conservative intersection/minimum
across every selectable route. A random registration must not determine client
admission policy.

## Model-capabilities switch (`uZc`)

The shipped predicate is folded to false. A structural one-byte patcher exists:

- `../util/patch_model_capabilities.py`
- `../util/test_patch_model_capabilities.py`

Its focused tests passed 24/24 and the pinned in-memory edit was exactly one
`1 → 0` change.

Architecture decision: keep this outside the immediate ladder.

Reasons:

- `max_input_tokens` has no production consumer in 2.1.220;
- discovered `max_tokens` affects only the output upper cap;
- current 128k discovery equals the built-in upper cap;
- it does not repair the 32k default telemetry;
- default model-list cloaking prevents bare GPT IDs from matching discovered IDs;
- the cache is not keyed by base/provider/route/catalog identity within one config
  lane;
- it is not a full catalog or capability system.

If pursued for lower or route-specific output caps, first validate:

- uncloaked or otherwise matching IDs;
- exact call-graph guards in predicate, lookup, and refresh;
- local `/v1/models` fetch and cache behavior;
- no unintended host traffic;
- source-identity cache invalidation before first request.

## Cache taxonomy

Do not solve every cache with one vague "profile generation."

### Claude `model-capabilities.json`

Partially isolated by `CLAUDE_CONFIG_DIR`, but within one lane it is not keyed by:

- base URL;
- provider;
- route;
- catalog source;
- auth identity.

Refresh is fire-and-forget, so stale data can be consumed before refresh.

### CLIProxy prompt cache

A performance namespace, not semantic authorization. It must be caller-namespaced
but cannot stand in for **C**, **U**, or **E**.

### CLIProxy reasoning replay

Semantic derived state requiring exact **C**, compatible **U**, and generation-aware
mutation.

Each cache needs a source identity appropriate to its semantics.

## Compaction taxonomy

Claude Code 2.1.220 has separate mechanisms:

### Legacy full-collapse compaction

Produces a compact boundary plus summary and no preserved raw tail. Used by default
auto-window routes and some teammate/fallback paths.

### Reactive group-preserving compaction

Summarizes a prefix and preserves recent groups and attachments. Used by manual
`/compact`, prompt-too-long recovery, and configured/non-auto routes when eligible.

### Precomputed compaction

A cached future reactive result, optionally persisted in `.precompact.json`. It is
not a third transcript format. It becomes semantic only when consumed.

### Physical transcript GC — source-observed and excluded

Claude source can rewrite JSONL storage after semantic compaction in a particular
`sdkUrl`/remote-session initialization path. The 6,003,800-byte ordinary local fixture
(about 5.73 MiB) did not activate that state. This remains a historical source and negative
runtime finding.

The accepted architecture does not use this mechanism. JSONL remains append-only, so no
physical-GC runtime matrix is pending for implementation. Tests and evidence classification
must still distinguish this excluded source path from semantic compaction.

## Fork, sidechain, background, and remote behavior

"Fork" is not one operation:

- UI/SDK session forks create new session IDs;
- agent-tool and worker forks retain the root session and change agent identity;
- raw/SDK fork paths and canonical UI fork paths have different reconstruction
  behavior.

Agent identity must be explicit. Missing agent headers must not collapse a
known subagent request to `main`.

Background shell, background agent, and agent fork have different transcript,
output, parent-anchor, and completion barriers. Test them separately.

Remote full and delta hydration need explicit synchronization rules:

- atomic replacement;
- local version check immediately before writing;
- replacement and tombstone propagation;
- sequence deduplication;
- queued-event preservation across reconnect/epoch rebuild;
- defined artifact transfer semantics.

Remote service ordering, UUID immutability, and catch-up behavior remain
runtime-dependent.

## Designs excluded from the current plan

The current plan excludes:

- hidden Codex `/responses/compact` behind Claude;
- a proxy-owned semantic transcript;
- generic proxy deletion of Claude tool results;
- a global zero output reservation;
- fake 392k capacity;
- one-expression `/context` semantic replacement;
- a generic nonzero `Oe` correction;
- continuation before replay and transport work;
- permanent blanket beta disabling;
- a broad first-party spoof;
- a stale-source Claude rebuild;
- model suffixes as the implicit Claude effort-control contract;
- `uZc` as a supposed 372k input-window solution;
- a unified external catalog inside the immediate context patch.

## Patching and distribution strategy

Do not rebuild from `~/claude-code`. That source is orientation, not a reproducible
2.1.220 build.

Use the official/native binary and the shared target-aware patch engine through the
CLI container codec:

1. acquire or identify the pinned binary;
2. verify version and source hash;
3. extract the entrypoint once with `bun_handler.py`;
4. discover and verify every requested structural anchor;
5. classify clean, fully patched, stale, and partial states;
6. apply all edits in memory;
7. report overlap and mixed states as errors;
8. repack once;
9. re-extract and require exact intended JS;
10. execute temporary output against deterministic local fixtures;
11. install atomically only after all checks pass.

For multi-site changes:

- require exact candidate and patch counts;
- use versioned structural markers;
- fail on partial application;
- obtain pristine input independently of the currently installed patched binary;
- keep the bytecode untouched;
- do not use `node --check` on this bundle—Node 22 cannot parse the newer `using`
  syntax; use exact extraction/diff plus real binary execution.

This project should eventually plug into the repository's shared linker/codec
architecture documented in `../docs/architecture.md`, rather than grow a second CLI
patch framework.

## Verification model

### Structural checks

- every anchor matches exactly once or the expected explicit count;
- clean and patched states are both recognized;
- partial or stale states return an explicit error;
- source edit overlap returns an error;
- repack is deterministic;
- no-op is byte-identical;
- re-extracted JS equals the intended text;
- the temporary binary executes.

### Profile and admission checks

For Sol, Terra, and Luna:

- bare IDs;
- 372k input policy;
- 128k request output ceiling;
- explicit `CLAUDE_CODE_AUTO_COMPACT_WINDOW` behavior;
- no GPT-5.5 fallback in the same profile;
- `vSe` and `Fny` exact-model behavior;
- reactive/auto-window admission-bypass and prompt-too-long recovery controls;
- direct Claude-native effort and fast/priority values survive translation as intended;
- ordinary Claude/shared-window controls retain existing behavior.

### Observability checks

- last API input and projected next request are separately labeled;
- raw, auto, and hard limits are separate;
- categories are explicitly estimated;
- output default and configured/hard limits are distinct;
- request max and telemetry follow the declared contract.

### Replay checks

- unanchored marked replay is skipped;
- target-model changes alter **C**;
- changed instructions/tools/betas/effort/output mode alter **C**;
- assistant segmentation cannot collide;
- tool IDs map injectively;
- valid client reasoning suppresses incompatible cached reasoning;
- callers are isolated by default;
- incompatible provider/endpoint/account routes do not share opaque replay;
- old requests do not delete newer replay generations.

### Mid-turn steering checks

- queue folding occurs only after tool completion and `PostToolBatch` hooks;
- `now`/`next` selection, main/subagent targeting, slash filtering, and attachment-mode
  filtering are tested separately;
- transcript order remains tool call, tool result, then queued attachment;
- top-level system becomes developer while a post-tool message-level system remains
  after `function_call_output`;
- multiple later system items keep their relative order;
- text-plus-image queued input remains one user item with both parts;
- already wrapped message-level system text does not acquire unintended nested wrappers;
- changing only trailing steering does not rewrite the earlier translated prefix;
- abort between attachment emission and queue removal has an explicit
  duplicate-delivery result;
- `cancel_async_message` and `interrupt(cancel_queued:true)` retain their distinct
  fold-in-flight behavior;
- input arriving during final prose or Stop hooks is reproduced as a later turn, or a
  future follow-up rule starts another sample.

### Durability checks

- model-visible replacements survive fresh-process reconstruction;
- replacement failures are error-bearing;
- background completion waits for transcript acceptance;
- rewind controls distinguish ordinary write-window loss from branch-selection bugs;
- tombstone fault injection yields an old or new complete state without suffix loss;
- greater-than-50-MiB removal reports whether the target was actually removed;
- forks retain valid artifact ownership;
- remote transcript replacement is atomic and generation-checked;
- sidechain initialization is recoverable or atomic.

### Context-hint and precompute checks

- a 422 retry produces a durable replacement, not retry-local wrappers;
- next turn and fresh resume use the same effective history;
- precompute rejects cross-model reuse;
- precompute rejects same-UUID content changes;
- dynamic tool/system/beta changes invalidate compatibility;
- `clear_thinking keep:"all"` is recognized as a no-op unless the target reports an actual change;
- unsupported/unknown context-management edits are preserved, materialized, or rejected explicitly after payload rules;
- preserved messages rebind to current content and order;
- no generic `Oe` double-counting across no-anchor, pre-anchor, post-anchor, and mixed
  fixtures.

### Transport checks

- late frames stay with the request that produced them;
- cancellation closes or demultiplexes retained transport;
- same-ID auth modification with changed token/headers forces a new handshake;
- the 32-MiB/65,536-byte partial-write regression returns an explicit ambiguous
  outcome and does not resend automatically;
- ambiguous writes are not transparently resent without verified idempotency;
- fresh `response.create` resets unless explicit continuation is present;
- `response.incomplete` follows one explicit partial/failure policy and does not advance completed continuation implicitly;
- HTTP JSON, HTTP SSE, and Responses WebSocket normalize to the same semantic request/error/usage trace;
- `previous_response_id` is disabled until **C/U/E** checks are green.

### Independent comparison checks

- **Offline catalog:** the Codex baked fallback and remote catalog both report the
  intended active window; a successful remote refresh cannot hide a stale 272k baked
  fallback.
- **Codex math:** expose and verify raw context, 90% automatic threshold, and 95%
  effective limit as distinct values (372k → 334,800 / 353,400 in the pinned source).
- **Tri-transport equivalence:** HTTP JSON, HTTP SSE, and Responses WebSocket produce
  the same normalized semantics before transport-specific framing.

## Executable evidence inventory

The durable corpus now lives inside this project:

- [`reports/workflow12-local-state-checks.md`](reports/workflow12-local-state-checks.md)
- [`reports/workflow12-outstanding5-local-checks.md`](reports/workflow12-outstanding5-local-checks.md)
- [`reports/founding-experiments-and-evidence-gaps.md`](reports/founding-experiments-and-evidence-gaps.md)
- [`reports/corpus-absorption-checklist.md`](reports/corpus-absorption-checklist.md)
- [`reports/midturn-steering-direct-path.md`](reports/midturn-steering-direct-path.md)
- [`evidence/claude/midturn-steering-order.json`](evidence/claude/midturn-steering-order.json)
- [`evidence/cliproxy/direct-midturn-translation.json`](evidence/cliproxy/direct-midturn-translation.json)
- [`evidence/provenance/source-pins.json`](evidence/provenance/source-pins.json)
- [`evidence/provenance/instrumented-binary-hashes.txt`](evidence/provenance/instrumented-binary-hashes.txt)
- [`evidence/claude/`](evidence/claude/)
- [`evidence/cliproxy/`](evidence/cliproxy/)
- [`probes/claude/`](probes/claude/)
- [`probes/cliproxy/`](probes/cliproxy/)
- [`fixtures/cliproxy-claude-models.json`](fixtures/cliproxy-claude-models.json)

[`ARTIFACTS.md`](ARTIFACTS.md) records per-artifact provenance, evidence class,
interpretation, exclusions, and reproduction notes. [`SHA256SUMS`](SHA256SUMS)
provides integrity hashes for the consolidated corpus.

Giant redundant raw request logs, temporary patched binaries, config/session roots,
worktrees, and credentials were deliberately excluded. The retained probes write
new run output to `/tmp` so reproduction does not dirty this directory.

## Remaining focused executable work

Still unexercised or runtime-incomplete:

- the abort window between queued-attachment emission and queue removal, including
  duplicate delivery after a retained queue entry;
- focused runtime coverage for `cancel_async_message` versus
  `interrupt(cancel_queued:true)` while a UUID is fold-in-flight;
- final-prose and Stop-hook late input, plus any future automatic follow-up rule;
- mixed text/image queued input and the already-wrapped message-level-system case on the
  direct translator;
- the producer, atomic commit point, request representation, and CLIProxy verification
  contract for **D/R/A/S** generations;
- provider-side cancellation acknowledgement and response-ID demultiplexing;
- the feature-gated `Yxd` background-main completion window in a real runtime path;
- remote full/delta hydration against an authenticated or faithful local service fixture;
- SDK/remote-session behavior when local replacements, tombstones, artifacts, and queued events
  cross hydration or resync boundaries;
- a genuine replacement-ledger/content-replacement record crossing a CLI session fork;
- an end-to-end auth-file rewrite → service update → next retained-WebSocket request
  credential-refresh fixture;
- torn-final-line rewind-marker recovery in a focused runtime fixture;
- a verbose rerun of the six replay/count probes with non-degeneracy assertions;
- the conditional stale-dynamic-tool precompute case after a schema-cache-clearing
  OAuth/provider transition;
- explicit product policy and cross-transport tests for `response.incomplete`;

No longer open as broad discovery questions:

- busy-session queue snapshot timing and post-tool attachment order;
- text-only mid-conversation-system promotion and chair-sermon precedence;
- direct top-level/message-level system translation and prompt-cache-key derivation;
- `/clear` custom-header identity split;
- unanchored and semantically incompatible replay;
- client/cached reasoning mixing;
- dynamic ScheduleWakeup description drift;
- non-durable context-hint clearing;
- same-UUID and cross-model stale precompute;
- late WebSocket-frame crossover;
- ordinary replacement crash window;
- manual and automatic compact reconstruction;
- unknown-outcome WebSocket partial-write resend;
- same-ID credential refresh retaining a stale WebSocket handshake;
- tombstone interruption and greater-than-50-MiB silent success;
- persisted-output ownership failure across a CLI session fork.

The previously proposed rewind-specific successor-marker issue was not reproduced and is
withdrawn. Only the ordinary early transcript-write window remains supported.

## Remaining analysis workflows

After the focused executable work:

1. **Cross-protocol workflow**
   - connect Claude effective-history mutations to CLIProxy replay, route, cache, and
     transport state;
   - use the executable findings rather than repeat broad source discovery;
   - analyze raw independent reports in the parent before proceeding.

2. **Static policy workflow**
   - run after the cross-protocol result;
   - finalize capability, cache, custom-base, model-routing, maintenance, and patch
     policy;
   - keep source-confirmed and provider-dependent claims separate.

A wholesale rerun of the Claude durability workflow is unnecessary.

## Immediate implementation scope

The first implementation batch may include only Rungs 0–2:

```text
coherent profile
+ exact-model input-only admission
+ additive observability/telemetry contract
```

Rung 3 is the next implementation dependency, not optional cleanup. Rung 4 must be
complete before Rung 5. Rung 6 remains optional.

Separate projects are not part of the first implementation batch:

- model-capability discovery;
- custom-base product and telemetry controls;
- unified external catalog;
- provider-contract experiments.

## Recorded decisions

| ID | Decision | Status |
|---|---|---|
| D001 | Claude Code remains responsible for semantic history | Accepted |
| D002 | CLIProxy-derived state must match C/U and, for continuation, E | Accepted |
| D003 | The Claude route does not use hidden Codex compaction without a shared history protocol | Accepted |
| D004 | Exact GPT-5.6 models use independent input/output admission | Accepted |
| D005 | Patch both `vSe` and `Fny` | Accepted |
| D006 | `/context` changes are additive, not scalar redefinition | Accepted |
| D007 | Bare model IDs are the default Claude-facing identity | Accepted |
| D008 | Claude-native effort and fast/priority settings are verified at the final direct CLIProxy request | Accepted |
| D009 | Unanchored marked reasoning does not replay | Accepted |
| D010 | Replay hashes the final materialized semantic request | Accepted |
| D011 | Valid client reasoning wins over incompatible cache | Accepted |
| D012 | Tool-call ID mapping is injective and reversible | Accepted |
| D013 | Prompt-cache identity is not a source of semantic continuity | Accepted |
| D014 | Model-visible history mutation requires a durable commit | Accepted |
| D015 | Hint clears use the durable replacement ledger | Accepted |
| D016 | Boundary UUID alone is insufficient to validate precompute | Accepted |
| D017 | The plan does not use generic shared `Oe` accounting | Accepted |
| D018 | `previous_response_id` remains disabled until replay and transport checks pass | Accepted |
| D019 | Outcome-unknown partial writes are not retried without verified idempotency | Accepted; reproduced locally |
| D020 | Model metadata must be route-pinned or conservative | Accepted |
| D021 | `uZc` is separate output-cap discovery work, not an input-window fix | Accepted |
| D022 | Blanket beta disablement is diagnostic only | Accepted |
| D023 | No rebuild from stale Claude source; patch native bundle structurally | Accepted |
| D024 | Claude JSONL is authoritative and append-only; semantic compaction appends a boundary and root, while physical transcript GC is excluded | Accepted |
| D025 | The separate rewind successor-marker issue was not reproduced; the ordinary write window remains | Accepted |
| D026 | Retained WebSockets are bound to credential and handshake generation, not auth ID plus URL alone | Accepted; executable controls plus source-confirmed composition |
| D027 | Reserved: ambiguous-write policy merged into D019 | Superseded by D019 |
| D028 | Tombstone removal uses crash-safe mutation and reports whether the target was removed regardless of transcript size | Accepted; locally reproduced with an instrumented interruption window |
| D029 | Persisted-output artifacts require fork-safe ownership independent of the parent session directory | Accepted; simulated-wrapper fork behavior reproduced |
| D030 | Background terminal notification follows an error-bearing transcript flush | Accepted; source-confirmed |
| D031 | Reserved: physical-GC runtime validation is excluded by the append-only JSONL decision | Superseded by D024 |
| D032 | Reserved: physical-GC child-retention work is excluded by the append-only JSONL decision | Superseded by D024 |
| D033 | Remote full/delta hydration requires atomic writes, a pre-write local-version check, and replacement/artifact propagation | Accepted; runtime fixture pending |
| D034 | Target model identity is mandatory in semantic replay compatibility C | Accepted; corpus correction |
| D035 | Context-management policy is enforced after payload rules and before replay/egress | Accepted; source-confirmed |
| D036 | Replay has a bounded age and includes provider/model revision where observable | Accepted; provider contract pending |
| D037 | `response.incomplete` requires an explicit partial/failure policy before continuation work | Accepted; source-confirmed conflict |
| D038 | Continuation validates a virtual full request; the retained prefix is disposable transport state, not semantic ownership | Accepted |
| D039 | Initial input-only rollout keeps precompute/context hints disabled and uses conservative operational thresholds until durability prerequisites are implemented | Accepted |
| D040 | The runtime request path is direct Claude Code → CLIProxyAPI; the service wrapper manages startup, health, and environment without rewriting request bodies | Accepted |
| D041 | Mid-turn human input preserves post-tool chronology and the two Claude system channels through direct translation | Accepted; source-confirmed plus localhost capture |
| D042 | Late input that misses the final tool snapshot requires Claude-side follow-up behavior; proxy translation cannot alter a completed sample | Accepted; focused runtime follow-up test pending |

## Runtime and provider questions still open

Local source cannot establish:

- portability of Codex encrypted reasoning across accounts, endpoints, model
  revisions, prompt-cache identities, or independently executed identical histories;
- a trustworthy provider/model revision identity and the maximum safe replay age when
  revisions are silent;
- provider behavior when two distinct valid encrypted reasoning items are supplied;
- provider prompt-cache isolation and content validation;
- real aggregate input/output boundary behavior near 372k/128k;
- response-ID cancellation and idempotency semantics required by future continuation;
- active production feature flags for every account/session;
- remote service ordering, UUID immutability, event projection, and catch-up repair;
- actual `Yxd` background-main activation and the measured notification-versus-flush
  window;
- SDK/remote-session pruning and hydration behavior when local replacement, tombstone, artifact,
  and queued-event state disagree;
- filesystem power-loss guarantees beyond visible fsync/rename calls.

When required compatibility information is absent, the default is to skip reuse. Provider-verified
capabilities can later widen compatibility deliberately.

## Current work queue

```text
focused steering abort/cancellation/final-response checks
    ↓
focused SDK/remote-session/Yxd runtime checks
    ↓
cross-protocol workflow
    ↓
static policy workflow
    ↓
Rungs 0–2 implementation
    ↓
Rung 3 proxy replay compatibility
    ↓
Rung 4 durable Claude history
    ↓
Rung 5 durable context management
    ↓
Rung 6 optional continuation
```

This queue is the current status snapshot, not timeless architecture. Update this
section as work lands; preserve the decisions and documented requirements above unless new
evidence explicitly supersedes them.
