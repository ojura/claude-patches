import sys, random, json, hashlib, time
if len(sys.argv) != 2:
    raise SystemExit('usage: python property_probe.py /path/to/bun_handler_v3')
sys.path.insert(0, sys.argv[1])
import bun_handler as h
from test_bun_handler import _module, build_bun_elf
from test_bun_interop import append_sources
rng=random.Random(809263); start=time.monotonic(); cases=0; compactions=0

def source(enc=None):
 alphabet='ab12 café' if enc==1 else 'ab12 café🦆中e\u0301'
 s=''.join(rng.choice(alphabet) for _ in range(rng.randrange(70)))
 return '' if rng.randrange(6)==0 else 'console.log("'+s+'");'

for case in range(1200):
 ms=rng.choice([36,52]); hs=rng.choice([4,8]); n=rng.randrange(1,10); flags=rng.randrange(1<<11)&~16
 modules=[]
 for mi in range(n):
  enc=rng.randrange(3); text=source(enc); name='/$bunfs/root/claude' if mi==0 else f'/$bunfs/root/other{mi}.js'
  modules.append(_module(name,text.encode(['utf-8','latin-1','utf-16-le'][enc]),encoding=enc,module_format=rng.choice([1,2]),bytecode=rng.randbytes(rng.randrange(200)),module_info=rng.randbytes(rng.randrange(25)),sourcemap=rng.randbytes(rng.randrange(30)),origin=name,source_hash=rng.getrandbits(32)))
 data=build_bun_elf(modules,module_struct_size=ms,section_header_size=hs,flags=flags,trailing_pad=bool(rng.randrange(2)),builtin_bytecode=[(2,rng.randbytes(rng.randrange(80)))],bytecode_string_table=rng.randbytes(rng.randrange(70)),module_info_string_table=rng.randbytes(rng.randrange(60)),compile_argv=rng.choice([b'',b'--smol']),trailing_section={'flags':0,'size':rng.randrange(1,30),'vaddr':0,'addralign':rng.choice([1,8,16,128])})
 assert h.can_handle(data),(case,'fixture')
 if rng.randrange(2):
  replacements={mi:source().encode() for mi in rng.sample(range(n),rng.randrange(1,n+1))}
  orig=data; data=append_sources(data,replacements)
  compact=h.compact_with_reference(data,orig); compactions+=1
  assert len(compact)<=len(data),(case,'compact-size')
  assert h.extract_js(compact)==h.extract_js(data),(case,'compact-sources')
  assert h.compact_with_reference(compact,orig)==compact,(case,'compact-idempotence')
 for cycle in range(3):
  before=h.BunImage(data); replacements=[]; desired={}
  for m in before.source_modules():
   val=before.read_source(m) if rng.randrange(3)==0 else source().encode()
   desired[m['index']]=val
   replacements.append((m['index'],m['name_bytes'],val))
  result=h.repack_module_sources(data,replacements,expected_sha256=hashlib.sha256(data).hexdigest())
  assert h.can_handle(result),(case,cycle,'invalid result')
  after=h.BunImage(result)
  for old,new in zip(before.modules,after.modules):
   idx=old['index'];assert after.read_source(new)==desired[idx],(case,cycle,'source',idx)
   changed=before.read_source(old)!=desired[idx]
   for field in ['name','bytecodeOriginPath','sourcemap','bytecode','moduleInfo']:
    if changed and field in ['sourcemap','bytecode','moduleInfo']:
     assert new[field]==(0,0),(case,cycle,'cache',idx,field)
    else:
     assert before.read_field(old,field)==after.read_field(new,field),(case,cycle,'retained',idx,field)
   assert after.source_hash(new)==(0 if changed else before.source_hash(old)),(case,cycle,'hash',idx)
  assert h.repack_unchanged(result)==result,(case,cycle,'no-op')
  assert h.repack_with_js(result,h.extract_js(result))==result,(case,cycle,'extract/repack')
  assert h.repack_module_sources(data,replacements,expected_sha256=None)==result,(case,cycle,'determinism')
  data=result;cases+=1
print(json.dumps({'seed':809263,'fixtures':1200,'edit_cycles':cases,'compactions':compactions,'all_pass':True,'seconds':round(time.monotonic()-start,3)},indent=2))
