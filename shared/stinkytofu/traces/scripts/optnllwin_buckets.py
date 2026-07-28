#!/usr/bin/env python3
"""Deliverables 2 & 3: per-static-instruction first-touch fetch stall in BOTH
runs, aligned across the +6 insertion, bucketed by static byte offset, with
before/after/delta. Then the exact CP-extend target window [32636,40828).

Every static instruction is compared to ITS matched counterpart (same TT-type
sequence), so id-shift of +6 after the early cover-burst insertion is handled.
Byte offset for the WITH-cpex layout = inst_id_B * 4 (the address the prefetch
actually targets).
"""
import sys
import difflib
import optnllwin_common as C

BUCKET_BYTES = 4096  # 1024 ids per bucket for the coarse histogram


def static_stream(d):
    return [(iid, d["type_by_id"][iid]) for iid in d["order_ids"]]


def align(A, B):
    sa = static_stream(A)
    sb = static_stream(B)
    ta = [t for _, t in sa]
    tb = [t for _, t in sb]
    sm = difflib.SequenceMatcher(a=ta, b=tb, autojunk=False)
    matched, inserted, deleted = [], [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            matched += [(i1 + k, j1 + k) for k in range(i2 - i1)]
        elif tag == "insert":
            inserted += list(range(j1, j2))
        elif tag == "delete":
            deleted += list(range(i1, i2))
        elif tag == "replace":
            deleted += list(range(i1, i2))
            inserted += list(range(j1, j2))
    return sa, sb, matched, inserted, deleted


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

    A = C.load(pa)  # NO cpex
    B = C.load(pb)  # WITH cpex
    fa, fb = A["fetch_by_id"], B["fetch_by_id"]
    sa, sb, matched, inserted, deleted = align(A, B)

    print(f"NO-cpex   fetch_stall total = {A['fetch_total']}")
    print(f"WITH-cpex fetch_stall total = {B['fetch_total']}")
    print(f"overall delta (WITH-NO)     = {B['fetch_total']-A['fetch_total']:+d}")
    print(
        f"matched static pairs={len(matched)} inserted(B)={len(inserted)} "
        f"deleted(A)={len(deleted)}"
    )

    # ---- per-matched-pair delta, bucketed by WITH-cpex byte offset ----
    buckets = {}  # bucket_index -> [stall_NO, stall_WITH, npairs]
    reconcile = 0
    for ia, ib in matched:
        ida = sa[ia][0]
        idb = sb[ib][0]
        s_no = fa.get(ida, 0)
        s_wi = fb.get(idb, 0)
        reconcile += s_wi - s_no
        byte_b = idb * 4
        bk = byte_b // BUCKET_BYTES
        e = buckets.setdefault(bk, [0, 0, 0])
        e[0] += s_no
        e[1] += s_wi
        e[2] += 1

    ins_stall = sum(fb.get(sb[j][0], 0) for j in inserted)
    del_stall = sum(fa.get(sa[i][0], 0) for i in deleted)
    reconcile += ins_stall - del_stall

    print(f"\ninserted-instruction fetch stall (B only) = {ins_stall}")
    print(f"deleted-instruction  fetch stall (A only) = {del_stall}")
    print(
        f"reconciliation sum of all deltas = {reconcile:+d} "
        f"(should equal overall delta)"
    )

    print(
        f"\n## Deliverable 2: fetch stall bucketed by static byte "
        f"(bucket={BUCKET_BYTES}B = {BUCKET_BYTES//4} ids), WITH-cpex layout ##"
    )
    print(f"  {'byte range':<20}{'ids':>6}{'NO':>9}{'WITH':>9}{'delta':>9}")
    for bk in sorted(buckets):
        lo = bk * BUCKET_BYTES
        hi = lo + BUCKET_BYTES - 1
        s_no, s_wi, n = buckets[bk]
        d = s_wi - s_no
        star = "  <==" if d <= -400 else ""
        print(f"  {lo:6d}-{hi:<13d}{n:>6}{s_no:>9}{s_wi:>9}{d:>+9}{star}")

    # rank buckets by improvement
    ranked = sorted(buckets.items(), key=lambda kv: (kv[1][1] - kv[1][0]))
    print("\n  -- top improving buckets (largest cycle reductions) --")
    cum = 0
    tot_delta = reconcile
    for bk, (s_no, s_wi, n) in ranked[:8]:
        d = s_wi - s_no
        cum += d
        lo = bk * BUCKET_BYTES
        print(
            f"    byte {lo:6d}-{lo+BUCKET_BYTES-1:<6d}  NO={s_no:>6} "
            f"WITH={s_wi:>6} delta={d:>+6}  "
            f"({100.0*d/tot_delta:5.1f}% of total saving)"
        )

    # ---- Deliverable 3: exact CP-extend window [32636, 40828) ----
    print(
        f"\n## Deliverable 3: CP-extend target window bytes "
        f"[{C.CPEX_BYTE_LO}, {C.CPEX_BYTE_HI}) "
        f"= WITH-cpex ids [0x{C.CPEX_ID_LO:04x}, 0x{C.CPEX_ID_HI:04x}) ##"
    )
    win_no = win_wi = win_n = 0
    detail = []
    for ia, ib in matched:
        idb = sb[ib][0]
        byte_b = idb * 4
        if C.CPEX_BYTE_LO <= byte_b < C.CPEX_BYTE_HI:
            ida = sa[ia][0]
            s_no = fa.get(ida, 0)
            s_wi = fb.get(idb, 0)
            win_no += s_no
            win_wi += s_wi
            win_n += 1
            if abs(s_wi - s_no) > 0:
                detail.append((byte_b, ida, idb, s_no, s_wi, sb[ib][1]))
    print(f"  matched static instrs in window = {win_n}")
    print(
        f"  fetch stall in window: NO={win_no}  WITH={win_wi}  "
        f"delta={win_wi-win_no:+d}"
    )
    frac = (win_wi - win_no) / tot_delta * 100 if tot_delta else 0
    print(f"  window delta as fraction of total saving = {frac:.1f}%")

    print("\n  -- per-instruction changes inside the window (|delta|>0) --")
    print(
        f"  {'byteB':>7}{'idNO':>7}{'idWITH':>8}{'NO':>7}{'WITH':>7}"
        f"{'delta':>7}  type"
    )
    for byte_b, ida, idb, s_no, s_wi, t in sorted(detail, key=lambda x: (x[4] - x[3])):
        print(
            f"  {byte_b:>7}{ida:>7}{idb:>8}{s_no:>7}{s_wi:>7}" f"{s_wi-s_no:>+7}  {t}"
        )

    # ---- context: what happens OUTSIDE the window (downstream ripple) ----
    out_no = out_wi = 0
    for ia, ib in matched:
        idb = sb[ib][0]
        byte_b = idb * 4
        if not (C.CPEX_BYTE_LO <= byte_b < C.CPEX_BYTE_HI):
            out_no += fa.get(sa[ia][0], 0)
            out_wi += fb.get(idb, 0)
    print(
        f"\n  OUTSIDE window: NO={out_no} WITH={out_wi} "
        f"delta={out_wi-out_no:+d} "
        f"({100.0*(out_wi-out_no)/tot_delta:.1f}% of total saving)"
    )
    # split outside into before-window and after-window
    pre_no = pre_wi = post_no = post_wi = 0
    for ia, ib in matched:
        idb = sb[ib][0]
        byte_b = idb * 4
        s_no = fa.get(sa[ia][0], 0)
        s_wi = fb.get(idb, 0)
        if byte_b < C.CPEX_BYTE_LO:
            pre_no += s_no
            pre_wi += s_wi
        elif byte_b >= C.CPEX_BYTE_HI:
            post_no += s_no
            post_wi += s_wi
    print(
        f"    before window (byte<{C.CPEX_BYTE_LO}): NO={pre_no} "
        f"WITH={pre_wi} delta={pre_wi-pre_no:+d}"
    )
    print(
        f"    after  window (byte>={C.CPEX_BYTE_HI}): NO={post_no} "
        f"WITH={post_wi} delta={post_wi-post_no:+d}"
    )


if __name__ == "__main__":
    main()
