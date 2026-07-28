#!/usr/bin/env python3
"""Localize where CP-extend prefetch saved ~8032 fetch-stall cycles.

Compares two gfx1250 csim traces of the SAME f8f8s OptNLL launch:
  NO-cpex   vs   WITH-cpex (6 inserted instrs: getpc + 3 s_add + 2 s_prefetch_inst)

Parses each file ONCE, busiest wave only. Aligns the two static-instruction
streams (handling the +6 id shift by TT-type sequence alignment via difflib),
computes per-static-instruction first-touch fetch stall in both runs, buckets by
inst_id range, and localizes the reduction relative to the CP-extend cover window
bytes [32636, 40828).
"""
import re
import sys
import difflib
from collections import defaultdict

INST_RE = re.compile(
    r"^(\d+) \((\d+),(\d+),(\d+),(\w+)\): INST instruction=(TT_INST_[A-Z0-9_]+)"
)
IDTS_RE = re.compile(r"inst_id=0x([0-9a-f]+), thread_id=\d+, TS=(\d+)")

NO = "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/f8f8s_sipa_beta0_gsu1_alpha1_optnll.mon"
WI = "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/f8f8s_sipa_cpex_beta0_gsu1_alpha1_optnll.mon"

# CP-extend cover window (static bytes) and byte<->obj-ordinal anchors.
WIN_LO, WIN_HI = 32636, 40828
ANCHORS = [(3586, 20504), (5744, 32636), (8140, 46940)]  # (obj#, byte)


def parse(path):
    recs = []
    pending = None
    with open(path) as f:
        for line in f:
            m = INST_RE.match(line)
            if m:
                pending = (m.group(2), m.group(3), m.group(4), m.group(5), m.group(6))
                continue
            if pending is not None:
                m2 = IDTS_RE.search(line)
                if m2:
                    se, sa, simd, slot, itype = pending
                    recs.append(
                        (
                            se,
                            sa,
                            simd,
                            slot,
                            itype,
                            int(m2.group(1), 16),
                            int(m2.group(2)),
                        )
                    )
                pending = None
    return recs


def busiest_wave(recs):
    by_wave = defaultdict(list)
    for r in recs:
        by_wave[(r[0], r[1], r[2], r[3])].append(r)
    mw = max(by_wave, key=lambda k: len(by_wave[k]))
    return mw, by_wave[mw]


def analyze(w):
    """Segment busiest wave by WG (inst_id==0) and compute per-id first-touch
    fetch stall. Returns dicts and ordered static-id metadata."""
    segs = []
    cur = []
    for r in w:
        if r[5] == 0 and cur:
            segs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        segs.append(cur)

    seen = set()
    fetch_by_id = defaultdict(int)  # first-touch fetch stall per inst_id
    id_type = {}  # inst_id -> TT type at first sight
    total_fetch = 0
    span = w[-1][6] - w[0][6]

    for seg in segs:
        prev = None
        for r in seg:
            iid, ts, itype = r[5], r[6], r[4]
            if prev is not None:
                gap = ts - prev
                if gap > 1 and iid not in seen:
                    s = gap - 1
                    fetch_by_id[iid] += s
                    total_fetch += s
            if iid not in seen:
                id_type[iid] = itype
            seen.add(iid)
            prev = ts
    return segs, fetch_by_id, id_type, total_fetch, span


def align(id_type_no, id_type_wi):
    """Align static inst_id streams by TT-type sequence (ordered by inst_id).
    Returns (mapping no_id->wi_id, inserted_wi_ids list, opcodes)."""
    ids_no = sorted(id_type_no)
    ids_wi = sorted(id_type_wi)
    seq_no = [id_type_no[i] for i in ids_no]
    seq_wi = [id_type_wi[i] for i in ids_wi]
    sm = difflib.SequenceMatcher(a=seq_no, b=seq_wi, autojunk=False)
    mapping = {}
    inserted = []
    ops = sm.get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            for k in range(i2 - i1):
                mapping[ids_no[i1 + k]] = ids_wi[j1 + k]
        elif tag == "insert":
            for j in range(j1, j2):
                inserted.append(ids_wi[j])
        elif tag == "replace":
            # map the common prefix length; extras counted as inserted/dropped
            n = min(i2 - i1, j2 - j1)
            for k in range(n):
                mapping[ids_no[i1 + k]] = ids_wi[j1 + k]
            for j in range(j1 + n, j2):
                inserted.append(ids_wi[j])
    return ids_no, ids_wi, mapping, inserted, ops


def byte_to_id_range():
    """Linear obj#<->byte fit from the two anchors bracketing the window, then
    treat inst_id == obj-ordinal (kernel entry obj#0 == byte 0). Returns
    (id_lo, id_hi, bytes_per_obj) for window [WIN_LO, WIN_HI)."""
    (o1, b1), (o2, b2) = ANCHORS[1], ANCHORS[2]  # obj5744@32636, obj8140@46940
    bpo = (b2 - b1) / (o2 - o1)
    lo = o1 + (WIN_LO - b1) / bpo
    hi = o1 + (WIN_HI - b1) / bpo
    return int(round(lo)), int(round(hi)), bpo


def main():
    print("Parsing NO-cpex ...", file=sys.stderr)
    recs_no = parse(NO)
    print("Parsing WITH-cpex ...", file=sys.stderr)
    recs_wi = parse(WI)

    mw_no, w_no = busiest_wave(recs_no)
    mw_wi, w_wi = busiest_wave(recs_wi)
    segs_no, fetch_no, type_no, tot_no, span_no = analyze(w_no)
    segs_wi, fetch_wi, type_wi, tot_wi, span_wi = analyze(w_wi)

    print("=" * 78)
    print("BUSIEST WAVE SUMMARY")
    print(
        f"  NO-cpex   wave={mw_no} n={len(w_no)} segs={len(segs_no)} "
        f"span={span_no} fetch_stall={tot_no} distinct_id={len(type_no)} "
        f"max_id=0x{max(type_no):x}"
    )
    print(
        f"  WITH-cpex wave={mw_wi} n={len(w_wi)} segs={len(segs_wi)} "
        f"span={span_wi} fetch_stall={tot_wi} distinct_id={len(type_wi)} "
        f"max_id=0x{max(type_wi):x}"
    )
    print(
        f"  -> fetch-stall reduction = {tot_no - tot_wi}   "
        f"span reduction = {span_no - span_wi}"
    )

    # ---- 1. Alignment / inserted-instruction confirmation ----
    ids_no, ids_wi, mapping, inserted, ops = align(type_no, type_wi)
    print("\n" + "=" * 78)
    print("1. STREAM ALIGNMENT  (difflib on TT-type sequence, ordered by inst_id)")
    ins_types = [type_wi[i] for i in inserted]
    print(f"  inserted WITH-cpex inst_ids: " f"{['0x%x' % i for i in inserted]}")
    print(f"  inserted TT types          : {ins_types}")
    if inserted:
        p_no = None
        # locate insertion point in NO-id space: last equal-mapped id below first inserted
        first_ins = min(inserted)
        below = [n for n, wid in mapping.items() if wid < first_ins]
        p_no = max(below) if below else None
        print(
            f"  insertion sits just after NO-cpex id 0x{p_no:x} "
            f"(WITH-cpex id 0x{mapping[p_no]:x}); ids after shift by "
            f"+{len(inserted)}"
        )
    # sanity: verify shift is constant +6 after insertion
    shifts = defaultdict(int)
    for n, wid in mapping.items():
        shifts[wid - n] += 1
    print(f"  id-shift histogram (wi_id - no_id): " f"{dict(sorted(shifts.items()))}")

    # ---- 2. Bucket first-touch fetch stall by inst_id range (0x200) ----
    # Fold WITH-cpex stalls back into NO-cpex id space via the alignment mapping.
    wi_in_no_space = defaultdict(int)
    unmapped_wi = 0
    inv = {}
    for n, wid in mapping.items():
        inv[wid] = n
    for wid, s in fetch_wi.items():
        if wid in inv:
            wi_in_no_space[inv[wid]] += s
        elif wid in inserted:
            wi_in_no_space[("INS", wid)] += s  # inserted-instruction stall
        else:
            unmapped_wi += s

    BK = 0x200
    buckets = defaultdict(lambda: [0, 0])  # bucket -> [no, wi]
    for iid, s in fetch_no.items():
        buckets[iid // BK][0] += s
    ins_stall = 0
    for key, s in wi_in_no_space.items():
        if isinstance(key, tuple):
            ins_stall += s
            continue
        buckets[key // BK][1] += s

    print("\n" + "=" * 78)
    print("2. FIRST-TOUCH FETCH-STALL BY inst_id BUCKET (size 0x200), NO-cpex id space")
    print(f"  {'bucket id-range':<22}{'NO':>10}{'WITH':>10}{'delta(NO-WI)':>14}")
    tot_delta = 0
    for b in sorted(buckets):
        no_s, wi_s = buckets[b]
        d = no_s - wi_s
        tot_delta += d
        lo, hi = b * BK, (b + 1) * BK - 1
        star = "  <==" if abs(d) >= 300 else ""
        print(f"  0x{lo:04x}-0x{hi:04x}     {no_s:>10}{wi_s:>10}{d:>14}{star}")
    print(f"  inserted-instr fetch stall (WITH only) = {ins_stall}")
    print(f"  unmapped WITH stall (alignment noise)  = {unmapped_wi}")
    print(
        f"  SUM delta over buckets = {tot_delta}  "
        f"(+ins {ins_stall}) net = {tot_delta - ins_stall}"
    )

    # ---- 3. Map byte window [32636,40828) -> inst_id range; report window stalls ----
    id_lo, id_hi, bpo = byte_to_id_range()
    print("\n" + "=" * 78)
    print("3. CP-EXTEND COVER WINDOW  bytes [32636, 40828)")
    print(
        f"  linear obj#<->byte fit: {bpo:.3f} bytes/obj (anchors "
        f"obj5744@32636, obj8140@46940)"
    )
    print(
        f"  assuming inst_id == obj-ordinal -> window inst_id "
        f"[0x{id_lo:x}, 0x{id_hi:x}]  ({id_lo}..{id_hi})"
    )
    win_no = sum(s for i, s in fetch_no.items() if id_lo <= i <= id_hi)
    win_wi = sum(
        s
        for i, s in wi_in_no_space.items()
        if not isinstance(i, tuple) and id_lo <= i <= id_hi
    )
    print(
        f"  window fetch stall  NO={win_no}  WITH={win_wi}  " f"delta={win_no - win_wi}"
    )

    # ICPREF anchor cross-check: where are the prefetch instrs in id space?
    icp_no = sorted(i for i, t in type_no.items() if t == "TT_INST_ICPREF")
    icp_wi = sorted(i for i, t in type_wi.items() if t == "TT_INST_ICPREF")
    print(f"  ICPREF inst_ids NO  : {['0x%x' % i for i in icp_no]}")
    print(f"  ICPREF inst_ids WITH: {['0x%x' % i for i in icp_wi]}")

    # ---- 4. Localization conclusion: inside window vs elsewhere ----
    inside = 0
    outside = 0
    for iid, s in fetch_no.items():
        wi_s = wi_in_no_space.get(iid, 0)
        d = s - wi_s
        if id_lo <= iid <= id_hi:
            inside += d
        else:
            outside += d
    # ids that exist only in WITH-space (rare) ignored; inserted handled above
    print("\n" + "=" * 78)
    print("4. LOCALIZATION  (delta = NO - WITH first-touch fetch stall)")
    print(f"  delta INSIDE  window [0x{id_lo:x},0x{id_hi:x}] = {inside}")
    print(f"  delta OUTSIDE window                    = {outside}")
    print(f"  inserted-instr added stall (WITH)       = -{ins_stall}")
    net = inside + outside - ins_stall
    print(f"  NET reduction (inside+outside-inserted) = {net}")
    if inside + outside:
        print(f"  share inside window = " f"{100.0 * inside / (inside + outside):.1f}%")


if __name__ == "__main__":
    main()
