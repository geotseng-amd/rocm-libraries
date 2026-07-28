#!/usr/bin/env python3
"""Analyze MI400 token-trace .mon files: instruction-fetch stall localization.

For the fully-detailed wave, each INST token carries an issue cycle (leading
column), an inst_id (static instruction ordinal), and a TS (the simulated cycle
the instruction actually issued). A jump in TS between consecutive issues is a
stall (I-cache fetch miss or execution dependency). We aggregate stalls, find
the largest gaps, and bucket them by inst_id so they can be mapped to code
regions via obj_dump.
"""
import re
import sys
from collections import defaultdict

INST_RE = re.compile(
    r"^(\d+) \((\d+),(\d+),(\d+),(\w+)\): INST instruction=(TT_INST_[A-Z0-9_]+)"
)
IDTS_RE = re.compile(r"inst_id=0x([0-9a-f]+), thread_id=\d+, TS=(\d+)")


def parse(path):
    """Return list of records (seq, inst_id, ts, itype) for the main detailed wave."""
    recs = []
    pending = None
    with open(path) as f:
        for line in f:
            m = INST_RE.match(line)
            if m:
                # record spans two lines; stash header, resolve on continuation
                pending = (
                    int(m.group(1)),
                    m.group(2),
                    m.group(3),
                    m.group(4),
                    m.group(5),
                    m.group(6),
                )
                continue
            if pending is not None:
                m2 = IDTS_RE.search(line)
                if m2:
                    seq, se, sa, simd, slot, itype = pending
                    recs.append(
                        (
                            seq,
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
    # pick the wave with the most INST (the fully detailed one)
    by_wave = defaultdict(list)
    for r in recs:
        by_wave[(r[1], r[2], r[3], r[4])] = by_wave[(r[1], r[2], r[3], r[4])]
        by_wave[(r[1], r[2], r[3], r[4])].append(r)
    main_wave = max(by_wave, key=lambda k: len(by_wave[k]))
    w = by_wave[main_wave]
    print(f"file={path}")
    print(
        f"main_wave={main_wave} n_inst={len(w)} "
        f"distinct_inst_id={len(set(r[6] for r in w))} "
        f"max_inst_id=0x{max(r[6] for r in w):x}"
    )

    ts = [r[7] for r in w]
    first_ts, last_ts = ts[0], ts[-1]
    span = last_ts - first_ts
    print(f"TS span: {first_ts}..{last_ts} = {span} cycles; n_inst={len(w)}")

    # per-issue gap = TS[i]-TS[i-1]; gap>1 => stall of (gap-1)
    stall_by_id = defaultdict(int)
    stall_by_type = defaultdict(int)
    big = []
    total_stall = 0
    prev = None
    for r in w:
        cur = r[7]
        if prev is not None:
            gap = cur - prev
            if gap > 1:
                s = gap - 1
                total_stall += s
                stall_by_id[r[6]] += s
                stall_by_type[r[5]] += s
                if s >= 50:
                    big.append((s, r[6], r[5], cur))
        prev = cur
    print(
        f"total_stall_cycles={total_stall} "
        f"({100.0*total_stall/span:.1f}% of span); "
        f"busy_cycles≈{span-total_stall}"
    )

    print("\n-- stall by instruction type (top) --")
    for t, s in sorted(stall_by_type.items(), key=lambda x: -x[1])[:8]:
        print(f"  {s:>8}  {t}")

    print(
        f"\n-- big stalls (>=50 cyc): {len(big)} events, "
        f"sum={sum(b[0] for b in big)} --"
    )
    for s, iid, itype, cur in sorted(big, reverse=True)[:25]:
        print(f"  stall={s:>5}  inst_id=0x{iid:04x}  {itype:28} at_TS={cur}")

    print("\n-- top stall-accumulating inst_ids --")
    for iid, s in sorted(stall_by_id.items(), key=lambda x: -x[1])[:20]:
        print(f"  0x{iid:04x}  stall={s}")


if __name__ == "__main__":
    main()
