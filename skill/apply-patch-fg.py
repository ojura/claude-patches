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

PATCHSET_VERSION = "1.4"
PATCHSET_SIG = f"/*pfg-v{PATCHSET_VERSION}*/"


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
RULES = [
    (
        "F.1+F.3 (updateSessionState preserves fields + writes panel.title)",
        re.compile(
            r'updateSessionState\(V,K,B\)\{this\.sessionStates\.set\(V,\{sessionId:V,state:K,title:B\}\),this\.broadcastSessionStates\(\)\}'
        ),
        'updateSessionState(V,K,B){' + PATCHSET_SIG + 'let _p=this.sessionStates.get(V);this.sessionStates.set(V,{sessionId:V,state:K!=null?K:_p?.state??"idle",title:B!=null?B:_p?.title}),this.broadcastSessionStates();if(B!=null){let _pnl=this.sessionPanels.get(V);if(_pnl)_pnl.title=B}}',
    ),
    (
        "F.2 (drop title at update_session_state boundary)",
        re.compile(
            r'if\(V\.request\.type==="update_session_state"\)return this\.onSessionStateChanged\?\.\(V\.request\.sessionId,V\.request\.state,V\.request\.title\),\{type:"update_session_state_response"\}'
        ),
        'if(V.request.type==="update_session_state")return this.onSessionStateChanged?.(V.request.sessionId,V.request.state,void 0),{type:"update_session_state_response"}',
    ),
    (
        "F-s2 (q8.renameSession invokes onSessionStateChanged)",
        re.compile(
            r'async renameSession\(V,K,B\)\{return\{type:"rename_session_response",skipped:await\(await (?P<storage>[A-Za-z_$][A-Za-z_$0-9]*)\.load\(this\.cwd,this\.logger\)\)\.renameSession\(V,K,B\)\}\}'
        ),
        r'async renameSession(V,K,B){let _r=await(await \g<storage>.load(this.cwd,this.logger)).renameSession(V,K,B);if(!_r)this.onSessionStateChanged?.(V,void 0,K);return{type:"rename_session_response",skipped:_r}}',
    ),
    (
        "F-s3 (sidebar q8 ctor wires onSessionStateChanged)",
        re.compile(r',void 0,\(\)=>this\.broadcastUsageUpdate\(\)\)'),
        ',void 0,()=>this.broadcastUsageUpdate(),!1,(H,D,O)=>{this.updateSessionState(H,D,O)})',
    ),
    (
        "G.1 (panel ctor callback supports skip-bookkeeping flag)",
        re.compile(
            r'\(H,D,(?P<p3>[A-Za-z_$][A-Za-z_$0-9]*)\)=>\{this\.updateSessionState\(H,D,(?P=p3)\);for\(let\[(?P<kvar>[A-Za-z_$][A-Za-z_$0-9]*),(?P<lvar>[A-Za-z_$][A-Za-z_$0-9]*)\]of this\.sessionPanels\)if\((?P=lvar)===V&&(?P=kvar)!==H\)this\.sessionPanels\.delete\((?P=kvar)\);if\(this\.sessionPanels\.set\(H,V\),V\.active\)this\.activeSessionId=H\}'
        ),
        r'(H,D,\g<p3>,_sk)=>{this.updateSessionState(H,D,\g<p3>);if(!_sk){for(let[\g<kvar>,\g<lvar>]of this.sessionPanels)if(\g<lvar>===V&&\g<kvar>!==H)this.sessionPanels.delete(\g<kvar>);if(this.sessionPanels.set(H,V),V.active)this.activeSessionId=H}}',
    ),
    # G.2 is built dynamically below — its replacement references three
    # bundle-globals (fs, path, projectRoot resolver) whose names drift
    # between releases (e.g. R1→W1, d5→n5 between 2.1.120 and 2.1.121).
    # We discover them structurally before composing the rule.
]


def discover_globals(s):
    """Locate fs / path / projectRoot-resolver names from the storage class's
    renameSession, which has a fixed structural shape:
      async renameSession(V,K,B){let x=<RES>(this.projectRoot),G=<PATH>.join(x,`${V}.jsonl`);...
        ...await <FS>.promises.appendFile(G,...)...}
    Returns (fs, path, res) or raises if any can't be located uniquely.
    """
    rx = re.compile(
        r'async renameSession\(V,K,B\)\{let x=(?P<res>[A-Za-z_$][A-Za-z_$0-9]*)\(this\.projectRoot\),'
        r'G=(?P<path>[A-Za-z_$][A-Za-z_$0-9]*)\.join\(x,`\$\{V\}\.jsonl`\)'
        r'.{0,1500}?'
        r'(?P<fs>[A-Za-z_$][A-Za-z_$0-9]*)\.promises\.appendFile\(G,'
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
        r'case"fork_conversation":return\{type:"fork_conversation_response",sessionId:await\(await (?P<storage>[A-Za-z_$][A-Za-z_$0-9]*)\.load\(this\.cwd,this\.logger\)\)\.forkSession\(V\.request\.forkedFromSession,V\.request\.resumeSessionAt\)\};'
    )
    g2_new = (
        'case"fork_conversation":{let _m=await \\g<storage>.load(this.cwd,this.logger),'
        '_src=V.request.forkedFromSession,_sid=await _m.forkSession(_src,V.request.resumeSessionAt);'
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
