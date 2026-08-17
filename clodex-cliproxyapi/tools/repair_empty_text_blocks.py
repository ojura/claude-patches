#!/usr/bin/env python3
"""Repair Claude Code transcripts that carry empty text content blocks.

Sessions routed through CLIProxyAPI to an OpenAI Responses-API model (assistant
message ids beginning `resp_`) sometimes record a content block of
`{"type":"text","text":""}`. Two things produce it:

  - Translation artifact. An empty text block sits between a thinking block and
    the real text of the same message, which still carries its text and its tool
    calls. Nothing is lost while the session stays on the proxy.
  - Empty model turn. The whole message is one empty text block with
    `stop_reason` `end_turn` and a few output tokens. Claude Code detects this
    and injects "[Your previous response had no visible output. Please continue
    and produce a user-visible response.]"; the following turn recovers.

CLIProxyAPI accepts both. api.anthropic.com rejects them with
`400 messages: text content blocks must be non-empty`. Every request replays the
whole transcript, so one such block anywhere in the file makes the session
unresumable on a Claude model, and the 400 arrives before any inference.

With --fix this replaces the empty string with a placeholder. Lines that need no
repair are copied byte for byte, the rewritten file is verified before it
replaces the original, and the original is backed up by default.

Empty *thinking* blocks occur in the same transcripts and are far more common;
their `signature` holds OpenAI's encrypted reasoning payload. Two repaired
sessions (8ee68baf, 11d44c34, 2026-08-16) resumed on Opus with 294 and 136 such
blocks untouched, so this reports them and changes nothing. Whether some larger
number or a different shape is rejected is untested.

Usage:
    repair_empty_text_blocks.py SESSION_OR_PATH...          # report, then offer to fix
    repair_empty_text_blocks.py --all                       # every transcript
    repair_empty_text_blocks.py SESSION_OR_PATH... --fix    # repair without asking
    repair_empty_text_blocks.py SESSION_OR_PATH... --check  # report only, never ask
    repair_empty_text_blocks.py --selftest

A target is a transcript path, a bare session UUID resolved under
~/.claude/projects, or a directory of transcripts.

When anything is found, the report ends with a [Y/n] prompt and repairs on
confirmation. Without a terminal on stdin, and with --check or --json, nothing
is asked and nothing is written.

Exit 0 = nothing to repair, or everything found was repaired and verified.
Exit 1 = repairable blocks were left in place (declined, --check, or no
         terminal to ask at).
Exit 2 = a file could not be read, repaired, or verified.
"""
import argparse
import copy
import glob
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

DEFAULT_PLACEHOLDER = "(empty)"
BACKUP_SUFFIX = ".bak-empty-text-"

# Substrings that indicate a repair candidate without parsing the file. Used
# only by --all, where parsing every transcript in full is wasteful. A
# whitespace-only text block does not match these and is therefore only found
# when a file is named explicitly.
QUICK_NEEDLES = (b'"text":""', b'"text": ""')


class Unrepairable(Exception):
    """A line holds an empty text block that cannot be rewritten safely."""


# --- inspection ------------------------------------------------------------


def is_empty_text(block):
    return (
        isinstance(block, dict)
        and block.get("type") == "text"
        and not (block.get("text") or "").strip()
    )


def content_blocks(record):
    """Yield the content blocks of a user or assistant record."""
    if record.get("type") not in ("user", "assistant"):
        return
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def inspect(record):
    """Classify one record. Returns (repairable, informational) counts."""
    repairable = []
    info = []
    message = record.get("message")
    role = message.get("role") if isinstance(message, dict) else None

    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and not content.strip():
            info.append("%s: empty string content" % role)
        elif isinstance(content, list) and not content:
            info.append("%s: empty content array" % role)

    for block in content_blocks(record):
        kind = block.get("type")
        if kind == "text" and is_empty_text(block):
            repairable.append("%s: empty text block" % role)
        elif kind == "thinking":
            if not (block.get("thinking") or "").strip():
                info.append("assistant: empty thinking block")
            if not (block.get("signature") or ""):
                info.append("assistant: thinking without signature")
        elif kind == "tool_result":
            payload = block.get("content")
            if payload is None or (isinstance(payload, (str, list)) and not payload):
                info.append("user: empty tool_result")
    return repairable, info


# --- rewriting -------------------------------------------------------------


def split_newline(raw):
    if raw.endswith(b"\r\n"):
        return raw[:-2], b"\r\n"
    if raw.endswith(b"\n"):
        return raw[:-1], b"\n"
    return raw, b""


def fill_empty_text(obj, placeholder):
    """Replace empty text blocks in a parsed record. Returns how many."""
    filled = 0
    for block in content_blocks(obj):
        if is_empty_text(block):
            block["text"] = placeholder
            filled += 1
    return filled


def repair_line(raw, placeholder):
    """Rewrite one JSONL line. Returns (bytes, method, count), or None.

    Two strategies, in order:

    1. Re-serialize. If dumping the parsed record reproduces the original line
       exactly, the writer's serialization is known and the modified record can
       be dumped the same way. This handles any escaping or spacing.
    2. Literal replacement. Used when re-serialization does not reproduce the
       line. It applies only when the count of the empty-text substring in the
       raw line equals the number of empty text blocks found, so a `"text":""`
       occurring inside some other string cannot be hit by accident.

    The result of either strategy is parsed again and compared against the
    expected record before it is returned.
    """
    body, newline = split_newline(raw)
    text = body.decode("utf-8")
    record = json.loads(text)

    expected = copy.deepcopy(record)
    count = fill_empty_text(expected, placeholder)
    if not count:
        return None

    def accept(candidate, method):
        if json.loads(candidate) != expected:
            raise Unrepairable("rewritten line does not match the expected record")
        return candidate.encode("utf-8") + newline, method, count

    compact = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    if compact == text:
        return accept(
            json.dumps(expected, separators=(",", ":"), ensure_ascii=False),
            "reserialize",
        )

    quoted = json.dumps(placeholder, ensure_ascii=False)
    for needle, replacement in (
        ('"text":""', '"text":' + quoted),
        ('"text": ""', '"text": ' + quoted),
    ):
        if text.count(needle) == count:
            return accept(text.replace(needle, replacement), "replace")

    raise Unrepairable(
        "line is not reproducible by re-serialization and the empty-text "
        "substring count does not match the %d empty block(s) found" % count
    )


# --- files -----------------------------------------------------------------


def processes_holding(path):
    """PIDs with the file open, best effort. Other users' entries are skipped."""
    target = os.path.realpath(path)
    pids = set()
    for entry in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.readlink(entry) == target:
                pids.add(entry.split("/")[2])
        except OSError:
            continue
    return sorted(pids, key=int)


def resolve_targets(targets, projects_dir, scan_all):
    """Turn CLI targets into transcript paths."""
    found = []
    if scan_all:
        found.extend(sorted(glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))))
    for target in targets:
        if os.path.isfile(target):
            found.append(target)
        elif os.path.isdir(target):
            found.extend(sorted(glob.glob(os.path.join(target, "*.jsonl"))))
        else:
            matches = sorted(
                glob.glob(os.path.join(projects_dir, "*", "%s.jsonl" % target))
            )
            if not matches:
                raise FileNotFoundError(
                    "no transcript for %r under %s" % (target, projects_dir)
                )
            found.extend(matches)
    # Never touch our own backups.
    return [p for p in dict.fromkeys(found) if BACKUP_SUFFIX not in p]


def last_error(records):
    """The trailing API error text, when the session died on one."""
    for _, record in reversed(records[-6:]):
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    body = block.get("text") or ""
                    if body.startswith("API Error:"):
                        return body.strip()
    return None


def process(path, args):
    """Check and optionally repair one transcript. Returns a result dict."""
    result = {
        "path": path,
        "repairable": [],
        "info": {},
        "patched": [],
        "skipped": [],
        "error": None,
        "last_error": None,
        "unparseable": [],
    }
    with open(path, "rb") as handle:
        raw_lines = handle.readlines()

    records = []
    info_counts = {}
    for number, raw in enumerate(raw_lines, 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError as exc:
            result["unparseable"].append((number, str(exc)))
            continue
        records.append((number, record))
        repairable, info = inspect(record)
        if repairable:
            result["repairable"].append((number, repairable))
        for label in info:
            info_counts[label] = info_counts.get(label, 0) + 1

    result["info"] = info_counts
    result["last_error"] = last_error(records)

    if not args.fix or not result["repairable"]:
        return result

    holders = processes_holding(path)
    if holders and not args.force:
        result["error"] = (
            "PID(s) %s have the file open; close the session or pass --force"
            % ", ".join(holders)
        )
        return result

    targets = {number for number, _ in result["repairable"]}
    untouched = hashlib.sha256()
    rewritten = []
    for number, raw in enumerate(raw_lines, 1):
        if number in targets:
            try:
                outcome = repair_line(raw, args.placeholder)
            except (Unrepairable, ValueError) as exc:
                result["skipped"].append((number, str(exc)))
                rewritten.append(raw)
                untouched.update(raw)
                continue
            new_raw, method, count = outcome
            rewritten.append(new_raw)
            result["patched"].append((number, method, count))
        else:
            rewritten.append(raw)
            untouched.update(raw)

    if not result["patched"]:
        result["error"] = "nothing could be repaired safely"
        return result

    if args.backup:
        backup = path + BACKUP_SUFFIX + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, backup)
        result["backup"] = backup

    mode = os.stat(path).st_mode & 0o777
    directory = os.path.dirname(os.path.abspath(path))
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".repair-", suffix=".jsonl")
    try:
        with os.fdopen(handle, "wb") as out:
            out.writelines(rewritten)
        os.chmod(tmp, mode)
        problem = verify(tmp, raw_lines, targets, untouched, args.placeholder)
        if problem:
            result["error"] = "verification failed: %s" % problem
            os.unlink(tmp)
            return result
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return result


def verify(candidate, original_lines, targets, untouched_digest, placeholder):
    """Check the rewritten file before it replaces the original."""
    with open(candidate, "rb") as handle:
        new_lines = handle.readlines()
    if len(new_lines) != len(original_lines):
        return "line count changed (%d -> %d)" % (len(original_lines), len(new_lines))

    check = hashlib.sha256()
    for number, raw in enumerate(new_lines, 1):
        if number not in targets:
            check.update(raw)
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError as exc:
            return "line %d no longer parses: %s" % (number, exc)
        for block in content_blocks(record):
            if is_empty_text(block):
                return "line %d still holds an empty text block" % number
    if check.hexdigest() != untouched_digest.hexdigest():
        return "lines outside the repair set changed"
    if not placeholder.strip():
        return "placeholder is itself empty"
    return None


# --- reporting -------------------------------------------------------------


def report(result, hint=None, brief=False):
    """Render one file's outcome. brief omits what the scan already printed."""
    blocks = sum(len(items) for _, items in result["repairable"])
    lines = [result["path"]]
    if brief:
        for number, method, count in result["patched"]:
            lines.append("  repaired line %d (%s, %d block(s))" % (number, method, count))
        for number, reason in result["skipped"]:
            lines.append("  SKIPPED line %d: %s" % (number, reason))
        if result.get("backup"):
            lines.append("  backup: %s" % result["backup"])
        if result["error"]:
            lines.append("  ERROR: %s" % result["error"])
        elif result["patched"]:
            lines.append("  verified: untouched lines byte-identical, all lines parse")
        return "\n".join(lines)
    if result["unparseable"]:
        lines.append(
            "  %d unparseable line(s): %s"
            % (
                len(result["unparseable"]),
                ", ".join(str(n) for n, _ in result["unparseable"][:5]),
            )
        )
    if blocks:
        numbers = ", ".join(str(n) for n, _ in result["repairable"][:12])
        if len(result["repairable"]) > 12:
            numbers += ", ..."
        lines.append("  %d empty text block(s) on line(s) %s" % (blocks, numbers))
    else:
        lines.append("  no empty text blocks")
    for label in sorted(result["info"]):
        lines.append("  note: %d x %s" % (result["info"][label], label))
    if result["last_error"]:
        lines.append("  last recorded error: %s" % result["last_error"])
    for number, method, count in result["patched"]:
        lines.append("  repaired line %d (%s, %d block(s))" % (number, method, count))
    for number, reason in result["skipped"]:
        lines.append("  SKIPPED line %d: %s" % (number, reason))
    if result.get("backup"):
        lines.append("  backup: %s" % result["backup"])
    if result["error"]:
        lines.append("  ERROR: %s" % result["error"])
    if result["patched"] and not result["error"]:
        lines.append("  verified: untouched lines byte-identical, all lines parse")
    if blocks and hint:
        lines.append("  %s" % hint)
    return "\n".join(lines)


def confirm(question):
    """Ask a [Y/n] question. Anything but an explicit no means yes."""
    try:
        answer = input(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("", "y", "yes")


# --- self test -------------------------------------------------------------

def selftest():
    """Build synthetic transcripts covering each case and repair them."""
    import io

    def record(uuid, content, **extra):
        base = {
            "type": "assistant",
            "uuid": uuid,
            "message": {
                "id": "resp_%s" % uuid,
                "role": "assistant",
                "model": "gpt-5.6-terra",
                "content": content,
            },
        }
        base["message"].update(extra)
        return json.dumps(base, separators=(",", ":"), ensure_ascii=False)

    interstitial = record("a1", [{"type": "text", "text": ""}], stop_reason="tool_use")
    real_text = record("a2", [{"type": "text", "text": "the real reply"}])
    empty_turn = record("a3", [{"type": "text", "text": ""}], stop_reason="end_turn")
    unicode_line = record("a4", [{"type": "text", "text": "kluč — ćевапи 🥩"}])
    thinking = record(
        "a5", [{"type": "thinking", "thinking": "", "signature": "gAAAAAB_x"}]
    )
    # A tool_result whose payload contains the empty-text substring, next to a
    # genuine empty block. Literal replacement must not be used here.
    decoy = json.dumps(
        {
            "type": "assistant",
            "uuid": "a6",
            "message": {
                "id": "resp_a6",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": ""},
                    {"type": "text", "text": 'saw {"text":"" } in the log'},
                ],
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    # Non-compact spacing, as a writer other than Claude Code would produce.
    spaced = '{"type": "assistant", "uuid": "a7", "message": {"id": "resp_a7", "role": "assistant", "content": [{"type": "text", "text": ""}]}}'

    lines = [
        interstitial,
        real_text,
        empty_turn,
        unicode_line,
        thinking,
        decoy,
        spaced,
    ]
    expected_targets = {1, 3, 6, 7}
    # One empty block per target line. The decoy's second block is non-empty
    # text that merely contains the empty-text substring.
    expected_blocks = 4

    failures = []
    workdir = tempfile.mkdtemp(prefix="repair-selftest-")
    try:
        path = os.path.join(workdir, "session.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(path, 0o600)
        with open(path, "rb") as handle:
            before = handle.readlines()

        args = argparse.Namespace(
            fix=False, placeholder=DEFAULT_PLACEHOLDER, backup=False, force=True
        )
        result = process(path, args)
        found = {number for number, _ in result["repairable"]}
        blocks = sum(len(items) for _, items in result["repairable"])
        if found != expected_targets:
            failures.append("check found lines %s, expected %s" % (sorted(found), sorted(expected_targets)))
        if blocks != expected_blocks:
            failures.append("check found %d blocks, expected %d" % (blocks, expected_blocks))
        if result["info"].get("assistant: empty thinking block") != 1:
            failures.append("empty thinking block not reported")
        with open(path, "rb") as handle:
            if handle.readlines() != before:
                failures.append("check mode modified the file")

        args.fix = True
        result = process(path, args)
        if result["error"]:
            failures.append("fix reported an error: %s" % result["error"])
        if len(result["patched"]) != len(expected_targets):
            failures.append(
                "repaired %d lines, expected %d" % (len(result["patched"]), len(expected_targets))
            )
        methods = {number: method for number, method, _ in result["patched"]}
        if methods.get(6) != "reserialize":
            failures.append("decoy line used %r, expected reserialize" % methods.get(6))
        if methods.get(7) != "replace":
            failures.append("spaced line used %r, expected replace" % methods.get(7))

        with open(path, "rb") as handle:
            after = handle.readlines()
        if len(after) != len(before):
            failures.append("line count changed")
        for index, (old, new) in enumerate(zip(before, after), 1):
            if index not in expected_targets and old != new:
                failures.append("line %d changed but should not have" % index)

        remaining = 0
        for raw in after:
            for block in content_blocks(json.loads(raw)):
                if is_empty_text(block):
                    remaining += 1
        if remaining:
            failures.append("%d empty text blocks remain" % remaining)

        decoy_after = json.loads(after[5])["message"]["content"]
        if decoy_after[1]["text"] != 'saw {"text":"" } in the log':
            failures.append("decoy text was altered: %r" % decoy_after[1]["text"])
        if decoy_after[0]["text"] != DEFAULT_PLACEHOLDER:
            failures.append("decoy empty block not repaired")
        if json.loads(after[3])["message"]["content"][0]["text"] != "kluč — ćевапи 🥩":
            failures.append("unicode line was altered")

        result = process(path, args)
        if result["repairable"]:
            failures.append("second run still found repairable blocks")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    stream = io.StringIO()
    if failures:
        print("selftest FAILED", file=stream)
        for failure in failures:
            print("  - %s" % failure, file=stream)
    else:
        print("selftest passed", file=stream)
        print("  cases: interstitial, empty end_turn, unicode, thinking, decoy, spacing, idempotence", file=stream)
    sys.stdout.write(stream.getvalue())
    return 0 if not failures else 2


# --- entry point -----------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("targets", nargs="*", help="transcript path, session UUID, or directory")
    parser.add_argument("--fix", action="store_true", help="repair without asking")
    parser.add_argument("--check", action="store_true", help="report only, never ask and never write")
    parser.add_argument("--all", action="store_true", dest="scan_all", help="scan every transcript under the projects directory")
    parser.add_argument("--placeholder", default=DEFAULT_PLACEHOLDER, help="replacement text (default %(default)r)")
    parser.add_argument("--no-backup", action="store_false", dest="backup", help="do not copy the original alongside")
    parser.add_argument("--force", action="store_true", help="repair even while a process holds the file open")
    parser.add_argument("--projects-dir", default=os.path.expanduser("~/.claude/projects"))
    parser.add_argument("--json", action="store_true", dest="as_json", help="print results as JSON")
    parser.add_argument("--selftest", action="store_true", help="run built-in tests and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.targets and not args.scan_all:
        parser.error("give at least one target, or --all")
    if not args.placeholder.strip():
        parser.error("--placeholder must not be empty or whitespace")
    if args.check and args.fix:
        parser.error("--check and --fix contradict each other")

    try:
        paths = resolve_targets(args.targets, args.projects_dir, args.scan_all)
    except FileNotFoundError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    interactive = (
        not args.check
        and not args.as_json
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )
    if args.check:
        hint = None
    elif interactive:
        hint = None  # the prompt below says it instead
    else:
        hint = "run again with --fix to replace them with %r" % args.placeholder

    scan = argparse.Namespace(**vars(args))
    scan.fix = False

    results = []
    failed = False
    repairable_total = 0
    repaired_total = 0

    for path in paths:
        if args.scan_all and not args.targets:
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError as exc:
                print("error: %s: %s" % (path, exc), file=sys.stderr)
                failed = True
                continue
            if not any(needle in data for needle in QUICK_NEEDLES):
                continue
        try:
            result = process(path, scan)
        except OSError as exc:
            print("error: %s: %s" % (path, exc), file=sys.stderr)
            failed = True
            continue
        blocks = sum(len(items) for _, items in result["repairable"])
        repairable_total += blocks
        if result["error"]:
            failed = True
        # A file can match the quick pre-filter because the empty-text
        # substring appears inside some other string. Scanning everything
        # should only report the files that actually hold something.
        interesting = bool(blocks or result["error"] or result["unparseable"])
        if not interesting and args.scan_all and not args.targets:
            continue
        results.append(result)
        if not args.as_json:
            print(report(result, hint))

    affected = [r for r in results if r["repairable"]]

    if args.as_json:
        print(json.dumps(results, indent=2))
        return 2 if failed else (1 if repairable_total else 0)

    if not results:
        print("no transcripts with empty text blocks")
        return 2 if failed else 0

    if repairable_total:
        print(
            "\n%d empty text block(s) in %d file(s)"
            % (repairable_total, len(affected))
        )
    else:
        print("\nno empty text blocks in %d file(s) checked" % len(results))

    do_repair = bool(affected) and args.fix
    if affected and not args.fix and interactive:
        do_repair = confirm(
            "Replace %d empty text block(s) in %d file(s) with %r? [Y/n] "
            % (repairable_total, len(affected), args.placeholder)
        )
        if not do_repair:
            print("left unchanged")

    if do_repair:
        print()
        for result in affected:
            try:
                repaired = process(result["path"], args if args.fix else _fixing(args))
            except OSError as exc:
                print("error: %s: %s" % (result["path"], exc), file=sys.stderr)
                failed = True
                continue
            repaired_total += sum(count for _, _, count in repaired["patched"])
            if repaired["error"] or repaired["skipped"]:
                failed = True
            print(report(repaired, brief=True))
        print("\n%d empty text block(s) repaired" % repaired_total)

    if failed:
        return 2
    if repairable_total > repaired_total:
        return 1
    return 0


def _fixing(args):
    """A copy of the parsed arguments with repairs enabled."""
    fixing = argparse.Namespace(**vars(args))
    fixing.fix = True
    return fixing


if __name__ == "__main__":
    sys.exit(main())
