# Subagent report: audit-architecture

- **Beat:** ARCHITECTURE.md itself — source-grounding of "source-confirmed" claims against the hash-verified bundle /tmp/claude-2.1.220.js, plus a conceptual attack on the reasoning and arithmetic. `~/claude-code` used for orientation only.
- **Delivered:** failed on account session limit at ~13:18 UTC, self-resumed after the reset, delivered 2026-08-05 ~18:44 UTC.
- **Lead verification:** PART1 byte-offset spot-checks (`sOu=20000` @5393191, `uZc(){return!1}` @3389509, `xI=65536` @1180696, `=52428800` @1266231, `CLAUDE_CODE_TRANSCRIPT_LOCAL_GC` ×2) re-run by the lead; PART2 finding 1 (model hole) and finding 3 (10.3k) re-verified. The 10.3k mechanism was subsequently fully source-confirmed by the lead — see REVIEW.md §9.

Verbatim report as delivered:

---

ARCHITECTURE.md adversarial audit. Bundle audited: /tmp/claude-2.1.220.js (sha256/size match corpus pins). All offsets are byte offsets into that file.

PART1 — SOURCE-GROUNDING VERDICTS

1. D005 vSe/Fny reservation = min(configured out, 20000); 3k hard margin; 13k auto-compact margin; >200k latch.
VERDICT: CONFIRMED-IN-SOURCE (all sub-claims), with one naming caveat.
- vSe @5391730: "function vSe(e,t){let r=Math.min(Efo(e),sOu),n=KI()?t:void 0,{window:o}=o7(e,n);return o-r}"
- Fny @5391821: "function Fny(e){let t=Math.min(Efo(e),sOu);return Xv(e,OA())-t}"; sOu @5393190: "sOu=20000". Both sites apply the identical min.
- 13k @~5388990 (yfo): "let r=e-13000"; 3k @~5389310 (rOu): "l=a!==void 0&&!isNaN(a)&&a>0?a:n-3000"; named twins "QMu=13000,ZMu=3000" @~5389480. Admission wiring @~5392770 (uFe): "return rOu(e,vSe(t,o),n,Fny(t))" — blocked at Fny−3000, compact/warn off vSe. So hard=372k−20k−3k=349k exactly as doc derives.
- >200k branch: s$t @~10357907: "let r=e.findLast((o)=>o.type===\"assistant\");...return n?cIe(n)>200000:!1"; latched consumer @7678607: "exceeds200kTokens:At===\"plan\"&&s$t(rt)"; cached per-messageId @~17158800 ("oe=L.current.exceeds200kTokens...if(ce!==L.current.messageId)oe=s$t(Y)"); consumed by LP @2478660 to suppress the opusplan plan-mode Opus upgrade. CAVEAT: nothing named "credit" exists at this branch (see Finding 7). Also note cIe sums input+cache_creation+cache_read+OUTPUT — a total, not input-only.

2. D021 uZc folded false; max_input_tokens no production consumer; max_tokens output-cap only.
VERDICT: CONFIRMED-IN-SOURCE.
- uZc @3389508: "function uZc(){return!1}". Both lookup dZc ("if(!uZc())return") and refresh pZc are gated on it.
- dZc has exactly two references (3389633 def-internal, 3393294 in lst); lst reads only "s?.max_tokens". max_input_tokens occurs at 3390677 (zod cache schema aZc — parsed, stored, never read) and 19311306/19769415+ (prose inside bundled claude-api doc text). No production consumer: verified negative after exhaustive occurrence sweep (7 hits total).
- Discovered max_tokens in lst @3393036: "if(s?.max_tokens&&s.max_tokens>=4096)r=s.max_tokens,t=Math.min(t,r)" — sets output upperLimit; nuance: it can also LOWER the default via min(t,r), so "affects only the output upper cap" is a hair loose, but for 128k-discovery min(32000,128000)=32000 → doc's conclusion exact.

3. lst default 32000/upper 128000 for unknown GPT; Efo caps configured at upper; builder uses Efo; modelUsage reports lst().default; discovery bound = min(existingDefault, discoveredUpper).
VERDICT: CONFIRMED-IN-SOURCE.
- lst @3393036 fallback: "else t=Mxg,r=Oxg" with "Mxg=32000,Oxg=128000" @~3393470 (any model not in catalog and not claude-3-* → 32000/128000).
- Efo @12152213: "function Efo(e){let t=lst(e);return U$e(\"CLAUDE_CODE_MAX_OUTPUT_TOKENS\",process.env.CLAUDE_CODE_MAX_OUTPUT_TOKENS,t.default,t.upperLimit).effective}"
- Request builder @~12098860: "let St=Efo(u),Jt=Math.min(Ho?.maxTokensOverride||i.maxOutputTokensOverride||St,St)" → max_tokens:Jt.
- modelUsage @6983481: "...contextWindow:Xv(o,OA()),maxOutputTokens:lst(o).default".
- min-bound: the t=Math.min(t,r) line above is literally min(existingDefault, discoveredUpper). Doc's sentence is source-exact.

4. ScheduleWakeup selector probes eFe('repl_main_thread') and eFe('sdk'); eFe precedence; qLo memoization.
VERDICT: CONFIRMED-IN-SOURCE, verbatim.
- Selector @9339558: "async prompt(){let e=eFe(\"repl_main_thread\"),t=eFe(\"sdk\");return NDu(\"noop\"in kOs().shape,e===t?e:void 0)}" — never consults current query source; e===t?e:void 0 yields exactly the doc's three variants (5m/1h/mixed).
- eFe @12081072: "if(Yt(process.env.FORCE_PROMPT_CACHING_5M))return!1;if(Yt(process.env.ENABLE_PROMPT_CACHING_1H)||Hn()===\"bedrock\"&&Yt(process.env.ENABLE_PROMPT_CACHING_1H_BEDROCK))return!0;if(!ii()||Gie().isUsingOverage)return!1" then flag allowlist tengu_prompt_cache_1h_config (default ["repl_main_thread*","sdk",...]). Precedence order matches the doc 1:1.
- qLo @12065145: memo key l = route flags + name + FO_(inputJSONSchema hash); cached value "u={name:e.name,description:await $O_(e,t),input_schema:f}" then "c.set(l,u)" — description lives INSIDE the memoized value and OUTSIDE the key, so policy changes don't refresh the model-visible description until cache clear. Doc claim exact.

5. Physical-GC starts false; env read only in sdkUrl/CCR resume; 5 MiB / 20 MiB thresholds.
VERDICT: CONFIRMED-IN-SOURCE.
- "Z2o=!1" @12492412; sole setter "function EVs(e){Z2o=e}" @~12400510; sole call @21070733: inside "if(t.resume){...if(t.sdkUrl){...EVs(kCm()),OVs(s.sessionId,p,hgi())...}" — the --resume + sdkUrl branch only.
- kCm @~20859780: "function kCm(){return Z.CLAUDE_CODE_TRANSCRIPT_LOCAL_GC??Ke(\"tengu_transcript_local_gc\",!1)}" (the only other occurrence of the env string @702440 is the env-registry table).
- Thresholds: "eB_=5242880" @12492375, used @12416620 "if(s.size<eB_)return" in performCompactTranscript (compaction eligibility); "tbr=20971520" @12492387 → "backstopThresholdBytes=tbr" @~12407500 (initial backstop). Also FLUSH_INTERVAL_MS=100 corroborates the doc's 100-ms-batched writer.

6. Tombstone removal: fast path truncate+suffix rewrite; final-64-KiB search; >50-MiB guard.
VERDICT: CONFIRMED-IN-SOURCE with a mechanism-attribution nuance.
- performRemoveByUuid @~12415690: "let l=Math.min(a,xI),c=a-l,...m=p.lastIndexOf(f);if(m>=0){...if(await n.truncate(A),b>0)await n.write(p,E,b,A);return}" then "if(r>ZF_){w(`Skipping tombstone removal: session file too large...`);return}" then whole-file filter/rewrite. xI=65536 @1180695; ZF_=52428800 @12492354.
- NUANCE: in source the final-64-KiB search IS the unconditional fast path (search+truncate+rewrite are one path); the >50-MiB guard suppresses the whole-file-rewrite FALLBACK. Doc line 725 ("The fallback searched only the final 64 KiB") attributes the 64-KiB scan to the fallback — mislabel of which path scans; observed behavior (silent success, rc 0, target remains) is exactly right. See Finding 11.

7. Y0 anchors on latest valid assistant API usage, estimates only the tail.
VERDICT: CONFIRMED-IN-SOURCE, exact.
- Y0 @10358289: "function Y0(e,t){let r=eOd(e);if(!r)return ZL(e,t);return cIe(r.usage)+ZL(e.slice(r.anchorIndex+1),t)}"; eOd walks backward for the last entry where lIe() yields usage; lIe @10356407 returns usage only for type==="assistant" with usage, excluding synthetic content/model — i.e. "latest valid assistant API usage" verbatim.

8. fork-context-ref carries parentSessionId + parentLastUuid.
VERDICT: CONFIRMED-IN-SOURCE, plus bonus.
- Writer @~12427150: "appendEntry({type:\"fork-context-ref\",...e})"; resolver EB_ @~12428060: "let r=tD(e.parentSessionId),...o=n.get(e.parentLastUuid);if(!o)return w(`[fork-context-ref] parent uuid ${e.parentLastUuid} not found in ${r}; returning empty prefix`...),[]" — the literal warning also confirms the doc's "returns an empty inherited prefix" (line 811) word-for-word. GC retention map @~12494400 routes "fork-context-ref":"route-by-agent".

PART2 — FINDINGS (most severe first)

1. Severity HIGH; Class UNDERSPECIFIED; ARCHITECTURE.md:488-500 (also 464-477, 515-521, 1352-1359 vs 905-908, 148).
The target MODEL is absent from every replay-compatibility list: C's minimum inclusion list (490-499: protocol, system, items, betas, tools, tool choice, thinking config, context-management result, "max_tokens and semantically relevant request modes" — no model); the current-fingerprint omission list (466-477 — model not even listed as omitted); and the replay verification gates ("changed instructions/tools/betas/effort/output mode alter **C**" — changed MODEL absent). U (148) lists "deployment... selected route" but is never required to encode the target model. Meanwhile the precompute section keys "effective model" (906) SEPARATELY from C and U — proof the authors know neither captures it — and cross-model reuse is the doc's own executable-reproduced defect class (876-884, Terra consumes Luna summary). By the letter of the replay-eligibility spec (517-520: anchor + exact C + compatible U + no conflicting reasoning), encrypted reasoning generated under gpt-5.6-luna replays into an otherwise-identical gpt-5.6-terra request if the U implementation is model-agnostic. Charitable reading: C is "digest of the... model-facing request" (147) and a whole-request digest would include the model field incidentally; but the doc's normative enumerations (an include list AND an exclude list) are clearly meant to define the digest, and all three lists omit it — a conforming-minimal implementation ships the doc's own proven defect class, and the specified gates would not catch it.

2. Severity MED; Class UNDERSPECIFIED; ARCHITECTURE.md:106 (with 155-157, 137-139, 1490).
The governing invariant's fourth clause — "AND durable source history is not contradicted by the current reconstruction" — has no enforcement owner, no protocol, and no encoding. The proxy cannot observe Claude's D/R state; the only transmission remark in 1569 lines is "A versioned structured envelope can encode several dimensions" (155-156) — a possibility, not a spec. Dimension symbols D/R/A/S are defined (150-153) and never referenced again anywhere in the document (verified by symbol sweep: every later bare use is C, U, or E); D002 (1490) requires CLIProxy to prove only "C/U and, for continuation, E". Unspecified: who computes/increments the D and R generations, when, how they reach the proxy, what the proxy does when they are absent (strict fail-closed would veto all replay for existing clients), and how the doc's own named race — "A proxy can advance replay state in **P** ... before Claude has durably advanced **D**" (137-139) — is closed. The invariant is stated as per-request ("For every model request...") but is per-request-checkable by no component as specified.

3. Severity MED; Class ARITHMETIC/UNDERSPECIFIED; ARCHITECTURE.md:207 (block 204-233).
"free shown by /context: approximately 10.3k" is never given a referent, and the only arithmetic that reconciles it with the section's own numbers is 369k−358.7k, i.e. (372k − 3k hard margin) − projected — free measured against a boundary that OMITS the 20k output reservation. The same section derives the operative hard limit as 349k (222-230), which the 358.7k projection exceeds by ~9.7k — so at the moment of failure /context was showing ~10.3k FREE. The text explains the 336.2k-vs-358.7k gap ("headline showed the latest API input usage...", 217-218) but never explains 10.3k; the reader who tries 372−358.7 gets 13.3k and is stuck. This unreconciled number in the motivating anecdote is precisely the mislabeled-boundary defect Rung 2 exists to fix (398-419: "remaining room before each boundary"), and the doc's stated purpose is to keep the reasoning beside the implementation — here the reasoning is missing at the origin story itself. [Lead follow-up: fully resolved and source-confirmed — the number is (raw − 3k Compact-buffer) − projected under the auto-compact-DISABLED display branch, which sheds the 20k reservation from the UI while admission keeps it. See REVIEW.md §9.]

4. Severity MED; Class CONCEPTUAL; ARCHITECTURE.md:1466-1475 (with 318, 343-345, 383-390, 723-737).
Shipping Rungs 0–2 first raises the hard boundary 349k→369k and the auto-compact threshold 339k→359k (table at 386-390) while auto-compaction is enabled (343: "Auto-compaction is currently enabled") — larger admitted requests, later and larger compactions, larger transcripts and precompute activity — over the UNFIXED Rung 4/5 defect set the doc itself proves: tombstone >50-MiB silent success (723-726, more likely as transcripts grow), the ordinary replacement crash window, native stale-precompute consumption (876-899, armed near the now-higher compact threshold). No passage argues this interaction. Rung 1 is "Retained as first independently shippable patch" (318) and the boundary section says only "Rung 3 is the next correctness foundation" (1474) — dependency ordering of FIXES is argued, but the claim that widening the operating envelope over known-broken durability machinery is safe in the interim is asserted by omission. Honest bounds: the defects are native and live today regardless, and +20k/372k is marginal — but a document this explicit about cross-component interaction never confronts its own rollout's interaction.

5. Severity MED; Class UNDERSPECIFIED; ARCHITECTURE.md:959-968, 1033-1043 (vs 84-93, 1268).
Rung 6 previous_response_id hands the provider a retained conversation state that the MODEL then consumes as its effective context — functionally a second history replica that Claude never observes. The executive section's own argument against hidden compaction is that it "would create two semantic histories" (86-89); that argument is never run against continuation. The C/U/E eligibility list (1037-1043) is a strong reset discipline, but the doc never defines what C MEANS for an abbreviated continuation request (digest of the virtual full request? computed from what?). If Claude keeps sending full input and the proxy elides the prefix, C stays computable per-request — but that mechanism is nowhere described; if instead the proxy must retain the semantic prefix to validate continuation, it brushes against the explicitly rejected "proxy-owned semantic transcript" (1268). D001's sole-ownership claim survives only via an unstated mechanism.

6. Severity MED; Class CONCEPTUAL; ARCHITECTURE.md:141-158, 1526-1528, 1543-1544.
No dimension in {C,U,E,D,R,A,S} encodes TIME. Replay eligibility has no age bound; grep confirms no expiry/TTL/age fence anywhere (the only TTL discussion is ScheduleWakeup's prompt-cache prompt). Provider model REVISION portability is acknowledged open ("across accounts, endpoints, model revisions...", 1527), but revision boundaries are invisible to the proxy, so the fail-closed default (1543: "fail closed where these semantics authorize reuse") is unimplementable against revision drift: C/U-matched replay IS reuse these semantics authorize, at any age, across any silent provider rollout. Every accepted replay decision (D010, D002) implicitly assumes revision stability for the lifetime of a cache entry; the open-questions list names the uncertainty but no decision requires the only implementable fence (a TTL/generation bound).

7. Severity LOW-MED; Class UNDERSPECIFIED; ARCHITECTURE.md:381.
"Recheck the related greater-than-200k credit-latch branch under the same predicate." — "credit-latch" appears exactly once in the corpus (verified: no other hit in ARCHITECTURE.md or reports/), is never defined, and has no evidence pointer. In source, the >200k branch is s$t (cIe(usage)>200000 on the last assistant turn) latched into exceeds200kTokens and consumed by LP to gate the opusplan plan-mode upgrade — nothing named "credit" appears at any of its sites (the bundle's credit-named machinery, fallback_credit beta / fallbackCreditLane, is elsewhere in the request path). An implementer cannot locate this branch from the doc. Additionally, s$t tests TOTAL usage (input+cache+output), so applying "the same predicate" (exact-model INPUT-only admission) to it is not directly meaningful as phrased.

8. Severity LOW; Class CONTRADICTION (register redundancy); ARCHITECTURE.md:1507 vs 1515.
D019 "Ambiguous WebSocket writes are not retried without idempotency proof | Accepted" and D027 "Outcome-unknown partial writes are not automatically resent without verified idempotency | Accepted; executable defect" are the same policy registered twice with divergent evidence annotations — D019's bare "Accepted" understates the executable evidence that D027 carries for the identical behavior. More broadly, evidence-class tags exist only on D026-D033; equally source-confirmed decisions (D005, D021) sit untagged, so tag absence carries no signal. A future editor updating one row and not its twin desynchronizes the register.

9. Severity LOW; Class CONCEPTUAL; ARCHITECTURE.md:121-153, 139, 1231.
The D/R/A/S symbol collision between planes (121-128) and dimensions (144-153) is real but almost entirely latent: a full symbol sweep shows the dimension letters D/R/A/S are never used bare after their table, so working text is unambiguous by accident, not by design. The two residual soft spots: 139 "before Claude has durably advanced **D**" reads identically under both tables (plane-D as location vs dimension-D as commit generation — evidence the tables partially encode one concept twice), and 1231 "preserve current **M** while changing **R/S**" is plane-read only because M is plane-only. Cost is borne with finding 2: four dimensions are defined, collide, and are then never exercised.

10. Severity LOW; Class OVERSTATED; ARCHITECTURE.md:234-244.
Header "Executable proof of the reserve equation" and "proved the admission equation independently of source inspection" vs the third bullet's data: pass at 10,062, block at 10,070 brackets the boundary in (10,062..10,070] at 8-token granularity — it contains but cannot isolate the "predicted boundary of 10,064" (any value 10,063-10,070 fits; the 3,000 margin is pinned only to [2,994..3,002)). The two config-shift bullets carry the equation's SHAPE honestly ("approximately 20k"), and source now independently pins the constants (sOu=20000, n-3000), so the conclusion is true — but the experiment section's wording claims proof where the boundary fixture delivers consistency.

11. Severity LOW; Class OVERSTATED (mechanism mislabel); ARCHITECTURE.md:723-726.
"The fallback searched only the final 64 KiB and silently left the older target in place." In source (performRemoveByUuid) the final-64-KiB search is the unconditional FAST PATH; the >50-MiB guard suppresses the whole-file-rewrite FALLBACK, leaving only the fast path's tail scan. Observable behavior in the capture (rc 0, target remains, file grew) is exactly as documented; the causal attribution of which path scans 64 KiB is inverted. Matters because a fix targeting "the fallback's 64-KiB scan" would patch the wrong site — the correct fix per the doc's own decision (734-737) replaces the whole mutation strategy, so no decision is invalidated.

CLEAN — attacks attempted that the text survives:
- Rung 1 table margins (383-390): 352−13=339, 372−13=359, 372−3=369, and Before-hard 349=Fny−3k — margins applied consistently with the source's actual structure (13k off the vSe/compact window, 3k off Fny).
- Reserve-shift arithmetic: min(32k,20k)=20k vs min(128,20k)=128 → predicted inter-config shift 19,872, matching "moved by approximately 20k"; honest "approximately".
- 128k/32k contradiction explanation (424-436): source-exact end to end (Efo env-capped at upper 128000; telemetry lst().default=32000; min(existingDefault, discoveredUpper) literal in lst).
- ScheduleWakeup table (571-575): byte/token deltas monotone and plausible (248B/72tok, 199B/45tok); mixed variant size sits between explicit variants, consistent with e===t?e:void 0 three-state selector.
- "Patch both vSe and Fny... leaves the other as an earlier boundary" — sound per rOu wiring: patching vSe alone leaves blocked at Fny−3k=349k; patching Fny alone leaves compact at 339k.
- Oe partition (941-957) vs rejected "generic nonzero Oe" (1274): genuinely different — rejection is scoped to ONE scalar SHARED across consumers/anchor-regimes; the partition is anchor-aware, per-consumer, and gate 1381 tests exactly the distinguishing fixtures (no-anchor/pre/post/mixed).
- Rejected-alternatives sweep vs accepted rungs: fake 392k, global zero reservation, one-expression /context, continuation-before-foundations, blanket beta disabling — no accepted rung quietly depends on any rejected item.
- Decision-register status annotations vs body evidence class: D026-D029 "executable defect" all match executable-reproduced body sections; D030/D032 "source-confirmed" match; D031/D033 "runtime fixture pending" match. No status OVERSTATES its body class (the drift found is only the D019/D027 duplication, Finding 8).
- Superseded-claims table (181-198) vs body: rewind withdrawal (D025/1443), tombstone promotion to executable, uZc-372k demotion, /v1/models field correction — all consistent, no zombie claims.
- Evidence-discipline self-compliance in the GC section: "negative diagnostics, not confirmations" (801-803) — the doc obeys its own rule 3 where it would have been easy to overclaim.
- Origin session "one-character prompt did not add 22.5k": 358.7−336.2=22.5 exact; headline-vs-projection explanation coherent.

GAPS:
- "credit" semantics of the >200k branch: the latch exists and is fully mapped (s$t → exceeds200kTokens → LP/opusplan, plus a per-messageId UI latch), but no "credit"-named or billing-related logic was found at that branch after real effort; either the doc's label imports outside knowledge (e.g. premium billing lane) or it is a misnomer. Substance verdict in PART1-1 unaffected.
- The doc's ScheduleWakeup byte sizes (2,933/2,685/2,734) and token counts are executable-class claims backed by evidence/claude/schedule-wakeup-*.json; I sanity-checked ratios only, did not re-execute (probes off-limits to this reviewer).
- Whether the /context "free" UI formula is literally (rawWindow−3000)−projected in source was not chased to its display site; Finding 3 rests on the doc's own numbers, not on a source claim about the UI. [Lead follow-up: chased to the display site and confirmed — REVIEW.md §9.]
- Physical-GC scheduling at compact-boundary insertion and CCR delta hydration (doc 790-794 items 1 and 3) were not individually traced; I confirmed initialization gating, both thresholds, and the writer backstop (item 2). No contrary evidence seen.
