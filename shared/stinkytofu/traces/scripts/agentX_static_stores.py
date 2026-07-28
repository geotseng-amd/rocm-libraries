"""Extract memory ops (stores/loads) within the Case-C GW_B1_GSU1 block from
the static obj_dump, grouped by sub-region (entry / Then / Else), with byte
addresses in pass-coord. Also reports how many memory ops fall inside the
one-shot ladder window [235340, 259916) (pass) = first 24576 B of the block.
"""

import sys
from agentX_static import parse_obj, region, PASS_DELTA

BLOCK_LO_PASS = 235340
BLOCK_SZ = 177636
LADDER = 24576
STORE_MN = ("buffer_store", "global_store", "scratch_store", "flat_store")
LOAD_MN = ("buffer_load", "global_load", "flat_load")


def is_store(m):
    return any(m.startswith(p) for p in STORE_MN)


def is_load(m):
    return any(m.startswith(p) for p in LOAD_MN)


def main():
    path = sys.argv[1]
    insns, labels = parse_obj(path)
    lo = BLOCK_LO_PASS + PASS_DELTA  # obj coord
    hi = lo + BLOCK_SZ
    blk = region(insns, lo, hi)
    ladder_hi = lo + LADDER

    # sub-region label boundaries (obj)
    then_lo = next(a for a, n in labels.items() if n == "label_GW_B1_FD0_VW4_GSU1_Then")
    else_lo = next(a for a, n in labels.items() if n == "label_GW_B1_FD0_VW4_GSU1_Else")

    def tag(a):
        if a < then_lo:
            return "entry"
        if a < else_lo:
            return "Then"
        return "Else"

    stores = [(a, m) for a, sz, m, op in blk if is_store(m)]
    loads = [(a, m) for a, sz, m, op in blk if is_load(m)]
    print(
        f"Case-C block obj[0x{lo:x},0x{hi:x}) pass[{BLOCK_LO_PASS},{BLOCK_LO_PASS+BLOCK_SZ})"
    )
    print(
        f"  block size = {hi-lo} B ; ladder window obj[0x{lo:x},0x{ladder_hi:x}) = {LADDER} B"
    )
    print(f"  n_static_insns_in_block = {len(blk)}")
    print(f"  n_stores={len(stores)}  n_loads={len(loads)}")

    from collections import Counter

    print("  -- stores by sub-region --")
    c = Counter(tag(a) for a, m in stores)
    for k in ("entry", "Then", "Else"):
        print(f"     {k:6s}: {c.get(k,0)}")
    print("  -- stores by mnemonic --")
    for m, n in Counter(m for a, m in stores).most_common():
        print(f"     {m:20s} {n}")

    inwin = [(a, m) for a, m in stores if a < ladder_hi]
    print(
        f"  stores inside ladder window (<0x{ladder_hi:x}): {len(inwin)} / {len(stores)}"
    )
    print(f"  first store addr obj=0x{stores[0][0]:x} pass={stores[0][0]-PASS_DELTA}")
    print(f"  last  store addr obj=0x{stores[-1][0]:x} pass={stores[-1][0]-PASS_DELTA}")

    # Path store sequences (for aligning to trace 128 stores)
    for pth in ("entry", "Then", "Else"):
        seq = [(a - PASS_DELTA, m) for a, m in stores if tag(a) == pth]
        if seq:
            print(
                f"  path {pth}: {len(seq)} stores, pass_addr[{seq[0][0]}..{seq[-1][0]}]"
            )


if __name__ == "__main__":
    main()
