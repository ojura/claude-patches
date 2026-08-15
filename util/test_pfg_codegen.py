"""
Codegen per-target minimal $pfg subset (task-23 core distribution): each target draws only
the pfg-core members it uses + their transitive deps from the one src/pfg-core.js, so a
second core copy is impossible-by-construction and the webview never ships the extension's
lineage logic. These are hermetic unit tests over the codegen's graph + subset + bijection.
"""
import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
# pfg-codegen.py has a hyphen, so import it by path.
_spec = importlib.util.spec_from_file_location("pfg_codegen", os.path.join(_HERE, "pfg-codegen.py"))
cg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cg)


def test_member_blocks_reconstruct_the_source():
    """Header + every block, verbatim (bar the stripped `export `), reproduces pfg-core.js:
    the subset emitter never drops or reorders source."""
    src = cg.read_src()
    header, blocks = cg._member_blocks(src)
    rebuilt = header + "".join(text for _n, _e, text in blocks)
    assert rebuilt == src.replace("export const", "const")


def test_extension_subset_exposes_exactly_its_pfg_refs():
    """The extension exposes exactly what d1e/i1e reference via $pfg (its direct refs); the
    members used only INTERNALLY (contentText, isContinuationPreamble, CONTINUATION_PREAMBLE)
    are emitted for the closure but NOT exposed."""
    block = cg.build_block("extension")
    refs = cg._pfg_refs("extension")
    # the IIFE's exposed return is the LAST `return { ... }` (members' own bodies have
    # earlier `return { reachedRoot... }` object literals), so rsplit.
    exposed = set(block.rsplit("return { ", 1)[1].split(" }")[0].split(", "))
    assert exposed == refs
    # internal-only members are present in the body (transitive deps) but not exposed
    assert "isContinuationPreamble" in block and "isContinuationPreamble" not in exposed


def test_webview_subset_derives_from_render_pfg_refs():
    """render.js now reads $pfg.isGhostUuid (out-of-band ghost detection on the uuid) and
    $pfg.PFGK1_PREFIX (payload envelope), so the webview $pfg subset is usage-derived to
    exactly those two, plus GHOST_PREFIX (isGhostUuid's transitive dep). It was empty
    pre-swap, when render.js parsed the "pfgk-" prefix inline (the retired roleFromUuid)."""
    assert cg._pfg_refs("webview") == {"isGhostUuid", "PFGK1_PREFIX"}
    block = cg.build_block("webview")
    assert block != ""
    for member in ("isGhostUuid", "PFGK1_PREFIX", "GHOST_PREFIX"):
        assert member in block, f"{member} missing from the webview subset"


def test_closure_is_downward_closed():
    """Every bare name a needed member references is itself emitted, so no reference dangles.
    Checked on the real graph: the closure of {classifyRoot} pulls in its transitive deps."""
    _h, blocks = cg._member_blocks(cg.read_src())
    graph = cg._dep_graph(blocks)
    needed = cg._closure({"classifyRoot"}, graph)
    for n in needed:
        assert graph.get(n, set()) <= needed, f"{n} references {graph[n] - needed} outside the closure"
    # classifyRoot -> isOrigin/isContinuationPreamble -> contentText/CONTINUATION_PREAMBLE
    assert {"isOrigin", "isContinuationPreamble", "contentText"} <= needed


def test_linker_is_member_type_agnostic(monkeypatch):
    """The subset is a GENERAL linker: it links ANY referenced core member, a PRIMITIVE
    FUNCTION as much as a constant, with the transitive closure of what that member itself
    uses. NOT constants-only (that would re-create the duplication pressure the moment a
    target needs a core function)."""
    _h, blocks = cg._member_blocks(cg.read_src())
    graph = cg._dep_graph(blocks)
    # a function member pulls in the members it uses (functions AND consts): ghostKind -> GHOST_PREFIX
    assert "GHOST_PREFIX" in cg._closure({"ghostKind"}, graph)
    # a target that references ONLY a function links that function + its transitive FUNCTION
    # deps, and exposes the function.
    monkeypatch.setattr(cg, "_pfg_refs", lambda target: {"walkToRoot"})
    block = cg.build_block("extension")
    assert "const walkToRoot" in block                       # the referenced fn is emitted
    assert "const edge" in block                             # a transitive FUNCTION dep is linked too
    assert block.rsplit("return { ", 1)[1].split(" }")[0] == "walkToRoot"  # exposed = exactly the fn ref


def test_membership_bijection_fails_loud_on_unexported_ref(monkeypatch):
    """A $pfg.<member> a target calls that pfg-core does not export fails loud at build time
    (else it passes node --check and throws TypeError at run time)."""
    monkeypatch.setattr(cg, "_pfg_refs", lambda target: {"edge", "notARealMember"})
    with pytest.raises(SystemExit) as ei:
        cg.check_membership("extension", ["edge", "isOrigin"])
    assert "notARealMember" in str(ei.value) and "NOT exported" in str(ei.value)


def test_default_target_is_extension():
    assert cg._target_arg([]) == "extension"
    assert cg._target_arg(["--target", "webview"]) == "webview"
