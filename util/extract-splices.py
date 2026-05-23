#!/usr/bin/env python3
"""
Diff a patched bundle file against its pre-patch backup and emit splice records
suitable for a prebuilt apply script.

Usage:
  python3 util/extract-splices.py /path/to/file /path/to/file.bak

Algorithm:
  - Walk byte-by-byte to find all positions where the two files differ.
  - Group adjacent diff positions into contiguous "diff regions" (with a
    small same-byte gap allowed within a region; splices are usually
    separated by lots of unchanged code, but a single splice may have
    interleaved short matches).
  - For each region, anchor it: extract the changed span from each side,
    then widen outward until the unpatched-side substring is unique-1 in
    the unpatched file (the apply script can locate it unambiguously).

Output format: a JSON list of dicts, each:
    {"offset": int, "old": str, "new": str, "expected_count": int}

expected_count is the number of occurrences of `old` the apply step should
replace, and how many it must find (a fail-closed gate). It is 1 for the common
unique-anchor case, which is exactly the historical behavior (find one, replace
one). When a region's context recurs identically at K sites beyond MAX_CONTEXT
so it cannot be made unique, and all K sites received the IDENTICAL edit, the
extractor collapses them into a single record with expected_count=K (a
replace-all). When the K colliding sites received DIFFERENT edits, replace-all
cannot represent them; the extractor exits with a loud collision report naming
the offsets and occurrence count rather than emitting a wrong splice.
"""
import json
import sys


GAP_THRESHOLD = 80   # bytes of identical content allowed within one splice
MIN_CONTEXT = 40     # initial context padding
MAX_CONTEXT = 800    # give up beyond this


def find_diff_regions(a: bytes, b: bytes):
    """Return list of (a_start, a_end, b_start, b_end) for each contiguous
    diff region. Regions are expanded if separated by < GAP_THRESHOLD
    matching bytes."""
    # Use simple alignment: walk forward from start matching a/b, then walk
    # backward from end. The middle is one big region. Within the middle,
    # find sub-regions by looking for runs of matching bytes >= GAP_THRESHOLD.
    n_a, n_b = len(a), len(b)
    pre = 0
    while pre < min(n_a, n_b) and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < min(n_a, n_b) - pre and a[n_a - 1 - suf] == b[n_b - 1 - suf]:
        suf += 1
    a_mid_start, a_mid_end = pre, n_a - suf
    b_mid_start, b_mid_end = pre, n_b - suf

    # Within the middle, find sub-splices via greedy matching:
    # Use a sliding diagonal scan looking for long matching runs.
    # For our case (extension.js with multiple small splices separated by
    # MB of unchanged code), the middle region IS the union of all changed
    # spans plus the unchanged code between them.
    splices = []
    a_pos, b_pos = a_mid_start, b_mid_start
    while a_pos < a_mid_end or b_pos < b_mid_end:
        # Look for a long match starting near (a_pos, b_pos); try shifting
        # b_pos by various deltas to align with the next unchanged run.
        # Simplification: just look for a run of GAP_THRESHOLD identical
        # bytes that exists in both halves.
        # Strategy: find next occurrence of a[a_pos:a_pos+GAP_THRESHOLD] in
        # b starting at b_pos. If found, everything between is one splice.
        if a_pos >= a_mid_end:
            # Trailing insert
            splices.append((a_pos, a_pos, b_pos, b_mid_end))
            break
        if b_pos >= b_mid_end:
            # Trailing delete
            splices.append((a_pos, a_mid_end, b_pos, b_pos))
            break
        # Find next "long matching block" anchored at some a_i, b_j
        next_match = find_next_long_match(
            a, b, a_pos, b_pos, a_mid_end, b_mid_end
        )
        if next_match is None:
            # Rest is one big splice
            splices.append((a_pos, a_mid_end, b_pos, b_mid_end))
            break
        a_match_start, b_match_start, match_len = next_match
        if a_match_start > a_pos or b_match_start > b_pos:
            # There's a splice between current pos and the next match
            splices.append((a_pos, a_match_start, b_pos, b_match_start))
        a_pos = a_match_start + match_len
        b_pos = b_match_start + match_len

    # Filter out zero-size splices (no-op)
    splices = [s for s in splices if (s[1] - s[0]) > 0 or (s[3] - s[2]) > 0]
    return splices


def find_next_long_match(a, b, a_start, b_start, a_end, b_end):
    """Find the next match of GAP_THRESHOLD bytes that appears in both a
    starting at >= a_start and b starting at >= b_start. Return
    (a_pos, b_pos, length) or None.

    Uses a small heuristic: pick the first GAP_THRESHOLD-byte chunk from
    a starting at a_start, find its next occurrence in b[b_start:b_end],
    if found extend the match in both directions and return."""
    if a_end - a_start < GAP_THRESHOLD:
        return None
    # Try anchors at a_start, a_start+1, ..., until we find one that exists in b
    max_anchor_offset = min(2000, a_end - a_start - GAP_THRESHOLD)
    for off in range(0, max_anchor_offset, 1):
        anchor = a[a_start + off : a_start + off + GAP_THRESHOLD]
        # Find anchor in b[b_start:b_end]
        idx = b.find(anchor, b_start, b_end)
        if idx == -1:
            continue
        # Found a match; extend in both directions
        a_match = a_start + off
        b_match = idx
        # Extend forward
        ext = GAP_THRESHOLD
        while (
            a_match + ext < a_end
            and b_match + ext < b_end
            and a[a_match + ext] == b[b_match + ext]
        ):
            ext += 1
        # Extend backward (but not past a_start / b_start)
        back = 0
        while (
            a_match - back - 1 >= a_start
            and b_match - back - 1 >= b_start
            and a[a_match - back - 1] == b[b_match - back - 1]
        ):
            back += 1
        return (a_match - back, b_match - back, ext + back)
    return None


class WidenCollision(Exception):
    """A changed region could not be widened to a unique anchor within
    MAX_CONTEXT because byte-identical context recurs at multiple sites.

    Carries the widest anchor tried and how many times it occurs, so the caller
    can decide whether the colliding sites all take the same edit (representable
    as one expected_count=K splice) or need per-site handling.
    """

    def __init__(self, a_start, a_end, anchor, count):
        self.a_start = a_start
        self.a_end = a_end
        self.anchor = anchor
        self.count = count
        super().__init__(
            f"Could not widen splice at offset {a_start}-{a_end} to a unique "
            f"anchor within {MAX_CONTEXT} bytes; widest anchor still occurs "
            f"{count} times."
        )


def widen_to_unique(a: bytes, splice_a_start: int, splice_a_end: int):
    """Widen [splice_a_start, splice_a_end) outward until the substring is
    unique-1 in `a`. Return (lo, hi).

    Raise WidenCollision (not SystemExit) when the anchor cannot be made unique
    within MAX_CONTEXT, so the caller can attempt the multi-site representation
    before giving up.
    """
    pad = MIN_CONTEXT
    last_lo, last_hi = max(0, splice_a_start - pad), min(len(a), splice_a_end + pad)
    while pad <= MAX_CONTEXT:
        lo = max(0, splice_a_start - pad)
        hi = min(len(a), splice_a_end + pad)
        candidate = a[lo:hi]
        if a.count(candidate) == 1:
            return lo, hi
        last_lo, last_hi = lo, hi
        pad += 20
    widest = a[last_lo:last_hi]
    raise WidenCollision(splice_a_start, splice_a_end, widest, a.count(widest))


def _resolve_collision(a: bytes, b: bytes, sa_s, sa_e, sb_s, sb_e, collision):
    """Attempt to represent a changed region whose anchor context recurs.

    The widest anchor `collision.anchor` occurs `collision.count` times in the
    pre-patch file `a`. If the SAME edit (this anchor with its changed span
    swapped for the patched bytes) applies at every one of those sites, the
    region is representable as a single replace-all splice with
    expected_count=K. Return that splice dict, or None if the sites are not
    uniform (different patched values), in which case the caller reports a hard
    collision that the simple model cannot represent.
    """
    count = collision.count
    if count < 2:
        return None
    # Derive a SITE-INDEPENDENT anchor for the replace-all. widen_to_unique's
    # window was clamped to whichever region it was called on, so two colliding
    # sites near opposite file edges would yield different clamped windows and
    # fail to dedupe (one would be reprocessed as a stale expected_count=1
    # splice). Instead, widen the changed span [sa_s, sa_e) symmetrically until
    # the window occurs exactly `count` times in `a`; because all K colliding
    # sites share the same identical run, this produces the SAME old at every
    # site regardless of edge clamping. Cap at MAX_CONTEXT.
    #
    # The window can clamp at a file edge on one side. That is fine for a
    # replace-all as long as the resulting old still occurs exactly `count`
    # times and maps uniformly to new (both verified below); a clamped-but-still
    # K-occurring window is a valid anchor because the K sites share that run.
    old_bytes = None
    inner_start = inner_end = None
    pad = MIN_CONTEXT
    while pad <= MAX_CONTEXT:
        lo = max(0, sa_s - pad)
        hi = min(len(a), sa_e + pad)
        window = a[lo:hi]
        occ = a.count(window)
        if occ <= count:
            # First pad at which the window stops occurring more than `count`
            # times. occ < count cannot happen (the window contains this site),
            # so occ == count here: a clean replace-all anchor.
            old_bytes = window
            inner_start = sa_s - lo
            inner_end = sa_e - lo
            break
        pad += 20
    if old_bytes is None:
        # Could not pin the occurrence count down to `count` within MAX_CONTEXT;
        # not representable as a uniform replace-all.
        return None
    new_bytes = old_bytes[:inner_start] + b[sb_s:sb_e] + old_bytes[inner_end:]

    # Verify every occurrence of old_bytes in `a` becomes new_bytes in `b`.
    # Because all K sites share identical context (that is why widening failed),
    # the patched file must contain exactly K copies of new_bytes and zero
    # residual copies of old_bytes for a clean replace-all.
    occurrences = a.count(old_bytes)
    if occurrences != count:
        return None
    if b.count(new_bytes) != count:
        return None
    return {
        "offset": sa_s,
        "old": old_bytes.decode("utf-8"),
        "new": new_bytes.decode("utf-8"),
        "expected_count": count,
        # Internal (stripped before emission): where the changed span sits inside
        # `old`, so extract() can mark every covered occurrence.
        "_inner_start": inner_start,
        "_inner_end": inner_end,
    }


def extract(unpatched_path: str, patched_path: str):
    with open(unpatched_path, "rb") as f:
        a = f.read()
    with open(patched_path, "rb") as f:
        b = f.read()
    if a == b:
        return []
    regions = find_diff_regions(a, b)

    # First pass: resolve colliding regions into replace-all splices and record
    # which changed-span positions each one covers. A replace-all for an anchor
    # that occurs K times in `a` covers the changed span inside ALL K occurrences,
    # which correspond to K of the diff regions. Recording those covered spans
    # lets the second pass skip the other regions of the same edit instead of
    # emitting a stale per-site splice for a span the replace-all already handles
    # (this is the mixed unique-plus-collision case that arises when one of the K
    # sites happens to anchor uniquely on its own, e.g. when it sits next to a
    # file edge or a unique boundary).
    out = []
    seen_replace_all = set()
    covered_spans = set()
    for sa_s, sa_e, sb_s, sb_e in regions:
        try:
            widen_to_unique(a, sa_s, sa_e)
            continue  # uniquely anchorable; handled in the second pass
        except WidenCollision as collision:
            multi = _resolve_collision(a, b, sa_s, sa_e, sb_s, sb_e, collision)
            if multi is None:
                _report_hard_collision(sa_s, sa_e, collision)
            old_bytes = multi["old"].encode("utf-8")
            inner_start = multi["_inner_start"]
            inner_end = multi["_inner_end"]
            # Mark the changed span inside every occurrence of the anchor.
            start = 0
            while True:
                pos = a.find(old_bytes, start)
                if pos < 0:
                    break
                covered_spans.add((pos + inner_start, pos + inner_end))
                start = pos + 1
            key = (multi["old"], multi["new"])
            if key not in seen_replace_all:
                seen_replace_all.add(key)
                out.append({k: v for k, v in multi.items() if not k.startswith("_")})

    # Second pass: emit unique splices for every region not already covered by a
    # replace-all from the first pass.
    for sa_s, sa_e, sb_s, sb_e in regions:
        if (sa_s, sa_e) in covered_spans:
            continue
        try:
            anchor_lo, anchor_hi = widen_to_unique(a, sa_s, sa_e)
        except WidenCollision:
            continue  # already turned into a replace-all in the first pass
        old = a[anchor_lo:anchor_hi].decode("utf-8")
        new = (
            a[anchor_lo:sa_s]
            + b[sb_s:sb_e]
            + a[sa_e:anchor_hi]
        ).decode("utf-8")
        out.append({
            "offset": sa_s,
            "old": old,
            "new": new,
            "expected_count": 1,
        })
    _verify_splices(a, b, out)
    return out


def _verify_splices(a: bytes, b: bytes, splices):
    """Fail-closed gate: simulate applying the emitted splices to the pre-patch
    bytes `a` and require the result to equal the patched bytes `b` exactly.

    This catches any case where region resolution produced an inconsistent splice
    set, for example a replace-all that overlaps a separately-emitted unique
    splice for the same edit. Such mixed cases do not arise for the real patches
    (every changed region anchors uniquely), but if a future bundle creates one,
    we refuse to emit a splice set that would not reproduce the target rather
    than shipping a silently broken prebuilt.
    """
    try:
        text = a.decode("utf-8")
        target = b.decode("utf-8")
    except UnicodeDecodeError:
        # Splice old/new are decoded as utf-8 elsewhere, so non-utf-8 inputs are
        # already unsupported; skip the simulation rather than crash here.
        return
    for sp in splices:
        old = sp["old"]
        new = sp["new"]
        expected = sp.get("expected_count", 1)
        count = text.count(old)
        if count != expected:
            raise SystemExit(
                "\n".join([
                    "",
                    "SPLICE VERIFICATION FAILED (extractor self-check).",
                    f"  a splice expects to replace {expected} occurrence(s) of its",
                    f"  anchor, but the (progressively patched) text contains {count}.",
                    "  The emitted splice set would not reproduce the patched file.",
                    "  This usually means two changed regions for the same edit could",
                    "  not be cleanly separated into independent anchors. Refusing to",
                    "  emit a splice set that does not round-trip.",
                    "",
                ]))
        text = text.replace(old, new, expected)
    if text != target:
        raise SystemExit(
            "\n".join([
                "",
                "SPLICE VERIFICATION FAILED (extractor self-check).",
                "  applying the emitted splices to the pre-patch file did not",
                "  reproduce the patched file. Refusing to emit a splice set that",
                "  does not round-trip byte-for-byte.",
                "",
            ]))


def _report_hard_collision(sa_s, sa_e, collision):
    """Emit a loud, actionable report and exit when a changed region cannot be
    represented by either a unique splice or a uniform replace-all splice."""
    msg = [
        "",
        "COLLISION: cannot represent a changed region as a splice.",
        f"  changed region in pre-patch file: bytes {sa_s}-{sa_e}",
        f"  widest {MAX_CONTEXT}-byte anchor still occurs {collision.count} times,",
        "  and the colliding sites did NOT all receive the same edit, so a single",
        "  replace-all splice would corrupt the sites that differ.",
        "",
        "  This is the multi-site case the unique-1 model cannot express. Options:",
        f"    - If the {collision.count} sites SHOULD all get the identical edit, the",
        "      extractor would have emitted one splice with expected_count="
        f"{collision.count}; the fact it did not means the patched bytes differ",
        "      between sites.",
        "    - Use an ordinal splice (replace the k-th occurrence) per differing",
        "      site, applied in descending occurrence order, or an offset-anchored",
        "      splice with a region-hash check. These richer modes are documented",
        "      in the splice apply algorithm but are not auto-emitted here.",
        "",
    ]
    raise SystemExit("\n".join(msg))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: extract-splices.py <patched_file> <pre_patch_backup>")
    patched, unpatched = sys.argv[1], sys.argv[2]
    splices = extract(unpatched, patched)
    json.dump(splices, sys.stdout, indent=2)
    print()
