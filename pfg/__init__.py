"""
pfg: the patch-apply engine that compiles the readable SSOT source (src/) into a
patched bundle, for the extension (v1) and the native CLI (v2, via bun_handler).

Layout (concerns separated, so the enforcer of SSOT is not itself a grab-bag):
  jslex      general minified-JS lexer + whole-function span extractor
  anchors    the structural anchor registry (DATA; the per-version/per-target
             surface a maintainer re-anchors)
  discovery  find_one/verify-once, discover, derive-deps, coverage (engine)
  rules      the rule definitions (label, kind, targets, ...) as data
  engine     discover -> verify-all -> write-all, I/O-free
  adapters   extension (file r/w + node --check) and cli (bun_handler) backends
  __main__   the CLI (discover, span, crosscheck, apply)
"""
