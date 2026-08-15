# Automated permission reviewers through CLIProxyAPI

Date: 2026-08-11

This document records how Claude Code auto mode and native Codex Guardian review proposed
actions, what CLIProxyAPI already translates on the direct route, and where permission
responsibility should remain.

The current route is:

```text
Claude Code → CLIProxyAPI → OpenAI Responses
```

Request-rewriting middleware that is not part of this route is outside this document.

## Evidence and versions

The findings use:

- Claude Code 2.1.220 authoritative extracted bundle:
  `/tmp/claude-2.1.220.js`, SHA-256
  `e32e7ead0b8ec4815fb69806bfad0116bdb9b51bba927fbd172f5a4a2903ce6e`;
- readable but stale Claude Code reconstruction under `/home/juraj/claude-code`;
- CLIProxyAPI source under `/tmp/cliproxyapi-source` at
  `a88197f845c979132c8978ea223c6af05cc81536`, tag `v7.2.116`;
- native Codex source under `/tmp/codex-source` at
  `d75f94a94d5cb0bbabc59b86c0427c7ad09a9d6d`;
- direct localhost cache experiments through CLIProxyAPI on port 8317, with no other
  request middleware in the route.

The stale Claude source provides readable names and comments. Claims about Claude Code
2.1.220 were checked against the extracted bundle. The bundle contains the active
permission-classifier path at `/tmp/claude-2.1.220.js:19769` and the auto-mode description
at `/tmp/claude-2.1.220.js:21723`.

Evidence classes in this document are:

- **Source-confirmed:** Claude auto-mode flow, Guardian flow, CLIProxyAPI field
  translation, cache-key construction, and usage conversion.
- **Executable-reproduced:** direct OpenAI prefix-cache reuse and Claude
  `5m`/`1h` translation equivalence through port 8317.
- **Not retained as an executable capture:** a complete live Claude auto-mode allow or
  deny request through the direct route. Its translation claims are source-confirmed.

The direct cache experiment's raw request history was not added to the durable corpus; the
aggregate values and limitation are recorded here.

## Main conclusion

Claude Code auto mode and Codex Guardian use the same basic design:

```text
acting model proposes an action
→ ordinary permission checks run
→ action would otherwise require approval
→ separate model request reviews the action and relevant context
→ reviewer returns allow or deny
→ the original permission layer executes or blocks the action
```

Neither design lets the acting model approve its own tool call merely by saying that it is
safe. A second model invocation returns a decision to the client-side permission layer.

There is no public OpenAI Guardian request type involved. Native Codex implements Guardian
inside its client. Claude Code implements auto mode inside its client. In the direct route,
CLIProxyAPI translates Claude's reviewer request as an ordinary OpenAI Responses request.
The current translator preserves the core messages, tools, forced tool choice, and result,
but it does not preserve every classifier control field; those omissions are listed below.

## Claude Code auto mode

### Permission-layer position

Claude Code first runs its normal permission logic. Auto mode handles an action only when
that logic returns `ask` and the active permission mode is `auto` (or plan mode is executing
with auto mode active).

Readable locations:

- permission entrypoint and initial decision:
  `/home/juraj/claude-code/src/utils/permissions/permissions.ts:473-525`;
- classifier invocation:
  `permissions.ts:688-702`;
- final classifier allow/deny conversion:
  `permissions.ts:818-926`;
- result delivery through `canUseTool`:
  `/home/juraj/claude-code/src/hooks/useCanUseTool.tsx:32-92`.

The user-facing description is explicit:

> Auto mode lets Claude handle permission prompts automatically — Claude checks each tool
> call for risky actions and prompt injection before executing. Actions Claude identifies
> as safe are executed, while actions Claude identifies as risky are blocked.

Source: `/home/juraj/claude-code/src/components/AutoModeOptInDialog.tsx:9-10`.

### Checks before the model reviewer

Auto mode does not send every action to the classifier. Before the classifier request,
Claude Code can:

- preserve safety checks that are not marked classifier-approvable as human-only;
- preserve tools that explicitly require user interaction as human-only;
- allow actions that the `acceptEdits` permission mode would already allow;
- allow tools on the auto-mode safe list, including read, search, planning, task metadata,
  and coordination tools;
- keep PowerShell out of the classifier unless its dedicated build feature is enabled;
- apply organization, MCP, and plan-mode permission ceilings;
- use an earlier definitive allow or deny rule without invoking the classifier.

Readable locations:

- exclusions and `acceptEdits` fast path: `permissions.ts:526-656`;
- safe-tool fast path: `permissions.ts:658-686`;
- safe-tool list:
  `/home/juraj/claude-code/src/utils/permissions/classifierDecision.ts:50-98`.

These checks remain Claude Code's responsibility. CLIProxyAPI receives only the reviewer
requests that Claude Code has chosen to make.

### Reviewer input

`classifyYoloAction` builds a separate model request containing:

- the auto-mode security system prompt;
- the selected permission and environment rules;
- cached `CLAUDE.md` content when present;
- a compact projection of user text;
- prior assistant tool calls;
- queued human messages converted to user transcript entries;
- the exact proposed tool name and input;
- the classifier output instruction.

It deliberately omits ordinary assistant prose while retaining user intent and tool
history. The exact projection is chosen by Claude Code, not by CLIProxyAPI.

Readable locations:

- transcript projection and queued-command handling:
  `/home/juraj/claude-code/src/utils/permissions/yoloClassifier.ts:287-320`;
- request construction: `yoloClassifier.ts:1012-1160`.

### Reviewer output

One active format forces a tool call named `classify_result`:

```json
{
  "thinking": "brief reasoning",
  "shouldBlock": false,
  "reason": "brief explanation"
}
```

The schema is defined at `yoloClassifier.ts:252-285`.

The two-stage XML format asks for ordinary text:

```xml
<block>no</block>
```

or:

```xml
<block>yes</block><reason>one short sentence</reason>
```

The fast first stage can allow immediately. A non-allow result can be reviewed by a larger
second-stage request. Both requests contain the same transcript and action prefix.

Source: `yoloClassifier.ts:695-940`.

### Decision and failure behavior

Claude Code converts:

```text
shouldBlock = false → permission allow
shouldBlock = true  → permission deny
```

Unparseable classifier output blocks the action. The authoritative 2.1.220 bundle also
denies when the classifier is unavailable and reports:

```text
Auto mode classifier unavailable, denying with retry guidance (fail closed)
```

If the classifier transcript exceeds its context window, an interactive session can return
to ordinary human approval; a headless session aborts rather than repeatedly making the
same impossible review request. Claude Code also tracks denials and can return repeated
blocks to human review when prompting is available.

These behaviors are applied after the reviewer response. They should not be recreated in
CLIProxyAPI.

## Native Codex Guardian

Guardian is Codex's automated approval reviewer. It runs when the approval policy is
`on-request` or `granular` and `approvals_reviewer` is `auto_review`. The legacy value
`guardian_subagent` maps to the same setting.

Source: `/tmp/codex-source/codex-rs/core/src/guardian/review.rs:170-194`.

Guardian receives:

- a compact transcript preserving user intent and recent assistant/tool context;
- the exact proposed action;
- a risk policy;
- a required structured response.

Its response requires `outcome`. The other fields are optional and receive defaults when
omitted:

```json
{
  "risk_level": "low | medium | high | critical",
  "user_authorization": "unknown | low | medium | high",
  "outcome": "allow | deny",
  "rationale": "..."
}
```

Guardian fails closed on timeout, execution failure, or malformed output. Its review timeout
is 90 seconds.

Source: `/tmp/codex-source/codex-rs/core/src/guardian/mod.rs:1-68`.

### Guardian's reusable review session

Codex runs Guardian in a read-only session with `approval_policy = never` and disables
features that could let the reviewer mutate state or request another approval. It may reuse
the parent's managed network allowlist for read-only checks.

Sequential reviews normally append to one reusable trunk session. If the trunk is already
busy, Codex creates an ephemeral fork from the last committed trunk state. The trunk is
recreated when settings that affect reviewer behavior change.

Source: `/tmp/codex-source/codex-rs/core/src/guardian/review.rs:815-828` and
`review_session.rs:97-207`.

This session manager improves prompt reuse, parallel review behavior, and accumulated review
context. It is not required to make one allow/deny decision.

### Guardian prompt-cache identity

Ordinary native Codex root and child agents share the root session ID as their
`prompt_cache_key`; their `thread-id` values differ. The test at
`/tmp/codex-source/codex-rs/core/tests/suite/prompt_cache_key.rs:40-156` asserts that
behavior.

Guardian review sessions are a deliberate exception. They can use:

```text
guardian:<parent-thread-id>
```

Source: `/tmp/codex-source/codex-rs/core/src/guardian/review_session.rs:211-223`.

That isolates the specialized reviewer conversation from the parent's ordinary prompt-cache
routing while preserving reuse for reviews associated with one parent thread.

## Direct translation through CLIProxyAPI

### Existing request path

Claude auto mode requires only ordinary protocol translation. The existing translator
implements the core message and tool path:

```text
Claude acting model
    │ proposes tool call
    ▼
Claude permission checks
    │ result = ask, mode = auto
    ▼
Claude auto-mode reviewer request
    │ system policy
    │ projected transcript
    │ exact proposed action
    │ forced classify_result tool or XML instruction
    ▼
CLIProxyAPI Claude→Codex translator
    │ developer/user messages
    │ OpenAI function schema and tool choice
    ▼
OpenAI Responses model
    │ function_call or XML text
    ▼
CLIProxyAPI Codex→Claude translator
    │ Anthropic tool_use or text
    ▼
Claude permission layer
    ├── allow → execute original action
    └── deny  → block original action
```

No Guardian endpoint or Guardian-specific OpenAI request field is needed. The OpenAI model
is already acting as the separate permission reviewer.

### Current field translation

At the pinned CLIProxyAPI revision, the translator preserves:

- top-level system content as a developer message;
- chronological user, assistant, image, tool-call, and tool-result content;
- the classifier tool's `input_schema` as OpenAI function `parameters`;
- Claude forced tool choice `{type:"tool", name:"classify_result"}` as OpenAI forced
  function choice `{type:"function", name:"classify_result"}`;
- Claude thinking configuration as OpenAI reasoning effort;
- the OpenAI function call as an Anthropic `tool_use` result.

Source:
`/tmp/cliproxyapi-source/internal/translator/codex/claude/codex_claude_request.go:41-362`
and `:425-462`.

The same translator does not currently copy these Claude classifier controls:

- `max_tokens`;
- `temperature`;
- `stop_sequences`.

This matters most to the XML implementation. Claude requests 64 output tokens and
`stop_sequences:["</block>"]` for the default fast stage, 256 tokens without a stop
sequence for fast-only mode, and 4,096 tokens for the thinking stage. It also requests
`temperature:0` for both XML stages and the forced-tool classifier. CLIProxyAPI instead
constructs a new Responses request without those three fields.

The forced-tool classifier still has a constrained result schema, so the omission does not
remove its allow/deny protocol. For the XML fast stage, however, it removes the requested
output cap and early stop. The parser can still read a valid `<block>` result, but latency,
cost, and generated suffix text no longer match Claude's request. If the OpenAI endpoint
accepts an equivalent field, CLIProxyAPI should translate it. If it does not, the mismatch
should remain explicit rather than being described as lossless translation.

Claude source:
`/home/juraj/claude-code/src/utils/permissions/yoloClassifier.ts:695-709`,
`:779-795`, `:866-881`, and `:1134-1159`.

### Responsibility boundary

Claude Code owns:

- whether an action may be reviewed automatically;
- earlier hard permission and safety checks;
- which context the reviewer receives;
- classifier policy and environment rules;
- reviewer model selection;
- output schema;
- denial tracking and human fallback;
- the final allow/deny decision used for execution.

CLIProxyAPI owns:

- faithful translation of supported protocol fields;
- explicit documentation and testing of unsupported fields;
- preservation of message order and roles;
- tool-schema and forced-tool-choice translation;
- model routing requested by the client;
- response and usage translation;
- honest propagation of errors and incomplete streams.

OpenAI supplies the model inference for the reviewer prompt.

CLIProxyAPI should not attempt to reconstruct Codex `GuardianApprovalRequest`, parent
`Session` or `TurnContext`, approval policy, sandbox state, denial counters, or Guardian
session management. Claude Code does not send those native Codex objects, has already
performed the relevant permission checks, and remains the authority that executes or blocks
the action.

If Claude's reviewer lacks useful context, the correction belongs in Claude Code's reviewer
request or in an explicit reviewer protocol. CLIProxyAPI should not guess what omitted state
might have meant.

## Cache behavior

Claude's classifier marks its stable system prompt and the current transcript/action prefix
with Claude `cache_control`. The two-stage implementation intends stage two to reuse the
prefix created by stage one. The comments describe a one-hour classifier cache policy.

On the direct route, CLIProxyAPI currently:

- discards Claude block-level `cache_control`, including `5m` and `1h` TTL values;
- removes the older OpenAI `prompt_cache_retention` request field;
- does not add `prompt_cache_options`;
- derives a stable `prompt_cache_key` from model, Claude root session, and Claude agent;
- sends the same UUID as the `Session_id` header.

Direct experiments on port 8317 showed:

```text
OpenAI Responses, stable key, 4,510 input tokens

initial request:      cached_tokens = 0
identical request:    cached_tokens = 3,840
tail-only change:     cached_tokens = 3,840
returned retention:  24h on all three requests
```

The requests did not select a retention value; OpenAI selected `24h`.

Alternating Claude `5m` and `1h` TTLs produced byte-identical translated requests. Repeated
Claude Messages requests returned:

```text
OpenAI total input:              6,015
OpenAI cached input:             5,888
Anthropic input_tokens:            127
Anthropic cache_read_input_tokens: 5,888
```

CLIProxyAPI correctly subtracts cached tokens from OpenAI's total input count when producing
Anthropic usage. It does not return OpenAI's retention label or a cache-creation count to
Claude Code.

The current key composition is:

```text
execution scope = claude:<root-session-id>:agent:<agent-id-or-main>
identity = cli-proxy-api:codex:claude-code NUL model NUL execution-scope
prompt_cache_key = UUIDv5(OID namespace, identity)
```

Sources:

- `/tmp/cliproxyapi-source/internal/runtime/executor/helps/claude_code_session.go:14-104`;
- `/tmp/cliproxyapi-source/internal/translator/codex/claude/codex_claude_response.go:792-809`.

Claude's internal `querySource:'auto_mode'` does not enter this key. When the acting request
and reviewer use the same model, root session, and agent, CLIProxyAPI gives them the same
provider prompt-cache key. OpenAI still checks the actual prompt prefix before reporting a
cache hit, so key reuse does not make unlike prompts equivalent; it can still affect cache
locality and hit rate.

The agent-specific execution scope is appropriate for CLIProxy's reasoning-replay state.
Native Codex gives ordinary parent and child agents the same prompt-cache key, so using the
agent-specific scope for provider prompt-cache routing is a CLIProxy policy choice rather
than a native Codex requirement. A specialized reviewer identity could justify a distinct
cache key, as Guardian demonstrates, but it should come from trusted request metadata rather
than English prompt matching.

## Recommended design

### Baseline

Keep the classifier request as an ordinary Claude request. No Codex Guardian object model is
needed. Complete the direct translation where the target protocol has equivalent controls,
and document any remaining differences.

CLIProxyAPI should preserve or explicitly map:

- system and user content without reordering;
- the projected transcript and exact action;
- text and image content;
- the `classify_result` tool schema;
- forced tool choice;
- XML classifier text when that format is used;
- the requested model and thinking setting;
- `max_tokens` as the target protocol's output-token limit;
- temperature and stop-sequence controls when the target model and endpoint support them;
- an explicit documented mismatch when the target protocol has no equivalent control;
- function-call and usage fields on the response;
- explicit API and incomplete-stream failures.

### Optional explicit reviewer metadata

If future optimization requires separate model routing, telemetry, or prompt-cache locality,
Claude Code should send a stable trusted reviewer identity. For example:

```json
{
  "reviewer": {
    "kind": "permission",
    "implementation": "claude-auto-mode",
    "parent_session_id": "...",
    "parent_agent_id": "..."
  }
}
```

The exact protocol would require a joint change. CLIProxyAPI must not detect classifier
requests by matching the English system prompt.

With explicit metadata, CLIProxyAPI could:

- route reviews to a configured approval-review model;
- choose a reviewer-specific prompt-cache namespace;
- record reviewer-specific latency and token usage;
- apply transport retry policy without changing the security decision;
- leave Claude Code's output schema and final permission decision unchanged.

### Non-goals

Do not:

- port Codex Guardian's permission state into CLIProxyAPI;
- create a second allow/deny policy in the proxy;
- infer missing sandbox or organization rules;
- convert model errors into successful allow results;
- invent approval decisions after malformed output;
- rewrite classifier messages based on prompt wording;
- reintroduce removed request middleware.

## Regression checks

A maintained direct route should test:

1. A Claude auto-mode tool request becomes the equivalent OpenAI function definition.
2. Forced `classify_result` tool choice remains forced after translation.
3. OpenAI `function_call` returns as the expected Anthropic `tool_use` block.
4. XML `<block>no</block>` and block-with-reason text return unchanged.
5. System policy, `CLAUDE.md`, projected transcript, action, and images retain order.
6. An allowed classifier result reaches Claude as `shouldBlock:false`.
7. A denied classifier result reaches Claude as `shouldBlock:true` with its reason.
8. Claude classifier `max_tokens` maps to the OpenAI output-token limit instead of being
   discarded.
9. The XML fast stage either receives an equivalent `</block>` stop control or the test
   records that the target endpoint cannot provide one.
10. `temperature:0` is preserved when the selected model accepts it; otherwise the test
    records the provider limitation.
11. Malformed, failed, timed-out, and incomplete responses never become successful
    classifier results.
12. Main-loop requests and classifier requests do not rewrite one another's early prompt
    prefix.
13. OpenAI cached tokens translate to Anthropic uncached `input_tokens` plus
    `cache_read_input_tokens` without double counting.
14. Tests document that Claude `5m` and `1h` TTLs are currently discarded and that OpenAI
    chooses retention.
15. Any future reviewer-specific routing depends on explicit metadata, not prompt text.
16. Ordinary non-classifier Claude requests remain unchanged.

## Decision summary

- Claude Code auto mode and Codex Guardian are separate-model permission reviewers with the
  same core design.
- An ordinary prompt plus the exact action, selected context, and structured output is
  sufficient for one review.
- The current direct CLIProxyAPI path runs Claude's reviewer prompt on an OpenAI model and
  preserves its core message/tool protocol, but currently discards classifier `max_tokens`,
  `temperature`, and `stop_sequences`.
- Codex Guardian's reusable session is an optional client optimization, not an OpenAI API
  requirement.
- Claude Code remains the permission authority. CLIProxyAPI remains a protocol translator.
- Future reviewer optimization requires explicit client metadata; downstream inference of
  hidden permission state is not acceptable.
