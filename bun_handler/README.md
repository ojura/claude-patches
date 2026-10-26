# bun_handler

`bun_handler.py` reads and rewrites the JavaScript inside Claude Code's native
executables, which `bun build --compile` produces. It uses only the Python
standard library (`hashlib`, `os`, `struct`, `subprocess`, `sys`, `tempfile`).
Every other file in this directory tests or validates it.

Scripts in `util/` and `hooks/` import it after adding this directory to
`sys.path`.

## Command line

```sh
python3 bun_handler.py detect <binary>
python3 bun_handler.py modules [--strict] <binary>
python3 bun_handler.py extract [--strict] <binary> [out.js]
python3 bun_handler.py selftest <binary>
python3 bun_handler.py recover-tweakcc <binary> <new-output> [--reference <original>]
python3 bun_handler.py compact <binary> <reference> <new-output>
```

An output path must not exist yet; the handler never overwrites a file.

`modules` prints the parsed module table as tab-separated columns: index, name,
the byte lengths of contents, bytecode and source map, then loader, encoding,
module format and side. Use it instead of parsing the table by hand. Bun has used
36-byte and 52-byte module records, and the parser picks the size and
bounds-checks every field. A hand parser that assumes the wrong size reads
misaligned fields as offsets and lengths, and copying those ranges for every
record can need many times the binary's size in memory.

`selftest` first runs three byte-stability checks on the binary: a no-op repack
is byte-identical, a length-changing edit round-trips and drops the changed
modules' compiled state, and two identical edits produce identical bytes. When
they pass, it runs `test_bun_handler.py` on the same binary, which must sit next
to `bun_handler.py`, and passes only if the harness passes too. The harness runs
patched copies of the binary with `--version`; for 2.1.304 the whole command
takes about 13 seconds and 1.8 GB of memory.

## Library

```python
import bun_handler

digest, source = bun_handler.extract_js_with_digest(original)
modified = source.replace(b"(Claude Code)", b"(ClauDe Code)")
patched = bun_handler.repack_with_js(original, modified, expected_sha256=digest)
```

`extract_js()` returns a single module's source as it is, or every JavaScript
module joined with marker lines when the bundle has several. That text is for
editing, not for running as one script. `repack_with_js()` writes back only the
modules whose text changed. With `expected_sha256`, it refuses a binary that
changed since extraction. `split_extracted_js()`, `changed_js_modules()` and
`repack_module_sources()` work on individual modules.

`BunImage(data).source_modules()` lists the JavaScript module records, and
`read_source(record)` gives a module's source as strict UTF-8. `pfg/bundle.py`
reads Claude Code's modules this way, and cc-env-overrides builds its catalog
from what pfg reads. A change to how this handler decodes module sources (the
three storage encodings, single-module builds, module identity) changes what pfg
extracts, so the commit that makes it must also raise the number in
`pfg/REVISION`. cc-env-overrides then rebuilds every catalog made before the
change.

## Accepted executables

- Linux x86-64 ELF64 whose `.bun` section is writable and is the last mapped
  section.
- Windows x86-64 PE32+ whose `.bun` section is the last section, with a 4- or
  8-byte Bun wrapper. An unchanged repack keeps the Authenticode signature. A real
  edit removes the certificate table, clears the Security Directory entry and
  writes an unsigned file with CheckSum 0.
- Mach-O and the old ELF form that stored Bun data after the last section are
  refused.

Bun stores source as UTF-8, Latin-1 or UTF-16LE. The handler decodes all three to
UTF-8 for editing. An unchanged module keeps its original storage, and a changed
module with non-ASCII text is stored as UTF-8.

When a module's source changes, the handler drops that module's source hash,
bytecode, module info and source map, so Bun compiles the new source instead of
running stale bytecode or stale import/export data. Unchanged modules keep their
compiled state byte for byte, and an unchanged repack is byte-identical.

## Graph flags 0x800 and 0x1000

Claude Code 2.1.278 and the later builds checked here (2.1.292, 2.1.295,
2.1.296) set module-graph flags `0x1bff`. Two of those bits add records after the
module table and the earlier optional records, in flag order:

| Flag | Record |
|---|---|
| `0x800` | prelinked module graph: u32 payload offset, u32 payload length, u32 map count, then that many u32 file-table indices |
| `0x1000` | runtime options: two u32 words |

The prelinked payload is aligned to 128 bytes, and its map links cached graph
modules to file-table indices. The handler checks the descriptor's bounds, the
count and index limits, that the indices are unique, and that the payload lies
inside the blob; it does not interpret the cached bytecode. Unknown higher flag
bits are refused.

An unchanged repack keeps the prelinked graph. Any effective edit can make it
stale, so the writer sets the descriptor to `(0, 0, 0)`, keeps the flag, and
copies the eight runtime-option bytes unchanged right after the descriptor. The
old cache and map bytes stay in the file without anything pointing to them. Bun
then starts without the prelinked cache; the effect on startup time has not been
measured.

The layout comes from `StandaloneModuleGraph.rs` on Bun's `main` branch, read on
2026-09-21, and from the bytes of Claude Code 2.1.278 (Bun 1.4.3, revision
`8c28b3167`, runtime options `02 00 00 00 00 00 00 41`). The Bun source revision
that built Claude Code was not retrieved, and the later `main` declares the flags
differently, so the record layout rests on the binary and on running edited
copies of it. `reference/schema_reference_status.json` records the retrieval
attempts.

## Linked bytecode: graph flag 0x2000

Claude Code 2.1.310 sets flags `0x3bff`. Bit `0x2000` adds a 32-byte record
after runtime options and before `compile_exec_argv`: a u32 payload offset,
a u32 payload length, and six u32 region ends relative to that payload.
The ends must be nondecreasing, and the last must equal the payload length.
The handler requires a nonempty payload inside the blob and checks its
128-byte runtime alignment.

Module and builtin bytecode pointers refer to entries inside this shared
payload. Each pointer starts at its entry and extends to the payload's end,
so these ranges overlap and their starts can be unaligned. The handler checks
each entry against the descriptor and records the whole payload as one region
when checking overlaps with source, metadata and other data. It rejects
zero-length builtin entries: Bun selects these by offset regardless of length,
so treating them as inactive would change their meaning when the payload moves.
A zero-length module pointer is inactive and remains valid.

After a source edit, the changed module's bytecode pointer becomes `(0, 0)`
and Bun compiles that module from source. Other modules and builtins retain
their entries in the unchanged payload. The writer aligns the payload's start;
adding padding before individual entries would corrupt the shared bytecode.
The region ends stay relative to the payload. Disabling the prelinked graph
removes its map from the record sequence, so both runtime options and the
linked-bytecode descriptor move immediately after the empty prelink record.
Compaction checks the descriptor and payload against the reference binary.

The record is defined by `HAS_LINKED_BYTECODE_PAYLOAD` in Bun's
[`StandaloneModuleGraph.rs`](https://github.com/oven-sh/bun/blob/449877f3e088feb6cc2dd2704ffb803d59be3277/src/standalone_graph/StandaloneModuleGraph.rs),
with the count of six in
[`standalone_module_graph.rs`](https://github.com/oven-sh/bun/blob/449877f3e088feb6cc2dd2704ffb803d59be3277/src/resolver/standalone_module_graph.rs).
These references are at commit `449877f3e088feb6cc2dd2704ffb803d59be3277`.
The installed 2.1.310 identifies its Bun runtime as 1.4.4, revision `2c2ae6246`,
which could not be retrieved from the public repository. Its bytes agree with
the public record layout:

- The payload is 100,235,616 bytes at blob offset 21,499,768.
- The six ends are 6,739,996; 20,662,014; 20,662,014; 22,498,324;
  84,658,629; and 100,235,616.
- All 2,549 module entries and 133 builtin entries start inside that payload
  and end at its end.
- The descriptor ends at 176,155,081, where `compile_exec_argv` starts.

`validate_linked_bytecode.py` checks cached importers against edited exporters,
missing exports, top-level await, empty source and regrowth, builtin bytecode,
and restoration of the original caches after compaction. It runs edited copies
of the supplied executable. The synthetic tests cover ELF and PE, both wrapper
sizes and module-record sizes, malformed descriptors and entries, and edits
that move the payload.

`pfg/REVISION` remains 9: this adds an executable layout the reader previously
refused and leaves source decoding unchanged. Extraction from 2.1.304, 2.1.305
and 2.1.309 matches the reader before this addition.

## Repairing other tools' output

- `extract` and `modules` read a binary rebuilt by tweakcc's full serializer
  (main at `a806feb`) with a separate reader and say so on stderr. `extract` also
  recovers source that tweakcc stored as UTF-8 under a stale Latin-1 tag. With
  `--strict` both refuse such a binary. The library's strict entry points
  (`extract_js`, `repack_with_js` and the others) refuse it with an error that
  says to run `recover-tweakcc`; `extract_tweakcc_js()` reads it.
- `recover-tweakcc` with `--reference <original>` checks that everything except
  the edited sources matches the original, then replays the edited sources onto
  the original with the normal writer, which restores the optional records tweakcc
  dropped and keeps the caches of unedited modules. Without a reference it builds a
  graph with source and assets only and no compiled caches. tweakcc's serializer
  drops the runtime-options record, so a binary that had one can only be recovered
  with a reference; without it the command fails with `tweakcc discarded runtime
  options; a matching reference is required`.
- `compact` removes the space that append-style writers leave behind. It needs the
  original binary and accepts only an append history it can verify, or an exact
  replay. It returns the input unchanged when replay would not make the file
  smaller. After an external writer appends an empty source body and the normal
  writer then regrows it, compaction refuses with `binary is neither a verified
  append lineage nor canonical replay`.

## Tests

Run native checks on disposable copies, from `/`, with a clean environment and
fresh HOME, XDG, CLAUDE_CONFIG_DIR and TMPDIR directories. Use synthetic
credentials, `ANTHROPIC_BASE_URL=http://127.0.0.1:9`,
`CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL=1`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `GIT_SSH_COMMAND=false`,
`GIT_ALLOW_PROTOCOL=file`, and `DBUS_SESSION_BUS_ADDRESS=disabled:`. Put
recording `ssh` and `gh` stubs first on PATH, and use the Node executable rather
than its snap launcher. Point the six uppercase/lowercase HTTP, HTTPS and ALL
proxy variables at the closed loopback port. Trace `execve`, `connect`, `sendto`,
`sendmsg` and `sendmmsg` with `strace -f --seccomp-bpf` and inspect the results.

```sh
# 380 synthetic tests using in-memory executable fixtures.
python3 -m unittest discover -s bun_handler -v

# 130 synthetic checks plus the gates on a real binary. The gates run patched
# copies of it. CLAUDE_PFG_STRICT_GATES=gate1,gate2,... turns a skipped gate into
# a failure. `bun_handler.py selftest <binary>` runs this harness after its
# byte-stability checks.
python3 bun_handler/test_bun_handler.py /path/to/disposable-claude-copy

# Additional runtime checks for linked bytecode, in the same isolated environment.
python3 bun_handler/validate_linked_bytecode.py /path/to/disposable-claude-copy

# Randomized ELF, PE and new-record fixtures.
python3 bun_handler/property_probe.py bun_handler
python3 bun_handler/pe_property_probe.py bun_handler
python3 bun_handler/run_review_probe.py property --fixtures 1200
```

Run the probes from a disposable working directory: a failed run keeps its
fixtures in a `review-property-failure-*` directory there.

The `validate_*.py` scripts, `extra_windows_runtime.py`, `audit_pe_storage.py`
and `run_review_probe.py runtime` check edited binaries at run time. Some need
Node and TypeScript (`TYPESCRIPT_PATH`) to run the tweakcc helpers in
`reference/`, and the Windows checks need Wine. `reference/` holds the tweakcc
`nativeInstallation.ts` snapshots (main at `a806feb`, PR #987 at `db7f183`; MIT,
see `reference/TWEAKCC_LICENSE`) and the Node scripts that produce fixtures with
them. No test runs node-lief or the tweakcc installer. `review_probes/` holds an
independent review's probe scripts, unchanged.

Windows binaries have been run only as 2.1.263 under Wine; the new graph records
on PE, the other wrapper widths and the 36-byte module records are covered by
synthetic fixtures only.
