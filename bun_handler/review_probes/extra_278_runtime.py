#!/usr/bin/env python3
"""Additional real-runtime checks using the uploaded Linux executable.
Small derived three-module programs are not claimed as full Claude sessions.
Only ordinary writer outputs and exact restored originals are executed.
"""
import argparse,gc,hashlib,json,os,subprocess,sys,tempfile,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('handler',type=Path);p.add_argument('binary',type=Path);p.add_argument('report',type=Path);a=p.parse_args()
sys.path.insert(0,str(a.handler.resolve()));import bun_handler as h
a.report.mkdir(exist_ok=False)
# Generate pinned append fixtures before retaining large Python snapshots.
producer=a.handler/'reference/make_tweakcc_binary.cjs'
producer_env={**os.environ,'TYPESCRIPT_PATH':os.environ.get('TYPESCRIPT_PATH','/opt/nvm/versions/node/v22.16.0/lib/node_modules/typescript')}
once=a.report/'pr-once-not-run';twice=a.report/'pr-twice-not-run';producer_results=[]
for src,dest,mode in [(a.binary,once,'literal'),(once,twice,'repeat')]:
    cp=subprocess.run(['node',str(producer.resolve()),str(src.resolve()),str(dest.resolve()),mode],env=producer_env,capture_output=True,text=True,timeout=60)
    producer_results.append({'mode':mode,'exit':cp.returncode,'stdout':cp.stdout,'stderr':cp.stderr})
    assert cp.returncode==0,(cp.stdout,cp.stderr)
original=a.binary.read_bytes();image=h.BunImage(original);sha=lambda b:hashlib.sha256(b).hexdigest()
env={**os.environ,'HOME':str(a.report/'home'),'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','DISABLE_TELEMETRY':'1','DISABLE_ERROR_REPORTING':'1','DISABLE_AUTOUPDATER':'1'};Path(env['HOME']).mkdir()
r={'input_sha256':sha(original),'handler_sha256':sha(Path(h.__file__).read_bytes()),'cases':[],'derived_program_modules':[5,0,1],'producers':producer_results}

def save():(a.report/'results.json').write_text(json.dumps(r,indent=2,ensure_ascii=False)+'\n')
def run(label,data,expected=None,error_tokens=(),argv=('--version',)):
    start=time.monotonic();i=h.BunImage(data)
    assert i.runtime_options_bytes==image.runtime_options_bytes
    with tempfile.TemporaryDirectory(prefix='review278-') as td:
        path=Path(td)/'cli';path.write_bytes(data);path.chmod(0o700)
        cp=subprocess.run([str(path),*argv],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45)
        tools=[]
        for tool,flags in [('readelf',['-aW']),('objdump',['-h'])]:
            t=subprocess.run([tool,*flags,str(path)],capture_output=True,timeout=15);assert t.returncode==0 and not t.stderr
            tools.append({'tool':tool,'exit':t.returncode,'stderr':t.stderr.decode()})
    stdout=cp.stdout.decode();stderr=cp.stderr.decode()
    passed=(cp.returncode==0 and (expected is None or stdout==expected)) if not error_tokens else cp.returncode!=0 and all(s in stderr for s in error_tokens)
    (a.report/(label+'.stdout')).write_bytes(cp.stdout);(a.report/(label+'.stderr')).write_bytes(cp.stderr)
    r['cases'].append({'label':label,'sha256':sha(data),'bytes':len(data),'exit':cp.returncode,'stdout_sha256':sha(cp.stdout),'stdout':stdout if len(stdout)<1000 else stdout[:120],'stderr':stderr,'prelinked_graph':i.prelinked_module_graph,'prelinked_map_count':len(i.prelinked_module_files),'runtime_options':i.runtime_options,'expected_outcome':bool(passed),'tools':tools,'seconds':round(time.monotonic()-start,3)})
    save();print(label,cp.returncode,repr(stdout[:100]),flush=True);assert passed,(label,stderr);return cp.stdout

def edit(data,replacements):
    return h.repack_module_sources(data,[(idx,image.modules[idx]['name_bytes'],source) for idx,source in replacements.items()],expected_sha256=sha(data))

oldhelp=run('original_help_baseline',original,argv=('--help',))
first=image.modules[0]['name_bytes'].decode().rsplit('/',1)[1];second=image.modules[1]['name_bytes'].decode().rsplit('/',1)[1]
entry=('import {value} from "./'+first+'";console.log("static="+value);const {word}=await import("./'+second+'");console.log(word);\n').encode()
out=edit(original,{5:entry,0:b'export let value = 41;\n',1:b'export const word = "dynamic";\n'})
run('static_then_dynamic_import',out,'static=41\ndynamic\n')
nextout=edit(out,{0:b'export let value = 42;\n'});r['equal_size_change_growth']=len(nextout)-len(out);assert len(nextout)==len(out);out=nextout;del nextout
run('unchanged_importer_changed_exporter',out,'static=42\ndynamic\n')
out=edit(out,{1:b'export const word = await Promise.resolve("dynamic-await");\n'})
run('dynamic_dependency_top_level_await',out,'static=42\ndynamic-await\n')
bad=edit(out,{0:b'export let other = 42;\n'})
run('removed_named_export',bad,error_tokens=('Export named','value','not found'));del bad;gc.collect()
empty=edit(out,{1:b''});run('empty_dynamic_module',empty,'static=42\nundefined\n')
regrown=edit(empty,{1:b'export const word = "regrown";\n'});run('regrown_dynamic_module',regrown,'static=42\nregrown\n');del empty,out;gc.collect()
restored=edit(regrown,{idx:image.read_source(image.modules[idx]) for idx in [5,0,1]});del regrown;gc.collect()
run('restore_vendor_sources',restored,'2.1.278 (Claude Code)\n')
helpout=run('restore_vendor_help',restored,argv=('--help',));assert helpout==oldhelp
r['restored_help_matches_original']=True;del restored;gc.collect()
# The pinned producer's stale-cache intermediate is never executed.
appended=twice.read_bytes();ai=h.BunImage(appended)
replacements={m['index']:image.read_source(image.modules[m['index']]) for m in ai.source_modules() if ai.read_source(m)!=image.read_source(image.modules[m['index']])}
reverted=edit(appended,replacements);del appended,ai;gc.collect()
compacted=h.compact_with_reference(reverted,original);r['reverted_compaction_identity']=compacted==original;assert compacted==original
r['reverted_compaction_reclaimed']=len(reverted)-len(compacted)
run('compaction_restores_original_active_cache',compacted,'2.1.278 (Claude Code)\n')
r['input_unchanged']=sha(a.binary.read_bytes())==r['input_sha256'];assert r['input_unchanged'];r['all_pass']=True;save()

once.unlink();twice.unlink()
