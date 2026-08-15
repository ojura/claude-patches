# Independent review of the Clodex + CLIProxyAPI corpus

- **Date:** 2026-08-05
- **Reviewed state:** the pre-fold 44-file corpus under
  `~/claude-patches/clodex-cliproxyapi`
- **Method:** six independent audits covering Claude evidence, CLIProxy evidence and
  probes, documentation consistency, integrity and provenance, architecture and source
  grounding, and transcript completeness
- **Audit reports:** [Claude evidence](audit-claude-evidence.md) ·
  [CLIProxy evidence](audit-cliproxy-evidence.md) ·
  [documentation consistency](audit-doc-consistency.md) ·
  [integrity](audit-integrity.md) ·
  [architecture](audit-architecture.md) ·
  [transcript completeness](audit-transcript-completeness.md)

> **Historical record.** This review describes the corpus before its findings were
> folded into [`../ARCHITECTURE.md`](../ARCHITECTURE.md),
> [`../ARTIFACTS.md`](../ARTIFACTS.md), the probe documentation, provenance files, and
> the regenerated integrity manifest. Citations below therefore use pre-fold line
> numbers. The individual audit reports are retained as the original review record and
> may use stronger language than the current architecture document.

## 1. Summary

The corpus was generally consistent and well preserved:

- all 43 manifest hashes verified;
- all 24 artifacts that still had `/tmp` originals were byte-identical;
- numeric cross-checks agreed;
- the superseded-claims table was consistent across the corpus;
- the decision register did not promote source findings into executable evidence;
- eight sampled source-confirmed claims matched the pinned Claude bundle;
- the probes used isolated local fixtures more consistently than the initial README
  described.

The main recurring issue was evidence strength. Several claims were written as
executable reproductions even though the retained support was a source reading, two
separate controls, report prose, or a file outside the durable corpus. The review also
found one missing replay-compatibility field and one omitted context-management topic.

## 2. Findings that required architecture changes

### 2.1 The replay compatibility digest omitted the target model

The original definition of **C**, its list of current fingerprint omissions, and the
replay verification checks did not explicitly include the target model. A minimal
implementation following those lists could therefore treat otherwise-identical Luna
and Terra requests as compatible, even though cross-model stale precompute had already
been reproduced.

The current architecture now includes the target model in **C** and in replay checks.

### 2.2 Context-management behavior was missing

The original corpus did not describe the following source-confirmed behavior:

- Claude Code 2.1.220 emits `clear_thinking_20251015` with `keep:"all"`;
- there is no production `clear_tool_uses_20250919` emitter in this client version;
- Claude-to-non-Claude translators drop the context-management fields;
- direct-Anthropic cloaked routes may add clear-thinking metadata;
- final handling belongs after payload rules and before replay or egress.

This material is now included in `ARCHITECTURE.md` with its source-confirmed evidence
class.

### 2.3 Founding evidence was incomplete

Three early experiments were described in the architecture without retaining all of
their original raw captures:

1. the admission-boundary search around 10,062/10,070 requested output tokens;
2. the custom-base predicate matrix (`Hn`, `dGr`, `Yd`, and related predicates);
3. the replacement crash-window sweep.

The conclusions remain useful, but the current corpus now states which raw evidence is
missing and avoids presenting report prose as a retained capture.

### 2.4 CLIProxy evidence used two versions

The Go/source lane and the late-frame executable lane used different CLIProxyAPI
versions:

```text
Go/source lane:
    commit a88197f845c979132c8978ea223c6af05cc81536
    tag v7.2.116

Late-frame binary lane:
    version 7.2.112
    commit a63da8ae
```

The current corpus records both lanes and requires each claim to identify which one
supports it.

## 3. Evidence-class corrections

The review required these narrower descriptions:

- Cross-caller and cross-auth replay sharing is confirmed by retained upstream source
  tests, not by a locally retained end-to-end run.
- Credential-refresh behavior is supported by two executable controls plus
  source-confirmed service wiring, rather than one complete watcher-to-request test.
- The replay probe contained six local functions; the two sharing claims came from
  upstream tests. The non-verbose executor log could not independently show every
  passing test name.
- The persisted-output fork probe used a stub-authored wrapper and a probe-planted
  artifact. It reproduced row copying and a dangling parent-owned path, but it did not
  exercise the genuine replacement-ledger fork path.
- The tombstone interruption used explicit instrumentation and a ten-second pause to
  widen the truncate-to-rewrite interval. It demonstrates the non-atomic consequence,
  not the natural production timing.
- The retained physical-GC negative JSON did not contain enough data to establish the
  attempted branch-forcing interpretation on its own.
- Cross-model precompute evidence depended on a request-order capture that was not
  retained.
- ScheduleWakeup size fields historically named `description_bytes` were Python string
  character counts.

These corrections are now recorded in the architecture and artifact index.

## 4. Findings recovered from the session record

The review found useful material that had not yet reached the corpus:

- Codex's approximate 90% automatic-compaction and 95% usable-window policy
  (334,800 and 353,400 for a 372k context);
- Codex tool-output, prompt-cache, and continuation behavior used as comparison points;
- the reactive/auto-window route that can bypass the local hard-admission check;
- the `response.incomplete` policy difference between CLIProxy and native Codex;
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW` behavior;
- offline baked-catalog, transport-equivalence, and Codex-math acceptance checks;
- the production `Oe=0` fact;
- non-Codex replay scope exclusions;
- concrete 64-MiB shell-output truncation;
- torn final-line rewind-marker loss;
- Responses-WebSocket fresh-`response.create` history inheritance.

These items are now present in the current architecture or remaining-work sections.

## 5. Architecture questions raised by the review

### 5.1 Durable-state dimensions needed a producer and transmission path

The original document defined **D/R/A/S** but did not state how those values would be
committed or made available to the proxy. The current architecture now states that
Claude produces those states and that stateful continuation cannot rely on them until a
versioned history/artifact/lineage record is committed with the corresponding JSONL
change.

### 5.2 Continuation needed a virtual full-request definition

An abbreviated `previous_response_id` request still represents a complete semantic
request. The current architecture now evaluates continuation against a virtual full
request and keeps retained provider state as optional transport data rather than a
second history source.

### 5.3 Replay needed age and provider-revision limits

Exact request bytes do not establish compatibility across silent provider revisions.
The current architecture now requires a bounded replay age and includes a provider or
model revision in **U** when one is available.

### 5.4 Widening the input envelope needed an explicit rollout note

Rungs 0–2 widen the admitted range while durability and precompute behavior remain
unchanged. The current rollout therefore keeps precompute and context hints disabled
initially and uses conservative warning and compaction thresholds until the later
prerequisites are implemented.

### 5.5 The greater-than-200k branch needed a precise description

The earlier nickname for this branch did not identify the code accurately. The branch
uses total recorded usage and controls a plan-mode model upgrade; it is separate from
the input-only admission calculation. The current document describes it by symbol and
behavior instead.

## 6. Reproduction and documentation repairs

The review identified these maintenance issues:

- `tombstone_removal.py` depended on a manually renamed rewind artifact and live
  temporary state;
- three retained evidence files did not have a generating probe;
- several instrumented binaries remained in `/tmp` without hashes or complete forcing
  recipes;
- the retained model fixture was a real cloaked CLIProxy catalog, not a synthetic
  plain-ID catalog;
- `websocket_late_frame.py` did not explicitly bind the proxy to loopback;
- the probe README used the wrong relative `bun_handler.py` path and omitted some
  dependencies;
- the replay probe needed non-degeneracy and post-fix assertions;
- several claims lacked direct `Capture:` links;
- D019 and D027 duplicated the same ambiguous-write decision.

The fold corrected the paths and listener binding, documented the instruments and
fixture cloaking, added missing provenance, merged duplicate decisions, and expanded
`SHA256SUMS` to include the review and new artifacts.

## 7. Confirmed strengths

The review found no numeric contradictions between the session record and the corpus.
Most investigation goals and findings mapped cleanly into the architecture or its
remaining-work lists. In particular:

- the retained header-precedence test exists;
- partial-write, late-frame, and stale-handshake behavior also matches the pinned
  CLIProxy source lane;
- the anchor-aware `Oe` partition is distinct from the rejected single shared scalar;
- no accepted implementation rung depends on an excluded design;
- the installed Claude binary hash had not changed;
- retained traffic targets were loopback-only after the listener correction;
- the secret scan found only synthetic fixture values;
- rung numbering and the superseded-claims table were internally consistent.

## 8. The 10.3k `/context` value

The review left one display calculation unresolved; follow-up source inspection
established it.

With auto-compaction disabled, Claude Code uses a flat 3k manual-compaction buffer in
the `/context` display:

```text
raw context:       372.0k
projected request: 358.7k
manual buffer:       3.0k
shown free space:   10.3k
```

The separate 20k admission reservation does not enter that display branch. Admission
therefore used a 349k hard limit while `/context` still showed 10.3k free:

```text
displayed free: 372.0k - 358.7k - 3.0k  = +10.3k
admission room: 349.0k - 358.7k          =  -9.7k
```

This is source-confirmed. It explains why a one-character prompt could be blocked even
though the context display showed positive space.

## 9. Review process record

Six auditors ran independent source, evidence, consistency, provenance, architecture,
and transcript-completeness passes. Some auditor processes stopped at an account
session limit and later resumed from their transcripts; their completed reports are
retained in this directory. The lead rechecked all high-priority findings and selected
medium-priority findings against files, source offsets, source trees, or the session
record.

The review itself initially changed no corpus files. The subsequent fold applied its
findings to the architecture, artifact index, probes, reports, provenance files, and
integrity manifest.
