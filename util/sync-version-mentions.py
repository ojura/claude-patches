#!/usr/bin/env python3
"""
Rewrite every `pfg-vX[.Y]` mention in SYNC_TARGETS to the current
PATCHSET_VERSION (extracted from skill/SKILL.md via version.py).

Run this as the LAST step before committing a version bump. The
intended workflow:

  1. Bump `**Patchset version**` in skill/SKILL.md (the SSOT).
  2. Re-apply patches locally and confirm the only diff vs the
     pre-bump live extension is the signature tag — anything else
     means apply-patch-fg.py drifted.
  3. python3 util/build-prebuilt.py <ext_dir> prebuilt/<VER>
  4. Add a CHANGELOG.md entry for the new version.
  5. git add -A
  6. python3 util/sync-version-mentions.py
  7. git diff   ← review carefully. SYNC_TARGETS is an allowlist;
                  any rewrite outside it would mean the allowlist
                  drifted. Catches the case where a doc file gained
                  a current-state pfg-v claim that shouldn't be
                  rewritten on every bump.
  8. git add -A && git commit ...

CHANGELOG.md, docs/debugging.md, and prebuilt/archive/* are
deliberately NOT in SYNC_TARGETS because their `pfg-vX` mentions
are historical and must stay frozen.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)
from version import PATCHSET_VERSION  # SSOT extracted from skill/SKILL.md


# README.md is the only file where a literal `pfg-vX` mention earns its
# keep: it's the public face of the repo, users grep README to confirm
# what signature they're getting, and you want the literal version visible
# on first glance without chasing a Python script.
#
# Every other file resolves the version dynamically:
#   - skill/SKILL.md         bash blocks call `python3 version.py`; prose
#                            uses "the patchset signature" abstractly.
#   - MAINTAINER.md          same approach — internal doc, same audience as
#                            SKILL.md, no need to hardcode.
#   - skill/apply-patch-fg.py, util/build-prebuilt.py, util/sync-...
#                            import PATCHSET_VERSION/SIGNATURE from version.py.
#
# And these are excluded for historical reasons (rewriting would corrupt
# the record):
#   - CHANGELOG.md           per-version entries.
#   - docs/debugging.md      case studies frozen at the time of writing.
#   - prebuilt/archive/*     frozen old prebuilts.
SYNC_TARGETS = ["README.md"]


def main() -> int:
    target_sig = f"pfg-v{PATCHSET_VERSION}"
    pat = re.compile(r"pfg-v\d+(?:\.\d+)?")
    any_changed = False
    for relpath in SYNC_TARGETS:
        path = os.path.join(REPO_ROOT, relpath)
        if not os.path.exists(path):
            print(f"SKIP {relpath} (not found)")
            continue
        with open(path, "r") as f:
            s = f.read()
        new_s = pat.sub(target_sig, s)
        if new_s != s:
            with open(path, "w") as f:
                f.write(new_s)
            print(f"SYNC {relpath} → {target_sig}")
            any_changed = True
        else:
            print(f"OK   {relpath} already at {target_sig}")
    if any_changed:
        print()
        print("Now run `git diff` and verify only SYNC_TARGETS were touched")
        print("and that every rewrite is a current-state claim, not history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
