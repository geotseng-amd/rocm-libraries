#!/usr/bin/env python3
"""optnll_mid_map.py  (READ-ONLY)

Builds the validated dynamic-issue -> static-byte mapping for the OptNLL
epilogue of BOTH traces (NO-cpex from optnll_cache.pkl, WITH-cpex parsed once via
optnllwin_common), reusing the TT-type / program-order forward-only greedy
alignment from optnll_map_align.py.

Then answers deliverables 1 & 2:
  Q1: map the NO-cpex fetch-stall in clock window [80000,90000) to STATIC byte
      ranges; classify HEAD [20504,32636) vs covered [32636,40828) vs tail.
  Q2: head [20504,32636) first-touch fetch-stall cycles, NO vs WITH.

Exposes build_map() for reuse by optnll_mid_verdict.py.
"""
import pickle
import sys
from collections import Counter

sys.path.insert(
    0, "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/scripts"
)
import optnllwin_common as C

CACHE = "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/scripts/optnll_cache.pkl"
WI = "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/f8f8s_sipa_cpex_beta0_gsu1_alpha1_optnll.mon"

OPT = 20504  # label_GW_B0_OptNLL_MB (obj#3586)
HEAD_HI = 32636  # label_SW_PrefetchAbs_CpBoundary (obj#5744) = CP preload end
CPEX_HI = 40828  # CP-extend cover end (uncovered tail begins)
GWEND = 46940  # label_GW_End (obj#8140)


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


COMPAT = {
    "BUF_WR": {"BUF_WR"},
    "BUF_RD": {"BUF_RD"},
    "ICPREF": {"ICPREF"},
    "BRANCH": {"BRANCH"},
    "WMMA": {"WMMA"},
    "VALU": {"VALU"},
    "SALU": {"SALU", "SMEM"},
    "SMEM": {"SMEM", "SALU"},
    "LDS": {"LDS"},
    "OTHER": None,
}


def greedy_align(dyn_classes, static_classes, start_ord, allow_skip=600):
    """Forward-only monotonic walk. Returns path[k] = static ordinal (or None)."""
    sp = start_ord
    n = len(static_classes)
    path = []
    matched = 0
    for dc in dyn_classes:
        if dc == "OTHER":
            path.append(None)
            continue
        target = None
        comp = COMPAT.get(dc, {dc})
        for j in range(sp, min(sp + allow_skip, n)):
            if comp is None or static_classes[j] in comp:
                target = j
                break
        if target is None:
            path.append(None)
            continue
        matched += 1
        sp = target + 1
        path.append(target)
    return path, matched


def store_anchored_ords(epi, dyn, insns, o_opt, o_end):
    """Robust mapping: the 128 dynamic epilogue stores map 1:1, in program
    order, to the 128 static stores in the OptNLL body. Between store anchors,
    execution is sequential, so interpolate each record's static ordinal
    linearly by its position between the bracketing store anchors (with virtual
    boundary anchors at body entry o_opt and body exit o_end-1). Returns
    path[k] = static ordinal (float rounded to int)."""
    byte_of = [b for b, _ in insns]
    scls = [mclass(m) for _, m in insns]
    static_store_ords = [i for i in range(o_opt, o_end) if scls[i] == "BUF_WR"]
    dyn_store_pos = [k for k, c in enumerate(dyn) if c == "BUF_WR"]
    assert len(static_store_ords) == len(dyn_store_pos), (
        len(static_store_ords),
        len(dyn_store_pos),
    )
    # anchor list (epi_pos, static_ord)
    anchors = [(0, o_opt)]
    for p, o in zip(dyn_store_pos, static_store_ords):
        anchors.append((p, o))
    anchors.append((len(dyn) - 1, o_end - 1))
    # dedupe/normalise positions monotonically
    path = [None] * len(dyn)
    for a in range(len(anchors) - 1):
        p0, o0 = anchors[a]
        p1, o1 = anchors[a + 1]
        if p1 <= p0:
            path[p0] = o0
            continue
        for p in range(p0, p1 + 1):
            frac = (p - p0) / (p1 - p0)
            path[p] = int(round(o0 + frac * (o1 - o0)))
    # ensure stores land exactly on their static store ordinal
    for p, o in zip(dyn_store_pos, static_store_ords):
        path[p] = o
    return path


def load_no():
    d = pickle.load(open(CACHE, "rb"))
    return d["insns"], d["labels"], d["wave"]


def load_wi():
    _, w = C.busiest_wave(WI)
    return [(r[4], r[5], r[6]) for r in w]


def build_map(insns, wave):
    """Align the OptNLL epilogue of `wave` to static bytes.
    Returns list of dicts: {ts, id, ttc, byte, ord, first_touch(bool), gap}
    for every epilogue record, plus meta.
    """
    byte_of = [b for b, _ in insns]
    scls = [mclass(m) for _, m in insns]
    o_opt = next(i for i, b in enumerate(byte_of) if b >= OPT)

    # split epilogue = after last WMMA/LDS
    last = 0
    for i, r in enumerate(wave):
        if ttclass(r[0]) in ("WMMA", "LDS"):
            last = i
    epi = wave[last + 1 :]
    dyn = [ttclass(r[0]) for r in epi]
    o_end = next(i for i, b in enumerate(byte_of) if b >= GWEND)
    # class-based greedy align (for match-rate sanity only)
    _, matched = greedy_align(dyn, scls, o_opt)
    # robust store-anchored ordinal mapping (used for byte attribution)
    path = store_anchored_ords(epi, dyn, insns, o_opt, o_end)

    # first-touch gap per whole-wave record (inst_id is dynamic & monotonic, so
    # every record is a first-touch; gap = TS - prevTS - 1). Compute over full
    # wave to get correct prev for the first epilogue record.
    recs = []
    for k, r in enumerate(epi):
        gi = last + 1 + k
        prev_ts = wave[gi - 1][2] if gi > 0 else r[2]
        gap = r[2] - prev_ts - 1
        o = path[k]
        recs.append(
            {
                "ts": r[2],
                "id": r[1],
                "ttc": ttclass(r[0]),
                "ord": o,
                "byte": byte_of[o] if o is not None else None,
                "gap": gap if gap > 0 else 0,
            }
        )
    return recs, matched, len(dyn), o_opt


def region(b):
    if b is None:
        return "unmapped"
    if b < OPT:
        return "pre_opt"
    if b < HEAD_HI:
        return "HEAD[20504,32636)"
    if b < CPEX_HI:
        return "COVERED[32636,40828)"
    if b < GWEND:
        return "TAIL[40828,46940)"
    return "beyond"


def main():
    insns, labels, wave_no = load_no()
    wave_wi = load_wi()

    rec_no, m_no, t_no, o_opt = build_map(insns, wave_no)
    rec_wi, m_wi, t_wi, _ = build_map(insns, wave_wi)
    print(
        f"epilogue alignment class-match: NO {m_no}/{t_no}="
        f"{100*m_no/t_no:.1f}%  WITH {m_wi}/{t_wi}={100*m_wi/t_wi:.1f}%"
    )

    # sanity: store ordering monotonic + head/tail store split
    for name, recs in (("NO", rec_no), ("WITH", rec_wi)):
        st = [r for r in recs if r["ttc"] == "BUF_WR"]
        bs = [r["byte"] for r in st if r["byte"] is not None]
        mono = all(bs[i] <= bs[i + 1] for i in range(len(bs) - 1))
        head_st = sum(1 for b in bs if OPT <= b < HEAD_HI)
        tail_st = sum(1 for b in bs if b >= HEAD_HI)
        print(
            f"  [{name}] BUF_WR mapped={len(bs)} monotonic={mono} "
            f"head_stores={head_st} tail_stores={tail_st} "
            f"byte[{min(bs)}..{max(bs)}]"
        )

    # ================= Q1: clock [80000,90000) -> static bytes (NO) =========
    print("\n" + "=" * 70)
    print("Q1: NO-cpex clock window [80000,90000) -> STATIC byte ranges")
    win = [r for r in rec_no if 80000 <= r["ts"] < 90000]
    bmapped = [r["byte"] for r in win if r["byte"] is not None]
    print(f"  epilogue records in window: {len(win)}  (mapped {len(bmapped)})")
    print(f"  aligned static byte span: [{min(bmapped)}, {max(bmapped)}]")
    bmapped_sorted = sorted(bmapped)
    med = bmapped_sorted[len(bmapped_sorted) // 2]
    print(f"  median aligned byte: {med}")
    # fetch-stall attribution by region within the window
    reg_stall = Counter()
    reg_cnt = Counter()
    for r in win:
        reg_stall[region(r["byte"])] += r["gap"]
        reg_cnt[region(r["byte"])] += 1
    tot_win_stall = sum(r["gap"] for r in win)
    print(f"  total fetch-stall in window = {tot_win_stall}")
    print(f"  {'region':<24}{'#rec':>6}{'stall':>9}{'%stall':>8}")
    for rg in (
        "pre_opt",
        "HEAD[20504,32636)",
        "COVERED[32636,40828)",
        "TAIL[40828,46940)",
        "beyond",
        "unmapped",
    ):
        if reg_cnt[rg] or reg_stall[rg]:
            pct = 100.0 * reg_stall[rg] / tot_win_stall if tot_win_stall else 0
            print(f"  {rg:<24}{reg_cnt[rg]:>6}{reg_stall[rg]:>9}{pct:>7.1f}%")

    # ================= Q2: HEAD [20504,32636) fetch-stall NO vs WITH ========
    print("\n" + "=" * 70)
    print("Q2: HEAD [20504,32636) first-touch fetch-stall (NO vs WITH)")
    for name, recs in (("NO", rec_no), ("WITH", rec_wi)):
        head = [r for r in recs if r["byte"] is not None and OPT <= r["byte"] < HEAD_HI]
        hstall = sum(r["gap"] for r in head)
        cov = [
            r for r in recs if r["byte"] is not None and HEAD_HI <= r["byte"] < CPEX_HI
        ]
        cstall = sum(r["gap"] for r in cov)
        tail = [
            r for r in recs if r["byte"] is not None and CPEX_HI <= r["byte"] < GWEND
        ]
        tstall = sum(r["gap"] for r in tail)
        body = [r for r in recs if r["byte"] is not None and OPT <= r["byte"] < GWEND]
        bstall = sum(r["gap"] for r in body)
        print(
            f"  [{name}] HEAD stall={hstall} ({len(head)} rec, "
            f"TS[{head[0]['ts']}..{head[-1]['ts']}])  "
            f"COVERED stall={cstall}  TAIL stall={tstall}  BODY={bstall}"
        )
    print("\n  -> HEAD delta (NO - WITH) computed in verdict script.")


if __name__ == "__main__":
    main()
