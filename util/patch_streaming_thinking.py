#!/usr/bin/env python3
"""Restore inline streaming-thinking display on the bun-packed Claude Code CLI.

Background
==========

Anthropic's 2.1.16x bundles split the streaming-thinking pipeline into two
separately-broken halves on contact with connoisseur's patches:

- **Writer side** (inside `c2H`, the stream-event reducer). Anthropic
  ships the `onStreamingThinking` option slot in the c2H signature, and
  one callsite passes `A4` (a React useState setter) into it, but the
  reducer never invokes `f?.(...)` on `thinking_delta` events. Only the
  one-shot assistant-message-completion path writes to A4 (setting
  `{thinking, isStreaming: false}` once per message). Result on pristine:
  finalized thinking lands once at message end; no progressive updates.

- **Renderer prop chain**. Connoisseur's `patchThinkingStreaming`
  searches for `hidePastThinking:!0,streamingThinking:VAR` to capture
  the renderer-side streamingThinking variable; this literal doesn't
  exist on any 2.1.16x bundle (verified empirically). The match returns
  null, the renderer-side block silently no-ops, and the lxH live mode,
  lxH transcript mode, QyA destructure, and MH useMemo fold never get
  wired together for streamingThinking.

What connoisseur ships on 2.1.16x: WRITER side partial (4 of 7
sub-replacements fire because shapes drifted), RENDERER side fully
absent. Net visible: nothing - the writer fires but no renderer reads.

What this patcher ships: both halves, owned by us, discovery-based.

Surfaces
========

**Production patches (always applied; no flag).**

  A. Live-mode `lxH(...)` props: append `streamingThinking:S4` to the
     live render-callsite's props object.
  B. Transcript-mode `lxH(...)` props: same, on the transcript callsite,
     keyed off a discovered tail that disambiguates it from the export
     and static-transcript callsites.
  C. QyA arrow-function signature: destructure `streamingThinking` from
     the props bag.
  D. MH useMemo body: fold streamingThinking.messages alongside the
     existing streamingToolUses extras, sorted by stream index, flattened
     to the message list the transcript renderer expects.
  E.1. c2H `stream_request_start`: reset streamingThinking to null at
     the start of a new request so prior turns don't bleed forward.
  E.3. c2H thinking content_block_start: seed streamingThinking with an
     empty thinking message; first delta arriving will extend it. Also
     handles redacted_thinking by seeding with the redacted data.
  E.4. c2H text content_block_start: switch streamingThinking to a
     non-streaming snapshot so the user sees the final thinking text
     while the response generates.
  E.7. c2H `thinking_delta` body: the progressive writer. Per delta,
     append `delta.thinking` to the accumulator, rebuild the virtual
     thinking message via the discovered createVirtualMessageHelper,
     and push the updated state through the React setter.

  Three connoisseur sub-replacements (message_stop and two message_delta
  variants) drift on 2.1.168 and are intentionally not ported. They
  affect only the `isStreaming` flag's reset timing - the visible thinking
  text streams correctly without them. If end-of-stream cleanup matters
  for a future surface, re-derive against current bytes.

**Instrumentation hooks (--instr only).** Per-PID logs to
  `/tmp/pfg-instr.${process.pid}.log` so multiple claude instances don't
  stomp on each other.

  R1  c2H entry: every stream event, with deltaType + sigLen broken out
      (so `thinking_delta` / `signature_delta` / `text_delta` are
      distinguishable, and signature_delta streaming-in-pieces would
      surface as multiple `signature_delta` lines with sigLen growing).
  R2  A4 setter wrap: every React setState call.
  W1  Inside the absorbed thinking_delta writer body: progressive
      write attributed to this writer specifically. Distinguishes
      streaming writes (W1) from finalized-message writes (R2-only, no
      W1) in the same A4 stream.
  L1  lxH render entry: streamingThinking shape on prop arrival.
  C2  QyA render: streamingThinking shape after destructure.
  M0  cyA memo comparator: previous-vs-current streamingThinking
      identity (catches React-memo-bypass regressions).
  M1  MH useMemo recompute trigger.
  M2  MH useMemo result counts.
  E1  Aggregator (post-MH transcript build) consumption of MH.

Discovery
=========

Anthropic re-minifies identifiers every release. Rather than versioning
the patcher per release, every minified name we touch is discovered by
structural pattern at synthesis time. Each lookup asserts a unique match;
0 or >1 hits fail the build loudly so we never silently mis-patch.

Discovered names: forget cache namespace + slot count (lxH React-forget
seeds), uuid_helper (within MH useMemo), create_msg_helper (the virtual
message factory used by MH and by the absorbed writer), qya_var,
cya_var, memo_ns, agg_fn (post-MH transcript aggregator), several
aggregator-callback skip predicates and arg vars, transcript_tail
(unique suffix of the transcript-mode lxH callsite), and the c2H
destructured option vars (setMode, setStreamingToolUses,
setStreamingThinking).

Usage
=====

::

    util/patch_streaming_thinking.py <input-binary> [--instr] [-o <out>]

Defaults: production-clean. Pass `--instr` to add the nine log hooks.
Output path defaults to `<input>.pfg` if `-o` is omitted.

Input expectations
==================

The input binary must be the bun-packed CLI form (Linux ELF with a
`.bun` section as parsed by `util/bun_handler`). Connoisseur's other
display patches (verbose tool-call rendering, diff colors, subagent
prompt visibility, spinner-tip suppression, version-output marker,
welcome-badge rebrand, etc.) are independent of streaming-thinking and
can be applied either before or after this patcher with no anchor
conflict - they don't touch c2H or the renderer prop chain at the
sites we own.

The thinking-streaming sub-patch of connoisseur's `patchThinkingStreaming`
must NOT be applied first: it injects `__cc_*`-prefixed code into c2H
that would conflict with our E.* anchors. Pass `--disable
thinking-streaming` when running connoisseur's TS patcher, or run this
patcher against pristine bundles directly.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bun_handler  # noqa: E402  (sys.path insert must precede this)


def discover_names(js):
    """Find the per-release minified variable names by structural shape.
    Each entry asserts a unique match: if any returns 0 or >1 hits, the
    bundle drifted enough that we want a loud failure rather than silent
    mis-patching.
    """
    def find_one(pattern, label, group=1):
        matches = list(re.finditer(pattern, js))
        if len(matches) != 1:
            raise SystemExit(
                f"[discover] {label}: expected 1 match, got {len(matches)}\n"
                f"           pattern: {pattern[:120]!r}"
            )
        return matches[0].group(group)

    names = {}
    # React-forget cache namespace and slot count (`X.c(N)`)
    names['forget_ns'] = find_one(
        r'function lxH\(H\)\{let \$=([A-Za-z_$][\w$]*)\.c\(\d+\),\{deferMessages:',
        'forget cache namespace',
    )
    names['forget_n'] = find_one(
        r'function lxH\(H\)\{let \$=[A-Za-z_$][\w$]*\.c\((\d+)\),\{deferMessages:',
        'forget cache slot count',
    )
    # uuid helper inside the MH useMemo (`eH.uuid=X(...)`)
    names['uuid_helper'] = find_one(
        r'function lxH[\s\S]{0,200}[\s\S]{0,5000}?(?:MH|[A-Za-z_$][\w$]*)='
        r'(?:zf|[A-Za-z_$][\w$]*)\.useMemo\(\(\)=>(?:fH|[A-Za-z_$][\w$]*)\.flatMap'
        r'\([\s\S]{0,200}?eH\.uuid=([A-Za-z_$][\w$]*)\(',
        'uuid helper (MH useMemo)',
    )
    # createVirtualMessageHelper inside the MH useMemo (`let eH=X(...)`).
    # Same statement scope as uuid_helper. Used by the absorbed writer (E.7,
    # E.3) to materialize virtual thinking messages, and by patch D's
    # rewritten MH body to keep the existing tool-use extras intact.
    names['create_msg_helper'] = find_one(
        r'MH=[A-Za-z_$][\w$]*\.useMemo\(\(\)=>[A-Za-z_$][\w$]*\.flatMap'
        r'\(\([A-Za-z_$][\w$]*\)=>\{let [A-Za-z_$][\w$]*='
        r'([A-Za-z_$][\w$]*)\(\{content:\[',
        'create virtual message helper (MH useMemo)',
    )
    # c2H signature destructure - capture the option vars the absorbed
    # writer (E.1, E.3, E.4, E.7) substitutes into its emitted bodies.
    # The c2H option names are stable (set by Anthropic in source); only
    # the local-bound minified vars drift release-to-release. Pulling them
    # all out at once gates the discovery on the signature shape being
    # the destructured-options form (vs the older positional one), which
    # is what 2.1.16x ships.
    c2h_match = re.search(
        r'function c2H\(([A-Za-z_$][\w$]*),([A-Za-z_$][\w$]*)\)'
        r'\{let\{([^}]+)\}=\2;',
        js,
    )
    if c2h_match is None:
        raise SystemExit('[discover] c2H destructured-options signature not found')
    names['c2h_event_param'] = c2h_match.group(1)
    destructure = c2h_match.group(3)
    for opt, slot in (
        ('onSetStreamMode', 'c2h_set_mode'),
        ('onStreamingToolUses', 'c2h_set_tools'),
        ('onStreamingThinking', 'c2h_set_thinking'),
    ):
        pm = re.search(rf'{opt}:([A-Za-z_$][\w$]*)', destructure)
        if pm is None:
            raise SystemExit(f'[discover] c2H {opt} not found in destructure')
        names[slot] = pm.group(1)
    # QyA-equivalent component alias
    names['qya_var'] = find_one(
        r',([A-Za-z_$][\w$]*)\s*=\s*\(\{messages:[A-Za-z_$][\w$]*,tools:\$,'
        r'commands:[A-Za-z_$][\w$]*[\s\S]{0,800}?renderRange:[A-Za-z_$][\w$]*\}\)=>\{',
        'QyA component',
    )
    # cyA memo alias + React.memo namespace
    cya_match = re.search(
        rf'([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\.memo\({re.escape(names["qya_var"])},'
        rf'\(H,\$\)=>\{{let q=Object\.keys\(H\);',
        js,
    )
    if not cya_match:
        raise SystemExit('[discover] cyA memo wrapper not found')
    names['cya_var'] = cya_match.group(1)
    names['memo_ns'] = cya_match.group(2)
    # Transcript-aggregator function (formerly eC4, now whatever release renames it)
    agg_match = re.search(
        r'eH=([A-Za-z_$][\w$]*)\(([A-Za-z_$][\w$]*)\.filter\(\(([A-Za-z_$][\w$]*)\)=>\3\.type!=="progress"\)'
        r'\.filter\(\(\3\)=>!([A-Za-z_$][\w$]*)\(\3\)\)'
        r'\.filter\(\(\3\)=>([A-Za-z_$][\w$]*)\(\3,JH\)\),MH\)',
        js,
    )
    if not agg_match:
        raise SystemExit('[discover] eC4 aggregator call not found')
    names['agg_fn'] = agg_match.group(1)
    names['agg_filtered_var'] = agg_match.group(2)
    names['agg_cb_param'] = agg_match.group(3)
    names['skip_pred1'] = agg_match.group(4)
    names['skip_pred2'] = agg_match.group(5)
    # Transcript-mode lxH callsite tail
    # Four createElement(lxH,{ sites exist: the live one has streamingText,
    # the transcript one has agentDefinitions + onSearchMatchesChange +
    # scrollRef:G$ but lacks streamingText, the export site has
    # conversationId:"export", and the static transcript export site has
    # screen:"transcript" with showAllInTranscript:!0. We want the
    # interactive transcript-mode one.
    transcript_tail = None
    for pos in (m.start() for m in re.finditer(r'createElement\(lxH,\{', js)):
        i = pos + len('createElement(lxH,{')
        depth = 1
        while i < len(js) and depth > 0:
            if js[i] == '{':
                depth += 1
            elif js[i] == '}':
                depth -= 1
            i += 1
        body = js[pos + len('createElement(lxH,{'):i - 1]
        if ('streamingText:' not in body
                and 'agentDefinitions:' in body
                and 'onSearchMatchesChange:' in body
                and 'scrollRef:G$' in body):
            transcript_tail = body[-260:].strip()
            break
    if transcript_tail is None:
        raise SystemExit('[discover] transcript-mode lxH callsite not found')
    names['transcript_tail'] = transcript_tail

    return names


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        print(f"usage: {sys.argv[0]} <input-binary> [--instr] [-o <output>]")
        sys.exit(2)
    src = sys.argv[1]
    add_instr = '--instr' in sys.argv
    if '-o' in sys.argv:
        dst = sys.argv[sys.argv.index('-o') + 1]
    else:
        dst = src + '.pfg'

    data = open(src, 'rb').read()
    js = bun_handler.extract_js(data).decode('utf-8', errors='surrogateescape')
    print(f"input:        {src} ({len(data)} bytes)")
    print(f"JS extracted: {len(js)} bytes")

    names = discover_names(js)
    print("\n--- discovered names ---")
    for k, v in names.items():
        if k == 'transcript_tail':
            print(f"  {k:>22} = {v[:60]!r}...")
        else:
            print(f"  {k:>22} = {v!r}")

    def splice(old, new, label, expected=1):
        nonlocal js
        cnt = js.count(old)
        if cnt != expected:
            raise SystemExit(
                f"[{label}] count {cnt}!={expected} anchor={old[:120]!r}"
            )
        js = js.replace(old, new, expected)
        print(f"  [{label}] applied ({len(new) - len(old):+d} bytes)")

    def logwrite(payload):
        """Emit a `fs.appendFileSync` block that writes one tagged line
        to a per-PID log. Reused by both the absorbed writer's W1 hook
        (production code path conditional on --instr) and by the eight
        renderer/reducer hooks below. Per-PID path lets multiple claude
        instances run concurrently without log races."""
        return (
            '    try {\n'
            '      const __pfg_fs = require("fs");\n'
            '      __pfg_fs.appendFileSync(`/tmp/pfg-instr.${process.pid}.log`, '
            + payload + ');\n'
            '    } catch (__pfg_e) { /* log-write failure: swallow */ }\n'
        )

    print("\n--- production patches (A/B/C/D) ---")
    # A: live-mode lxH props
    splice(
        'scrollRef:YK()||TEH()?tA:void 0,trackStickyPrompt:YK()?!0:void 0})',
        'scrollRef:YK()||TEH()?tA:void 0,trackStickyPrompt:YK()?!0:void 0'
        + ',/*pfg-streaming-thinking*/streamingThinking:S4})',
        'A live-mode lxH props',
    )
    # B: transcript-mode lxH props (uses discovered tail)
    splice(
        names['transcript_tail'] + '})',
        names['transcript_tail']
        + ',/*pfg-streaming-thinking*/streamingThinking:S4})',
        'B transcript-mode lxH props',
    )
    # C: QyA signature
    splice(
        'setPositions:h,disableRenderCap:S=!1,renderRange:y})=>{',
        'setPositions:h,disableRenderCap:S=!1,renderRange:y,'
        + '/*pfg-streaming-thinking*/streamingThinking:__pfg_st})=>{',
        'C QyA signature',
    )
    # D: MH useMemo
    uuid_h = names['uuid_helper']
    splice(
        f'MH=zf.useMemo(()=>fH.flatMap((Y$)=>{{let eH=Gj({{content:[Y$.contentBlock]}});'
        f'return eH.uuid={uuid_h}(Y$.contentBlock.id,0),DZ([eH])}}),[fH])',
        'MH=zf.useMemo(()=>{\n'
        '    /* pfg-streaming-thinking: fold streamingToolUses + streamingThinking '
        'into one sorted transcript-extras list */\n'
        '    const __pfg_toolExtras = fH.map((Y$) => {\n'
        '      const eH = Gj({content:[Y$.contentBlock]});\n'
        f'      eH.uuid = {uuid_h}(Y$.contentBlock.id, 0);\n'
        '      return {\n'
        '        index: Y$.index ?? Number.MAX_SAFE_INTEGER,\n'
        '        messages: DZ([eH]),\n'
        '      };\n'
        '    });\n'
        '    const __pfg_thinkExtras = (__pfg_st?.messages ?? []).map((__pfg_entry, __pfg_idx) => ({\n'
        '      index: __pfg_entry.index ?? (Number.MAX_SAFE_INTEGER + __pfg_idx),\n'
        '      messages: DZ([__pfg_entry.message ?? __pfg_entry]),\n'
        '    }));\n'
        '    return [...__pfg_toolExtras, ...__pfg_thinkExtras]\n'
        '      .sort((a, b) => a.index === b.index ? 0 : a.index - b.index)\n'
        '      .flatMap((e) => e.messages);\n'
        '  }, [fH, __pfg_st])',
        'D MH useMemo',
    )

    # ---- E.* writer patches absorbed from connoisseur's patchThinkingStreaming
    # The connoisseur source publishes 7 before/after sub-replacements for
    # the c2H handler segment, of which 4 anchor cleanly on pristine 2.1.168
    # (E.1 stream_request_start, E.3 thinking content_block_start, E.4 text
    # content_block_start, E.7 thinking_delta body). The three intentionally-
    # omitted ones (message_stop, message_delta-if-usage, message_delta-return)
    # touched only the `isStreaming` reset and their anchors drifted; the
    # visible thinking text streams correctly without them.
    H = names['c2h_event_param']
    SET_MODE = names['c2h_set_mode']
    SET_TOOLS = names['c2h_set_tools']
    SET_THINK = names['c2h_set_thinking']
    CREATE_MSG = names['create_msg_helper']

    def writer_body_thinking_delta(with_w1):
        """The progressive accumulator body injected at the start of
        c2H's `case "thinking_delta":` branch. Keeps connoisseur's exact
        semantics (mutable map + replaced-flag fallback) so behavior
        matches between this patcher and connoisseur for a side-by-side
        diff if anyone needs one in the future.
        """
        w1 = ''
        if with_w1:
            w1 = (
                '    /* pfg-instr W1: absorbed progressive writer fired '
                '(distinct from finalized-message writes at assistant.type). */\n'
                + logwrite(
                    '`[pfg-instr W1 writer=thinking_delta '
                    'prev=${__pfg_prev?.thinking?.length ?? 0} '
                    'next=${__pfg_nextText.length} '
                    'msgs=${__pfg_nextMsgs.length}]\\n`'
                )
            )
        return (
            f'{SET_THINK}?.((__pfg_prev) => {{\n'
            f'    let __pfg_nextDelta = typeof {H}.event.delta.thinking === "string"\n'
            f'          ? {H}.event.delta.thinking : "",\n'
            f'        __pfg_nextText = (__pfg_prev?.thinking ?? "") + __pfg_nextDelta,\n'
            f'        __pfg_nextIdx = __pfg_prev?.currentIndex ?? {H}.event.index,\n'
            f'        __pfg_nextMsg = {CREATE_MSG}({{content:[{{type:"thinking",'
            f'thinking:__pfg_nextText}}],isVirtual:!0}}),\n'
            f'        __pfg_replaced = false,\n'
            f'        __pfg_nextMsgs = (__pfg_prev?.messages ?? []).map((__pfg_e) =>\n'
            f'          __pfg_e.index === __pfg_nextIdx\n'
            f'            ? (__pfg_replaced = true, {{...__pfg_e, message: __pfg_nextMsg}})\n'
            f'            : __pfg_e);\n'
            f'    if (!__pfg_replaced) __pfg_nextMsgs = [...__pfg_nextMsgs, '
            f'{{index: __pfg_nextIdx, message: __pfg_nextMsg}}];\n'
            + w1 +
            f'    return __pfg_prev\n'
            f'      ? {{...__pfg_prev, thinking: __pfg_nextText, isStreaming: !0, '
            f'streamingEndedAt: void 0, currentIndex: __pfg_nextIdx, '
            f'currentMessage: __pfg_nextMsg, messages: __pfg_nextMsgs}}\n'
            f'      : {{thinking: __pfg_nextText, isStreaming: !0, '
            f'streamingEndedAt: void 0, currentIndex: {H}.event.index, '
            f'currentMessage: __pfg_nextMsg, messages: [{{index: {H}.event.index, '
            f'message: __pfg_nextMsg}}]}};\n'
            f'  }});'
        )

    print("\n--- absorbed writer patches (E.1/E.3/E.4/E.7) ---")
    # E.1: at stream_request_start, reset streamingThinking so prior turns
    # don't bleed into a new one.
    splice(
        f'if({H}.type==="stream_request_start"){{{SET_MODE}("requesting");return}}',
        f'if({H}.type==="stream_request_start"){{'
        f'{SET_THINK}?.(null),{SET_MODE}("requesting");return}}',
        'E.1 stream_request_start reset',
    )
    # E.3: on a thinking (or redacted_thinking) content_block_start,
    # initialize streamingThinking with an empty thinking accumulator so
    # the first delta has something to extend, AND seed a virtual message
    # entry so the transcript shows a live row immediately.
    splice(
        f'case"thinking":case"redacted_thinking":{SET_MODE}("thinking");return;',
        f'case"thinking":case"redacted_thinking":{SET_THINK}?.((__pfg_prev) => {{'
        f'let __pfg_msg = {CREATE_MSG}({{content:['
        f'{H}.event.content_block.type==="redacted_thinking"'
        f'?{{type:"redacted_thinking",data:{H}.event.content_block.data??""}}'
        f':{{type:"thinking",thinking:""}}'
        f'],isVirtual:!0}});'
        f'return{{thinking:{H}.event.content_block.type==="redacted_thinking"'
        f'?{H}.event.content_block.data??"":"",'
        f'isStreaming:!0,streamingEndedAt:void 0,'
        f'currentIndex:{H}.event.index,currentMessage:__pfg_msg,'
        f'messages:[...(__pfg_prev?.messages??[]),{{index:{H}.event.index,message:__pfg_msg}}]}};'
        f'}}),{SET_MODE}("thinking");return;',
        'E.3 thinking content_block_start init',
    )
    # E.4: when text content_block_start fires, switch the streaming
    # state to "no longer streaming" (snapshot of the final thinking text)
    # so the user can read the finished thinking while text generates.
    splice(
        f'case"text":{SET_MODE}("responding");return;',
        f'case"text":{SET_THINK}?.((__pfg_prev) => __pfg_prev'
        f'?{{...__pfg_prev,isStreaming:!1,streamingEndedAt:void 0,'
        f'currentIndex:null,currentMessage:null}}:__pfg_prev),'
        f'{SET_MODE}("responding");return;',
        'E.4 text content_block_start snapshot',
    )
    # E.7: progressive writer. Pristine 2.1.168 ships the with-text
    # thinking_delta shape (`else if("thinking" in delta...)`) - we anchor
    # against the full body, inject the accumulator at the head, and keep
    # the existing api-metrics block downstream of it.
    OLD_TD_BODY = (
        f'case"thinking_delta":{{let{{delta:w}}={H}.event;'
        f'if("estimated_tokens"in w&&typeof w.estimated_tokens==="number")'
        f'Y?.({{type:"thinking_progress",estimatedTokensDelta:w.estimated_tokens}});'
        f'else if("thinking"in w&&typeof w.thinking==="string"&&w.thinking.length>0)'
        f'Y?.({{type:"thinking_progress",estimatedTokensDelta:Jh8(w.thinking)}});'
        f'return}}'
    )
    # Production E.7 (no W1 in --instr-off path)
    new_td_body_production = (
        'case"thinking_delta":{\n'
        '  /* pfg-streaming-thinking E.7: absorbed progressive writer. */\n'
        '  ' + writer_body_thinking_delta(with_w1=False) + '\n'
        '  '
        + OLD_TD_BODY[len('case"thinking_delta":{'):]
    )

    if not add_instr:
        splice(OLD_TD_BODY, new_td_body_production, 'E.7 thinking_delta writer')
    else:
        # Same E.7 but with W1 hook embedded inside the writer body.
        new_td_body_instr = (
            'case"thinking_delta":{\n'
            '  /* pfg-streaming-thinking E.7+W1: absorbed progressive writer. */\n'
            '  ' + writer_body_thinking_delta(with_w1=True) + '\n'
            '  '
            + OLD_TD_BODY[len('case"thinking_delta":{'):]
        )
        splice(OLD_TD_BODY, new_td_body_instr, 'E.7 thinking_delta writer (+W1)')

    if not add_instr:
        print("\n(instrumentation skipped, default; pass --instr to enable)")
    else:
        print("\n--- instrumentation hooks (per-PID log) ---")

        # R1: c2H entry. Logs the full event shape we care about:
        #   - type             - outer wrapper (stream_event, assistant, etc.)
        #   - eventType        - H.event.type (content_block_start/delta/stop,
        #                        message_start/delta/stop, ping, ...)
        #   - deltaType        - H.event.delta.type (thinking_delta /
        #                        signature_delta / text_delta / input_json_delta);
        #                        distinguishes which delta sub-shape just arrived.
        #   - sigLen           - H.event.delta.signature.length; detects whether
        #                        signature arrives whole-in-one or streams in
        #                        pieces (the `signature: $.delta.signature`
        #                        assignment would drop all but the last chunk
        #                        if signature streams).
        #   - blockType        - H.event.content_block.type; populated on
        #                        content_block_start events (thinking / text /
        #                        redacted_thinking / tool_use / ...).
        #                        Lets the grader count distinct thinking-block
        #                        starts without inferring from W1 alone.
        #   - blockIndex       - H.event.index; the content-block index the
        #                        current event applies to. With blockType,
        #                        unique (blockIndex, blockType="thinking") pairs
        #                        count thinking blocks in a run.
        splice(
            'function c2H(H,$){let{onMessage:q,',
            'function c2H(H,$){\n'
            '  /* pfg-instr R1: every event reaching the stream-handler. */\n'
            + logwrite(
                '`[pfg-instr R1 c2H type=${H?.type} eventType=${H?.event?.type} ` +\n'
                '      `deltaType=${H?.event?.delta?.type ?? ""} ` +\n'
                '      `sigLen=${H?.event?.delta?.signature?.length ?? 0} ` +\n'
                '      `blockType=${H?.event?.content_block?.type ?? ""} ` +\n'
                '      `blockIndex=${H?.event?.index ?? -1}]\\n`'
            )
            + '  let{onMessage:q,',
            'R1 c2H entry',
        )
        # R2: A4 setter wrap
        splice(
            '[S4,A4]=W8.useState(null)',
            '[S4, __pfg_A4_real] = W8.useState(null),\n'
            '  /* pfg-instr R2: wrap A4 setter to log each invocation. */\n'
            '  A4 = (__pfg_arg) => {\n'
            + logwrite('`[pfg-instr R2 A4 t=${typeof __pfg_arg}]\\n`')
            + '    return __pfg_A4_real(__pfg_arg);\n'
            '  }',
            'R2 A4 wrap',
        )
        # L1: lxH render entry (uses discovered forget_ns + forget_n)
        forget = names['forget_ns']
        slot = names['forget_n']
        splice(
            f'function lxH(H){{let $={forget}.c({slot}),{{deferMessages:q,placeholderBaseline:K,placeholderElement:_,...A}}=H,',
            'function lxH(H){\n'
            '  /* pfg-instr L1: lxH receives props; log streamingThinking shape. */\n'
            + logwrite(
                '`[pfg-instr L1 lxH hasST=${H?.streamingThinking ? "y" : "n"} ` +\n'
                '      `msgs=${H?.streamingThinking?.messages?.length || 0} ` +\n'
                '      `thinkLen=${H?.streamingThinking?.thinking?.length || 0} ` +\n'
                '      `streamingToolUses=${H?.streamingToolUses?.length || 0}]\\n`'
            )
            + f'  let $={forget}.c({slot}),{{deferMessages:q,placeholderBaseline:K,placeholderElement:_,...A}}=H,',
            'L1 lxH entry',
        )
        # C2: QyA body entry
        splice(
            ',/*pfg-streaming-thinking*/streamingThinking:__pfg_st})=>{',
            ',/*pfg-streaming-thinking*/streamingThinking:__pfg_st})=>{\n'
            '  /* pfg-instr C2: QyA render + streamingThinking shape on arrival. */\n'
            + logwrite(
                '`[pfg-instr C2 QyA hasST=${__pfg_st ? "y" : "n"} ` +\n'
                '      `msgs=${(__pfg_st && __pfg_st.messages) ? __pfg_st.messages.length : 0} ` +\n'
                '      `thinkLen=${(__pfg_st && __pfg_st.thinking) ? __pfg_st.thinking.length : 0} ` +\n'
                '      `isStreaming=${__pfg_st ? !!__pfg_st.isStreaming : false}]\\n`'
            ),
            'C2 QyA render',
        )
        # M0: cyA memo entry
        cya = names['cya_var']
        qya = names['qya_var']
        memons = names['memo_ns']
        splice(
            f'{cya}={memons}.memo({qya},(H,$)=>{{let q=Object.keys(H);',
            f'{cya}={memons}.memo({qya},(H,$)=>{{\n'
            '  /* pfg-instr M0: log cyA memo comparator firing + ST identity check. */\n'
            + logwrite(
                '`[pfg-instr M0 cyA-cmp newSThas=${(H && H.streamingThinking) ? "y" : "n"} ` +\n'
                '      `prevSThas=${($ && $.streamingThinking) ? "y" : "n"} ` +\n'
                '      `sameRef=${H?.streamingThinking === $?.streamingThinking}]\\n`'
            )
            + '  let q=Object.keys(H);',
            'M0 cyA memo entry',
        )
        # M1: MH useMemo body entry
        splice(
            'MH=zf.useMemo(()=>{\n    /* pfg-streaming-thinking:',
            'MH=zf.useMemo(()=>{\n'
            '    /* pfg-instr M1: MH recompute (deps fH or __pfg_st changed). */\n'
            + logwrite(
                '`[pfg-instr M1 MH fH=${fH.length} ` +\n'
                '      `stMsgs=${(__pfg_st && __pfg_st.messages) ? __pfg_st.messages.length : 0}]\\n`'
            )
            + '    /* pfg-streaming-thinking:',
            'M1 MH compute',
        )
        # M2: MH useMemo result
        splice(
            'return [...__pfg_toolExtras, ...__pfg_thinkExtras]\n'
            '      .sort((a, b) => a.index === b.index ? 0 : a.index - b.index)\n'
            '      .flatMap((e) => e.messages);\n'
            '  }, [fH, __pfg_st])',
            'const __pfg_result = [...__pfg_toolExtras, ...__pfg_thinkExtras]\n'
            '      .sort((a, b) => a.index === b.index ? 0 : a.index - b.index)\n'
            '      .flatMap((e) => e.messages);\n'
            '    /* pfg-instr M2: MH result size + per-channel counts. */\n'
            + logwrite(
                '`[pfg-instr M2 result=${__pfg_result.length} ` +\n'
                '      `toolE=${__pfg_toolExtras.length} ` +\n'
                '      `thinkE=${__pfg_thinkExtras.length}]\\n`'
            )
            + '    return __pfg_result;\n'
            '  }, [fH, __pfg_st])',
            'M2 MH result',
        )
        # E1: aggregator-fn call (eC4-equivalent)
        agg = names['agg_fn']
        filtered = names['agg_filtered_var']
        cb = names['agg_cb_param']
        skip1 = names['skip_pred1']
        skip2 = names['skip_pred2']
        splice(
            f'eH={agg}({filtered}.filter(({cb})=>{cb}.type!=="progress").filter(({cb})=>!{skip1}({cb})).filter(({cb})=>{skip2}({cb},JH)),MH)',
            'eH = ((__pfg_mh) => {\n'
            f'      const __pfg_eH = {agg}(\n'
            f'        {filtered}.filter(({cb}) => {cb}.type !== "progress")\n'
            f'          .filter(({cb}) => !{skip1}({cb}))\n'
            f'          .filter(({cb}) => {skip2}({cb}, JH)),\n'
            '        __pfg_mh\n'
            '      );\n'
            '      /* pfg-instr E1: log aggregator consumption of MH. */\n'
            + logwrite(
                '`[pfg-instr E1 agg MH=${__pfg_mh.length} ` +\n'
                f'      `filteredIn=${{{filtered}.length}} eHout=${{__pfg_eH.length}}]\\n`'
            )
            + '      return __pfg_eH;\n'
            '    })(MH)',
            'E1 aggregator wrap',
        )

        # X1: instrument the user-interrupt-on-submit site. Anchor on the
        # debug log string '[interrupt] Aborting current turn:' which is
        # unique. Inject a marker line before the existing tengu_cancel
        # telemetry call so we can see when an interrupt fires. The
        # surrounding stream-thinking state shows up via the adjacent L1/C2
        # render logs (correlate by timestamp); X1 itself fires once per
        # interrupt to make the moment greppable in the log.
        splice(
            'N(`[interrupt] Aborting current turn: streamMode=${H.streamMode}`);',
            'N(`[interrupt] Aborting current turn: streamMode=${H.streamMode}`);\n'
            '  /* pfg-instr X1: fires at the moment user-input interrupt is processed. */\n'
            + logwrite(
                '`[pfg-instr X1 interrupt source=submit streamMode=${H.streamMode}]\\n`'
            )
            + '  ',
            'X1 interrupt-on-submit',
        )

        # X2.a: wrap pOA = filterTrailingThinkingFromLastAssistant. The
        # function strips trailing thinking blocks from the last assistant
        # message before the next request. Anchored on the unique telemetry
        # name. Logs the input message count, the trim count, and whether
        # the last assistant ended with a thinking block (i.e. whether the
        # filter actually did something on this call).
        ptt_match = re.search(
            r'function ([A-Za-z_$][\w$]*)\(H\)\{let \$=H\.at\(-1\);'
            r'if\(!\$\|\|\$\.type!=="assistant"\)return H;'
            r'let q=\$\.message\.content,K=q\.at\(-1\);'
            r'if\(!K\|\|!Oh\$\(K\)\)return H;'
            r'let _=q\.length-1;while\(_>=0\)\{let f=q\[_\];'
            r'if\(!f\|\|!Oh\$\(f\)\)break;_--\}'
            r'l\("tengu_filtered_trailing_thinking_block"',
            js,
        )
        if ptt_match is None:
            raise SystemExit('[X2.a] filterTrailingThinking anchor not found')
        ptt_name = ptt_match.group(1)
        ptt_old = f'function {ptt_name}(H){{let $=H.at(-1);'
        ptt_new = (
            f'function {ptt_name}(H){{\n'
            '  /* pfg-instr X2.a: filterTrailingThinkingFromLastAssistant entry. */\n'
            + logwrite(
                '`[pfg-instr X2.a filterTrailing entry inMsgs=${H?.length ?? 0} ` +\n'
                '      `lastIsAssistant=${(H?.at(-1)?.type === "assistant") ? "y" : "n"} ` +\n'
                '      `lastEndsThinking=${(()=>{ try { const m = H.at(-1); '
                'if (m?.type !== "assistant") return "n"; const c = m.message?.content; '
                'if (!Array.isArray(c)) return "n"; const t = c.at(-1); '
                'return (t?.type === "thinking" || t?.type === "redacted_thinking") ? "y" : "n"; } '
                'catch (e) { return "?"; } })()}]\\n`'
            )
            + '  let $=H.at(-1);'
        )
        splice(ptt_old, ptt_new, f'X2.a filterTrailing entry ({ptt_name})')

        # X2.b: wrap QN$ = filterOrphanedThinkingOnlyMessages. Anchored on
        # the unique telemetry name. Logs the input message count and how
        # many orphaned-thinking-only messages the filter dropped.
        qno_match = re.search(
            r'function ([A-Za-z_$][\w$]*)\(H\)\{let \$=new Set;'
            r'for\(let K of H\)\{if\(K\.type!=="assistant"\)continue;'
            r'let _=K\.message\.content;if\(!Array\.isArray\(_\)\)continue;'
            r'if\(_\.some\([\s\S]{0,200}?\)&&K\.message\.id\)'
            r'\$\.add\(K\.message\.id\)\}let q;for\(let K=0;K<H\.length;K\+\+\)',
            js,
        )
        if qno_match is None:
            raise SystemExit('[X2.b] filterOrphaned anchor not found')
        qno_name = qno_match.group(1)
        qno_old = f'function {qno_name}(H){{let $=new Set;'
        qno_new = (
            f'function {qno_name}(H){{\n'
            '  /* pfg-instr X2.b: filterOrphanedThinkingOnlyMessages entry. */\n'
            + logwrite(
                '`[pfg-instr X2.b filterOrphaned entry inMsgs=${H?.length ?? 0} ` +\n'
                '      `orphanedThinkingOnly=${(()=>{ try { let count = 0; '
                'for (const m of H) { if (m?.type !== "assistant") continue; '
                'const c = m.message?.content; if (!Array.isArray(c) || c.length === 0) continue; '
                'if (c.every((b) => b?.type === "thinking" || b?.type === "redacted_thinking")) count++; } '
                'return count; } catch (e) { return -1; } })()}]\\n`'
            )
            + '  let $=new Set;'
        )
        splice(qno_old, qno_new, f'X2.b filterOrphaned entry ({qno_name})')

    new_data = bun_handler.repack_with_js(data, js.encode('utf-8', errors='surrogateescape'))
    print(f"\nfinal JS: {len(js)} bytes")
    print(f"binary:   {len(new_data)} bytes (delta {len(new_data) - len(data):+d})")
    open(dst, 'wb').write(new_data)
    os.chmod(dst, 0o755)
    print(f"wrote {dst}")


if __name__ == '__main__':
    main()
