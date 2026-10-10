"""Orchestration and native-gate verdict contracts, using explicit test doubles.

No synthetic native binary is executed: subprocess.run/_run_binary are mocked at
that boundary. These tests prove result handling, not runtime compatibility.
"""
import contextlib,io,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import test_bun_handler as t

GATES=['gate1_noop','gate2_length_change','gate3_determinism','gate5_shrink',
       'gate6_multi_edit','gate7_equal_length_control_flow','gate8_equal_length_literal',
       'gate9_dependency_reanalysis']
ENV=('CLAUDE_NATIVE_BINARY','CLAUDE_PFG_STRICT_GATES','CLAUDE_PFG_STRICT_GATES_WAIVE',
     'BUN_HANDLER_WINE','WINE_BINARY')


class RunnerMutationCoverage(unittest.TestCase):
    def invoke_main(self,*,strict='',waived='',missing=False,outcomes=None,synthetic=True,guards=True):
        with tempfile.TemporaryDirectory() as td,contextlib.ExitStack() as stack:
            # A real file satisfies only main's IO. Its native use is blocked by
            # stubbing each gate before main can dispatch it.
            path=Path(td)/'fixture';path.write_bytes(b'non-executable fixture bytes')
            env={k:v for k,v in os.environ.items() if k not in ENV}
            env.update(CLAUDE_PFG_STRICT_GATES=strict,CLAUDE_PFG_STRICT_GATES_WAIVE=waived)
            stack.enter_context(patch.dict(os.environ,env,clear=True))
            stack.enter_context(patch.object(t,'find_binary',return_value=None if missing else str(path)))
            stack.enter_context(patch.object(t,'synthetic_tests',return_value=synthetic))
            stack.enter_context(patch.object(t,'gate4_format_guards',return_value=(guards,'controlled guard result')))
            stack.enter_context(patch.object(t.bun_handler,'can_handle',return_value=True))
            stack.enter_context(patch.object(t.bun_handler,'detect_format',return_value='test double'))
            for name in GATES:
                stack.enter_context(patch.object(t,name,return_value=(outcomes or {}).get(name,(True,'controlled pass'))))
            with contextlib.redirect_stdout(io.StringIO()):return t.main()

    def test_all_pass_control(self):self.assertEqual(self.invoke_main(strict='gate7'),0)
    def test_no_native_fails_required_gates(self):self.assertEqual(self.invoke_main(strict='gate7',missing=True),1)
    def test_skip_is_failure_when_required(self):
        self.assertEqual(self.invoke_main(strict='gate7',outcomes={'gate7_equal_length_control_flow':(None,'not available')}),1)
    def test_explicit_waiver_is_not_failure(self):
        self.assertEqual(self.invoke_main(strict='gate7',waived='gate7',outcomes={'gate7_equal_length_control_flow':(None,'waived')}),0)
    def test_real_gate_failure_is_not_lost(self):
        self.assertEqual(self.invoke_main(outcomes={'gate8_equal_length_literal':(False,'actual failure')}),1)
    def test_synthetic_and_format_failures_reach_exit(self):
        self.assertEqual(self.invoke_main(missing=True,synthetic=False),1)
        self.assertEqual(self.invoke_main(missing=True,guards=False),1)
    def test_invalid_strict_and_waiver_tokens_are_rejected(self):
        for kwargs in ({'strict':'gate77'},{'waived':'gate77'}):
            with self.subTest(kwargs=kwargs):self.assertEqual(self.invoke_main(missing=True,**kwargs),1)
    def test_explicit_missing_paths_do_not_become_discovery_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            missing=str(Path(td)/'absent')
            for args,env in [(['harness'],{'CLAUDE_NATIVE_BINARY':missing}),(['harness',missing],{}),(['harness',missing,missing],{})]:
                clean={k:v for k,v in os.environ.items() if k not in ENV};clean.update(env)
                with self.subTest(args=args,env=env),patch.object(sys,'argv',args),patch.dict(os.environ,clean,clear=True):
                    with self.assertRaises(ValueError):t.find_binary()
    def fixture(self):
        return t.build_bun_elf([t._module('/$bunfs/root/cli',b'console.log("(Claude Code)");x={}.BUILD_REF_NAME){return""}',module_format=1,bytecode=b"opaque-compiled-state",module_info=b"opaque-module-info")])
    def test_dependency_gate_rejects_wrong_failure_diagnostic(self):
        data=self.fixture()
        for stderr in ('SyntaxError: unrelated','Cannot find module OTHER-PATH'):
            result=subprocess.CompletedProcess(['mocked-native'],1,'',stderr)
            with self.subTest(stderr=stderr),patch.object(t,'_run_binary',return_value=(result,None)) as runtime:
                ok,_=t.gate9_dependency_reanalysis(data)
                self.assertIs(ok,False);runtime.assert_called_once()
    def test_dependency_gate_accepts_exact_controlled_diagnostic(self):
        result=subprocess.CompletedProcess(['mocked-native'],1,'',"Cannot find module /$bunfs/root/pfg-definitely-missing.js")
        with patch.object(t,'_run_binary',return_value=(result,None)) as runtime:
            ok,_=t.gate9_dependency_reanalysis(self.fixture());self.assertIs(ok,True);runtime.assert_called_once()
    def test_literal_gate_requires_observed_replacement(self):
        result=subprocess.CompletedProcess(['mocked-native'],0,'2.1.263 (Claude Code)','')
        with patch.object(t,'_run_binary',return_value=(result,None)) as runtime:
            ok,_=t.gate8_equal_length_literal(self.fixture());self.assertIs(ok,False);runtime.assert_called_once()
    def test_native_timeout_is_not_reported_as_success(self):
        with patch.object(t.subprocess,'run',side_effect=subprocess.TimeoutExpired('mocked',90)):
            result,reason=t._run_binary(b'non-executable test fixture','--version')
        self.assertIsNone(result);self.assertIn('timed out',reason)
    def test_file_capture_preserves_failure_code_and_unicode(self):
        with tempfile.TemporaryDirectory() as td:
            wine=Path(td)/'wine';wine.touch()
            def run(command,**kwargs):
                kwargs['stdout'].write('hello čaj\r\n'.encode());kwargs['stderr'].write(b'diagnostic\r\n')
                return subprocess.CompletedProcess(command,7)
            with patch.dict(os.environ,{'BUN_HANDLER_WINE':str(wine)}),patch.object(t.subprocess,'run',side_effect=run):
                result,reason=t._run_binary(b'MZ non-executable test fixture','--version')
            self.assertIsNone(reason);self.assertEqual(result.returncode,7)
            self.assertEqual(result.stdout,'hello čaj\n');self.assertEqual(result.stderr,'diagnostic\n')

if __name__=='__main__':unittest.main(verbosity=2)
