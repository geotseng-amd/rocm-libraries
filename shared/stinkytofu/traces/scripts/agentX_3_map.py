"""Deliverable 3: tie the trace epilogue cluster to the STATIC Case-C block and
split its fetch-stall cycles into (a) inside the one-shot ladder window
[235340, 259916) = first 24576 B of the block, vs (b) OUTSIDE it (uncoverable
by the one-shot burst).

Method (trace<->static tie):
  * The executed Case-C path issues exactly 128 buffer_store_b32 + 128
    buffer_load_b32 (verified in agentX_2). Two static candidates have that
    signature: the entry/NonEdge body and the ..._Then body. We pick the one
    whose static store spacing best matches the trace store spacing.
  * Each of the 128 stores is an ANCHOR: (trace inst_id) <-> (static byte addr).
    Between consecutive anchors both streams are straight-line, so we map any
    cluster inst_id to a static byte by piecewise-linear interpolation on
    inst_id, extrapolating with the nearest slope outside the anchor span.
  * Each first-touch (fetch) stall gap precedes an inst_id; attribute the stall
    to that inst_id's mapped static byte, then bucket vs the ladder window.
"""

import sys
from agentC_common import parse, busiest_wave
from agentX_static import parse_obj, region, PASS_DELTA

BLOCK_LO = 235340
BLOCK_SZ = 177636
LADDER = 24576
LADDER_HI = BLOCK_LO + LADDER  # 259916
MAIN_LOOP_TYPES = {"TT_INST_WMMA_XDL_4", "TT_INST_LDS_RD", "TT_INST_LDS_OTHER_SIMD_1"}


def static_store_paths(objpath):
    insns, labels = parse_obj(objpath)
    lo = BLOCK_LO + PASS_DELTA
    then_lo = next(a for a, n in labels.items() if n == "label_GW_B1_FD0_VW4_GSU1_Then")
    else_lo = next(a for a, n in labels.items() if n == "label_GW_B1_FD0_VW4_GSU1_Else")
    blk = region(insns, lo, lo + BLOCK_SZ)
    entry = [
        a - PASS_DELTA
        for a, sz, m, op in blk
        if m == "buffer_store_b32" and a < then_lo
    ]
    then = [
        a - PASS_DELTA
        for a, sz, m, op in blk
        if m == "buffer_store_b32" and then_lo <= a < else_lo
    ]
    return {"entry": sorted(entry), "Then": sorted(then)}


def cluster_of(w):
    w = sorted(w, key=lambda r: r[5])
    last_main = max(i for i, r in enumerate(w) if r[4] in MAIN_LOOP_TYPES)
    return w, w[last_main + 1 :]


def spacing_corr(trace_ids, static_addrs):
    """Compare normalized inter-anchor spacing; return mean abs relative error."""
    n = len(trace_ids)
    ti = [trace_ids[i + 1] - trace_ids[i] for i in range(n - 1)]
    sa = [static_addrs[i + 1] - static_addrs[i] for i in range(n - 1)]
    st = sum(ti) / sum(sa)  # global exec-dword-per-static-byte
    err = sum(abs(ti[i] - sa[i] * st) for i in range(n - 1)) / sum(ti)
    return st, err


def build_map(anchor_ids, anchor_addrs):
    """Piecewise-linear inst_id -> static byte; anchors are the store points."""

    def f(iid):
        if iid <= anchor_ids[0]:
            # extrapolate with first segment slope
            i0, i1 = anchor_ids[0], anchor_ids[1]
            a0, a1 = anchor_addrs[0], anchor_addrs[1]
        elif iid >= anchor_ids[-1]:
            i0, i1 = anchor_ids[-2], anchor_ids[-1]
            a0, a1 = anchor_addrs[-2], anchor_addrs[-1]
        else:
            lo = 0
            hi = len(anchor_ids) - 1
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if anchor_ids[mid] <= iid:
                    lo = mid
                else:
                    hi = mid
            i0, i1 = anchor_ids[lo], anchor_ids[hi]
            a0, a1 = anchor_addrs[lo], anchor_addrs[hi]
        if i1 == i0:
            return a0
        return a0 + (a1 - a0) * (iid - i0) / (i1 - i0)

    return f


def analyze(monpath, objpath):
    recs = parse(monpath)
    mw, ww = busiest_wave(recs)
    w, cluster = cluster_of(ww)

    trace_store_ids = [r[5] for r in cluster if r[4] == "TT_INST_BUF_WR_1"]
    paths = static_store_paths(objpath)

    print(f"==== {monpath}")
    print(
        f"  trace stores={len(trace_store_ids)}  "
        f"static entry-body stores={len(paths['entry'])}  "
        f"Then stores={len(paths['Then'])}"
    )

    # pick best matching static path by store-spacing
    best = None
    for name, addrs in paths.items():
        if len(addrs) != len(trace_store_ids):
            continue
        slope, err = spacing_corr(trace_store_ids, addrs)
        print(
            f"    candidate '{name}': slope={slope:.4f} exec_dw/B  "
            f"spacing_rel_err={err:.3f}  static_span="
            f"[{addrs[0]}..{addrs[-1]}]"
        )
        if best is None or err < best[2]:
            best = (name, addrs, err)
    name, addrs, err = best
    print(f"  --> executed path = '{name}' (best spacing fit)")

    fmap = build_map(trace_store_ids, addrs)

    # attribute first-touch (fetch) stall to mapped static byte
    inside = outside = 0
    total_fetch = 0
    prev_ts = None
    below_block = 0
    cl = sorted(cluster, key=lambda r: r[5])
    for r in cl:
        iid, ts = r[5], r[6]
        if prev_ts is not None:
            gap = ts - prev_ts
            if gap > 1:
                s = gap - 1
                total_fetch += s
                sb = fmap(iid)
                if sb < BLOCK_LO:
                    below_block += s  # entry code before first mapped store
                    inside += s  # entry precedes ladder start -> covered
                elif sb < LADDER_HI:
                    inside += s
                else:
                    outside += s
        prev_ts = ts

    print(f"  cluster fetch-stall total = {total_fetch} clk")
    print(
        f"    inside ladder window  [<{LADDER_HI}] = {inside} "
        f"({100.0*inside/total_fetch:.1f}%)  (incl below-block entry={below_block})"
    )
    print(
        f"    OUTSIDE ladder window [>={LADDER_HI}] = {outside} "
        f"({100.0*outside/total_fetch:.1f}%)  <-- uncoverable by one-shot burst"
    )
    # coverage by static extent
    covered_B = min(LADDER, addrs[-1] - BLOCK_LO)
    exec_static_extent = addrs[-1] - min(addrs[0], BLOCK_LO)
    print(
        f"  executed static extent within block: "
        f"[{min(addrs[0],BLOCK_LO)}..{addrs[-1]}] = ~{exec_static_extent} B; "
        f"ladder covers {covered_B} B "
        f"({100.0*covered_B/exec_static_extent:.1f}% of executed extent, "
        f"{100.0*LADDER/BLOCK_SZ:.1f}% of full block)"
    )
    print()
    return dict(
        total_fetch=total_fetch, inside=inside, outside=outside, span=w[-1][6] - w[0][6]
    )


if __name__ == "__main__":
    objpath = sys.argv[1]
    mons = sys.argv[2:] or [
        "f8f8s_sipa_beta1_gsu1.mon",
        "f8f8s_sipa_cpex_beta1_gsu1.mon",
    ]
    for m in mons:
        analyze(m, objpath)
