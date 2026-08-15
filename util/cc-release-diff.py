#!/usr/bin/env python3
"""Compare two Claude Code native builds the way that does not lie.

Motivation lives in ~/claude-skills/team-necromancy/upstream.md. Two comparison
mistakes were made by hand and both are easy to repeat, because the bundle is
minified and every local identifier is renamed between builds:

  1. A diff keyed on an identifier is worthless. A flag-name diff run through
     the gate-reading function's minified name once reported 318 "new" flags,
     because that function has a different name in each build, so the old build
     matched nothing. Diff string LITERALS, never identifiers.

  2. A string literal that embeds a minified identifier is not prose. A diff of
     such literals once made an old feature look new, because renaming moved
     the literal. A prose diff must keep only literals of letters, spaces and
     punctuation.

This script does the three comparisons upstream.md describes, each avoiding the
matching trap:

  literals   diff of a chosen literal family (tengu_ flags, CLAUDE_ env vars,
             --cli-flags, or plain prose), taken on the literals themselves so
             renaming cannot move an entry
  region     structural token comparison of a code region around an anchor
             string: expand outward while identifiers correspond one-to-one and
             consistently, with property names and object keys pinned so a
             .mkdir cannot pass for a .readFile
  fs-ops     filesystem-call census of a region (mkdir/rmdir/open/...), the
             cheap check that a lock or writer region did not change behaviour

It fetches and unpacks builds itself (via bun_handler in this directory), or
takes already-extracted .js bundles. Downloads and extractions are cached under
/tmp so re-runs are fast.

Examples:
  cc-release-diff.py fetch 2.1.220 2.1.226
  cc-release-diff.py literals tengu /tmp/claude-2.1.220.js /tmp/claude-2.1.226.js
  cc-release-diff.py region 'Lock file is already being held' A.js B.js
  cc-release-diff.py fs-ops 'Lock file is already being held' A.js B.js

This is a maintenance tool, not part of any shipped patch or of agent-resume's
own suite. Run it by hand when a release lands.
"""
import os
import re
import signal
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

RELEASES = "https://downloads.claude.ai/claude-code-releases"
PLATFORM = "linux-x64"
CACHE = "/tmp"

# JS keywords and global names that are NOT renamed between builds, so they must
# compare literally in the structural pass rather than as free identifiers.
KEEP = set("""var let const function return if else for while do switch case break continue new
typeof instanceof throw try catch finally delete void in of this null true false undefined class
extends super yield await async static get set Object Error Promise Math JSON Number String Array
Date RegExp Symbol Map Set WeakMap WeakSet process require module exports default import from
Boolean Buffer Infinity NaN globalThis Reflect Proxy""".split())

# Literal families for the `literals` command. Each is a regex whose group 1 is
# the token compared. Prose keeps only letters/spaces/punctuation so an embedded
# minified name disqualifies the literal (that is the point).
FAMILIES = {
    "tengu": r'"(tengu_[a-z0-9_]+)"',
    "env": r'\b(CLAUDE_[A-Z0-9_]{3,60})\b',
    "flags": r'"(--[a-z][a-z0-9-]{2,40})"',
    "prose": r'"([A-Za-z][A-Za-z0-9 ,.\'\":;()/&?!-]{29,300})"',
}

TOKEN = re.compile(r'''
    (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)
  | (?P<id>[A-Za-z_$][A-Za-z0-9_$]*)
  | (?P<num>\d+(?:\.\d+)?(?:e[+-]?\d+)?)
  | (?P<op>\s+|.)
''', re.X | re.S)


def read(path):
    with open(path, encoding="utf8", errors="surrogateescape") as f:
        return f.read()


def fetch(version):
    """Download and extract one build; return the path to its .js bundle.

    Both the binary and the extracted JS are cached under /tmp, so a second call
    for the same version does no network or extraction work.
    """
    import bun_handler
    js = os.path.join(CACHE, f"claude-{version}.js")
    if os.path.exists(js) and os.path.getsize(js) > 1_000_000:
        return js
    binp = os.path.join(CACHE, f"claude-{version}.bin")
    if not (os.path.exists(binp) and os.path.getsize(binp) > 1_000_000):
        url = f"{RELEASES}/{version}/{PLATFORM}/claude"
        sys.stderr.write(f"downloading {url}\n")
        with urllib.request.urlopen(url, timeout=300) as r, open(binp, "wb") as out:
            out.write(r.read())
    data = open(binp, "rb").read()
    if not bun_handler.can_handle(data):
        raise SystemExit(f"{binp}: not a bun ELF this handler supports")
    with open(js, "wb") as out:
        out.write(bun_handler.extract_js(data))
    sys.stderr.write(f"extracted {js} ({os.path.getsize(js)} bytes)\n")
    return js


def channel(name):
    with urllib.request.urlopen(f"{RELEASES}/{name}", timeout=30) as r:
        return r.read().decode().strip()


def cmd_fetch(args):
    """fetch <version|stable|latest> [more...] — download, extract, print paths."""
    if not args:
        print(f"stable={channel('stable')}  latest={channel('latest')}")
        return 0
    for a in args:
        v = channel(a) if a in ("stable", "latest") else a
        print(fetch(v))
    return 0


def cmd_literals(args):
    """literals <family> <A.js> <B.js> [keyword] — diff one literal family.

    Rename-proof: the compared token is the literal itself, so a renamed
    identifier cannot move an entry. An optional case-insensitive keyword keeps
    only literals containing it, which is how the broad `prose` family becomes
    useful — unfiltered it is thousands of unrelated strings. `prose` without a
    keyword prints a count and a warning rather than the full list.
    """
    if len(args) not in (3, 4) or args[0] not in FAMILIES:
        raise SystemExit(f"usage: literals <{'|'.join(FAMILIES)}> A.js B.js [keyword]")
    fam, a, b = args[0], args[1], args[2]
    kw = args[3].lower() if len(args) == 4 else None
    pat = re.compile(FAMILIES[fam])

    def keep(x):
        return kw is None or kw in x.lower()

    A = set(x for x in pat.findall(read(a)) if keep(x))
    B = set(x for x in pat.findall(read(b)) if keep(x))
    scope = f" matching {kw!r}" if kw else ""
    print(f"{fam}{scope}: A={len(A)}  B={len(B)}  new={len(B - A)}  gone={len(A - B)}")
    if fam == "prose" and kw is None:
        print("  (prose without a keyword is mostly noise; pass a keyword to scope it)")
        return 0
    for label, s in (("NEW", B - A), ("GONE", A - B)):
        for x in sorted(s):
            print(f"  {label:4} {x}")
    return 0


def _tokens(s):
    """Tokenise, dropping whitespace; pin property names and object keys.

    A name after '.' or before ':' is marked 'pinned' so the structural compare
    requires it to match literally. Without that a renamed .mkdir could pair
    with a renamed .readFile and a changed region would read as identical.
    """
    out = []
    for m in TOKEN.finditer(s):
        k = m.lastgroup
        v = m.group()
        if k == "op" and v.isspace():
            continue
        out.append([k, v])
    for i, (k, v) in enumerate(out):
        if k != "id":
            continue
        prev = out[i - 1][1] if i else ""
        nxt = out[i + 1][1] if i + 1 < len(out) else ""
        if prev == "." or nxt == ":":
            out[i][0] = "pinned"
    return [(k, v) for k, v in out]


def _structural_run(ta, tb, ia, ib):
    """Longest run around anchors ia/ib where the two token lists correspond.

    Identifiers must map one-to-one and consistently in BOTH directions; keywords,
    pinned names, strings and numbers must match literally. Returns (lo, hi,
    renames) so the matched span is ta[ia-lo : ia+hi].
    """
    fwd, rev = {}, {}

    def same(x, y):
        (ka, va), (kb, vb) = x, y
        if ka != kb:
            return False
        if ka == "id" and va not in KEEP and vb not in KEEP:
            if fwd.get(va, vb) != vb or rev.get(vb, va) != va:
                return False
            fwd[va] = vb
            rev[vb] = va
            return True
        return va == vb

    hi = 0
    while ia + hi < len(ta) and ib + hi < len(tb) and same(ta[ia + hi], tb[ib + hi]):
        hi += 1
    lo = 0
    while ia - lo > 0 and ib - lo > 0 and same(ta[ia - lo - 1], tb[ib - lo - 1]):
        lo += 1
    return lo, hi, fwd


def cmd_region(args):
    """region <anchor> <A.js> <B.js> [pad] — structural compare around a string.

    Prints the size of the corresponding span (identifiers mapped one-to-one,
    keywords and pinned names matched literally) and the first text on each side
    where correspondence breaks. There is deliberately no "identical" boolean:
    the matched span always corresponds by construction, and comparing its raw
    text would just report that identifiers were renamed, which they always are.
    Read the divergence instead — if it falls in code unrelated to what you are
    checking (a neighbouring function), the region of interest is unchanged.
    """
    if len(args) not in (3, 4):
        raise SystemExit("usage: region <anchor-substring> A.js B.js [pad]")
    anchor, a, b = args[:3]
    pad = int(args[3]) if len(args) == 4 else 20000
    da, db = read(a), read(b)
    pa, pb = da.find(anchor), db.find(anchor)
    if pa < 0 or pb < 0:
        raise SystemExit(f"anchor not found in {'A' if pa < 0 else 'B'}")
    ta = _tokens(da[max(0, pa - pad):pa + pad])
    tb = _tokens(db[max(0, pb - pad):pb + pad])

    def anchor_ix(ts):
        return next(i for i, (k, v) in enumerate(ts) if anchor in v)

    ia, ib = anchor_ix(ta), anchor_ix(tb)
    lo, hi, renames = _structural_run(ta, tb, ia, ib)
    sa = "".join(v for k, v in ta[ia - lo:ia + hi])
    print(f"corresponding span: {lo + hi} tokens, {len(sa)} chars "
          f"({lo} back + {hi} fwd from the anchor)")
    print(f"identifiers renamed one-to-one: {len(renames)}")
    if not (lo + hi):
        print("  region differs at the anchor token itself")
        return 1
    back_a = "".join(v for k, v in ta[max(0, ia - lo - 12):ia - lo])
    back_b = "".join(v for k, v in tb[max(0, ib - lo - 12):ib - lo])
    fwd_a = "".join(v for k, v in ta[ia + hi:ia + hi + 12])
    fwd_b = "".join(v for k, v in tb[ib + hi:ib + hi + 12])
    print(f"  diverges backward:\n    A: ...{back_a[-110:]}\n    B: ...{back_b[-110:]}")
    print(f"  diverges forward:\n    A: {fwd_a[:110]}...\n    B: {fwd_b[:110]}...")
    print("Read both divergences: if each is in code unrelated to your target,")
    print("the region between them is unchanged apart from renaming.")
    return 0


FS_OPS = ["mkdir", "rmdir", "utimes", "stat", "readFile", "writeFile",
          "open(", "appendFile", "unlink", "readdir", "chmod", "rename", "link"]


def cmd_fsops(args):
    """fs-ops <start> <end> <A.js> <B.js> — count fs calls between two anchors.

    Bound the region precisely with a start and end substring, both present in
    both builds, so the census covers your region and not a neighbouring
    function. A pad-based window was tried and rejected: it reached into
    adjacent code and reported a difference (a stat count) that was not in the
    region under test.
    """
    if len(args) != 4:
        raise SystemExit("usage: fs-ops <start-substring> <end-substring> A.js B.js")
    start, end, a, b = args
    out = {}
    for label, path in (("A", a), ("B", b)):
        d = read(path)
        p = d.find(start)
        if p < 0:
            raise SystemExit(f"start anchor not found in {label}")
        q = d.find(end, p)
        if q < 0:
            raise SystemExit(f"end anchor not found in {label} after the start")
        seg = d[p:q + len(end)]
        out[label] = {op: seg.count(op) for op in FS_OPS}
        out[label + "_len"] = len(seg)
    print(f"region: A={out['A_len']} chars, B={out['B_len']} chars")
    diff = [op for op in FS_OPS if out["A"][op] != out["B"][op]]
    for op in FS_OPS:
        mark = "  <-- differs" if op in diff else ""
        print(f"  {op:12} A={out['A'][op]:3}  B={out['B'][op]:3}{mark}")
    print("IDENTICAL fs-call census" if not diff else f"DIFFERS on: {', '.join(diff)}")
    return 0 if not diff else 1


COMMANDS = {
    "fetch": cmd_fetch,
    "literals": cmd_literals,
    "region": cmd_region,
    "fs-ops": cmd_fsops,
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write(__doc__)
        sys.stderr.write("\ncommands: " + ", ".join(COMMANDS) + "\n")
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    # Let a downstream `head` closing the pipe end this quietly instead of
    # raising BrokenPipeError through a half-printed list.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main(sys.argv[1:]))
