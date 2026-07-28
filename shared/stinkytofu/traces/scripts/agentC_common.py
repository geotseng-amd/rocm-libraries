"""Shared parsing for agentC I-cache modeling scripts.

Line format (2 physical lines per instruction record):
  "TS (SE,SA,SIMD,SLOT): INST instruction=TT_INST_<TYPE>..."
  "...inst_id=0x<hex>, thread_id=N, TS=<int>..."

A record = (se, sa, simd, slot, itype, inst_id:int, ts:int).
"""

import re
from collections import defaultdict

INST_RE = re.compile(
    r"^(\d+) \((\d+),(\d+),(\d+),(\w+)\): INST instruction=(TT_INST_[A-Z0-9_]+)"
)
IDTS_RE = re.compile(r"inst_id=0x([0-9a-f]+), thread_id=\d+, TS=(\d+)")


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
