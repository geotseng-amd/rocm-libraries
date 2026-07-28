#!/usr/bin/env python3
"""agentA fetch-stall spatial analysis.

Reuses analyze_mon2.py semantics:
  - parse (SE,SA,SIMD,SLOT,itype,inst_id,TS) records
  - pick busiest wave (max instruction count)
  - segment into workgroups on inst_id==0
  - fetch-suspect stall = gap (TS delta > 1, credit gap-1 cycles) before an
    inst_id seen for the FIRST time in the whole run.

Deliverables:
  1. histogram of fetch-stall cycles vs inst_id, bucket = 0x400 (1024 ids)
  2. top-50 highest-stall inst_ids -> aggregate carried stall by TT_INST_<TYPE>
  3. largest contiguous inst_id stall cluster (id range, cycles, % of total)
  4. CP-extend window 0x1FE0..0x2800 stall accounting
"""
import re
import sys
from collections import defaultdict

INST_RE = re.compile(
    r"^(\d+) \((\d+),(\d+),(\d+),(\w+)\): INST instruction=(TT_INST_[A-Z0-9_]+)"
)
IDTS_RE = re.compile(r"inst_id=0x([0-9a-f]+), thread_id=\d+, TS=(\d+)")

CPEX_LO = 0x1FE0
CPEX_HI = 0x2800
BUCKET = 0x400
CLUSTER_GAP = 0x40  # ids; merge stalling ids into one cluster if within this


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


def analyze(path):
    recs = parse(path)
    by_wave = defaultdict(list)
    for r in recs:
        by_wave[(r[0], r[1], r[2], r[3])].append(r)
    mw = max(by_wave, key=lambda k: len(by_wave[k]))
    w = by_wave[mw]

    segs = []
    cur = []
    for r in w:
        if r[5] == 0 and cur:
            segs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        segs.append(cur)

    seen = set()
    fetch_stall = 0
    exec_stall = 0
    boundary_stall = 0
    fetch_by_id = defaultdict(int)
    type_by_id = {}  # first-touch instruction type for each inst_id
    span = w[-1][6] - w[0][6]

    for si, seg in enumerate(segs):
        prev = None
        for r in seg:
            iid, ts, itype = r[5], r[6], r[4]
            if iid not in type_by_id:
                type_by_id[iid] = itype
            if prev is not None:
                gap = ts - prev
                if gap > 1:
                    s = gap - 1
                    if iid in seen:
                        exec_stall += s
                    else:
                        fetch_stall += s
                        fetch_by_id[iid] += s
            else:
                if si > 0:
                    b = ts - segs[si - 1][-1][6]
                    if b > 1:
                        boundary_stall += b - 1
            seen.add(iid)
            prev = ts

    return {
        "path": path,
        "mw": mw,
        "n_inst": len(w),
        "n_segs": len(segs),
        "span": span,
        "distinct": len(seen),
        "max_id": max(seen),
        "fetch_stall": fetch_stall,
        "exec_stall": exec_stall,
        "boundary_stall": boundary_stall,
        "fetch_by_id": dict(fetch_by_id),
        "type_by_id": type_by_id,
    }


def report(a):
    path = a["path"]
    fbi = a["fetch_by_id"]
    tbi = a["type_by_id"]
    total_fetch = a["fetch_stall"]

    print("=" * 78)
    print(f"FILE: {path}")
    print(
        f"  busiest wave {a['mw']}  n_inst={a['n_inst']}  "
        f"segments={a['n_segs']}  TS_span={a['span']}"
    )
    print(f"  distinct_inst_id={a['distinct']}  max_id=0x{a['max_id']:x}")
    print(
        f"  FETCH-suspect stall total = {total_fetch} "
        f"({100.0*total_fetch/a['span']:.1f}% of span)"
    )
    print(
        f"  (exec/mem stall={a['exec_stall']}  "
        f"WG-boundary stall={a['boundary_stall']})"
    )

    # 1. histogram by 0x400 bucket
    print("\n  [1] FETCH-STALL histogram (bucket=0x400=1024 ids)")
    buckets = defaultdict(int)
    for iid, s in fbi.items():
        buckets[iid // BUCKET] += s
    maxbar = max(buckets.values()) if buckets else 1
    print(f"      {'id-range':<20}{'cycles':>10}  {'%':>6}")
    for b in sorted(buckets):
        lo, hi = b * BUCKET, (b + 1) * BUCKET - 1
        s = buckets[b]
        pct = 100.0 * s / total_fetch if total_fetch else 0
        bar = "#" * int(40 * s / maxbar)
        print(f"      0x{lo:04x}-0x{hi:04x} {s:>10}  {pct:5.1f}  {bar}")

    # 2. top-50 stalling inst_ids -> aggregate by type
    print("\n  [2] TOP-50 stalling inst_ids -> carried stall by TYPE")
    top = sorted(fbi.items(), key=lambda x: -x[1])[:50]
    top_total = sum(s for _, s in top)
    type_agg = defaultdict(int)
    type_cnt = defaultdict(int)
    for iid, s in top:
        t = tbi.get(iid, "?")
        type_agg[t] += s
        type_cnt[t] += 1
    print(
        f"      top-50 carry {top_total} cycles "
        f"({100.0*top_total/total_fetch:.1f}% of all fetch stall)"
    )
    print(f"      {'TYPE':<28}{'ids':>5}{'cycles':>10}  {'% top50':>8}")
    for t, s in sorted(type_agg.items(), key=lambda x: -x[1]):
        print(f"      {t:<28}{type_cnt[t]:>5}{s:>10}  " f"{100.0*s/top_total:7.1f}")
    print("      -- single largest stalling inst_ids --")
    for iid, s in top[:10]:
        print(f"        0x{iid:04x}  {s:>7}  {tbi.get(iid,'?')}")

    # 3. largest contiguous HIGH-DENSITY stall cluster (the deep epilogue).
    # Density-based: work in 0x400 buckets, keep the maximal contiguous run of
    # buckets whose stall density exceeds 2x the median bucket (the steady-state
    # kernel body). This isolates the tail cluster instead of merging the whole
    # id space (every first-touch id carries a small gap).
    print("\n  [3] LARGEST contiguous HIGH-DENSITY stall cluster (deep epilogue)")
    buck = defaultdict(int)
    for iid, s in fbi.items():
        buck[iid // BUCKET] += s
    bvals = sorted(buck.values())
    median = bvals[len(bvals) // 2]
    thresh = 2 * median
    hot = sorted(b for b in buck if buck[b] >= thresh)
    # maximal contiguous run of hot buckets carrying the most cycles
    runs = []
    cur = None
    for b in hot:
        if cur is None:
            cur = [b, b]
        elif b == cur[1] + 1:
            cur[1] = b
        else:
            runs.append(cur)
            cur = [b, b]
    if cur:
        runs.append(cur)
    best = None
    if runs:

        def run_cycles(r):
            return sum(buck[b] for b in range(r[0], r[1] + 1))

        rr = max(runs, key=run_cycles)
        lo = rr[0] * BUCKET
        hi = (rr[1] + 1) * BUCKET - 1
        # tighten to actual stalling ids present in the window
        wids = [iid for iid in fbi if lo <= iid <= hi]
        cyc = sum(fbi[iid] for iid in wids)
        best = (min(wids), max(wids), cyc)
        print(f"      median bucket={median}  threshold(2x)={thresh}")
        print(
            f"      id range 0x{best[0]:04x}..0x{best[1]:04x} "
            f"(width={best[1]-best[0]+1} ids)"
        )
        print(
            f"      stall cycles = {best[2]}  "
            f"({100.0*best[2]/total_fetch:.1f}% of all fetch stall)"
        )

    # 4. CP-extend window
    print(f"\n  [4] CP-EXTEND window 0x{CPEX_LO:04x}..0x{CPEX_HI:04x}")
    cpex = sum(s for iid, s in fbi.items() if CPEX_LO <= iid <= CPEX_HI)
    ncpex = sum(1 for iid in fbi if CPEX_LO <= iid <= CPEX_HI)
    print(f"      stalling ids in window = {ncpex}")
    print(
        f"      stall cycles in window = {cpex} "
        f"({100.0*cpex/total_fetch:.1f}% of all fetch stall)"
    )
    return {
        "total_fetch": total_fetch,
        "cpex": cpex,
        "best_cluster": best if clusters else None,
    }


def main():
    results = {}
    for path in sys.argv[1:]:
        a = analyze(path)
        results[path] = report(a)
    if len(results) == 2:
        (p1, r1), (p2, r2) = results.items()
        print("\n" + "=" * 78)
        print("CROSS-TRACE CP-EXTEND COMPARISON")
        for p, r in results.items():
            tag = "WITH-cpex" if "cpex" in p else "NO-cpex"
            print(
                f"  {tag:<10} total_fetch={r['total_fetch']:>7}  "
                f"cpex_window={r['cpex']:>7} "
                f"({100.0*r['cpex']/r['total_fetch']:.1f}%)"
            )


if __name__ == "__main__":
    main()
