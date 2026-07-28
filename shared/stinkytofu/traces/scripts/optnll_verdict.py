"""OptNLL verdict: does the f8f8s gfx1250 kernel LAUNCH take the OptNLL fast-store
path, or fall through to the deep general GW_B0_GSU1 path?

Key model (stated by the task and cross-checked here):
  * The trace `inst_id` is a STATIC instruction ORDINAL (the Nth instruction in
    program order), NOT an execution counter. So inst_id=N  <->  the Nth
    instruction in obj_dump.log, whose real byte address is insns[N].addr.
  * We build ORDINAL -> BYTE-ADDRESS from obj_dump.log (instructions sorted by
    address, 0-based ordinal), validate it against known label byte offsets, then
    map the SET of executed inst_ids to byte addresses and see which static
    labels the executed code spans.

obj_dump byte offsets ARE the coordinate system the task's label offsets use
(obj total = 413264 == task's "total 413264").
"""

import sys
from agentX_static import parse_obj
from agentC_common import parse, busiest_wave

# Known static label byte offsets (obj_dump coordinates), from objdump/.s.
LABELS = [
    (0, "label_ASM_Start (kernel entry)"),
    (20360, "label_toPGR1end_OptNLL"),
    (20504, "label_GW_B0_OptNLL_MB  (OptNLL body ENTRY)"),
    (46940, "label_GW_End"),
    (46952, "label_OptNLL_End  (OptNLL body EXIT / s_endpgm)"),
    (51980, "label_GW_B0_MB  (deep general store block)"),
    (111204, "label_GW_End_1  (~deep GW_B0_GSU1 region)"),
    (137348, "label_GW_B0_FD0_VW4_GSU1_Then"),
    (170952, "label_GW_B0_FD0_VW4_GSU1_Else"),
    (235628, "label_GW_B1_GSU1  (B1 deep path)"),
]

OPTNLL_LO, OPTNLL_HI = 20504, 46952  # OptNLL body [entry, s_endpgm)
DEEP_B0_LO = 111204  # deep GW_B0_GSU1 region start
DEEP_B1_LO = 235628  # deep GW_B1_GSU1
CP_WINDOW_HI = 40828  # CP + CP-extend covered window [0,40828)


def build_ordinal_map(objpath):
    insns, labels = parse_obj(objpath)  # insns sorted by addr
    addr_of = [i[0] for i in insns]  # ordinal -> byte addr
    return insns, labels, addr_of


def ordinal_of_addr(insns, addr):
    """Return ordinal whose instruction starts exactly at addr, else -1."""
    lo, hi = 0, len(insns) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        a = insns[mid][0]
        if a == addr:
            return mid
        if a < addr:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


def label_for_addr(addr):
    """Nearest preceding named label."""
    name = "(before first label)"
    for a, n in LABELS:
        if a <= addr:
            name = n
        else:
            break
    return name


def contiguous_ranges(sorted_addrs, gap_tol_ordinals, insns, exec_ords):
    """Largest CONTIGUOUS executed byte range. Contiguity is judged on ordinals:
    consecutive executed ordinals (allowing a small gap of branched-over statics)
    belong to the same run. Returns list of (lo_addr, hi_addr, n_ords)."""
    runs = []
    cur_lo_ord = exec_ords[0]
    prev = exec_ords[0]
    for o in exec_ords[1:]:
        if o - prev > gap_tol_ordinals:
            runs.append((cur_lo_ord, prev))
            cur_lo_ord = o
        prev = o
    runs.append((cur_lo_ord, prev))
    out = []
    for lo_o, hi_o in runs:
        lo_a = insns[lo_o][0]
        hi_a = insns[hi_o][0] + insns[hi_o][1]  # end of last instruction
        out.append((lo_a, hi_a, hi_o - lo_o + 1))
    return out


def main():
    objpath = sys.argv[1]
    monpath = sys.argv[2]

    insns, labels, addr_of = build_ordinal_map(objpath)
    n_static = len(insns)
    total_bytes = insns[-1][0] + insns[-1][1]

    print("=" * 78)
    print(f"OBJ  = {objpath.split('/')[-2]}")
    print(f"MON  = {monpath.split('/')[-1]}")
    print("=" * 78)
    print(f"[map] static instructions in obj_dump = {n_static}")
    print(f"[map] total kernel bytes              = {total_bytes}")

    # ---- 1. Validate ordinal->byte map on known label points ----
    print("\n---- 1. ORDINAL -> BYTE map validation (known labels) ----")
    print(f"  {'label':44s} {'byte_addr':>10s} {'ordinal':>8s}  {'roundtrip'}")
    for a, n in LABELS:
        o = ordinal_of_addr(insns, a)
        if o >= 0:
            rt = addr_of[o]
            ok = "OK" if rt == a else f"MISMATCH({rt})"
            print(f"  {n:44s} {a:10d} {o:8d}  addr_of[ord]={rt} {ok}")
        else:
            # not an instruction boundary (label may sit between); show nearest
            import bisect

            idx = bisect.bisect_left(addr_of, a)
            near = addr_of[idx] if idx < len(addr_of) else -1
            print(
                f"  {n:44s} {a:10d} {'--':>8s}  not-an-inst-boundary; "
                f"nearest inst addr={near}"
            )

    # ---- 2. Executed set (trace) ----
    recs = parse(monpath)
    mw, w = busiest_wave(recs)
    exec_ids = sorted(set(r[5] for r in w))
    max_id = exec_ids[-1]
    print("\n---- 2. EXECUTED SET (busiest wave) ----")
    print(f"  wave={mw}  total_records={len(w)}  distinct_inst_id={len(exec_ids)}")
    print(
        f"  inst_id (ordinal) range = {exec_ids[0]}..{max_id} "
        f"(0x{exec_ids[0]:x}..0x{max_id:x})"
    )
    if max_id >= n_static:
        print(f"  !! max ordinal {max_id} >= n_static {n_static} -- model broken")
        return

    exec_addrs = [addr_of[i] for i in exec_ids]
    min_a, max_a = min(exec_addrs), max(exec_addrs)
    max_a_end = addr_of[max_id] + insns[max_id][1]
    print(f"  MIN executed byte addr  = {min_a}   -> {label_for_addr(min_a)}")
    print(f"  MAX executed byte addr  = {max_a} (end {max_a_end})")
    print(f"                             -> {label_for_addr(max_a)}")

    # sample ordinal->addr for a handful of executed ids (validation of forward map)
    print("  sample executed ordinal->addr:")
    for i in [
        exec_ids[0],
        exec_ids[len(exec_ids) // 4],
        exec_ids[len(exec_ids) // 2],
        exec_ids[3 * len(exec_ids) // 4],
        max_id,
    ]:
        print(f"      ord={i:6d} -> addr={addr_of[i]:8d}  {label_for_addr(addr_of[i])}")

    # largest contiguous executed byte range (tolerate branched-over statics)
    runs = contiguous_ranges(
        exec_addrs, gap_tol_ordinals=64, insns=insns, exec_ords=exec_ids
    )
    runs_by_span = sorted(runs, key=lambda r: r[1] - r[0], reverse=True)
    print(f"  #contiguous runs (gap_tol=64 ord) = {len(runs)}")
    print("  largest contiguous executed byte ranges:")
    for lo_a, hi_a, no in runs_by_span[:5]:
        print(
            f"      [{lo_a:8d}, {hi_a:8d})  span={hi_a-lo_a:7d}B  ords={no:6d}  "
            f"{label_for_addr(lo_a)}"
        )

    # ---- membership tests ----
    in_optnll = [a for a in exec_addrs if OPTNLL_LO <= a < OPTNLL_HI]
    in_deep_b0 = [a for a in exec_addrs if a >= DEEP_B0_LO]
    in_deep_b1 = [a for a in exec_addrs if a >= DEEP_B1_LO]
    in_gw_b0_mb = [a for a in exec_addrs if 51980 <= a < 111204]
    print("\n---- 3. REGION MEMBERSHIP ----")
    print(
        f"  executed in OptNLL body [{OPTNLL_LO},{OPTNLL_HI}) = "
        f"{len(in_optnll)} insts"
        + (f"  addr[{min(in_optnll)}..{max(in_optnll)}]" if in_optnll else "")
    )
    print(
        f"  executed in GW_B0_MB    [51980,111204)          = "
        f"{len(in_gw_b0_mb)} insts"
        + (f"  addr[{min(in_gw_b0_mb)}..{max(in_gw_b0_mb)}]" if in_gw_b0_mb else "")
    )
    print(
        f"  executed in deep GW_B0  [>= {DEEP_B0_LO}]          = "
        f"{len(in_deep_b0)} insts"
        + (f"  addr[{min(in_deep_b0)}..{max(in_deep_b0)}]" if in_deep_b0 else "")
    )
    print(
        f"  executed in deep GW_B1  [>= {DEEP_B1_LO}]          = "
        f"{len(in_deep_b1)} insts"
        + (f"  addr[{min(in_deep_b1)}..{max(in_deep_b1)}]" if in_deep_b1 else "")
    )

    # ---- verdict ----
    print("\n---- VERDICT ----")
    takes_optnll = len(in_optnll) > 0 and len(in_deep_b0) == 0
    if takes_optnll:
        body_lo = min([a for a in exec_addrs if a >= OPTNLL_LO] or [max_a])
        body_hi = max_a_end
        print("  OptNLL FAST PATH == *** TAKEN ***")
        print(
            f"    execution confined to prologue+Kloop+OptNLL body; "
            f"max exec addr {max_a} (end {max_a_end}) <= OptNLL_End {OPTNLL_HI}"
        )
        # deliverable 4: body span vs CP window
        beyond = max(0, body_hi - CP_WINDOW_HI)
        span = body_hi - OPTNLL_LO
        print(
            f"\n---- 4. OptNLL body span vs CP+CP-extend window [0,{CP_WINDOW_HI}) ----"
        )
        print(
            f"    OptNLL body byte span (entry->exit) = [{OPTNLL_LO}, {body_hi}) "
            f"= {span} B"
        )
        print(
            f"    portion beyond CP window ({CP_WINDOW_HI}) = {beyond} B "
            f"({100.0*beyond/span:.1f}% of body)"
        )
    else:
        print("  OptNLL FAST PATH == *** NOT TAKEN ***")
        print(
            f"    execution reaches deep general path; max exec addr {max_a} "
            f">= GW_B0_MB/GSU1 blocks"
        )
    print()


if __name__ == "__main__":
    main()
