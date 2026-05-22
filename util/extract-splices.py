#!/usr/bin/env python3
"""
Diff a patched bundle file against its pre-patch backup and emit a list of
(old, new) splice pairs suitable for a prebuilt apply script.

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
  - Emit (label, old, new) tuples ready to be embedded in a Python list
    of replacements.

Output format: pickle-able dicts written as JSON to stdout.
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


def widen_to_unique(a: bytes, splice_a_start: int, splice_a_end: int):
    """Widen [splice_a_start, splice_a_end) outward until the substring is
    unique-1 in `a`. Return (lo, hi)."""
    pad = MIN_CONTEXT
    while pad <= MAX_CONTEXT:
        lo = max(0, splice_a_start - pad)
        hi = min(len(a), splice_a_end + pad)
        candidate = a[lo:hi]
        if a.count(candidate) == 1:
            return lo, hi
        pad += 20
    raise SystemExit(
        f"Could not widen splice at offset {splice_a_start}-{splice_a_end} "
        f"to unique anchor within {MAX_CONTEXT} bytes."
    )


def extract(unpatched_path: str, patched_path: str):
    with open(unpatched_path, "rb") as f:
        a = f.read()
    with open(patched_path, "rb") as f:
        b = f.read()
    if a == b:
        return []
    regions = find_diff_regions(a, b)
    out = []
    for sa_s, sa_e, sb_s, sb_e in regions:
        anchor_lo, anchor_hi = widen_to_unique(a, sa_s, sa_e)
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
        })
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: extract-splices.py <patched_file> <pre_patch_backup>")
    patched, unpatched = sys.argv[1], sys.argv[2]
    splices = extract(unpatched, patched)
    json.dump(splices, sys.stdout, indent=2)
    print()
