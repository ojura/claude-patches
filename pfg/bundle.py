"""
bundle: the JavaScript modules of a Bun executable as syntax and bindings, for code that reads tables
out of them without running them.

bun_handler reads the modules out of the binary. The node helper pfg/js/facts.mjs parses each module on
its own with the TypeScript parser vendored in pfg/js/vendor/typescript and answers questions about its
syntax and bindings. This module keeps the helper running for the life of a Bundle, links the modules
through their imports and exports, and converts the helper's positions.

Positions. The helper counts UTF-16 code units, as JavaScript does; Python strings count code points.
They differ after a character above U+FFFF. Every position in this module's API is a UTF-16 offset, and
Module.to_codepoints converts one where Python text is involved. An offset that falls between the two
halves of a surrogate pair is refused.

Handles. A handle names a node in one module. It is the list of child indices on the path from the
module's root to the node, in ts.forEachChild order, joined with dots; "" is the module itself (facts.mjs,
nodeAt). The same handle string exists in every module, so a handle means nothing without its module
index: store and compare nodes as (module index, handle). A node's handle does not change between parses
of the same text. A binding id is different. It is the module index and the UTF-16 offset of the
declaration's name ("m199@57" is the binding whose name starts at offset 57 of module 199), so ids from
different modules are never equal, and a binding id is a key on its own.

Failures:
  BackendError  bun_handler cannot read the binary, or the helper or its protocol failed: node missing,
                a crash, a malformed or mismatched reply, or a request the helper refused as malformed.
                Extraction then fails.
  AnchorError   a structural predicate matched other than exactly one entity.
  Unresolved    a supported question whose answer the source does not establish, with the module, the
                position and the reason.

REVISION is the number in pfg/REVISION as this process read it when it imported this module. The helper
reads the same file when it starts and reports its number; a difference means pfg changed under a running
process, which the client refuses. The number changes with every change that alters what pfg extracts:
in this module, in pfg/values.py, in pfg/js/facts.mjs and what it imports, in the vendored TypeScript,
and in bun_handler's decoding of module sources (UTF-8, Latin-1 and UTF-16 storage, single-module builds,
module identity).
Maintainers must raise this revision; a code change alone does not invalidate a cached catalog.
"""
import bisect
import collections
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import weakref

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "js", "facts.mjs")
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bun_handler"))
import bun_handler  # noqa: E402

with open(os.path.join(HERE, "REVISION"), encoding="ascii") as _file:
    REVISION = int(_file.read().strip())
PROTOCOL = 1
_ASTRAL = re.compile("[\U00010000-\U0010FFFF]")
# How many bytes from the end of the helper's stderr a failure message quotes.
_STDERR_TAIL = 8192


class BackendError(Exception):
    """bun_handler cannot read the binary, or the helper or the protocol failed; the message says how, for
    the helper with the end of its stderr."""


class AnchorError(Exception):
    """A predicate matched other than exactly one entity. candidates lists (module, offset, kind) of
    each match. A predicate that matched zero entities has an empty list and cannot name a location."""

    def __init__(self, label, candidates):
        self.label, self.candidates = label, list(candidates)
        self.count = len(self.candidates)
        where = "" if not self.candidates else ": " + ", ".join(f"module {m} at {s} ({k})" for m, s, k in self.candidates)
        super().__init__(f"{label}: expected exactly one match, found {self.count}{where}")


class Unresolved(Exception):
    """A question the source does not answer, at a UTF-16 offset of a module."""

    def __init__(self, module, offset, reason):
        self.module, self.offset, self.reason = module, offset, reason
        super().__init__(f"module {module} at {offset}: {reason}")


class NotExported(Unresolved):
    """A module does not export the name asked for."""


# The catalog rests on an assumption: the program does not change the built-in operations its tables
# depend on in a way the built-in scan does not identify. Juraj, to pfg-advisor: "we don't have to
# overengineer if you say it's ok". The option he answered, labelled "State the assumption", was the
# coordinator's text. pfg-advisor judged the assumption acceptable on three conditions: a write the scan
# identifies refuses the tables that depend on what it writes; the statement of the assumption lists, for
# each dependency of a table, the places where values escape the scan; and the finite-key proof and the
# Symbol.for exception rest on the same assumption. A place where a value escapes refuses nothing,
# because closing that gap would need an alias analysis of the whole program.
#
# The forms of writes the scan does not identify. builtin_summary labels each entry that comes from one of
# them with its key (the entry's limit) and states them under "limits".
BUILTIN_SCAN_LIMITS = {
    "instances": (
        "A write through an object whose prototype the scan does not name: an object received as a parameter, "
        "returned by a function, read from a member or held in a variable. The scan names the prototype of a "
        "literal, of `new X()` with X a built-in constructor and of the iterator a built-in iterator method "
        "returns only in the expression that creates the object; other writes into prototypes go to "
        "prototypeWrites. On 2.1.304, zod's six prototype writes are of this form: they define members on "
        "Object.getPrototypeOf of a parameter or of a parameter's _zod member. Four of them, the toString and "
        "isEmpty definitions in modules 41 and 1520, skip the write when that prototype is in a WeakSet that "
        "starts with Object.prototype and Error.prototype, created in module 41 at offsets 1087 and 78821 and "
        "in module 1520 at 109263 and 593604; next to isEmpty, zod defines format, flatten, addIssue and "
        "addIssues on the same prototype through a helper that receives it as a parameter, which the scan "
        "counts as unidentified uses. The other two (module 40 at 9369, module 1520 at 104580) skip the write "
        "when the prototype already has the member, unless zod is initializing the same object again. An "
        "instance passed to a function, which may read its prototype and hand it on, is not listed either: on "
        "2.1.304, get-intrinsic's table in module 119 stores %ArrayIteratorPrototype%, %IteratorPrototype%, "
        "%MapIteratorPrototype%, %SetIteratorPrototype% and %StringIteratorPrototype% as the prototypes of "
        "iterators it creates, through a getProto function from another module (offsets 13276, 14404, "
        "14530, 14950 and 15122), and code that reads the table can write their members."),
    "definer-calls": (
        "A definer called other than directly or through one call or apply: through a chain of call or apply, "
        "Reflect.apply or bind, or by a higher-order built-in it is passed to. The scan lists the definer where "
        "it goes unfollowed. On 2.1.304: signal-exit binds Object.defineProperty in module 86 at offset 17362 "
        "(`Object.defineProperty.bind(Object)`) and defines a member of globalThis under a Symbol.for key with it. "
        "A mutating method of Array.prototype is modelled directly and through one call, apply, bind or "
        "Reflect.apply; called from anywhere else, it writes only index members and the length of its this "
        "value, none of which is a dependency slot, so the scan does not record a limit for it."),
    "with": (
        "A built-in's name inside a with statement, which may name a member of the with's object instead. Both "
        "passes follow it as the built-in and list the place. An ES module cannot contain a with statement, "
        "and every module of 2.1.304 is an ES module."),
    "bounds": (
        "A variable that derives a value from its own value, as `t = t.prototype` does, or that holds many "
        "built-in values: the scan lists and does not follow a value deeper than its bound on members "
        "(facts.mjs, MAX_VALUE_PATH) or past its bound on values per variable (MAX_VALUES_PER_VARIABLE). A write "
        "through such a value is not recorded, so the entry stands for possible writes to that value and its "
        "members."),
}

Needle = collections.namedtuple("Needle", "key type value")
Needle.__doc__ = """A literal fact a predicate looks for, which the index pass finds in every module: a
string literal's cooked value (type "string"), a template head's (type "template-head"), a property
name in an object literal ("property") or a member name of a property access ("member")."""
NEEDLE_TYPES = ("string", "template-head", "property", "member")


def _is_index(value):
    return isinstance(value, int) and not isinstance(value, bool)


def builtin_write_affects(target, dependency):
    """Whether replacing target can replace a dependency the model uses. A write at an ancestor replaces
    its descendants too; * names an unknown member. A computed member of the global object could name
    any built-in."""
    if target.startswith("globalThis.*"):
        return True
    written, needed = target.split("."), dependency.split(".")
    for left, right in zip(written, needed):
        if left == "*":
            return True
        if left != right:
            return False
    return len(written) <= len(needed)


def possible_writes(summary, dependency):
    """The entries of a builtin_summary through which the program may change the slot dependency (a
    path such as Set.prototype.has) without the scan identifying a write to it, as (category, entry)
    pairs in the summary's order. They refuse nothing; the statement of the catalog's assumption lists
    them for each dependency of a table and of a key proof.
      unfollowed       a value whose path is a proper prefix of the slot's goes where the scan does not
                       follow it, and the code there may write its members at any depth; the global
                       object, an escaping definer (limit definer-calls) and a name inside a with
                       statement (limit with) reach every slot;
      dynamic          code the scan cannot see into reaches every slot;
      prototypeWrites  a write into an object the scan cannot identify, under the slot's own key or an
                       unknown one: the object may be the slot's;
      unparsed         a module with parse diagnostics reaches every slot.
    The convention for an escaped value follows its member paths only: code that receives Set.prototype
    may write Set.prototype.has, and the scan does not count what it could reach through constructor,
    __proto__ or the Function constructor, which would make every escape reach every slot. Uses of
    values the scan cannot identify are counted in unidentified and not listed."""
    needed = dependency.split(".")
    found = []
    for entry in summary["unfollowed"]:
        value = entry["value"].split(".")
        if entry.get("limit") in ("definer-calls", "with") or value[0] == "globalThis" or (
                len(value) < len(needed) and needed[:len(value)] == value):
            found.append(("unfollowed", entry))
    found.extend(("dynamic", entry) for entry in summary["dynamic"])
    for entry in summary["prototypeWrites"]:
        if entry["target"].split(".")[-1] in ("*", needed[-1]):
            found.append(("prototypeWrites", entry))
    found.extend(("unparsed", entry) for entry in summary["unparsed"])
    return found


class Module:
    """A module of the bundle: its index in Bun's module table, its name, its length in UTF-16 units and
    in code points, and where its characters above U+FFFF are, which convert positions between the two."""

    def __init__(self, index, name, text):
        self.index, self.name = index, name
        # The code point index of each character above U+FFFF, and the UTF-16 offset of its surrogate pair.
        self._astral = [match.start() for match in _ASTRAL.finditer(text)]
        self._pairs = [at + before for before, at in enumerate(self._astral)]
        self.codepoints = len(text)
        self.length = len(text) + len(self._astral)

    def check(self, offset):
        """offset itself, after checking that it is a UTF-16 offset of this module outside a surrogate pair."""
        if not _is_index(offset) or not 0 <= offset <= self.length:
            raise BackendError(f"offset {offset!r} lies outside module {self.index} ({self.length} UTF-16 units)")
        before = bisect.bisect_left(self._pairs, offset)
        if before and self._pairs[before - 1] + 1 == offset:
            raise BackendError(f"offset {offset} of module {self.index} falls inside a surrogate pair")
        return offset

    def to_codepoints(self, offset):
        """The code point index of a UTF-16 offset."""
        self.check(offset)
        return offset - bisect.bisect_left(self._pairs, offset)

    def to_utf16(self, index):
        """The UTF-16 offset of a code point index."""
        if not _is_index(index) or not 0 <= index <= self.codepoints:
            raise BackendError(f"code point index {index!r} lies outside module {self.index} ({self.codepoints} code points)")
        return index + bisect.bisect_left(self._astral, index)

    def __repr__(self):
        return f"Module({self.index}, {self.name!r})"


_helpers = weakref.WeakSet()


def _after_fork_in_child():
    # A forked child inherits the parent's pipes to the helper. It must not write to them, close the
    # helper or wait for it, and it must not keep them open, or the helper would not see the end of its
    # input when the parent closes it. _disown points the child's descriptors at /dev/null.
    for helper in list(_helpers):
        helper._disown()


os.register_at_fork(after_in_child=_after_fork_in_child)


class _Helper:
    """The node process running facts.mjs, one request at a time over JSON lines on two pipes. Its stderr
    goes to an unnamed temporary file, whose end a failure message quotes. Reading that file needs no
    thread, so a process using the helper stays single-threaded: a thread blocked in a read would hold
    its stream's lock across a fork, and the child would hang when it closed that stream."""

    def __init__(self):
        # Inside an npm script, use the Node executable npm runs on. This process then has the node
        # snap's AppArmor label, and starting /snap/bin/node from it runs snap-confine, whose profile
        # denies inheriting the unlinked stderr file (operation="file_inherit" in the kernel log); the
        # helper's stderr becomes a read-only null file, where every write fails with EBADF. Outside
        # npm, use the node that PATH selects.
        node = os.environ.get("npm_node_execpath") or shutil.which("node")
        if node is None:
            raise BackendError("pfg's JavaScript helper needs node, which was not found on PATH")
        self._owned = True
        self._closed = False
        self._next = 0
        self._stderr = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen([node, "--expose-gc", HELPER], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=self._stderr, start_new_session=True)
        except OSError as exc:
            self._stderr.close()
            raise BackendError(f"pfg's JavaScript helper could not start ({node} {HELPER}): {exc}") from None
        except BaseException:
            self._stderr.close()
            raise
        _helpers.add(self)
        try:
            hello = self._read()
            info = hello.get("hello") if isinstance(hello, dict) else None
            if not isinstance(info, dict) or info.get("protocol") != PROTOCOL or info.get("helper") != "pfg-facts":
                raise BackendError(f"the helper's first line is not pfg-facts protocol {PROTOCOL}: {hello!r}")
            if info.get("revision") != REVISION:
                raise BackendError(f"pfg changed while this process ran: its helper is revision {info.get('revision')}, "
                                   f"the pfg this process loaded is revision {REVISION}; restart the process")
            self.node, self.typescript = info.get("node"), info.get("typescript")
        except BaseException:
            self.close(kill=True)
            raise

    def _stderr_tail(self):
        """The last lines of what the helper wrote to stderr so far, at most _STDERR_TAIL bytes."""
        try:
            fd = self._stderr.fileno()
            size = os.fstat(fd).st_size
            data = os.pread(fd, _STDERR_TAIL, max(0, size - _STDERR_TAIL))
        except (OSError, ValueError):
            return ""
        return data.decode("utf-8", "replace").rstrip("\n")

    def _failure(self, text):
        tail = self._stderr_tail()
        return BackendError(f"{text}" + (f"; the helper's stderr ends:\n{tail}" if tail else ""))

    def _fatal(self, text):
        """The protocol broke: the message, with the end of the helper's stderr, after the helper is gone.
        Requests after this fail at once."""
        error = self._failure(text)
        self.close(kill=True)
        return error

    def _read(self):
        line = self._process.stdout.readline()
        if not line:
            # Closing stdout prevents every later reply, even if the process keeps running.
            status = self._process.poll()
            raise self._fatal("the helper closed its output before it answered"
                              + (f" and exited with status {status}" if status is not None else ""))
        try:
            return json.loads(line)
        except ValueError as exc:
            raise self._fatal(f"the helper wrote a line that is not JSON ({exc}): {line[:200]!r}") from None

    def _write(self, message):
        if not self._owned:
            raise BackendError("this process forked from the one that owns the helper; it must start its own")
        if self._closed:
            raise BackendError("the helper was closed, after a failure or by its owner")
        try:
            self._process.stdin.write(json.dumps(message, allow_nan=False).encode() + b"\n")
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise self._fatal(f"the helper stopped reading its input ({exc})") from None

    def notify(self, op, **args):
        self._write({"op": op, **args})

    def request(self, op, **args):
        self._next += 1
        self._write({"id": self._next, "op": op, **args})
        reply = self._read()
        if not isinstance(reply, dict) or reply.get("id") != self._next:
            raise self._fatal(f"the helper answered request {self._next} with {str(reply)[:200]}")
        if reply.get("ok") is True and "result" in reply:
            return reply["result"]
        error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
        raise self._failure(f"the helper refused {op} ({error.get('kind')}): {error.get('message')}"
                            + (f"\n{error['detail']}" if error.get("detail") else ""))

    def _pipes(self):
        return (self._process.stdin, self._process.stdout)

    def _disown(self):
        """In a forked child: let go of the helper. Each pipe descriptor the child inherited is pointed at
        /dev/null with dup2, which closes the child's copy of the pipe while its stream object keeps owning
        the descriptor number; closing that object later closes /dev/null, never a descriptor the child
        opened since."""
        self._owned = False
        null = os.open(os.devnull, os.O_RDWR | os.O_CLOEXEC)
        try:
            for stream in self._pipes():
                try:
                    # Not inheritable, as the pipe was: a program the child execs does not get it.
                    os.dup2(null, stream.fileno(), inheritable=False)
                except ValueError:
                    pass  # closed before the fork
        finally:
            os.close(null)

    def close(self, kill=False):
        """End the helper and release its pipes and its stderr file. The owner closes the helper's input
        and waits up to 5 s for it to exit, then kills its process group; kill=True kills at once. A forked
        child only closes its own copies, which for the pipes _disown pointed at /dev/null. Closing twice
        is idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._owned and not kill:
                try:
                    self._process.stdin.close()
                    self._process.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    pass
        finally:
            # Also when the wait is interrupted, as by KeyboardInterrupt, which then goes on: the helper
            # is killed and every stream closed first.
            try:
                if self._owned and self._process.poll() is None:
                    try:
                        os.killpg(self._process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self._process.wait()
            finally:
                for stream in (*self._pipes(), self._stderr):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass


class Bundle:
    """The modules of one executable, with the helper that holds their texts, for use in a with block:

        with Bundle.from_binary(open_file, needles) as bundle:
            ...

    The constructor streams the modules to the helper and runs the index pass, which parses every
    module once and keeps a detached record of each: its imports, exports and other loads in source
    order, the needle hits, and its built-in scan (facts.mjs, "The built-in scan"). A failure while
    starting, streaming or indexing ends the helper before the exception leaves the constructor."""

    def __init__(self, sources, needles=()):
        sources = list(sources)
        modules = {}
        names = set()
        for index, name, text in sources:
            if not _is_index(index) or index < 0:
                raise ValueError(f"a module index must be a non-negative integer, not {index!r}")
            if index in modules or name in names:
                raise ValueError(f"module index {index} or name {name!r} occurs twice")
            if not isinstance(name, str) or not isinstance(text, str):
                raise ValueError(f"module {index} needs a name and a text")
            modules[index] = Module(index, name, text)
            names.add(name)
        self.modules = modules
        self.by_name = {module.name: module for module in modules.values()}
        self._imports_cache = {}
        self._exports_cache = {}
        self._importers_by_target = None
        needles = [Needle(*needle) for needle in needles]
        self.needles = {}
        for needle in needles:
            if not isinstance(needle.key, str) or needle.type not in NEEDLE_TYPES or not isinstance(needle.value, str):
                raise ValueError(f"a needle is a key, one of the types {', '.join(NEEDLE_TYPES)} and a string value, not {needle!r}")
            if needle.key in self.needles:
                raise ValueError(f"two needles have the key {needle.key!r}")
            self.needles[needle.key] = needle
        self._helper = _Helper()
        try:
            for index, name, text in sources:
                self._helper.notify("module", index=index, name=name, text=text)
            result = self._helper.request("index", needles=[
                {"key": needle.key, "type": needle.type, "value": needle.value} for needle in needles])
            self.records = {record["index"]: record for record in result["modules"]}
            if set(self.records) != set(modules):
                raise BackendError("the index pass did not return one record for each module")
        except BaseException:
            self._helper.close(kill=True)
            raise

    @classmethod
    def from_binary(cls, source, needles=()):
        """The bundle of a Bun executable, read with bun_handler from bytes or from an open binary file.
        The file is read from its start, through the descriptor it already has, so the bytes extracted are
        those of the file the caller opened and identified. Raises BackendError for a binary bun_handler
        cannot read: one that is not a well-formed Bun image (BunFormatError), or a format it does not
        support (NotImplementedError)."""
        if isinstance(source, (bytes, bytearray, memoryview)):
            data = bytes(source)
        else:
            source.seek(0)
            data = source.read()
        try:
            image = bun_handler.BunImage(data)
            sources = [(record["index"], record["name_str"], image.read_source(record).decode("utf-8"))
                       for record in image.source_modules()]
        except (bun_handler.BunFormatError, NotImplementedError) as exc:
            raise BackendError(f"bun_handler cannot read the binary: {exc}") from None
        del image, data
        return cls(sources, needles)

    @classmethod
    def from_sources(cls, sources, needles=()):
        """The bundle of (index, name, text) modules, as tests build them."""
        return cls(list(sources), needles)

    def close(self):
        self._helper.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._helper.close(kill=exc[0] is not None)
        return False

    @property
    def node_version(self):
        return self._helper.node

    @property
    def typescript_version(self):
        return self._helper.typescript

    def operation_vocabulary(self):
        """Every built-in operation a use of a value can require in the helper's operation model
        (facts.mjs, OPERATION_VOCABULARY), as dotted paths such as Set.prototype.has."""
        return self._helper.request("vocabulary")["operations"]

    # -- the index -------------------------------------------------------------------------------------

    def module(self, index):
        try:
            return self.modules[index]
        except KeyError:
            raise BackendError(f"module {index} is absent from this bundle") from None

    def diagnostics(self, index):
        """The parse diagnostics TypeScript reported for a module, as [{s, length, message}]."""
        self.module(index)
        return self.records[index]["diagnostics"]

    def check_parsed(self, index):
        """Raise Unresolved for a module with parse diagnostics, at its first one. Such a module gives no
        needle hits, links or facts: its tree after a syntax error is TypeScript's recovery, not the
        program, so a hit in it could decide an exactly-one count and a link could lead into it."""
        diagnostics = self.diagnostics(index)
        if diagnostics:
            more = f" (and {len(diagnostics) - 1} more)" if len(diagnostics) > 1 else ""
            raise Unresolved(index, diagnostics[0]["s"], f"TypeScript cannot parse this module: {diagnostics[0]['message']}{more}")

    def hits(self, key):
        """The index pass's hits of a needle: (module index, hit) pairs, hit being the helper's record of
        the node (handle h, to be used with the module index, kind k, start s, end e, chain). Modules
        with parse diagnostics give none."""
        if key not in self.needles:
            raise BackendError(f"needle {key!r} was not registered for the index pass")
        return [(index, hit) for index in sorted(self.records) if not self.records[index]["diagnostics"]
                for hit in self.records[index]["hits"].get(key, [])]

    def loads(self, index):
        """A module's imports, exports and other loads, in source order. Raises Unresolved for a module
        with parse diagnostics."""
        self.check_parsed(index)
        return self.records[index]["loads"]

    # -- facts -----------------------------------------------------------------------------------------

    def facts(self, index, queries):
        """Answer queries about one module in one batch (facts.mjs, factsBatch). Raises Unresolved for a
        module with parse diagnostics."""
        self.check_parsed(index)
        return self._helper.request("facts", module=index, queries=list(queries))

    def syntax(self, index, handle, depth=64):
        """The syntax tree of the node at handle in module index, down to depth levels of children. Deeper
        nodes are replaced by records with truncated set; ask for one with its own handle. The handle
        belongs to the module (see Handles above). A handle that does not name a node of the module
        raises BackendError."""
        return self.facts(index, [{"q": "node", "handle": handle, "depth": depth}])[0]

    def binding_at(self, index, start, end):
        """The binding the identifier at exactly start:end refers to: its record, or {"free": name} for a
        name the module does not declare."""
        self.module(index).check(start)
        self.module(index).check(end)
        return self.facts(index, [{"q": "bindingAt", "s": start, "e": end}])[0]

    def binding(self, index, binding_id):
        return self.facts(index, [{"q": "record", "id": binding_id}])[0]

    def scope_binding(self, index, name):
        """The record of the binding a name has in a module's scope, or {"free": name}."""
        return self.facts(index, [{"q": "scope", "name": name}])[0]

    @staticmethod
    def refusal(index, refused):
        """The Unresolved for a binding or text the binding model refuses (facts.mjs, "The binding
        model"), from the helper's list of {s, e, reason}, at the first place it names."""
        reasons = "; ".join(f"at {item['s']}: {item['reason']}" for item in refused)
        return Unresolved(index, refused[0]["s"], f"the binding model refuses it: {reasons}")

    def occurrences(self, index, binding_id):
        """Every occurrence of a binding, declarations included, with roles and chain. Raises Unresolved
        for a binding the binding model refuses."""
        result = self.facts(index, [{"q": "occurrences", "id": binding_id}])[0]
        if isinstance(result, dict):
            raise self.refusal(index, result["refused"])
        return result

    def canonical(self, index, handle, roles=None):
        """{text, others} for the node at handle (facts.mjs, canonical). Raises Unresolved where the
        binding model refuses a binding the text names, or the node holds a direct eval or a with."""
        result = self.facts(index, [{"q": "canonical", "handle": handle, "roles": dict(roles or {})}])[0]
        if "refused" in result:
            raise self.refusal(index, result["refused"])
        return result

    def text(self, index, start, end):
        module = self.module(index)
        module.check(start)
        module.check(end)
        return self.facts(index, [{"q": "text", "s": start, "e": end}])[0]

    def leading_comments(self, index):
        return self.facts(index, [{"q": "comments"}])[0]

    # -- linking ---------------------------------------------------------------------------------------

    def imports(self, index):
        """{local name: (target module index or None, imported name or "*" or "default", local position)}
        for a module's static imports; a target of None is a module outside the bundle, such as a Node
        builtin."""
        if index not in self._imports_cache:
            found = {}
            for load in self.loads(index):
                if load["k"] != "import":
                    continue
                target = load.get("t")
                if "default" in load:
                    found[load["default"][0]] = (target, "default", load["default"][1])
                if "namespace" in load:
                    found[load["namespace"][0]] = (target, "*", load["namespace"][1])
                for imported, local, position in load.get("named", []):
                    found[local] = (target, imported, position)
            self._imports_cache[index] = found
        return self._imports_cache[index].copy()

    def export(self, index, name, path=()):
        """Resolve an exported name (see _export). Cache completed resolutions, whose answer does not
        depend on a traversal path; recursive resolutions retain the path used to detect cycles."""
        if path:
            return self._export(index, name, path)
        key = (index, name)
        if key not in self._exports_cache:
            self._exports_cache[key] = self._export(index, name, ())
        return self._exports_cache[key]

    def _export(self, index, name, path):
        """What a module's export name refers to:
          ("binding", module index, local name)  a binding of that module's scope, which holds exactly one
                                                 binding per name (scope_binding gives its record);
          ("namespace", module index)            a module namespace object;
          ("declaration", module index, s, e)    an `export default function(){}` or `class{}` without a
                                                 name; its value is not held by a local binding;
          ("expression", module index, s, e)     the expression of an `export default` that is not a
                                                 declaration, evaluated once when the module runs.
        Follows imports that are exported again, re-exports from another module and export *; an explicit
        export wins over export *, export * never supplies default, and two export * paths that reach
        different bindings make the name ambiguous. Raises NotExported for a name the module does not
        export, and Unresolved for a cycle, an ambiguity, a module outside the bundle or a module with parse
        diagnostics."""
        self.check_parsed(index)
        if (index, name) in path:
            raise Unresolved(index, 0, f"export {name} leads round a cycle of modules")
        path = path + ((index, name),)
        imports = self.imports(index)
        for load in self.loads(index):
            if load["k"] == "export":
                for local, exported, _ in load["named"]:
                    if exported != name:
                        continue
                    if local in imports:
                        target, imported, _ = imports[local]
                        if target is None:
                            raise Unresolved(index, load["s"], f"{name} is imported from a module outside the bundle")
                        if imported == "*":
                            return ("namespace", target)
                        return self.export(target, imported, path)
                    return ("binding", index, local)
            elif load["k"] == "reexport":
                for imported, exported, _ in load["named"]:
                    if exported == name:
                        if load.get("t") is None:
                            raise Unresolved(index, load["s"], f"{name} is re-exported from a module outside the bundle")
                        return self.export(load["t"], imported, path)
            elif load["k"] == "export*as" and load["as"] == name:
                if load.get("t") is None:
                    raise Unresolved(index, load["s"], f"{name} is a namespace of a module outside the bundle")
                return ("namespace", load["t"])
            elif load["k"] in ("exportdecl", "export-default"):
                for declared, _ in load["names"]:
                    exported = "default" if load["k"] == "export-default" else declared
                    if exported == name:
                        return ("binding", index, declared)
                if load.get("anonymous") and name == "default":
                    return ("declaration", index, load["s"], load["e"])
            elif load["k"] == "export=" and name == "default":
                return ("expression", index, load["s"], load["e"])
        if name == "default":
            raise NotExported(index, 0, "the module does not export default")
        found = []
        for load in self.loads(index):
            if load["k"] != "export*":
                continue
            if load.get("t") is None:
                raise Unresolved(index, load["s"], f"export * from a module outside the bundle may supply {name}")
            try:
                found.append(self.export(load["t"], name, path))
            except NotExported:
                pass
        if not found:
            raise NotExported(index, 0, f"the module does not export {name}")
        if any(result != found[0] for result in found[1:]):
            raise Unresolved(index, 0, f"export * makes {name} ambiguous")
        return found[0]

    def export_names(self, index, seen=frozenset()):
        """Every name a module exports, through export * as well (which never supplies default). Raises
        Unresolved where a module with parse diagnostics is involved."""
        if index in seen:
            return set()
        names = set()
        for load in self.loads(index):
            kind = load["k"]
            if kind in ("export", "reexport"):
                names.update(exported for _, exported, _ in load["named"])
            elif kind == "export*as":
                names.add(load["as"])
            elif kind == "exportdecl":
                names.update(name for name, _ in load["names"] if name is not None)
            elif kind in ("export-default", "export="):
                names.add("default")
            elif kind == "export*" and load.get("t") is not None:
                names.update(name for name in self.export_names(load["t"], seen | {index}) if name != "default")
        return names

    # -- the built-in scan -----------------------------------------------------------------------------

    def builtin_scan(self):
        """The built-in scan of every module (facts.mjs, "The built-in scan"), as {module index: scan}.

        The index pass scanned each module on its own. The facts pass scans a module again with its
        bindings where the index pass cannot decide: a variable receives a built-in value, the module
        declares a built-in's name, a computed key may be a Symbol.for result, or an import receives a
        value another module exports. The values a module exports are followed into every module that
        imports them by name, through re-exports and export *, until no import gains a value; an export
        of a variable that may also hold a value the scan does not identify makes its importers' bindings
        uncertain too. Where a namespace of such a module is taken, by `import * as`, `export * as`,
        import() or import.meta.require, the values go where the scan does not follow them, and that place
        is recorded as unfollowed in the module that takes it. A module with parse diagnostics keeps its
        index-pass scan and gets no facts pass (builtin_summary lists it as unparsed)."""
        scans = {index: record["premise"] for index, record in self.records.items()}
        # {importer: {position of the import's local name: (values, unknown)}}
        seeds = collections.defaultdict(dict)
        exported = {}
        done = {}
        pending = sorted(index for index, scan in scans.items() if not self.records[index]["diagnostics"] and (
            scan["references"] or scan["scoped"] or scan["unknownKeys"]))
        while pending:
            index = pending.pop(0)
            given = tuple(sorted((at, tuple(sorted(values)), unknown) for at, (values, unknown) in seeds[index].items()))
            if done.get(index) == given:
                continue
            done[index] = given
            scans[index] = self.facts(index, [{"q": "premise", "seeds": [
                {"at": at, "values": list(values), "unknown": unknown} for at, values, unknown in given]}])[0]
            changed = set()
            for export in scans[index]["exports"]:
                target = self.export(index, export["name"])
                values, unknown = exported.get(target, (frozenset(), False))
                merged = (values | {export["value"]}, unknown or bool(export.get("unknown")))
                if merged != (values, unknown):
                    exported[target] = merged
                    changed.add(target)
            for target in sorted(changed):
                values, unknown = exported[target]
                for importer, at, _ in self.importers(target):
                    given_before = seeds[importer].get(at, (frozenset(), False))
                    seed = (given_before[0] | values, given_before[1] or unknown)
                    if seed != given_before:
                        seeds[importer][at] = seed
                        pending.append(importer)
        for importer, entry in self._namespace_sites(exported):
            scans[importer] = {**scans[importer], "unfollowed": sorted([*scans[importer]["unfollowed"], entry], key=lambda u: u["s"])}
        return scans

    def importers(self, target):
        """(module, position, local name) of every import by name or default that reaches target, an
        export() result: the import binding is the identifier at position. Modules with parse diagnostics
        have no links and import nothing. The reverse index is built once, instead of resolving every
        import again for each target."""
        if self._importers_by_target is None:
            reverse = collections.defaultdict(list)
            for index in sorted(self.records):
                if self.records[index]["diagnostics"]:
                    continue
                for load in self.loads(index):
                    if load["k"] != "import" or load.get("t") is None:
                        continue
                    named = [(imported, local, position) for imported, local, position in load.get("named", [])]
                    if "default" in load:
                        named.append(("default", load["default"][0], load["default"][1]))
                    for imported, local, position in named:
                        try:
                            resolved = self.export(load["t"], imported)
                        except Unresolved:
                            continue
                        reverse[resolved].append((index, position, local))
            self._importers_by_target = reverse
        return list(self._importers_by_target.get(target, ()))

    def _namespace_sites(self, exported):
        """(module, unfollowed entry) for every place a namespace is taken of a module that exports one of
        the values in exported ({export() result: (values, unknown)}). A module with parse diagnostics that
        takes such a namespace is listed too: the values go there."""
        if not exported:
            return
        holders = {}
        for index in sorted(self.records):
            try:
                names = sorted(self.export_names(index))
            except Unresolved:
                continue
            for name in names:
                try:
                    target = self.export(index, name)
                except Unresolved:
                    continue
                if target in exported:
                    holders.setdefault(index, []).append((name, sorted(exported[target][0])))
        for index in sorted(self.records):
            for load in self.records[index]["loads"]:
                target = load.get("t")
                namespace = (load["k"] == "import" and "namespace" in load) or load["k"] in ("export*as", "import()", "import.meta.require")
                if namespace and target in holders:
                    names = ", ".join(f"{name} ({', '.join(values)})" for name, values in holders[target])
                    yield index, {"s": load["s"], "e": load["e"], "h": load["h"], "value": holders[target][0][1][0],
                                  "reason": f"a namespace of module {target}, which exports built-in values: {names}"}

    def builtin_summary(self, scans):
        """The bundle's built-in scan as one record, for the catalog's statement of its assumption:
          writes          visible writes to a built-in or one of its members (module, s, target, how); the
                          tables that depend on a written target are refused;
          globalWrites    writes to the global object under keys that are not built-ins' names;
          registryWrites  writes under a key proven to be a Symbol.for result, which is never a built-in's
                          key; when the program writes Symbol, Symbol.for or the global object under a key
                          the scan cannot read, they count as writes under an unknown key instead;
          prototypeWrites writes into an object reached by .constructor, .__proto__ or getPrototypeOf from
                          an object the scan cannot identify;
          unfollowed      the places a built-in value goes that the scan does not follow;
          dynamic         code the scan cannot see into;
          passed          how many occurrences of each built-in value matched modelled use forms;
          references      every place a variable received a built-in value, its binding, and whether the
                          scan followed it or left it unclassified, with the reason;
          unidentified    how many other uses of values the scan cannot identify it counted;
          unparsed        the modules with parse diagnostics (module, s, message, count): the scan keeps
                          what the index pass found in them, without their bindings;
          limits          {limit: {form, entries}} for each form in BUILTIN_SCAN_LIMITS, entries counting the
                          summary's entries labelled with it, by category.
        An entry that comes from a stated limit has that limit's key in its limit field."""
        lists = collections.defaultdict(list)
        passed = collections.Counter()
        unidentified = 0
        for index in sorted(scans):
            scan = scans[index]
            for key in ("writes", "globalWrites", "registryWrites", "prototypeWrites", "unfollowed", "dynamic", "references"):
                lists[key].extend({"module": index, **{k: v for k, v in entry.items() if k != "h"}} for entry in scan[key])
            lists["writes"].extend({"module": index, **{k: v for k, v in entry.items() if k != "h"}} for entry in scan["unknownKeys"])
            passed.update(scan["passed"])
            unidentified += scan["unidentified"]
        # A key proof uses the standard operations it lists. Invalidating one can make its write an
        # unknown global key, which can invalidate another proof, so repeat until every remaining proof
        # has dependencies the scan did not find written.
        categories = ("writes", "globalWrites", "registryWrites", "prototypeWrites")
        while True:
            written = {entry["target"] for entry in lists["writes"]}
            changed = False
            next_lists = {key: [] for key in categories}
            for category in categories:
                for entry in lists[category]:
                    invalid = [dependency for dependency in entry.get("keyRequires", [])
                               if any(builtin_write_affects(target, dependency) for target in written)]
                    if not invalid:
                        next_lists[category].append(entry)
                        continue
                    changed = True
                    fallback = entry["keyFallback"]
                    revised = {key: value for key, value in entry.items() if key not in ("keyRequires", "keyFallback", "key")}
                    revised.update(target=fallback, invalidatedKeyProof=invalid)
                    if category == "prototypeWrites":
                        destination = category
                    elif fallback.startswith("globalThis.") and not fallback.startswith("globalThis.*"):
                        destination = "globalWrites"
                        revised["key"] = fallback.split(".")[1]
                    else:
                        destination = "writes"
                    next_lists[destination].append(revised)
            for category in categories:
                lists[category] = next_lists[category]
            if not changed:
                break
        ordered = {}
        for key in (*categories, "unfollowed", "dynamic", "references"):
            # Several proved keys at one call can become the same unknown key after invalidation.
            unique = {json.dumps(entry, sort_keys=True): entry for entry in lists[key]}
            ordered[key] = sorted(unique.values(), key=lambda entry: (entry["module"], entry["s"]))
        unparsed = []
        for index in sorted(self.records):
            diagnostics = self.records[index]["diagnostics"]
            if diagnostics:
                unparsed.append({"module": index, "s": diagnostics[0]["s"], "message": diagnostics[0]["message"],
                                 "count": len(diagnostics)})
        limits = {}
        for limit, form in BUILTIN_SCAN_LIMITS.items():
            counts = {key: sum(1 for entry in ordered[key] if entry.get("limit") == limit) for key in ordered}
            limits[limit] = {"form": form, "entries": {key: count for key, count in counts.items() if count}}
        return {**ordered, "passed": dict(sorted(passed.items())), "unidentified": unidentified, "unparsed": unparsed,
                "limits": limits}
