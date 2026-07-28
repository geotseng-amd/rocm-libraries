#!/usr/bin/env python3
"""Phase characterization to localize the ~8032-cycle CP-extend saving.

Key fact discovered by optnll_win_analyze.py: in these traces inst_id is a
DYNAMIC monotonically-increasing issue counter (distinct_id == n_inst, max_id >>
n, single WG segment), NOT a static object ordinal. So a static byte range
cannot be mapped to inst_id linearly via obj#. Instead we identify the dynamic
execution PHASE (main WMMA loop vs OptNLL global-write epilogue) and show that
the saving lands in the epilogue tail -- exactly the code the CP-extend cover
([32636,40828), inside the GW/OptNLL epilogue [20504,46940)) warms.

Parses each file ONCE (busiest wave). Reuses optnll_win_analyze.parse/analyze.
"""
from collections import defaultdict
import optnll_win_analyze as A


def bucket_types(w):
    """Per-0x200 inst_id bucket: total first-touch fetch stall and dominant TT
    type of the instructions whose first-touch caused the stall."""
    seen = set()
    bstall = defaultdict(int)
    btype_stall = defaultdict(lambda: defaultdict(int))
    btype_count = defaultdict(lambda: defaultdict(int))
    prev = None
    for r in w:
        iid, ts, itype = r[5], r[6], r[4]
        b = iid // 0x200
        btype_count[b][itype] += 1
        if prev is not None and iid not in seen:
            gap = ts - prev
            if gap > 1:
                bstall[b] += gap - 1
                btype_stall[b][itype] += gap - 1
        seen.add(iid)
        prev = ts
    return bstall, btype_stall, btype_count


def last_wmma(w):
    last = None
    for r in w:
        if r[4] == "TT_INST_WMMA_XDL_4":
            last = r[5]
    return last


def main():
    recs_no = A.parse(A.NO)
    recs_wi = A.parse(A.WI)
    _, w_no = A.busiest_wave(recs_no)
    _, w_wi = A.busiest_wave(recs_wi)

    lw_no = last_wmma(w_no)
    lw_wi = last_wmma(w_wi)
    print(
        f"last WMMA_XDL_4 dyn inst_id: NO=0x{lw_no:x} ({lw_no})  "
        f"WITH=0x{lw_wi:x} ({lw_wi})"
    )
    print(f"  (instructions after last WMMA = OptNLL/global-write epilogue tail)")

    bstall_no, btype_no, bcount_no = bucket_types(w_no)
    bstall_wi, btype_wi, bcount_wi = bucket_types(w_wi)

    print("\nPer-bucket NO-cpex fetch stall + dominant stalling TT type + phase mix")
    print(f"{'bucket':<16}{'NOstall':>9}  top-stall-type            wmma_in_bucket")
    for b in sorted(bstall_no):
        lo, hi = b * 0x200, (b + 1) * 0x200 - 1
        tops = sorted(btype_no[b].items(), key=lambda x: -x[1])[:2]
        topstr = ", ".join(f"{t.replace('TT_INST_',''):<14}{s}" for t, s in tops)
        wmma = bcount_no[b].get("TT_INST_WMMA_XDL_4", 0)
        print(f"0x{lo:04x}-0x{hi:04x} {bstall_no[b]:>8}  {topstr:<40} {wmma}")

    # Epilogue tail = instructions strictly after last WMMA, in NO-id space.
    tail_lo = lw_no  # dyn id where epilogue begins (~last WMMA)
    print(f"\nEPILOGUE TAIL boundary (last WMMA) at NO dyn id ~0x{tail_lo:x}")

    # Sum stall in tail vs pre-tail for both runs (fold WITH via +6 for id>=0xc3).
    def foldwi(iid):
        return iid - 6 if iid > 0xC2 else iid  # inverse of +6 insertion shift

    def touch_stall_by_region(w, fold):
        seen = set()
        pre = tail = 0
        prev = None
        for r in w:
            iid, ts = r[5], r[6]
            nid = fold(iid)
            if prev is not None and iid not in seen:
                gap = ts - prev
                if gap > 1:
                    s = gap - 1
                    if nid >= tail_lo:
                        tail += s
                    else:
                        pre += s
            seen.add(iid)
            prev = ts
        return pre, tail

    pre_no, tail_no = touch_stall_by_region(w_no, lambda x: x)
    pre_wi, tail_wi = touch_stall_by_region(w_wi, foldwi)
    print("\nREGION SPLIT (dyn id, NO-space; tail = post-last-WMMA epilogue)")
    print(
        f"  PRE-tail (main loop+prologue): NO={pre_no}  WITH={pre_wi}  "
        f"delta={pre_no - pre_wi}"
    )
    print(
        f"  TAIL (OptNLL/GW epilogue)    : NO={tail_no}  WITH={tail_wi}  "
        f"delta={tail_no - tail_wi}"
    )
    tot = (pre_no - pre_wi) + (tail_no - tail_wi)
    print(f"  total delta = {tot}")
    if tot:
        print(
            f"  share of saving in epilogue tail = "
            f"{100.0 * (tail_no - tail_wi) / tot:.1f}%"
        )


if __name__ == "__main__":
    main()
