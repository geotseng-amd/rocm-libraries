#!/usr/bin/env bash
# Run config yaml(s) (Tensile.sh) and collect instruction-size comparison for all kernels.
#
# Step 1 – Run Tensile (generates .o/.s under tensile output dir, somewhere under an `assembly/` folder).
# Note: newer TensileLite layouts may include a hash under `00_Final/caches/<hash>/.../assembly/`.
# run_tensile_collect_all.py uses a recursive scan of 1_BenchmarkProblems to find kernel objects.
# It rewrites
# a temp yaml with ISA: [[12, 5, 0]] (plus cost dir / validate / warmup tweaks) so gfx12 configs
# target gfx12.5.0 without editing each yaml.
# Step 2 – For each .o: dump to obj_dump.log, copy .s/.o into <output-dir>/<full_kernel_name>/, compare, report.
#
# Usage:
#   ./run_tensile_collect_all.sh                        # Single config (1024_vgpr_gfx1250), Step 1 + 2
#   ./run_tensile_collect_all.sh --all-gfx1250           # All Tensile/.../gfx12/*_gfx1250.yaml configs
#   ./run_tensile_collect_all.sh --data-root /data0      # All under /data0 (tensile out + comparison_output)
#   ./run_tensile_collect_all.sh --config 1024_vgpr_gfx1250 --data-root /data0   # Single config: always Step 1 + 2 (rerun Tensile)
#   ./run_tensile_collect_all.sh --all-gfx1250 --data-root /data0             # All gfx1250 configs (if tensileLite/<config> exists with .o → Step 2 only)
#   ./run_tensile_collect_all.sh --collect-only         # Step 2 only (existing Tensile output)
#   ./run_tensile_collect_all.sh --from-existing        # Regenerate aggregate from existing output dir
#   ./run_tensile_collect_all.sh --config spmm_f8_ml --config-yaml Tensile/Tests/common/sparse/gfx1250/spmm_f8_ml.yaml
#     # Sparse (or other) yaml outside gemm/gfx12: --config-name sets output dirs; --config-yaml is the actual file
#   ./run_tensile_collect_all.sh --all-sparse-gfx1250 --data-root /data0
#     # Every Tensile/Tests/common/sparse/gfx1250/*.yaml (same Step 1/2 rules as --all-gfx1250)
#   ./run_tensile_collect_all.sh --all-gradient-gfx1250 --data-root /data0
#     # Every Tensile/Tests/common/gradient/gfx1250/*.yaml
#   ./run_tensile_collect_all.sh --all-streamk-gfx1250 --data-root /data0
#     # Every Tensile/Tests/common/streamk/gfx1250/*.yaml
#
# With DATA_ROOT set (e.g. export DATA_ROOT=/data0), Tensile output and comparison_output go under $DATA_ROOT.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$SCRIPT_DIR"

GFX12_DIR="Tensile/Tests/common/gemm/gfx12"
SPARSE_GFX1250_DIR="Tensile/Tests/common/sparse/gfx1250"
GRADIENT_GFX1250_DIR="Tensile/Tests/common/gradient/gfx1250"
STREAMK_GFX1250_DIR="Tensile/Tests/common/streamk/gfx1250"
DEFAULT_CONFIG="1024_vgpr_gfx1250"

# Optional: put everything under DATA_ROOT (e.g. /data0)
DATA_ROOT="${DATA_ROOT:-}"
RUN_ALL_GFX1250=false
RUN_ALL_SPARSE_GFX1250=false
RUN_ALL_GRADIENT_GFX1250=false
RUN_ALL_STREAMK_GFX1250=false
SINGLE_CONFIG=""   # when set, run only this config (e.g. b6f4ss_gfx1250)
CONFIG_YAML_PATH=""  # optional: path to yaml when not under Tensile/Tests/common/gemm/gfx12/
EXTRA_ARGS=()

# Temp yamls we generate to force SwInstructionPrefetch=[2] (Absolute) for this script's runs
# (see inject_swipf_abs). Cleaned up on exit; source yamls are never modified.
PATCHED_YAMLS=()
cleanup_patched_yamls() { rm -f "${PATCHED_YAMLS[@]}" 2>/dev/null || true; }
trap cleanup_patched_yamls EXIT
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--data-root" && -n "${2:-}" ]]; then
    DATA_ROOT="$2"
    shift 2
    continue
  fi
  if [[ "$1" == "--all-gfx1250" ]]; then
    RUN_ALL_GFX1250=true
    shift
    continue
  fi
  if [[ "$1" == "--all-sparse-gfx1250" ]]; then
    RUN_ALL_SPARSE_GFX1250=true
    shift
    continue
  fi
  if [[ "$1" == "--all-gradient-gfx1250" ]]; then
    RUN_ALL_GRADIENT_GFX1250=true
    shift
    continue
  fi
  if [[ "$1" == "--all-streamk-gfx1250" ]]; then
    RUN_ALL_STREAMK_GFX1250=true
    shift
    continue
  fi
  if [[ "$1" == "--config" && -n "${2:-}" ]]; then
    SINGLE_CONFIG="$2"
    shift 2
    continue
  fi
  if [[ "$1" == "--config-yaml" && -n "${2:-}" ]]; then
    CONFIG_YAML_PATH="$2"
    shift 2
    continue
  fi
  EXTRA_ARGS+=("$1")
  shift
done

# Discover config names: *_gfx1250.yaml under GFX12_DIR
get_all_gfx1250_configs() {
  local dir="$SCRIPT_DIR/$GFX12_DIR"
  if [[ ! -d "$dir" ]]; then
    echo "$DEFAULT_CONFIG"
    return
  fi
  for f in "$dir"/*_gfx1250*.yaml; do
    [[ -e "$f" ]] || continue
    basename "$f" .yaml
  done | sort -u
}

# Discover config names: *.yaml under sparse/gfx1250 (basename without .yaml)
get_all_sparse_gfx1250_configs() {
  local dir="$SCRIPT_DIR/$SPARSE_GFX1250_DIR"
  if [[ ! -d "$dir" ]]; then
    return
  fi
  for f in "$dir"/*.yaml; do
    [[ -e "$f" ]] || continue
    basename "$f" .yaml
  done | sort -u
}

# Discover config names: *.yaml under gradient/gfx1250 (basename without .yaml)
get_all_gradient_gfx1250_configs() {
  local dir="$SCRIPT_DIR/$GRADIENT_GFX1250_DIR"
  if [[ ! -d "$dir" ]]; then
    return
  fi
  for f in "$dir"/*.yaml; do
    [[ -e "$f" ]] || continue
    basename "$f" .yaml
  done | sort -u
}

# Discover config names: *.yaml under streamk/gfx1250 (basename without .yaml)
get_all_streamk_gfx1250_configs() {
  local dir="$SCRIPT_DIR/$STREAMK_GFX1250_DIR"
  if [[ ! -d "$dir" ]]; then
    return
  fi
  for f in "$dir"/*.yaml; do
    [[ -e "$f" ]] || continue
    basename "$f" .yaml
  done | sort -u
}

# Return 0 if tensile output dir exists and has at least one .o file anywhere under it
tensile_output_has_o_files() {
  local tensile_out="$1"
  [[ -d "$tensile_out" ]] || return 1
  local found
  found=$(find "$tensile_out" -name "*.o" -type f 2>/dev/null | head -1)
  [[ -n "$found" ]]
}

run_one_config() {
  local CONFIG_NAME="$1"
  local FORCE_FULL_RUN="${2:-}"   # when non-empty (e.g. "force"), always run Step 1 + Step 2
  # Optional $3: repo-relative yaml path (e.g. sparse/gfx1250/<name>.yaml). Else gemm/gfx12 or --config-yaml.
  local YAML_REL_OVERRIDE="${3:-}"
  local YAML
  local -a yaml_flags=()
  if [[ -n "$YAML_REL_OVERRIDE" ]]; then
    YAML="$YAML_REL_OVERRIDE"
    yaml_flags=(--config-yaml "$YAML_REL_OVERRIDE")
  elif [[ -n "${CONFIG_YAML_PATH:-}" ]]; then
    YAML="$CONFIG_YAML_PATH"
    yaml_flags=(--config-yaml "$CONFIG_YAML_PATH")
  else
    YAML="$GFX12_DIR/${CONFIG_NAME}.yaml"
  fi

  # Report config-name → yaml mismatches (warning only; no behavior change).
  if [[ ! -f "$SCRIPT_DIR/$YAML" ]]; then
    echo "WARNING: [$CONFIG_NAME] YAML not found: $YAML (use --config-yaml <path> if this config lives elsewhere)" >&2
  fi

  # Force SwInstructionPrefetch=[2] (Absolute) as the default for runs launched via this script,
  # without touching source yamls or Tensile/Common/GlobalParameters.py. We inject the parameter
  # into BenchmarkCommonParameters of a temp copy of the yaml and run against that. Configs that
  # already set SwInstructionPrefetch are left as-is (skip to avoid duplicate-key errors).
  local src_yaml="$SCRIPT_DIR/$YAML"
  if [[ -f "$src_yaml" ]] && ! grep -q "SwInstructionPrefetch" "$src_yaml"; then
    local patched_yaml
    patched_yaml="$(mktemp "$SCRIPT_DIR/swipfabs_XXXXXX.yaml")"
    PATCHED_YAMLS+=("$patched_yaml")
    awk '
      { print }
      /^[[:space:]]*BenchmarkCommonParameters:[[:space:]]*$/ {
        match($0, /^[[:space:]]*/); ind = substr($0, 1, RLENGTH)
        print ind "  - SwInstructionPrefetch: [2]"
      }
    ' "$src_yaml" > "$patched_yaml"
    yaml_flags=(--config-yaml "$(basename "$patched_yaml")")
    echo "  [$CONFIG_NAME] Injected SwInstructionPrefetch:[2] (Absolute) → $(basename "$patched_yaml")"
  fi
  local TENSILE_OUT="tensileLite/${CONFIG_NAME}"
  local OUT_DIR="comparison_output/${CONFIG_NAME}"
  local COST_DIR="$OUT_DIR"
  if [[ -n "${DATA_ROOT:-}" ]]; then
    TENSILE_OUT="${DATA_ROOT}/tensileLite/${CONFIG_NAME}"
    OUT_DIR="${DATA_ROOT}/comparison_output/${CONFIG_NAME}"
    COST_DIR="$OUT_DIR"
  fi

  if [[ "${MODE:-}" == "from-existing" ]]; then
    echo "=== [$CONFIG_NAME] Regenerate aggregate from existing $OUT_DIR ==="
    python3 run_compare_all_kernels.py --config-name "$CONFIG_NAME" --from-existing-output --output-dir "$OUT_DIR" --cost-dir "$OUT_DIR" --allow-single-cost-file
  elif [[ "${MODE:-}" == "collect-only" ]]; then
    echo "=== [$CONFIG_NAME] Step 2 only: Collect from existing Tensile output $TENSILE_OUT ==="
    python3 run_tensile_collect_all.py "${yaml_flags[@]}" --config-name "$CONFIG_NAME" --tensile-output-dir "$TENSILE_OUT" --output-dir "$OUT_DIR" --cost-dir "$COST_DIR" --allow-single-cost-file
  else
    # Full run (Step 1 + Step 2). With --config <name> we always rerun Tensile; with --all-gfx1250 skip Step 1 if output exists with .o.
    local do_collect_only=false
    if [[ -n "$FORCE_FULL_RUN" ]]; then
      echo "=== [$CONFIG_NAME] Target config: run Tensile (Step 1 + 2) ==="
    elif tensile_output_has_o_files "$TENSILE_OUT"; then
      echo "=== [$CONFIG_NAME] $TENSILE_OUT exists with .o files → Step 2 only (re-dump obj, comparison, report) ==="
      do_collect_only=true
    else
      echo "=== [$CONFIG_NAME] $TENSILE_OUT missing or no .o files → run Tensile (Step 1 + 2) ==="
    fi
    if [[ "$do_collect_only" == true ]]; then
      python3 run_tensile_collect_all.py "${yaml_flags[@]}" --config-name "$CONFIG_NAME" --tensile-output-dir "$TENSILE_OUT" --output-dir "$OUT_DIR" --cost-dir "$COST_DIR" --allow-single-cost-file
    else
      # Full steps: clear both dirs first so we start from a clean state
      [[ -d "$TENSILE_OUT" ]] && rm -rf "$TENSILE_OUT" && echo "  Cleared $TENSILE_OUT"
      [[ -d "$OUT_DIR" ]] && rm -rf "$OUT_DIR" && echo "  Cleared $OUT_DIR"
      echo "=== [$CONFIG_NAME] Step 1: Run Tensile ==="
      echo "  bash build_tmp/Tensile.sh $YAML $TENSILE_OUT"
      echo "=== [$CONFIG_NAME] Step 2: Dump .o, copy .s/.o → $OUT_DIR/<full_kernel_name>/, compare, report ==="
      if [[ -n "${DATA_ROOT:-}" ]]; then
        python3 run_tensile_collect_all.py "${yaml_flags[@]}" --config-name "$CONFIG_NAME" --run-tensile --data-root "$DATA_ROOT" --allow-single-cost-file
      else
        python3 run_tensile_collect_all.py "${yaml_flags[@]}" --config-name "$CONFIG_NAME" --run-tensile --tensile-output-dir "$TENSILE_OUT" --output-dir "$OUT_DIR" --cost-dir "$COST_DIR" --allow-single-cost-file
      fi
    fi
  fi
}

# Determine mode and config list
MODE=""
if [[ "${EXTRA_ARGS[0]:-}" == "--from-existing" ]]; then
  MODE="from-existing"
elif [[ "${EXTRA_ARGS[0]:-}" == "--collect-only" ]]; then
  MODE="collect-only"
fi

if [[ "$RUN_ALL_SPARSE_GFX1250" == true ]]; then
  CONFIGS=()
  while IFS= read -r c; do CONFIGS+=("$c"); done < <(get_all_sparse_gfx1250_configs)
  if [[ ${#CONFIGS[@]} -eq 0 ]]; then
    echo "No *.yaml found under $SPARSE_GFX1250_DIR" >&2
    exit 1
  fi
  echo "Running ${#CONFIGS[@]} sparse gfx1250 config(s) from $SPARSE_GFX1250_DIR: ${CONFIGS[*]}"
  for CONFIG_NAME in "${CONFIGS[@]}"; do
    run_one_config "$CONFIG_NAME" "" "$SPARSE_GFX1250_DIR/${CONFIG_NAME}.yaml"
  done
elif [[ "$RUN_ALL_GRADIENT_GFX1250" == true ]]; then
  CONFIGS=()
  while IFS= read -r c; do CONFIGS+=("$c"); done < <(get_all_gradient_gfx1250_configs)
  if [[ ${#CONFIGS[@]} -eq 0 ]]; then
    echo "No *.yaml found under $GRADIENT_GFX1250_DIR" >&2
    exit 1
  fi
  echo "Running ${#CONFIGS[@]} gradient gfx1250 config(s) from $GRADIENT_GFX1250_DIR: ${CONFIGS[*]}"
  for CONFIG_NAME in "${CONFIGS[@]}"; do
    run_one_config "$CONFIG_NAME" "" "$GRADIENT_GFX1250_DIR/${CONFIG_NAME}.yaml"
  done
elif [[ "$RUN_ALL_STREAMK_GFX1250" == true ]]; then
  CONFIGS=()
  while IFS= read -r c; do CONFIGS+=("$c"); done < <(get_all_streamk_gfx1250_configs)
  if [[ ${#CONFIGS[@]} -eq 0 ]]; then
    echo "No *.yaml found under $STREAMK_GFX1250_DIR" >&2
    exit 1
  fi
  echo "Running ${#CONFIGS[@]} streamk gfx1250 config(s) from $STREAMK_GFX1250_DIR: ${CONFIGS[*]}"
  for CONFIG_NAME in "${CONFIGS[@]}"; do
    run_one_config "$CONFIG_NAME" "" "$STREAMK_GFX1250_DIR/${CONFIG_NAME}.yaml"
  done
elif [[ "$RUN_ALL_GFX1250" == true ]]; then
  CONFIGS=()
  while IFS= read -r c; do CONFIGS+=("$c"); done < <(get_all_gfx1250_configs)
  if [[ ${#CONFIGS[@]} -eq 0 ]]; then
    echo "No *_gfx1250*.yaml found under $GFX12_DIR; using default $DEFAULT_CONFIG" >&2
    CONFIGS=("$DEFAULT_CONFIG")
  fi
  echo "Running for ${#CONFIGS[@]} config(s): ${CONFIGS[*]}"
  for CONFIG_NAME in "${CONFIGS[@]}"; do
    run_one_config "$CONFIG_NAME"
  done
elif [[ -n "$SINGLE_CONFIG" ]]; then
  # Explicit --config <name>: always run full pipeline (Step 1 + Step 2), including re-running Tensile
  run_one_config "$SINGLE_CONFIG" "force"
else
  run_one_config "$DEFAULT_CONFIG"
fi

echo ""
echo "=== Done. Check comparison_output/<config>/ and AGGREGATE_<config>.md ==="
