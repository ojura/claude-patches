"""
values: the constant values of a native build's bindings, for code that reads tables out of the build.

Values(bundle).value(module, binding, fact) evaluates the initializer of a module-scope binding from its
syntax, without running any code, and checks what the fact claims:

  primitive  a string, number, boolean, null or undefined; the binding is never written.
  elements   an Array or a Set of primitives, with its members in order; neither the binding nor the
             container ever changes.
  entries    an object whose members are data members with primitive values, or a Map with primitive keys
             and values, with its entries in order; neither the binding nor the container ever changes.
  keys       the own keys of an object literal, in order, without evaluating its values; the binding is
             never written, and the object's own keys never change.
  declared   the value as the declarations write it, with the bindings it refers to evaluated the same
             way. This fact does not check whether the value changes later, and an evaluation under
             another fact never uses a declared value.

For elements, entries and keys, every use of the value must have a form that the operation model in
pfg/js/facts.mjs lists. The uses checked are those in the binding's module, in every module that imports
the binding directly or through re-exports, at every place where a module takes a namespace that contains
it (import * as, import() and import.meta.require), and in every parameter that receives the value, also
of functions that other modules declare. An import() or import.meta.require whose specifier is not a
literal could load any module, so it refuses these three facts for every value. The exception is a
template whose fixed head is a bare specifier (it does not start with . or /) and so refers to a package
outside the build, because code loaded at run time from outside the binary is not part of the analysed
program. The requires field of each Value lists the built-in operations that its evaluation and its uses
rely on, and the caller checks them against the built-in scan (Bundle.builtin_summary,
builtin_write_affects).

The evaluator reads literal syntax only: strings and templates without substitutions (their cooked
values), finite numbers (keeping -0), true, false, null, !0, !1, void 0, unary minus on a number,
undefined, names of bindings (evaluated under the fact their position needs), array literals with spreads
of arrays and Sets, new Set(iterable), new Map(array of pairs or Map), object literals with literal or
computed keys and spreads of objects, and flatMap, map or filter on an empty array, whose callback is
therefore never called. The evaluator refuses anything else, such as holes, a literal __proto__ member,
methods, accessors, a template with a substitution, or any operator. An initializer may read only a module
binding that is initialized before it: one declared in an earlier statement or declarator, a function
declaration, or an import from a module that this one statically depends on and that is not in a cycle
with it.

A refusal raises Unresolved at the first refused site, and its sites attribute lists every refused site as
(module, offset, reason).
"""
from collections import deque
from dataclasses import dataclass, field
import heapq
import itertools
import json
import re

from .bundle import BackendError, Unresolved, builtin_write_affects  # noqa: F401 (re-exported for callers)

FACTS = ("primitive", "elements", "entries", "keys", "declared")
# Every built-in operation the evaluator itself can require; _Frame.require raises AssertionError for any
# other name. util/test_pfg_values.py checks that the built-in scan can record a write to each.
EVALUATION_REQUIRES = frozenset((
    "Array.prototype.@@iterator", "%ArrayIteratorPrototype%.next", "Set.prototype.@@iterator", "%SetIteratorPrototype%.next",
    "Map.prototype.@@iterator", "%MapIteratorPrototype%.next", "Set", "Set.prototype.add", "Map", "Map.prototype.set",
    "Array.prototype.flatMap", "Array.prototype.map", "Array.prototype.filter", "Array.prototype.constructor", "Array.@@species",
))
_EXPAND_DEPTH = 24
_STATIC_LOADS = ("import", "side", "reexport", "export*", "export*as")
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


class _Undefined:
    __slots__ = ()

    def __repr__(self):
        return "undefined"


UNDEFINED = _Undefined()


@dataclass(frozen=True)
class JSNumber:
    """A finite number: value is -0.0 for negative zero, and js is String(value) in JavaScript."""
    value: float
    js: str


@dataclass(frozen=True)
class JSArray:
    items: tuple


@dataclass(frozen=True)
class JSSet:
    """A Set's members in insertion order, distinct under SameValueZero."""
    items: tuple


@dataclass(frozen=True)
class JSMap:
    """A Map's (key, value) entries in insertion order, keys distinct under SameValueZero."""
    entries: tuple


@dataclass(frozen=True)
class JSObject:
    """An object literal's (key, value) members in JavaScript's own-key order: array indexes in
    ascending order, then the other keys in the order they were first defined."""
    entries: tuple


@dataclass(frozen=True)
class JSKeys:
    """The own keys of an object literal in JavaScript's own-key order, and which of them are accessors
    and which are methods. The values are not evaluated."""
    keys: tuple
    accessors: frozenset
    methods: frozenset


@dataclass(frozen=True)
class Value:
    """A binding's value under a fact, with the built-in operations it relies on. For Values.expression,
    binding is the handle of the expression."""
    value: object
    fact: str
    module: int
    binding: str
    requires: frozenset


@dataclass(frozen=True)
class Function:
    """The function a binding holds, for reading a table out of its code: module, binding, the handle of
    the function declaration or expression, and its syntax tree."""
    module: int
    binding: str
    handle: str
    syntax: dict


@dataclass
class _Frame:
    """The evaluation of one initializer: its module, the indexes of its declaration's statement and
    declarator, whether it evaluates declared values, the evaluations in progress that led to it as
    (module, binding, fact) keys (path), and the built-in operations it relies on (requires)."""
    module: int
    statement: int
    declarator: object
    declared: bool
    path: tuple
    requires: set = field(default_factory=set)

    def require(self, *names):
        for name in names:
            if name not in EVALUATION_REQUIRES:
                raise AssertionError(f"the evaluator requires {name}, which EVALUATION_REQUIRES does not list")
            self.requires.add(name)


def is_primitive(value):
    return isinstance(value, (str, bool, JSNumber)) or value is None or value is UNDEFINED


def _same_value_zero(value):
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, JSNumber):
        return ("number", 0.0 if value.value == 0 else value.value)
    if isinstance(value, str):
        return ("string", value)
    if value is None:
        return ("null",)
    if value is UNDEFINED:
        return ("undefined",)
    raise TypeError(f"{value!r} is not a primitive")


def _property_key(value):
    """ToPropertyKey of a primitive."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, JSNumber):
        return value.js
    if value is None:
        return "null"
    return "undefined"


def _is_array_index(key):
    return bool(_ARRAY_INDEX.fullmatch(key)) and int(key) < 2 ** 32 - 1


def _own_key_order(keys):
    """Keys in JavaScript's own-key order, from keys in the order they were first defined."""
    keys = list(keys)
    return [key for key in sorted((key for key in keys if _is_array_index(key)), key=int)] + \
        [key for key in keys if not _is_array_index(key)]


def _child(node, name):
    value = node.get(name)
    if value is None:
        raise KeyError(name)
    return value


# Readers for tables: each raises ValueError for a value of another type.

def strings(value):
    """The members of an Array or a Set of strings."""
    if isinstance(value, (JSArray, JSSet)) and all(isinstance(item, str) for item in value.items):
        return list(value.items)
    raise ValueError(f"not an Array or Set of strings: {value!r}")


def string_map(value):
    """The members of an object whose member values are strings, as a dict."""
    if isinstance(value, JSObject) and all(isinstance(item, str) for _, item in value.entries):
        return dict(value.entries)
    raise ValueError(f"not an object of strings: {value!r}")


def keys(value):
    """The own keys of an object."""
    if isinstance(value, JSKeys):
        return list(value.keys)
    if isinstance(value, JSObject):
        return [key for key, _ in value.entries]
    raise ValueError(f"not an object: {value!r}")


def number(value):
    if isinstance(value, JSNumber):
        return value
    raise ValueError(f"not a number: {value!r}")


def string(value):
    if isinstance(value, str):
        return value
    raise ValueError(f"not a string: {value!r}")


class Values:
    """The values of a Bundle's bindings (see the module docstring)."""

    def __init__(self, bundle):
        self.bundle = bundle
        self._values = {}
        self._functions = {}
        self._reach = {}
        self._unfollowed_loads = None
        self._export_names = {}
        self._declarations = {}
        self._scopes = {}

    # -- refusals ----------------------------------------------------------------------------------

    @staticmethod
    def _refuse(module, offset, reason):
        error = Unresolved(module, offset, reason)
        error.sites = [(module, offset, reason)]
        return error

    @staticmethod
    def _refuse_all(sites):
        module, offset, reason = sites[0]
        listed = "; ".join(f"module {m} at {s}: {r}" for m, s, r in sites)
        error = Unresolved(module, offset, reason if len(sites) == 1 else f"{len(sites)} uses are refused: {listed}")
        error.sites = list(sites)
        return error

    # -- syntax ------------------------------------------------------------------------------------

    def _tree(self, module, handle):
        return self.bundle.syntax(module, handle, _EXPAND_DEPTH)

    def _full(self, module, node):
        """Return node with its children, asking the helper for them when a depth-limited reply left them out."""
        return self._tree(module, node["h"]) if node.get("truncated") else node

    def _unparenthesized(self, module, node):
        node = self._full(module, node)
        while node["k"] == "ParenthesizedExpression":
            node = self._full(module, _child(node, "expression"))
        return node

    # -- bindings ----------------------------------------------------------------------------------

    def _record(self, module, binding):
        record = self.bundle.binding(module, binding)
        if record.get("refused"):
            raise self._refuse_all([(module, item["s"], item["reason"]) for item in record["refused"]])
        return record

    def declaring(self, module, binding):
        """The (module, binding id) of the binding that a binding resolves to: the binding itself, or, for
        an import, the binding that its exporting module declares, also through re-exports. Values caches
        these answers and the scope lookups in the declaring modules. The helper keeps the program of only
        one module at a time, so without the cache, a client that follows the imports of one function from
        many modules would make the helper build the declaring module's program again for each importer."""
        key = (module, binding)
        if key not in self._declarations:
            self._declarations[key] = self._declaring(module, binding)
        return self._declarations[key]

    def _declaring(self, module, binding):
        record = self._record(module, binding)
        if not record["kind"].startswith("import"):
            return module, binding
        declaration = record["declarations"][0]
        if record["kind"] == "import namespace":
            raise self._refuse(module, declaration["s"], "the name is a module namespace, not a literal value")
        target, imported, _ = self.bundle.imports(module)[record["name"]]
        if target is None:
            raise self._refuse(module, declaration["s"], f"{record['name']} is imported from a module outside the bundle")
        try:
            resolved = self.bundle.export(target, imported)
        except Unresolved as error:
            raise self._refuse(error.module, error.offset, error.reason) from None
        if resolved[0] != "binding":
            raise self._refuse(module, declaration["s"], f"{record['name']} is imported from an export that is not a binding")
        _, declaring_module, local = resolved
        if (declaring_module, local) not in self._scopes:
            self._scopes[(declaring_module, local)] = self.bundle.scope_binding(declaring_module, local)
        found = self._scopes[(declaring_module, local)]
        if "id" not in found:
            raise self._refuse(declaring_module, 0, f"module {declaring_module} exports {local}, which it does not declare")
        return declaring_module, found["id"]

    # -- initialization order ----------------------------------------------------------------------

    def _reachable(self, module):
        """The modules that module statically depends on, including module itself. A module with parse
        diagnostics on the way raises Unresolved, because its links cannot be read."""
        if module not in self._reach:
            seen, queue = {module}, deque([module])
            while queue:
                current = queue.popleft()
                try:
                    loads = self.bundle.loads(current)
                except Unresolved as error:
                    raise self._refuse(error.module, error.offset, error.reason) from None
                for load in loads:
                    target = load.get("t")
                    if load["k"] in _STATIC_LOADS and target is not None and target not in seen:
                        seen.add(target)
                        queue.append(target)
            self._reach[module] = seen
        return self._reach[module]

    def _check_order(self, frame, module, binding, reference):
        """Check that the binding (module, binding) is initialized before frame's initializer runs.
        reference is the name in that initializer that reads the binding."""
        if module == frame.module:
            record = self._record(module, binding)
            declaration = record["declarations"][0]
            if record["kind"] == "function":
                return
            if not declaration.get("module"):
                raise self._refuse(frame.module, reference["s"], f"{reference['text']} is local to a function")
            mine = (frame.statement, -1 if frame.declarator is None else frame.declarator)
            theirs = (declaration["statement"], -1 if declaration.get("declarator") is None else declaration["declarator"])
            if theirs >= mine:
                raise self._refuse(frame.module, reference["s"],
                                   f"{reference['text']} is initialized after the initializer that reads it")
            return
        if module not in self._reachable(frame.module) or frame.module in self._reachable(module):
            raise self._refuse(frame.module, reference["s"],
                               f"{reference['text']} comes from module {module}, which either "
                               f"is not a static dependency of module {frame.module} or is in a cycle with it, "
                               "so it may be initialized later")

    # -- evaluation --------------------------------------------------------------------------------

    def _identifier(self, module, node, frame, role):
        if node.get("refused"):
            record = self.bundle.binding(module, node["b"])
            raise self._refuse_all([(module, item["s"], item["reason"]) for item in record.get("refused", [])]
                                   or [(module, node["s"], "the binding model refuses the binding this name refers to")])
        if "b" in node:
            declaring_module, binding = self.declaring(module, node["b"])
            self._check_order(frame, declaring_module, binding, node)
            fact = "declared" if frame.declared else {"primitive": "primitive", "elements": "elements",
                                                      "entries": "entries", "any": "declared"}[role]
            value = self.value(declaring_module, binding, fact, _path=frame.path)
            frame.requires |= value.requires
            return value.value
        if node.get("free") == "undefined":
            return UNDEFINED
        raise self._refuse(module, node["s"], f"{node.get('text')} is not declared in the module")

    def _evaluate(self, module, node, frame, role):
        """The value of an expression. role says what its position needs: a primitive ('primitive'), an
        Array or a Set ('elements'), an object or a Map ('entries'), or any literal value ('any', for
        declared values)."""
        node = self._unparenthesized(module, node)
        kind = node["k"]
        if kind == "Identifier":
            value = self._identifier(module, node, frame, role)
        elif kind == "StringLiteral":
            value = node["value"]
        elif kind == "NoSubstitutionTemplateLiteral":
            if node.get("invalid"):
                raise self._refuse(module, node["s"],
                                   "the cooked value of a template with an invalid escape is undefined")
            value = node["value"]
        elif kind == "NumericLiteral":
            value = self._number(module, node)
        elif kind == "TrueKeyword":
            value = True
        elif kind == "FalseKeyword":
            value = False
        elif kind == "NullKeyword":
            value = None
        elif kind == "PrefixUnaryExpression":
            value = self._unary(module, node)
        elif kind == "VoidExpression":
            operand = self._unparenthesized(module, _child(node, "expression"))
            if operand["k"] != "NumericLiteral":
                raise self._refuse(module, node["s"], "void of an expression that is not a number literal")
            value = UNDEFINED
        elif kind == "ArrayLiteralExpression":
            value = self._array(module, node, frame, role)
        elif kind == "ObjectLiteralExpression":
            value = self._object(module, node, frame, role)
        elif kind == "NewExpression":
            value = self._construct(module, node, frame, role)
        elif kind == "CallExpression":
            value = self._empty_call(module, node, frame)
        elif kind == "TemplateExpression":
            raise self._refuse(module, node["s"], "a template with a substitution is not a literal")
        else:
            raise self._refuse(module, node["s"], f"a {kind} is not a literal value the evaluator reads")
        self._check_role(module, node, value, role)
        return value

    def _check_role(self, module, node, value, role):
        if role == "primitive" and not is_primitive(value):
            raise self._refuse(module, node["s"], "the value at this position is not a primitive")
        if role == "elements" and not isinstance(value, (JSArray, JSSet)):
            raise self._refuse(module, node["s"], "the value at this position is not an Array or a Set")
        if role == "entries" and not isinstance(value, (JSObject, JSMap)):
            raise self._refuse(module, node["s"], "the value at this position is not an object or a Map")

    def _number(self, module, node):
        if not node["finite"]:
            raise self._refuse(module, node["s"], "the number is not finite")
        return JSNumber(float(node["value"]), node["js"])

    def _unary(self, module, node):
        operand = self._unparenthesized(module, _child(node, "operand"))
        if operand["k"] == "NumericLiteral":
            number_ = self._number(module, operand)
            if node["op"] == "-":
                return JSNumber(-number_.value, "0" if number_.value == 0 else f"-{number_.js}")
            if node["op"] == "!" and number_.value in (0, 1):
                return number_.value == 0
        raise self._refuse(module, node["s"],
                           f"the {node['op']} expression is not !0, !1 or a minus before a number literal")

    def _items(self, module, node, frame, role):
        """The members an iterable expression gives, for a spread or a constructor argument."""
        source = self._evaluate(module, node, frame, "any" if frame.declared else "elements")
        if isinstance(source, JSArray):
            frame.require("Array.prototype.@@iterator", "%ArrayIteratorPrototype%.next")
            return list(source.items)
        if isinstance(source, JSSet):
            frame.require("Set.prototype.@@iterator", "%SetIteratorPrototype%.next")
            return list(source.items)
        raise self._refuse(module, node["s"], "the spread or argument is not an Array or a Set")

    def _array(self, module, node, frame, role):
        member_role = "any" if role == "any" else "primitive"
        items = []
        for element in node.get("elements") or []:
            element = self._full(module, element)
            if element["k"] == "OmittedExpression":
                raise self._refuse(module, element["s"], "the array has a hole")
            if element["k"] == "SpreadElement":
                for item in self._items(module, _child(element, "expression"), frame, role):
                    if member_role == "primitive" and not is_primitive(item):
                        raise self._refuse(module, element["s"], "the spread gives a member that is not a primitive")
                    items.append(item)
            else:
                items.append(self._evaluate(module, element, frame, member_role))
        return JSArray(tuple(items))

    def _key(self, module, member, frame):
        """The property key of an object literal member."""
        if "key" in member:
            return member["key"]
        name = self._full(module, _child(member, "name"))
        if name["k"] == "ComputedPropertyName":
            return _property_key(self._evaluate(module, _child(name, "expression"), frame, "primitive"))
        raise self._refuse(module, name["s"], "the member's name is not a key the evaluator reads")

    def _object(self, module, node, frame, role):
        member_role = "any" if role == "any" else "primitive"
        members = {}
        for member in node.get("properties") or []:
            member = self._full(module, member)
            kind = member["k"]
            if kind == "SpreadAssignment":
                source = self._evaluate(module, _child(member, "expression"), frame, "any" if frame.declared else "entries")
                if not isinstance(source, JSObject):
                    raise self._refuse(module, member["s"], "the spread is not an object")
                for key, value in source.entries:
                    members[key] = value
                continue
            if kind == "PropertyAssignment":
                name = self._full(module, _child(member, "name"))
                key = self._key(module, member, frame)
                if key == "__proto__" and name["k"] != "ComputedPropertyName":
                    raise self._refuse(module, member["s"], "a __proto__ member sets the object's prototype")
                members[key] = self._evaluate(module, _child(member, "initializer"), frame, member_role)
                continue
            if kind == "ShorthandPropertyAssignment":
                if member.get("objectAssignmentInitializer"):
                    raise self._refuse(module, member["s"],
                                       "a shorthand member with a default is valid only in a destructuring pattern")
                members[self._key(module, member, frame)] = self._evaluate(module, _child(member, "name"), frame, member_role)
                continue
            raise self._refuse(module, member["s"], f"a {kind} defines a member that is not a data member")
        return JSObject(tuple((key, members[key]) for key in _own_key_order(members)))

    def _construct(self, module, node, frame, role):
        callee = self._unparenthesized(module, _child(node, "expression"))
        arguments = [self._full(module, argument) for argument in node.get("arguments") or []]
        name = callee.get("free") if callee["k"] == "Identifier" else None
        if name not in ("Set", "Map"):
            raise self._refuse(module, node["s"], "only new Set and new Map with the free names Set and Map are evaluated")
        if any(argument["k"] == "SpreadElement" for argument in arguments) or len(arguments) > 1:
            raise self._refuse(module, node["s"], f"new {name} has a spread argument or more than one argument")
        if name == "Set":
            frame.require("Set", "Set.prototype.add")
            members = {}
            for item in self._items(module, arguments[0], frame, role) if arguments else []:
                if not is_primitive(item):
                    raise self._refuse(module, node["s"], "a Set member is not a primitive")
                members.setdefault(_same_value_zero(item), item)
            return JSSet(tuple(members.values()))
        frame.require("Map", "Map.prototype.set")
        if not arguments:
            return JSMap(())
        source = self._unparenthesized(module, arguments[0])
        if source["k"] == "ArrayLiteralExpression":
            frame.require("Array.prototype.@@iterator", "%ArrayIteratorPrototype%.next")
            entries = {}
            for pair in source.get("elements") or []:
                pair = self._unparenthesized(module, pair)
                elements = [self._full(module, element) for element in pair.get("elements") or []] \
                    if pair["k"] == "ArrayLiteralExpression" else None
                if elements is None or len(elements) != 2 or any(e["k"] in ("SpreadElement", "OmittedExpression") for e in elements):
                    raise self._refuse(module, pair["s"], "a Map entry is not a pair literal [key, value]")
                key = self._evaluate(module, elements[0], frame, "primitive")
                value = self._evaluate(module, elements[1], frame, "primitive")
                previous = entries.get(_same_value_zero(key))
                entries[_same_value_zero(key)] = (previous[0] if previous else key, value)
            return JSMap(tuple(entries.values()))
        copied = self._evaluate(module, source, frame, "any" if frame.declared else "entries")
        if not isinstance(copied, JSMap):
            raise self._refuse(module, source["s"],
                               "the argument of new Map is neither an array literal of pairs nor a Map")
        frame.require("Map.prototype.@@iterator", "%MapIteratorPrototype%.next")
        return JSMap(copied.entries)

    def _empty_call(self, module, node, frame):
        """X.flatMap(f), X.map(f) or X.filter(f) on an empty Array X: f is never called."""
        callee = self._unparenthesized(module, _child(node, "expression"))
        arguments = [self._unparenthesized(module, argument) for argument in node.get("arguments") or []]
        method = self._full(module, _child(callee, "name")).get("text") if callee["k"] == "PropertyAccessExpression" else None
        if method not in ("flatMap", "map", "filter") or callee.get("optionalChain") or node.get("optionalChain") or \
                not 1 <= len(arguments) <= 2 or arguments[0]["k"] not in ("ArrowFunction", "FunctionExpression"):
            raise self._refuse(module, node["s"], "a call is not a literal value; only flatMap, map or filter on an empty array is")
        receiver = self._evaluate(module, _child(callee, "expression"), frame, "any" if frame.declared else "elements")
        if not isinstance(receiver, JSArray) or receiver.items:
            raise self._refuse(module, node["s"], f"{method} is called on something other than an empty array")
        frame.require(f"Array.prototype.{method}", "Array.prototype.constructor", "Array.@@species")
        return JSArray(())

    def _keys(self, module, node, frame):
        node = self._unparenthesized(module, node)
        if node["k"] != "ObjectLiteralExpression":
            raise self._refuse(module, node["s"], "the initializer is not an object literal")
        kinds = {}
        for member in node.get("properties") or []:
            member = self._full(module, member)
            kind = member["k"]
            if kind == "SpreadAssignment":
                raise self._refuse(module, member["s"], "a spread member hides which keys the object has")
            key = self._key(module, member, frame)
            if kind == "PropertyAssignment":
                name = self._full(module, _child(member, "name"))
                if key == "__proto__" and name["k"] != "ComputedPropertyName":
                    raise self._refuse(module, member["s"], "a __proto__ member sets the object's prototype")
                kinds[key] = "data"
            elif kind == "ShorthandPropertyAssignment":
                kinds[key] = "data"
            elif kind == "MethodDeclaration":
                kinds[key] = "method"
            elif kind in ("GetAccessor", "SetAccessor"):
                kinds[key] = "accessor"
            else:
                raise self._refuse(module, member["s"], f"a {kind} is not an object literal member the evaluator reads")
        return JSKeys(tuple(_own_key_order(kinds)), frozenset(k for k, v in kinds.items() if v == "accessor"),
                      frozenset(k for k, v in kinds.items() if v == "method"))

    # -- values ------------------------------------------------------------------------------------

    def value(self, module, binding, fact, _path=()):
        """The Value of a binding under fact; raises Unresolved when the source does not establish it."""
        if fact not in FACTS:
            raise ValueError(f"a fact is one of {', '.join(FACTS)}, not {fact!r}")
        key = (module, binding, fact)
        if key in self._values:
            return self._values[key]
        declaring_module, declaring = self.declaring(module, binding)
        if (declaring_module, declaring) != (module, binding):
            result = self.value(declaring_module, declaring, fact, _path)
            self._values[key] = result
            return result
        if key in _path:
            raise self._refuse(module, int(binding.split("@")[1]), "the value depends on itself")
        record = self._record(module, binding)
        initialized = [declaration for declaration in record["declarations"] if "init" in declaration]
        if record["kind"] not in ("var", "let", "const") or len(initialized) != 1:
            raise self._refuse(module, record["declarations"][0]["s"],
                               f"{record['name']} is not one variable declaration with an initializer")
        declaration = initialized[0]
        if declaration["kind"] != record["kind"] or not declaration.get("module"):
            raise self._refuse(module, declaration["s"], f"{record['name']} is not a variable of the module's scope")
        frame = _Frame(module, declaration["statement"], declaration.get("declarator"), fact == "declared", _path + (key,))
        initializer = self._tree(module, declaration["init"]["h"])
        if fact == "keys":
            js = self._keys(module, initializer, frame)
        else:
            js = self._evaluate(module, initializer, frame, {"primitive": "primitive", "elements": "elements",
                                                              "entries": "entries", "declared": "any"}[fact])
            self._check_fact(module, declaration, js, fact)
        if fact != "declared":
            frame.requires |= self._constancy(module, binding, self._model(js))
        result = Value(js, fact, module, binding, frozenset(frame.requires))
        self._values[key] = result
        return result

    def _check_fact(self, module, declaration, value, fact):
        offset = declaration["init"]["s"]
        if fact == "elements" and not all(is_primitive(item) for item in value.items):
            raise self._refuse(module, offset, "a member is not a primitive")
        if fact == "entries":
            pairs = value.entries
            if not all(is_primitive(v) for _, v in pairs) or (isinstance(value, JSMap) and not all(is_primitive(k) for k, _ in pairs)):
                raise self._refuse(module, offset, "an entry is not a primitive")

    @staticmethod
    def _model(value):
        if isinstance(value, JSArray):
            return {"kind": "array"}
        if isinstance(value, JSSet):
            return {"kind": "set"}
        if isinstance(value, JSMap):
            return {"kind": "map"}
        if isinstance(value, JSObject):
            return {"kind": "object", "keys": [key for key, _ in value.entries], "accessors": []}
        if isinstance(value, JSKeys):
            return {"kind": "object", "keys": list(value.keys), "accessors": sorted(value.accessors)}
        return {"kind": "primitive"}

    def expression(self, module, handle, statement, declarator=None):
        """The value of the literal expression at handle under the declared fact, evaluated as part of the
        module-scope statement whose index is statement (and of its declarator, for a variable
        declaration): the names it reads must be initialized before that statement or declarator. Like the
        declared fact, it does not check whether the value changes later."""
        frame = _Frame(module, statement, declarator, True, ())
        value = self._evaluate(module, self._tree(module, handle), frame, "any")
        return Value(value, "declared", module, handle, frozenset(frame.requires))

    def function(self, module, binding):
        """The Function a binding holds: a function declaration, or a variable with a function or arrow
        expression as its initializer. The binding must never be written."""
        key = (module, binding)
        if key in self._functions:
            return self._functions[key]
        declaring_module, declaring = self.declaring(module, binding)
        if (declaring_module, declaring) != key:
            # The recursive call caches the result under the declaring binding as well, so the next module
            # that imports the function does not make the helper read the declaring module again.
            result = self.function(declaring_module, declaring)
            self._functions[key] = result
            return result
        record = self._record(declaring_module, declaring)
        declarations = record["declarations"]
        if len(declarations) != 1:
            raise self._refuse(declaring_module, declarations[0]["s"], f"{record['name']} has several declarations")
        declaration = declarations[0]
        if record["kind"] == "function":
            handle = declaration["h"]
        elif record["kind"] in ("var", "let", "const") and "init" in declaration:
            initializer = self._unparenthesized(declaring_module, self._tree(declaring_module, declaration["init"]["h"]))
            if initializer["k"] not in ("FunctionExpression", "ArrowFunction"):
                raise self._refuse(declaring_module, declaration["s"], f"{record['name']} does not hold a function expression")
            handle = initializer["h"]
        else:
            raise self._refuse(declaring_module, declaration["s"], f"{record['name']} is not a function or a variable holding one")
        self._constancy(declaring_module, declaring, {"kind": "primitive"})
        result = Function(declaring_module, declaring, handle, self._tree(declaring_module, handle))
        self._functions[key] = result
        return result

    def unchanged_array(self, module, binding):
        """The built-in operations that the uses of an array rely on. It first checks that the binding is
        a module-scope variable initialized with an array literal, and that every use of it in the program
        leaves the array unchanged, as value() checks for the elements fact: the binding is never written,
        and the array is only read. The elements are not evaluated, so they may be values the evaluator
        does not read, such as objects with methods, and a write to a member of an element changes the
        element, not the array. Raises Unresolved listing every use that may change the array."""
        declaring_module, declaring = self.declaring(module, binding)
        record = self._record(declaring_module, declaring)
        declarations = record["declarations"]
        if record["kind"] not in ("var", "let", "const") or len(declarations) != 1 or "init" not in declarations[0]:
            raise self._refuse(declaring_module, declarations[0]["s"],
                               f"{record['name']} is not one variable declaration with an initializer")
        declaration = declarations[0]
        if not declaration.get("module"):
            raise self._refuse(declaring_module, declaration["s"], f"{record['name']} is not a variable of the module's scope")
        initializer = self._unparenthesized(declaring_module, self._tree(declaring_module, declaration["init"]["h"]))
        if initializer["k"] != "ArrayLiteralExpression":
            raise self._refuse(declaring_module, declaration["init"]["s"], f"{record['name']} is not initialized with an array literal")
        return frozenset(self._constancy(declaring_module, declaring, {"kind": "array"}))

    # -- constancy ---------------------------------------------------------------------------------

    def unfollowed_loads(self):
        """(module, offset, reason) of every import() or import.meta.require whose specifier could refer
        to a module of the build: a specifier that is neither a literal nor a template whose fixed head is
        a bare specifier (one that does not start with . or /), which refers to a package. A module with
        parse diagnostics counts as one such place, because its loads cannot be read."""
        if self._unfollowed_loads is None:
            sites = []
            for module in sorted(self.bundle.records):
                diagnostics = self.bundle.diagnostics(module)
                if diagnostics:
                    sites.append((module, diagnostics[0]["s"], "the module has parse diagnostics, so the modules it loads and "
                                  f"its uses of values cannot be read: {diagnostics[0]['message']}"))
                    continue
                for load in self.bundle.loads(module):
                    if load["k"] not in ("import()", "import.meta.require") or load.get("form") == "literal":
                        continue
                    head = load.get("head")
                    if load.get("form") == "template-head" and head and not head.startswith((".", "/")):
                        continue
                    sites.append((module, load["s"], f"{load['k']} with a specifier that is not a literal can load any module"))
            self._unfollowed_loads = sites
        return self._unfollowed_loads

    def _names(self, module):
        """The names a module exports, or an empty list when the module or a module it reaches through
        export * has parse diagnostics. unfollowed_loads already lists such a module, which refuses every
        container fact (elements, entries and keys)."""
        if module not in self._export_names:
            try:
                self._export_names[module] = sorted(self.bundle.export_names(module))
            except Unresolved:
                self._export_names[module] = []
        return self._export_names[module]

    def _holders(self, targets):
        """{module: the export names under which the module's namespace contains one of targets}, and the
        set of modules whose namespace contains such a namespace, at any depth."""
        holders, nested = {}, set()
        for module in sorted(self.bundle.records):
            for name in self._names(module):
                try:
                    resolved = self.bundle.export(module, name)
                except Unresolved:
                    continue
                if resolved in targets:
                    holders.setdefault(module, set()).add(name)
        changed = True
        while changed:
            changed = False
            for module in sorted(self.bundle.records):
                if module in nested:
                    continue
                for name in self._names(module):
                    try:
                        resolved = self.bundle.export(module, name)
                    except Unresolved:
                        continue
                    if resolved[0] == "namespace" and (resolved[1] in holders or resolved[1] in nested):
                        nested.add(module)
                        changed = True
                        break
        return holders, nested

    @staticmethod
    def _query_key(module, query):
        """The key under which a use query counts as done. For a parameter query it is the function and
        the parameter's position, because the first such query, the one with the fewest calls, classifies
        the same uses as any later one."""
        if query.get("mode") == "parameter":
            return module, "parameter", query["id"], query["position"]
        return module, json.dumps(query, sort_keys=True)

    def _constancy(self, module, binding, model):
        """The built-in operations the uses of the value rely on, after classifying every use; raises
        Unresolved listing every refused use. The uses are classified first in the binding's module, then
        in every module that imports one of the names the value is exported under, in the module that
        declares each imported function the value is passed to, and at every place where a module takes a
        namespace that contains the value. This repeats until the set of places stops growing. Queries run
        in order of the number of calls between the value's binding and the place, so that a parameter is
        classified at the call depth of the route with the fewest calls."""
        sites, requires = set(), set()
        if model["kind"] != "primitive":
            sites.update(self.unfollowed_loads())
        pending, order = [], itertools.count()

        def add(query_module, query):
            heapq.heappush(pending, (query.get("depth", 0), next(order), query_module, query))

        add(module, {"q": "uses", "id": binding, "model": model})
        done, targets = set(), set()
        while pending:
            while pending:
                _, _, query_module, query = heapq.heappop(pending)
                key = self._query_key(query_module, query)
                if key in done:
                    continue
                done.add(key)
                result = self.bundle.facts(query_module, [query])[0]
                requires.update(result["requires"])
                sites.update((query_module, item["s"], item["reason"]) for item in result["refused"])
                if model["kind"] == "primitive":
                    continue
                for call in result["calls"]:
                    try:
                        declaring_module, function = self.declaring(query_module, call["id"])
                    except Unresolved as error:
                        sites.add((query_module, call["s"], f"the value is passed to {call['name']}: {error.reason}"))
                        continue
                    add(declaring_module, {"q": "uses", "mode": "parameter", "id": function, "position": call["position"],
                                           "depth": call["depth"], "model": model})
                for export in result["exports"]:
                    try:
                        target = self.bundle.export(query_module, export["name"])
                    except Unresolved as error:
                        sites.add((query_module, export["s"], f"export {export['name']}: {error.reason}"))
                        continue
                    if target in targets:
                        continue
                    targets.add(target)
                    for importer, position, local in self.bundle.importers(target):
                        found = self.bundle.binding_at(importer, position, position + len(local.encode("utf-16-le")) // 2)
                        if "id" not in found:
                            sites.add((importer, position, f"the import of {local} is not a binding the helper resolves"))
                            continue
                        add(importer, {"q": "uses", "id": found["id"], "model": model})
            if model["kind"] == "primitive":
                break
            for importer, query in self._namespace_queries(targets, model, sites):
                if self._query_key(importer, query) not in done:
                    add(importer, query)
        if sites:
            raise self._refuse_all(sorted(sites))
        return requires

    def _namespace_queries(self, targets, model, sites):
        """The use queries for every place where a module takes a namespace that contains the value. Where
        the value is inside a namespace that another namespace contains, it adds a refusal to sites
        instead of a query."""
        holders, nested = self._holders(targets)
        queries = []
        for holder in sorted(set(holders) | nested):
            names = sorted(holders.get(holder, ()))
            for importer in sorted(self.bundle.records):
                # A module with parse diagnostics is listed by unfollowed_loads.
                if self.bundle.diagnostics(importer):
                    continue
                for load in self.bundle.loads(importer):
                    if load.get("t") != holder:
                        continue
                    if not ((load["k"] == "import" and "namespace" in load) or
                            load["k"] in ("export*as", "import()", "import.meta.require")):
                        continue
                    if holder in nested or load["k"] == "export*as":
                        sites.add((importer, load["s"],
                                   f"the namespace of module {holder} contains the value inside another namespace"))
                    elif load["k"] == "import":
                        name, position = load["namespace"]
                        found = self.bundle.binding_at(importer, position, position + len(name.encode("utf-16-le")) // 2)
                        queries.append((importer, {"q": "uses", "id": found["id"], "mode": "namespace", "names": names,
                                                   "model": model}))
                    else:
                        queries.append((importer, {"q": "loadUse", "handle": load["h"], "names": names, "model": model}))
        return queries
