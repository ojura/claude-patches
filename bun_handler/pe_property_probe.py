#!/usr/bin/env python3
"""Independent PE container builder/oracle and multi-module semantic property checks.
Synthetic fixtures are never executed. Uses packaged _build_bun_blob only for
well-formed graph creation, not for outer PE construction or size-field oracles.
"""
import argparse, hashlib, json, random, struct, sys, time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('handler',type=Path);p.add_argument('--fixtures',type=int,default=1200)
opts=p.parse_args();sys.path.insert(0,str(opts.handler.resolve()))
import bun_handler as h
from test_bun_handler import _module, _build_bun_blob
R=random.Random(52632026); started=time.monotonic()
def align(n,a):return (n+a-1)//a*a

def make_pe(mods,ms,width,flags,fa,sa,base,signed):
    headers=fa;textoff=fa;bunoff=2*fa;rva=2*sa
    argv=R.choice([b'',b'--smol',b'--title=review'])
    blob=_build_bun_blob(mods,ms,base+rva+width,flags=flags,
        builtin_bytecode=[(1,b'BUILTIN-A'*R.randrange(1,15)),(2,b'BUILTIN-B')],
        bytecode_string_table=b'BC-STRING-TABLE',module_info_string_table=b'MI-STRING-TABLE',
        startup_module_count=1,compile_argv=argv,trailing_pad=bool(R.randrange(2)))
    logical=width+len(blob);raw=align(logical,fa)
    data=bytearray(bunoff+raw);data[:2]=b'MZ';struct.pack_into('<I',data,60,128)
    data[128:132]=b'PE\0\0';struct.pack_into('<HHIIIHH',data,132,0x8664,2,0,0,0,240,0x22)
    o=152;struct.pack_into('<H',data,o,0x20b);struct.pack_into('<II',data,o+4,fa,raw)
    struct.pack_into('<I',data,o+16,sa);struct.pack_into('<Q',data,o+24,base)
    struct.pack_into('<II',data,o+32,sa,fa);struct.pack_into('<II',data,o+56,align(rva+raw,sa),headers)
    struct.pack_into('<I',data,o+64,0x12345678);struct.pack_into('<H',data,o+68,3);struct.pack_into('<I',data,o+108,16)
    for idx,name,vs,va,rs,fo,sc in [(0,b'.text',1,sa,fa,textoff,0x60000020),(1,b'.bun',logical,rva,raw,bunoff,0x40000040)]:
        at=o+240+idx*40;data[at:at+8]=name.ljust(8,b'\0');struct.pack_into('<IIIIIIHHI',data,at+8,vs,va,rs,fo,0,0,0,0,sc)
    data[textoff]=0xc3;struct.pack_into('<Q' if width==8 else '<I',data,bunoff,len(blob));data[bunoff+width:bunoff+logical]=blob
    if signed:
        cert=struct.pack('<IHH',19,0x200,2)+b'CERTIFICATE'+b'\0'*5
        struct.pack_into('<II',data,o+144,len(data),len(cert));data+=cert
    return bytes(data)

def source(enc):
    chars='abc 123 éñ' if enc==1 else 'abc 123 éñ中🦆'
    return ('export const v="'+''.join(R.choice(chars) for _ in range(R.randrange(60)))+'";') if R.randrange(6) else ''

def append(data,replacements):
    img=h.BunImage(data); block=b''.join(v+b'\0' for _,v in sorted(replacements.items()))
    out=bytearray(img.blob[:img.offsets_off])+block+img.blob[img.offsets_off:]; cursor=img.offsets_off
    for idx,val in sorted(replacements.items()):
        rc=img.modules_off+idx*img.module_struct_size;struct.pack_into('<II',out,rc+8,cursor,len(val))
        for k in range(2,6 if img.module_struct_size==52 else 4):struct.pack_into('<II',out,rc+8*k,0,0)
        out[rc+img.module_struct_size-4]=0
        if img.source_hashes_off is not None:struct.pack_into('<I',out,img.source_hashes_off+4*idx,0)
        cursor+=len(val)+1
    new=img.offsets_off+len(block);struct.pack_into('<Q',out,new,new);struct.pack_into('<I',out,new+28,img.flags&~16)
    return h._repack_blob_pe(data,out,img.section_header_size)

def outer_oracle(data,width,changed):
    o=152;at=432;u32=lambda at:struct.unpack_from('<I',data,at)[0]
    vs,va,rs,fo=struct.unpack_from('<IIII',data,at+8);sa,fa=struct.unpack_from('<II',data,o+32)
    logical=width+struct.unpack_from('<Q' if width==8 else '<I',data,fo)[0]
    assert vs==logical and rs==align(logical,fa)
    assert u32(o+56)==align(va+max(vs,rs),sa)
    assert u32(o+8)==rs
    assert not any(data[fo+logical:fo+rs])
    if changed:
        assert len(data)==fo+rs and struct.unpack_from('<II',data,o+144)==(0,0) and u32(o+64)==0
    return fo

cycles=compactions=0;matrix={}
for case in range(opts.fixtures):
    ms=R.choice([36,52]);width=R.choice([4,8]);fa=R.choice([512,1024,4096,65536]);sa=R.choice([x for x in [4096,8192,65536] if x>=fa]);base=R.choice([0x140000000,0x400000,0x7ff00000000]);signed=bool(R.randrange(2));flags=R.randrange(2048)&~16
    mods=[]; n=R.randrange(1,13)
    for idx in range(n):
        enc=R.randrange(3); src=source(enc);name='B:/~BUN/root/cli' if idx==0 else f'B:/~BUN/root/module{idx}.js'
        mods.append(_module(name,src.encode(['utf8','latin1','utf-16-le'][enc]),encoding=enc,module_format=R.choice([1,2]),bytecode=R.randbytes(R.randrange(180)),module_info=R.randbytes(R.randrange(50)),sourcemap=R.randbytes(R.randrange(60)),origin=name,source_hash=R.getrandbits(32)))
    mods.append(_module('B:/~BUN/root/asset.bin',R.randbytes(R.randrange(80)),loader=5,encoding=0,module_format=0))
    mods.append(_module('B:/~BUN/root/utf16.asset','čaj中'.encode('utf-16-le'),loader=5,encoding=2,module_format=0))
    data=make_pe(mods,ms,width,flags,fa,sa,base,signed); original=data
    assert h.can_handle(data) and h.repack_unchanged(data)==data
    fo=outer_oracle(data,width,False)
    if R.randrange(2):
        data=append(data,{idx:source(0).encode() for idx in R.sample(range(n),R.randrange(1,n+1))})
        compact=h.compact_with_reference(data,original)
        assert len(compact)<=len(data) and h.extract_js(compact)==h.extract_js(data)
        assert h.compact_with_reference(compact,original)==compact
        compactions+=1
    for cycle in range(4):
        old=h.BunImage(data);desired={}; repl=[]
        for m in old.source_modules():
            val=old.read_source(m) if R.randrange(3)==0 else source(0).encode();desired[m['index']]=val;repl.append((m['index'],m['name_bytes'],val))
        result=h.repack_module_sources(data,repl,expected_sha256=hashlib.sha256(data).hexdigest());new=h.BunImage(result)
        assert new.section_header_size==width and h.can_handle(result)
        changed=False
        for bm,am in zip(old.modules,new.modules):
            src=bm['index'] in desired;change=src and old.read_source(bm)!=desired[bm['index']];changed|=change
            assert bm['name_bytes']==am['name_bytes'] and (bm['loader'],bm['module_format'],bm['side'])==(am['loader'],am['module_format'],am['side'])
            if src:assert new.read_source(am)==desired[bm['index']]
            else:assert old.read_field(bm,'contents')==new.read_field(am,'contents') and bm['encoding']==am['encoding']
            for fld in ['bytecode','moduleInfo','sourcemap','bytecodeOriginPath']:
                if change and fld!='bytecodeOriginPath':assert am[fld]==(0,0)
                else:assert old.read_field(bm,fld)==new.read_field(am,fld)
            assert new.source_hash(am)==(0 if change else old.source_hash(bm))
        for b,a in zip(old.builtin_bytecode,new.builtin_bytecode):
            x,l=b['bytes'];y,k=a['bytes'];assert b['id']==a['id'] and bytes(old.blob[x:x+l])==bytes(new.blob[y:y+k])
        for fld in ['bytecode_string_table','module_info_string_table']:
            x,l=getattr(old,fld);y,k=getattr(new,fld);assert bytes(old.blob[x:x+l])==bytes(new.blob[y:y+k])
        assert bytes(old.blob[old.compile_argv_off:old.compile_argv_off+old.compile_argv_len])==bytes(new.blob[new.compile_argv_off:new.compile_argv_off+new.compile_argv_len])
        assert h.repack_with_js(result,h.extract_js(result))==result and h.repack_unchanged(result)==result
        assert result==h.repack_module_sources(data,repl,expected_sha256=None)
        outer_oracle(result,width,changed)
        # No original native bytes may change outside the six documented header fields.
        b=bytearray(data[:fo]);c=bytearray(result[:fo])
        for start,end in [(160,164),(208,212),(216,220),(296,304),(440,444),(448,452)]:b[start:end]=c[start:end]=bytes(end-start)
        assert b==c
        if not changed:assert result==data
        data=result;cycles+=1
    key=f'{width}-byte/{ms}-record';matrix[key]=matrix.get(key,0)+1
print(json.dumps({'seed':52632026,'fixtures':opts.fixtures,'edit_cycles':cycles,'compactions':compactions,'matrix':matrix,'all_pass':True,'seconds':round(time.monotonic()-started,3)},indent=2))
