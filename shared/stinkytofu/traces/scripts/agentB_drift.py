#!/usr/bin/env python3
"""agentB step 2+3: cumulative drift analysis between matched instruction pairs,
and first-touch gap comparison in the CP-extend target window
(inst_id 0x1FE0..0x27E0, bytes [32640,40832)).
"""
import sys
from agentB_align import busiest
import difflib
from collections import defaultdict

WIN_LO, WIN_HI = 0x1FE0, 0x27E0  # dword ids; bytes 32640..40832


def align(A, B):
    ta = [r[4] for r in A]
    tb = [r[4] for r in B]
    sm = difflib.SequenceMatcher(a=ta, b=tb, autojunk=False)
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
    a0, b0 = A[0][6], B[0][6]

    # per-transition gap difference (gapB - gapA) between consecutive matched pairs
    steps = []  # (idxA_to, gapB - gapA, inst_id_to)
    prev = None
    for ia, ib in matched:
        if prev is not None:
            pa_i, pb_i = prev
            gapA = A[ia][6] - A[pa_i][6]
            gapB = B[ib][6] - B[pb_i][6]
            steps.append((ia, gapB - gapA, A[ia][5]))
        prev = (ia, ib)

    # cumulative drift over the run
    drift = 0
    drift_at = []
    for ia, dd, iid in steps:
        drift += dd
        drift_at.append((ia, drift, iid))

    print("## STEP 2: cumulative drift (B-A) of matched-pair relative issue time ##")
    print(f"  matched pairs={len(matched)} inserted={len(inserted)}")
    print(
        f"  final drift = {drift_at[-1][1]:+d} TS  (== raw span diff minus burst-internal)"
    )

    # Report drift at key milestones
    def drift_near(target_iid):
        best = min(drift_at, key=lambda x: abs(x[0] - _idxA_of(A, target_iid)))
        return best

    # find first matched idxA whose inst_id >= insertion point
    print("\n  drift trajectory (sampled every ~2000 matched steps):")
    for k in range(0, len(drift_at), 2000):
        ia, dv, iid = drift_at[k]
        print(f"    matched#{k:5d} A_idx={ia:5d} inst_id=0x{iid:04x}  drift={dv:+d}")
    ia, dv, iid = drift_at[-1]
    print(
        f"    matched#{len(drift_at)-1:5d} A_idx={ia:5d} inst_id=0x{iid:04x}  drift={dv:+d}  (END)"
    )

    # biggest positive (B slower) and negative (B faster) single transitions
    pos = sorted(steps, key=lambda x: -x[1])[:10]
    neg = sorted(steps, key=lambda x: x[1])[:10]
    print("\n  top transitions where B got SLOWER (gapB-gapA > 0):")
    for ia, dd, iid in pos:
        print(f"    A_idx={ia:5d} inst_id=0x{iid:04x} (byte {iid*4})  +{dd}")
    print("  top transitions where B got FASTER (gapB-gapA < 0):")
    for ia, dd, iid in neg:
        print(f"    A_idx={ia:5d} inst_id=0x{iid:04x} (byte {iid*4})  {dd}")

    # STEP 3: first-touch gap in the target window, per run.
    def window_firsttouch(recs):
        seen = set()
        prev = None
        gap_in_win = 0
        n = 0
        details = []
        for r in recs:
            iid, ts = r[5], r[6]
            if prev is not None:
                gap = ts - prev
                if iid not in seen and WIN_LO <= iid < WIN_HI:
                    if gap > 1:
                        gap_in_win += gap - 1
                    n += 1
                    if gap - 1 > 0:
                        details.append((iid, gap - 1))
            seen.add(iid)
            prev = ts
        return gap_in_win, n, details

    ga, na, da = window_firsttouch(A)
    gb, nb, db = window_firsttouch(B)
    print(
        "\n## STEP 3: CP-extend target window inst_id[0x1FE0,0x27E0) bytes[32640,40832) ##"
    )
    print(f"  NO-cpex : first-touch insts={na}  fetch-stall cycles={ga}")
    print(f"  WITH-cpex: first-touch insts={nb}  fetch-stall cycles={gb}")
    print(
        f"  window stall delta (B-A) = {gb-ga:+d}  ({'shrank' if gb<ga else 'grew' if gb>ga else 'unchanged'})"
    )
    # show largest stalls in window for each
    print("  NO-cpex top window stalls:")
    for iid, s in sorted(da, key=lambda x: -x[1])[:8]:
        print(f"    inst_id=0x{iid:04x} (byte {iid*4})  stall={s}")
    print("  WITH-cpex top window stalls:")
    for iid, s in sorted(db, key=lambda x: -x[1])[:8]:
        print(f"    inst_id=0x{iid:04x} (byte {iid*4})  stall={s}")


def _idxA_of(A, iid):
    for i, r in enumerate(A):
        if r[5] == iid:
            return i
    return 0


if __name__ == "__main__":
    main()
