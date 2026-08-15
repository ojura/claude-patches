"""
adapters: the target backends the I/O-free engine runs behind. Same engine, two
transports: `extension` (plain file read/write + node --check) and, at v2.0, `cli`
(bun_handler.extract_js -> engine -> repack_with_js + execute-assert).

The two parse-check with different tools, and they are not interchangeable. The
extension bundle is run by Node, so `node --check` is both correct and the right
runtime to prove it against. The CLI bundle is run by bun and already ships
`using` declarations, which Node 22 cannot parse: `node --check` reports a syntax
error on pristine input there, so the cli transport checks with bun instead (see
util/patch_streaming_thinking.py:bun_syntax_check).
"""
