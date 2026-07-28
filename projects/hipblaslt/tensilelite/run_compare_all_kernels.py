#!/usr/bin/env python3
"""
Run instruction size comparison for all kernels from a config (e.g. 1024_vgpr_gfx1250).

Flow:
  1) Discover kernels from .o files (--tensile-output-dir or --assembly-dir). Full kernel name = .o basename (no .o).
  2) For each kernel: create <out_dir>/<full_kernel_name>/; run objdump on the .o and write obj_dump.log there;
     copy .s and .o from tensile/assembly into that dir. Copy cost file into aggregated_instruction_cost.txt if needed.
  3) Compare and generate per-kernel report only when both obj_dump.log and aggregated_instruction_cost.txt exist.

Output layout:
  <out_dir>/<full_kernel_name>/obj_dump.log
  <out_dir>/<full_kernel_name>/<full_name>.s, <full_name>.o
  <out_dir>/<full_kernel_name>/aggregated_instruction_cost.txt
  <out_dir>/<full_kernel_name>/instruction_size_comparison_report.md  (only if both obj_dump and cost exist)
  <out_dir>/AGGREGATE_<config_name>.md

Usage:
  python3 run_compare_all_kernels.py --config-name 1024_vgpr_gfx1250 \\
      --tensile-output-dir <tensile_top> [--cost-dir <dir>] [--output-dir <out_dir>]
  python3 run_compare_all_kernels.py --config-name 1024_vgpr_gfx1250 --assembly-dir <dir_with_.o_files> ...
"""
import argparse
import os
import shutil
import subprocess
import sys
from collections import defaultdict

LLVM_OBJDUMP = "/opt/rocm/lib/llvm/bin/llvm-objdump"


def find_o_files(assembly_dir, assembly_only=True):
    """Recursively find .o files under assembly_dir. Returns list of (full_path, kernel_name).
    If assembly_only=True, only include .o files under a path containing '/assembly/' (kernel objects)."""
    out = []
    for root, _dirs, files in os.walk(assembly_dir):
        for f in files:
            if f.endswith(".o"):
                path = os.path.join(root, f)
                if assembly_only and "/assembly/" not in path:
                    continue
                name = f[:-2]  # strip .o
                out.append((path, name))
    return out


def find_o_files_from_tensile_output(tensile_output_dir):
    """Find all kernel .o files under Tensile output: 1_BenchmarkProblems/*/00_Final/source/build_tmp/SOURCE/assembly/."""
    out = []
    base = os.path.join(tensile_output_dir, "1_BenchmarkProblems")
    if not os.path.isdir(base):
        return out
    for name in os.listdir(base):
        assembly_dir = os.path.join(
            base, name, "00_Final", "source", "build_tmp", "SOURCE", "assembly"
        )
        if os.path.isdir(assembly_dir):
            out.extend(find_o_files(assembly_dir, assembly_only=False))
    return out


def filter_o_list_to_pairs_with_s(o_list):
    """Keep only (o_path, kernel_name) where the .s file exists next to the .o. Returns (filtered_list, skipped_count)."""
    filtered = []
    skipped = 0
    for o_path, kernel_name in o_list:
        if o_path is None:
            filtered.append((o_path, kernel_name))
            continue
        o_dir = os.path.dirname(o_path)
        s_path = os.path.join(o_dir, kernel_name + ".s")
        if os.path.isfile(s_path):
            filtered.append((o_path, kernel_name))
        else:
            skipped += 1
    return filtered, skipped


def find_cost_files(cost_dir):
    """Find cost files: 1) in cost_dir/<kernel_name>/ (aggregated_instruction_cost.txt or <name>_aggregated_instruction_cost.txt),
    2) flat in cost_dir (*_aggregated_instruction_cost.txt). Returns dict kernel_name -> path."""
    d = {}
    base = cost_dir or "."
    if not os.path.isdir(base):
        return d
    # 1) Per-kernel subdirs: cost_dir/<full_kernel_name>/aggregated_instruction_cost.txt or .../<name>_aggregated_instruction_cost.txt
    for name in os.listdir(base):
        kernel_dir = os.path.join(base, name)
        if not os.path.isdir(kernel_dir):
            continue
        for cand in (
            os.path.join(kernel_dir, name + "_aggregated_instruction_cost.txt"),
            os.path.join(kernel_dir, "aggregated_instruction_cost.txt"),
        ):
            if os.path.isfile(cand):
                d[name] = cand
                break
    # 2) Flat layout: cost_dir/*_aggregated_instruction_cost.txt (don't overwrite if already in d)
    for f in os.listdir(base):
        if f.endswith("_aggregated_instruction_cost.txt"):
            name = f.replace("_aggregated_instruction_cost.txt", "")
            if name not in d:
                d[name] = os.path.join(base, f)
    return d


def find_accumulate_pass_debug_files(base_dir):
    """Find per-kernel pass debug files:
    1) base_dir/<kernel_name>/accumulate_instruction_size_pass_debug.txt
    Returns dict kernel_name -> path."""
    d = {}
    if not base_dir or not os.path.isdir(base_dir):
        return d
    for name in os.listdir(base_dir):
        kernel_dir = os.path.join(base_dir, name)
        if not os.path.isdir(kernel_dir):
            continue
        cand = os.path.join(kernel_dir, "accumulate_instruction_size_pass_debug.txt")
        if os.path.isfile(cand):
            d[name] = cand
    return d


def dump_obj(o_path, out_log_path, llvm_objdump_path):
    """Run llvm-objdump -d o_path > out_log_path. Returns True on success."""
    try:
        with open(out_log_path, "w") as out:
            subprocess.run(
                [llvm_objdump_path, "-d", o_path],
                stdout=out,
                stderr=subprocess.PIPE,
                check=True,
                timeout=60,
            )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(f"llvm-objdump failed for {o_path}: {e}\n")
        return False


def main():
    ap = argparse.ArgumentParser(description="Compare instruction sizes for all kernels from a config")
    ap.add_argument("--config-name", default="1024_vgpr_gfx1250", help="Config short name (e.g. 1024_vgpr_gfx1250)")
    ap.add_argument("--assembly-dir", default=None, help="Directory containing .o files (searched recursively)")
    ap.add_argument("--tensile-output-dir", default=None, help="Top-level Tensile output (e.g. from: Tensile ... 1024_vgpr_gfx1250.yaml <dir>). Finds all 1_BenchmarkProblems/*/.../assembly/*.o")
    ap.add_argument("--cost-dir", default=None, help="Root for cost files: per-kernel subdirs <cost-dir>/<kernel_name>/aggregated_instruction_cost.txt, or flat *_aggregated_instruction_cost.txt. Default: same as --output-dir (comparison_output/<config-name>/).")
    ap.add_argument("--output-dir", default=None, help="Output dir for obj logs and report (default: comparison_output/<config-name>)")
    ap.add_argument("--llvm-objdump", default=LLVM_OBJDUMP, help="Path to llvm-objdump")
    ap.add_argument("--obj-dump-log", default="obj_dump.log", help="Filename for objdump output in each kernel dir (default: obj_dump.log)")
    ap.add_argument("--allow-single-cost-file", action="store_true", help="If only one cost file exists, use it for every .o (for runs where cost file name != kernel .o name)")
    ap.add_argument("--from-existing-output", action="store_true", help="Regenerate AGGREGATE from existing obj_dump.log/obj.log in output-dir (no .o or assembly-dir needed)")
    ap.add_argument("--copy-s-o", action="store_true", default=True, help="Copy .s and .o into each kernel dir (default: True). Set --no-copy-s-o to disable.")
    ap.add_argument("--no-copy-s-o", action="store_false", dest="copy_s_o", help="Do not copy .s and .o into kernel dirs")
    args = ap.parse_args()

    out_dir = args.output_dir or os.path.join("comparison_output", args.config_name)
    os.makedirs(out_dir, exist_ok=True)
    cost_dir = args.cost_dir if args.cost_dir is not None else out_dir
    cost_map = find_cost_files(cost_dir)
    cost_kind = "aggregated_cost"
    if not cost_map:
        # Fallback: allow compare using AccumulateInstructionSizePass debug output as the "cost" source.
        # This is useful when Backend did not emit *_aggregated_instruction_cost.txt but the pass wrote
        # <out_dir>/<kernel>/accumulate_instruction_size_pass_debug.txt.
        dbg_map = find_accumulate_pass_debug_files(out_dir if args.cost_dir is None else cost_dir)
        if dbg_map:
            cost_map = dbg_map
            cost_kind = "accumulate_pass_debug"

    if args.from_existing_output:
        # Prefer per-kernel dirs: out_dir/<kernel_name>/obj_dump.log or obj.log (full kernel name = dir name)
        o_list = []
        for name in sorted(os.listdir(out_dir)):
            kernel_dir = os.path.join(out_dir, name)
            if not os.path.isdir(kernel_dir):
                continue
            obj_log = os.path.join(kernel_dir, args.obj_dump_log)
            if not os.path.isfile(obj_log):
                obj_log = os.path.join(kernel_dir, "obj.log")
            if not os.path.isfile(obj_log) or os.path.getsize(obj_log) == 0:
                continue
            o_list.append((None, name))
        # Fallback: flat layout out_dir/<kernel_name>_obj.log (legacy)
        if not o_list:
            for f in sorted(os.listdir(out_dir)):
                if f.endswith("_obj.log"):
                    path = os.path.join(out_dir, f)
                    if not os.path.isfile(path) or os.path.getsize(path) == 0:
                        continue
                    kernel_name = f.replace("_obj.log", "")
                    o_list.append((None, kernel_name))
        if not o_list:
            sys.stderr.write(f"No kernel dirs with {args.obj_dump_log}/obj.log or *_obj.log found in {out_dir}\n")
            return 1
    elif args.tensile_output_dir:
        o_list = find_o_files_from_tensile_output(args.tensile_output_dir)
        if not o_list:
            sys.stderr.write(f"No .o files found under {args.tensile_output_dir}/1_BenchmarkProblems/*/.../assembly\n")
            return 1
        # Only compare/aggregate kernels that have both .o and .s in the Tensile output
        o_list, skipped = filter_o_list_to_pairs_with_s(o_list)
        if skipped:
            sys.stderr.write(f"Skipped {skipped} kernel(s) with no .s file next to .o in {args.tensile_output_dir}\n")
        if not o_list:
            sys.stderr.write("No kernels with both .o and .s found; nothing to do.\n")
            return 1
    elif args.assembly_dir:
        o_list = find_o_files(args.assembly_dir)
        if not o_list:
            sys.stderr.write(f"No .o files found under {args.assembly_dir}\n")
            return 1
        # Only compare/aggregate kernels that have both .o and .s
        o_list, skipped = filter_o_list_to_pairs_with_s(o_list)
        if skipped:
            sys.stderr.write(f"Skipped {skipped} kernel(s) with no .s file next to .o in {args.assembly_dir}\n")
        if not o_list:
            sys.stderr.write("No kernels with both .o and .s found; nothing to do.\n")
            return 1
    else:
        sys.stderr.write("Provide either --assembly-dir or --tensile-output-dir\n")
        return 1
    if not cost_map:
        sys.stderr.write(
            f"No *_aggregated_instruction_cost.txt found in {args.cost_dir}; "
            f"also no per-kernel accumulate_instruction_size_pass_debug.txt found in {out_dir}\n"
        )
        return 1

    # Import here so we can run compare_instruction_sizes logic
    import compare_instruction_sizes as cmp_mod

    single_cost_path = None
    if args.allow_single_cost_file and len(cost_map) == 1:
        single_cost_path = list(cost_map.values())[0]
        sys.stderr.write(f"Using single {cost_kind} file for all .o: {single_cost_path}\n")
    elif len(cost_map) > 1:
        sys.stderr.write(
            f"Multiple {cost_kind} files ({len(cost_map)}): each kernel will use only exact-name match.\n"
        )

    obj_dump_name = args.obj_dump_log
    cost_in_dir_name = "aggregated_instruction_cost.txt"
    per_kernel = []

    for o_path, kernel_name in o_list:
        # Full kernel name = .o basename without .o (from find_o_files / find_o_files_from_tensile_output)
        kernel_dir = os.path.join(out_dir, kernel_name)
        obj_log_path = os.path.join(kernel_dir, obj_dump_name)
        cost_in_kernel_dir = os.path.join(kernel_dir, cost_in_dir_name)

        if o_path is not None:
            # 1) Create kernel dir and dump .o into it; copy .s and .o
            os.makedirs(kernel_dir, exist_ok=True)
            if not dump_obj(o_path, obj_log_path, args.llvm_objdump):
                continue
            if args.copy_s_o:
                shutil.copy2(o_path, os.path.join(kernel_dir, kernel_name + ".o"))
                o_dir = os.path.dirname(o_path)
                s_path = os.path.join(o_dir, kernel_name + ".s")
                if os.path.isfile(s_path):
                    shutil.copy2(s_path, os.path.join(kernel_dir, kernel_name + ".s"))
            # 2) Resolve cost file (may already be in kernel_dir if StinkyTofu wrote there)
            cost_path = cost_map.get(kernel_name) or single_cost_path
            if cost_path and os.path.isfile(cost_path):
                # For aggregated cost files, keep the historical copy into aggregated_instruction_cost.txt.
                # For pass debug files, keep them in place (avoid copying into aggregated_instruction_cost.txt).
                if cost_kind == "aggregated_cost":
                    dst = cost_in_kernel_dir
                    if os.path.abspath(cost_path) != os.path.abspath(dst):
                        shutil.copy2(cost_path, dst)
                    cost_path = cost_in_kernel_dir if os.path.isfile(cost_in_kernel_dir) else cost_path
                else:
                    cost_path = cost_path
            else:
                cost_path = cost_in_kernel_dir if os.path.isfile(cost_in_kernel_dir) else (
                    cost_map.get(kernel_name) or single_cost_path
                )
        else:
            # --from-existing-output: use existing obj_dump.log and cost in kernel_dir
            if not os.path.isfile(obj_log_path):
                obj_log_path = os.path.join(kernel_dir, "obj.log")
            if not os.path.isfile(obj_log_path) or os.path.getsize(obj_log_path) == 0:
                sys.stderr.write(f"No obj log for kernel {kernel_name}, skip\n")
                continue
            cost_path = cost_in_kernel_dir if os.path.isfile(cost_in_kernel_dir) else None
            if not cost_path:
                # Prefer per-kernel pass debug file if present.
                dbg = os.path.join(kernel_dir, "accumulate_instruction_size_pass_debug.txt")
                if os.path.isfile(dbg):
                    cost_path = dbg
                else:
                    flat_cost = os.path.join(out_dir, kernel_name + "_aggregated_instruction_cost.txt")
                    if os.path.isfile(flat_cost):
                        cost_path = flat_cost

        # 3) Compare and generate report only when both obj_dump and cost file exist
        if not cost_path or not os.path.isfile(cost_path):
            sys.stderr.write(f"No cost file for kernel {kernel_name}, skip comparison\n")
            continue
        if not os.path.isfile(obj_log_path) or os.path.getsize(obj_log_path) == 0:
            sys.stderr.write(f"No obj dump for kernel {kernel_name}, skip comparison\n")
            continue

        acc_dbg = os.path.join(kernel_dir, "accumulate_instruction_size_pass_debug.txt")
        acc_arg = acc_dbg if os.path.isfile(acc_dbg) else None
        # Prefer the kernel’s accumulate pass debug (same dir as obj) so cost total works even when
        # cost_path is an aggregated file under a different directory (e.g. out_dir).
        result = cmp_mod.run_comparison(obj_log_path, cost_path, acc_arg)
        cmp_mod.write_report(result, "", kernel_name, output_dir=kernel_dir)

        total_obj = result["total_obj_bytes"]
        byte_diff = result["total_byte_diff"]
        success_rate_pct = (100.0 - (abs(byte_diff) / total_obj * 100)) if total_obj and total_obj > 0 else None
        per_kernel.append({
            "kernel_name": kernel_name,
            "byte_diff": byte_diff,
            "total_obj": total_obj,
            "total_cost": result["total_cost_bytes"],
            "num_diffs": result["num_size_diffs"],
            "root_causes": result["root_cause_counts"],
            "not_covered_by_mnemonic": result.get("not_covered_by_mnemonic", {}),
            "by_mnemonic": result["by_mnemonic"],
            "diffs": result["diffs"],
            "obj_from_last_line": result.get("total_obj_from_last_line_used", False),
            "success_rate_pct": success_rate_pct,
            "paired_count": result.get("paired_count"),
            "unmatched_obj_count": result.get("unmatched_obj_count", 0),
            "unmatched_cost_count": result.get("unmatched_cost_count", 0),
        })
        sr_str = f"{success_rate_pct:.2f}%" if success_rate_pct is not None else "N/A"
        print(f"  {kernel_name}: byte_diff={byte_diff:+d}, size_diffs={result['num_size_diffs']}, success_rate={sr_str}")

    if not per_kernel:
        sys.stderr.write("No kernel had both .o and cost file; nothing to aggregate.\n")
        return 1

    # Aggregate report
    agg_path = os.path.join(out_dir, f"AGGREGATE_{args.config_name}.md")
    total_byte_diff = sum(p["byte_diff"] for p in per_kernel)
    success_rates = [p["success_rate_pct"] for p in per_kernel if p["success_rate_pct"] is not None]
    avg_success_rate_pct = (sum(success_rates) / len(success_rates)) if success_rates else None
    all_root_causes = defaultdict(int)
    all_not_covered = defaultdict(int)
    for p in per_kernel:
        for rc, cnt in p["root_causes"].items():
            all_root_causes[rc] += cnt
        for mnem, cnt in p.get("not_covered_by_mnemonic", {}).items():
            all_not_covered[mnem] += cnt

    lines = [
        f"# Instruction size comparison – all kernels ({args.config_name})",
        "",
        f"**Total kernels: {len(per_kernel)}**",
        "",
        "---",
        "",
        "## Summary table",
        "",
        "| Kernel | Difference (bytes) | Total obj (bytes) | Total cost (bytes) | # size diffs | Success rate % |",
        "|--------|--------------------|--------------------|--------------------|--------------|-----------------|",
    ]
    for p in per_kernel:
        sr = f"{p['success_rate_pct']:.2f}%" if p["success_rate_pct"] is not None else "N/A"
        lines.append(
            f"| {p['kernel_name']} | {p['byte_diff']:+d} | {p['total_obj']} | {p['total_cost']} | {p['num_diffs']} | {sr} |"
        )
    lines.extend([
        "",
        "**Aggregate byte difference (obj − cost) over all kernels:** " + f"**{total_byte_diff:+d} bytes**",
        "",
    ])
    if avg_success_rate_pct is not None:
        lines.append(f"**Average success rate:** **{avg_success_rate_pct:.2f}%** (100% − |difference| / total obj bytes per kernel).")
    else:
        lines.append("**Average success rate:** N/A (no kernel with valid total obj bytes).")
    lines.extend([
        "",
        "---",
        "",
    ])

    # Full section per kernel: (1) difference in bytes, (2) instructions with different size and distance, (3) root cause
    for p in per_kernel:
        kn = p["kernel_name"]
        lines.append(f"## Kernel: {kn}")
        lines.append("")
        lines.append("### 1) Difference in bytes")
        lines.append("")
        obj_note = " (from last line: last_offset + last_instruction_size)" if p.get("obj_from_last_line") else ""
        lines.append(f"- Total size (obj.log):   **{p['total_obj']}** bytes{obj_note}")
        lines.append(f"- Total size (cost file): **{p['total_cost']}** bytes")
        lines.append(f"- **Difference (obj − cost): {p['byte_diff']:+d} bytes**")
        sr = p["success_rate_pct"]
        if sr is not None:
            lines.append(f"- **Success rate:** **{sr:.2f}%** (100% − |difference| / total obj bytes)")
        lines.append(f"- **Output dir:** `{kn}/` ({args.obj_dump_log}, .s, .o, aggregated_instruction_cost.txt, instruction_size_comparison_report.md, instruction_size_diffs.txt)")
        if p.get("unmatched_obj_count", 0) or p.get("unmatched_cost_count", 0):
            lines.append(f"- **Alignment:** Paired by mnemonic; unmatched obj: {p.get('unmatched_obj_count', 0)}, unmatched cost: {p.get('unmatched_cost_count', 0)}")
        lines.append("")
        lines.append("### 2) Miss instructions (obj, cost, probable reason)")
        lines.append("")
        if p["diffs"]:
            for d in p["diffs"][:30]:
                rc = d.get("root_cause", "—")
                lines.append(f"- **Index {d['index']}**  obj={d['obj_size']} cost={d['cost_size']} diff={d['diff']:+d}  **Reason:** {rc}")
                lines.append(f"  - *obj:* `{d['obj_line'][:75]}{'…' if len(d['obj_line']) > 75 else ''}`")
                lines.append(f"  - *cost:* `{d['cost_line_raw'][:75]}{'…' if len(d['cost_line_raw']) > 75 else ''}`")
            if len(p["diffs"]) > 30:
                lines.append(f"- *… and {len(p['diffs']) - 30} more (see instruction_size_diffs.txt)*")
        else:
            lines.append("None.")
        lines.append("")
        lines.append("### 3) By mnemonic (obj_size, cost_size, count)")
        lines.append("")
        lines.append(f"- Count: **{p['num_diffs']}** instructions")
        lines.append("")
        if p["by_mnemonic"]:
            for (mnem, o_sz, c_sz), v in sorted(p["by_mnemonic"].items(), key=lambda x: -x[1]["count"])[:25]:
                lines.append(f"- `{mnem}`  obj={o_sz} cost={c_sz}  count={v['count']}  distance={o_sz - c_sz}")
            if len(p["by_mnemonic"]) > 25:
                lines.append(f"- ... and {len(p['by_mnemonic']) - 25} more")
        lines.append("")
        lines.append("### 4) Root cause (reasoning)")
        lines.append("")
        for rc, count in sorted(p["root_causes"].items(), key=lambda x: -x[1]):
            lines.append(f"- **[{count}]** {rc}")
        if p.get("not_covered_by_mnemonic"):
            lines.append("")
            lines.append("**Not covered (no specific rule):**")
            for mnem, count in sorted(p["not_covered_by_mnemonic"].items(), key=lambda x: -x[1]):
                lines.append(f"- **{mnem}**: {count}")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.extend([
        "## Root cause summary (all kernels combined)",
        "",
    ])
    for rc, count in sorted(all_root_causes.items(), key=lambda x: -x[1]):
        lines.append(f"- **[{count}]** {rc}")
    lines.append("")

    lines.extend([
        "## Not covered cases (all kernels)",
        "",
    ])
    if all_not_covered:
        lines.append("Instructions with a size diff that have no specific root-cause rule (generic fallback); consider adding rules for these mnemonics:")
        lines.append("")
        for mnem, count in sorted(all_not_covered.items(), key=lambda x: -x[1]):
            lines.append(f"- **{mnem}**: {count} instruction(s)")
    else:
        lines.append("**None.** All size diffs have a specific root-cause rule.")
    lines.append("")

    with open(agg_path, "w") as f:
        f.write("\n".join(lines))

    print(f"\nAggregate report: {agg_path}")
    print(f"Total byte difference (all kernels): {total_byte_diff:+d}")
    if avg_success_rate_pct is not None:
        print(f"Average success rate: {avg_success_rate_pct:.2f}%")
    if all_not_covered:
        print("Not covered (no specific rule):", dict(all_not_covered))
    else:
        print("Not covered (no specific rule): none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
