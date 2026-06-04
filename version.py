"""
Single source of truth for the patchset version.

Extracts the version from skill/SKILL.md (the canonical declaration). Both
util/build-prebuilt.py and skill/apply-patch-fg.py import from here, so
bumping requires editing exactly ONE line in SKILL.md.

The line in SKILL.md must match the regex below, for example:
    **Patchset version**: `1.8`

build-prebuilt.py also auto-syncs the README.md `pfg-vN` mention to the
extracted version on every prebuilt synthesis, so README never drifts.
"""
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL_MD = os.path.join(_HERE, "skill", "SKILL.md")

_VERSION_RE = re.compile(r"\*\*Patchset version\*\*:\s*`(\d+(?:\.\d+)?)`")


def _read_version() -> str:
    with open(_SKILL_MD, "r") as f:
        text = f.read()
    m = _VERSION_RE.search(text)
    if not m:
        raise RuntimeError(
            f"Could not find patchset version in {_SKILL_MD}. "
            f"Expected a line matching: **Patchset version**: `<X.Y>`"
        )
    return m.group(1)


PATCHSET_VERSION: str = _read_version()
SIGNATURE: str = f"/*pfg-v{PATCHSET_VERSION}*/"


if __name__ == "__main__":
    # Allow shell scripts and SKILL.md bash blocks to fetch the live signature
    # via `python3 version.py` instead of hardcoding `/*pfg-vN*/` literals
    # (which would have to be sync-rewritten on every version bump).
    print(SIGNATURE)
