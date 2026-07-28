import re
from collections import defaultdict

INST_RE = re.compile(
    r"^(\d+) \((\d+),(\d+),(\d+),(\w+)\): INST instruction=(TT_INST_[A-Z0-9_]+)"
)
IDTS_RE = re.compile(r"inst_id=0x([0-9a-f]+), thread_id=\d+, TS=(\d+)")


def load(p):
    recs = []
    pend = None
    for line in open(p):
        m = INST_RE.match(line)
        if m:
            pend = (m.group(2), m.group(3), m.group(4), m.group(5))
            continue  # 4-tuple wave key
        if pend is not None:
            m2 = IDTS_RE.search(line)
            if m2:
                recs.append((pend, int(m2.group(1), 16), int(m2.group(2))))
            pend = None
    byw = defaultdict(list)
    for k, iid, ts in recs:
        byw[k].append((iid, ts))
    return max(byw.values(), key=len)


def phases(p, label):
    w = load(p)
    tstart = w[0][1]
    tend = w[-1][1]
    seen = set()
    prev = None
    bins = defaultdict(int)
    for iid, ts in w:
        if prev is not None:
            g = ts - prev
            if g > 1 and iid not in seen:
                bins[(ts // 10000) * 10000] += g - 1
        seen.add(iid)
        prev = ts
    print(f"### {label}: TS[{tstart}..{tend}] span={tend-tstart} n={len(w)}")
    for bb in sorted(bins):
        print(
            f"   clk {bb:>7}-{bb+10000:<7} stall={bins[bb]:>6} {'#'*int(bins[bb]/300)}"
        )
    return bins


a = phases("f8f8s_sipa_beta0_gsu1_alpha1_optnll.mon", "NO cpex")
b = phases("f8f8s_sipa_cpex_beta0_gsu1_alpha1_optnll.mon", "WITH cpex")
print("\n### per-window delta (NO - WITH) = cycles saved by cpex")
for bb in sorted(set(a) | set(b)):
    print(f"   clk {bb:>7}-{bb+10000:<7} saved={a.get(bb,0)-b.get(bb,0):>6}")
