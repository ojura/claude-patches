# Founding experiments and retained-evidence status

Date: 2026-08-05

This report closes the corpus gap where several founding results were stated in
`ARCHITECTURE.md` without a durable capture, probe, or explicit evidence-class
correction. It preserves what is known, the surviving reproduction material, and the
remaining provenance gap.

## Admission-reservation boundary search

Evidence class: **executable-reproduced; original console capture not retained**.

The original local Luna/loopback experiment established:

- configured output `32,000`: filler `520,312` passed and `520,507` blocked;
- configured output `128`: filler `550,195` passed and `550,390` blocked;
- at the fixed midpoint, output `10,062` passed and `10,070` blocked;
- source arithmetic predicted a boundary of `10,064`.

The eight-token output bracket is consistent with, but does not alone isolate, the
predicted value. The source independently fixes the policy as:

```text
reservation = min(configured max output, 20,000)
hard boundary = raw input - reservation - 3,000
```

The surviving probe is
[`../probes/claude/admission_boundary.py`](../probes/claude/admission_boundary.py).
It requires the localhost admission stub that returns `ADMISSION_PASSED_STUB`; that
stub was not retained and must be reconstructed before a full rerun.

## Custom-base classification

Evidence class: **source-confirmed plus exact-function execution reported in the
session; raw execution capture not retained**.

For `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`, the audited 2.1.220 predicates
resolved to:

```text
Hn  = firstParty
Dc  = true
rm  = true
dj  = true
dGr = false
Yd  = false
```

`_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL` changes only `Yd` to true. The result
supports preserving Anthropic Messages transport while separately controlling direct-host
product assumptions. It must not be cited as a retained raw executable capture.

## Persisted-output replacement crash window

Evidence class: **executable-reproduced; report-derived summary retained, per-delay
raw captures not retained**.

A roughly 1.9-MiB Bash result was replaced with a 2,276-character wrapper. After the
model-facing wrapper request had reached the localhost stub:

| Kill delay | Fresh-process reconstruction |
|---:|---|
| 0 ms | entire assistant/tool interaction absent |
| 10 ms | absent |
| 25 ms | absent |
| 50 ms | absent |
| 100 ms | wrapper reconstructed |
| 200 ms | wrapper reconstructed |
| 500 ms | wrapper reconstructed |

The observed result was omission of the entire turn, not restoration of the original
raw output. The surviving probe is
[`../probes/claude/replacement_crash_window.py`](../probes/claude/replacement_crash_window.py),
and the durable report-derived summary is
[`../evidence/claude/replacement-crash-window-summary.json`](../evidence/claude/replacement-crash-window-summary.json).

## Evidence discipline

These results remain architecture inputs, but claim sites must state their actual
retention status. Report prose is not a substitute for a raw capture, and a source
confirmation is not an executable reproduction. Future reruns should retain verbose
console output and per-delay JSON results directly in the project corpus.
