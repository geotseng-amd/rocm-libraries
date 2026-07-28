#!/usr/bin/env python3
"""optnll_map_analyze.py  (READ-ONLY; loads optnll_cache.pkl only)

Derive the trace inst_id -> obj (ordinal, byte) mapping empirically, validate it
by TT-type vs obj-mnemonic class agreement, then decide whether the gfx1250
f8f8s OptNLL fast-store body is TAKEN.
"""
import pickle
from collections import Counter, defaultdict

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


# class equivalence: which obj classes are acceptable for a given TT class
COMPAT = {
    "BUF_WR": {"BUF_WR"},
    "BUF_RD": {"BUF_RD"},
    "ICPREF": {"ICPREF"},
    "BRANCH": {"BRANCH"},
    "WMMA": {"WMMA"},
    "SALU": {"SALU", "SMEM"},  # trace may fold scalar mem into SALU
    "SMEM": {"SMEM", "SALU"},
    "VALU": {"VALU"},
    "LDS": {"LDS"},
    "FLAT": {"FLAT", "BUF_RD", "BUF_WR"},
}


def main():
    d = pickle.load(open(CACHE, "rb"))
    insns = d["insns"]  # [(byte, mnemonic)] index == obj ordinal
    labels = d["labels"]
    wave = d["wave"]  # [(tt_type, inst_id, ts)]
    N = len(insns)

    byte_of = [b for b, _ in insns]
    mnem_of = [m for _, m in insns]

    # ---- anchor verification: ordinal at a given byte ----
    def ord_at_byte(ba):
        # first ordinal whose byte == ba
        lo, hi = 0, N
        while lo < hi:
            mid = (lo + hi) // 2
            if byte_of[mid] < ba:
                lo = mid + 1
            else:
                hi = mid
        return lo

    print("=== anchor check (byte -> obj ordinal) ===")
    for nm in (
        "label_GW_B0_OptNLL_MB",
        "label_SW_PrefetchAbs_CpBoundary",
        "label_GW_End",
        "label_OptNLL_End",
        "label_GW_B0_MB",
        "label_GW_B1_GSU1",
    ):
        if nm in labels:
            ba = labels[nm]
            print(f"  {nm:32s} 0x{ba:x}={ba:<7d} ordinal={ord_at_byte(ba)}")

    # ---- TT type distribution in busiest wave ----
    ttcnt = Counter(r[0] for r in wave)
    print("\n=== busiest-wave TT type distribution ===")
    for t, c in ttcnt.most_common():
        print(f"  {c:>6d}  {t}  -> class {ttclass(t)}")

    ids = [r[1] for r in wave]
    max_id = max(ids)
    print(f"\nmax inst_id = 0x{max_id:x} = {max_id}   (obj N={N})")

    # ---- candidate mappings: ordinal = a*inst_id + b ----
    cands = [(1, 0), (1, 1), (1, -1), (1, 2), (1, -2)]
    print("\n=== mapping validation (ordinal = a*id + b) ===")
    best = None
    for a, b in cands:
        ok = tot = oob = 0
        for tt, iid, ts in wave:
            o = a * iid + b
            if o < 0 or o >= N:
                oob += 1
                continue
            tot += 1
            tc = ttclass(tt)
            if mclass(mnem_of[o]) in COMPAT.get(tc, {tc}):
                ok += 1
        acc = 100.0 * ok / tot if tot else 0.0
        print(f"  a={a} b={b:+d}: agree {ok}/{tot} = {acc:.2f}%  (oob={oob})")
        if best is None or acc > best[0]:
            best = (acc, a, b)

    acc, a, b = best
    print(f"\nBEST mapping: ordinal = {a}*inst_id + {b}  ({acc:.2f}% agree)")

    # ---- apply best mapping ----
    exec_ords = sorted(set(a * iid + b for iid in ids if 0 <= a * iid + b < N))
    exec_bytes = [byte_of[o] for o in exec_ords]
    min_o, max_o = exec_ords[0], exec_ords[-1]
    min_b, max_b = min(exec_bytes), max(exec_bytes)
    print(f"\n=== executed span (best mapping) ===")
    print(f"  ordinals: {min_o} .. {max_o}   ({len(exec_ords)} distinct)")
    print(f"  bytes:    0x{min_b:x}={min_b} .. 0x{max_b:x}={max_b}")

    # ---- labels spanned ----
    lab_sorted = sorted(labels.items(), key=lambda kv: kv[1])
    print("\n=== labels within executed byte range ===")
    spanned = [(nm, ba) for nm, ba in lab_sorted if min_b <= ba <= max_b]
    for nm, ba in spanned:
        print(f"  0x{ba:x}={ba:<7d} {nm}")
    # deepest label at or below max_b
    deepest = None
    for nm, ba in lab_sorted:
        if ba <= max_b:
            deepest = (nm, ba)
    print(f"\n  deepest label reached: {deepest}")

    # ---- verdict thresholds ----
    GW_END = labels.get("label_GW_End")
    GW_B0_MB = labels.get("label_GW_B0_MB")
    GW_B1_GSU1 = labels.get("label_GW_B1_GSU1")
    OPT_ENTRY = labels.get("label_GW_B0_OptNLL_MB")
    print(f"\n=== verdict inputs ===")
    print(f"  OptNLL entry  = 0x{OPT_ENTRY:x}={OPT_ENTRY}")
    print(f"  GW_End        = 0x{GW_END:x}={GW_END}")
    print(f"  GW_B0_MB      = 0x{GW_B0_MB:x}={GW_B0_MB}")
    print(f"  GW_B1_GSU1    = 0x{GW_B1_GSU1:x}={GW_B1_GSU1}")
    print(f"  max exec byte = 0x{max_b:x}={max_b}")

    taken = max_b < GW_B0_MB
    print("\n" + "=" * 60)
    if taken:
        print(
            "VERDICT: OptNLL FAST PATH **TAKEN** "
            "(execution confined below GW_B0_MB)."
        )
    else:
        print(
            "VERDICT: OptNLL fast path **NOT TAKEN** "
            "(execution reaches deep GW blocks)."
        )
    print("=" * 60)

    # ---- OptNLL body coverage vs CP window [0,40828) ----
    CP_END = 40828
    if taken:
        body_ords = [o for o in exec_ords if OPT_ENTRY <= byte_of[o] < GW_END]
        body_bytes = [byte_of[o] for o in body_ords]
        tail = [bb for bb in body_bytes if bb >= CP_END]
        head = [bb for bb in body_bytes if bb < OPT_ENTRY]  # sanity
        print(f"\n=== OptNLL body [{OPT_ENTRY},{GW_END}) coverage ===")
        print(f"  executed body instrs: {len(body_ords)}")
        print(
            f"  in CP window [0,{CP_END}): "
            f"{sum(1 for bb in body_bytes if bb < CP_END)}"
        )
        print(
            f"  in uncovered tail [{CP_END},{GW_END}): {len(tail)} instrs "
            f"(~{(GW_END-CP_END)} bytes region)"
        )
        return exec_ords, tail
    return exec_ords, None


if __name__ == "__main__":
    main()
