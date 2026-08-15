#!/usr/bin/env python3
"""Restore inline streaming-thinking display on the bun-packed Claude Code CLI.

Background
==========

Anthropic ships an `onStreamingThinking` option on the stream-event
reducer and wires a React state setter into it, but nothing writes to it
progressively. The only write is a one-shot at message completion,
`{thinking, isStreaming:false}`, once per assistant message. So stock
shows finalized thinking after the fact and never streams it. The state
that write lands in is not rendered at all: it feeds only a
30-second expiry timer and the Esc-cancel commit.

Two halves therefore have to be built, and this patcher owns both:

- **Writer side.** Invoke the setter on every `thinking_delta`, seeding
  at the thinking `content_block_start` so the first delta has something
  to extend, resetting per request, and snapshotting at text start.

- **Renderer prop chain.** Thread `streamingThinking` from the live and
  transcript render callsites through the transcript component's props
  destructure into the useMemo that builds the transcript extras list.

The reducer itself is split in two. The message-level half
destructures `onStreamingThinking`; the event-level half handles content
blocks and deltas and does not. The writer patches live in the
event-level half and reach the setter through the options bag both
halves share, which `discover_names` asserts is actually handed over.

Connoisseur's `patchThinkingStreaming` covers this surface too. This
patcher owns it instead, because it also seeds redacted_thinking, resets
per request, snapshots at text start, and carries the Tier-1
interrupted-thinking commit. CON-F is disabled so the two never both
edit the same code.

Surfaces
========

**Production patches (always applied; no flag).**

  A. Live-mode renderer props: append `streamingThinking:<state>` to the
     live render-callsite's props object.
  B. Transcript-mode renderer props: same, on the transcript callsite,
     keyed off a discovered tail that tells it apart from the export and
     static-transcript callsites.
  C. Transcript component signature: destructure `streamingThinking` from
     the props bag.
  D. Transcript-extras useMemo: fold streamingThinking.messages alongside
     the existing streamingToolUses extras, sorted by stream index,
     flattened to the message list the transcript renderer expects.
  E.1. Reducer `stream_request_start`: reset streamingThinking to null at
     the start of a new request so prior turns don't bleed forward.
  E.3. Reducer thinking content_block_start: seed streamingThinking with
     an empty thinking message; the first delta extends it. Also handles
     redacted_thinking by seeding with the redacted data.
  E.4. Reducer text content_block_start: switch streamingThinking to a
     non-streaming snapshot so the user sees the final thinking text
     while the response generates.
  E.7. Reducer `thinking_delta` body: the progressive writer. Per delta,
     append `delta.thinking` to the accumulator, rebuild the virtual
     thinking message via the discovered createVirtualMessageHelper,
     and push the updated state through the React setter.
  T1. onCancel interrupted-thinking commit (Tier 1): pristine commits the
     streamed thinking summary on Esc as a virtual thinking block. Rewrite
     that commit to append a cut marker and reuse the streaming preview's
     uuid, so the block replaces the preview instead of landing beside it,
     then clear the preview. One gray block, rendered once.
     Display only: the model does not see it. N3 skips virtual messages
     (Edt -> lBr), and atr drops thinking-only assistant messages anyway.
     Feeding the summary back would take a text block, not just dropping
     the flag, since a thinking block also needs its issued signature.

  Connoisseur's message_stop and message_delta sub-replacements are not
  ported. They affect only the `isStreaming` flag's reset timing, and the
  visible thinking text streams correctly without them. If end-of-stream
  cleanup matters for a future surface, derive it against current bytes.

**Instrumentation hooks (always present; gated at runtime).** Every hook
  is injected unconditionally and self-gates on the constant
  `globalThis.__PFG_INSTRUMENT`, seeded once from `PFG_INSTRUMENT=1`. No
  build-time flag, no second code version; logging is off by default and
  toggled at launch. Per-PID logs to `/tmp/pfg-instr.${process.pid}.log`
  so multiple claude instances don't stomp on each other.

  R1  Reducer entry: every stream event, with deltaType + sigLen broken out
      (so `thinking_delta` / `signature_delta` / `text_delta` are
      distinguishable, and signature_delta streaming-in-pieces would
      surface as multiple `signature_delta` lines with sigLen growing).
  R2  Thinking-setter wrap: every React setState call.
  W1  Inside the absorbed thinking_delta writer body: progressive
      write attributed to this writer specifically. Distinguishes
      streaming writes (W1) from finalized-message writes (R2-only, no
      W1) in the same setter stream.
  L1  Renderer entry: streamingThinking shape on prop arrival.
  C2  Transcript component render: streamingThinking after destructure.
  M0  Memo comparator: previous-vs-current streamingThinking identity
      (catches React-memo-bypass regressions).
  M1  Transcript-extras useMemo recompute trigger.
  M2  Transcript-extras useMemo result counts.
  E1  Aggregator (post-useMemo transcript build) consumption of it.
  X1  chat:cancel handler: the bare-Esc interrupt moment. Reports the
      cancel-time streamingThinking from the R2 render stash and reads
      streamingText live off its signal.
  X3  Message-dispatch commit chokepoint (query-generator commits only).
      Blind by design to onCancel's setMessages commits; see X4.
  X2  normalize-filter entries (filterTrailingThinking /
      filterOrphanedThinking): whether thinking blocks survive into the
      next request.
  X4  Tier-1 onCancel commit: fires when the interrupted thinking
      summary is committed (the setMessages path X3 cannot see).

Discovery
=========

Anthropic re-minifies identifiers every release. Rather than versioning
the patcher per release, every minified name we touch is discovered by
structural pattern at synthesis time. Each lookup asserts a unique match;
0 or >1 hits fail the build loudly so we never silently mis-patch.

Every minified name the splices substitute is discovered, roughly fifty
of them: the renderer function and its React-Forget prologue, the live
and transcript callsite tails, the transcript component and its
signature tail, the whole transcript-extras useMemo (its variable,
useMemo namespace, streamingToolUses array, callback param, local, the
virtual-message factory, the uuid helper, the wrapper), both reducer
halves with their option aliases, the onCancel commit and its store and
setMessages, the streamingThinking useState pair and React namespace,
the memo comparator, the transcript aggregator with its skip predicates,
the message-dispatch callback, the streamingText signal and its writer,
the cancel telemetry source converter, and the two request-shaping
thinking filters.

Discovering every substituted name is what keeps a bundle bump to a
re-anchor rather than a rewrite. Brace matching for the callsite spans
goes through `pfg.jslex`, which is lexer-aware: a naive counter picks
the wrong close brace when one hides in a string, a template literal or
a regex, and the resulting truncation still parses.

Usage
=====

::

    util/patch_streaming_thinking.py <pristine-binary> [-o <out>]

Instrumentation is always built in and gated at runtime: it stays off
unless launched with `PFG_INSTRUMENT=1`. Output path defaults to
`<input>.pfg` if `-o` is omitted.

Input
=====

A pristine bun-packed Claude Code CLI binary (Linux ELF with a `.bun`
section as parsed by `util/bun_handler`). Get it from npm:

::

    npm pack @anthropic-ai/claude-code-linux-x64@<version>
    tar xf anthropic-ai-claude-code-linux-x64-<version>.tgz
    # Then the pristine binary is at package/claude

DO NOT pre-apply connoisseur's display patches or any other in-bundle
modifications - this patcher applies connoisseur's display tweaks
itself, in-process, via the vendored `vendor/connoisseur/patch-claude-
display.ts` subtree (run with `node --experimental-strip-types
--disable thinking-streaming`, since Patch S owns the thinking-streaming
surface end-to-end).

Output: a final patched binary with three layers in this order.

1. Connoisseur's display tweaks. Patch IDs match the v2.0 plan's
   PATCH_MODULES enumeration; function names are connoisseur's exports
   in `patch-claude-display.ts`. CON-F (thinking-streaming) is
   explicitly disabled because Patch S below owns that surface.

   - CON-A (`patchCollapsedReadSearch`): verbose tool-call rendering.
   - CON-B (`patchWriteCreateDiffColors`): write + create diff colors.
   - CON-C (`patchWordDiffLineBackgrounds`): word-diff line backgrounds.
   - CON-D (`patchThinkingCase`): inline thinking display.
   - CON-E (`patchRedactedThinkingSummaries`): inline redacted-thinking
     display. Applies 1/1.
   - CON-F (`patchThinkingStreaming`): SKIPPED. Patch S owns this
     surface end-to-end, and connoisseur's version of it applies here
     too, so the skip is what keeps two patchers off one piece of code.
   - CON-G (`patchSubagentPromptVisibility`): subagent prompt
     visibility outside transcript mode.
   - CON-H (`patchDisableSpinnerTips`): suppress spinner tips.
   - CON-I (`patchVersionOutput`): append (patched) to --version.
   - CON-J (`patchInstallerMigrationMessage`): rewrite the npm-to-
     native installer warning. 0-matches: the literal string is not in
     the bundle. Retained as required=False.

2. Patch S: the streaming-thinking restoration. Discovery-based
   writer + renderer end-to-end (see "Surfaces" above for the per-
   sub-patch breakdown). Replaces CON-F.

3. Instrumentation hooks, always built in and gated at runtime by the
   `PFG_INSTRUMENT` env var (see "Surfaces"), written to
   `/tmp/pfg-instr.${process.pid}.log`.

4. A syntax check of the spliced JS before it is repacked, so a malformed
   splice fails the build instead of the next launch.

Requirements on PATH: Node.js >= 22 (to run the vendored connoisseur
patcher via `--experimental-strip-types`) and bun (to parse the result;
Node cannot parse this bundle, see `bun_syntax_check`).
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
CONNOISSEUR_TS = os.path.join(
    REPO_ROOT, 'vendor', 'connoisseur', 'patch-claude-display.ts'
)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO_ROOT)
import bun_handler  # noqa: E402  (sys.path insert must precede this)
from pfg import jslex  # noqa: E402  (lexer-aware brace matching for callsite spans)


# Map connoisseur's --list-patches IDs (the labels in patch-claude-display.ts)
# to the v2.0 plan's PATCH_MODULES enumeration. Used to prefix the runtime
# echo of connoisseur's stdout so users see plan-level IDs next to the
# connoisseur-labelled output.
CONNOISSEUR_TO_CON_ID = {
    'tool-call-verbose':         'CON-A',
    'create-diff-colors':        'CON-B',
    'word-diff-line-bg':         'CON-C',
    'thinking-inline':           'CON-D',
    'redacted-thinking-inline':  'CON-E',
    'thinking-streaming':        'CON-F',  # disabled; never echoes
    'subagent-prompt':           'CON-G',
    'disable-spinner-tips':      'CON-H',
    'version-output':            'CON-I',
    'installer-label':           'CON-J',
    # welcome-badge runs as-is for now (rebrands to "Connoisseur's Code").
    # The v2.0 plan supersedes it with repo-authored Patch O that scopes
    # down to just the version-bearing site; not implemented separately
    # in this script yet.
    'welcome-badge':             'connoisseur-welcome (Patch O TBD)',
}


def apply_connoisseur_display_patches(js):
    """Run the vendored connoisseur display-patch transformations against
    the extracted JS. Disables connoisseur's thinking-streaming sub-patch
    (CON-F) because Patch S (the renderer + writer code below) owns that
    surface end-to-end; running both would edit the same code twice.
    """
    if not os.path.exists(CONNOISSEUR_TS):
        raise SystemExit(
            f'[connoisseur] missing vendored patcher at {CONNOISSEUR_TS}.\n'
            f'           Run: git subtree add --prefix=vendor/connoisseur '
            'https://github.com/a-connoisseur/patch-claude-code main --squash'
        )
    with tempfile.NamedTemporaryFile(
        suffix='.js', mode='w', delete=False, encoding='utf-8'
    ) as f:
        f.write(js)
        tmp = f.name
    try:
        result = subprocess.run(
            ['node', '--experimental-strip-types', CONNOISSEUR_TS,
             '--file', tmp, '--disable', 'thinking-streaming'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise SystemExit(
                '[connoisseur] node patcher failed.\n'
                f'  exit: {result.returncode}\n'
                f'  stdout: {result.stdout.strip()}\n'
                f'  stderr: {result.stderr.strip()}'
            )
        for line in result.stdout.splitlines():
            ls = line.strip()
            if not ls:
                continue
            if 'Patch summary' in ls or ls.startswith('Patched:'):
                print(f'  {ls}')
                continue
            if 'candidates' in ls:
                m = re.match(r'^([\w-]+)\s+candidates', ls)
                if m and m.group(1) in CONNOISSEUR_TO_CON_ID:
                    print(f'  [{CONNOISSEUR_TO_CON_ID[m.group(1)]}] {ls}')
                else:
                    print(f'  {ls}')
        with open(tmp, encoding='utf-8') as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def bun_syntax_check(js):
    """Parse the spliced JS before it goes back into the binary, so a malformed
    splice fails here instead of at the user's next launch.

    Checked with bun, because bun is what runs this bundle. `node --check` is
    not interchangeable: Node 22 cannot parse the `using` declarations the
    vendor bundle already ships, so it reports a syntax error on pristine input
    and would fail every build for a reason that has nothing to do with us.
    """
    with tempfile.NamedTemporaryFile(
        suffix='.js', mode='w', delete=False, encoding='utf-8', errors='surrogateescape'
    ) as f:
        f.write(js)
        tmp = f.name
    try:
        result = subprocess.run(
            ['bun', 'build', '--no-bundle', tmp, '--outfile=/dev/null'],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise SystemExit(
            '[check] bun not found on PATH. It is the runtime this bundle is '
            'packed for and the only parser that accepts it; install bun or '
            'run this on a host that has it.'
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if result.returncode != 0:
        raise SystemExit(
            '[check] bun rejected the patched JS; not writing the binary.\n'
            f'  exit: {result.returncode}\n'
            f'  {result.stderr.strip()[:2000]}'
        )
    print('  [check] bun parsed the patched JS')


def discover_names(js):
    """Find the per-release minified variable names by structural shape.
    Each entry asserts a unique match: if any returns 0 or >1 hits, the
    bundle drifted enough that we want a loud failure rather than silent
    mis-patching.

    Everything the splices below substitute is discovered here, so a bundle
    bump costs a re-anchor rather than a rewrite. Brace matching goes through
    `pfg.jslex`, which is lexer-aware: a naive counter picks the wrong close
    brace when one hides in a string, a template literal or a regex, and the
    resulting truncation still parses.
    """
    def find_one(pattern, label, group=1):
        return find_match(pattern, label).group(group)

    def find_match(pattern, label):
        matches = list(re.finditer(pattern, js))
        if len(matches) != 1:
            raise SystemExit(
                f"[discover] {label}: expected 1 match, got {len(matches)}\n"
                f"           pattern: {pattern[:120]!r}"
            )
        return matches[0]

    ID = r'[A-Za-z_$][\w$]*'
    names = {}
    # ---- renderer function -----------------------------------------------
    # Compiled through React Forget, so the props destructure sits in the
    # cache-miss branch rather than the `let`:
    #   function h3e(Nsh){let lBl=fBl.c(15),cBl,..;if(lBl[0]!==Nsh)({deferMessages:..}=Nsh),..
    m = find_match(
        rf'function ({ID})\(({ID})\)\{{let ({ID})=({ID})\.c\((\d+)\),'
        rf'[^;]{{0,200}};if\(\3\[0\]!==\2\)\(\{{deferMessages:',
        'renderer function (forget-memoized deferMessages destructure)',
    )
    names['render_fn'] = m.group(1)
    names['render_param'] = m.group(2)
    names['forget_cache'] = m.group(3)
    names['forget_ns'] = m.group(4)
    names['forget_n'] = m.group(5)
    names['render_entry'] = m.group(0)

    # ---- renderer callsites --------------------------------------------
    # Four sites call the renderer, and only two get streamingThinking. They are
    # told apart by props, never by order: the export site pins
    # conversationId:"export"; the static transcript export has screen:"transcript"
    # with no search wiring; the interactive transcript has onSearchMatchesChange
    # but no deferMessages; the live site is the one with deferMessages. These
    # are emitted through the JSX runtime (jsx/jsxs), not createElement.
    live_tail = transcript_tail = None
    for cm in re.finditer(rf'jsxs?\({re.escape(names["render_fn"])},\{{', js):
        open_brace = cm.end() - 1
        body = js[open_brace + 1:jslex._match_delim(js, open_brace)]
        if 'conversationId:"export"' in body:
            continue
        if 'deferMessages:' in body:
            live_tail = body[-200:]
        elif 'onSearchMatchesChange:' in body and 'agentDefinitions:' in body:
            transcript_tail = body[-200:]
    if live_tail is None:
        raise SystemExit('[discover] live-mode renderer callsite not found')
    if transcript_tail is None:
        raise SystemExit('[discover] transcript-mode renderer callsite not found')
    names['live_tail'] = live_tail
    names['transcript_tail'] = transcript_tail

    # ---- transcript component --------------------------------------------
    comp = find_match(
        rf'({ID})\s*=\s*\(\{{messages:{ID},tools:{ID},commands:{ID}[\s\S]{{0,900}}?'
        rf'(setPositions:{ID},disableRenderCap:{ID}=!1,renderRange:{ID}\}}\)=>\{{)',
        'transcript component signature',
    )
    names['comp_var'] = comp.group(1)
    names['comp_sig_tail'] = comp.group(2)

    # ---- the transcript-extras useMemo -----------------------------------
    # Folds streamingToolUses into the message list; patch D rewrites its body to
    # fold streamingThinking alongside, so every name inside it is substituted.
    memo = find_match(
        rf'({ID})=({ID})\.useMemo\(\(\)=>({ID})\.flatMap\(\(({ID})\)=>\{{'
        rf'let ({ID})=({ID})\(\{{content:\[\4\.contentBlock\]\}}\);'
        rf'return \5\.uuid=({ID})\(\4\.contentBlock\.id,0\),({ID})\(\[\5\]\)\}}\),\[\3\]\)',
        'transcript-extras useMemo',
    )
    names['memo_var'] = memo.group(1)
    names['usememo_ns'] = memo.group(2)
    names['tooluses_var'] = memo.group(3)
    names['memo_cb'] = memo.group(4)
    names['memo_local'] = memo.group(5)
    names['create_msg_helper'] = memo.group(6)
    names['uuid_helper'] = memo.group(7)
    names['wrap_helper'] = memo.group(8)
    names['memo_body'] = memo.group(0)

    # ---- the stream-event reducer, in two halves -------------------------
    # Split in two: a message-level function that destructures
    # onStreamingThinking, and an event-level one that handles content blocks and
    # deltas. The event-level half is where E.1/E.3/E.4/E.7 live, and it does NOT
    # destructure the thinking setter - but the message-level half hands it the
    # same options bag, so the setter is reachable as <opts>.onStreamingThinking.
    msg_fn = find_match(
        rf'function ({ID})\(({ID}),({ID})\)\{{let\{{[^}}]{{0,400}}'
        rf'onStreamingThinking:({ID})[^}}]{{0,400}}\}}=\3;',
        'message-level stream reducer',
    )
    names['msg_fn'] = msg_fn.group(1)
    names['msg_think_setter'] = msg_fn.group(4)
    evt_fn = find_match(
        rf'function ({ID})\(({ID}),({ID})(?:,{ID})?\)\{{let\{{onSetStreamMode:({ID}),'
        rf'onApiMetrics:({ID}),[^}}]{{0,400}}\}}=\3,',
        'event-level stream reducer',
    )
    names['evt_fn'] = evt_fn.group(1)
    names['evt_param'] = evt_fn.group(2)
    names['evt_opts'] = evt_fn.group(3)
    names['evt_set_mode'] = evt_fn.group(4)
    names['evt_api_metrics'] = evt_fn.group(5)
    # Assert the handoff: the message-level half must pass its own options bag to
    # the event-level one, or <opts>.onStreamingThinking inside it is a different
    # object and the writer would silently update nothing.
    find_one(
        rf'\}}{re.escape(names["evt_fn"])}\(({ID}),{re.escape(msg_fn.group(3))}\)\}}',
        'reducer handoff (event fn receives the message fn options bag)',
    )

    # ---- the interrupted-thinking commit in onCancel (Tier 1) -----------
    t1 = find_match(
        rf'if\(({ID})&&({ID})\.get\(\)\.thinkingStartedAt!==null\)'
        rf'({ID})\(\(({ID})\)=>\[\.\.\.\4,{re.escape(names["create_msg_helper"])}'
        rf'\(\{{content:\[\{{type:"thinking",thinking:\1,signature:""\}}\],isVirtual:!0\}}\)\]\);',
        'onCancel interrupted-thinking commit',
    )
    names['t1_text_var'] = t1.group(1)
    names['t1_store'] = t1.group(2)
    names['t1_set_messages'] = t1.group(3)
    names['t1_acc'] = t1.group(4)
    names['t1_anchor'] = t1.group(0)

    # ---- streamingThinking state pair + React namespace -----------------
    st = find_match(
        rf'\[({ID}),({ID})\]=({ID})\.useState\(null\);\3\.useEffect\(\(\)=>\{{if\(\1&&!\1\.isStreaming',
        'streamingThinking useState pair',
    )
    names['think_state'] = st.group(1)
    names['think_setter'] = st.group(2)
    names['react_ns'] = st.group(3)

    # ---- memo comparator wrapping the transcript component ---------------
    memo_cmp = find_match(
        rf'({ID})=({ID})\.memo\({re.escape(names["comp_var"])},'
        rf'\(({ID}),({ID})\)=>\{{let ({ID})=Object\.keys\(\3\);',
        'transcript component memo comparator',
    )
    names['memo_wrapper'] = memo_cmp.group(1)
    names['memo_ns'] = memo_cmp.group(2)
    names['cmp_next'] = memo_cmp.group(3)
    names['cmp_prev'] = memo_cmp.group(4)
    names['cmp_keys'] = memo_cmp.group(5)

    # ---- transcript aggregator consuming the useMemo result -------------
    agg = find_match(
        rf'({ID})=({ID})\(({ID})\.filter\(\(({ID})\)=>\4\.type!=="progress"\)'
        rf'\.filter\(\(\4\)=>!({ID})\(\4\)\)'
        rf'\.filter\(\(\4\)=>({ID})\(\4,({ID})\)\),{re.escape(names["memo_var"])}\)',
        'transcript aggregator call',
    )
    names['agg_out'] = agg.group(1)
    names['agg_fn'] = agg.group(2)
    names['agg_filtered_var'] = agg.group(3)
    names['agg_cb_param'] = agg.group(4)
    names['skip_pred1'] = agg.group(5)
    names['skip_pred2'] = agg.group(6)
    names['agg_mode_var'] = agg.group(7)
    names['agg_anchor'] = agg.group(0)

    # ---- message-dispatch callback ---------------------------------------
    # The commit chokepoint for query-generator messages. Early returns sit
    # ahead of the compactMetadata read, so anchor on the read itself and
    # walk out to the enclosing useCallback.
    disp = find_match(
        rf'({ID})=({ID})\.useCallback\(\(({ID})\)=>\{{if\(\3\.type==="system"'
        rf'[\s\S]{{0,400}}?\3\.compactMetadata\.preservedMessages,',
        'message-dispatch callback',
    )
    names['dispatch_fn'] = disp.group(1)
    names['dispatch_ns'] = disp.group(2)
    names['dispatch_arg'] = disp.group(3)

    # ---- streamingText ---------------------------------------------------
    # Held in a signal, read with .peek(), so X1 takes its cancel-moment value
    # straight off the signal rather than from a render-time stash.
    stext = find_match(
        rf'({ID})=({ID})\.useCallback\(\(({ID})\)=>\{{if\(!{ID}\)\{{'
        rf'if\(\3\(({ID})\.peek\(\)\)===null\)\4\.clear\(\);return\}}\4\.apply\(\3\)\}}',
        'streamingText signal writer',
    )
    names['stext_setter'] = stext.group(1)
    names['stext_signal'] = stext.group(4)

    # ---- cancel telemetry source converter (X1) --------------------------
    names['cancel_src_fn'] = find_one(
        rf'source:({ID})\("escape"\),streamMode:',
        'chat:cancel escape source converter',
    )

    # ---- request-shaping thinking filters (X2) ---------------------------
    # Both take an optional preserve-trailing flag, so the arity is matched
    # loosely and the body shape carries the identification.
    names['filter_trailing'] = find_one(
        rf'function ({ID})\(({ID})(?:,{ID}=!1)?\)\{{let ({ID})=\2\.at\(-1\);'
        rf'if\(!\3\|\|\3\.type!=="assistant"\)return \2;',
        'filterTrailingThinkingFromLastAssistant',
    )
    names['filter_orphaned'] = find_one(
        rf'function ({ID})\(({ID})(?:,{ID}=!1)?\)\{{let ({ID})=new Set;'
        rf'for\(let ({ID}) of \2\)\{{if\(\4\.type!=="assistant"\)continue;',
        'filterOrphanedThinkingOnlyMessages',
    )

    return names


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        print(f"usage: {sys.argv[0]} <input-binary> [-o <output>]")
        sys.exit(2)
    src = sys.argv[1]
    if '-o' in sys.argv:
        dst = sys.argv[sys.argv.index('-o') + 1]
    else:
        dst = src + '.pfg'

    data = open(src, 'rb').read()
    js = bun_handler.extract_js(data).decode('utf-8', errors='surrogateescape')
    print(f"input:        {src} ({len(data)} bytes)")
    print(f"JS extracted: {len(js)} bytes")

    print("\n--- connoisseur display patches (thinking-streaming disabled) ---")
    js = apply_connoisseur_display_patches(js)
    print(f"JS post-connoisseur: {len(js)} bytes")

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
        """Emit a runtime-gated `fs.appendFileSync` block that writes one
        tagged line to a per-PID log. The single gate is the constant
        `globalThis.__PFG_INSTRUMENT`, seeded once from `PFG_INSTRUMENT=1`.
        Every hook emits this same body, so there is exactly one version of
        each instrumented function: instrumentation is always present and
        toggled at runtime, never conditionally generated. Per-PID path lets
        multiple claude instances run concurrently without log races."""
        return (
            '    if (globalThis.__PFG_INSTRUMENT ??= process.env.PFG_INSTRUMENT === "1") try {\n'
            '      const __pfg_fs = require("fs");\n'
            '      __pfg_fs.appendFileSync(`/tmp/pfg-instr.${process.pid}.log`, '
            + payload + ');\n'
            '    } catch (__pfg_e) { /* log-write failure: swallow */ }\n'
        )

    def find_text(pattern, label):
        """Return the single matched span verbatim, for use as a splice anchor.
        Same verify-once discipline as discovery: a body we cannot pin uniquely
        is one we must not edit."""
        ms = list(re.finditer(pattern, js))
        if len(ms) != 1:
            raise SystemExit(
                f"[{label}] expected 1 match, got {len(ms)}\n"
                f"           pattern: {pattern[:120]!r}"
            )
        return ms[0].group(0)

    ID = r'[A-Za-z_$][\w$]*'
    ST = names['think_state']
    EVT = names['evt_param']
    OPTS = names['evt_opts']
    SET_MODE = names['evt_set_mode']
    CREATE_MSG = names['create_msg_helper']
    # The event-level reducer does not destructure the thinking setter (only the
    # message-level half does), so the writer reaches it through the options bag
    # both halves share. discover_names asserts that handoff.
    SET_THINK = f'{OPTS}.onStreamingThinking'

    print("\n--- production patches (A/B/C/D) ---")
    # A: live-mode renderer props
    splice(
        names['live_tail'] + '})',
        names['live_tail'] + f',/*pfg-streaming-thinking*/streamingThinking:{ST}}})',
        'A live-mode renderer props',
    )
    # B: transcript-mode renderer props
    splice(
        names['transcript_tail'] + '})',
        names['transcript_tail'] + f',/*pfg-streaming-thinking*/streamingThinking:{ST}}})',
        'B transcript-mode renderer props',
    )
    # C: transcript component signature
    splice(
        names['comp_sig_tail'],
        names['comp_sig_tail'].replace(
            '})=>{', ',/*pfg-streaming-thinking*/streamingThinking:__pfg_st})=>{'),
        'C component signature',
    )
    # D: transcript-extras useMemo
    uuid_h = names['uuid_helper']
    cb = names['memo_cb']
    local = names['memo_local']
    tools = names['tooluses_var']
    wrap = names['wrap_helper']
    splice(
        names['memo_body'],
        f'{names["memo_var"]}={names["usememo_ns"]}.useMemo(()=>{{\n'
        '    /* pfg-streaming-thinking: fold streamingToolUses + streamingThinking '
        'into one sorted transcript-extras list */\n'
        f'    const __pfg_toolExtras = {tools}.map(({cb}) => {{\n'
        f'      const {local} = {CREATE_MSG}({{content:[{cb}.contentBlock]}});\n'
        f'      {local}.uuid = {uuid_h}({cb}.contentBlock.id, 0);\n'
        '      return {\n'
        f'        index: {cb}.index ?? Number.MAX_SAFE_INTEGER,\n'
        f'        messages: {wrap}([{local}]),\n'
        '      };\n'
        '    });\n'
        '    const __pfg_thinkExtras = (__pfg_st?.messages ?? []).map((__pfg_entry, __pfg_idx) => ({\n'
        '      index: __pfg_entry.index ?? (Number.MAX_SAFE_INTEGER + __pfg_idx),\n'
        f'      messages: {wrap}([__pfg_entry.message ?? __pfg_entry]),\n'
        '    }));\n'
        '    return [...__pfg_toolExtras, ...__pfg_thinkExtras]\n'
        '      .sort((a, b) => a.index === b.index ? 0 : a.index - b.index)\n'
        '      .flatMap((e) => e.messages);\n'
        f'  }}, [{tools}, __pfg_st])',
        'D transcript-extras useMemo',
    )

    # ---- E.* writer patches, absorbed from connoisseur's patchThinkingStreaming.
    # All four live in the event-level reducer half, which is why they
    # address the setter through the options
    # bag rather than a destructured local.
    def writer_body_thinking_delta():
        """The progressive accumulator body injected at the head of the
        `case "thinking_delta":` branch. Keeps connoisseur's semantics (mutable
        map + replaced-flag fallback). The W1 log self-gates via logwrite, so the
        body is one version regardless of whether instrumentation is on."""
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
            f'    let __pfg_nextDelta = typeof {EVT}.event.delta.thinking === "string"\n'
            f'          ? {EVT}.event.delta.thinking : "",\n'
            f'        __pfg_nextText = (__pfg_prev?.thinking ?? "") + __pfg_nextDelta,\n'
            f'        __pfg_nextIdx = __pfg_prev?.currentIndex ?? {EVT}.event.index,\n'
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
            f'streamingEndedAt: void 0, currentIndex: {EVT}.event.index, '
            f'currentMessage: __pfg_nextMsg, messages: [{{index: {EVT}.event.index, '
            f'message: __pfg_nextMsg}}]}};\n'
            f'  }});'
        )

    print("\n--- absorbed writer patches (E.1/E.3/E.4/E.7) ---")
    # E.1: reset at stream_request_start so a prior turn does not bleed forward.
    splice(
        f'if({EVT}.type==="stream_request_start"){{{SET_MODE}?.("requesting");return}}',
        f'if({EVT}.type==="stream_request_start"){{'
        f'{SET_THINK}?.(null),{SET_MODE}?.("requesting");return}}',
        'E.1 stream_request_start reset',
    )
    # E.3: seed an empty accumulator (or the redacted payload) on a thinking
    # content_block_start, so the first delta has something to extend and the
    # transcript shows a live row immediately.
    splice(
        f'case"thinking":case"redacted_thinking":{SET_MODE}?.("thinking");return;',
        f'case"thinking":case"redacted_thinking":{SET_THINK}?.((__pfg_prev) => {{'
        f'let __pfg_msg = {CREATE_MSG}({{content:['
        f'{EVT}.event.content_block.type==="redacted_thinking"'
        f'?{{type:"redacted_thinking",data:{EVT}.event.content_block.data??""}}'
        f':{{type:"thinking",thinking:""}}'
        f'],isVirtual:!0}});'
        f'return{{thinking:{EVT}.event.content_block.type==="redacted_thinking"'
        f'?{EVT}.event.content_block.data??"":"",'
        f'isStreaming:!0,streamingEndedAt:void 0,'
        f'currentIndex:{EVT}.event.index,currentMessage:__pfg_msg,'
        f'messages:[...(__pfg_prev?.messages??[]),{{index:{EVT}.event.index,message:__pfg_msg}}]}};'
        f'}}),{SET_MODE}?.("thinking");return;',
        'E.3 thinking content_block_start init',
    )
    # E.4: on text content_block_start, snapshot to non-streaming so the finished
    # thinking stays readable while the response generates.
    splice(
        f'case"text":{SET_MODE}?.("responding");return;',
        f'case"text":{SET_THINK}?.((__pfg_prev) => __pfg_prev'
        f'?{{...__pfg_prev,isStreaming:!1,streamingEndedAt:void 0,'
        f'currentIndex:null,currentMessage:null}}:__pfg_prev),'
        f'{SET_MODE}?.("responding");return;',
        'E.4 text content_block_start snapshot',
    )
    # E.7: the progressive writer. Anchored on the whole existing branch so the
    # api-metrics token counting downstream of it is preserved verbatim.
    OLD_TD_BODY = find_text(
        rf'case"thinking_delta":\{{let\{{delta:{ID}\}}={re.escape(EVT)}\.event;'
        rf'[\s\S]{{0,500}}?return\}}',
        'E.7 thinking_delta branch',
    )
    new_td_body = (
        'case"thinking_delta":{\n'
        '  /* pfg-streaming-thinking E.7: absorbed progressive writer (W1 self-gated). */\n'
        '  ' + writer_body_thinking_delta() + '\n'
        '  '
        + OLD_TD_BODY[len('case"thinking_delta":{'):]
    )
    splice(OLD_TD_BODY, new_td_body, 'E.7 thinking_delta writer')

    # Tier 1: the interrupted-thinking commit in onCancel.
    # Pristine already commits the streamed summary on Esc as a virtual thinking
    # block (isVirtual:!0, empty signature). Rewrite that one commit to append a
    # cut marker and reuse the streaming preview's uuid, so the committed block
    # takes the preview's place instead of landing beside it, then clear the
    # preview.
    #
    # This changes rendering only; the block stays virtual and never reaches the
    # model. Two separate exclusions on the request path drop it: N3's message
    # loop skips it via Edt -> lBr (isVirtual), and atr
    # (filterOrphanedThinkingOnlyMessages) drops assistant messages whose content
    # is entirely thinking blocks, which is this commit's exact shape. Emitting a
    # text block instead would clear both, and would also avoid sending a thinking
    # block without the signature it was issued with. Verified against 2.1.232.
    #
    # X4 (the commit observation) is embedded here and self-gates via logwrite.
    # onCancel commits through setMessages, which X3 cannot see, so X4 is the only
    # direct trace of the Tier-1 commit firing.
    print("\n--- Tier 1 (interrupted-thinking commit) ---")
    T1_TEXT = names['t1_text_var']
    splice(
        names['t1_anchor'],
        f'if({T1_TEXT}&&{names["t1_store"]}.get().thinkingStartedAt!==null){{\n'
        + logwrite(f'`[pfg-instr X4 tier1-finalize kind=thinking len=${{{T1_TEXT}.length}}]\\n`')
        + f'let __pfg_cur={ST}?.currentMessage,'
        f'__pfg_fin={CREATE_MSG}({{content:[{{type:"thinking",thinking:'
        f'{T1_TEXT}+"\\n\\n[Interrupted by the user here. The above is my reasoning so '
        'far, cut off mid-thought.]",signature:""}],isVirtual:!0});'
        'if(__pfg_cur&&__pfg_cur.uuid)__pfg_fin.uuid=__pfg_cur.uuid;'
        '/* Tier1 gray-finalize: commit the partial thinking as a real thinking '
        'block reusing the streaming-preview identity, then drop the preview, so '
        'the gray block renders once (no white text, no seam, no leftover). */'
        f'{names["t1_set_messages"]}(({names["t1_acc"]})=>[...{names["t1_acc"]},__pfg_fin]);'
        f'{names["think_setter"]}(null);}}',
        'Tier1 onCancel interrupted-thinking commit (+X4)',
    )

    def apply_pfg_instr():
        print("\n--- instrumentation hooks (always present; toggle at runtime with PFG_INSTRUMENT=1) ---")

        # R1: reducer entry. The message-level half is the single door every
        # event goes through (it forwards stream events to the event-level half),
        # so one line lands per event, with the delta/content-block shape
        # broken out.
        msg_fn = names['msg_fn']
        splice(
            find_text(rf'function {re.escape(msg_fn)}\({ID},{ID}\)\{{let\{{', 'R1 reducer entry'),
            f'function {msg_fn}({EVT},{OPTS})' + '{\n'
            '  /* pfg-instr R1: every event reaching the stream-handler. */\n'
            + logwrite(
                f'`[pfg-instr R1 reducer type=${{{EVT}?.type}} eventType=${{{EVT}?.event?.type}} ` +\n'
                f'      `deltaType=${{{EVT}?.event?.delta?.type ?? ""}} ` +\n'
                f'      `sigLen=${{{EVT}?.event?.delta?.signature?.length ?? 0}} ` +\n'
                f'      `blockType=${{{EVT}?.event?.content_block?.type ?? ""}} ` +\n'
                f'      `blockIndex=${{{EVT}?.event?.index ?? -1}}]\\n`'
            )
            + '  let{',
            'R1 reducer entry',
        )
        # R2: wrap the streamingThinking setter, and stash the current value on
        # every render so X1 can report the cancel-moment state inline.
        splice(
            f'[{ST},{names["think_setter"]}]={names["react_ns"]}.useState(null)',
            f'[{ST}, __pfg_setThinking_real] = {names["react_ns"]}.useState(null),\n'
            '  __pfg_cache = ((globalThis.__pfg_cache = globalThis.__pfg_cache || {}), globalThis.__pfg_cache),\n'
            f'  __pfg_stash_st = (__pfg_cache.lastSt = {ST}, __pfg_cache.lastStAt = Date.now(), 0),\n'
            '  /* pfg-instr R2: wrap the setter with kind discrimination. */\n'
            f'  {names["think_setter"]} = (__pfg_arg) => {{\n'
            + logwrite(
                '`[pfg-instr R2 setThinking t=${typeof __pfg_arg} ` +\n'
                '      `kind=${typeof __pfg_arg === "function" ? "func" : '
                '__pfg_arg === null ? "null" : '
                '(__pfg_arg && typeof __pfg_arg === "object" && "isStreaming" in __pfg_arg) ? '
                '(__pfg_arg.isStreaming ? "streaming" : "finalized") : "other"}]\\n`'
            )
            + '    return __pfg_setThinking_real(__pfg_arg);\n'
            '  }',
            'R2 thinking-setter wrap',
        )
        # L1: renderer entry. The props destructure sits behind a React-Forget
        # cache check, so the hook goes ahead of the whole discovered prologue.
        rp = names['render_param']
        splice(
            names['render_entry'],
            f'function {names["render_fn"]}({rp})' + '{\n'
            '  /* pfg-instr L1: renderer receives props; log streamingThinking shape. */\n'
            + logwrite(
                f'`[pfg-instr L1 render hasST=${{{rp}?.streamingThinking ? "y" : "n"}} ` +\n'
                f'      `msgs=${{{rp}?.streamingThinking?.messages?.length || 0}} ` +\n'
                f'      `thinkLen=${{{rp}?.streamingThinking?.thinking?.length || 0}} ` +\n'
                f'      `streamingToolUses=${{{rp}?.streamingToolUses?.length || 0}}]\\n`'
            )
            + '  ' + names['render_entry'][len(f'function {names["render_fn"]}({rp}){{'):],
            'L1 renderer entry',
        )
        # C2: component body entry (keyed off patch C's own output).
        splice(
            ',/*pfg-streaming-thinking*/streamingThinking:__pfg_st})=>{',
            ',/*pfg-streaming-thinking*/streamingThinking:__pfg_st})=>{\n'
            '  /* pfg-instr C2: component render + streamingThinking shape on arrival. */\n'
            + logwrite(
                '`[pfg-instr C2 component hasST=${__pfg_st ? "y" : "n"} ` +\n'
                '      `msgs=${(__pfg_st && __pfg_st.messages) ? __pfg_st.messages.length : 0} ` +\n'
                '      `thinkLen=${(__pfg_st && __pfg_st.thinking) ? __pfg_st.thinking.length : 0} ` +\n'
                '      `isStreaming=${__pfg_st ? !!__pfg_st.isStreaming : false}]\\n`'
            ),
            'C2 component render',
        )
        # M0: memo comparator entry.
        nxt, prv = names['cmp_next'], names['cmp_prev']
        splice(
            f'{names["memo_wrapper"]}={names["memo_ns"]}.memo({names["comp_var"]},'
            f'({nxt},{prv})=>{{let {names["cmp_keys"]}=Object.keys({nxt});',
            f'{names["memo_wrapper"]}={names["memo_ns"]}.memo({names["comp_var"]},'
            f'({nxt},{prv})=>{{\n'
            '  /* pfg-instr M0: memo comparator firing + streamingThinking identity. */\n'
            + logwrite(
                f'`[pfg-instr M0 memo-cmp newSThas=${{({nxt} && {nxt}.streamingThinking) ? "y" : "n"}} ` +\n'
                f'      `prevSThas=${{({prv} && {prv}.streamingThinking) ? "y" : "n"}} ` +\n'
                f'      `sameRef=${{{nxt}?.streamingThinking === {prv}?.streamingThinking}}]\\n`'
            )
            + f'  let {names["cmp_keys"]}=Object.keys({nxt});',
            'M0 memo comparator entry',
        )
        # M1: useMemo body entry (keyed off patch D's own output).
        tools_v = names['tooluses_var']
        splice(
            f'{names["memo_var"]}={names["usememo_ns"]}.useMemo(()=>{{\n'
            '    /* pfg-streaming-thinking:',
            f'{names["memo_var"]}={names["usememo_ns"]}.useMemo(()=>{{\n'
            f'    /* pfg-instr M1: recompute ({tools_v} or __pfg_st changed). */\n'
            + logwrite(
                f'`[pfg-instr M1 memo tools=${{{tools_v}.length}} ` +\n'
                '      `stMsgs=${(__pfg_st && __pfg_st.messages) ? __pfg_st.messages.length : 0}]\\n`'
            )
            + '    /* pfg-streaming-thinking:',
            'M1 useMemo compute',
        )
        # M2: useMemo result.
        splice(
            'return [...__pfg_toolExtras, ...__pfg_thinkExtras]\n'
            '      .sort((a, b) => a.index === b.index ? 0 : a.index - b.index)\n'
            '      .flatMap((e) => e.messages);\n'
            f'  }}, [{tools_v}, __pfg_st])',
            'const __pfg_result = [...__pfg_toolExtras, ...__pfg_thinkExtras]\n'
            '      .sort((a, b) => a.index === b.index ? 0 : a.index - b.index)\n'
            '      .flatMap((e) => e.messages);\n'
            '    /* pfg-instr M2: result size + per-channel counts. */\n'
            + logwrite(
                '`[pfg-instr M2 result=${__pfg_result.length} ` +\n'
                '      `toolE=${__pfg_toolExtras.length} ` +\n'
                '      `thinkE=${__pfg_thinkExtras.length}]\\n`'
            )
            + '    return __pfg_result;\n'
            f'  }}, [{tools_v}, __pfg_st])',
            'M2 useMemo result',
        )
        # E1: aggregator consumption of the useMemo result.
        agg_cb = names['agg_cb_param']
        splice(
            names['agg_anchor'],
            f'{names["agg_out"]} = ((__pfg_memo) => {{\n'
            f'      const __pfg_agg = {names["agg_fn"]}(\n'
            f'        {names["agg_filtered_var"]}.filter(({agg_cb}) => {agg_cb}.type !== "progress")\n'
            f'          .filter(({agg_cb}) => !{names["skip_pred1"]}({agg_cb}))\n'
            f'          .filter(({agg_cb}) => {names["skip_pred2"]}({agg_cb}, {names["agg_mode_var"]})),\n'
            '        __pfg_memo\n'
            '      );\n'
            '      /* pfg-instr E1: aggregator consumption of the transcript extras. */\n'
            + logwrite(
                '`[pfg-instr E1 agg memo=${__pfg_memo.length} ` +\n'
                f'      `filteredIn=${{{names["agg_filtered_var"]}.length}} out=${{__pfg_agg.length}}]\\n`'
            )
            + '      return __pfg_agg;\n'
            f'    }})({names["memo_var"]})',
            'E1 aggregator wrap',
        )
        # X1: the bare-Escape chat:cancel handler. Anchored on the telemetry
        # source converter, which is unique to this handler. streamingText is a
        # signal, so its cancel-moment value is read straight off it; only
        # streamingThinking needs the R2 render stash.
        csf = names['cancel_src_fn']
        splice(
            f'source:{csf}("escape"),streamMode:',
            'source:(() => {\n'
            '    /* pfg-instr X1: chat:cancel fired (bare Escape during streaming).\n'
            '     * streamingThinking comes from the R2 render stash; streamingText is\n'
            '     * read live off the signal. */\n'
            '    const __pfg_c = (globalThis.__pfg_cache = globalThis.__pfg_cache || {});\n'
            '    const __pfg_st = __pfg_c.lastSt;\n'
            f'    let __pfg_text = null; try {{ __pfg_text = {names["stext_signal"]}.peek(); }} catch (__pfg_e) {{}}\n'
            + logwrite(
                '`[pfg-instr X1 cancel handler=chat-cancel source=escape ` +\n'
                '      `stHas=${__pfg_st ? "y" : "n"} ` +\n'
                '      `stThinkLen=${__pfg_st?.thinking?.length ?? 0} ` +\n'
                '      `stMsgs=${__pfg_st?.messages?.length ?? 0} ` +\n'
                '      `stIsStreaming=${__pfg_st?.isStreaming ?? false} ` +\n'
                '      `stEndedAt=${__pfg_st?.streamingEndedAt ?? 0} ` +\n'
                '      `textHas=${__pfg_text != null ? "y" : "n"} ` +\n'
                '      `textLen=${(typeof __pfg_text === "string") ? __pfg_text.length : 0}]\\n`'
            )
            + f'    return {csf}("escape");\n'
            '  })(),streamMode:',
            f'X1 chat:cancel handler ({csf})',
        )
        # X3: the message-dispatch commit chokepoint for query-generator messages.
        # NOT the only commit path: onCancel commits through setMessages directly,
        # which X3 does not see, so the Tier-1 commit is observed by X4 instead.
        dfn, darg = names['dispatch_fn'], names['dispatch_arg']
        splice(
            f'{dfn}={names["dispatch_ns"]}.useCallback(({darg})=>{{if(',
            f'{dfn}={names["dispatch_ns"]}.useCallback(({darg})=>{{\n'
            '  /* pfg-instr X3: message-dispatch callback invoked. */\n'
            + logwrite(
                f'`[pfg-instr X3 commit type=${{{darg}?.type}} ` +\n'
                f'      `uuid=${{{darg}?.uuid ?? ""}} ` +\n'
                f'      `msgId=${{{darg}?.message?.id ?? ""}} ` +\n'
                f'      `blocks=${{Array.isArray({darg}?.message?.content) ? {darg}.message.content.length : -1}} ` +\n'
                f'      `blockTypes=${{Array.isArray({darg}?.message?.content) ? '
                f'{darg}.message.content.map((__pfg_b) => __pfg_b?.type ?? "?").join(",") : ""}} ` +\n'
                f'      `isVirtual=${{{darg}?.isVirtual ? "y" : "n"}} ` +\n'
                f'      `isErr=${{{darg}?.isApiErrorMessage ? "y" : "n"}}]\\n`'
            )
            + '  if(',
            f'X3 message-commit wrap ({dfn})',
        )
        # X2.a / X2.b: the two request-shaping thinking filters. Whether a
        # thinking block survives into the next request is decided here, which is
        # what makes the Tier-1 commit observable end to end.
        for key, tag, note in (
            ('filter_trailing', 'X2.a filterTrailing',
             'filterTrailingThinkingFromLastAssistant'),
            ('filter_orphaned', 'X2.b filterOrphaned',
             'filterOrphanedThinkingOnlyMessages'),
        ):
            fname = names[key]
            head = find_text(
                rf'function {re.escape(fname)}\(({ID})(?:,{ID}=!1)?\)\{{',
                f'{tag} entry',
            )
            arg = re.match(rf'function {re.escape(fname)}\(({ID})', head).group(1)
            splice(
                head,
                head + '\n'
                f'  /* pfg-instr {tag}: {note} entry. */\n'
                + logwrite(
                    f'`[pfg-instr {tag} entry inMsgs=${{{arg}?.length ?? 0}} ` +\n'
                    f'      `lastIsAssistant=${{({arg}?.at(-1)?.type === "assistant") ? "y" : "n"}} ` +\n'
                    f'      `thinkingOnly=${{(()=>{{ try {{ let n = 0; for (const __pfg_m of {arg}) {{ '
                    'if (__pfg_m?.type !== "assistant") continue; const __pfg_c = __pfg_m.message?.content; '
                    'if (!Array.isArray(__pfg_c) || __pfg_c.length === 0) continue; '
                    'if (__pfg_c.every((__pfg_b) => __pfg_b?.type === "thinking" || '
                    '__pfg_b?.type === "redacted_thinking")) n++; } '
                    'return n; } catch (__pfg_e) { return -1; } })()}]\\n`'
                ),
                f'{tag} entry ({fname})',
            )
    apply_pfg_instr()

    print("\n--- syntax check ---")
    bun_syntax_check(js)

    new_data = bun_handler.repack_with_js(data, js.encode('utf-8', errors='surrogateescape'))
    print(f"\nfinal JS: {len(js)} bytes")
    print(f"binary:   {len(new_data)} bytes (delta {len(new_data) - len(data):+d})")
    open(dst, 'wb').write(new_data)
    os.chmod(dst, 0o755)
    print(f"wrote {dst}")


if __name__ == '__main__':
    main()
