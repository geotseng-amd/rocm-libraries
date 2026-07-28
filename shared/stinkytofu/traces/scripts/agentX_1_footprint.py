"""Deliverable 1: from the trace (busiest wave):
- total executed instructions
- executed footprint estimate in bytes (exec-stream = inst_id dword index)
- confirm every inst executes exactly once (compulsory / first-touch stream)
"""

import sys
from collections import Counter
from agentC_common import parse, busiest_wave


def analyze(path):
    recs = parse(path)
    mw, w = busiest_wave(recs)
    ids = [r[5] for r in w]
    cnt = Counter(ids)
    n = len(w)
    distinct = len(cnt)
    lo, hi = min(ids), max(ids)
    id_span = hi - lo + 1  # exec-stream dwords touched
    footprint = id_span * 4  # bytes (dword exec index -> *4)
    dup = {i: c for i, c in cnt.items() if c > 1}
    ts = [r[6] for r in w]
    print(f"==== {path}  wave={mw}")
    print(f"  total executed instructions   = {n}")
    print(f"  distinct inst_id              = {distinct}")
    print(
        f"  inst_id range                 = 0x{lo:x}..0x{hi:x} "
        f"({lo}..{hi}), dword span={id_span}"
    )
    print(
        f"  executed footprint (span*4)   = {footprint} B "
        f"({footprint/1024:.1f} KiB)"
    )
    print(f"  avg bytes/executed inst       = {footprint/n:.2f}")
    print(
        f"  EVERY inst exactly once?      = "
        f"{'YES (compulsory stream, no loops)' if not dup else 'NO'} "
        f"(distinct={distinct} == n={n}: {distinct==n})"
    )
    print(f"  TS span                       = {max(ts)-min(ts)}")
    if dup:
        print(f"  repeated inst_ids: {len(dup)} (e.g. " f"{list(dup.items())[:5]})")
    print()


if __name__ == "__main__":
    for f in sys.argv[1:] or [
        "f8f8s_sipa_beta1_gsu1.mon",
        "f8f8s_sipa_cpex_beta1_gsu1.mon",
    ]:
        analyze(f)
