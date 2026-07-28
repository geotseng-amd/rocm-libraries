#!/usr/bin/env python3
"""Deliverable 4: attribute the -8032 fetch-stall saving between the CP-extend
target window [32636,40828) (a "real hit" would mean the executed OptNLL code in
that window got prefetched) vs downstream ripple.

Splits the aligned per-static-instruction delta into gains vs losses per region
and per TT-type, so we can say how many of the 8032 cycles come from the window
vs the tail.
"""
import sys
import difflib
from collections import defaultdict
import optnllwin_common as C


def static_stream(d):
    return [(iid, d["type_by_id"][iid]) for iid in d["order_ids"]]


def align(A, B):
    sa = static_stream(A)
    sb = static_stream(B)
    sm = difflib.SequenceMatcher(
        a=[t for _, t in sa], b=[t for _, t in sb], autojunk=False
    )
    matched = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            matched += [(i1 + k, j1 + k) for k in range(i2 - i1)]
    return sa, sb, matched


def region_of(byte_b):
    if byte_b < C.CPEX_BYTE_LO:
        return "1_pre_window"
    if byte_b < C.CPEX_BYTE_HI:
        return "2_CPEX_WINDOW"
    if byte_b < 81920:
        return "3_post_window_mid"
    return "4_deep_tail(>=81920)"


def main():
    pa = (
        sys.argv[1]
        if len(sys.argv) > 1
        else (
            "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/"
            "traces/f8f8s_sipa_beta0_gsu1_alpha1_optnll.mon"
        )
    )
    pb = (
        sys.argv[2]
        if len(sys.argv) > 2
        else (
            "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/"
            "traces/f8f8s_sipa_cpex_beta0_gsu1_alpha1_optnll.mon"
        )
    )
    A = C.load(pa)
    B = C.load(pb)
    fa, fb = A["fetch_by_id"], B["fetch_by_id"]
    sa, sb, matched = align(A, B)

    reg = defaultdict(lambda: [0, 0, 0, 0])  # NO, WITH, gains(neg), losses(pos)
    for ia, ib in matched:
        idb = sb[ib][0]
        s_no = fa.get(sa[ia][0], 0)
        s_wi = fb.get(idb, 0)
        d = s_wi - s_no
        e = reg[region_of(idb * 4)]
        e[0] += s_no
        e[1] += s_wi
        if d < 0:
            e[2] += d
        elif d > 0:
            e[3] += d

    print("## Deliverable 4: regional attribution of the fetch-stall delta ##")
    print(
        f"  {'region':<24}{'NO':>8}{'WITH':>8}{'net':>8}" f"{'gains':>9}{'losses':>8}"
    )
    tot = 0
    for k in sorted(reg):
        s_no, s_wi, g, l = reg[k]
        net = s_wi - s_no
        tot += net
        print(f"  {k:<24}{s_no:>8}{s_wi:>8}{net:>+8}{g:>+9}{l:>+8}")
    print(f"  {'TOTAL':<24}{'':>8}{'':>8}{tot:>+8}")

    # window internal composition by type (real prefetch hits vs regressions)
    print(
        f"\n## CP-extend WINDOW [{C.CPEX_BYTE_LO},{C.CPEX_BYTE_HI}) "
        f"internal composition by TT-type ##"
    )
    by_t = defaultdict(lambda: [0, 0])  # gains, losses
    win_net = 0
    for ia, ib in matched:
        idb = sb[ib][0]
        byte_b = idb * 4
        if not (C.CPEX_BYTE_LO <= byte_b < C.CPEX_BYTE_HI):
            continue
        d = fb.get(idb, 0) - fa.get(sa[ia][0], 0)
        win_net += d
        t = sb[ib][1]
        if d < 0:
            by_t[t][0] += d
        elif d > 0:
            by_t[t][1] += d
    print(f"  {'type':<26}{'gains':>9}{'losses':>9}{'net':>8}")
    for t in sorted(by_t, key=lambda x: by_t[x][0] + by_t[x][1]):
        g, l = by_t[t]
        print(f"  {t:<26}{g:>+9}{l:>+9}{g+l:>+8}")
    print(
        f"  window net = {win_net:+d}  " f"(gross prefetch gains offset by regressions)"
    )

    # deep tail composition by type (where the real saving lives)
    print("\n## DEEP TAIL (byte>=81920) composition by TT-type ##")
    by_t2 = defaultdict(lambda: [0, 0, 0, 0])  # NO,WITH,gains,losses
    for ia, ib in matched:
        idb = sb[ib][0]
        if idb * 4 < 81920:
            continue
        s_no = fa.get(sa[ia][0], 0)
        s_wi = fb.get(idb, 0)
        d = s_wi - s_no
        t = sb[ib][1]
        e = by_t2[t]
        e[0] += s_no
        e[1] += s_wi
        if d < 0:
            e[2] += d
        elif d > 0:
            e[3] += d
    print(f"  {'type':<26}{'NO':>8}{'WITH':>8}{'net':>8}")
    for t in sorted(by_t2, key=lambda x: by_t2[x][1] - by_t2[x][0]):
        s_no, s_wi, g, l = by_t2[t]
        print(f"  {t:<26}{s_no:>8}{s_wi:>8}{s_wi-s_no:>+8}")


if __name__ == "__main__":
    main()
