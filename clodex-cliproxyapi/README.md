# Clodex + CLIProxyAPI

Notes and tools for running Claude Code on OpenAI models through the CLIProxyAPI fork in
`/tmp/cliproxyapi-source`, which forwards to Codex.

- [`ARCHITECTURE.md`](ARCHITECTURE.md): the route, what Claude Code and the proxy each
  own, the rules for state the proxy derives from requests, and what sets the context
  window and compaction point of a GPT session.
- [`NATIVE_COMPACTION.md`](NATIVE_COMPACTION.md): provisional study of translating Claude
  Code's compaction to Codex's `/responses/compact` and recording the result in the
  JSONL. Its observations were made on Claude Code 2.1.220 and CLIProxyAPI v7.2.116.
- [`reports/claude-messages-to-codex-translation.md`](reports/claude-messages-to-codex-translation.md):
  source map of how the proxy turns Claude Messages requests into Codex Responses
  requests and Codex output back into Claude responses.
- [`tools/repair_empty_text_blocks.py`](tools/repair_empty_text_blocks.py): finds and
  repairs the empty text blocks that stop a proxied session from resuming on a Claude
  model. See [Repairing a transcript](#repairing-a-transcript).

## Repairing a transcript

A session routed through CLIProxyAPI to an OpenAI Responses-API model can record an
assistant content block of `{"type":"text","text":""}`. Two things produce it: a
translation artifact between a thinking block and the real text of the same message,
which loses nothing; and a genuinely empty model turn, where the whole message is that
one block with `stop_reason` `end_turn` and a few output tokens. Claude Code recognizes
the second case and injects a request for visible output, and the next turn recovers.

CLIProxyAPI accepts both. api.anthropic.com rejects them with `400 messages: text
content blocks must be non-empty`. Since every request replays the whole transcript,
one such block anywhere in the file stops the session from resuming on a Claude model,
and the 400 arrives before any inference.

```sh
tools/repair_empty_text_blocks.py SESSION_UUID          # report, then offer to fix
tools/repair_empty_text_blocks.py --all                 # every stored transcript
tools/repair_empty_text_blocks.py SESSION_UUID --fix    # repair without asking
tools/repair_empty_text_blocks.py SESSION_UUID --check   # report only, never ask
tools/repair_empty_text_blocks.py --selftest
```

When anything is found the report ends with a `[Y/n]` prompt and repairs on
confirmation, so the usual run is a single command. Nothing is asked and nothing is
written when stdin is not a terminal or when `--check` or `--json` is given; those cases
exit 1 with the blocks left in place, which suits a scripted check.

Repairs rewrite only the offending lines. Every other line is copied byte for byte, the
rewritten file is verified before it replaces the original, and the original is copied
to a `.bak-empty-text-<timestamp>` name unless `--no-backup` is given. The tool refuses
to write while a process holds the transcript open unless `--force` is given.

Empty *thinking* blocks appear in the same transcripts and are much more common; their
`signature` carries OpenAI's encrypted reasoning payload rather than an Anthropic
thinking signature. Sessions 8ee68baf and 11d44c34 were repaired on 2026-08-16 and
resumed on Opus with 294 and 136 such blocks left in place, so the tool reports them and
changes nothing. Whether a larger number or a different block structure is rejected is
untested; the API reports only its first validation failure, so the text blocks are the
only confirmed cause.
