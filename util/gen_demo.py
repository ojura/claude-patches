import json, uuid, os
# gen_demo.py -- writes synthetic Patch K demo sessions that deterministically
# trigger the recovery markers, for the end-to-end render gate (see
# docs/debugging.md, Step 8). Fixtures are written into the project dir of the
# CURRENT working directory, so run this from inside the OPEN workspace folder
# (the conversations panel only lists projects of open workspace folders), or set
# PFG_DEMO_CWD / PFG_DEMO_PROJ to target a different one.
#
# SEAM fixture => kind:"seam" (unpersisted-predecessor case): a compact_boundary
#   whose logicalParentUuid (PHANTOM) exists in NO .jsonl in the dir, so the
#   walker's Loop A guard  !_seen.has(logicalParentUuid)  is TRUE and it plants a
#   pfgk-seam- ghost reattached to the nearest in-file predecessor. If the phantom
#   WERE resolvable (a sibling defines it), the crossing renders kind:"seamClean" or
#   kind:"bridge" instead, so keep the phantom uuid unique to this fixture.
# BROKEN fixture => kind:"broken": root parentUuid dangles at a unique missing uuid.
def u(): return str(uuid.uuid4())
TS="2026-06-04T11:30:00.000Z"; VER="2.1.159"
# Target the project dir of the current workspace. Claude Code encodes a workspace
# path as the path with every "/" replaced by "-" (so a leading slash becomes a
# leading dash). Override either value via the environment for a different folder.
CWD=os.environ.get("PFG_DEMO_CWD") or os.getcwd()
PROJ_DIR=os.environ.get("PFG_DEMO_PROJ") or CWD.replace("/","-")
def user_rec(uid,parent,sid,cwd,text):
    return {"parentUuid":parent,"isSidechain":False,"promptId":u(),"type":"user","message":{"role":"user","content":text},"isMeta":False,"uuid":uid,"timestamp":TS,"userType":"external","entrypoint":"cli","cwd":cwd,"sessionId":sid,"version":VER,"gitBranch":""}
def asst_rec(uid,parent,sid,cwd,text):
    return {"parentUuid":parent,"isSidechain":False,"message":{"model":"claude-opus-4-8","id":"msg_"+uid.replace('-','')[:24],"type":"message","role":"assistant","content":[{"type":"text","text":text}],"stop_reason":"end_turn","stop_sequence":None,"usage":{"input_tokens":10,"output_tokens":10}},"type":"assistant","uuid":uid,"timestamp":TS,"userType":"external","entrypoint":"cli","cwd":cwd,"sessionId":sid,"version":VER,"gitBranch":""}
def title_rec(sid,t): return {"type":"ai-title","aiTitle":t,"sessionId":sid}
def leaf_rec(sid,leaf): return {"type":"last-prompt","leafUuid":leaf,"sessionId":sid}
def compact_rec(uid,lpu,sid,preserved):
    return {"parentUuid":None,"logicalParentUuid":lpu,"isSidechain":False,"type":"system","subtype":"compact_boundary","content":"Conversation compacted","isMeta":False,"timestamp":TS,"uuid":uid,"level":"info","compactMetadata":{"trigger":"auto","preTokens":900000,"preservedSegment":{"headUuid":preserved[0],"anchorUuid":preserved[0],"tailUuid":preserved[-1]},"preservedMessages":{"anchorUuid":preserved[0],"uuids":preserved}},"sessionId":sid}
def write(projdir,sid,recs):
    d=os.path.expanduser("~/.claude/projects/"+projdir); os.makedirs(d,exist_ok=True)
    with open(os.path.join(d,sid+".jsonl"),"w") as f:
        for r in recs: f.write(json.dumps(r)+"\n")
    return os.path.join(d,sid+".jsonl")

# BROKEN: root parentUuid dangles at a unique missing uuid => kind:"broken"
sid_b=u()
DANGLING="deadbeef-0000-4000-8000-000000000001"
b_u1,b_a1,b_u2,b_a2=u(),u(),u(),u()
broken=[title_rec(sid_b,"PFGK DEMO broken: incomplete-transcript marker (synthetic)"),
 user_rec(b_u1,DANGLING,sid_b,CWD,"PFGK DEMO (broken). This synthetic transcript's root points at a missing upstream message, to exercise the incomplete-transcript marker."),
 asst_rec(b_a1,b_u1,sid_b,CWD,"Acknowledged, synthetic demo conversation for the broken marker."),
 user_rec(b_u2,b_a1,sid_b,CWD,"Continue."),
 asst_rec(b_a2,b_u2,sid_b,CWD,"Continuing the synthetic demo."),
 leaf_rec(sid_b,b_a2)]
pb=write(PROJ_DIR,sid_b,broken)

# SEAM: compact_boundary whose logicalParentUuid is a unique missing phantom => kind:"seam"
sid_s=u()
PHANTOM="deadbeef-0000-4000-8000-000000000002"
s_u1,s_a1,s_cb,s_u2,s_a2=u(),u(),u(),u(),u()
seam=[title_rec(sid_s,"PFGK DEMO seam: in-file phantom reattach marker (synthetic)"),
 user_rec(s_u1,None,sid_s,CWD,"PFGK DEMO (seam). This synthetic transcript has a compaction whose predecessor was never persisted, to exercise the in-file reattach marker."),
 asst_rec(s_a1,s_u1,sid_s,CWD,"Acknowledged, synthetic demo with an in-file compaction."),
 compact_rec(s_cb,PHANTOM,sid_s,[s_u1,s_a1]),
 user_rec(s_u2,s_cb,sid_s,CWD,"Continue after the compaction."),
 asst_rec(s_a2,s_u2,sid_s,CWD,"Continuing after the in-file compaction boundary."),
 leaf_rec(sid_s,s_a2)]
ps=write(PROJ_DIR,sid_s,seam)

# SEAMCLEAN: compact_boundary whose logicalParentUuid resolves to an IN-FILE
#   predecessor in the same .jsonl => kind:"seamClean" (clean in-file crossing,
#   no phantom). A unique NONCE in the message text lets a render probe assert
#   the transcript actually re-rendered fresh (not a stale/rehydrated tab).
NONCE="PFGKNONCE"+uuid.uuid4().hex[:12]
sid_sc=u()
sc_u1,sc_a1,sc_cb,sc_u2,sc_a2=u(),u(),u(),u(),u()
seamclean=[title_rec(sid_sc,"PFGK DEMO seamClean: clean in-file compaction marker (synthetic)"),
 user_rec(sc_u1,None,sid_sc,CWD,"PFGK DEMO (seamClean) "+NONCE+". In-file compaction whose logical parent IS persisted in this file, to exercise the clean in-file crossing marker."),
 asst_rec(sc_a1,sc_u1,sid_sc,CWD,"Acknowledged, synthetic clean in-file compaction. "+NONCE),
 compact_rec(sc_cb,sc_a1,sid_sc,[sc_u1,sc_a1]),
 user_rec(sc_u2,sc_cb,sid_sc,CWD,"Continue after the clean compaction. "+NONCE),
 asst_rec(sc_a2,sc_u2,sid_sc,CWD,"Continuing after the clean in-file compaction boundary.")]
seamclean.append(leaf_rec(sid_sc,sc_a2))
psc=write(PROJ_DIR,sid_sc,seamclean)

print("project dir:",os.path.expanduser("~/.claude/projects/"+PROJ_DIR))
print("BROKEN:   ",sid_b,"->",pb)
print("SEAM:     ",sid_s,"->",ps)
print("SEAMCLEAN:",sid_sc,"-> NONCE",NONCE,"->",psc)
