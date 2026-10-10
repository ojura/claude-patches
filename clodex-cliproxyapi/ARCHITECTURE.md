# Clodex + CLIProxyAPI architecture

How Claude Code runs OpenAI models through CLIProxyAPI and Codex, checked on 2026-10-10
against:

```text
Claude Code:  2.1.296, extracted bundle /tmp/claude-2.1.296.js
CLIProxyAPI:  fork in /tmp/cliproxyapi-source at 2048218c; running binary PID 2835674,
              started 2026-10-09 09:35
Codex:        /tmp/codex-source at b741e480e (2026-10-03)
Launcher:     scripts/proxy-codex.sh in /tmp/cliproxyapi-source
Settings:     ~/.claude/settings.json (autoCompactWindow, modelSettings)
Proxy logs:   ~/.local/state/cliproxyapi/logs/v1-messages-*.log (local time)
Transcripts:  ~/.claude/projects/*/*.jsonl and their subagents/ files (UTC)
```

Each claim says whether it was confirmed in source, observed in logs or transcripts, or
is not established.

## Route

```text
Claude Code → CLIProxyAPI (127.0.0.1:8317) → https://chatgpt.com/backend-api/codex/responses
```

`scripts/proxy-codex.sh`, sourced through the symlink `~/.local/bin/proxy-codex.sh`,
starts one shared proxy if none answers its health check, waits until it is healthy, and
exports the Claude Code environment. It does not rewrite request bodies. The proxy talks
to Codex over HTTP: of the last 200 request logs of 2026-10-10, the 62 that name an
upstream transport all name `http`.

Claude Code owns the conversation: the transcript, the history sent to the model,
compaction, and resume, fork and rewind. CLIProxyAPI translates between the Claude
Messages and OpenAI Responses protocols
([`reports/claude-messages-to-codex-translation.md`](reports/claude-messages-to-codex-translation.md))
and keeps state derived from the requests it sees, such as the Codex reasoning replay
cache (`internal/cache/codex_reasoning_replay_cache.go`) and prompt-cache keys.

Three rules follow from that split:

- Claude Code's JSONL transcript is the authoritative, append-only record. The proxy
  keeps no second copy of the conversation, and every request must be reconstructible
  from the JSONL alone.
- The proxy reuses derived state for a request only when that state matches the
  request's model-facing content, target model and route (account and endpoint). A
  shared session ID is not enough. When the match cannot be shown, the proxy sends the
  full request without reuse.
- Codex's own compaction (`/responses/compact`) does not replace Claude Code's compaction
  unless its result is recorded in Claude Code's JSONL; otherwise the compacted history
  would exist only on the proxy side. [`NATIVE_COMPACTION.md`](NATIVE_COMPACTION.md)
  studies how that result could be recorded.

## Context window and compaction of GPT sessions

### Summary

- Claude Code takes a GPT model's context window from `CLAUDE_CODE_MAX_CONTEXT_TOKENS`
  and otherwise assumes 200,000 tokens. It does not read the limits the proxy lists in
  `/v1/models`.
- A GPT session compacts at
  `min(context window, autoCompactWindow) - min(output limit, 20,000) - 13,000`. With the
  launcher's 1,000,000 window and the 300,000 setting this is 267,000 tokens; for
  gpt-5.6-sol, whose setting is 272,000, it is 239,000.
- Codex's catalog, the ChatGPT backend's model list and CLIProxy's Codex-client catalog
  all give 272,000 as the default window and 872,000 as the maximum. Only CLIProxy's own
  provider registry lists 372,000.
- Codex has accepted much larger inputs than any catalog default: 921,105 tokens on
  gpt-5.6-sol, 888,091 on gpt-6-astra and 762,404 on gpt-6.1-sol. It rejected gpt-5.6-sol
  requests less than 1,000 tokens above 921,105 with `context_length_exceeded` on
  2026-09-19, and no request since.

### Context window

`Jv`, the window lookup, returns `CLAUDE_CODE_MAX_CONTEXT_TOKENS` when the variable is set
and positive and the model ID is not a Claude ID (`Yv`). Otherwise a GPT model gets
`d6e=200000`. Because `Yv` is false for Claude IDs, the variable leaves the window of
Claude models in the same shell unchanged.

The proxy cannot change this window:

- The per-model `GET /v1/models/{id}` request, which logs
  `[modelConfig] ... max_input_tokens=... max_tokens=...`, runs only when `XSe()` returns
  true, and the bundle defines `function XSe(){return!1}`.
- `~/.claude/cache/gateway-models.json`, Claude Code's cached copy of the proxy's model
  list, holds IDs and display names of Claude models and nothing else.

The proxy's `max_input_tokens: 272000` and `max_tokens: 64000` for GPT models therefore
have no effect. The proxy does not send an output limit to Codex either: the
Claude-to-Codex translator (`internal/translator/codex/claude/codex_claude_request.go`)
does not copy `max_tokens`, and the Responses request path deletes `max_output_tokens`
and `max_completion_tokens`
(`internal/translator/codex/openai/responses/codex_openai-responses_request.go:30`).

### Compaction window and thresholds

`iv` selects the compaction window. The first of these that is set wins:

1. `CLAUDE_CODE_AUTO_COMPACT_WINDOW` in the environment;
2. `autoCompactWindow` in settings: the model's entry under `modelSettings`, else the
   top-level value. `/autocompact` writes this setting;
3. client data, experiments and model defaults, which apply only when neither of the
   above is set.

The window used is the smaller of that value and the context window. While the
environment variable is set, `/autocompact` changes nothing and answers
"CLAUDE_CODE_AUTO_COMPACT_WINDOW is set and takes precedence. Unset it to change this
setting." (session e9c13d2e, 2026-10-04).

The thresholds follow from that window:

```text
effective window   = compaction window - min(output limit, 20,000)        p3
compaction point   = effective window - 13,000                             t6
warning            = compaction point - 20,000                             WFe
local hard limit   = context window - min(output limit, 20,000) - 3,000    frr, WFe
```

The local hard limit uses the context window, not the compaction window. A GPT model's
output limit is Claude Code's default for models it does not know, 32,000 (`Wv`), unless
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` sets another, so the reservation is 20,000.
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` and `CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE` would move the
compaction point and the hard limit; neither is set. Precomputed compaction, which starts
a summary before the compaction point, also requires the GrowthBook flag
`tengu_sepia_moth`, which the cache in `~/.claude.json` holds as false.

With the current settings (top-level `autoCompactWindow` 300,000; gpt-6.1-sol and
gpt-6-astra 300,000; gpt-5.6-sol 272,000):

| GPT model | Compaction window | Compaction point | Warning | Local hard limit |
|---|---:|---:|---:|---:|
| gpt-6.1-sol, gpt-6-astra, others without an entry | 300,000 | 267,000 | 247,000 | 977,000 |
| gpt-5.6-sol | 272,000 | 239,000 | 219,000 | 977,000 |
| any, without `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | 200,000 | 167,000 | 147,000 | 177,000 |

Recorded compactions agree. From 2026-10-05 to 2026-10-10 the transcripts hold 488
automatic compactions on gpt-6.1-sol with a median `preTokens` of 269,637 (10th
percentile 261,093) and 43 on gpt-6-astra with a median of 270,143. `preTokens` is the
count stored with the compact boundary, not the estimate that triggered compaction, so it
scatters around the compaction point.

gpt-5.5 is the one catalog model whose maximum, 272,000, lies close to the 267,000
compaction point. A turn that adds more than about 5,000 tokens past that point would
reach Codex's advertised limit before Claude Code compacts.

### What the catalogs advertise

| Source | Default window | Maximum |
|---|---:|---:|
| Codex bundled catalog, every GPT-5.6 and GPT-6 model (`codex-rs/models-manager/models.json`) | 272,000 | 872,000 |
| ChatGPT backend `/codex/models`, GPT-5.6 models, queried 2026-10-07 in session 897ba623 | 272,000 | 872,000 |
| CLIProxy Codex-client catalog (`internal/registry/models/codex_client_models.json`) | 272,000 | 872,000 |
| CLIProxy provider registry (`internal/registry/models/models.json`), gpt-5.6-sol | 372,000 for Team and Plus, 921,000 for Pro | none |
| CLIProxy provider registry, gpt-6.1-sol and gpt-6-astra | 272,000 | none |

gpt-5.5 lists 272,000 as both default and maximum. Codex itself uses `context_window`
when present and `max_context_window` otherwise (`ModelInfo::resolved_context_window`), so
it runs these models with 272,000 unless the user configures another window. The provider
registry's 372,000 and 921,000 and its `max_completion_tokens: 128000` were written by the
CLIProxy maintainers; the backend's model list has no output-limit field.

Agreement among these lists does not show what Codex accepts. The accepted and rejected
sizes below come from real requests.

### What Codex accepted and rejected

Largest inputs of completed responses, as Claude Code recorded the usage the proxy
reported (input, cache-read and cache-creation tokens), in transcripts written since
2026-08-26:

| Model | Input tokens | Time (UTC) | Transcript |
|---|---:|---|---|
| gpt-5.6-sol | 921,105 | 2026-09-19 19:26:19 | `-home-juraj-navines/50b6da86-5ad3-4cf6-b4f0-902a03957c56.jsonl` |
| gpt-6-astra | 888,091 | 2026-09-06 15:49:54 | `-home-juraj/3767e7e1-9319-480e-a649-b96678eb8932/subagents/agent-ainvestigate-defects-expl-c6153c8f8afefbf5.jsonl` |
| gpt-6.1-sol | 762,404 | 2026-10-07 18:36:56 | `-home-juraj-navines/15e8f3f8-318a-4347-896c-ac874a9f7b42/subagents/agent-ac204684c509d6e4a.jsonl` |

The gpt-5.6-sol and gpt-6-astra values exceed the advertised 872,000 maximum. The
gpt-5.6-sol request went through the Plus account, for which the provider registry lists
372,000.

The session's next request, at 21:26:43 local time, added only the 222 output tokens of
that response, a 394-character tool result and an attachment of about 1,000 characters.
Codex rejected it with
`{"code":"context_length_exceeded","message":"Your input exceeds the context window of this model. ..."}`,
and it rejected every later attempt of the session that reached it, through 21:29:28.
Codex answered each with HTTP 200 and an error event. The proxy passed the event on to
the streaming requests and returned HTTP 400 to Claude Code's non-streaming retries (for
example `v1-messages-2026-09-19T212733-5c746457.log`). The gpt-5.6-sol limit of the Plus
account on that day therefore lay between 921,105 tokens and less than 1,000 tokens more.

None of the 75,112 request logs written from 2026-09-19 23:00 to 2026-10-10 23:22 contains
`"code":"context_length_exceeded"`. The same search finds the 14 logs of the rejections
above.

### Launcher settings

`scripts/proxy-codex.sh` exports `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000` and exports
neither `CLAUDE_CODE_AUTO_COMPACT_WINDOW` nor `CLAUDE_CODE_MAX_OUTPUT_TOKENS`.

- `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`: without it every GPT session compacts at
  167,000 and stops at 177,000, whatever `autoCompactWindow` says. With it, the
  `autoCompactWindow` settings alone set each model's compaction point.
- No `CLAUDE_CODE_AUTO_COMPACT_WINDOW`: the variable overrides the per-model settings and
  `/autocompact`, so one value would apply to every model started from the shell and could
  not be changed in a session.
- No `CLAUDE_CODE_MAX_OUTPUT_TOKENS`: Claude Code sends the output limit as `max_tokens`,
  which the proxy drops, and reserves `min(limit, 20,000)`, which stays 20,000 for any
  limit of 20,000 or more.

The local hard limit of 977,000 lies above the gpt-5.6-sol limit measured on 2026-09-19,
so Claude Code does not stop a request that Codex will reject. The compaction points of
267,000 and 239,000 keep sessions far below both. A request that does reach Codex's limit
fails with `context_length_exceeded`, which the proxy returns to Claude Code as an error
whose message starts with "Prompt is too long: "
(`sdk/api/handlers/claude/code_handlers.go`; the running binary contains the string).
Claude Code answers a prompt-too-long error with reactive compaction (the `ptl` trigger,
which calls `oir`), provided automatic compaction is enabled. This path is
source-confirmed only.

### Not established

- Codex's limits for gpt-6.1-sol and gpt-6-astra. Neither has had a request rejected, and
  measuring them means sending requests of 800,000 tokens or more on the user's accounts.
- Whether the gpt-5.6-sol limit depends on the plan. The only rejection came through the
  Plus account.
- Claude Code's recovery from a Codex context error end to end. No session has reached
  Codex's limit since the proxy began translating the error. A local stub that answers
  with `context_length_exceeded` would test it without using Codex.
