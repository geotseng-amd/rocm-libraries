#!/usr/bin/env python3
"""agentD_timeline.py -- reconstruct the clock-time timeline of the busiest wave.

For each .mon trace:
  * parse the busiest wave (same selection as analyze_mon2.py)
  * every inst_id is first-touch (fully-unrolled kernel), so each inter-issue
    gap == a fetch/I-cache miss-latency sample for that static instruction
  * bin the wave on the absolute TS (clock) axis and report, per bin:
      - number of instructions issued
      - total fetch-stall cycles (sum of gap-1)
      - mean gap-per-first-touch (miss-latency proxy)
      - inst_id min..max covered
This reproduces the "Instruction Request Miss Latency vs clocks" curve.
"""
import re
import sys
from collections import defaultdict

INST_RE = re.compile(
    r"^(\d+) \((\d+),(\d+),(\d+),(\w+)\): INST instruction=(TT_INST_[A-Z0-9_]+)"
)
IDTS_RE = re.compile(r"inst_id=0x([0-9a-f]+), thread_id=\d+, TS=(\d+)")


def parse(path):
    recs = []
    pending = None
    with open(path) as f:
        for line in f:
            m = INST_RE.match(line)
            if m:
                pending = (m.group(2), m.group(3), m.group(4), m.group(5), m.group(6))
                continue
            if pending is not None:
                m2 = IDTS_RE.search(line)
                if m2:
                    se, sa, simd, slot, itype = pending
                    recs.append(
                        (
                            se,
                            sa,
                            simd,
                            slot,
                            itype,
                            int(m2.group(1), 16),
                            int(m2.group(2)),
                        )
                    )
                pending = None
    return recs


def busiest_wave(recs):
    by_wave = defaultdict(list)
    for r in recs:
        by_wave[(r[0], r[1], r[2], r[3])].append(r)
    mw = max(by_wave, key=lambda k: len(by_wave[k]))
    return mw, by_wave[mw]


def build(w):
    """Return list of (iid, ts, gap) for the wave; gap = fetch stall before issue."""
    out = []
    prev = None
    for r in w:
        iid, ts = r[5], r[6]
        gap = 0 if prev is None else max(0, ts - prev - 1)
        out.append((iid, ts, gap))
        prev = ts
    return out


def analyze(path, binw=2500):
    recs = parse(path)
    mw, w = busiest_wave(recs)
    seq = build(w)
    t0, t1 = seq[0][1], seq[-1][1]
    span = t1 - t0
    total_stall = sum(g for _, _, g in seq)

    print(f"\n===== {path} =====")
    print(
        f"wave={mw} n_inst={len(seq)} TS=[{t0}..{t1}] span={span} "
        f"max_id=0x{max(i for i, _, _ in seq):x}"
    )
    print(
        f"total fetch-stall cycles = {total_stall} ({100.0*total_stall/span:.1f}% of span)"
    )

    # bin on absolute clock axis
    bins = defaultdict(lambda: [0, 0, 1 << 60, -1])  # n, stall, idmin, idmax
    for iid, ts, gap in seq:
        b = (ts - t0) // binw
        e = bins[b]
        e[0] += 1
        e[1] += gap
        e[2] = min(e[2], iid)
        e[3] = max(e[3], iid)

    print(f"  bin(clocks)         n_inst  tot_stall  mean_gap   inst_id_range")
    for b in sorted(bins):
        n, st, lo, hi = bins[b]
        c0 = t0 + b * binw
        c1 = c0 + binw
        print(
            f"  [{c0:>7}..{c1:>7})  {n:>6}  {st:>8}  {st/n:>7.1f}   "
            f"0x{lo:04x}..0x{hi:04x}"
        )
    return seq, t0, t1, span, total_stall


if __name__ == "__main__":
    for p in sys.argv[1:]:
        analyze(p)
