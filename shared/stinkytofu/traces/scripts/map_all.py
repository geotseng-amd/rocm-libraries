import re, sys
from collections import defaultdict

INST_RE = re.compile(
    r"^(\d+) \((\d+),(\d+),(\d+),(\w+)\): INST instruction=(TT_INST_[A-Z0-9_]+)"
)
IDTS_RE = re.compile(r"inst_id=0x([0-9a-f]+), thread_id=\d+, TS=(\d+)")


def analyze(p):
    recs = []
    pend = None
    for line in open(p):
        m = INST_RE.match(line)
        if m:
            pend = (m.group(2), m.group(3), m.group(4), m.group(5))
            continue
        if pend is not None:
            m2 = IDTS_RE.search(line)
            if m2:
                recs.append((pend, int(m2.group(1), 16), int(m2.group(2))))
            pend = None
    byw = defaultdict(list)
    for k, iid, ts in recs:
        byw[k].append((iid, ts))
    w = max(byw.values(), key=len)
    seen = set()
    prev = None
    fetch = 0
    for iid, ts in w:
        if prev is not None:
            g = ts - prev
            if g > 1 and iid not in seen:
                fetch += g - 1
        seen.add(iid)
        prev = ts
    span = w[-1][1] - w[0][1]
    return len(w), span, fetch


files = sys.argv[1:]
res = {}
for f in files:
    n, span, fetch = analyze(f)
    res[f] = (n, span, fetch)
    print(
        f"{f:52s} n={n:6d} span={span:7d} fetch_stall={fetch:7d} ({100*fetch/span:.1f}%)"
    )
print("\n=== PAIRS (no-cpex -> with-cpex) ===")
pairs = [
    ("f8f8s beta0_gsu1", "f8f8s_sipa_beta0_gsu1.mon", "f8f8s_sipa_cpex_beta0_gsu1.mon"),
    (
        "f8f8s beta0_gsu1_alpha1_OPTNLL",
        "f8f8s_sipa_beta0_gsu1_alpha1_optnll.mon",
        "f8f8s_sipa_cpex_beta0_gsu1_alpha1_optnll.mon",
    ),
    ("f8f8s beta1_gsu1", "f8f8s_sipa_beta1_gsu1.mon", "f8f8s_sipa_cpex_beta1_gsu1.mon"),
    ("bbs beta1_gsu10", "bbs_beta1_gsu10.mon", "bbs_sipa_beta1_gsu10.mon"),
]
for name, a, b in pairs:
    if a in res and b in res:
        na, sa, fa = res[a]
        nb, sb, fb = res[b]
        print(
            f"{name:34s}  span {sa:7d} -> {sb:7d}  (Δ{sb-sa:+6d}, {100*(sb-sa)/sa:+5.1f}%)   fetch {fa:7d} -> {fb:7d} (Δ{fb-fa:+6d})"
        )
