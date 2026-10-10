# bun_handler

`bun_handler.py` reads and rewrites the JavaScript inside Claude Code's native
executables, which `bun build --compile` produces. It uses only the Python
standard library (`hashlib`, `os`, `struct`, `sys`, `tempfile`). Every other file
in this directory tests or validates it.

Scripts in `util/` and `hooks/` import it after adding this directory to
`sys.path`.

## Command line

```sh
python3 bun_handler.py detect <binary>
python3 bun_handler.py extract <binary> [out.js]
python3 bun_handler.py selftest <binary>
python3 bun_handler.py extract-tweakcc <binary> [out.js]
python3 bun_handler.py recover-tweakcc <binary> <new-output> [--reference <original>]
python3 bun_handler.py compact <binary> <reference> <new-output>
```

An output path must not exist yet; the handler never overwrites a file.

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

## Repairing other tools' output

- `extract-tweakcc` reads a binary rebuilt by tweakcc's full serializer (main at
  `a806feb`), including source that tweakcc stored as UTF-8 under a stale Latin-1
  tag.
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

```sh
# 343 synthetic tests; none runs a native binary.
python3 -m unittest discover -s bun_handler -v

# 130 synthetic checks plus the gates on a real binary. The gates run patched
# copies of it. CLAUDE_PFG_STRICT_GATES=gate1,gate2,... turns a skipped gate into
# a failure.
python3 bun_handler/test_bun_handler.py ~/.local/share/claude/versions/<version>

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
