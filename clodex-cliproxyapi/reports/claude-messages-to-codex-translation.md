# Claude Messages to Codex translation in CLIProxyAPI

Date: 2026-09-05

This report describes how CLIProxyAPI converts Anthropic Claude Messages API requests
into OpenAI Responses API requests for Codex, then converts Codex output back into
Claude Messages responses.

The findings are **source-confirmed**. The request and response paths were traced in the
source tree, but this report does not claim a new end-to-end executable reproduction.
Existing runtime findings remain classified separately in
[`../ARCHITECTURE.md`](../ARCHITECTURE.md) and the retained evidence files.

## Source identity

Inspected source:

```text
repository:  https://github.com/router-for-me/CLIProxyAPI.git
working copy: /tmp/cliproxyapi-source
commit:      5a87e084ca274d53dcdf98f8de3b993228354239
describe:    v7.2.146-23-g5a87e084-dirty
```

The working tree contained changes under `scripts/` only:

```text
M  scripts/swap-local-proxy.sh
?? scripts/tests/
```

The translator, executor, routing, thinking, utility, and handler paths cited in this
report matched `HEAD` at the time of inspection.

Line references below identify this source revision. Earlier project documents pin
CLIProxyAPI `v7.2.116`; their line numbers and version-specific claims remain tied to
that older revision.

## End-to-end path

The request path is:

```text
Claude POST /v1/messages
    -> Claude handler
    -> provider and model selection
    -> claude-to-codex request translator
    -> OpenAI Responses request
    -> Codex /responses with stream:true
    -> Responses SSE events
    -> codex-to-claude response translator
    -> Claude SSE events or one reconstructed Claude response
```

`POST /v1/messages` is registered in:

```text
internal/api/server_routes.go:75
```

The Claude handler reads the raw request and selects streaming or non-streaming
handling from the request's `stream` field:

```text
sdk/api/handlers/claude/code_handlers.go:69-92
```

The shared handlers set the source and response protocols to Claude, resolve the
requested model to registered providers, and invoke the selected executor:

```text
sdk/api/handlers/handlers_execution.go:44-95
sdk/api/handlers/handlers_stream.go:292-354
sdk/api/handlers/handlers_routing.go:159-223
internal/util/provider.go:46-73
```

For a Codex provider, the executor invokes the registered `claude -> codex`
translator:

```text
internal/runtime/executor/codex_executor_execute.go:28-68
internal/translator/codex/claude/init.go:9-19
sdk/translator/registry.go:62-109
```

The resulting request is sent to the provider's `/responses` endpoint. Codex upstream
streaming is enabled even when the downstream Claude request is non-streaming:

```text
internal/runtime/executor/codex_executor_execute.go:55-104
```

For a downstream non-streaming request, the executor consumes the upstream event
stream, reconstructs a terminal Responses object, and passes that object to the
non-streaming response translator:

```text
internal/runtime/executor/codex_executor_execute.go:126-185
```

## Request construction

The translator creates a new Responses request from this template:

```json
{
  "model": "",
  "instructions": "",
  "input": []
}
```

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:52-60
```

This matters because Claude fields are copied only when the translator handles them
explicitly. Fields absent from the conversion logic do not survive merely because they
were present in the original request.

## Two Claude system channels

CLIProxyAPI handles Claude's top-level `system` field differently from a message-level
`role: "system"` item.

### Top-level `system`

A top-level Claude system string or array of text blocks becomes a Responses input
message with `role: "developer"` and `input_text` content:

```json
{
  "type": "message",
  "role": "developer",
  "content": [
    {
      "type": "input_text",
      "text": "..."
    }
  ]
}
```

The translator removes Claude Code attribution text. It does not place the text in the
Responses `instructions` string; that field remains empty.

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:62-94
```

### Message-level `role: "system"`

A system item inside the Claude message sequence becomes a Responses user message at
the corresponding history position. Its text is wrapped in literal reminder tags:

```json
{
  "type": "message",
  "role": "user",
  "content": [
    {
      "type": "input_text",
      "text": "<system-reminder>\n...\n</system-reminder>"
    }
  ]
}
```

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:101-111
internal/translator/common/claude_system.go:15-56
```

The outer Codex `user` role therefore does not prove that the human user authored the
message. It can be a serialization of a Claude message-level system item. The literal
`<system-reminder>` marker is added by the translator.

This conversion loses the original role in the Responses role field while retaining an
origin marker in the message text. A captured Codex request must be interpreted with
this conversion in mind. Reading only the outer role can misclassify the message.

## Message and content-block conversion

The translator walks normal `user` and `assistant` messages and converts their blocks
in order:

| Claude content | Responses representation |
|---|---|
| User `text` | `message`, `role: "user"`, `input_text` |
| Assistant `text` | `message`, `role: "assistant"`, `output_text` |
| Image source | `input_image` with a data URL |
| Base64 PDF document | `input_file` with `file_data` and filename `document.pdf` |
| Assistant `tool_use` | standalone `function_call` item |
| `tool_result` | standalone `function_call_output` item |
| Compatible assistant `thinking` | standalone `reasoning` item containing `encrypted_content` |

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:96-295
```

Text accumulated before and after a reasoning or tool-call item is emitted as separate
Responses message items. This retains its relative position around standalone
`reasoning`, `function_call`, and `function_call_output` items.

Images become data URLs:

```text
data:<media-type>;base64,<data>
```

A base64 PDF document becomes an `input_file` with the fixed filename
`document.pdf`.

When a Claude tool result contains an array, recognized text and image blocks become
`input_text` and `input_image` parts. Other content-block types are not explicitly
converted by this branch:

```text
internal/translator/codex/claude/codex_claude_request.go:237-285
```

## Thinking request settings

Claude thinking settings affect two separate parts of the Codex request:

1. requested reasoning effort;
2. whether Codex should return a readable reasoning summary.

### Budget to reasoning effort

The default Codex reasoning effort is `medium`. Claude
`thinking.type: "enabled"` converts `budget_tokens` according to these thresholds:

| Claude `budget_tokens` | Codex effort |
|---:|---|
| `-1` | `auto` |
| `0` | `none` |
| `1-512` | `minimal` |
| `513-1024` | `low` |
| `1025-8192` | `medium` |
| `8193-24576` | `high` |
| `24577+` | `xhigh` |

`adaptive` or `auto` uses `output_config.effort` when present, otherwise `xhigh`.
`disabled` becomes `none`.

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:351-380
internal/thinking/convert.go:47-96
```

The central thinking pipeline can subsequently normalize or limit the requested effort
according to the resolved target model:

```text
internal/runtime/executor/helps/model_capabilities.go:16-28
internal/thinking/provider/codex/apply.go:35-120
```

### Summary request

An active Claude `thinking.display: "summarized"` request becomes Codex
`reasoning.summary`. Omitted or disabled summary intent removes that field:

```text
internal/thinking/summary.go:63-70
internal/thinking/summary.go:216-228
sdk/translator/registry.go:75-84
```

## Replaying earlier assistant thinking

When an earlier Claude assistant message contains a `thinking` block, its readable
`thinking` text is not sent back to Codex. A compatible signature becomes a Responses
`reasoning` item:

```json
{
  "type": "reasoning",
  "summary": [],
  "content": null,
  "encrypted_content": "<compatible signature>"
}
```

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:149-174
```

Foreign signatures, including ordinary native Anthropic signatures, are normally
omitted. The compatibility endpoint has a separate empty-signature case, and Grok
targets have provider-specific validation.

The generated request always asks Codex to return encrypted reasoning state and disables
provider-side response storage:

```json
{
  "include": ["reasoning.encrypted_content"],
  "store": false
}
```

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:391-394
```

The translator never decrypts this value. Readable reasoning comes from summary or
content fields returned separately by Codex. Encrypted content is carried back as a
Claude thinking signature and can later be replayed as opaque provider state.

## Tool definitions

Claude tools are converted to Responses function tools:

```json
{
  "type": "function",
  "name": "read_file",
  "description": "...",
  "parameters": {
    "type": "object",
    "properties": {}
  },
  "strict": false
}
```

The converter:

- changes `input_schema` to `parameters`;
- inserts missing object `type` and `properties` fields;
- removes `input_schema` after conversion;
- removes `parameters.$schema`;
- removes `cache_control`;
- removes `defer_loading`;
- forces `strict` to `false`.

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:297-340
internal/translator/codex/claude/codex_claude_request.go:624-640
```

### Tool choice

| Claude choice | Responses choice |
|---|---|
| `auto` | `"auto"` |
| `any` | `"required"` |
| `none` | `"none"` |
| named `tool` | `{"type":"function","name":"..."}` |
| named web-search tool | `{"type":"web_search"}` |

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:480-517
```

Claude's `disable_parallel_tool_use` is inverted into Responses
`parallel_tool_calls`. Parallel calls are enabled by default:

```text
parallel_tool_calls = !disable_parallel_tool_use
```

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:342-349
```

### Web search

Claude tools named `web_search_20250305` or `web_search_20260209` become native
Responses `web_search` tools rather than ordinary functions. `allowed_domains` moves to
`filters.allowed_domains`, and `user_location` is retained:

```text
internal/translator/codex/claude/codex_claude_request.go:455-527
```

## Tool names and call IDs

### Tool-name shortening

Codex function names are limited to 64 characters. The request translator builds one
name map for the request:

- long MCP names preferentially become `mcp__<last-segment>`;
- other long names are truncated;
- collisions receive `_1`, `_2`, and later numeric suffixes;
- declarations and historical Claude `tool_use` blocks use the same generated names.

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:530-621
```

The response translator rebuilds the reverse map from the original Claude request and
restores the original Claude-visible tool name:

```text
internal/translator/codex/claude/codex_claude_response.go:348
internal/translator/codex/claude/codex_claude_response.go:444-449
internal/translator/codex/claude/codex_claude_response.go:640-645
internal/translator/codex/claude/codex_claude_response.go:789-795
internal/translator/codex/claude/codex_claude_response.go:817-839
```

There is no persistent cross-request tool-name map. Reversal depends on the original
request supplied to the response translator.

### Call IDs

Call IDs longer than 64 bytes are shortened deterministically to a retained prefix plus
16 hexadecimal characters derived from SHA-256:

```text
internal/translator/codex/claude/codex_claude_request.go:438-453
```

Codex-returned IDs are also sanitized to Claude's permitted character set
`[a-zA-Z0-9_-]+`:

```text
internal/util/claude_tool_id.go:21-29
```

During a stream, function-call state is indexed by output index, call ID, item ID, and
the last observed call. Argument events that identify the call through different fields
can therefore update one state object:

```text
internal/translator/codex/claude/codex_claude_response.go:526-620
```

This state exists only for the current stream.

## Other request fields

### Structured JSON output

Claude `output_config.format` with a JSON schema becomes Responses `text.format` with:

- `type: "json_schema"`;
- a supplied or generated name;
- `strict`;
- the schema itself.

Source:

```text
internal/translator/codex/claude/codex_claude_request.go:395-410
```

### Priority service tier

Claude `service_tier: "fast"`, `service_tier: "priority"`, or `speed: "fast"`
becomes Responses `service_tier: "priority"`:

```text
internal/translator/codex/claude/codex_claude_request.go:384-390
```

### Fields omitted by the native translator

The native converter does not copy these Claude request fields into its rebuilt payload:

- `max_tokens`;
- `temperature`;
- `top_p`;
- `top_k`;
- `stop_sequences`;
- general metadata.

A configured payload rule or plugin can add fields after native translation, but that is
a separate stage. In particular, Claude `max_tokens` does not directly become a Codex
output-token field in this translator.

## Model alias resolution

Configured Codex API-key models use `alias` as the client-facing name and `name` as the
upstream model:

```text
internal/config/config_types.go:514-540
```

Before each attempt, the auth conductor resolves any alias and replaces the request
model with the configured upstream name:

```text
sdk/cliproxy/auth/oauth_model_alias.go:140-201
sdk/cliproxy/auth/conductor_execution.go:467-507
```

When `force-mapping` is enabled, the response model can be rewritten back to the
client-facing alias:

```text
sdk/cliproxy/auth/conductor_execution.go:577-580
```

## Streaming response conversion

The streaming converter keeps state for:

- the next Claude content-block index;
- open text and thinking blocks;
- active and queued function calls;
- postponed events;
- reasoning signatures;
- web-search IDs.

State definition:

```text
internal/translator/codex/claude/codex_claude_response.go:28-58
```

Main mappings:

| Responses event | Claude SSE output |
|---|---|
| `response.created` | `message_start` |
| reasoning-summary part or text delta | thinking block and `thinking_delta` |
| completed reasoning item | `signature_delta`, then thinking block stop |
| `response.output_text.delta` | text block and `text_delta` |
| function-call events | `tool_use` block and `input_json_delta` |
| completed web-search item | `server_tool_use` and `web_search_tool_result` blocks |
| `response.completed` or `response.incomplete` | `message_delta`, usage, then `message_stop` |
| `error` | Claude `event:error` |

Main state machine:

```text
internal/translator/codex/claude/codex_claude_response.go:76-277
```

### Reasoning events

Multiple Codex summary parts remain inside one Claude thinking block and are separated
with a blank line. The block remains open until the completed reasoning item provides
`encrypted_content`. That value becomes a Claude `signature_delta`, after which the
thinking block is closed.

A signature-only reasoning item still produces a Claude thinking block.

Source:

```text
internal/translator/codex/claude/codex_claude_response.go:110-128
internal/translator/codex/claude/codex_claude_response.go:183-191
internal/translator/codex/claude/codex_claude_response.go:243-254
internal/translator/codex/claude/codex_claude_response.go:870-925
```

### Parallel function calls

Responses function calls can produce interleaved events, while Claude SSE content
blocks must be emitted consecutively. The converter therefore buffers argument deltas,
queues calls, and temporarily postpones unrelated events while one tool block is active:

```text
internal/translator/codex/claude/codex_claude_response.go:93-98
internal/translator/codex/claude/codex_claude_response.go:280-309
internal/translator/codex/claude/codex_claude_response.go:526-638
internal/translator/codex/claude/codex_claude_response.go:640-776
```

Web-search responses use separate conversion logic:

```text
internal/translator/codex/claude/codex_claude_response_web_search.go:13-89
```

## Non-streaming response conversion

For a downstream non-streaming request, the executor reconstructs terminal output from
collected `response.output_item.done` events before calling the translator. This is
necessary because a terminal Responses event can omit complete output items:

```text
internal/runtime/executor/codex_executor_execute.go:130-180
```

The translator then traverses the reconstructed Responses `output` array in order:

| Responses output item | Claude content block |
|---|---|
| `reasoning` | `thinking`, using summary/content text and encrypted content as `signature` |
| `message` | `text` |
| `function_call` | `tool_use` |
| `web_search_call` | `server_tool_use`, followed by `web_search_tool_result` |

Source:

```text
internal/translator/codex/claude/codex_claude_response.go:343-475
```

## Stop reasons, usage, and errors

Terminal status is converted as follows:

| Responses status or condition | Claude stop reason |
|---|---|
| completed, stop, or empty reason | `end_turn` |
| token limit | `max_tokens` |
| content filter | `refusal` |
| any emitted function call | `tool_use` |
| supported Claude reason already present | passed through |

Supported pass-through values include `stop_sequence`, `pause_turn`, and
`model_context_window_exceeded`.

Source:

```text
internal/translator/codex/claude/codex_claude_response.go:478-513
```

For usage, cached tokens are subtracted from `input_tokens` and reported separately as
`cache_read_input_tokens`. Output tokens pass through:

```text
internal/translator/codex/claude/codex_claude_response.go:797-815
```

Raw Codex SSE errors become Claude error events. `cyber_policy` and `invalid_request`
are normalized to Claude `invalid_request_error`:

```text
internal/translator/codex/claude/codex_claude_response.go:311-340
```

HTTP and terminal failures detected before response translation are emitted through the
Claude handler's JSON or SSE error writers:

```text
internal/runtime/executor/codex_executor_execute.go:114-147
sdk/api/handlers/claude/code_handlers.go:303-359
```

## Count Tokens

The Codex implementation of Claude `/messages/count_tokens` translates the request but
does not call the upstream provider. It locally counts extracted instructions, messages,
function calls and results, tool definitions, and structured-output schemas:

```text
internal/runtime/executor/codex_executor_tokens.go:18-52
internal/runtime/executor/codex_executor_tokens.go:75-175
```

The Claude response is:

```json
{"input_tokens": 123}
```

Response helper:

```text
internal/translator/codex/claude/codex_claude_response.go:841-843
```

## WebSocket path

The optional Codex WebSocket executor uses the same Claude-to-Codex request translator
and central thinking processing:

```text
internal/runtime/executor/codex_websockets_stream.go:39-67
```

Transport handling differs, but the source request conversion described above is shared.

## Concise mapping summary

```text
Claude top-level system
    -> Responses developer message

Claude message-level role:system
    -> Responses user message containing <system-reminder>

Claude user text
    -> Responses user input_text

Claude assistant text
    -> Responses assistant output_text

Claude assistant tool_use
    -> Responses function_call

Claude tool_result
    -> Responses function_call_output

Claude thinking budget
    -> Responses reasoning effort

Claude prior thinking text
    -> not replayed upstream

Claude prior compatible thinking signature
    -> Responses reasoning.encrypted_content

Responses reasoning summary/content
    -> Claude thinking text

Responses reasoning encrypted_content
    -> Claude thinking signature

Responses function call
    -> Claude tool_use

Responses output text
    -> Claude text
```

## Consequences

1. **The serialized role cannot always identify origin.** A Codex-side `user` message
   containing `<system-reminder>` may have originated as a Claude message-level system
   item. The translator created both the role and wrapper.
2. **Top-level and later system content use different Codex roles.** Stable top-level
   Claude system text becomes `developer`; later message-level system content becomes a
   reminder-wrapped `user` item at its original chronological position.
3. **Readable earlier thinking is not continuation state.** Codex continuation uses a
   compatible encrypted signature. The readable Claude thinking text is not sent back.
4. **The translator does not reveal encrypted reasoning.** It forwards readable summary
   or content fields and carries encrypted content as an opaque signature.
5. **Claude request controls survive only when explicitly converted.** Several sampling,
   output-limit, stop, and metadata fields are omitted by the native translator.
6. **Parallel tool events require serialization.** The response converter postpones
   events so each Claude content block remains consecutive.
7. **Tool-name restoration depends on the original request.** No persistent cross-request
   map stores long-to-short names.

## Verification status and remaining work

This report records static source findings at the pinned commit. Useful executable
follow-up would include:

- capture one request containing both top-level and message-level system content;
- capture thinking replay with a compatible and an incompatible signature;
- verify the effective upstream payload after configured payload rules and plugins;
- compare streaming and non-streaming Claude output for identical Responses events;
- exercise parallel function calls with interleaved upstream deltas;
- verify every omitted Claude field against the final upstream request rather than only
  the native translator output;
- retain exact request and response captures without credentials or unrelated harness
  content.

Until those checks are run, the conversion details above remain source-confirmed rather
than executable-reproduced for this source revision.
