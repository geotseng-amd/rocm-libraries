#!/usr/bin/env python3
"""optnll_map_explore.py  (READ-ONLY; loads optnll_cache.pkl)

Exploration: compare STATIC obj mnemonic-class counts vs EXECUTED TT-class
counts, and inspect how inst_id relates to program order, to derive the true
(nonlinear) mapping.
"""
import pickle
from collections import Counter

CACHE = "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/scripts/optnll_cache.pkl"


def mclass(mn):
    if mn.startswith("buffer_store") or mn.startswith("buffer_atomic"):
        return "BUF_WR"
    if mn.startswith("buffer_load"):
        return "BUF_RD"
    if mn.startswith("s_prefetch"):
        return "ICPREF"
    if mn.startswith("s_branch") or mn.startswith("s_cbranch"):
        return "BRANCH"
    if mn.startswith(("s_load", "s_buffer_load", "s_store")):
        return "SMEM"
    if mn.startswith("s_"):
        return "SALU"
    if mn.startswith("v_wmma") or mn.startswith("v_mfma") or mn.startswith("v_dot"):
        return "WMMA"
    if mn.startswith("v_"):
        return "VALU"
    if mn.startswith("ds_"):
        return "LDS"
    if mn.startswith(("global_", "flat_", "scratch_")):
        return "FLAT"
    return "OTHER"


def ttclass(tt):
    t = tt[len("TT_INST_") :]
    if "BUF_WR" in t:
        return "BUF_WR"
    if "BUF_RD" in t:
        return "BUF_RD"
    if "ICPREF" in t:
        return "ICPREF"
    if "BRANCH" in t:
        return "BRANCH"
    if "WMMA" in t or "XDL" in t:
        return "WMMA"
    if "SALU" in t:
        return "SALU"
    if "VALU" in t:
        return "VALU"
    if "LDS" in t:
        return "LDS"
    return "OTHER"


def main():
    d = pickle.load(open(CACHE, "rb"))
    insns = d["insns"]
    wave = d["wave"]
    mnem_of = [m for _, m in insns]

    print("=== STATIC obj mnemonic-class counts (whole program) ===")
    sc = Counter(mclass(m) for m in mnem_of)
    for k, v in sc.most_common():
        print(f"  {v:>6d}  {k}")

    print("\n=== EXECUTED TT-class counts (busiest wave) ===")
    ec = Counter(ttclass(r[0]) for r in wave)
    for k, v in ec.most_common():
        print(f"  {v:>6d}  {k}")

    # static wmma count within OptNLL body region [3586,8140)
    body = mnem_of[3586:8140]
    bc = Counter(mclass(m) for m in body)
    print("\n=== STATIC class counts in OptNLL body ordinals [3586,8140) ===")
    for k, v in bc.most_common():
        print(f"  {v:>6d}  {k}")

    # how monotonic is inst_id vs TS?
    ids = [r[1] for r in wave]
    mono = sum(1 for i in range(1, len(ids)) if ids[i] > ids[i - 1])
    print(
        f"\ninst_id strictly increasing along TS order: " f"{mono}/{len(ids)-1} steps"
    )
    # distribution of forward jumps
    jumps = [ids[i] - ids[i - 1] for i in range(1, len(ids))]
    print(
        f"  min step={min(jumps)} max step={max(jumps)}  "
        f"neg steps={sum(1 for j in jumps if j<0)}"
    )
    print(f"  first 20 ids: {ids[:20]}")
    print(f"  last 20 ids:  {ids[-20:]}")

    # WMMA inst_id range
    wids = sorted(r[1] for r in wave if ttclass(r[0]) == "WMMA")
    print(f"\nWMMA executed: {len(wids)}  id range 0x{wids[0]:x}..0x{wids[-1]:x}")
    ldsids = sorted(r[1] for r in wave if ttclass(r[0]) == "LDS")
    print(f"LDS executed:  {len(ldsids)}  id range 0x{ldsids[0]:x}..0x{ldsids[-1]:x}")
    bwids = sorted(r[1] for r in wave if ttclass(r[0]) == "BUF_WR")
    print(f"BUF_WR exec:   {len(bwids)}  id range 0x{bwids[0]:x}..0x{bwids[-1]:x}")


if __name__ == "__main__":
    main()
