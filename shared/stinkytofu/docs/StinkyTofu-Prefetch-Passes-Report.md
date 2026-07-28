---


# StinkyTofu: Assembly instruction-size analysis and software prefetch (Gfx1250)


**Description**

- **Byte total:** One walk sums each instruction’s encoded size (literals, labels, etc.). The CP uses that total to prefetch the right amount of code. Tensile **`CheckASMCodeSize`** (assert assembled **`.text`** vs. the published total) is **not** part of this change; it is handled in a separate PR.
- **Software prefetch:** Optional **`SwInstructionPrefetchRelStaticPass`** (per-solution **`SwInstructionPrefetch`** YAML → **`EnableSwInstructionPrefetchRelStatic`**). CP prefetch is bounded; large kernels can lose early code from the I-cache before use—software prefetch helps keep fetch ahead of execution. Off when YAML is false; no scratch SGPR; no semantic change to math.
- **One size model:** `InstructionSizeCosting` + **`accumulateInstructionSize`** so prefetch insertion and totals stay consistent.
- **Tests / debug:** Unit tests on sizing and prefetch; optional `accumulate_instruction_size_pass_debug.txt` and `sw_prefetch_pass.txt` when an output dir is set.

---

## 1. Purpose (Tensile: `SwInstructionPrefetch`; sizing in Stinky)

| Topic | In one line |
|-------|-------------|
| **Software prefetch** | CP prefetch covers only a finite window. Very large kernels can see entry code evicted from the I-cache before it runs; the pass inserts **`s_prefetch_inst_pc_rel`** (`0, null, 31`) on a global-byte grid so software can keep fetch ahead of execution. Gated by **`EnableSwInstructionPrefetchRelStatic`** (Tensile YAML **`SwInstructionPrefetch`**). No scratch SGPR. |
| **Instruction-size total** | CP needs an accurate **byte count** of the kernel image. The pipeline sums encoded lengths from the Stinky passes and can publish a module total (e.g. **`STINKY_TOTAL_INST_BYTES`** in emitted `.s`). **ELF / Tensile `CheckASMCodeSize` verification is deferred** to another PR. |
| **Order** | If prefetch is on, it runs **before** the accumulate pass so the published total matches **final** asm. |

---

## 2. Passes (conceptual)

| Pass | Mutates IR? | Role |
|------|----------------|------|
| **Accumulate instruction size** | No | Linear walk: bytes per instruction, cycles, label → offset map; optional module **total instruction bytes**. |
| **Sw prefetch insertion** | If enabled | Inserts setup + PC-rel prefetch on a fixed global-byte grid; protects **`s_getpc_b64`** chains; then runs the **same** accumulate walk as above. |

**Enablement:** Gfx1250 backend adds prefetch when **`EnableSwInstructionPrefetchRelStatic`** is set (Tensile **`SwInstructionPrefetch`**); accumulate is always scheduled.

---

## 3. Pipeline order (gfx1250, whole-kernel)

1. Insert VGPR MSB (when required).
2. **Optional** software prefetch.
3. **Always** accumulate instruction sizes (and labels).

---

## 4. Shared implementation

- **`InstructionSizeCosting`** — Per-instruction byte size + literals; gfx12 / **`Gfx1250Formats.def`** alignment.
- **`AsmSetSymbolMap`** — `.set` values for literal sizing.
- **`accumulateInstructionSize`** — Single walk shared by accumulate pass and prefetch pass.

---

## 5. Maintainer map

| Topic | Path |
|-------|------|
| Accumulate + walk | `transforms/asm/AccumulateInstructionSizePass.{hpp,cpp}` |
| SW prefetch | `transforms/asm/SwInstructionPrefetchRelStaticPass.{hpp,cpp}` |
| Size rules | `transforms/asm/InstructionSizeCosting.{hpp,cpp}` |
| `.set` | `ir/asm/AsmSetSymbolMap.{hpp,cpp}` |
| Pass order | `pipeline/backend/Gfx1250Backend.cpp` |
| Module flags | `bindings/python/Module.{hpp,cpp}` |

**Prefetch debugging:** Thresholds follow a global byte grid; **`s_getpc_b64`** windows may **redirect** inserts; proposal debug can disagree with final IR in those cases. **`.stir`:** no per-instruction `sizeInBytes`; sizes come from encoding metadata + `InstructionSizeCosting`.

---

## 6. Unit tests

- `tests/unit/asm/AccumulateInstructionSizePassTest.cpp` — sizing, literals, labels, alignment, representative formats.
- `tests/unit/asm/SwInstructionPrefetchRelStaticPassTest.cpp` — pass API, below-threshold kernel, debug output.

---

## 7. Known TODOs (source)

Cross-BB / last-BB threshold handling; tail append vs next-BB handoff.

**Follow-up (separate PR):** Tensile **`CheckASMCodeSize`** — assert Stinky-published instruction bytes against assembled **`.text`** (only when that path is enabled for Stinky-emitted `.s`).

---

<!-- HackMD: YAML front matter optional in some views. -->
