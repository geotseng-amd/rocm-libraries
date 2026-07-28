"""Deliverable 4: ceiling if the whole Case-C block were kept resident
(CompactLoopStore) or rolling-prefetched.

The epilogue cluster is a compulsory (no-loop) stream, so every inter-issue gap
is a first-touch = I-cache miss. Keeping the block resident / rolling-prefetch
removes ALL those epilogue fetch-stall cycles. The one-shot ladder can only
remove the fraction inside its 24576 B window (agentX_3). We report:
  - epilogue fetch-stall recoverable (clk) and as % of the ~96k full-wave span
  - the marginal cycles the one-shot leaves on the table (outside-window)
  - epilogue fetch-stall as a share of ALL wave fetch-stall
"""

import sys
from agentC_common import parse, busiest_wave
from agentX_3_map import (
    cluster_of,
    static_store_paths,
    spacing_corr,
    build_map,
    BLOCK_LO,
    LADDER_HI,
)


def wave_fetch_total(w):
    """Total first-touch fetch-stall over the whole wave (no loops -> all gaps)."""
    w = sorted(w, key=lambda r: r[5])
    tot = 0
    prev = None
    for r in w:
        if prev is not None and r[6] - prev > 1:
            tot += r[6] - prev - 1
        prev = r[6]
    return tot


def analyze(monpath, objpath):
    recs = parse(monpath)
    mw, ww = busiest_wave(recs)
    w, cluster = cluster_of(ww)
    span = w[-1][6] - w[0][6]

    trace_ids = [r[5] for r in cluster if r[4] == "TT_INST_BUF_WR_1"]
    addrs = static_store_paths(objpath)["entry"]
    fmap = build_map(trace_ids, addrs)

    cl = sorted(cluster, key=lambda r: r[5])
    cluster_fetch = inside = outside = 0
    prev = None
    for r in cl:
        if prev is not None and r[6] - prev > 1:
            s = r[6] - prev - 1
            cluster_fetch += s
            if fmap(r[5]) < LADDER_HI:
                inside += s
            else:
                outside += s
        prev = r[6]

    wave_fetch = wave_fetch_total(w)

    print(f"==== {monpath}")
    print(f"  full-wave TS span                     = {span}")
    print(
        f"  full-wave fetch-stall (all)           = {wave_fetch} "
        f"({100.0*wave_fetch/span:.1f}% of span)"
    )
    print(
        f"  epilogue (Case-C) fetch-stall         = {cluster_fetch} "
        f"({100.0*cluster_fetch/span:.1f}% of span; "
        f"{100.0*cluster_fetch/wave_fetch:.1f}% of all fetch-stall)"
    )
    print(f"  -- CEILING (block fully resident / rolling-prefetched) --")
    print(
        f"    recoverable epilogue fetch-stall    = {cluster_fetch} clk "
        f"= {100.0*cluster_fetch/span:.1f}% of the ~{round(span,-3):.0f} span"
    )
    print(f"  -- one-shot ladder (24576 B) achievable subset --")
    print(
        f"    inside-window (one-shot can remove) = {inside} clk "
        f"({100.0*inside/span:.1f}% of span)"
    )
    print(
        f"    outside-window LEFT ON THE TABLE    = {outside} clk "
        f"({100.0*outside/span:.1f}% of span; "
        f"{100.0*outside/cluster_fetch:.1f}% of epilogue fetch-stall)"
    )
    print()


if __name__ == "__main__":
    objpath = sys.argv[1]
    for m in sys.argv[2:] or [
        "f8f8s_sipa_beta1_gsu1.mon",
        "f8f8s_sipa_cpex_beta1_gsu1.mon",
    ]:
        analyze(m, objpath)
