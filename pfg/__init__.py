"""
pfg has two parts. The patch engine compiles the readable patch code in src/, the single source of truth
for the patches, into a patched bundle of the IDE extension. The module reader reads the JavaScript
modules of a native build and answers questions about their syntax and bindings.

Layout:
  jslex      a general-purpose lexer for minified JavaScript, and find_function_span, which finds the span
             of a whole function.
  anchors    the structural anchors, as data: for each target, the regular expressions that find the
             vendor's minified names. A maintainer updates them when a new version changes the code.
  discovery  discover finds each vendor name and exits when an anchor matches zero times or more than
             once. derive_deps lists the vendor names that the code in src/ uses, and coverage checks that
             those names and the anchors correspond one to one.
  rules      the patch rules (label, kind, targets, ...), as data.
  engine     finds the vendor names, checks that every anchor of every rule resolves, and only then
             applies the edits. It takes and returns the target's text, and the adapters read and write
             the target file.
  bundle     reads a native build's modules out of the binary with bun_handler, and answers questions
             about their syntax and bindings through the node helper in js/.
  values     the constant values of a native build's bindings, and the check that every use of a value
             leaves it unchanged.
  js         facts.mjs, the node helper that parses each module with the TypeScript compiler vendored in
             js/vendor, and its tests.
  REVISION   the number that a maintainer raises with every change to what bundle and values extract (see
             bundle's docstring).
  adapters   the backend for the extension, which reads and writes the extension's file and checks the
             result with node --check.
  __main__   the command line, with the commands discover, span, crosscheck and apply.
"""
