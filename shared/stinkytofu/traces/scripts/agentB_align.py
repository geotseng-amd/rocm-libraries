#!/usr/bin/env python3
"""agentB step 1+2: align two .mon instruction streams (busiest wave) and
report inserted instructions + per-inst issue-time deltas.

inst_id is a dword index: byte_offset = inst_id * 4.
"""
import re
import sys
import difflib
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


def busiest(path):
    recs = parse(path)
    by_wave = defaultdict(list)
    for r in recs:
        by_wave[(r[0], r[1], r[2], r[3])].append(r)
    mw = max(by_wave, key=lambda k: len(by_wave[k]))
    return mw, by_wave[mw]


def main():
    pa, pb = sys.argv[1], sys.argv[2]  # a = NO cpex, b = WITH cpex
    wa_key, A = busiest(pa)
    wb_key, B = busiest(pb)
    print(f"# A (NO cpex)   = {pa}  wave={wa_key} n={len(A)} span={A[-1][6]-A[0][6]}")
    print(f"# B (WITH cpex) = {pb}  wave={wb_key} n={len(B)} span={B[-1][6]-B[0][6]}")

    # sequences of instruction types for structural alignment
    ta = [r[4] for r in A]
    tb = [r[4] for r in B]
    sm = difflib.SequenceMatcher(a=ta, b=tb, autojunk=False)

    inserted = []  # records only in B
    deleted = []  # records only in A
    matched = []  # (idxA, idxB) pairs
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                matched.append((i1 + k, j1 + k))
        elif tag == "insert":
            for j in range(j1, j2):
                inserted.append(j)
        elif tag == "delete":
            for i in range(i1, i2):
                deleted.append(i)
        elif tag == "replace":
            # treat as delete+insert
            for i in range(i1, i2):
                deleted.append(i)
            for j in range(j1, j2):
                inserted.append(j)

    print("\n## STEP 1: instructions present ONLY in WITH-cpex (inserted) ##")
    print(
        f"count inserted (B-only) = {len(inserted)} ; deleted (A-only) = {len(deleted)}"
    )
    for j in inserted:
        r = B[j]
        iid = r[5]
        # locate preceding matched anchor to describe phase
        print(
            f"  B_idx={j:5d}  inst_id=0x{iid:04x} (byte {iid*4})  {r[4]:22s} TS={r[6]}"
        )
    if deleted:
        print("  -- A-only (unexpected) --")
        for i in deleted:
            r = A[i]
            print(f"  A_idx={i:5d}  inst_id=0x{r[5]:04x}  {r[4]:22s} TS={r[6]}")

    # Describe insertion neighborhood
    if inserted:
        jmin = min(inserted)
        print("\n## context around insertion (B) ##")
        for j in range(max(0, jmin - 3), min(len(B), max(inserted) + 4)):
            r = B[j]
            mark = "  <== INSERTED" if j in inserted else ""
            print(f"  B_idx={j:5d} inst_id=0x{r[5]:04x} {r[4]:24s} TS={r[6]}{mark}")

    # STEP 2: per-inst issue-time delta on matched pairs.
    # Normalize by aligning both streams to their first TS (relative issue time),
    # and also compute cumulative inter-issue gaps.
    a0 = A[0][6]
    b0 = B[0][6]

    # per-matched-pair relative-time delta
    prev_ia = prev_ib = None
    faster = slower = same = 0
    faster_ts = slower_ts = 0
    max_slow = (0, None)
    max_fast = (0, None)
    # gap-based (inter-issue) comparison to localize where divergence happens
    for ia, ib in matched:
        ra = A[ia][6] - a0
        rb = B[ib][6] - b0
        d = rb - ra  # positive => B later (slower)
        if d > 0:
            slower += 1
            slower_ts += d
            if d > max_slow[0]:
                max_slow = (d, (ia, ib, A[ia][5]))
        elif d < 0:
            faster += 1
            faster_ts += -d
            if -d > max_fast[0]:
                max_fast = (-d, (ia, ib, A[ia][5]))
        else:
            same += 1

    print("\n## STEP 2: per-inst issue-time delta (relative to each run's first TS) ##")
    print(f"matched pairs = {len(matched)}")
    print(f"  B later (slower): {slower} insts, cumulative +{slower_ts}")
    print(f"  B earlier (faster): {faster} insts, cumulative -{faster_ts}")
    print(f"  identical timing: {same} insts")
    print(f"  NET relative delta at end (B-A) = {slower_ts - faster_ts:+d}")
    print(f"  raw span diff (B-A) = {(B[-1][6]-B[0][6]) - (A[-1][6]-A[0][6]):+d}")
    if max_slow[1]:
        print(
            f"  biggest single slowdown: +{max_slow[0]} at A_idx-inst_id=0x{max_slow[1][2]:04x}"
        )
    if max_fast[1]:
        print(
            f"  biggest single speedup:  -{max_fast[0]} at inst_id=0x{max_fast[1][2]:04x}"
        )

    # fetch-stall (first-touch gap) totals for each run (matches analyze_mon2 logic)
    def fetch_stall(recs):
        seen = set()
        tot = 0
        by_id = defaultdict(int)
        prev = None
        for r in recs:
            iid, ts = r[5], r[6]
            if prev is not None:
                gap = ts - prev
                if gap > 1 and iid not in seen:
                    tot += gap - 1
                    by_id[iid] += gap - 1
            seen.add(iid)
            prev = ts
        return tot, by_id

    fa, _ = fetch_stall(A)
    fb, _ = fetch_stall(B)
    print(f"\n  total fetch-suspect stall A={fa}  B={fb}  diff(B-A)={fb-fa:+d}")

    return A, B, matched, inserted


if __name__ == "__main__":
    main()
