#!/usr/bin/env python3
"""First run the untouched PE, then every native gate, with an isolated Wine prefix.

No absent-runtime skip is accepted. Report directory must be new. Requires Wine
on Linux; Xvfb is used when no DISPLAY is available. Native Windows runs the EXE
directly. Tests are local and do not authenticate or make model requests.
"""
import argparse, hashlib, json, os
from pathlib import Path
import shutil, subprocess, sys, tempfile


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
    parser.add_argument('legacy_report_dir', nargs='?',type=Path, help='v5 compatibility: binary wine report_dir')
    parser.add_argument('--wine',type=Path)
    args=parser.parse_args()
    if args.legacy_report_dir is not None:
        if args.wine is not None:parser.error('do not combine positional Wine with --wine')
        args.wine=args.report_dir;args.report_dir=args.legacy_report_dir
    binary=args.binary.resolve()
    if not binary.is_file():parser.error('input binary is missing')
    if os.name!='nt' and (not args.wine or not args.wine.is_file()):parser.error('--wine is required')
    report=args.report_dir.resolve();report.mkdir(parents=True,exist_ok=False)
    sha=lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    before=sha(binary)
    env=dict(os.environ,PYTHONUNBUFFERED='1',PYTHONDONTWRITEBYTECODE='1',
        CLAUDE_NATIVE_BINARY=str(binary),CLAUDE_PFG_STRICT_GATES='gate1,gate2,gate3,gate5,gate6,gate7,gate8,gate9',CLAUDE_PFG_STRICT_GATES_WAIVE='')
    server=None;xvfb=None
    # Configure the server and runner explicitly from the same Wine installation.
    if os.name!='nt':
        wine=args.wine.resolve();server=wine.parent/'wineserver'
        if not server.is_file():parser.error('Wine installation lacks sibling wineserver')
        env.update(BUN_HANDLER_WINE=str(wine),WINESERVER=str(server),
            WINEPREFIX=str(report/'wine-prefix'),WINEARCH='win64',WINEDEBUG='-all',
            WINEDLLOVERRIDES='mscoree,mshtml=',PATH=str(wine.parent)+os.pathsep+env.get('PATH',''))
        if not env.get('DISPLAY'):
            executable=shutil.which('Xvfb')
            if not executable:parser.error('DISPLAY or Xvfb is required for isolated Wine initialization')
            # Xvfb chooses a free display and reports it over a pipe.
            rfd,wfd=os.pipe()
            xvfb_log=(report/'xvfb.log').open('wb')
            xvfb=subprocess.Popen([executable,'-displayfd',str(wfd),'-screen','0','1024x768x24'],pass_fds=(wfd,),stdout=xvfb_log,stderr=subprocess.STDOUT)
            os.close(wfd)
            with os.fdopen(rfd) as stream:display=stream.readline().strip()
            if not display.isdecimal():raise RuntimeError('Xvfb did not supply a display')
            env['DISPLAY']=':'+display
        command=[str(wine),str(binary),'--version']
    else:command=[str(binary),'--version']
    result={'input_sha256':before,'implementation_sha256':sha(Path(__file__).parent/'bun_handler.py')}
    try:
        original=run_captured(command,env=env,timeout=120)
        (report/'original.stdout').write_text(original.stdout);(report/'original.stderr').write_text(original.stderr)
        result.update(original_exit=original.returncode,original_stdout=original.stdout.strip())
        if original.returncode!=0:raise RuntimeError('untouched original failed; native gates not run')
        # Do not toggle server persistence after the original: the server may
        # already be shutting down. Each Wine command can start its own server,
        # and regular-file captures do not wait for inherited service pipes.
        with (report/'strict.log').open('w') as log:
            strict=subprocess.run([sys.executable,str(Path(__file__).parent/'test_bun_handler.py'),str(binary)],env=env,stdout=log,stderr=subprocess.STDOUT,timeout=420)
        text=(report/'strict.log').read_text()
        result.update(strict_exit=strict.returncode,input_unchanged=before==sha(binary))
        result['pass']=(strict.returncode==0 and 'SUITE PASS' in text and 'SKIP' not in text and result['input_unchanged'])
    except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
        result.update(error=str(exc),**{'pass':False})
    finally:
        if server:subprocess.run([str(server),'-k'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        if xvfb:xvfb.terminate();xvfb.wait(timeout=10);xvfb_log.close()
        (report/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)
    return 0 if result.get('pass') else 1

if __name__=='__main__':sys.exit(main())
