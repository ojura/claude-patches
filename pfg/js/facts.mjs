// The syntax and binding facts of the JavaScript modules of a Bun executable, for pfg/bundle.py.
//
// pfg.bundle starts this helper with `node --expose-gc facts.mjs`, streams it the modules that
// bun_handler read out of the binary, and asks it for facts. The helper parses each module on its own,
// with the TypeScript parser vendored in ./vendor/typescript, and never runs module code.
//
// Protocol: UTF-8 JSON, one value per line, on stdin and stdout; stdout is reserved for this protocol.
//   - The first line the helper writes is {"hello": {"protocol": 1, "helper": "pfg-facts",
//     "revision": R, "node": ..., "typescript": ...}}, R being the number in pfg/REVISION as this
//     process read it.
//   - A request is {"id": n, "op": ..., ...}. Its reply is {"id": n, "ok": true, "result": ...} or
//     {"id": n, "ok": false, "error": {"kind": "request" | "internal", "message": ...}}: "request" when
//     the request names something absent or is malformed, "internal" for an exception in the helper.
//   - A line without an id is a notification and does not receive a reply. "module" is the one notification; an
//     error it causes is the reply to the next request.
//   - The helper exits when its stdin closes.
//
// Positions are UTF-16 offsets into a module's text, as TypeScript counts them. A node is named by its
// handle: the indices of the children on the path from the module's root, each index counting the
// children in the order ts.forEachChild visits them, joined with dots ("" is the module itself).
//
// Requests:
//   index {needles}               parse every module once and return a record of each (indexModule)
//   facts {module, queries}       answer queries about one module with a checker (factsBatch)
//   vocabulary                    {operations}: every built-in operation a use can require
//                                 (OPERATION_VOCABULARY)
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const ts = require('./vendor/typescript/lib/typescript.js');
const REVISION = Number(fs.readFileSync(path.join(HERE, '..', 'REVISION'), 'utf8').trim());
const PROTOCOL = 1;
const K = ts.SyntaxKind;

// The compiler options of the retained structural matcher, with every file a module: a Bun chunk is
// an ES module even when it does not contain an import or export. The parser applies the same rule through
// setExternalModuleIndicator; without it, a module with neither imports nor exports (2.1.304 has seven)
// would be parsed and bound as a script.
const OPTIONS = {
  allowJs: true, checkJs: false, noResolve: true, noLib: true, noEmit: true,
  target: ts.ScriptTarget.Latest, module: ts.ModuleKind.ESNext, moduleDetection: ts.ModuleDetectionKind.Force,
};
const PARSE_OPTIONS = {
  languageVersion: ts.ScriptTarget.Latest, setExternalModuleIndicator: ts.getSetExternalModuleIndicator(OPTIONS),
};

class RequestError extends Error {}

const modules = new Map();
let deferredError = null;

function fileName(index) {
  // TypeScript leaves a root file without a known extension out of the program, as the entrypoint
  // /$bunfs/root/cli would be; the module's Bun name stays in its record.
  return `/module/${index}.js`;
}

function moduleOf(index) {
  const module = modules.get(index);
  if (module === undefined) throw new RequestError(`module ${index} is absent from this bundle`);
  return module;
}

function parse(module) {
  return ts.createSourceFile(fileName(module.index), module.text, PARSE_OPTIONS, true, ts.ScriptKind.JS);
}

// Release points: a full collection after a large module and after every 8 MB of parsed text, so that
// parse trees which are already garbage do not pile up before V8 grows its heap. --expose-gc makes
// global.gc available; it does not set a memory limit.
let sinceCollection = 0;
function released(length) {
  sinceCollection += length;
  if (global.gc && (length > 1e6 || sinceCollection > 8e6)) {
    global.gc();
    sinceCollection = 0;
  }
}

function children(node) {
  const list = [];
  ts.forEachChild(node, (child) => {
    list.push(child);
  });
  return list;
}

function nodeAt(sourceFile, handle) {
  let node = sourceFile;
  if (handle === '') return node;
  for (const part of handle.split('.')) {
    const index = Number(part);
    const list = children(node);
    if (!/^\d+$/.test(part) || index >= list.length) throw new RequestError(`handle ${handle} does not identify a node`);
    node = list[index];
  }
  return node;
}

function handleOf(node) {
  const parts = [];
  for (let child = node; child.parent; child = child.parent) parts.push(children(child.parent).indexOf(child));
  return parts.reverse().join('.');
}

// SyntaxKind's names, without its range markers: K[kind] gives FirstStatement for VariableStatement and
// FirstLiteralToken for NumericLiteral, because those markers share the value.
const KIND_NAMES = new Map();
for (const [name, value] of Object.entries(K)) {
  if (typeof value === 'number' && !/^(First|Last)[A-Z]/.test(name) && !KIND_NAMES.has(value)) KIND_NAMES.set(value, name);
}
const kindName = (node) => KIND_NAMES.get(node.kind) ?? K[node.kind];
const start = (node, sourceFile) => node.getStart(sourceFile, false);

// The fields of a node that hold its children: every field TypeScript 5.9.3's forEachChild visits
// (forEachChildTable in the vendored typescript.js), with the ones JavaScript syntax uses first. A
// child's field is the first that holds it; fieldOf throws when every known field fails to hold it, so a node kind
// that a later TypeScript adds cannot go unnamed.
const FIELDS = [
  'expression', 'name', 'propertyName', 'questionDotToken', 'arguments', 'elements', 'properties', 'left',
  'operatorToken', 'right', 'operand', 'initializer', 'objectAssignmentInitializer', 'equalsToken', 'condition',
  'whenTrue', 'whenFalse', 'questionToken', 'colonToken', 'parameters', 'body', 'statements', 'declarationList',
  'declarations', 'argumentExpression', 'tag', 'template', 'head', 'templateSpans', 'literal', 'thenStatement',
  'elseStatement', 'incrementor', 'statement', 'awaitModifier', 'label', 'members', 'heritageClauses', 'types',
  'caseBlock', 'clauses', 'block', 'tryBlock', 'catchClause', 'finallyBlock', 'variableDeclaration', 'importClause',
  'namedBindings', 'exportClause', 'moduleSpecifier', 'attributes', 'modifiers', 'dotDotDotToken', 'asteriskToken',
  'exclamationToken', 'equalsGreaterThanToken', 'endOfFileToken', 'typeArguments', 'typeParameters', 'type',
  'argument', 'value', 'assertClause', 'moduleReference', 'tagName', 'openingElement', 'closingElement', 'children',
  'openingFragment', 'closingFragment', 'typeExpression', 'comment', 'fullName', 'typeParameter', 'class',
  'typeName', 'trueType', 'falseType', 'checkType', 'extendsType', 'tags', 'readonlyToken', 'qualifier',
  'parameterName', 'objectType', 'indexType', 'elementType', 'nameType', 'namespace', 'jsDocPropertyTags',
  'isNameFirst', 'exprName', 'default', 'assertsModifier', 'constraint',
];

function fieldOf(parent, child) {
  for (const field of FIELDS) {
    const value = parent[field];
    if (value === child) return { field };
    if (Array.isArray(value)) {
      const index = value.indexOf(child);
      if (index >= 0) return { field, index, count: value.length };
    }
  }
  throw new Error(`the known fields of ${kindName(parent)} do not include its ${kindName(child)} child`);
}

function isFunctionLike(node) {
  return ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node) ||
    ts.isMethodDeclaration(node) || ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node) ||
    ts.isConstructorDeclaration(node) || ts.isClassStaticBlockDeclaration(node);
}

// Whether a function reads its own `arguments`, in its parameters or its body. An arrow function inside
// it shares that object. A nested non-arrow function has its own, but its computed name and its
// decorators run in the enclosing function, as do a nested class's extends clause and the computed
// names of its members, which the walk reaches through the class.
function readsArguments(fn) {
  if (ts.isArrowFunction(fn) || !fn.body) return false;
  let found = false;
  const visit = (node) => {
    if (found) return;
    if (ts.isIdentifier(node) && node.text === 'arguments' && !isPropertyName(node)) {
      found = true;
    } else if (isFunctionLike(node) && !ts.isArrowFunction(node)) {
      if (node.name && ts.isComputedPropertyName(node.name)) visit(node.name);
      for (const modifier of node.modifiers ?? []) if (ts.isDecorator(modifier)) visit(modifier);
    } else {
      ts.forEachChild(node, visit);
    }
  };
  for (const parameter of fn.parameters) visit(parameter);
  visit(fn.body);
  return found;
}

// The cooked value of a string or template part as JavaScript computes it. A tagged template may hold
// an escape that is not valid, such as \u{ZZ}; its cooked value is undefined, and TypeScript marks the
// node with ContainsInvalidEscape while its text holds the escape's characters.
function cooked(node) {
  if ((node.templateFlags ?? 0) & ts.TokenFlags.ContainsInvalidEscape) return undefined;
  return node.text;
}

// var, let, const, using or await using.
function declarationListKind(list) {
  switch (list.flags & ts.NodeFlags.BlockScoped) {
    case ts.NodeFlags.Let: return 'let';
    case ts.NodeFlags.Const: return 'const';
    case ts.NodeFlags.Using: return 'using';
    case ts.NodeFlags.AwaitUsing: return 'await using';
    default: return 'var';
  }
}

// A literal primitive key: the JavaScript property name of a string, a number, true, false or null,
// and of a minus sign before a number; undefined for any other expression.
function literalKey(node) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return cooked(node);
  if (ts.isNumericLiteral(node)) return String(Number(node.text));
  if (node.kind === K.TrueKeyword) return 'true';
  if (node.kind === K.FalseKeyword) return 'false';
  if (node.kind === K.NullKeyword) return 'null';
  if (ts.isPrefixUnaryExpression(node) && node.operator === K.MinusToken && ts.isNumericLiteral(node.operand)) {
    return String(-Number(node.operand.text));
  }
  return undefined;
}

// The property name a member's name gives, or undefined when it is computed from an expression that is
// not a literal primitive.
function memberKey(name) {
  if (ts.isIdentifier(name) || ts.isPrivateIdentifier(name)) return name.text;
  if (ts.isComputedPropertyName(name)) return literalKey(name.expression);
  return literalKey(name);
}

function step(parent, child) {
  const { field, index, count } = fieldOf(parent, child);
  const result = { k: kindName(parent), f: field };
  if (index !== undefined) Object.assign(result, { i: index, n: count });
  if (ts.isPropertyAccessExpression(parent)) {
    result.name = parent.name.text;
    if (parent.questionDotToken) result.optional = true;
  } else if (ts.isElementAccessExpression(parent)) {
    const key = literalKey(parent.argumentExpression);
    result.key = key === undefined ? null : key;
    if (parent.questionDotToken) result.optional = true;
  } else if (ts.isCallExpression(parent) || ts.isNewExpression(parent)) {
    result.argc = parent.arguments ? parent.arguments.length : 0;
    if (ts.isCallExpression(parent) && parent.questionDotToken) result.optional = true;
  } else if (ts.isBinaryExpression(parent)) {
    result.op = ts.tokenToString(parent.operatorToken.kind);
  } else if (ts.isPrefixUnaryExpression(parent) || ts.isPostfixUnaryExpression(parent)) {
    result.op = ts.tokenToString(parent.operator);
  } else if (isFunctionLike(parent)) {
    result.params = parent.parameters ? parent.parameters.length : 0;
    result.arrow = ts.isArrowFunction(parent);
    result.args = readsArguments(parent);
  } else if (ts.isPropertyAssignment(parent) || ts.isShorthandPropertyAssignment(parent)) {
    const key = memberKey(parent.name);
    result.key = key === undefined ? null : key;
  }
  return result;
}

function isStatementLike(node) {
  return ts.isStatement(node) || ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node) ||
    ts.isImportDeclaration(node) || ts.isExportDeclaration(node) || ts.isExportAssignment(node);
}

// The steps from a node up to the statement that holds it, that statement included.
function chainOf(node) {
  const steps = [];
  for (let child = node, parent = node.parent; parent; child = parent, parent = parent.parent) {
    steps.push(step(parent, child));
    if (ts.isSourceFile(parent) || isStatementLike(parent)) break;
  }
  return steps;
}

// Whether an identifier names a property, a member, a label, an import attribute or a module's public
// name, which are not references to a binding of the module. The public names are the outside name of
// an import or export specifier, both names of a re-export (`export {a as b} from "m"` names exports of
// two modules), and the name of `export * as n from "m"`.
function isPropertyName(node) {
  const parent = node.parent;
  if (!parent) return false;
  if ((ts.isPropertyAccessExpression(parent) || ts.isPropertyAssignment(parent) || ts.isMethodDeclaration(parent) ||
       ts.isPropertyDeclaration(parent) || ts.isGetAccessorDeclaration(parent) || ts.isSetAccessorDeclaration(parent) ||
       ts.isMetaProperty(parent) || ts.isEnumMember(parent) || ts.isImportAttribute(parent) ||
       ts.isNamespaceExport(parent)) && parent.name === node) return true;
  if (ts.isBindingElement(parent) && parent.propertyName === node) return true;
  if (ts.isImportSpecifier(parent) && parent.propertyName === node) return true;
  if (ts.isExportSpecifier(parent) && (parent.parent.parent.moduleSpecifier || (parent.name === node && parent.propertyName))) {
    return true;
  }
  if ((ts.isLabeledStatement(parent) || ts.isBreakStatement(parent) || ts.isContinueStatement(parent)) &&
      parent.label === node) return true;
  return false;
}

// ---------------------------------------------------------------------------------------------------
// The index pass

const BINARY_ASSIGNMENTS = new Set([
  K.EqualsToken, K.PlusEqualsToken, K.MinusEqualsToken, K.AsteriskEqualsToken, K.AsteriskAsteriskEqualsToken,
  K.SlashEqualsToken, K.PercentEqualsToken, K.LessThanLessThanEqualsToken, K.GreaterThanGreaterThanEqualsToken,
  K.GreaterThanGreaterThanGreaterThanEqualsToken, K.AmpersandEqualsToken, K.BarEqualsToken, K.CaretEqualsToken,
  K.BarBarEqualsToken, K.AmpersandAmpersandEqualsToken, K.QuestionQuestionEqualsToken,
]);
// ---------------------------------------------------------------------------------------------------
// The built-in scan
//
// The catalog's tables rely on the built-in operations its model uses (new Set, Set.prototype.has,
// Array.prototype.includes and the like) keeping their standard behaviour. The scan starts at each
// built-in name and follows the use forms listed below. Standard behaviour remains an assumption;
// passed counts matched use forms, not a proof about code invoked by arguments, getters or callbacks.
// It sorts each place a value goes into:
//   passed        a call, construction, member read, comparison, typeof, test, discarded value or
//                 class heritage clause that matches the model;
//   writes        a write that replaces a built-in or one of its members (below);
//   globalWrites  a write to the global object under a key that is not a built-in's name;
//   references    the value is stored in a variable; the facts pass follows the variable's uses;
//   unfollowed    the value goes where the scan does not follow it: passed to a function, stored in an
//                 object or array, returned, thrown, yielded, spread, a member call the scan cannot name;
//   dynamic       code the scan cannot see into: a direct eval call, any other call of eval, a call or
//                 construction of the Function constructor or its async and generator kinds, a call of a
//                 .constructor the scan cannot identify, and eval or Function going where the scan does
//                 not follow them;
//   prototypeWrites  a write into an object reached by .constructor, .__proto__ or getPrototypeOf from
//                 an object the scan cannot identify.
// The writes: an assignment of any operator, ++, --, delete, or a destructuring or for-in/of target, at
// a built-in's name or at a member of a built-in value; a call of Object.defineProperty,
// defineProperties, assign or setPrototypeOf, or of Reflect.set, defineProperty, deleteProperty or
// setPrototypeOf, directly or through call or apply, with a built-in value as the target; a call of
// __defineGetter__ or __defineSetter__ on a built-in value, also through call or apply; a mutating
// array method (push, pop, shift, unshift, splice, sort, reverse, fill, copyWithin) called on
// Array.prototype. A write names its target as a path, such as Set.prototype.has, or
// %ArrayIteratorPrototype%.next for the prototype of an array's iterators; a key the scan cannot read is
// *, and a well-known symbol is @@iterator and the like.
//
// Values: a built-in's name (BUILTINS), the global object under any of its names (globalThis.Set is
// Set), and every static member read from a built-in value, normalized where the language fixes the
// result for a recognized constructor or prototype. Native error and typed-array prototypes retain
// their distinct parents. An arbitrary member's type is not inferred: Math.PI.constructor must not be
// called Function merely because Math is a built-in. An instance is a new object whose prototype the
// scan names: a literal ([], {}, a function or arrow expression, a regular expression, and a string,
// number, bigint or boolean, whose members are read from its wrapper's prototype), `new X()` with X a
// built-in constructor, and the iterator the iterator methods of Array, Set, Map and String return. A
// write into an instance's own member is not a write to a built-in; its constructor and __proto__ are
// followed from where it is created. An object literal with a spread, a computed key the scan cannot
// read, or its own constructor or __proto__ member, and a class expression, are not identified, since
// their members can differ from those of their prototype. A value the scan
// cannot identify is (object); its writes are prototypeWrites, calls of its constructor are dynamic
// code, and its other uses are counted only. Calls have special rules for definers, prototype readers,
// eval and function constructors, and for the mutating methods of Array.prototype, which write the
// members of their this value also through call, apply, bind and Reflect.apply. Template tags receive
// their implicit template object and their raw substitution values as arguments.
//
// An expression may hold several values (BuiltinScan.abstract): a conditional, a logical operator or a
// variable assigned more than once gives each, and a write or a definer through it reaches each. The
// facts pass follows a variable through its occurrences with every value it receives, and repeats its
// pass over the module until no variable gains a value, because a use classified early can give a
// variable a value that an earlier use needed. It is flow-insensitive: after `var t=Set;t=t.prototype`
// the variable holds Set, Set.prototype and the values derived from them again, up to the bounds below.
// A variable with a source outside the model (Facts.hasUnknownOrigins) may also hold a value the scan
// does not identify; its known values are still followed.
//
// The forms the scan does not identify are stated limits; an entry that comes from one has the limit's
// name in its limit field, and pfg/bundle.py states them (BUILTIN_SCAN_LIMITS):
//   instances      a write through an object whose prototype the scan does not name, such as an object a
//                  parameter or a variable holds;
//   definer-calls  a definer bound, passed or stored where the scan does not follow it, which may be called
//                  from there;
//   with           a built-in's name inside a with statement, or a built-in value as a with's object;
//   bounds         a value derived more than MAX_VALUE_PATH members deep, or past the
//                  MAX_VALUES_PER_VARIABLE values a variable can hold; a write through it is not recorded.

const GLOBAL_NAMES = new Set(['globalThis', 'global', 'self', 'window']);
// The global names of ECMAScript's built-ins, except undefined, NaN and Infinity, which cannot be changed.
const BUILTINS = new Set([
  'AggregateError', 'Array', 'ArrayBuffer', 'Atomics', 'BigInt', 'BigInt64Array', 'BigUint64Array', 'Boolean', 'DataView',
  'Date', 'Error', 'EvalError', 'FinalizationRegistry', 'Float16Array', 'Float32Array', 'Float64Array', 'Function',
  'Int8Array', 'Int16Array', 'Int32Array', 'Intl', 'Iterator', 'JSON', 'Map', 'Math', 'Number', 'Object', 'Promise', 'Proxy',
  'RangeError', 'ReferenceError', 'Reflect', 'RegExp', 'Set', 'SharedArrayBuffer', 'String', 'Symbol', 'SyntaxError',
  'TypeError', 'URIError', 'Uint8Array', 'Uint8ClampedArray', 'Uint16Array', 'Uint32Array', 'WeakMap', 'WeakRef', 'WeakSet',
  'decodeURI', 'decodeURIComponent', 'encodeURI', 'encodeURIComponent', 'escape', 'eval', 'isFinite', 'isNaN', 'parseFloat',
  'parseInt', 'unescape', ...GLOBAL_NAMES,
]);
const NAMESPACES = new Set(['Atomics', 'Intl', 'JSON', 'Math', 'Reflect']);
const NATIVE_ERRORS = new Set(['AggregateError', 'EvalError', 'RangeError', 'ReferenceError', 'SyntaxError', 'TypeError', 'URIError']);
const TYPED_ARRAYS = new Set(['BigInt64Array', 'BigUint64Array', 'Float16Array', 'Float32Array', 'Float64Array', 'Int8Array',
  'Int16Array', 'Int32Array', 'Uint8Array', 'Uint8ClampedArray', 'Uint16Array', 'Uint32Array']);
const CODE_RUNNERS = new Set(['eval', 'Function', 'AsyncFunction', 'GeneratorFunction', 'AsyncGeneratorFunction']);
// Definers with the target as their first argument, and the key as their second where there is one.
const STATIC_DEFINERS = new Map([
  ['Object.defineProperty', 'key'], ['Object.defineProperties', 'keys'], ['Object.assign', 'keys'],
  ['Object.setPrototypeOf', 'prototype'], ['Reflect.set', 'key'], ['Reflect.defineProperty', 'key'],
  ['Reflect.deleteProperty', 'key'], ['Reflect.setPrototypeOf', 'prototype'],
]);
const RECEIVER_DEFINERS = new Set(['__defineGetter__', '__defineSetter__']);
const ARRAY_MUTATORS = new Set(['push', 'pop', 'shift', 'unshift', 'splice', 'sort', 'reverse', 'fill', 'copyWithin']);
const PROTOTYPE_READERS = new Set(['Object.getPrototypeOf', 'Reflect.getPrototypeOf']);
const WELL_KNOWN_SYMBOLS = new Set(['asyncIterator', 'hasInstance', 'isConcatSpreadable', 'iterator', 'match', 'matchAll',
  'replace', 'search', 'species', 'split', 'toPrimitive', 'toStringTag', 'unscopables', 'dispose', 'asyncDispose']);
const HAS_PROTOTYPE = new Set([
  'AggregateError', 'Array', 'ArrayBuffer', 'BigInt', 'BigInt64Array', 'BigUint64Array', 'Boolean', 'DataView', 'Date',
  'Error', 'EvalError', 'FinalizationRegistry', 'Float16Array', 'Float32Array', 'Float64Array', 'Function', 'Int8Array',
  'Int16Array', 'Int32Array', 'Iterator', 'Map', 'Number', 'Object', 'Promise', 'RangeError', 'ReferenceError', 'RegExp',
  'Set', 'SharedArrayBuffer', 'String', 'Symbol', 'SyntaxError', 'TypeError', 'URIError', 'Uint8Array',
  'Uint8ClampedArray', 'Uint16Array', 'Uint32Array', 'WeakMap', 'WeakRef', 'WeakSet', '%TypedArray%',
  'AsyncFunction', 'GeneratorFunction', 'AsyncGeneratorFunction',
]);
const UNKNOWN = '(object)';
// The bounds on what the facts pass follows into one variable: how many members deep a derived value
// may be, and how many values a variable may hold.
const MAX_VALUE_PATH = 6;
const MAX_VALUES_PER_VARIABLE = 16;

// A value with instance set stands for a new object whose prototype is the value's path, as
// Set.prototype for `new Set()` (see "Values" above); its name ends in #instance.
const valueName = (value) => [value.root, ...value.path].join('.') + (value.instance ? '#instance' : '');
// The methods that return an iterator, and the intrinsic prototype of that iterator.
const ITERATOR_METHODS = new Map([
  ...['@@iterator', 'values', 'keys', 'entries'].flatMap((name) => [
    [`Array.prototype.${name}`, '%ArrayIteratorPrototype%'], [`Set.prototype.${name}`, '%SetIteratorPrototype%'],
    [`Map.prototype.${name}`, '%MapIteratorPrototype%']]),
  ['String.prototype.@@iterator', '%StringIteratorPrototype%'],
  ...['@@iterator', 'values', 'keys', 'entries'].map((name) => [`%TypedArray%.prototype.${name}`, '%ArrayIteratorPrototype%']),
]);
const ITERATOR_PROTOTYPES = new Set(ITERATOR_METHODS.values());

// Whether a value is one of the iterator prototypes above: an ordinary object whose prototype is
// Iterator.prototype, not a constructor.
const isIteratorPrototype = (value) => value.path.length === 0 && ITERATOR_PROTOTYPES.has(value.root);

// The intrinsic prototype of the iterator a call of the value returns, or undefined. A method called
// through call or apply returns the same kind of iterator.
function iteratorPrototypeOf(callee) {
  const path = callee.instance ? undefined : [callee.root, ...callee.path];
  if (!path) return undefined;
  if (['call', 'apply'].includes(path.at(-1))) path.pop();
  return ITERATOR_METHODS.get(path.join('.'));
}

// What an expression may evaluate to (BuiltinScan.abstract): the built-in values the scan identifies,
// and whether it may also be a value the scan does not identify. Empty values without unknown means the
// expression holds no built-in value the scan knows of: it reads a variable that has not received one
// yet in this pass, or one whose sources the scan identifies and which holds none, as after `var x;`.
const UNIDENTIFIED = Object.freeze({ values: [], unknown: true });

// Tells values apart, including how a call would receive this: a method read through an instance is
// called with the instance as this.
const valueKey = (value) => `${valueName(value)}${value.receiver ? ' through an instance' : ''}${value.foreign ? ' (global key)' : ''}`;

function distinct(values) {
  return [...new Map(values.map((value) => [valueKey(value), value])).values()];
}

function union(...parts) {
  return { values: distinct(parts.flatMap((part) => part.values)), unknown: parts.some((part) => part.unknown) };
}

// Whether a built-in value is an object, a function or a symbol, which a test always finds true: a
// built-in's name, an intrinsic prototype, the prototype object of a constructor, a well-known symbol,
// or an object instance. A primitive's instance can be false, and so can a member the scan does not
// type, such as Array.prototype.length.
function isTruthy(value) {
  if (value.foreign || value.root === UNKNOWN) return false;
  if (value.instance) return !['String', 'Number', 'BigInt', 'Boolean'].includes(value.root);
  if (value.path.length === 0) return true;
  if (value.path.length === 1 && value.path[0] === 'prototype') return HAS_PROTOTYPE.has(value.root);
  return value.root === 'Symbol' && value.path.length === 1 && WELL_KNOWN_SYMBOLS.has(value.path[0]);
}

// Whether value is a mutating method of Array.prototype, such as Array.prototype.push.
function isArrayMutator(value) {
  return !value.instance && !value.foreign && value.root === 'Array' && value.path.length === 2 && value.path[0] === 'prototype' &&
    ARRAY_MUTATORS.has(value.path[1]);
}

// The argument position at which a call of fn passes a definer, or a mutating method of Array.prototype
// called or bound through call, apply or bind, its target, or -1.
function definerTargetPosition(fn) {
  if (STATIC_DEFINERS.has(valueName(fn))) return 0;
  const last = fn.path.at(-1);
  const inner = fn.path.length ? valueName({ root: fn.root, path: fn.path.slice(0, -1) }) : '';
  if (last === 'call' && STATIC_DEFINERS.has(inner)) return 1;
  if ((last === 'call' || last === 'apply') && RECEIVER_DEFINERS.has(fn.path.at(-2))) return 0;
  if (['call', 'apply', 'bind'].includes(last) && isArrayMutator({ root: fn.root, path: fn.path.slice(0, -1) })) return 0;
  return -1;
}

// The value of reading key from value, normalized where the language fixes the result.
function memberValue(value, key) {
  if (value.instance) {
    const prototype = { root: value.root, path: value.path };
    if (key === '__proto__') return prototype;
    // A member read through an instance keeps the instance as the receiver a call would get.
    return { ...memberValue(prototype, key), receiver: 'instance' };
  }
  const { root, path } = value;
  if (root === 'globalThis' && path.length === 0) {
    if (BUILTINS.has(key) && !GLOBAL_NAMES.has(key)) return { root: key, path: [] };
    if (GLOBAL_NAMES.has(key)) return value;
    if (key === '*') return { root, path: ['*'] };
    if (key === 'constructor' || key === '__proto__') return { root: UNKNOWN, path: [key] };
    return { root, path: [key], foreign: true };
  }
  if (value.foreign) return { root, path: [...path, key], foreign: true };
  if (root === UNKNOWN) return { root, path: [...path, key] };
  if (key === 'constructor') {
    if (path.length === 1 && path[0] === 'prototype' && HAS_PROTOTYPE.has(root)) return { root, path: [] };
    // Iterator.prototype.constructor, which the iterator prototypes inherit, is an accessor.
    if (isIteratorPrototype(value)) return { root: UNKNOWN, path: ['constructor'] };
    if (path.length === 0 && NAMESPACES.has(root)) return { root: 'Object', path: [] };
    if (path.length === 0) return { root: 'Function', path: [] };
    // A member can be a primitive, a function or another object. Without its type, its constructor is
    // unknown; Math.PI.constructor, for example, is Number, not Function.
    return { root: UNKNOWN, path: ['constructor'] };
  }
  if (key === '__proto__') return prototypeOf(value);
  return { root, path: [...path, key] };
}

// The prototype of a built-in value, where the language fixes it.
function prototypeOf(value) {
  if (value.instance) return { root: value.root, path: value.path };
  const { root, path } = value;
  if (root === UNKNOWN || value.foreign || (root === 'globalThis' && path.length === 0)) return { root: UNKNOWN, path: ['__proto__'] };
  if (path.length === 1 && path[0] === 'prototype') {
    if (root === 'Object') return { root: UNKNOWN, path: ['null'] };
    if (NATIVE_ERRORS.has(root)) return { root: 'Error', path: ['prototype'] };
    if (TYPED_ARRAYS.has(root)) return { root: '%TypedArray%', path: ['prototype'] };
    if (['AsyncFunction', 'GeneratorFunction', 'AsyncGeneratorFunction'].includes(root)) return { root: 'Function', path: ['prototype'] };
    if (!HAS_PROTOTYPE.has(root)) return { root: UNKNOWN, path: ['__proto__'] };
    return { root: 'Object', path: ['prototype'] };
  }
  if (path.length === 0 && NAMESPACES.has(root)) return { root: 'Object', path: ['prototype'] };
  if (isIteratorPrototype(value)) return { root: 'Iterator', path: ['prototype'] };
  if (path.length === 0 && NATIVE_ERRORS.has(root)) return { root: 'Error', path: [] };
  if (path.length === 0 && TYPED_ARRAYS.has(root)) return { root: '%TypedArray%', path: [] };
  if (path.length === 0 && ['AsyncFunction', 'GeneratorFunction', 'AsyncGeneratorFunction'].includes(root)) return { root: 'Function', path: [] };
  if (path.length === 0) return { root: 'Function', path: ['prototype'] };
  return { root: UNKNOWN, path: ['__proto__'] };
}

// The constructor of a literal's kind, for literal.constructor, literal.__proto__ and getPrototypeOf.
function literalConstructor(node) {
  let current = node;
  while (ts.isParenthesizedExpression(current)) current = current.expression;
  if (ts.isArrayLiteralExpression(current)) return 'Array';
  if (ts.isObjectLiteralExpression(current)) {
    // An object literal can declare its own constructor or prototype. Those members are handled from
    // their initializer where possible; an unknown key or spread prevents assuming the inherited value.
    if (current.properties.some((property) => ts.isSpreadAssignment(property) ||
        memberKey(property.name) === undefined || ['constructor', '__proto__'].includes(memberKey(property.name)))) return undefined;
    return 'Object';
  }
  if (ts.isStringLiteral(current) || ts.isNoSubstitutionTemplateLiteral(current) || ts.isTemplateExpression(current)) return 'String';
  if (ts.isNumericLiteral(current)) return 'Number';
  if (ts.isBigIntLiteral(current)) return 'BigInt';
  if (ts.isRegularExpressionLiteral(current)) return 'RegExp';
  if (current.kind === K.TrueKeyword || current.kind === K.FalseKeyword) return 'Boolean';
  if (ts.isFunctionExpression(current) || ts.isArrowFunction(current)) {
    const isAsync = (ts.getModifiers(current) ?? []).some((m) => m.kind === K.AsyncKeyword);
    const generator = Boolean(current.asteriskToken);
    return generator ? (isAsync ? 'AsyncGeneratorFunction' : 'GeneratorFunction') : isAsync ? 'AsyncFunction' : 'Function';
  }
  // A class expression can have heritage, static fields or static blocks that change these members.
  return undefined;
}


function isWriteTarget(expression) {
  const outer = expression.parent;
  if (!outer) return false;
  if (ts.isBinaryExpression(outer) && outer.left === expression && BINARY_ASSIGNMENTS.has(outer.operatorToken.kind)) return true;
  if ((ts.isPrefixUnaryExpression(outer) || ts.isPostfixUnaryExpression(outer)) &&
      (outer.operator === K.PlusPlusToken || outer.operator === K.MinusMinusToken)) return true;
  if (ts.isDeleteExpression(outer)) return true;
  if ((ts.isForInStatement(outer) || ts.isForOfStatement(outer)) && outer.initializer === expression) return true;
  return isDestructuringTarget(expression);
}

function writeHow(expression) {
  const outer = expression.parent;
  if (ts.isBinaryExpression(outer) && outer.left === expression) return ts.tokenToString(outer.operatorToken.kind);
  if (ts.isPrefixUnaryExpression(outer) || ts.isPostfixUnaryExpression(outer)) return ts.tokenToString(outer.operator);
  if (ts.isDeleteExpression(outer)) return 'delete';
  if (ts.isForInStatement(outer) || ts.isForOfStatement(outer)) return 'for-in/of target';
  return 'destructuring target';
}

class BuiltinScan {
  // freeValue(identifier) gives the value a free built-in's name holds, else null. isFree(identifier)
  // tells whether a name is a free name, not a variable of the module. In the facts pass,
  // variableValue(identifier) gives what a variable of the module may hold (see abstract), and null for
  // a free name; in the index pass every name the module declares is a value the scan does not identify.
  constructor(sourceFile, freeValue, handleOf, isFree) {
    this.sourceFile = sourceFile;
    this.freeValue = freeValue;
    this.handleOf = handleOf;
    this.isFree = isFree;
    this.result = { passed: {}, writes: [], globalWrites: [], registryWrites: [], prototypeWrites: [], references: [],
      unfollowed: [], dynamic: [], unknownKeys: [], exports: [], unidentified: 0 };
    // The values the references received, in the order of result.references.
    this.referenceValues = [];
  }

  where(node) {
    return { s: start(node, this.sourceFile), e: node.end, h: this.handleOf(node) };
  }

  // A key is literal, a registry symbol, a well-known symbol, a string drawn from a proved finite
  // list, or unknown. requires lists the built-in operations a proof uses. The summary invalidates
  // that proof if the scan detects a write to a dependency.
  keyInfo(expression) {
    if (!expression) return { keys: ['*'], requires: [] };
    const literal = literalKey(expression);
    if (literal !== undefined) return { keys: [literal], requires: [] };
    if (this.registry(expression)) return { keys: ['@registry'], requires: ['Symbol.for'] };
    let current = expression;
    while (ts.isParenthesizedExpression(current)) current = current.expression;
    if (ts.isPropertyAccessExpression(current) && ts.isIdentifier(current.expression) && current.expression.text === 'Symbol' &&
        this.isFree(current.expression) && WELL_KNOWN_SYMBOLS.has(current.name.text)) {
      return { keys: [`@@${current.name.text}`], requires: [`Symbol.${current.name.text}`] };
    }
    return this.finiteKeys?.(current) ?? { keys: ['*'], requires: [] };
  }

  keyOf(expression) {
    const { keys } = this.keyInfo(expression);
    return keys.length === 1 ? keys[0] : '*';
  }

  // Whether an expression is a call of the built-in Symbol.for, or, in the facts pass, a variable that
  // holds one and is never written (registryVariable).
  registry(expression) {
    let current = expression;
    while (ts.isParenthesizedExpression(current)) current = current.expression;
    if (ts.isCallExpression(current) && !current.questionDotToken) {
      let callee = current.expression;
      while (ts.isParenthesizedExpression(callee)) callee = callee.expression;
      return ts.isPropertyAccessExpression(callee) && callee.name.text === 'for' && ts.isIdentifier(callee.expression) &&
        callee.expression.text === 'Symbol' && this.isFree(callee.expression);
    }
    return ts.isIdentifier(current) && Boolean(this.registryVariable?.(current));
  }

  pass(value, how) {
    const name = valueName(value);
    if (value.root === UNKNOWN) {
      this.result.unidentified += 1;
      return;
    }
    if (value.foreign) return;
    this.result.passed[name] = (this.result.passed[name] ?? 0) + 1;
    void how;
  }

  // What an expression may evaluate to: the built-in values the scan identifies, and whether it may also
  // be a value the scan does not identify (unknown). Every possible value counts, so that a write through
  // a variable or a conditional reaches each built-in it may hold. Literals, `new X()` of a built-in
  // constructor and the iterators of the iterator methods are instances; the && operator passes on its
  // left operand only when that can be false, and || and ?? pass on their right operand unless the left
  // one names an always truthy built-in directly (leftDecides). With onlyValues, the result's unknown is
  // not computed where it cannot become a value: whether a variable has a source outside the model costs
  // a walk over its occurrences.
  abstract(expression, onlyValues = false) {
    let current = expression;
    while (ts.isParenthesizedExpression(current) || ts.isNonNullExpression?.(current) || ts.isAwaitExpression(current)) {
      current = current.expression;
    }
    if (ts.isIdentifier(current)) return isPropertyName(current) ? UNIDENTIFIED : this.identifierValue(current, onlyValues);
    if (ts.isPropertyAccessExpression(current) || ts.isElementAccessExpression(current)) return this.memberRead(current, onlyValues);
    if (ts.isCallExpression(current)) return this.callResult(current, onlyValues);
    if (ts.isNewExpression(current)) return this.construction(current, onlyValues);
    if (ts.isBinaryExpression(current)) {
      const op = current.operatorToken.kind;
      if (op === K.CommaToken || op === K.EqualsToken) return this.abstract(current.right, onlyValues);
      if (op === K.AmpersandAmpersandToken) {
        const left = this.abstract(current.left, onlyValues);
        return union({ values: left.values.filter((value) => !isTruthy(value)), unknown: left.unknown },
          this.abstract(current.right, onlyValues));
      }
      if (op === K.BarBarToken || op === K.QuestionQuestionToken) {
        // The right operand runs only when the left one can be falsy (||) or null or undefined (??).
        const left = this.abstract(current.left, onlyValues);
        if (this.leftDecides(current.left)) return left;
        return union(left, this.abstract(current.right, onlyValues));
      }
      return UNIDENTIFIED;
    }
    if (ts.isConditionalExpression(current)) {
      return union(this.abstract(current.whenTrue, onlyValues), this.abstract(current.whenFalse, onlyValues));
    }
    const literal = literalConstructor(current);
    return literal ? { values: [{ root: literal, path: ['prototype'], instance: true }], unknown: false } : UNIDENTIFIED;
  }

  identifierValue(identifier, onlyValues = false) {
    const variable = this.variableValue?.(identifier, onlyValues);
    if (variable) return variable;
    const value = this.freeValue(identifier);
    return value ? { values: [value], unknown: false } : UNIDENTIFIED;
  }

  // A member read. An object literal's own constructor or __proto__ member gives its initializer's value
  // when no later member can replace it; those keys of an object the scan does not identify give an
  // (object) value, whose writes are prototypeWrites.
  memberRead(current, onlyValues = false) {
    const key = ts.isPropertyAccessExpression(current) ? current.name.text : this.keyOf(current.argumentExpression);
    const special = key === 'constructor' || key === '__proto__';
    let object = current.expression;
    while (ts.isParenthesizedExpression(object)) object = object.expression;
    if (special && ts.isObjectLiteralExpression(object)) {
      for (const property of [...object.properties].reverse()) {
        if (ts.isSpreadAssignment(property) || memberKey(property.name) === undefined) break;
        if (memberKey(property.name) !== key) continue;
        if (ts.isPropertyAssignment(property)) {
          const value = this.abstract(property.initializer, onlyValues);
          if (value.values.length) return value;
        }
        break;
      }
    }
    // Those keys of an object the scan does not identify are values of their own.
    const base = this.abstract(current.expression, onlyValues && !special);
    const values = base.values.map((value) => memberValue(value, key));
    if (base.unknown && special) values.push({ root: UNKNOWN, path: [key] });
    return { values: distinct(values), unknown: base.unknown && !special };
  }

  // A call's result: the prototypes a prototype reader returns, and the iterators of the iterator methods.
  callResult(call, onlyValues = false) {
    const callee = this.abstract(call.expression, onlyValues);
    const values = [];
    let unknown = callee.unknown;
    for (const fn of callee.values) {
      const iterator = iteratorPrototypeOf(fn);
      if (PROTOTYPE_READERS.has(valueName(fn)) && call.arguments.length) values.push(...this.prototypesOf(call.arguments[0]));
      else if (iterator) values.push({ root: iterator, path: [], instance: true });
      else unknown = true;
    }
    return { values: distinct(values), unknown };
  }

  // `new X(...)` of a built-in constructor X is an instance of X.prototype.
  construction(expression, onlyValues = false) {
    const callee = this.abstract(expression.expression, onlyValues);
    const values = [];
    let unknown = callee.unknown;
    for (const fn of callee.values) {
      if (!fn.instance && !fn.foreign && fn.path.length === 0 && HAS_PROTOTYPE.has(fn.root)) {
        values.push({ root: fn.root, path: ['prototype'], instance: true });
      } else {
        unknown = true;
      }
    }
    return { values: distinct(values), unknown };
  }

  // The prototypes of the objects an expression may evaluate to. An object the scan does not identify
  // has the prototype (object).__proto__.
  prototypesOf(expression) {
    const object = this.abstract(expression);
    const values = object.values.map(prototypeOf);
    if (object.unknown) values.push({ root: UNKNOWN, path: ['__proto__'] });
    return distinct(values);
  }

  // The built-in values an expression may hold, for a definer's target.
  possibleValues(expression) {
    return this.abstract(expression, true).values;
  }

  // The value of node when it names a built-in directly: a free built-in's name, or a chain of member
  // reads without ?. from one; else null.
  builtinReference(node) {
    let current = node;
    while (ts.isParenthesizedExpression(current)) current = current.expression;
    if (ts.isIdentifier(current)) return this.isFree(current) ? this.freeValue(current) : null;
    if (ts.isPropertyAccessExpression(current) && !current.questionDotToken) {
      const base = this.builtinReference(current.expression);
      return base ? memberValue(base, current.name.text) : null;
    }
    return null;
  }

  // Whether the left operand of || or ?? always gives the result, so the right one never runs: it names
  // a built-in directly, and that built-in is always truthy (isTruthy), which also rules out null and
  // undefined. A variable never counts, since the abstract values have no element for undefined or
  // null: `var w;if(c)w=Set;w||Map` gives Map when c is false.
  leftDecides(left) {
    const value = this.builtinReference(left);
    return value !== null && isTruthy(value);
  }

  // A mutating method of Array.prototype runs with the value of thisNode as this, and may write any of
  // its members, as Array.prototype.push(...) writes Array.prototype's.
  mutate(node, thisNode, how) {
    for (const target of thisNode ? this.possibleValues(thisNode) : []) this.writeKeys(node, target, ['*'], how);
  }

  write(node, target, how, proof = {}) {
    if (target.instance) return;
    const name = valueName(target);
    const entry = { ...this.where(node), target: name, how, ...proof };
    if (target.path.at(-1) === '@registry') {
      this.result.registryWrites.push(entry);
    } else if (target.root === UNKNOWN) {
      this.result.prototypeWrites.push({ ...entry, limit: 'instances' });
    } else if (target.foreign) {
      this.result.globalWrites.push({ ...entry, key: target.path[0] });
    } else if (target.root === 'globalThis' && target.path.includes('*')) {
      this.result.unknownKeys.push(entry);
    } else {
      this.result.writes.push(entry);
    }
  }

  // Writes each possible key. A proof records the fallback target in case a built-in operation it
  // depends on is written elsewhere in the program.
  writeKeys(node, value, keys, how, requires = []) {
    if (value.instance) return;
    const proof = requires.length ? { keyRequires: requires, keyFallback: valueName(value) + '.*' } : {};
    for (const key of keys) {
      const target = { root: value.root, path: [...value.path, key], foreign: value.foreign };
      if (value.root === 'globalThis' && value.path.length === 0 && !value.foreign) {
        if (key === '*' || key === '@registry') this.write(node, { root: 'globalThis', path: [key] }, how, proof);
        else if (BUILTINS.has(key)) this.write(node, { root: key, path: [] }, how, proof);
        else this.write(node, { root: 'globalThis', path: [key], foreign: true }, how, proof);
      } else {
        this.write(node, target, how, proof);
      }
    }
  }

  // The keys a definer writes, and the dependencies of their proofs. Object literal sources can have
  // several static keys. A spread or an expression the key model does not cover gives an unknown key.
  definerKeys(kind, args) {
    if (kind === 'prototype') return { keys: ['__proto__'], requires: [] };
    if (kind === 'key') return this.keyInfo(args[1]);
    const keys = [];
    const requires = [];
    for (const source of args.slice(1)) {
      let current = source;
      while (current && ts.isParenthesizedExpression(current)) current = current.expression;
      if (!current || !ts.isObjectLiteralExpression(current)) return { keys: ['*'], requires: [] };
      for (const property of current.properties) {
        if (ts.isSpreadAssignment(property)) return { keys: ['*'], requires: [] };
        const literal = memberKey(property.name);
        const info = literal !== undefined ? { keys: [literal], requires: [] }
          : ts.isComputedPropertyName(property.name) ? this.keyInfo(property.name.expression) : { keys: ['*'], requires: [] };
        keys.push(...info.keys);
        requires.push(...info.requires);
      }
    }
    return { keys: [...new Set(keys)], requires: [...new Set(requires)] };
  }

  // The arguments of a call, construction or template tag. A tag receives its implicit template
  // object first, represented by null here since it is a fresh frozen array, then each substitution
  // unchanged. In particular, Object.defineProperty.call`${Set.prototype}${"has"}${descriptor}`
  // gives defineProperty the target, key and descriptor after call removes its this argument.
  argumentsOf(call) {
    if (ts.isTaggedTemplateExpression(call)) {
      return [null, ...(ts.isTemplateExpression(call.template) ? call.template.templateSpans.map((span) => span.expression) : [])];
    }
    return [...(call.arguments ?? [])];
  }

  // A call of value at call (whose callee is node), with its arguments.
  call(node, call, value) {
    const name = valueName(value);
    const args = this.argumentsOf(call);
    if (value.root === 'eval' && value.path.length === 0) {
      // A call whose callee is the name eval, also in parentheses, is a direct eval; eval reached any
      // other way, through (0,eval), globalThis.eval or ?.(), is indirect.
      this.result.dynamic.push({ ...this.where(call), kind: isDirectEval(call) ? 'direct eval' : 'indirect eval' });
      return;
    }
    if (CODE_RUNNERS.has(value.root) && value.path.length === 0) {
      this.result.dynamic.push({ ...this.where(call), kind: `${value.root} constructor` });
      return;
    }
    const last = value.path.at(-1);
    // The receiver: the value the member was read from, or the instance it was read through.
    const target = !value.path.length ? null : value.receiver === 'instance'
      ? { root: value.root, path: value.path.slice(0, -1), instance: true }
      : { root: value.root, path: value.path.slice(0, -1), foreign: value.foreign };
    if (target && (last === 'call' || last === 'apply' || last === 'bind')) {
      const inner = valueName(target);
      if (CODE_RUNNERS.has(target.root) && target.path.length === 0) {
        this.result.dynamic.push({ ...this.where(call), kind: `${target.root === 'eval' ? 'indirect eval' : `${target.root} constructor`}` +
          ` through ${last}` });
        return;
      }
      const receiver = target.path.at(-1);
      if (STATIC_DEFINERS.has(inner) || RECEIVER_DEFINERS.has(receiver)) {
        if (last === 'bind') {
          this.result.unfollowed.push({ ...this.where(call), value: inner, reason: 'a definer is bound, and the bound function is not followed',
            limit: 'definer-calls' });
          return;
        }
        let definerArgs;
        if (last === 'call') definerArgs = STATIC_DEFINERS.has(inner) ? args.slice(1) : args;
        else {
          const list = args[1];
          definerArgs = STATIC_DEFINERS.has(inner) ? (list && ts.isArrayLiteralExpression(list) ? [...list.elements] : null) : [args[0]];
          if (STATIC_DEFINERS.has(inner) && definerArgs === null) {
            this.result.unfollowed.push({ ...this.where(call), value: inner, reason: 'a definer is applied to arguments the scan cannot read' });
            return;
          }
        }
        this.define(call, inner, definerArgs, RECEIVER_DEFINERS.has(receiver) ? { receiver: true, args, apply: last === 'apply' } : null);
        return;
      }
      if (isArrayMutator(target)) {
        // M.call(T, ...) and M.apply(T, list) run M with T as this, as T.M(...) does, and the function
        // M.bind(T) returns does whenever it is called.
        this.mutate(call, args[0], `${receiver} through ${last}`);
        return;
      }
    }
    if (name === 'Reflect.apply' && args[0]) {
      // Reflect.apply(M, T, list) runs M with T as this.
      const mutators = this.possibleValues(args[0]).filter(isArrayMutator);
      for (const mutator of mutators) this.mutate(call, args[1], `${mutator.path[1]} through Reflect.apply`);
      if (mutators.length) return;
    }
    if (STATIC_DEFINERS.has(name)) {
      this.define(call, name, args, null);
      return;
    }
    if (target && RECEIVER_DEFINERS.has(last)) {
      // value is X.__defineGetter__: it defines key args[0] on X.
      const info = this.keyInfo(args[0]);
      this.writeKeys(call, target, info.keys, last, info.requires);
      return;
    }
    if (target && ARRAY_MUTATORS.has(last) && valueName(target) === 'Array.prototype') {
      this.write(call, { root: 'Array', path: ['prototype', '*'] }, last);
      return;
    }
    if (value.root === UNKNOWN && last === 'constructor') {
      this.result.dynamic.push({ ...this.where(call), kind: 'a constructor the scan cannot identify, which may be the Function constructor' });
      return;
    }
    if (last === '*' && target && (valueName(target) === 'globalThis' || ['Object', 'Reflect'].includes(valueName(target)))) {
      this.result.unfollowed.push({ ...this.where(call), value: name,
        reason: `a member of ${valueName(target)} the scan cannot name is called, which may be eval, Function or a definer` });
      return;
    }
    this.pass(value, 'call');
  }

  // A definer called with definerArgs, its target first; receiver for __defineGetter__ through call or
  // apply, whose target is the this argument.
  define(call, name, definerArgs, receiver) {
    if (receiver) {
      const targets = receiver.args[0] ? this.possibleValues(receiver.args[0]) : [];
      const list = receiver.apply ? receiver.args[1] : null;
      const keyNode = receiver.apply ? (list && ts.isArrayLiteralExpression(list) ? list.elements[0] : null) : receiver.args[1];
      const info = this.keyInfo(keyNode);
      if (targets.length) for (const target of targets) this.writeKeys(call, target, info.keys, name, info.requires);
      else this.pass({ root: 'Object', path: ['prototype', name.split('.').at(-1)] }, 'definer');
      return;
    }
    const targets = definerArgs[0] ? this.possibleValues(definerArgs[0]) : [];
    if (!targets.length) {
      this.pass({ root: name.split('.')[0], path: [name.split('.')[1]] }, 'definer on an object the scan does not identify as a built-in');
      return;
    }
    const info = this.definerKeys(STATIC_DEFINERS.get(name), definerArgs);
    for (const target of targets) this.writeKeys(call, target, info.keys, name, info.requires);
  }

  // Classifies the value of node, which is value, by where it goes. variable is true when node is a
  // variable that holds the value: writing it changes the variable, not the built-in.
  follow(node, value, variable = false) {
    let current = node;
    for (;;) {
      if (isWriteTarget(current)) {
        if (current === node && variable) this.pass(value, 'the variable is written');
        else this.write(current, value, writeHow(current));
        return;
      }
      const outer = current.parent;
      if (!outer) return this.pass(value, 'end');
      if (ts.isParenthesizedExpression(outer) || ts.isNonNullExpression?.(outer) || ts.isAwaitExpression(outer)) {
        current = outer;
        continue;
      }
      if ((ts.isPropertyAccessExpression(outer) || ts.isElementAccessExpression(outer)) && outer.expression === current) {
        const key = ts.isPropertyAccessExpression(outer) ? outer.name.text : this.keyOf(outer.argumentExpression);
        if (isWriteTarget(outer)) {
          const info = ts.isPropertyAccessExpression(outer) ? { keys: [key], requires: [] } : this.keyInfo(outer.argumentExpression);
          this.writeKeys(outer, value, info.keys, writeHow(outer), info.requires);
          return;
        }
        let up = outer;
        while (up.parent && ts.isParenthesizedExpression(up.parent)) up = up.parent;
        const member = { ...memberValue(value, key) };
        if (ts.isCallExpression(up.parent) && up.parent.expression === up) {
          this.call(up, up.parent, member);
          return this.followResults(up.parent, member);
        }
        if (ts.isTaggedTemplateExpression(up.parent) && up.parent.tag === up) return this.call(up, up.parent, member);
        if (ts.isNewExpression(up.parent) && up.parent.expression === up) return this.construct(up.parent, member);
        current = outer;
        value = member;
        continue;
      }
      if (ts.isCallExpression(outer) && outer.expression === current) {
        this.call(current, outer, value);
        return this.followResults(outer, value);
      }
      if (ts.isTaggedTemplateExpression(outer) && outer.tag === current) return this.call(current, outer, value);
      if (ts.isNewExpression(outer) && outer.expression === current) return this.construct(outer, value);
      if ((ts.isCallExpression(outer) || ts.isNewExpression(outer)) && outer.arguments?.includes(current)) {
        return this.argument(current, value, outer, outer.arguments.indexOf(current));
      }
      if (ts.isTemplateSpan(outer) && ts.isTemplateExpression(outer.parent) &&
          ts.isTaggedTemplateExpression(outer.parent.parent) && outer.parent.parent.template === outer.parent) {
        // A tagged substitution is an argument, not a string coercion.
        return this.argument(current, value, outer.parent.parent, outer.parent.templateSpans.indexOf(outer) + 1);
      }
      if (ts.isBinaryExpression(outer)) {
        const op = outer.operatorToken.kind;
        if ([K.EqualsEqualsEqualsToken, K.ExclamationEqualsEqualsToken, K.EqualsEqualsToken, K.ExclamationEqualsToken,
          K.InstanceOfKeyword, K.InKeyword].includes(op)) return this.pass(value, 'comparison');
        if (op === K.AmpersandAmpersandToken && outer.left === current && isTruthy(value)) return this.pass(value, 'test');
        if ((op === K.BarBarToken || op === K.QuestionQuestionToken) && outer.right === current &&
            this.leftDecides(outer.left)) return this.pass(value, 'not evaluated');
        if (op === K.BarBarToken || op === K.AmpersandAmpersandToken || op === K.QuestionQuestionToken) {
          current = outer;
          continue;
        }
        if (op === K.CommaToken) {
          if (outer.right === current) {
            current = outer;
            continue;
          }
          return this.pass(value, 'discarded');
        }
        if (op === K.EqualsToken && outer.right === current) {
          if (ts.isIdentifier(outer.left)) return this.reference(outer.left, value, current);
          if (ts.isObjectLiteralExpression(outer.left) || ts.isArrayLiteralExpression(outer.left)) {
            return this.destructure(outer.left, value, current);
          }
          return this.escape(current, value, 'stored in an object');
        }
        if (BINARY_ASSIGNMENTS.has(op)) return this.escape(current, value, 'stored by a compound assignment');
        return this.pass(value, 'operand');
      }
      if (ts.isConditionalExpression(outer)) {
        if (outer.condition === current) return this.pass(value, 'test');
        current = outer;
        continue;
      }
      if (ts.isTypeOfExpression(outer) || ts.isVoidExpression(outer) || ts.isPrefixUnaryExpression(outer) ||
          ts.isTemplateSpan(outer) || ts.isComputedPropertyName(outer) ||
          (ts.isElementAccessExpression(outer) && outer.argumentExpression === current)) return this.pass(value, 'operand');
      if (ts.isExpressionStatement(outer) || ts.isIfStatement(outer) || ts.isWhileStatement(outer) || ts.isDoStatement(outer) ||
          ts.isSwitchStatement(outer) || ts.isCaseClause(outer) || (ts.isForStatement(outer) && outer.condition === current) ||
          ((ts.isForInStatement(outer) || ts.isForOfStatement(outer)) && outer.expression === current)) {
        return this.pass(value, 'statement');
      }
      if (ts.isExpressionWithTypeArguments(outer) && ts.isHeritageClause(outer.parent)) {
        if (CODE_RUNNERS.has(value.root) && value.path.length === 0 && value.root !== 'eval') {
          this.result.dynamic.push({ ...this.where(outer.parent.parent), kind: `a class that extends the ${value.root} constructor` });
          return;
        }
        return this.pass(value, 'extends');
      }
      if (ts.isVariableDeclaration(outer) && outer.initializer === current) {
        if (ts.isIdentifier(outer.name)) return this.reference(outer.name, value, current);
        return this.destructure(outer.name, value, current);
      }
      if (ts.isDecorator(outer)) return this.pass(value, 'decorator');
      if (ts.isWithStatement(outer) && outer.expression === current) {
        return this.escape(current, value, 'the object of a with statement, whose members the names inside it can write', 'with');
      }
      if (ts.isExportSpecifier(outer) && !outer.parent.parent.moduleSpecifier) {
        if (value.root !== UNKNOWN && !value.foreign) {
          this.result.exports.push({ ...this.where(current), value: valueName(value), name: outer.name.text });
        }
        return;
      }
      return this.escape(current, value, outer.kind === K.ReturnStatement ? 'returned'
        : outer.kind === K.ThrowStatement ? 'thrown' : outer.kind === K.YieldExpression ? 'yielded'
        : ts.isArrowFunction(outer) ? 'returned from an arrow function'
        : ts.isPropertyAssignment(outer) || ts.isShorthandPropertyAssignment(outer) ? 'stored in an object literal'
        : ts.isArrayLiteralExpression(outer) ? 'stored in an array literal'
        : ts.isSpreadElement(outer) || ts.isSpreadAssignment(outer) ? 'spread'
        : ts.isParameter(outer) || ts.isBindingElement(outer) ? 'a default value'
        : ts.isPropertyDeclaration(outer) ? 'stored in a class field'
        : ts.isExportAssignment(outer) ? 'exported as the default export'
        : `used as the ${fieldOf(outer, current).field} of a ${kindName(outer)}`);
    }
  }

  // An argument passed unchanged to a function, constructor or template tag. Where every function the
  // call may call receives it as a definer's target, the callee's scan records the write, and where every
  // one is a prototype reader, the callee's scan follows the prototype; otherwise the value escapes.
  argument(node, value, call, position) {
    const calleeNode = ts.isTaggedTemplateExpression(call) ? call.tag : call.expression;
    const callable = ts.isCallExpression(call) || ts.isTaggedTemplateExpression(call);
    const functions = callable ? this.possibleValues(calleeNode) : [];
    const definer = functions.length > 0 && functions.every((fn) => definerTargetPosition(fn) === position) &&
      this.possibleValues(node).some((target) => valueName(target) === valueName(value));
    const reader = functions.length > 0 && position === 0 && functions.every((fn) => PROTOTYPE_READERS.has(valueName(fn)));
    // Reflect.apply(M, T, list) with M only ever a mutating method of Array.prototype: the call records the
    // write to T, so neither M nor T escapes there.
    // The full abstract value, whose unknown counts a variable that may also hold a value the scan does
    // not identify.
    const applied = callable && call.arguments?.[0] ? this.abstract(call.arguments[0]) : UNIDENTIFIED;
    const mutated = functions.length > 0 && functions.every((fn) => valueName(fn) === 'Reflect.apply') && !applied.unknown &&
      applied.values.length > 0 && applied.values.every(isArrayMutator) && (position === 0 ||
        (position === 1 && this.possibleValues(node).some((target) => valueName(target) === valueName(value))));
    if ((definer || reader || mutated) && !this.abstract(calleeNode).unknown) return;
    return this.escape(node, value, ts.isTaggedTemplateExpression(call) ? 'passed to a template tag'
      : ts.isNewExpression(call) ? 'passed to a constructor' : 'passed to a function');
  }

  // A call of a prototype reader gives the prototype of each object its argument may be; each is
  // followed from the call.
  followResults(call, value) {
    if (!PROTOTYPE_READERS.has(valueName(value)) || !call.arguments[0]) return;
    for (const prototype of this.prototypesOf(call.arguments[0])) this.follow(call, prototype);
  }

  construct(expression, value) {
    if (CODE_RUNNERS.has(value.root) && value.path.length === 0 && value.root !== 'eval') {
      this.result.dynamic.push({ ...this.where(expression), kind: `${value.root} constructor` });
    } else if (value.root === UNKNOWN && value.path.at(-1) === 'constructor') {
      this.result.dynamic.push({ ...this.where(expression), kind: 'a constructor the scan cannot identify, which may be the Function constructor' });
    } else {
      this.pass(value, 'construction');
    }
  }

  escape(node, value, reason, limit) {
    if (value.root === UNKNOWN) {
      this.result.unidentified += 1;
      return;
    }
    if (value.foreign) return;
    if (CODE_RUNNERS.has(value.root) && value.path.length === 0) {
      this.result.dynamic.push({ ...this.where(node), kind: `${value.root === 'eval' ? 'eval' : `the ${value.root} constructor`} ${reason}` });
      return;
    }
    // A definer that goes where the scan does not follow may be called from there: through call,
    // apply, Reflect.apply or bind, or by a higher-order built-in. A mutating method of Array.prototype
    // called from there writes only index members and the length of its this value, none of which is a
    // dependency slot, so the scan does not record a limit for it.
    const definer = STATIC_DEFINERS.has(valueName(value)) || RECEIVER_DEFINERS.has(value.path.at(-1));
    const stated = limit ?? (definer ? 'definer-calls' : undefined);
    this.result.unfollowed.push({ ...this.where(node), value: valueName(value), reason, ...(stated ? { limit: stated } : {}) });
  }

  // A variable receives value: the caller follows its uses.
  reference(name, value, from) {
    if (value.root === UNKNOWN) this.result.unidentified += 1;
    if (value.foreign) return;
    const record = { ...this.where(from), value: valueName(value), unknown: value.root === UNKNOWN || undefined,
      name: name.text, at: start(name, this.sourceFile) };
    this.result.references.push(record);
    // A method read through an instance and stored in a variable is called without that instance as this.
    this.referenceValues.push({ name, value: { root: value.root, path: value.path }, record });
  }

  // A destructuring pattern receives value: each element with a static key receives that member.
  destructure(pattern, value, from) {
    const elements = ts.isObjectBindingPattern(pattern) || ts.isArrayBindingPattern(pattern) ? pattern.elements
      : ts.isObjectLiteralExpression(pattern) ? pattern.properties : pattern.elements;
    for (const element of elements) {
      if (ts.isOmittedExpression(element)) continue;
      if (ts.isArrayBindingPattern(pattern) || ts.isArrayLiteralExpression(pattern) || ts.isSpreadAssignment(element) ||
          ts.isSpreadElement(element) || element.dotDotDotToken) {
        this.escape(from, value, 'destructured by position or with a rest element');
        continue;
      }
      let key;
      let targetNode;
      if (ts.isBindingElement(element)) {
        key = element.propertyName ? memberKey(element.propertyName) : element.name.text;
        targetNode = element.name;
      } else if (ts.isShorthandPropertyAssignment(element)) {
        key = element.name.text;
        targetNode = element.name;
      } else if (ts.isPropertyAssignment(element)) {
        key = memberKey(element.name);
        targetNode = element.initializer;
        if (ts.isBinaryExpression(targetNode) && targetNode.operatorToken.kind === K.EqualsToken) targetNode = targetNode.left;
      } else {
        this.escape(from, value, 'destructured');
        continue;
      }
      const member = memberValue(value, key === undefined ? '*' : key);
      if (ts.isIdentifier(targetNode)) this.reference(targetNode, member, from);
      else if (ts.isObjectBindingPattern(targetNode) || ts.isObjectLiteralExpression(targetNode)) this.destructure(targetNode, member, from);
      else this.escape(from, member, 'destructured into an object');
    }
  }
}

// A built-in's name inside a with statement may name a member of the with's object instead. Both passes
// follow it as the built-in, so a write through it counts, and record the place with this note. A key
// read under with is not proved to be a Symbol.for result or a well-known symbol.
const WITH_NOTE = 'a with statement may give the name another value; the scan follows it as the built-in';

function noteWith(scan, node) {
  scan.result.unfollowed.push({ ...scan.where(node), value: node.text, reason: WITH_NOTE, limit: 'with' });
}

// The value a built-in's name holds: the global object under any of its names, else the built-in.
function nameValue(name) {
  return GLOBAL_NAMES.has(name) ? { root: 'globalThis', path: [] } : { root: name, path: [] };
}

// Scans a module's occurrences of free built-in names (scan.freeValue gives a free name's value) and its
// .constructor and .__proto__ reads.
function scanBuiltins(names, reaches, scan) {
  for (const node of names) {
    const value = scan.freeValue(node);
    if (value) scan.follow(node, value);
  }
  for (const node of reaches) {
    // A write to the object's own constructor or __proto__ member is a write the object's own follow sees.
    if (isWriteTarget(node)) continue;
    const key = ts.isPropertyAccessExpression(node) ? node.name.text : literalKey(node.argumentExpression);
    let object = node.expression;
    while (ts.isParenthesizedExpression(object)) object = object.expression;
    if (ts.isObjectLiteralExpression(object)) {
      // The literal's own member, or the one it inherits: no name leads here.
      for (const value of scan.abstract(node).values) scan.follow(node, value);
      continue;
    }
    // A built-in value that a built-in's name or a followed variable leads to is followed from there. An
    // instance, which no name leads to, and an object the scan does not identify are followed from here.
    const base = scan.abstract(node.expression);
    for (const value of base.values) if (value.instance) scan.follow(node, memberValue(value, key));
    if (base.unknown) scan.follow(node, { root: UNKNOWN, path: [key] });
  }
  return scan.result;
}

// A scan's lists in source order.
function inSourceOrder(result) {
  for (const value of Object.values(result)) if (Array.isArray(value)) value.sort((a, b) => a.s - b.s);
  return result;
}

function staticSpecifier(node) {
  if (!node) return { form: 'none' };
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return { form: 'literal', spec: node.text };
  if (ts.isTemplateExpression(node)) return { form: 'template-head', head: node.head.text ?? null };
  return { form: 'none' };
}

function indexModule(module, needles, byName) {
  const sourceFile = parse(module);
  const record = {
    index: module.index, name: module.name, length: module.text.length,
    diagnostics: sourceFile.parseDiagnostics.map((d) => ({ s: d.start, length: d.length,
      message: ts.flattenDiagnosticMessageText(d.messageText, '\n') })),
    loads: [], hits: Object.create(null),
  };
  // The built-in scan's starting points, scanned after the walk, when the module's declarations are known:
  // occurrences of a built-in's name, and .constructor and .__proto__ reads.
  const declared = new Set();
  const names = [];
  const reaches = [];
  const target = (spec) => (byName.has(spec) ? byName.get(spec) : null);
  const path = [];
  const functions = [];
  const handle = () => path.join('.');
  const hit = (key, node) => {
    if (!needles.has(key)) return;
    for (const needle of needles.get(key)) {
      (record.hits[needle] ??= []).push({ h: handle(), k: kindName(node), s: start(node, sourceFile), e: node.end,
        chain: chainOf(node) });
    }
  };
  const load = (kind, node, specifier) => {
    const entry = { k: kind, s: start(node, sourceFile), e: node.end, h: handle(), chain: chainOf(node) };
    const { form, spec, head } = staticSpecifier(specifier);
    entry.form = form;
    if (spec !== undefined) {
      entry.t = target(spec);
      if (entry.t === null) entry.spec = spec;
    }
    if (head !== undefined) entry.head = head;
    const inside = functions.at(-1);
    entry.inside = inside === undefined ? null : { k: kindName(inside.node), h: inside.handle };
    record.loads.push(entry);
  };
  const statement = (node) => {
    if (ts.isImportDeclaration(node)) {
      const spec = node.moduleSpecifier.text;
      const clause = node.importClause;
      const entry = { k: clause ? 'import' : 'side', s: start(node, sourceFile), e: node.end, h: handle(), t: target(spec) };
      if (entry.t === null) entry.spec = spec;
      if (clause) {
        entry.named = [];
        if (clause.name) entry.default = [clause.name.text, start(clause.name, sourceFile)];
        const bindings = clause.namedBindings;
        if (bindings && ts.isNamespaceImport(bindings)) entry.namespace = [bindings.name.text, start(bindings.name, sourceFile)];
        if (bindings && ts.isNamedImports(bindings)) {
          for (const element of bindings.elements) {
            entry.named.push([(element.propertyName ?? element.name).text, element.name.text, start(element.name, sourceFile)]);
          }
        }
      }
      record.loads.push(entry);
      return;
    }
    if (ts.isExportDeclaration(node)) {
      const entry = { s: start(node, sourceFile), e: node.end, h: handle() };
      if (node.moduleSpecifier) {
        const spec = node.moduleSpecifier.text;
        entry.t = target(spec);
        if (entry.t === null) entry.spec = spec;
      }
      if (!node.exportClause) entry.k = 'export*';
      else if (ts.isNamespaceExport(node.exportClause)) Object.assign(entry, { k: 'export*as', as: node.exportClause.name.text });
      else {
        entry.k = node.moduleSpecifier ? 'reexport' : 'export';
        entry.named = node.exportClause.elements.map((element) => {
          const local = element.propertyName ?? element.name;
          return [local.text, element.name.text, start(local, sourceFile)];
        });
      }
      record.loads.push(entry);
      return;
    }
    if (ts.isExportAssignment(node)) {
      record.loads.push({ k: 'export=', s: start(node, sourceFile), e: node.end, h: handle() });
      return;
    }
    const modifiers = ts.canHaveModifiers(node) ? ts.getModifiers(node) : undefined;
    if (modifiers && modifiers.some((modifier) => modifier.kind === K.ExportKeyword)) {
      const names = [];
      if (ts.isVariableStatement(node)) {
        // `export var {a, b: [c]} = o` exports a and c: every name its patterns bind.
        const bound = (name) => {
          if (ts.isIdentifier(name)) names.push([name.text, start(name, sourceFile)]);
          else for (const element of name.elements) if (ts.isBindingElement(element)) bound(element.name);
        };
        for (const declaration of node.declarationList.declarations) bound(declaration.name);
      } else if (node.name) names.push([node.name.text, start(node.name, sourceFile)]);
      const isDefault = modifiers.some((modifier) => modifier.kind === K.DefaultKeyword);
      const entry = { k: isDefault ? 'export-default' : 'exportdecl', s: start(node, sourceFile), e: node.end,
        h: handle(), names };
      // Anonymous default functions and classes export a value without introducing a local name.
      if (isDefault && !node.name) entry.anonymous = true;
      record.loads.push(entry);
    }
  };
  const visit = (node) => {
    if (node.parent && ts.isSourceFile(node.parent)) statement(node);
    if (ts.isIdentifier(node) && node.parent) {
      if (isDeclarationName(node)) declared.add(node.text);
      else if (BUILTINS.has(node.text) && !isPropertyName(node)) names.push(node);
    }
    if ((ts.isPropertyAccessExpression(node) && ['constructor', '__proto__'].includes(node.name.text)) ||
        (ts.isElementAccessExpression(node) && ['constructor', '__proto__'].includes(literalKey(node.argumentExpression)))) {
      reaches.push(node);
    }
    if (ts.isCallExpression(node)) {
      const callee = node.expression;
      if (callee.kind === K.ImportKeyword) load('import()', node, node.arguments[0]);
      else if (ts.isPropertyAccessExpression(callee) && callee.name.text === 'require' &&
               ts.isMetaProperty(callee.expression) && callee.expression.keywordToken === K.ImportKeyword &&
               callee.expression.name.text === 'meta') load('import.meta.require', node, node.arguments[0]);
    } else if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      const value = cooked(node);
      if (value !== undefined) hit(`string\0${value}`, node);
    } else if (ts.isTemplateHead(node)) {
      const value = cooked(node);
      if (value !== undefined) hit(`template-head\0${value}`, node);
    }
    if (ts.isPropertyAccessExpression(node.parent ?? node) && node.parent.name === node) hit(`member\0${node.text}`, node);
    if (node.parent && ts.isObjectLiteralElementLike(node.parent) && node.parent.name === node &&
        ts.isObjectLiteralExpression(node.parent.parent)) {
      const key = memberKey(node);
      if (key !== undefined) hit(`property\0${key}`, node);
    }
    const isFunction = isFunctionLike(node);
    if (isFunction) functions.push({ node, handle: handle() });
    let index = 0;
    ts.forEachChild(node, (child) => {
      path.push(index++);
      visit(child);
      path.pop();
    });
    if (isFunction) functions.pop();
  };
  visit(sourceFile);
  // A name the module declares anywhere is a value this pass does not identify; the facts pass resolves it.
  const freeValue = (identifier) => (BUILTINS.has(identifier.text) && !declared.has(identifier.text) &&
    !isPropertyName(identifier) && !isDeclarationName(identifier) ? nameValue(identifier.text) : null);
  const scan = new BuiltinScan(sourceFile, freeValue, handleOf, (identifier) => !declared.has(identifier.text) && !insideWith(identifier));
  for (const node of names) if (!declared.has(node.text) && insideWith(node)) noteWith(scan, node);
  record.premise = inSourceOrder(scanBuiltins(names, reaches, scan));
  // A built-in's name the module declares somewhere may be a local binding where it occurs; the facts
  // pass resolves those occurrences.
  record.premise.scoped = [...new Set(names.map((node) => node.text).filter((name) => declared.has(name)))];
  return record;
}

function index(request) {
  const needles = new Map();
  for (const needle of request.needles ?? []) {
    if (!['string', 'template-head', 'property', 'member'].includes(needle.type) || typeof needle.value !== 'string') {
      throw new RequestError(`a needle needs a type of string, template-head, property or member and a string value: ${JSON.stringify(needle)}`);
    }
    const key = `${needle.type}\0${needle.value}`;
    if (!needles.has(key)) needles.set(key, []);
    needles.get(key).push(needle.key);
  }
  const byName = new Map([...modules.values()].map((module) => [module.name, module.index]));
  const records = [];
  for (const module of [...modules.values()].sort((a, b) => a.index - b.index)) {
    records.push(indexModule(module, needles, byName));
    released(module.text.length);
  }
  return { modules: records };
}

// ---------------------------------------------------------------------------------------------------
// The facts pass

function programFor(module) {
  const name = fileName(module.index);
  const sourceFile = parse(module);
  const host = {
    getSourceFile: (file) => (file === name ? sourceFile : undefined),
    getDefaultLibFileName: () => 'lib.d.ts',
    writeFile: () => {},
    getCurrentDirectory: () => '/',
    getCanonicalFileName: (file) => file,
    useCaseSensitiveFileNames: () => true,
    getNewLine: () => '\n',
    fileExists: (file) => file === name,
    readFile: () => undefined,
  };
  const program = ts.createProgram({ rootNames: [name], options: OPTIONS, host });
  if (program.getSourceFile(name) !== sourceFile) {
    throw new Error(`module ${module.index} is not the source file of its own program`);
  }
  return { sourceFile, checker: program.getTypeChecker() };
}

// ---------------------------------------------------------------------------------------------------
// The binding model
//
// The facts about a binding (its record, its occurrences, and the names canonical text gives it) are
// given only for a binding whose every declaration and every reference has a form listed here. Every
// other binding is refused: its record has a `refused` list of {s, e, reason}, and the queries that
// would give its occurrences or rename it give that list instead. TypeScript's binder supplies the
// symbols, and this model accepts a symbol as one JavaScript binding, with exactly the references
// TypeScript resolves to it, only where the two agree.
//
// Declarations:
//   var, let and const declarations, with identifiers or destructuring patterns, in a statement or in
//     a for, for-in or for-of head;
//   function and class declarations with a name;
//   the name of a function expression or class expression, bound inside the expression;
//   parameters of functions, arrow functions, methods, accessors and constructors, with identifiers or
//     patterns;
//   catch clause variables, with identifiers or patterns;
//   import bindings: default, named and namespace.
// References:
//   an identifier in an expression: read, written by =, compound or logical assignment, ++ or --, as a
//     destructuring assignment target or as a for-in or for-of head, or the operand of typeof;
//   a shorthand property's value, also as a destructuring target;
//   the local name in an export list without a from clause.
// Scopes, as binding records report them:
//   the module: its top-level declarations, its vars outside functions, and its imports;
//   a function: its parameters, its vars, and the function declarations directly in its body; when
//     the parameter list has expressions (defaults, or patterns with defaults or computed keys), the
//     vars and those function declarations belong to the function body, which JavaScript then gives an
//     environment of its own;
//   a block, switch case block or for head: let, const, class, and a function declaration nested in a
//     block, a function body counting as a block of its own;
//   a class static block: the vars and function declarations directly in it;
//   a catch clause: its variable; a function or class expression: its own name.
// Refused:
//   every binding in a module TypeScript reports a parse diagnostic for;
//   using and await using declarations, and declarations of any other kind;
//   declarations JavaScript merges into one binding but TypeScript's binder keeps apart, which it
//     reports at their names, as `var V=1;function V(){}` in a function body;
//   a parameter and a var of the same name in the body of a function whose parameter list has
//     expressions: two bindings in JavaScript, one symbol in TypeScript;
//   a catch variable and a var of the same name in its catch block: the var's initializer assigns
//     the catch variable (ECMAScript Annex B.3.4), so neither symbol has the right writes;
//   a class declaration whose name is written: the references in the class body go to an inner
//     binding that stays the class;
//   a binding in whose scope a direct eval call or a with statement occurs, which can read and write
//     it by name;
//   a delete of the name, a reference in any other position, and a reference outside the scopes of
//     the binding's declarations.
// Modelled exactly where TypeScript differs:
//   a reference in a parameter list (in a default or a computed key, at any depth of nested functions
//     there) that TypeScript resolves to a declaration in that function's body is resolved again from
//     outside the function, where JavaScript looks it up: TypeScript resolves the x of
//     `function f(a=x){function x(){}}` to the body's x;
//   the named default export of a function or class is a binding under its local name, though
//     TypeScript names its symbol "default".

function declarationName(declaration) {
  return ts.getNameOfDeclaration(declaration) ?? declaration;
}

function bindingKind(declaration) {
  if (ts.isVariableDeclaration(declaration)) {
    const list = declaration.parent;
    if (ts.isCatchClause(list)) return 'catch';
    return declarationListKind(list);
  }
  if (ts.isBindingElement(declaration)) {
    let node = declaration;
    while (ts.isBindingElement(node) || ts.isObjectBindingPattern(node) || ts.isArrayBindingPattern(node)) node = node.parent;
    return `destructured ${bindingKind(node)}`;
  }
  if (ts.isParameter(declaration)) return 'parameter';
  if (ts.isFunctionDeclaration(declaration)) return 'function';
  if (ts.isFunctionExpression(declaration)) return 'function-expression name';
  if (ts.isClassDeclaration(declaration)) return 'class';
  if (ts.isClassExpression(declaration)) return 'class-expression name';
  if (ts.isImportSpecifier(declaration)) return 'import';
  if (ts.isImportClause(declaration)) return 'import default';
  if (ts.isNamespaceImport(declaration)) return 'import namespace';
  return kindName(declaration);
}

// A symbol's declarations in a module. In a JavaScript file TypeScript also records some references as
// declarations of a binding, to type the members they add: the object of a property assignment (the p
// of `p.x=1` and of `p.a.b=1`) and the first argument of `Object.defineProperty(p, ...)`. Those are
// Identifier nodes, which a declaration never is; they are references and are left out here.
function declarationsIn(symbol, sourceFile) {
  return (symbol.declarations ?? []).filter((d) => d.getSourceFile() === sourceFile && !ts.isIdentifier(d))
    .sort((a, b) => start(declarationName(a), sourceFile) - start(declarationName(b), sourceFile));
}

// The declaration a binding element belongs to: a variable declaration or a parameter.
function patternRoot(node) {
  let current = node;
  while (ts.isBindingElement(current)) current = current.parent.parent;
  return current;
}

// The one symbol the helper uses for a binding. TypeScript gives an exported declaration (`export let x`,
// `export function f`, `export default function f`) two symbols with the same declarations: the module's
// export symbol, which every reference resolves to, and a local symbol in the module's scope table, which
// points to it as exportSymbol. Both stand for one binding; this is the export symbol. Import aliases are
// left as they are: following them would lead to TypeScript's unknown symbol, because module resolution is disabled.
function bindingSymbol(symbol) {
  return symbol?.exportSymbol ?? symbol;
}

function isModelledDeclaration(declaration) {
  if (ts.isBindingElement(declaration)) {
    const root = patternRoot(declaration);
    return ts.isParameter(root) ? isFunctionLike(root.parent) : isModelledDeclaration(root);
  }
  if (ts.isVariableDeclaration(declaration)) {
    if (ts.isCatchClause(declaration.parent)) return true;
    return ['var', 'let', 'const'].includes(declarationListKind(declaration.parent));
  }
  if (ts.isParameter(declaration)) return isFunctionLike(declaration.parent);
  if (ts.isFunctionDeclaration(declaration) || ts.isClassDeclaration(declaration) ||
      ts.isFunctionExpression(declaration) || ts.isClassExpression(declaration)) return Boolean(declaration.name);
  return ts.isImportSpecifier(declaration) || ts.isImportClause(declaration) || ts.isNamespaceImport(declaration);
}

// Whether a function's parameter list holds an expression: a default, or a pattern with a default or a
// computed key. JavaScript then evaluates the parameters in an environment of their own.
function hasParameterExpressions(fn) {
  const inPattern = (name) => !ts.isIdentifier(name) && name.elements.some((element) => ts.isBindingElement(element) &&
    (element.initializer || (element.propertyName && ts.isComputedPropertyName(element.propertyName)) || inPattern(element.name)));
  return (fn.parameters ?? []).some((parameter) => parameter.initializer || inPattern(parameter.name));
}

// The var scope around a node: the module, a class static block, or a function, which is its body when
// its parameter list has expressions.
function varScope(node) {
  for (let current = node.parent; current; current = current.parent) {
    if (ts.isSourceFile(current) || ts.isClassStaticBlockDeclaration(current)) return current;
    if (isFunctionLike(current)) return hasParameterExpressions(current) && current.body ? current.body : current;
  }
  return node.getSourceFile();
}

// The block scope around a node: the nearest block, switch case block, for head or module.
function lexicalScope(node) {
  for (let current = node.parent; current; current = current.parent) {
    if (ts.isSourceFile(current) || ts.isBlock(current) || ts.isCaseBlock(current) || ts.isForStatement(current) ||
        ts.isForInStatement(current) || ts.isForOfStatement(current)) return current;
  }
  return node.getSourceFile();
}

// The scope a modelled declaration binds its name in (see the binding model above).
function scopeOf(declaration) {
  const root = ts.isBindingElement(declaration) ? patternRoot(declaration) : declaration;
  if (ts.isParameter(root)) return root.parent;
  if (ts.isVariableDeclaration(root)) {
    if (ts.isCatchClause(root.parent)) return root.parent;
    return declarationListKind(root.parent) === 'var' ? varScope(root) : lexicalScope(root);
  }
  if (ts.isFunctionExpression(root) || ts.isClassExpression(root)) return root;
  if (ts.isImportSpecifier(root) || ts.isImportClause(root) || ts.isNamespaceImport(root)) return root.getSourceFile();
  if (ts.isFunctionDeclaration(root)) {
    const holder = root.parent;
    const directlyInBody = ts.isSourceFile(holder) || (ts.isBlock(holder) && holder.parent &&
      (isFunctionLike(holder.parent) || ts.isClassStaticBlockDeclaration(holder.parent)) && holder.parent.body === holder);
    return ts.isSourceFile(holder) ? holder : directlyInBody ? varScope(root) : lexicalScope(root);
  }
  return lexicalScope(root);
}

function contains(outer, node) {
  return node.pos >= outer.pos && node.end <= outer.end;
}

// Whether a declaration is a var (or an element of one) or a function declaration: the declarations
// that share one binding with a parameter of the same name in the function body.
function isVarLike(declaration) {
  const root = ts.isBindingElement(declaration) ? patternRoot(declaration) : declaration;
  return (ts.isVariableDeclaration(root) && !ts.isCatchClause(root.parent) && declarationListKind(root.parent) === 'var') ||
    ts.isFunctionDeclaration(root);
}

function isParameterDeclaration(declaration) {
  return ts.isParameter(ts.isBindingElement(declaration) ? patternRoot(declaration) : declaration);
}

function catchClauseOf(declaration) {
  const root = ts.isBindingElement(declaration) ? patternRoot(declaration) : declaration;
  return ts.isVariableDeclaration(root) && ts.isCatchClause(root.parent) ? root.parent : undefined;
}

// The names a binding pattern or identifier binds.
function boundNames(name, into = []) {
  if (ts.isIdentifier(name)) into.push(name.text);
  else for (const element of name.elements) if (ts.isBindingElement(element)) boundNames(element.name, into);
  return into;
}

// The var declarations of a name directly in a catch clause's block, outside nested functions.
function catchBlockVars(clause, name) {
  const found = [];
  const visit = (node) => {
    if (isFunctionLike(node) || ts.isClassDeclaration(node) || ts.isClassExpression(node)) return;
    if (ts.isVariableDeclaration(node) && ts.isVariableDeclarationList(node.parent) &&
        declarationListKind(node.parent) === 'var' && boundNames(node.name).includes(name)) found.push(node);
    ts.forEachChild(node, visit);
  };
  visit(clause.block);
  return found;
}

function insideWith(node) {
  for (let current = node.parent, child = node; current; child = current, current = current.parent) {
    if (ts.isWithStatement(current) && current.statement === child) return true;
  }
  return false;
}

function isDirectEval(call) {
  if (!ts.isCallExpression(call) || call.questionDotToken || (call.flags & ts.NodeFlags.OptionalChain)) return false;
  let callee = call.expression;
  while (ts.isParenthesizedExpression(callee)) callee = callee.expression;
  return ts.isIdentifier(callee) && callee.text === 'eval';
}

// Whether an identifier is the name of a declaration.
function isDeclarationName(node) {
  const parent = node.parent;
  return Boolean(parent) && (ts.isVariableDeclaration(parent) || ts.isParameter(parent) || ts.isBindingElement(parent) ||
    ts.isFunctionDeclaration(parent) || ts.isFunctionExpression(parent) || ts.isClassDeclaration(parent) ||
    ts.isClassExpression(parent) || ts.isImportSpecifier(parent) || ts.isImportClause(parent) ||
    ts.isNamespaceImport(parent)) && parent.name === node;
}

function topLevelStatement(node) {
  let current = node;
  while (current.parent && !ts.isSourceFile(current.parent)) current = current.parent;
  return current.parent ? current : undefined;
}

// ---------------------------------------------------------------------------------------------------
// The operation model
//
// pfg.values evaluates a binding's initializer, then asks whether the program keeps that value.
// Facts.uses classifies every use of the binding in one module, with the uses of the variables and
// parameters the value reaches there; Facts.loadUse does the same from an import() or
// import.meta.require call whose module exports the value. Each use must match a form listed here;
// any other use is refused, with its place and the reason. A primitive cannot change, so for one
// only writes to its binding count. For a value of kind array, set, map or object:
//
//   Parents that hand the value on unchanged: parentheses, either operand of ||, && and ??, a
//   branch of ?:, the last operand of a comma, the right side of a destructuring assignment, and
//   await (for an object, only without an own then member, which await would call).
//   Reads, which run none of the value's code: typeof, void, !, the test of if, while, do, for and
//   ?:, === and !==, switch and case, a discarded expression, for-in, `k in V`, and
//   `V instanceof C` with C the free name Array, Set, Map or Object.
//   Conversions to a primitive: String(V), a template substitution, a computed key, an element
//   key, unary + - ~, the arithmetic and bitwise operators, == != and the relational comparisons,
//   and `V in o`. For an object they call its own toString or valueOf with V as this, so they are
//   refused when the object has either.
//   Member reads: V.p, V[k] (also optionally chained) and destructuring of V. For an object, a
//   read of an accessor calls its getter with V as this, so it is refused; so is a computed key or
//   a rest element when the object has an accessor.
//   Read methods: a call of a method in READ_METHODS with V as this (a member of V that is called,
//   also through parentheses). A callback argument must be an arrow or function expression written
//   in the call that does not read arguments, has no rest parameter at or before the position at
//   which the method passes the container, and declares no parameter at that position. For an
//   object, an own member named like the method refuses the call.
//   Iteration of an array, a Set or a Map through its built-in iterator: for-of, [...V], f(...V),
//   array destructuring, Array.from(V), new Set(V), new Map(V).
//   Object spread {...V}: refused for an object with an accessor.
//   Built-in functions with V as an argument, in exactly these forms: Object.keys(V),
//   Object.values(V), Object.entries(V), Object.hasOwn(V, k), Array.isArray(V), Array.from(V),
//   JSON.stringify(V), String(V), new Set(V) and new Map(V), with the name of the built-in free.
//   Object.values, Object.entries and JSON.stringify read every member (refused for an object with
//   an accessor), and JSON.stringify calls an own toJSON (refused).
//   An alias: `const W = V` (also let or var), with no other write to W; W's uses are classified.
//   A parameter: V as an argument of a plain call of a function the module declares (a function
//   declaration, or a variable never written that holds a function or arrow expression) or imports
//   by name or as a default, with no spread argument before V. The function must not read
//   arguments or have a rest parameter at or before V's position, and the parameter that receives
//   V must be a plain identifier. Its uses are classified, up to four calls from the binding on the
//   route with the fewest calls. Beyond the last parameter, V reaches only arguments. A call of an
//   imported function is listed in the result's calls; pfg.values finds the module that declares
//   the function and classifies the parameter's uses there, with a uses query in mode parameter.
//   An export: an export list entry, export default, or a declaration with export. pfg.values
//   classifies the uses in the modules that import the value, and wherever a namespace holding it
//   is taken.
// Everything else is refused, among it: a write to the binding or to a member of the value; a
// store into an object or array literal, a class field, or another variable by assignment; a
// return, throw or yield; a call of the value, or of a member outside READ_METHODS; the value as
// any other argument, as the this of call, apply or bind, or as a template tag's substitution; and
// instanceof with another right operand, which calls that operand's Symbol.hasInstance.
// Each accepted operation adds the built-in operations it relies on to requires, from
// OPERATION_VOCABULARY; pfg.values passes them on in each Value, and its caller checks them against
// the built-in scan.
//
// A namespace that holds the value under some export names is followed too. A member read with
// one of those names is a use of the value; a read of another name is a read; destructuring with
// static keys gives aliases of the value; `const ns2 = ns` gives another namespace variable. A
// computed member, a rest element, a call of a member (whose this would be the namespace), await
// (which would call an exported then), and any other use of the namespace are refused. The promise
// of import(M) may be awaited, given to .then with an arrow or function expression whose first
// parameter receives the namespace (a function expression must not read arguments), or be an
// element of the array literal given to Promise.all, whose result is awaited into an array pattern
// or given to .then the same way; the pattern element at the import's position receives the
// namespace.

const CONTAINER_CONSTRUCTORS = { array: 'Array', set: 'Set', map: 'Map', object: 'Object' };
const PROTOTYPE_CHAINS = {
  array: ['Array.prototype', 'Object.prototype'], set: ['Set.prototype', 'Object.prototype'],
  map: ['Map.prototype', 'Object.prototype'], object: ['Object.prototype'],
};
const ITERATION_REQUIRES = {
  array: ['Array.prototype.@@iterator', '%ArrayIteratorPrototype%.next'],
  set: ['Set.prototype.@@iterator', '%SetIteratorPrototype%.next'],
  map: ['Map.prototype.@@iterator', '%MapIteratorPrototype%.next'],
};
// The read methods of each kind of value: container is the position at which the method passes the
// value to its callback, and species marks a method that creates its result through the value's
// constructor (ArraySpeciesCreate).
const READ_METHODS = {
  array: new Map([
    ['includes', {}], ['indexOf', {}], ['lastIndexOf', {}], ['join', {}], ['at', {}], ['keys', {}], ['values', {}],
    ['entries', {}], ['slice', { species: true }], ['concat', { species: true }], ['some', { container: 2 }],
    ['every', { container: 2 }], ['find', { container: 2 }], ['findIndex', { container: 2 }], ['findLast', { container: 2 }],
    ['findLastIndex', { container: 2 }], ['forEach', { container: 2 }], ['filter', { container: 2, species: true }],
    ['map', { container: 2, species: true }], ['flatMap', { container: 2, species: true }], ['reduce', { container: 3 }],
    ['reduceRight', { container: 3 }],
  ]),
  set: new Map([['has', {}], ['keys', {}], ['values', {}], ['entries', {}], ['forEach', { container: 2 }]]),
  map: new Map([['get', {}], ['has', {}], ['keys', {}], ['values', {}], ['entries', {}], ['forEach', { container: 2 }]]),
  object: new Map([['hasOwnProperty', {}]]),
};
const READING_FUNCTIONS = new Map([
  ['Object.keys', { argc: 1 }], ['Object.values', { argc: 1, getters: true }], ['Object.entries', { argc: 1, getters: true }],
  ['Object.hasOwn', { argc: 2 }], ['Array.isArray', { argc: 1 }], ['Array.from', { argc: 1, iterates: true }],
  ['JSON.stringify', { argc: 1, getters: true, hook: 'toJSON' }], ['String', { argc: 1, converts: true }],
]);
const COPYING_CONSTRUCTORS = new Map([['Set', ['Set', 'Set.prototype.add']], ['Map', ['Map', 'Map.prototype.set']]]);
const INSTANCEOF_CONSTRUCTORS = new Set(['Array', 'Set', 'Map', 'Object']);
const CONVERSION_HOOKS = ['toString', 'valueOf'];
const VALUE_KINDS = new Set(['array', 'set', 'map', 'object', 'primitive']);
const ARTICLES = { array: 'an array', set: 'a Set', map: 'a Map', object: 'an object' };
// Every built-in operation a use can require; UseContext.require refuses any other name, so that the
// test that the built-in scan can name a write to each covers all of them (request vocabulary).
const OPERATION_VOCABULARY = (() => {
  const prototypes = [...new Set(Object.values(PROTOTYPE_CHAINS).flat())];
  const hooks = [...READING_FUNCTIONS.values()].filter((reading) => reading.hook).map((reading) => reading.hook);
  return new Set([
    ...[...INSTANCEOF_CONSTRUCTORS].map((constructor) => `${constructor}.@@hasInstance`), 'Function.prototype.@@hasInstance',
    ...prototypes.flatMap((prototype) => ['then', '@@toPrimitive', ...CONVERSION_HOOKS, ...hooks].map((name) => `${prototype}.${name}`)),
    'Array.prototype.join', 'Set.prototype.size', 'Map.prototype.size',
    ...Object.entries(READ_METHODS).flatMap(([kind, methods]) => [...methods.keys()].map((key) => `${CONTAINER_CONSTRUCTORS[kind]}.prototype.${key}`)),
    'Array.prototype.constructor', 'Array.@@species', 'Array.prototype.@@isConcatSpreadable',
    ...Object.values(ITERATION_REQUIRES).flat(), ...[...COPYING_CONSTRUCTORS.values()].flat(), ...READING_FUNCTIONS.keys(),
    'Promise.prototype.then', 'Promise.all', 'Promise.resolve',
  ]);
})();

// The state of one uses or loadUse query.
class UseContext {
  constructor(model) {
    if (!model || !VALUE_KINDS.has(model.kind)) {
      throw new RequestError(`a value model needs a kind among ${[...VALUE_KINDS].join(', ')}: ${JSON.stringify(model)}`);
    }
    this.model = { kind: model.kind, keys: model.keys ?? [], accessors: model.accessors ?? [] };
    this.refused = [];
    this.requires = new Set();
    this.exports = [];
    this.calls = [];
    this.followed = [];
    this.queue = [];
    this.visited = new Set();
    this.classified = 0;
  }

  require(...names) {
    for (const name of names) {
      if (!OPERATION_VOCABULARY.has(name)) throw new Error(`the operation model requires ${name}, which OPERATION_VOCABULARY does not list`);
      this.requires.add(name);
    }
  }

  result() {
    return { refused: this.refused, requires: [...this.requires].sort(), exports: this.exports, calls: this.calls,
      followed: this.followed, classified: this.classified };
  }
}

// What a refused use does, in words, from its parent.
function storeReason(outer, current) {
  if (ts.isReturnStatement(outer) || (ts.isArrowFunction(outer) && outer.body === current)) return 'the value is returned';
  if (ts.isThrowStatement(outer)) return 'the value is thrown';
  if (ts.isYieldExpression(outer)) return 'the value is yielded';
  if (ts.isPropertyAssignment(outer) || ts.isShorthandPropertyAssignment(outer)) return 'the value is stored in an object literal';
  if (ts.isArrayLiteralExpression(outer)) return 'the value is stored in an array literal';
  if (ts.isPropertyDeclaration(outer)) return 'the value is stored in a class field';
  if (ts.isParameter(outer) || ts.isBindingElement(outer)) return 'the value is a default value';
  if (ts.isExpressionWithTypeArguments(outer)) return 'the value is the base of a class';
  return `the value is used as the ${fieldOf(outer, current).field} of a ${kindName(outer)}`;
}

// The problem with a callback that a read method calls with the value at position at, or null.
function callbackProblem(argument, at) {
  let fn = argument;
  while (fn && ts.isParenthesizedExpression(fn)) fn = fn.expression;
  if (!fn || !(ts.isArrowFunction(fn) || ts.isFunctionExpression(fn))) {
    return 'the callback is not an arrow or function expression written in the call';
  }
  const rest = fn.parameters.findIndex((parameter) => parameter.dotDotDotToken);
  if (rest !== -1 && rest <= at) return 'a rest parameter of the callback receives the value';
  if (fn.parameters.length > at) return `the callback declares parameter ${at + 1}, which receives the value`;
  if (readsArguments(fn)) return 'the callback reads arguments, which hold the value';
  return null;
}

function unparenthesized(node) {
  let current = node;
  while (current && ts.isParenthesizedExpression(current)) current = current.expression;
  return current;
}

// The outermost expression that is node in parentheses.
function outermostParentheses(node) {
  let current = node;
  while (current.parent && ts.isParenthesizedExpression(current.parent)) current = current.parent;
  return current;
}

function isLiteralExpression(node) {
  const current = unparenthesized(node);
  return ts.isStringLiteral(current) || ts.isNoSubstitutionTemplateLiteral(current) || ts.isNumericLiteral(current) ||
    current.kind === K.TrueKeyword || current.kind === K.FalseKeyword || current.kind === K.NullKeyword ||
    (ts.isPrefixUnaryExpression(current) && ts.isNumericLiteral(current.operand)) ||
    (ts.isVoidExpression(current) && ts.isNumericLiteral(current.expression));
}

class Facts {
  constructor(module) {
    this.module = module;
    const { sourceFile, checker } = programFor(module);
    this.sourceFile = sourceFile;
    this.checker = checker;
    this.ids = new Map();
    this.symbols = new Map();
    this.examined = new Map();
    this.identifiers = null;
    this.dynamicSites = null;
  }

  idOf(given) {
    const symbol = bindingSymbol(given);
    if (!symbol) return null;
    if (this.ids.has(symbol)) return this.ids.get(symbol);
    const declarations = declarationsIn(symbol, this.sourceFile);
    const id = declarations.length ? `m${this.module.index}@${start(declarationName(declarations[0]), this.sourceFile)}` : null;
    this.ids.set(symbol, id);
    if (id !== null) this.symbols.set(id, symbol);
    return id;
  }

  // The symbol an identifier refers to in JavaScript: TypeScript's, except that a reference in a
  // parameter list which TypeScript resolves into that function's body is resolved again from outside
  // the function (see the binding model).
  resolve(node) {
    const parent = node.parent;
    let symbol;
    if (parent && ts.isShorthandPropertyAssignment(parent) && parent.name === node) {
      symbol = this.checker.getShorthandAssignmentValueSymbol(parent);
    } else if (parent && ts.isExportSpecifier(parent)) {
      symbol = this.checker.getExportSpecifierLocalTargetSymbol(parent);
    } else {
      symbol = this.checker.getSymbolAtLocation(node);
    }
    symbol = bindingSymbol(symbol);
    if (!symbol || isDeclarationName(node)) return symbol;
    for (let child = node, current = node.parent; current; child = current, current = current.parent) {
      if (!(ts.isParameter(child) && isFunctionLike(current) && current.body)) continue;
      const body = current.body;
      const declarations = declarationsIn(symbol, this.sourceFile);
      if (declarations.length && declarations.every((d) => contains(body, d))) {
        const own = ts.isFunctionExpression(current) && current.name?.text === node.text;
        symbol = bindingSymbol(own ? this.checker.getSymbolAtLocation(current.name)
          : this.checker.resolveName(node.text, current.parent, ts.SymbolFlags.Value, false));
        if (!symbol) return undefined;
      }
    }
    return symbol;
  }

  // The identifiers of the module that are not property names, by spelling, found in one walk.
  // The local symbol of each exported declaration in the module's scope table, by its export symbol.
  exportLocals() {
    if (this.locals) return this.locals;
    this.locals = new Map();
    this.sourceFile.locals?.forEach((local) => {
      if (local.exportSymbol) this.locals.set(local.exportSymbol, local);
    });
    return this.locals;
  }

  identifierIndex() {
    if (this.identifiers) return this.identifiers;
    const index = new Map();
    const visit = (node) => {
      if (ts.isIdentifier(node) && !isPropertyName(node)) {
        const list = index.get(node.escapedText);
        if (list) list.push(node);
        else index.set(node.escapedText, [node]);
      }
      ts.forEachChild(node, visit);
    };
    visit(this.sourceFile);
    this.identifiers = index;
    return index;
  }

  // Direct eval calls and with statements, which can read and write any binding whose scope holds them.
  dynamicScopes() {
    if (this.dynamicSites) return this.dynamicSites;
    const sites = [];
    const visit = (node) => {
      if (ts.isWithStatement(node)) sites.push({ node, reason: 'a with statement here can read and write it by name' });
      else if (isDirectEval(node)) sites.push({ node, reason: 'a direct eval call here can read and write it by name' });
      ts.forEachChild(node, visit);
    };
    visit(this.sourceFile);
    this.dynamicSites = sites;
    return sites;
  }

  // The occurrences of a binding and the reasons it is refused. Only the identifiers spelled like its
  // declarations are resolved.
  examine(given) {
    const symbol = bindingSymbol(given);
    if (this.examined.has(symbol)) return this.examined.get(symbol);
    const sourceFile = this.sourceFile;
    const declarations = declarationsIn(symbol, sourceFile);
    const refused = sourceFile.parseDiagnostics.map((diagnostic) => ({ s: diagnostic.start,
      e: diagnostic.start + diagnostic.length,
      reason: `TypeScript cannot parse this module: ${ts.flattenDiagnosticMessageText(diagnostic.messageText, ' ')}` }));
    const refuse = (node, reason) => refused.push({ s: start(node, sourceFile), e: node.end, reason });
    for (const declaration of declarations) {
      if (!isModelledDeclaration(declaration)) {
        refuse(declaration, `it is declared by a ${kindName(declaration)}, which the binding model does not cover`);
      }
    }
    // An exported declaration also has a local symbol in the module's scope table, whose exportSymbol is
    // the export symbol (bindingSymbol). Another declaration of the name at the module's top level goes
    // into that local symbol only: `export var x=1;var x=2;` is one JavaScript binding, and
    // `export function f(){}function f(){}` a module JavaScript rejects. Either way the export symbol's
    // declarations leave it out, so the binding is refused.
    for (const declaration of declarationsIn(this.exportLocals().get(symbol) ?? { declarations: [] }, sourceFile)) {
      if (!declarations.includes(declaration)) {
        refuse(declaration, 'the module declares the name again besides its exported declaration, which TypeScript keeps in ' +
          'another symbol');
      }
    }
    const modelled = declarations.filter(isModelledDeclaration);
    const names = new Set(declarations.map((d) => start(declarationName(d), sourceFile)));
    for (const d of sourceFile.bindDiagnostics ?? []) {
      if (names.has(d.start)) {
        refused.push({ s: d.start, e: d.start + d.length, reason: 'TypeScript keeps apart declarations JavaScript ' +
          `merges into one binding (${ts.flattenDiagnosticMessageText(d.messageText, ' ')})` });
      }
    }
    for (const parameter of modelled.filter(isParameterDeclaration)) {
      const fn = patternRoot(parameter).parent;
      if (hasParameterExpressions(fn) && modelled.some((d) => isVarLike(d) && fn.body && contains(fn.body, d))) {
        refuse(parameter, 'the parameter list has expressions, so the parameter and the var of the same name in the ' +
          'body are two bindings in JavaScript but one symbol in TypeScript');
      }
    }
    for (const declaration of modelled) {
      const clause = catchClauseOf(declaration);
      const name = declarationName(declaration);
      if (clause && ts.isIdentifier(name) && catchBlockVars(clause, name.text).length) {
        refuse(declaration, 'a var of the same name in the catch block assigns this catch variable in its initializer');
      }
      if (isVarLike(declaration) && ts.isIdentifier(name)) {
        for (let current = declaration.parent; current && !isFunctionLike(current); current = current.parent) {
          if (ts.isCatchClause(current) && current.variableDeclaration && contains(current.block, declaration) &&
              boundNames(current.variableDeclaration.name).includes(name.text)) {
            refuse(declaration, 'the catch clause around it has a variable of the same name, which its initializer ' +
              'assigns instead of this var');
          }
        }
      }
    }
    const scopes = modelled.map(scopeOf);
    for (const site of this.dynamicScopes()) {
      if (scopes.some((scope) => contains(scope, site.node))) refuse(site.node, site.reason);
    }
    const spellings = new Set([symbol.escapedName]);
    for (const declaration of declarations) {
      const name = declarationName(declaration);
      if (ts.isIdentifier(name)) spellings.add(name.escapedText);
    }
    const index = this.identifierIndex();
    const candidates = [...spellings].flatMap((spelling) => index.get(spelling) ?? []).sort((a, b) => a.pos - b.pos);
    const occurrences = [];
    const nodes = [];
    for (const node of candidates) {
      if (this.resolve(node) !== symbol) continue;
      const roles = rolesOf(node);
      nodes.push(node);
      occurrences.push({ s: start(node, sourceFile), e: node.end, h: handleOf(node), roles, chain: chainOf(node) });
      if (isDeclarationName(node)) {
        // Its declaration is one of the declarations checked above.
      } else if (roles.includes('delete-operand')) {
        refuse(node, 'the name is deleted');
      } else if (!scopes.some((scope) => contains(scope, node))) {
        refuse(node, 'TypeScript resolves a reference here that lies outside the scope of every declaration');
      }
      if (roles.includes('write') && declarations.some((d) => ts.isClassDeclaration(d))) {
        refuse(node, 'the class name is written here, while the class body keeps an inner binding to the class');
      }
    }
    const result = { occurrences, nodes, refused };
    this.examined.set(symbol, result);
    return result;
  }

  // The symbol of a binding id: the identifier at the id's position is one of its declaration names.
  symbolOfId(id) {
    const match = typeof id === 'string' ? /^m(\d+)@(\d+)$/.exec(id) : null;
    if (!match || Number(match[1]) !== this.module.index) {
      throw new RequestError(`binding ${id} is not one of module ${this.module.index}`);
    }
    if (!this.symbols.has(id)) {
      const token = ts.getTokenAtPosition(this.sourceFile, Number(match[2]));
      if (ts.isIdentifier(token) && start(token, this.sourceFile) === Number(match[2])) this.idOf(this.resolve(token));
    }
    const symbol = this.symbols.get(id);
    if (!symbol) throw new RequestError(`binding ${id} is absent from module ${this.module.index}`);
    return symbol;
  }

  // The binding a name has in the module scope: a top-level declaration, a var anywhere outside a
  // function, or an import.
  scopeBinding(name) {
    const symbol = bindingSymbol(this.sourceFile.locals?.get(ts.escapeLeadingUnderscores(name)));
    const id = symbol ? this.idOf(symbol) : null;
    return id === null ? { free: name } : this.binding(id);
  }

  reference(node) {
    if (!ts.isIdentifier(node) || isPropertyName(node)) return { role: 'name' };
    const symbol = this.resolve(node);
    const id = this.idOf(symbol);
    if (id !== null) return this.examine(symbol).refused.length ? { b: id, refused: true } : { b: id };
    if (symbol && node.text === 'arguments') return { special: 'arguments' };
    return { free: node.text };
  }

  binding(id) {
    const symbol = this.symbolOfId(id);
    const sourceFile = this.sourceFile;
    const declarations = declarationsIn(symbol, sourceFile);
    const first = declarationName(declarations[0]);
    const record = { id, name: ts.isIdentifier(first) ? first.text : symbol.name, kind: bindingKind(declarations[0]),
      declarations: [] };
    const { refused } = this.examine(symbol);
    if (refused.length) record.refused = refused;
    for (const declaration of declarations) {
      const kind = bindingKind(declaration);
      const name = declarationName(declaration);
      const entry = {
        kind, s: start(declaration, sourceFile), e: declaration.end, h: handleOf(declaration),
        name: { s: start(name, sourceFile), e: name.end },
      };
      if (isModelledDeclaration(declaration)) {
        const scope = scopeOf(declaration);
        entry.scope = { k: kindName(scope), s: start(scope, sourceFile), e: scope.end, h: handleOf(scope) };
        entry.module = ts.isSourceFile(scope);
      }
      const initializer = declaration.initializer;
      if (initializer) entry.init = { s: start(initializer, sourceFile), e: initializer.end, h: handleOf(initializer) };
      if (entry.module) {
        const statement = topLevelStatement(declaration);
        entry.statement = sourceFile.statements.indexOf(statement);
        if (ts.isVariableDeclaration(declaration) && ts.isVariableDeclarationList(declaration.parent)) {
          entry.declarator = declaration.parent.declarations.indexOf(declaration);
        }
      }
      if (kind.startsWith('import')) {
        let node = declaration;
        while (node && !ts.isImportDeclaration(node)) node = node.parent;
        entry.import = {
          spec: node.moduleSpecifier.text,
          imported: ts.isImportSpecifier(declaration) ? (declaration.propertyName ?? declaration.name).text
            : ts.isImportClause(declaration) ? 'default' : '*',
        };
      }
      record.declarations.push(entry);
    }
    return record;
  }

  // The built-in scan of this module with its bindings (see "The built-in scan"). A built-in's name
  // counts where it is free; a variable that receives a value is followed through its occurrences, and
  // one the binding model refuses is unfollowed. seeds gives what import bindings receive from other
  // modules' exports, as [{at, values, unknown}]: at is the position of the import's local name, values
  // are paths such as Set.prototype, and unknown says that the exporting variable may also hold a value
  // the scan does not identify.
  premise(seeds) {
    const seeded = new Map();
    for (const seed of seeds ?? []) {
      if (!Number.isInteger(seed.at) || !Array.isArray(seed.values)) throw new RequestError(`a seed needs a position and values: ${JSON.stringify(seed)}`);
      const known = seeded.get(seed.at) ?? { values: [], unknown: false };
      seeded.set(seed.at, { values: [...new Set([...known.values, ...seed.values.map(String)])], unknown: known.unknown || Boolean(seed.unknown) });
    }
    const values = new Map();
    // Each pass classifies every use with what the earlier passes found each variable may hold, and may
    // find more: a variable's value can come from a use classified before the variable got it. The
    // passes repeat until no variable gains a value. Values only accumulate, and the bounds on each
    // variable's values keep them finite.
    let signature = null;
    for (;;) {
      const result = this.premisePass(seeded, values);
      const next = JSON.stringify([...values].map(([symbol, held]) => [this.idOf(symbol), held.map(valueName).sort()]).sort());
      if (next === signature) return result;
      signature = next;
    }
  }

  premisePass(seeded, values) {
    const sourceFile = this.sourceFile;
    const free = (identifier) => this.idOf(this.resolve(identifier)) === null;
    // TypeScript resolves globalThis to a symbol of its own, without a declaration in the module.
    const freeValue = (identifier) => (!isPropertyName(identifier) && !isDeclarationName(identifier) && BUILTINS.has(identifier.text) &&
      free(identifier) ? nameValue(identifier.text) : null);
    const scan = new BuiltinScan(sourceFile, freeValue, handleOf, (identifier) => free(identifier) && !insideWith(identifier));
    scan.registryVariable = (identifier) => this.holdsRegistrySymbol(identifier, scan);
    scan.finiteKeys = (identifier) => this.entriesKeys(identifier, scan);
    scan.seeded = seeded;
    scan.origins = new Map();
    scan.visiting = new Set();
    // A variable may hold the values it received so far, and a value the scan does not identify when it
    // has a source outside the model (hasUnknownOrigins).
    scan.variableValue = (identifier, onlyValues) => {
      const symbol = this.resolve(identifier);
      if (!symbol || this.idOf(symbol) === null) return null;
      return { values: values.get(symbol) ?? [], unknown: !onlyValues && this.hasUnknownOrigins(symbol, scan) };
    };
    const names = [];
    const reaches = [];
    const visit = (node) => {
      if (ts.isIdentifier(node) && BUILTINS.has(node.text) && !isPropertyName(node) && !isDeclarationName(node)) {
        if (insideWith(node) && free(node)) noteWith(scan, node);
        names.push(node);
      }
      if ((ts.isPropertyAccessExpression(node) && ['constructor', '__proto__'].includes(node.name.text)) ||
          (ts.isElementAccessExpression(node) && ['constructor', '__proto__'].includes(literalKey(node.argumentExpression)))) {
        reaches.push(node);
      }
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
    const pending = [];
    for (const [at, seed] of seeded) {
      const token = ts.getTokenAtPosition(sourceFile, at);
      if (!ts.isIdentifier(token) || start(token, sourceFile) !== at) throw new RequestError(`position ${at} does not identify an identifier`);
      for (const value of seed.values) {
        const [root, ...path] = value.split('.');
        const record = { ...scan.where(token), value, name: token.text, at, import: true };
        pending.push({ name: token, value: { root, path }, record });
        scan.result.references.push(record);
      }
    }
    scanBuiltins(names, reaches, scan);
    // Follow each variable and value pair once.
    let taken = 0;
    const followed = new Set();
    const unknownOrigins = 'the variable can also receive values from declarations or writes outside the built-in value model';
    while (taken < scan.referenceValues.length || pending.length) {
      const { name, value, record } = pending.length ? pending.shift() : scan.referenceValues[taken++];
      // A value the scan does not follow from here is unfollowed; one it follows through a variable that
      // may also hold other values stays followed, and only its record says so.
      const unclassified = (reason, limit, followedAnyway = false) => {
        record.status = 'unclassified';
        record.reason = reason;
        if (!followedAnyway) scan.result.unfollowed.push({ ...scan.where(name), value: valueName(value), reason, ...(limit ? { limit } : {}) });
      };
      const symbol = this.resolve(name);
      const id = this.idOf(symbol);
      if (!symbol || id === null) {
        unclassified('stored in a name the module does not declare');
        continue;
      }
      record.binding = id;
      const examined = this.examine(symbol);
      if (examined.refused.length) {
        unclassified(`stored in a variable the binding model refuses (${examined.refused.map((r) => r.reason).join('; ')})`);
        continue;
      }
      const key = `${id} ${valueName(value)}`;
      if (followed.has(key)) {
        if (this.hasUnknownOrigins(symbol, scan)) unclassified(unknownOrigins, undefined, true);
        else record.status = 'followed';
        continue;
      }
      // A variable that derives its value from its own value, as `t = t.prototype`, gets a new value each
      // round. The bounds stop that derivation.
      if (value.path.length > MAX_VALUE_PATH) {
        unclassified(`a value derived more than ${MAX_VALUE_PATH} members deep is not followed`, 'bounds');
        continue;
      }
      const held = values.get(symbol) ?? [];
      if (held.length >= MAX_VALUES_PER_VARIABLE && !held.some((known) => valueName(known) === valueName(value))) {
        unclassified(`the variable holds more than ${MAX_VALUES_PER_VARIABLE} built-in values, and the others are not followed`, 'bounds');
        continue;
      }
      // Known built-in alternatives are still followed for writes; the record says that the variable may
      // also hold other values.
      if (this.hasUnknownOrigins(symbol, scan)) unclassified(unknownOrigins, undefined, true);
      else record.status = 'followed';
      followed.add(key);
      if (!held.some((known) => valueName(known) === valueName(value))) held.push(value);
      values.set(symbol, held);
      for (const declaration of declarationsIn(symbol, sourceFile)) {
        const root = ts.isBindingElement(declaration) ? patternRoot(declaration) : declaration;
        if (exportedByModifier(root) && value.root !== UNKNOWN) {
          scan.result.exports.push({ ...scan.where(declarationName(declaration)), value: valueName(value), name: declarationName(declaration).text });
        }
      }
      for (const node of examined.nodes) if (!isDeclarationName(node)) scan.follow(node, value, true);
    }
    // An export of a variable with sources outside the model passes that on to its importers.
    for (const entry of scan.result.exports) {
      const token = ts.getTokenAtPosition(sourceFile, entry.s);
      const symbol = ts.isIdentifier(token) ? this.resolve(token) : undefined;
      if (symbol && this.idOf(symbol) !== null && this.hasUnknownOrigins(symbol, scan)) entry.unknown = true;
    }
    return inSourceOrder(scan.result);
  }

  // Whether a variable may hold a value from a source outside the built-in value model, which the scan
  // then does not identify. The sources outside it: a parameter, a catch variable, a function or class,
  // an import that no export seeds with built-in values or whose exporting variable has such sources
  // itself, a binding the binding model refuses, an element of an array pattern, a rest element or an
  // element with a default, and a declaration or `=` assignment whose value the scan does not identify,
  // is an instance or is a member read through one; and every other write, as a compound assignment, a
  // for-in or for-of head or a destructuring target. A variable whose sources are other variables
  // depends on their sources; a cycle among them adds no source. This does not stop the scan from
  // following the variable's known built-in values, but makes the variable an uncertain callee.
  hasUnknownOrigins(symbol, scan) {
    if (scan.origins.has(symbol)) return scan.origins.get(symbol);
    if (scan.visiting.has(symbol)) return false;
    // Only the outermost query sees every source: an inner one stops where the cycle began. A found
    // source outside the model holds wherever it was found.
    const outermost = scan.visiting.size === 0;
    scan.visiting.add(symbol);
    let unknown;
    try {
      unknown = this.originsOutsideModel(symbol, scan);
    } finally {
      scan.visiting.delete(symbol);
    }
    if (unknown || outermost) scan.origins.set(symbol, unknown);
    return unknown;
  }

  originsOutsideModel(symbol, scan) {
    const sourceFile = this.sourceFile;
    const declarations = declarationsIn(symbol, sourceFile);
    // An import holds what the exporting module's variable holds; an ES module cannot assign it.
    if (declarations.every((d) => ts.isImportSpecifier(d) || ts.isImportClause(d))) {
      return declarations.some((d) => {
        const seed = scan.seeded.get(start(declarationName(d), sourceFile));
        return !seed || seed.unknown;
      });
    }
    // The declarations decide most variables before their occurrences are resolved.
    for (const declaration of declarations) {
      const root = ts.isBindingElement(declaration) ? patternRoot(declaration) : declaration;
      if (!ts.isVariableDeclaration(root) || catchClauseOf(declaration)) return true;
      for (let node = declaration; ts.isBindingElement(node); node = node.parent.parent) {
        if (ts.isArrayBindingPattern(node.parent) || node.dotDotDotToken || node.initializer) return true;
      }
    }
    const identified = (expression) => {
      const value = scan.abstract(expression);
      return !value.unknown && value.values.every((v) => !v.instance && !v.receiver);
    };
    for (const declaration of declarations) {
      const root = ts.isBindingElement(declaration) ? patternRoot(declaration) : declaration;
      if (root.initializer && !identified(root.initializer)) return true;
    }
    const examined = this.examine(symbol);
    if (examined.refused.length) return true;
    for (const node of examined.nodes) {
      if (!rolesOf(node).includes('write')) continue;
      let current = node;
      while (ts.isParenthesizedExpression(current.parent)) current = current.parent;
      const outer = current.parent;
      if (!ts.isBinaryExpression(outer) || outer.left !== current || outer.operatorToken.kind !== K.EqualsToken || !identified(outer.right)) {
        return true;
      }
    }
    return false;
  }

  // In `for (let [key, value] of Object.entries({a: ..., b: ...}))`, key can be a or b. The
  // pattern must have exactly two identifiers without defaults or rest elements, and the key binding
  // may only be written by that loop head. Array iteration is used both by for-of and by destructuring
  // each pair, so its operations are dependencies of the proof as well as Object.entries.
  entriesKeys(identifier, scan) {
    if (!ts.isIdentifier(identifier)) return null;
    const symbol = this.resolve(identifier);
    if (!symbol) return null;
    const declarations = declarationsIn(symbol, this.sourceFile);
    if (declarations.length !== 1) return null;
    const element = declarations[0];
    if (!ts.isBindingElement(element) || !ts.isArrayBindingPattern(element.parent)) return null;
    const pattern = element.parent;
    if (pattern.elements.length !== 2 || pattern.elements[0] !== element || pattern.elements.some((part) =>
      !ts.isBindingElement(part) || !ts.isIdentifier(part.name) || part.initializer || part.dotDotDotToken)) return null;
    const declaration = pattern.parent;
    const list = declaration.parent;
    const loop = list.parent;
    if (!ts.isVariableDeclaration(declaration) || !ts.isVariableDeclarationList(list) || list.declarations.length !== 1 ||
        !ts.isForOfStatement(loop) || loop.initializer !== list || loop.awaitModifier) return null;
    const examined = this.examine(symbol);
    if (examined.refused.length || examined.occurrences.some((occurrence) => occurrence.roles.includes('write') &&
        occurrence.s !== start(element.name, this.sourceFile))) return null;
    let call = loop.expression;
    while (ts.isParenthesizedExpression(call)) call = call.expression;
    if (!ts.isCallExpression(call) || call.questionDotToken || call.arguments.length !== 1) return null;
    const callee = call.expression;
    if (!ts.isPropertyAccessExpression(callee) || callee.name.text !== 'entries' || callee.questionDotToken ||
        !ts.isIdentifier(callee.expression) || callee.expression.text !== 'Object' || !scan.isFree(callee.expression)) return null;
    let object = call.arguments[0];
    while (ts.isParenthesizedExpression(object)) object = object.expression;
    if (!ts.isObjectLiteralExpression(object)) return null;
    const keys = [];
    for (const property of object.properties) {
      if (ts.isSpreadAssignment(property)) return null;
      const key = memberKey(property.name);
      if (key === undefined) return null;
      if (key === '__proto__' && ts.isPropertyAssignment(property) && !ts.isComputedPropertyName(property.name)) continue;
      keys.push(key);
    }
    return { keys: [...new Set(keys)], requires: ['Object.entries', 'Array.prototype.@@iterator', '%ArrayIteratorPrototype%.next'] };
  }

  // Whether an identifier is a variable the binding model accepts, with one declaration whose
  // initializer is a call of the built-in Symbol.for, and is not written afterwards.
  holdsRegistrySymbol(identifier, scan) {
    const symbol = this.resolve(identifier);
    if (!symbol) return false;
    const { refused, occurrences } = this.examine(symbol);
    const declarations = declarationsIn(symbol, this.sourceFile);
    if (refused.length || declarations.length !== 1 || !ts.isVariableDeclaration(declarations[0]) || !declarations[0].initializer) {
      return false;
    }
    if (occurrences.some((o) => o.roles.includes('write'))) return false;
    let initializer = declarations[0].initializer;
    while (ts.isParenthesizedExpression(initializer)) initializer = initializer.expression;
    return ts.isCallExpression(initializer) && !initializer.questionDotToken && scan.registry(initializer);
  }

  // The uses of a binding's value in this module under the operation model. query: {id, model: {kind,
  // keys, accessors}, mode}, with mode one of
  //   value (the default)  id is the binding that holds the value;
  //   namespace            id is a namespace that holds the value under the export names in names;
  //   parameter            id is a function that another module imports and calls with the value as
  //                        the argument at position, depth calls from the value's binding.
  uses(query) {
    const ctx = new UseContext(query.model);
    const mode = query.mode ?? 'value';
    if (!['value', 'namespace', 'parameter'].includes(mode)) throw new RequestError(`a uses mode is value, namespace or parameter, not ${mode}`);
    const symbol = this.symbolOfId(query.id);
    if (mode === 'parameter') {
      for (const name of ['position', 'depth']) {
        if (!Number.isInteger(query[name]) || query[name] < 0) throw new RequestError(`a parameter use needs ${name}, a whole number`);
      }
      const declaration = declarationsIn(symbol, this.sourceFile)[0];
      const site = declaration ? declarationName(declaration) : this.sourceFile;
      this.enterFunction(symbol, ts.isIdentifier(site) ? site.text : symbol.name, query.position, site, ctx, query.depth);
    } else {
      ctx.queue.push({ id: query.id, mode, names: new Set(query.names ?? []), depth: 0, root: mode === 'value', declaration: null });
    }
    this.drainUses(ctx);
    return ctx.result();
  }

  // The uses of the namespace that the import() or import.meta.require call at handle loads, which
  // holds the value under names.
  loadUse(query) {
    const ctx = new UseContext(query.model);
    const node = nodeAt(this.sourceFile, query.handle);
    if (!ts.isCallExpression(node)) throw new RequestError(`handle ${query.handle} is not a call`);
    const names = new Set(query.names ?? []);
    if (node.expression.kind === K.ImportKeyword) this.importPromiseUse(node, names, ctx);
    else this.namespaceUse(node, names, ctx, 0);
    this.drainUses(ctx);
    return ctx.result();
  }

  refuseUse(ctx, node, reason) {
    ctx.refused.push({ s: start(node, this.sourceFile), e: node.end, reason });
  }

  // Classifies the occurrences of every binding in the queue: the root binding, aliases, parameters
  // and namespace variables. The entry with the fewest calls goes first, so a binding the value reaches
  // on several routes is classified at the depth of the route with the fewest calls.
  drainUses(ctx) {
    while (ctx.queue.length) {
      let next = 0;
      for (let i = 1; i < ctx.queue.length; i += 1) if (ctx.queue[i].depth < ctx.queue[next].depth) next = i;
      const [entry] = ctx.queue.splice(next, 1);
      const key = `${entry.mode} ${entry.id} ${[...entry.names].sort().join(',')}`;
      if (ctx.visited.has(key)) continue;
      ctx.visited.add(key);
      const symbol = this.symbolOfId(entry.id);
      const examined = this.examine(symbol);
      if (examined.refused.length) {
        for (const item of examined.refused) ctx.refused.push(item);
        continue;
      }
      ctx.followed.push({ id: entry.id, mode: entry.mode, depth: entry.depth });
      for (const declaration of declarationsIn(symbol, this.sourceFile)) {
        const root = ts.isBindingElement(declaration) ? patternRoot(declaration) : declaration;
        if (!exportedByModifier(root)) continue;
        const name = declarationName(declaration);
        if (entry.mode === 'namespace') this.refuseUse(ctx, name, 'a namespace that holds the value is exported');
        else ctx.exports.push({ name: name.text, s: start(name, this.sourceFile) });
      }
      // The root binding has one declaration that assigns it, the one pfg.values evaluated; an alias
      // or a parameter is assigned only where the value reaches it.
      const assigning = [];
      for (const node of examined.nodes) {
        const roles = rolesOf(node);
        if (isDeclarationName(node)) {
          if (roles.includes('write')) this.refuseUse(ctx, node, 'a loop head assigns the binding on every iteration');
          else if (roles.includes('initialized-declaration') && node.parent !== entry.declaration) assigning.push(node);
          continue;
        }
        if (roles.includes('write')) {
          this.refuseUse(ctx, node, 'the binding is written here');
          continue;
        }
        ctx.classified += 1;
        if (ctx.model.kind === 'primitive') continue;
        if (entry.mode === 'namespace') this.namespaceUse(node, entry.names, ctx, entry.depth);
        else this.valueUse(node, ctx, entry.depth);
      }
      for (const node of assigning.slice(entry.root ? 1 : 0)) {
        this.refuseUse(ctx, node, 'another declaration also assigns the binding');
      }
    }
  }

  // Follows an expression whose value is the value, up through the parents that hand it on.
  valueUse(expression, ctx, depth) {
    const { kind } = ctx.model;
    let current = expression;
    for (;;) {
      const outer = current.parent;
      if (!outer) return;
      if (ts.isParenthesizedExpression(outer)) {
        current = outer;
        continue;
      }
      if (ts.isBinaryExpression(outer)) {
        const op = outer.operatorToken.kind;
        if (op === K.BarBarToken || op === K.AmpersandAmpersandToken || op === K.QuestionQuestionToken) {
          current = outer;
          continue;
        }
        if (op === K.CommaToken) {
          if (outer.right !== current) return;
          current = outer;
          continue;
        }
        if (op === K.EqualsEqualsEqualsToken || op === K.ExclamationEqualsEqualsToken) return;
        if (op === K.InKeyword) {
          if (outer.right === current) return;
          return this.convertUse(current, ctx);
        }
        if (op === K.InstanceOfKeyword) {
          const constructor = outer.left === current ? this.freeBuiltinPath(outer.right) : null;
          if (constructor && INSTANCEOF_CONSTRUCTORS.has(constructor)) {
            ctx.require(`${constructor}.@@hasInstance`, 'Function.prototype.@@hasInstance');
            return;
          }
          return this.refuseUse(ctx, current, 'instanceof calls the Symbol.hasInstance method of its right operand with the value');
        }
        if (op === K.EqualsToken && outer.right === current) {
          const target = unparenthesized(outer.left);
          if (ts.isObjectLiteralExpression(target)) this.destructureUse(target, current, ctx);
          else if (ts.isArrayLiteralExpression(target)) this.iterateUse(current, ctx);
          else return this.refuseUse(ctx, current, 'the value is stored by an assignment');
          current = outer;
          continue;
        }
        if (BINARY_ASSIGNMENTS.has(op)) {
          return this.refuseUse(ctx, current, outer.left === current ? 'the value is the target of an assignment'
            : 'the value is an operand of a compound assignment');
        }
        return this.convertUse(current, ctx);
      }
      if (ts.isConditionalExpression(outer)) {
        if (outer.condition === current) return;
        current = outer;
        continue;
      }
      if (ts.isAwaitExpression(outer)) {
        if (kind === 'object' && ctx.model.keys.includes('then')) {
          return this.refuseUse(ctx, current, 'await calls the value\'s own then method');
        }
        ctx.require(...PROTOTYPE_CHAINS[kind].map((prototype) => `${prototype}.then`));
        current = outer;
        continue;
      }
      if (ts.isTypeOfExpression(outer) || ts.isVoidExpression(outer)) return;
      if (ts.isPrefixUnaryExpression(outer)) {
        if (outer.operator === K.ExclamationToken) return;
        if (outer.operator === K.PlusPlusToken || outer.operator === K.MinusMinusToken) {
          return this.refuseUse(ctx, current, 'the value is the operand of ++ or --');
        }
        return this.convertUse(current, ctx);
      }
      if (ts.isPostfixUnaryExpression(outer)) return this.refuseUse(ctx, current, 'the value is the operand of ++ or --');
      if (ts.isDeleteExpression(outer)) return this.refuseUse(ctx, current, 'the value is the operand of delete');
      if (ts.isExpressionStatement(outer)) return;
      if ((ts.isIfStatement(outer) || ts.isWhileStatement(outer) || ts.isDoStatement(outer)) && outer.expression === current) return;
      if (ts.isForStatement(outer) && outer.condition === current) return;
      if ((ts.isSwitchStatement(outer) || ts.isCaseClause(outer)) && outer.expression === current) return;
      if (ts.isTemplateSpan(outer)) {
        if (ts.isTaggedTemplateExpression(outer.parent.parent)) {
          return this.refuseUse(ctx, current, 'the value is passed to a template tag');
        }
        return this.convertUse(current, ctx);
      }
      if (ts.isComputedPropertyName(outer)) return this.convertUse(current, ctx);
      if (ts.isElementAccessExpression(outer) && outer.argumentExpression === current) return this.convertUse(current, ctx);
      if ((ts.isPropertyAccessExpression(outer) || ts.isElementAccessExpression(outer)) && outer.expression === current) {
        return this.memberUse(outer, ctx);
      }
      if (ts.isCallExpression(outer) && outer.expression === current) return this.refuseUse(ctx, current, 'the value is called');
      if (ts.isNewExpression(outer) && outer.expression === current) return this.refuseUse(ctx, current, 'the value is constructed');
      if (ts.isTaggedTemplateExpression(outer) && outer.tag === current) {
        return this.refuseUse(ctx, current, 'the value is called as a template tag');
      }
      if ((ts.isCallExpression(outer) || ts.isNewExpression(outer)) && outer.arguments?.includes(current)) {
        return this.argumentUse(outer, outer.arguments.indexOf(current), current, ctx, depth);
      }
      if (ts.isSpreadElement(outer)) {
        const holder = outer.parent;
        if (ts.isArrayLiteralExpression(holder) || ((ts.isCallExpression(holder) || ts.isNewExpression(holder)) &&
            holder.arguments?.includes(outer))) return this.iterateUse(current, ctx);
        return this.refuseUse(ctx, current, storeReason(outer, current));
      }
      if (ts.isSpreadAssignment(outer)) return this.copyUse(current, ctx);
      if (ts.isForOfStatement(outer) && outer.expression === current) {
        if (outer.awaitModifier) return this.refuseUse(ctx, current, 'for await calls the value\'s Symbol.asyncIterator');
        return this.iterateUse(current, ctx);
      }
      if (ts.isForInStatement(outer) && outer.expression === current) return;
      if (ts.isVariableDeclaration(outer) && outer.initializer === current) {
        if (ts.isIdentifier(outer.name)) return this.aliasUse(outer.name, outer, ctx, depth);
        if (ts.isObjectBindingPattern(outer.name)) return this.destructureUse(outer.name, current, ctx);
        return this.iterateUse(current, ctx);
      }
      if (ts.isExportSpecifier(outer)) {
        if (!outer.parent.parent.moduleSpecifier) ctx.exports.push({ name: outer.name.text, s: start(current, this.sourceFile) });
        return;
      }
      if (ts.isExportAssignment(outer)) {
        ctx.exports.push({ name: 'default', s: start(current, this.sourceFile) });
        return;
      }
      return this.refuseUse(ctx, current, storeReason(outer, current));
    }
  }

  // A conversion of the value to a primitive.
  convertUse(node, ctx) {
    const { kind, keys } = ctx.model;
    if (kind === 'object') {
      const hooks = CONVERSION_HOOKS.filter((name) => keys.includes(name));
      if (hooks.length) {
        return this.refuseUse(ctx, node, `converting the value to a primitive calls its own ${hooks.join(' or ')} with it as this`);
      }
    }
    for (const prototype of PROTOTYPE_CHAINS[kind]) ctx.require(`${prototype}.@@toPrimitive`, `${prototype}.toString`, `${prototype}.valueOf`);
    if (kind === 'array') ctx.require('Array.prototype.join');
  }

  // A member of the value, read or called.
  memberUse(access, ctx) {
    if (isWriteTarget(access)) return this.refuseUse(ctx, access, 'a member of the value is written');
    const key = ts.isPropertyAccessExpression(access) ? access.name.text : literalKey(access.argumentExpression);
    const callee = outermostParentheses(access);
    if (ts.isCallExpression(callee.parent) && callee.parent.expression === callee) return this.methodUse(key, callee.parent, access, ctx);
    if (ts.isTaggedTemplateExpression(callee.parent) && callee.parent.tag === callee) {
      return this.refuseUse(ctx, access, 'a member of the value is called as a template tag, with the value as this');
    }
    return this.readUse(key, access, ctx);
  }

  readUse(key, node, ctx) {
    const { kind, accessors } = ctx.model;
    if (kind === 'object') {
      if (key === undefined ? accessors.length > 0 : accessors.includes(key)) {
        return this.refuseUse(ctx, node, 'the read calls a getter of the value with the value as this');
      }
      return;
    }
    if ((kind === 'set' || kind === 'map') && key === 'size') ctx.require(`${CONTAINER_CONSTRUCTORS[kind]}.prototype.size`);
  }

  // A call of a member of the value, with the value as this.
  methodUse(key, call, access, ctx) {
    const { kind, keys } = ctx.model;
    const method = key === undefined ? undefined : READ_METHODS[kind].get(key);
    if (!method) {
      return this.refuseUse(ctx, access, key === undefined ? 'a computed member of the value is called with the value as this'
        : `${key} is called with the value as this, and it is not a read method of ${ARTICLES[kind]}`);
    }
    if (kind === 'object' && keys.includes(key)) {
      return this.refuseUse(ctx, access, `the value has its own ${key}, which the call runs with the value as this`);
    }
    ctx.require(`${CONTAINER_CONSTRUCTORS[kind]}.prototype.${key}`);
    if (method.species) ctx.require('Array.prototype.constructor', 'Array.@@species');
    if (key === 'concat') ctx.require('Array.prototype.@@isConcatSpreadable');
    if (method.container !== undefined) {
      const problem = callbackProblem(call.arguments[0], method.container);
      if (problem) this.refuseUse(ctx, call.arguments[0] ?? call, problem);
    }
  }

  iterateUse(node, ctx) {
    const { kind } = ctx.model;
    if (kind === 'object') return this.refuseUse(ctx, node, 'the value is iterated, and an object literal has no iterator');
    ctx.require(...ITERATION_REQUIRES[kind]);
  }

  // {...V}: the value's own enumerable members are read.
  copyUse(node, ctx) {
    if (ctx.model.kind === 'object' && ctx.model.accessors.length) {
      return this.refuseUse(ctx, node, 'copying the members calls the value\'s getters with the value as this');
    }
  }

  destructureUse(pattern, node, ctx) {
    const elements = ts.isObjectBindingPattern(pattern) ? pattern.elements : pattern.properties;
    for (const element of elements) {
      if (ts.isSpreadAssignment(element) || element.dotDotDotToken) {
        this.copyUse(node, ctx);
        continue;
      }
      let key;
      if (ts.isBindingElement(element)) key = element.propertyName ? memberKey(element.propertyName) : element.name.text;
      else if (ts.isShorthandPropertyAssignment(element)) key = element.name.text;
      else if (ts.isPropertyAssignment(element)) key = memberKey(element.name);
      this.readUse(key, node, ctx);
    }
  }

  // The dotted name of a free built-in an expression reads, such as Object.keys, or null.
  freeBuiltinPath(expression) {
    let current = unparenthesized(expression);
    const parts = [];
    while (ts.isPropertyAccessExpression(current) && !current.questionDotToken) {
      parts.unshift(current.name.text);
      current = unparenthesized(current.expression);
    }
    if (!ts.isIdentifier(current) || !BUILTINS.has(current.text) || GLOBAL_NAMES.has(current.text)) return null;
    if (this.idOf(this.resolve(current)) !== null || insideWith(current)) return null;
    return [current.text, ...parts].join('.');
  }

  // The value as the argument at position of a call or construction.
  argumentUse(call, position, node, ctx, depth) {
    const { kind, keys, accessors } = ctx.model;
    const args = call.arguments;
    if (args.slice(0, position).some(ts.isSpreadElement)) {
      return this.refuseUse(ctx, node, 'a spread argument before the value hides the position at which it is passed');
    }
    const path = this.freeBuiltinPath(call.expression);
    if (ts.isNewExpression(call)) {
      const requires = path && COPYING_CONSTRUCTORS.get(path);
      if (requires && position === 0 && args.length === 1 && kind !== 'object') {
        ctx.require(...requires);
        return this.iterateUse(node, ctx);
      }
      return this.refuseUse(ctx, node, 'the value is passed to a constructor the model does not list');
    }
    const reading = path ? READING_FUNCTIONS.get(path) : undefined;
    if (reading) {
      if (position !== 0 || args.length !== reading.argc) {
        return this.refuseUse(ctx, node, `the model lists ${path} only with the value as its ${reading.argc === 2 ? 'first of two' : 'only'} argument`);
      }
      ctx.require(path);
      if (kind === 'object' && reading.getters && accessors.length) {
        return this.refuseUse(ctx, node, `${path} reads every member, which calls the value's getters with it as this`);
      }
      if (kind === 'object' && reading.hook && keys.includes(reading.hook)) {
        return this.refuseUse(ctx, node, `${path} calls the value's own ${reading.hook} with it as this`);
      }
      if (reading.hook) ctx.require(...PROTOTYPE_CHAINS[kind].map((prototype) => `${prototype}.${reading.hook}`));
      if (reading.iterates) return this.iterateUse(node, ctx);
      if (reading.converts) return this.convertUse(node, ctx);
      return;
    }
    if (path) return this.refuseUse(ctx, node, `the value is passed to ${path}, a built-in function the model does not list`);
    return this.parameterUse(call, position, node, ctx, depth);
  }

  // The value passed to a function the module declares or imports.
  parameterUse(call, position, node, ctx, depth) {
    if (!ts.isCallExpression(call)) return this.refuseUse(ctx, node, 'the value is passed to a template tag');
    const callee = unparenthesized(call.expression);
    if (!ts.isIdentifier(callee)) return this.refuseUse(ctx, node, 'the value is passed to a function the model cannot identify');
    const symbol = this.resolve(callee);
    if (!symbol || this.idOf(symbol) === null) return this.refuseUse(ctx, node, `the value is passed to ${callee.text}, which the module does not declare`);
    const declarations = declarationsIn(symbol, this.sourceFile);
    if (declarations.length !== 1 || !(ts.isImportSpecifier(declarations[0]) || ts.isImportClause(declarations[0]))) {
      return this.enterFunction(symbol, callee.text, position, node, ctx, depth);
    }
    // An imported function: the module that declares it classifies the parameter's uses.
    const examined = this.examine(symbol);
    if (examined.refused.length) return this.refuseUse(ctx, node, `the value is passed to ${callee.text}, a binding the binding model refuses`);
    if (examined.occurrences.some((occurrence) => occurrence.roles.includes('write'))) {
      return this.refuseUse(ctx, node, `the value is passed to ${callee.text}, an import that is written`);
    }
    if (depth >= 4) return this.refuseUse(ctx, node, 'the value is passed on more than four calls deep');
    ctx.calls.push({ name: callee.text, id: this.idOf(symbol), position, depth, s: start(node, this.sourceFile), e: node.end });
  }

  // The value passed as the argument at position of a plain call of the function bound to symbol, depth
  // calls from the value's binding: the parameter that receives it is queued. Refusals are placed at site.
  enterFunction(symbol, name, position, site, ctx, depth) {
    const examined = this.examine(symbol);
    if (examined.refused.length) return this.refuseUse(ctx, site, `the value is passed to ${name}, a binding the binding model refuses`);
    const declarations = declarationsIn(symbol, this.sourceFile);
    let fn = declarations.length === 1 ? declarations[0] : undefined;
    if (fn && ts.isVariableDeclaration(fn)) fn = unparenthesized(fn.initializer);
    if (!fn || !(ts.isFunctionDeclaration(fn) || ts.isFunctionExpression(fn) || ts.isArrowFunction(fn))) {
      return this.refuseUse(ctx, site, `the value is passed to ${name}, which is not a function declaration or a variable holding a function`);
    }
    if (examined.occurrences.some((occurrence) => occurrence.roles.includes('write'))) {
      return this.refuseUse(ctx, site, `the value is passed to ${name}, a variable that is written`);
    }
    if (depth >= 4) return this.refuseUse(ctx, site, 'the value is passed on more than four calls deep');
    if (readsArguments(fn)) return this.refuseUse(ctx, site, `${name} reads arguments, which hold the value`);
    const rest = fn.parameters.findIndex((parameter) => parameter.dotDotDotToken);
    if (rest !== -1 && rest <= position) return this.refuseUse(ctx, site, `a rest parameter of ${name} receives the value`);
    if (position >= fn.parameters.length) return;
    const parameter = fn.parameters[position];
    if (!ts.isIdentifier(parameter.name)) {
      return this.refuseUse(ctx, site, `parameter ${position + 1} of ${name}, which receives the value, is a pattern`);
    }
    ctx.queue.push({ id: this.idOf(this.resolve(parameter.name)), mode: 'value', names: new Set(), depth: depth + 1,
      root: false, declaration: parameter });
  }

  aliasUse(name, declaration, ctx, depth) {
    const id = this.idOf(this.resolve(name));
    if (id === null) return this.refuseUse(ctx, name, 'the value is stored in a variable the module does not declare');
    ctx.queue.push({ id, mode: 'value', names: new Set(), depth, root: false, declaration });
  }

  // Follows an expression whose value is a namespace holding the value under names.
  namespaceUse(expression, names, ctx, depth) {
    let current = expression;
    for (;;) {
      const outer = current.parent;
      if (!outer) return;
      if (ts.isParenthesizedExpression(outer)) {
        current = outer;
        continue;
      }
      if ((ts.isPropertyAccessExpression(outer) || ts.isElementAccessExpression(outer)) && outer.expression === current) {
        const key = ts.isPropertyAccessExpression(outer) ? outer.name.text : literalKey(outer.argumentExpression);
        if (key === undefined) return this.refuseUse(ctx, outer, 'a computed member of a namespace that holds the value');
        if (isWriteTarget(outer)) return this.refuseUse(ctx, outer, 'a member of a namespace that holds the value is written');
        const callee = outermostParentheses(outer);
        if ((ts.isCallExpression(callee.parent) && callee.parent.expression === callee) ||
            (ts.isTaggedTemplateExpression(callee.parent) && callee.parent.tag === callee)) {
          return this.refuseUse(ctx, outer, 'a member of a namespace that holds the value is called with the namespace as this');
        }
        if (names.has(key)) return this.valueUse(outer, ctx, depth);
        return;
      }
      if (ts.isVariableDeclaration(outer) && outer.initializer === current) {
        if (ts.isIdentifier(outer.name)) {
          const id = this.idOf(this.resolve(outer.name));
          if (id === null) return this.refuseUse(ctx, outer.name, 'the namespace is stored in a variable the module does not declare');
          ctx.queue.push({ id, mode: 'namespace', names, depth, root: false, declaration: outer });
          return;
        }
        if (ts.isObjectBindingPattern(outer.name)) return this.namespacePattern(outer.name, names, ctx, depth);
        return this.refuseUse(ctx, current, 'a namespace that holds the value is destructured as an array');
      }
      if (ts.isExpressionStatement(outer) || ts.isTypeOfExpression(outer) || ts.isVoidExpression(outer)) return;
      if (ts.isPrefixUnaryExpression(outer) && outer.operator === K.ExclamationToken) return;
      if (ts.isBinaryExpression(outer) && [K.EqualsEqualsEqualsToken, K.ExclamationEqualsEqualsToken].includes(outer.operatorToken.kind)) return;
      if (ts.isAwaitExpression(outer)) return this.refuseUse(ctx, current, 'await calls a then member of a namespace that holds the value');
      return this.refuseUse(ctx, current, 'a namespace that holds the value is used outside the forms the model follows');
    }
  }

  // An object pattern that destructures a namespace holding the value under names.
  namespacePattern(pattern, names, ctx, depth) {
    for (const element of pattern.elements) {
      if (element.dotDotDotToken) return this.refuseUse(ctx, element, 'a rest element collects the members of a namespace that holds the value');
      const key = element.propertyName ? memberKey(element.propertyName) : ts.isIdentifier(element.name) ? element.name.text : undefined;
      if (key === undefined) return this.refuseUse(ctx, element, 'a computed key destructures a namespace that holds the value');
      if (!names.has(key)) continue;
      if (element.initializer && !isLiteralExpression(element.initializer)) {
        this.refuseUse(ctx, element, 'the member that holds the value has a default that runs code');
        continue;
      }
      if (!ts.isIdentifier(element.name)) {
        this.refuseUse(ctx, element, 'the member that holds the value is destructured further');
        continue;
      }
      const id = this.idOf(this.resolve(element.name));
      if (id === null) {
        this.refuseUse(ctx, element, 'the member that holds the value is bound to a name the module does not declare');
        continue;
      }
      ctx.queue.push({ id, mode: 'value', names: new Set(), depth, root: false, declaration: element });
    }
  }

  // A pattern that receives a namespace holding the value under names: a variable or an object pattern.
  namespaceTarget(element, names, ctx, depth) {
    if (element.initializer && !isLiteralExpression(element.initializer)) {
      return this.refuseUse(ctx, element, 'the namespace has a default that runs code');
    }
    if (ts.isIdentifier(element.name)) {
      const id = this.idOf(this.resolve(element.name));
      if (id === null) return this.refuseUse(ctx, element, 'the namespace is bound to a name the module does not declare');
      ctx.queue.push({ id, mode: 'namespace', names, depth, root: false, declaration: element });
      return;
    }
    if (ts.isObjectBindingPattern(element.name)) return this.namespacePattern(element.name, names, ctx, depth);
    return this.refuseUse(ctx, element, 'the namespace is destructured as an array');
  }

  // The promise from import(M), whose module's namespace holds the value under names.
  importPromiseUse(call, names, ctx) {
    const current = outermostParentheses(call);
    const outer = current.parent;
    if (ts.isAwaitExpression(outer)) return this.namespaceUse(outer, names, ctx, 0);
    if (ts.isExpressionStatement(outer)) return;
    if (ts.isPropertyAccessExpression(outer) && outer.expression === current && outer.name.text === 'then' &&
        ts.isCallExpression(outer.parent) && outer.parent.expression === outer) {
      return this.thenUse(outer.parent, (parameter) => this.namespaceTarget(parameter, names, ctx, 0), ctx);
    }
    if (ts.isArrayLiteralExpression(outer)) return this.promiseAllUse(outer, outer.elements.indexOf(current), names, ctx);
    return this.refuseUse(ctx, current, 'the promise of a namespace that holds the value is used outside the forms the model follows');
  }

  // promise.then(callback): receive is given the callback's first parameter.
  thenUse(call, receive, ctx) {
    const callback = unparenthesized(call.arguments[0]);
    if (!callback || !(ts.isArrowFunction(callback) || ts.isFunctionExpression(callback))) {
      return this.refuseUse(ctx, call, 'then is given a callback that is not an arrow or function expression written in the call');
    }
    if (ts.isFunctionExpression(callback) && readsArguments(callback)) {
      return this.refuseUse(ctx, callback, 'the callback reads arguments, which hold what the promise gives');
    }
    ctx.require('Promise.prototype.then');
    const first = callback.parameters[0];
    if (!first) return;
    if (first.dotDotDotToken) return this.refuseUse(ctx, first, 'a rest parameter receives what the promise gives');
    return receive(first);
  }

  // import(M) as the element at index of the array literal given to Promise.all.
  promiseAllUse(array, index, names, ctx) {
    if (array.elements.slice(0, index).some(ts.isSpreadElement)) {
      return this.refuseUse(ctx, array, 'a spread element before the import hides its position');
    }
    const all = outermostParentheses(array).parent;
    if (!ts.isCallExpression(all) || all.arguments[0] !== outermostParentheses(array) || this.freeBuiltinPath(all.expression) !== 'Promise.all') {
      return this.refuseUse(ctx, array, 'an array holding the promise of a namespace that holds the value is used outside Promise.all');
    }
    ctx.require('Promise.all', 'Promise.resolve', 'Promise.prototype.then', ...ITERATION_REQUIRES.array);
    const element = (pattern) => {
      for (const [position, item] of pattern.elements.entries()) {
        if (position > index) break;
        if (ts.isBindingElement(item) && item.dotDotDotToken) {
          return this.refuseUse(ctx, item, 'a rest element collects the namespace that holds the value');
        }
      }
      const item = pattern.elements[index];
      if (!item || ts.isOmittedExpression(item)) return;
      return this.namespaceTarget(item, names, ctx, 0);
    };
    const current = outermostParentheses(all);
    const outer = current.parent;
    if (ts.isAwaitExpression(outer)) {
      const awaited = outermostParentheses(outer);
      const holder = awaited.parent;
      if (ts.isExpressionStatement(holder)) return;
      if (ts.isVariableDeclaration(holder) && holder.initializer === awaited && ts.isArrayBindingPattern(holder.name)) return element(holder.name);
      return this.refuseUse(ctx, outer, 'the namespaces Promise.all gives are used outside the forms the model follows');
    }
    if (ts.isExpressionStatement(outer)) return;
    if (ts.isPropertyAccessExpression(outer) && outer.expression === current && outer.name.text === 'then' &&
        ts.isCallExpression(outer.parent) && outer.parent.expression === outer) {
      return this.thenUse(outer.parent, (parameter) => {
        if (parameter.initializer && !isLiteralExpression(parameter.initializer)) {
          return this.refuseUse(ctx, parameter, 'the namespaces have a default that runs code');
        }
        if (!ts.isArrayBindingPattern(parameter.name)) {
          return this.refuseUse(ctx, parameter, 'the array of namespaces Promise.all gives is not destructured');
        }
        return element(parameter.name);
      }, ctx);
    }
    return this.refuseUse(ctx, current, 'the promise Promise.all gives is used outside the forms the model follows');
  }

  // Every occurrence of a binding, declarations included, or {refused} for a binding the model refuses.
  occurrences(id) {
    const { occurrences, refused } = this.examine(this.symbolOfId(id));
    return refused.length ? { refused } : occurrences;
  }

  syntax(node, handle, depth) {
    const sourceFile = this.sourceFile;
    const result = { k: kindName(node), s: start(node, sourceFile), e: node.end, h: handle };
    if (ts.isIdentifier(node)) {
      Object.assign(result, { text: node.text }, this.reference(node));
      if (result.b) result.roles = rolesOf(node);
    } else if (ts.isPrivateIdentifier(node)) {
      result.text = node.text;
    } else if (ts.isStringLiteral(node)) {
      result.value = node.text;
    } else if (ts.isNoSubstitutionTemplateLiteral(node) || ts.isTemplateHead(node) || ts.isTemplateMiddle(node) ||
               ts.isTemplateTail(node)) {
      const value = cooked(node);
      if (value === undefined) Object.assign(result, { value: null, invalid: true });
      else result.value = value;
    } else if (ts.isNumericLiteral(node)) {
      const value = Number(node.text);
      Object.assign(result, { value: Number.isFinite(value) ? value : null, js: String(value),
        finite: Number.isFinite(value) });
    } else if (ts.isBigIntLiteral(node) || ts.isRegularExpressionLiteral(node)) {
      result.text = node.text;
    }
    // What a node holds outside its children: operators that are token kinds rather than child nodes,
    // the keyword of a declaration list, a meta property and a heritage clause, and `export =`.
    if (ts.isPrefixUnaryExpression(node) || ts.isPostfixUnaryExpression(node)) result.op = ts.tokenToString(node.operator);
    else if (ts.isBinaryExpression(node)) result.op = ts.tokenToString(node.operatorToken.kind);
    else if (ts.isVariableDeclarationList(node)) result.declares = declarationListKind(node);
    else if (ts.isMetaProperty(node)) result.keyword = ts.tokenToString(node.keywordToken);
    else if (ts.isHeritageClause(node)) result.token = ts.tokenToString(node.token);
    else if (ts.isExportAssignment(node)) result.exportEquals = Boolean(node.isExportEquals);
    // A call, member access or non-null assertion inside an optional chain: a?.b() short-circuits the
    // call although only the access has the ?. token.
    if (node.flags & ts.NodeFlags.OptionalChain) result.optionalChain = true;
    if (ts.isComputedPropertyName(node) || ts.isPropertyAssignment(node) || ts.isShorthandPropertyAssignment(node) ||
        ts.isMethodDeclaration(node) || ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node)) {
      const key = ts.isComputedPropertyName(node) ? literalKey(node.expression) : memberKey(node.name);
      if (key !== undefined) result.key = key;
    }
    const list = children(node);
    list.forEach((child, index) => {
      const childHandle = handle === '' ? String(index) : `${handle}.${index}`;
      const value = depth > 0 ? this.syntax(child, childHandle, depth - 1)
        : { k: kindName(child), s: start(child, sourceFile), e: child.end, h: childHandle, truncated: true };
      const { field, index: position } = fieldOf(node, child);
      if (position === undefined) result[field] = value;
      else {
        result[field] ??= new Array(node[field].length).fill(null);
        result[field][position] = value;
      }
    });
    return result;
  }

  find(s, e, kind) {
    const found = [];
    const sourceFile = this.sourceFile;
    const visit = (node, handle) => {
      const nodeStart = start(node, sourceFile);
      if (nodeStart === s && node.end === e && kindName(node) === kind) found.push(handle);
      children(node).forEach((child, index) => {
        if (child.pos <= s && child.end >= e) visit(child, handle === '' ? String(index) : `${handle}.${index}`);
      });
    };
    visit(sourceFile, '');
    return found;
  }

  // The node at handle as TypeScript prints it, without comments, after renaming every binding to a
  // name that a source identifier cannot have, since ‹ and › cannot occur in a JavaScript identifier: the
  // parameters declared inside the node ‹p0›, ‹p1›, ... and its other declarations ‹l0›, ... in the order
  // they are declared; the bindings that roles names (binding id to role) ‹ROLE›; other bindings of the
  // module ‹m0›, ... in the order they are first used. A role is upper case, so it never spells a
  // generated name. Free names, property names and literals stay as written, and so do the public names
  // of a module: an import specifier is written out as `a as ‹l0›`, a local export specifier as
  // `‹l0› as a`, a re-export and an `export * as` name stay, and a declaration exported by an export
  // modifier keeps its name. Shorthand properties and object binding elements are written out as
  // key: value (with their defaults), so a key is never renamed with the binding it holds. Two texts are
  // equal when the nodes have the same syntax, the same literals, free names and public names, and the
  // same pattern of bindings; others lists the binding ids behind ‹m0›, ‹m1›, ..., for a caller that
  // needs to know which bindings they are. A node that holds a direct eval call or a with statement, or
  // that names a binding the binding model refuses, gives {refused} instead.
  canonical(handle, roles) {
    for (const role of Object.values(roles)) {
      if (typeof role !== 'string' || !/^[A-Z][A-Z0-9_]*$/.test(role)) {
        throw new RequestError(`a role is an upper-case name such as SOURCE, not ${JSON.stringify(role)}`);
      }
    }
    const sourceFile = this.sourceFile;
    const node = nodeAt(sourceFile, handle);
    const refused = [];
    for (const site of this.dynamicScopes()) {
      if (contains(node, site.node)) {
        refused.push({ s: start(site.node, sourceFile), e: site.node.end,
          reason: 'the text holds a direct eval call or a with statement, which can reach its bindings by name' });
      }
    }
    const names = new Map();
    const otherIds = [];
    let parameters = 0;
    let locals = 0;
    const admit = (symbol) => refused.push(...this.examine(symbol).refused);
    // Declares each name an identifier or pattern binds, naming it with generate(identifier).
    const declare = (name, generate) => {
      if (ts.isIdentifier(name)) {
        const symbol = this.resolve(name);
        if (symbol && !names.has(symbol)) {
          admit(symbol);
          names.set(symbol, generate(name));
        }
      } else if (ts.isObjectBindingPattern(name) || ts.isArrayBindingPattern(name)) {
        for (const element of name.elements) if (ts.isBindingElement(element)) declare(element.name, generate);
      }
    };
    const parameter = () => `‹p${parameters++}›`;
    const local = () => `‹l${locals++}›`;
    const localOf = (declaration) => (exportedByModifier(declaration) ? (name) => name.text : local);
    const collect = (current) => {
      if (ts.isParameter(current)) declare(current.name, parameter);
      else if (ts.isVariableDeclaration(current)) declare(current.name, localOf(current));
      else if ((ts.isFunctionDeclaration(current) || ts.isClassDeclaration(current) || ts.isFunctionExpression(current) ||
                ts.isClassExpression(current)) && current !== node && current.name) declare(current.name, localOf(current));
      else if ((ts.isImportSpecifier(current) || ts.isImportClause(current) || ts.isNamespaceImport(current)) && current.name) {
        declare(current.name, local);
      }
      ts.forEachChild(current, collect);
    };
    collect(node);
    const rename = (identifier) => {
      const symbol = this.resolve(identifier);
      if (!symbol) return identifier.text;
      if (names.has(symbol)) return names.get(symbol);
      const id = this.idOf(symbol);
      if (id === null) return identifier.text;
      admit(symbol);
      const name = Object.hasOwn(roles, id) ? `‹${roles[id]}›` : `‹m${otherIds.length}›`;
      if (!Object.hasOwn(roles, id)) otherIds.push(id);
      names.set(symbol, name);
      return name;
    };
    const transformer = (context) => (root) => {
      const visit = (current) => {
        if (ts.isShorthandPropertyAssignment(current)) {
          // {x} and {x = 1} (the second only as an assignment pattern) become {x: ‹p0›} and {x: ‹p0› = 1}.
          const value = ts.factory.createIdentifier(rename(current.name));
          const initializer = current.objectAssignmentInitializer;
          return ts.factory.createPropertyAssignment(current.name.text,
            initializer ? ts.factory.createAssignment(value, ts.visitNode(initializer, visit)) : value);
        }
        if (ts.isBindingElement(current) && ts.isObjectBindingPattern(current.parent) && !current.propertyName &&
            ts.isIdentifier(current.name)) {
          return ts.factory.createBindingElement(current.dotDotDotToken, current.dotDotDotToken ? undefined : current.name.text,
            ts.factory.createIdentifier(rename(current.name)), current.initializer && ts.visitNode(current.initializer, visit));
        }
        if (ts.isImportSpecifier(current) && !current.propertyName) {
          return ts.factory.createImportSpecifier(current.isTypeOnly, ts.factory.createIdentifier(current.name.text),
            ts.factory.createIdentifier(rename(current.name)));
        }
        if (ts.isExportSpecifier(current) && !current.propertyName && !current.parent.parent.moduleSpecifier) {
          return ts.factory.createExportSpecifier(current.isTypeOnly, ts.factory.createIdentifier(rename(current.name)),
            current.name.text);
        }
        if (ts.isIdentifier(current) && !isPropertyName(current)) return ts.factory.createIdentifier(rename(current));
        return ts.visitEachChild(current, visit, context);
      };
      return ts.visitNode(root, visit);
    };
    const result = ts.transform(node, [transformer]);
    try {
      if (refused.length) {
        const seen = new Set();
        return { refused: refused.filter((r) => !seen.has(`${r.s}:${r.reason}`) && seen.add(`${r.s}:${r.reason}`)) };
      }
      const printer = ts.createPrinter({ removeComments: true, newLine: ts.NewLineKind.LineFeed });
      return { text: printer.printNode(ts.EmitHint.Unspecified, result.transformed[0], sourceFile), others: otherIds };
    } finally {
      result.dispose();
    }
  }
}

// Whether a declaration is exported by an export modifier, other than as the default export: its name
// is then the module's public name for it.
function exportedByModifier(declaration) {
  const holder = ts.isVariableDeclaration(declaration) ? declaration.parent?.parent : declaration;
  const modifiers = holder && ts.canHaveModifiers(holder) ? ts.getModifiers(holder) ?? [] : [];
  return modifiers.some((m) => m.kind === K.ExportKeyword) && !modifiers.some((m) => m.kind === K.DefaultKeyword);
}

// The roles of an identifier that refers to a binding, several at once.
function rolesOf(node) {
  const roles = [];
  const parent = node.parent;
  const declared = parent && (ts.isVariableDeclaration(parent) || ts.isParameter(parent) || ts.isBindingElement(parent) ||
    ts.isFunctionDeclaration(parent) || ts.isFunctionExpression(parent) || ts.isClassDeclaration(parent) ||
    ts.isClassExpression(parent) || ts.isImportSpecifier(parent) || ts.isImportClause(parent) ||
    ts.isNamespaceImport(parent)) && parent.name === node;
  if (declared) {
    roles.push('declaration');
    const assigned = declarationAssigns(parent);
    if (assigned === 'for-target') roles.push('write', 'for-target');
    else if (assigned) roles.push('initialized-declaration');
    if (ts.isImportSpecifier(parent) || ts.isImportClause(parent) || ts.isNamespaceImport(parent)) roles.push('import-specifier');
    return roles;
  }
  if (parent && ts.isExportSpecifier(parent)) return ['export-specifier'];
  if (parent && ts.isShorthandPropertyAssignment(parent) && parent.name === node) roles.push('shorthand-value');
  let expression = node;
  while (expression.parent && ts.isParenthesizedExpression(expression.parent)) expression = expression.parent;
  const outer = expression.parent;
  if (outer && ts.isTypeOfExpression(outer)) roles.push('typeof-operand');
  if (outer && ts.isDeleteExpression(outer)) roles.push('delete-operand');
  const write = writeOf(expression);
  if (write === 'assign' || write === 'for-target' || write === 'destructuring-target') roles.push('write', write);
  else if (write === 'compound' || write === 'update' || write === 'logical') roles.push('read', 'write', write);
  else roles.push('read');
  return roles;
}

// Whether a declaration gives its name a value: "for-target" for the head of a for-in or for-of loop,
// which assigns it on every iteration; true for a variable with an initializer, an element of a pattern
// whose declaration has one, an element with a default, a catch clause's variable, and a function or
// class, whose declaration is its value; false for a plain `var V`, a parameter, which a call gives its
// value, and an import, which is bound to another module's export.
function declarationAssigns(declaration) {
  if (ts.isFunctionDeclaration(declaration) || ts.isClassDeclaration(declaration) ||
      ts.isFunctionExpression(declaration) || ts.isClassExpression(declaration)) return true;
  let node = declaration;
  let defaulted = false;
  while (ts.isBindingElement(node)) {
    if (node.initializer) defaulted = true;
    node = node.parent.parent;
  }
  if (!ts.isVariableDeclaration(node)) return false;
  const list = node.parent;
  if (ts.isCatchClause(list)) return true;
  if (ts.isVariableDeclarationList(list) && (ts.isForInStatement(list.parent) || ts.isForOfStatement(list.parent)) &&
      list.parent.initializer === list) return 'for-target';
  return Boolean(node.initializer) || defaulted;
}

// How an expression is written to, when it is the target of an assignment, an update, a for-in/of head
// or a destructuring assignment; undefined when it is only read.
function writeOf(expression) {
  const parent = expression.parent;
  if (!parent) return undefined;
  if (ts.isBinaryExpression(parent) && parent.left === expression && BINARY_ASSIGNMENTS.has(parent.operatorToken.kind)) {
    const kind = parent.operatorToken.kind;
    if (kind === K.EqualsToken) return isDestructuringTarget(parent) ? 'destructuring-target' : 'assign';
    if (kind === K.BarBarEqualsToken || kind === K.AmpersandAmpersandEqualsToken || kind === K.QuestionQuestionEqualsToken) {
      return 'logical';
    }
    return 'compound';
  }
  if ((ts.isPrefixUnaryExpression(parent) || ts.isPostfixUnaryExpression(parent)) &&
      (parent.operator === K.PlusPlusToken || parent.operator === K.MinusMinusToken)) return 'update';
  if ((ts.isForInStatement(parent) || ts.isForOfStatement(parent)) && parent.initializer === expression) return 'for-target';
  if (isDestructuringTarget(expression)) return 'destructuring-target';
  return undefined;
}

// Whether a node lies in the pattern of a destructuring assignment, as an element, a property's value, a
// shorthand name or a spread target.
function isDestructuringTarget(node) {
  let current = node;
  for (;;) {
    const parent = current.parent;
    if (!parent) return false;
    if (ts.isArrayLiteralExpression(parent) || ts.isSpreadElement(parent) || ts.isSpreadAssignment(parent) ||
        (ts.isPropertyAssignment(parent) && parent.initializer === current) ||
        (ts.isShorthandPropertyAssignment(parent) && parent.name === current) || ts.isParenthesizedExpression(parent) ||
        (ts.isBinaryExpression(parent) && parent.operatorToken.kind === K.EqualsToken && parent.left === current &&
         (ts.isArrayLiteralExpression(parent.parent) || ts.isPropertyAssignment(parent.parent)))) {
      current = parent;
      continue;
    }
    if (ts.isObjectLiteralExpression(parent)) {
      current = parent;
      continue;
    }
    if (current === node) return false;
    if (ts.isBinaryExpression(parent) && parent.operatorToken.kind === K.EqualsToken && parent.left === current &&
        (ts.isArrayLiteralExpression(current) || ts.isObjectLiteralExpression(current))) return true;
    if ((ts.isForInStatement(parent) || ts.isForOfStatement(parent)) && parent.initializer === current &&
        (ts.isArrayLiteralExpression(current) || ts.isObjectLiteralExpression(current))) return true;
    return false;
  }
}

// The facts of the module asked about last. Building a module's program is the slow part of a batch
// (1.5 to 2 s for the largest 2.1.304 module), and an extraction asks about the same module in several
// batches in a row; one program is kept, never more.
let lastFacts = null;

// The queries of a facts request, each answered in order:
//   node {handle, depth}     the syntax tree at a handle, children below depth marked truncated
//   find {s, e, kind}        the handles of the nodes of that kind with exactly that span
//   binding {handle}         the binding record of the identifier at a handle, or {free}/{role}
//   bindingAt {s, e}         the same for the one identifier with exactly that span
//   record {id}              the record of a binding id
//   scope {name}             the module-scope binding of a name, or {free}
//   occurrences {id}         every occurrence of a binding, with roles and chain
//   canonical {handle, roles}  the node's text with bindings renamed, and the ids behind ‹m0›, ...
//   text {s, e}              a slice of the module's text
//   comments                 the comments before the module's first token
//   premise {seeds}          the built-in scan with the module's bindings (Facts.premise)
//   uses {id, model, mode, names, position, depth}  the uses of a value under the operation model (Facts.uses)
//   loadUse {handle, model, names} the uses of a namespace from an import() or import.meta.require call
function factsBatch(request) {
  const module = moduleOf(request.module);
  if (lastFacts?.module !== module) {
    lastFacts = null;
    if (global.gc) global.gc();
    lastFacts = new Facts(module);
  }
  const facts = lastFacts;
  const results = [];
  for (const query of request.queries ?? []) {
    switch (query.q) {
      case 'node':
        results.push(facts.syntax(nodeAt(facts.sourceFile, query.handle), query.handle, query.depth ?? 0));
        break;
      case 'find':
        results.push(facts.find(query.s, query.e, query.kind));
        break;
      case 'binding': {
        const node = nodeAt(facts.sourceFile, query.handle);
        if (!ts.isIdentifier(node)) throw new RequestError(`the node at handle ${query.handle} is not an identifier`);
        const reference = facts.reference(node);
        results.push(reference.b ? facts.binding(reference.b) : reference);
        break;
      }
      case 'record':
        results.push(facts.binding(query.id));
        break;
      case 'scope':
        if (typeof query.name !== 'string') throw new RequestError('a scope query needs a name');
        results.push(facts.scopeBinding(query.name));
        break;
      case 'premise':
        results.push(facts.premise(query.seeds ?? []));
        break;
      case 'uses':
        results.push(facts.uses(query));
        break;
      case 'loadUse':
        results.push(facts.loadUse(query));
        break;
      case 'bindingAt': {
        const found = facts.find(query.s, query.e, 'Identifier');
        if (found.length !== 1) throw new RequestError(`span ${query.s}:${query.e} does not identify exactly one identifier in module ${module.index}`);
        const reference = facts.reference(nodeAt(facts.sourceFile, found[0]));
        results.push(reference.b ? facts.binding(reference.b) : reference);
        break;
      }
      case 'occurrences':
        results.push(facts.occurrences(query.id));
        break;
      case 'canonical':
        results.push(facts.canonical(query.handle, query.roles ?? {}));
        break;
      case 'text':
        if (!(Number.isInteger(query.s) && Number.isInteger(query.e) && query.s >= 0 && query.s <= query.e &&
              query.e <= module.text.length)) throw new RequestError(`span ${query.s}:${query.e} lies outside module ${module.index}`);
        results.push(module.text.slice(query.s, query.e));
        break;
      case 'comments':
        results.push((ts.getLeadingCommentRanges(module.text, 0) ?? []).map((range) => ({
          s: range.pos, e: range.end, text: module.text.slice(range.pos, range.end) })));
        break;
      default:
        throw new RequestError(`unknown query ${JSON.stringify(query.q)}`);
    }
  }
  return results;
}

// ---------------------------------------------------------------------------------------------------
// The protocol

function addModule(note) {
  if (!Number.isInteger(note.index) || note.index < 0 || typeof note.name !== 'string' || typeof note.text !== 'string') {
    throw new RequestError('a module needs a non-negative integer index, a name and a text');
  }
  if (modules.has(note.index)) throw new RequestError(`module ${note.index} was sent twice`);
  for (const module of modules.values()) {
    if (module.name === note.name) throw new RequestError(`two modules are named ${note.name}`);
  }
  modules.set(note.index, { index: note.index, name: note.name, text: note.text });
}

function answer(request) {
  if (deferredError) {
    const error = deferredError;
    deferredError = null;
    throw error;
  }
  switch (request.op) {
    case 'index':
      return index(request);
    case 'facts':
      return factsBatch(request);
    case 'vocabulary':
      return { operations: [...OPERATION_VOCABULARY].sort() };
    default:
      throw new RequestError(`unknown op ${JSON.stringify(request.op)}`);
  }
}

function write(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function failure(error) {
  if (error instanceof RequestError) return { kind: 'request', message: error.message };
  return { kind: 'internal', message: String(error?.message ?? error), detail: String(error?.stack ?? '') };
}

write({ hello: { protocol: PROTOCOL, helper: 'pfg-facts', revision: REVISION, node: process.version, typescript: ts.version } });
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  let message;
  try {
    message = JSON.parse(line);
  } catch (error) {
    deferredError = new RequestError(`a line is not JSON: ${error.message}`);
    continue;
  }
  if (message.id === undefined) {
    try {
      if (message.op === 'module') addModule(message);
      else throw new RequestError(`unknown notification ${JSON.stringify(message.op)}`);
    } catch (error) {
      deferredError ??= error;
    }
    continue;
  }
  try {
    write({ id: message.id, ok: true, result: answer(message) });
  } catch (error) {
    write({ id: message.id, ok: false, error: failure(error) });
  }
}
