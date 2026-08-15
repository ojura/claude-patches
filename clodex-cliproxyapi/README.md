# Clodex + CLIProxyAPI

Durable architecture, evidence, and localhost reliability probes for the Claude Code
2.1.220 → CLIProxyAPI → Codex integration investigation.

## Start here

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — current system model, evidence taxonomy,
  findings, decisions, rejected designs, rollout ladder, and remaining work.
- [`CLASSIFIER.md`](CLASSIFIER.md) — Claude auto mode, Codex Guardian, direct
  CLIProxyAPI reviewer translation, responsibility boundaries, cache behavior, and
  regression checks.
- [`NATIVE_COMPACTION.md`](NATIVE_COMPACTION.md) — provisional native-compaction
  investigation; separates verified observations from unfinished process drafts and is
  not an accepted architecture decision.
- [`ARTIFACTS.md`](ARTIFACTS.md) — provenance, evidence classification, exclusions,
  retention gaps, and reproduction constraints.
- [`reports/corpus-absorption-checklist.md`](reports/corpus-absorption-checklist.md) —
  finding-by-finding map from consolidation corrections to durable destinations and
  explicit open items.
- [`evidence/claude/midturn-steering-order.json`](evidence/claude/midturn-steering-order.json)
  and [`evidence/cliproxy/direct-midturn-translation.json`](evidence/cliproxy/direct-midturn-translation.json) —
  canonical focused evidence for queue/normalization behavior and direct translation.
- [`reports/midturn-steering-direct-path.md`](reports/midturn-steering-direct-path.md) —
  absorbed focused narrative retained for readable sequencing.
- [`SHA256SUMS`](SHA256SUMS) — integrity manifest for the durable current corpus.

## Layout

```text
clodex-cliproxyapi/
├── ARCHITECTURE.md
├── ARTIFACTS.md
├── CLASSIFIER.md
├── NATIVE_COMPACTION.md
├── README.md
├── SHA256SUMS
├── reports/                 # dated check reports and founding-evidence gaps
├── evidence/
│   ├── claude/              # focused Claude Code captures and summaries
│   ├── cliproxy/            # focused CLIProxy captures and logs
│   └── provenance/          # source/binary pins and instrument hashes
├── fixtures/                # captured cloaked, non-secret model catalog
└── probes/
    ├── README.md
    ├── claude/              # localhost Claude Code reproduction scripts
    └── cliproxy/            # localhost CLIProxy scripts and focused Go tests
```

## Retention policy

This directory keeps the smallest durable corpus available to understand or reproduce
a finding. It intentionally excludes credentials, temporary binaries and worktrees,
per-run config/session directories, large source trees, and many raw request histories.

Not every excluded file was redundant. Some founding captures, request orderings,
instrument patch recipes, and per-delay results were lost or deliberately omitted. The
architecture, artifact index, founding-evidence report, and probe README name those gaps
rather than treating report prose as a raw capture.

All retained credentials and IDs are synthetic fixture values such as `test`,
`token-one`, and fixed UUIDs. The model fixture is a faithful capture of CLIProxy's
cloaked model-list representation; it is not a plain-ID production catalog.

## Writing and evidence style

Use plain descriptions of the mechanism, consequence, and remaining uncertainty.
Define internal labels such as `CCR` at first use. Avoid rhetorical labels, invented
terms, and commandment-style wording where ordinary engineering language is clearer.

Rewriting must preserve the evidence class in both directions:

- source-confirmed behavior is not promoted to an executable result;
- executable evidence is not softened into a runtime possibility;
- runtime or provider questions remain identified as open;
- supporting details and consequences stay with the claim instead of being removed for
  brevity.

`ARCHITECTURE.md` is the self-contained system statement. `CLASSIFIER.md` is the
focused permission-reviewer statement. `NATIVE_COMPACTION.md` is explicitly provisional
and does not amend either one. Durable conclusions must be written in accepted documents
or in indexed reports and evidence, not depend on temporary consolidation working files.

## Verification

From this directory:

```sh
sha256sum -c SHA256SUMS
```

The probes are historical reproductions, not a turnkey suite. Several require a
specific pinned CLIProxy revision, third-party Python modules, reconstructed localhost
stubs, preserved `/tmp` state, or structurally recreated instrumented Claude binaries.
Read [`probes/README.md`](probes/README.md) before running them.
