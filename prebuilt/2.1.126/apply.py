#!/usr/bin/env python3
"""
Prebuilt patch apply for the anthropic.claude-code VS Code extension 2.1.126.

Patches A through K applied as literal string replacements verified
byte-stable against the 2.1.126 bundle. Synthesized by
util/build-prebuilt.py from the diff between the patched live extension
and its pre-patch backups.

Usage:
  python3 apply.py [/path/to/extension/dir] [--force]

Default: auto-discovers an installed 2.1.126 extension under
~/.<ide>/extensions/ for any IDE that pulls from Open VSX (VS Code,
Antigravity, Cursor, VSCodium, etc.).

Idempotent: re-running on already-patched files is a no-op (detects the
pfg-v1.2 signature in extension.js). With --force, restores from .bak files
and re-applies.
"""
import glob
import os
import re
import shutil
import subprocess
import sys

VERSION = "2.1.126"


def find_default_ext_dir():
    # Strongest signal: CLAUDE_CODE_EXECPATH (set by the IDE-hosted Claude
    # Code CLI) points at the extension install of the running session.
    # Caveat: standalone CLI layout (~/.local/share/claude/...) also sets
    # this var but isn't an extension install — match the structural name
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
            f"Multiple installs of {VERSION} detected — pass the path "
            f"explicitly, or invoke from inside the IDE you want to patch "
            f"(CLAUDE_CODE_EXECPATH disambiguates):\n  "
            + "\n  ".join(matches)
        )
    return matches[0]


SIGNATURE = "/*pfg-v1.2*/"
PATCHSET_VERSION = re.match(r'/\*pfg-v(\d+(?:\.\d+)?)\*/', SIGNATURE).group(1)

# Each entry: (file_relpath, [(old, new), (old, new), ...])
SPLICES = [('extension.js', [('}async function Rz4(V,K){try{if(K>Hz4&&!M2(process.env.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP))return(await jz4(V,K)).postBoundaryBuf', '}async function Rz4(V,K){try{if(K>Hz4&&!(!0||M2(process.env.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP)))return(await jz4(V,K)).postBoundaryBuf'), ('reak}O=O.parentUuid?K.get(O.parentUuid):void 0}}if(U.length===0)return[];let N=U.filte', 'reak}O=O.parentUuid?K.get(O.parentUuid):(O.logicalParentUuid?K.get(O.logicalParentUuid):void 0)}}if(U.length===0)return[];let N=U.filte'), ('h(z),z=z.parentUuid?K.get(z.parentUuid):void 0}return H.reverse(),Pz4(K,H,D)}function ', 'h(z),z=z.parentUuid?K.get(z.parentUuid):(z.logicalParentUuid?K.get(z.logicalParentUuid):void 0)}return H.reverse(),Pz4(K,H,D)}function '), ('4(B.filePath,B.fileSize);if(!x)return[];return dl(Yz4(x),K)}function $l(V,K,B){let{head:x,tail:G', '4(B.filePath,B.fileSize);if(!x)return[];let _parsed=Yz4(x);let _seen=new Set(_parsed.map(_m=>_m.uuid));let _dir=jK.dirname(B.filePath);let _entries=await Y8.readdir(_dir);let _filesParsed=new Map();for(let _pass=0;_pass<10;_pass++){let _dangling=[];for(let _m of _parsed)if(_m.type==="system"&&_m.subtype==="compact_boundary"&&!_m.parentUuid&&_m.logicalParentUuid&&!_seen.has(_m.logicalParentUuid))_dangling.push(_m.logicalParentUuid);if(_dangling.length===0)break;let _maxByFile=new Map();for(let _lpu of _dangling){for(let _name of _entries){if(!_name.endsWith(".jsonl"))continue;let _path=jK.join(_dir,_name);if(_path===B.filePath)continue;let _siblingMsgs=_filesParsed.get(_path);if(!_siblingMsgs){let _buf=await ml.readFile(_path);let _str=_buf.toString("utf-8");if(!_str.includes(`"uuid":"${_lpu}"`))continue;_siblingMsgs=Yz4(_buf);_filesParsed.set(_path,_siblingMsgs);}let _found=-1;for(let _i=0;_i<_siblingMsgs.length;_i++)if(_siblingMsgs[_i].uuid===_lpu){_found=_i;break}if(_found===-1)continue;let _prev=_maxByFile.get(_path);if(_prev===void 0||_found>_prev)_maxByFile.set(_path,_found);break;}}if(_maxByFile.size===0)break;let _newPrepend=[];for(let[_path,_maxIdx]of _maxByFile){let _siblingMsgs=_filesParsed.get(_path);for(let _i=0;_i<=_maxIdx;_i++){let _m=_siblingMsgs[_i];if(_m&&!_seen.has(_m.uuid)){_newPrepend.push(_m);_seen.add(_m.uuid)}}}if(_newPrepend.length===0)break;_parsed=[..._newPrepend,..._parsed];}let _kFired=!1;for(let _i=0;_i<_parsed.length;_i++){let _m=_parsed[_i];if(_m.type==="system"&&_m.subtype==="compact_boundary"&&!_m.parentUuid&&_m.logicalParentUuid&&!_seen.has(_m.logicalParentUuid)){let _predUuid=null;for(let _j=_i-1;_j>=0;_j--){if(_parsed[_j].uuid){_predUuid=_parsed[_j].uuid;break}}if(!_predUuid)continue;let _seamUuid="pfgk-seam-"+_m.uuid.slice(0,8);let _origLpu=_m.logicalParentUuid;let _ghost={type:"user",uuid:_seamUuid,parentUuid:_predUuid,sessionId:_m.sessionId,timestamp:_m.timestamp,message:{role:"user",content:"\\uD83D\\uDD3A Orphaned compaction pointer (seam)\\n\\nThe compactor referenced a chain predecessor uuid (\\""+_origLpu.slice(0,8)+"\\u2026) that was never persisted to disk \\u2014 a Claude Code bug. Pre-compaction history above this notice has been reattached via the in-file predecessor by Patch K (claude-patches pfg-v1.2). Click to jump to the start of the recovered chain."}};_parsed.splice(_i,0,_ghost);_m.logicalParentUuid=_seamUuid;_seen.add(_ghost.uuid);_kFired=!0;_i++}}if(_kFired){for(let _i=0;_i<_parsed.length;_i++){let _r=_parsed[_i];if(_r.uuid&&_r.parentUuid==null&&!_r.logicalParentUuid&&_r.type!=="system"){let _bid="pfgk-bookend-"+_r.uuid;let _be={type:"user",uuid:_bid,parentUuid:null,sessionId:_r.sessionId,timestamp:_r.timestamp,message:{role:"user",content:"\\uD83D\\uDD3B Recovered orphan chain (start)\\n\\nThe content below this notice was orphaned by a Claude Code compaction bug. The compact boundary further down referenced a chain predecessor that was never persisted to disk; the in-file pre-compaction history was reattached as a best-effort fallback by Patch K (claude-patches pfg-v1.2). Click to jump to the seam at the end of the recovered section."}};_parsed.splice(_i,0,_be);_r.parentUuid=_bid;_seen.add(_bid);break}}}return dl(_parsed,K)}function $l(V,K,B){let{head:x,tail:G'), ('rse(z);if(!Number.isNaN(j))L=j}let O=H||c5(G,"lastPrompt")||c5(G,"summary")||D;if(!O)return null;let A=c5(G,"gitBranch', 'rse(z);if(!Number.isNaN(j))L=j}let O=H||D||c5(G,"summary")||c5(G,"lastPrompt");if(!O)return null;let A=c5(G,"gitBranch'), ('e")??z9(q,"aiTitle")??z9(N,"aiTitle"))||z9(q,"lastPrompt")||z9(q,"summary")||xt(N);if(!O)return null;return{lastModified:', 'e")??z9(q,"aiTitle")??z9(N,"aiTitle"))||xt(N)||z9(q,"summary")||z9(q,"lastPrompt");if(!O)return null;return{lastModified:'), ('geId,w);if(H&&v)this.summaries.set(v,H);return this.loadedSessions.add(N),N}}asy', 'geId,w);if(H&&v)this.summaries.set(v,H);{let _srcCustom="",_srcAi="";try{let _src=(await _1.promises.readFile(D,"utf8")).split(`\n`);for(let _line of _src){if(!_line)continue;try{let _M=JSON.parse(_line);if(_M.type==="custom-title"&&_M.customTitle)_srcCustom=_M.customTitle;if(_M.type==="ai-title"&&_M.aiTitle)_srcAi=_M.aiTitle}catch(_){}}}catch(_){}let _srcTitle=_srcCustom||_srcAi;let _lp="",_lpEndBytes=-1,_byteOffset=0;for(let _i=0;_i<A.length;_i++){let _m=A[_i];let _lineBytes=Buffer.byteLength(JSON.stringify(_m)+`\n`,"utf8");if(_lpEndBytes<0&&_m.type==="user"&&!_m.isCompactSummary&&!_m.isMeta){let _mc=_m.message?.content;let _txt=null;if(typeof _mc==="string"&&_mc.trim())_txt=_mc;else if(Array.isArray(_mc))for(let _c of _mc){if(_c.type==="text"&&_c.text?.trim()){_txt=_c.text;break}if(_c.type==="tool_result")break}if(_txt){_lp=_txt;_lpEndBytes=_byteOffset+_lineBytes}}_byteOffset+=_lineBytes;if(_lpEndBytes>=0&&_byteOffset>65536)break}let _titleToWrite="";if(_srcTitle)_titleToWrite=_srcTitle;else if(_lpEndBytes<0||_lpEndBytes>65536)_titleToWrite=_lp||"Forked conversation";if(_titleToWrite){if(_titleToWrite.length>200)_titleToWrite=_titleToWrite.slice(0,200);await _1.promises.appendFile(Z,JSON.stringify({type:"custom-title",customTitle:_titleToWrite,sessionId:N})+`\n`)}}return this.loadedSessions.add(N),N}}asy'), ('anel_response"};case"fork_conversation":return{type:"fork_conversation_response",sessionId:await(await c1.load(this.cwd,this.logger)).forkSession(V.request.forkedFromSession,V.request.resumeSessionAt)};case"rewind_code":{let{userMessageId:B,d', 'anel_response"};case"fork_conversation":{let _m=await c1.load(this.cwd,this.logger),_src=V.request.forkedFromSession,_sid=await _m.forkSession(_src,V.request.resumeSessionAt);let _t="";try{let _lines=(await _1.promises.readFile(L1.join(s5(_m.projectRoot),`${_src}.jsonl`),"utf8")).split(`\\n`),_c="",_a="";for(let _line of _lines){if(!_line)continue;try{let _M=JSON.parse(_line);if(_M.type==="custom-title"&&_M.customTitle)_c=_M.customTitle;if(_M.type==="ai-title"&&_M.aiTitle)_a=_M.aiTitle}catch(_){}}_t=_c||_a}catch(_){}if(!_t)_t="Forked conversation";this.onSessionStateChanged?.(_sid,"idle",_t,!0);return{type:"fork_conversation_response",sessionId:_sid}}case"rewind_code":{let{userMessageId:B,d'), ('as(U.id)):B}}async renameSession(V,K,B){return{type:"rename_session_response",skipped:await(await c1.load(this.cwd,this.logger)).renameSession(V,K,B)}}async messageRated(V){try{await this.w', 'as(U.id)):B}}async renameSession(V,K,B){let _r=await(await c1.load(this.cwd,this.logger)).renameSession(V,K,B);if(!_r)this.onSessionStateChanged?.(V,void 0,K);return{type:"rename_session_response",skipped:_r}}async messageRated(V){try{await this.w'), ('d?.(V.request.sessionId,V.request.state,V.request.title),{type:"update_session_state_response"}', 'd?.(V.request.sessionId,V.request.state,void 0),{type:"update_session_state_response"}'), ('geUpdate(K,B)}updateSessionState(V,K,B){this.sessionStates.set(V,{sessionId:V,state:K,title:B}),this.broadcastSessionStates()}setActivePanel(V){for(let[K,B]of this.s', 'geUpdate(K,B)}updateSessionState(V,K,B){/*pfg-v1.2*/let _p=this.sessionStates.get(V);this.sessionStates.set(V,{sessionId:V,state:K!=null?K:_p?.state??"idle",title:B!=null?B:_p?.title}),this.broadcastSessionStates();if(B!=null){let _pnl=this.sessionPanels.get(V);if(_pnl)_pnl.title=B}}setActivePanel(V){for(let[K,B]of this.s'), ('=>this.broadcastUsageUpdate(),!!x,(H,D,z)=>{this.updateSessionState(H,D,z);for(let[L,O]of this.sessionPanels)if(O==', '=>this.broadcastUsageUpdate(),!!x,(H,D,z,_sk)=>{this.updateSessionState(H,D,z);if(!_sk){for(let[L,O]of this.sessionPanels)if(O=='), ('et(H,V),V.active)this.activeSessionId=H});this.allComms.add(Z),V.webview.onDidRe', 'et(H,V),V.active)this.activeSessionId=H}});this.allComms.add(Z),V.webview.onDidRe'), ('),void 0,()=>this.broadcastUsageUpdate());this.allComms.add(N),this.broadcastSes', '),void 0,()=>this.broadcastUsageUpdate(),!1,(H,D,O)=>{this.updateSessionState(H,D,O)});this.allComms.add(N),this.broadcastSes')]), ('webview/index.js', [('t.id===Z)return X}return}function OD($){if($.length>g20){let Z=$.length-u20;return $.slice(Z)}return $}function Hn($,Z,J){if(Z.type===', 't.id===Z)return X}return}function OD($){return $}function Hn($,Z,J){if(Z.type==='), ('ics:X,originalText:Z}}let J=KN0(Z)||Z,Y=J.startsWith("/");return{type:"text",text:J,isSlashComman', 'ics:X,originalText:Z}}let J=KN0(Z)||Z,Y=!1;return{type:"text",text:J,isSlashComman'), ('eturn null;if(Z.isSynthetic)return null;return n1.default.createElement(XR0,{session:$,', 'eturn null;if(Z.isSynthetic)return null;let _ws=n1.default.createElement(XR0,{session:$,'), ('G,setInputError:q,onCreateNewSession:z})}if(Z.type==="assistant"){if(Z.content.e', 'G,setInputError:q,onCreateNewSession:z});if(typeof Z.uuid==="string"){let _r=Z.uuid.startsWith("pfgk-bookend")?"bookend":Z.uuid.startsWith("pfgk-seam-")?"seam":null;if(_r){let _o=_r==="seam"?"bookend":"seam";let _bg=_r==="seam"?"rgba(255,159,28,0.20)":"rgba(220,53,69,0.18)";let _bd=_r==="seam"?"#ff9f1c":"#dc3545";let _emoji="\\u26A0\\uFE0F";_ws=n1.default.createElement("div",{className:"pfgkAlert pfgk-"+_r,"data-pfgk-role":_r,style:{background:_bg,borderLeft:"4px solid "+_bd,borderRadius:"6px",padding:"6px 12px 12px",margin:"6px 0",cursor:"pointer"},title:"Click to jump to "+_o,onClick:function(){var _t=document.querySelector("[data-pfgk-role=\\""+_o+"\\"]");if(_t)_t.scrollIntoView({behavior:"smooth",block:"center"})}},n1.default.createElement("style",{key:"_pfgks"},".pfgkAlert .content_xGDvVg.collapsed_xGDvVg{max-height:none!important}.pfgkAlert .truncationGradient_xGDvVg{display:none}.pfgkAlert .buttonContainer_xGDvVg{display:none}.pfgkAlert .actionButton_v2CdxQ{display:none}"),n1.default.createElement("div",{key:"_pfgkemoji",style:{fontSize:"42px",textAlign:"center",lineHeight:1.1,padding:"6px 0 4px",userSelect:"none"}},_emoji),_ws)}}return _ws}if(Z.type==="assistant"){if(Z.content.e')]), ('webview/index.css', [(':var(--app-primary-background);position:sticky;z-index:2;background-image:linear-gradient(to bottom,var(--sticky-bg)calc(100% - 12px),transparent 100%),linear-gradient(to bottom,var(--app-secondary-background)calc(100% - 12px),transparent 100%);align-items:stretch;padding-top:14px;padding-bottom:12px;top:0}.message_07S1Yg.stickyHeader_07S1Yg:has([aria-expanded=true]){z-index:3}.fullEditor_07S1Yg .message_07S1Yg.stic', ':var(--app-primary-background);position:relative;z-index:auto;align-items:stretch;padding-top:14px;padding-bottom:12px}.message_07S1Yg.stickyHeader_07S1Yg:has([aria-expanded=true]){z-index:auto}.fullEditor_07S1Yg .message_07S1Yg.stic')])]


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

    # Decide state by checking the signature in extension.js. Recognize ANY
    # pfg-vX or pfg-vX.Y signature so a stale prior version (e.g. v1) is
    # detected as needing restore+reapply rather than silently no-op'd or
    # erroring on splice 0.
    ext_js = os.path.join(ext_dir, "extension.js")
    if not os.path.exists(ext_js):
        sys.exit(f"missing: {ext_js}")
    with open(ext_js, "r") as f:
        head = f.read()
    has_current_sig = SIGNATURE in head
    sig_match = re.search(r'/\*pfg-v(\d+(?:\.\d+)?)\*/', head)
    other_sig = sig_match.group(1) if sig_match and not has_current_sig else None

    if has_current_sig and not force:
        print(f"Already patched (signature {SIGNATURE} present). Nothing to do.")
        return

    needs_restore = (force and has_current_sig) or other_sig is not None

    # Apply each file's splices
    for relpath, file_splices in SPLICES:
        target = os.path.join(ext_dir, relpath)
        bak = target + ".bak"
        if needs_restore:
            if not os.path.exists(bak):
                sys.exit(f"need to restore but no backup at {bak}")
            if other_sig:
                print(f"Stale patchset (file has v{other_sig}, current is v{PATCHSET_VERSION}); restoring {target} from {bak}")
            else:
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
                # Maybe already patched — check that new is present
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
        print("node not found on PATH — skipping syntax check.")

    print(f"Patches A-K applied (prebuilt {VERSION}). Reload VSCode to activate.")


if __name__ == "__main__":
    main()
