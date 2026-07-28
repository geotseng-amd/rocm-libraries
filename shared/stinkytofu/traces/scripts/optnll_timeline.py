"""Fetch-vs-execute discriminator for the OptNLL verdict.

The .mon INST stream records each static instruction at most once (first-touch),
so we cannot see loop repetition. To decide whether the deep general path was
truly EXECUTED (branched into) vs merely front-end FETCHED past the OptNLL
s_endpgm, we inspect the TEMPORAL trajectory:

  * Sort the busiest wave by TS.
  * Map each record's inst_id (static ordinal) -> byte addr.
  * Bucket into phases by static byte region and report, per region, the TS
    window (min/max) and the instruction-TYPE histogram.

Reasoning:
  - If OptNLL were TAKEN, the wave hits s_endpgm@46948 and terminates. Deep-path
    bytes (>=51980) could only appear via a few cache-lines of front-end
    fetch-ahead near t_end; they would carry LATE TS clustered right after the
    OptNLL region and carry NO real compute/store issue types.
  - If the DEEP path is TAKEN, deep-path bytes carry their own progressive TS
    (the epilogue runs after the K-loop) and real store/compute types; and the
    OptNLL body s_endpgm@46948 is NOT the wave's last executed instruction.
"""

import sys
from collections import Counter
from agentX_static import parse_obj
from agentC_common import parse, busiest_wave

REGIONS = [
    ("prologue+Kloop  [0,20504)", 0, 20504),
    ("OptNLL body     [20504,46948]", 20504, 46949),
    ("gap             [46949,51980)", 46949, 51980),
    ("GW_B0_MB        [51980,111204)", 51980, 111204),
    ("deep GW_B0_GSU1 [111204,235628)", 111204, 235628),
    ("deep GW_B1      [235628,end]", 235628, 10**9),
]
ENDPGM_ADDRS = {1952, 2216, 46948, 413260}


def region_name(addr):
    for name, lo, hi in REGIONS:
        if lo <= addr < hi:
            return name
    return "?"


def main():
    objpath, monpath = sys.argv[1], sys.argv[2]
    insns, labels = parse_obj(objpath)
    addr_of = [i[0] for i in insns]
    mnem_of = [i[2] for i in insns]

    recs = parse(monpath)
    mw, w = busiest_wave(recs)
    # (ts, inst_id, addr, itype, mnem)
    rows = []
    for r in w:
        iid = r[5]
        if iid < len(addr_of):
            rows.append((r[6], iid, addr_of[iid], r[4], mnem_of[iid]))
    rows.sort()  # by TS

    t0 = rows[0][0]
    print(
        f"MON={monpath.split('/')[-1]}  wave={mw}  n={len(rows)}  "
        f"TS[{rows[0][0]}..{rows[-1][0]}] span={rows[-1][0]-t0}"
    )

    # per-region TS window + type histogram
    print("\n-- per static-region: TS window (rel t0), #insts, top types --")
    buckets = {}
    for ts, iid, a, it, mn in rows:
        buckets.setdefault(region_name(a), []).append((ts, it, mn))
    for name, lo, hi in REGIONS:
        b = buckets.get(name)
        if not b:
            print(f"  {name:34s}  <none executed>")
            continue
        tss = [x[0] for x in b]
        types = Counter(x[1] for x in b)
        top = ", ".join(
            f"{t.replace('TT_INST_','')}:{c}" for t, c in types.most_common(4)
        )
        print(
            f"  {name:34s}  n={len(b):5d}  TS[{min(tss)-t0:7d}..{max(tss)-t0:7d}]  {top}"
        )

    # the actual LAST executed (max TS) instruction and its address
    last = rows[-1]
    print(f"\n-- LAST executed (max TS) --")
    print(
        f"   TS+{last[0]-t0}  ord={last[1]}  addr={last[2]}  "
        f"{last[3]}  '{last[4]}'  region={region_name(last[2])}"
    )

    # which s_endpgm(s) were executed, and their TS
    print("\n-- s_endpgm executions (addr -> was it in trace? at what TS) --")
    ex_by_addr = {}
    for ts, iid, a, it, mn in rows:
        ex_by_addr.setdefault(a, ts)
    for ea in sorted(ENDPGM_ADDRS):
        if ea in ex_by_addr:
            print(
                f"   s_endpgm@{ea:8d}  EXECUTED at TS+{ex_by_addr[ea]-t0}  "
                f"(region {region_name(ea)})"
            )
        else:
            print(f"   s_endpgm@{ea:8d}  NOT in trace")

    # highest-address run: check TS ordering near OptNLL exit boundary to detect
    # whether deep path TS continues progressively (execute) or is a small
    # fetch-ahead blob right after OptNLL (fetch).
    optnll_last_ts = max(
        (ts for ts, i, a, it, mn in rows if 20504 <= a < 46949), default=None
    )
    deep_first_ts = min((ts for ts, i, a, it, mn in rows if a >= 51980), default=None)
    deep_last_ts = max((ts for ts, i, a, it, mn in rows if a >= 51980), default=None)
    print("\n-- OptNLL/deep TS relationship --")
    if optnll_last_ts is not None:
        print(f"   OptNLL body last TS   = +{optnll_last_ts-t0}")
    if deep_first_ts is not None:
        print(f"   deep path first TS    = +{deep_first_ts-t0}")
        print(
            f"   deep path last  TS    = +{deep_last_ts-t0}  "
            f"(deep TS span={deep_last_ts-deep_first_ts})"
        )


if __name__ == "__main__":
    main()
