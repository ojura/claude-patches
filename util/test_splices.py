#!/usr/bin/env python3
"""
Standalone test harness for the splice tooling:
  - util/extract-splices.py: region diffing + widening + expected_count emission
    + the loud collision report that replaces the old bare SystemExit.
  - the expected_count apply ALGORITHM (defined here as the canonical reference),
    proving default-1 reproduces today's `str.replace(old,new,1)` + `count==1`
    behavior exactly, and expected_count=N handles the multi-site case.

This proves the tooling in isolation. Nothing here is wired into the shipped
prebuilt apply.py; that integration is a separate, later step.

Run: python3 util/test_splices.py
Exit code 0 on full pass, 1 otherwise.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# extract-splices.py has a hyphen, so import it by file path.
_spec = importlib.util.spec_from_file_location(
    "extract_splices", os.path.join(HERE, "extract-splices.py"))
extract_splices = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_splices)


# ---------------------------------------------------------------------------
# Reference apply algorithm with expected_count support.
#
# This is the canonical splice apply semantics the shipped apply.py will adopt.
# Each splice is a dict: {"old": str, "new": str, "expected_count": int}.
# Default expected_count is 1 == today's behavior:
#     count must equal expected_count, then replace exactly that many.
# ---------------------------------------------------------------------------

def apply_splices(text: str, splices) -> str:
    for i, sp in enumerate(splices):
        old = sp["old"]
        new = sp["new"]
        expected = sp.get("expected_count", 1)
        count = text.count(old)
        if count == 0:
            # Idempotency: treat as already-applied if the result is present.
            if text.count(new) >= max(1, expected):
                continue
            raise ValueError(f"splice {i}: anchor not found (count=0)")
        if count != expected:
            raise ValueError(
                f"splice {i}: anchor count {count} != expected_count {expected}")
        text = text.replace(old, new, expected)
    return text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

PASS = []


def check(name, ok, detail=""):
    PASS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def write_pair(tmpdir, pre, post):
    pre_p = os.path.join(tmpdir, "pre.js")
    post_p = os.path.join(tmpdir, "post.js")
    with open(pre_p, "w") as f:
        f.write(pre)
    with open(post_p, "w") as f:
        f.write(post)
    return pre_p, post_p


def test_default_count_matches_today(tmp):
    """A normal unique edit: extractor emits expected_count=1, and apply with the
    reference algorithm equals today's str.replace(old,new,1) result exactly."""
    print("test: default expected_count=1 reproduces today's behavior")
    pre = "alpha UNIQUE_LEFT render(x,{v:cfg.v}) UNIQUE_RIGHT omega and other text here"
    post = pre.replace("render(x,{v:cfg.v})", "render(x,{v:!0})")
    pre_p, post_p = write_pair(tmp, pre, post)
    splices = extract_splices.extract(pre_p, post_p)
    check("emits exactly one splice", len(splices) == 1, f"got {len(splices)}")
    if splices:
        check("expected_count defaults to 1", splices[0].get("expected_count") == 1)
    # Reference apply reproduces post.
    out = apply_splices(pre, splices)
    check("reference apply reproduces patched output", out == post)
    # And it equals the literal today's-behavior path (single replace under count==1).
    today = pre
    for sp in splices:
        assert today.count(sp["old"]) == 1
        today = today.replace(sp["old"], sp["new"], 1)
    check("identical to today's count==1 + replace(...,1)", today == post)


def test_multi_site_expected_count(tmp):
    """Two byte-identical edit sites with identical context > MAX_CONTEXT: the
    extractor must collapse them to ONE expected_count=2 splice, and the apply
    algorithm must reproduce the patched output."""
    print("test: multi-site identical edit -> expected_count=N")
    filler = ("function pad(){let acc=0;"
              + "acc+=Math.sin(acc)*Math.cos(acc);" * 120
              + "return acc}\n")
    assert len(filler) > 2 * extract_splices.MAX_CONTEXT
    site_pre = 'render(opts,{verbose:cfg.v,mode:"compact"});'
    site_post = 'render(opts,{verbose:!0,mode:"compact"});'
    unit = filler + site_pre + filler
    boundary = "\n/*BOUNDARY_A*/\n" + "z" * 50 + "\n/*BOUNDARY_B*/\n"
    pre = "//HEAD\n" + unit + boundary + unit + "//FOOT\n"
    post = pre.replace(site_pre, site_post)
    assert post.count(site_post) == 2

    pre_p, post_p = write_pair(tmp, pre, post)
    splices = extract_splices.extract(pre_p, post_p)
    check("collapses to a single splice", len(splices) == 1, f"got {len(splices)}")
    if splices:
        sp = splices[0]
        check("expected_count == 2", sp.get("expected_count") == 2,
              f"got {sp.get('expected_count')}")
        check("old occurs exactly 2x in pre", pre.count(sp["old"]) == 2)
        check("new occurs exactly 2x in post", post.count(sp["new"]) == 2)
    out = apply_splices(pre, splices)
    check("reference apply reproduces patched output", out == post)


def test_expected_count_gate_rejects_wrong_count(tmp):
    """The apply gate must refuse when the actual count != expected_count, rather
    than silently under/over-applying."""
    print("test: expected_count gate fails closed on count mismatch")
    text = "aXa Xb Xc"  # 'X' appears 3 times
    splices = [{"old": "X", "new": "Y", "expected_count": 2}]
    raised = False
    try:
        apply_splices(text, splices)
    except ValueError:
        raised = True
    check("count 3 vs expected 2 raises", raised)

    # And the correct count applies all of them.
    splices_ok = [{"old": "X", "new": "Y", "expected_count": 3}]
    out = apply_splices("aXa Xb Xc", splices_ok)
    check("expected_count=3 replaces all three", out == "aYa Yb Yc", repr(out))


def test_hard_collision_reports_not_silent(tmp):
    """When two identical-context sites need DIFFERENT edits, replace-all cannot
    represent it; the extractor must raise a loud SystemExit (collision report),
    not silently produce a wrong splice."""
    print("test: non-uniform multi-site -> loud collision report")
    filler = ("function pad(){let acc=0;"
              + "acc+=Math.sin(acc)*Math.cos(acc);" * 120
              + "return acc}\n")
    site_pre = 'render(opts,{verbose:cfg.v,mode:"compact"});'
    unit = filler + site_pre + filler
    boundary = "\n/*BOUNDARY_A*/\n" + "z" * 50 + "\n/*BOUNDARY_B*/\n"
    pre = "//HEAD\n" + unit + boundary + unit + "//FOOT\n"
    # Patch the two identical sites DIFFERENTLY.
    idx1 = pre.find(site_pre)
    idx2 = pre.find(site_pre, idx1 + len(site_pre))
    post = (pre[:idx1] + 'render(opts,{verbose:!0,mode:"compact"});'
            + pre[idx1 + len(site_pre):idx2]
            + 'render(opts,{verbose:!1,mode:"compact"});'
            + pre[idx2 + len(site_pre):])
    pre_p, post_p = write_pair(tmp, pre, post)
    raised = False
    try:
        extract_splices.extract(pre_p, post_p)
    except SystemExit as exc:
        raised = True
        msg = str(exc)
        check("report names the collision", "COLLISION" in msg)
        check("report states occurrence count", "occurs 2 times" in msg)
    check("non-uniform multi-site raises SystemExit (no silent wrong splice)", raised)


def main():
    import tempfile
    print("=== splice tooling standalone tests ===")
    with tempfile.TemporaryDirectory() as tmp:
        test_default_count_matches_today(tmp)
        test_multi_site_expected_count(tmp)
        test_expected_count_gate_rejects_wrong_count(tmp)
        test_hard_collision_reports_not_silent(tmp)
    ok = all(PASS)
    print(f"\nSUITE {'PASS' if ok else 'FAIL'} ({sum(PASS)}/{len(PASS)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
