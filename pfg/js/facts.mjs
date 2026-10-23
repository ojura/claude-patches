// The syntax and binding facts of the JavaScript modules of a Bun executable, for pfg/bundle.py.
//
// pfg.bundle starts this helper with `node --expose-gc facts.mjs`, streams it the modules that
// bun_handler read out of the binary, and asks it for facts. The helper parses each module on its own,
// with the TypeScript parser vendored in ./vendor/typescript, and never runs module code.
//
// Protocol: UTF-8 JSON, one value per line, on stdin and stdout; stdout carries nothing else.
//   - The first line the helper writes is {"hello": {"protocol": 1, "helper": "pfg-facts",
//     "revision": R, "node": ..., "typescript": ...}}, R being the number in pfg/REVISION as this
//     process read it.
//   - A request is {"id": n, "op": ..., ...}. Its reply is {"id": n, "ok": true, "result": ...} or
//     {"id": n, "ok": false, "error": {"kind": "request" | "internal", "message": ...}}: "request" when
//     the request names something absent or is malformed, "internal" for an exception in the helper.
//   - A line without an id is a notification and gets no reply. "module" is the one notification; an
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
// an ES module even when it holds no import or export. The parser applies the same rule through
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
  if (module === undefined) throw new RequestError(`there is no module ${index}`);
  return module;
}

function parse(module) {
  return ts.createSourceFile(fileName(module.index), module.text, PARSE_OPTIONS, true, ts.ScriptKind.JS);
}

// Release points: a full collection after a large module and after every 8 MB of parsed text, so that
// parse trees which are already garbage do not pile up before V8 grows its heap. --expose-gc makes
// global.gc available; it sets no limit.
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
    if (!/^\d+$/.test(part) || index >= list.length) throw new RequestError(`there is no node at handle ${handle}`);
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
// child's field is the first that holds it; fieldOf throws for a child in none of them, so a node kind
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
  throw new Error(`a ${kindName(child)} child of a ${kindName(parent)} is in none of the known fields`);
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
// Array.prototype.includes and the like) keeping their standard behaviour. The scan follows every value
// the program reaches from a built-in's name, and sorts each place a value goes into:
//   passed        its effect is known and cannot change a built-in: a call, a construction, a member
//                 read, a comparison, typeof, a test, a discarded value, a class's extends clause;
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
// Array.prototype. A write names its target as a path, such as Set.prototype.has; a key the scan
// cannot read is *, and a well-known symbol is @@iterator and the like.
//
// Values: a built-in's name (BUILTINS), the global object under any of its names (globalThis.Set is
// Set), and every static member read from a built-in value, normalized where the language fixes the
// result: X.prototype.constructor is X, the constructor of a built-in function is Function, the
// prototype of X.prototype is Object.prototype (Error.prototype for the native errors), the prototype
// of a built-in function is Function.prototype. The constructor and prototype of a literal are those of
// its kind: [].constructor is Array, the constructor of a function expression is Function, and that of
// an async function AsyncFunction. A value whose identity the scan cannot establish, such as
// x.constructor for an unknown x, is (object); its writes are prototypeWrites, calls of its constructor
// are dynamic code, and its other uses are counted only.
// A member read of a built-in the program has not written is not followed further when its result
// cannot be a built-in object: the scan follows the result of every read, and only the members that
// are definers, eval or the Function constructor change what a call does.

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
const UNKNOWN = '(object)';

const valueName = (value) => [value.root, ...value.path].join('.');

// The value of reading key from value, normalized where the language fixes the result.
function memberValue(value, key) {
  const { root, path } = value;
  if (root === 'globalThis' && path.length === 0) {
    if (BUILTINS.has(key) && !GLOBAL_NAMES.has(key)) return { root: key, path: [] };
    if (GLOBAL_NAMES.has(key)) return value;
    if (key === 'constructor' || key === '__proto__') return { root: UNKNOWN, path: [key] };
    return { root, path: [key], foreign: true };
  }
  if (value.foreign) return { root, path: [...path, key], foreign: true };
  if (root === UNKNOWN) return { root, path: [...path, key] };
  if (key === 'constructor') {
    if (path.at(-1) === 'prototype') return { root, path: path.slice(0, -1) };
    if (path.length === 0 && NAMESPACES.has(root)) return { root: 'Object', path: [] };
    if (path.length === 0 && CODE_RUNNERS.has(root) && root !== 'eval' && root !== 'Function') return { root: 'Function', path: [] };
    // Any other built-in value the scan follows is a function, whose constructor is Function, or a
    // prototype-less value whose .constructor the scan cannot name.
    return path.length === 0 || path.at(-1) !== '*' ? { root: 'Function', path: [] } : { root: UNKNOWN, path: ['constructor'] };
  }
  if (key === '__proto__') return prototypeOf(value);
  return { root, path: [...path, key] };
}

// The prototype of a built-in value, where the language fixes it.
function prototypeOf(value) {
  const { root, path } = value;
  if (root === UNKNOWN || value.foreign || (root === 'globalThis' && path.length === 0)) return { root: UNKNOWN, path: ['__proto__'] };
  if (path.length === 1 && path[0] === 'prototype') {
    if (root === 'Object') return { root: UNKNOWN, path: ['null'] };
    if (NATIVE_ERRORS.has(root)) return { root: 'Error', path: ['prototype'] };
    if (TYPED_ARRAYS.has(root)) return { root: '%TypedArray%', path: ['prototype'] };
    return { root: 'Object', path: ['prototype'] };
  }
  if (path.length === 0 && NAMESPACES.has(root)) return { root: 'Object', path: ['prototype'] };
  if (path.length === 0 && NATIVE_ERRORS.has(root)) return { root: 'Error', path: [] };
  if (path.length === 0 && TYPED_ARRAYS.has(root)) return { root: '%TypedArray%', path: [] };
  if (path.length === 0 || path.at(-1) !== '*') return { root: 'Function', path: ['prototype'] };
  return { root: UNKNOWN, path: ['__proto__'] };
}

// The constructor of a literal's kind, for literal.constructor, literal.__proto__ and getPrototypeOf.
function literalConstructor(node) {
  let current = node;
  while (ts.isParenthesizedExpression(current)) current = current.expression;
  if (ts.isArrayLiteralExpression(current)) return 'Array';
  if (ts.isObjectLiteralExpression(current)) return 'Object';
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
  if (ts.isClassExpression(current)) return 'Function';
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
  // resolve(identifier) gives the value an identifier holds when it is a built-in's name or a variable
  // the caller follows, else null; aliasOf(declarationName) records a variable that receives a value.
  // isFree(identifier) tells whether a name is a free name, not a variable of the module.
  constructor(sourceFile, resolve, handleOf, isFree) {
    this.sourceFile = sourceFile;
    this.resolve = resolve;
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

  // The key of a member access or of a definer's key argument: a literal key; @@name for a well-known
  // symbol read from the built-in Symbol; @registry for a symbol from the built-in Symbol.for, which
  // is never a built-in's key (registry); or * for any other expression.
  keyOf(expression) {
    const literal = literalKey(expression);
    if (literal !== undefined) return literal;
    if (this.registry(expression)) return '@registry';
    // A well-known symbol only when read from the free name Symbol itself: a variable holding one may
    // hold other keys as well.
    let current = expression;
    while (ts.isParenthesizedExpression(current)) current = current.expression;
    if (ts.isPropertyAccessExpression(current) && ts.isIdentifier(current.expression) && current.expression.text === 'Symbol' &&
        this.isFree(current.expression) && !['for', 'keyFor'].includes(current.name.text)) {
      return `@@${current.name.text}`;
    }
    return '*';
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

  // The value an expression evaluates to, if it is one the scan follows.
  valueOf(expression) {
    let current = expression;
    while (ts.isParenthesizedExpression(current) || ts.isNonNullExpression?.(current)) current = current.expression;
    if (ts.isIdentifier(current)) return isPropertyName(current) ? null : this.resolve(current);
    if (ts.isPropertyAccessExpression(current) || ts.isElementAccessExpression(current)) {
      const base = this.valueOf(current.expression);
      const key = ts.isPropertyAccessExpression(current) ? current.name.text : this.keyOf(current.argumentExpression);
      if (base) return memberValue(base, key);
      const literal = literalConstructor(current.expression);
      if (literal && (key === 'constructor' || key === '__proto__')) {
        return key === 'constructor' ? { root: literal, path: [] } : { root: literal, path: ['prototype'] };
      }
      if (key === 'constructor' || key === '__proto__') return { root: UNKNOWN, path: [key] };
      return null;
    }
    if (ts.isCallExpression(current) && current.arguments.length >= 1) {
      const callee = this.valueOf(current.expression);
      if (callee && PROTOTYPE_READERS.has(valueName(callee))) return this.prototypeOfArgument(current.arguments[0]);
      return null;
    }
    if (ts.isBinaryExpression(current)) {
      const op = current.operatorToken.kind;
      if (op === K.CommaToken) return this.valueOf(current.right);
      if (op === K.BarBarToken || op === K.AmpersandAmpersandToken || op === K.QuestionQuestionToken) {
        return this.valueOf(current.right) ?? this.valueOf(current.left);
      }
      if (op === K.EqualsToken) return this.valueOf(current.right);
      return null;
    }
    if (ts.isConditionalExpression(current)) return this.valueOf(current.whenTrue) ?? this.valueOf(current.whenFalse);
    if (ts.isAwaitExpression(current)) return this.valueOf(current.expression);
    return null;
  }

  prototypeOfArgument(argument) {
    const value = this.valueOf(argument);
    if (value) return prototypeOf(value);
    const literal = literalConstructor(argument);
    if (literal) return { root: literal, path: ['prototype'] };
    return { root: UNKNOWN, path: ['__proto__'] };
  }

  write(node, target, how) {
    const name = valueName(target);
    if (target.path.at(-1) === '@registry') {
      this.result.registryWrites.push({ ...this.where(node), target: name, how });
    } else if (target.root === UNKNOWN) {
      this.result.prototypeWrites.push({ ...this.where(node), target: name, how });
    } else if (target.foreign) {
      this.result.globalWrites.push({ ...this.where(node), key: target.path[0], how });
    } else if (target.root === 'globalThis' && target.path.length === 1 && target.path[0] === '*') {
      this.result.unknownKeys.push({ ...this.where(node), target: name, how });
    } else {
      this.result.writes.push({ ...this.where(node), target: name, how });
    }
  }

  // A write of key into value at node; keys gives several keys, ['*'] when they are not known.
  writeKeys(node, value, keys, how) {
    for (const key of keys) {
      const target = { root: value.root, path: [...value.path, key], foreign: value.foreign };
      // Writing a key of the global object that names a built-in replaces that built-in.
      if (value.root === 'globalThis' && value.path.length === 0 && !value.foreign) {
        if (key === '*' || key === '@registry') this.write(node, { root: 'globalThis', path: [key] }, how);
        else if (BUILTINS.has(key)) this.write(node, { root: key, path: [] }, how);
        else this.write(node, { root: 'globalThis', path: [key], foreign: true }, how);
      } else {
        this.write(node, target, how);
      }
    }
  }

  // The keys a definer call writes: its key argument, or the static keys of an object literal of
  // properties, or * when they are not known.
  definerKeys(kind, args) {
    if (kind === 'prototype') return ['__proto__'];
    if (kind === 'key') return [args[1] ? this.keyOf(args[1]) : '*'];
    const keys = [];
    for (const source of args.slice(1)) {
      let current = source;
      while (current && ts.isParenthesizedExpression(current)) current = current.expression;
      if (!current || !ts.isObjectLiteralExpression(current)) return ['*'];
      for (const property of current.properties) {
        if (ts.isSpreadAssignment(property)) return ['*'];
        const key = memberKey(property.name);
        keys.push(key === undefined ? '*' : key);
      }
    }
    return keys.length ? keys : [];
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
    const target = value.path.length ? { ...value, path: value.path.slice(0, -1) } : null;
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
          this.result.unfollowed.push({ ...this.where(call), value: inner, reason: 'a definer is bound, and the bound function is not followed' });
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
    }
    if (STATIC_DEFINERS.has(name)) {
      this.define(call, name, args, null);
      return;
    }
    if (target && RECEIVER_DEFINERS.has(last)) {
      // value is X.__defineGetter__: it defines key args[0] on X.
      this.writeKeys(call, target, [args[0] ? this.keyOf(args[0]) : '*'], last);
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
      const targetValue = receiver.args[0] ? this.valueOf(receiver.args[0]) : null;
      const list = receiver.apply ? receiver.args[1] : null;
      const keyNode = receiver.apply ? (list && ts.isArrayLiteralExpression(list) ? list.elements[0] : null) : receiver.args[1];
      if (targetValue) this.writeKeys(call, targetValue, [keyNode ? this.keyOf(keyNode) : '*'], name);
      else this.pass({ root: 'Object', path: ['prototype', name.split('.').at(-1)] }, 'definer');
      return;
    }
    const targetValue = definerArgs[0] ? this.valueOf(definerArgs[0]) : null;
    if (!targetValue) {
      this.pass({ root: name.split('.')[0], path: [name.split('.')[1]] }, 'definer on an object that is not a built-in');
      return;
    }
    this.writeKeys(call, targetValue, this.definerKeys(STATIC_DEFINERS.get(name), definerArgs), name);
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
          this.writeKeys(outer, value, [key], writeHow(outer));
          return;
        }
        let up = outer;
        while (up.parent && ts.isParenthesizedExpression(up.parent)) up = up.parent;
        const member = { ...memberValue(value, key) };
        if (ts.isCallExpression(up.parent) && up.parent.expression === up) {
          this.call(up, up.parent, member);
          if (this.followsResult(member)) {
            current = up.parent;
            value = this.resultOf(member, up.parent);
            if (!value) return;
            continue;
          }
          return;
        }
        if (ts.isTaggedTemplateExpression(up.parent) && up.parent.tag === up) return this.call(up, up.parent, member);
        if (ts.isNewExpression(up.parent) && up.parent.expression === up) return this.construct(up.parent, member);
        current = outer;
        value = member;
        continue;
      }
      if (ts.isCallExpression(outer) && outer.expression === current) {
        this.call(current, outer, value);
        if (this.followsResult(value)) {
          value = this.resultOf(value, outer);
          if (!value) return;
          current = outer;
          continue;
        }
        return;
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

  // An argument passed unchanged to a function, constructor or template tag, at position (a tag's
  // template object is at 0). Where a known definer receives it as its target, the definer's call
  // records the write instead of recording an escape too.
  argument(node, value, call, position) {
    const callee = this.valueOf(ts.isTaggedTemplateExpression(call) ? call.tag : call.expression);
    const calleeName = callee ? valueName(callee) : '';
    const callable = ts.isCallExpression(call) || ts.isTaggedTemplateExpression(call);
    const definerTarget = callable && callee && (
      (STATIC_DEFINERS.has(calleeName) && position === 0) ||
      (callee.path.at(-1) === 'call' && STATIC_DEFINERS.has(valueName({ ...callee, path: callee.path.slice(0, -1) })) && position === 1) ||
      (callee.path.at(-1) === 'call' && RECEIVER_DEFINERS.has(callee.path.at(-2)) && position === 0) ||
      (callee.path.at(-1) === 'apply' && RECEIVER_DEFINERS.has(callee.path.at(-2)) && position === 0));
    if (definerTarget && this.valueOf(node)) return;  // the definer's call records the write
    if (callable && callee && PROTOTYPE_READERS.has(calleeName) && position === 0) return;  // followed from the reader
    return this.escape(node, value, ts.isTaggedTemplateExpression(call) ? 'passed to a template tag'
      : ts.isNewExpression(call) ? 'passed to a constructor' : 'passed to a function');
  }

  // Whether a call of value gives a value the scan follows: the prototype readers' results.
  followsResult(value) {
    return PROTOTYPE_READERS.has(valueName(value));
  }

  resultOf(value, call) {
    if (PROTOTYPE_READERS.has(valueName(value))) return call.arguments[0] ? this.prototypeOfArgument(call.arguments[0]) : null;
    return null;
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

  escape(node, value, reason) {
    if (value.root === UNKNOWN) {
      this.result.unidentified += 1;
      return;
    }
    if (value.foreign) return;
    if (CODE_RUNNERS.has(value.root) && value.path.length === 0) {
      this.result.dynamic.push({ ...this.where(node), kind: `${value.root === 'eval' ? 'eval' : `the ${value.root} constructor`} ${reason}` });
      return;
    }
    this.result.unfollowed.push({ ...this.where(node), value: valueName(value), reason });
  }

  // A variable receives value: the caller follows its uses.
  reference(name, value, from) {
    if (value.root === UNKNOWN) this.result.unidentified += 1;
    if (value.foreign) return;
    this.result.references.push({ ...this.where(from), value: valueName(value), unknown: value.root === UNKNOWN || undefined,
      name: name.text, at: start(name, this.sourceFile) });
    this.referenceValues.push({ name, value });
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

// The value a built-in's name holds: the global object under any of its names, else the built-in.
function nameValue(name) {
  return GLOBAL_NAMES.has(name) ? { root: 'globalThis', path: [] } : { root: name, path: [] };
}

// Scans a module's occurrences of built-in names and its .constructor and .__proto__ reads, with
// resolve giving the value an identifier holds, or null.
function scanBuiltins(sourceFile, names, reaches, resolve, scan) {
  for (const node of names) {
    const value = resolve(node);
    if (value) scan.follow(node, value);
  }
  for (const node of reaches) {
    // A write to the object's own constructor or prototype, or a read a chain from a name already covers.
    if (isWriteTarget(node) || scan.valueOf(node.expression)) continue;
    scan.follow(node, scan.valueOf(node));
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
    loads: [], hits: {},
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
      // `export default function(){}` and `export default class{}` export a value no local name holds.
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
  const resolve = (identifier) => (BUILTINS.has(identifier.text) && !declared.has(identifier.text) &&
    !isPropertyName(identifier) && !isDeclarationName(identifier) ? nameValue(identifier.text) : null);
  record.premise = inSourceOrder(scanBuiltins(sourceFile, names, reaches, resolve,
    new BuiltinScan(sourceFile, resolve, handleOf, (identifier) => !declared.has(identifier.text))));
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
// other binding is refused: its record carries `refused`, a list of {s, e, reason}, and the queries that
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
// left as they are: following them would lead to TypeScript's unknown symbol, as no module is resolved.
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
    const refused = [];
    const refuse = (node, reason) => refused.push({ s: start(node, sourceFile), e: node.end, reason });
    for (const declaration of declarations) {
      if (!isModelledDeclaration(declaration)) {
        refuse(declaration, `it is declared by a ${kindName(declaration)}, which the binding model does not cover`);
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
    if (!symbol) throw new RequestError(`module ${this.module.index} has no binding ${id}`);
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
  // one the binding model refuses is unfollowed. seeds gives the values import bindings receive from
  // other modules' exports, as [{at, value}] with at the position of the import's local name and value
  // a path such as Set.prototype.
  premise(seeds) {
    const values = new Map();
    // The first pass finds what each variable holds; the second classifies every use with all of it
    // known, so that a definer called through a variable is seen at a target scanned before it.
    this.premisePass(seeds, values);
    return this.premisePass(seeds, values);
  }

  premisePass(seeds, values) {
    const sourceFile = this.sourceFile;
    const resolve = (identifier) => {
      if (isPropertyName(identifier) || isDeclarationName(identifier)) return null;
      const symbol = this.resolve(identifier);
      // TypeScript resolves globalThis to a symbol of its own, with no declaration in the module.
      if (!symbol || this.idOf(symbol) === null) {
        return BUILTINS.has(identifier.text) && !insideWith(identifier) ? nameValue(identifier.text) : null;
      }
      const held = values.get(symbol);
      return held && held.length === 1 ? held[0] : null;
    };
    const scan = new BuiltinScan(sourceFile, resolve, handleOf, (identifier) => this.idOf(this.resolve(identifier)) === null);
    scan.registryVariable = (identifier) => this.holdsRegistrySymbol(identifier, scan);
    const names = [];
    const reaches = [];
    const visit = (node) => {
      if (ts.isIdentifier(node) && BUILTINS.has(node.text) && !isPropertyName(node) && !isDeclarationName(node)) {
        if (insideWith(node) && this.idOf(this.resolve(node)) === null) {
          scan.result.unfollowed.push({ ...scan.where(node), value: node.text, reason: 'a with statement may give the name another value' });
        } else {
          names.push(node);
        }
      }
      if ((ts.isPropertyAccessExpression(node) && ['constructor', '__proto__'].includes(node.name.text)) ||
          (ts.isElementAccessExpression(node) && ['constructor', '__proto__'].includes(literalKey(node.argumentExpression)))) {
        reaches.push(node);
      }
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
    const pending = [];
    for (const seed of seeds ?? []) {
      const token = ts.getTokenAtPosition(sourceFile, seed.at);
      if (!ts.isIdentifier(token) || start(token, sourceFile) !== seed.at) throw new RequestError(`no identifier at ${seed.at}`);
      const [root, ...path] = String(seed.value).split('.');
      pending.push({ name: token, value: { root, path } });
    }
    scanBuiltins(sourceFile, names, reaches, resolve, scan);
    // Follow the variables that received a value, until no variable receives a new one.
    let taken = 0;
    const followed = new Set();
    const several = new Set();
    while (taken < scan.referenceValues.length || pending.length) {
      const { name, value } = pending.length ? pending.shift() : scan.referenceValues[taken++];
      const symbol = this.resolve(name);
      if (!symbol || this.idOf(symbol) === null) {
        scan.result.unfollowed.push({ ...scan.where(name), value: valueName(value), reason: 'stored in a name the module does not declare' });
        continue;
      }
      const examined = this.examine(symbol);
      if (examined.refused.length) {
        scan.result.unfollowed.push({ ...scan.where(name), value: valueName(value),
          reason: `stored in a variable the binding model refuses (${examined.refused.map((r) => r.reason).join('; ')})` });
        continue;
      }
      const key = `${this.idOf(symbol)} ${valueName(value)}`;
      if (followed.has(key)) continue;
      followed.add(key);
      const held = values.get(symbol) ?? [];
      if (!held.some((known) => valueName(known) === valueName(value))) held.push(value);
      values.set(symbol, held);
      if (held.length >= 2 && !several.has(symbol)) {
        several.add(symbol);
        scan.result.unfollowed.push({ ...scan.where(name), value: held.map(valueName).join(' or '),
          reason: 'a variable that holds several built-in values, so a definer called with it as the target is not followed' });
      }
      for (const declaration of declarationsIn(symbol, sourceFile)) {
        const root = ts.isBindingElement(declaration) ? patternRoot(declaration) : declaration;
        if (exportedByModifier(root) && value.root !== UNKNOWN) {
          scan.result.exports.push({ ...scan.where(declarationName(declaration)), value: valueName(value), name: declarationName(declaration).text });
        }
      }
      for (const node of examined.nodes) if (!isDeclarationName(node)) scan.follow(node, value, true);
    }
    return inSourceOrder(scan.result);
  }

  // Whether an identifier is a variable the binding model accepts, with one declaration whose
  // initializer is a call of the built-in Symbol.for, and no other write.
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
    // call although only the access carries the ?. token.
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
  // name no source identifier can have, since ‹ and › cannot occur in a JavaScript identifier: the
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
      case 'bindingAt': {
        const found = facts.find(query.s, query.e, 'Identifier');
        if (found.length !== 1) throw new RequestError(`no single identifier at ${query.s}:${query.e} in module ${module.index}`);
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
              query.e <= module.text.length)) throw new RequestError(`no text ${query.s}:${query.e} in module ${module.index}`);
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
