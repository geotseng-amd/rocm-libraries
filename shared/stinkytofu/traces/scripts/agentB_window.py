#!/usr/bin/env python3
"""agentB step 2/3 refinement: use matched pairs (correct static-instruction
correspondence, immune to the +6 dword PC shift) to measure:
  - exact cost of the inserted burst,
  - drift immediately before/after the burst,
  - first-touch stall change for the SAME static instructions in the
    CP-extend target window bytes[32640,40832).
"""
import sys
import difflib
from agentB_align import busiest

WIN_LO_BYTE, WIN_HI_BYTE = 32640, 40832


def align(A, B):
    sm = difflib.SequenceMatcher(
        a=[r[4] for r in A], b=[r[4] for r in B], autojunk=False
    )
    matched, inserted = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                matched.append((i1 + k, j1 + k))
        elif tag in ("insert", "replace"):
            for j in range(j1, j2):
                inserted.append(j)
    return matched, inserted


def main():
    pa, pb = sys.argv[1], sys.argv[2]
    _, A = busiest(pa)
    _, B = busiest(pb)
    matched, inserted = align(A, B)

    # exact burst cost: TS of real inst before first insert vs real inst after last
    j_first = min(inserted)
    j_last = max(inserted)
    before_B = B[j_first - 1]  # last matched inst before burst
    after_B = B[j_last + 1]  # first matched inst after burst
    # its counterpart in A
    a_before = a_after = None
    for ia, ib in matched:
        if ib == j_first - 1:
            a_before = A[ia]
        if ib == j_last + 1:
            a_after = A[ia]
    print("## exact inserted-burst cost ##")
    print(
        f"  B: last inst before burst  idx{j_first-1} id0x{before_B[5]:04x} {before_B[4]} TS={before_B[6]}"
    )
    for j in inserted:
        r = B[j]
        print(f"     INSERTED idx{j} id0x{r[5]:04x} {r[4]:14s} TS={r[6]}")
    print(
        f"  B: first inst after burst  idx{j_last+1} id0x{after_B[5]:04x} {after_B[4]} TS={after_B[6]}"
    )
    print(f"  B elapsed across burst (after-before) = {after_B[6]-before_B[6]} TS")
    if a_before and a_after:
        print(
            f"  A same transition           id0x{a_before[5]:04x}->0x{a_after[5]:04x} "
            f"TS {a_before[6]}->{a_after[6]} elapsed={a_after[6]-a_before[6]} TS"
        )
        print(
            f"  >>> net burst insertion cost = {(after_B[6]-before_B[6])-(a_after[6]-a_before[6]):+d} TS"
        )

    # drift right at boundary
    def rel(recs, idx, base):
        return recs[idx][6] - base

    a0, b0 = A[0][6], B[0][6]
    # drift at the matched pair just before burst and just after
    ib_before = j_first - 1
    ib_after = j_last + 1
    for ia, ib in matched:
        if ib == ib_before:
            d_before = (B[ib][6] - b0) - (A[ia][6] - a0)
        if ib == ib_after:
            d_after = (B[ib][6] - b0) - (A[ia][6] - a0)
    print(
        f"\n  drift just BEFORE burst = {d_before:+d} ;  just AFTER burst = {d_after:+d}"
    )

    # STEP 3 refined: window by matched pairs (A inst_id*4 in target byte range)
    prev = None
    win_gapA = win_gapB = 0
    win_n = 0
    seenA = set()
    seenB = set()

    # need first-touch semantics per run; matched pairs are in order & 1:1 here
    # compute first-touch stall per static inst using each run's own prev-issue.
    # Build per-run prev arrays.
    def firsttouch_map(recs):
        seen = set()
        prev = None
        m = {}
        for i, r in enumerate(recs):
            iid, ts = r[5], r[6]
            stall = 0
            if prev is not None and iid not in seen:
                g = ts - prev
                stall = g - 1 if g > 1 else 0
            m[i] = stall
            seen.add(iid)
            prev = ts
        return m

    fa = firsttouch_map(A)
    fb = firsttouch_map(B)
    sumA = sumB = cnt = 0
    for ia, ib in matched:
        b = A[ia][5] * 4
        if WIN_LO_BYTE <= b < WIN_HI_BYTE:
            sumA += fa[ia]
            sumB += fb[ib]
            cnt += 1
    print(
        "\n## STEP 3 (matched, same static insts) target window bytes[32640,40832) ##"
    )
    print(f"  matched static insts in window = {cnt}")
    print(f"  NO-cpex  first-touch stall = {sumA}")
    print(f"  WITH-cpex first-touch stall = {sumB}")
    print(f"  window stall delta (B-A) = {sumB-sumA:+d}")


if __name__ == "__main__":
    main()
