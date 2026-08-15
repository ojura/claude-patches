"""
pfg CLI:
  python3 -m pfg discover   <bundle.js> [target]      resolve vendor names + coverage
  python3 -m pfg crosscheck <bundle.js> [target]      per-anchor match count, no abort
  python3 -m pfg span       <bundle.js> <fnname>      extract a function span (debug)
  python3 -m pfg apply      <extension.js> [--force]  patch a file (extension adapter)

`crosscheck` is the 2b re-anchor worklist generator: unlike discover (which aborts
on the first drifted anchor), it reports every anchor's count so a target bump
surfaces all the re-anchor tickets in one pass.
"""
import re
import sys

from . import anchors, discovery, jslex
from .adapters import extension


def main(argv):
    if not argv:
        sys.stdout.write(__doc__); return 0
    cmd = argv[0]

    if cmd == "discover" and len(argv) >= 2:
        js = open(argv[1], encoding="utf-8").read()
        target = argv[2] if len(argv) >= 3 else "extension"
        for pv, name in sorted(discovery.discover(js, target).items()):
            print(f"  {pv:10} -> {name}")
        discovery.coverage(target)
        print(f"coverage OK ({target}): core surface and registry are 1:1")
        return 0

    if cmd == "crosscheck" and len(argv) >= 2:
        js = open(argv[1], encoding="utf-8").read()
        target = argv[2] if len(argv) >= 3 else "extension"
        print(f"crosscheck ({target}): per-anchor match count, no abort")
        bad = 0
        for a in anchors.ANCHORS:
            label = " + ".join(d.pv for d, _g in a.deps)
            if target not in a.regexes:
                print(f"  SKIP   {label}"); continue
            n = len(re.findall(a.regexes[target], js))
            if n != 1:
                bad += 1
            print(f"  {'OK  ' if n == 1 else f'({n})!'} {label}")
        print(f"{bad} anchor(s) need a {target}-specific variant" if bad
              else f"all anchors resolve uniquely on {target}")
        return 0

    if cmd == "span" and len(argv) >= 3:
        js = open(argv[1], encoding="utf-8").read()
        s, e = jslex.find_function_span(js, argv[2])
        print(f"{argv[2]}: [{s}:{e}] len {e - s}")
        print(f"  head {js[s:s + 70]!r}")
        print(f"  tail ...{js[e - 40:e]!r}")
        return 0

    if cmd == "apply" and len(argv) >= 2:
        extension.apply_to_file(argv[1], force="--force" in argv)
        return 0

    sys.stderr.write(f"unknown or malformed command: {' '.join(argv)}\n\n")
    sys.stderr.write(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
