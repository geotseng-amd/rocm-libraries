#!/usr/bin/env python3
"""Shared parsing + first-touch fetch-stall model for the OptNLL CP-extend
localization (beta0/gsu1/alpha1 f8f8s).

Model (matches analyze_mon2.py):
  - parse (SE,SA,SIMD,SLOT,itype,inst_id,TS) records
  - busiest wave = wave slot with most INST records
  - segment into workgroups whenever inst_id resets to 0
  - first-touch fetch stall for an inst_id = (gap-1) cycles on the FIRST time
    that inst_id is issued in the whole wave (an I-cache miss can only happen on
    the first fetch of a static instruction). Re-executed ids -> exec/mem stall.

Static-address model: inst_id is a dword ordinal, byte_offset = inst_id * 4.
"""
import re
from collections import defaultdict

INST_RE = re.compile(
    r"^(\d+) \((\d+),(\d+),(\d+),(\w+)\): INST instruction=(TT_INST_[A-Z0-9_]+)"
)
IDTS_RE = re.compile(r"inst_id=0x([0-9a-f]+), thread_id=\d+, TS=(\d+)")

# CP-extend target window, in BYTES, converted to dword inst_id ordinals.
CPEX_BYTE_LO = 32636
CPEX_BYTE_HI = 40828  # exclusive
CPEX_ID_LO = CPEX_BYTE_LO // 4  # 8159
CPEX_ID_HI = CPEX_BYTE_HI // 4  # 10207 (exclusive)

# Known static landmarks (bytes -> id)
LANDMARKS_BYTE = {
    "label_GW_B0_OptNLL_MB": 20504,
    "OptNLL_body_start": 20504,
    "label_SW_PrefetchAbs_CpBoundary": 32636,
    "OptNLL_body_end": 46952,
    "cpex_cover_end": 40828,
}


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


def busiest_wave(path):
    recs = parse(path)
    by_wave = defaultdict(list)
    for r in recs:
        by_wave[(r[0], r[1], r[2], r[3])].append(r)
    mw = max(by_wave, key=lambda k: len(by_wave[k]))
    return mw, by_wave[mw]


def segment(w):
    """Split busiest-wave record list into per-workgroup segments (inst_id==0)."""
    segs, cur = [], []
    for r in w:
        if r[5] == 0 and cur:
            segs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        segs.append(cur)
    return segs


def first_touch_stall(w):
    """Return per-inst_id first-touch fetch stall + bookkeeping for one wave.

    Returns dict with:
      fetch_by_id : {inst_id: first-touch stall cycles}
      type_by_id  : {inst_id: TT type at first touch}
      order_ids   : inst_ids in first-touch order (== program order of the run)
      fetch_total, exec_total, boundary_total, span, n_inst, n_segs, distinct
    """
    segs = segment(w)
    seen = set()
    fetch_by_id = defaultdict(int)
    type_by_id = {}
    order_ids = []
    fetch_total = exec_total = boundary_total = 0
    span = w[-1][6] - w[0][6]

    for si, seg in enumerate(segs):
        prev = None
        for r in seg:
            iid, ts, itype = r[5], r[6], r[4]
            if iid not in type_by_id:
                type_by_id[iid] = itype
                order_ids.append(iid)
            if prev is not None:
                gap = ts - prev
                if gap > 1:
                    s = gap - 1
                    if iid in seen:
                        exec_total += s
                    else:
                        fetch_total += s
                        fetch_by_id[iid] += s
            else:
                if si > 0:
                    b = ts - segs[si - 1][-1][6]
                    if b > 1:
                        boundary_total += b - 1
            seen.add(iid)
            prev = ts

    return {
        "fetch_by_id": dict(fetch_by_id),
        "type_by_id": type_by_id,
        "order_ids": order_ids,
        "fetch_total": fetch_total,
        "exec_total": exec_total,
        "boundary_total": boundary_total,
        "span": span,
        "n_inst": len(w),
        "n_segs": len(segs),
        "distinct": len(seen),
        "max_id": max(seen),
    }


def load(path):
    mw, w = busiest_wave(path)
    d = first_touch_stall(w)
    d["path"] = path
    d["wave"] = mw
    d["recs"] = w
    return d
