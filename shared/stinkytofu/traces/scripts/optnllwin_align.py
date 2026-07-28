#!/usr/bin/env python3
"""Deliverable 1: align the two OptNLL instruction streams (NO-cpex vs
WITH-cpex), handle the +6 insertion (N=2 cover burst near label_MultiGemmEnd),
and confirm the inserted instructions + their location.

Alignment is done on the STATIC program (the ordered list of distinct inst_ids
in first-touch order), aligning by TT-type sequence so we compare the SAME
static instruction across runs even though ids shift by +6 after the insertion.
"""
import sys
import difflib
import optnllwin_common as C


def static_stream(d):
    """Ordered list of (inst_id, type) for distinct static instructions."""
    return [(iid, d["type_by_id"][iid]) for iid in d["order_ids"]]


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

    print("# A (NO   cpex):", pa)
    print(
        f"#   wave={A['wave']} n_inst={A['n_inst']} segs={A['n_segs']} "
        f"span={A['span']} distinct={A['distinct']} "
        f"fetch_stall={A['fetch_total']}"
    )
    print("# B (WITH cpex):", pb)
    print(
        f"#   wave={B['wave']} n_inst={B['n_inst']} segs={B['n_segs']} "
        f"span={B['span']} distinct={B['distinct']} "
        f"fetch_stall={B['fetch_total']}"
    )
    print(
        f"# distinct-id delta (B-A) = {B['distinct']-A['distinct']:+d} "
        f"(expect +6 inserted static instructions)"
    )

    sa = static_stream(A)
    sb = static_stream(B)
    ta = [t for _, t in sa]
    tb = [t for _, t in sb]
    sm = difflib.SequenceMatcher(a=ta, b=tb, autojunk=False)

    matched = []  # (idxA, idxB) into static streams
    inserted = []  # idxB only-in-B
    deleted = []  # idxA only-in-A
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                matched.append((i1 + k, j1 + k))
        elif tag == "insert":
            inserted += list(range(j1, j2))
        elif tag == "delete":
            deleted += list(range(i1, i2))
        elif tag == "replace":
            deleted += list(range(i1, i2))
            inserted += list(range(j1, j2))

    print("\n## STEP 1: static instructions present ONLY in WITH-cpex ##")
    print(f"inserted (B-only) = {len(inserted)}   deleted (A-only) = {len(deleted)}")
    for j in inserted:
        iid, t = sb[j]
        print(f"  B static#{j:5d}  inst_id=0x{iid:04x} (byte {iid*4:6d})  {t}")

    if inserted:
        jlo, jhi = min(inserted), max(inserted)
        print("\n## context around insertion (WITH-cpex static stream) ##")
        for j in range(max(0, jlo - 4), min(len(sb), jhi + 5)):
            iid, t = sb[j]
            mark = "   <== INSERTED" if j in inserted else ""
            print(f"  B#{j:5d} inst_id=0x{iid:04x} byte={iid*4:6d} {t:24s}{mark}")

        # Locate insertion vs landmarks (bytes)
        first_ins_byte = sb[jlo][0] * 4
        print(
            f"\n  first inserted byte offset = {first_ins_byte} "
            f"(id 0x{sb[jlo][0]:04x})"
        )
        for name, b in sorted(C.LANDMARKS_BYTE.items(), key=lambda x: x[1]):
            print(f"    landmark {name:32s} @byte {b:6d} (id 0x{b//4:04x})")

    # id-shift verification: after the insertion point, B ids should be A ids + 6
    print("\n## STEP 1b: id-shift verification on matched static pairs ##")
    shift_hist = {}
    for ia, ib in matched:
        d = sb[ib][0] - sa[ia][0]
        shift_hist[d] = shift_hist.get(d, 0) + 1
    for d in sorted(shift_hist):
        print(f"  id-shift {d:+d} : {shift_hist[d]} matched static pairs")

    # Save alignment for downstream scripts
    return A, B, sa, sb, matched, inserted


if __name__ == "__main__":
    main()
