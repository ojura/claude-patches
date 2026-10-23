"""
Tests for pfg.bundle: the helper process and its pipes, positions, needles, linking through imports and
exports, module-scope bindings, and modules read from synthetic Bun binaries.
"""
import os
import re
import sys
import threading

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)  # for `pfg`
sys.path.insert(0, os.path.join(REPO, "bun_handler"))
from pfg import bundle as pb  # noqa: E402
import test_bun_handler as tb  # noqa: E402  (its synthetic ELF builders)


def build(*texts, needles=()):
    """A bundle of modules /m0.js, /m1.js, ... with the given texts."""
    return pb.Bundle.from_sources([(index, f"/m{index}.js", text) for index, text in enumerate(texts)], needles)


# Positions


def test_offsets_convert_between_utf16_and_code_points_around_astral_characters():
    module = pb.Module(0, "/a.js", "a\U0001f600b\U0001f600")
    assert module.length == 6 and module.codepoints == 4
    assert [module.to_utf16(i) for i in range(5)] == [0, 1, 3, 4, 6]
    assert [module.to_codepoints(o) for o in (0, 1, 3, 4, 6)] == [0, 1, 2, 3, 4]
    with pytest.raises(pb.BackendError, match="surrogate pair"):
        module.to_codepoints(2)


def test_a_position_must_be_an_integer_inside_the_module():
    # bool must be rejected before arithmetic can turn it into an integer offset.
    module = pb.Module(0, "/a.js", "\U0001f600")
    for bad in (True, False, 1.0, -1, 2, None):
        with pytest.raises(pb.BackendError):
            module.to_utf16(bad)
    for bad in (True, -1, 3, "1"):
        with pytest.raises(pb.BackendError):
            module.check(bad)


def test_an_anchor_error_counts_candidates_given_as_a_generator():
    error = pb.AnchorError("a", ((0, s, "CallExpression") for s in (4, 9)))
    assert error.count == 2 and "module 0 at 4" in str(error)
    assert pb.AnchorError("b", (c for c in [])).count == 0


# Needles


def test_needles_from_a_generator_are_registered_and_sent():
    # Both the registry and the index must receive the needles from a one-pass iterable.
    with build('const x="flag";', needles=(n for n in [pb.Needle("flag", "string", "flag")])) as bundle:
        assert list(bundle.needles) == ["flag"]
        assert len(bundle.hits("flag")) == 1


def test_needles_with_one_key_twice_or_an_unknown_type_are_refused():
    with pytest.raises(ValueError, match="two needles"):
        build('const a="a",b="b";', needles=[pb.Needle("same", "string", "a"), pb.Needle("same", "string", "b")])
    with pytest.raises(ValueError, match="types"):
        build("", needles=[pb.Needle("k", "regex", "a")])


def test_needle_hits_come_from_every_module_in_order():
    with build('var a="flag";', 'var b=["flag","other","flag"];', 'var c=1;', 'f(`flag`);',
               needles=[pb.Needle("flag", "string", "flag")]) as bundle:
        assert [(index, hit["s"]) for index, hit in bundle.hits("flag")] == [(0, 6), (1, 7), (1, 22), (3, 2)]


def test_needles_hit_syntax_only():
    with build('var x=k("flag",{list:[]});// k("flag")\nvar s=\'"flag"\';o.list;',
               needles=[pb.Needle("s", "string", "flag"), pb.Needle("p", "property", "list"),
                        pb.Needle("m", "member", "list")]) as bundle:
        assert [hit["s"] for _, hit in bundle.hits("s")] == [8]
        assert len(bundle.hits("p")) == 1 and len(bundle.hits("m")) == 1
        with pytest.raises(pb.BackendError):
            bundle.hits("absent")


# Linking


def test_named_renamed_and_reexported_names_reach_the_declaring_module():
    with build('var inner=["x"];export{inner as z}',
               'import{z as local}from"/m0.js";export{local as again}',
               'export{z as viaFrom}from"/m0.js"',
               'export*from"/m2.js"') as bundle:
        assert bundle.export(0, "z") == ("binding", 0, "inner")
        assert bundle.export(1, "again") == ("binding", 0, "inner")
        assert bundle.export(2, "viaFrom") == ("binding", 0, "inner")
        assert bundle.export(3, "viaFrom") == ("binding", 0, "inner")
        assert bundle.imports(1)["local"][:2] == (0, "z")


def test_two_export_names_of_one_binding_through_two_paths_are_not_ambiguous():
    # Both names export the same binding, whose value is 1.
    with build('const x=1;export{x as one,x as two};',
               'export{one as result}from"/m0.js";',
               'export{two as result}from"/m0.js";',
               'export*from"/m1.js";export*from"/m2.js";') as bundle:
        assert bundle.export(3, "result") == ("binding", 0, "x")


def test_export_star_with_two_different_bindings_is_ambiguous_and_an_explicit_export_wins():
    with build('export var v=1;', 'export var v=2;', 'export*from"/m0.js";export*from"/m1.js";',
               'export*from"/m0.js";export*from"/m1.js";export var v=3;') as bundle:
        with pytest.raises(pb.Unresolved, match="ambiguous"):
            bundle.export(2, "v")
        assert bundle.export(3, "v") == ("binding", 3, "v")


def test_export_star_never_supplies_default():
    with build('export default 1;', 'export*from"/m0.js";') as bundle:
        with pytest.raises(pb.NotExported):
            bundle.export(1, "default")


def test_default_exports_of_every_form():
    # Anonymous default declarations export values without introducing a local name.
    with build('export default function(){return 1}', 'export default class{}', 'const x=1;export default x;',
               'export default function f(){}', 'var y=1;export{y as default}') as bundle:
        kind, module, start, end = bundle.export(0, "default")
        assert (kind, module, start) == ("declaration", 0, 0) and end == len('export default function(){return 1}')
        assert bundle.export(1, "default")[0] == "declaration"
        assert bundle.export(2, "default")[0] == "expression"
        assert bundle.export(3, "default") == ("binding", 3, "f")
        assert bundle.export(4, "default") == ("binding", 4, "y")


def test_namespaces_and_destructured_exports():
    with build('export var {a,b:[c]}={a:1,b:[2]};', 'export*as ns from"/m0.js";',
               'import*as all from"/m0.js";export{all}') as bundle:
        assert bundle.export(0, "a") == ("binding", 0, "a")
        assert bundle.export(0, "c") == ("binding", 0, "c")
        with pytest.raises(pb.NotExported):
            bundle.export(0, "b")
        assert bundle.export(1, "ns") == ("namespace", 0)
        assert bundle.export(2, "all") == ("namespace", 0)


def test_cycles_missing_names_and_outside_modules_are_refused():
    with build('import{q}from"/m1.js";export{q as p}', 'import{p}from"/m0.js";export{p as q}',
               'import{x}from"node:fs";export{x}', 'export*from"node:path"', 'var k=1;') as bundle:
        with pytest.raises(pb.Unresolved, match="cycle"):
            bundle.export(0, "p")
        with pytest.raises(pb.Unresolved, match="outside the bundle"):
            bundle.export(2, "x")
        with pytest.raises(pb.Unresolved, match="outside the bundle"):
            bundle.export(3, "anything")
        with pytest.raises(pb.NotExported):
            bundle.export(4, "k")


# Bindings


def test_the_module_scope_binding_of_a_name():
    text = 'import{y}from"/m1.js";if(1){var hoisted=1}function f(){var local=2;let y=3;return y}'
    with build(text, "export var y=1;") as bundle:
        hoisted = bundle.scope_binding(0, "hoisted")
        assert hoisted["kind"] == "var" and hoisted["declarations"][0]["module"] is True
        assert bundle.scope_binding(0, "local") == {"free": "local"}
        imported = bundle.scope_binding(0, "y")
        assert imported["kind"] == "import" and imported["declarations"][0]["import"] == {"spec": "/m1.js", "imported": "y"}
        occurrences = bundle.occurrences(0, imported["id"])
        assert len(occurrences) == 1  # the function's own y is another binding


def test_an_exported_declaration_keeps_its_occurrences_whatever_the_query_order():
    # A scope lookup and a reference lookup must select the same symbol for an exported declaration.
    for text, name in (("export let x=1;x=2;", "x"), ("export default function f(){return 1};f=()=>2;f();", "f")):
        start = re.search(rf"\b{name}\b", text).start()
        with build(text, "var q=1;") as bundle:
            first = bundle.binding_at(0, start, start + 1)
            scoped = bundle.scope_binding(0, name)
            assert scoped["id"] == first["id"]
            occurrences = bundle.occurrences(0, first["id"])
            assert any("write" in o["roles"] for o in occurrences)
            bundle.scope_binding(1, "q")  # the next query on module 0 parses it again
            assert bundle.occurrences(0, scoped["id"]) == occurrences
        with build(text, "var q=1;") as bundle:
            scoped = bundle.scope_binding(0, name)
            assert bundle.occurrences(0, scoped["id"]) == occurrences
            assert bundle.binding_at(0, start, start + 1)["id"] == scoped["id"]


def test_occurrence_queries_resolve_names_through_scopes():
    text = 'var y="outer";function f(){let y="inner";return y}var out=y'
    with build(text) as bundle:
        outer = bundle.scope_binding(0, "y")
        assert [o["s"] for o in bundle.occurrences(0, outer["id"])] == [4, len(text) - 1]
        assert bundle.binding_at(0, len(text) - 1, len(text))["id"] == outer["id"]


# The helper process


def _streams(bundle):
    process = bundle._helper._process
    return [process.stdin, process.stdout, bundle._helper._stderr]


def test_close_releases_both_pipes_and_the_stderr_file():
    # Closing the helper releases both pipes and its stderr file.
    for kill in (False, True):
        bundle = build("const x=1;")
        streams = _streams(bundle)
        bundle._helper.close(kill=kill)
        assert [stream.closed for stream in streams] == [True, True, True]
        assert bundle._helper._process.returncode is not None
        bundle._helper.close()  # closing twice is idempotent


def test_the_helper_adds_no_thread_and_a_failure_quotes_its_stderr():
    # A background stderr reader would hold its buffered stream's lock while blocked in read. That lock
    # would remain locked in a forked child, where closing the stream could hang.
    threads = threading.active_count()
    with build("const x=1;") as bundle:
        assert threading.active_count() == threads
        os.write(bundle._helper._stderr.fileno(), b"first\nTypeError: something broke\n")
        assert str(bundle._helper._failure("the helper failed")).endswith("stderr ends:\nfirst\nTypeError: something broke")
        os.write(bundle._helper._stderr.fileno(), b"x" * (pb._STDERR_TAIL * 2) + b"last part")
        tail = bundle._helper._stderr_tail()
        assert len(tail) == pb._STDERR_TAIL and tail.endswith("last part")


def test_close_cleans_up_when_its_wait_is_interrupted(monkeypatch):
    # Cancellation propagates after the owned process has ended and its streams have closed.
    bundle = build("const x=1;")
    process = bundle._helper._process
    real_wait = process.wait
    calls = []

    def interrupted(timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real_wait(timeout)

    monkeypatch.setattr(process, "wait", interrupted)
    with pytest.raises(KeyboardInterrupt):
        bundle.close()
    assert process.returncode is not None
    assert all(stream.closed for stream in _streams(bundle))


def test_close_after_the_helper_already_exited():
    bundle = build("const x=1;")
    bundle._helper._process.kill()
    bundle._helper._process.wait()
    bundle.close()
    assert all(stream.closed for stream in _streams(bundle))


def test_a_failed_request_ends_the_helper_when_the_bundle_exits_with_an_error():
    with pytest.raises(pb.BackendError):
        with build("const x=1;") as bundle:
            process = bundle._helper._process
            bundle.facts(0, [{"q": "nonsense"}])
    assert process.returncode is not None


def test_a_helper_of_another_revision_is_refused(monkeypatch):
    monkeypatch.setattr(pb, "REVISION", pb.REVISION + 1)
    with pytest.raises(pb.BackendError, match="restart"):
        build("const x=1;")


def test_a_helper_that_cannot_start_is_a_backend_error(monkeypatch):
    # Startup errors use BackendError and release the file opened for stderr.
    files = []
    real = pb.tempfile.TemporaryFile

    def recorded(*args, **kwargs):
        files.append(real(*args, **kwargs))
        return files[-1]

    def fail(*args, **kwargs):
        raise FileNotFoundError(2, "synthetic missing interpreter")

    monkeypatch.setattr(pb.tempfile, "TemporaryFile", recorded)
    monkeypatch.setattr(pb.subprocess, "Popen", fail)
    with pytest.raises(pb.BackendError, match="could not start"):
        pb._Helper()
    assert files and files[0].closed


def test_a_helper_that_closes_its_output_and_keeps_running_is_ended(monkeypatch, tmp_path):
    # A helper that closes stdout cannot answer further requests, even if it keeps running.
    fake = tmp_path / "closed-stdout.mjs"
    # The explicit synchronous write completes before stdout closes.
    fake.write_text("import fs from 'node:fs';\nfs.writeSync(2,'STDOUT_CLOSED\\n');\nfs.closeSync(1);\n"
                    "setInterval(()=>{},1000);\n")
    monkeypatch.setattr(pb, "HELPER", str(fake))
    helpers, outcome = [], {}

    class Recorded(pb._Helper):
        def _read(self):
            helpers.append(self)
            return super()._read()

    def construct():
        try:
            Recorded()
        except BaseException as exc:  # noqa: BLE001 - the outcome is examined below
            outcome["error"] = exc

    thread = threading.Thread(target=construct, daemon=True)
    thread.start()
    thread.join(20)
    try:
        assert not thread.is_alive(), "the client still waits for a helper that closed its output"
        assert isinstance(outcome.get("error"), pb.BackendError)
        assert "closed its output" in str(outcome["error"]) and "STDOUT_CLOSED" in str(outcome["error"]), outcome
        assert helpers[0]._process.returncode is not None
    finally:
        for helper in helpers:
            if helper._process.poll() is None:
                os.killpg(helper._process.pid, 9)


def test_a_mismatched_reply_id_ends_the_helper_and_later_requests_fail_at_once():
    bundle = build("const x=1;")
    try:
        # A reply to request 99 must not be accepted as the reply to request 1.
        with pytest.raises(pb.BackendError):
            bundle._helper._write({"id": 99, "op": "nonsense"})
            bundle._helper._next = 0
            bundle._helper.request("nonsense")
        assert bundle._helper._process.returncode is not None
        with pytest.raises(pb.BackendError, match="was closed"):
            bundle.scope_binding(0, "x")
    finally:
        bundle.close()


def test_a_non_json_output_line_is_a_protocol_failure(monkeypatch, tmp_path):
    fake = tmp_path / "not-json.mjs"
    fake.write_text("process.stdout.write('not a JSON value\\n');setInterval(()=>{},1000);\n")
    monkeypatch.setattr(pb, "HELPER", str(fake))
    helpers = []

    class Recorded(pb._Helper):
        def _read(self):
            helpers.append(self)
            return super()._read()

    with pytest.raises(pb.BackendError, match="not JSON"):
        Recorded()
    assert helpers[0]._process.returncode is not None
    assert all(stream.closed for stream in (helpers[0]._process.stdin, helpers[0]._process.stdout, helpers[0]._stderr))


def test_a_forked_child_lets_go_of_the_helper_without_closing_descriptors_it_reuses():
    # Each inherited stream must retain ownership of its descriptor number until it closes. Releasing
    # the number first could let a stale stream close an unrelated file that reused it.
    bundle = build("const x=1;")
    try:
        null = os.stat(os.devnull)
        read_end, write_end = os.pipe()
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                process = bundle._helper._process
                fds = [stream.fileno() for stream in (process.stdin, process.stdout)]
                # Each inherited pipe descriptor now refers to /dev/null, and like the pipe is not inherited
                # by a program the child execs.
                same = all((os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (null.st_dev, null.st_ino) for fd in fds)
                same = same and not any(os.get_inheritable(fd) for fd in fds)
                # The child closes its streams, opens files until one reuses the stdin number, then closes
                # the stale stream again: the new file must stay open.
                bundle.close()
                opened = []
                while fds[0] not in opened and len(opened) < 64:
                    opened.append(os.open(os.devnull, os.O_RDONLY))
                process.stdin.close()
                os.fstat(fds[0])
                code = 0 if same and fds[0] in opened else 2
            finally:
                os.write(write_end, bytes([code]))
                os._exit(0)
        os.close(write_end)
        _, status = os.waitpid(pid, 0)
        result = os.read(read_end, 1)
        os.close(read_end)
        assert os.waitstatus_to_exitcode(status) == 0 and result == b"\x00"
        # The child's close neither killed nor waited for the parent's helper, which still answers.
        assert bundle.scope_binding(0, "x")["kind"] == "const"
    finally:
        bundle.close()


# Binaries


def _esm(name, text, encoding=0):
    codec = {0: "utf-8", 1: "latin-1", 2: "utf-16-le"}[encoding]
    return tb._module(name, text.encode(codec), module_format=1, encoding=encoding)


def test_modules_of_a_binary_in_every_encoding_bun_stores(tmp_path):
    binary = tb.build_bun_elf([
        _esm("/$bunfs/root/claude", 'import{a}from"/$bunfs/root/chunk-a.js";import{b}from"/$bunfs/root/chunk-b.js";'
                                    'var flag=k("tengu_x",{list:[...a,...b]});'),
        _esm("/$bunfs/root/chunk-a.js", 'var a=["h\xe9 ☃ \U0001f600"];export{a};', encoding=2),
        _esm("/$bunfs/root/chunk-b.js", 'var b=["caf\xe9"];export{b};', encoding=1),
    ])
    path = tmp_path / "claude"
    path.write_bytes(binary)
    with open(path, "rb") as file:
        file.read(10)  # from_binary reads from the start whatever the position
        with pb.Bundle.from_binary(file, [pb.Needle("flag", "string", "tengu_x")]) as bundle:
            assert [bundle.modules[i].name for i in sorted(bundle.modules)] == [
                "/$bunfs/root/claude", "/$bunfs/root/chunk-a.js", "/$bunfs/root/chunk-b.js"]
            assert len(bundle.hits("flag")) == 1
            imports = bundle.imports(0)
            assert bundle.export(imports["a"][0], "a") == ("binding", 1, "a")
            assert bundle.export(imports["b"][0], "b") == ("binding", 2, "b")
            text = 'var a=["h\xe9 ☃ \U0001f600"];export{a};'
            assert bundle.modules[1].length == len(text) + 1
            assert bundle.text(1, 0, bundle.modules[1].length) == text


def test_a_binary_bun_handler_cannot_read_is_a_backend_error():
    # bun_handler raises BunFormatError for the first (too small for ELF64) and NotImplementedError for the
    # second (not ELF64).
    for data in (b"do not execute this", b"\x7fELF" + b"\0" * 60):
        with pytest.raises(pb.BackendError, match="bun_handler cannot read the binary"):
            pb.Bundle.from_binary(data)


# The built-in scan across modules


def test_a_built_in_value_exported_by_a_variable_is_followed_into_its_importers():
    with build('var S=Set;export{S as T};export const P=Promise;',
               'import{T}from"/m0.js";T.prototype.x=1;',
               'export{T as U}from"/m0.js";',
               'import{U as V}from"/m2.js";import{P}from"/m0.js";V.prototype.y=2;f(P);',
               'import*as ns from"/m0.js";ns.T.prototype.z=3;',
               'var later=import("/m2.js");',
               'Object.defineProperty(Map.prototype,"get",{});') as bundle:
        scans = bundle.builtin_scan()
        assert [w["target"] for w in scans[1]["writes"]] == ["Set.prototype.x"]
        assert [w["target"] for w in scans[3]["writes"]] == ["Set.prototype.y"]
        assert [u["value"] for u in scans[3]["unfollowed"]] == ["Promise"]
        assert [w["target"] for w in scans[6]["writes"]] == ["Map.prototype.get"]
        # A namespace of a module that exports a built-in value is where the scan stops following it.
        assert [u["reason"] for u in scans[4]["unfollowed"]] == [
            "a namespace of module 0, which exports built-in values: P (Promise), T (Set)"]
        assert scans[5]["unfollowed"][0]["reason"].startswith("a namespace of module 2")
        summary = bundle.builtin_summary(scans)
        assert [(w["module"], w["target"]) for w in summary["writes"]] == [
            (1, "Set.prototype.x"), (3, "Set.prototype.y"), (6, "Map.prototype.get")]
        # S and P in module 0, and the imports T, V and P that receive their values.
        assert len(summary["references"]) == 5
        assert all(entry["status"] == "followed" and entry["binding"] for entry in summary["references"])


def test_a_registry_key_loses_its_exemption_when_the_program_writes_symbol_for():
    with build('const k=Symbol.for("x");globalThis[k]=1;', 'Symbol.for=f;') as bundle:
        summary = bundle.builtin_summary(bundle.builtin_scan())
        assert [w["target"] for w in summary["writes"]] == ["globalThis.*", "Symbol.for"]
        assert summary["registryWrites"] == []
    with build('const k=Symbol.for("x");globalThis[k]=1;') as bundle:
        summary = bundle.builtin_summary(bundle.builtin_scan())
        assert summary["writes"] == []
        assert [w["target"] for w in summary["registryWrites"]] == ["globalThis.@registry"]


def test_a_needle_key_can_name_an_inherited_object_property():
    needles = [pb.Needle(key, "string", "flag") for key in ("constructor", "__proto__", "toString")]
    with build('const x="flag";', needles=needles) as bundle:
        for needle in needles:
            assert len(bundle.hits(needle.key)) == 1
            assert bundle.hits(needle.key)[0][1]["s"] == 8


def test_finite_global_keys_are_invalidated_by_writes_to_their_operation_dependencies():
    loop = 'for(let[n,r]of Object.entries({h:f,Fragment:g}))Object.defineProperty(globalThis,n,{value:r});'
    with build(loop) as bundle:
        summary = bundle.builtin_summary(bundle.builtin_scan())
        assert summary["writes"] == []
        assert [entry["key"] for entry in summary["globalWrites"]] == ["h", "Fragment"]
    for write in ('Object.entries=f;', 'Array.prototype[Symbol.iterator]=f;'):
        with build(loop, write, 'const k=Symbol.for("x");globalThis[k]=1;') as bundle:
            summary = bundle.builtin_summary(bundle.builtin_scan())
            assert summary["globalWrites"] == []
            assert summary["registryWrites"] == []
            invalid = [entry for entry in summary["writes"] if entry.get("invalidatedKeyProof")]
            assert {(entry["module"], entry["target"]) for entry in invalid if entry["module"] in (0, 2)} == {
                (0, "globalThis.*"), (2, "globalThis.*")}
            if write.startswith("Array"):
                assert any(entry["module"] == 1 and entry["target"] == "Array.prototype.*" for entry in invalid)


def test_a_value_found_in_a_later_pass_reaches_uses_classified_before_it_across_modules():
    with build('export var p=Set.prototype;',
               'import{p}from"/m0.js";var q=Object.getPrototypeOf(p);Object.defineProperty(q,"has",{value:f});') as bundle:
        summary = bundle.builtin_summary(bundle.builtin_scan())
        assert [(w["module"], w["target"]) for w in summary["writes"]] == [(1, "Object.prototype.has")]
        assert summary["prototypeWrites"] == []
        assert {(r["module"], r["name"], r["value"], r["status"]) for r in summary["references"]} == {
            (0, "p", "Set.prototype", "followed"), (1, "p", "Set.prototype", "followed"), (1, "q", "Object.prototype", "followed")}


def test_an_export_seeds_its_importers_with_every_value_and_with_its_other_sources():
    with build('export var t=Set;t=Map;export var u=flag?WeakMap:other();',
               'import{t,u}from"/m0.js";t.prototype.x=1;u.prototype.y=2;',
               'export{t as v}from"/m0.js";',
               'import{v}from"/m2.js";v.z=3;') as bundle:
        summary = bundle.builtin_summary(bundle.builtin_scan())
        assert {(w["module"], w["target"]) for w in summary["writes"]} == {
            (1, "Set.prototype.x"), (1, "Map.prototype.x"), (1, "WeakMap.prototype.y"), (3, "Set.z"), (3, "Map.z")}
        statuses = {(r["module"], r["name"], r["status"]) for r in summary["references"]}
        # u may also hold what other() returns, in the exporting module and in the importing one.
        assert {(0, "u", "unclassified"), (1, "u", "unclassified"), (1, "t", "followed"), (3, "v", "followed")} <= statuses


def test_a_module_with_parse_diagnostics_gives_no_hits_links_or_facts():
    broken = 'export const x="flag";Set.prototype.has=f;var p=Set.prototype;p.add=g;const ='
    with build(broken, 'import{x}from"/m0.js";x;', 'export{x}from"/m0.js";', 'const y="flag";',
               needles=[pb.Needle("flag", "string", "flag")]) as bundle:
        assert bundle.diagnostics(0) and not bundle.diagnostics(1)
        assert [index for index, _ in bundle.hits("flag")] == [3]
        for call in (lambda: bundle.export(0, "x"), lambda: bundle.export(2, "x"), lambda: bundle.loads(0),
                     lambda: bundle.imports(0), lambda: bundle.scope_binding(0, "x"), lambda: bundle.export_names(0)):
            with pytest.raises(pb.Unresolved, match="TypeScript cannot parse this module"):
                call()
        target, imported, _ = bundle.imports(1)["x"]
        with pytest.raises(pb.Unresolved) as refusal:
            bundle.export(target, imported)
        assert refusal.value.module == 0
        summary = bundle.builtin_summary(bundle.builtin_scan())
        # The index pass's write in the broken module stays; it gets no facts pass, so p is not followed.
        assert [(w["module"], w["target"]) for w in summary["writes"]] == [(0, "Set.prototype.has")]
        assert [(u["module"], u["count"]) for u in summary["unparsed"]] == [(0, 1)]
        assert all(r["module"] != 0 or "status" not in r for r in summary["references"])


# The keys a proof depends on, from its own write records, and programs that write each one in three
# ways: an assignment, a definer, and an assignment through a variable that holds the object.
PROOFS = ('for(let[n,r]of Object.entries({h:f}))globalThis[n]=r;const k=Symbol.for("x");globalThis[k]=1;'
          'globalThis[Symbol.iterator]=1;')
INTRINSIC_OBJECTS = {
    "%ArrayIteratorPrototype%": ["Object.getPrototypeOf([].values())", "[][Symbol.iterator]().__proto__",
                                 "Object.getPrototypeOf(Array.prototype.entries.call(a))"],
    "%SetIteratorPrototype%": ["Object.getPrototypeOf(new Set().values())"],
    "%MapIteratorPrototype%": ["Object.getPrototypeOf(new Map()[Symbol.iterator]())"],
    "%StringIteratorPrototype%": ['Object.getPrototypeOf(""[Symbol.iterator]())'],
}


def writing_programs(key):
    """Programs whose scan should record a write to exactly the slot key names."""
    root, *path = key.split(".")
    member = lambda name: f"[Symbol.{name[2:]}]" if name.startswith("@@") else f".{name}"  # noqa: E731
    literal = lambda name: f"Symbol.{name[2:]}" if name.startswith("@@") else f'"{name}"'  # noqa: E731
    if not path:
        return [f"{root}=f;", f"globalThis.{root}=f;", f'Object.defineProperty(globalThis,"{root}",{{value:f}});']
    *parents, last = path
    programs = []
    for base in INTRINSIC_OBJECTS.get(root, [root]):
        target = base + "".join(member(name) for name in parents)
        programs += [f"({target}){member(last)}=f;", f"Object.defineProperty({target},{literal(last)},{{value:f}});",
                     f"var o={target};o{member(last)}=f;"]
    return programs


def assert_writes_produced(keys):
    """Every program of writing_programs(key), each in a module of its own, gives a write to key."""
    programs = [(key, program) for key in keys for program in writing_programs(key)]
    with build(*(program for _, program in programs)) as bundle:
        scans = bundle.builtin_scan()
    for index, (key, program) in enumerate(programs):
        assert key in {entry["target"] for entry in scans[index]["writes"]}, program


def test_every_dependency_key_of_a_proof_is_a_write_the_scan_can_produce():
    with build(PROOFS) as bundle:
        summary = bundle.builtin_summary(bundle.builtin_scan())
    keys = sorted({key for category in ("writes", "globalWrites", "registryWrites") for entry in summary[category]
                   for key in entry.get("keyRequires", [])})
    assert keys == ["%ArrayIteratorPrototype%.next", "Array.prototype.@@iterator", "Object.entries", "Symbol.for", "Symbol.iterator"]
    assert_writes_produced(keys + ["%SetIteratorPrototype%.next", "%MapIteratorPrototype%.next", "%StringIteratorPrototype%.next", "Set"])
    # And each such write invalidates the proofs that depend on its key.
    for key in keys:
        with build(PROOFS, writing_programs(key)[0]) as bundle:
            summary = bundle.builtin_summary(bundle.builtin_scan())
        invalidated = {k for entry in summary["writes"] if entry["module"] == 0 for k in entry.get("invalidatedKeyProof", [])}
        assert key in invalidated, key


def test_the_stated_limits_are_listed_in_the_summary_and_not_as_writes():
    with build('function z(e){let r=Object.getPrototypeOf(e);Object.defineProperty(r,"toString",{get(){}})}',
               'Reflect.apply(Object.defineProperty,null,[Set.prototype,"has",{value:f}]);'
               'var d=Object.defineProperty.bind(Object);d(Map.prototype,"get",{value:f});',
               'with(WeakMap.prototype){set=f}',
               'var a=Object,b;b=a.prototype;a=b.prototype;') as bundle:
        summary = bundle.builtin_summary(bundle.builtin_scan())
        assert summary["writes"] == []
        labelled = {(category, entry["module"], entry["limit"]) for category in ("prototypeWrites", "unfollowed")
                    for entry in summary[category] if entry.get("limit")}
        assert labelled == {("prototypeWrites", 0, "instances"), ("unfollowed", 1, "definer-calls"),
                            ("unfollowed", 2, "with"), ("unfollowed", 3, "bounds")}
        assert set(summary["limits"]) == set(pb.BUILTIN_SCAN_LIMITS)
        assert summary["limits"]["instances"]["entries"] == {"prototypeWrites": 1}
        assert summary["limits"]["definer-calls"]["entries"] == {"unfollowed": 2}
        assert summary["limits"]["instances"]["form"] == pb.BUILTIN_SCAN_LIMITS["instances"]


def test_possible_writes_to_a_slot_list_escapes_of_its_ancestors_and_what_reaches_every_slot():
    with build('f(Set.prototype);g(Map);h(Set.prototype.has);',
               'function z(e){Object.getPrototypeOf(e).has=1;Object.getPrototypeOf(e)[k]=2;Object.getPrototypeOf(e).get=3}',
               'k(Object.defineProperty);new Function("x");',
               'var t=x;t=Symbol.iterator;q(t);') as bundle:
        summary = bundle.builtin_summary(bundle.builtin_scan())

        def sites(slot):
            return sorted((category, entry["module"], entry.get("value") or entry.get("target") or entry.get("kind"))
                          for category, entry in pb.possible_writes(summary, slot))

        every_slot = [("dynamic", 2, "Function constructor"), ("unfollowed", 2, "Object.defineProperty")]
        assert sites("Set.prototype.has") == sorted([
            ("unfollowed", 0, "Set.prototype"), ("prototypeWrites", 1, "(object).__proto__.has"),
            ("prototypeWrites", 1, "(object).__proto__.*"), *every_slot])
        # Code that receives Set.prototype.has may write its members, not the slot that holds it.
        assert ("unfollowed", 0, "Set.prototype.has") not in sites("Set.prototype.has")
        assert sites("Map.prototype.get") == sorted([
            ("unfollowed", 0, "Map"), ("prototypeWrites", 1, "(object).__proto__.get"),
            ("prototypeWrites", 1, "(object).__proto__.*"), *every_slot])
        assert sites("Object.entries") == sorted([("prototypeWrites", 1, "(object).__proto__.*"), *every_slot])
    with build('const =') as bundle:
        summary = bundle.builtin_summary(bundle.builtin_scan())
        assert [category for category, _ in pb.possible_writes(summary, "Object.entries")] == ["unparsed"]


def test_builtin_write_dependencies_match_ancestors_and_unknown_members():
    assert pb.builtin_write_affects("Object", "Object.entries")
    assert pb.builtin_write_affects("Array.prototype.*", "Array.prototype.@@iterator")
    assert pb.builtin_write_affects("globalThis.*", "Symbol.for")
    assert not pb.builtin_write_affects("Error.stackTraceLimit", "Object.entries")
    assert not pb.builtin_write_affects("Array.prototype.push", "Array.prototype.@@iterator")


def test_npm_selected_node_is_used_without_reentering_a_launcher(monkeypatch):
    chosen = os.environ.get("npm_node_execpath")
    if not chosen:
        chosen = pb.subprocess.check_output([pb.shutil.which("node"), "-p", "process.execPath"], text=True).strip()
    monkeypatch.setenv("npm_node_execpath", chosen)

    def unexpected_lookup(name):
        raise AssertionError("npm already selected the Node executable")

    monkeypatch.setattr(pb.shutil, "which", unexpected_lookup)
    with build("const x=1;") as bundle:
        assert bundle._helper._process.args[0] == chosen


def test_an_invalid_npm_node_selection_does_not_fall_back_to_path(monkeypatch, tmp_path):
    monkeypatch.setenv("npm_node_execpath", str(tmp_path / "missing-node"))
    with pytest.raises(pb.BackendError, match="could not start"):
        build("const x=1;")
