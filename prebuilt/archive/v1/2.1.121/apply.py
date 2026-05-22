#!/usr/bin/env python3
"""
Prebuilt patch apply for the anthropic.claude-code VS Code extension 2.1.121.

Patches A through G applied as literal string replacements verified
byte-stable against the 2.1.121 bundle. Synthesized by
util/build-prebuilt.py from the diff between the patched live extension
and its pre-patch backups.

Usage:
  python3 apply.py [/path/to/extension/dir] [--force]

Default: auto-discovers an installed 2.1.121 extension under
~/.<ide>/extensions/ for any IDE that pulls from Open VSX (VS Code,
Antigravity, Cursor, VSCodium, etc.).

Idempotent: re-running on already-patched files is a no-op (detects the
pfg-v1 signature in extension.js). With --force, restores from .bak files
and re-applies.
"""
import glob
import os
import shutil
import subprocess
import sys

VERSION = "2.1.121"


def find_default_ext_dir():
    # Strongest signal: CLAUDE_CODE_EXECPATH (set by the IDE-hosted Claude
    # Code CLI) points at the extension install of the running session.
    # Caveat: standalone CLI layout (~/.local/share/claude/...) also sets
    # this var but isn't an extension install; match the structural name
    # explicitly so it falls through to the glob fallback in that case.
    execpath = os.environ.get("CLAUDE_CODE_EXECPATH", "")
    if execpath:
        target = f"anthropic.claude-code-{VERSION}-linux-x64"
        parts = execpath.split("/")
        for i, p in enumerate(parts):
            if p == target:
                candidate = "/" + "/".join(parts[1:i+1])
                if os.path.isdir(candidate):
                    return candidate
                break

    pattern = os.path.expanduser(
        f"~/.*/extensions/anthropic.claude-code-{VERSION}-linux-x64"
    )
    matches = glob.glob(pattern)
    if not matches:
        return None
    if len(matches) > 1:
        sys.exit(
            f"Multiple installs of {VERSION} detected. Pass the path "
            f"explicitly, or invoke from inside the IDE you want to patch "
            f"(CLAUDE_CODE_EXECPATH disambiguates):\n  "
            + "\n  ".join(matches)
        )
    return matches[0]


SIGNATURE = "/*pfg-v1*/"

# Each entry: (file_relpath, [(old, new), (old, new), ...])
SPLICES = [('extension.js', [('reak}A=A.parentUuid?K.get(A.parentUuid):void 0}}if(U.length===0)return[];let q=U.filte', 'reak}A=A.parentUuid?K.get(A.parentUuid):(A.logicalParentUuid?K.get(A.logicalParentUuid):void 0)}}if(U.length===0)return[];let q=U.filte'), ('h(O),O=O.parentUuid?K.get(O.parentUuid):void 0}return H.reverse(),RO4(K,H,D)}function ', 'h(O),O=O.parentUuid?K.get(O.parentUuid):(O.logicalParentUuid?K.get(O.logicalParentUuid):void 0)}return H.reverse(),RO4(K,H,D)}function '), ('rse(O);if(!Number.isNaN(v))z=v}let A=H||m5(G,"lastPrompt")||m5(G,"summary")||D;if(!A)return null;let L=m5(G,"gitBranch', 'rse(O);if(!Number.isNaN(v))z=v}let A=H||D||m5(G,"summary")||m5(G,"lastPrompt");if(!A)return null;let L=m5(G,"gitBranch'), ('e")??N9(N,"aiTitle")??N9(q,"aiTitle"))||N9(N,"lastPrompt")||N9(N,"summary")||na(q);if(!A)return null;return{lastModified:', 'e")??N9(N,"aiTitle")??N9(q,"aiTitle"))||na(q)||N9(N,"summary")||N9(N,"lastPrompt");if(!A)return null;return{lastModified:'), ('geId,M);if(H&&j)this.summaries.set(j,H);return this.loadedSessions.add(q),q}}asy', 'geId,M);if(H&&j)this.summaries.set(j,H);{let _srcCustom="",_srcAi="";try{let _src=(await W1.promises.readFile(D,"utf8")).split(`\n`);for(let _line of _src){if(!_line)continue;try{let _M=JSON.parse(_line);if(_M.type==="custom-title"&&_M.customTitle)_srcCustom=_M.customTitle;if(_M.type==="ai-title"&&_M.aiTitle)_srcAi=_M.aiTitle}catch(_){}}}catch(_){}let _srcTitle=_srcCustom||_srcAi;let _lp="",_lpEndBytes=-1,_byteOffset=0;for(let _i=0;_i<L.length;_i++){let _m=L[_i];let _lineBytes=Buffer.byteLength(JSON.stringify(_m)+`\n`,"utf8");if(_lpEndBytes<0&&_m.type==="user"&&!_m.isCompactSummary&&!_m.isMeta){let _mc=_m.message?.content;let _txt=null;if(typeof _mc==="string"&&_mc.trim())_txt=_mc;else if(Array.isArray(_mc))for(let _c of _mc){if(_c.type==="text"&&_c.text?.trim()){_txt=_c.text;break}if(_c.type==="tool_result")break}if(_txt){_lp=_txt;_lpEndBytes=_byteOffset+_lineBytes}}_byteOffset+=_lineBytes;if(_lpEndBytes>=0&&_byteOffset>65536)break}let _titleToWrite="";if(_srcTitle)_titleToWrite=_srcTitle;else if(_lpEndBytes<0||_lpEndBytes>65536)_titleToWrite=_lp||"Forked conversation";if(_titleToWrite){if(_titleToWrite.length>200)_titleToWrite=_titleToWrite.slice(0,200);await W1.promises.appendFile(Z,JSON.stringify({type:"custom-title",customTitle:_titleToWrite,sessionId:q})+`\n`)}}return this.loadedSessions.add(q),q}}asy'), ('anel_response"};case"fork_conversation":return{type:"fork_conversation_response",sessionId:await(await c1.load(this.cwd,this.logger)).forkSession(V.request.forkedFromSession,V.request.resumeSessionAt)};case"rewind_code":{let{userMessageId:B', 'anel_response"};case"fork_conversation":{let _m=await c1.load(this.cwd,this.logger),_src=V.request.forkedFromSession,_sid=await _m.forkSession(_src,V.request.resumeSessionAt);let _t="";try{let _lines=(await W1.promises.readFile(O1.join(n5(_m.projectRoot),`${_src}.jsonl`),"utf8")).split(`\n`),_c="",_a="";for(let _line of _lines){if(!_line)continue;try{let _M=JSON.parse(_line);if(_M.type==="custom-title"&&_M.customTitle)_c=_M.customTitle;if(_M.type==="ai-title"&&_M.aiTitle)_a=_M.aiTitle}catch(_){}}_t=_c||_a}catch(_){}if(!_t)_t="Forked conversation";this.onSessionStateChanged?.(_sid,"idle",_t,!0);return{type:"fork_conversation_response",sessionId:_sid}};case"rewind_code":{let{userMessageId:B'), ('as(U.id)):B}}async renameSession(V,K,B){return{type:"rename_session_response",skipped:await(await c1.load(this.cwd,this.logger)).renameSession(V,K,B)}}async messageRated(V){try{await this.w', 'as(U.id)):B}}async renameSession(V,K,B){let _r=await(await c1.load(this.cwd,this.logger)).renameSession(V,K,B);if(!_r)this.onSessionStateChanged?.(V,void 0,K);return{type:"rename_session_response",skipped:_r}}async messageRated(V){try{await this.w'), ('d?.(V.request.sessionId,V.request.state,V.request.title),{type:"update_session_state_response"}', 'd?.(V.request.sessionId,V.request.state,void 0),{type:"update_session_state_response"}'), ('geUpdate(K,B)}updateSessionState(V,K,B){this.sessionStates.set(V,{sessionId:V,state:K,title:B}),this.broadcastSessionStates()}setActivePanel(V){for(let[K,B]of this.s', 'geUpdate(K,B)}updateSessionState(V,K,B){/*pfg-v1*/let _p=this.sessionStates.get(V);this.sessionStates.set(V,{sessionId:V,state:K!=null?K:_p?.state??"idle",title:B!=null?B:_p?.title}),this.broadcastSessionStates();if(B!=null){let _pnl=this.sessionPanels.get(V);if(_pnl)_pnl.title=B}}setActivePanel(V){for(let[K,B]of this.s'), ('=>this.broadcastUsageUpdate(),!!x,(H,D,O)=>{this.updateSessionState(H,D,O);for(let[z,A]of this.sessionPanels)if(A==', '=>this.broadcastUsageUpdate(),!!x,(H,D,O,_sk)=>{this.updateSessionState(H,D,O);if(!_sk){for(let[z,A]of this.sessionPanels)if(A=='), ('et(H,V),V.active)this.activeSessionId=H});this.allComms.add(Z),V.webview.onDidRe', 'et(H,V),V.active)this.activeSessionId=H}});this.allComms.add(Z),V.webview.onDidRe'), ('),void 0,()=>this.broadcastUsageUpdate());this.allComms.add(q),this.broadcastSes', '),void 0,()=>this.broadcastUsageUpdate(),!1,(H,D,O)=>{this.updateSessionState(H,D,O)});this.allComms.add(q),this.broadcastSes')]), ('webview/index.js', [('ics:X,originalText:Z}}let J=KN0(Z)||Z,Y=J.startsWith("/");return{type:"text",text:J,isSlashComman', 'ics:X,originalText:Z}}let J=KN0(Z)||Z,Y=!1;return{type:"text",text:J,isSlashComman')]), ('webview/index.css', [(':var(--app-primary-background);position:sticky;z-index:2;background-image:linear-gradient(to bottom,var(--sticky-bg)calc(100% - 12px),transparent 100%),linear-gradient(to bottom,var(--app-secondary-background)calc(100% - 12px),transparent 100%);align-items:stretch;padding-top:14px;padding-bottom:12px;top:0}.message_07S1Yg.stickyHeader_07S1Yg:has([aria-expanded=true]){z-index:3}.fullEditor_07S1Yg .message_07S1Yg.stic', ':var(--app-primary-background);position:relative;z-index:auto;align-items:stretch;padding-top:14px;padding-bottom:12px}.message_07S1Yg.stickyHeader_07S1Yg:has([aria-expanded=true]){z-index:auto}.fullEditor_07S1Yg .message_07S1Yg.stic')])]


def main():
    args = sys.argv[1:]
    force = False
    if "--force" in args:
        force = True
        args = [a for a in args if a != "--force"]
    ext_dir = args[0] if args else find_default_ext_dir()
    if not ext_dir or not os.path.isdir(ext_dir):
        sys.exit(
            f"could not locate an installed {VERSION} extension; pass the "
            f"path explicitly or install it first"
        )

    # Decide state by checking the signature in extension.js
    ext_js = os.path.join(ext_dir, "extension.js")
    if not os.path.exists(ext_js):
        sys.exit(f"missing: {ext_js}")
    with open(ext_js, "r") as f:
        head = f.read()
    is_patched = SIGNATURE in head
    if is_patched and not force:
        print(f"Already patched (signature {SIGNATURE} present). Nothing to do.")
        return

    # Apply each file's splices
    for relpath, file_splices in SPLICES:
        target = os.path.join(ext_dir, relpath)
        bak = target + ".bak"
        if force and is_patched:
            if not os.path.exists(bak):
                sys.exit(f"--force but no backup at {bak}")
            print(f"--force: restoring {target} from {bak}")
            shutil.copy2(bak, target)
        elif not os.path.exists(bak):
            shutil.copy2(target, bak)
            print(f"Backup -> {bak}")
        else:
            print(f"Backup exists: {bak}")

        with open(target, "r") as f:
            s = f.read()
        for i, (old, new) in enumerate(file_splices):
            cnt = s.count(old)
            if cnt == 0:
                # Maybe already patched; check that new is present
                if s.count(new) >= 1:
                    print(f"  {relpath} splice {i}: already applied (skipped)")
                    continue
                sys.exit(
                    f"  {relpath} splice {i}: anchor not found "
                    f"(old_count={cnt}). Bundle may have shifted; this "
                    f"prebuilt is for {VERSION}. Use the version-tolerant "
                    f"skill/apply-patch-fg.py instead."
                )
            if cnt != 1:
                sys.exit(
                    f"  {relpath} splice {i}: anchor not unique "
                    f"(old_count={cnt}). Refusing to apply."
                )
            s = s.replace(old, new, 1)
            print(f"  {relpath} splice {i}: applied")
        with open(target, "w") as f:
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

    print(f"Patches A-G applied (prebuilt {VERSION}). Reload VSCode to activate.")


if __name__ == "__main__":
    main()
