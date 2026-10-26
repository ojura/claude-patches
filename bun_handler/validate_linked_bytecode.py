#!/usr/bin/env python3
"""Exercise linked bytecode using edited copies of a supplied Claude executable.

Run in an isolated environment as described in README.md. The supplied file is
read only. Each generated executable is deleted after its case. The module API
finds the entrypoint and two existing cached functions: an object predicate and
its forwarding function. Changing only the predicate then tests whether the
cached forwarding function uses the edited source.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bun_handler as h
from test_bun_interop import append_sources

ID = r'[A-Za-z_$][\w$]*'
PREDICATE = re.compile(
    rf'function ({ID})\(({ID})\)\{{return typeof \2==="object"&&\2!==null'
    rf'&&!Array\.isArray\(\2\)\}}\s*export\{{\1\}};\s*$')


def find_functions(image):
    candidates = []
    for module in image.source_modules():
        if module['bytecode'][1] and module['contents'][1] < 2000:
            text = image.read_source(module).decode()
            match = PREDICATE.search(text)
            if match:
                candidates.append((module, match.group(1)))
    if len(candidates) != 1:
        raise AssertionError(f'expected one cached object predicate, found {len(candidates)}')
    predicate, export = candidates[0]
    forward = re.compile(
        rf'import\{{{re.escape(export)}\}}from"{re.escape(predicate["name_str"])}";'
        rf'function ({ID})\(({ID})\)\{{return {re.escape(export)}\(\2\)\}}'
        rf'\s*export\{{\1\}};\s*$')
    candidates = []
    for module in image.source_modules():
        if module['bytecode'][1] and module['contents'][1] < 2000:
            match = forward.search(image.read_source(module).decode())
            if match:
                candidates.append((module, match.group(1)))
    if len(candidates) != 1:
        raise AssertionError(f'expected one cached forwarding function, found {len(candidates)}')
    wrapper, wrapper_export = candidates[0]
    return predicate, export, wrapper, wrapper_export


def main(binary):
    original = Path(binary).read_bytes()
    original_digest = hashlib.sha256(original).hexdigest()
    image = h.BunImage(original)
    assert image.flags & h.BUN_FLAG_HAS_LINKED_BYTECODE_PAYLOAD
    entry = image.entrypoint_module()
    predicate, export, wrapper, wrapper_export = find_functions(image)
    payload_off, payload_len = image.linked_bytecode_payload
    payload_digest = hashlib.sha256(image.blob[payload_off:payload_off + payload_len]).digest()
    results = []

    def edit(data, replacements):
        return h.repack_module_sources(data, [
            (m['index'], m['name_bytes'], text.encode()) for m, text in replacements
        ], expected_sha256=None)

    def run(label, data, expected=None, error=None, args=()):
        parsed = h.BunImage(data)
        off, length = parsed.linked_bytecode_payload
        assert length == payload_len
        assert hashlib.sha256(parsed.blob[off:off + length]).digest() == payload_digest
        assert parsed.linked_bytecode_region_ends == image.linked_bytecode_region_ends
        assert parsed.runtime_options_bytes == image.runtime_options_bytes
        # The forwarding function stays compiled in every edited executable.
        assert parsed.modules[wrapper['index']]['bytecode'][1] == wrapper['bytecode'][1]
        with tempfile.TemporaryDirectory(prefix='bun-linked-runtime-') as work:
            executable = Path(work) / 'claude'
            executable.write_bytes(data)
            executable.chmod(0o700)
            process = subprocess.run([str(executable), *args], capture_output=True,
                                     text=True, encoding='utf-8', errors='replace', timeout=90)
        result = {'case': label, 'exit': process.returncode,
                  'stdout': process.stdout, 'stderr': process.stderr}
        print(json.dumps(result), flush=True)
        results.append(result)
        if error is None:
            assert process.returncode == 0, result
            assert process.stdout.strip() == expected, result
            assert process.stderr == '', result
        else:
            assert process.returncode != 0, result
            assert error in process.stderr, result
        return result

    baseline = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=90)
    assert baseline.returncode == 0 and baseline.stderr == ''
    run('unchanged repack', h.repack_unchanged(original), baseline.stdout.strip(), args=('--version',))
    specifier = json.dumps(predicate['name_str'])
    wrapper_specifier = json.dumps(wrapper['name_str'])
    original_main = (
        f'import{{{wrapper_export} as f}}from{wrapper_specifier};'
        f'const m=await import({specifier});'
        f'console.log([f({{}}),f(null),m.{export}([])].join(","));')
    run('new entrypoint imports cached modules', edit(original, [(entry, original_main)]),
        'true,false,false')
    main = (f'import{{{wrapper_export} as f}}from{wrapper_specifier};'
            f'const m=await import({specifier});'
            f'console.log([f(17),f({{}}),m.{export}(17)].join(","));')
    new_predicate = f'export function {export}(x){{return x===17}}'
    run('cached importer uses edited exporter',
        edit(original, [(entry, main), (predicate, new_predicate)]), 'true,false,true')
    run('cached importer sees missing export',
        edit(original, [(entry, main), (predicate, 'export const unrelated=1;')]),
        error=f"Export named '{export}' not found")
    await_main = (f'import{{{wrapper_export} as f}}from{wrapper_specifier};'
                  'console.log([f(17),f({})].join(","));')
    await_predicate = ('let ready=false;'
                       'await Promise.resolve().then(()=>{ready=true});'
                       f'export function {export}(x){{return ready&&x===17}}')
    run('cached importer waits for top-level await',
        edit(original, [(entry, await_main), (predicate, await_predicate)]), 'true,false')
    empty_main = f'const m=await import({specifier});console.log(Object.keys(m).length);'
    empty = edit(original, [(entry, empty_main), (predicate, '')])
    run('empty dynamic source', empty, '0')
    regrown_main = f'const m=await import({specifier});console.log(m.restored);'
    regrown = edit(empty, [(entry, regrown_main), (predicate, 'export const restored=42;')])
    del empty
    run('regrown dynamic source', regrown, '42')
    del regrown
    builtin_main = ('import{createHash}from"node:crypto";'
                    'console.log(createHash("sha256").update("linked").digest("hex"));')
    run('builtin bytecode survives edits', edit(original, [(entry, builtin_main)]),
        hashlib.sha256(b'linked').hexdigest())
    if image.elf is not None:
        # The external append fixture is never executed with a stale prelink.
        appended = append_sources(original, {entry['index']: original_main.encode()})
        edited = edit(appended, [(entry, original_main + '\n')])
        del appended
        run('append then normal edit', edited, 'true,false,false')
        reverted = append_sources(edited, {entry['index']: image.read_source(entry)})
        del edited
        restored = h.compact_with_reference(reverted, original)
        del reverted
        assert restored == original
        run('revert and compact restores original caches', restored,
            baseline.stdout.strip(), args=('--version',))
        del restored
    assert hashlib.sha256(Path(binary).read_bytes()).hexdigest() == original_digest
    print(json.dumps({'passed': len(results), 'entrypoint': entry['index'],
                      'predicate': predicate['index'], 'cached_importer': wrapper['index']}))


if __name__ == '__main__':
    main(sys.argv[1])
