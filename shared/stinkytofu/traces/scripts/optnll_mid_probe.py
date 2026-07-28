#!/usr/bin/env python3
"""optnll_mid_probe.py  (READ-ONLY) inspect static store layout + dyn store bytes."""
import pickle, sys

sys.path.insert(
    0, "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/scripts"
)
from optnll_mid_map import mclass, ttclass, OPT, HEAD_HI, CPEX_HI, GWEND

CACHE = "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/scripts/optnll_cache.pkl"

d = pickle.load(open(CACHE, "rb"))
insns = d["insns"]
wave = d["wave"]
byte_of = [b for b, _ in insns]
o_opt = next(i for i, b in enumerate(byte_of) if b >= OPT)
o_end = next(i for i, b in enumerate(byte_of) if b >= GWEND)

# static stores in body
sstores = [
    (i, byte_of[i], insns[i][1])
    for i in range(o_opt, o_end)
    if mclass(insns[i][1]) == "BUF_WR"
]
print(f"static body stores = {len(sstores)}  first={sstores[0]} last={sstores[-1]}")
head_s = [s for s in sstores if s[1] < HEAD_HI]
print(
    f"head static stores={len(head_s)} (last head store byte={head_s[-1][1]})  "
    f"first tail store byte={[s for s in sstores if s[1]>=HEAD_HI][0][1]}"
)

# dyn epilogue stores
last = 0
for i, r in enumerate(wave):
    if ttclass(r[0]) in ("WMMA", "LDS"):
        last = i
epi = wave[last + 1 :]
dstores = [r for r in epi if ttclass(r[0]) == "BUF_WR"]
print(
    f"dyn epilogue stores = {len(dstores)}  first id 0x{dstores[0][1]:x} "
    f"TS={dstores[0][2]}  last TS={dstores[-1][2]}"
)
# store TS spacing to see head/tail timing
print(
    "store TS samples (every 10th):",
    [dstores[i][2] for i in range(0, len(dstores), 10)],
)
