"""Validate pfg's syntax, bindings, linking and built-in scan on the installed Claude Code 2.1.304.

The tests skip when that binary is absent. CLAUDE_PFG_STRICT_GATES=pfg304 makes its absence a failure.
The catalog repository separately tests its table predicates and expected table values.
"""
import os
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from pfg import bundle as pb  # noqa: E402

BINARY = Path.home() / ".local/share/claude/versions/2.1.304"
NEEDLES = [pb.Needle("allowlist", "string", "tengu_prompt_cache_1h_config"),
           pb.Needle("idle", "string", "tengu_sunny_locket"),
           pb.Needle("palette", "string", "red_FOR_SUBAGENTS_ONLY")]


@pytest.fixture(scope="module")
def native():
    if not BINARY.is_file():
        message = f"Claude Code 2.1.304 is absent at {BINARY}"
        if "pfg304" in os.environ.get("CLAUDE_PFG_STRICT_GATES", "").split(","):
            pytest.fail(message)
        pytest.skip(message)
    with BINARY.open("rb") as file:
        before = os.fstat(file.fileno())
        with pb.Bundle.from_binary(file, NEEDLES) as bundle:
            after = os.fstat(file.fileno())
            assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            yield bundle


def test_every_module_is_parsed_and_the_entrypoint_is_in_its_program(native):
    assert len(native.modules) == 2461
    assert all(record["diagnostics"] == [] for record in native.records.values())
    assert native.modules[7].name == "/$bunfs/root/cli"
    assert any("Version: 2.1.304" in comment["text"] for comment in native.leading_comments(7))


def test_needle_hits_name_the_modules_with_the_flags_and_palette(native):
    assert {index for index, _ in native.hits("allowlist")} == {442}
    assert {index for index, _ in native.hits("idle")} == {442}
    assert {index for index, _ in native.hits("palette")} == {365}
    for key in ("allowlist", "idle", "palette"):
        for index, hit in native.hits(key):
            node = native.syntax(index, hit["h"], 0)
            assert node["value"] == native.needles[key].value
            assert (node["s"], node["e"]) == (hit["s"], hit["e"])


def test_the_blocked_set_export_reaches_its_declaring_module(native):
    kind, index, name = native.export(959, "PROJECT_SCOPE_BLOCKED_ENV_VARS")
    assert kind == "binding" and index == 199
    record = native.scope_binding(index, name)
    assert record.get("refused") is None
    assert record["declarations"][0]["module"] is True
    assert native.occurrences(index, record["id"])


def test_factory_bindings_keep_their_identity_across_scope_queries_and_reparses(native):
    kind, index, name = native.export(42, "H")
    assert kind == "binding" and index == 42
    record = native.scope_binding(index, name)
    assert record.get("refused") is None
    position = record["declarations"][0]["name"]
    assert native.binding_at(index, position["s"], position["e"])["id"] == record["id"]
    first = native.occurrences(index, record["id"])
    native.leading_comments(7)
    assert native.occurrences(index, record["id"]) == first


def test_the_native_sources_use_the_same_codepoint_and_utf16_offsets(native):
    # Bun stores these sources as Latin-1, which cannot contain a character above U+FFFF.
    for module in native.modules.values():
        assert module.codepoints == module.length
        assert module.to_utf16(module.codepoints) == module.length
        assert module.to_codepoints(module.length) == module.codepoints


def test_visible_builtin_writes_and_computed_global_keys_are_classified(native):
    summary = native.builtin_summary(native.builtin_scan())
    assert {entry["target"] for entry in summary["writes"]} == {"Error.stackTraceLimit", "Error.prepareStackTrace"}
    assert {entry["key"] for entry in summary["globalWrites"] if entry["module"] == 442} == {"h", "Fragment"}
    assert {(entry["module"], entry["target"]) for entry in summary["registryWrites"]} == {
        (161, "globalThis.@registry"), (268, "globalThis.@registry"), (1520, "globalThis.@registry")}
    assert all(entry["status"] in ("followed", "unclassified") for entry in summary["references"])
    assert any(entry["module"] == 433 and "cycle of stored references" in entry["reason"] for entry in summary["unfollowed"])
    assert any(entry["module"] == 2254 and entry["kind"] == "direct eval" for entry in summary["dynamic"])


def test_the_native_helper_is_the_loaded_protocol_and_typescript_version(native):
    assert native.typescript_version == "5.9.3"
    assert native.node_version.startswith("v")
    assert int((REPO / "pfg/REVISION").read_text().strip()) == pb.REVISION
