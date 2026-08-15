#!/usr/bin/env python3
"""Grade a /tmp/pfg-instr.<pid>.log against the streaming-thinking stress
thresholds documented in util/streaming_thinking_stress.md.

Usage:
    util/check_streaming_log.py /tmp/pfg-instr.<pid>.log

Exit 0 = all thresholds met. Exit 1 = at least one row failed; the table
prints which.
"""
import os
import re
import sys

# Thresholds from streaming_thinking_stress.md. Tuple: (op, value).
#   op '>=' means metric must be at least value
#   op '==' means metric must equal value (use for "max = 0" style)
# Thresholds assume the interleaved canonical prompt at N=1..3 on Sonnet
# (three API turns). For the quick single-turn variant divide W1/R2/M2/E1/
# healthy_pairs by ~3 and ignore the m1_stmsgs_zero_transitions /
# distinct_blockindex_* rows (they're interleaved-only signals).
THRESHOLDS = [
    ('thinkLen_peak', '>=', 1200,
     "max chars of thinking seen in any C2 log line; calibrated against Sonnet"),
    ('w1_progressive_writes', '>=', 60,
     "count of W1 lines (absorbed writer firing per thinking_delta)"),
    ('r2_setter_calls', '>=', 60,
     "count of R2 lines (thinking-setter invocations: progressive W1 + finalized)"),
    ('m2_with_think', '>=', 60,
     "count of M2 lines whose thinkE >= 1 (the useMemo carried a thinking message)"),
    ('e1_with_memo', '>=', 60,
     "count of E1 lines whose memo >= 1 (aggregator received the thinking message)"),
    ('broken_pairs', '==', 0,
     "L1(n) -> C2(y) sequences (propagation gap; must be zero)"),
    ('healthy_pairs', '>=', 30,
     "L1(y) -> C2(y) sequences (propagation succeeded)"),
    ('m1_stmsgs_zero_transitions', '>=', 2,
     "M1 lines with stMsgs=0 (turn boundaries; N-1 for N interleaved turns)"),
    ('distinct_blockindex_thinking', '>=', 3,
     "distinct blockIndex values seen on content_block_start with blockType=thinking"),
    ('distinct_blockindex_text', '>=', 3,
     "distinct blockIndex values seen on content_block_start with blockType=text"),
]


def parse(log_text):
    """Compute the metric dict from one log file's text."""
    metrics = {}

    thinklens = [int(m) for m in re.findall(r'thinkLen=(\d+)', log_text)]
    metrics['thinkLen_peak'] = max(thinklens) if thinklens else 0

    metrics['r2_setter_calls'] = len(
        re.findall(r'\[pfg-instr R2 setThinking t=function', log_text)
    )

    # W1 = absorbed thinking_delta writer firing. Each line is one
    # progressive write attributed to the writer body itself, distinct
    # from the finalized write the reducer makes through the same setter
    # when an assistant message lands.
    metrics['w1_progressive_writes'] = len(
        re.findall(r'\[pfg-instr W1 writer=thinking_delta ', log_text)
    )

    metrics['m2_with_think'] = sum(
        1 for m in re.findall(r'\[pfg-instr M2 result=\d+ toolE=\d+ thinkE=(\d+)\]', log_text)
        if int(m) >= 1
    )

    metrics['e1_with_memo'] = sum(
        1 for m in re.findall(r'\[pfg-instr E1 agg memo=(\d+)', log_text)
        if int(m) >= 1
    )

    # Chain integrity: pair each L1 line with the next C2 line.
    # A "broken" pair = L1 saw no streamingThinking but the next C2 did.
    last_l1 = None
    broken = healthy = 0
    for line in log_text.split('\n'):
        m = re.search(r'\[pfg-instr L1 render hasST=(.)', line)
        if m:
            last_l1 = m.group(1)
            continue
        m = re.search(r'\[pfg-instr C2 component hasST=(.)', line)
        if m and last_l1 is not None:
            if last_l1 == 'n' and m.group(1) == 'y':
                broken += 1
            elif last_l1 == 'y' and m.group(1) == 'y':
                healthy += 1
    metrics['broken_pairs'] = broken
    metrics['healthy_pairs'] = healthy

    # M1 stMsgs=0 transitions = turn boundaries between separate API
    # streaming responses. For an interleaved N-turn run, expect N-1
    # (each new turn starts with an empty streamingThinking.messages
    # list before E.3 init repopulates it).
    metrics['m1_stmsgs_zero_transitions'] = len(
        re.findall(r'\[pfg-instr M1 memo tools=\d+ stMsgs=0\]', log_text)
    )

    # Distinct content-block indices observed at content_block_start
    # events, split by block type. Confirms the interleaved variant
    # actually produced N thinking + N text blocks across the run.
    # Requires the extended R1 hook (blockType + blockIndex fields).
    thinking_indices = set()
    text_indices = set()
    for line in log_text.split('\n'):
        m = re.search(
            r'\[pfg-instr R1 reducer .*eventType=content_block_start.*'
            r'blockType=(\w+) blockIndex=(-?\d+)\]',
            line,
        )
        if not m:
            continue
        btype, bidx = m.group(1), int(m.group(2))
        if btype == 'thinking':
            thinking_indices.add(bidx)
        elif btype == 'text':
            text_indices.add(bidx)
    metrics['distinct_blockindex_thinking'] = len(thinking_indices)
    metrics['distinct_blockindex_text'] = len(text_indices)

    return metrics


def grade(metrics):
    """Compare each metric to its threshold and print a table.
    Return True if every row passed."""
    print(f"{'Metric':<32} {'Value':<8} {'Test':<12} {'Status':<6}")
    print('-' * 78)
    all_pass = True
    for key, op, threshold, _desc in THRESHOLDS:
        v = metrics.get(key, 0)
        if op == '>=':
            ok = v >= threshold
            test = f">= {threshold}"
        elif op == '==':
            ok = v == threshold
            test = f"== {threshold}"
        else:
            raise ValueError(f"unknown op {op!r}")
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"{key:<32} {v:<8} {test:<12} {status:<6}")
    print('-' * 78)
    print(f"Overall: {'PASS' if all_pass else 'FAIL'}")
    return all_pass


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        sys.exit(2)
    path = sys.argv[1]
    if not os.path.exists(path):
        sys.exit(f"log file not found: {path}")
    log_text = open(path).read()
    if not log_text.strip():
        sys.exit(
            f"log file is empty: {path}\n"
            "Is the live binary actually the --instr build? "
            "Did the prompt run to completion before grading?"
        )
    metrics = parse(log_text)
    ok = grade(metrics)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
