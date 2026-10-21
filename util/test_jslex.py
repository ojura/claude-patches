"""
Adversarial suite for the minified-JS lexer (find_function_span / _match_body).

These are exactly the cases that break a naive { } counter, the lexer's whole
reason to exist. A wrong close brace can still `node --check`, so the ONLY thing
standing between a truncation and a shipped-broken patch is that this matcher is
lexically correct. Prove it here.

"""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # repo root, for `pfg`
from pfg.jslex import find_function_span, enclosing_function_start, _match_delim  # noqa: E402


def _span(js, name):
    s, e = find_function_span(js, name)
    return js[s:e]


def test_brace_in_string():
    js = 'z;function f(a){let s="a}b";return s}y'
    assert _span(js, "f") == 'function f(a){let s="a}b";return s}'


def test_brace_in_single_quote_string():
    js = "z;function f(a){let s='}}}';return s}y"
    assert _span(js, "f") == "function f(a){let s='}}}';return s}"


def test_escaped_quote_then_brace():
    js = r'z;function f(a){let s="a\"}b";return s}y'
    assert _span(js, "f") == r'function f(a){let s="a\"}b";return s}'


def test_brace_in_template_literal():
    js = "z;function f(a){let s=`a}b`;return s}y"
    assert _span(js, "f") == "function f(a){let s=`a}b`;return s}"


def test_template_interpolation_with_object():
    # ${ {k:1} } : the interpolation itself contains an object literal's braces
    js = "z;function f(a){let s=`x${ {k:1} }y`;return s}y"
    assert _span(js, "f") == "function f(a){let s=`x${ {k:1} }y`;return s}"


def test_nested_template_in_interpolation():
    js = "z;function f(a){let s=`${`}${a}`}`;return s}y"
    assert _span(js, "f") == "function f(a){let s=`${`}${a}`}`;return s}"


def test_regex_literal_with_brace():
    js = "z;function f(a){let r=/a}b/g;return r.test(a)}y"
    assert _span(js, "f") == "function f(a){let r=/a}b/g;return r.test(a)}"


def test_regex_after_return_keyword():
    # `return /}/` is a regex, not division; keyword-aware disambiguation
    js = "z;function f(a){return/}/.test(a)}y"
    assert _span(js, "f") == "function f(a){return/}/.test(a)}"


def test_division_is_not_regex():
    js = "z;function f(a){let b=a/2/1;return b}y"
    assert _span(js, "f") == "function f(a){let b=a/2/1;return b}"


def test_regex_char_class_hides_slash_and_brace():
    js = "z;function f(a){let r=/[/}]/;return r}y"
    assert _span(js, "f") == "function f(a){let r=/[/}]/;return r}"


def test_brace_in_line_comment():
    js = "z;function f(a){//}\nreturn a}y"
    assert _span(js, "f") == "function f(a){//}\nreturn a}"


def test_brace_in_block_comment():
    js = "z;function f(a){/* } } } */return a}y"
    assert _span(js, "f") == "function f(a){/* } } } */return a}"


def test_nested_function_declarations():
    js = "z;function f(a){function g(){return{x:1}}return g()}y"
    assert _span(js, "f") == "function f(a){function g(){return{x:1}}return g()}"


def test_async_function():
    js = "z;async function f(a){return a}y"
    assert _span(js, "f") == "async function f(a){return a}"


def test_next_token_boundary_is_clean():
    js = "z;function f(a){return a}function g(){}"
    s, e = find_function_span(js, "f")
    assert js[e:e + 8] == "function"  # span ends exactly at f's close brace


def test_param_default_paren_in_string():
    js = 'z;function f(a="("){return a}y'
    assert _span(js, "f") == 'function f(a="("){return a}'


def test_param_default_close_paren_in_string():
    # a `)` hidden in a default string must not close the param list early
    js = 'z;function f(a="){",b){return b}y'
    assert _span(js, "f") == 'function f(a="){",b){return b}'


def test_param_default_paren_in_regex():
    js = 'z;function f(a=/)/){return a}y'
    assert _span(js, "f") == 'function f(a=/)/){return a}'


def test_param_default_paren_in_template():
    js = "z;function f(a=`)`){return a}y"
    assert _span(js, "f") == "function f(a=`)`){return a}"


def test_param_default_arrow_with_body():
    # nested () and {} inside a default arrow: only the param-list ( ) closes it
    js = "z;function f(a=()=>{}){return a}y"
    assert _span(js, "f") == "function f(a=()=>{}){return a}"


def test_param_destructuring_with_braces():
    js = "z;function f({a,b}){return a}y"
    assert _span(js, "f") == "function f({a,b}){return a}"


def test_param_comment_with_paren():
    js = "z;function f(a/*)*/,b){return b}y"
    assert _span(js, "f") == "function f(a/*)*/,b){return b}"


# The exact param-list-close token ")" + "{" hidden inside a default value. A scanner
# that greps for the "){" that separates the param list from the body (not lexer-aware)
# would end the param list INSIDE the literal and truncate the span. These pin that the
# lexer skips the regex/template/comment/string and only the STRUCTURAL "){" closes it.
def test_param_default_brace_paren_in_regex():
    js = "z;function f(a=/){/){return a}y"
    assert _span(js, "f") == "function f(a=/){/){return a}"


def test_param_default_brace_paren_in_template():
    js = "z;function f(a=`){`){return a}y"
    assert _span(js, "f") == "function f(a=`){`){return a}"


def test_param_default_brace_paren_in_block_comment():
    js = "z;function f(a/*){*/,b){return b}y"
    assert _span(js, "f") == "function f(a/*){*/,b){return b}"


def test_param_default_brace_paren_in_single_quote_string():
    js = "z;function f(a='){',b){return b}y"
    assert _span(js, "f") == "function f(a='){',b){return b}"


def test_keyword_as_property_before_slash_is_division():
    # `.of` is a property, so `/` is division; a regex misread would swallow the
    # closing } and truncate the span (or raise unterminated regex).
    js = "z;function f(a){return a.of/2}y"
    assert _span(js, "f") == "function f(a){return a.of/2}"


def test_all_hazards_at_once():
    body = (
        'function f(a){'
        'let s="}";'          # brace in string
        "let t=`}${ {q:1} }`;"  # brace in template + object in interpolation
        "let r=/[}]/g;"        # brace in regex char class
        "/* } */"             # brace in block comment
        "if(a)return/}/.test(s);"  # regex after return
        "return a/2}"        # division, then the real close brace
    )
    js = "z;" + body + "trailer"
    assert _span(js, "f") == body


# ---- enclosing_function_start: hoist an in-body position to its function's start ------

def test_enclosing_fn_simple():
    js = "head;function WRAP(a){X;PIN;Y}tail"
    assert enclosing_function_start(js, js.index("PIN")) == js.index("function WRAP")


def test_enclosing_fn_nested_returns_innermost():
    js = "function OUTER(){function INNER(){PIN}}"
    assert enclosing_function_start(js, js.index("PIN")) == js.index("function INNER")


def test_enclosing_fn_sibling_before_pos_is_skipped():
    # SIB's body closes before PIN, so the encloser is OUTER, not the nearer SIB head.
    js = "function OUTER(){function SIB(){z}PIN}"
    assert enclosing_function_start(js, js.index("PIN")) == js.index("function OUTER")


def test_enclosing_fn_none_at_module_level():
    js = "a;function f(){z}PIN;"
    assert enclosing_function_start(js, js.index("PIN")) is None


def test_enclosing_fn_brace_in_string_does_not_close_body_early():
    # a `}` inside a string before PIN must not end f's body early (lexer-aware match)
    js = 'function f(a){let s="}}}";PIN}tail'
    assert enclosing_function_start(js, js.index("PIN")) == 0


def test_enclosing_fn_arrow_resolves_to_named_encloser():
    # PIN sits inside an arrow inside f; arrows have no `function NAME`, so the innermost
    # NAMED encloser is f (the hoist target). The block hoisted here is self-contained.
    js = "function f(){let g=(x)=>{PIN};return g}"
    assert enclosing_function_start(js, js.index("PIN")) == 0


def test_enclosing_fn_async_named():
    js = "q;async function f(a){PIN}z"
    assert enclosing_function_start(js, js.index("PIN")) == js.index("async function f")


# Known failures of the lexer, recorded as strict expected failures: a fix makes these tests fail,
# so that this record is updated with the fix.
#
# _regex_ok decides whether a `/` starts a regex literal from the character before it, which
# JavaScript's grammar does not determine: after `)` or `}` either reading is possible, and a value
# can end in a character that also ends an operator or punctuation. A line comment ends only at a
# line feed, where JavaScript also ends it at a carriage return, U+2028 and U+2029. Each case is
# valid JavaScript (node --check accepts it), and the matcher reports a span cut short or raises.
_KNOWN_FAILURES = [
    ("a regex after the condition of an if statement is read as division",
     'function f(a,s){if(a)/}/.test(s);return 1}'),
    ("a regex containing a quote after the condition of an if statement is read as division",
     'function f(x){if(x)/["]/.test(x);return 1}'),
    ("a regex after the closing brace of a block statement is read as division",
     'function f(s){{}/}/.test(s);return 1}'),
    ("a division after postfix ++ is read as a regex",
     'function f(b){let a=b++/2,c="/}";return a}'),
    ("a division after a string literal is read as a regex",
     'function f(){let a="8"/2,c="/}";return a}'),
    ("a division after a template literal is read as a regex",
     'function f(){let a=`8`/2,c="/}";return a}'),
    ("a division after a regex literal is read as a regex",
     'function f(){let a=/x//2,c="/}";return a}'),
    ("a division by a variable named of is read as a regex",
     'function f(of){let a=of/2,c="/}";return a}'),
    ("a line comment ended by a carriage return runs on to the next line feed",
     'function f(){// c\rreturn"}"}'),
    ("a line comment ended by U+2028 runs on to the next line feed",
     'function f(){// c return"}"}'),
    ("a line comment ended by U+2029 runs on to the next line feed",
     'function f(){// c return"}"}'),
]
_LABELS = [label for label, _ in _KNOWN_FAILURES]


@pytest.mark.xfail(strict=True, reason="known lexing failure; see _KNOWN_FAILURES")
@pytest.mark.parametrize("label,js", _KNOWN_FAILURES, ids=_LABELS)
def test_known_lexing_failures(label, js):
    assert _span(js, "f") == js


@pytest.mark.parametrize("label,js", _KNOWN_FAILURES, ids=_LABELS)
def test_known_failures_are_valid_javascript(label, js):
    assert _node_check(js) == 0


# _match_delim counts only the bracket pair it matches, so an input whose brackets of different
# kinds cross gets a span where it should get an error. The inputs are not valid JavaScript.
_CROSSED_BRACKETS = [("[(])", "[", "]"), ("[{x:1)]", "[", "]"), ("([)]", "(", ")")]


@pytest.mark.xfail(strict=True, reason="known: crossed bracket kinds are not refused")
@pytest.mark.parametrize("js,opening,closing", _CROSSED_BRACKETS)
def test_crossed_bracket_kinds_are_refused(js, opening, closing):
    with pytest.raises(ValueError):
        _match_delim(js, 0, opening, closing)


@pytest.mark.parametrize("js,opening,closing", _CROSSED_BRACKETS)
def test_crossed_brackets_are_not_valid_javascript(js, opening, closing):
    assert _node_check(js) != 0


def _node_check(js):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    return subprocess.run([node, "--check", "--input-type=module"], input=js, capture_output=True, text=True).returncode
