#!/usr/bin/env python3
"""Add a per-source "Tool results by source" breakdown to /context on the
bun-packed native Claude CLI, in BOTH the markdown table and the interactive
(ink) view.

The problem
===========

/context already buckets tool-result tokens by tool NAME (the in-bundle
``toolResultsByType`` map) and the suggestion line can tell you "Read results
using 350k tokens". What it never tells you is WHICH files those reads were, or
which commands the Bash output came from, the one thing you actually need to see
what is filling the Messages bucket and which re-reads to stop making.

What this patch adds
====================

It attributes every tool result to its source, the file for Read/Edit/Write, the
command for Bash, the pattern for Grep/Glob, the url for WebFetch, aggregating
repeat results for the same source so re-reads show up with a count, and renders
a "Tool results by source" section (top 15 by tokens, with a named "N more
sources" rollup) right after Skills, in both /context surfaces:

  * the markdown table (``formatContextAsMarkdownTable`` / ``SWt``), and
  * the interactive ink view (the ``ContextVisualization`` component), as a tree
    matching the Skills tree.

Patch sites
===========

Data layer (feeds both surfaces):
  1. ``c3p`` (approximateMessageTokens) init: add a ``toolResultsByDetail`` map.
  2. ``c3p`` tool_use id->name build loop: store ``{name, target}`` instead of
     just the tool name, deriving ``target`` from the tool_use input.
  3. ``a3p`` (processUserMessage): read ``.name`` off the new record, keep the
     existing per-name bucket, and accumulate the per-source detail.
  4. ``vWn`` (analyzeContextUsage): add a sorted ``toolResultsByDetail`` array to
     the formatted messageBreakdown both surfaces read.

Render layer:
  5. ``SWt`` (formatContextAsMarkdownTable): emit the markdown table after Skills.
  6. ``ContextVisualization``: repurpose the dead ``messageBreakdown && false``
     render slot to render the per-source tree (collapse-reactive).
  7. grid category: split tool-result tokens out of "Messages" into a distinct red
     "Tool results" category (proportional when the bucket is reconciled).
  11. collapsed MCP-tools section: show the top tools (loaded-first, tokens-desc)
     plus an "N more tools" rollup instead of the bare one-line count.

Transcript-mode collapse (Ctrl+O):
  8a/8b. ``/context`` mounts: bake a collapsed and an expanded snapshot and join
     them with a U+001E-wrapped delimiter for a live component to choose between.
  9. ``UserTextMessage`` local-command-stdout branch: split on the delimiter and
     show the expanded half when ``verbose || isTranscriptMode``, re-balancing the
     wrapper tags so the receiver parses a well-formed block.
  12. ``GVn`` (SDK / external-viewer converter): strip the delimiter, showing the
     collapsed half rather than the raw combined string.

Conventions
===========

Injected code is written readably (multi-line, commented), never minified to
mirror the bundle; byte-stability of the repack holds either way.

Anthropic re-minifies locals every release. The stable anchors here are property
names (``toolResultsByType``, ``skillFrontmatter``) and string literals
(``### Skills``, ``messageBreakdown:``); those are matched directly. The drifting
single-letter locals (breakdown var, output-string var, formatTokens helper,
React/Box/Text/Tree aliases, the dead-slot cache indices) are discovered by
structural regex and asserted to match exactly once, so the patch aborts before
writing anything rather than silently mis-applying across a version bump. The
local identities were read from native build 2.1.185; the structural patterns are
version-independent and will fail loud if a future minifier reshapes them.

Usage
=====

::

    util/patch_context_read_breakdown.py <input-binary> [-o <output>]

Output defaults to ``<input>.ctxrb``. Targets the bun ``.bun``-section ELF form
via ``util/bun_handler`` (Linux x64).
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bun_handler  # noqa: E402  (sys.path insert must precede this)


def sub(template, **names):
    """Fill @NAME@ placeholders in a readable JS template with discovered
    minified identifiers."""
    out = template
    for key, val in names.items():
        out = out.replace('@' + key + '@', val)
    leftover = re.search(r'@[A-Z0-9_]+@', out)
    if leftover:
        raise SystemExit(f"[template] unresolved placeholder {leftover.group(0)}")
    return out


# --- readable injected JS (filled with discovered names at apply time) ---------

# Derives a short, readable source label from a tool_use input. Used at the
# id->name build loop (site 2).
TOOL_TARGET_FN = r'''(function pcbToolTarget(input) {
          /* Sanitize every label centrally: strip control chars (NUL, newline, tab,
             ESC, U+001E RS) and angle brackets, collapse whitespace, cap length. This
             keeps a source label from forging a <local-command-stdout> wrapper tag,
             injecting ANSI, carrying a newline into the table/tree, or colliding with
             the U+001E delimiter once it is baked into the snapshot. */
          const clean = (s) => {
            if (typeof s !== "string") return null;
            let out = ""; for (let k = 0; k < s.length; k++) { const c = s.charCodeAt(k); out += (c < 32 || c === 60 || c === 62) ? " " : s[k]; } out = out.replace(/ +/g, " ").trim().slice(0, 80);
            return out.length > 0 ? out : null;
          };
          try {
            if (!input || typeof input !== "object") return null;
            const pick = (k) => (typeof input[k] === "string" && input[k].length > 0 ? input[k] : undefined);
            const path = pick("file_path") || pick("notebook_path");
            if (path) {
              const home = process.env.HOME;
              let p = home && path.indexOf(home) === 0 ? "~" + path.slice(home.length) : path;
              /* cap from the tail so the filename stays visible (paths are the longest labels) */
              if (p.length > 80) p = "..." + p.slice(p.length - 77);
              return clean(p);
            }
            const command = pick("command");
            if (command) return clean(command);
            const pattern = pick("pattern");
            if (pattern) return clean(pattern);
            const url = pick("url");
            if (url) return clean(url);
            const generic = pick("query") || pick("description") || pick("path");
            return clean(generic);
          } catch (e) { return null; }
        })("input" in @BLK@ ? @BLK@.input : void 0)'''

# Replaces the build loop's `<map>.set(<id>, <name>)` with a {name, target} record.
BUILD_SET = (
    'if (@IDV@) @MPV@.set(@IDV@, {\n'
    '        name: @NMV@,\n'
    '        /* pcb: label this tool call by what it acted on (file, command, ...) */\n'
    '        target: ' + TOOL_TARGET_FN + ',\n'
    '      })'
)

# Replaces a3p's tool_result body: keep the per-name bucket, add per-source detail.
A3P_BODY = (
    'let @IDV@ = "tool_use_id" in @BLK@ ? @BLK@.tool_use_id : void 0,\n'
    '          /* pcb: the build map now stores {name, target} */\n'
    '          pcbRec = @IDV@ ? @MPV@.get(@IDV@) : void 0,\n'
    '          @NMV@ = (pcbRec && pcbRec.name) || "unknown";\n'
    '      @BDV@.toolResultsByType.set(@NMV@, (@BDV@.toolResultsByType.get(@NMV@) || 0) + @TOKV@);\n'
    '      if (@BDV@.toolResultsByDetail) {\n'
    '        /* pcb: per-source detail; repeat results for one source are merged,\n'
    '           so re-reading a file shows as a single row with a count. */\n'
    '        const pcbTarget = (pcbRec && pcbRec.target) || @NMV@,\n'
    '              pcbKey = @NMV@ + "\\0" + pcbTarget,\n'
    '              pcbEntry = @BDV@.toolResultsByDetail.get(pcbKey);\n'
    '        if (pcbEntry) { pcbEntry.tokens += @TOKV@; pcbEntry.count += 1; }\n'
    '        else @BDV@.toolResultsByDetail.set(pcbKey, { toolName: @NMV@, target: pcbTarget, tokens: @TOKV@, count: 1 });\n'
    '      }'
)

# Markdown section emitted after the Skills table (site 5).
MD_SECTION = (
    'if (@D@ && @D@.toolResultsByDetail && @D@.toolResultsByDetail.length > 0) {\n'
    '    /* pcb: the first 15 sources OR every source >= 300 tokens, whichever reaches\n'
    '       further down the (tokens-desc) list; the smaller long tail rolled into one\n'
    '       "N more sources" line (with its token total). */\n'
    '    const pcbRows = @D@.toolResultsByDetail,\n'
    '          pcbCut = Math.max(15, pcbRows.filter((r) => r.tokens >= 300).length),\n'
    '          pcbBig = pcbRows.slice(0, pcbCut),\n'
    '          pcbSmall = pcbRows.slice(pcbCut),\n'
    '          pcbSmallTokens = pcbSmall.reduce((a, b) => a + b.tokens, 0), pcbEsc = (s) => String(s).split("|").join(String.fromCharCode(92) + "|"), pcbLabel = (r) => r.toolName === r.target ? r.toolName : r.toolName + " " + r.target;\n'
    '    @OUT@ += `### Tool results by source\\n\\n`;\n'
    '    @OUT@ += `| Source | Count | Tokens |\\n`;\n'
    '    @OUT@ += `|--------|-------|--------|\\n`;\n'
    '    for (const r of pcbBig) {\n'
    '      @OUT@ += `| ${pcbEsc(pcbLabel(r))} | ${r.count} | ${@FMT@(r.tokens)} |\\n`;\n'
    '    }\n'
    '    if (pcbSmall.length > 0) {\n'
    '      @OUT@ += `| ${pcbSmall.length} more sources | | ${@FMT@(pcbSmallTokens)} |\\n`;\n'
    '    }\n'
    '    @OUT@ += `\\n`;\n'
    '  }'
)

# Ink tree rendered in the repurposed dead slot (site 6). Mirrors the Skills tree:
# a bold header + " \xB7 by source" hint, then a tree of rows.
INK_RENDER = (
    '@S@ && @S@.toolResultsByDetail && @S@.toolResultsByDetail.length > 0\n'
    '        ? (() => {\n'
    '            const rows = @S@.toolResultsByDetail;\n'
    '            /* collapsed (@I@ truthy): show the first 15 sources OR every source\n'
    '               above 300 tokens, whichever reaches further down the (already\n'
    '               tokens-desc) list, then a rolled-up "+N more sources: total" line.\n'
    '               expanded (/context all, Ctrl+O): every source. */\n'
    '            const pcbCut = @I@ ? Math.max(15, rows.filter((r) => r.tokens >= 300).length) : rows.length;\n'
    '            const big = rows.slice(0, pcbCut);\n'
    '            const small = rows.slice(pcbCut);\n'
    '            const node = (row, idx) => @OR@.createElement(@TREE@.Node, { key: idx },\n'
    '              @OR@.createElement(@TXT@, null,\n'
    '                (row.toolName === row.target ? row.toolName : row.toolName + " " + row.target) + (row.count > 1 ? " \\xD7" + row.count : ""), ":", " ",\n'
    '                @OR@.createElement(@TXT@, { dimColor: !0 }, @FMT@(row.tokens), " tokens")));\n'
    '            const children = big.map(node);\n'
    '            if (small.length > 0) {\n'
    '              const tot = small.reduce((a, b) => a + b.tokens, 0);\n'
    '              children.push(@OR@.createElement(@TREE@.Node, { key: "more" },\n'
    '                @OR@.createElement(@TXT@, { dimColor: !0 }, "+" + small.length + " more sources: ", @FMT@(tot), " tokens")));\n'
    '            }\n'
    '            return @OR@.createElement(@BOX@, { flexDirection: "column", marginTop: 1 },\n'
    '              @OR@.createElement(@BOX@, null,\n'
    '                @OR@.createElement(@TXT@, { bold: !0 }, "Tool results"),\n'
    '                @OR@.createElement(@TXT@, { dimColor: !0 }, " \\xB7 by source")),\n'
    '              @OR@.createElement(@TREE@, { variant: "tree" }, children));\n'
    '          })()\n'
    '        : !1'
)

# Collapsed MCP-tools render (site 11). Replaces the bare one-line count with the
# first 5 tools as a tree plus a "+N more tools" rollup. Each row mirrors the native
# expanded row style (read from the leak's ContextVisualization mappers): a LOADED
# tool renders "name: <tokens> tokens"; an on-demand/Available tool (not yet loaded,
# so no token cost) renders the name only. Expanded (Ctrl+O) keeps the full native
# Loaded/Available trees.
MCP_COLLAPSED = (
    '@I@ ? (() => {\n'
    '          const pcbTools = @A@,\n'
    '                pcbSorted = pcbTools.slice().sort((a, b) => ((b.isLoaded ? 1 : 0) - (a.isLoaded ? 1 : 0)) || ((b.tokens || 0) - (a.tokens || 0))), pcbShown = pcbSorted.slice(0, 5),\n'
    '                pcbRest = pcbTools.length - pcbShown.length,\n'
    '                /* loaded -> "name: N tokens"; available (on-demand) -> name only */\n'
    '                pcbNode = (t, idx) => t.isLoaded\n'
    '                  ? @OR@.createElement(@TREE@.Node, { key: idx },\n'
    '                      @OR@.createElement(@TXT@, null, t.name, ":", " ",\n'
    '                        @OR@.createElement(@TXT@, { dimColor: !0 }, @FMT@(t.tokens), " tokens")))\n'
    '                  : @OR@.createElement(@TREE@.Node, { key: idx, dimColor: !0 }, t.name);\n'
    '          const pcbChildren = pcbShown.map(pcbNode);\n'
    '          if (pcbRest > 0) {\n'
    '            pcbChildren.push(@OR@.createElement(@TREE@.Node, { key: "more", dimColor: !0 },\n'
    '              "+" + pcbRest + " more " + (pcbRest === 1 ? "tool" : "tools")));\n'
    '          }\n'
    '          return @OR@.createElement(@TREE@, { variant: "tree" }, pcbChildren);\n'
    '        })()'
)

# Splits tool-result tokens out of the "Messages" grid category into a distinct
# "Tool results" block with its own colour (site 7).
CAT_SPLIT = (
    'if (@X@ > 0) {\n'
    '    /* pcb: split tool-result tokens out of Messages into their own red grid\n'
    '       category, placed before the Messages remainder. The total is\n'
    '       preserved (Messages keeps the remainder), so Free space is unchanged. */\n'
    '    const pcbSub = (@V@.toolResultTokens || 0) + (@V@.toolCallTokens || 0) + (@V@.attachmentTokens || 0) + (@V@.assistantMessageTokens || 0) + (@V@.userMessageTokens || 0); const pcbTr = pcbSub > 0 ? Math.min(@X@, Math.round(@X@ * (@V@.toolResultTokens || 0) / pcbSub)) : 0; const pcbMsg = @X@ - pcbTr;\n'
    '    if (pcbTr > 0) @NE@.push({ name: "Tool results", tokens: pcbTr, color: "error" });\n'
    '    if (pcbMsg > 0) @NE@.push({ name: "Messages", tokens: pcbMsg, color: "purple_FOR_SUBAGENTS_ONLY" });\n'
    '  }'
)


# One owner of the /context collapse handshake. Defined once at module top level
# (globalThis, because the four call sites live in different bundle closures --
# mGe/UserTextMessage, t6p/the command, GVn/the SDK converter -- and a hoisted
# const wouldn't reach all of them). Every site that touches the sentinel goes
# through this object instead of open-coding the "__PCBX__" literal:
#
#   join(collapsed, expanded) -> the combined string the bakes emit (8a/8b)
#   pick(text, wantExpanded)  -> the live-renderer select (site 9). Returns text
#       unchanged unless it is a <local-command-stdout> wrapper carrying the
#       delimiter; then re-balances the wrapper tags and returns the wanted half
#       as a well-formed wrapper. Folds in site 9's stdout-prefix gate + re-balance.
#   strip(text)               -> external-viewer collapse (GVn / site 12): the part
#       before the delimiter, so SDK/claude.ai/mobile show the collapsed half.
#
# Behaviour is byte-identical (in the produced string VALUES) to the previous
# open-coded versions, so the existing runtime verification still holds.
PCB_HELPER = (
    'globalThis.__pcb ??= {\n'
    '  /* U+001E-wrapped sentinel: null-free, never appears in ANSI output. */\n'
    '  D: @D@,\n'
    '  /* combined string the /context bakes emit */\n'
    '  join(collapsed, expanded) { return collapsed + this.D + expanded; },\n'
    '  /* live-renderer select: pass through unless this is a <local-command-stdout>\n'
    '     wrapper carrying the delimiter, then re-balance the tags and return the\n'
    '     wanted half as a valid wrapper. */\n'
    '  pick(text, wantExpanded) {\n'
    '    if (typeof text !== "string") return text;\n'
    '    const parts = text.split(this.D);\n'
    '    if (parts.length > 1 && text.startsWith("<local-command-stdout")) {\n'
    '      return wantExpanded\n'
    '        ? "<local-command-stdout>" + parts[1]\n'
    '        : parts[0] + "</local-command-stdout>";\n'
    '    }\n'
    '    return text;\n'
    '  },\n'
    '  /* external-viewer collapse (GVn): the part before the delimiter. */\n'
    '  strip(text) { return typeof text === "string" ? text.split(this.D)[0] : text; },\n'
    '};'
)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        print(f"usage: {sys.argv[0]} <input-binary> [-o <output>]")
        sys.exit(2)
    src = sys.argv[1]
    if '-o' in sys.argv:
        dst = sys.argv[sys.argv.index('-o') + 1]
    else:
        dst = src + '.ctxrb'

    data = open(src, 'rb').read()
    js = bun_handler.extract_js(data).decode('utf-8', errors='surrogateescape')
    print(f"input:        {src} ({len(data)} bytes)")
    print(f"JS extracted: {len(js)} bytes")

    def splice(old, new, label, expected=1):
        nonlocal js
        cnt = js.count(old)
        if cnt != expected:
            raise SystemExit(
                f"[{label}] anchor count {cnt} != {expected}; refusing to patch.\n"
                f"           anchor: {old[:140]!r}"
            )
        js = js.replace(old, new, expected)
        print(f"  [{label}] applied ({len(new) - len(old):+d} bytes)")

    def find1(pattern, label):
        ms = list(re.finditer(pattern, js))
        if len(ms) != 1:
            raise SystemExit(
                f"[discover] {label}: expected 1 match, got {len(ms)}\n"
                f"           pattern: {pattern[:160]!r}"
            )
        return ms[0]

    print("\n--- sentinel helper (one owner of the /context collapse handshake) ---")
    # The /context collapse delimiter, defined once on the Python side. The injected
    # helper (PCB_HELPER) is the single JS-side owner; every other site references
    # globalThis.__pcb, never the literal. DELIM is the JS string literal (with quotes).
    DELIM = '"\\u001E__PCBX__\\u001E"'  # U+001E RS delimiter (null-free, never in output)
    # 0. Define globalThis.__pcb once at module-load time, before any render runs.
    #    Anchored on the bun CJS wrapper prologue so it executes at the very top of
    #    the module, guaranteeing mGe (render) / GVn (SDK) / t6p (command) all reach
    #    it regardless of which fires first (e.g. a resumed transcript renders before
    #    the user ever runs /context).
    splice(
        '(function(exports, require, module, __filename, __dirname) {',
        '(function(exports, require, module, __filename, __dirname) {\n'
        '/* pcb: single owner of the /context collapse sentinel; see patcher PCB_HELPER. */\n'
        + sub(PCB_HELPER, D=DELIM) + '\n',
        '0 sentinel helper: globalThis.__pcb',
    )

    print("\n--- data layer (feeds both surfaces) ---")

    # 1. c3p init: add the toolResultsByDetail map.
    splice(
        'toolResultsByType:new Map,attachmentsByType:new Map}',
        'toolResultsByType:new Map,'
        '/* pcb: per-source tool-result detail */toolResultsByDetail:new Map,'
        'attachmentsByType:new Map}',
        '1 c3p init: toolResultsByDetail map',
    )

    # 2. c3p id->name build loop: store {name, target}.
    m = find1(
        r'let (\w+)="id"in (\w+)\?\2\.id:void 0,(\w+)=\("name"in \2\?\2\.name:void 0\)\|\|"unknown";if\(\1\)(\w+)\.set\(\1,\3\)',
        'c3p id->name build loop',
    )
    idv, blk, nmv, mpv = m.group(1), m.group(2), m.group(3), m.group(4)
    new2 = (
        'let ' + idv + '="id"in ' + blk + '?' + blk + '.id:void 0,'
        + nmv + '=("name"in ' + blk + '?' + blk + '.name:void 0)||"unknown";'
        + sub(BUILD_SET, IDV=idv, BLK=blk, NMV=nmv, MPV=mpv)
    )
    splice(m.group(0), new2, '2 c3p build loop: store {name,target}')

    # 3. a3p tool_result branch: read .name, accumulate per-source detail.
    m = find1(
        r'let (\w+)="tool_use_id"in (\w+)\?\2\.tool_use_id:void 0,(\w+)=\(\1\?(\w+)\.get\(\1\):void 0\)\|\|"unknown";(\w+)\.toolResultsByType\.set\(\3,\(\5\.toolResultsByType\.get\(\3\)\|\|0\)\+(\w+)\)',
        'a3p tool_result branch',
    )
    idv, blk, nmv, mpv, bdv, tokv = (m.group(i) for i in range(1, 7))
    new3 = sub(A3P_BODY, IDV=idv, BLK=blk, NMV=nmv, MPV=mpv, BDV=bdv, TOKV=tokv)
    splice(m.group(0), new3, '3 a3p: accumulate per-source detail')

    # 4. vWn formatted object: add a sorted detail array.
    m = find1(
        r'\{toolCallTokens:(\w+)\.toolCallTokens[\s\S]{0,400}?toolCallsByType:(\w+),attachmentsByType:(\w+)\}',
        'vWn formatted messageBreakdown object',
    )
    bdv = m.group(1)
    obj = m.group(0)
    new4 = (
        obj[:-1]
        + ',\n      /* pcb: every per-source tool result, biggest first. The >= 300\n'
        '         threshold + "N more" rollup is applied at render time (collapsed),\n'
        '         so the expanded view can still reveal the full tail. */\n'
        '      toolResultsByDetail: ' + bdv + '.toolResultsByDetail\n'
        '        ? Array.from(' + bdv + '.toolResultsByDetail.values()).sort((a, b) => b.tokens - a.tokens)\n'
        '        : []}'
    )
    splice(obj, new4, '4 vWn: formatted toolResultsByDetail array')

    print("\n--- render layer ---")

    # 5. SWt markdown: emit the table after Skills.
    mb = find1(r',messageBreakdown:(\w+),systemTools:(\w+),systemPromptSections:(\w+)\}=', 'SWt messageBreakdown var')
    d_var = mb.group(1)
    fmt = find1(r'\*\*Tokens:\*\* \$\{(\w+)\(', 'SWt formatTokens helper')
    l_var = fmt.group(1)
    sk = find1(
        r'if\((\w+)&&\1\.tokens>0&&\1\.skillFrontmatter\.length>0\)\{(\w+)\+=`### Skills[\s\S]*?return \2\}',
        'SWt skills block + return',
    )
    out_var = sk.group(2)
    old5 = sk.group(0)
    ret = 'return ' + out_var + '}'
    if not old5.endswith(ret):
        raise SystemExit('[5 SWt] skills-block match did not end at the expected return')
    section = sub(MD_SECTION, D=d_var, OUT=out_var, FMT=l_var)
    new5 = old5[:-len(ret)] + section + ret
    splice(old5, new5, '5 SWt: markdown Tool-results table')

    # 6. ContextVisualization: repurpose the dead `messageBreakdown && false` slot.
    #    Discover React/Box/Text/Tree/formatTokens aliases from the Skills render,
    #    and the dead slot (cache var + slot indices) from its unique shape.
    # Tree-row renders (`<name> : <fmt>(<x>.tokens) tokens`) give the React, Tree,
    # Text and formatTokens aliases in the ink component's scope. Several sections
    # render such rows; they share the same module-level aliases, so collect all
    # and assert agreement rather than demanding a single match.
    rows = list(re.finditer(
        r'(\w+)\.createElement\((\w+)\.Node,\{key:\w+\},'
        r'\1\.createElement\((\w+),null,[^,]+,":"," ",'
        r'\1\.createElement\(\3,\{dimColor:!0\},(\w+)\([^)]*\.tokens\)," tokens"\)\)\)',
        js,
    ))
    if not rows:
        raise SystemExit('[discover] ink tree row: no match')
    # React/Tree/Text must agree across every row (one component scope). The
    # formatTokens alias legitimately varies by section (e.g. _l vs Bre, both
    # format a token count); any is fine, so take it from the first row.
    structural = {(r.group(1), r.group(2), r.group(3)) for r in rows}
    if len(structural) != 1:
        raise SystemExit(f'[discover] ink tree row: inconsistent React/Tree/Text across {len(rows)} rows: {structural}')
    or_var, tree_var, txt_var = rows[0].group(1), rows[0].group(2), rows[0].group(3)
    # Token rows do NOT all share one formatter: an estimate one (~N / "< 20") and the
    # exact one coexist, so rows[0] could grab the estimate. These counts are MEASURED,
    # so render them with the SAME exact formatter the markdown headline uses (l_var,
    # the "**Tokens:**" helper), and assert it is one of the in-scope ink-row aliases so
    # a future binding change fails loud instead of silently mis-rendering (a majority
    # vote would quietly flip to the estimate formatter if estimate rows ever outnumber).
    fmts = [r.group(4) for r in rows]
    if l_var not in fmts:
        raise SystemExit(f'[discover] ink fmt: exact formatter {l_var!r} (markdown headline) '
                         f'not among ink-row formatters {sorted(set(fmts))}; refusing to guess')
    fmt_ink = l_var
    print(f"  ink aliases: React={or_var} Tree={tree_var} Text={txt_var} fmt={fmt_ink} (from {len(rows)} rows)")
    # Box alias from the Skills section header wrapper.
    box_var = find1(
        re.escape(or_var) + r'\.createElement\((\w+),\{flexDirection:"column",marginTop:1\},'
        + re.escape(or_var) + r'\.createElement\(\1,null,'
        + re.escape(or_var) + r'\.createElement\(' + re.escape(txt_var) + r',\{bold:!0\},"Skills"\)',
        'ink Box alias (Skills wrapper)',
    ).group(1)
    # The dead slot: `<Q>=<S>&&!1,<cache>[a]=<S>,<cache>[b]=<Q>` (S = messageBreakdown).
    # Capture the WHOLE memoised dead slot (`if(t[n1]!==S)Q=S&&!1,...;else Q=t[n2]`),
    # not just the assignment, so we can drop the S-only memo entirely.
    slot = find1(
        r'if\((\w+)\[(\d+)\]!==(\w+)\)(\w+)=\3&&!1,\1\[\2\]=\3,\1\[(\d+)\]=\4;else \4=\1\[\5\]',
        'ink dead messageBreakdown slot (full memo block)',
    )
    cache, n1, s_var, q_var, n2 = slot.group(1), slot.group(2), slot.group(3), slot.group(4), slot.group(5)
    # Collapsed/expanded flag + one-line count component, from the Skills collapsed
    # ternary (`<i> ? createElement(<Oqn>, {count: <b>.skillFrontmatter.length, ...`).
    coll = find1(
        r'(\w+)\?' + re.escape(or_var) + r'\.createElement\((\w+),\{count:\w+\.skillFrontmatter\.length,noun:"skill"',
        'ink collapse flag + count component',
    )
    i_flag, oqn = coll.group(1), coll.group(2)
    render = sub(INK_RENDER, S=s_var, OR=or_var, BOX=box_var, TXT=txt_var, TREE=tree_var, FMT=fmt_ink, I=i_flag, OQN=oqn)
    # Replace the whole S-only memo block with an unconditional compute, so the tree
    # re-renders when the collapse flag <i> toggles (the original slot only watched
    # messageBreakdown, which is why it never collapsed/expanded). The old cache
    # slots t[n1]/t[n2] simply go unused.
    new6 = q_var + ' = (\n      ' + render + '\n    )'
    splice(slot.group(0), new6, '6 ink: Tool-results tree (collapse-reactive)')

    print("\n--- grid category ---")

    # 7. Split tool-result tokens out of the "Messages" grid category.
    msg = find1(
        r'if\((\w+)>0\)(\w+)\.push\(\{name:"Messages",tokens:\1,color:"purple_FOR_SUBAGENTS_ONLY"\}\)',
        'Messages grid category push',
    )
    x_var, ne_var = msg.group(1), msg.group(2)
    v_var = find1(
        r'Math\.max\(0,\w+-(\w+)\.toolCallTokens-\1\.toolResultTokens-\1\.attachmentTokens-\1\.assistantMessageTokens-\1\.userMessageTokens',
        'messageBreakdown var (grid split)',
    ).group(1)
    splice(msg.group(0), sub(CAT_SPLIT, X=x_var, NE=ne_var, V=v_var), '7 grid: split Tool results out of Messages')

    print("\n--- transcript-mode collapse (Ctrl+O) ---")
    # /context bakes its output to a static ANSI string via hat(); it can't react
    # to anything afterward. So pre-render BOTH a collapsed and an expanded snapshot,
    # join them with a delimiter, and let a live component (which receives
    # verbose/isTranscriptMode) choose which half to show. Transcript mode (Ctrl+O)
    # re-renders messages with verbose=true, so it shows the expanded half. The same
    # happens for free when /config verbose is on, because that setting is exactly
    # the `verbose` prop the renderer already keys on -- no special-casing.
    #
    # The render path (confirmed by runtime profiling, PCB-gated probes driven
    # through an interactive /context + Ctrl+O run): the combined string is emitted
    # via e(content, {display:"system"}), which the command dispatcher WRAPS as
    # `<local-command-stdout>${content}</local-command-stdout>` before storing it as
    # a local_command system message. Message.tsx renders local_command via
    # UserTextMessage, whose `startsWith("<local-command-stdout")` branch hands the
    # whole wrapped string to UserLocalCommandOutputMessage (which extractTag()s the
    # inner stdout). So the split/select belongs in UserTextMessage's
    # local-command-stdout branch (site 9), where verbose + isTranscriptMode are in
    # scope -- NOT in SystemTextMessage (never reached) and NOT in
    # UserLocalCommandOutputMessage (no verbose/transcript props).
    #
    # Two delimiter constraints the first cut got wrong, both fixed here:
    #   1. NO null bytes. A "\0"-bearing message renders fine in transcript mode but
    #      blanks out in the live scrollback render; use a printable-but-inert
    #      control char (U+001E RECORD SEPARATOR) that never appears in ANSI output.
    #   2. The delimiter sits INSIDE the single <local-command-stdout> wrapper, so a
    #      naive split leaves parts[0] with an open tag but no close, parts[1] with a
    #      close but no open. Site 9 re-balances: the collapsed half keeps the open
    #      tag and gets the close re-appended; the expanded half gets the open tag
    #      re-prepended and keeps the close. Each half is then a valid wrapper.
    #      (The delimiter + the split/join/strip/re-balance now live in one place:
    #      globalThis.__pcb, defined at site 0; the sites below just call it.)
    argv = find1(r'\w+=\$s\(\)&&(\w+)\.trim\(\)\.toLowerCase\(\)!=="all"',
                 'context command arg var').group(1)

    # 8a. /context remote mount. The default render (A) stays collapsed; we add an
    #     expanded copy and emit "<default>DELIM<expanded>". "/context all" makes the
    #     default expanded; anything else defaults to collapsed.
    splice(
        'A=await hat(EWt.createElement(ubo,{data:m,isRemote:!0,collapseDetailSections:r}));'
        'e(A,{display:"system",metaMessages:[SWt(m,{skipCollapseStatus:!0})]})',
        'A = await hat(EWt.createElement(ubo, { data: m, isRemote: true, collapseDetailSections: r })),\n'
        '    /* pcb: expanded copy for transcript mode (Ctrl+O) / verbose, baked only when the default is collapsed (else reuse the default render); a render error never masquerades as a remote-fetch failure. The default half keeps native collapseDetailSections: r, so screen-reader / no-alt-screen / tmux-CC / context-all still default to expanded. */\n'
        '    __pcbExpanded = A; if (r) { try { __pcbExpanded = await hat(EWt.createElement(ubo, { data: m, isRemote: true, collapseDetailSections: false })); } catch (pcbErr) { __pcbExpanded = A; } }\n'
        'e(\n'
        '  globalThis.__pcb.join(A, __pcbExpanded),\n'
        '  { display: "system", metaMessages: [SWt(m, { skipCollapseStatus: true })] }\n'
        ')',
        '8a /context remote: bake collapsed+expanded',
    )

    # 8b. /context local mount.
    splice(
        'f=await hat(EWt.createElement(ubo,{data:p,collapseDetailSections:r}));'
        'return e(f,{display:"system",metaMessages:[SWt(p)]}),null',
        'f = await hat(EWt.createElement(ubo, { data: p, collapseDetailSections: r })),\n'
        '    /* pcb: native default half (collapseDetailSections: r); expanded copy baked only when the default is collapsed, else reuse f. See 8a. */\n'
        '    __pcbExpanded = f; if (r) { try { __pcbExpanded = await hat(EWt.createElement(ubo, { data: p, collapseDetailSections: false })); } catch (pcbErr) { __pcbExpanded = f; } }\n'
        'return e(\n'
        '  globalThis.__pcb.join(f, __pcbExpanded),\n'
        '  { display: "system", metaMessages: [SWt(p)] }\n'
        '), null',
        '8b /context local: bake collapsed+expanded',
    )

    # 9. UserTextMessage local-command-stdout branch: pick the collapsed or expanded
    #    half before handing the wrapped string to UserLocalCommandOutputMessage.
    #
    #    /context's combined output arrives here as a single
    #    `<local-command-stdout>COLLAPSED<DELIM>EXPANDED</local-command-stdout>` string
    #    (param.text). Splitting on the delimiter alone would tear the wrapper tags, so
    #    we re-balance: the collapsed half keeps the leading "<local-command-stdout>"
    #    and we re-append the closing tag; the expanded half gets the opening tag
    #    re-prepended and keeps the trailing "</local-command-stdout>". UserLocalCommand-
    #    OutputMessage then extractTag()s a well-formed wrapper either way. verbose and
    #    isTranscriptMode are in scope here (the component's own props); transcript mode
    #    (Ctrl+O) and /config verbose both flip verbose, so either reveals the expanded
    #    half. Any local-command-stdout WITHOUT the delimiter (every other local command)
    #    has parts.length === 1 and passes through byte-identically.
    #
    #    Discover the param var + verbose/isTranscriptMode vars and the memo cache var
    #    structurally so a re-minify fails loud rather than mis-patching.
    m9 = find1(
        r'function mGe\(\w+\)\{let \w+=\w+\.c\(\d+\),\{addMargin:\w+,param:(\w+),'
        r'verbose:(\w+),planContent:\w+,isTranscriptMode:(\w+),timestamp:\w+\}',
        'UserTextMessage (mGe) signature: param/verbose/isTranscriptMode vars',
    )
    param_var, verbose_var, transcript_var = m9.group(1), m9.group(2), m9.group(3)
    branch = find1(
        r'if\(' + re.escape(param_var) + r'\.text\.startsWith\("<local-command-stdout"\)\|\|'
        + re.escape(param_var) + r'\.text\.startsWith\("<local-command-stderr"\)\)\{'
        r'let (\w+);if\((\w+)\[(\d+)\]!==' + re.escape(param_var) + r'\.text\)'
        r'\1=(\w+)\.createElement\((\w+),\{content:' + re.escape(param_var) + r'\.text\}\),'
        r'\2\[\3\]=' + re.escape(param_var) + r'\.text,\2\[(\d+)\]=\1;else \1=\2\[\6\];',
        'UserTextMessage local-command-stdout branch',
    )
    out_var, cache_var, slot1, react_var, comp_var, slot2 = (branch.group(i) for i in range(1, 7))
    splice(
        branch.group(0),
        'if(' + param_var + '.text.startsWith("<local-command-stdout")||'
        + param_var + '.text.startsWith("<local-command-stderr")){\n'
        '  /* pcb: /context bakes COLLAPSED<DELIM>EXPANDED inside one\n'
        '     <local-command-stdout> wrapper. __pcb.pick selects the half by\n'
        '     verbose/transcript and re-balances the wrapper tags (and passes any\n'
        '     non-/context local-command output straight through). */\n'
        '  let __pcbText = globalThis.__pcb.pick(' + param_var + '.text, '
        + verbose_var + ' || ' + transcript_var + ');\n'
        '  let ' + out_var + ';'
        'if(' + cache_var + '[' + slot1 + ']!==__pcbText)'
        + out_var + '=' + react_var + '.createElement(' + comp_var + ',{content:__pcbText}),'
        + cache_var + '[' + slot1 + ']=__pcbText,' + cache_var + '[' + slot2 + ']=' + out_var + ';'
        'else ' + out_var + '=' + cache_var + '[' + slot2 + '];',
        '9 UserTextMessage local-command-stdout: pick + re-balance wrapper',
    )
    print("\n--- MCP tools collapse ---")
    # 11. Collapsed MCP-tools section: show 5 tools + "+N more tools" instead of the
    #     bare one-line count. The MCP section's collapsed branch is `<i> ? <Oqn count>
    #     : <full Loaded/Available trees>`; we swap the Oqn count for a short tree. The
    #     collapse flag <i>, React/Tree aliases match the ones site 6 discovered, but
    #     re-discover the exact anchor here (with its own A/ce/qVp locals) so the splice
    #     is self-checking.
    mcp = find1(
        r'(\w+)\?(\w+)\.createElement\((\w+),\{count:(\w+)\.length,noun:"tool",'
        r'tokens:\4\.filter\(\((\w+)\)=>!(\w+)\|\|\5\.isLoaded\)\.reduce\((\w+),0\)\}\)',
        'collapsed MCP-tools count line',
    )
    mcp_i, mcp_or, mcp_tools = mcp.group(1), mcp.group(2), mcp.group(4)
    # Tree/Text/formatTokens aliases are shared module-wide; reuse the ones site 6
    # discovered (asserted consistent across every ink tree row there).
    splice(
        mcp.group(0),
        sub(MCP_COLLAPSED, I=mcp_i, OR=mcp_or, A=mcp_tools,
            TREE=tree_var, TXT=txt_var, FMT=fmt_ink),
        '11 MCP tools: collapsed shows 5 + rollup',
    )

    print("\n--- external session viewers (GVn) ---")
    # 12. GVn (localCommandOutputToSDKAssistantMessage) is the external-viewer display
    #     path: it strips the <local-command-stdout> wrapper but keeps the inner text,
    #     so a /context message would show COLLAPSED + the raw U+001E delimiter +
    #     EXPANDED. Route the unwrapped text through __pcb.strip so external viewers
    #     (SDK/claude.ai/mobile) get the collapsed half (matching the local default);
    #     other local-command output has no delimiter and passes through.
    splice(
        'local-command-stderr>([\\s\\S]*?)<\\/local-command-stderr>/,"$1").trim();'
        'return{type:"assistant",message:SS({content:n})',
        'local-command-stderr>([\\s\\S]*?)<\\/local-command-stderr>/,"$1").trim();'
        'n=globalThis.__pcb.strip(n);'
        'return{type:"assistant",message:SS({content:n})',
        '12 GVn: external viewers show collapsed half (strip delimiter)',
    )

    # NOTE on model context: the combined "<local-command-stdout>COLLAPSED<DELIM>
    # EXPANDED</local-command-stdout>" string is persisted to the session JSONL
    # verbatim, matching how upstream persists local-command output, so a resume
    # reconstructs the exact combined string and the renderer (site 9) still offers
    # collapse/expand. This does NOT leak into model context: the model prompt is
    # built by selectableUserMessagesFilter (src/components/MessageSelector.tsx),
    # which excludes any message containing <local-command-stdout> -- and /context's
    # message is type:"system" besides. Verified empirically: ANTHROPIC_LOG capture
    # of the outbound /v1/messages body on --continue contains no delimiter, no
    # local-command-stdout, no "Context Usage". The GVn unwrap-to-assistant
    # converter (localCommandOutputToSDKAssistantMessage) is the SDK/RC/mobile DISPLAY
    # path for external session viewers, not the model prompt; site 12 strips the
    # delimiter there too, so those viewers show the collapsed half rather than the raw
    # COLLAPSED<DELIM>EXPANDED string.

    # ------------------------------------------------------------------
    new_data = bun_handler.repack_with_js(
        data, js.encode('utf-8', errors='surrogateescape')
    )
    print(f"\nfinal JS: {len(js)} bytes")
    print(f"binary:   {len(new_data)} bytes (delta {len(new_data) - len(data):+d})")
    open(dst, 'wb').write(new_data)
    os.chmod(dst, 0o755)
    print(f"wrote {dst}")


if __name__ == '__main__':
    main()
