#!/usr/bin/env python3
"""Additional local PE runtime probes; no malformed producer image is executed.

Uses the uploaded native executable as its only runtime base. The UTF-16 input
is explicitly derived, not claimed to be a vendor-shipped build. Product files
and the uploaded executable are not overwritten. Run after the other suites to
limit peak memory on a 4-GiB sandbox.
"""
from __future__ import annotations
import argparse,gc,hashlib,json,os,struct,subprocess,sys,time
from pathlib import Path


def digest(data):return hashlib.sha256(data).hexdigest()


def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('handler',type=Path);p.add_argument('binary',type=Path)
 p.add_argument('wine',type=Path);p.add_argument('report',type=Path)
 args=p.parse_args();args.handler=args.handler.resolve();args.wine=args.wine.resolve()
 sys.path.insert(0,str(args.handler))
 import bun_handler as h
 from validate_pe_runtime import run_captured
 from validate_interop_native import payload_check
 args.report.mkdir(parents=True,exist_ok=False)
 server=args.wine.parent/'wineserver'
 env=dict(os.environ,WINEPREFIX=str(args.report/'wine-prefix'),WINEARCH='win64',
  WINEDEBUG='-all',WINEDLLOVERRIDES='mscoree,mshtml=',WINESERVER=str(server),
  PATH=str(args.wine.parent)+os.pathsep+os.environ.get('PATH',''),
  TYPESCRIPT_PATH=os.environ.get('TYPESCRIPT_PATH','/opt/nvm/versions/node/v22.16.0/lib/node_modules/typescript/lib/typescript.js'),
  PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1')
 original=args.binary.read_bytes();original_digest=digest(original)
 report={'binary_sha256':original_digest,'implementation_sha256':digest(Path(h.__file__).read_bytes()),
  'cases':[],'operations':[],'derived_utf16_fixture':True}
 def save():
  (args.report/'results.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
 def run(name,data,expected,*,arguments=('--version',),stderr_expected=None,missing=None):
  assert h.repack_unchanged(data)==data
  path=args.report/(name+'.exe');path.write_bytes(data)
  command=[str(args.wine),str(path.resolve()),*arguments];start=time.monotonic()
  result=run_captured(command,env=env,timeout=90)
  (args.report/(name+'.stdout')).write_text(result.stdout)
  (args.report/(name+'.stderr')).write_text(result.stderr)
  native=subprocess.run(['objdump','-h',str(path)],capture_output=True,text=True,timeout=30)
  (args.report/(name+'.objdump')).write_text(native.stdout+native.stderr)
  ok=(result.returncode==0 and expected in result.stdout) if missing is None else (result.returncode!=0 and missing in result.stdout+result.stderr and 'Cannot find module' in result.stdout+result.stderr)
  if stderr_expected:ok=ok and stderr_expected in result.stderr
  ok=ok and native.returncode==0 and not native.stderr
  item={'name':name,'arguments':list(arguments),'bytes':len(data),'sha256':digest(data),
   'exit':result.returncode,'stdout':result.stdout.strip() if len(result.stdout)<2000 else result.stdout[:250],
   'stdout_bytes':len(result.stdout.encode()),'stdout_sha256':digest(result.stdout.encode()),
   'stderr':result.stderr[:1800],'expected_result':bool(ok),
   'objdump_exit':native.returncode,'seconds':round(time.monotonic()-start,3)}
  report['cases'].append(item);save();print(json.dumps(item,ensure_ascii=False),flush=True)
  assert ok,name
  path.unlink();return result.stdout
 def produce(branch,input_path,mode):
  output=args.report/('producer_'+branch+'_'+mode+'.section')
  command=['node',str(args.handler/'reference/make_pe_blob.cjs'),branch,str(input_path),str(output),mode]
  result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=60)
  assert result.returncode==0,result.stderr
  wrapper=output.read_bytes();assert int.from_bytes(wrapper[:8],'little')==len(wrapper)-8
  generated=h._repack_blob(input_path.read_bytes(),memoryview(wrapper)[8:],8)
  report['operations'].append({'producer':json.loads(result.stdout),'command':command});save()
  output.unlink();return generated
 def cli(command,output,expected_bytes):
  result=subprocess.run([sys.executable,str(args.handler/'bun_handler.py'),*command],env=env,capture_output=True,text=True,timeout=120)
  report['operations'].append({'cli':command,'exit':result.returncode,'stdout':result.stdout,'stderr':result.stderr});save()
  assert result.returncode==0,result.stderr
  observed=output.read_bytes();assert observed==expected_bytes
  output.unlink();return observed
 start=time.monotonic()
 try:
  help_stdout=run('original_help',original,'Usage:',arguments=('--help',))
  # Preserve the real Windows container and all unchanged compiled graph data.
  # Append one properly terminated, two-byte aligned UTF-16 source, update only
  # its descriptor and hash, and clear its now-stale compiled metadata.
  image=h.BunImage(original)
  target=next(m for m in image.source_modules() if b'}.BUILD_REF_NAME){return""}' in image.read_source(m))
  index=target['index'];name=target['name_bytes']
  text=image.read_source(target).decode('utf-8')+'\nexport const __winUtf16 = await Promise.resolve("🦆 UTF16"); console.error("encoding-probe:", __winUtf16);\n'
  encoded=text.encode('utf-16-le');pad=(-(image._runtime_blob_vaddr()+image.offsets_off))%2
  block=b'\0'*pad+encoded+b'\0\0'
  blob=bytearray(image.blob[:image.offsets_off]);blob.extend(block);blob.extend(image.blob[image.offsets_off:])
  record=image.modules_off+index*image.module_struct_size
  struct.pack_into('<II',blob,record+8,image.offsets_off+pad,len(encoded))
  blob[record+16:record+48]=b'\0'*32;blob[record+48]=2
  struct.pack_into('<I',blob,image.source_hashes_off+index*4,0)
  struct.pack_into('<Q',blob,image.offsets_off+len(block),image.offsets_off+len(block))
  struct.pack_into('<I',blob,image.offsets_off+len(block)+28,image.flags&~h.BUN_FLAG_SOURCE_TEXT_CONTIGUOUS)
  derived=h._repack_blob(original,blob,image.section_header_size)
  del blob,block,image;gc.collect()
  assert h.repack_with_js(derived,h.extract_js(derived))==derived
  run('derived_utf16',derived,'(Claude Code)',stderr_expected='encoding-probe: 🦆 UTF16')
  modified=text.replace('🦆 UTF16','🐧 UTF8!').encode()
  edited=h.repack_module_sources(derived,[(index,name,modified)],expected_sha256=digest(derived))
  edited_image=h.BunImage(edited)
  assert edited_image.modules[index]['encoding']==0 and edited_image.read_source(edited_image.modules[index])==modified
  report['utf16_payload_audit']=payload_check(derived,edited);save();del edited_image
  run('utf16_to_utf8_targeted',edited,'(Claude Code)',stderr_expected='encoding-probe: 🐧 UTF8!')
  del derived,edited;gc.collect()
  # Explicit recovery through the public CLI must equal the API, then permit
  # another source edit. The malformed helper intermediates are never run.
  produced=produce('main',args.binary,'literal');assert not h.can_handle(produced)
  damaged=args.report/'main_damaged_not_executed.exe';damaged.write_bytes(produced)
  for policy,reference in (('reference',original),('source_only',None)):
   recovered=h.recover_tweakcc(produced,reference)
   out=args.report/('main_'+policy+'_cli.exe')
   command=['recover-tweakcc',str(damaged),str(out)]
   if reference is not None:command+=['--reference',str(args.binary)]
   recovered=cli(command,out,recovered)
   run('main_'+policy+'_cli_recovered',recovered,'(ClauDe Code)')
   source=h.extract_js(recovered);modified=source.replace(b'(ClauDe Code)',b'(OURWIN Code)');assert modified!=source
   repatched=h.repack_with_js(recovered,modified,expected_sha256=digest(recovered))
   assert len(repatched)==len(recovered) and h.extract_js(repatched)==modified
   audit=payload_check(recovered,repatched)
   report.setdefault('recovery_repatch',{})[policy]={'no_growth':True,'bytes':len(repatched),'payload_audit':audit};save()
   run('main_'+policy+'_repatch',repatched,'(OURWIN Code)')
   current_help=run('main_'+policy+'_repatch_help',repatched,'Usage:',arguments=('--help',))
   assert current_help==help_stdout,'help differs after recovery/repatch: '+policy
   ii=h.BunImage(repatched);module=ii.modules[index]
   missing='B:/~BUN/root/review-followup-missing.js'
   dependency=b'import "'+missing.encode()+b'";\n'+ii.read_source(module)
   bad_import=h.repack_module_sources(repatched,[(index,module['name_bytes'],dependency)],expected_sha256=digest(repatched))
   del ii
   run('main_'+policy+'_followup_dependency',bad_import,'',missing=missing)
   del recovered,repatched,bad_import,source,modified;gc.collect()
  damaged.unlink();del produced;gc.collect()
  # Two real PR-helper appends, followed by our equal-size edit and compaction.
  once=produce('pr',args.binary,'literal');prior=args.report/'pr_once.exe';prior.write_bytes(once)
  twice=produce('pr',prior,'repeat');prior.unlink()
  sources=h.extract_js(twice);changed=sources.replace(b'(CLAUDE Code)',b'(OURNEW Code)');assert changed!=sources
  edited=h.repack_with_js(twice,changed,expected_sha256=digest(twice));assert len(edited)==len(twice)
  report['pr_repatch_payload_audit']=payload_check(twice,edited)
  sizes={'original':len(original),'once':len(once),'twice':len(twice),'repatch':len(edited)}
  del once,twice,sources,changed;gc.collect()
  run('pr_two_appends_repatch',edited,'(OURNEW Code)')
  compacted=h.compact_with_reference(edited,original,expected_sha256=digest(edited),reference_sha256=original_digest)
  assert h.extract_js(compacted)==h.extract_js(edited)
  assert h.compact_with_reference(compacted,original)==compacted
  intermediate=args.report/'pr_repatched.exe';intermediate.write_bytes(edited)
  published=args.report/'compacted_cli.exe'
  compacted=cli(['compact',str(intermediate),str(args.binary),str(published)],published,compacted)
  intermediate.unlink()
  sizes.update(compacted=len(compacted),recovered=len(edited)-len(compacted));report['pr_sizes']=sizes;save()
  run('pr_repatch_compacted_cli',compacted,'(OURNEW Code)')
  run('pr_repatch_compacted_help',compacted,'Usage:',arguments=('--help',))
  assert report['cases'][-1]['stdout_sha256']==digest(help_stdout.encode())
  report['input_unchanged']=digest(args.binary.read_bytes())==original_digest
  assert report['input_unchanged'];report['pass']=True
 finally:
  report['seconds']=round(time.monotonic()-start,3);save()
  subprocess.run([str(server),'-k'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
 return 0

if __name__=='__main__':sys.exit(main())
