#!/usr/bin/env python3
"""Strict Wine/Windows runtime tests, originals never overwritten.

Requires an explicit existing Wine executable on non-Windows systems; no skips.
A supplied prefix should be isolated. Source-only recovery tests intentionally
lose compilation caches. The malformed current-tweakcc intermediate is NOT run.
Node/TypeScript are optional fixture-generation dependencies, never runtime ones.
"""
import argparse, hashlib, json, os
from pathlib import Path
import subprocess, sys, time, tempfile
import bun_handler as h


def sha(b):return hashlib.sha256(b).hexdigest()

def run_captured(command, **kwargs):
    """Wait for the CLI, not EOF from Wine descendants holding capture pipes."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run(command, stdout=stdout, stderr=stderr, **kwargs)
        stdout.seek(0); stderr.seek(0)
        result.stdout = stdout.read().decode("utf-8", "replace").replace("\r\n", "\n")
        result.stderr = stderr.read().decode("utf-8", "replace").replace("\r\n", "\n")
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path);parser.add_argument('report_dir',type=Path)
    parser.add_argument('--wine',type=Path);parser.add_argument('--producer-tests',action='store_true')
    args=parser.parse_args()
    if not args.binary.is_file():parser.error('binary missing')
    if os.name!='nt' and (args.wine is None or not args.wine.is_file()):parser.error('--wine executable required')
    args.report_dir.mkdir(parents=True,exist_ok=False)
    data=args.binary.read_bytes(); original_sha=sha(data)
    report={'binary_sha256':original_sha,'implementation_sha256':sha(Path(h.__file__).read_bytes()),'cases':[]}
    image=h.BunImage(data);source=h.extract_js(data)
    def save():
        (args.report_dir/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    def check(name,out,expected=None,missing=None):
        started=time.monotonic();path=args.report_dir/(name+'.exe');path.write_bytes(out)
        command=([str(args.wine.resolve())] if os.name!='nt' else [])+[str(path.resolve()),'--version']
        result=run_captured(command,timeout=90)
        (args.report_dir/(name+'.stdout')).write_text(result.stdout)
        (args.report_dir/(name+'.stderr')).write_text(result.stderr)
        output=h.BunImage(out);unchanged_sources=0;assets=0
        for old,new in zip(image.modules,output.modules):
            assert (old['name_bytes'],old['loader'],old['module_format'],old['side'])==(new['name_bytes'],new['loader'],new['module_format'],new['side'])
            if old['loader'] not in h._JS_LOADERS or old['module_format'] not in (1,2):
                assert image.read_field(old,'contents')==output.read_field(new,'contents');assets+=1
            elif image.read_source(old)==output.read_source(new):unchanged_sources+=1
        ok=(result.returncode==0 and expected in result.stdout) if missing is None else (result.returncode!=0 and missing in result.stdout+result.stderr and 'Cannot find module' in result.stdout+result.stderr)
        item={'name':name,'bytes':len(out),'sha256':sha(out),'exit':result.returncode,'stdout':result.stdout.strip(),'stderr':result.stderr[:1800],'expected_result':ok,'seconds':round(time.monotonic()-started,3),'assets_preserved':assets,'unchanged_source_modules':unchanged_sources}
        report['cases'].append(item);save();print(json.dumps(item),flush=True)
        assert ok,name
        assert h.repack_unchanged(out)==out
        path.unlink() # keep logs, not 200+ MB copies
        return out
    check('original',data,'2.1.263 (Claude Code)')
    assert h.repack_with_js(data,source)==data
    literal=h.repack_with_js(data,source.replace(b'(Claude Code)',b'(ClauDe Code)'))
    check('literal',literal,'(ClauDe Code)')
    repatch=h.repack_with_js(literal,h.extract_js(literal).replace(b'(ClauDe Code)',b'(OURSOK Code)'))
    check('repatch',repatch,'(OURSOK Code)');assert len(repatch)==len(literal)
    # Audit each preserved opaque payload, hash, both shared tables and builtins.
    li=h.BunImage(literal);changed=set()
    for old,new in zip(image.modules,li.modules):
        is_source=old['loader'] in h._JS_LOADERS and old['module_format'] in (1,2)
        changed_source=is_source and image.read_source(old)!=li.read_source(new)
        if changed_source:
            changed.add(old['index']);assert li.source_hash(new)==0
            assert all(new[f]==(0,0) for f in ('bytecode','moduleInfo','sourcemap'))
        else:
            assert image.source_hash(old)==li.source_hash(new)
            for field in ('contents','bytecode','moduleInfo','sourcemap','bytecodeOriginPath'):
                assert image.read_field(old,field)==li.read_field(new,field)
    for a,b in zip(image.builtin_bytecode,li.builtin_bytecode):
        assert a['id']==b['id'];ao,an=a['bytes'];bo,bn=b['bytes']
        assert bytes(image.blob[ao:ao+an])==bytes(li.blob[bo:bo+bn])
    for field in ('bytecode_string_table','module_info_string_table'):
        ao,an=getattr(image,field);bo,bn=getattr(li,field)
        assert bytes(image.blob[ao:ao+an])==bytes(li.blob[bo:bo+bn])
    report['literal_payload_audit']={'changed_modules':sorted(changed),'builtins_preserved':len(image.builtin_bytecode),'shared_tables_preserved':2,'repatch_file_growth':len(repatch)-len(literal)};save()
    del repatch,literal,li
    shrunk=h.repack_with_js(data,source.replace(b'(Claude Code)',b'(CC)'))
    check('valid_shrink',shrunk,'(CC)');del shrunk
    unicode=h.repack_with_js(data,source.replace(b'(Claude Code)','(Claude 🦆 čaj)'.encode()))
    check('unicode',unicode,'(Claude 🦆 čaj)');del unicode
    ep=image.entrypoint_module();body=image.read_source(ep)
    grown=h.repack_module_sources(data,[(ep['index'],ep['name_bytes'],body.replace(b'(Claude Code)',b'(Claude Code) GROWN')+b'\n/*'+b'X'*1500+b'*/\n')],expected_sha256=original_sha)
    check('growth_1500',grown,'GROWN');assert h.Pe64(grown).bun_section['raw_size']>image.pe.bun_section['raw_size'];del grown
    targets=[m for m in image.source_modules() if b'}.BUILD_REF_NAME){return""}' in image.read_source(m)];assert len(targets)==1
    target=targets[0];body=image.read_source(target)
    exported=h.repack_module_sources(data,[(target['index'],target['name_bytes'],body+b'\nexport const runtimeProbe = await Promise.resolve(1); console.error("runtime-probe: \\u010daj");\n')],expected_sha256=original_sha)
    check('new_export_await',exported,'(Claude Code)');assert 'runtime-probe: čaj' in report['cases'][-1]['stderr'];del exported
    path=args.report_dir/'source-snapshot.exe';path.write_bytes(data)
    if args.producer_tests:
        node=os.environ.get('NODE','node');producer=Path(__file__).parent/'reference/make_pe_blob.cjs'
        for branch in ('main','pr'):
            prior=None
            for mode in ('literal','repeat','unicode','shrink','dependency','noop'):
                current_path=path
                if mode=='repeat':
                    assert prior is not None
                    current_path=args.report_dir/'producer-prior.exe';current_path.write_bytes(prior)
                output=args.report_dir/'producer-section.bin'
                run=subprocess.run([node,str(producer),branch,str(current_path),str(output),mode],capture_output=True,text=True,timeout=45)
                assert run.returncode==0,run.stderr
                wrapped=output.read_bytes();assert int.from_bytes(wrapped[:8],'little')==len(wrapped)-8
                produced=h._repack_blob(current_path.read_bytes(),wrapped[8:],8);output.unlink()
                if mode=='literal':prior=produced
                elif mode=='repeat':current_path.unlink()
                producer_record=json.loads(run.stdout)
                expected={'literal':'(ClauDe Code)','repeat':'(CLAUDE Code)','unicode':'(Claude 🦆)','shrink':'(CC)','dependency':None,'noop':'(Claude Code)'}[mode]
                missing='B:/~BUN/root/recovery-missing.js' if mode=='dependency' else None
                if branch=='main':
                    assert not h.can_handle(produced)
                    for policy,reference in (('reference',data),('source_only',None)):
                        recovered=h.recover_tweakcc(produced,reference)
                        check(branch+'_'+mode+'_'+policy,recovered,expected,missing)
                        report['cases'][-1]['producer']=producer_record;save();del recovered
                else:
                    check('pr_'+mode,produced,expected,missing)
                    compacted=h.compact_with_reference(produced,data)
                    check('pr_'+mode+'_compacted',compacted,expected,missing)
                    assert h.extract_js(compacted)==h.extract_js(produced)
                    assert h.compact_with_reference(compacted,data)==compacted
                    report['cases'][-1]['producer']=producer_record;save();del compacted
                del produced
            del prior
    path.unlink()
    report['input_unchanged']=sha(args.binary.read_bytes())==original_sha
    assert report['input_unchanged']
    report['pass']=True;save()
    return 0

if __name__=='__main__':sys.exit(main())
