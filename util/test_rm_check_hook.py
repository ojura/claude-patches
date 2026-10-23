"""rm-check hook: the check runs with the request's directories.

A session that started in /tmp ran `cd /home/juraj && ... rm -rf
/tmp/cpa-deploy-7237e8a8` while its Bash working directory was
/tmp/cpa-deploy-7237e8a8. Claude Code asked before running it, with
"Dangerous rm operation on working directory or its ancestor:
/tmp/cpa-deploy-7237e8a8": /home/juraj is outside the session's working
directories, so the check does not follow the cd and checks the rm in
/tmp/cpa-deploy-7237e8a8. The check's message for that case says the command
"would remove a workspace directory".

The test repeats that with a new session directory S and its subdirectory
S/child, on the newest Claude Code version installed under
~/.local/share/claude/versions/, with the hook's own cache and runtime
directories. The hook runs with RMCHECK_CLAUDE_VERSION, so it takes the
session's original directory from CLAUDE_PROJECT_DIR. For the same command
`cd <home> && rm -rf S/child`:

  - session in S, working directory S/child: denied with that message;
  - session in S, working directory S: the rm removes a subdirectory, and the
    hook lets the command through;
  - session in <home>, working directory S/child: the check follows the cd to
    <home>, and the hook lets the command through.

Each verdict must come out the same whether the hook, and the server it
starts, run in the home directory or in S/child.
"""
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "rm-check", "rm-check-hook.mjs")
PREPARE = os.path.join(REPO, "hooks", "rm-check", "prepare.py")
VERSIONS = os.path.expanduser("~/.local/share/claude/versions")
HOME = os.path.expanduser("~")
WORKSPACE = "This command would remove a workspace directory"


def newest_version():
    try:
        names = os.listdir(VERSIONS)
    except FileNotFoundError:
        return None
    found = [n for n in names if re.fullmatch(r"\d+\.\d+\.\d+", n)]
    return max(found, key=lambda v: tuple(map(int, v.split("."))), default=None)


def find_bun():
    local = os.path.expanduser("~/.local/bin/bun")
    return shutil.which("bun") or (local if os.access(local, os.X_OK) else None)


def servers_under(directory):
    """PIDs of the rm-check servers with an argument under directory."""
    prefix = os.fsencode(directory + os.sep)
    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if (any(a.endswith(b"/rm-check-server.mjs") for a in argv)
                and any(a.startswith(prefix) for a in argv)):
            pids.append(int(pid))
    return pids


def stop_servers(directory):
    """Wait for the servers a test started to exit after their idle time, and
    stop those still running after 30 s."""
    deadline = time.time() + 30
    while servers_under(directory) and time.time() < deadline:
        time.sleep(0.2)
    for pid in servers_under(directory):
        os.kill(pid, signal.SIGTERM)


@pytest.fixture
def scratch():
    """Makes new directories for one test and removes them afterwards, once the
    servers started with an argument in them have exited."""
    made = []

    def make(prefix):
        d = tempfile.mkdtemp(prefix=prefix)
        made.append(d)
        return d

    yield make
    for d in made:
        stop_servers(d)
    for d in made:
        shutil.rmtree(d, ignore_errors=True)


def run_hook(bun, env, start, command, cwd):
    """Runs the hook in directory start for a request with working directory
    cwd; returns its deny reason, or None when it lets the command through. A
    hook that fails open fails the test."""
    p = subprocess.run(
        [bun, HOOK], cwd=start, env=env, text=True, capture_output=True, timeout=600,
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                          "cwd": cwd, "permission_mode": "bypassPermissions"}))
    assert p.returncode == 0, p.stderr
    if not p.stdout:
        return None
    out = json.loads(p.stdout)
    assert "systemMessage" not in out, out["systemMessage"]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def test_check_uses_the_request_directories(scratch):
    version = newest_version()
    if version is None:
        pytest.skip(f"no Claude Code version installed under {VERSIONS}")
    bun = find_bun()
    if bun is None:
        pytest.skip("bun is not installed")
    cache = scratch("rmcheck-test-cache.")
    session = scratch("rmcheck-test-session.")
    child = os.path.join(session, "child")
    os.mkdir(child)
    command = f"cd {HOME} && rm -rf {child}"
    cases = [(session, child, True), (session, session, False), (HOME, child, False)]
    for start in (HOME, child):
        # A runtime directory of its own gives each start directory its own server.
        runtime = scratch("rmcheck-test-run.")
        for project, cwd, denied in cases:
            env = dict(os.environ, XDG_CACHE_HOME=cache, XDG_RUNTIME_DIR=runtime,
                       RMCHECK_CLAUDE_VERSION=version, RMCHECK_IDLE_SECONDS="2",
                       CLAUDE_PROJECT_DIR=project)
            reason = run_hook(bun, env, start, command, cwd)
            where = (f"{version}, hook started in {start}, session in {project}, "
                     f"working directory {cwd}")
            if denied:
                assert reason is not None, f"{where}: {command!r} was let through"
                assert WORKSPACE in reason and f"'{child}'" in reason, f"{where}: {reason}"
            else:
                assert reason is None, f"{where}: {command!r} was denied: {reason}"


def load_prepare():
    spec = importlib.util.spec_from_file_location("rmcheck_prepare", PREPARE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prune_removes_other_layouts_and_keeps_what_servers_use(scratch):
    """prune(final) removes the modules directories of other layouts and the
    versions nobody needs, and keeps the directories a running server was
    started with. The versions 1.0.x are neither installed nor running."""
    prepare = load_prepare()
    base = os.path.join(scratch("rmcheck-test-prune."), "rm-check")
    final = os.path.join(base, "1.0.0", "modules-v2")
    kept = [final, os.path.join(base, "1.0.0", "modules.work"),
            os.path.join(base, "1.0.0", "modules-v1"), os.path.join(base, "1.0.3", "modules")]
    removed = [os.path.join(base, "1.0.0", "modules"), os.path.join(base, "1.0.2", "modules-v2")]
    for d in kept + removed:
        os.makedirs(d)
    # A process whose arguments look like a server's, for the two directories
    # that a running server uses.
    servers = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                 "/x/rm-check-server.mjs", d, "/x/sock"])
               for d in (kept[2], kept[3])]
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not {kept[2], kept[3]} <= prepare.server_dirs():
            time.sleep(0.05)
        prepare.prune(final)
    finally:
        for p in servers:
            p.kill()
            p.wait()
    assert [d for d in kept if not os.path.isdir(d)] == []
    assert [d for d in removed if os.path.exists(d)] == []


def test_prepare_refuses_a_directory_outside_the_cache_layout():
    prepare = load_prepare()
    for final in ("relative/rm-check/2.1.305/modules-v2", "/x/rm-check/2.1.304/modules-v2",
                  "/x/cache/2.1.305/modules-v2", "/x/rm-check/2.1.305/other"):
        with pytest.raises(SystemExit, match="is not an absolute"):
            prepare.main(["/x/claude", "2.1.305", final])


# A chunk that behaves like the Bash tool and the two directory functions the
# hook needs, and like look-alikes of them. Its tool's checkPermissions flags
# `rm -rf /`, and the removal of its working directory or a parent of it; it
# follows a cd only into the session's original directory.
FAKE_CHUNK = r"""
import { AsyncLocalStorage } from "node:async_hooks";
const store = new AsyncLocalStorage();
let originalCwd = "/start";
const within = (path, dir) => dir === "/" || path === dir || path.startsWith(dir + "/");
function run(dir, fn) { return store.run({ cwd: dir }, fn); }
function runWithout(dir, fn) { return fn(); }
function setOriginal(dir) { originalCwd = dir; }
function setNothing(dir) {}
async function check({ command }, context) {
  let cwd = store.getStore()?.cwd ?? "/start";
  const m = /^(?:cd (\S+) && )?rm -rf (\S+)$/.exec(command);
  if (m === null) return { behavior: "passthrough", message: "" };
  if (m[1] !== undefined && within(m[1], originalCwd)) cwd = m[1];
  if (m[2] === "/" || within(cwd, m[2])) {
    return { behavior: "ask", message: `removes ${m[2]}`,
             decisionReason: { type: "safetyCheck", reason: "x", circuitBreaker: "dangerousRemoval" } };
  }
  return { behavior: "passthrough", message: "" };
}
var bashTool = { name: "Bash", checkPermissions: check };
var lookalike = { name: "Bash", checkPermissions(input, context) { return undefined; } };
var noMethod = { name: "Bash" };
"""
DRIVER = """
import { loadCheck } from %s;
const [dir, request] = process.argv.slice(2);
try {
  const run = await loadCheck(dir);
  console.log(JSON.stringify({ out: await run(JSON.parse(request)) }));
} catch (e) {
  console.log(JSON.stringify({ error: e.message }));
}
"""


def load_fake(scratch, tool, run, origin, request):
    """Writes the fake chunk with prepare.py's export statement, lists the given
    names for each role in a manifest, and returns what loadCheck and one
    request give: {"out": the check's text or None} or {"error": message}."""
    bun = find_bun()
    if bun is None:
        pytest.skip("bun is not installed")
    prepare = load_prepare()
    d = scratch("rmcheck-test-fake.")
    names = ["bashTool", "lookalike", "noMethod", "run", "runWithout", "setOriginal", "setNothing"]
    with open(os.path.join(d, "fake.js"), "wb") as f:
        f.write(FAKE_CHUNK.encode() + prepare.export_statement(names))
    role = lambda found: [{"chunk": "fake.js", "name": n, "export": prepare.export_name(n)}  # noqa: E731
                          for n in found]
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"version": "0.0.0", "exe": "", "tool": role(tool), "runInCwd": role(run),
                   "setOriginalCwd": role(origin)}, f)
    driver = os.path.join(d, "driver.mjs")
    lib = os.path.join(REPO, "hooks", "rm-check", "rm-check-lib.mjs")
    with open(driver, "w") as f:
        f.write(DRIVER % json.dumps(lib))
    p = subprocess.run([bun, driver, d, json.dumps(request)], text=True, capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


REQUEST = {"command": "cd / && rm -rf /s/c", "cwd": "/s/c", "originalCwd": "/s", "mode": "default"}


def test_loadcheck_picks_what_behaves_as_the_hook_needs(scratch):
    """Among look-alikes, the self-test picks the tool and the pair of
    directory functions that work."""
    got = load_fake(scratch, ["lookalike", "noMethod", "bashTool"], ["runWithout", "run"],
                    ["setNothing", "setOriginal"], REQUEST)
    assert got == {"out": "removes /s/c"}
    got = load_fake(scratch, ["lookalike", "bashTool"], ["runWithout", "run"],
                    ["setNothing", "setOriginal"], dict(REQUEST, cwd="/s"))
    assert got == {"out": None}


def test_loadcheck_refuses_what_does_not_behave(scratch):
    """Without a working candidate for a role, loadCheck throws and says what
    the candidates did."""
    got = load_fake(scratch, ["lookalike"], ["run"], ["setOriginal"], REQUEST)
    assert "could not be identified" in got["error"] and "lookalike in fake.js did not flag it" in got["error"]
    got = load_fake(scratch, ["bashTool"], ["runWithout"], ["setOriginal"], REQUEST)
    assert "0 pairs pass" in got["error"] and "probe 1" in got["error"], got
    got = load_fake(scratch, ["bashTool"], ["run"], ["setNothing"], REQUEST)
    assert "0 pairs pass" in got["error"] and "probe 3" in got["error"], got
    got = load_fake(scratch, ["noMethod"], ["run"], ["setOriginal"], REQUEST)
    assert "offers no usable tool" in got["error"], got


def discover_on(monkeypatch, modules):
    """prepare.discover on synthetic modules {name: source}, which pfg.bundle
    parses."""
    if shutil.which("node") is None and not os.environ.get("npm_node_execpath"):
        pytest.skip("node is not installed")
    prepare = load_prepare()
    records = [(i, f"/$bunfs/root/{name}".encode(), src.encode()) for i, (name, src) in enumerate(modules.items())]
    monkeypatch.setattr(prepare.bun_handler, "split_extracted_js", lambda data: records)
    return prepare.discover(b"")


NAME = 'var Be="Bash";export{Be};'
BASH = ('import{Be}from"/$bunfs/root/name.js";function tn(d){return d}'
        'var Xi=tn({name:Be,searchHint:"execute shell commands",create(e){return{async '
        'checkPermissions(n,r){return n}}}});')
# Look-alikes the anchors must refuse: a callback that is not the second
# parameter, and parameters that are destructuring patterns with a free name as
# the value, whose missing and null binding ids must not compare equal.
CWD = ('import{AsyncLocalStorage as d}from"async_hooks";var n=new d;function On(e){return e}'
       'function Dg(e,o){return n.run({cwd:On(e)},o)}function other(e,o){return n.run({cwd:e},x)}'
       'function pattern({dir},fn){return n.run({cwd:free},fn)}')
STATE = ('var s={};function DEn(e){Object.assign(s,e)}function mI(e){DEn({originalCwd:e})}'
         'function pattern({d}){DEn({originalCwd:free})}')


def test_discover_finds_the_tool_and_the_directory_functions(monkeypatch):
    roles, _ = discover_on(monkeypatch, {"name.js": NAME, "bash.js": BASH, "cwd.js": CWD, "state.js": STATE})
    assert roles == {"tool": [{"chunk": "bash.js", "name": "Xi"}],
                     "runInCwd": [{"chunk": "cwd.js", "name": "Dg"}],
                     "setOriginalCwd": [{"chunk": "state.js", "name": "mI"}]}


def test_discover_names_every_anchor_that_fails(monkeypatch):
    with pytest.raises(SystemExit) as e:
        discover_on(monkeypatch, {"other.js": "let x=1;"})
    message = str(e.value)
    for anchor in ("TOOL_HINT", "CWD", "ORIGINAL_CWD"):
        assert anchor in message, message
    with pytest.raises(SystemExit) as e:
        discover_on(monkeypatch, {"name.js": NAME.replace("Bash", "Monitor"), "bash.js": BASH, "cwd.js": CWD,
                                  "state.js": STATE + "var __ccRmCheck=1;"})
    message = str(e.value)
    assert "TOOL_HINT" in message and "found 0" in message and "EXPORT" in message, message
    assert "refused Xi in bash.js: name 'Monitor'" in message, message
    assert "CWD:" not in message and "ORIGINAL_CWD" not in message, message
