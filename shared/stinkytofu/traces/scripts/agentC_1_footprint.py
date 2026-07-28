"""agentC part 1+2: repeats (loops) and executed code footprint.

Q1: Is EVERY inst executed exactly once (distinct_inst_id == n_inst)?
    Any repeats => loops. List repeated inst_ids and their counts.
Q2: Estimate executed code footprint in bytes (assume B bytes/inst),
    compare to 64 KiB I-cache and 32640 B CP window.
"""

import sys
from collections import Counter
from agentC_common import parse, busiest_wave

ICACHE = 64 * 1024  # 65536
CP_WINDOW = 32640  # CP hardware preload bytes [0, 32640)


def analyze(path):
    recs = parse(path)
    mw, w = busiest_wave(recs)
    n_inst = len(w)
    ids = [r[5] for r in w]
    cnt = Counter(ids)
    distinct = len(cnt)
    repeats = {i: c for i, c in cnt.items() if c > 1}
    min_id, max_id = min(ids), max(ids)
    id_span = max_id - min_id + 1  # includes not-taken/other-path slots

    print(f"==== {path}")
    print(f"  busiest_wave={mw}  n_inst={n_inst}")
    print(
        f"  distinct_inst_id={distinct}  min_id=0x{min_id:x}  "
        f"max_id=0x{max_id:x}({max_id})  id_span={id_span}"
    )
    print(
        f"  EVERY-inst-once? {'YES' if distinct == n_inst else 'NO'} "
        f"(distinct==n_inst: {distinct}=={n_inst})"
    )
    if repeats:
        print(f"  REPEATED inst_ids (loops): {len(repeats)}")
        for i, c in sorted(repeats.items(), key=lambda x: -x[1])[:20]:
            print(f"      0x{i:04x}  x{c}")
    else:
        print("  REPEATED inst_ids: NONE (no loops -> straight-line)")

    print("  -- footprint (B = bytes/inst) --")
    for B in (4, 6, 8):
        # executed distinct footprint (count-based) and program-layout span
        exec_bytes = distinct * B
        span_bytes = id_span * B
        print(
            f"    B={B}: executed={exec_bytes} B ({exec_bytes/1024:.1f} KiB), "
            f"layout_span={span_bytes} B ({span_bytes/1024:.1f} KiB)  | "
            f">64KiB? exec:{exec_bytes>ICACHE} span:{span_bytes>ICACHE}  | "
            f">CP(32640)? span:{span_bytes>CP_WINDOW}"
        )
    print()
    return dict(
        n_inst=n_inst,
        distinct=distinct,
        min_id=min_id,
        max_id=max_id,
        id_span=id_span,
        repeats=repeats,
    )


if __name__ == "__main__":
    files = sys.argv[1:] or [
        "f8f8s_sipa_beta1_gsu1.mon",
        "f8f8s_sipa_cpex_beta1_gsu1.mon",
    ]
    for f in files:
        analyze(f)
