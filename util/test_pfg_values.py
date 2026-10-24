"""
Tests for pfg.values: the evaluator, the facts and the checks across modules that a value never changes,
and the initialization order.

The tests named test_behaviour_... come from an earlier draft of pfg.bundle that read the bundle with
regular expressions (plugin-fixes' util/test_pfg_bundle.py of 2026-10-20). That draft had 13 value-level
tests, which are test_behaviour_... tests here, and seven others, covered as follows:
  find_one_returns_the_module_and_match_of_the_only_site, find_one_counts_every_match_in_every_module:
    pfg gives every needle hit in every module (test_pfg_bundle.py, test_needles_hit_syntax_only and
    test_needle_hits_come_from_every_module_in_order). The catalog's predicate decides whether exactly one
    site matches, and raises AnchorError otherwise
    (test_an_anchor_error_counts_candidates_given_as_a_generator).
  contains_limits_the_search_to_modules_that_hold_the_text:
    the index pass parses every module once and records every hit in that pass, so pfg does not search the
    modules again. test_needles_hit_syntax_only checks that only code gives hits.
  an_import_from_a_module_outside_the_build_is_refused, an_import_cycle_is_refused:
    test_behaviour_an_import_from_a_module_outside_the_build_is_refused and
    test_behaviour_an_import_cycle_is_refused here, for values, and test_pfg_bundle.py,
    test_cycles_missing_names_and_outside_modules_are_refused, for export().
  function_span_within_a_module: test_behaviour_the_function_a_binding_holds_spans_its_declaration.
  modules_of_a_binary_in_every_encoding_bun_stores:
    test_pfg_bundle.py, test_modules_of_a_binary_in_every_encoding_bun_stores, and
    test_behaviour_values_of_a_binary_in_every_encoding_bun_stores here.
The tests named test_counterexample_... are 13 programs that each change the value in Node through a use
that a list of accepted method names would let through. They are the reason the model classifies each use
by what the parent expression does with the value.
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
    # The draft's fixture, with m and q declared before the declarators that read them. JavaScript runs
    # the declarators of a var statement in order, so the draft's l=[s,...m] before m=[...] would spread
    # undefined, which throws a TypeError.
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
        # The fixture stores m in o's shorthand member, where code that reads o can change it. So the
        # elements fact is refused for m, and for l, which spreads m. A member of o is an array, so the
        # entries fact is refused for o.
        assert "stored in an object literal" in b.refused(0, "m", "elements").reason
        assert "stored in an object literal" in b.refused(0, "l", "elements").reason
        assert "not a primitive" in b.refused(0, "o", "entries").reason


def test_behaviour_a_value_two_items_need_is_evaluated_for_each():
    with Build("var x=1,out=[x,x],d={p:x,q:x};out.at(0);d.p;") as b:
        assert b.value(0, "out", "elements").value == pv.JSArray((num("1"), num("1")))
        assert b.value(0, "d", "entries").value == pv.JSObject((("p", num("1")), ("q", num("1"))))


def test_behaviour_a_value_that_needs_itself_is_refused():
    # In execution order, a self-reference reads a binding that is not initialized yet.
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


def test_an_array_of_records_with_methods_is_unchanged_when_every_use_only_reads_it():
    # Like the idle compaction kinds of Claude Code 2.1.309: objects with string members and functions,
    # read with find and some, also from a module that imports the array.
    kinds = 'export var K=[{name:"a",flag:"f",isInUse:()=>x(),due(e){return e}},{name:"b",flag:"g",isInUse:()=>!0}];'
    with Build(kinds + 'function pick(){return K.find((S)=>S.isInUse())}',
               'import{K}from"/m0.js";K.some((e)=>e.flag==="g");') as b:
        assert b.values.unchanged_array(1, b.binding(1, "K")) == {"Array.prototype.find", "Array.prototype.some"}
    changes = [('K.push({name:"c"});', "push is called"), ("K=[];", "written"), ("K.length=0;", "written")]
    for change, reason in changes:
        with Build(kinds + change) as b, pytest.raises(pb.Unresolved, match=reason):
            b.values.unchanged_array(0, b.binding(0, "K"))
    # A write to a member of an element changes the element, not the array.
    with Build(kinds + 'K.find((S)=>S.isInUse()).flag="h";') as b:
        assert b.values.unchanged_array(0, b.binding(0, "K")) == {"Array.prototype.find"}
    others = [('var K=new Array(2);', "not initialized with an array literal"),
              ('function f(){var K=[1];return K}', "not a variable of the module's scope"),
              ('var K;K=[1];', "not one variable declaration with an initializer")]
    for text, reason in others:
        with Build(text) as b:
            at = text.index("K")
            binding = b.bundle.binding_at(0, at, at + 1)["id"]
            with pytest.raises(pb.Unresolved, match=reason):
                b.values.unchanged_array(0, binding)


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


# The counterexamples to classifying uses by method name: each is a program of one module, with the fact
# asked for and the expected result, which is a part of the refusal's reason, or ("written", operation) for
# a program that replaces a built-in operation, which the value's requires must then list.
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
            # The value's own uses only read it, but the program replaces the method they call. The scan
            # finds that write, and the value's requires lists the method.
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
    import test_pfg_bundle  # for assert_writes_produced
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


def test_a_value_passed_to_an_imported_function_is_classified_where_the_function_is_declared():
    # How Claude Code 2.1.305 reads its table of rate-limit words: function t1e(e){return fo(cae,e)||e},
    # with fo imported.
    declaring = 'export function fo(e,n){return Object.hasOwn(e,n)?e[n]:void 0}export function put(e){e.a="y"}'
    with Build('import{fo}from"/m1.js";var V={a:"x"};function t(e){return fo(V,e)||e}', declaring) as b:
        value = b.value(0, "V", "entries")
        assert pv.string_map(value.value) == {"a": "x"} and "Object.hasOwn" in value.requires
    with Build('import{put}from"/m1.js";var V={a:"x"};put(V);', declaring) as b:
        assert b.refused(0, "V", "entries").sites == [(1, declaring.index('e.a="y"'), "a member of the value is written")]
    # Through a default import and a re-export.
    texts = ('import f from"/m1.js";import{g}from"/m2.js";var V=["x"];f(V);g(V);',
             'export default function f(a){return a.includes("x")}', 'export{h as g}from"/m3.js";', 'export const h=(c)=>c.at(0);')
    with Build(*texts) as b:
        value = b.value(0, "V", "elements")
        assert pv.strings(value.value) == ["x"]
        assert {"Array.prototype.includes", "Array.prototype.at"} <= value.requires
    # Passing the value to a function from outside the bundle, or to an anonymous default export, is
    # refused at the call.
    with Build('import{z}from"zlib";import f from"/m1.js";var V=["x"];z(V);f(V);', 'export default function(a){}') as b:
        assert sorted(site[2] for site in b.refused(0, "V", "elements").sites) == [
            "the value is passed to f: f is imported from an export that is not a binding",
            "the value is passed to z: z is imported from a module outside the bundle"]


def test_a_function_is_classified_on_the_route_with_the_fewest_calls():
    # j is called with the value directly and through g, h and i. On that long route, k(t) would be
    # the fifth call.
    texts = ('import{g}from"/m1.js";import{j}from"/m4.js";export var V=["x"];g(V);j(V);',
             'import{h}from"/m2.js";export function g(q){h(q)}',
             'import{i}from"/m3.js";export function h(r){i(r)}',
             'import{j}from"/m4.js";export function i(s){j(s)}',
             'import{k}from"/m5.js";export function j(t){k(t)}',
             'export function k(u){return u.includes("x")}')
    with Build(*texts) as b:
        assert "Array.prototype.includes" in b.value(0, "V", "elements").requires
    with Build(texts[0].replace("j(V);", ""), *texts[1:]) as b:
        error = b.refused(0, "V", "elements")
        assert error.sites == [(4, texts[4].index("t)}"), "the value is passed on more than four calls deep")]


def test_a_function_imported_by_several_modules_is_read_once_in_its_module():
    texts = ('export function f(a){return a}', 'import{f}from"/m0.js";f(1);', 'import{f as g}from"/m0.js";g(2);')
    with Build(*texts) as b:
        f, f1, g2 = b.binding(0, "f"), b.binding(1, "f"), b.binding(2, "g")
        asked = []
        facts = b.bundle.facts
        b.bundle.facts = lambda index, queries: asked.append(index) or facts(index, queries)
        first = b.values.function(1, f1)
        assert first.module == 0 and 0 in asked
        asked.clear()
        second = b.values.function(2, g2)
        # Module 2's record of g is asked for; module 0, which declares f, is not asked again.
        assert second is first and asked == [2]
        asked.clear()
        assert b.values.declaring(2, g2) == (0, f) and asked == []


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
