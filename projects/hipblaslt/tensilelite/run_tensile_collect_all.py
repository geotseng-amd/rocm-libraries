#!/usr/bin/env python3
"""
Run Tensile with a gfx12 config yaml, then collect instruction-size comparison for every kernel.

Step 1 (--run-tensile): run Tensile via build_tmp/Tensile.sh to generate kernels:
  bash build_tmp/Tensile.sh <yaml> <tensile-output-dir>
  The config yaml is copied to a temp file with GlobalParameters ISA: [[12, 5, 0]] (set or replaced)
  so runs default to gfx12.5.0 without changing checked-in yamls.

Step 2: run run_compare_all_kernels.py to collect obj dump, copy .s/.o, compare to cost, and write
  per-kernel dirs + AGGREGATE_<config>.md under output-dir.

Output layout (with --data-root /data0):
  - /data0/tensileLite/<config_name>/   (Tensile output: .o and .s)
  - /data0/comparison_output/<config_name>/
    - AGGREGATE_<config_name>.md
    - <full_kernel_name>/  (obj_dump.log, .s, .o, aggregated_instruction_cost.txt, reports)

Usage:
  # All under /data0: Tensile out + comparison_output
  python3 run_tensile_collect_all.py --run-tensile --data-root /data0

  # Only collect from existing Tensile output under /data0
  python3 run_tensile_collect_all.py --data-root /data0 --tensile-output-dir /data0/tensileLite/1024_vgpr_gfx1250

  # Explicit paths (no --data-root)
  python3 run_tensile_collect_all.py --run-tensile --tensile-output-dir tensileLite/1024_vgpr_gfx1250 --output-dir comparison_output/1024_vgpr_gfx1250

  # Sparse yaml outside gemm/gfx12: --config-name sets output dirs; --config-yaml is the file
  python3 run_tensile_collect_all.py --run-tensile --config-name spmm_f8_ml --config-yaml Tensile/Tests/common/sparse/gfx1250/spmm_f8_ml.yaml --tensile-output-dir tensileLite/spmm_f8_ml --output-dir comparison_output/spmm_f8_ml
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GFX12_YAML_DIR = os.path.join(SCRIPT_DIR, "Tensile", "Tests", "common", "gemm", "gfx12")
# Default: build_tmp/Tensile.sh (builds then runs Tensile; same as manual run)
TENSILE_SH = os.path.join(SCRIPT_DIR, "build_tmp", "Tensile.sh")
DEFAULT_CONFIG_NAME = "1024_vgpr_gfx1250"


def main():
    ap = argparse.ArgumentParser(
        description="Run a gfx12 config yaml and collect instruction-size comparison for all kernels"
    )
    ap.add_argument(
        "--config-name",
        default=DEFAULT_CONFIG_NAME,
        help=f"Config name for output dirs (tensileLite/<name>, comparison_output/<name>). Default: {DEFAULT_CONFIG_NAME}",
    )
    ap.add_argument(
        "--config-yaml",
        default=None,
        metavar="PATH",
        help="Path to the Tensile config .yaml (relative to tensilelite dir or absolute). "
        "Use this for yamls outside Tensile/Tests/common/gemm/gfx12/ (e.g. sparse/gfx1250). "
        "Still pass --config-name matching the logical name (usually the yaml basename without .yaml).",
    )
    ap.add_argument(
        "--run-tensile",
        action="store_true",
        help="Run Tensile with the config yaml first (generates .o under --tensile-output-dir)",
    )
    ap.add_argument(
        "--tensile-output-dir",
        default=None,
        help="Tensile output dir (required if --run-tensile or for collection). Contains 1_BenchmarkProblems/*/.../assembly/*.o",
    )
    ap.add_argument(
        "--cost-dir",
        default=None,
        help="Directory with *_aggregated_instruction_cost.txt (default: same as --tensile-output-dir)",
    )
    ap.add_argument(
        "--output-dir",
        default=None,
        help="Where to write comparison output (default: comparison_output/<config_name>)",
    )
    ap.add_argument(
        "--data-root",
        default=None,
        help="Put Tensile output and comparison_output under this dir (e.g. /data0). "
        "Implies: tensile-output-dir=<data-root>/tensileLite/<config_name>, "
        "output-dir=<data-root>/comparison_output/<config_name> unless overridden.",
    )
    ap.add_argument(
        "--skip-tensile",
        action="store_true",
        help="Do not run Tensile; only collect from existing --tensile-output-dir",
    )
    ap.add_argument(
        "--allow-single-cost-file",
        action="store_true",
        help="If only one cost file exists, use it for every kernel",
    )
    ap.add_argument(
        "--tensile-script",
        default=TENSILE_SH,
        help=f"Path to Tensile.sh (default: build_tmp/Tensile.sh). Used when --run-tensile.",
    )
    args = ap.parse_args()

    CONFIG_NAME = args.config_name
    if args.config_yaml:
        CONFIG_YAML = (
            os.path.abspath(args.config_yaml)
            if os.path.isabs(args.config_yaml)
            else os.path.normpath(os.path.join(SCRIPT_DIR, args.config_yaml))
        )
    else:
        CONFIG_YAML = os.path.join(GFX12_YAML_DIR, CONFIG_NAME + ".yaml")

    # Derive paths from --data-root when set
    data_root = None
    if args.data_root:
        data_root = os.path.abspath(args.data_root)
        if not args.tensile_output_dir:
            args.tensile_output_dir = os.path.join(data_root, "tensileLite", CONFIG_NAME)
        if not args.output_dir:
            args.output_dir = os.path.join(data_root, "comparison_output", CONFIG_NAME)

    if args.run_tensile:
        if not args.tensile_output_dir:
            sys.stderr.write("--run-tensile requires --tensile-output-dir or --data-root\n")
            return 1
        if not os.path.isfile(CONFIG_YAML):
            sys.stderr.write(f"Config not found: {CONFIG_YAML}\n")
            return 1
        tensile_script = os.path.abspath(args.tensile_script)
        if not os.path.isfile(tensile_script):
            sys.stderr.write(
                f"Tensile script not found: {tensile_script}\n"
                "Build rocisa first (e.g. cmake -DTENSILE_BIN=Tensile -S . -B rocisa/build && cmake --build rocisa/build).\n"
            )
            return 1
        # Resolve tensile output path (absolute)
        out_path = args.tensile_output_dir
        if not os.path.isabs(out_path):
            out_path = os.path.join(SCRIPT_DIR, out_path)
        out_path = os.path.abspath(out_path)
        os.makedirs(out_path, exist_ok=True)

        # Where comparison output (and cost files) will go
        comparison_out = args.output_dir or os.path.join(SCRIPT_DIR, "comparison_output", CONFIG_NAME)
        if not os.path.isabs(comparison_out):
            comparison_out = os.path.join(SCRIPT_DIR, comparison_out)
        comparison_out = os.path.abspath(comparison_out)
        os.makedirs(comparison_out, exist_ok=True)

        # Use a yaml that writes cost files to comparison_out (e.g. /data0/comparison_output/<config_name>)
        yaml_to_use = CONFIG_YAML
        try:
            with open(CONFIG_YAML, "r") as f:
                yaml_content = f.read()
            new_line = f"  StinkyTofuCostOutputDir: {comparison_out}"
            # Replace existing StinkyTofuCostOutputDir, or add it if missing (so all configs write cost files to comparison_out)
            if "StinkyTofuCostOutputDir:" in yaml_content:
                yaml_content = re.sub(
                    r"\n  StinkyTofuCostOutputDir:.*",
                    "\n" + new_line,
                    yaml_content,
                    count=1,
                )
            else:
                # Insert under GlobalParameters (after "Architecture: gfx1250" which every gfx12 yaml has)
                yaml_content = re.sub(
                    r"(\n  Architecture: gfx1250\n)",
                    r"\1" + new_line + "\n",
                    yaml_content,
                    count=1,
                )
            # Force NumElementsToValidate: 0 in temp yaml only (original gfx12 yamls are not modified)
            if "NumElementsToValidate:" in yaml_content:
                yaml_content = re.sub(
                    r"\n  NumElementsToValidate:.*",
                    "\n  NumElementsToValidate: 0",
                    yaml_content,
                    count=1,
                )
            else:
                yaml_content = re.sub(
                    r"(\nGlobalParameters:\n)",
                    r"\1  NumElementsToValidate: 0\n",
                    yaml_content,
                    count=1,
                )
            # NumWarmups: 0, EnqueuesPerSync: 1 when running via script
            for key, val in (("NumWarmups", "0"), ("EnqueuesPerSync", "1")):
                pattern = r"\n  " + re.escape(key) + r":.*"
                repl = "\n  " + f"{key}: {val}"
                if f"{key}:" in yaml_content:
                    yaml_content = re.sub(pattern, repl, yaml_content, count=1)
                else:
                    # Insert after Architecture: gfx1250 when key is missing
                    yaml_content = re.sub(
                        r"(\n  Architecture: gfx1250\n)",
                        r"\1  " + key + ": " + val + "\n",
                        yaml_content,
                        count=1,
                    )
            # Default ISA for this script (gfx12.5.0); many gfx12 yamls omit ISA and rely on agent.
            if re.search(r"\n  ISA:", yaml_content):
                yaml_content = re.sub(
                    r"\n  ISA:.*",
                    "\n  ISA: [[12, 5, 0]]",
                    yaml_content,
                    count=1,
                )
            else:
                patched = re.sub(
                    r"(\n  Architecture: gfx1250\n)",
                    r"\1  ISA: [[12, 5, 0]]\n",
                    yaml_content,
                    count=1,
                )
                if patched == yaml_content:
                    yaml_content = re.sub(
                        r"(\nGlobalParameters:\n)",
                        r"\1  ISA: [[12, 5, 0]]\n",
                        yaml_content,
                        count=1,
                    )
                else:
                    yaml_content = patched
            fd, tmp_yaml = tempfile.mkstemp(suffix=".yaml", prefix="tensile_", dir=SCRIPT_DIR)
            try:
                os.write(fd, yaml_content.encode("utf-8"))
                os.close(fd)
                fd = None
                yaml_to_use = tmp_yaml
                yaml_rel = os.path.relpath(yaml_to_use, SCRIPT_DIR)
            except Exception:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                os.remove(tmp_yaml)
                raise
        except Exception as e:
            sys.stderr.write(f"Warning: could not patch yaml for cost output dir, using default: {e}\n")
            yaml_rel = os.path.relpath(CONFIG_YAML, SCRIPT_DIR)

        out_rel = os.path.relpath(out_path, SCRIPT_DIR)
        script_rel = os.path.relpath(tensile_script, SCRIPT_DIR)
        cmd = ["bash", script_rel, yaml_rel, out_rel]
        sys.stderr.write(f"Running Tensile (generating kernels): {' '.join(cmd)}\n")
        if data_root:
            sys.stderr.write(f"  Tensile output: {out_path}\n")
            sys.stderr.write(f"  Cost/output dir: {comparison_out}\n")
        r = subprocess.run(cmd, cwd=SCRIPT_DIR, shell=False)
        if yaml_to_use != CONFIG_YAML and os.path.isfile(yaml_to_use):
            try:
                os.remove(yaml_to_use)
            except OSError:
                pass
        if r.returncode != 0:
            sys.stderr.write(f"Tensile exited with code {r.returncode}\n")
            return r.returncode
        args.tensile_output_dir = out_path

    if not args.tensile_output_dir:
        sys.stderr.write(
            "Provide --tensile-output-dir (path to Tensile output with 1_BenchmarkProblems/*/.../assembly/*.o). "
            "To only regenerate the aggregate from existing comparison_output, run run_compare_all_kernels.py --from-existing-output.\n"
        )
        return 1

    out_dir = args.output_dir or os.path.join(SCRIPT_DIR, "comparison_output", CONFIG_NAME)
    if not os.path.isabs(out_dir):
        out_dir = os.path.abspath(os.path.join(SCRIPT_DIR, out_dir))
    cost_dir = args.cost_dir if args.cost_dir is not None else out_dir
    if not os.path.isabs(cost_dir):
        cost_dir = os.path.abspath(os.path.join(SCRIPT_DIR, cost_dir))

    # Run comparison driver (dump .o → obj_dump.log, copy .s/.o, compare when both obj_dump and cost exist)
    run_compare = os.path.join(SCRIPT_DIR, "run_compare_all_kernels.py")
    cmd = [
        sys.executable,
        run_compare,
        "--config-name",
        CONFIG_NAME,
        "--output-dir",
        out_dir,
        "--cost-dir",
        cost_dir,
    ]
    if args.tensile_output_dir:
        tensile_dir = args.tensile_output_dir
        if not os.path.isabs(tensile_dir):
            tensile_dir = os.path.abspath(os.path.join(SCRIPT_DIR, tensile_dir))
        # TensileLite outputs kernel objects under an "assembly" directory, but the directory
        # depth/layout varies (e.g. older: 00_Final/source/.../assembly; newer:
        # 00_Final/caches/<hash>/source/.../assembly). Use the generic recursive assembly-dir
        # scan to support both layouts.
        cmd.extend(["--assembly-dir", os.path.join(tensile_dir, "1_BenchmarkProblems")])
    if args.allow_single_cost_file:
        cmd.append("--allow-single-cost-file")

    sys.stderr.write(f"Running: {' '.join(cmd)}\n")
    r = subprocess.run(cmd, cwd=SCRIPT_DIR)
    if r.returncode != 0:
        return r.returncode

    print(f"\nCollected all kernel data under: {out_dir}")
    print(f"Aggregate report: {out_dir}/AGGREGATE_{CONFIG_NAME}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
