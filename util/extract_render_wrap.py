#!/usr/bin/env python3
"""
Extract the SHIPPED (minified) webview render-wrap from a prebuilt apply.py, so
the faithfulness harness (src/render.test.mjs) can run it against the readable
src/render.js on identical inputs and diff the element trees.

WHY read the prebuilt and not the live bundle: the prebuilt's webview/index.js
splices are the byte-exact, known-good render-wrap we are lifting FROM. It is the
reference the readable source must reproduce; any divergence between the two on
the same PFGK1 ghost input is a lift bug. Sourcing the reference from the shipped
bytes (not a hand copy) keeps the harness honest: it cannot drift from what ships.

The render-wrap is TWO webview splices (see SKILL.md Step 13): a binding
conversion (`return b(...)` -> `let _ws=b(...)`) and the card block appended
after it. This tool extracts the card block's injected code as a runnable
function body, plus the self-contained `_pfDiagram` SVG builder in isolation
(a pure `(kind, theme) -> string`, the sharpest faithfulness lever).

The extracted card body references only four free names -- `t` (the message),
`e` (the session, read via `e.messages.peek()`), `b` (the element factory), and
the global `document` -- and assigns/returns `_ws` (the default element). It
contains no `$pfg` / `_pv_` / vendor-component references, so the harness runs it
as `new Function("t","e","b","_ws", body)` with `globalThis.document` mocked.

Usage:
  python3 util/extract_render_wrap.py [--version 2.1.195] [--what body|diagram|json]

  body     : print the runnable card-block body (default)
  diagram  : print the isolated `_pfDiagram` function text
  json     : print {"body": ..., "diagram": ...} (what the JS harness consumes)
"""
import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# The card-block splice's `new` value is PREFIX + <card body> + SUFFIX, where
# PREFIX/SUFFIX are the shared context with the splice's `old` (the anchor). The
# card body is exactly the injected code: everything between them.
PREFIX = "setInputError:a,onCreateNewSession:l},i);"
SUFFIX = '}if(t.type==="assistant"){if(t.content.e'
BODY_START = 'if(typeof t.uuid==="string"){'   # sanity anchor: body must start here
BODY_END = "return _ws"                         # ...and end here
DIAGRAM_DECL = "function _pfDiagram(kind, T)"


def _load_prebuilt_splices(version):
    path = os.path.join(REPO, "prebuilt", version, "apply.py")
    if not os.path.exists(path):
        raise SystemExit(f"[extract_render_wrap] no prebuilt at {path}")
    spec = importlib.util.spec_from_file_location(f"prebuilt_apply_{version}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # safe: apply.py guards main() behind __main__
    return dict(mod.SPLICES)


def _match_brace_span(s, decl):
    """Return s[i:j+1] spanning `decl` and its brace-balanced body. Deliberately
    a plain brace counter: the _pfDiagram body's braces live only in code and in
    single-quoted SVG string fragments that themselves contain no braces, so a
    naive count is exact here. (The general lexer-aware matcher is pfg/jslex.py;
    this helper stays dependency-free for the extractor.)"""
    i = s.index(decl)
    j = s.index("{", i)
    depth, k = 0, j
    while k < len(s):
        c = s[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i : k + 1]
        k += 1
    raise SystemExit("[extract_render_wrap] unbalanced braces in _pfDiagram")


def extract(version):
    splices = _load_prebuilt_splices(version)
    if "webview/index.js" not in splices:
        raise SystemExit("[extract_render_wrap] prebuilt has no webview/index.js splices")
    wv = splices["webview/index.js"]

    # Find the card-block splice by its unique anchor rather than a fixed index,
    # so a reordering in a future prebuilt does not silently grab the wrong one.
    card = None
    for sp in wv:
        if sp["old"].startswith("setInputError:a,onCreateNewSession:l},i)}"):
            card = sp
            break
    if card is None:
        raise SystemExit("[extract_render_wrap] could not find the card-block splice (anchor drifted)")

    new = card["new"]
    if not new.startswith(PREFIX) or not new.endswith(SUFFIX):
        raise SystemExit(
            "[extract_render_wrap] card splice new-value does not match the expected "
            "PREFIX/SUFFIX context; the wrap shape drifted -- re-verify against the prebuilt."
        )
    body = new[len(PREFIX) : len(new) - len(SUFFIX)]
    if not body.startswith(BODY_START) or not body.endswith(BODY_END):
        raise SystemExit(
            f"[extract_render_wrap] extracted body did not start with {BODY_START!r} "
            f"and end with {BODY_END!r}; refusing to emit a mis-sliced wrap."
        )
    # Guard the isolation invariant the harness relies on: no vendor symbols leak.
    for forbidden in ("$pfg", "_pv_", "V8t"):
        if forbidden in body:
            raise SystemExit(
                f"[extract_render_wrap] wrap body references {forbidden!r}; the harness "
                f"assumption that the wrap is vendor-symbol-free is broken."
            )
    diagram = _match_brace_span(body, DIAGRAM_DECL)
    return {"body": body, "diagram": diagram}


def main(argv):
    ap = argparse.ArgumentParser(description="Extract the shipped minified render-wrap.")
    ap.add_argument("--version", default="2.1.195")
    ap.add_argument("--what", default="body", choices=("body", "diagram", "json"))
    args = ap.parse_args(argv)
    out = extract(args.version)
    if args.what == "json":
        sys.stdout.write(json.dumps(out))
    else:
        sys.stdout.write(out[args.what])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
