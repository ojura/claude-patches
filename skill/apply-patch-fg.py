#!/usr/bin/env python3
"""
Patches F (+F.2/F.3) and G for the anthropic.claude-code VS Code extension.

Version-tolerant: locates anchors via regex on structural shape rather than
literal strings, so the same script applies to 2.1.120, 2.1.121, and any
future release that doesn't restructure the relevant code (only renames
locals or storage-class identifiers).

Splices (six total in extension.js):
  F.1+F.3  updateSessionState preserves missing fields + writes panel.title
  F.2      drop title at update_session_state message-handler boundary
  F-s2     q8.renameSession invokes onSessionStateChanged?.(V, void 0, K)
  F-s3     sidebar q8 ctor wires onSessionStateChanged
  G.1      panel ctor callback supports skip-bookkeeping flag
  G.2      fork_conversation handler reads source's customTitle/aiTitle
           from JSONL and pushes Map entry so sidebar sees the fork

Idempotent: re-running on already-patched file (with the same patchset
version) is a no-op. If the file is patched with an OLDER patchset
version, the script restores from .pre-patchFG.bak and re-applies fresh.

Usage:
  python3 apply-patch-fg.py [/path/to/extension.js] [--force]

  --force     restore from .pre-patchFG.bak and re-apply unconditionally

Default: latest install discovered under ~/.<ide>/extensions/ for any IDE
that pulls the extension from Open VSX (VS Code, Antigravity, Cursor,
VSCodium, etc.).

Patchset version: bump PATCHSET_VERSION below whenever the SPLICES change
materially. Each successful application embeds the version signature
(/*pfg-vN*/) into the patched code so subsequent runs can detect a stale
prior application and re-apply cleanly.
"""
import os, re, shutil, subprocess, sys, glob

# Single source of truth: skill/SKILL.md → version.py at repo root.
# realpath resolves the patch-claude symlink (~/.claude/skills/patch-claude/)
# back to the actual repo so the import resolves regardless of how the script
# was invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from version import PATCHSET_VERSION, SIGNATURE as PATCHSET_SIG


def _version_from_path(p: str) -> str:
    # ".../anthropic.claude-code-<VER>-linux-x64/extension.js" → "<VER>"
    parent = os.path.basename(os.path.dirname(p))
    return parent[len("anthropic.claude-code-"):-len("-linux-x64")]


def _version_key(v: str):
    return tuple(int(x) if x.isdigit() else x for x in v.split("."))


def find_default_extension():
    # Strongest signal: when Claude Code (the CLI) is invoked from inside
    # an IDE, its extension host sets CLAUDE_CODE_EXECPATH pointing at the
    # specific install hosting the running session.
    #
    # CAVEAT: CLAUDE_CODE_EXECPATH is also set in the *standalone CLI*
    # layout (e.g. ~/.local/share/claude/versions/X.Y.Z), which is NOT an
    # extension install. The walk-the-components loop below only matches
    # an "anthropic.claude-code-*-linux-x64" path component, so the
    # standalone layout falls through to the glob fallback automatically.
    # The os.path.exists check is the second line of defense.
    execpath = os.environ.get("CLAUDE_CODE_EXECPATH", "")
    if execpath:
        parts = execpath.split("/")
        for i, p in enumerate(parts):
            if p.startswith("anthropic.claude-code-") and p.endswith("-linux-x64"):
                candidate = "/" + "/".join(parts[1:i+1]) + "/extension.js"
                if os.path.exists(candidate):
                    return candidate
                break  # found a match but it doesn't have extension.js;
                       # don't keep walking, fall through to glob

    # Fallback: glob ~/.<ide>/extensions/ across all known IDE variants
    # (VS Code, Insiders, VSCodium, Antigravity, Cursor, etc.).
    pattern = os.path.expanduser(
        "~/.*/extensions/anthropic.claude-code-*-linux-x64/extension.js"
    )
    matches = glob.glob(pattern)
    if not matches:
        return None
    # Pick the latest version (semver-aware), then error out if that
    # version is installed in multiple IDE dirs simultaneously
    by_version = {}
    for m in matches:
        by_version.setdefault(_version_from_path(m), []).append(m)
    latest = max(by_version, key=_version_key)
    candidates = by_version[latest]
    if len(candidates) > 1:
        sys.exit(
            f"Multiple installs of {latest} detected — pass the path "
            f"explicitly, or invoke from inside the IDE you want to patch "
            f"(CLAUDE_CODE_EXECPATH disambiguates):\n  "
            + "\n  ".join(candidates)
        )
    return candidates[0]


# Each rule: (label, regex_for_old, replacement_template)
# replacement_template uses \g<name> for backrefs to capture groups in regex_for_old.
# Each regex must produce exactly one match in the unpatched file.
#
# Parameter-name agnosticism: minifier-assigned param names drift between
# releases (e.g. (V,K,B) in 2.1.120-126 → (z,K,V) in 2.1.139). Each rule
# captures the params it references and substitutes them into the
# replacement via \g<name> backrefs, so the same regex matches any
# minifier output that keeps the structural shape.

# Reusable identifier-character class (JS-legal local-var prefix)
_ID = r'[A-Za-z_$][A-Za-z_$0-9]*'

RULES = [
    (
        "F.1+F.3 (updateSessionState preserves fields + writes panel.title)",
        re.compile(
            r'updateSessionState\((?P<sid>' + _ID + r'),(?P<st>' + _ID + r'),(?P<ti>' + _ID + r')\)\{'
            r'this\.sessionStates\.set\((?P=sid),\{sessionId:(?P=sid),state:(?P=st),title:(?P=ti)\}\),'
            r'this\.broadcastSessionStates\(\)\}'
        ),
        r'updateSessionState(\g<sid>,\g<st>,\g<ti>){' + PATCHSET_SIG
        + r'let _p=this.sessionStates.get(\g<sid>);'
        r'this.sessionStates.set(\g<sid>,{sessionId:\g<sid>,'
        r'state:\g<st>!=null?\g<st>:_p?.state??"idle",'
        r'title:\g<ti>!=null?\g<ti>:_p?.title}),'
        r'this.broadcastSessionStates();'
        r'if(\g<ti>!=null){let _pnl=this.sessionPanels.get(\g<sid>);if(_pnl)_pnl.title=\g<ti>}}',
    ),
    (
        "F.2 (drop title at update_session_state boundary)",
        re.compile(
            r'if\((?P<d>' + _ID + r')\.request\.type==="update_session_state"\)'
            r'return this\.onSessionStateChanged\?\.\((?P=d)\.request\.sessionId,(?P=d)\.request\.state,(?P=d)\.request\.title\),'
            r'\{type:"update_session_state_response"\}'
        ),
        r'if(\g<d>.request.type==="update_session_state")'
        r'return this.onSessionStateChanged?.(\g<d>.request.sessionId,\g<d>.request.state,void 0),'
        r'{type:"update_session_state_response"}',
    ),
    (
        "F-s2 (q8.renameSession invokes onSessionStateChanged)",
        re.compile(
            r'async renameSession\((?P<sid>' + _ID + r'),(?P<title>' + _ID + r'),(?P<isAi>' + _ID + r')\)\{'
            r'return\{type:"rename_session_response",skipped:await\(await (?P<storage>' + _ID + r')\.load\(this\.cwd,this\.logger\)\)\.renameSession\((?P=sid),(?P=title),(?P=isAi)\)\}\}'
        ),
        r'async renameSession(\g<sid>,\g<title>,\g<isAi>){'
        r'let _r=await(await \g<storage>.load(this.cwd,this.logger)).renameSession(\g<sid>,\g<title>,\g<isAi>);'
        r'if(!_r)this.onSessionStateChanged?.(\g<sid>,void 0,\g<title>);'
        r'return{type:"rename_session_response",skipped:_r}}',
    ),
    (
        "F-s3 (sidebar q8 ctor wires onSessionStateChanged)",
        re.compile(r',void 0,\(\)=>this\.broadcastUsageUpdate\(\)\)'),
        ',void 0,()=>this.broadcastUsageUpdate(),!1,(H,D,O)=>{this.updateSessionState(H,D,O)})',
    ),
    (
        "G.1 (panel ctor callback supports skip-bookkeeping flag)",
        # Captures: a1/a2/a3 = callback args, kvar/lvar = Map destructure,
        #          pvar = outer panel reference (this varies — V in 2.1.120,
        #          z in 2.1.139).
        re.compile(
            r'\((?P<a1>' + _ID + r'),(?P<a2>' + _ID + r'),(?P<a3>' + _ID + r')\)=>\{'
            r'this\.updateSessionState\((?P=a1),(?P=a2),(?P=a3)\);'
            r'for\(let\[(?P<kvar>' + _ID + r'),(?P<lvar>' + _ID + r')\]of this\.sessionPanels\)'
            r'if\((?P=lvar)===(?P<pvar>' + _ID + r')&&(?P=kvar)!==(?P=a1)\)this\.sessionPanels\.delete\((?P=kvar)\);'
            r'if\(this\.sessionPanels\.set\((?P=a1),(?P=pvar)\),(?P=pvar)\.active\)this\.activeSessionId=(?P=a1)\}'
        ),
        r'(\g<a1>,\g<a2>,\g<a3>,_sk)=>{this.updateSessionState(\g<a1>,\g<a2>,\g<a3>);'
        r'if(!_sk){for(let[\g<kvar>,\g<lvar>]of this.sessionPanels)'
        r'if(\g<lvar>===\g<pvar>&&\g<kvar>!==\g<a1>)this.sessionPanels.delete(\g<kvar>);'
        r'if(this.sessionPanels.set(\g<a1>,\g<pvar>),\g<pvar>.active)this.activeSessionId=\g<a1>}}',
    ),
    # G.2 is built dynamically below — its replacement references three
    # bundle-globals (fs, path, projectRoot resolver) whose names drift
    # between releases (e.g. R1→W1, d5→n5 between 2.1.120 and 2.1.121).
    # We discover them structurally before composing the rule.
]


def discover_globals(s):
    """Locate fs / path / projectRoot-resolver names from the storage class's
    renameSession, which has a fixed structural shape:
      async renameSession(<sid>,<title>,<isAi>){let <a>=<RES>(this.projectRoot),<b>=<PATH>.join(<a>,`${<sid>}.jsonl`);...
        ...await <FS>.promises.appendFile(<b>,...)...}
    Param names AND local names drift across releases — both are captured.
    Returns (fs, path, res) or raises if any can't be located uniquely.
    """
    rx = re.compile(
        r'async renameSession\((?P<sid>' + _ID + r'),' + _ID + r',' + _ID + r'\)\{'
        r'let (?P<a>' + _ID + r')=(?P<res>' + _ID + r')\(this\.projectRoot\),'
        r'(?P<b>' + _ID + r')=(?P<path>' + _ID + r')\.join\((?P=a),`\$\{(?P=sid)\}\.jsonl`\)'
        r'.{0,1500}?'
        r'(?P<fs>' + _ID + r')\.promises\.appendFile\((?P=b),'
    )
    m = rx.search(s)
    if not m:
        raise SystemExit(
            "Could not locate <fs>/<path>/<resolver> globals via storage's "
            "renameSession. Bundle structure may have changed."
        )
    return m.group("fs"), m.group("path"), m.group("res")


def main():
    args = sys.argv[1:]
    force = False
    if "--force" in args:
        force = True
        args = [a for a in args if a != "--force"]
    path = args[0] if args else find_default_extension()
    if not path or not os.path.exists(path):
        sys.exit(f"not found: {path}")

    with open(path, "r") as f:
        s = f.read()

    # Detect prior application by:
    #   (a) the patchset signature comment (/*pfg-vN*/), set by all
    #       versioned scripts >= v1
    #   (b) legacy markers, in case an unsigned (pre-versioning) build
    #       was applied
    has_current_sig = PATCHSET_SIG in s
    # Match v1, v1.1, v2, v2.5, etc. — number with optional .number suffix.
    # Letter suffixes (v1a) intentionally not supported.
    sig_match = re.search(r'/\*pfg-v(\d+(?:\.\d+)?)\*/', s)
    other_sig = sig_match.group(1) if sig_match and not has_current_sig else None
    legacy_markers = [
        'if(!_r)this.onSessionStateChanged?.(V,void 0,K)',
        'this.onSessionStateChanged?.(V.request.sessionId,V.request.state,void 0)',
        '(H,D,O,_sk)=>{this.updateSessionState(H,D,O);if(!_sk)',
        ',!1,(H,D,O)=>{this.updateSessionState(H,D,O)})',
        'updateSessionState(V,K,B){let _p=this.sessionStates.get(V)',
        'case"fork_conversation":{let _m=await ',
    ]
    has_legacy_patches = (
        not sig_match
        and any(m in s for m in legacy_markers)
    )

    if has_current_sig and not force:
        print(f"Patchset v{PATCHSET_VERSION} already applied. Nothing to do.")
        return

    if other_sig or has_legacy_patches or force:
        # Stale prior application OR forced re-apply — restore from backup
        bak = path + ".pre-patchFG.bak"
        if not os.path.exists(bak):
            sys.exit(
                f"Patches present but no .pre-patchFG.bak at {bak} to restore "
                f"from. Refusing to apply on top of an unknown patched state. "
                f"To upgrade: reinstall the extension OR restore from "
                f"extension.js.bak (or whatever pre-everything backup you "
                f"have), re-run /patch-claude to apply A–E, then re-run "
                f"this script to apply F–G fresh."
            )
        if force:
            print(f"--force: restoring {path} from {bak} and re-applying.")
        elif other_sig:
            print(
                f"Stale patchset detected (file has v{other_sig}, current "
                f"script is v{PATCHSET_VERSION}). Restoring from {bak} and "
                f"re-applying."
            )
        else:
            print(
                f"Legacy unsigned patches detected (pre-versioning). Restoring "
                f"from {bak} and re-applying as v{PATCHSET_VERSION}."
            )
        shutil.copy2(bak, path)
        with open(path, "r") as f:
            s = f.read()

    # Discover bundle-global names for G.2's replacement (fs/path/resolver
    # rename between releases). The storage class's renameSession exposes
    # all three in one structural anchor.
    fs_g, path_g, res_g = discover_globals(s)
    print(f"Discovered: fs={fs_g}, path={path_g}, projectRoot-resolver={res_g}")

    # Build G.2 rule dynamically with the discovered globals
    g2_old = re.compile(
        r'case"fork_conversation":return\{type:"fork_conversation_response",'
        r'sessionId:await\(await (?P<storage>' + _ID + r')\.load\(this\.cwd,this\.logger\)\)\.forkSession\('
        r'(?P<d>' + _ID + r')\.request\.forkedFromSession,(?P=d)\.request\.resumeSessionAt\)\};'
    )
    g2_new = (
        'case"fork_conversation":{let _m=await \\g<storage>.load(this.cwd,this.logger),'
        '_src=\\g<d>.request.forkedFromSession,_sid=await _m.forkSession(_src,\\g<d>.request.resumeSessionAt);'
        f'let _t="";try{{let _lines=(await {fs_g}.promises.readFile({path_g}.join({res_g}(_m.projectRoot),'
        '`${_src}.jsonl`),"utf8")).split(`\\\\n`),_c="",_a="";'
        'for(let _line of _lines){if(!_line)continue;try{let _M=JSON.parse(_line);'
        'if(_M.type==="custom-title"&&_M.customTitle)_c=_M.customTitle;'
        'if(_M.type==="ai-title"&&_M.aiTitle)_a=_M.aiTitle}catch(_){}}_t=_c||_a}catch(_){}'
        'if(!_t)_t="Forked conversation";'
        'this.onSessionStateChanged?.(_sid,"idle",_t,!0);'
        'return{type:"fork_conversation_response",sessionId:_sid}}'
    )
    rules = list(RULES) + [("G.2 (fork_conversation populates Map with source's title)", g2_old, g2_new)]

    # Verify all anchors match exactly once before applying
    counts = []
    for label, rx, _ in rules:
        c = len(rx.findall(s))
        counts.append((label, c))
    failing = [l for l, c in counts if c != 1]
    if failing:
        print("Anchor counts not all 1:")
        for label, c in counts:
            mark = "OK" if c == 1 else "??"
            print(f"  [{mark}] {label}: {c}")
        sys.exit(
            "Refusing to write. Bundle structure may have shifted; "
            "re-locate by structure manually (see SKILL.md)."
        )

    bak = path + ".pre-patchFG.bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        print(f"Backup -> {bak}")
    else:
        print(f"Backup exists at {bak}; not overwriting.")

    for label, rx, repl in rules:
        s_new, n = rx.subn(repl, s, count=1)
        assert n == 1, f"unexpected miss after pre-check: {label}"
        s = s_new
        print(f"Applied: {label}")

    with open(path, "w") as f:
        f.write(s)

    try:
        r = subprocess.run(
            ["node", "--check", path], capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            print("node --check: OK")
        else:
            print("node --check FAILED:", r.stderr)
            sys.exit("Restoring may be needed. Investigate before reload.")
    except FileNotFoundError:
        print("node not found on PATH — skipping syntax check.")

    print("Patches F and G applied. Reload the VSCode window to activate.")


if __name__ == "__main__":
    main()
