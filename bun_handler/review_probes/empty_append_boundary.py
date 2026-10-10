import sys,json
sys.path.insert(0,sys.argv[1]);import bun_handler as h
from test_bun_interop import append_sources,fixture
original=fixture();i=h.BunImage(original)
empty=append_sources(original,{0:b''});regrown=h.repack_module_sources(empty,[(0,i.modules[0]['name_bytes'],b'console.log("regrown");')],expected_sha256=None)
r={'handler':h.__file__,'can_handle':h.can_handle(regrown),'source':h.BunImage(regrown).read_source(h.BunImage(regrown).modules[0]).decode()}
try:r['compacted_bytes']=len(h.compact_with_reference(regrown,original))
except Exception as e:r['compaction_error']=str(e)
print(json.dumps(r))
