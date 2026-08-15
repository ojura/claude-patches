"""
Adversarial suite for the minified-JS lexer (find_function_span / _match_body).

These are exactly the cases that break a naive { } counter, the lexer's whole
reason to exist. A wrong close brace can still `node --check`, so the ONLY thing
standing between a truncation and a shipped-broken patch is that this matcher is
lexically correct. Prove it here.

"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # repo root, for `pfg`
from pfg.jslex import find_function_span, enclosing_function_start  # noqa: E402


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
