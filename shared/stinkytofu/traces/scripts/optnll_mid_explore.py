#!/usr/bin/env python3
"""optnll_mid_explore.py  (READ-ONLY)

Groundwork for the I-cache "middle miss" verification. Reuses:
  * optnll_cache.pkl  (NO-cpex busiest wave + objdump insns/labels) built by
    optnll_map_build.py using analyze_mon2.py parser logic.
  * optnllwin_common.py parser to load the WITH-cpex busiest wave once.

The validated model (optnll_map_align.py / optnll_win_analyze.py): inst_id is a
DYNAMIC monotonic issue counter (distinct == n_inst), TS is the clock. The
OptNLL epilogue = dynamic records AFTER the last WMMA/LDS. We align that
epilogue's TT-class stream forward-only onto the static disassembly starting at
the OptNLL entry ordinal to get a per-record static byte.

This script prints the scaffolding facts we need before answering:
  - epilogue split point + its TS span (to see where clock [80000,90000) lands)
  - per-10k-clock (absolute TS) first-touch fetch-stall for the whole wave
  - static class sequence structure of the OptNLL body
  - ICPREF (s_prefetch_inst) records in the whole wave (TS + neighbours)
"""
import pickle
import sys
from collections import Counter, defaultdict

sys.path.insert(
    0, "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/scripts"
)
import optnllwin_common as C

CACHE = "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/scripts/optnll_cache.pkl"
WI = "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/f8f8s_sipa_cpex_beta0_gsu1_alpha1_optnll.mon"


def ttclass(tt):
    t = tt[len("TT_INST_") :] if tt.startswith("TT_INST_") else tt
    if "BUF_WR" in t:
        return "BUF_WR"
    if "BUF_RD" in t:
        return "BUF_RD"
    if "ICPREF" in t or "PREF" in t:
        return "ICPREF"
    if "BRANCH" in t:
        return "BRANCH"
    if "WMMA" in t or "XDL" in t or "MFMA" in t:
        return "WMMA"
    if "SMEM" in t or "SLOAD" in t:
        return "SMEM"
    if "SALU" in t:
        return "SALU"
    if "VALU" in t:
        return "VALU"
    if "LDS" in t or t.startswith("DS") or "SHARED" in t:
        return "LDS"
    if "FLAT" in t or "GLOBAL" in t:
        return "FLAT"
    return "OTHER"


def clockbuckets(wave_recs, get):
    """First-touch fetch-stall bucketed by absolute-TS 10k windows.
    wave_recs: list of (tt, id, ts).  get: accessor returning (tt,id,ts)."""
    seen = set()
    buck = defaultdict(int)
    prev = None
    for r in wave_recs:
        tt, iid, ts = get(r)
        if prev is not None and iid not in seen:
            gap = ts - prev
            if gap > 1:
                buck[(ts // 10000) * 10000] += gap - 1
        seen.add(iid)
        prev = ts
    return buck


def main():
    d = pickle.load(open(CACHE, "rb"))
    insns = d["insns"]
    labels = d["labels"]
    wave_no = d["wave"]  # (tt, id, ts) in issue order
    byte_of = [b for b, _ in insns]

    # WITH-cpex wave via common parser: recs are (se,sa,simd,slot,tt,id,ts)
    mw_wi, w_wi = C.busiest_wave(WI)
    wave_wi = [(r[4], r[5], r[6]) for r in w_wi]

    print(f"NO   wave n={len(wave_no)} TS[{wave_no[0][2]}..{wave_no[-1][2]}]")
    print(f"WITH wave={mw_wi} n={len(wave_wi)} TS[{wave_wi[0][2]}..{wave_wi[-1][2]}]")

    # ---- epilogue split (last WMMA/LDS) for both ----
    def split(wave):
        last = 0
        for i, r in enumerate(wave):
            if ttclass(r[0]) in ("WMMA", "LDS"):
                last = i
        return last

    for name, wave in (("NO", wave_no), ("WITH", wave_wi)):
        li = split(wave)
        epi = wave[li + 1 :]
        print(
            f"\n[{name}] last WMMA/LDS at dyn idx {li} (id 0x{wave[li][1]:x} "
            f"TS={wave[li][2]}); epilogue len={len(epi)} "
            f"TS[{epi[0][2]}..{epi[-1][2]}]"
        )
        print(f"  epilogue class counts: {Counter(ttclass(r[0]) for r in epi)}")
        # where does TS cross 80000 and 90000 in the whole wave?
        for thr in (80000, 90000, 100000):
            idx = next((i for i, r in enumerate(wave) if r[2] >= thr), None)
            if idx is not None:
                print(
                    f"  first dyn idx with TS>={thr}: {idx} "
                    f"(id 0x{wave[idx][1]:x}, class {ttclass(wave[idx][0])}, "
                    f"TS={wave[idx][2]}) [epilogue={'Y' if idx>li else 'N'}]"
                )
            else:
                print(f"  no record reaches TS>={thr}")

    # ---- per-10k clock fetch-stall (whole wave) ----
    print("\n=== per-10k absolute-TS first-touch fetch-stall (whole wave) ===")
    bno = clockbuckets(wave_no, lambda r: (r[0], r[1], r[2]))
    bwi = clockbuckets(wave_wi, lambda r: (r[0], r[1], r[2]))
    allk = sorted(set(bno) | set(bwi))
    print(f"  {'clock':>10}{'NO':>10}{'WITH':>10}{'delta(NO-WI)':>14}")
    for k in allk:
        n, w = bno.get(k, 0), bwi.get(k, 0)
        print(f"  {k:>10}{n:>10}{w:>10}{n-w:>14}")

    # ---- ICPREF records in whole wave ----
    print("\n=== ICPREF (s_prefetch_inst) records in whole wave ===")
    for name, wave in (("NO", wave_no), ("WITH", wave_wi)):
        icp = [(i, r) for i, r in enumerate(wave) if ttclass(r[0]) == "ICPREF"]
        print(f"  [{name}] ICPREF count = {len(icp)}")
        for i, r in icp:
            print(f"     dyn idx {i:6d}  id 0x{r[1]:x}  TS={r[2]}  tt={r[0]}")

    # ---- static OptNLL body structure ----
    OPT = labels["label_GW_B0_OptNLL_MB"]
    GWEND = labels["label_GW_End"]
    print(f"\n=== static OptNLL body [{OPT},{GWEND}) mnemonic-class histogram ===")

    def mclass(mn):
        if mn.startswith(("buffer_store", "buffer_atomic")):
            return "BUF_WR"
        if mn.startswith("buffer_load"):
            return "BUF_RD"
        if mn.startswith("s_prefetch"):
            return "ICPREF"
        if mn.startswith(("s_branch", "s_cbranch")):
            return "BRANCH"
        if mn.startswith(("s_load", "s_buffer_load", "s_store")):
            return "SMEM"
        if mn.startswith("s_"):
            return "SALU"
        if mn.startswith(("v_wmma", "v_mfma", "v_dot")):
            return "WMMA"
        if mn.startswith("v_"):
            return "VALU"
        if mn.startswith("ds_"):
            return "LDS"
        if mn.startswith(("global_", "flat_", "scratch_")):
            return "FLAT"
        return "OTHER"

    o_opt = next(i for i, b in enumerate(byte_of) if b >= OPT)
    o_end = next(i for i, b in enumerate(byte_of) if b >= GWEND)
    body = insns[o_opt:o_end]
    print(f"  ord [{o_opt},{o_end}) = {o_end-o_opt} static insns")
    print(f"  class hist: {Counter(mclass(m) for _, m in body)}")
    HEAD_HI = 32636
    head = [(b, m) for b, m in body if b < HEAD_HI]
    tail = [(b, m) for b, m in body if b >= HEAD_HI]
    print(
        f"  HEAD [{OPT},{HEAD_HI}) static insns={len(head)} "
        f"class {Counter(mclass(m) for _, m in head)}"
    )
    print(
        f"  TAIL [{HEAD_HI},{GWEND}) static insns={len(tail)} "
        f"class {Counter(mclass(m) for _, m in tail)}"
    )


if __name__ == "__main__":
    main()
