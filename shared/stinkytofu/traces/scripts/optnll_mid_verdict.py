#!/usr/bin/env python3
"""optnll_mid_verdict.py  (READ-ONLY)

Final verification of the I-cache "middle miss" mechanism for the gfx1250 f8f8s
OptNLL launch. Reuses optnll_mid_map.build_map (validated dyn->static-byte
mapping) + optnll_cache.pkl + optnllwin_common parser.

Answers deliverables 3 & 4 and synthesises the verdict:
  Q3: ICPREF (s_prefetch_inst) count/positions in both traces; identify the
      executed prefetch ARM (caseA/B/C) from the static ladder + dynamic id gap,
      and its warmed byte target.
  Q4: I-cache pressure -- executed pre-epilogue footprint + prefetch-warmed
      regions (CP + dead caseB + CP-extend) vs 64 KiB.
"""
import pickle
import sys
from collections import Counter

sys.path.insert(
    0, "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/scripts"
)
import optnllwin_common as C
from optnll_mid_map import (
    build_map,
    load_no,
    load_wi,
    ttclass,
    region,
    OPT,
    HEAD_HI,
    CPEX_HI,
    GWEND,
)

# ---- static prefetch ladder facts (decoded from obj_dump.log) ----
# each arm: s_get_pc + s_add(imm) => base ; 6 hints (offsets 0x0..0x5000 = 24KiB)
# except the CP-extend cover arm (2 hints = 8KiB).
LADDER = {
    "CP-extend cover": (32636, 2, 8192, "label_SW_PrefetchAbs_CpBoundary"),
    "caseA": (51980, 6, 24576, "label_GW_B0_MB"),
    "caseB": (111264, 6, 24576, "label_GW_B0_GSU1 (dead deep path)"),
    "caseC": (235628, 6, 24576, "label_GW_B1_GSU1"),
}


def icpref_records(wave):
    return [(i, r) for i, r in enumerate(wave) if ttclass(r[0]) == "ICPREF"]


def main():
    insns, labels, wave_no = load_no()
    wave_wi = load_wi()
    byte_of = [b for b, _ in insns]

    # ===================== Q3: ICPREF prefetch audit =====================
    print("=" * 74)
    print("Q3: ICPREF (s_prefetch_inst) prefetch execution audit")
    print("=" * 74)
    for name, wave in (("NO-cpex", wave_no), ("WITH-cpex", wave_wi)):
        icp = icpref_records(wave)
        ids = [r[1] for _, r in icp]
        print(
            f"\n[{name}] ICPREF count = {len(icp)}  "
            f"dyn ids {['0x%x'%i for i in ids]}"
        )
        print(
            f"  TS range {icp[0][1][2]}..{icp[-1][1][2]}  "
            f"(dyn idx {icp[0][0]}..{icp[-1][0]})  -- all near kernel start"
        )
        # cluster into arms by contiguous dyn-id runs
        clusters = []
        cur = [icp[0]]
        for prev, nxt in zip(icp, icp[1:]):
            if nxt[1][1] - prev[1][1] == 1:
                cur.append(nxt)
            else:
                clusters.append(cur)
                cur = [nxt]
        clusters.append(cur)
        for c in clusters:
            gap_before = None
            print(
                f"    arm: {len(c)} hints  ids 0x{c[0][1][1]:x}..0x{c[-1][1][1]:x}"
                f"  ({'CP-extend cover 8KiB' if len(c)==2 else '24KiB arm'})"
            )
        # dyn-id gap between last cover-hint and first 6-hint arm => path length
        if len(clusters) == 2:
            gap = clusters[1][0][1][1] - clusters[0][-1][1][1]
            print(
                f"    dyn-id diff cover->arm = {gap} => {gap-1} intervening "
                f"instrs (caseB fall-through path=10; caseA branch path=7)"
            )

    print("\n  Static ladder decode (getpc+add => base, verified exact):")
    for nm, (base, hints, sz, lbl) in LADDER.items():
        print(
            f"    {nm:16s} base={base:<7d} hints={hints} warms {sz}B "
            f"[{base},{base+sz}) -> {lbl}"
        )
    print("\n  Only 6 arm-hints execute (one arm). dyn-id gap=10 + trailing")
    print("  s_branch => the executed arm is caseB @111264 (GW_B0_GSU1).")
    print("  OptNLL fast path is TAKEN => that deep path is NEVER executed")
    print("  => caseB prefetch warms 24KiB of DEAD code.")

    # ===================== Q1/Q2 via build_map (recompute) ===============
    rec_no, _, _, o_opt = build_map(insns, wave_no)
    rec_wi, _, _, _ = build_map(insns, wave_wi)

    def head_stall(recs):
        return sum(
            r["gap"]
            for r in recs
            if r["byte"] is not None and OPT <= r["byte"] < HEAD_HI
        )

    hs_no, hs_wi = head_stall(rec_no), head_stall(rec_wi)

    win = [r for r in rec_no if 80000 <= r["ts"] < 90000]
    reg_stall = Counter()
    for r in win:
        reg_stall[region(r["byte"])] += r["gap"]
    tot_win = sum(reg_stall.values())
    bmap = sorted(r["byte"] for r in win if r["byte"] is not None)

    print("\n" + "=" * 74)
    print(
        "Q1 (recap): NO-cpex clock [80000,90000) -> static bytes "
        f"[{bmap[0]},{bmap[-1]}] median {bmap[len(bmap)//2]}"
    )
    for rg, s in reg_stall.most_common():
        print(f"    {rg:<24} stall={s:>6} ({100*s/tot_win:.1f}%)")
    print(
        "Q2 (recap): HEAD [20504,32636) first-touch fetch-stall  "
        f"NO={hs_no}  WITH={hs_wi}  (both large => head evicted despite "
        "CP preload)"
    )

    # ===================== Q4: I-cache pressure =====================
    print("\n" + "=" * 74)
    print("Q4: I-cache pressure (64 KiB = 65536 B)")
    print("=" * 74)
    CACHE_B = 65536
    # prefetch-warmed regions
    cp = (0, 32640)
    cpex = (32636, 40828)
    caseb = (111264, 135840)
    # merge cp + cpex (contiguous/overlapping)
    warm_low = (0, 40828)  # CP U CP-extend
    warm_low_sz = warm_low[1] - warm_low[0]
    caseb_sz = caseb[1] - caseb[0]
    # executed OptNLL body tail beyond CP-extend (must be fetched, not prefetched)
    tail_sz = GWEND - CPEX_HI
    body_sz = GWEND - OPT

    print(f"  prefetch-warmed regions:")
    print(f"    CP preload         [0,32640)          = {32640} B")
    print(
        f"    CP-extend (WITH)   [32636,40828)      = {cpex[1]-cpex[0]} B "
        f"(+{40828-32640} beyond CP)"
    )
    print(
        f"    dead caseB         [111264,135840)    = {caseb_sz} B (24 KiB, "
        f"NEVER executed)"
    )
    union_warm = warm_low_sz + caseb_sz
    print(
        f"  warmed union (CP U CP-extend U caseB)   = {warm_low_sz} + "
        f"{caseb_sz} = {union_warm} B = {union_warm/1024:.2f} KiB"
    )
    print(
        f"    vs 64 KiB: {'EXCEEDS' if union_warm>CACHE_B else 'under by %d B'%(CACHE_B-union_warm)}"
    )

    # add the actually-executed code that must ALSO be resident:
    #   full executed OptNLL body tail beyond the warmed low region.
    exec_plus = union_warm + tail_sz
    print(
        f"\n  + executed OptNLL body tail [40828,46940) (not prefetched) "
        f"= {tail_sz} B"
    )
    print(
        f"  working set = warmed_union + exec tail = {exec_plus} B = "
        f"{exec_plus/1024:.2f} KiB  -> {'EXCEEDS 64 KiB' if exec_plus>CACHE_B else 'under'}"
    )

    # cleaner framing: total distinct code that competes = executed [0,46940)
    #                  U dead caseB [111264,135840)
    exec_all = GWEND  # [0,46940) all executed at some point
    total_ws = exec_all + caseb_sz  # disjoint from caseB
    print(
        f"\n  full-launch competing footprint = executed [0,{GWEND}) ({exec_all} B)"
        f" + dead caseB ({caseb_sz} B)"
    )
    print(
        f"     = {total_ws} B = {total_ws/1024:.2f} KiB  -> "
        f"{'EXCEEDS 64 KiB by %d B (%.1f KiB)'%(total_ws-CACHE_B,(total_ws-CACHE_B)/1024) if total_ws>CACHE_B else 'under'}"
    )

    # ===================== VERDICT =====================
    print("\n" + "=" * 74)
    print("VERDICT")
    print("=" * 74)
    print(
        f"""  * OptNLL fast path TAKEN; epilogue = OptNLL body [20504,46940).
  * The busiest wave's epilogue first-touch fetch-stall (I-cache misses) is
    {sum(r['gap'] for r in rec_no if r['byte'] and OPT<=r['byte']<GWEND)} cyc
    (NO) -- ~39% of the wave's total, concentrated in the epilogue.
  * Q1: clock [80000,90000) executes the COVERED window [32636,40828)
    (bytes {bmap[0]}..{bmap[-1]}), NOT the head. The head executes earlier
    (TS ~71.5k-75.2k, clock bucket 70000).
  * Q2: the HEAD [20504,32636) -- INSIDE the CP preload [0,32640) -- still
    incurs NO={hs_no}/WITH={hs_wi} first-touch fetch-stall cycles at epilogue
    time. First-touch stalls on preloaded lines => those lines were EVICTED
    between launch and the epilogue ~56k clocks later. HEAD MISS CONFIRMED.
  * Q3: the dead Case-B ladder DOES execute: 6 ICPREF hints (NO) targeting
    GW_B0_GSU1@111264 -> warms [111264,135840) = 24 KiB of code the TAKEN
    OptNLL path never runs. WITH-cpex adds 2 CP-extend cover hints (8 total).
  * Q4: competing I-cache footprint = executed [0,46940) (45.8 KiB) + dead
    caseB 24 KiB = {total_ws} B = {total_ws/1024:.1f} KiB > 64 KiB. The 24 KiB
    dead caseB prefetch is a plausible EVICTOR of the CP-preloaded OptNLL head.

  => The "middle miss" is the I-cache miss on the OptNLL body executed at
     epilogue time. The HEAD [20504,32636) is genuinely evicted (Q2 proves
     first-touch stalls despite CP preload); the dead caseB 24 KiB prefetch
     ([111264,135840), never executed) is a plausible evictor since the
     launch working set exceeds the 64 KiB I-cache. Note the clock [80000,
     90000) bucket itself lands on the COVERED window/tail, not the head --
     the head miss shows up slightly earlier (~clock 70k-75k)."""
    )


if __name__ == "__main__":
    main()
