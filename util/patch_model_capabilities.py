#!/usr/bin/env python3
"""Enable the model-capabilities API in a Bun-packed native Claude CLI.

External builds retain the complete model-capabilities cache and refresh path,
but fold its eligibility predicate to an always-false function. This patch finds
that predicate from the surrounding feature-specific literals and changes only
``return!1`` to ``return!0``. The minified function name is discovered; it is
never hardcoded.

Usage::

    util/patch_model_capabilities.py <input-binary> [-o <output>]

Output defaults to ``<input>.model-capabilities``. Targets the Bun ``.bun``-
section ELF form via ``util/bun_handler`` (Linux x64).
"""
import os
import re
import sys
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bun_handler  # noqa: E402  (sys.path insert must precede this)


ANCHORS = (
    ('cache filename', 'model-capabilities.json'),
    ('unchanged-cache log', '[modelCapabilities] cache unchanged, skipping write'),
    ('cached-models log', '[modelCapabilities] cached '),
    ('fetch-failure log', '[modelCapabilities] fetch failed:'),
)
IDENTIFIER = r'[A-Za-z_$][A-Za-z0-9_$]*'
DISABLED_STUB = re.compile(
    rf'function (?P<name>{IDENTIFIER})\(\)\{{return!1\}}'
)


class PatchError(RuntimeError):
    """The bundle did not have the one exact structure this patch supports."""


@dataclass(frozen=True)
class EligibilityMatch:
    name: str
    stub_start: int
    stub_end: int
    cluster_start: int
    cluster_end: int

    @property
    def old_stub(self):
        return f'function {self.name}(){{return!1}}'

    @property
    def new_stub(self):
        return f'function {self.name}(){{return!0}}'


def _anchor_positions(js):
    positions = []
    for label, anchor in ANCHORS:
        count = js.count(anchor)
        if count != 1:
            raise PatchError(
                f'[{label}] anchor count {count} != 1; refusing to patch: '
                f'{anchor!r}'
            )
        positions.append(js.index(anchor))

    if positions != sorted(positions):
        order = ', '.join(label for label, _ in ANCHORS)
        raise PatchError(f'[anchors] feature anchors are not in order: {order}')
    return positions


def discover_eligibility(js):
    """Locate the folded predicate from the model-capabilities feature cluster."""
    positions = _anchor_positions(js)
    filename_pos, first_log_pos, _, fetch_log_pos = positions
    search_start = filename_pos + len(ANCHORS[0][1])
    region = js[search_start:first_log_pos]
    matches = list(DISABLED_STUB.finditer(region))
    if len(matches) != 1:
        enabled = re.findall(
            rf'function ({IDENTIFIER})\(\)\{{return!0\}}', region
        )
        detail = ''
        if not matches and enabled:
            detail = f'; always-true candidate(s) already present: {enabled!r}'
        raise PatchError(
            '[discover] disabled model-capabilities eligibility predicate: '
            f'expected 1 match, got {len(matches)}{detail}'
        )

    match = matches[0]
    stub_start = search_start + match.start()
    stub_end = search_start + match.end()
    cluster_end = fetch_log_pos + len(ANCHORS[-1][1])
    return EligibilityMatch(
        name=match.group('name'),
        stub_start=stub_start,
        stub_end=stub_end,
        cluster_start=filename_pos,
        cluster_end=cluster_end,
    )


def patch_js(js):
    """Return ``(patched_js, discovered_name)`` or raise ``PatchError``."""
    found = discover_eligibility(js)
    if js[found.stub_start:found.stub_end] != found.old_stub:
        raise PatchError('[splice] discovered predicate did not equal its exact stub')

    old_cluster = js[found.cluster_start:found.cluster_end]
    if js.count(old_cluster) != 1:
        raise PatchError(
            '[splice] model-capabilities feature cluster is not unique; '
            'refusing to patch'
        )

    local_start = found.stub_start - found.cluster_start
    local_end = found.stub_end - found.cluster_start
    if old_cluster[local_start:local_end] != found.old_stub:
        raise PatchError('[splice] predicate is outside the validated feature cluster')
    new_cluster = (
        old_cluster[:local_start] + found.new_stub + old_cluster[local_end:]
    )
    patched = js.replace(old_cluster, new_cluster, 1)

    if len(patched) != len(js):
        raise PatchError(
            f'[verify] source length changed by {len(patched) - len(js):+d} bytes'
        )
    differences = [
        index for index, (before, after) in enumerate(zip(js, patched))
        if before != after
    ]
    if len(differences) != 1:
        raise PatchError(
            f'[verify] expected one changed character, got {len(differences)}'
        )
    changed = differences[0]
    if js[changed] != '1' or patched[changed] != '0':
        raise PatchError(
            f'[verify] expected 1 -> 0, got {js[changed]!r} -> {patched[changed]!r}'
        )

    positions = _anchor_positions(patched)
    verify_start = positions[0] + len(ANCHORS[0][1])
    verify_region = patched[verify_start:positions[1]]
    if verify_region.count(found.new_stub) != 1:
        raise PatchError('[verify] enabled predicate is not unique in its feature region')
    if DISABLED_STUB.search(verify_region):
        raise PatchError('[verify] disabled predicate remains in its feature region')

    return patched, found.name


def patch_js_bytes(source):
    """Patch raw JS bytes while preserving arbitrary non-UTF-8 bytes."""
    js = source.decode('utf-8', errors='surrogateescape')
    patched, name = patch_js(js)
    return patched.encode('utf-8', errors='surrogateescape'), name


def patch_binary(data):
    """Return ``(patched_binary, predicate_name, old_js_size, new_js_size)``."""
    source = bun_handler.extract_js(data)
    patched_source, name = patch_js_bytes(source)
    patched_binary = bun_handler.repack_with_js(data, patched_source)
    return patched_binary, name, len(source), len(patched_source)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        print(f'usage: {sys.argv[0]} <input-binary> [-o <output>]')
        sys.exit(2)

    src = sys.argv[1]
    if '-o' in sys.argv:
        output_index = sys.argv.index('-o') + 1
        if output_index >= len(sys.argv):
            raise SystemExit('-o requires an output path')
        dst = sys.argv[output_index]
    else:
        dst = src + '.model-capabilities'

    data = open(src, 'rb').read()
    print(f'input:        {src} ({len(data)} bytes)')
    try:
        new_data, name, old_js_size, new_js_size = patch_binary(data)
    except PatchError as error:
        raise SystemExit(f'[model-capabilities] {error}') from error

    print(f'JS extracted: {old_js_size} bytes')
    print(f'predicate:    {name} (return!1 -> return!0)')
    print(f'final JS:     {new_js_size} bytes')
    print(f'binary:       {len(new_data)} bytes (delta {len(new_data) - len(data):+d})')
    open(dst, 'wb').write(new_data)
    os.chmod(dst, 0o755)
    print(f'wrote {dst}')


if __name__ == '__main__':
    main()
