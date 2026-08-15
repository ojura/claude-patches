"""
jslex: a minified-JS-aware lexer + whole-function span extractor.

General-purpose, zero pfg knowledge. The wholesale-replace locators sit on it, and
so will the 2b vendor-extract tool. Its silent failure mode (a wrong close brace
that truncates a function which still parses) is the single highest-risk thing in
the patch machinery, which is why it is its own module with its own adversarial
suite (util/test_jslex.py). A whole-function locator is signature-anchor + this
brace-match; a naive { } counter picks the wrong close brace in minified code
because braces hide in strings, template literals (${...}), regex literals, and
comments, and a truncation can still pass `node --check`.
"""
import re

_REGEX_KW = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "do", "else", "yield", "await", "case", "throw",
}


def _regex_ok(prev, prev_word):
    """Is `/` here a regex literal vs division? Division follows a value; a regex
    follows an operator/punct, or a value-less keyword (return /re/, typeof /x/)."""
    if prev == "":
        return True
    if prev in ")]}":
        return False
    if prev.isalnum() or prev in "_$":
        return prev_word in _REGEX_KW
    return True


def _skip_string(js, i):
    q = js[i]; i += 1; n = len(js)
    while i < n:
        c = js[i]
        if c == "\\": i += 2; continue
        if c == q: return i + 1
        i += 1
    raise ValueError("unterminated string")


def _skip_regex(js, i):
    i += 1; n = len(js); in_class = False
    while i < n:
        c = js[i]
        if c == "\\": i += 2; continue
        if c == "[": in_class = True
        elif c == "]": in_class = False
        elif c == "/" and not in_class:
            i += 1
            while i < n and js[i].isalpha(): i += 1  # regex flags
            return i
        elif c == "\n": raise ValueError("unterminated regex")
        i += 1
    raise ValueError("unterminated regex")


def _skip_template(js, i):
    i += 1; n = len(js)
    while i < n:
        c = js[i]
        if c == "\\": i += 2; continue
        if c == "`": return i + 1
        if c == "$" and i + 1 < n and js[i + 1] == "{":
            close = _match_body(js, i + 1)  # interpolation nests braces/strings
            i = close + 1; continue
        i += 1
    raise ValueError("unterminated template")


def _match_delim(js, open_idx, oc="{", cc="}"):
    """Index of the delimiter `cc` closing the `oc` at open_idx, lexer-aware (see
    module doc). Generalized over the delimiter pair so the param-list ( ) scan gets
    the SAME string/regex/template/comment awareness as the body { } scan: a naive
    ( ) counter miscounts a default like f(a="(") exactly the way a naive { } counter
    truncates a function whose body hides a } in a string."""
    n = len(js)
    if js[open_idx] != oc:
        raise ValueError(f"open_idx is not {oc!r}")
    i = open_idx + 1; depth = 1; prev = oc; prev_word = ""
    while i < n:
        c = js[i]
        if c.isalpha() or c in "_$":  # consume an identifier as one unit
            j = i + 1
            while j < n and (js[j].isalnum() or js[j] in "_$"): j += 1
            # A keyword after '.' is a PROPERTY (x.of, x.return), not a value-less
            # keyword, so it must not put us in regex position: blank prev_word.
            prev_word = "" if prev == "." else js[i:j]
            prev = js[j - 1]; i = j; continue
        if c == "/" and i + 1 < n and js[i + 1] == "/":
            nl = js.find("\n", i); i = n if nl < 0 else nl; continue
        if c == "/" and i + 1 < n and js[i + 1] == "*":
            e = js.find("*/", i + 2)
            if e < 0: raise ValueError("unterminated block comment")
            i = e + 2; continue
        if c == "/" and _regex_ok(prev, prev_word):
            i = _skip_regex(js, i); prev, prev_word = "/", ""; continue
        if c in "\"'":
            i = _skip_string(js, i); prev, prev_word = c, ""; continue
        if c == "`":
            i = _skip_template(js, i); prev, prev_word = "`", ""; continue
        if c == oc: depth += 1
        elif c == cc:
            depth -= 1
            if depth == 0: return i
        if not c.isspace(): prev, prev_word = c, ""
        i += 1
    raise ValueError(f"unbalanced {oc!r} from {open_idx}")


def _match_body(js, open_idx):
    """Index of the } closing the { at open_idx. Thin wrapper over _match_delim."""
    return _match_delim(js, open_idx, "{", "}")


def find_function_span(js, name):
    """Locate `(?:async )?function <name>(...)` (verify-once) and return the
    (start, end) of the whole declaration via signature + tokenizer brace-match.
    The caller MUST validate the extracted span parses (a truncation can parse)."""
    ms = list(re.finditer(rf"(?:async )?function {re.escape(name)}\(", js))
    if len(ms) != 1:
        raise SystemExit(f"[pfg locate] function {name}: expected 1, got {len(ms)}")
    start = ms[0].start()
    paren_open = ms[0].end() - 1                          # the ( of the param list
    close_paren = _match_delim(js, paren_open, "(", ")")  # lexer-aware, not a raw ( ) count
    end = _match_body(js, js.index("{", close_paren))
    return start, end + 1


def enclosing_function_start(js, pos):
    """Start offset of the INNERMOST `(?:async )?function NAME(...)` whose BODY contains
    `pos`, or None if `pos` sits inside no such function. Used to HOIST an injected block
    out of a function body (where an anchor sits) to that function's own scope, so a
    target's module-level definitions land ONCE instead of being re-allocated on every
    call into the function.

    Nearest-head-first: the last `function NAME` head before `pos` whose lexer-matched body
    span contains `pos` is the innermost encloser. A sibling function declared before `pos`
    has a body that closes before `pos`, so it is correctly skipped. Only NAMED function
    declarations are scanned (arrow / anonymous functions have no `function NAME`), so a
    position inside an arrow resolves to the enclosing NAMED function; the block hoisted
    here must therefore be self-contained, not a closure over an intervening arrow's locals,
    which the webview render.js is (param-based). Early-returns on the nearest enclosing
    head, so the common case brace-matches once. Braces inside strings / regex / templates /
    comments do not miscount (the shared tokenizer, the reason a naive scan would truncate)."""
    sig = re.compile(r"(?:async )?function\s+[A-Za-z_$][\w$]*\s*\(")
    heads = []
    for m in sig.finditer(js):
        if m.start() >= pos:
            break                       # declared at/after pos (finditer is in order): cannot enclose
        heads.append((m.start(), m.end() - 1))
    for start, paren_open in reversed(heads):   # nearest head first == innermost encloser
        try:
            close_paren = _match_delim(js, paren_open, "(", ")")
            body_open = js.index("{", close_paren)
            body_close = _match_body(js, body_open)
        except (ValueError, IndexError):
            continue
        if body_open < pos <= body_close:
            return start
    return None
