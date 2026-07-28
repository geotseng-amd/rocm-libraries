#!/usr/bin/env python3
"""agentD_check.py -- confirm the epilogue miss-latency PLATEAU and CP-extend placement."""
import sys
from agentD_phases import parse, busiest, build, CPEX_LO, CPEX_HI


def main(path):
    mw, w = busiest(parse(path))
    seq = build(w)
    print(f"\n== {path} ==")
    # epilogue big misses (>=300): show they recur across the whole window
    big = [(ts, g, iid, t) for iid, ts, g, t in seq if g >= 300]
    print(f"gaps>=300: {len(big)} samples, " f"TS spread [{big[0][0]}..{big[-1][0]}]")
    # decade histogram of those gaps
    from collections import Counter

    c = Counter(50 * (g // 50) for _, g, _, _ in big)
    print("  gap histogram (bucketed by 50):")
    for k in sorted(c):
        print(f"    {k:>5}-{k+49:<5}: {c[k]}")
    # sample every ~8th big miss to show plateau along clock axis
    print("  plateau samples (every ~8th):")
    for ts, g, iid, t in big[::8]:
        print(f"    TS={ts:>7} gap={g:>4} id=0x{iid:04x} {t.replace('TT_INST_','')}")
    # CP-extend window latency profile
    cp = [(ts, g) for iid, ts, g, t in seq if CPEX_LO <= iid < CPEX_HI]
    over = [g for _, g in cp if g >= 300]
    print(
        f"CP-extend window: n={len(cp)} TS=[{cp[0][0]}..{cp[-1][0]}] "
        f"gaps>=300: {len(over)}  (=> {'IN plateau' if over else 'FLAT/prefetched K-loop'})"
    )


if __name__ == "__main__":
    for p in sys.argv[1:]:
        main(p)
