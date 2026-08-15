# Subagent report: audit-transcript-completeness

- **Beat:** completeness — the session transcript ee749a2d (2,232 lines, 2026-08-04→05) plus its pre-compaction backup vs what the corpus actually recorded. What the session found but the corpus silently dropped.
- **Delivered:** failed on account session limit at ~13:18 UTC, self-resumed after the reset, delivered 2026-08-05 ~18:44 UTC.
- **Lead verification:** findings 1, 3, 5 re-verified by the lead against corpus vs transcript grep (confirmed absent from corpus; present in transcript — clear_thinking 35 hits, AUTO_COMPACT_WINDOW 31 hits, reactive bypass absent from ARCHITECTURE/reports).

Verbatim report as delivered:

---

COMPLETENESS AUDIT — transcript ee749a2d (2,232 lines, 2026-08-04T16:19Z → 2026-08-05T11:57Z) vs /home/juraj/claude-patches/clodex-cliproxyapi. Transcript is self-contained: first record is the original user ask; the .bak-pseudocompact is a byte-exact prefix of the live file (sha256 verified), so both in-file compact events (line 861 summary, line 1514 pseudocompact) lost no history. SHA256SUMS verifies clean.

FINDINGS

1. Severity HIGH; Class OMISSION — The entire `context_management`/`clear_thinking` finding cluster is absent from the corpus.
Transcript L655 ("I captured the actual Claude Code 2.1.220 request... context_management: edits: [{type: clear_thinking_20251015, keep: all}]... beta header context-management-2025-06-27"), L699 ("Claude Code sends declarative context management, and CLIProxy ignores it"; "clear_tool_uses_20250919... no built-in executable emitter"; "keep: all... non-shrinking"), L720 (adversarial audit CONFIRMED all of these claim-by-claim), workflow-1 agent [6] verdict ("production query path emits exactly one context-edit form: {edits:[{type:"clear_thinking_20251015",keep:"all"}]}"), and workflow-2 [5] + parent analysis L1497 §16 ("All five Claude→non-Claude translators silently drop both context fields"; "CLIProxy can synthetically add [clear_thinking keep:all] to every cloaked direct-Anthropic request"; "payload rules can re-add them after translation"; enforcement must be "after payload rules, immediately before replay/egress").
Corpus checked: grep of ARCHITECTURE.md, ARTIFACTS.md, reports/, probes/README for clear_thinking / clear_tool_uses / compact_20260112 / context_management-as-mechanism / EXTRA_BODY / cloak+context — zero hits beyond generic "context controls" (§1061) and "context-management fields" in passing (§617). Not in the Superseded table (it was never superseded — it was confirmed twice).
Why it matters: this was the answer to the user's central mid-session question ("What context_management is sent by Claude?... Looks like all we need is to wire that in", L638/L660/L668), and the doc's stated purpose is to stop later work from rediscovering exactly this. A reader today cannot learn from the corpus that Claude's only built-in context edit is a no-op, that clear_tool_uses has no emitter in 2.1.220, that CLIProxy both drops and synthetically injects the field route-dependently, or where context-control enforcement must sit.

2. Severity MED-HIGH; Class OMISSION (partial UNRESOLVED-GOAL) — The Codex-side analysis, one third of the original ask, left no durable trace beyond "Codex intervenes earlier" (§393) and the rejected hidden-compact design (§80).
Transcript L4 (goal: "analyze how context size is managed by Codex... and what the model limits are"), L627 ("automatic compaction: 90% of C; usable full window: 95% of C... Codex's Responses request does not contain max_output_tokens at all... 334,800 / 353,400" for 372k), L222 (verified path:symbol facts incl. COMPACT_USER_MESSAGE_MAX_TOKENS=20,000), L655/L226 (tool-output lifecycle: 10k-token model-visible cap, 1 MiB retained raw exec cap, truncation markers, orphan repair; "session-derived prompt_cache_key"; WebSocket incremental-suffix continuation; "native Codex mechanisms... should be reused rather than reimplemented").
Corpus checked: grep 90%/95%/max_output_tokens/334/353/prompt_cache_key/1 MiB/10k — zero hits anywhere.
Why it matters: the corpus repeatedly says Claude's thresholds "are not proof that they are optimal operational policy" and that "Codex intervenes earlier" — but the benchmark numbers and mechanisms that statement rests on now exist nowhere durable.

3. Severity MED; Class OMISSION — The reactive/auto-window-route local-admission bypass is undocumented.
Workflow-1 journal (wf_e6eeb5ab), items [0] and [9], both confidence "high": "admission is not a single preflight wall. It is estimate-based, can be bypassed for the reactive configuration"; "the local admission guard is deliberately bypassed in the reactive/auto-window route" (adversarial completeness agent).
Corpus checked: grep bypass/reactive-near-admission in ARCHITECTURE.md + reports — nothing; Rung 1 (§373) and Profile/admission gates (§1330) cover only vSe/Fny and the >200k credit-latch branch.
Why it matters: Rung 1 patches vSe/Fny and its gates test those two sites; a route where the hard-block guard doesn't apply at all is exactly the kind of coverage caveat the gates section exists to hold.

4. Severity MED; Class OMISSION — CPA's `response.incomplete`-as-success decision conflict is dropped.
Workflow-2 journal (wf_b3e564fc) item [4], confidence high: "CPA intentionally treats Codex response.incomplete as a successful truncated response despite the Codex reference treating it as a stream failure" (codex_executor_execute.go:164-182, codex_executor_stream.go:172-200), flagged as needing "explicit resolution before changing retry or continuity behavior". Its sibling finding (auto-replay of ambiguous WS sends) IS documented (§1012/D027) — this half vanished.
Corpus checked: grep incomplete — only "runtime-incomplete" (§1417).
Why it matters: it is a Rung 6 retry/continuation precondition of the same class as D027, and neither the decision register nor the transport sections record it.

5. Severity MED; Class OMISSION — `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (named in the original ask, set to 372000 in the live profile, quantified in-session) is absent from Rung 0.
Transcript: zero-patch-options report L840 ("Active teammate process env already has... CLAUDE_CODE_AUTO_COMPACT_WINDOW=372000"), plus worked examples elsewhere in the transcript ("CLAUDE_CODE_AUTO_COMPACT_WINDOW=352000... auto-compact threshold: 319,000" / "=372000 would produce: auto-compact threshold: 359,000; local hard block: 369,000") and "Do not set DISABLE_AUTO_COMPACT, because it overrides that".
Corpus checked: grep AUTO_COMPACT — Rung 0 (§337) configures only MAX_CONTEXT_TOKENS and MAX_OUTPUT_TOKENS.
Why it matters: Rung 0 is "coherent profile"; the documented profile omits one of the three context env knobs the real deployment sets, and the only no-patch mitigation lever the session quantified.

6. Severity MED; Class OMISSION — Three designed acceptance-suite gates never reached the Verification model: (a) offline Codex baked-catalog gate ("the inspected Codex baked catalog still reports 272k, so remote catalog success alone cannot validate a 372k deployment", L854 — highlighted again by the in-file compact summary L861); (b) tri-transport equivalence oracle ("HTTP JSON, HTTP SSE, and Responses WebSocket must normalize to the same semantic request/error/usage trace", L847 oracle 6); (c) separate Codex-math oracle (353,400/334,800 as distinct exposed fields, L847 oracle 4).
Corpus checked: §1317-1393 gates; grep baked/offline/272 (only the GPT-5.5-route line §341, a different fact).
Why it matters: these were the ladder's rollout criteria; §341 does not substitute for the catalog-validation gate.

7. Severity LOW-MED; Class OMISSION — Non-Codex provider replay findings dropped without a scope note: xAI replay reinserts cached reasoning/message/tool state after Claude local compaction removes the assistant anchor (L1497 §17; wf2 [8] "strongest is xAI replay"), and CLIProxy's "Persistent Home KV replay" state (L1497 executive list). Corpus checked: grep xAI/Gemini/KV — nothing, and no "Codex-route-only" exclusion statement in README/ARCHITECTURE scope text.
Why it matters: defensible scope trim, but it is silent — the retention policy enumerates excluded bytes, not excluded findings.

8. Severity LOW-MED; Class OMISSION — The production fact that `snipTokensFreed`/`Oe` is hardcoded to zero (only production caller `let Oe = 0`, bundle L3853; earlier byte 7674678; confirmed by the adversary L720 and wf1 [9]) is not stated. §929 only rejects the future generic-Oe design; §198 supersedes the one-scalar proposal.
Why it matters: without it a reader can believe 2.1.220 currently credits hint savings during admission; the session proved it never does.

9. Severity LOW; Class OMISSION — Bash persisted-output handoff truncates the saved file at 64 MiB while advertising the original size (durability workflow item [7], "most serious verified defects... shell handoff additionally advertises original output size after truncating the saved file to 64 MiB"). Corpus has the generic manifest decision (§700, "declared original size; truncation state") but not this concrete verified instance.
10. Severity LOW; Class OMISSION — JSONL torn-line handling can lose a rewind marker and fall back to retained history (durability item [3] verdict). Distinct from the withdrawn successor-marker defect (§194/D025) and from the ordinary write window; not in durability sections or gates.
11. Severity LOW; Class OMISSION — No probe or evidence file for the founding executable result: the reserve-equation binary search (10,062/10,070/10,064; 535,351-filler midpoint; Luna local stub). Documented as §234, but probes/claude/ retains scripts for every other executable claim, and ARTIFACTS.md flags missing files elsewhere (0-ms rewind note) while this absence is unflagged.
12. Severity LOW; Class OMISSION — The downstream Responses-WS HTTP-mode defect (fresh `response.create` without previous_response_id silently inherits merged prior input/output + pending tool state, L1497 §8 with file:line) survives only as its fix-gate (§1392). No defect narration anywhere; acceptable-but-thin given every comparable transport defect has a narrated section.

DISCREPANCIES: none found. Spot-checked numbers all match transcript/retained reports: 402,116/253,221/402,241; 29,273→9,020; 65,536/32 MiB; 2,933/2,685/2,734; 1,849/1,777/1,804; 336.2k/358.7k/10.3k (user's "339k/372k" was paraphrase — the /context capture says 336.2k, and 339k correctly appears as the auto-compact threshold §389); 10,062/10,070/10,064; 2,276 B/1.9 MiB; 0/10/25/50/100/200/500 ms; rewind 0/100/500/1000 ms with the 0-ms file absence honestly noted in ARTIFACTS. 53,069,996→53,071,918 appear only in the retained outstanding5 report (the fork that ran the test), not the parent transcript — consistent, not a mismatch. Bundle size 21,636,716 B matches wf1 [9] mission line.

COVERED (method proof; sample of the mapping)
- Origin failure 336.2k/372k, Bash 82.3k/Read 145.8k, reserve=min(out,20k), vSe+Fny both → §200-252.
- User "349 is not hard block" challenge → §221-232 (349k derivation).
- Binary-search proof incl. midpoint triple → §234-241.
- "form a global cohesive plan... organize by patching rungs" (L738) → rollout ladder §280 + per-rung sections; fork's rung-mapping table (L1970) → §312 Historical rung mapping.
- "11 - no, treat 2.1.220 as authoritative" (L1077) → §34.
- Auto-compaction re-enabled steer (L1642) → §343.
- 128k/32k telemetry contradiction → §421 + retained report §87.
- Workflow-1 (CLIProxy) 16-section analysis (L1497): §§1-7,9-15 all map (→ §449-535, §970-1031, §1047-1080, §1082-1121, D009-D013, D026-D027, replay/transport gates).
- Workflow-2 (durability) journal spot-reads: duplicate-assistant recovery → §697; CCR destructive resume/dup-sequence/epoch-queue-loss → §816-830 + §1252-1259; EEXIST-without-verify → §712; session-deletion/relocation lifecycle → §700-717.
- Mutation matrix (L1695, 16 rows) → distilled across §112-166 planes/dimensions + per-mutation sections; adversary "needs narrowing" corrections (L1713/1761: custom-header /clear pin, stale same-UUID precompute, physical-GC changes R/S, two-IDs-insufficient) → §537, §874, §1228, §155-166.
- ScheduleWakeup TTL cluster incl. qLo memoization + eFe precedence + deferred seam (L1849/1895/1898) → §566-601 + retained report + evidence pair.
- CCS bundle folds + 3 corrections (L1894/1935: route-dependent effort, uZc, cloaking) → §347-371, §1123-1152, §196-197, §1154-1175.
- Adversary's final ARCHITECTURE deltas (L2158/2163): all incorporated in substance (ambiguous-execution §1027/D027; U expansion §148/§1006; demux §983; tombstone postcondition §734; rewind narrowing §194/D025; GC narrowing §784/D031; background barrier §761/D030; CCR bounded §816/D033; dependency order §295) — only the three-state retry enumeration survives as prose rather than the explicit taxonomy.
- Fork remaining-work list (L2053) → §1415-1444 nearly verbatim, incl. the "no longer open" list and rewind withdrawal; task graph #3→#1→#2 (L2078) → §1546 Current work queue; "always keep ARCHITECTURE.md up to date" (L2119) → §1566 + this corpus's existence (L2169).
Inventory counts: 157 human text turns extracted (of 517 user-type records); ~48 substantive goal/steer turns; ~50 concrete findings tracked from assistant syntheses + 4 workflow journals (30 agent verdicts read, 6 deep-read). ~40/50 findings and 45/48 goals map cleanly to corpus locations or in-session resolution; the remainder are Findings 1-12 above.

GAPS
- Workflow journals total ~570 KB; I read all 30 per-agent verdicts and deep-read 6 reports (wf1[0][6][7][9], wf2[4][5], durability[3][6][7] partially). Unread report bodies could hide further sub-findings of classes I've already flagged, but every verdict line was checked.
- 358 files in tool-results/ and 35 subagent transcripts were not exhaustively read; fork-owned work (/btw, /subtask forks: do-it, have-you-incorporated, etc.) was audited only where the parent ingested its output (L1968/1970/2053/2119), per the session's own fork-ownership ground rule.
- Early teammate agents (codex/claude/cliproxy context analysis, patch-rung fleet) live in separate teammate sessions; I audited them via their relayed reports in this transcript only.
- The corpus was finalized ~13:39-13:54 local (11:39-11:54Z), i.e. within the transcript's final minutes; no post-transcript session was found or needed.
