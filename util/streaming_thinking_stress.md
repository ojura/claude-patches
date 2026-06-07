# Streaming-thinking stress test

## Purpose

Verify that the inline streaming-thinking restoration applied by
`util/patch_streaming_thinking.py --instr` survives **sustained streaming
load**, not just a brief flash of one or two `thinking_delta` events.

The chain (reducer call → `S4` setter → React prop down through `lxH` →
memo bypass → `QyA` re-render → `MH` useMemo recompute → aggregator merge)
runs through enough indirection that holding it through a single delta is
not informative. What matters is whether it stays correct under a real
high-frequency stream.

## Model selection: use Sonnet for the test claude

The claude session under test is what generates the streaming
thinking the patcher's instrumentation captures. The test asks a
known canonical prompt and grades a wiring property, not the model's
reasoning quality, so use Sonnet:

```
claude --model claude-sonnet-4-6
```

The thresholds below were calibrated against Sonnet on the
absorbed-writer build (interleaved canonical prompt at N=1..3). The
load-bearing assertions (`W1 ≥ 1` shows progressive writer is wired,
`broken_pairs == 0` shows propagation chain is intact) hold across
runs regardless of small Sonnet cadence variation.

## Prompt wording matters — both parts

The right stress needs both halves: a problem the model can't just
retrieve, AND an explicit instruction to derive rather than recall.
Either half alone fails:

- **Bare problem, no preamble.** Without an explicit "derive, don't
  retrieve" instruction, the model recognises the knight-on-infinite-
  board sequence as OEIS A118312 and answers from memory: a handful
  of `R2` setter calls, a couple hundred chars of thinking, total run
  ~20 seconds. The chain technically fires but doesn't sustain.
- **Preamble alone, easy problem.** "Think really carefully step by
  step" on a problem with a short closed form (factor a number, list
  primes) gives one-sentence thinking. The model complies with the
  preamble only to the extent the problem rewards it.

The combination — preamble + a problem whose answer the model has to
derive from constraints — is what produces sustained streaming.

## Canonical test prompt: interleaved multi-turn

```
Think through this combinatorial problem case-by-case. After
completing your reasoning for each value of N, immediately stop
thinking and write a single short visible sentence reporting the
count for that N. Then begin reasoning fresh for the next N — do not
carry over your previous thinking. Produce one short pause-sentence
per value, one per N from 1 to 3. Problem: Consider an infinite
chessboard. A knight starts at (0,0). After exactly N moves, how
many distinct squares can the knight be on?
```

Why this is canonical, not the simpler "Show your reasoning for each
step" form: the interleaved variant exercises the full streaming
state-machine cycle — E.3 (thinking content_block_start init) and
E.4 (text content_block_start snapshot) fire once per turn rather
than once per run, the renderer prop chain re-fires through the lxH
/ QyA / MH path on every turn boundary, and the absorbed writer
(E.7) runs through a fresh accumulator each turn. The simpler form
covers only the single-turn case and under-tests the lifecycle hooks.

**Why N=3, not N=5**: N=1..5 is empirically too slow — Sonnet
finishes it cleanly but takes 12+ minutes and stalls partway through
the later turns. N=1..3 drives all 3 turn boundaries in 4-6 minutes.

Models naturally produce 3 separate API turns for this prompt: each
turn has one thinking block followed by one text pause-sentence. If
a turn stalls (thinking but no text), the test is still valid for
the turns it completed — just compare metric counts against `actual
turns × per-turn thresholds`.

Substitute problems that share the same shape (combinatorial counting
on an infinite/large structure with a parity or symmetry constraint)
work equivalently, but only with an equivalent "stop thinking after
each case, write a one-sentence summary" preamble. Bad substitutes:
anything whose answer the model has memorised (famous proofs, well-
known sequences with short closed forms) — even the preamble doesn't
help because there's no derivation to do.

## Quick alternate: single-turn derivation

If you only need to verify the absorbed writer + propagation chain
fast (~30-60 s on Sonnet) and don't care about the state-machine
cycle, the single-turn form works:

```
Think really carefully before answering, working through the math
step by step: Consider an infinite chessboard. A knight starts at
(0,0). After exactly N moves, how many distinct squares can the
knight be on for N=1,2,3? Show your reasoning for each step.
```

Produces one thinking block and one text block — only verifies E.3
init and E.4 snapshot once each, but the load-bearing assertions
(W1 ≥ 1, broken_pairs == 0) hold the same way and per-delta volume
is comparable.

**Don't use the W1 `msgs > 1` metric to verify interleaving.** The
`__pfg_nextMsgs` array tracks blocks within a single streaming
response; it resets per API turn, so `msgs` stays at 1 across the
whole run even when interleaving works as designed.

## Pass thresholds

Calibrated against the 2026-06-07 absorbed-writer build (pristine
2.1.168 + connoisseur display + Patch S with --instr). Load-bearing
assertions are `broken_pairs == 0` (prop chain is wired) and `W1 ≥ 1`
(absorbed writer fires).

The thresholds below assume the **interleaved canonical** prompt at
N=1..3 on Sonnet. For the quick single-turn variant, multiply thread
counts (W1, R2, M2, E1, healthy_pairs) by ~1/3 since you only get
one turn instead of three.

| Metric | Threshold (interleaved N=1..3) | What a failure means |
|---|---|---|
| `W1` writer fires | ≥ 60 | Absorbed thinking_delta writer (E.7) not firing — A4 only sees finalized writes. **Load-bearing.** |
| `thinkLen` peak | ≥ 1200 chars | Either prompt too easy (model retrieved a closed form) or thinking not received |
| `R2 A4 t=function` | ≥ 60 | Total React useState setter invocations: should approximately equal W1 + finalized writes per turn. R2 ≈ W1 means all writes are progressive (good) |
| `M2` with `thinkE ≥ 1` | ≥ 60 | MH not folding streaming thinking into transcript |
| `E1` with `MH ≥ 1` | ≥ 60 | Aggregator dropping the thinking branch |
| `L1(n) → C2(y)` broken pairs | 0 | streamingThinking prop appeared at QyA without arriving at lxH — propagation bug. **Load-bearing.** |
| `L1(y) → C2(y)` healthy pairs | ≥ 30 | Prop chain propagating through memo enough times |
| `M1 stMsgs=0` transitions | ≥ 2 (= N-1 turn boundaries) | Each new API turn re-initializes streamingThinking; counts boundaries between turns. If < N-1, the model didn't actually interleave |
| Distinct `blockIndex` for `blockType=thinking` | ≥ 3 | One content-block index per thinking block; confirms a fresh thinking block per turn |
| Distinct `blockIndex` for `blockType=text` | ≥ 3 | One content-block index per text block; lower if the model stalled before producing some pause-sentences |

The last three rows (M1 transitions + distinct blockIndex per type)
are the interleaved-only signals. They detect whether the model
actually interleaved across turns or collapsed into a single response.

## How to run

1. Confirm the live binary is the `--instr` build (rebuild from pristine
   if needed):
   ```
   util/patch_streaming_thinking.py /path/to/display-patched.bin --instr \
     -o /home/juraj/.local/share/claude/versions/<VER>
   ```
2. Start `claude` in tmux, **specifying Sonnet** so the test response is
   cheap:
   ```
   tmux send-keys -t <window> 'claude --model claude-sonnet-4-6' Enter
   ```
   No `--allow-dangerously-skip-permissions` or `--permission-mode
   bypassPermissions` needed: the test sends a math prompt and reads
   instrumentation logs from `/tmp/`. No tool use, no file edits.

   Record its PID (find the one whose `/proc/<pid>/exe` resolves to the
   installed `--instr` build, not other concurrent claudes).
3. Truncate the per-PID log so the run measurement is clean:
   ```
   : > /tmp/pfg-instr.<PID>.log
   ```
4. Paste the canonical prompt above and submit.
5. Wait for `✻ Cooked/Crunched/Baked for Xs` to settle and the prompt to
   return. **Always use a fresh claude session per test** — the prompt
   cache + in-session context will short-circuit thinking on repeat
   sends.
6. Grade the resulting log:
   ```
   util/check_streaming_log.py /tmp/pfg-instr.<PID>.log
   ```
   Exit 0 = all thresholds met. Exit 1 = at least one row failed; the
   table shows which.

## Dispatching a test run via subagent

If you're delegating the run to a Sonnet subagent (the usual cheap-
orchestration pattern), two specific footguns to avoid:

**Newline-as-Enter in `tmux send-keys`.** Bash multi-line strings get
sent VERBATIM to tmux — each embedded newline becomes a literal Enter
keypress in the target pane. A naive `tmux send-keys -t 3 "$PROMPT"`
where `$PROMPT` is a multi-line variable will submit the first line
prematurely, then submit each subsequent line as if it were a fresh
turn. The correct form: collapse the prompt to one literal line in
the bash string, send it as a single argument with no embedded
newlines, then send `Enter` as a separate send-keys call. Example:

```bash
PROMPT='Think through this combinatorial problem case-by-case. After completing your reasoning for each value of N, immediately stop thinking and write a single short visible sentence reporting the count for that N. Then begin reasoning fresh for the next N - do not carry over your previous thinking. Produce one short pause-sentence per value, one per N from 1 to 3. Problem: Consider an infinite chessboard. A knight starts at (0,0). After exactly N moves, how many distinct squares can the knight be on?'
tmux send-keys -t 3 "$PROMPT"
sleep 1
tmux send-keys -t 3 Enter
```

NOT:

```bash
# WRONG — heredoc/multi-line interpretation will hit Enter mid-string
tmux send-keys -t 3 'Think through this combinatorial problem case-by-case.
After completing your reasoning for each value of N...'
```

And NOT re-typing `claude --model claude-sonnet-4-6` after the prompt
has been entered (a recurring agent error: agents that hold both
"start claude" and "send prompt" in their step list will sometimes
re-issue the start command at the wrong moment, polluting the prompt
input). After the prompt is typed, the only remaining action is
sending `Enter`.

**Use Monitor + periodic capture-pane, not blocking bash-until.** A
blocking `until tmux capture-pane ...; sleep 15; done` inside a Bash
call ties up a tool slot for the full run duration (often 3-10 min)
and gives zero in-flight visibility. The agent can't notice when the
prompt was contaminated, when claude is sitting at an unexpected
state, or when the model is stalling. The right shape:

1. Send the prompt
2. Arm Monitor with the completion-detection grep (timeout 600 s),
   `persistent: false`
3. Between dispatching Monitor and the completion event, do a few
   foreground `tmux capture-pane -t <window> -p | tail -20` checks
   (one ~30 s in to verify the prompt looks right, one ~90 s in to
   sanity-check progress, one near the expected completion time).
   If anything looks wrong, abort via TaskStop on the Monitor + a
   send-keys Escape to claude; don't wait the full timeout

The cost of periodic capture-pane is one extra tool call per check;
the benefit is catching dispatch contamination or model stall before
wasting the full 5-10 min run. Periodic inspection is cheap and
diagnostic-rich; blind blocking is neither.

## Reading the result

A `PASS` on `W1 ≥ 1` AND `broken_pairs == 0` is the minimal
load-bearing success — the absorbed progressive writer is firing AND
the prop chain is wired end-to-end. Volume metrics (thinkLen, R2,
M2/think, E1/MH, healthy_pairs) are advisory; they vary with prompt
difficulty, model choice, and Anthropic-side streaming cadence.

A `FAIL` on `W1 == 0` with `broken_pairs == 0` means the renderer
chain is wired but no progressive writes are coming through. Most
likely the absorbed writer (E.7) didn't land or anchor-drifted on
this version. Check the patcher's apply log for E.7 status.

A `FAIL` on `broken_pairs ≥ 1` means the propagation chain is wrong
even if the totals look healthy — **load-bearing**.

A `FAIL` on the M1-transitions / distinct-blockIndex rows with the
volume rows passing means the model didn't actually interleave —
either it collapsed to one streaming turn or it stalled after one
turn. Verify by reading the tmux session; if the model produced only
one thinking block + one text block, the interleaving instruction
didn't land. Try a sterner phrasing or a less complex problem.

A `FAIL` on every row simultaneously usually means the binary is not
the instrumented build (no logs are being written) or the `claude`
PID you graded against is the wrong one.
