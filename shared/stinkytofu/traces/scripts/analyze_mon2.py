#!/usr/bin/env python3
"""Refined .mon analysis: separate instruction-FETCH stalls from exec/memory stalls.

Rationale: an I-cache miss can only occur on the FIRST fetch of a static
instruction (first-touch). A gap before a re-executed inst_id (e.g. a loop body
already resident) is an execution/memory dependency stall, NOT a fetch miss.
Prefetch (sipa) should shrink the first-touch (fetch) component specifically.

The wave slot runs multiple workgroups back-to-back (inst_id resets to 0). We
segment on that, then per segment classify each inter-issue gap as:
  - fetch-suspect: gap before an inst_id seen for the FIRST time in the run
  - exec/mem     : gap before an already-seen inst_id
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


def main():
    path = sys.argv[1]
    recs = parse(path)
    by_wave = defaultdict(list)
    for r in recs:
        by_wave[(r[0], r[1], r[2], r[3])].append(r)
    mw = max(by_wave, key=lambda k: len(by_wave[k]))
    w = by_wave[mw]

    # segment into workgroups: new segment whenever inst_id == 0
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
    fetch_by_id = defaultdict(int)
    exec_by_type = defaultdict(int)
    boundary_stall = 0
    span = w[-1][6] - w[0][6]

    for si, seg in enumerate(segs):
        prev = None
        for i, r in enumerate(seg):
            iid, ts, itype = r[5], r[6], r[4]
            if prev is not None:
                gap = ts - prev
                if gap > 1:
                    s = gap - 1
                    if iid in seen:
                        exec_stall += s
                        exec_by_type[itype] += s
                    else:
                        fetch_stall += s
                        fetch_by_id[iid] += s
            else:
                # gap across WG boundary (from previous seg's last TS)
                if si > 0:
                    b = ts - segs[si - 1][-1][6]
                    if b > 1:
                        boundary_stall += b - 1
            seen.add(iid)
            prev = ts

    print(f"file={path}")
    print(f"main_wave={mw}  n_inst={len(w)}  n_workgroups(segments)={len(segs)}")
    print(f"TS span={span}  distinct_inst_id={len(seen)}  max_id=0x{max(seen):x}")
    print(
        f"  WG-boundary stall (turnover+cold restart) = {boundary_stall} "
        f"({100.0*boundary_stall/span:.1f}%)"
    )
    print(
        f"  FETCH-suspect stall (first-touch gaps)     = {fetch_stall} "
        f"({100.0*fetch_stall/span:.1f}%)"
    )
    print(
        f"  EXEC/MEM stall (re-executed inst gaps)     = {exec_stall} "
        f"({100.0*exec_stall/span:.1f}%)"
    )
    print("  -- exec/mem stall by type (top) --")
    for t, s in sorted(exec_by_type.items(), key=lambda x: -x[1])[:6]:
        print(f"      {s:>8}  {t}")
    print("  -- fetch-suspect stall by inst_id (top) --")
    for iid, s in sorted(fetch_by_id.items(), key=lambda x: -x[1])[:12]:
        print(f"      0x{iid:04x}  {s}")


if __name__ == "__main__":
    main()
