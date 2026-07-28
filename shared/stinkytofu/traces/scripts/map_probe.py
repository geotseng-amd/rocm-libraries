import re, sys
from collections import defaultdict

KOBJ = "/data0/geotseng/comparison_output/f8f8s_pgr2_sia0/Cijk_Alik_Bljk_F8F8S_BH_UserArgs_MT256x256x256_M500j5HDzeGjVrslaaZ_587D-PeHMg5RXGAWPTfenu70=/obj_dump.log"
INST_RE = re.compile(
    r"^(\d+) \((\d+),(\d+),(\d+),(\w+)\): INST instruction=(TT_INST_[A-Z0-9_]+)"
)
IDTS_RE = re.compile(r"inst_id=0x([0-9a-f]+), thread_id=\d+, TS=(\d+)")


def parse(p):
    recs = []
    pend = None
    for line in open(p):
        m = INST_RE.match(line)
        if m:
            pend = (m.group(2), m.group(3), m.group(4), m.group(5), m.group(6))
            continue
        if pend is not None:
            m2 = IDTS_RE.search(line)
            if m2:
                recs.append((pend, int(m2.group(1), 16), int(m2.group(2))))
            pend = None
    return recs


def busiest(recs):
    byw = defaultdict(list)
    for k, iid, ts in recs:
        byw[k].append((iid, ts))
    return max(byw.values(), key=len)


# obj_dump: instruction lines have a trailing "// <hexaddr>: <enc>"
addr_re = re.compile(r"//\s*([0-9A-F]+):")


def objaddrs(p):
    addrs = []
    for line in open(p):
        m = addr_re.search(line)
        if m:
            addrs.append(int(m.group(1), 16))
    return addrs


for f in sys.argv[1:]:
    w = busiest(parse(f))
    ids = sorted(set(i for i, _ in w))
    print(f"### {f}")
    print(f"  n={len(w)} distinct={len(ids)} min_id=0x{ids[0]:x} max_id=0x{ids[-1]:x}")
    print(f"  min_id_dec={ids[0]} max_id_dec={ids[-1]}")
    # test byte hypotheses
    for div, name in [(1, "id=byte"), (2, "id=byte/2"), (4, "id=byte/4")]:
        print(f"    if {name}: exec byte range [{ids[0]*div}, {ids[-1]*div}]")
oa = objaddrs(KOBJ)
print(
    f"### obj_dump: {len(oa)} instr lines; first addrs {oa[:3]}, last {oa[-1]:#x}={oa[-1]}"
)
# ordinal of key byte addrs
for lbl, b in [
    ("OptNLL", 0x5018),
    ("CpBoundary", 0x7F7C),
    ("GW_End", 0xB75C),
    ("GW_B0_MB", 0xCB0C),
    ("GW_B1_GSU1", 0x3986C),
]:
    # find ordinal (index) of first instr with addr>=b
    import bisect

    idx = bisect.bisect_left(oa, b)
    print(f"    {lbl}@{b}(0x{b:x}) -> obj ordinal {idx}")
