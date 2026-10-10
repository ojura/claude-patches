#!/usr/bin/env python3
"""Runtime audit of supplied 2.1.278. Only valid derived outputs are executed.

Optional pinned-producer tests run actual upstream graph helpers, not full
node-lief integration. Malformed/relinked-but-stale intermediates are not run.
"""
import argparse, gc, hashlib, json, os, shutil, subprocess, tempfile, time
from pathlib import Path
import bun_handler as h


def digest(data): return hashlib.sha256(data).hexdigest()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path);parser.add_argument('report',type=Path)
    parser.add_argument('--producers',action='store_true');args=parser.parse_args()
    args.binary=args.binary.resolve()
    if not args.binary.is_file():parser.error('input binary is missing')
    args.report.mkdir(parents=True,exist_ok=False)
    # Produce upstream fixtures before retaining large Python binary snapshots.
    # Otherwise the Node serializer and live Python audit can compete for the
    # same bounded container memory. No malformed/stale fixture is executed.
    prepared = {}
    producer_records = []
    producer_dir = args.report / 'producer-work'
    if args.producers:
        producer_dir.mkdir()
        scripts = Path(__file__).resolve().parent / 'reference'
        plans = [('main', args.binary, 'literal', producer_dir/'main-not-run'),
                 ('pr', args.binary, 'literal', producer_dir/'pr-once-not-run'),
                 ('pr', producer_dir/'pr-once-not-run', 'repeat', producer_dir/'pr-twice-not-run')]
        for branch, input_path, mode, output_path in plans:
            script = scripts / ('make_tweakcc_main_binary.cjs' if branch == 'main' else 'make_tweakcc_binary.cjs')
            q = subprocess.run(['node', str(script), str(input_path), str(output_path), mode],
                               env=os.environ.copy(), capture_output=True, text=True, timeout=60)
            assert q.returncode == 0, (q.stdout, q.stderr)
            producer_records.append({'branch': branch, 'mode': mode, 'stdout': q.stdout,
                                     'adapter_sha256': digest(script.read_bytes())})
            prepared[(branch, mode)] = output_path
            print('producer prepared', branch, mode, flush=True)
    original=args.binary.read_bytes();original_digest=digest(original)
    image=h.BunImage(original);source=image.extract_js()
    results={'input_sha256':original_digest,'source_sha256':digest(Path(h.__file__).read_bytes()),
        'input_bytes':len(original),'extracted_bytes':len(source),'extracted_sha256':digest(source),
        'flags':hex(image.flags),'module_count':len(image.modules),'source_module_count':len(image.source_modules()),
        'prelinked_graph_bytes':image.prelinked_module_graph[1],'prelinked_file_count':len(image.prelinked_module_files),
        'runtime_options':image.runtime_options,'noop_identity':h.repack_unchanged(original)==original,'cases':[],'producers':producer_records}
    assert results['noop_identity']
    env={**os.environ,'HOME':str(args.report/'home'),'PYTHONDONTWRITEBYTECODE':'1'}
    (args.report/'home').mkdir()
    def save():
        (args.report/'results.json').write_text(json.dumps(results,indent=2,ensure_ascii=False)+'\n')
    def run(label,data,expected='',missing=None,argv=('--version',),stderr_expected=None):
        now=time.monotonic();i=h.BunImage(data)
        assert h.repack_unchanged(data)==data
        assert i.runtime_options_bytes==image.runtime_options_bytes
        with tempfile.TemporaryDirectory(prefix='bun278-native-') as td:
            path=Path(td)/'claude';path.write_bytes(data);path.chmod(0o700)
            r=subprocess.run([str(path),*argv],capture_output=True,text=True,timeout=45,env=env)
            ok=(r.returncode==0 and expected in r.stdout) if missing is None else (r.returncode!=0 and missing in r.stderr and 'Cannot find module' in r.stderr)
            if stderr_expected:ok=ok and stderr_expected in r.stderr
            checks=[]
            for tool,opts in [('readelf',['-aW']),('objdump',['-h'])]:
                q=subprocess.run([tool,*opts,str(path)],capture_output=True,text=True,timeout=20)
                assert q.returncode==0 and not q.stderr,(tool,q.stderr)
                checks.append({'tool':tool,'exit':q.returncode,'stderr':q.stderr})
        (args.report/(label+'.stdout')).write_text(r.stdout);(args.report/(label+'.stderr')).write_text(r.stderr)
        item={'label':label,'bytes':len(data),'sha256':digest(data),'exit':r.returncode,'expected_outcome':bool(ok),'stdout_sha256':digest(r.stdout.encode()),'stderr':r.stderr,'prelinked_graph':i.prelinked_module_graph,'prelinked_file_count':len(i.prelinked_module_files),'runtime_options':i.runtime_options,'tools':checks,'seconds':round(time.monotonic()-now,3)}
        results['cases'].append(item);save();print(label,r.returncode,len(data),r.stdout[:90].strip(),flush=True)
        assert ok,(label,r.stdout,r.stderr)
        return r.stdout
    def verify_payloads(data):
        i=h.BunImage(data);changed=[];assets=0
        for a,b in zip(image.modules,i.modules):
            assert a['name_bytes']==b['name_bytes']
            actual_change=(a['loader'] in h._JS_LOADERS and a['module_format'] in (1,2)
                           and image.read_source(a)!=i.read_source(b))
            if actual_change:
                changed.append(a['index']);assert i.source_hash(b)==0
                assert all(b[f]==(0,0) for f in ('bytecode','moduleInfo','sourcemap'))
            else:
                for f in ('contents','name','bytecode','moduleInfo','sourcemap','bytecodeOriginPath'):
                    assert image.read_field(a,f)==i.read_field(b,f),(a['index'],f)
                assert image.source_hash(a)==i.source_hash(b)
                if a['module_format']==0:assets+=1
        def field(img,p):o,n=p;return bytes(img.blob[o:o+n])
        assert len(image.builtin_bytecode)==len(i.builtin_bytecode)
        for a,b in zip(image.builtin_bytecode,i.builtin_bytecode):
            assert a['id']==b['id'] and field(image,a['bytes'])==field(i,b['bytes'])
        for f in ('bytecode_string_table','module_info_string_table'):
            assert field(image,getattr(image,f))==field(i,getattr(i,f))
        off,n=image.prelinked_module_graph
        assert bytes(i.blob[off:off+n])==field(image,image.prelinked_module_graph)
        return {'changed_modules':changed,'builtins_preserved':len(i.builtin_bytecode),'shared_tables_preserved':2,'assets_preserved':assets,'physical_prelinked_cache_preserved':True}
    run('original',original,'2.1.278 (Claude Code)')
    original_help=run('original_help',original,'Usage:',argv=('--help',))
    edited_source=source.replace(b'(Claude Code)',b'(ClauDe Code)')
    edited=h.repack_with_js(original,edited_source)
    assert len(edited)==len(original) and h.extract_js(edited)==edited_source
    assert edited==h.repack_with_js(original,edited_source)
    results['equal_size_payload_audit']=verify_payloads(edited)
    run('equal_size',edited,'(ClauDe Code)')
    repeated=h.repack_with_js(edited,h.extract_js(edited).replace(b'(ClauDe Code)',b'(OURSOK Code)'))
    assert len(repeated)==len(edited)
    run('repeated_equal_size',repeated,'(OURSOK Code)')
    assert run('repeated_help',repeated,'Usage:',argv=('--help',))==original_help
    del repeated,edited;gc.collect()
    cases=[('grow',source.replace(b'(Claude Code)',b'(Claude Code) GROW278'),'(Claude Code) GROW278'),
           ('shrink',source.replace(b'(Claude Code)',b'(CC)'),'(CC)'),
           ('unicode',source.replace(b'(Claude Code)','(Claude 🦆 čaj)'.encode()),'(Claude 🦆 čaj)')]
    for label,new,expected in cases:
        out=h.repack_with_js(original,new)
        assert h.extract_js(out)==new
        verify_payloads(out);run(label,out,expected)
        del out;gc.collect()
    del cases;gc.collect()
    target=next(m for m in image.source_modules() if b'}.BUILD_REF_NAME){return""}' in image.read_source(m))
    content=image.read_source(target)
    exported=content+b'\nexport const __newGraph278 = await Promise.resolve("new-export-278"); console.error(__newGraph278);\n'
    out=h.repack_module_sources(original,[(target['index'],target['name_bytes'],exported)],expected_sha256=original_digest)
    run('new_export_await',out,'(Claude Code)',stderr_expected='new-export-278');del out;gc.collect()
    missing='/$bunfs/root/new-dependency-278-missing.js'
    out=h.repack_module_sources(original,[(target['index'],target['name_bytes'],b'import"'+missing.encode()+b'";\n'+content)],expected_sha256=original_digest)
    run('new_dependency',out,missing=missing);del out;gc.collect()
    if args.producers:
        rebuilt = prepared[('main','literal')].read_bytes()
        assert not h.can_handle(rebuilt)
        recovered = h.recover_tweakcc(rebuilt, original)
        run('current_main_reference_recovery', recovered, '(ClauDe Code)')
        try:
            h.recover_tweakcc(rebuilt)
        except h.BunFormatError as e:
            assert 'discarded runtime options' in str(e)
            results['source_only_missing_options_refusal'] = str(e)
        else:
            raise AssertionError('lost runtime options silently defaulted')
        assert run('recovered_help', recovered, 'Usage:', argv=('--help',)) == original_help
        del rebuilt, recovered; gc.collect()
        second = prepared[('pr','repeat')].read_bytes()
        edited = h.repack_with_js(second, h.extract_js(second).replace(b'(CLAUDE Code)', b'(OURSOK Code)'))
        assert len(edited) == len(second)
        run('pr_append_then_our_edit', edited, '(OURSOK Code)')
        compact = h.compact_with_reference(edited, original)
        assert h.extract_js(compact) == h.extract_js(edited)
        assert h.compact_with_reference(compact, original) == compact
        run('compacted_append', compact, '(OURSOK Code)')
        results['compaction'] = {'before': len(edited), 'after': len(compact), 'reclaimed': len(edited)-len(compact)}
        del second, edited, compact; gc.collect()
        for path in prepared.values():
            path.unlink()
        producer_dir.rmdir()
    results['input_unchanged']=digest(args.binary.read_bytes())==original_digest
    assert results['input_unchanged']
    results['all_pass']=True;save()
    return 0

if __name__=='__main__':raise SystemExit(main())
