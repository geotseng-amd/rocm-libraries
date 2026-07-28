#!/usr/bin/env python3
"""
Compare instruction sizes: obj.log (disassembly) vs cost pipeline output.
Usage:
  python3 compare_instruction_sizes.py [--obj-log PATH] [--cost-file PATH] [--output-prefix PREFIX] [--accumulate-debug PATH]
  python3 compare_instruction_sizes.py --object-file <path.to.o> --cost-file <cost.txt> [--output-dir DIR]
If --object-file is given, llvm-objdump -d is run to produce obj.log, then comparison runs.
If ``accumulate_instruction_size_pass_debug.txt`` (or ``--accumulate-debug``) is present and contains
``[AccumulateInstructionSizePass] total size = N bytes``, that **N** is the reported cost total
(canonical pass total, not the sum of per-instruction lines).

Lightweight ELF check (no disassembly)::

  python3 compare_instruction_sizes.py --verify-elf-text -o kernel.o \\
    --accumulate-debug path/to/accumulate_instruction_size_pass_debug.txt --kernel-name MyKernel

Defaults: obj.log, kernel_name_aggregated_instruction_cost.txt
"""
import argparse
import os
import re
import struct
import subprocess
import sys
from collections import defaultdict

LLVM_OBJDUMP_DEFAULT = "/opt/rocm/lib/llvm/bin/llvm-objdump"


def read_elf64_text_section_size_bytes(obj_path):
    """Read `.text` section `sh_size` from an ELF64 LE relocatable/object (no external tools).

    Returns int byte size, or None if not ELF64 / `.text` missing / parse error.
    """
    try:
        with open(obj_path, "rb") as f:
            hdr = f.read(64)
            if len(hdr) < 64 or hdr[0:4] != b"\x7fELF" or hdr[4] != 2:
                return None
            e_shoff = struct.unpack_from("<Q", hdr, 40)[0]
            e_shentsize = struct.unpack_from("<H", hdr, 58)[0]
            e_shnum = struct.unpack_from("<H", hdr, 60)[0]
            e_shstrndx = struct.unpack_from("<H", hdr, 62)[0]
            if e_shnum == 0 or e_shentsize < 64:
                return None

            def read_shdr(idx):
                f.seek(e_shoff + idx * e_shentsize)
                d = f.read(64)
                if len(d) < 64:
                    return None
                (
                    sh_name,
                    _sh_type,
                    _sh_flags,
                    _sh_addr,
                    sh_offset,
                    sh_size,
                    _sh_link,
                    _sh_info,
                    _sh_align,
                    _sh_entsize,
                ) = struct.unpack("<IIQQQQIIQQ", d)
                return sh_name, sh_offset, sh_size

            shstr_ent = read_shdr(e_shstrndx)
            if not shstr_ent:
                return None
            _, shstr_off, shstr_sz = shstr_ent
            f.seek(shstr_off)
            strtab = f.read(shstr_sz)

            def sect_name(name_idx):
                if name_idx >= len(strtab):
                    return b""
                end = strtab.find(b"\x00", name_idx)
                if end < 0:
                    return strtab[name_idx:]
                return strtab[name_idx:end]

            for i in range(e_shnum):
                ent = read_shdr(i)
                if not ent:
                    continue
                sh_name, _off, sh_size = ent
                if sect_name(sh_name) == b".text":
                    return int(sh_size)
    except (OSError, struct.error, ValueError):
        return None
    return None


def sum_size_lines_from_accumulate_debug(accumulate_debug_path):
    """Sum per-instruction `size=` bytes in accumulate_instruction_size_pass_debug.txt (excludes LABEL placeholder)."""
    if not accumulate_debug_path or not os.path.isfile(accumulate_debug_path):
        return None
    size_re = re.compile(r"size=(\d+)\s*bytes|size=(\d+)\+(\d+)\(literal\)=(\d+)\s*bytes")
    total = 0
    try:
        with open(accumulate_debug_path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                if "size=" not in line or "cost=" not in line:
                    continue
                if "opcode=758" in line:
                    continue
                m = size_re.search(line)
                if not m:
                    continue
                if m.lastindex == 4:
                    total += int(m.group(4))
                else:
                    total += int(m.group(1))
    except (OSError, IOError, ValueError):
        return None
    return total


def verify_accumulate_pass_vs_elf_text(object_path, accumulate_debug_path, kernel_name="kernel"):
    """Compare `[AccumulateInstructionSizePass] total size` to `.text` sh_size in the `.o`.

    Returns a dict: pass_total, elf_text_size, sum_size_lines, diff_obj_minus_pass, ok, messages.
    """
    pass_total = parse_accumulate_instruction_size_pass_total(accumulate_debug_path)
    elf_sz = read_elf64_text_section_size_bytes(object_path) if object_path else None
    sum_lines = sum_size_lines_from_accumulate_debug(accumulate_debug_path)

    out = {
        "kernel_name": kernel_name,
        "object_path": object_path,
        "pass_total": pass_total,
        "elf_text_size": elf_sz,
        "sum_size_lines": sum_lines,
        "diff_elf_minus_pass": None,
        "ok": False,
    }
    if pass_total is None:
        out["error"] = "missing or unreadable pass total in accumulate debug file"
        return out
    if elf_sz is None:
        out["error"] = "could not read .text size from object (not ELF64 or no .text)"
        return out
    out["diff_elf_minus_pass"] = elf_sz - pass_total
    out["ok"] = elf_sz == pass_total
    return out


def format_cost_vs_elf_report(v):
    """Markdown-style lines matching the instruction-size comparison report (elf vs pass)."""
    lines = []
    kn = v.get("kernel_name", "kernel")
    if v.get("error"):
        lines.append(f"# Cost vs ELF `.text` ({kn}): **ERROR** — {v['error']}")
        return "\n".join(lines)
    pt = v["pass_total"]
    es = v["elf_text_size"]
    sl = v.get("sum_size_lines")
    diff = v["diff_elf_minus_pass"]
    lines.append(f"# Cost vs ELF `.text`: {kn}")
    lines.append("")
    op = v.get("object_path") or "kernel.o"
    lines.append(
        f"- Total size (ELF `.text`): **{es} bytes** (lightweight ELF64 parser on `{os.path.basename(op)}`)"
    )
    note = (
        " (from `[AccumulateInstructionSizePass] total size` in `accumulate_instruction_size_pass_debug.txt`"
    )
    if sl is not None and sl != pt:
        note += f"; sum of per-instruction `size=` lines: **{sl} bytes**"
    note += ")"
    lines.append(f"- Total size (cost file): **{pt} bytes**{note}")
    lines.append(f"- **Difference (ELF `.text` − pass): {diff:+d} bytes**")
    if v.get("ok"):
        lines.append("")
        lines.append("**OK:** AccumulateInstructionSizePass total matches `.text` section size.")
    else:
        lines.append("")
        lines.append("**MISMATCH:** Investigate padding, directives not modeled in the pass, or stale debug file.")
    return "\n".join(lines)


def dump_object_to_obj_log(o_path, obj_log_path, llvm_objdump_path):
    """Run llvm-objdump -d on o_path, write to obj_log_path. Return True on success."""
    try:
        with open(obj_log_path, "w") as out:
            subprocess.run(
                [llvm_objdump_path, "-d", o_path],
                stdout=out,
                stderr=subprocess.PIPE,
                check=True,
                timeout=120,
            )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(f"llvm-objdump failed for {o_path}: {e}\n")
        return False


def get_full_kernel_name_from_obj_log(obj_log_path):
    """Read obj.log; llvm-objdump prints a line 'PATH_TO_O:	file format ...' (may be after a blank line).
    Return basename of that path with .o stripped (full kernel name), or None."""
    try:
        with open(obj_log_path, "r") as f:
            for line in f:
                line = line.rstrip("\n")
                if ".o:" in line and "file format" in line:
                    break
            else:
                return None
    except (OSError, IOError):
        return None
    # line is "path/to/KernelName_MT64x64x256_xxx=.o:\tfile format elf64-amdgpu" (path may end with .o:)
    if "\t" in line:
        path_part = line.split("\t", 1)[0].strip()
    elif ":" in line:
        path_part = line.split(":", 1)[0].strip()
    else:
        return None
    path_part = path_part.rstrip(":")  # path can end with .o:
    if not path_part.endswith(".o"):
        return None
    base = os.path.basename(path_part)
    if base.endswith(".o"):
        return base[:-2]
    return None


def get_obj_instructions(obj_log_path):
    """Parse obj.log (llvm-objdump -d). Returns list of (mnemonic, size_bytes, line_num, raw_line) in file order.
    Only instruction lines (with '//' and encoding) are included. Label lines and s_nop are skipped."""
    lines = []
    with open(obj_log_path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.rstrip("\n")
            # Skip all label lines in obj dump (e.g. 0000000000000888 <label_NoEarlyStop_wgExceed>:)
            if "<" in line and ">:" in line:
                continue
            # Instruction line: has "// " then "ADDRESS: ENCODING_WORDS"
            if "//" in line:
                parts = line.split("//", 1)
                right = parts[1].strip()
                if ":" in right:
                    right = right.split(":", 1)[1].strip()
                enc = right.split()
                num_words = sum(1 for w in enc if re.match(r"^[0-9A-Fa-f]{8}$", w))
                size = num_words * 4
                inst_part = parts[0].strip()
                if "\t" in inst_part:
                    inst_part = inst_part.split("\t", 1)[1]
                tokens = inst_part.split()
                mnemonic = tokens[0] if tokens else ""
                # Skip s_nop for sequential comparison (avoids position mismatch from inserted NOPs)
                if mnemonic == "s_nop":
                    continue
                lines.append((mnemonic, size, line_num, line))
    return lines


def get_obj_total_from_last_line(obj_log_path):
    """Compute total obj size from the instruction with maximum address: max_offset + that instruction's size.
    llvm-objdump format: '// ADDRESS: ENCODING_WORDS'. Total = address + (num_encoding_words * 4).
    We use the line with the *maximum* address so we get the true end of .text (handles multiple sections).
    Returns None if no instruction line can be parsed."""
    max_offset = -1
    size_at_max = 0
    with open(obj_log_path, "r") as f:
        for line in f:
            if "//" not in line:
                continue
            right = line.split("//", 1)[1].strip()
            if ":" not in right:
                continue
            addr_str, enc_str = right.split(":", 1)
            addr_str = addr_str.strip()
            try:
                offset = int(addr_str, 16)
            except ValueError:
                continue
            enc = enc_str.strip().split()
            num_words = sum(1 for w in enc if re.match(r"^[0-9A-Fa-f]{8}$", w))
            if offset > max_offset:
                max_offset = offset
                size_at_max = num_words * 4
    if max_offset < 0:
        return None
    return max_offset + size_at_max


def parse_accumulate_instruction_size_pass_total(accumulate_debug_path):
    """Read accumulate_instruction_size_pass_debug.txt; return N from
    '[AccumulateInstructionSizePass] total size = N bytes', or None if missing/unreadable."""
    if not accumulate_debug_path or not os.path.isfile(accumulate_debug_path):
        return None
    pat = re.compile(
        r"\[AccumulateInstructionSizePass\]\s*total size\s*=\s*(\d+)\s*bytes",
        re.IGNORECASE,
    )
    try:
        with open(accumulate_debug_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = pat.search(line)
                if m:
                    return int(m.group(1))
    except (OSError, IOError, ValueError):
        return None
    return None


def get_cost_sizes(cost_file_path):
    """Parse cost file. Yields (mnemonic, size_bytes, line_number, raw_line) in file order.
    Lines with cost= and size= are included; LABEL placeholder (opcode=758) and s_nop are skipped."""
    size_re = re.compile(r"size=(\d+)\s*bytes|size=(\d+)\+(\d+)\(literal\)=(\d+)\s*bytes")
    mnemonic_re = re.compile(r'"st\.([^"]+)"')
    with open(cost_file_path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if "size=" not in line or "cost=" not in line:
                continue
            # Skip LABEL placeholder: [cost=0 cycles, size=4 bytes, opcode=758 (isa=758)]
            if "opcode=758" in line:
                continue
            m = size_re.search(line)
            if not m:
                continue
            if m.lastindex == 4:
                size = int(m.group(4))
            else:
                size = int(m.group(1))
            # Primary source (aggregated_instruction_cost.txt): `"st.<mnemonic>"`
            # Fallback (accumulate_instruction_size_pass_debug.txt): instruction dump after the metadata bracket.
            mn = mnemonic_re.search(line)
            if mn:
                mnemonic = mn.group(1)
            else:
                mnemonic = ""
                # Example debug line:
                #   [cost=4 cycles, size=4+4(literal)=8 bytes, total=..., opcode=...] v_add_f32 v0, ...
                if "]" in line:
                    after = line.rsplit("]", 1)[-1].strip()
                    if after:
                        mnemonic = after.split()[0]
            # Skip s_nop for sequential comparison (avoids position mismatch from inserted NOPs)
            if mnemonic == "s_nop":
                continue
            yield (mnemonic, size, line_num, line)


# Normalize mnemonic for pairing: obj dump may add encoding tags (e.g. v_and_b32_e32) while cost has base (v_and_b32).
# Strip trailing _e32, _e64, _e16, etc. so we pair by base mnemonic + size.
_MNEMONIC_SUFFIX_RE = re.compile(r"_e\d+$")


def normalize_mnemonic(mnemonic):
    """Return base mnemonic for pairing: strip encoding suffix like _e32, _e64 so v_and_b32_e32 -> v_and_b32."""
    if not mnemonic:
        return mnemonic
    return _MNEMONIC_SUFFIX_RE.sub("", mnemonic)


# Generic fallback root cause (no specific rule matched) = "not covered"
ROOT_CAUSE_NOT_COVERED = "VOP2/VOP1 with non-VGPR source or literal -> VOP3 (8 bytes); or long literal +4."


def infer_root_cause(obj_mnemonic, obj_size, cost_size):
    """Infer main root cause for size difference (obj - cost). Returns (reason_string, covered: bool)."""
    diff = obj_size - cost_size
    if not obj_mnemonic:
        return "Label slot in obj (0 bytes) vs cost placeholder", True
    if diff <= 0:
        return "N/A (cost >= obj)", False
    # s_nop: SOPP is 4 bytes in both obj and cost; diff usually from position mismatch (inserted NOP after label).
    if "s_nop" in obj_mnemonic:
        return "s_nop is 4 bytes (SOPP) in both; diff likely from position mismatch (e.g. inserted NOP after label shifted pairing).", True
    # obj > cost: typically obj=8, cost=4
    if "mov_b32" in obj_mnemonic or "s_mov_b32" in obj_mnemonic:
        return "Long literal: 32-bit immediate (e.g. 0x80000000) not in short literal set; cost may show symbol (e.g. BufferOOB) so +4 not added.", True
    if "s_add_co_i32" in obj_mnemonic or "s_add_i32" in obj_mnemonic:
        return "Label/offset: 32-bit immediate in second word; cost may not count label as literal.", True
    if "v_mul_i32_i24" in obj_mnemonic:
        return "Long literal: e.g. -64 (0xffffffc0) outside short range [0..64, -16..-1]; cost may not have LiteralInt in IR.", True
    if "setreg" in obj_mnemonic:
        return "s_setreg_imm32_b32: 32-bit immediate in second word; handled in pass when source present.", True
    if "cndmask" in obj_mnemonic or "add_co_ci" in obj_mnemonic:
        return "VOP2->VOP3: last source not VCC; 8-byte encoding.", True
    if "v_cmp_" in obj_mnemonic:
        return "VOPC with dest != vcc (e.g. SGPR): promoted to VOP3.", True
    return ROOT_CAUSE_NOT_COVERED, False


def _align_by_position(obj_list, cost_list):
    """Pair obj and cost instructions by position (order): 1st with 1st, 2nd with 2nd, etc.
    Returns (pairs, extra_obj, extra_cost). pairs = [(i, obj_rec, cost_rec) for i in range(min(lens))].
    extra_obj/extra_cost are the tail when lengths differ.
    obj_rec = (mnemonic, size, obj_line_num, raw_line), cost_rec = (mnemonic, size, cost_line_num, raw_line)."""
    n = min(len(obj_list), len(cost_list))
    pairs = [(i, obj_list[i], cost_list[i]) for i in range(n)]
    # extra_obj: (idx, mnemonic, size, raw_line) for reporting
    extra_obj = [(n + i, obj_list[n + i][0], obj_list[n + i][1], obj_list[n + i][3]) for i in range(len(obj_list) - n)]
    extra_cost = [
        (cost_list[n + i][2], cost_list[n + i][0], cost_list[n + i][1], cost_list[n + i][3])
        for i in range(len(cost_list) - n)
    ]  # line_num, mnemonic, size, raw_line
    return pairs, extra_obj, extra_cost


def run_comparison(obj_log_path, cost_file_path, accumulate_debug_path=None):
    """Run comparison. Returns dict with total_byte_diff, total_obj, total_cost, diffs list, etc.
    Pairing: 1:1 by position (instruction index). Labels and s_nop are skipped on both sides; only remaining instructions are paired. Size is compared at each position.

    Total cost bytes: if ``accumulate_debug_path`` (or
    ``<dir of cost_file>/accumulate_instruction_size_pass_debug.txt``) contains
    ``[AccumulateInstructionSizePass] total size = N bytes``, that **N** is used as the canonical
    cost total (same as the pass summary). Otherwise the total is the sum of per-instruction sizes
    parsed from the cost file.
    """
    obj_list = get_obj_instructions(obj_log_path)
    cost_list = list(get_cost_sizes(cost_file_path))
    total_cost_from_lines = sum(s for _, s, _, _ in cost_list)

    acc_path = accumulate_debug_path
    if not acc_path:
        _d = os.path.dirname(os.path.abspath(cost_file_path))
        _cand = os.path.join(_d, "accumulate_instruction_size_pass_debug.txt")
        if os.path.isfile(_cand):
            acc_path = _cand
    total_from_accumulate = parse_accumulate_instruction_size_pass_total(acc_path) if acc_path else None
    if total_from_accumulate is not None:
        total_cost = total_from_accumulate
        total_cost_source = "accumulate_pass"
    else:
        total_cost = total_cost_from_lines
        total_cost_source = "sum_of_lines"
        acc_path = None

    if len(obj_list) != len(cost_list):
        sys.stderr.write(
            f"Warning: instruction count mismatch obj={len(obj_list)} cost={len(cost_list)}; "
            "pairing by position (1st with 1st, ...); extra at end.\n"
        )

    # Pair by position (same order in obj dump and cost file)
    pairs, extra_obj_list, extra_cost_list = _align_by_position(obj_list, cost_list)
    extra_obj_count = len(extra_obj_list)
    extra_cost_count = len(extra_cost_list)
    # Summarize extra by mnemonic for the report
    extra_obj_by_mnem = defaultdict(int)
    extra_cost_by_mnem = defaultdict(int)
    for _idx, mnem, _size, _line in extra_obj_list:
        extra_obj_by_mnem[mnem] += 1
    for _ln, mnem, _size, _line in extra_cost_list:
        extra_cost_by_mnem[mnem] += 1

    # Use last line (last offset + last instruction size) as canonical obj total when available
    total_obj_from_last = get_obj_total_from_last_line(obj_log_path)
    used_last_line = total_obj_from_last is not None
    total_obj = total_obj_from_last if used_last_line else sum(s for _, s, _, _ in obj_list)
    # total_cost / total_from_accumulate / total_cost_from_lines / acc_path set above
    total_byte_diff = total_obj - total_cost

    diffs = []
    by_mnemonic = defaultdict(lambda: {"count": 0, "obj_size": None, "cost_size": None, "diff": None})
    root_cause_counts = defaultdict(int)
    not_covered_by_mnemonic = defaultdict(int)

    for obj_idx, obj_rec, cost_rec in pairs:
        obj_mnem, obj_size, obj_ln, obj_line = obj_rec
        cost_mnem, cost_size, cost_ln, cost_line = cost_rec
        if obj_size != cost_size:
            diff = obj_size - cost_size
            diffs.append({
                "index": obj_idx,
                "obj_mnemonic": obj_mnem,
                "cost_mnemonic": cost_mnem,
                "obj_size": obj_size,
                "cost_size": cost_size,
                "diff": diff,
                "obj_line_no": obj_ln,
                "cost_line": cost_ln,
                "obj_line": obj_line,
                "cost_line_raw": cost_line,
            })
            key = (obj_mnem, obj_size, cost_size)
            by_mnemonic[key]["count"] += 1
            by_mnemonic[key]["obj_size"] = obj_size
            by_mnemonic[key]["cost_size"] = cost_size
            by_mnemonic[key]["diff"] = diff
            rc, covered = infer_root_cause(obj_mnem, obj_size, cost_size)
            root_cause_counts[rc] += 1
            if rc == ROOT_CAUSE_NOT_COVERED:
                not_covered_by_mnemonic[obj_mnem] += 1
            diffs[-1]["root_cause"] = rc

    return {
        "total_obj_bytes": total_obj,
        "total_obj_from_last_line_used": used_last_line,
        "total_cost_bytes": total_cost,
        "total_cost_bytes_from_lines": total_cost_from_lines,
        "total_cost_source": total_cost_source,
        "accumulate_debug_path_used": acc_path if total_from_accumulate is not None else None,
        "total_byte_diff": total_byte_diff,
        "obj_count": len(obj_list),
        "cost_count": len(cost_list),
        "paired_count": len(pairs),
        "unmatched_obj_count": extra_obj_count,
        "unmatched_cost_count": extra_cost_count,
        "unmatched_obj_by_mnemonic": dict(extra_obj_by_mnem),
        "unmatched_cost_by_mnemonic": dict(extra_cost_by_mnem),
        "unmatched_obj_list": extra_obj_list,
        "unmatched_cost_list": extra_cost_list,
        "diffs": diffs,
        "by_mnemonic": dict(by_mnemonic),
        "root_cause_counts": dict(root_cause_counts),
        "not_covered_by_mnemonic": dict(not_covered_by_mnemonic),
        "num_size_diffs": len(diffs),
    }


def write_report(result, output_prefix, kernel_name="kernel", output_dir=None):
    """Write diffs file and report with (1) byte diff (2) instructions with size diff and distance (3) root cause.
    If output_dir is set, write to output_dir/instruction_size_comparison_report.md and
    output_dir/instruction_size_diffs.txt (fixed names); otherwise use output_prefix for paths."""
    base = output_prefix or ""
    diffs = result["diffs"]
    by_mnemonic = result["by_mnemonic"]
    root_cause_counts = result["root_cause_counts"]
    not_covered = result.get("not_covered_by_mnemonic", {})

    # (1) Difference in bytes
    total_byte_diff = result["total_byte_diff"]
    total_obj = result["total_obj_bytes"]
    total_cost = result["total_cost_bytes"]
    from_last = result.get("total_obj_from_last_line_used", False)
    success_rate_pct = (100.0 - (abs(total_byte_diff) / total_obj * 100)) if total_obj and total_obj > 0 else None
    tcs = result.get("total_cost_source", "sum_of_lines")
    tcsum = result.get("total_cost_bytes_from_lines", total_cost)
    if tcs == "accumulate_pass":
        if tcsum is not None and tcsum != total_cost:
            cost_size_note = (
                " (from `[AccumulateInstructionSizePass] total size` in `accumulate_instruction_size_pass_debug.txt`; "
                f"sum of per-instruction `size=` lines: {tcsum} bytes)"
            )
        else:
            cost_size_note = (
                " (from `[AccumulateInstructionSizePass] total size` in `accumulate_instruction_size_pass_debug.txt`)"
            )
    else:
        cost_size_note = " (sum of per-instruction `size=` in the cost file)"
    pass_vs_line_explain = ""
    if tcs == "accumulate_pass" and tcsum is not None and tcsum != total_cost:
        pass_vs_line_explain = (
            f" The **{abs(tcsum - total_cost)}-byte** gap between the pass summary and the sum of `size=` lines in the cost file "
            "does not show up as paired per-index diffs (pairing still uses per-line sizes)."
        )

    # (2) Instructions with different size and distance
    lines_diff = []
    for d in diffs:
        obj_ln = d.get("obj_line_no", "")
        cost_ln = d["cost_line"]
        lines_diff.append(
            f"{d['index']}\tobj={d['obj_size']}\tcost={d['cost_size']}\tdiff={d['diff']:+d}\tobj_line={obj_ln}\tcost_line={cost_ln}\t{d['obj_mnemonic']}"
        )
    by_mnemonic_summary = []
    for (mnem, o_sz, c_sz), v in sorted(by_mnemonic.items(), key=lambda x: -x[1]["count"]):
        by_mnemonic_summary.append(f"  {mnem}  obj={o_sz} cost={c_sz}  count={v['count']}  (distance={o_sz - c_sz})")

    # (3) Root cause summary
    root_cause_lines = []
    for rc, count in sorted(root_cause_counts.items(), key=lambda x: -x[1]):
        root_cause_lines.append(f"  [{count}] {rc}")

    paired = result.get("paired_count", result["obj_count"])
    un_obj = result.get("unmatched_obj_count", 0)
    un_cost = result.get("unmatched_cost_count", 0)
    # Explain why total diff can be large even when "Count: N instructions" (paired size diffs) is small
    diff_from_paired = sum(d["diff"] for d in diffs)
    report_lines = [
        f"# Instruction size comparison: {kernel_name}",
        "",
        "## 1) Difference in bytes",
        f"- Total size (obj.log):   {total_obj} bytes" + (" (from max instruction address + that instruction size)" if from_last else ""),
        f"- Total size (cost file): {total_cost} bytes{cost_size_note}",
        f"- **Difference (obj − cost): {total_byte_diff:+d} bytes**",
        "",
        "**Why the total diff can be large:** The difference above is over *all* instructions (obj and cost totals). "
        f"Only **{diff_from_paired:+d} bytes** come from the {len(diffs)} paired instructions with size diff below. "
        + (
            f"The rest (**{total_byte_diff - diff_from_paired:+d} bytes**) is from **length mismatch**: "
            f"obj has {un_obj} extra at end, cost has {un_cost} extra at end (included in totals)."
            if total_byte_diff != diff_from_paired
            else ""
        )
        + pass_vs_line_explain,
        "",
        "**Alignment:** Strict 1:1 by position (instruction index 0 with 0, 1 with 1, …). "
        "Obj dump labels (<label_xxx>:), cost LABEL lines (opcode=758), and s_nop on both sides are skipped; only remaining instructions are paired. "
        f"Paired: {paired}; extra at end — obj: {un_obj}, cost: {un_cost}. "
        "**Why line numbers differ:** `obj_line` is the line number in the obj dump file; `cost_line` is the line in the cost file (which has many header/block lines before the first instruction), so cost line numbers are usually much larger.",
    ]
    if success_rate_pct is not None:
        report_lines.append(f"- **Success rate:** **{success_rate_pct:.2f}%** (100% − |difference| / total obj bytes)")
    report_lines.extend([
        "",
        "## 2) Instructions with different size (and distance)",
        f"- Count: {len(diffs)} instructions",
        "",
        "By (obj_mnemonic, obj_size, cost_size) with count and distance:",
        "",
    ] + by_mnemonic_summary + [
        "",
        "### Mismatched instructions (obj dump vs cost file)",
        "",
    ])
    # Show each diff with actual instruction lines from both obj and cost
    for d in diffs[:100]:
        obj_ln = d.get("obj_line_no", "?")
        cost_ln = d["cost_line"]
        report_lines.append(f"- **Index {d['index']}**  obj={d['obj_size']} cost={d['cost_size']} diff={d['diff']:+d}  obj_line={obj_ln} cost_line={cost_ln}  `{d['obj_mnemonic']}`")
        report_lines.append(f"  - **obj (line {obj_ln}):**   `{d['obj_line'].strip()[:120]}{'…' if len(d['obj_line']) > 120 else ''}`")
        report_lines.append(f"  - **cost (line {cost_ln}):**  `{d['cost_line_raw'].strip()[:120]}{'…' if len(d['cost_line_raw']) > 120 else ''}`")
        report_lines.append("")
    if len(diffs) > 100:
        report_lines.append(f"- *… and {len(diffs) - 100} more (see instruction_size_diffs.txt)*")
        report_lines.append("")
    report_lines.extend([
        "Per-index summary: index, obj_size, cost_size, diff, obj_line, cost_line, mnemonic",
        "",
    ] + [f"  {x}" for x in lines_diff[:50]])
    if len(lines_diff) > 50:
        report_lines.append(f"  ... and {len(lines_diff) - 50} more (see diffs file)")

    report_lines += [
        "",
        "## 3) Root cause (reasoning) for size differences",
        "",
    ] + root_cause_lines

    report_lines += [
        "",
        "## 4) Not covered cases",
        "",
    ]
    if not_covered:
        report_lines.append("Instructions with a size diff that have no specific root-cause rule (generic fallback):")
        report_lines.append("")
        for mnem, count in sorted(not_covered.items(), key=lambda x: -x[1]):
            report_lines.append(f"  - **{mnem}**: {count} instruction(s)")
    else:
        report_lines.append("  **None.** All size diffs have a specific root-cause rule.")
    report_lines.append("")

    # 5) Extra instructions at end (when obj and cost lengths differ)
    report_lines += ["", "## 5) Extra instructions at end (length mismatch)", ""]
    un_obj_by_mnem = result.get("unmatched_obj_by_mnemonic", {})
    un_cost_by_mnem = result.get("unmatched_cost_by_mnemonic", {})
    un_obj_list = result.get("unmatched_obj_list", [])
    un_cost_list = result.get("unmatched_cost_list", [])
    if un_obj or un_cost:
        report_lines.append(
            "When obj and cost have different instruction counts, the tail is not paired. "
            "These are included in the total byte counts above."
        )
        report_lines.append("")
        if un_obj_by_mnem:
            report_lines.append("**Extra obj at end (by mnemonic):**")
            for mnem, count in sorted(un_obj_by_mnem.items(), key=lambda x: -x[1]):
                report_lines.append(f"  - `{mnem}`: {count}")
            report_lines.append("")
        if un_cost_by_mnem:
            report_lines.append("**Extra cost at end (by mnemonic):**")
            for mnem, count in sorted(un_cost_by_mnem.items(), key=lambda x: -x[1]):
                report_lines.append(f"  - `{mnem}`: {count}")
            report_lines.append("")
        report_lines.append("**Sample extra obj (first 20):**")
        for idx, mnem, size, raw_line in un_obj_list[:20]:
            s = raw_line.strip()
            report_lines.append(f"  - [{idx}] {size}B `{mnem}`: `{s[:90]}{'…' if len(s) > 90 else ''}`")
        if len(un_obj_list) > 20:
            report_lines.append(f"  - *… and {len(un_obj_list) - 20} more*")
        report_lines.append("")
        report_lines.append("**Sample extra cost (first 20):**")
        for line_num, mnem, size, raw_line in un_cost_list[:20]:
            s = raw_line.strip()
            report_lines.append(f"  - [line {line_num}] {size}B `{mnem}`: `{s[:90]}{'…' if len(s) > 90 else ''}`")
        if len(un_cost_list) > 20:
            report_lines.append(f"  - *… and {len(un_cost_list) - 20} more*")
    else:
        report_lines.append("None; obj and cost have the same instruction count.")
    report_lines.append("")

    report_lines += [
        "",
        "---",
        "",
    ]

    report_text = "\n".join(report_lines)
    if output_dir:
        report_path = os.path.join(output_dir, "instruction_size_comparison_report.md")
        diffs_path = os.path.join(output_dir, "instruction_size_diffs.txt")
    elif base:
        report_path = base.rstrip("_") + "_instruction_size_comparison_report.md"
        diffs_path = base.rstrip("_") + "_instruction_size_diffs.txt"
    else:
        report_path = "instruction_size_comparison_report.md"
        diffs_path = "instruction_size_diffs.txt"

    with open(report_path, "w") as f:
        f.write(report_text)

    with open(diffs_path, "w") as f:
        f.write(f"Total instructions with different sizeInBytes: {len(diffs)}\n\n")
        f.write("Index\tobj_size\tcost_size\tdiff\tobj_line\tcost_line\tprobable_reason\n")
        f.write("-" * 60 + "\n")
        for d in diffs:
            rc = d.get("root_cause", "")
            obj_ln = d.get("obj_line_no", "")
            cost_ln = d["cost_line"]
            f.write(f"{d['index']}\t{d['obj_size']}\t{d['cost_size']}\t{d['diff']:+d}\t{obj_ln}\t{cost_ln}\t{rc}\n")
            f.write(f"  obj line {obj_ln}: {d['obj_line'][:90]}\n")
            f.write(f"  cost line {cost_ln}: {d['cost_line_raw'][:90]}\n\n")
        # Extra at end when lengths differ
        if un_obj_list or un_cost_list:
            f.write("\n" + "=" * 60 + "\n")
            f.write("EXTRA OBJ DUMP AT END (length mismatch)\n")
            f.write("=" * 60 + "\n\n")
            for idx, mnem, size, raw_line in un_obj_list:
                f.write(f"[{idx}] {size}B {mnem}: {raw_line.strip()}\n")
            f.write("\n" + "=" * 60 + "\n")
            f.write("EXTRA COST FILE AT END (length mismatch)\n")
            f.write("=" * 60 + "\n\n")
            for line_num, mnem, size, raw_line in un_cost_list:
                f.write(f"[line {line_num}] {size}B {mnem}: {raw_line.strip()}\n")
    return report_path, diffs_path, report_text


def main():
    ap = argparse.ArgumentParser(description="Compare instruction sizes: obj.log vs cost file")
    ap.add_argument("--obj-log", default=None, help="Path to obj.log (disassembly). Ignored if --object-file is set.")
    ap.add_argument("--object-file", "-o", default=None, help="Path to kernel .o; dump disassembly to obj.log then compare")
    ap.add_argument("--cost-file", default="kernel_name_aggregated_instruction_cost.txt", help="Path to cost file")
    ap.add_argument("--output-prefix", default="", help="Prefix for report/diffs output files")
    ap.add_argument("--output-dir", default=None, help="Directory for obj.log and reports when using --object-file")
    ap.add_argument("--kernel-name", default="kernel", help="Kernel name for report title")
    ap.add_argument("--llvm-objdump", default=LLVM_OBJDUMP_DEFAULT, help="Path to llvm-objdump")
    ap.add_argument(
        "--accumulate-debug",
        default=None,
        help="Path to accumulate_instruction_size_pass_debug.txt. If set, and it contains the pass "
        "summary line, that total is used for cost. If omitted, the file next to --cost-file is used if present.",
    )
    ap.add_argument(
        "--verify-elf-text",
        action="store_true",
        help="Only compare ELF64 .text sh_size (stdlib parser, no llvm-objdump) to "
        "[AccumulateInstructionSizePass] total size. Requires --object-file and --accumulate-debug.",
    )
    args = ap.parse_args()

    if args.verify_elf_text:
        if not args.object_file or not args.accumulate_debug:
            sys.stderr.write("--verify-elf-text requires --object-file / -o and --accumulate-debug\n")
            return 2
        v = verify_accumulate_pass_vs_elf_text(
            args.object_file, args.accumulate_debug, kernel_name=args.kernel_name
        )
        print(format_cost_vs_elf_report(v))
        return 0 if v.get("ok") else 1

    obj_log_path = args.obj_log
    output_dir = args.output_dir

    if args.object_file:
        # Automatically dump .o to obj.log, then compare
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            obj_log_path = os.path.join(output_dir, "obj.log")
        else:
            obj_log_path = "obj.log"
        if not dump_object_to_obj_log(args.object_file, obj_log_path, args.llvm_objdump):
            return 1
        print(f"Dumped: {args.object_file} -> {obj_log_path}")
        if output_dir:
            output_prefix = ""
        else:
            output_prefix = args.output_prefix
    else:
        obj_log_path = obj_log_path or "obj.log"
        output_prefix = args.output_prefix

    result = run_comparison(obj_log_path, args.cost_file, args.accumulate_debug)
    report_path, diffs_path, report_text = write_report(
        result, output_prefix, args.kernel_name, output_dir=args.output_dir or output_dir
    )

    print("============================================================")
    print("COMPARISON:", obj_log_path, "vs", args.cost_file)
    print("============================================================")
    print(f"\n1) Difference in bytes: {result['total_byte_diff']:+d} (obj={result['total_obj_bytes']} cost={result['total_cost_bytes']})")
    print(f"2) Instructions with different size: {result['num_size_diffs']}")
    print(f"3) Root causes: {result['root_cause_counts']}")
    if result.get("not_covered_by_mnemonic"):
        print(f"4) Not covered (no specific rule): {result['not_covered_by_mnemonic']}")
    print(f"\nReport: {report_path}")
    print(f"Diffs:  {diffs_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
