"""
Behavioral gate: the necessary-AND-sufficient proof that the patched output BINDS.

node --check proves the patched bundle PARSES; it says nothing about whether $pfg is
in scope, the _pv_ aliases resolve, or the require()-bound builtins load at RUN time
(all ReferenceErrors / runtime errors, invisible to node --check). This applies the
REAL engine to a REAL pristine bundle, extracts the ACTUALLY-INJECTED block plus the
injected i1e and d1e, and runs them:
  - i1e over a continuation-preamble session: the loud marker fires, no green bookend.
  - d1e over a real on-disk corpus: readdir/readFile (require("fs/promises")) and
    dirname/join (require("path")) resolve and the cross-file backfill runs. The i1e
    path never touches fs/path, so d1e is the only test that binds those builtins.
If $pfg were out of scope, an alias mis-bound, or a builtin unresolved, this throws or
renders wrong here, where the param-mocked unit tests cannot see it.

A pristine 2.1.195/2.1.191 bundle is auto-discovered from /tmp/ext_pristine.js or any
installed extension's pre-patch .bak, so the gate runs on any dev machine with the
extension installed. If none is found the sufficient gate LOUDLY skips (it never
silently passes): node --check alone is not sufficient.
"""
import functools
import glob
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from pfg.engine import apply, SIGNATURE  # noqa: E402
from pfg import jslex  # noqa: E402


def _find_pristine():
    """The first signature-free (truly pre-patch) 2.1.195/2.1.191 bundle we can find.
    The anchors resolve on both; 2.1.173 is out of scope (n1e was still synchronous)."""
    cands = ["/tmp/ext_pristine.js"]
    for ver in ("2.1.195", "2.1.191"):
        cands += sorted(glob.glob(os.path.expanduser(
            f"~/.*/extensions/anthropic.claude-code-{ver}*/extension.js.bak")))
    for p in cands:
        try:
            with open(p, encoding="utf-8") as f:
                if SIGNATURE not in f.read():  # a truly pristine (pre-patch) bundle
                    return p
        except OSError:
            continue
    return None


PRISTINE = _find_pristine()


# Patch ONCE, but LAZILY (memoized, on first test USE), never at import. Running the
# engine's apply at MODULE level would put it in pytest COLLECTION, where a module-level
# engine-state change in some OTHER test file (collected earlier) could make it raise and
# ABORT collection for the whole directory, masking every other file's results. Deferred to
# test execution, the worst case is a localized failure of this gate's own tests.
@functools.lru_cache(maxsize=1)
def _patched():
    return apply(open(PRISTINE, encoding="utf-8").read()) if PRISTINE else None


_skip = pytest.mark.skipif(
    PRISTINE is None,
    reason="no pristine 2.1.195/2.1.191 bundle found: the SUFFICIENT binding gate did NOT run",
)

# The 9 vendor names the alias prologue binds. i1e only uses o1e/s1e, but the prologue
# binds every derived dep, so all must be defined or `const _pv_Nk=Nk` throws. These
# come BEFORE the block so the prologue's `const _pv_X = X` has its RHS in scope.
_I1E_MOCKS = (
    "const Nk=()=>null, qAe=async()=>null, r1e=async()=>null, n1e=async()=>[], "
    "GY=()=>[], o1e=(t,u,d)=>u.slice(), s1e=()=>false;\n"
)

_I1E_DRIVER = r"""
const PRE={uuid:"P",type:"user",parentUuid:null,sessionId:"s",timestamp:"t1",
  message:{role:"user",content:"This session is being continued from a previous conversation that ran out of context. Summary: ..."}};
const MID={uuid:"M",type:"assistant",parentUuid:null,logicalParentUuid:"P",sessionId:"s",timestamp:"t2",
  message:{role:"assistant",content:"a middle turn that bridges via the logical parent"}};
const LEAF={uuid:"L",type:"user",parentUuid:"M",sessionId:"s",timestamp:"t3",
  message:{role:"user",content:"the latest turn"}};
(async()=>{
  const ren = await i1e([PRE,MID,LEAF], {timing:null,siblingsScanned:0,phantomsBackfilled:0,phantomsCouldNotBackfill:0,provBasenames:{}});
  const whole = JSON.stringify(ren);
  if(!/TRANSCRIPT INCOMPLETE/.test(whole)){ console.error("FAIL: no loud marker"); process.exit(1); }
  if(/reconstructed/i.test(whole)){ console.error("FAIL: false green bookend on a preamble"); process.exit(1); }
  if(typeof $pfg!=="object"){ console.error("FAIL: $pfg not in scope"); process.exit(1); }
  console.log("BEHAVIORAL_OK");
})();
"""

# d1e binds the require() builtins (fs/promises, path) plus Nk/qAe/r1e/n1e/GY. Setup
# writes a REAL corpus dir and defines the mocks BEFORE the block; the driver runs d1e
# and lets the real fs/path run, so a mis-bound _pv_MY / _pv_zn throws here. main's
# boundary dangles on a phantom lpu a sibling carries; the backfill must prepend the
# sibling's origin, which is only reachable through real readdir/readFile.
_D1E_SETUP = r"""
const fs=require("fs"), os=require("os"), path=require("path");
const dir=fs.mkdtempSync(path.join(os.tmpdir(),"pfgbe-"));
const jl=(recs)=>recs.map(r=>JSON.stringify(r)).join("\n")+"\n";
fs.writeFileSync(path.join(dir,"main.jsonl"), jl([
  {type:"system",subtype:"compact_boundary",uuid:"bnd",parentUuid:null,logicalParentUuid:"PH",sessionId:"s",timestamp:"t"},
  {uuid:"leaf",type:"user",parentUuid:"bnd",message:{role:"user",content:"latest"}},
]));
fs.writeFileSync(path.join(dir,"sib.jsonl"), jl([
  {uuid:"orig",type:"user",parentUuid:null,message:{role:"user",content:"the real first prompt"}},
  {uuid:"pre1",type:"user",parentUuid:"orig",message:{role:"user",content:"pre-compaction turn"}},
  {type:"system",subtype:"compact_boundary",uuid:"sbnd",parentUuid:null,logicalParentUuid:"PH",sessionId:"s",timestamp:"t"},
  {uuid:"spost",type:"user",parentUuid:"sbnd",message:{role:"user",content:"sibling post"}},
]));
const Nk=()=>true;
const qAe=async()=>({filePath:path.join(dir,"main.jsonl"),fileSize:1});
const r1e=async(fp)=>fs.readFileSync(fp);
const n1e=async(buf)=>buf.toString("utf8").split("\n").filter(Boolean).map(l=>JSON.parse(l));
const o1e=(t,u,d)=>u.slice(), s1e=()=>false, GY=(parsed)=>parsed;
"""

_D1E_DRIVER = r"""
(async()=>{
  const out = await d1e({}, {dir});
  const uuids = out.map(r=>r.uuid);
  if(!uuids.includes("orig")){ console.error("FAIL: sibling origin not backfilled (fs/path mis-bound?) got "+JSON.stringify(uuids)); process.exit(1); }
  if(typeof $pfg!=="object"){ console.error("FAIL: $pfg not in scope in d1e"); process.exit(1); }
  console.log("BEHAVIORAL_OK");
})();
"""

# d1e fork-backfill over a REAL corpus: main opens on a forkedFrom copy whose SOURCE is
# on disk. F must readFile the source (real fs/path binding), find the copy's pre-fork
# ancestry, prepend it, and re-root the copy. Without a forkedFrom fixture the F block
# runs INERT on the repacked bundle (its binding is covered transitively via the K1
# test, but F's build-path/read/re-root behavior never actually runs). This closes that.
_D1E_FORK_SETUP = r"""
const fs=require("fs"), os=require("os"), path=require("path");
const dir=fs.mkdtempSync(path.join(os.tmpdir(),"pfgfk-"));
const jl=(recs)=>recs.map(r=>JSON.stringify(r)).join("\n")+"\n";
fs.writeFileSync(path.join(dir,"main.jsonl"), jl([
  {uuid:"FC",type:"user",parentUuid:null,forkedFrom:{sessionId:"fsrc",messageUuid:"FC"},message:{role:"user",content:"copied fork point"}},
  {uuid:"fleaf",type:"user",parentUuid:"FC",message:{role:"user",content:"latest"}},
]));
fs.writeFileSync(path.join(dir,"fsrc.jsonl"), jl([
  {uuid:"fsO",type:"user",parentUuid:null,message:{role:"user",content:"the source's real first prompt"}},
  {uuid:"fpre",type:"user",parentUuid:"fsO",message:{role:"user",content:"a pre-fork turn"}},
  {uuid:"FC",type:"user",parentUuid:"fpre",message:{role:"user",content:"the fork point in the source"}},
]));
const Nk=()=>true;
const qAe=async()=>({filePath:path.join(dir,"main.jsonl"),fileSize:1});
const r1e=async(fp)=>fs.readFileSync(fp);
const n1e=async(buf)=>buf.toString("utf8").split("\n").filter(Boolean).map(l=>JSON.parse(l));
const o1e=(t,u,d)=>u.slice(), s1e=()=>false, GY=(parsed)=>parsed;
"""

_D1E_FORK_DRIVER = r"""
(async()=>{
  const out = await d1e({}, {dir});
  const uuids = out.map(r=>r.uuid);
  if(!uuids.includes("fsO") || !uuids.includes("fpre")){ console.error("FAIL: fork source ancestry not backfilled (F inert / fs mis-bound?) got "+JSON.stringify(uuids)); process.exit(1); }
  const fc = out.find(r=>r.uuid==="FC");
  if(!fc || fc.parentUuid!=="fpre"){ console.error("FAIL: fork copy not re-rooted onto its real source parent"); process.exit(1); }
  console.log("BEHAVIORAL_OK");
})();
"""


def _extract(name):
    """The injected $pfg block ALONE (SIGNATURE through the alias prologue's `;`) and
    the named injected function ALONE. Bounding the block precisely, rather than taking
    everything up to the target function, avoids sweeping in the intervening vendor code
    (GY, the other wholesale body, arbitrary bundle statements) that references globals
    this harness never defines."""
    patched = _patched()
    sig = patched.index(SIGNATURE)
    end_marker = patched.index("// ==== end pfg-core ====", sig)
    block_end = patched.index(";", end_marker) + 1  # the alias prologue's terminator
    s, e = jslex.find_function_span(patched, name)
    return patched[sig:block_end], patched[s:e]


def _run(harness):
    fd, tmp = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(harness)
        return subprocess.run(["node", tmp], capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(tmp)


@_skip
def test_patched_i1e_binds_and_fires_marker():
    block, i1e = _extract("i1e")
    r = _run(_I1E_MOCKS + block + i1e + _I1E_DRIVER)
    assert r.returncode == 0, f"i1e behavioral gate failed:\nSTDOUT {r.stdout}\nSTDERR {r.stderr}"
    assert "BEHAVIORAL_OK" in r.stdout


@_skip
def test_patched_d1e_binds_fs_and_backfills():
    block, d1e = _extract("d1e")
    r = _run(_D1E_SETUP + block + d1e + _D1E_DRIVER)
    assert r.returncode == 0, f"d1e behavioral gate failed:\nSTDOUT {r.stdout}\nSTDERR {r.stderr}"
    assert "BEHAVIORAL_OK" in r.stdout


@_skip
def test_patched_d1e_forkbackfill_binds_and_reroots():
    block, d1e = _extract("d1e")
    r = _run(_D1E_FORK_SETUP + block + d1e + _D1E_FORK_DRIVER)
    assert r.returncode == 0, f"d1e fork-backfill gate failed:\nSTDOUT {r.stdout}\nSTDERR {r.stderr}"
    assert "BEHAVIORAL_OK" in r.stdout
