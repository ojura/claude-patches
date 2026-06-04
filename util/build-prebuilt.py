#!/usr/bin/env python3
"""
Maintainer tool: synthesize a prebuilt apply.py for a freshly-patched
extension version.

Given a patched extension directory (post-A-K) and its three pre-patch
backups, this:
  1. Diffs each file against its .bak via util/extract_splices.py
  2. Aggregates the splices into a single self-contained Python script
  3. Validates the script is byte-stable (running it on the pristine
     .bak files reproduces the patched files exactly)
  4. Writes the script to prebuilt/<VER>/apply.py

Usage:
  python3 util/build-prebuilt.py <ext_dir> <output_dir>

Example:
  python3 util/build-prebuilt.py \\
    ~/.antigravity/extensions/anthropic.claude-code-2.1.121-linux-x64 \\
    prebuilt/2.1.121

After running, commit prebuilt/<VER>/apply.py to the repo so future users
with that version can skip synthesis.

This script is a *maintainer* tool: it produces output that gets
committed and used by end-users via the much simpler
prebuilt/<VER>/apply.py. End-users never run util/build-prebuilt.py.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)
from version import PATCHSET_VERSION, SIGNATURE  # SSOT extracted from skill/SKILL.md


def extract(file_path: str, bak_path: str):
    out = subprocess.check_output(
        ["python3", os.path.join(HERE, "extract_splices.py"), file_path, bak_path],
        text=True,
    )
    return json.loads(out)


PREBUILT_TEMPLATE = '''#!/usr/bin/env python3
"""
Prebuilt patch apply for the anthropic.claude-code VS Code extension {version}.

All patches applied as literal string replacements verified
byte-stable against the {version} bundle. Synthesized by
util/build-prebuilt.py from the diff between the patched live extension
and its pre-patch backups.

Usage:
  python3 apply.py [/path/to/extension/dir] [--force]

Default: auto-discovers an installed {version} extension under
~/.<ide>/extensions/ for any IDE that pulls from Open VSX (VS Code,
Antigravity, Cursor, VSCodium, etc.).

Idempotent: re-running on already-patched files is a no-op (detects the
{signature} signature in extension.js). With --force, restores from .bak files
and re-applies.
"""
import glob
import os
import re
import shutil
import subprocess
import sys

VERSION = "{version}"


def find_default_ext_dir():
    # Strongest signal: CLAUDE_CODE_EXECPATH (set by the IDE-hosted Claude
    # Code CLI) points at the extension install of the running session.
    # Caveat: standalone CLI layout (~/.local/share/claude/...) also sets
    # this var but isn't an extension install. Match the name pattern
    # explicitly so it falls through to the glob fallback in that case.
    execpath = os.environ.get("CLAUDE_CODE_EXECPATH", "")
    if execpath:
        target = f"anthropic.claude-code-{{VERSION}}-linux-x64"
        parts = execpath.split("/")
        for i, p in enumerate(parts):
            if p == target:
                candidate = "/" + "/".join(parts[1:i+1])
                if os.path.isdir(candidate):
                    return candidate
                break

    pattern = os.path.expanduser(
        f"~/.*/extensions/anthropic.claude-code-{{VERSION}}-linux-x64"
    )
    matches = glob.glob(pattern)
    if not matches:
        return None
    if len(matches) > 1:
        sys.exit(
            f"Multiple installs of {{VERSION}} detected. Pass the path "
            f"explicitly, or invoke from inside the IDE you want to patch "
            f"(CLAUDE_CODE_EXECPATH disambiguates):\\n  "
            + "\\n  ".join(matches)
        )
    return matches[0]


SIGNATURE = "{signature}"
PATCHSET_VERSION = re.match(r'/\\*pfg-v(\\d+(?:\\.\\d+)?)\\*/', SIGNATURE).group(1)

# Each entry: (file_relpath, [(old, new), (old, new), ...])
SPLICES = {splices_repr}


def main():
    args = sys.argv[1:]
    force = False
    if "--force" in args:
        force = True
        args = [a for a in args if a != "--force"]
    ext_dir = args[0] if args else find_default_ext_dir()
    if not ext_dir or not os.path.isdir(ext_dir):
        sys.exit(
            f"could not locate an installed {{VERSION}} extension; pass the "
            f"path explicitly or install it first"
        )

    # Decide state by checking the signature in extension.js. Recognize ANY
    # pfg-vX or pfg-vX.Y signature so a stale prior version (e.g. v1) is
    # detected as needing restore+reapply rather than silently no-op'd or
    # erroring on splice 0.
    ext_js = os.path.join(ext_dir, "extension.js")
    if not os.path.exists(ext_js):
        sys.exit(f"missing: {{ext_js}}")
    with open(ext_js, "r") as f:
        head = f.read()
    has_current_sig = SIGNATURE in head
    sig_match = re.search(r'/\\*pfg-v(\\d+(?:\\.\\d+)?)\\*/', head)
    other_sig = sig_match.group(1) if sig_match and not has_current_sig else None

    if has_current_sig and not force:
        print(f"Already patched (signature {{SIGNATURE}} present). Nothing to do.")
        return

    needs_restore = (force and has_current_sig) or other_sig is not None

    # Apply each file's splices
    for relpath, file_splices in SPLICES:
        target = os.path.join(ext_dir, relpath)
        bak = target + ".bak"
        if needs_restore:
            if not os.path.exists(bak):
                sys.exit(f"need to restore but no backup at {{bak}}")
            if other_sig:
                print(f"Stale patchset (file has v{{other_sig}}, current is v{{PATCHSET_VERSION}}); restoring {{target}} from {{bak}}")
            else:
                print(f"--force: restoring {{target}} from {{bak}}")
            shutil.copy2(bak, target)
        elif not os.path.exists(bak):
            shutil.copy2(target, bak)
            print(f"Backup -> {{bak}}")
        else:
            print(f"Backup exists: {{bak}}")

        with open(target, "r", encoding="utf-8", errors="surrogateescape") as f:
            s = f.read()
        for i, (old, new) in enumerate(file_splices):
            cnt = s.count(old)
            if cnt == 0:
                # Maybe already patched; check that new is present
                if s.count(new) >= 1:
                    print(f"  {{relpath}} splice {{i}}: already applied (skipped)")
                    continue
                sys.exit(
                    f"  {{relpath}} splice {{i}}: anchor not found "
                    f"(old_count={{cnt}}). Bundle may have shifted; this "
                    f"prebuilt is for {{VERSION}}. Use the version-tolerant "
                    f"skill/apply-patch-fg.py instead."
                )
            if cnt != 1:
                sys.exit(
                    f"  {{relpath}} splice {{i}}: anchor not unique "
                    f"(old_count={{cnt}}). Refusing to apply."
                )
            s = s.replace(old, new, 1)
            print(f"  {{relpath}} splice {{i}}: applied")
        with open(target, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(s)

    # Syntax-check extension.js
    try:
        r = subprocess.run(
            ["node", "--check", ext_js], capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            print("node --check: OK")
        else:
            print("node --check FAILED:", r.stderr)
            sys.exit("Patched files may be broken; investigate before reload.")
    except FileNotFoundError:
        print("node not found on PATH, skipping syntax check.")

    print(f"All patches applied (prebuilt {{VERSION}}). Reload VSCode to activate.")


if __name__ == "__main__":
    main()
'''


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: build-prebuilt.py <ext_dir> <output_dir>")
    ext_dir = sys.argv[1]
    out_dir = sys.argv[2]
    if not os.path.isdir(ext_dir):
        sys.exit(f"not a directory: {ext_dir}")
    os.makedirs(out_dir, exist_ok=True)

    # Derive version from path
    base = os.path.basename(ext_dir.rstrip("/"))
    if not base.startswith("anthropic.claude-code-") or not base.endswith("-linux-x64"):
        sys.exit(f"unrecognized extension dir name: {base}")
    version = base[len("anthropic.claude-code-"):-len("-linux-x64")]
    print(f"Synthesizing prebuilt for version: {version}")

    # Three target files
    targets = [
        ("extension.js", os.path.join(ext_dir, "extension.js")),
        ("webview/index.js", os.path.join(ext_dir, "webview/index.js")),
        ("webview/index.css", os.path.join(ext_dir, "webview/index.css")),
    ]

    splices_by_file = []
    for relpath, fpath in targets:
        bak = fpath + ".bak"
        if not os.path.exists(bak):
            sys.exit(f"missing backup: {bak} (need pristine pre-patch baseline)")
        if not os.path.exists(fpath):
            sys.exit(f"missing patched file: {fpath}")
        sps = extract(fpath, bak)
        print(f"  {relpath}: {len(sps)} splice(s)")
        if sps:
            splices_by_file.append((relpath, [(s["old"], s["new"]) for s in sps]))

    # Build the apply.py content
    splices_repr = repr(splices_by_file)
    script = PREBUILT_TEMPLATE.format(
        version=version,
        signature=SIGNATURE,
        splices_repr=splices_repr,
    )

    # Validate byte-stability by running the synthesized script against fresh copies
    print("Validating byte-stability...")
    with tempfile.TemporaryDirectory() as td:
        # Mirror ext_dir structure with .bak files
        validation_ext = os.path.join(td, base)
        os.makedirs(os.path.join(validation_ext, "webview"), exist_ok=True)
        for relpath, fpath in targets:
            bak = fpath + ".bak"
            shutil.copy2(bak, os.path.join(validation_ext, relpath))
        # Write the script and run it against the temp ext dir
        script_path = os.path.join(td, "apply.py")
        with open(script_path, "w") as f:
            f.write(script)
        os.chmod(script_path, 0o755)
        r = subprocess.run(
            ["python3", script_path, validation_ext],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            print("Synthesized script failed validation run:")
            print("STDOUT:", r.stdout)
            print("STDERR:", r.stderr)
            sys.exit(1)
        print("  apply ran clean")
        # Compare each file
        for relpath, fpath in targets:
            with open(os.path.join(validation_ext, relpath), "rb") as f:
                v = f.read()
            with open(fpath, "rb") as f:
                live = f.read()
            if v != live:
                sys.exit(f"  {relpath}: BYTE MISMATCH against live patched file")
            print(f"  {relpath}: byte-identical to live ({len(v)} bytes)")

    out_path = os.path.join(out_dir, "apply.py")

    # Guardrail bundle: refuse to publish a prebuilt that's byte-identical to
    # something the maintainer probably shouldn't be re-publishing.
    #
    # Three cases caught:
    #   (a) byte-identical to a known-broken archived prebuilt for THIS bundle
    #       version, under ANY patchset version. Catches "404 → re-synthesize
    #       from non-pristine live install → re-publish the same broken file".
    #   (b) byte-identical to the currently-published prebuilt at
    #       prebuilt/<VER>/apply.py, and the embedded SIGNATURE constant is
    #       unchanged. Means the maintainer is shipping no functional change
    #       (probably forgot to bump skill/SKILL.md's `**Patchset version**`
    #       before re-baking and synthesizing).
    #
    # Override: --force-republish-broken (use only with explicit reason).
    if "--force-republish-broken" not in sys.argv:
        # Case (a): glob across all patchset versions
        broken_glob = os.path.join(
            REPO_ROOT, "prebuilt", "archive", "broken", "*", version, "apply.py"
        )
        for broken_path in glob.glob(broken_glob):
            with open(broken_path, "r") as f:
                broken_content = f.read()
            if broken_content == script:
                rel = os.path.relpath(broken_path, REPO_ROOT)
                print()
                print(f"REFUSING to publish: byte-identical to known-broken prebuilt at")
                print(f"  {rel}")
                print()
                print(f"This means the live install you're synthesizing from has the same")
                print(f"non-pristine .bak that produced the archived broken prebuilt. The")
                print(f"prebuilt would inherit the same dead-code / silent-no-op bug.")
                print()
                print(f"Action: see prebuilt/archive/broken/README.md for the diagnosis,")
                print(f"then either reinstall the extension from scratch (pristine .bak)")
                print(f"or fix the splice on the live install before re-running.")
                print(f"To override anyway: pass --force-republish-broken.")
                sys.exit(2)

        # Case (b): identical to currently-published, no signature change
        if os.path.exists(out_path):
            with open(out_path, "r") as f:
                current_content = f.read()
            if current_content == script:
                # Extract the SIGNATURE from both; if both have the same signature
                # AND same content, the maintainer is probably re-publishing without
                # a meaningful change.
                import re as _re
                cur_sig_m = _re.search(r'SIGNATURE\s*=\s*"([^"]+)"', current_content)
                cur_sig = cur_sig_m.group(1) if cur_sig_m else None
                if cur_sig == SIGNATURE:
                    print()
                    print(f"REFUSING to publish: byte-identical to currently-published")
                    print(f"  prebuilt/{version}/apply.py, with the same signature ({SIGNATURE}).")
                    print()
                    print(f"No functional change is being shipped. This usually means the")
                    print(f"maintainer forgot to bump `**Patchset version**` in skill/SKILL.md")
                    print(f"before re-baking and re-synthesizing.")
                    print()
                    print(f"Action: bump skill/SKILL.md's `**Patchset version**` line, then")
                    print(f"re-apply patches locally (verify only the signature tag changed)")
                    print(f"before re-running build-prebuilt.py.")
                    print(f"To override (e.g. legitimate no-op resync): pass --force-republish-broken.")
                    sys.exit(2)

    with open(out_path, "w") as f:
        f.write(script)
    os.chmod(out_path, 0o755)
    print(f"Wrote {out_path}")
    print(f"  size: {os.path.getsize(out_path)} bytes")
    print(f"  total splices: {sum(len(s[1]) for s in splices_by_file)}")
    print()
    print(f"Next: add CHANGELOG.md entry for v{PATCHSET_VERSION}, then")
    print(f"      git add -A && python3 util/sync-version-mentions.py")
    print(f"      review the resulting diff for any unwanted rewrites,")
    print(f"      then git add -A && commit.")


if __name__ == "__main__":
    main()
