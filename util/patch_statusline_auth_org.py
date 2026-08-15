#!/usr/bin/env python3
"""Expose the inference identity (auth source, a short token preview, and the
live org id) to the statusline command on the bun-packed native Claude CLI.

The problem
===========

When the CLI authenticates with an env setup-token (``CLAUDE_CODE_OAUTH_TOKEN``)
instead of the interactive keychain login, two things the operator cares about
are invisible:

  * The statusline's quota (``rate_limits``) is sourced from the logged-in
    account, not from the org the setup-token actually bills, so the percentages
    on screen can describe a different org than the one serving inference.
  * The org id of the billed account (the ``anthropic-organization-id`` response
    header) is never captured anywhere in the binary, so nothing downstream can
    show which org is in use.

The statusline command cannot recover any of this itself: the harness scrubs
``CLAUDE_CODE_OAUTH_TOKEN`` from every spawned subprocess, and the org header is
only ever seen inside the harness.

What this patch adds
====================

Three NEUTRAL facts on the statusline stdin JSON. This patch carries no display
policy; the statusline script decides what (if anything) to show:

  * ``auth_source``        -- how the harness authenticated, mirroring
                             ``getAuthTokenSource()``'s env-var branch:
                             ``"CLAUDE_CODE_OAUTH_TOKEN"`` /
                             ``"ANTHROPIC_AUTH_TOKEN"`` / ``null`` (keychain login,
                             API key, none -> the default, left uncluttered).
  * ``auth_token_preview`` -- first 17 chars of the active env-var token: the
                             ``sk-ant-...`` type tag plus 4 entropy chars. A
                             fingerprint that cannot reconstruct the 108-char
                             token. ``null`` when not an env-var token source.
  * ``organization_id``    -- the org UUID from the live inference response header,
                             so the org shown is the org whose quota is shown.

Patch sites
===========

  A. ``extractRawUtilization`` entry (the per-response 5h/7d header reader):
     stash ``anthropic-organization-id`` off the SAME ``Headers`` object that
     feeds the rate-limit utilizations, onto ``globalThis.__aoiOrg``. Sourcing
     org and quota from one response keeps them consistent by construction.
  B. ``buildStatusLineCommandInput`` payload object (the ``subscription_type``,
     ``rate_limits_available``, ``rate_limits`` object piped to the command):
     spread in ``auth_source`` / ``auth_token_preview`` / ``organization_id``.

Why ``auth_source`` is computed inline (not via ``getAuthTokenSource()``)
========================================================================

``getAuthTokenSource()`` (minified ``AI()``) is a top-level function in another
module closure. Calling it from the payload builder would be a cross-module
reference whose reachability cannot be verified by string-matching at apply time;
if it is out of scope, the patched binary throws at RENDER time. ``process.env``
is always reachable, so we mirror the function's two env-var branches inline. The
keychain (``"claude.ai"``) and FD/CCR/apiKeyHelper labels are deliberately not
reproduced: those collapse to ``null``, which the script reads as "default, show
nothing", which is the desired behavior anyway.

Conventions
===========

Injected code is written readably (multi-line, commented), never minified.
Stable anchors are the property-name / string literals
(``["five_hour","5h"]``..., ``subscription_type:``, ``rate_limits_available:``);
the drifting single-letter locals (the ``Headers`` param, the
``subscription_type`` getter) are discovered by structural regex and asserted to
match exactly once, so a future re-minify fails loud rather than mis-applying.
``globalThis.__aoiOrg`` bridges the two sites (they live in different closures),
matching the existing patches' ``globalThis.__pcb*`` style.

Usage
=====

::

    util/patch_statusline_auth_org.py <input-binary> [-o <output>]

Output defaults to ``<input>.aoi``. Targets the bun ``.bun``-section ELF form via
``util/bun_handler`` (Linux x64).
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bun_handler  # noqa: E402  (sys.path insert must precede this)


def sub(template, **names):
    """Fill @NAME@ placeholders in a readable JS template with discovered
    minified identifiers."""
    out = template
    for key, val in names.items():
        out = out.replace('@' + key + '@', val)
    leftover = re.search(r'@[A-Z0-9_]+@', out)
    if leftover:
        raise SystemExit(f"[template] unresolved placeholder {leftover.group(0)}")
    return out


# --- readable injected JS (filled with discovered names at apply time) ---------

# Site A: stash the org id off this response's headers. @HV@ is the Headers param.
CAPTURE = (
    '/* aoi: stash this response\'s org id. anthropic-organization-id rides the\n'
    '   same Headers as the rate-limit utilizations read just below, so the\n'
    '   statusline shows the org whose quota it shows. globalThis bridges to the\n'
    '   payload builder, which lives in another bundle closure. Last write wins;\n'
    '   a response without the header keeps the previous value. */\n'
    'try{globalThis.__aoiOrg=@HV@.get("anthropic-organization-id")??globalThis.__aoiOrg;}catch(aoiE){}'
)

# Site B: three neutral identity facts spread into the statusline payload object.
EXPOSE = (
    '/* aoi: neutral identity facts; the statusline SCRIPT owns what to show.\n'
    '   auth_source mirrors getAuthTokenSource()\'s env-var branch inline --\n'
    '   process.env is always reachable here, whereas a cross-module call to the\n'
    '   minified getAuthTokenSource could be out of scope and throw at render.\n'
    '   auth_token_preview = "sk-ant-..." type tag + 4 entropy chars (cannot\n'
    '   reconstruct the full token). organization_id is captured at site A. */\n'
    '...(() => {\n'
    '  const aoiSource = process.env.CLAUDE_CODE_OAUTH_TOKEN ? "CLAUDE_CODE_OAUTH_TOKEN"\n'
    '    : process.env.ANTHROPIC_AUTH_TOKEN ? "ANTHROPIC_AUTH_TOKEN"\n'
    '    : null;\n'
    '  const aoiTok = aoiSource ? process.env[aoiSource] : null;\n'
    '  return {\n'
    '    auth_source: aoiSource,\n'
    '    auth_token_preview: (typeof aoiTok === "string" && aoiTok.length > 0) ? aoiTok.slice(0, 17) : null,\n'
    '    organization_id: (typeof globalThis.__aoiOrg === "string" && globalThis.__aoiOrg.length > 0) ? globalThis.__aoiOrg : null,\n'
    '  };\n'
    '})(),'
)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        print(f"usage: {sys.argv[0]} <input-binary> [-o <output>]")
        sys.exit(2)
    src = sys.argv[1]
    if '-o' in sys.argv:
        dst = sys.argv[sys.argv.index('-o') + 1]
    else:
        dst = src + '.aoi'

    data = open(src, 'rb').read()
    js = bun_handler.extract_js(data).decode('utf-8', errors='surrogateescape')
    print(f"input:        {src} ({len(data)} bytes)")
    print(f"JS extracted: {len(js)} bytes")

    def splice(old, new, label, expected=1):
        nonlocal js
        cnt = js.count(old)
        if cnt != expected:
            raise SystemExit(
                f"[{label}] anchor count {cnt} != {expected}; refusing to patch.\n"
                f"           anchor: {old[:140]!r}"
            )
        js = js.replace(old, new, expected)
        print(f"  [{label}] applied ({len(new) - len(old):+d} bytes)")

    def find1(pattern, label):
        ms = list(re.finditer(pattern, js))
        if len(ms) != 1:
            raise SystemExit(
                f"[discover] {label}: expected 1 match, got {len(ms)}\n"
                f"           pattern: {pattern[:160]!r}"
            )
        return ms[0]

    print("\n--- A: capture org from the per-response headers ---")
    # extractRawUtilization: NAME(e){let t={};for(let[n,r]of[["five_hour","5h"],
    #   ["seven_day","7d"],["seven_day_overage_included","7d_oi"],["overage","overage"]]){...}
    # The 4-pair name/abbrev array is stable data; the function name, Headers param,
    # result var and loop vars are minifier-drift, discovered + asserted-unique here.
    m = find1(
        r'([A-Za-z0-9_$]+)\((\w+)\)\{let \w+=\{\};for\(let\[\w+,\w+\]of'
        r'\[\["five_hour","5h"\],\["seven_day","7d"\],'
        r'\["seven_day_overage_included","7d_oi"\],\["overage","overage"\]\]\)\{',
        'extractRawUtilization (per-response header read)',
    )
    fn, hv = m.group(1), m.group(2)
    head = fn + '(' + hv + '){'
    if not m.group(0).startswith(head):
        raise SystemExit('[A] extractRawUtilization head did not start at the function body')
    print(f"  fn: {fn}; Headers var: {hv}")
    new_a = head + sub(CAPTURE, HV=hv) + m.group(0)[len(head):]
    splice(m.group(0), new_a, 'A capture org -> globalThis.__aoiOrg')

    print("\n--- B: expose auth_source / auth_token_preview / organization_id ---")
    # buildStatusLineCommandInput payload object. subscription_type's getter is a
    # drifting local; rate_limits_available is the stable property name we inject
    # before. The combination is unique to the statusline payload.
    m = find1(
        r'subscription_type:(\w+)\(\),rate_limits_available:',
        'statusline payload object (subscription_type + rate_limits_available)',
    )
    old_b = m.group(0)
    tail = 'rate_limits_available:'
    if not old_b.endswith(tail):
        raise SystemExit('[B] payload anchor did not end at rate_limits_available:')
    new_b = old_b[:-len(tail)] + EXPOSE + tail
    splice(old_b, new_b, 'B expose auth_source/auth_token_preview/organization_id')

    # ------------------------------------------------------------------
    new_data = bun_handler.repack_with_js(
        data, js.encode('utf-8', errors='surrogateescape')
    )
    print(f"\nfinal JS: {len(js)} bytes")
    print(f"binary:   {len(new_data)} bytes (delta {len(new_data) - len(data):+d})")
    open(dst, 'wb').write(new_data)
    os.chmod(dst, 0o755)
    print(f"wrote {dst}")


if __name__ == '__main__':
    main()
