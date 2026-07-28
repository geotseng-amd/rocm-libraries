"""Deliverable 2: delineate the deep-epilogue stall cluster in the trace and
show (by the store/load instruction types present) that it is the Case-C
GW_B1_GSU1 store block executing.

Cluster definition: the tail of the compulsory stream after the last main-loop
compute op (WMMA/LDS), i.e. from the branch/JUMP into the write-back epilogue
through max inst_id. We detect the boundary as the first inst_id at/after the
last TT_INST_WMMA_XDL / TT_INST_LDS_RD (main mac loop), which is the entry into
the C write-back (Case-C) region.
"""

import sys
from collections import Counter
from agentC_common import parse, busiest_wave

MAIN_LOOP_TYPES = {"TT_INST_WMMA_XDL_4", "TT_INST_LDS_RD", "TT_INST_LDS_OTHER_SIMD_1"}
STORE_TYPES = {"TT_INST_BUF_WR_1"}
LOAD_TYPES = {"TT_INST_BUF_RD_1"}


def analyze(path):
    recs = parse(path)
    mw, w = busiest_wave(recs)
    w = sorted(w, key=lambda r: r[5])  # by inst_id == issue order (no loops)

    last_main = max(i for i, r in enumerate(w) if r[4] in MAIN_LOOP_TYPES)
    cstart = last_main + 1
    cluster = w[cstart:]

    lo_id, hi_id = cluster[0][5], cluster[-1][5]
    lo_ts, hi_ts = cluster[0][6], cluster[-1][6]
    span = w[-1][6] - w[0][6]

    print(f"==== {path}  wave={mw}")
    print(
        f"  main-loop last idx={last_main} (id=0x{w[last_main][5]:x} "
        f"{w[last_main][4]})"
    )
    print(
        f"  EPILOGUE CLUSTER: {len(cluster)} insts, "
        f"inst_id[0x{lo_id:x}..0x{hi_id:x}] ({lo_id}..{hi_id})"
    )
    print(
        f"    TS[{lo_ts}..{hi_ts}] cluster_clocks={hi_ts-lo_ts}  "
        f"({100.0*(hi_ts-lo_ts)/span:.1f}% of {span} span)"
    )

    tc = Counter(r[4] for r in cluster)
    print("  -- instruction types in cluster --")
    for t, n in tc.most_common():
        mark = ""
        if t in STORE_TYPES:
            mark = "  <-- STORE (buffer_store)"
        elif t in LOAD_TYPES:
            mark = "  <-- LOAD (buffer_load: beta*C)"
        print(f"      {n:6d}  {t}{mark}")

    st = [r for r in cluster if r[4] in STORE_TYPES]
    ld = [r for r in cluster if r[4] in LOAD_TYPES]
    print(f"  stores in cluster={len(st)}  loads in cluster={len(ld)}")
    if st:
        print(
            f"    first store id=0x{st[0][5]:x} TS={st[0][6]}  "
            f"last store id=0x{st[-1][5]:x} TS={st[-1][6]}"
        )
    return dict(cstart=cstart, cluster=cluster, span=span, w=w, w0=w[0][6], wN=w[-1][6])


if __name__ == "__main__":
    files = sys.argv[1:] or [
        "f8f8s_sipa_beta1_gsu1.mon",
        "f8f8s_sipa_cpex_beta1_gsu1.mon",
    ]
    for f in files:
        analyze(f)
        print()
