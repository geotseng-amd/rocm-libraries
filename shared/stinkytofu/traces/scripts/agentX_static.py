"""Parse obj_dump.log -> ordered list of (byte_addr, size, mnemonic) and labels.

obj_dump instruction line (real byte addr is in the trailing comment):
    \t<mnemonic> <operands> // <16hex_addr>: <encoding dwords>
Label line:
    <16hex_addr> <label>:

We derive per-instruction *size* from the number of encoding dwords in the
trailing comment (each dword = 4 bytes) as a cross-check against next-addr.
"""

import re
import sys
from collections import OrderedDict

# The trailing address marker "// <hexaddr>: <encoding dwords>" is the only
# "//" in the line (operand /* */ comments use a single slash), so anchor on it.
ADDR_RE = re.compile(r"//\s*([0-9a-fA-F]{6,16}):\s*([0-9A-Fa-f ]+)\s*$")
LABEL_RE = re.compile(r"^([0-9a-fA-F]{16}) <([^>]+)>:")

# user-facing pass-dump coordinate is obj_addr - PASS_DELTA (verified 288)
PASS_DELTA = 288


def parse_obj(path):
    insns = []  # (addr, size_bytes, mnemonic, full_operand)
    labels = OrderedDict()
    with open(path) as f:
        for line in f:
            ml = LABEL_RE.match(line)
            if ml:
                labels[int(ml.group(1), 16)] = ml.group(2)
                continue
            mi = ADDR_RE.search(line)
            if mi and (line.startswith("\t") or line.startswith("  ")):
                operand = line[: mi.start()].strip()
                if not operand:
                    continue
                addr = int(mi.group(1), 16)
                dwords = len(mi.group(2).split())
                mnem = operand.split()[0]
                insns.append((addr, dwords * 4, mnem, operand))
    insns.sort()
    return insns, labels


def region(insns, lo, hi):
    return [i for i in insns if lo <= i[0] < hi]


def main():
    path = sys.argv[1]
    insns, labels = parse_obj(path)
    total = insns[-1][0] + insns[-1][1]
    print(f"obj total bytes = {total}  (pass-coord total = {total - PASS_DELTA})")
    print(f"n_insns = {len(insns)}")
    # print label offsets in both coord systems for the GW blocks
    for addr, name in labels.items():
        if "GW_B1_GSU1" in name or name in (
            "label_GW_B0_MB",
            "label_GW_B0_FD0_VW4_GSU1_Then",
            "label_GW_End",
            "label_GW_B1_FD0_VW4_GSU1_Then",
            "label_GW_B1_FD0_VW4_GSU1_Else",
        ):
            print(f"  {name:40s} obj=0x{addr:x}({addr})  pass={addr-PASS_DELTA}")


if __name__ == "__main__":
    main()
