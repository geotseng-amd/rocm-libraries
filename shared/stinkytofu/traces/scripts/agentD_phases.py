#!/usr/bin/env python3
"""agentD_phases.py -- per-phase stats, big-miss samples, CP-extend mapping, ideal runtime.

Every inst_id in the busiest wave is first-touch, so each inter-issue gap is a
direct I-cache miss-latency sample plotted in the user's "Instruction Request
Miss Latency vs clocks" chart. We:
  1. Split the timeline into prologue / K-loop / epilogue phases.
  2. Per phase: total fetch-stall, mean gap over all inst, and the mean/peak of
     the *significant* misses (gap>=100) -- the latter is what forms the visible
     spikes/plateau on the chart.
  3. Map the CP-extend covered window (inst_id 0x1FE0..0x2800) onto the clock axis.
  4. Ideal runtime = span - total_stall; quantify epilogue-only speedup.
"""
import re
import sys
from collections import defaultdict

INST_RE = re.compile(
    r"^(\d+) \((\d+),(\d+),(\d+),(\w+)\): INST instruction=(TT_INST_[A-Z0-9_]+)"
)
IDTS_RE = re.compile(r"inst_id=0x([0-9a-f]+), thread_id=\d+, TS=(\d+)")

CPEX_LO, CPEX_HI = 0x1FE0, 0x2800  # CP-extend covered static-inst window


def parse(path):
    recs, pending = [], None
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


def busiest(recs):
    by = defaultdict(list)
    for r in recs:
        by[(r[0], r[1], r[2], r[3])].append(r)
    mw = max(by, key=lambda k: len(by[k]))
    return mw, by[mw]


def build(w):
    seq, prev = [], None
    for r in w:
        iid, ts, itype = r[5], r[6], r[4]
        gap = 0 if prev is None else max(0, ts - prev - 1)
        seq.append((iid, ts, gap, itype))
        prev = ts
    return seq


def phase_stats(name, seq, lo_ts, hi_ts, sig=100):
    sub = [s for s in seq if lo_ts <= s[1] < hi_ts]
    if not sub:
        print(f"  {name:<10} (empty)")
        return 0
    n = len(sub)
    tot = sum(g for _, _, g, _ in sub)
    sigs = [g for _, _, g, _ in sub if g >= sig]
    ids = [i for i, _, _, _ in sub]
    types = defaultdict(int)
    for _, _, g, t in sub:
        types[t] += g
    top_types = sorted(types.items(), key=lambda x: -x[1])[:3]
    ttxt = ", ".join(f"{t.replace('TT_INST_','')}:{v}" for t, v in top_types)
    sig_mean = sum(sigs) / len(sigs) if sigs else 0.0
    sig_max = max(sigs) if sigs else 0
    print(
        f"  {name:<9} TS[{lo_ts:>6}..{hi_ts:>6})  n={n:<5} "
        f"id=0x{min(ids):04x}..0x{max(ids):04x}"
    )
    print(
        f"             tot_stall={tot:<6} mean_gap_all={tot/n:5.1f}  "
        f"big_misses(>= {sig}):{len(sigs):<4} mean={sig_mean:5.1f} max={sig_max}"
    )
    print(f"             stall_by_type: {ttxt}")
    return tot


def cpex_window(seq):
    sub = [s for s in seq if CPEX_LO <= s[0] < CPEX_HI]
    if not sub:
        print("  CP-extend window: no inst_id in range")
        return
    tsv = [s[1] for s in sub]
    gv = [s[2] for s in sub]
    print(
        f"  CP-extend window inst_id[0x{CPEX_LO:04x}..0x{CPEX_HI:04x}): "
        f"n={len(sub)}  TS=[{min(tsv)}..{max(tsv)}]"
    )
    print(
        f"             mean_gap={sum(gv)/len(gv):.1f}  max_gap={max(gv)}  "
        f"tot_stall={sum(gv)}"
    )


def main(path, cuts):
    recs = parse(path)
    mw, w = busiest(recs)
    seq = build(w)
    t0, t1 = seq[0][1], seq[-1][1]
    span = t1 - t0
    total = sum(g for _, _, g, _ in seq)
    print(f"\n===== {path} =====")
    print(f"wave={mw} n={len(seq)} TS=[{t0}..{t1}] span={span} total_stall={total}")

    pro_hi, kloop_hi = cuts
    st_pro = phase_stats("PROLOGUE", seq, t0, pro_hi)
    st_k = phase_stats("K-LOOP", seq, pro_hi, kloop_hi)
    st_epi = phase_stats("EPILOGUE", seq, kloop_hi, t1 + 1)

    cpex_window(seq)

    ideal = span - total
    ideal_epi = span - st_epi
    print(
        f"  IDEAL (0 fetch stall) runtime = {ideal} clocks "
        f"(span {span} -> {ideal}, {100.0*total/span:.1f}% removable)"
    )
    print(
        f"  Epilogue-only fetch stall = {st_epi} " f"({100.0*st_epi/span:.1f}% of span)"
    )
    print(
        f"  Eliminate epilogue stalls only: {span} -> {ideal_epi} clocks, "
        f"speedup = {100.0*(span-ideal_epi)/span:.1f}% "
        f"(={span/ideal_epi:.3f}x)"
    )
    return span, total, st_epi


if __name__ == "__main__":
    # cuts = (prologue_end_TS, kloop_end_TS) chosen from agentD_timeline.py bins
    CFG = {
        "f8f8s_sipa_beta1_gsu1.mon": (23029, 68029),
        "f8f8s_sipa_cpex_beta1_gsu1.mon": (23001, 70501),
    }
    for p in sys.argv[1:]:
        main(p, CFG.get(p, (23000, 68000)))
