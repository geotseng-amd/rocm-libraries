#!/usr/bin/env python3
"""optnll_map_build.py  (READ-ONLY analysis, writes only a local cache pickle)

Parse the two large inputs ONCE and cache compact arrays:
  1. objdump  -> ordered list of (obj_ordinal, byte_addr, mnemonic) + labels
  2. .mon     -> busiest wave records (inst_id, tt_type, ts) using EXACT
                 analyze_mon2.py parser logic.

Everything downstream (mapping derivation / verdict) reads the pickle so the
multi-hundred-MB files are never re-parsed.
"""
import re
import pickle
from collections import defaultdict

MON = "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/f8f8s_sipa_beta0_gsu1_alpha1_optnll.mon"
OBJ = "/data0/geotseng/comparison_output/f8f8s_pgr2_sia0/Cijk_Alik_Bljk_F8F8S_BH_UserArgs_MT256x256x256_M500j5HDzeGjVrslaaZ_587D-PeHMg5RXGAWPTfenu70=/obj_dump.log"
CACHE = "/home/geotseng/workspace/fork/rocm-libraries/shared/stinkytofu/traces/scripts/optnll_cache.pkl"

# ---- EXACT analyze_mon2.py parser regexes ----
INST_RE = re.compile(
    r"^(\d+) \((\d+),(\d+),(\d+),(\w+)\): INST instruction=(TT_INST_[A-Z0-9_]+)"
)
IDTS_RE = re.compile(r"inst_id=0x([0-9a-f]+), thread_id=\d+, TS=(\d+)")

# obj instruction line:  "\t<mnemonic> ...  // 0000000000005018: <encoding>"
OBJ_INSN_RE = re.compile(r"^\s+(\S+).*//\s+([0-9A-Fa-f]+):\s")
# obj label line:        "0000000000005018 <label_name>:"
OBJ_LABEL_RE = re.compile(r"^([0-9A-Fa-f]+)\s+<([^>]+)>:")


def parse_obj(path):
    insns = []  # list of (byte_addr, mnemonic) in program order == obj ordinal
    labels = {}  # name -> byte_addr
    with open(path) as f:
        for line in f:
            ml = OBJ_LABEL_RE.match(line)
            if ml:
                labels[ml.group(2)] = int(ml.group(1), 16)
                continue
            mi = OBJ_INSN_RE.match(line)
            if mi:
                insns.append((int(mi.group(2), 16), mi.group(1)))
    return insns, labels


def parse_mon(path):
    """Return busiest wave list of (se,sa,simd,slot,type,inst_id,ts)."""
    by_wave = defaultdict(list)
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
                    key = (se, sa, simd, slot)
                    by_wave[key].append((itype, int(m2.group(1), 16), int(m2.group(2))))
                pending = None
    mw = max(by_wave, key=lambda k: len(by_wave[k]))
    return mw, by_wave[mw]


def main():
    insns, labels = parse_obj(OBJ)
    print(
        f"obj: {len(insns)} instruction lines, last addr "
        f"0x{insns[-1][0]:x}={insns[-1][0]}"
    )

    mw, wave = parse_mon(MON)
    ids = [r[1] for r in wave]
    distinct = set(ids)
    print(
        f"mon busiest wave={mw}  n_inst={len(wave)}  "
        f"distinct={len(distinct)}  max_id=0x{max(distinct):x}"
    )

    with open(CACHE, "wb") as f:
        pickle.dump(
            {"insns": insns, "labels": labels, "main_wave": mw, "wave": wave}, f
        )
    print(f"cached -> {CACHE}")


if __name__ == "__main__":
    main()
