// Tests of facts.mjs through its protocol, as pfg.bundle uses it: node --test pfg/js/facts.test.mjs
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REVISION = Number(fs.readFileSync(path.join(HERE, '..', 'REVISION'), 'utf8').trim());

// A helper process with the given modules, answering one request at a time.
async function helper(modules) {
  const child = spawn(process.execPath, ['--expose-gc', path.join(HERE, 'facts.mjs')], { stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = readline.createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  const read = async () => JSON.parse((await lines.next()).value);
  const hello = await read();
  let id = 0;
  for (const [index, name, text] of modules) child.stdin.write(`${JSON.stringify({ op: 'module', index, name, text })}\n`);
  const request = async (op, args = {}) => {
    id += 1;
    child.stdin.write(`${JSON.stringify({ id, op, ...args })}\n`);
    const reply = await read();
    assert.equal(reply.id, id);
    return reply;
  };
  const ok = async (op, args) => {
    const reply = await request(op, args);
    assert.equal(reply.ok, true, JSON.stringify(reply.error));
    return reply.result;
  };
  const facts = async (module, queries) => ok('facts', { module, queries });
  const close = () => new Promise((resolve) => {
    child.on('exit', resolve);
    child.stdin.end();
  });
  return { hello, request, ok, facts, close, child };
}

async function withHelper(modules, body) {
  const h = await helper(modules);
  try {
    return await body(h);
  } finally {
    await h.close();
  }
}

// The span of the n-th occurrence of the identifier `name` in text: a whole word, not part of another.
function spanOf(text, name, n = 0) {
  const pattern = new RegExp(`(?<![\\w$])${name.replace(/[$]/g, '\\$&')}(?![\\w$])`, 'g');
  const matches = [...text.matchAll(pattern)];
  assert.ok(matches.length > n, `${name} occurs ${n + 1} times`);
  return { s: matches[n].index, e: matches[n].index + name.length };
}

// The span of the n-th occurrence of a literal piece of text.
function spanOfText(text, piece, n = 0) {
  let at = -1;
  for (let i = 0; i <= n; i += 1) at = text.indexOf(piece, at + 1);
  assert.ok(at >= 0, `${piece} occurs ${n + 1} times`);
  return { s: at, e: at + piece.length };
}

async function bindingOf(h, module, text, needle, n = 0) {
  const { s, e } = spanOf(text, needle, n);
  const [record] = await h.facts(module, [{ q: 'bindingAt', s, e }]);
  return record;
}

test('the hello line names the protocol, the helper and the revision in pfg/REVISION', async () => {
  await withHelper([], async (h) => {
    assert.equal(h.hello.hello.protocol, 1);
    assert.equal(h.hello.hello.helper, 'pfg-facts');
    assert.equal(h.hello.hello.revision, REVISION);
    assert.equal(h.hello.hello.typescript, '5.9.3');
  });
});

test('the vendored TypeScript is the version its own package.json declares', async () => {
  const declared = JSON.parse(fs.readFileSync(path.join(HERE, 'vendor', 'typescript', 'package.json'), 'utf8')).version;
  await withHelper([], async (h) => assert.equal(h.hello.hello.typescript, declared));
});

test('the index keeps every load in source order, with its target and its lexical context', async () => {
  const text = 'import"/c.js";import d,{a as b}from"/b.js";import*as ns from"path";export{b as c};' +
    'var f=()=>import("/b.js");(()=>import.meta.require("/c.js"))();import(`@x/${f}.js`);export*from"/c.js";';
  await withHelper([[0, '/a.js', text], [1, '/b.js', 'export var a=1;'], [2, '/c.js', '']], async (h) => {
    const { modules } = await h.ok('index', { needles: [], builtins: [] });
    const loads = modules[0].loads;
    assert.deepEqual(loads.map((load) => load.k), ['side', 'import', 'import', 'export', 'import()', 'import.meta.require', 'import()', 'export*']);
    assert.equal(loads[0].t, 2);
    assert.deepEqual(loads[1].default.slice(0, 1), ['d']);
    assert.deepEqual(loads[1].named.map((n) => n.slice(0, 2)), [['a', 'b']]);
    assert.equal(loads[2].t, null);
    assert.equal(loads[2].spec, 'path');
    assert.deepEqual(loads[3].named.map((n) => n.slice(0, 2)), [['b', 'c']]);
    assert.equal(loads[4].t, 1);
    assert.equal(loads[4].inside.k, 'ArrowFunction');
    // A load inside a function is recorded as lexically inside it, even when the function runs at once.
    assert.equal(loads[5].inside.k, 'ArrowFunction');
    assert.equal(loads[5].t, 2);
    assert.equal(loads[6].form, 'template-head');
    assert.equal(loads[6].head, '@x/');
    assert.equal(loads[6].inside, null);
    assert.equal(modules[1].loads[0].k, 'exportdecl');
    assert.deepEqual(modules[1].loads[0].names.map((n) => n[0]), ['a']);
  });
});

test('needles hit code only: a quoted or commented copy is not a hit', async () => {
  const text = 'var x=k("flag",{list:[]});// k("flag")\nvar s=\'k("flag",{list:[]})\';var t=`flag ${x}`;o.list;';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const needles = [{ key: 's', type: 'string', value: 'flag' }, { key: 'p', type: 'property', value: 'list' },
      { key: 'm', type: 'member', value: 'list' }, { key: 't', type: 'template-head', value: 'flag ' }];
    const { modules } = await h.ok('index', { needles, builtins: [] });
    const hits = modules[0].hits;
    assert.deepEqual(hits.s.map((hit) => hit.s), [spanOfText(text, '"flag"').s]);
    assert.deepEqual(hits.p.map((hit) => hit.s), [spanOfText(text, 'list').s]);
    assert.deepEqual(hits.m.map((hit) => hit.s), [spanOfText(text, 'list', 2).s]);
    assert.equal(hits.t.length, 1);
    assert.deepEqual(hits.s[0].chain.slice(0, 1), [{ k: 'CallExpression', f: 'arguments', i: 0, n: 2, argc: 2 }]);
  });
});

test('a syntax error is a diagnostic of its module, not a failure of the index', async () => {
  await withHelper([[0, '/a.js', 'var x=[1;'], [1, '/b.js', 'var y=1;']], async (h) => {
    const { modules } = await h.ok('index', { needles: [], builtins: [] });
    assert.ok(modules[0].diagnostics.length > 0);
    assert.equal(modules[1].diagnostics.length, 0);
  });
});

test('binding ids come from the declaration and stay the same across batches, orders and reparses', async () => {
  const a = 'var out=[1],other=2;function f(x){return out[x]}';
  const b = 'var z=1;';
  await withHelper([[0, '/a.js', a], [1, '/b.js', b]], async (h) => {
    const first = await bindingOf(h, 0, a, 'out');
    assert.equal(first.id, `m0@${a.indexOf('out')}`);
    await h.facts(1, [{ q: 'bindingAt', ...spanOf(b, 'z') }]);
    const again = await bindingOf(h, 0, a, 'out', 1);
    assert.equal(again.id, first.id);
    const [occurrences, record] = await h.facts(0, [{ q: 'occurrences', id: first.id }, { q: 'record', id: first.id }]);
    assert.equal(occurrences.length, 2);
    assert.equal(record.kind, 'var');
    assert.equal(record.declarations[0].module, true);
    assert.equal(record.declarations[0].statement, 0);
    assert.equal(record.declarations[0].declarator, 0);
  });
});

test('an occurrence has every role it plays', async () => {
  const text = 'var v=1;v+=1;v++;v=2;[v]=[3];({v}={v:4});for(v of[5]);v??=6;typeof v;({v});export{v};';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const { id } = await bindingOf(h, 0, text, 'v');
    const [occurrences] = await h.facts(0, [{ q: 'occurrences', id }]);
    const roles = occurrences.map((o) => o.roles);
    assert.deepEqual(roles[0], ['declaration', 'initialized-declaration']);
    assert.deepEqual(roles[1], ['read', 'write', 'compound']);
    assert.deepEqual(roles[2], ['read', 'write', 'update']);
    assert.deepEqual(roles[3], ['write', 'assign']);
    assert.deepEqual(roles[4], ['write', 'destructuring-target']);
    assert.deepEqual(roles[5], ['shorthand-value', 'write', 'destructuring-target']);
    assert.deepEqual(roles[6], ['write', 'for-target']);
    assert.deepEqual(roles[7], ['read', 'write', 'logical']);
    assert.deepEqual(roles[8], ['typeof-operand', 'read']);
    assert.deepEqual(roles[9], ['shorthand-value', 'read']);
    assert.deepEqual(roles[10], ['export-specifier']);
  });
});

test('a local of the same spelling never counts as an occurrence of the module binding', async () => {
  const text = 'var x="outer";function f(x){return x}function g(){let x=1;return x}' +
    'try{}catch(x){x}for(let x of[]){x}var h=function x(){return x};var c=class x{m(){return x}};' +
    'var k=(x)=>x;function d({x}){return x}x:for(;;){break x}export{x};';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const { id } = await bindingOf(h, 0, text, 'x');
    const [occurrences] = await h.facts(0, [{ q: 'occurrences', id }]);
    assert.deepEqual(occurrences.map((o) => o.s), [spanOf(text, 'x').s, text.lastIndexOf('x')]);
  });
});

test('import and export aliases keep their own bindings; the outside names are not bindings', async () => {
  // The frozen suite's import-export-alpha case.
  const text = 'import { sourceName as localName } from "pkg";\nexport { localName as publicName };';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const local = await bindingOf(h, 0, text, 'localName');
    assert.equal(local.kind, 'import');
    assert.deepEqual(local.declarations[0].import, { spec: 'pkg', imported: 'sourceName' });
    const [occurrences] = await h.facts(0, [{ q: 'occurrences', id: local.id }]);
    assert.equal(occurrences.length, 2);
    assert.deepEqual(await bindingOf(h, 0, text, 'sourceName'), { role: 'name' });
    assert.deepEqual(await bindingOf(h, 0, text, 'publicName'), { role: 'name' });
  });
});

test('labels and private names that share a spelling are not bindings', async () => {
  // The frozen suite's labeled-statement identity and private-alpha cases.
  const text = 'var a=1;a:for(;;){break a}a:for(;;){continue a}class S{#a=1;r(){return this.#a+a}}';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const { id } = await bindingOf(h, 0, text, 'a');
    const [occurrences] = await h.facts(0, [{ q: 'occurrences', id }]);
    assert.deepEqual(occurrences.map((o) => o.s), [spanOf(text, 'a').s, text.lastIndexOf('a')]);
  });
});

test('contextual words as property names stay names while their bindings are bindings', async () => {
  // The frozen suite's contextual-alpha case.
  const text = 'const authorize = value => value;\nfunction contextual(record) { const { get: reader, set: writer, async: mode, from: origin } = record; return authorize({ get: reader, set: writer, async: mode, from: origin }); }';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const reader = await bindingOf(h, 0, text, 'reader');
    assert.equal(reader.kind, 'destructured const');
    const [occurrences] = await h.facts(0, [{ q: 'occurrences', id: reader.id }]);
    assert.equal(occurrences.length, 2);
  });
});

test('literals carry the values JavaScript gives them', async () => {
  const text = 'var n=[0x10,1_000,1e2,1e21,1e-7,1e999,-0],t=`a\\x41`,r=/ab+c/gi,o={1e2:1,0x10:2,[-0]:3,["k"]:4,[Sx]:5},s="\u{1F600}x";';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const [tree] = await h.facts(0, [{ q: 'node', handle: '0', depth: 12 }]);
    const declarations = tree.declarationList.declarations;
    const numbers = declarations[0].initializer.elements;
    assert.deepEqual(numbers.slice(0, 6).map((n) => n.js), ['16', '1000', '100', '1e+21', '1e-7', 'Infinity']);
    assert.equal(numbers[5].finite, false);
    assert.equal(numbers[5].value, null);
    assert.equal(numbers[6].k, 'PrefixUnaryExpression');
    assert.equal(declarations[1].initializer.value, 'aA');
    assert.equal(declarations[2].initializer.text, '/ab+c/gi');
    assert.deepEqual(declarations[3].initializer.properties.map((p) => p.key), ['100', '16', '0', 'k', undefined]);
    assert.equal(declarations[4].initializer.value, '\u{1F600}x');
  });
});

test('positions are UTF-16 offsets, also after a character above U+FFFF', async () => {
  const text = 'var e="\u{1F600}";var after=1;';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const record = await bindingOf(h, 0, text, 'after');
    // The emoji takes two UTF-16 units, so `after` starts at 15 although it is the 14th code point.
    assert.equal(record.declarations[0].name.s, 15);
    assert.equal(text.indexOf('after'), 15);
  });
});

test('the entrypoint without a file extension is part of its program', async () => {
  const text = 'var x=1;function f(){return x}';
  await withHelper([[7, '/$bunfs/root/cli', text]], async (h) => {
    const { id } = await bindingOf(h, 7, text, 'x');
    const [occurrences] = await h.facts(7, [{ q: 'occurrences', id }]);
    assert.equal(occurrences.length, 2);
  });
});

test('a depth-limited syntax tree marks what it leaves out', async () => {
  await withHelper([[0, '/a.js', 'var x=[1,[2]];']], async (h) => {
    const [shallow, one] = await h.facts(0, [{ q: 'node', handle: '0', depth: 0 }, { q: 'node', handle: '0', depth: 1 }]);
    assert.equal(shallow.declarationList.truncated, true);
    assert.equal(shallow.declarationList.declarations, undefined);
    assert.equal(one.declarationList.truncated, undefined);
    assert.equal(one.declarationList.declarations[0].truncated, true);
  });
});

test('canonical text keeps bindings apart and cannot collide with a source name', async () => {
  const text = 'function f(a){return a}function g(a){return $p0}function h(a){return function(b){return [a,b]}}' +
    'function k(x){return{x}}var m=1;function r(a){return m+a}';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const statements = [0, 1, 2, 3, 5].map((i) => ({ q: 'canonical', handle: String(i) }));
    const [f, g, nested, shorthand, withModule] = await h.facts(0, statements);
    assert.notEqual(f.text, g.text);
    assert.match(g.text, /return \$p0/);
    assert.match(nested.text, /return \[‹p0›, ‹p1›\]/);
    assert.match(shorthand.text, /x: ‹p0›/);
    // r itself is a module binding too, first used in its own declaration: ‹m0›, then m is ‹m1›.
    assert.equal(withModule.text, 'function ‹m0›(‹p0›) { return ‹m1› + ‹p0›; }');
    const mId = (await bindingOf(h, 0, text, 'm')).id;
    const rId = (await bindingOf(h, 0, text, 'r')).id;
    assert.deepEqual(withModule.others, [rId, mId]);
    const [roled] = await h.facts(0, [{ q: 'canonical', handle: '5', roles: { [mId]: 'M' } }]);
    assert.equal(roled.text, 'function ‹m0›(‹p0›) { return ‹M› + ‹p0›; }');
    assert.deepEqual(roled.others, [rId]);
  });
});

test('canonical text keeps destructuring defaults and renames array elements without inventing keys', async () => {
  // The review's probes: ({x=1}=o) and ({x=2}=o) printed the same, and [x] printed as [x: ‹p0›].
  const text = 'function f(x,o){({x=1}=o);return x}function g(x,o){({x=2}=o);return x}' +
    'function a([x]){return x}function b([y]){return y}function c({x=3}){return x}';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const [f, g, a, b, c] = await h.facts(0, [0, 1, 2, 3, 4].map((i) => ({ q: 'canonical', handle: String(i) })));
    assert.notEqual(f.text, g.text);
    assert.match(f.text, /\{ x: ‹p0› = 1 \} = ‹p1›/);
    assert.equal(a.text, b.text);
    assert.match(a.text, /\(\[‹p0›\]\)/);
    assert.match(c.text, /\{ x: ‹p0› = 3 \}/);
  });
});

test('a canonical role must be an upper-case name, so it cannot spell a generated one', async () => {
  const text = 'var source=1;function f(a){return a}function g(a){return source}';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const { id } = await bindingOf(h, 0, text, 'source');
    const reply = await h.request('facts', { module: 0, queries: [{ q: 'canonical', handle: '2', roles: { [id]: 'p0' } }] });
    assert.equal(reply.ok, false);
    assert.equal(reply.error.kind, 'request');
    assert.match(reply.error.message, /upper-case/);
  });
});

test('a syntax record names every child field and keeps operators and declaration keywords', async () => {
  const text = 'var a=!0,b=-0,c=+0,d=~0,e=void 0,f=x++,g=--x;let h=a?.b();const i=import.meta;' +
    'class K extends Object{}async function m(){for await(const x of y){}({x=1}=y)}';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const [tree] = await h.facts(0, [{ q: 'node', handle: '', depth: 30 }]);
    const nodes = [];
    const walk = (node) => {
      if (!node || typeof node !== 'object') return;
      if (Array.isArray(node)) return node.forEach(walk);
      if (node.k) nodes.push(node);
      for (const [key, value] of Object.entries(node)) {
        assert.notEqual(key, '?', `a ${node.k} has a child without a field name`);
        walk(value);
      }
    };
    walk(tree);
    const declarations = tree.statements[0].declarationList.declarations;
    assert.deepEqual(declarations.map((d) => d.initializer.op), ['!', '-', '+', '~', undefined, '++', '--']);
    assert.equal(declarations[4].initializer.k, 'VoidExpression');
    assert.deepEqual(tree.statements.slice(0, 3).map((s) => s.declarationList.declares), ['var', 'let', 'const']);
    assert.equal(tree.statements[1].declarationList.declarations[0].initializer.optionalChain, true);
    assert.equal(tree.statements[2].declarationList.declarations[0].initializer.keyword, 'import');
    assert.equal(tree.statements[3].heritageClauses[0].token, 'extends');
    const forOf = nodes.find((node) => node.k === 'ForOfStatement');
    assert.equal(forOf.awaitModifier.k, 'AwaitKeyword');
    const shorthand = nodes.find((node) => node.k === 'ShorthandPropertyAssignment');
    assert.equal(shorthand.objectAssignmentInitializer.value, 1);
    assert.equal(tree.endOfFileToken.k, 'EndOfFileToken');
  });
});

test('a declaration that assigns its name is a write: for-in/of heads, initialized patterns, functions', async () => {
  // The review's probes: `var V=1;for(var V of[2]){}` reported the second declaration as a plain one.
  const cases = [
    ['var V=1;for(var V of[2]){}', ['write', 'for-target']],
    ['var V=1;for(var V in {x:2}){}', ['write', 'for-target']],
    ['var V=1;var {V}={V:2};', ['initialized-declaration']],
    ['var V=1;for(var {V} of[{V:2}]){}', ['write', 'for-target']],
    ['var V=1;var [V=2]=[];', ['initialized-declaration']],
    ['var V=1;function f(){}var V;', []],
  ];
  for (const [text, extra] of cases) {
    await withHelper([[0, '/a.js', text]], async (h) => {
      const { id } = await bindingOf(h, 0, text, 'V');
      const [occurrences] = await h.facts(0, [{ q: 'occurrences', id }]);
      assert.deepEqual(occurrences[0].roles, ['declaration', 'initialized-declaration'], text);
      assert.deepEqual(occurrences[1].roles, ['declaration', ...extra], text);
    });
  }
  const text = 'try{}catch(E){}';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const { id } = await bindingOf(h, 0, text, 'E');
    const [occurrences] = await h.facts(0, [{ q: 'occurrences', id }]);
    assert.deepEqual(occurrences[0].roles, ['declaration', 'initialized-declaration']);
  });
});

// The binding model (facts.mjs, "The binding model"): every case a review found, modelled or refused.

async function refusedReasons(h, module, text, name, n = 0) {
  const record = await bindingOf(h, module, text, name, n);
  return (record.refused ?? []).map((r) => r.reason).join('; ');
}

test('declarations TypeScript keeps apart but JavaScript merges refuse both bindings, in either order', async () => {
  // JavaScript has one V in outer, which the function declaration and the var initializer both write;
  // TypeScript's binder gives each declaration a symbol and reports a duplicate at both names.
  for (const text of ['function outer(){var V=1;function V(){}return V}', 'function outer(){function V(){}var V=1;return V}',
    'function outer(){var {V}={V:1};function V(){}return V}']) {
    await withHelper([[0, '/a.js', text]], async (h) => {
      // Every V that is a binding (not the key of {V:1}) belongs to a refused binding, and there are two.
      const ids = new Set();
      const count = [...text.matchAll(/(?<![\w$])V(?![\w$])/g)].length;
      for (let n = 0; n < count; n += 1) {
        const record = await bindingOf(h, 0, text, 'V', n);
        if (record.role) continue;
        assert.match(record.refused.map((r) => r.reason).join(), /keeps apart/, text);
        ids.add(record.id);
      }
      assert.equal(ids.size, 2, text);
      assert.equal(await refusedReasons(h, 0, text, 'outer'), '', text);
      const [occurrences] = await h.facts(0, [{ q: 'occurrences', id: [...ids][0] }]);
      assert.ok(occurrences.refused.length > 0);
    });
  }
  // Separate scopes are separate bindings, and nothing is refused.
  const nested = 'function outer(){var V=1;function nested(){function V(){}return V}return V}';
  await withHelper([[0, '/a.js', nested]], async (h) => {
    assert.equal(await refusedReasons(h, 0, nested, 'V'), '');
    assert.equal(await refusedReasons(h, 0, nested, 'V', 1), '');
  });
});

test('a catch variable and a var of its name in the catch block refuse both bindings', async () => {
  // The review's probe: the var's initializer assigns the catch variable, so x.push changes V.
  const text = 'const V=[1];function f(){try{throw []}catch(x){var x=V;x.push(2)}}f();';
  await withHelper([[0, '/a.js', text]], async (h) => {
    assert.match(await refusedReasons(h, 0, text, 'x'), /catch block assigns this catch variable/);
    assert.match(await refusedReasons(h, 0, text, 'x', 1), /catch clause around it/);
    assert.equal(await refusedReasons(h, 0, text, 'V'), '');
  });
});

test('a named default export function or class is a binding under its local name', async () => {
  // The review's probe: TypeScript names the symbol "default", and the occurrences were lost.
  for (const text of ['export default function f(){return 1};f=()=>2;f();', 'export default class f{};f=null;f;']) {
    await withHelper([[0, '/a.js', text]], async (h) => {
      const record = await bindingOf(h, 0, text, 'f');
      assert.equal(record.name, 'f');
      const [occurrences] = await h.facts(0, [{ q: 'occurrences', id: record.id }]);
      const list = occurrences.refused ? [] : occurrences;
      if (text.includes('class')) {
        // A written class name is refused: the class body would keep an inner binding to the class.
        assert.match(record.refused.map((r) => r.reason).join(), /class name is written/);
      } else {
        assert.deepEqual(list.map((o) => o.roles[0]), ['declaration', 'write', 'read']);
      }
    });
  }
});

test('a parameter list with expressions gets the scopes JavaScript gives it', async () => {
  // The body's declarations are not visible in the defaults: x in the default is the module's x, also
  // where TypeScript resolves it to the body's function x.
  const text = 'let x=1;function f(a=x){let x=2;return a}function g(b=x){function x(){}return b}function h(c=1){var v=2;return v}';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const moduleX = await bindingOf(h, 0, text, 'x');
    const inDefaultF = await bindingOf(h, 0, text, 'x', 1);
    const inDefaultG = await bindingOf(h, 0, text, 'x', 3);
    assert.equal(inDefaultF.id, moduleX.id);
    assert.equal(inDefaultG.id, moduleX.id);
    const [occurrences] = await h.facts(0, [{ q: 'occurrences', id: moduleX.id }]);
    assert.equal(occurrences.length, 3);
    const bodyX = await bindingOf(h, 0, text, 'x', 2);
    const body = text.indexOf('{let x=2');
    assert.deepEqual([bodyX.declarations[0].scope.k, bodyX.declarations[0].scope.s], ['Block', body]);
    const bodyFunction = await bindingOf(h, 0, text, 'x', 4);
    assert.equal(bodyFunction.declarations[0].scope.k, 'Block');
    const v = await bindingOf(h, 0, text, 'v');
    assert.equal(v.declarations[0].scope.k, 'Block');
    assert.equal(v.refused, undefined);
  });
});

test('a parameter and a body var of its name are refused when the parameter list has expressions', async () => {
  // The review's probe: Node returns 1, the parameter's value, while TypeScript has one symbol for both.
  const text = 'function outer(V=1,g=()=>V){var V=2;return g()}function plain(W){var W=2;return W}';
  await withHelper([[0, '/a.js', text]], async (h) => {
    assert.match(await refusedReasons(h, 0, text, 'V'), /two bindings in JavaScript/);
    // Without parameter expressions, the parameter and the var are one binding, as TypeScript has them.
    assert.equal(await refusedReasons(h, 0, text, 'W'), '');
  });
});

test('direct eval and with statements refuse the bindings in their scope; indirect eval does not', async () => {
  const cases = [
    ['let V=1;eval("V=2");', true],
    ['let V=1;(eval)("V=2");', true],
    ['let V=1;function f(){eval("V=2")}', true],
    ['function f(){let V=1;return V}eval("1");', false],
    ['let V=1;(0,eval)("V=2");', false],
    ['let V=1;eval?.("V=2");', false],
    ['let V=1;with(o){V=2}', true],
  ];
  for (const [text, refused] of cases) {
    await withHelper([[0, '/a.js', text]], async (h) => {
      const reasons = await refusedReasons(h, 0, text, 'V');
      if (refused) assert.match(reasons, /direct eval|with statement/, text);
      else assert.equal(reasons, '', text);
    });
  }
});

test('a written class declaration and a deleted name are refused', async () => {
  const text = 'class C{m(){return C}};C=null;var d=1;function g(){return d}';
  await withHelper([[0, '/a.js', text]], async (h) => {
    assert.match(await refusedReasons(h, 0, text, 'C'), /inner binding/);
    assert.equal(await refusedReasons(h, 0, text, 'd'), '');
  });
});

test('an exported declaration is one binding whatever order the queries come in', async () => {
  // The review's probe: after scope_binding cached TypeScript's local symbol under the id its export
  // symbol also has, occurrences found nothing and hid the writes.
  const cases = [['export let x=1;x=2;export{x as y};', 'x', 3], ['export default function f(){return 1};f=()=>2;f();', 'f', 3],
    ['export function g(){};g=1;', 'g', 2], ['export default class C{};C=null;C;', 'C', null]];
  for (const [text, name, count] of cases) {
    const at = spanOf(text, name);
    for (const order of [['bindingAt', 'scope'], ['scope', 'bindingAt']]) {
      await withHelper([[0, '/a.js', text], [1, '/b.js', 'var q=1;']], async (h) => {
        const queries = order.map((q) => (q === 'scope' ? { q, name } : { q, ...at }));
        // In one batch, in one program.
        const [first, second] = await h.facts(0, queries);
        assert.equal(first.id, second.id, text);
        const [occurrences, record] = await h.facts(0, [{ q: 'occurrences', id: first.id }, { q: 'record', id: first.id }]);
        // Across a reparse: another module's query drops this module's program.
        await h.facts(1, [{ q: 'scope', name: 'q' }]);
        const [again] = await h.facts(0, [{ q: 'occurrences', id: first.id }]);
        if (count === null) {
          assert.match(record.refused.map((r) => r.reason).join(), /class name is written/, text);
          assert.ok(occurrences.refused && again.refused, text);
        } else {
          assert.equal(occurrences.length, count, text);
          assert.deepEqual(again, occurrences, text);
          assert.ok(occurrences.some((o) => o.roles.includes('write')), text);
        }
      });
    }
  }
});

test('re-export names and export * as names are public names, not references', async () => {
  // The review's probe: `export{Set}from` gave {free:"Set"}, and `export*as Set from` a local binding.
  const text = 'export{Set}from"/x.js";export{Map as M}from"/x.js";export*as Promise from"/x.js";';
  await withHelper([[0, '/a.js', text]], async (h) => {
    for (const name of ['Set', 'Map', 'M', 'Promise']) assert.deepEqual(await bindingOf(h, 0, text, name), { role: 'name' }, name);
  });
});

test('canonical text keeps a module public names', async () => {
  // The review's probe: export{a} and export{b}, import{a} and import{b} printed the same.
  const sources = ['const a=1;export{a};', 'const b=1;export{b};', 'import{a}from"/x.js";a();', 'import{b}from"/x.js";b();',
    'export*as Set from"/x.js";', 'export var a=1;', 'export var b=1;'];
  await withHelper(sources.map((text, i) => [i, `/m${i}.js`, text]), async (h) => {
    const texts = [];
    for (let i = 0; i < sources.length; i += 1) texts.push((await h.facts(i, [{ q: 'canonical', handle: '' }]))[0].text);
    assert.notEqual(texts[0], texts[1]);
    assert.match(texts[0], /export \{ ‹l0› as a \}/);
    assert.notEqual(texts[2], texts[3]);
    assert.match(texts[2], /import \{ a as ‹l0› \}/);
    assert.match(texts[4], /export \* as Set from/);
    assert.notEqual(texts[5], texts[6]);
    assert.match(texts[5], /export var a = 1/);
  });
});

test('canonical text is refused where it would rename a refused binding or holds a direct eval', async () => {
  // In the second module the direct eval can also reach k, a binding of the module's scope, by name.
  const text = 'function f(){try{}catch(x){var x=1}return x}function k(a){return a}';
  const withEval = 'function g(s){return eval(s)}function k(a){return a}';
  await withHelper([[0, '/a.js', text], [1, '/b.js', withEval]], async (h) => {
    const [f, k] = await h.facts(0, [0, 1].map((i) => ({ q: 'canonical', handle: String(i) })));
    assert.ok(f.refused.length > 0);
    assert.equal(k.text, 'function ‹m0›(‹p0›) { return ‹p0›; }');
    const [g, k1] = await h.facts(1, [0, 1].map((i) => ({ q: 'canonical', handle: String(i) })));
    assert.match(g.refused.map((r) => r.reason).join(), /direct eval/);
    assert.match(k1.refused.map((r) => r.reason).join(), /direct eval/);
  });
});

test('arguments read in a nested method name, class heritage or decorator belongs to the enclosing function', async () => {
  // The review's probe: f(V) pushes onto V through arguments[0] inside a computed method name.
  const cases = [
    ['function f(x){const o={[arguments[0].push(2)](){}}}', true],
    ['function f(x){class C extends (arguments[0]){}}', true],
    ['function f(x){class C{[arguments[0]](){}}}', true],
    ['function f(x){class C{@(arguments[0]) m(){}}}', true],
    ['function f(x){return function(){return arguments}}', false],
    ['function f(x){return ()=>arguments}', true],
  ];
  for (const [text, reads] of cases) {
    await withHelper([[0, '/a.js', text]], async (h) => {
      const { id } = await bindingOf(h, 0, text, 'x');
      const [occurrences] = await h.facts(0, [{ q: 'occurrences', id }]);
      const step = occurrences[0].chain.find((s) => s.k === 'FunctionDeclaration');
      assert.equal(step.args, reads, text);
    });
  }
});

test('a tagged template with an invalid escape has no cooked value and makes no needle hit', async () => {
  const text = 'const x=tag`\\u{ZZ}`,y=`\\u{41}`;';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const { modules } = await h.ok('index', { needles: [{ key: 'raw', type: 'string', value: '\\u{ZZ}' }, { key: 'a', type: 'string', value: 'A' }], builtins: [] });
    assert.equal(modules[0].hits.raw, undefined);
    assert.equal(modules[0].hits.a.length, 1);
    const [tree] = await h.facts(0, [{ q: 'node', handle: '0', depth: 8 }]);
    const template = tree.declarationList.declarations[0].initializer.template;
    assert.equal(template.value, null);
    assert.equal(template.invalid, true);
  });
});

test('a module without imports or exports is parsed as a module', async () => {
  // In a script, await(1) calls a function named await; in a module it awaits.
  const text = 'var z=await(1);';
  await withHelper([[0, '/a.js', text]], async (h) => {
    const [tree] = await h.facts(0, [{ q: 'node', handle: '0', depth: 6 }]);
    assert.equal(tree.declarationList.declarations[0].initializer.k, 'AwaitExpression');
  });
});

// The built-in scan (facts.mjs, "The built-in scan"), in the index pass and with the module's bindings.

// A one-line summary of a scan result per category, for compact assertions.
function scanSummary(premise) {
  const entries = (key, field = 'target') => (premise[key] ?? []).map((w) => `${w[field]} ${w.how ?? w.reason ?? w.kind ?? ''}`.trim());
  return {
    writes: entries('writes'), globalWrites: entries('globalWrites', 'key'), registryWrites: entries('registryWrites'),
    prototypeWrites: entries('prototypeWrites'), unknownKeys: entries('unknownKeys'),
    unfollowed: premise.unfollowed.map((u) => `${u.value}: ${u.reason}`), dynamic: premise.dynamic.map((d) => d.kind),
    references: premise.references.map((r) => `${r.name}=${r.value}`), exports: (premise.exports ?? []).map((e) => `${e.name}=${e.value}`),
    passed: Object.values(premise.passed).reduce((a, b) => a + b, 0),
  };
}

async function scans(text) {
  return withHelper([[0, '/a.js', text]], async (h) => {
    const { modules } = await h.ok('index', { needles: [] });
    const [facts] = await h.facts(0, [{ q: 'premise' }]);
    return { index: scanSummary(modules[0].premise), facts: scanSummary(facts), raw: modules[0].premise };
  });
}

test('the scan passes calls, constructions, reads, comparisons and tests, and counts no write', async () => {
  const { index, facts } = await scans('Array.isArray(x);new Set([1]);Object.keys(o);typeof window;x instanceof Map;' +
    'Array.prototype.slice.call(x);if(Symbol.iterator in o);`${String}`;o[Symbol.iterator];class C extends Map{}');
  for (const result of [index, facts]) {
    assert.deepEqual([result.writes, result.unfollowed, result.dynamic], [[], [], []]);
    assert.equal(result.passed, 10);
  }
});

test('the scan records every visible write to a built-in, through any form the review named', async () => {
  const { index } = await scans('Set.prototype.has=g;Object["defineProperty"](Set.prototype,"has",{value:1});[Set]=[1];' +
    '({a:Set}=o);for(Set of r){}Set.prototype.__defineGetter__("has",f);' +
    'Object.prototype.__defineSetter__.call(Map.prototype,"get",f);Object.defineProperty.call(null,Array.prototype,"at",{});' +
    'Reflect.set(Math,"max",f);Object.setPrototypeOf(Object.prototype,null);delete Array.prototype.includes;' +
    'Object.getPrototypeOf([]).push=1;[].__proto__.pop=1;Array.prototype.push(1);Array.prototype[Symbol.iterator]=f;' +
    'Array.prototype[k]=1;globalThis.Set=1;Object.assign(String.prototype,{trim:f});');
  assert.deepEqual(index.writes, [
    'Set.prototype.has =', 'Set.prototype.has Object.defineProperty', 'Set destructuring target', 'Set destructuring target',
    'Set for-in/of target', 'Set.prototype.has __defineGetter__', 'Map.prototype.get Object.prototype.__defineSetter__',
    'Array.prototype.at Object.defineProperty', 'Math.max Reflect.set', 'Object.prototype.__proto__ Object.setPrototypeOf',
    'Array.prototype.includes delete', 'Array.prototype.push =', 'Array.prototype.pop =', 'Array.prototype.* push',
    'Array.prototype.@@iterator =', 'Array.prototype.* =', 'Set =', 'String.prototype.trim Object.assign',
  ]);
});

test('global writes under other keys, registry symbols and unknown keys are told apart', async () => {
  const text = 'const ag=Symbol.for("x");function lj(e){globalThis[ag]=e}globalThis[Symbol.for("y")]=1;' +
    'globalThis.awslambda=1;globalThis.awslambda.InvokeStore=2;Object.assign(globalThis,{setTimeout:n});globalThis[k]=2;' +
    'let s=Symbol.for("q");s=Symbol.iterator;globalThis[s]=3;';
  const { index, facts } = await scans(text);
  assert.deepEqual(index.globalWrites, ['awslambda =', 'awslambda =', 'setTimeout Object.assign']);
  assert.deepEqual(index.registryWrites, ['globalThis.@registry =']);
  // The index pass cannot see what ag holds; the facts pass proves it a Symbol.for result, never written.
  assert.equal(index.unknownKeys.length, 3);
  assert.deepEqual(facts.registryWrites, ['globalThis.@registry =', 'globalThis.@registry =']);
  // s is written after its Symbol.for initializer, so its key stays unknown.
  assert.deepEqual(facts.unknownKeys, ['globalThis.* =', 'globalThis.* =']);
});

test('a Symbol.for key counts only from the free, unshadowed Symbol', async () => {
  const { facts } = await scans('function f(Symbol){globalThis[Symbol.for("z")]=1;Array.prototype[Symbol.iterator]=1}');
  assert.deepEqual(facts.registryWrites, []);
  assert.deepEqual(facts.unknownKeys, ['globalThis.* =']);
  assert.deepEqual(facts.writes, ['Array.prototype.* =']);
});

test('dynamic code: direct eval in parentheses, indirect eval, the Function constructor in every form', async () => {
  const { index, facts } = await scans('let V=1;eval("1");(eval)("V=2");(0,eval)("V=3");eval?.("1");globalThis.eval("2");' +
    'eval.call(null,"3");Function("return this")();(Function)("x")();new Function("");Function.apply(null,l);' +
    'Reflect.construct(Function,[]);(async()=>{}).constructor("x");new (class extends Function{})("x");' +
    'new e.constructor(r);f(eval);');
  const expected = ['direct eval', 'direct eval', 'indirect eval', 'indirect eval', 'indirect eval', 'indirect eval through call',
    'Function constructor', 'Function constructor', 'Function constructor', 'Function constructor through apply',
    'the Function constructor passed to a function', 'AsyncFunction constructor', 'a class that extends the Function constructor',
    'a constructor the scan cannot identify, which may be the Function constructor', 'eval passed to a function'];
  assert.deepEqual(index.dynamic, expected);
  assert.deepEqual(facts.dynamic, expected);
});

test('a built-in value that escapes is unfollowed, and a prototype reached from an unknown object is counted', async () => {
  const { index, raw } = await scans('f(Array);x.map(String);var o={t:Object};return1(Set);[Map];' +
    'o.constructor.prototype.x=1;Object.getPrototypeOf(o).y=2;q(o.constructor);');
  assert.deepEqual(index.unfollowed, ['Array: passed to a function', 'String: passed to a function',
    'Object: stored in an object literal', 'Set: passed to a function', 'Map: stored in an array literal']);
  assert.deepEqual(index.prototypeWrites, ['(object).constructor.prototype.x =', '(object).__proto__.y =']);
  assert.equal(raw.unidentified, 1);
});

test('the facts pass follows variables, also into a definer call and out through an export', async () => {
  const text = 'var p=Object.prototype;p.x=1;var dp=Object.defineProperty;dp(Set.prototype,"has",{});dp(obj,"x",{});' +
    'var a=Set;a=Map;a.y=1;var S=Set;export{S as T};export const P=Promise;';
  const { index, facts } = await scans(text);
  // The index pass sees Set.prototype passed to dp, which it cannot identify.
  assert.ok(index.unfollowed.includes('Set.prototype: passed to a function'));
  assert.deepEqual(facts.writes, ['Object.prototype.x =', 'Set.prototype.has Object.defineProperty', 'Set.y =', 'Map.y =']);
  assert.deepEqual(facts.unfollowed, [
    'Set or Map: a variable that holds several built-in values, so a definer called with it as the target is not followed']);
  assert.deepEqual(facts.exports, ['T=Set', 'P=Promise']);
});

test('a variable the binding model refuses is unfollowed rather than followed', async () => {
  const { facts } = await scans('function f(){try{}catch(x){var x=Set.prototype;x.has=1}}');
  assert.equal(facts.writes.length, 0);
  assert.match(facts.unfollowed.join(), /binding model refuses/);
});

test('a name the module declares is resolved by the facts pass, and public names are not occurrences', async () => {
  const { index, facts } = await scans('function f(Set){Set.prototype.x=1}Set.prototype.y=2;' +
    'export{Map}from"/x.js";export*as Promise from"/x.js";');
  assert.deepEqual(index.writes, []);
  assert.deepEqual(facts.writes, ['Set.prototype.y =']);
  assert.equal(facts.passed, 0);
});

test('a malformed request and a notification error come back as request errors', async () => {
  await withHelper([[0, '/a.js', 'var x=1;'], [0, '/b.js', 'var y=2;']], async (h) => {
    const duplicate = await h.request('index', { needles: [], builtins: [] });
    assert.equal(duplicate.ok, false);
    assert.equal(duplicate.error.kind, 'request');
    assert.match(duplicate.error.message, /sent twice/);
    const unknown = await h.request('nonsense');
    assert.equal(unknown.error.kind, 'request');
    const handle = await h.request('facts', { module: 0, queries: [{ q: 'node', handle: '9.9' }] });
    assert.match(handle.error.message, /no node at handle/);
  });
});
