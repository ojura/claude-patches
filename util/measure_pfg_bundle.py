#!/usr/bin/env python3
"""Measure the time and memory of pfg's module reader and built-in scan, with the helper's real JSON replies.

Usage: python3 -I -B util/measure_pfg_bundle.py /path/to/claude

The script samples the VmRSS of this Python process and of the node helper every 10 ms. The combined peak
is the largest sum of the two in one sample. The two individual peaks may occur at different times. The
script measures module extraction, the index pass and the built-in scan. It does not measure the table
predicates or value queries that a catalog runs afterwards.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from pfg import bundle as pb  # noqa: E402


def rss(pid):
    if pid is None:
        return 0
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as file:
            for line in file:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except FileNotFoundError:
        pass
    return 0


def measure(binary):
    node = [None]
    stop = threading.Event()
    peaks = {"python": 0, "node": 0, "combined": 0, "samples": 0}

    def sample():
        while True:
            python_rss, node_rss = rss(os.getpid()), rss(node[0])
            peaks["python"] = max(peaks["python"], python_rss)
            peaks["node"] = max(peaks["node"], node_rss)
            peaks["combined"] = max(peaks["combined"], python_rss + node_rss)
            peaks["samples"] += 1
            if stop.wait(0.010):
                return

    original = pb._Helper

    class Observed(original):
        def __init__(self):
            super().__init__()
            node[0] = self._process.pid

    pb._Helper = Observed
    sampler = threading.Thread(target=sample)
    sampler.start()
    bundle = None
    try:
        start = time.monotonic()
        with open(binary, "rb") as file:
            before = os.fstat(file.fileno())
            bundle = pb.Bundle.from_binary(file)
            after = os.fstat(file.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise RuntimeError("the executable changed while it was read")
        indexed = time.monotonic()
        scans = bundle.builtin_scan()
        summary = bundle.builtin_summary(scans)
        finished = time.monotonic()
        return {
            "binary": str(Path(binary).resolve()), "pfgRevision": pb.REVISION,
            "node": bundle.node_version, "typescript": bundle.typescript_version,
            "modules": len(bundle.modules),
            "parseDiagnostics": sum(len(record["diagnostics"]) for record in bundle.records.values()),
            "indexSeconds": indexed - start,
            "builtinScanSeconds": finished - indexed, "totalSeconds": finished - start,
            "sampleIntervalMs": 10, "rssBytes": peaks,
            "scan": {"passedOccurrences": sum(summary["passed"].values()),
                     **{key: len(summary[key]) for key in ("references", "writes", "globalWrites", "registryWrites",
                                                          "prototypeWrites", "unfollowed", "dynamic")}},
        }
    finally:
        stop.set()
        sampler.join()
        if bundle is not None:
            bundle.close()
        pb._Helper = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(measure(args.binary), indent=2))


if __name__ == "__main__":
    main()
