# Architecture

This document is the single source of truth for how the PFG patch complex is built
and why. It is owned by the architect. The binding correctness contract lives in
`docs/invariant.md`; this document situates that contract inside the system that
enforces it and describes the build toolchain, the discovery mechanism, the
component pipeline, and the verification model.

The bulk of it states the **timeless design**: the model, the mechanisms, and the
principles that generate the decisions. Per-version vendor symbols, exact byte
offsets, and file names are deliberately absent; components are named by role (the
core, the loader, the chain-builder, the renderer, the engine) so the document does
not rot when files are renamed. A final, clearly separated **Current state**
section carries the status snapshot: what is landed, in build, and queued. The two
are kept apart on purpose, so the status churn never pollutes the timeless part.

## The four demands

The problem was posed as four demands, and the architecture is the shape that
serves all four at once:

1. **A solid, readable, SSOT, correct patch core.** One clean source of truth for
   the lineage recovery logic, with each concept defined once, guarding a hard
   correctness invariant.
2. **Synthesis against new vendor releases.** The target is a minified bundle whose
   symbols are re-mangled every build. Two kinds of drift must be handled: the
   constant minified-symbol drift, solved **programmatically** so a routine release
   needs no human touch; and the occasional structural or refactoring drift, where
   the vendor changes the code's shape, met with **tooling** so Claude can resolve
   it efficiently rather than by a rewrite.
3. **Efficient deployment.** Each supported release ships as a thin prebuilt patch
   script, stdlib-only, that reproduces the patched bundle with no dependency on the
   full toolchain.
4. **Testing and verification** of all of the above, in layers, each covering a
   failure class the others cannot see.

The core is demand 1. The engine-as-linker and the discovery mechanism are the
programmatic half of demand 2; the fail-loud surfacing and the re-anchoring tooling
are its human-assisted half. The prebuilts are demand 3. The verification model is
demand 4. Everything below is one of these four.

## The invariant: the north star

The full contract is in `docs/invariant.md`. In one sentence: every session load
that the patches touch terminates in exactly one of two states, never a third. It
reaches the conversation **origin** (the real first prompt), or it fires the single
loud marker `⛔ TRANSCRIPT INCOMPLETE · Conversation root not found`. The origin is
binary: a record either is the root or it is not. There is no "authentic" or
"partial" origin, and the deepest-on-disk root is not the origin when the true first
prompt predates it off disk.

Three failures are forbidden middles, and most of the design exists to make each one
unrepresentable rather than merely unlikely:

- a false success on a non-origin (a green "reconstructed" bookend, or simply the
  absence of the marker, on a terminus that is not the root);
- a premature marker (giving up while one more edge or one more sibling file would
  have recovered the root);
- a soft note for a hard failure (dressing an undecidable case as a provenance hint
  instead of the marker).

The rest of the architecture is judged by whether it makes these three impossible by
construction. When a mechanism can only "fail loud on the current corpus" rather
than structurally, that is a latent gap, not a guarantee.

## The core: define each concept once

The pre-refactor patches were hand-written splices into the minified bundle, so "is
this a compaction boundary", "which edge does the walk follow", and "is this a
conversation root" each got re-decided inline at many sites, and they drifted apart.
"Root reached" had on the order of eleven inconsistent definitions. You cannot guard
an invariant whose central concept is defined eleven ways.

The **core** (injected into the bundle as the `$pfg` block) defines each lineage,
compaction, and marker concept exactly once: the lineage edge (`parentUuid` else
`logicalParentUuid`), the boundary predicate, the structural origin, the
continuation-preamble content test, the fork classifier, the cycle-guarded walk, the
ghost-kind parser, and the marker-prefix constant. Every consumer references the
core; none re-decides. The core is the one library the whole system links against,
and it is the SSOT for correctness.

A verdict primitive splits two axes that must never be conflated: `reachedRoot` (the
boolean verdict, true only for an origin) and `reason` (the data behind it:
`origin | preamble | fork | none | cycle | dangling`). Path properties that modulate
trust (crossed a compaction boundary, crossed an unverified seam) ride alongside as
flags, not as reasons. A renderer keying on the boolean cannot paint a non-origin as
success, because for a non-origin the boolean is false.

## The engine is a linker

This is the organizing idea. The toolchain that composes the clean source modules
into the minified bundle is a **linker**, and naming it that way is what makes the
rest coherent. Each piece of the build is a linker phase:

- **Internal symbols** are the core members. They are statically linked by name (we
  own the source, so the names are stable) and **tree-shaken per target**: each
  target receives only the closure of core members its own source references, plus
  what those transitively use. Nothing more is emitted.
- **External symbols** are the vendor dependencies. They are **dynamically linked by
  structural discovery**, because the vendor re-mangles its symbol table on every
  build. We cannot resolve them by name; we resolve them by shape. The anchors are
  the resolver, and the discovery is a dynamic loader for an ABI whose names are
  deliberately unstable.
- The **alias prologue** is relocation. Our source refers to each vendor dependency
  through a stable placeholder in the `_pv_` namespace; the prologue rebinds each
  placeholder to the concrete resolved name (`const _pv_X = <discovered>`) so the
  body stays version-agnostic.
- The **coverage and membership checks** are the linker's undefined-symbol and
  dead-code detection. Per target, every referenced symbol must resolve and every
  emitted symbol must be referenced; a mismatch is a link error and fails loud.

The two symbol classes are split by exactly one question: do we control the name. We
own the core's names, so they are resolved by name. We do not own the vendor's, so
they are resolved by shape. That single distinction generates the whole
two-mechanism design, and the coverage and membership checks converge on one
per-target symbol table, `{internal referenced, external referenced}`, with one link
check over both.

The build objective, "clean source, the engine produces the artifact", is not a
house style. It is the compilation model: source is source, the linker emits the
relocated binary. The source never carries a discovered value, a relocated name, or
a hand-copied duplicate; the engine puts those in the output. If the engine cannot
emit clean output from clean source, the engine is improved, never the source
uglified.

## Targets and codecs

The engine is target-aware. A target is a patchable artifact (the extension-host
bundle, the webview bundle, and later the CLI), and target-awareness is just the linker
producing a different output per translation unit. Each target carries its own
applicable rule set (a rule that does not apply is skipped, not failed, so an
extension-only rule's zero match on the CLI does not abort the run), its own discovery
pass over its own bundle, its own signature (so a partial patch state across two files
is detectable and targets can version independently), and its own symbol table and
tree-shaken core subset.

A rule declares its kind (inject a block, replace a whole function, or splice a small
structural edit), its targets, its locator, its binding, and its dependencies. The
engine verifies every rule's anchors resolve before writing any of them, verify-all
then write-all, so no target is ever left half-patched by the engine itself.

A target is also reached through a **codec**, and the engine is codec-agnostic: it
operates on JS text and does not care how that text is obtained or written back. For
the extension and the webview the codec is a plain file read and write, because those
bundles are ordinary JavaScript. The CLI is the case that makes the idea explicit.
Anthropic ships it as a `bun build --compile` single-file executable: a host ELF with
the bundled JS, and a JavaScriptCore bytecode copy of the entrypoint, appended as a
trailing blob in a `.bun` section. Its codec extracts the embedded JS, hands it to the
same discovery-splice-verify engine, and repacks the binary. The repack is surgical and
in place: a full blob rebuild would reflow every region and break the bytecode's
absolute source offsets (and bloat the file), so instead the edited bytes are spliced in
place and only the module table, the offset struct, and the ELF headers are recomputed
for the byte delta. The compiled bytecode is left untouched, because it reads its string
literals from the live source buffer rather than inlining them, so a source edit takes
effect with the bytecode intact. The codec is stdlib-only and fail-closed, the same code
at synthesis time and in the end-user apply path. The point is that the CLI is not a
separate patcher: it is the one shared engine reaching a new container through a new
transport, so the container format is a pluggable codec, not a fork of the machinery.

## Surviving vendor drift

Demand 2 is the moving target. The vendor ships new bundles continually, and they
drift in two distinct ways that need two distinct responses.

**Constant minified-symbol drift, handled programmatically.** Every release
re-mangles the minified names, but the code's *structure* is stable across a routine
build. Because discovery resolves by shape, a pure rename needs no intervention at
all: the anchors still match, the prologue rebinds to the new names, and the same
source patches the new bundle. This is the common case and it is fully automatic.

**Occasional structural drift, handled with tooling.** When the vendor genuinely
refactors (splits a function, moves a call site, changes a record's shape, renames a
concept), the structural assumption an anchor rests on can break. The architecture's
job here is twofold. First, **surface it loudly**: an anchor that no longer matches
exactly once aborts the apply, so a refactor can never silently mis-patch. Second,
**make the fix cheap for Claude**: the readable source states the intent, the
fail-loud diagnostic names exactly which anchor and which symbol broke, a cross-check
pass reports every anchor's match count in one sweep without aborting (so a bump
surfaces the whole re-anchor worklist at once rather than one failure at a time), and
the `_pv_` grep plus the coverage bijection show precisely what is unresolved. Updating
the affected anchor or core primitive to the new structure, then confirming it through
the behavioral gate, is a bounded edit rather than a rewrite. The occasional
refactor is thus turned into a small, tool-assisted re-anchoring with a tight
feedback loop, not a silent breakage and not a from-scratch reauthoring.

## Discovery: resolve by shape, never by name

Discovery is the external linker: it resolves each vendor symbol to its current
minified name by a structural **anchor**, a pattern keyed on the code's shape rather
than on a name that changes every release.

An anchor pins a symbol between two **structural bookends** (a function signature, a
call-graph position, a stable behavioral string such as an environment-variable name
or a trace label) and bridges the volatile span between them with a **bounded
window**. The windows are small and per-anchor, on the order of tens to a few hundred
characters, and the bound is a guardrail, not a size to reach: it is written to
tolerate the benign drift between two features (a reordered or added property) while
failing loud on a structural change. The webview render anchor, for instance, spans
the volatile middle properties with `[^}]{0,400}`, which cannot cross a brace, so a
future property carrying an object default breaks the match rather than binding a
neighbouring site. There is no global context window and no expand-until-unique loop;
each anchor is hand-written to key on structure that is already unique.

The hard constraint is **verify-once-or-abort**: every anchor must match exactly one
place. Zero means the vendor reshaped this site, so it is re-anchored; more than one
means the pattern is ambiguous, so it is tightened. A unique-but-wrong bind is never
accepted, because that is the one silent hazard the wholesale replacements carry. The
system is, in that precise sense, limited by anchor uniqueness: a symbol that cannot
be pinned to exactly one structural site cannot be resolved. When a definition's own
shape is not unique (two vendor functions share it), the anchor keys instead on a site
where the symbol is uniquely used, for example the loader's own call to it.

Two symbol classes are located this way:

- **Code symbols.** A symbol whose NAME we bind is anchored on a signature plus a
  discriminating feature. A whole FUNCTION we replace is located by its signature plus
  a lexer-aware brace match that spans its body. The brace match is its own module with
  its own adversarial test suite, because a naive counter picks the wrong close brace
  in minified code (braces hide in strings, template literals, regex literals, and
  comments) and a truncated body can still pass `node --check`. That silent
  mistruncation is the single highest-risk failure in the machinery, so it is isolated
  and tested hardest.
- **Data symbols.** The webview's CSS-module hashes are anchored by **co-occurrence**:
  a single class prefix maps to many module hashes, so the hash is the set intersection
  of several prefixes that share the one module, a singleton or the apply aborts. The
  literal hash is never written in source; it is discovered, shape-validated, and
  emitted into the relocation prologue as a quoted constant.

The `_pv_` namespace makes the vendor dependency set a grep, not a hand-maintained
list: every vendor reference in source is `_pv_<name>`, so the dependency set is the
matches of `/_pv_\w+/`, exact and driftless. A `_pv_` reference with no registered
anchor fails loud; an anchor with no reference is dead. That bijection keeps the
source and the resolver registry from silently drifting apart, the same disease the
whole refactor exists to kill.

## The component pipeline

The recovered transcript is produced by a short pipeline, each stage authored as
readable source over the core:

- **The loader** parses the session file and, before handing records to the
  chain-builder, repairs any lineage a compaction severed. It backfills a boundary's
  pre-compaction history from the sibling file that contributes the deepest ancestry
  (cross-file fixed-point backfill), backfills a phantom logical parent that lives on
  no held record, plants marker ghosts where a compaction was crossed and how, and
  recovers a fork's off-disk source through its `forkedFrom` edge. The fork and
  cross-file passes iterate to a joint fixed point, because each can reveal work for
  the other.
- **The chain-builder** walks the lineage from the newest turn back to the root
  through the shared walk, decides whether the chain is healthy or corrupt, and emits
  the render. On a healthy chain it renders normally with a root bookend; on a corrupt
  chain (a cycle, or history stranded off the canonical walk) it resplices every saved
  message in write order under a banner. Both root checks go through the core's
  content-aware classifier, so a continuation preamble is a hard failure on either
  path, never a silent green bookend.
- **The renderer** paints the chain-builder's marker payloads as cards in the webview.
  It is a wholesale-readable replacement of the vendor's render wrap, proven
  byte-faithful to the shipped original by a two-arm harness that runs both on
  identical inputs and compares the element trees.

`forkedFrom` is a cross-session edge that most-likely disqualifies a record as the
origin: a record carrying it is a copy whose real ancestry is off disk. The loader
investigates rather than guesses (read the source, re-root onto the real parent, or
leave it for the marker). The corpus measurement behind the fork classifier is that
`forkedFrom` is the sole reliable copy marker; the session id is re-stamped on copy
and cannot discriminate.

## The exthost-to-webview seam

The extension host and the webview are two bundles with a boundary between them. The
core (`$pfg`) lives in the host. The seam is governed by one rule: the webview
consumes data, not logic.

Anything core-derived the webview needs (a marker's role and kind, the per-message
context bands) is computed once in the host, where the core runs, and stamped into
the **PFGK1 payload**, the data channel that already crosses the boundary. The
renderer reads the role from the payload; it never re-derives it from a copied
primitive. So no core logic runs twice and no core concept is re-implemented across
the boundary.

Detecting *that* a message is a marker is done out of band, by its unspoofable ghost
uuid (a `pfgk-` prefixed id the system assigns and a user cannot forge), never by its
content. Content-based detection would be in-band and spoofable: an ordinary message
whose text began with the payload envelope would render as a fake marker. So the
renderer identifies a marker through the core's uuid predicate and reads the payload
only for a confirmed one. In-band detection is the forced necessary evil, used only
where no out-of-band signal exists (the continuation-preamble content sniff is the one
genuinely forced case; a ghost uuid is not).

That predicate is the irreducible residue a target holds from the core, and it is a
*function*, not a constant. The linker emits each target's minimal core closure into
its prologue, from the one core, member-type-agnostic: a linked function is as natural
as a linked constant. It is not the whole block (that is waste) and not a hand-copied
literal (that is duplication); the engine does the propagation. Because this is a
general linker, any future target draws its core closure the same way and can never be
pushed toward re-implementing the core: the pull to centralize is enforced
structurally, not by discipline.

## The render and severity model

The terminal cards carry a three-tier severity palette, and the tiers are distinct on
purpose because two of them can co-occur:

- **success** (cyan): the origin was reached; the bookend reads "reconstructed".
- **hard failure** (red): `TRANSCRIPT INCOMPLETE`, the root was not found. Red is
  reserved for this. A conversation's beginning is gone.
- **irregularity** (amber): `CHAIN CORRUPT · RESPLICED`, the chain's threading was
  unreliable and had to be respliced. The data is present but resequenced. This is a
  lesser tier than a hard failure, so it takes the amber irregularity color and its
  own badge, never the red of `INCOMPLETE`. A corrupt chain that also loses its root
  fires both cards, and distinct colors let both severities read at once.

Every terminal card carries the reconstruction's wall-clock timing, on the failure
path as much as the success path, because "searched N files and still no root" is if
anything the more useful place for it. Timing is display-only telemetry, explicitly
permitted by the determinism clause; it never feeds the set or order of recovered
records.

Trust is a property of the path, not only the terminus. A walk can reach a
structurally perfect origin but cross an **unverified seam** to get there: a
positional reattachment made when a phantom logical parent could not be found
anywhere. Reaching an origin across such a guess does not license a green bookend,
because the guess could join a different conversation. The default policy is
conservative: any unverified seam on the live path poisons the promotion and fires
the marker. Where the seam can be *proven* to join the same conversation (an in-file
reattachment onto this session's own, `forkedFrom`-free lineage, in a file with a
single own-origin), it promotes to a trusted bridge. The gradation lives entirely in
the classification and collapses to the binary at proof: a promotion happens only
when proven, never on a probability, so no soft middle reaches the render.

## Deployment: the prebuilts

The full toolchain (discovery, the linker, the anchor registry) is how a patch is
*authored and synthesized* against a version. It is not what a user runs. Each
supported release ships as a **prebuilt**: a thin, stdlib-only Python script that
reproduces the exact patched bundle for that one version, with no dependency on the
engine, no discovery pass, and no third-party packages.

The prebuilt is generated by diffing the fully-patched bundle against its pristine
backup and turning each changed region into a self-locating splice. For each edit, the
extractor **widens the surrounding context outward until the pre-patch substring is
unique in the file**, and that unique window becomes the splice's anchor; the widening
has a fixed maximum, empirically sufficient to make every real patch anchor uniquely.
Each splice is an `(old, new, expected_count)` triple, and the thin apply finds exactly
`expected_count` occurrences of `old` and replaces them, a fail-closed count gate. Where
identical context recurs at several sites that all take the same edit, they collapse
into one replace-all (`expected_count` = K); a region whose colliding sites take
DIFFERENT edits is a loud refusal, never a wrong splice; and the extractor self-checks
by applying its own emitted splices to the pristine bytes and requiring a byte-exact
reproduction of the patched bundle before it emits anything.

This is a SECOND anchor system, distinct from discovery's, and the difference is the
point. Discovery's anchors are version-agnostic and resolve by shape, because the
synthesis toolchain must patch a bundle whose names it has never seen. The prebuilt's
anchors are version-pinned literal context, because a prebuilt targets exactly one
release: it need not survive drift, only relocate its edits unambiguously within the
one bundle it was cut for. Anchor uniqueness is still the hard limit on this side too,
a region that cannot be made unique within the cap either becomes a replace-all or
fails loud. A per-version signature lets a prebuilt detect its own staleness (an older
signature than the current one means it is applying superseded patches) and refuse to
mis-upgrade. So deployment stays small and dependency-free for the many consumers, while
the heavyweight, drift-resilient synthesis stays in the toolchain used by the few who
cut a new version.

## The verification model

Correctness is guarded in layers, each covering a failure class the others cannot
see:

- **Unit contracts** over the core primitives and the render roles, RED-first: each
  test is written to fail on the specific bug it guards, so it is not vacuous. A
  content-blind origin check or a parent-only reachability walk cannot make the
  invariant assertions pass.
- **The behavioral gate** is the necessary-and-sufficient proof that the patched
  bundle *binds*. `node --check` proves the output parses; it says nothing about
  whether the core is in scope, the aliases resolve, or the required builtins load at
  run time. The gate applies the real engine to a real pristine bundle, extracts the
  actually-injected code, and runs it over a real corpus. If no pristine bundle is
  present it skips loudly; it never silently passes.
- **The faithfulness harness** proves the readable renderer produces the byte-exact
  output of the shipped original, so the readable lift changed no rendered byte.
- **Determinism**: the reconstruction is a pure function of the input corpus bytes.
  Sibling order is by filename, not `readdir` order or mtime; the spine is ordered by
  write index, not wall clock. A reconstruction that varies by machine is a bug of the
  same class as a false root.

The recurring discipline across all of it: investigate ambiguity rather than guess,
cover every branch of a definition regardless of how rare it is (the prose may note
the base rate, the code may not), and prefer a loud failure over a silent degradation
everywhere the two compete.

## Design principles, distilled

- **One concept, one definition.** Duplication is a defect. If the architecture makes
  defining-once hard, the architecture is the defect, and the fix is to build the
  thing that makes the right choice natural (the codegen, the linker), never to accept
  the copy.
- **Clean source, engine-made artifact.** Every ugliness the minified target forces
  (relocated names, discovered values, the duplication a bundle boundary would
  otherwise demand) is produced by the engine at build time, never written into the
  source.
- **Resolve by shape when you do not own the name.** Anchoring on a minified name is
  anchoring on sand. Anchor on structure, verify exactly once, abort loud on zero or
  many.
- **No soft middle.** Reach the origin or fire the marker. Every graded internal
  measure collapses to that binary at proof, never at a probability.
- **Fail loud, never degrade silently.** Guard the outcome invariant, not a list of
  known causes. A partial, inconsistent, or undecidable state is surfaced loudly and
  routed to recovery or the marker, never quietly treated as success.

---

## Current state (status, not architecture)

Everything above is the timeless design. This section is the status snapshot and
will change as the work lands; keep it out of the sections above.

- **Extension-host target**: complete and green. The SSOT core, the loader, the
  chain-builder, and the telemetry splice are landed with the invariant fixes (the
  content-aware root verdict, the fork classifier and complete-fork recursion, the
  fork/cross-file joint fixed point, the seam-trust proof-promotion, the divergent-lpu
  loud detect). The readable-naming pass and the per-target membership check are in.
  Unit contracts, the behavioral gate, and determinism are green.
- **Webview target**: in build. The readable renderer is proven byte-faithful; the
  CHAIN CORRUPT card is on the amber tier; the CSS-module hashes are discovered by
  co-occurrence; the context gutter, the engine webview target, the per-target core
  linker, and the payload role-stamping are being landed as one cluster, brought for
  ratification when green.
- **CLI target (v2)**: scaffolded, not shipped. The container codec (extract and
  repack the entrypoint) exists and is proven to execute source edits; the target's
  anchors and behavioral gate are deferred to the v2 cut.
- **Queued**: the repository reorganization (descriptive slugs, a central top-level
  core), and the symbol-table unification (fold the coverage and membership bijections
  into one per-target link check).
