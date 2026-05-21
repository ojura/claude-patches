#!/usr/bin/env python3
"""
Prebuilt patch apply for the anthropic.claude-code VS Code extension 2.1.146.

Patches A through K applied as literal string replacements verified
byte-stable against the 2.1.146 bundle. Synthesized by
util/build-prebuilt.py from the diff between the patched live extension
and its pre-patch backups.

Usage:
  python3 apply.py [/path/to/extension/dir] [--force]

Default: auto-discovers an installed 2.1.146 extension under
~/.<ide>/extensions/ for any IDE that pulls from Open VSX (VS Code,
Antigravity, Cursor, VSCodium, etc.).

Idempotent: re-running on already-patched files is a no-op (detects the
/*pfg-v1.7*/ signature in extension.js). With --force, restores from .bak files
and re-applies.
"""
import glob
import os
import re
import shutil
import subprocess
import sys

VERSION = "2.1.146"


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


SIGNATURE = "/*pfg-v1.7*/"
PATCHSET_VERSION = re.match(r'/\*pfg-v(\d+(?:\.\d+)?)\*/', SIGNATURE).group(1)

# Each entry: (file_relpath, [(old, new), (old, new), ...])
SPLICES = [('extension.js', [('"adaptive");break}if(L.type!=="disabled"&&L.display)i.push("--thinking-display",L.display)}if(this.options.effort)i.push("--effor', '"adaptive");break}if(L.type!=="disabled")i.push("--thinking-display",L.display||"summarized")}if(this.options.effort)i.push("--effor'), ('}async function I_0(z,V){try{if(V>O_0&&!B8(process.env.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP))return(await j_0(z,V)).postBoundaryBuf', '}async function I_0(z,V){try{if(V>O_0&&!(!0||B8(process.env.CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP)))return(await j_0(z,V)).postBoundaryBuf'), ('reak}M=M.parentUuid?V.get(M.parentUuid):void 0}}if(B.length===0)return[];let Z=B.filte', 'reak}M=M.parentUuid?V.get(M.parentUuid):(M.logicalParentUuid?V.get(M.logicalParentUuid):void 0)}}if(B.length===0)return[];let Z=B.filte'), ('h(D),D=D.parentUuid?V.get(D.parentUuid):void 0}return L.reverse(),E_0(V,L,q)}function ', 'h(D),D=D.parentUuid?V.get(D.parentUuid):(D.logicalParentUuid?V.get(D.logicalParentUuid):void 0)}return L.reverse(),E_0(V,L,q)}function '), ('0(K.filePath,K.fileSize);if(!x)return[];return wa(v_0(x),V)}function Fa(z,V,K){let{head:x,tail:N', '0(K.filePath,K.fileSize);if(!x)return[];let _kT0=Date.now();let _parsed=v_0(x);let _kTparse=Date.now();let _seen=new Set(_parsed.map(_m=>_m.uuid));let _dir=u1.dirname(K.filePath);let _entries=await r1.promises.readdir(_dir);let _filesParsed=new Map();for(let _pass=0;_pass<10;_pass++){let _dangling=[];for(let _m of _parsed)if(_m.type==="system"&&_m.subtype==="compact_boundary"&&!_m.parentUuid&&_m.logicalParentUuid&&!_seen.has(_m.logicalParentUuid))_dangling.push(_m.logicalParentUuid);if(_dangling.length===0)break;let _maxByFile=new Map();for(let _lpu of _dangling){for(let _name of _entries){if(!_name.endsWith(".jsonl"))continue;let _path=u1.join(_dir,_name);if(_path===K.filePath)continue;let _siblingMsgs=_filesParsed.get(_path);if(!_siblingMsgs){let _buf=await Ha.readFile(_path);let _str=_buf.toString("utf-8");if(!_str.includes(`"uuid":"${_lpu}"`))continue;_siblingMsgs=v_0(_buf);_filesParsed.set(_path,_siblingMsgs);}let _found=-1;for(let _i=0;_i<_siblingMsgs.length;_i++)if(_siblingMsgs[_i].uuid===_lpu){_found=_i;break}if(_found===-1)continue;let _prev=_maxByFile.get(_path);if(_prev===void 0||_found>_prev)_maxByFile.set(_path,_found);break;}}if(_maxByFile.size===0)break;let _newPrepend=[];for(let[_path,_maxIdx]of _maxByFile){let _siblingMsgs=_filesParsed.get(_path);for(let _i=0;_i<=_maxIdx;_i++){let _m=_siblingMsgs[_i];if(_m&&!_seen.has(_m.uuid)){_newPrepend.push(_m);_seen.add(_m.uuid)}}}if(_newPrepend.length===0)break;_parsed=[..._newPrepend,..._parsed];}let _kTjprepend=Date.now();let _ambigPhLpus=new Set();let _k1Sources=new Map();{let _phLpus=new Set();for(let _m of _parsed)if(_m.type==="system"&&_m.subtype==="compact_boundary"&&!_m.parentUuid&&_m.logicalParentUuid&&!_seen.has(_m.logicalParentUuid))_phLpus.add(_m.logicalParentUuid);for(let _phLpu of _phLpus){let _bestPre=null;let _bestSrc=null;let _ambigCount=0;for(let _name of _entries){if(!_name.endsWith(".jsonl"))continue;let _path=u1.join(_dir,_name);if(_path===K.filePath)continue;let _sm=_filesParsed.get(_path);if(!_sm){try{let _buf=await Ha.readFile(_path);let _str=_buf.toString("utf-8");if(!_str.includes(`"logicalParentUuid":"${_phLpu}"`))continue;_sm=v_0(_buf);_filesParsed.set(_path,_sm);}catch{continue}}else{let _has=!1;for(let _x of _sm){if(_x.type==="system"&&_x.subtype==="compact_boundary"&&_x.logicalParentUuid===_phLpu){_has=!0;break}}if(!_has)continue}let _bIdx=-1;for(let _j=0;_j<_sm.length;_j++){let _x=_sm[_j];if(_x.type==="system"&&_x.subtype==="compact_boundary"&&_x.logicalParentUuid===_phLpu){_bIdx=_j;break}}if(_bIdx<=0)continue;let _hasReal=!1;for(let _j=0;_j<_bIdx;_j++){if(_sm[_j].type==="user"||_sm[_j].type==="assistant"){_hasReal=!0;break}}if(!_hasReal)continue;let _firstChild=null;for(let _j=0;_j<_sm.length;_j++){if(_sm[_j].parentUuid==null&&(_sm[_j].type==="user"||_sm[_j].type==="assistant")&&!_sm[_j].logicalParentUuid){_firstChild=_sm[_j];break}}if(!_firstChild)continue;_ambigCount++;let _pre=[];for(let _j=0;_j<_bIdx;_j++){let _x=_sm[_j];if(_x&&_x.uuid&&!_seen.has(_x.uuid)){_pre.push(_x);_seen.add(_x.uuid)}}if(_pre.length===0)continue;if(!_bestPre||_pre.length>_bestPre.length){_bestPre=_pre;_bestSrc=_name}}if(_bestPre){_parsed=[..._bestPre,..._parsed];_k1Sources.set(_phLpu,{src:_bestSrc,count:_bestPre.length,candidates:_ambigCount})}if(_ambigCount>1)_ambigPhLpus.add(_phLpu)}}let _kTk1=Date.now();let _kFired=!1;let _kAttempted=!1;for(let _i=0;_i<_parsed.length;_i++){let _m=_parsed[_i];if(_m.type==="system"&&_m.subtype==="compact_boundary"&&!_m.parentUuid&&_m.logicalParentUuid&&!_seen.has(_m.logicalParentUuid)){_kAttempted=!0;let _predUuid=null;for(let _j=_i-1;_j>=0;_j--){if(_parsed[_j].uuid){_predUuid=_parsed[_j].uuid;break}}if(!_predUuid)continue;let _seamUuid="pfgk-seam-"+_m.uuid.slice(0,8);let _origLpu=_m.logicalParentUuid;let _ghostContent="PATCH K · Compaction event (in-file orphan reattached)\\n\\n  • missing phantom uuid: "+_origLpu+"\\n  • reattached to in-file predecessor: "+String(_predUuid)+"\\n\\nClaude Code compacted the conversation here. The compactor referenced a chain predecessor that was never persisted to disk — a write-side bug at compact.ts:598. Patch K reconnected the chain via the in-file predecessor so no persisted message is dropped from the rendered transcript. Click anywhere on this notice to jump to the next marker.";if(_ambigPhLpus&&_ambigPhLpus.has(_origLpu))_ghostContent="⚠ AMBIGUOUS RECONSTRUCTION: multiple sibling files qualified for backfill at this compaction event — the prepended pre-compaction content is one of several possible reconstructions. The chain root marker may not match this branch\'s actual canonical origin.\\n\\n"+_ghostContent;let _ghost={type:"user",uuid:_seamUuid,parentUuid:_predUuid,sessionId:_m.sessionId,timestamp:_m.timestamp,message:{role:"user",content:_ghostContent}};_parsed.splice(_i,0,_ghost);_m.logicalParentUuid=_seamUuid;_seen.add(_ghost.uuid);_kFired=!0;_i++}}if(_kAttempted){let _bookendFired=!1;for(let _i=0;_i<_parsed.length;_i++){let _r=_parsed[_i];if(_r.uuid&&_r.parentUuid==null&&!_r.logicalParentUuid&&_r.type!=="system"){let _bid="pfgk-bookend-"+_r.uuid;let _beHeader="PATCH K · Conversation origin (chain root recovered)\\n\\n";let _beDetails="";if(_k1Sources&&_k1Sources.size>0){let _lines=[];for(let[_lpu,_info]of _k1Sources)_lines.push("  • phantom "+_lpu+": backfilled "+_info.count+" msgs from "+_info.src+(_info.candidates>1?" (chosen from "+_info.candidates+" candidates)":""));_beDetails="K1 sibling-backfill summary:\\n"+_lines.join("\\n")+"\\n\\n"}let _beTiming="K stitching wall-clock: parse "+(_kTparse-_kT0)+"ms, J cross-file prepend "+(_kTjprepend-_kTparse)+"ms, K1 sibling backfill "+(_kTk1-_kTjprepend)+"ms, K2/K3/bookend "+(Date.now()-_kTk1)+"ms";let _beContent=_beHeader+_beDetails+"Claude Code\'s compactor wrote a phantom logicalParentUuid (compact.ts:598 bug). Patches J + K reconstructed the conversation chain by following shared phantom-lpu pointers across sibling .jsonls — every persisted message in this branch is now reachable from this view. The full conversation flows below in chronological order, with each subsequent compaction event marked. Click anywhere on this notice to jump to the next marker.\\n\\n"+_beTiming;if(_ambigPhLpus&&_ambigPhLpus.size>0)_beContent="⚠ AMBIGUOUS RECONSTRUCTION: "+_ambigPhLpus.size+" phantom-lpu compaction event(s) had multiple sibling-file candidates for backfill. The chain root shown here is one of several possible canonical origins. Verify against your other forked sessions if uncertain.\\n\\n"+_beContent;let _be={type:"user",uuid:_bid,parentUuid:null,sessionId:_r.sessionId,timestamp:_r.timestamp,message:{role:"user",content:_beContent}};_parsed.splice(_i,0,_be);_r.parentUuid=_bid;_seen.add(_bid);_bookendFired=!0;break}}for(let _i=0;_i<_parsed.length;_i++){let _m=_parsed[_i];if(_m.type==="system"&&_m.subtype==="compact_boundary"&&!_m.parentUuid&&_m.logicalParentUuid&&_seen.has(_m.logicalParentUuid)&&!String(_m.logicalParentUuid).startsWith("pfgk-")){let _prev=null;for(let _j=_i-1;_j>=0;_j--){if(_parsed[_j].uuid){_prev=_parsed[_j];break}}if(!_prev)continue;if(_prev.sessionId!==_m.sessionId)continue;let _firstChild=null;for(let _j=0;_j<_parsed.length;_j++){if(_parsed[_j].parentUuid===_m.uuid){_firstChild=_parsed[_j];break}}if(!_firstChild)continue;let _bridgeUuid="pfgk-bridge-"+_m.uuid.slice(0,8);let _xfileSrc=null;if(_filesParsed)for(let[_p,_sm]of _filesParsed){let _hit=!1;for(let _x of _sm)if(_x.uuid===_m.logicalParentUuid){_hit=!0;break}if(_hit){_xfileSrc=u1.basename(_p);break}}let _bridgeContent="PATCH K · Compaction event (cross-file resolved, in-file orphan kept)\\n\\n  • boundary uuid: "+_m.uuid+"\\n  • J-resolved predecessor uuid: "+String(_m.logicalParentUuid)+" (lives in sibling: "+(_xfileSrc||"unknown")+")\\n  • K bridge points at in-file predecessor: "+_prev.uuid+"\\n\\nClaude Code compacted the conversation here. The compactor\'s captured predecessor uuid lives in a sibling .jsonl (forked-from session). Patch J resolved it cross-file; Patch K bridges the in-file orphan chain back into view so the conversation\'s in-file pre-compaction history isn\'t silently dropped in favour of the cross-file shortcut. Click anywhere on this notice to jump to the next marker.";let _bridge={type:"user",uuid:_bridgeUuid,parentUuid:_prev.uuid,sessionId:_m.sessionId,timestamp:_m.timestamp,message:{role:"user",content:_bridgeContent}};_firstChild.parentUuid=_bridgeUuid;_parsed.push(_bridge);_seen.add(_bridgeUuid);break}}if(!_bookendFired){let _byUuid=new Map();for(let _m of _parsed)if(_m.uuid)_byUuid.set(_m.uuid,_m);for(let _i=0;_i<_parsed.length;_i++){let _r=_parsed[_i];if(!_r.uuid||(_r.type!=="user"&&_r.type!=="assistant"))continue;let _walk=_r,_hops=0,_hitPhantom=!1;while(_walk&&_hops<5){if(_walk.parentUuid==null){if(_walk.type==="system"&&_walk.subtype==="compact_boundary"&&_walk.logicalParentUuid&&!_seen.has(_walk.logicalParentUuid)&&!String(_walk.logicalParentUuid).startsWith("pfgk-"))_hitPhantom=!0;break}let _parent=_byUuid.get(_walk.parentUuid);if(!_parent)break;_walk=_parent;_hops++}if(_hitPhantom){let _bid="pfgk-broken-"+_r.uuid;let _phantomLpu=_walk.logicalParentUuid;let _siblingsCount=(_entries||[]).filter(n=>n.endsWith(".jsonl")).length-1;let _phantomsTotal=(_k1Sources?_k1Sources.size:0);let _phantomsAttempted=0;for(let _bm of _parsed)if(_bm.type==="system"&&_bm.subtype==="compact_boundary"&&_bm.logicalParentUuid&&!_seen.has(_bm.logicalParentUuid)&&!String(_bm.logicalParentUuid).startsWith("pfgk-"))_phantomsAttempted++;let _bcontent="⛔ INCOMPLETE TRANSCRIPT — RECONSTRUCTION FAILED\\n\\n  • dead-end phantom uuid: "+String(_phantomLpu)+"\\n  • sibling .jsonls examined in project dir: "+_siblingsCount+"\\n  • phantoms successfully backfilled: "+_phantomsTotal+"\\n  • phantoms K could not backfill: "+_phantomsAttempted+"\\n\\nThe canonical conversation origin for this branch could not be reconstructed. The chain walker reached this point by walking up parentUuid pointers and dead-ending at a phantom-lpu compaction boundary that K could not backfill from any sibling .jsonl. Upstream lineage is missing from this view.\\n\\nPossible causes: (1) the sibling .jsonl that originally held this branch\'s pre-compaction content has been deleted or moved; (2) the compaction predecessor uuid was never persisted to any file (singular phantom message that K could not recover); (3) the conversation tree has a topology K does not yet handle.\\n\\nThe content below this notice is what K was able to assemble — it is a partial view, not the full canonical chain. Patch K (claude-patches pfg-v1.6).\\n\\nK stitching wall-clock: parse "+(_kTparse-_kT0)+"ms, J cross-file prepend "+(_kTjprepend-_kTparse)+"ms, K1 sibling backfill "+(_kTk1-_kTjprepend)+"ms, K2/K3/bookend "+(Date.now()-_kTk1)+"ms";let _be={type:"user",uuid:_bid,parentUuid:null,sessionId:_r.sessionId,timestamp:_r.timestamp,message:{role:"user",content:_bcontent}};_parsed.splice(_i,0,_be);_r.parentUuid=_bid;_seen.add(_bid);_bookendFired=!0;break}}}}let _kTk2=Date.now();return wa(_parsed,V)}function Fa(z,V,K){let{head:x,tail:N'), ('rse(D);if(!Number.isNaN(R))U=R}let M=L||kz(N,"lastPrompt")||kz(N,"summary")||q;if(!M)return null;let G=kz(N,"gitBranch', 'rse(D);if(!Number.isNaN(R))U=R}let M=L||q||kz(N,"summary")||kz(N,"lastPrompt");if(!M)return null;let G=kz(N,"gitBranch'), ('e")??$9(O,"aiTitle")??$9(Z,"aiTitle"))||$9(O,"lastPrompt")||$9(O,"summary")||e10(Z);if(!M)return null;return{lastModified:', 'e")??$9(O,"aiTitle")??$9(Z,"aiTitle"))||e10(Z)||$9(O,"summary")||$9(O,"lastPrompt");if(!M)return null;return{lastModified:'), ('geId,v);if(L&&I)this.summaries.set(I,L);return this.loadedSessions.add(Z),Z}}asy', 'geId,v);if(L&&I)this.summaries.set(I,L);{let _srcCustom="",_srcAi="";try{let _src=(await r1.promises.readFile(q,"utf8")).split(`\\n`);for(let _line of _src){if(!_line)continue;try{let _M=JSON.parse(_line);if(_M.type==="custom-title"&&_M.customTitle)_srcCustom=_M.customTitle;if(_M.type==="ai-title"&&_M.aiTitle)_srcAi=_M.aiTitle}catch(_){}}}catch(_){}let _srcTitle=_srcCustom||_srcAi;let _lp="",_lpEndBytes=-1,_byteOffset=0;for(let _i=0;_i<G.length;_i++){let _m=G[_i];let _lineBytes=Buffer.byteLength(JSON.stringify(_m)+`\\n`,"utf8");if(_lpEndBytes<0&&_m.type==="user"&&!_m.isCompactSummary&&!_m.isMeta){let _mc=_m.message?.content;let _txt=null;if(typeof _mc==="string"&&_mc.trim())_txt=_mc;else if(Array.isArray(_mc))for(let _c of _mc){if(_c.type==="text"&&_c.text?.trim()){_txt=_c.text;break}if(_c.type==="tool_result")break}if(_txt){_lp=_txt;_lpEndBytes=_byteOffset+_lineBytes}}_byteOffset+=_lineBytes;if(_lpEndBytes>=0&&_byteOffset>65536)break}let _titleToWrite="";if(_srcTitle)_titleToWrite=_srcTitle;else if(_lpEndBytes<0||_lpEndBytes>65536)_titleToWrite=_lp||"Forked conversation";if(_titleToWrite){if(_titleToWrite.length>200)_titleToWrite=_titleToWrite.slice(0,200);await r1.promises.appendFile(H,JSON.stringify({type:"custom-title",customTitle:_titleToWrite,sessionId:Z})+`\\n`)}}return this.loadedSessions.add(Z),Z}}asy'), ('anel_response"};case"fork_conversation":return{type:"fork_conversation_response",sessionId:await(await U8.load(this.cwd,this.logger)).forkSession(z.request.forkedFromSession,z.request.resumeSessionAt)};case"rewind_code":{let{userMessageId:K,d', 'anel_response"};case"fork_conversation":{let _m=await U8.load(this.cwd,this.logger),_src=z.request.forkedFromSession,_sid=await _m.forkSession(_src,z.request.resumeSessionAt);let _t="";try{let _lines=(await r1.promises.readFile(u1.join(oz(_m.projectRoot),`${_src}.jsonl`),"utf8")).split(`\\n`),_c="",_a="";for(let _line of _lines){if(!_line)continue;try{let _M=JSON.parse(_line);if(_M.type==="custom-title"&&_M.customTitle)_c=_M.customTitle;if(_M.type==="ai-title"&&_M.aiTitle)_a=_M.aiTitle}catch(_){}}_t=_c||_a}catch(_){}if(!_t)_t="Forked conversation";this.onSessionStateChanged?.(_sid,"idle",_t,!0);return{type:"fork_conversation_response",sessionId:_sid}}case"rewind_code":{let{userMessageId:K,d'), ('as(B.id)):K}}async renameSession(z,V,K){return{type:"rename_session_response",skipped:await(await U8.load(this.cwd,this.logger)).renameSession(z,V,K)}}async messageRated(z){try{await this.w', 'as(B.id)):K}}async renameSession(z,V,K){let _r=await(await U8.load(this.cwd,this.logger)).renameSession(z,V,K);if(!_r)this.onSessionStateChanged?.(z,void 0,V);return{type:"rename_session_response",skipped:_r}}async messageRated(z){try{await this.w'), ('d?.(z.request.sessionId,z.request.state,z.request.title),{type:"update_session_state_response"}', 'd?.(z.request.sessionId,z.request.state,void 0),{type:"update_session_state_response"}'), ('geUpdate(V,K)}updateSessionState(z,V,K){this.sessionStates.set(z,{sessionId:z,state:V,title:K}),this.broadcastSessionStates()}setActivePanel(z){for(let[V,K]of this.s', 'geUpdate(V,K)}updateSessionState(z,V,K){/*pfg-v1.7*/let _p=this.sessionStates.get(z);this.sessionStates.set(z,{sessionId:z,state:V!=null?V:_p?.state??"idle",title:K!=null?K:_p?.title}),this.broadcastSessionStates();if(K!=null){let _pnl=this.sessionPanels.get(z);if(_pnl)_pnl.title=K}}setActivePanel(z){for(let[V,K]of this.s'), ('=>this.broadcastUsageUpdate(),!!x,(q,D,U)=>{this.updateSessionState(q,D,U);for(let[M,G]of this.sessionPanels)if(G==', '=>this.broadcastUsageUpdate(),!!x,(q,D,U,_sk)=>{this.updateSessionState(q,D,U);if(!_sk){for(let[M,G]of this.sessionPanels)if(G=='), ('et(q,z),z.active)this.activeSessionId=q});this.allComms.add(H),z.webview.onDidRe', 'et(q,z),z.active)this.activeSessionId=q}});this.allComms.add(H),z.webview.onDidRe'), ('),void 0,()=>this.broadcastUsageUpdate());this.allComms.add(Z),this.broadcastSes', '),void 0,()=>this.broadcastUsageUpdate(),!1,(H,D,O)=>{this.updateSessionState(H,D,O)});this.allComms.add(Z),this.broadcastSes')]), ('webview/index.js', [('t.id===Z)return X}return}function TD($){if($.length>d20){let Z=$.length-a20;return $.slice(Z)}return $}function Wn($,Z,J){if(Z.type===', 't.id===Z)return X}return}function TD($){return $}function Wn($,Z,J){if(Z.type==='), ('ics:X,originalText:Z}}let J=MN0(Z)||Z,Y=J.startsWith("/");return{type:"text",text:J,isSlashComman', 'ics:X,originalText:Z}}let J=MN0(Z)||Z,Y=!1;return{type:"text",text:J,isSlashComman'), ('eturn null;if(Z.isSynthetic)return null;return n1.default.createElement(HR0,{session:$,', 'eturn null;if(Z.isSynthetic)return null;let _ws=n1.default.createElement(HR0,{session:$,'), ('G,setInputError:q,onCreateNewSession:z})}if(Z.type==="assistant"){if(Z.content.e', 'G,setInputError:q,onCreateNewSession:z});if(typeof Z.uuid==="string"){let _r=Z.uuid.startsWith("pfgk-broken-")?"broken":Z.uuid.startsWith("pfgk-bookend")?"bookend":Z.uuid.startsWith("pfgk-seam-")?"seam":Z.uuid.startsWith("pfgk-bridge-")?"bridge":null;if(_r){let _bg=_r==="seam"?"rgba(255,159,28,0.20)":_r==="bookend"?"rgba(220,53,69,0.18)":_r==="broken"?"rgba(180,0,0,0.50)":"rgba(255,107,28,0.20)";let _bd=_r==="seam"?"#ff9f1c":_r==="bookend"?"#dc3545":_r==="broken"?"#990000":"#ff6b1c";let _emoji=_r==="broken"?"\\u26D4":"\\u26A0\\uFE0F";let _allPfgk=[];try{let _allMsgs=$.messages.peek();for(let _m of _allMsgs)if(String(_m.uuid).startsWith("pfgk-"))_allPfgk.push(String(_m.uuid))}catch(_){}let _myIdx=_allPfgk.indexOf(String(Z.uuid));let _total=_allPfgk.length;let _isLast=_myIdx===_total-1;let _headerStr=_total>0?("MARKER "+(_myIdx+1)+" OF "+_total+" \\u00B7 "+(_isLast?"CYCLE TO TOP \\u21BA":"CLICK FOR NEXT \\u2193")):"PATCH K \\u00B7 CLICK TO NAVIGATE";_ws=n1.default.createElement("div",{className:"pfgkAlert pfgk-"+_r,"data-pfgk-role":_r,style:{background:_bg,borderLeft:"6px solid "+_bd,border:_r==="broken"?"4px solid "+_bd:undefined,borderRadius:"6px",padding:"6px 12px 12px",margin:"6px 0",cursor:"pointer",boxShadow:_r==="broken"?"0 0 12px rgba(180,0,0,0.6)":undefined},title:"Click to jump to next Patch K marker",onClick:function(_e){var _all=Array.from(document.querySelectorAll("[data-pfgk-role]"));var _idx=_all.indexOf(_e.currentTarget);if(_idx<0)return;var _next=_all[(_idx+1)%_all.length];if(_next)_next.scrollIntoView({behavior:"smooth",block:"center"})}},n1.default.createElement("style",{key:"_pfgks"},".pfgkAlert .content_xGDvVg.collapsed_xGDvVg{max-height:none!important}.pfgkAlert .truncationGradient_xGDvVg{display:none}.pfgkAlert .buttonContainer_xGDvVg{display:none}.pfgkAlert .actionButton_v2CdxQ{display:none}"),n1.default.createElement("div",{key:"_pfgkhead",style:{fontSize:"13px",fontWeight:800,letterSpacing:"2px",textAlign:"center",color:_r==="broken"?"#ffffff":_bd,textTransform:"uppercase",padding:"6px 0 4px",borderBottom:"2px dashed "+(_r==="broken"?"#ffffff":_bd),margin:"0 0 6px",userSelect:"none"}},_headerStr),n1.default.createElement("div",{key:"_pfgkemoji",style:{fontSize:"42px",textAlign:"center",lineHeight:1.1,padding:"4px 0 4px",userSelect:"none"}},_emoji),_ws)}}return _ws}if(Z.type==="assistant"){if(Z.content.e')]), ('webview/index.css', [(':var(--app-primary-background);position:sticky;z-index:2;background-image:linear-gradient(to bottom,var(--sticky-bg)calc(100% - 12px),transparent 100%),linear-gradient(to bottom,var(--app-secondary-background)calc(100% - 12px),transparent 100%);align-items:stretch;padding-top:14px;padding-bottom:12px;top:0}.message_07S1Yg.stickyHeader_07S1Yg:has([aria-expanded=true]){z-index:3}.fullEditor_07S1Yg .message_07S1Yg.stic', ':var(--app-primary-background);position:relative;z-index:auto;align-items:stretch;padding-top:14px;padding-bottom:12px}.message_07S1Yg.stickyHeader_07S1Yg:has([aria-expanded=true]){z-index:auto}.fullEditor_07S1Yg .message_07S1Yg.stic')])]


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
