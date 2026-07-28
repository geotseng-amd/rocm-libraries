"""Exploratory probe: locate memory-op clusters by inst_id in the busiest wave.

Read-only. Prints, for the store/load-ish instruction types, the inst_id and TS
of each occurrence, plus a coarse histogram of first-touch fetch-stall by
inst_id bucket, so we can delineate the deep-epilogue (Case-C store) cluster.
"""

import sys
from collections import defaultdict
from agentC_common import parse, busiest_wave

MEMISH = {
    "TT_INST_BUF_WR_1",
    "TT_INST_BUF_RD_1",
    "TT_INST_TDM",
    "TT_INST_TDM_OTHER_SIMD",
    "TT_INST_VMEM_OTHER_SIMD_1",
}


def main(path):
    recs = parse(path)
    mw, w = busiest_wave(recs)
    w = sorted(w, key=lambda r: r[5])  # by inst_id (== issue order, no loops)
    print(f"file={path} wave={mw} n={len(w)} maxid=0x{w[-1][5]:x}")

    # per-type inst_id ranges
    by_type = defaultdict(list)
    for r in w:
        by_type[r[4]].append(r[5])
    print("-- inst_id range per type --")
    for t, ids in sorted(by_type.items(), key=lambda x: min(x[1])):
        print(f"   {t:32s} n={len(ids):5d}  id[0x{min(ids):x}..0x{max(ids):x}]")

    # buffer stores specifically
    print("-- BUF_WR_1 occurrences (inst_id, TS) --")
    wr = [(r[5], r[6]) for r in w if r[4] == "TT_INST_BUF_WR_1"]
    if wr:
        print(
            f"   count={len(wr)} first=(0x{wr[0][0]:x},TS={wr[0][1]}) "
            f"last=(0x{wr[-1][0]:x},TS={wr[-1][1]})"
        )
        # gaps between store groups
        for i in range(1, len(wr)):
            if wr[i][0] - wr[i - 1][0] > 50:
                print(f"     GAP before store {i}: 0x{wr[i-1][0]:x} -> 0x{wr[i][0]:x}")
    print("-- BUF_RD_1 occurrences (inst_id, TS) --")
    rd = [(r[5], r[6]) for r in w if r[4] == "TT_INST_BUF_RD_1"]
    if rd:
        print(
            f"   count={len(rd)} first=(0x{rd[0][0]:x},TS={rd[0][1]}) "
            f"last=(0x{rd[-1][0]:x},TS={rd[-1][1]})"
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "f8f8s_sipa_beta1_gsu1.mon")
