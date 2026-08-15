# Making patches

This guide documents the patching patterns used by the utilities in this directory. The central rule is to fail closed: identify a patch site structurally, verify every assumption, make all edits in memory, and write the result only after every check succeeds.

## Choose the patching layer

There are three distinct layers:

1. **Standalone Bun patch scripts** modify the JavaScript embedded in a Bun-compiled native Claude executable.
2. **Declarative `pfg` rules** patch extension and webview JavaScript through registered anchors and rules.
3. **Prebuilt splices** reproduce an already-verified patch as exact literal replacements.

These layers are complementary, not interchangeable. A standalone `patch_*.py` file is not automatically registered with `pfg`, and the prebuilt generator does not discover native-binary patch scripts.

## Bun binary handler

### Path and purpose

`util/bun_handler.py` is the canonical stdlib-only handler for the Linux x64 Bun-compiled native executable. It:

- Parses little-endian ELF64.
- Locates the `.bun` section and Bun entrypoint module.
- Extracts the bundled JavaScript.
- Replaces source while leaving JavaScriptCore bytecode untouched.
- Rewrites Bun module ranges, offsets, section sizes, ELF section offsets, and affected segment sizes.
- Fails closed on unsupported layouts.

Control-flow source edits work despite the preserved bytecode. The executable control-flow gate in `util/test_bun_handler.py:171-249` verifies that premise.

### Public API

Defined in `util/bun_handler.py:716-779`:

```python
can_handle(data) -> bool
extract_js(data) -> bytes
repack_with_js(data, new_js) -> bytes
repack_unchanged(data) -> bytes
detect_format(data) -> str
```

Lower-level types include:

```python
BunFormatError
BunImage
Elf64
```

A patch that changes only the entrypoint JavaScript should use `extract_js()` and `repack_with_js()` rather than the internal multi-module APIs.

### Encoding contract

Decode and encode symmetrically with surrogate escaping:

```python
js = bun_handler.extract_js(data).decode(
    "utf-8", errors="surrogateescape"
)

# Patch js in memory.

new_data = bun_handler.repack_with_js(
    data,
    js.encode("utf-8", errors="surrogateescape"),
)
```

The bundled source may contain non-UTF-8 bytes. Using ordinary UTF-8 error handling can silently corrupt them.

## Standalone Bun patch scripts

The native-binary patch scripts repeat a small, deliberately strict helper API:

- `find1(pattern, label)` requires exactly one structural regex match.
- `splice(old, new, label, expected=1)` requires an exact occurrence count before replacement.
- Optional `sub(template, **names)` fills readable JavaScript templates with structurally discovered minified identifiers.
- The script extracts once, applies all transformations in memory, and repacks once.

`util/patch_statusline_auth_org.py:141-219` is the best compact reference.

The essential behavior is:

```python
matches = list(re.finditer(pattern, js))
if len(matches) != 1:
    raise SystemExit(...)

count = js.count(old)
if count != expected:
    raise SystemExit(...)
js = js.replace(old, new, expected)
```

Minified names must be captured from structural context rather than hardcoded.

### Structural matching strategy

Do not identify a target only by a minified name or a common body shape:

```javascript
function a(){return!1}
```

Both will drift or collide. Instead:

1. **Identify the feature from a stable callsite.**
   Use durable property names, event names, endpoint fragments, configuration keys, or user-visible literals. Capture the current minified identifier from that context.

2. **Locate the captured declaration.**
   Search for that exact discovered identifier, not a generic function with a similar body.

3. **Use the old body as a state guard.**
   Require the declaration to contain precisely the code expected in an unpatched build.

4. **Replace the narrowest safe span.**
   Preserve the discovered identifier, parameters, and unrelated surrounding code.

5. **Discover every minified dependency.**
   Capture dependencies structurally and substitute them into readable replacement code. Never paste aliases from one bundle version into a release-tolerant patch.

6. **Use known-good behavior.**
   Copy replacement logic from readable source or a verified bundle. Do not infer a function body from its semantic name.

For functions with nested lexical hazards, use the tested `pfg.jslex.find_function_span` API rather than a general brace-counting regex. Adversarial cases are covered by `util/test_jslex.py`.

### Generic standalone shape

```python
data = open(src, "rb").read()
js = bun_handler.extract_js(data).decode(
    "utf-8", errors="surrogateescape"
)

callsite = find1(
    CALLSITE_REGEX_WITH_STABLE_ANCHORS,
    "feature callsite",
)
function_name = callsite.group("fn")

declaration = find1(
    rf"function {re.escape(function_name)}\((?P<args>[^)]*)\)"
    rf"\{{(?P<body>EXPECTED_OLD_BODY)\}}",
    "feature function declaration",
)

old = declaration.group(0)
new = (
    f"function {function_name}({declaration.group('args')}){{"
    f"VERIFIED_REPLACEMENT_BODY"
    f"}}"
)
splice(old, new, "patch feature function")

new_data = bun_handler.repack_with_js(
    data,
    js.encode("utf-8", errors="surrogateescape"),
)
```

Before writing `new_data`, make sure every discovery and replacement has already passed. Do not leave a partially modified binary after a later check fails.

## Declarative `pfg` toolkit

The rule-driven toolkit uses explicit registries rather than filename discovery.

Observed APIs include:

- `pfg.engine.apply(js, target="extension")`
- `pfg.engine.SIGNATURE`
- `pfg.engine.patch_state`
- `pfg.engine.RULES`
- `pfg.rules.Rule`
- `pfg.anchors.Anchor`
- `pfg.anchors.anchor_for`
- `pfg.discovery.discover`
- `pfg.discovery.derive_deps`
- `pfg.discovery.coverage`
- `pfg.discovery.SRC_FILES`
- `pfg.jslex.find_function_span`
- `pfg.jslex.enclosing_function_start`

### Rule forms

A splice rule, from `util/test_target_aware.py:173-190`:

```python
Rule("toy splice", "splice", frozenset({"extension"}), {
    "anchor": "_pv_x",
    "verify": {"_pv_x": 1},
    "edits": [{"group": 2, "template": "BAZ"}],
})
```

A source-injection rule, from `util/test_target_aware.py:220-230`:

```python
Rule(
    "inject render body",
    "inject",
    frozenset({"webview"}),
    {"src": "render.js", "at_anchor": "_pv_wvFactory", "pfg": False},
)
```

Templates may refer to discovered identifiers with Python percent-template notation:

```python
"%(_pv_name)s"
"%(g8)s"  # Preserve an anchor capture.
```

### Registration and discovery

1. `engine.RULES` is an ordered list of `Rule` instances.
2. `anchor_for("_pv_name")` resolves an anchor from the anchor registry.
3. Each `Anchor` contains target-specific regexes.
4. Discovery requires exactly one match and captures current minified identifiers.
5. `derive_deps(target)` derives `_pv_*` dependencies from injected source.
6. `coverage(target)` rejects dependencies without an anchor or builtin binding.
7. `SRC_FILES` declares source consumers by target.
8. `util/pfg-codegen.py:44-47` maps targets to `$pfg` consumers and emits only their usage-derived subset.

`engine.apply`:

- Stamps a version signature at every changed site.
- Derives the expected site count from registered rules.
- Treats fully current output as idempotent.
- Distinguishes clean, patched, stale, and partial states.
- Refuses partial or mixed-version output.

See `util/test_target_aware.py:244-272`.

### Generic declarative shape

```python
Rule(
    "patch feature behavior",
    "splice",
    frozenset({"extension"}),
    {
        "anchor": "_pv_featureFunction",
        "verify": {
            "_pv_featureFunction": FUNCTION_NAME_CAPTURE_GROUP,
        },
        "edits": [
            {
                "group": OLD_BODY_CAPTURE_GROUP,
                "template": "VERIFIED_REPLACEMENT_BODY",
            },
        ],
    },
)
```

The corresponding anchor should:

- Use a semantic `_pv_*` name.
- Capture the current minified identifier from stable feature structure.
- Capture the old code as a separate edit group.
- Require a unique target-specific match.
- Include enough surrounding grammar to prevent collisions.

Each edit adds a signed patch site, so site-count tests must be updated. If replacement code introduces `_pv_*` references, dependency derivation and coverage need matching anchors or builtins. If it introduces `$pfg.*` references, `pfg-codegen.py` must expose the referenced member to the relevant source consumer.

## Representative native patches

### `patch_statusline_auth_org.py`

Best small reference for a new standalone patch:

- Stable property and string literals identify two sites.
- Minified function and argument names are captured structurally.
- Every discovery and replacement requires exactly one occurrence.
- Readable JavaScript is injected through placeholder substitution.
- The final binary is repacked once.

Relevant sections:

- Matching conventions: `util/patch_statusline_auth_org.py:64-74`
- Helpers: `util/patch_statusline_auth_org.py:95-104` and `:157-175`
- Structural discovery: `util/patch_statusline_auth_org.py:177-209`
- Repack: `util/patch_statusline_auth_org.py:211-219`

### `patch_streaming_thinking.py`

Best reference for cross-release minifier tolerance:

- `discover_names(js)` resolves numerous minified locals from structural shapes.
- Every lookup is exactly-one or abort.
- Discovered names are substituted into readable replacement bodies.
- Literal anchors are used only with stable surrounding structure.
- Related writer and renderer code is patched together.

Relevant sections:

- Discovery design: `util/patch_streaming_thinking.py:106-121`
- `discover_names`: `util/patch_streaming_thinking.py:281-439`
- Patch/repack pipeline: `util/patch_streaming_thinking.py:442-478` and `:1013-1019`

### `patch_context_read_breakdown.py`

Best broad multi-site reference:

- Stable properties and user-facing literals anchor searches.
- Drifting React, component, and cache aliases are discovered structurally.
- Repeated matches must agree instead of selecting an arbitrary occurrence.
- Data collection and rendering are changed together.
- All changes accumulate before a single repack.

Relevant sections:

- Matching policy: `util/patch_context_read_breakdown.py:76-90`
- Helpers: `util/patch_context_read_breakdown.py:348-382`
- Structural discovery: `util/patch_context_read_breakdown.py:384-477`
- Data and rendering patches: `util/patch_context_read_breakdown.py:479-727`
- Repack: `util/patch_context_read_breakdown.py:759-767`

## Prebuilt splice layer

### Extractor

`util/extract_splices.py` exposes:

```python
find_diff_regions(a, b)
find_next_long_match(a, b, ...)
widen_to_unique(a, start, end)
extract(unpatched_path, patched_path)
```

Splice schema:

```python
{
    "offset": int,
    "old": str,
    "new": str,
    "expected_count": int,
}
```

The extractor:

- Widens changed regions to unique anchors.
- Supports uniform multi-site edits with `expected_count=N`.
- Refuses non-uniform collisions.
- Simulates all emitted splices and requires byte-for-byte reproduction.
- Preserves arbitrary bytes through `surrogateescape`.

### Prebuilt generator

`util/build-prebuilt.py` diffs an already-patched installation against backups and synthesizes a literal-splice apply script. It is not a patch registry.

Its fixed target list at `util/build-prebuilt.py:237-242` is:

```text
extension.js
webview/index.js
webview/index.css
```

It does not automatically include native Bun binaries or standalone native patch scripts.

## Verification checklist

### Hermetic matching tests

Build a fixture containing:

- The intended function or expression.
- A structurally similar decoy.
- The stable caller or feature structure.
- A duplicate caller variant.

Assert:

1. The intended site changes.
2. The decoy remains byte-identical.
3. Zero anchor matches fail loudly.
4. Multiple anchor matches fail loudly.
5. An unexpected old body fails loudly.
6. No release-specific minified identifier is hardcoded.

### Syntax gate

Run `node --check` on re-extracted patched JavaScript. Existing parse-only gates are in:

- `util/build-prebuilt.py:200-211`
- `util/pfg-codegen.py:197-215`

Syntax validation is necessary but not sufficient.

### Behavioral gate

Follow `util/test_behavioral.py`:

- Extract the actually patched function.
- Evaluate it under Node with mocked runtime dependencies.
- Test both the behavior that should change and behavior that must remain unchanged.

This proves the patch changed semantics as intended rather than merely producing valid JavaScript.

### Bun repack gates

Follow `util/test_bun_handler.py`:

- Re-extracted JavaScript equals the patched JavaScript.
- Binary size delta equals source size delta.
- Two identical patch runs produce identical bytes.
- The patched binary parses through `BunImage`.
- An observable CLI workflow exercises the changed branch.
- Control-flow changes retain an executable control-flow gate.

### Idempotency and partial-state handling

For declarative rules, use the engine’s per-site signature and `patch_state` behavior. Do not add a weaker check for a signature appearing somewhere in the bundle.

For standalone scripts, either:

- Match existing scripts and fail when the expected old code is absent, or
- Add an exact patch marker and distinguish clean, fully patched, and inconsistent states.

Do not treat an absent old body as success merely because part of the replacement appears elsewhere.

## Integration checklist

Before considering a patch shipped, verify all applicable wiring:

- The patch script is invoked by the patch orchestration layer, or the rule and anchor are registered explicitly.
- Target and expected-site registries include the new rule.
- Dependency coverage passes for every introduced `_pv_*` or `$pfg.*` reference.
- Prebuilt generation includes the target if the patch must ship through that path.
- Tests cover clean, duplicate, stale, partial, and already-patched states.

Creating a standalone `patch_*.py` file alone does not make it part of the shipped patchset.
