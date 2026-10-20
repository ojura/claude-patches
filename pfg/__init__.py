"""
pfg: the patch-apply engine that compiles the readable SSOT source (src/) into a
patched bundle, for the extension (v1) and the native CLI (v2, via bun_handler),
and the reader of a native build's modules as syntax and bindings.

Layout:
  jslex      general minified-JS lexer + whole-function span extractor
  anchors    the structural anchor registry (data: the per-version and per-target
             anchors a maintainer updates)
  discovery  find_one/verify-once, discover, derive-deps, coverage (engine)
  rules      the rule definitions (label, kind, targets, ...) as data
  engine     discover -> verify-all -> write-all, I/O-free
  bundle     a native build's modules, read out of the binary by bun_handler, as
             syntax and bindings through the node helper in js/
  js         facts.mjs, the node helper that parses each module with the
             TypeScript compiler vendored in js/vendor, and its tests
  REVISION   the version of what bundle extracts (see bundle's docstring)
  adapters   extension (file r/w + node --check) and cli (bun_handler) backends
  __main__   the CLI (discover, span, crosscheck, apply)
"""
