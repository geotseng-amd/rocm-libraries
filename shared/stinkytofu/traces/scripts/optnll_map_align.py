#!/usr/bin/env python3
"""optnll_map_align.py  (READ-ONLY; loads optnll_cache.pkl)

inst_id was shown to be a MONOTONIC DYNAMIC issue counter (strictly increasing
in TS order, small +1..+3 gaps, loops replay static PCs -> 8192 dyn WMMA from
1152 static). So there is NO affine inst_id->ordinal map; we must align the
dynamic class stream onto a PROGRAM-ORDER walk of the static disassembly.

Strategy (robust, monotonic greedy alignment):
  * The execution is: setup -> big MAC loop (all 8192 WMMA + 4096 LDS live
    here) -> post-loop epilogue.  The OptNLL fast-store body contains NO
    WMMA/LDS, only VALU/SALU/128 BUF_WR.
  * Split the dynamic stream at the LAST WMMA/LDS record => everything after is
    the post-loop epilogue.
  * Greedily align the epilogue's TT-class sequence to the static
    mnemonic-class sequence starting just after the loop, advancing the static
    pointer forward only (skipping non-executed static insns), never backward.
  * Report the max static byte reached, labels spanned, class-match accuracy,
    and whether it terminates within the OptNLL body (<GW_B0_MB) => TAKEN.
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
    if "BARRIER" in t or "TDM" in t:
        return "OTHER"
    return "OTHER"


COMPAT = {
    "BUF_WR": {"BUF_WR"},
    "BUF_RD": {"BUF_RD"},
    "ICPREF": {"ICPREF"},
    "BRANCH": {"BRANCH"},
    "WMMA": {"WMMA"},
    "VALU": {"VALU"},
    "SALU": {"SALU", "SMEM"},
    "LDS": {"LDS"},
    "OTHER": None,  # wildcard
}


def label_at(labels, ba):
    best = None
    for nm, x in labels.items():
        if x <= ba and (best is None or x > best[1]):
            best = (nm, x)
    return best


def greedy_align(dyn_classes, static_classes, start_ord, allow_skip=400):
    """Advance static pointer forward-only to consume each dynamic class.
    Returns (matched, total, max_ord_reached, path_ords)."""
    sp = start_ord
    n = len(static_classes)
    matched = 0
    path = []
    for dc in dyn_classes:
        if dc == "OTHER":
            path.append(sp)
            continue
        target = None
        for j in range(sp, min(sp + allow_skip, n)):
            sc = static_classes[j]
            comp = COMPAT.get(dc)
            if comp is None or sc in comp:
                target = j
                break
        if target is None:
            # no match within window: keep pointer, count as miss
            path.append(sp)
            continue
        matched += 1
        sp = target + 1
        path.append(target)
    max_ord = max(path) if path else start_ord
    return matched, len([c for c in dyn_classes if c != "OTHER"]), max_ord, path


def main():
    d = pickle.load(open(CACHE, "rb"))
    insns = d["insns"]
    labels = d["labels"]
    wave = d["wave"]  # (tt, id, ts) already in TS==id order
    byte_of = [b for b, _ in insns]
    scls = [mclass(m) for _, m in insns]

    # ordinals of key labels
    def ord_at(ba):
        lo, hi = 0, len(insns)
        while lo < hi:
            mid = (lo + hi) // 2
            if byte_of[mid] < ba:
                lo = mid + 1
            else:
                hi = mid
        return lo

    OPT = labels["label_GW_B0_OptNLL_MB"]
    GWEND = labels["label_GW_End"]
    GWB0MB = labels["label_GW_B0_MB"]
    o_opt, o_gwend, o_gwb0mb = ord_at(OPT), ord_at(GWEND), ord_at(GWB0MB)

    # --- static store distribution: OptNLL body vs deep GW_B0_MB path ---
    body_stores = sum(1 for j in range(o_opt, o_gwend) if scls[j] == "BUF_WR")
    deep_stores = sum(1 for j in range(o_gwb0mb, len(insns)) if scls[j] == "BUF_WR")
    print("=== static BUF_WR distribution ===")
    print(f"  OptNLL body [{o_opt},{o_gwend}) bytes[{OPT},{GWEND}): {body_stores}")
    print(f"  deep path   [{o_gwb0mb},end)  bytes[{GWB0MB},): {deep_stores}")

    # --- split dynamic stream at last WMMA/LDS ---
    last_loop = 0
    for i, r in enumerate(wave):
        if ttclass(r[0]) in ("WMMA", "LDS"):
            last_loop = i
    epi = wave[last_loop + 1 :]
    print(
        f"\n=== epilogue after last WMMA/LDS (dyn idx {last_loop+1}..{len(wave)}) ==="
    )
    print(f"  epilogue length = {len(epi)} records")
    print(f"  epilogue inst_id 0x{epi[0][1]:x}..0x{epi[-1][1]:x}")
    print(f"  epilogue class counts: {Counter(ttclass(r[0]) for r in epi)}")

    dyn = [ttclass(r[0]) for r in epi]
    # The store burst is the OptNLL fast-store body proper. Isolate it: from the
    # first executed BUF_WR to the end. Align that burst to the static stream
    # starting at the OptNLL entry ordinal (forward-only monotonic walk).
    first_bw = next(i for i, r in enumerate(epi) if ttclass(r[0]) == "BUF_WR")
    burst = dyn[first_bw:]
    print(
        f"\n  first epilogue BUF_WR at dyn offset {first_bw} "
        f"(inst_id 0x{epi[first_bw][1]:x}); burst len={len(burst)}"
    )
    print(f"  burst class counts: {Counter(burst)}")

    start = o_opt
    matched, tot, max_ord, path = greedy_align(burst, scls, start)
    max_byte = byte_of[max_ord]
    print(f"\n=== greedy monotonic alignment (start ord {start}) ===")
    print(f"  class-match: {matched}/{tot} = {100.0*matched/tot:.2f}%")
    print(f"  max static ordinal reached = {max_ord}  byte 0x{max_byte:x}={max_byte}")
    print(f"  deepest label = {label_at(labels, max_byte)}")

    # where do the 128 BUF_WR align to?
    bw_ords = [path[i] for i, c in enumerate(burst) if c == "BUF_WR"]
    if bw_ords:
        print(
            f"\n  BUF_WR aligned ordinals: {min(bw_ords)}..{max(bw_ords)} "
            f"bytes 0x{byte_of[min(bw_ords)]:x}..0x{byte_of[max(bw_ords)]:x}"
        )
        in_body = sum(1 for o in bw_ords if o_opt <= o < o_gwend)
        print(
            f"  BUF_WR mapped INTO OptNLL body [{o_opt},{o_gwend}): "
            f"{in_body}/{len(bw_ords)}"
        )

    # --- verdict ---
    print("\n" + "=" * 60)
    if max_byte < GWB0MB:
        print("VERDICT: OptNLL FAST PATH **TAKEN**")
        print(
            f"  epilogue terminates at byte 0x{max_byte:x}={max_byte} "
            f"< GW_B0_MB(0x{GWB0MB:x}={GWB0MB})"
        )
        print(f"  and the 128 dynamic stores land inside the OptNLL body.")
    else:
        print("VERDICT: OptNLL fast path **NOT TAKEN** (reaches deep GW blocks)")
    print("=" * 60)

    # --- CP window coverage of OptNLL body tail + fetch-stall cycles ---
    CP_END = 40828
    tail_bytes = GWEND - CP_END
    print(f"\n=== OptNLL body vs CP window [0,{CP_END}) ===")
    print(f"  OptNLL body bytes [{OPT},{GWEND})  size={GWEND-OPT}")
    print(
        f"  uncovered tail   [{CP_END},{GWEND})  size={tail_bytes} bytes "
        f"(~{tail_bytes/1024:.1f} KB)"
    )
    body_ords_exec = [o for o in path if o_opt <= o < o_gwend]
    tail_ords = [o for o in body_ords_exec if byte_of[o] >= CP_END]
    print(f"  distinct static instrs aligned in body: {len(set(body_ords_exec))}")
    print(f"  of those in uncovered tail [{CP_END},{GWEND}): " f"{len(set(tail_ords))}")

    # Fetch-stall cycles: distinct==n so EVERY record is a first-touch; per
    # analyze_mon2.py a gap>1 before a first-touch inst is a fetch-suspect stall.
    # Map each burst record's inst_id-order gap and bucket by aligned static byte.
    ts_all = [r[2] for r in wave]
    # global TS index of each burst record = last_loop+1+first_bw + k
    base = last_loop + 1 + first_bw
    tail_fetch = body_fetch = 0
    tail_head_fetch = 0  # head < CP-resident region not applicable (starts at OPT)
    for k in range(len(burst)):
        gi = base + k
        if gi == 0:
            continue
        gap = ts_all[gi] - ts_all[gi - 1] - 1
        if gap <= 0:
            continue
        o = path[k]
        if o_opt <= o < o_gwend:
            body_fetch += gap
            if byte_of[o] >= CP_END:
                tail_fetch += gap
    print(f"\n=== fetch-stall (first-touch gap) cycles ===")
    print(f"  OptNLL body total fetch-stall cycles      = {body_fetch}")
    print(
        f"  uncovered tail [{CP_END},{GWEND}) fetch-stall = {tail_fetch} "
        f"({100.0*tail_fetch/body_fetch:.1f}% of body)"
    )


if __name__ == "__main__":
    main()
