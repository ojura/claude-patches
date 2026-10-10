#!/usr/bin/env python3
"""Independent section-byte locality/size audit with optional objdump; no EXE run."""
import hashlib,json,subprocess,shutil,sys,tempfile
from pathlib import Path
import bun_handler as h

def main():
    original=Path(sys.argv[1]).read_bytes();report_path=Path(sys.argv[2])
    before=h.Pe64(original);source=h.extract_js(original)
    edited=h.repack_with_js(original,source.replace(b'(Claude Code)',b'(ClauDe Code)'))
    after=h.Pe64(edited);bs=before.bun_section
    differences=[]
    for at in range(0,len(edited),1<<20):
        a=original[at:at+(1<<20)];b=edited[at:at+(1<<20)]
        if a!=b:
            differences.extend(at+i for i,(x,y) in enumerate(zip(a,b)) if x!=y)
    allowed=[(before.checksum_off,before.checksum_off+4),(before.security_off,before.security_off+8)]
    outside=[o for o in differences if not bs['foff']<=o<bs['foff']+bs['size']]
    assert all(any(a<=o<b for a,b in allowed) for o in outside)
    unchanged=[]
    for a,b in zip(before.sections,after.sections):
        if a['name']=='.bun':continue
        assert a==b
        assert original[a['foff']:a['foff']+a['raw_size']]==edited[b['foff']:b['foff']+b['raw_size']]
        unchanged.append(a['index'])
    result={'original_size':len(original),'edited_size':len(edited),'certificate_bytes_removed':before.certificate_size,'differing_bytes_before_removed_certificate':len(differences),'differences_outside_bun':outside,'outside_differences_only_checksum_security_directory':True,'unchanged_other_sections':unchanged,'edited_sha256':hashlib.sha256(edited).hexdigest()}
    if shutil.which('objdump'):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'patched.exe';path.write_bytes(edited)
            run=subprocess.run(['objdump','-h','-p',str(path)],capture_output=True,text=True,timeout=30)
            result['objdump_exit']=run.returncode;result['objdump_stderr']=run.stderr
            assert run.returncode==0 and not run.stderr
    report_path.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':main()
