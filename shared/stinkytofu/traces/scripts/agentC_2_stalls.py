"""agentC part 3+4: classify fetch stalls & quantify prefetch payoff.

Model
-----
inst_id is a monotonic per-static-instruction index over the program layout
(taken + not-taken slots). Byte offset of an instruction ~= inst_id * B, with
B bytes/inst (gfx1250 SALU/VALU are 4-8 B; we report B=4 primary, B=8 sens).

A "fetch stall" = an issue gap (>1 cycle) before an inst_id touched for the
FIRST time (every inst_id is unique here, so ALL gaps are first-touch =>
compulsory misses in the no-prefetch sense). We then bucket each stall by the
byte region its line falls in, to see which prefetch strategy could warm it:

  CP-covered      offset <  32640                 (CP hw preload window)
  one-shot        32640 <= offset < 32640+24576   (leading N=6 ladder = 24 KiB)
  tail(stream)    offset >= 57216                 (beyond a single 24 KiB burst)

Recoverability:
  * would-be-covered-by-CP : stalls in [0,32640) the CP preload should erase.
  * one-shot burst         : deep-block stalls only within the first 24 KiB.
  * ROLLING prefetch       : stays N lines ahead -> warms the ENTIRE deep-block
                             stream; recovers ~all deep-block stalls minus an
                             un-hideable startup ramp (first prefetch latency).
  * CompactLoopStore       : shrink code < 64 KiB so it fits I-cache -> the
                             deep streaming component disappears (becomes reuse).
"""

import sys
from collections import defaultdict
from agentC_common import parse, busiest_wave

ICACHE = 64 * 1024
CP_WINDOW = 32640
LADDER_N = 6
PREFETCH_CHUNK = 4096
ONESHOT_BYTES = LADDER_N * PREFETCH_CHUNK  # 24576
LINE = 128


def fetch_stalls(w):
    """Return list of (inst_id, stall_cycles) for first-touch gaps."""
    seen = set()
    out = []
    prev = None
    for r in w:
        iid, ts = r[5], r[6]
        if prev is not None:
            gap = ts - prev
            if gap > 1 and iid not in seen:
                out.append((iid, gap - 1))
        seen.add(iid)
        prev = ts
    return out


def analyze(path, B):
    recs = parse(path)
    mw, w = busiest_wave(recs)
    span = w[-1][6] - w[0][6]
    stalls = fetch_stalls(w)
    total_fetch = sum(s for _, s in stalls)

    oneshot_hi = CP_WINDOW + ONESHOT_BYTES
    cp_cyc = os_cyc = tail_cyc = 0
    cp_n = os_n = tail_n = 0
    deep_ids = []
    for iid, s in stalls:
        off = iid * B
        if off < CP_WINDOW:
            cp_cyc += s
            cp_n += 1
        else:
            deep_ids.append((off, s))
            if off < oneshot_hi:
                os_cyc += s
                os_n += 1
            else:
                tail_cyc += s
                tail_n += 1
    deep_cyc = os_cyc + tail_cyc

    # startup ramp not hideable by rolling prefetch: ~ latency of the single
    # biggest early deep-block miss (first prefetch can't be warm instantly).
    # Estimate ramp = largest single deep-block stall (conservative upper bound).
    ramp = max((s for _, s in deep_ids), default=0)
    rolling_recover = deep_cyc - ramp

    print(f"==== {path}  (B={B} bytes/inst)")
    print(
        f"  TS span={span}  total_fetch_stall={total_fetch} "
        f"({100.0*total_fetch/span:.1f}% of span)"
    )
    print(
        f"  [compulsory] every stall is a first-touch miss = {total_fetch} "
        f"(100% of fetch stall)"
    )
    print(f"  --- coverage buckets by byte offset (offset=inst_id*{B}) ---")
    print(
        f"  (a) would-be-covered-by-CP  [0,{CP_WINDOW})        : "
        f"{cp_cyc:>6} cyc  ({100.0*cp_cyc/total_fetch:4.1f}%)  n={cp_n}"
    )
    print(
        f"  (b) one-shot burst  [{CP_WINDOW},{oneshot_hi}) 24KiB: "
        f"{os_cyc:>6} cyc  ({100.0*os_cyc/total_fetch:4.1f}%)  n={os_n}"
    )
    print(
        f"  (c) streaming tail  [{oneshot_hi},end) NOT 1-shot  : "
        f"{tail_cyc:>6} cyc  ({100.0*tail_cyc/total_fetch:4.1f}%)  n={tail_n}"
    )
    print(
        f"  deep-block total (beyond CP) = {deep_cyc} cyc "
        f"({100.0*deep_cyc/total_fetch:.1f}%)"
    )
    print(f"  --- prefetch payoff (recoverable fetch-stall cycles) ---")
    print(
        f"  one-shot ladder N=6 (24KiB)      : ~{os_cyc:>6} cyc "
        f"({100.0*os_cyc/total_fetch:4.1f}% of fetch, "
        f"{100.0*os_cyc/span:4.1f}% of span)"
    )
    print(
        f"  ROLLING prefetch (N ahead, ramp={ramp}): ~{rolling_recover:>6} cyc "
        f"({100.0*rolling_recover/total_fetch:4.1f}% of fetch, "
        f"{100.0*rolling_recover/span:4.1f}% of span)"
    )
    print(
        f"  CompactLoopStore (fit<64KiB)     : removes deep stream = up to "
        f"~{deep_cyc} cyc, and lets CP window cover the rest"
    )
    print()
    return dict(
        total=total_fetch,
        cp=cp_cyc,
        os=os_cyc,
        tail=tail_cyc,
        deep=deep_cyc,
        ramp=ramp,
        rolling=rolling_recover,
        span=span,
    )


if __name__ == "__main__":
    files = [
        "f8f8s_sipa_beta1_gsu1.mon",
        "f8f8s_sipa_cpex_beta1_gsu1.mon",
    ]
    Bs = [int(x) for x in sys.argv[1:]] or [4, 8]
    for B in Bs:
        for f in files:
            analyze(f, B)
        print("-" * 70)
