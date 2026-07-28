# SetMatrixReusePass — PR description

Copy the sections below into your GitHub pull request.

---

## Motivation

On gfx1250, WMMA/MFMA instructions support `matrix_a_reuse` and `matrix_b_reuse` so the hardware can reuse matrix A or B from the previous matrix instruction when operands match. TensileLite kernels generated through the StinkyTofu backend did not set these modifiers reliably in final program order (especially at O0 and after scheduling / VGPR MSB insertion).

This PR adds **`SetMatrixReusePass`** to set reuse bits from consecutive matrix-instruction operand equality in Stinky IR, runs it on the **whole gfx1250 kernel** at **all opt levels**, and wires **TensileLite asm verification** so CI checks that set flags match the next instruction’s operands without failing on optional missing flags.

**Goals:**

- Emit correct `matrix_a_reuse` / `matrix_b_reuse` in generated `.s` for gfx1250 Stinky paths (including `_StinkyTofuOptLevel: 0`).
- Align reuse gating with rocisa (skip non-scale `*_f8f6f4` WMMA and MX `*_f4`; allow scaled `*_f8f6f4` WMMA).
- Keep `CheckWMMAReuse` useful in tox/benchmarks without false FATALs when IR register identity differs from printed asm text.

## Technical Details

### StinkyTofu: `SetMatrixReusePass`

- **New pass:** `shared/stinkytofu/src/transforms/asm/SetMatrixReusePass.cpp`, `include/stinkytofu/transforms/asm/SetMatrixReusePass.hpp`
- Walks the kernel `Function` in BB/insn order; tracks the previous matrix insn (MFMA / SMFMA / WMMA / SWMMA / MXWMMA).
- Clears stale `MFMAModifiers::reuseA` / `reuseB`, then sets on the **previous** insn when comparing `src0`/`src1` (`EncodeField`) via `StinkyRegister::operator==`.
- Non-matrix insns do not reset the chain (reuse is between consecutive matrix insns in final order).
- **`supportsMatrixReuse()`** mirrors rocisa: no reuse for non-scale WMMA with `_f8f6f4`; no reuse for MX WMMA ending in `_f4`.
- Emitted by `StinkyAsmEmitter` as `matrix_a_reuse` / `matrix_b_reuse`.

### Gfx1250 pipeline

- **`Gfx1250Backend.cpp`:** `createSetMatrixReusePass()` on the **kernel** pass manager after `EstimateAsmCyclesPass` and after `InsertVgprMsbPass` (and after region scheduler at O1+).
- Runs at **O0 and O1+** (not only inside `addGfx1250RegionPasses`).
- Registered in **`stinkytofu-opt`** for isolated testing.

### TensileLite: asm verification (relaxed)

- **`Tensile/verify_wmma_reuse.py`**
  - **Error:** `matrix_a_reuse` / `matrix_b_reuse` set but next insn `<a>` / `<b>` does not match.
  - **Warning:** operands repeat in asm text but flag not set; flag on last WMMA with no successor.
- **`Tensile/TensileCreateLibrary/Run.py`:** log warnings; `printExit` only on errors.
- **`GlobalParameters.py`:** `CheckWMMAReuse` comment updated (gfx1250, default `True`; tox enables it).

### Key files

| Area | Path |
|------|------|
| Pass | `shared/stinkytofu/src/transforms/asm/SetMatrixReusePass.cpp` |
| Pipeline | `shared/stinkytofu/src/pipeline/backend/Gfx1250Backend.cpp` |
| FileCheck tests | `shared/stinkytofu/tests/filecheck/set_matrix_reuse.stir` |
| Verifier | `projects/hipblaslt/tensilelite/Tensile/verify_wmma_reuse.py` |
| CLI | `projects/hipblaslt/tensilelite/scripts/check_wmma_reuse.py` |

## Test Plan

- [ ] Rebuild StinkyTofu and run FileCheck test:
  ```bash
  cd shared/stinkytofu/build && cmake --build . --target stinkytofu-opt stinkytofu-check -j
  ctest -R FileCheck.set_matrix_reuse -V
  ```
- [ ] Run TensileLite Python unit tests:
  ```bash
  cd projects/hipblaslt/tensilelite
  python3 -m pytest Tensile/Tests/unit/test_verify_wmma_reuse.py \
                   Tensile/Tests/unit/test_run_check_wmma_reuse.py -q
  PYTHONPATH=. python3 scripts/check_wmma_reuse.py --self-test
  ```
- [ ] Optional: `stinkytofu-opt` with `SetMatrixReusePass` on a small WMMA IR snippet.
- [ ] Regenerate a gfx1250 kernel (e.g. benchmark or `sk_hgemm_quick`) and confirm `.s` contains `matrix_*_reuse` where expected.
- [ ] Run with `CheckWMMAReuse=True` and confirm no FATAL on previously failing kernels; review WARN lines for missing flags.
- [ ] Optional: tox gfx1250 common tests with `CheckWMMAReuse=True` (as in `tox.ini`).

## Test Result

| Test | Command | Result |
|------|---------|--------|
| `FileCheck.set_matrix_reuse` | `ctest -R FileCheck.set_matrix_reuse -V` | _pending_ |
| `test_verify_wmma_reuse.py` | `pytest Tensile/Tests/unit/test_verify_wmma_reuse.py` | _pending_ |
| `test_run_check_wmma_reuse.py` | `pytest Tensile/Tests/unit/test_run_check_wmma_reuse.py` | _pending_ |
| `check_wmma_reuse.py --self-test` | `PYTHONPATH=. python3 scripts/check_wmma_reuse.py --self-test` | _pending_ |
| gfx1250 kernel + `CheckWMMAReuse` | _kernel name_ | _pending_ |

## Submission Checklist

- [ ] Look over the contributing guidelines at https://github.com/ROCm/ROCm/blob/develop/CONTRIBUTING.md#pull-requests.

---

## Appendix: verification rules

| Case | Severity | Build fails? |
|------|----------|--------------|
| `matrix_a_reuse` set, next `<a>` differs | Error | Yes |
| `matrix_b_reuse` set, next `<b>` differs | Error | Yes |
| Operands match in asm, flag missing | Warning | No |
| Flag on last WMMA, no successor | Warning | No |

**Manual asm check:**

```bash
cd projects/hipblaslt/tensilelite
PYTHONPATH=. python3 scripts/check_wmma_reuse.py -v path/to/kernel.s
```

**Benchmark run (example):**

```bash
./run_tensile_collect_all.sh --config sk_hgemm_quick \
  --config-yaml Tensile/Tests/common/streamk/gfx1250/sk_hgemm_quick.yaml
```
