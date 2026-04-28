#!/usr/bin/env python3
"""
Patches F (+F.2/F.3) and G for anthropic.claude-code (Antigravity).

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

Default: latest installed version under ~/.antigravity/extensions/.

Patchset version: bump PATCHSET_VERSION below whenever the SPLICES change
materially. Each successful application embeds the version signature
(/*pfg-vN*/) into the patched code so subsequent runs can detect a stale
prior application and re-apply cleanly.
"""
import os, re, shutil, subprocess, sys, glob

PATCHSET_VERSION = "1"
PATCHSET_SIG = f"/*pfg-v{PATCHSET_VERSION}*/"


def find_default_extension():
    pattern = os.path.expanduser(
        "~/.antigravity/extensions/anthropic.claude-code-*-linux-x64/extension.js"
    )
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


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
            r'\(H,D,O\)=>\{this\.updateSessionState\(H,D,O\);for\(let\[z,(?P<lvar>[A-Za-z_$][A-Za-z_$0-9]*)\]of this\.sessionPanels\)if\((?P=lvar)===V&&z!==H\)this\.sessionPanels\.delete\(z\);if\(this\.sessionPanels\.set\(H,V\),V\.active\)this\.activeSessionId=H\}'
        ),
        r'(H,D,O,_sk)=>{this.updateSessionState(H,D,O);if(!_sk){for(let[z,\g<lvar>]of this.sessionPanels)if(\g<lvar>===V&&z!==H)this.sessionPanels.delete(z);if(this.sessionPanels.set(H,V),V.active)this.activeSessionId=H}}',
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
    sig_match = re.search(r'/\*pfg-v(\d+)\*/', s)
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

    # Legacy (unsigned) patches present, no other signature, and current
    # version is v1: the functional code is identical, just missing the
    # signature comment. Inject it in-place rather than restore + re-apply.
    if has_legacy_patches and not other_sig and not force and PATCHSET_VERSION == "1":
        legacy_anchor = 'updateSessionState(V,K,B){let _p=this.sessionStates.get(V)'
        new_anchor = 'updateSessionState(V,K,B){' + PATCHSET_SIG + 'let _p=this.sessionStates.get(V)'
        if legacy_anchor in s and new_anchor not in s:
            s = s.replace(legacy_anchor, new_anchor, 1)
            with open(path, "w") as f:
                f.write(s)
            print(
                f"Legacy unsigned patches detected (functionally equivalent "
                f"to v{PATCHSET_VERSION}). Signature injected; file now "
                f"marked as v{PATCHSET_VERSION}."
            )
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
