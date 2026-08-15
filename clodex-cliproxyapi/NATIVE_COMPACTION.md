# Native Codex compaction through Claude Code

Date: 2026-08-11

> **Status: provisional design.** This document separates verified observations from an
> unfinished process draft. It does not change the accepted decisions in
> [`ARCHITECTURE.md`](ARCHITECTURE.md), does not authorize an implementation, and must not
> be cited as proof that the proposed replay process works.

The question under study is whether a Claude Code compaction request can be translated to
OpenAI `/responses/compact`, with the resulting Codex `ResponseItem` history stored in
Claude's append-only JSONL and replayed through an original Claude Code instance.

## Fixed project constraint

Claude JSONL is the authoritative append-only event log.

- Existing rows are never truncated, rewritten, deleted, or physically garbage-collected.
- Semantic compaction appends a compact boundary and a new root.
- Earlier rows remain on disk and are omitted only during active-lineage reconstruction.
- Resume, fork, rewind, and recovery must remain derivable from JSONL alone.
- CLIProxyAPI must not own a second conversation-history database.

Any native-compaction design that requires an identifier-to-history table stored only by
CLIProxyAPI is rejected by this constraint.

## Hard observations

### Evidence classes

- **Authoritative source-confirmed:** reachable behavior in the extracted Claude Code
  2.1.220 bundle at `/tmp/claude-2.1.220.js`.
- **Readable-source-confirmed:** names and comments in `/home/juraj/claude-code`, used for
  orientation and checked against the bundle where this document makes a 2.1.220 claim.
- **Codex-source-confirmed:** behavior in `/tmp/codex-source` at
  `d75f94a94d5cb0bbabc59b86c0427c7ad09a9d6d`.
- **CLIProxy-source-confirmed:** behavior in `/tmp/cliproxyapi-source` at
  `a88197f845c979132c8978ea223c6af05cc81536`, tag `v7.2.116`.
- **Not executable-reproduced:** the proposed compaction interception, JSONL carrier, and
  replay process described later in this file.

### Claude sends history followed by a compaction prompt

For full compaction, Claude Code constructs a user summary request containing its compaction
prompt and sends it after the selected history. In simplified notation:

```text
H + CP
```

The 2.1.220 bundle builds the compact prompt, creates a user message from it, and passes the
selected messages plus that request to the compact summary query. The relevant minified
flow is around `/tmp/claude-2.1.220.js:16912`.

Readable orientation:

- `/home/juraj/claude-code/src/services/compact/compact.ts:440-458`;
- `/home/juraj/claude-code/src/services/compact/prompt.ts:337-374`.

### Claude 2.1.220 requires nonempty assistant text

The compaction response is not installed as arbitrary history. Claude extracts assistant
text and fails if no nonempty text exists:

```text
Compact failed: no summary text in response.
Failed to generate conversation summary - response did not contain valid text content
```

The authoritative check and failure are at `/tmp/claude-2.1.220.js:16912`, around byte
10,306,067.

The readable equivalent is
`/home/juraj/claude-code/src/services/compact/compact.ts:447-505`.

Non-text response blocks are not added to the compact result by this path. The assistant
response is otherwise used for text extraction and usage accounting.

### Claude wraps the returned text and creates a user compact root

Claude formats the returned text, prefixes it with:

```text
This session is being continued from a previous conversation that ran out of context.
The summary below covers the earlier portion of the conversation.
```

and may append the transcript path, recent-message notice, cleared-REPL notice, and direct
continuation instruction.

The wrapper is source-confirmed at `/tmp/claude-2.1.220.js:3246`, around byte 7,430,541.
The compact result then creates one user message with:

```text
isCompactSummary: true
isVisibleInTranscriptOnly: true
```

at `/tmp/claude-2.1.220.js:16912`.

Readable orientation:

- `/home/juraj/claude-code/src/services/compact/prompt.ts:337-374`;
- `/home/juraj/claude-code/src/services/compact/compact.ts:596-624`.

Therefore an unmodified 2.1.220 compaction cannot accept a list of Codex `ResponseItem`s in
place of text.

### The compact boundary and root are appended semantic records

Claude constructs a `compact_boundary` system record and compact-summary user record. Later
reconstruction uses the boundary and active parent chain to omit the preceding semantic
prefix. The project constraint requires all earlier JSONL rows to remain physically present.

Readable installation order is defined by
`/home/juraj/claude-code/src/services/compact/compact.ts:325-338`:

```text
boundary marker
summary messages
preserved messages, if any
attachments
hook results
```

### Native Codex receives structured compaction input

The native endpoint is:

```text
POST /responses/compact
```

Its request contains:

```text
model
input: ResponseItem[]
instructions
tools
parallel_tool_calls
reasoning
service_tier
prompt_cache_key
text controls
```

Source: `/tmp/codex-source/codex-rs/codex-api/src/common.rs:26-44` and
`codex-api/src/endpoint/compact.rs:35-88`.

The endpoint returns:

```rust
Vec<ResponseItem>
```

Native Codex then filters and adjusts the result, installs it as replacement thread history,
and recomputes token usage. That installation is native Codex client behavior, not an OpenAI
API side effect.

Source: `/tmp/codex-source/codex-rs/core/src/compact_remote.rs:261-299`.

The Clodex design cannot copy native Codex's destructive history replacement because Claude
JSONL must remain append-only.

### CLIProxy already round-trips opaque reasoning state

For assistant Claude `thinking` blocks, the current Claude-to-Codex translator reads the
`signature` and emits an OpenAI reasoning item containing `encrypted_content` when the
signature is compatible with the target provider.

Source:
`/tmp/cliproxyapi-source/internal/translator/codex/claude/codex_claude_request.go:132-153`.

In the reverse direction, an OpenAI reasoning item becomes a Claude `thinking` block with
its `encrypted_content` in `signature`.

Source:
`/tmp/cliproxyapi-source/internal/translator/codex/claude/codex_claude_response.go:374-417`.

This establishes a standard JSONL-carried path for opaque provider state. It does not
establish that a complete compacted `ResponseItem` list can use that path unchanged.
CLIProxy currently treats a thinking signature as one reasoning item, not as a serialized
replacement history.

### Claude may remove a thinking-only assistant message

Claude 2.1.220 filters assistant messages containing only thinking or redacted-thinking
blocks when it considers them unpaired. The active bundle contains the
`tengu_filtered_orphaned_thinking_message` path at `/tmp/claude-2.1.220.js:20319`.

A carrier design using an assistant thinking block must account for that normalization. A
non-thinking text block in the same assistant message is one possible way to keep the
message, but that use has not been tested for native compaction.

### No live native-compaction bridge has been reproduced

The investigation has not yet demonstrated:

- recognition of a real Claude 2.1.220 compaction request in CLIProxyAPI;
- a successful `/responses/compact` call made from that request;
- translation of the returned history into a Claude response;
- persistence of that history in Claude JSONL;
- replay after restarting Claude and CLIProxyAPI;
- recursive compaction of an earlier native compact result.

Every process below remains provisional until those checks exist.

## Provisional process draft

### Proposed sequence

The current draft is:

```text
Claude Code
    sends H + CP
        │
        ▼
CLIProxyAPI
    recognizes a genuine compact request
    removes CP from the history sent to /responses/compact
    translates H to Codex ResponseItems
        │
        ▼
OpenAI /responses/compact
    returns candidate compacted history H'
        │
        ▼
CLIProxyAPI
    applies the required native-Codex-compatible post-processing
    serializes H' into a Claude-replayable provider-state block
    also returns nonempty text required by Claude 2.1.220
        │
        ▼
Claude Code
    appends compact_boundary
    appends wrapped summary/root text
    appends the provider-state block containing H'
```

On a later ordinary request, the draft translator would read the provider-state block from
the request itself, restore `H'`, omit the fallback summary and carrier label from the
OpenAI input, and append only messages after that compact generation.

CLIProxyAPI would not consult a persistent history table. The request replayed from JSONL
would contain every byte needed to restore `H'`.

### Candidate JSONL carrier

One candidate uses a standard assistant thinking block because original Claude instances
already persist and replay signed thinking state:

```json
{
  "type": "assistant",
  "message": {
    "role": "assistant",
    "model": "gpt-5.6-sol",
    "content": [
      {
        "type": "thinking",
        "thinking": "",
        "signature": "cliproxy:codex-compact:v1:<encoded-response-items>"
      },
      {
        "type": "text",
        "text": "[Provider-native compact history]"
      }
    ]
  }
}
```

This is only a candidate. Open questions include whether `thinking`,
`redacted_thinking`, a text envelope, or individually mapped messages are safer and more
portable.

### Required Claude writer change

The existing 2.1.220 compact path discards non-text response blocks. A candidate bundle
patch would retain one recognized provider-state block and place its assistant message after
the compact-summary user message.

Readable orientation suggests extending the compact result and installation order, but the
actual patch must locate and verify structural anchors in `/tmp/claude-2.1.220.js`. The
readable source is not an authoritative patch target.

An unmodified Claude reader should then be able to load the standard assistant message from
JSONL and include it in the next request. That has not yet been tested.

### Summary alternatives

Claude 2.1.220 still requires nonempty text. The draft has two unresolved choices.

#### Real fallback summary

Run ordinary summary generation and native compaction, potentially concurrently:

```text
H + CP                 → S
/responses/compact(H)  → H'
```

JSONL would contain both readable `S` and exact provider state `H'`. CLIProxy would use
`H'` for a compatible Codex route and `S` would remain available for recovery or a route
that cannot replay `H'`.

This costs another model operation but keeps the transcript useful without native replay.

#### Minimal required text

Return a short valid summary such as:

```text
Provider-native compact history is attached to this compact generation.
```

This avoids another summary-generation call. It also makes continuation depend on the
provider-state block and compatible translation. A direct route unable to decode that block
would not have a useful textual summary.

No choice has been accepted.

### Recursive compaction draft

If a later compaction request contains an earlier provider-state block plus a new suffix:

```text
carrier(H') + N + CP
```

CLIProxy would first decode the carrier from the request, reconstruct:

```text
H' + N + CP
```

remove `CP`, and call:

```text
/responses/compact(H' + N) → H''
```

Claude would append a new boundary, root, and carrier for `H''`. All earlier JSONL rows,
including the carrier for `H'`, would remain on disk. Active-lineage reconstruction would
exclude them semantically after the new boundary.

This recursive process has not been executed.

## Unresolved design questions

### Recognizing a genuine compact request

Matching English prompt text is vulnerable to false matches and user-supplied copies. The
2.1.220 full compact prompt also permits appended custom instructions.

Candidates include:

- an exact version-pinned prompt prefix and suffix check;
- additional structural checks on tools, message position, and query form;
- a small Claude patch that sends explicit trusted request metadata.

No recognizer has been selected.

### Representing the returned history

Unresolved choices:

- serialize the complete ordered `ResponseItem[]` in one opaque block;
- map user, assistant, function-call, and function-output items to ordinary Claude messages
  and use an opaque block only for the Codex compaction item;
- store a text envelope in the compact-summary root;
- use a thinking or redacted-thinking block.

Mapping individual items is easier to inspect but allows Claude normalization to merge,
move, or remove messages. One opaque block preserves exact provider ordering but requires a
new CLIProxy envelope decoder.

### Post-processing native output

Native Codex does not install the endpoint output without adjustment. The bridge must decide
which parts of `process_compacted_history` are required, including:

- removal or replacement of stale developer items;
- reinsertion of current initial context;
- treatment of user messages retained by the endpoint;
- model-switch context;
- current tools and parallel-tool settings;
- world-state or client-only items that Claude does not represent.

This must be settled from source and executable captures before choosing a carrier format.

### Full versus partial Claude compaction

Full compaction replaces the selected history with one compact root. Partial and reactive
compaction may preserve messages on one side of the summary. A bridge must know exactly
which portion `H'` replaces and must not duplicate preserved messages.

The first executable experiment should cover full compaction only. Partial and reactive
modes remain separate work.

### Model, provider, and credential changes

OpenAI compact and encrypted state may be limited to a model revision, provider route, or
credential identity. The replay rules and readable-summary fallback need controlled tests
for:

- model changes;
- provider changes;
- credential rotation;
- CLIProxy version changes;
- native compact format changes.

### Carrier integrity and hostile input

A user or edited transcript can contain carrier-like text or signatures. The decoder must
reject malformed, truncated, oversized, or unsupported payloads and must not expand an
ordinary user message merely because it resembles internal syntax.

It remains undecided whether provider authentication of the encrypted compact item is
sufficient or whether the carrier needs an additional locally verifiable envelope.

### Crash ordering

Append-only writes still need a defined order. A provisional safe order is:

```text
compact boundary
readable fallback summary
provider-state carrier
```

A crash before the carrier leaves the readable summary as the active fallback. A crash after
the carrier leaves both forms in JSONL. The actual writer batching and incomplete-final-line
behavior need a focused fault-injection test.

### Token and size accounting

The investigation has not measured:

- serialized `H'` size;
- base64 or envelope overhead;
- Claude local token estimation for the chosen block;
- Count Tokens behavior;
- prompt-cache effects;
- maximum safe content-block or JSONL-line size.

These results may determine whether one opaque block is practical.

## Required experiments before a decision

1. Capture a real Claude 2.1.220 full-compaction request and prove the exact `H + CP`
   separation used by the bridge.
2. Send the translated `H` to `/responses/compact` and retain the complete response.
3. Compare raw endpoint output with native Codex post-processing and installed history.
4. Define and implement one temporary JSONL carrier in a copy of the 2.1.220 bundle.
5. Restart with an original 2.1.220 binary and verify that it reconstructs and sends the
   carrier from JSONL.
6. Restart CLIProxyAPI with no retained in-memory state and verify that the request alone
   reconstructs `H'`.
7. Verify the next model request receives `H'` exactly once, with current system and tool
   configuration and without duplicate fallback summary content.
8. Run a second compaction and prove that `H' + N` becomes `H''` while every prior JSONL row
   remains unchanged.
9. Test resume, fork after compaction, fork before compaction, and rewind across the compact
   boundary.
10. Test model, provider, credential, and CLIProxy restarts.
11. Inject malformed, forged, truncated, and oversized carriers.
12. Kill the writer between boundary, summary, and carrier appends and verify deterministic
    recovery from the append-only log.
13. Measure request size, local token estimates, provider token usage, latency, and cache
    behavior.
14. Test partial and reactive compaction separately after the full-compaction process works.

## Non-decisions

This document does not decide:

- that CLIProxyAPI should recognize compaction prompts;
- that `/responses/compact` should replace ordinary Claude summary generation;
- that a thinking signature is the correct carrier;
- that a real fallback summary is required or unnecessary;
- that native compact state is portable across routes;
- that the proposed bundle patch is safe;
- that native compaction belongs in the immediate implementation work.

Until the experiments above succeed and the results are incorporated into accepted
architecture, decision D003 in [`ARCHITECTURE.md`](ARCHITECTURE.md#recorded-decisions)
remains the current position.
