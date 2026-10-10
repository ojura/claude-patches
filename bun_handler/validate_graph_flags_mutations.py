#!/usr/bin/env python3
"""Focused source mutations for the new record contract. No native image runs.

Every case has an explicit assertion detector. Errors, launch failures and
syntax failures do not count. Each mutant is a disposable independent copy.
"""
import argparse, hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path

MUTATIONS=[
 ('keep_global_cache','disable_prelinked = bool(edited_values) and img.prelinked_module_graph_record_off is not None',
  'disable_prelinked = False','test_equal_edit_detaches_pointer_and_map_before_reparsing'),
 ('keep_file_map_count','struct.pack_into("<III", out, record_off, 0, 0, 0)',
  'struct.pack_into("<III", out, record_off, 0, 0, len(img.prelinked_module_files))','test_equal_edit_detaches_pointer_and_map_before_reparsing'),
 ('keep_graph_pointer','struct.pack_into("<III", out, record_off, 0, 0, 0)',
  'struct.pack_into("<III", out, record_off, *img.prelinked_module_graph, 0)','test_equal_edit_detaches_pointer_and_map_before_reparsing'),
 ('lose_runtime_relocation','out[record_off + 12:record_off + 20] = img.runtime_options_bytes',
  'pass','test_runtime_options_follow_tombstone_not_old_map'),
 ('reset_runtime_options','out[record_off + 12:record_off + 20] = img.runtime_options_bytes',
  'out[record_off + 12:record_off + 20] = b"\\0" * 8','test_runtime_options_follow_tombstone_not_old_map'),
 ('allow_bad_file_index','if any(index >= module_count for index in files):',
  'if False:','test_map_index_out_of_range'),
 ('allow_duplicate_file_index','if len(set(files)) != count:',
  'if False:','test_duplicate_map_index'),
 ('allow_graph_map_disagreement','if bool(length) != bool(count) or (not length and off):',
  'if False:','test_empty_pointer_cannot_have_live_map'),
 ('skip_graph_alignment','add_region("prelinked-module-graph", *self.prelinked_module_graph,\n                   alignment=BUN_BYTECODE_ALIGNMENT)',
  'add_region("prelinked-module-graph", *self.prelinked_module_graph)','test_prelinked_payload_must_be_runtime_aligned'),
 ('skip_graph_ownership','add_region("prelinked-module-graph", *self.prelinked_module_graph,\n                   alignment=BUN_BYTECODE_ALIGNMENT)',
  'pass','test_prelinked_payload_cannot_alias_bytecode'),
 ('silently_drop_lost_policy','if current.flags & BUN_FLAG_HAS_RUNTIME_OPTIONS:\n        raise BunFormatError(\n            "tweakcc discarded runtime options; a matching reference is required")',
  'if False:\n        raise BunFormatError("disabled")','test_source_only_recovery_must_not_guess_discarded_options'),
 ('silently_restore_other_policy','if current.runtime_options_bytes != reference.runtime_options_bytes:\n        raise BunFormatError("reference runtime options differ")',
  'if False:\n        raise BunFormatError("disabled")','test_append_compaction_rejects_changed_runtime_policy'),
]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path);args=parser.parse_args()
    root=Path(__file__).resolve().parent;source=(root/'bun_handler.py').read_text()
    results=[]
    runner='''import hashlib,json,sys,unittest
sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2])
import bun_handler
assert hashlib.sha256(open(bun_handler.__file__,"rb").read()).hexdigest()==sys.argv[4]
suite=unittest.defaultTestLoader.loadTestsFromName("test_graph_flags_278.GraphFlags278Tests."+sys.argv[3])
r=unittest.TextTestRunner(verbosity=2).run(suite)
print("RESULT_JSON="+json.dumps({"tests":r.testsRun,"failures":[[t.id(),s] for t,s in r.failures],"errors":[[t.id(),s] for t,s in r.errors]}))
'''
    with tempfile.TemporaryDirectory(prefix='bun-flags-mutations-') as temp:
        temp=Path(temp)
        for name,before,after,test in MUTATIONS:
            if source.count(before)!=1:raise RuntimeError('mutation anchor not unique: '+name)
            code=source.replace(before,after);compile(code,name,'exec')
            p=temp/name;p.mkdir();(p/'bun_handler.py').write_text(code)
            digest=hashlib.sha256(code.encode()).hexdigest()
            run=subprocess.run([sys.executable,'-c',runner,str(p),str(root),test,digest],capture_output=True,text=True,timeout=20,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
            lines=[s for s in run.stdout.splitlines() if s.startswith('RESULT_JSON=')]
            if len(lines)!=1:raise RuntimeError((name,run.stdout,run.stderr))
            observed=json.loads(lines[0].split('=',1)[1])
            credited=(run.returncode==0 and observed['tests']==1 and len(observed['failures'])==1 and not observed['errors'])
            item={'mutation':name,'before':before,'after':after,'mutant_sha256':digest,'detector':test,'assertion_detected':credited,**observed}
            results.append(item)
    report={'source_sha256':hashlib.sha256(source.encode()).hexdigest(),'mutants':results,'total':len(results),'assertion_detected':sum(r['assertion_detected'] for r in results),'native_mutants_executed':False}
    args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='mutants'}))
    return 0 if report['total']==report['assertion_detected'] else 1

if __name__=='__main__':raise SystemExit(main())
