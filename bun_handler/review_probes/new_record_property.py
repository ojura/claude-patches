#!/usr/bin/env python3
"""Additional new-record campaign. Synthetic executable bytes are never run.
Uses the bundled old-format graph builders and prior independent PE constructor.
New record placement/remapping and the append layout below are constructed here.
"""
import argparse, ast, hashlib, json, random, struct, sys, time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('handler',type=Path);p.add_argument('--fixtures',type=int,default=1200);a=p.parse_args()
sys.path.insert(0,str(a.handler.resolve()))
import bun_handler as h
from test_bun_handler import _module,build_bun_elf,_build_bun_blob
R=random.Random(530921);start=time.monotonic()
# Reuse only the previous review's native PE constructor, not its campaign.
ns={'h':h,'R':R,'struct':struct,'_build_bun_blob':_build_bun_blob}
tree=ast.parse((a.handler/'pe_property_probe.py').read_text())
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('align','make_pe')],type_ignores=[]),'<prior PE constructor>','exec'),ns)

def add_new_records(data,prelink,mapping,cache,options,insert_at):
    i=h.BunImage(data);pad=(-(i._runtime_blob_vaddr()+insert_at))%128
    block=(b'\0'*pad+cache) if cache else b''
    if block:block+=b'\0'*(-len(block)%128)
    shift=len(block);ptr=(insert_at+pad,len(cache)) if cache else (0,0)
    record=struct.pack('<III',*ptr,len(mapping))+b''.join(struct.pack('<I',v) for v in mapping) if prelink else b''
    if options is not None:record+=options
    out=bytearray(i.blob[:insert_at])+block+i.blob[insert_at:i.optional_tail_end]+record+i.blob[i.optional_tail_end:i.offsets_off]
    def remap(off,length):return off+shift if length and off>=insert_at else off
    for m in i.modules:
        at=i.modules_off+shift+m['index']*i.module_struct_size
        for field,k in [('name',0),('contents',8),('sourcemap',16),('bytecode',24),('moduleInfo',32),('bytecodeOriginPath',40)]:
            if k>=i.module_struct_size-4:continue
            off,n=m[field];struct.pack_into('<II',out,at+k,remap(off,n),n)
    for b in i.builtin_bytecode:
        off,n=b['bytes'];struct.pack_into('<II',out,b['pointer_record_off']+shift,remap(off,n),n)
    for name in ['bytecode_string_table','module_info_string_table']:
        loc=getattr(i,name+'_record_off')
        if loc is not None:
            off,n=getattr(i,name);struct.pack_into('<II',out,loc+shift,remap(off,n),n)
    alignment=h._required_container_shift_alignment(i.container,i.bun_section['foff']+i.bun_section['size'])
    out+=b'\0'*(-(shift+len(record))%alignment)
    flags=i.flags|(0x800 if prelink else 0)|(0x1000 if options is not None else 0)
    at=len(out);argv=i.compile_argv_off+shift+len(record) if i.compile_argv_off else 0
    out+=struct.pack('<QIIIIII',at,i.modules_off+shift,i.modules_len,i.entry_point_id,argv,i.compile_argv_len,flags)
    out+=i.blob[i.offsets_off+32:]
    return h._repack_blob(data,out,i.section_header_size)

def appended(data,sources):
    i=h.BunImage(data);block=b''.join(s+b'\0' for _,s in sorted(sources.items()))
    align=h._required_container_shift_alignment(i.container,i.bun_section['foff']+i.bun_section['size']);block+=b'\0'*(-len(block)%align)
    out=bytearray(i.blob[:i.offsets_off])+block+i.blob[i.offsets_off:];at=i.offsets_off
    for idx,src in sorted(sources.items()):
        loc=i.modules_off+idx*i.module_struct_size;struct.pack_into('<II',out,loc+8,at,len(src))
        for off in range(16,i.module_struct_size-4,8):struct.pack_into('<II',out,loc+off,0,0)
        out[loc+i.module_struct_size-4]=0
        if i.source_hashes_off is not None:struct.pack_into('<I',out,i.source_hashes_off+4*idx,0)
        at+=len(src)+1
    end=i.offsets_off+len(block);struct.pack_into('<Q',out,end,end);struct.pack_into('<I',out,end+28,i.flags&~16)
    return h._repack_blob(data,out,i.section_header_size)

def source(enc=0):
    if R.randrange(8)==0:return ''
    chars='ab c123 éñ' if enc==1 else 'ab c123 éñ中🦆'
    return 'export const v="'+''.join(R.choice(chars) for _ in range(R.randrange(60)))+'";'

counts={'fixtures':0,'cycles':0,'compactions':0,'recoveries':0,'noops':0,'matrix':{},'placement':{}}
for case in range(a.fixtures):
    kind=R.choice(['PE','ELF']);ms=R.choice([36,52]);width=R.choice([4,8]);n=R.randrange(1,9)
    prefix='B:/~BUN/root/' if kind=='PE' else '/$bunfs/root/'
    mods=[]
    for idx in range(n):
        enc=R.randrange(3);s=source(enc).encode(['utf-8','latin1','utf-16-le'][enc]);name=prefix+('cli' if idx==0 else f'm{idx}.js')
        mods.append(_module(name,s,encoding=enc,module_format=R.choice([1,2]),bytecode=R.randbytes(R.randrange(100)),module_info=R.randbytes(R.randrange(40)),sourcemap=R.randbytes(R.randrange(40)),origin=name,source_hash=R.getrandbits(32)))
    mods.append(_module(prefix+'asset.bin',R.randbytes(64),loader=5,module_format=0,encoding=0))
    flags=R.randrange(2048)&~16
    if kind=='PE':base=ns['make_pe'](mods,ms,width,flags,512,4096,0x140000000,False)
    else:base=build_bun_elf(mods,module_struct_size=ms,section_header_size=width,flags=flags,builtin_bytecode=[(1,b'B'*130)],bytecode_string_table=b'S'*90,module_info_string_table=b'M'*30,startup_module_count=1,compile_argv=R.choice([b'',b'--smol']))
    i=h.BunImage(base);prelink=bool(R.randrange(4));active=prelink and bool(R.randrange(4));mapping=tuple(R.sample(range(n),R.randrange(1,n+1))) if active else ()
    cache=b'REVIEW-OPAQUE-NOT-EXECUTED'+R.randbytes(R.randrange(1,1000)) if active else b''
    options=struct.pack('<II',R.getrandbits(32),R.getrandbits(32)) if R.randrange(4) else None
    placement=R.choice(['first','middle','last'])
    region_offsets=sorted(set(x[0] for x in i.regions if x[0]<i.modules_off))
    insert_at=region_offsets[0] if placement=='first' else i.modules_off if placement=='last' else R.choice(region_offsets)
    data=add_new_records(base,prelink,mapping,cache,options,insert_at);original=data;oi=h.BunImage(original)
    assert oi.prelinked_module_files==mapping and oi.runtime_options_bytes==(options or b'')
    assert h.repack_unchanged(data)==data and h.repack_with_js(data,h.extract_js(data))==data;counts['noops']+=2
    key=f'{kind}/{width}/{ms}/prelink={int(prelink)}/options={int(options is not None)}';counts['matrix'][key]=counts['matrix'].get(key,0)+1;counts['placement'][placement]=counts['placement'].get(placement,0)+1
    if R.randrange(2):
        data=appended(data,{0:source().encode() or b'export {};'})
        # An actual edit detaches the global cache from the producer's result.
        ai=h.BunImage(data);data=h.repack_module_sources(data,[(0,ai.modules[0]['name_bytes'],ai.read_source(ai.modules[0])+b'\n// appended repatch')],expected_sha256=None)
        try:
            compact=h.compact_with_reference(data,original)
        except Exception:
            e=Path(__file__).parent/'evidence';(e/'property_failure_original.bin').write_bytes(original);(e/'property_failure_current.bin').write_bytes(data)
            print(json.dumps({'failed_case':case,'key':key,'placement':placement,'old_modules_off':oi.modules_off,'new_modules_off':h.BunImage(data).modules_off,'original_source':oi.read_source(oi.modules[0]).decode(),'appended_source':ai.read_source(ai.modules[0]).decode(),'final_source':h.BunImage(data).read_source(h.BunImage(data).modules[0]).decode()}),flush=True)
            raise
        assert len(compact)<=len(data) and h.extract_js(compact)==h.extract_js(data)
        assert h.compact_with_reference(compact,original)==compact;counts['compactions']+=1
    for cycle in range(3):
        before=h.BunImage(data);desired={m['index']:(before.read_source(m) if R.randrange(4)==0 else source().encode()) for m in before.source_modules()}
        repl=[(m['index'],m['name_bytes'],desired[m['index']]) for m in before.source_modules()]
        effective=any(before.read_source(m)!=desired[m['index']] for m in before.source_modules())
        out=h.repack_module_sources(data,repl,expected_sha256=hashlib.sha256(data).hexdigest());after=h.BunImage(out)
        assert after.runtime_options_bytes==(options or b'')
        assert after.module_struct_size==ms and after.section_header_size==width
        assert after.flags&~16==oi.flags&~16
        if prelink and effective:
            absolute=after.bun_section['foff']+width+after.prelinked_module_graph_record_off
            assert struct.unpack_from('<III',out,absolute)==(0,0,0)
            if options is not None:assert out[absolute+12:absolute+20]==options
        elif not effective:assert out==data
        for old,new in zip(before.modules,after.modules):
            changed=old['index'] in desired and before.read_source(old)!=desired[old['index']]
            if old['index'] in desired:assert after.read_source(new)==desired[old['index']]
            else:assert before.read_field(old,'contents')==after.read_field(new,'contents')
            for f in ['name','bytecodeOriginPath','sourcemap','bytecode','moduleInfo']:
                if changed and f in ['sourcemap','bytecode','moduleInfo']:assert new[f]==(0,0)
                else:assert before.read_field(old,f)==after.read_field(new,f)
            assert after.source_hash(new)==(0 if changed else before.source_hash(old))
        for f in ['bytecode_string_table','module_info_string_table']:
            x,l=getattr(before,f);y,k=getattr(after,f);assert bytes(before.blob[x:x+l])==bytes(after.blob[y:y+k])
        for b,c in zip(before.builtin_bytecode,after.builtin_bytecode):
            x,l=b['bytes'];y,k=c['bytes'];assert b['id']==c['id'] and bytes(before.blob[x:x+l])==bytes(after.blob[y:y+k])
        assert h.repack_module_sources(data,repl,expected_sha256=None)==out
        assert h.repack_unchanged(out)==out and h.repack_with_js(out,h.extract_js(out))==out
        counts['cycles']+=1;counts['noops']+=2;data=out
    counts['fixtures']+=1
counts.update(all_pass=True,seed=530921,seconds=round(time.monotonic()-start,3))
print(json.dumps(counts,indent=2))
