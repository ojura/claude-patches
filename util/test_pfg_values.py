"""
Tests for pfg.values: the evaluator, the facts and their constancy checks across modules, and the
initialization order.

The cases named "behaviour" are the 13 value-level behaviours of plugin-fixes' retired draft of
pfg.bundle (its util/test_pfg_bundle.py of 2026-10-20, which read the bundle with regular expressions).
Its other seven are covered elsewhere:
  find_one_returns_the_module_and_match_of_the_only_site, find_one_counts_every_match_in_every_module:
    pfg gives every needle hit in every module (test_pfg_bundle.py, test_needles_hit_syntax_only and
    test_needle_hits_come_from_every_module_in_order); deciding that exactly one site matches is the
    catalog predicate's job, which raises AnchorError otherwise (test_an_anchor_error_counts_candidates_
    given_as_a_generator).
  contains_limits_the_search_to_modules_that_hold_the_text: the index pass parses every module once
    and records every hit in that pass, so there is no search to limit; that only code hits is tested
    by test_needles_hit_syntax_only.
  an_import_from_a_module_outside_the_build_is_refused, an_import_cycle_is_refused: also value-level
    here, and test_pfg_bundle.py, test_cycles_missing_names_and_outside_modules_are_refused.
  function_span_within_a_module: test_behaviour_the_function_a_binding_holds_spans_its_declaration.
  modules_of_a_binary_in_every_encoding_bun_stores: test_pfg_bundle.py,
    test_modules_of_a_binary_in_every_encoding_bun_stores, and test_behaviour_values_of_a_binary_in_
    every_encoding_bun_stores here.
The cases named "counterexample" are pfg-advisor's 13 counterexamples to classifying uses by a list
of method names; each changes the value in Node through a use such a list accepts.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)  # for `pfg`
sys.path.insert(0, os.path.join(REPO, "bun_handler"))
from pfg import bundle as pb  # noqa: E402
from pfg import values as pv  # noqa: E402
import test_bun_handler as tb  # noqa: E402  (its synthetic ELF builders)


class Build:
    """A bundle of modules /m0.js, /m1.js, ... with their Values, closed at the end of a with block."""

    def __init__(self, *texts):
        self.bundle = pb.Bundle.from_sources([(index, f"/m{index}.js", text) for index, text in enumerate(texts)])
        self.values = pv.Values(self.bundle)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.bundle.close()

    def binding(self, module, name):
        record = self.bundle.scope_binding(module, name)
        assert "id" in record, (module, name, record)
        return record["id"]

    def value(self, module, name, fact):
        return self.values.value(module, self.binding(module, name), fact)

    def refused(self, module, name, fact):
        with pytest.raises(pb.Unresolved) as error:
            self.value(module, name, fact)
        return error.value


def num(js):
    return pv.JSNumber(float(js), js)


# Behaviours of the draft


def test_behaviour_a_value_resolves_through_an_import_and_an_export_alias():
    with Build('import{z as local}from"/m1.js";export var out=local;', 'var inner=["x"];export{inner as z}') as b:
        assert pv.strings(b.value(1, "inner", "elements").value) == ["x"]
        # The importer's alias evaluates to the declaring module's binding, and so does a variable
        # initialized with it.
        alias = b.bundle.scope_binding(0, "local")["id"]
        assert pv.strings(b.values.value(0, alias, "elements").value) == ["x"]
        assert b.value(0, "out", "declared").value == pv.JSArray(("x",))
        assert "not a primitive" in b.refused(0, "out", "primitive").reason


def test_behaviour_a_local_of_a_function_does_not_hide_an_import():
    with Build('var y="outer";export{y}', 'import{y}from"/m0.js";function f(){let y="inner";return y}var out=y') as b:
        assert b.value(1, "out", "primitive").value == "outer"


def test_behaviour_a_top_level_definition_wins_over_locals_of_the_same_name():
    with Build('function f(r){let n=r;return n}var r=new Set(["a","b"]);function g(){let r=1;return r}r.has("a");') as b:
        assert pv.strings(b.value(0, "r", "elements").value) == ["a", "b"]


def test_behaviour_a_name_assigned_only_inside_a_function_is_refused():
    with Build('var r;function init(){r=["late"]}') as b:
        b.refused(0, "r", "elements")


def test_behaviour_a_name_assigned_twice_at_the_top_level_is_refused():
    with Build('var r=["a"];r=["b"]') as b:
        assert "written" in b.refused(0, "r", "elements").reason


def test_behaviour_a_list_the_module_changes_is_refused():
    with Build('var r=["a"];function f(){r.push("b")}') as b:
        assert "push is called" in b.refused(0, "r", "elements").reason


def test_behaviour_an_import_from_a_module_outside_the_build_is_refused():
    with Build('import{q}from"/missing.js";var out=q') as b:
        assert "outside the bundle" in b.refused(0, "out", "primitive").reason


def test_behaviour_an_import_cycle_is_refused():
    with Build('import{q}from"/m1.js";export{q as p};export var v=q;', 'import{p}from"/m0.js";export{p as q}') as b:
        assert "cycle" in b.refused(0, "v", "primitive").reason


def test_behaviour_a_text_that_looks_like_an_assignment_inside_a_string_is_not_one():
    with Build('var s="r=[1]",r=["real"]') as b:
        assert pv.strings(b.value(0, "r", "elements").value) == ["real"]


def test_behaviour_literals_of_every_kind_evaluate():
    # The draft's fixture, with m and q declared before the declarators that read them: JavaScript runs
    # a var's initializer in order, so the draft's l=[s,...m] before m=[...] spreads undefined.
    text = ('var s="a\\u00e9",n=1e5,f=0.5,h=0x10,t=!0,u=!1,z=null,m=["x","y"],l=[s,...m],e=new Set(["a","a","b"]),'
            'p=new Map([["k","v"],["j",n]]),q={r:3},o={a:1,"b c":s,[s]:2,...q,m};'
            'e.has("a");p.get("k");l.includes("x");m.includes("x");o.a;')
    with Build(text) as b:
        primitives = [b.value(0, name, "primitive").value for name in ("s", "n", "f", "h", "t", "u", "z")]
        assert primitives == ["a\xe9", num("100000"), num("0.5"), num("16"), True, False, None]
        assert b.value(0, "l", "declared").value == pv.JSArray(("a\xe9", "x", "y"))
        assert pv.strings(b.value(0, "e", "elements").value) == ["a", "b"]
        assert b.value(0, "p", "entries").value == pv.JSMap((("k", "v"), ("j", num("100000"))))
        declared = b.value(0, "o", "declared").value
        assert declared == pv.JSObject((("a", num("1")), ("b c", "a\xe9"), ("a\xe9", num("2")), ("r", num("3")),
                                        ("m", pv.JSArray(("x", "y")))))
        # The fixture stores m in o's shorthand member, where code that reads o can change it, so m's
        # elements, and l's, which spread m, are not established; o holds an array, so it is not an
        # object of primitive entries.
        assert "stored in an object literal" in b.refused(0, "m", "elements").reason
        assert "stored in an object literal" in b.refused(0, "l", "elements").reason
        assert "not a primitive" in b.refused(0, "o", "entries").reason


def test_behaviour_a_value_two_items_need_is_evaluated_for_each():
    with Build("var x=1,out=[x,x],d={p:x,q:x};out.at(0);d.p;") as b:
        assert b.value(0, "out", "elements").value == pv.JSArray((num("1"), num("1")))
        assert b.value(0, "d", "entries").value == pv.JSObject((("p", num("1")), ("q", num("1"))))


def test_behaviour_a_value_that_needs_itself_is_refused():
    # In executable order a self-reference reads a binding that is not initialized yet.
    with Build("var a=[...b],b=[...a],self={[self]:1}") as b:
        for name in ("a", "self"):
            assert "initialized after" in b.refused(0, name, "declared").reason


def test_behaviour_a_list_method_over_an_empty_list_is_an_empty_list():
    with Build('var te=[],full=[{names:["X"]}],a=[...te.flatMap((e)=>e.names),"Y"],b=[...full.flatMap((e)=>e.names)]') as b:
        assert pv.strings(b.value(0, "a", "elements").value) == ["Y"]
        b.refused(0, "b", "elements")


def test_behaviour_code_that_is_not_a_literal_is_refused():
    with Build('var d="d",a=f(),b=x+1,c=`t${d}`,e=new Set(["a"]).add("b"),g=[1,h()]') as b:
        for name in "abceg":
            b.refused(0, name, "declared")


def test_behaviour_object_keys_leave_the_values_alone():
    with Build('var D="default",S={[D]:null,Proactive:{run:()=>go()},"Two words":f()};Object.keys(S);') as b:
        assert pv.keys(b.value(0, "S", "keys").value) == ["default", "Proactive", "Two words"]


def test_behaviour_the_function_a_binding_holds_spans_its_declaration():
    text = 'function Zoe(t,e){if(e)return t?`agent:builtin:${t}`:"agent:default";return"x"}var y=1;var k=(a)=>a;'
    with Build(text) as b:
        function = b.values.function(0, b.binding(0, "Zoe"))
        assert text[function.syntax["s"]:function.syntax["e"]].endswith('return"x"}')
        arrow = b.values.function(0, b.binding(0, "k"))
        assert arrow.syntax["k"] == "ArrowFunction"
    with Build('function Zoe(){}Zoe=null;') as b:
        with pytest.raises(pb.Unresolved, match="written"):
            b.values.function(0, b.binding(0, "Zoe"))


def _esm(name, text, encoding=0):
    codec = {0: "utf-8", 1: "latin-1", 2: "utf-16-le"}[encoding]
    return tb._module(name, text.encode(codec), module_format=1, encoding=encoding)


def test_behaviour_values_of_a_binary_in_every_encoding_bun_stores(tmp_path):
    binary = tb.build_bun_elf([
        _esm("/$bunfs/root/claude", 'import{a}from"/$bunfs/root/chunk-a.js";import{b}from"/$bunfs/root/chunk-b.js";'
                                    'var flag=[...a,...b];flag.includes("x");'),
        _esm("/$bunfs/root/chunk-a.js", 'var a=["h\xe9 ☃ \U0001f600"];export{a};', encoding=2),
        _esm("/$bunfs/root/chunk-b.js", 'var b=["caf\xe9"];export{b};', encoding=1),
    ])
    path = tmp_path / "claude"
    path.write_bytes(binary)
    with open(path, "rb") as file, pb.Bundle.from_binary(file) as bundle:
        values = pv.Values(bundle)
        flag = bundle.scope_binding(0, "flag")["id"]
        assert pv.strings(values.value(0, flag, "elements").value) == ["h\xe9 ☃ \U0001f600", "caf\xe9"]


# Counterexamples


# pfg-advisor's constancy counterexamples, each in one module: the fact asked, and either a part of the
# refusal's reason or the built-in operation the program writes, which the value's requires name.
CONSTANCY_COUNTEREXAMPLES = [
    ("const V=[1];V.forEach((...args)=>args[2].push(2));", "elements", "a rest parameter of the callback receives the value"),
    ("const V=[1];Array.from(V,function(x){this.pop();return x},V);", "elements", "Array.from only with the value as its only argument"),
    ("const V=[1];JSON.stringify(V,function(k,x){if(k==='0')this.pop();return x});", "elements", "JSON.stringify only"),
    ("const V=[1];const C={[Symbol.hasInstance](x){x.pop();return true}};const accepted=V instanceof C;", "elements",
     "Symbol.hasInstance"),
    ("function f(x,y=arguments[0]){y.push(2)}const V=[1];f(V);", "elements", "reads arguments"),
    ("const proto=Set.prototype;proto.has=function(){this.add('b');return true};const V=new Set(['a']);V.has('a');",
     "elements", ("written", "Set.prototype.has")),
    ("const proto=Array.prototype;proto.includes=function(){this.push(2);return true};const V=[1];V.includes(1);",
     "elements", ("written", "Array.prototype.includes")),
    ("global.Set.prototype.has=function(){this.add('b');return true};const V=new Set(['a']);V.has('a');", "elements",
     ("written", "Set.prototype.has")),
    ("let V=1;eval('V=2');", "primitive", "direct eval"),
    ("const V=[1];eval('V.push(2)');", "elements", "direct eval"),
    ("function stringify(){delete this.x;return 'done'}const V={x:1,toString:stringify};String(V);", "keys", "own toString"),
    ("function encode(){delete this.x;return 1}const V={x:1,toJSON:encode};JSON.stringify(V);", "keys", "own toJSON"),
]


@pytest.mark.parametrize("text, fact, expected", CONSTANCY_COUNTEREXAMPLES)
def test_counterexample_a_use_that_runs_code_is_refused_or_its_written_operation_is_required(text, fact, expected):
    with Build(text) as b:
        if isinstance(expected, tuple):
            # The value's own uses only read it, but the program replaces the method they call: the scan
            # finds the write, and the value's requires names the method.
            value = b.value(0, "V", fact)
            assert expected[1] in value.requires
            writes = {entry["target"] for entry in b.bundle.builtin_summary(b.bundle.builtin_scan())["writes"]}
            assert expected[1] in writes
        else:
            assert expected in b.refused(0, "V", fact).reason


def test_counterexample_a_namespace_callback_default_parameter_reaches_the_value():
    texts = ('export const f=()=>{};export const V=[1];',
             'import("/m0.js").then(function({f},whole=arguments[0]){whole.V.pop()});')
    with Build(*texts) as b:
        assert "reads arguments" in b.refused(0, "V", "elements").reason


def test_every_operation_a_value_can_require_is_a_write_the_scan_can_produce():
    import test_pfg_bundle  # its writing programs for a dependency key
    with Build("") as b:
        vocabulary = set(b.bundle.operation_vocabulary())
    assert {"Set.prototype.has", "Array.prototype.includes", "%ArrayIteratorPrototype%.next"} <= vocabulary
    test_pfg_bundle.assert_writes_produced(sorted(vocabulary | pv.EVALUATION_REQUIRES))


def test_a_module_with_parse_diagnostics_refuses_container_facts_but_not_other_values():
    with Build('export var V=["a"];export var P="p";V.includes("a");', 'import{V}from"/m0.js";V.push(') as b:
        assert "parse diagnostics" in b.refused(0, "V", "elements").reason
        assert b.value(0, "P", "primitive").value == "p"
        assert b.value(0, "V", "declared").value == pv.JSArray(("a",))


# Evaluation


def test_numbers_keep_their_javascript_spelling_and_negative_zero():
    with Build("var a=-0,b=1e21,c=-1e-7,d=0x10,e=1_000,f=1e999;") as b:
        assert b.value(0, "a", "primitive").value == pv.JSNumber(-0.0, "0")
        assert b.value(0, "b", "primitive").value.js == "1e+21"
        assert b.value(0, "c", "primitive").value.js == "-1e-7"
        assert b.value(0, "d", "primitive").value.js == "16"
        assert b.value(0, "e", "primitive").value.js == "1000"
        assert "not finite" in b.refused(0, "f", "primitive").reason


def test_set_members_are_distinct_under_same_value_zero():
    with Build("var s=new Set([true,1,0,-0,'1',null,void 0,undefined]);s.has(1);") as b:
        assert b.value(0, "s", "elements").value == pv.JSSet((True, num("1"), num("0"), "1", None, pv.UNDEFINED))


def test_object_keys_follow_javascript_order_and_duplicates_keep_their_first_place():
    with Build('var o={b:1,2:"two",a:2,1:"one",b:3,["__proto__"]:4};o.b;') as b:
        assert b.value(0, "o", "entries").value == pv.JSObject(
            (("1", "one"), ("2", "two"), ("b", num("3")), ("a", num("2")), ("__proto__", num("4"))))
    with Build("var o={__proto__:null,a:1};") as b:
        assert "prototype" in b.refused(0, "o", "declared").reason


def test_holes_operators_and_substitutions_are_refused():
    with Build('var a=[1,,2],b="x"+"y",c=`a${1}`,d=+1,e=-x;') as b:
        assert "hole" in b.refused(0, "a", "declared").reason
        for name in "bcde":
            b.refused(0, name, "declared")


def test_a_map_copies_a_map_and_keeps_the_first_key_with_the_last_value():
    with Build('var m=new Map([["a",1],["a",2],[1,"x"]]),n=new Map(m);n.get("a");') as b:
        assert b.value(0, "m", "entries").value == pv.JSMap((("a", num("2")), (num("1"), "x")))
        assert b.value(0, "n", "entries").value == pv.JSMap((("a", num("2")), (num("1"), "x")))


def test_a_literal_expression_inside_a_declaration_is_evaluated_as_declared():
    with Build('var C=["a","b"];var d=f.kind({min:1,max:C.length,list:[...C,"c"]}),e=g([x]);') as b:
        declaration = b.bundle.binding(0, b.binding(0, "d"))["declarations"][0]
        call = b.bundle.syntax(0, declaration["init"]["h"], 4)
        argument = call["arguments"][0]
        # max reads a member, which is not a literal.
        assert "not a literal" in pytest.raises(pb.Unresolved, b.values.expression, 0, argument["h"],
                                                declaration["statement"], declaration["declarator"]).value.reason
        later = b.bundle.binding(0, b.binding(0, "e"))["declarations"][0]
        array = b.bundle.syntax(0, later["init"]["h"], 4)["arguments"][0]
        assert "not declared" in pytest.raises(pb.Unresolved, b.values.expression, 0, array["h"],
                                               later["statement"], later["declarator"]).value.reason
    with Build('var C=["a","b"];var d=f.kind({min:1,list:[...C,"c"]});var late=["z"];var e=f.kind(late);') as b:
        declaration = b.bundle.binding(0, b.binding(0, "d"))["declarations"][0]
        argument = b.bundle.syntax(0, declaration["init"]["h"], 4)["arguments"][0]
        value = b.values.expression(0, argument["h"], declaration["statement"], declaration["declarator"])
        assert value.value == pv.JSObject((("min", num("1")), ("list", pv.JSArray(("a", "b", "c")))))
        assert value.fact == "declared" and "Array.prototype.@@iterator" in value.requires
        # A name that a later statement initializes is not initialized yet.
        first = b.bundle.binding(0, b.binding(0, "d"))["declarations"][0]
        later = b.bundle.syntax(0, b.bundle.binding(0, b.binding(0, "e"))["declarations"][0]["init"]["h"], 4)["arguments"][0]
        assert "initialized after" in pytest.raises(pb.Unresolved, b.values.expression, 0, later["h"], first["statement"]).value.reason


# Initialization order


def test_a_forward_reference_in_one_statement_or_a_later_statement_is_refused():
    with Build('var l=[...m],m=["x"];var e=[...k];var k=["y"];function f(){return 1}var g=[f];') as b:
        assert "initialized after" in b.refused(0, "l", "elements").reason
        assert "initialized after" in b.refused(0, "e", "elements").reason


def test_an_import_from_a_module_in_a_static_cycle_is_refused():
    with Build('import{y}from"/m1.js";export var x=["a"];export var out=[...y];',
               'import{x}from"/m0.js";export var y=["b"];export var back=[...x];') as b:
        assert "not a static dependency" in b.refused(0, "out", "elements").reason


def test_an_import_from_a_static_dependency_is_read():
    with Build('import{tA}from"/m1.js";var n=new Set([tA,"B"]);n.has("B");', 'export var tA="A";') as b:
        assert pv.strings(b.value(0, "n", "elements").value) == ["A", "B"]


# Constancy across modules


def test_a_use_in_an_importing_module_is_classified_there():
    with Build('export var V=["a"];', 'import{V}from"/m0.js";V.push("b");') as b:
        error = b.refused(0, "V", "elements")
        assert error.sites[0][0] == 1 and "push is called" in error.reason
    with Build('export var V=["a"];', 'import{V as W}from"/m0.js";export{W as X};', 'import{X}from"/m1.js";X.includes("a");') as b:
        value = b.value(0, "V", "elements")
        assert "Array.prototype.includes" in value.requires


def test_a_namespace_import_is_followed_through_member_reads():
    with Build('export var V=["a"];export function f(){}', 'import*as ns from"/m0.js";ns.V.includes("a");',
               'import*as ns from"/m0.js";ns.V.push("b");') as b:
        error = b.refused(0, "V", "elements")
        assert [site[0] for site in error.sites] == [2]


def test_the_2_1_304_dynamic_import_forms_expose_only_the_functions_they_destructure():
    texts = ('export var S=new Set(["A"]);export function apply(){}',
             'Promise.all([g(),import("/m0.js")]).then(([x,{apply:e}])=>e()).finally(h);',
             'async function k(){let[{apply:e}]=await Promise.all([import("/m0.js")]);e()}',
             'S.has?.("A");')
    with Build(*texts) as b:
        assert pv.strings(b.value(0, "S", "elements").value) == ["A"]


def test_a_dynamic_import_with_a_computed_specifier_refuses_every_container_fact():
    with Build('export var V=["a"];', 'async function f(n){return import(n)}') as b:
        assert "can load any module" in b.refused(0, "V", "elements").reason
        assert b.value(0, "V", "declared").value == pv.JSArray(("a",))
    with Build('export var V=["a"];', 'async function f(e){return import(`@withfig/autocomplete/build/${e}.js`)}') as b:
        assert pv.strings(b.value(0, "V", "elements").value) == ["a"]


def test_keys_are_read_without_the_values_and_hooks_refuse_conversions():
    text = ('var Sx="default",j9={[Sx]:{prompt:`a${x}`},Proactive:{p:`b${y}`},get G(){return 1}};'
            'Object.keys(j9);Object.hasOwn(j9,"x");')
    with Build(text) as b:
        value = b.value(0, "j9", "keys").value
        assert pv.keys(value) == ["default", "Proactive", "G"] and value.accessors == {"G"}
    with Build('var V={x:1,toString(){delete this.x;return "d"}};String(V);') as b:
        assert "own toString" in b.refused(0, "V", "keys").reason
