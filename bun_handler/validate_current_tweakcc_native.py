#!/usr/bin/env python3
"""Test actual current-main helpers, then recovery/repatch on disposable binaries.

Requires Node+TypeScript for fixture generation, not for the handler. No mocked
LIEF parser: the adapter explicitly tests pure helpers using independently read
ELF geometry. It never executes a damaged main-helper output. This is not a full
node-lief, interactive tweakcc, or authenticated Claude integration test.
"""
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import bun_handler as h
from validate_interop_native import payload_check

BASE=Path(__file__).resolve().parent

def sha(data):return hashlib.sha256(data).hexdigest()

def execute(data,path,expected=None,missing=False):
    h._write_new_executable(str(path),data)
    run=subprocess.run([str(path),'--version'],capture_output=True,text=True,timeout=90)
    if missing:
        assert run.returncode!=0 and 'Cannot find module' in run.stderr and 'recovery-missing.js' in run.stderr,run
    else:
        assert run.returncode==0 and expected in run.stdout,(run.returncode,run.stdout,run.stderr)
    checks={}
    for name,args in [('readelf',['-aW']),('objdump',['-h'])]:
        tool=subprocess.run([name,*args,str(path)],capture_output=True,timeout=60)
        assert tool.returncode==0 and not tool.stderr,(name,tool.stderr)
        checks[name]={'status':tool.returncode,'stderr_bytes':len(tool.stderr)}
    return {'bytes':len(data),'sha256':sha(data),'status':run.returncode,
            'stdout':run.stdout.strip(),'stderr':run.stderr[:1000].strip(),'elf_checks':checks}

def generate(input,output,mode):
    run=subprocess.run(['node',str(BASE/'reference/make_tweakcc_main_binary.cjs'),str(input),str(output),mode],
                        capture_output=True,text=True,timeout=120)
    assert run.returncode==0,(run.stdout,run.stderr)
    result=json.loads(run.stdout)
    # Exact current extractor accepted the actual section. No fixture-only
    # trimming/padding/repair was needed before invoking the current rebuild.
    assert result['sectionExtraction']['ok'] and not result['normalizedInputPad']
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path)
    parser.add_argument('work',type=Path)
    parser.add_argument('report',type=Path)
    args=parser.parse_args();args.work.mkdir(parents=True,exist_ok=True)
    original=args.binary.read_bytes();origin=h.BunImage(original)
    result={'input':str(args.binary),'input_sha256':sha(original),'input_bytes':len(original),
            'tweakcc_commit':'a806febde332beb471b07d53c403d8e196142da3',
            'tweakcc_source_sha256':sha((BASE/'reference/tweakcc-main-nativeInstallation.ts').read_bytes()),
            'scope':__doc__,'cases':[]}
    start=time.monotonic()
    for mode in ('literal','repeat','unicode','shrink','dependency','noop'):
        current=args.work/('main-'+mode)
        input=args.work/'main-literal' if mode=='repeat' else args.binary
        generation=generate(input,current,mode)
        damaged=current.read_bytes();view=h._TweakccRebuildImage(damaged)
        case={'mode':mode,'producer':generation,'ordinary_can_handle':h.can_handle(damaged),'policies':{}}
        for policy in ('reference','source_only'):
            out=h.recover_tweakcc(damaged,original if policy=='reference' else None,
                                  expected_sha256=sha(damaged))
            image=h.BunImage(out)
            assert h.can_handle(out) and h.repack_unchanged(out)==out
            assert h.repack_with_js(out,h.extract_js(out))==out
            assert h.recover_tweakcc(damaged,original if policy=='reference' else None)==out
            expected={'literal':'(ClauDe Code)','repeat':'(CLAUDE Code)',
                      'unicode':'(Claude 🦆)','shrink':'(CC)','noop':'(Claude Code)'}.get(mode)
            p=args.work/(mode+'-'+policy)
            record=execute(out,p,expected,missing=mode=='dependency')
            if policy=='reference':
                record['payload_audit']=payload_check(original,out)
                if mode=='noop':assert out==original
            else:
                assert image.flags == view.flags & 0xf
                assert not image.builtin_bytecode
                assert not image.bytecode_string_table[1] and not image.module_info_string_table[1]
                assert all(m['bytecode']==(0,0) and m['moduleInfo']==(0,0) for m in image.modules)
                assert h.extract_js(out)==h.extract_tweakcc_js(damaged)
                asset_count=0
                for m,old in zip(image.modules,view.modules):
                    if m['loader'] not in h._JS_LOADERS or m['module_format'] not in (1,2):
                        assert image.read_field(m,'contents')==view.read_field(old,'contents')
                        asset_count+=1
                record['payload_audit']={'all_compiled_caches_detached':True,'assets_preserved':asset_count}
            if mode in ('literal','repeat','unicode'):
                old={'literal':'(ClauDe Code)','repeat':'(CLAUDE Code)','unicode':'(Claude 🦆)'}[mode]
                token='(OURSOK 🦆)' if mode=='unicode' else '(OURSOK Code)'
                modified=h.extract_js(out).replace(old.encode(),token.encode())
                repatched=h.repack_with_js(out,modified)
                assert len(repatched)==len(out)
                record['repatch']=execute(repatched,args.work/(mode+'-'+policy+'-repatched'),token)
                record['repatch_growth']=len(repatched)-len(out)
                assert h.extract_js(repatched)==modified
                # CLI uses the same library policy and exclusive publication.
                if mode=='literal':
                    cliout=args.work/('cli-'+policy)
                    command=[sys.executable,str(BASE/'bun_handler.py'),'recover-tweakcc',str(current),str(cliout)]
                    if policy=='reference':command+=['--reference',str(args.binary)]
                    cli=subprocess.run(command,capture_output=True,text=True,timeout=120)
                    assert cli.returncode==0,(cli.stdout,cli.stderr)
                    assert cliout.read_bytes()==out
                    record['cli']={'status':cli.returncode,'stdout':cli.stdout.strip(),'matches_api':True}
                    cliout.unlink()
                del repatched
            case['policies'][policy]=record
            del out,image
            gc.collect()
        result['cases'].append(case)
        args.report.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({'binary':args.binary.name,'mode':mode,'sizes':{
            name:info['bytes'] for name,info in case['policies'].items()},'pass':True}),flush=True)
        del damaged,view
        gc.collect()
    assert sha(args.binary.read_bytes())==result['input_sha256']
    result.update(all_pass=True,seconds=round(time.monotonic()-start,3))
    args.report.write_text(json.dumps(result,indent=2)+'\n')

if __name__=='__main__':main()
