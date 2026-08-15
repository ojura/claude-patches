import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import patch_model_capabilities as patch  # noqa: E402


UNCHANGED_LOG = '[modelCapabilities] cache unchanged, skipping write'
CACHED_LOG = '[modelCapabilities] cached '
FAILED_LOG = '[modelCapabilities] fetch failed:'


def feature_bundle(name='uZc', stub=None, logs=None, quote='"'):
    if stub is None:
        stub = f'function {name}(){{return!1}}'
    if logs is None:
        logs = (
            f'log("{UNCHANGED_LOG}");',
            'log(`[modelCapabilities] cached ${models.length} models`);',
            'log(`[modelCapabilities] fetch failed: ${error}`);',
        )
    return (
        'const pre="model-api fixture";'
        'function outsideBefore(){return!1}'
        'function cacheDir(){return join(home(),"cache")}'
        f'function cachePath(){{return join(cacheDir(),{quote}model-capabilities.json{quote})}}'
        f'{stub}'
        'function sortModels(models){return[...models].sort()}'
        'function getCapability(model){if(!eligible())return;return model}'
        'async function refreshCapabilities(){'
        + ''.join(logs)
        + '}'
        'function outsideAfter(){return!1}'
    )


@pytest.mark.parametrize('name', ['uZc', '$a', '_x9', 'abc$'])
def test_patches_discovered_identifier_with_one_byte_change(name):
    original = feature_bundle(name=name)
    patched, discovered = patch.patch_js(original)

    assert discovered == name
    assert len(patched) == len(original)
    assert f'function {name}(){{return!1}}' not in patched
    assert f'function {name}(){{return!0}}' in patched
    differences = [
        (before, after)
        for before, after in zip(original, patched)
        if before != after
    ]
    assert differences == [('1', '0')]


def test_ignores_disabled_decoys_outside_feature_cluster():
    original = feature_bundle(name='target')
    patched, _ = patch.patch_js(original)

    assert 'function outsideBefore(){return!1}' in patched
    assert 'function outsideAfter(){return!1}' in patched
    assert 'function target(){return!0}' in patched


@pytest.mark.parametrize(
    'anchor',
    ['model-capabilities.json', UNCHANGED_LOG, CACHED_LOG, FAILED_LOG],
)
def test_missing_anchor_fails_closed(anchor):
    original = feature_bundle().replace(anchor, 'anchor-missing', 1)
    with pytest.raises(patch.PatchError, match='anchor count 0 != 1'):
        patch.patch_js(original)


@pytest.mark.parametrize(
    'anchor',
    ['model-capabilities.json', UNCHANGED_LOG, CACHED_LOG, FAILED_LOG],
)
def test_duplicate_anchor_fails_closed(anchor):
    original = feature_bundle() + anchor
    with pytest.raises(patch.PatchError, match='anchor count 2 != 1'):
        patch.patch_js(original)


def test_reordered_anchors_fail_closed():
    logs = (
        'log(`[modelCapabilities] cached ${models.length} models`);',
        f'log("{UNCHANGED_LOG}");',
        'log(`[modelCapabilities] fetch failed: ${error}`);',
    )
    with pytest.raises(patch.PatchError, match='not in order'):
        patch.patch_js(feature_bundle(logs=logs))


def test_no_disabled_candidate_in_feature_region_fails():
    original = feature_bundle(stub='function eligible(){return false}')
    with pytest.raises(patch.PatchError, match='expected 1 match, got 0'):
        patch.patch_js(original)


def test_two_disabled_candidates_in_feature_region_fail():
    original = feature_bundle(
        stub='function first(){return!1}function second(){return!1}'
    )
    with pytest.raises(patch.PatchError, match='expected 1 match, got 2'):
        patch.patch_js(original)


def test_already_patched_input_fails_loudly():
    original = feature_bundle(stub='function eligible(){return!0}')
    with pytest.raises(patch.PatchError, match='always-true candidate'):
        patch.patch_js(original)


@pytest.mark.parametrize(
    'stub',
    [
        'function eligible(){return false}',
        'function eligible(){ return!1}',
        'function eligible(thing){return!1}',
        'const eligible=()=>!1;',
    ],
)
def test_drifted_predicate_forms_fail_closed(stub):
    with pytest.raises(patch.PatchError, match='expected 1 match, got 0'):
        patch.patch_js(feature_bundle(stub=stub))


def test_string_delimiters_are_not_assumed():
    logs = (
        f"log('{UNCHANGED_LOG}');",
        'log(`[modelCapabilities] cached ${models.length} models`);',
        'log(`[modelCapabilities] fetch failed: ${error}`);',
    )
    original = feature_bundle(name='$eligible', logs=logs, quote="'")
    patched, discovered = patch.patch_js(original)
    assert discovered == '$eligible'
    assert 'function $eligible(){return!0}' in patched


def test_surrogateescape_round_trip_preserves_invalid_utf8():
    source = b'\x80\xfe' + feature_bundle(name='_eligible').encode() + b'\xff\x81'
    patched, discovered = patch.patch_js_bytes(source)

    assert discovered == '_eligible'
    assert len(patched) == len(source)
    assert patched[:2] == b'\x80\xfe'
    assert patched[-2:] == b'\xff\x81'
    assert b'function _eligible(){return!0}' in patched
    differences = [
        (before, after)
        for before, after in zip(source, patched)
        if before != after
    ]
    assert differences == [(ord('1'), ord('0'))]


def test_binary_orchestration_extracts_and_repacks_once(monkeypatch):
    source = feature_bundle(name='runtimeGate').encode()
    calls = []

    def extract_js(data):
        calls.append(('extract', data))
        return source

    def repack_with_js(data, new_js):
        calls.append(('repack', data, new_js))
        return b'REPACKED:' + new_js

    monkeypatch.setattr(patch.bun_handler, 'extract_js', extract_js)
    monkeypatch.setattr(patch.bun_handler, 'repack_with_js', repack_with_js)

    binary = b'fake bun binary'
    output, name, old_size, new_size = patch.patch_binary(binary)

    assert name == 'runtimeGate'
    assert old_size == len(source)
    assert new_size == len(source)
    assert calls[0] == ('extract', binary)
    assert calls[1][0:2] == ('repack', binary)
    assert b'function runtimeGate(){return!0}' in calls[1][2]
    assert len([call for call in calls if call[0] == 'repack']) == 1
    assert output == b'REPACKED:' + calls[1][2]
