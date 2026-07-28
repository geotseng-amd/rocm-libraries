# Register allocation in StinkyTofu

Design document for automatic register allocation (RA). **Status: proposal** — not implemented.

Related: [Architecture overview](architecture.md), [Virtual registers (user)](../user/virtual-registers.md), [Adding intrinsics](adding-intrinsics.md).

---

## 1. Summary

| Topic | Decision |
|-------|----------|
| **Problem** | StinkyTofu Asm IR is mostly **physical** (non-SSA). Virtual temps require manual `resolveVirtualToPhysical()` or upstream Tensile `RegisterPool`. |
| **Goal** | Optional RA pass: map `StinkyRegister` virtual operands (`kVirtualBit`) to physical VGPR/SGPR indices under arch limits. |
| **LLVM alignment** | Follow LLVM **Machine IR** RA shape: live intervals → VirtRegMap → rewrite; start with **Fast** (linear scan), not Greedy+spill. |
| **Default** | **Off** for production GEMM (Tensile still owns layout). **On** for intrinsics, logical IR, experiments. |
| **M1** | `RegAllocConfig`, `LiveIntervalAnalysis`, reserved physical set. |
| **M2** | `RegisterAllocationPass` + `LinearScanAllocator` + verifier hook. |

---

## 2. Current state (no RA today)

### 2.1 Where registers come from

| Source | Mechanism |
|--------|-----------|
| **Tensile / rocisa** | `RegisterPool::checkOut`, `checkOutAligned`, `addSgprVarToPool`, `allocTmpGpr` in `KernelWriter.py` / `KernelWriterAssembly.py` |
| **Conversion** | `ToStinkyTofuUtils.cpp::toStinkyRegister()` copies physical `regIdx` + optional symbolic name |
| **Asm parse** | `RawAsmParser.cpp` — indices from text |
| **Virtual templates** | `StinkyRegister::Virtual()` + manual `resolveVirtualToPhysical(offset)` |
| **Intrinsics** | Caller must bind every `temp` in `arguments { }` before `IntrinsicExpansionPass` |

`PassManager` notes: the module uses **physical registers** and is **not SSA** (`include/stinkytofu/core/PassManager.hpp`).

### 2.2 What StinkyTofu passes do *not* do

- No graph coloring / linear scan / spill lowering in the pipeline today.
- `ScheduleFirstLRsPass` / `ScheduleLastLRsPass` = **local-read scheduling**, not RA.
- `InsertVgprMsbPass` = encoding for VGPR &gt; 255, not allocation.
- `SwInstructionPrefetchRelStaticPass` emits `s_prefetch_inst_pc_rel 0, null, 31`; enable via Tensile **`SwInstructionPrefetch`** / **`EnableSwInstructionPrefetchRelStatic`** (no SGPR reserved).
- Signature metadata reports `.vgpr_spill_count: 0` / `.sgpr_spill_count: 0` (`StinkySignature.cpp`).

### 2.3 Existing analysis to reuse

| Component | Location | Use for RA |
|-----------|----------|------------|
| **CFG** | `CFGBuilderPass` | Block order, predecessors |
| **Def-use + pseudo-PHI** | `BuildDefUseChain.cpp` | Reaching defs, join points |
| **RegKey (per DWORD)** | `RegisterKey.hpp` | Liveness at sub-register granularity |
| **Dominance / RPO** | `DominanceAnalysis` | Global program order |

Pseudo-PHI nodes (`GFX::PHI`) are inserted for analysis only and are **not emitted** ([architecture.md](architecture.md)).

---

## 3. How LLVM does register allocation

LLVM allocates **after instruction selection**, on **Machine IR** (`MachineInstr` with virtual register operands).

### 3.1 High-level pipeline

```
LLVM IR (SSA)
    → Instruction selection
    → Machine IR (vregs + RegisterClass)
    → (optional) coalescing, two-address fixes
    → LiveIntervals / LiveRangeCalc
    → Register allocator (Greedy [default] or Fast)
    → Spill / split (Greedy) → VirtRegMap
    → Rewrite operands to phys reg or stack slot
    → Asm printer
```

References:

- [Code Generator — Register allocation](https://llvm.org/docs/CodeGenerator.html#register-allocation)
- Greedy allocator: `llvm/lib/CodeGen/RegAllocGreedy.cpp`
- Fast allocator: `llvm/lib/CodeGen/RegAllocFast.cpp`
- Live intervals: `llvm/include/llvm/CodeGen/LiveIntervals.h`

### 3.2 Core LLVM concepts

| LLVM | Meaning |
|------|---------|
| **Virtual register** | Operand reference until RA; unlimited in principle. |
| **Register class** | e.g. `VGPR32`, `SGPR32`; allocator only uses regs from the class. |
| **SlotIndex** | Global timeline on the machine instruction stream (supports mid-block inserts). |
| **Live interval** | `[start, end)` per vreg (or register unit) in slot index space. |
| **Interference** | Overlapping live intervals in the same class cannot share a phys reg. |
| **VirtRegMap** | Result: vreg → physical reg or spill slot (`FrameIndex`). |
| **Greedy RA** | Linear-scan style + **live range splitting** + **spilling** + reassignment. |
| **Fast RA** | Simpler linear scan; used for `-O0` / fast builds. |

### 3.3 AMDGPU in LLVM

The AMDGPU backend uses the same RA framework with multiple register classes (VGPR, SGPR, AGPR, …) and target-specific constraints. StinkyTofu’s **`InsertVgprMsbPass`** is **not** RA; it is post-physical encoding legalization (analogous to subregister / mode setup), and must run **after** physical indices are known.

---

## 4. LLVM → StinkyTofu mapping

| LLVM | StinkyTofu (proposed) | Milestone |
|------|------------------------|-----------|
| Machine vreg | `StinkyRegister` + `kVirtualBit` | M2 |
| `RegisterClass` | **`RegBank`** (`VGPR`, `SGPR`) | M1 |
| `SlotIndex` | **`ProgramPoint`** (BB + inst index); SlotIndex later if RA inserts code | M1 / M3 |
| `LiveIntervals` | **`LiveIntervalAnalysis`** | M1 |
| `LiveRangeCalc` | Extend intervals via def-use + pseudo-PHI | M1 |
| `VirtRegMap` | **`VirtualRegMap`** | M2 |
| `RegAllocFast` | **`LinearScanAllocator`** | M2 |
| `RegAllocGreedy` | Greedy + split + spill | M5+ |
| `RegisterCoalescer` | Copy coalescing before RA | M4 |
| Reserved / ABI phys regs | **`RegAllocConfig::reserved`** + scan all physical operands | M1 |
| Inline spiller | Scratch SGPR/VGPR, then `scratch_*` ops | M3+ |

---

## 5. Design principles

1. **Opt-in** — `EnableRegisterAllocation` default `false`; Tensile GEMM path unchanged until explicitly enabled.
2. **Physical-after-RA** — Scheduler, waitcnt, VGPR MSB, emitter, and width verifier run only on physical regs (when RA is on).
3. **Reserved registers are sacred** — ABI, preload, module scratch, and every already-physical operand are **pre-colored** (occupied).
4. **Reuse def-use** — Extend `buildUseDefChain()`, do not build a second SSA world.
5. **Fail loud (M2)** — On pressure, return `OutOfRegisters` with virtual reg id and interval; do not emit `kVirtualBit` indices.

---

## 6. Proposed pass pipeline

### 6.1 Gfx1250 backend (when RA enabled)

```
CFGBuilderPass                         (existing)
BuildUseDefChainPass                   (existing)
RegisterAllocationPass                 (NEW — M2)
  └─ internally: LiveIntervalAnalysis (M1)
StinkyBuildImplicitDependencyPass      (existing)
StinkyDAGSchedulerPass                 (existing)
StinkyWaitCntInsertionPass             (optional, existing)
...
InsertVgprMsbPass                      (existing — after physical indices)
MemTokenConsistencyCheckPass
...
```

### 6.2 Logical IR path (primary M2 consumer)

```
IntrinsicExpansionPass                 (may use virtual temps in patterns)
RegisterAllocationPass                 (NEW)
ToStinkyAsmPass
Backend::runOptimization()             (RA off if already all-physical)
```

### 6.3 Thin prepare pass (optional, can fold into M2)

**`RegAllocPreparePass`**: ensure CFG, `buildUseDefChain`, collect `VirtualRegId` set and occupied physical `RegKeySet` — mirrors LLVM setup in `RegAllocBase`.

---

## 7. Data structures

### 7.1 `RegBank` (LLVM register class lite)

```cpp
enum class RegBank { VGPR, SGPR };  // AccVGPR: later

struct RegBankInfo {
    RegBank  bank;
    unsigned maxCount;      // from GpuArch::RegisterLimits / arch .def
    unsigned defaultAlign;  // 2 if width > 1 else 1
};
```

- `RegType::V` → `VGPR`, `RegType::S` → `SGPR`.
- Skip pseudo regs, literals, EXEC/VCC (same rules as `BuildDefUseChain`).

### 7.2 `VirtualRegId` (allocation unit = register tuple)

LLVM allocates per register unit for some targets; StinkyTofu M2 allocates **whole tuples** (width `reg.num`) to match `RegisterPool::checkOut(n)`.

```cpp
struct VirtualRegId {
    RegBank  bank;
    uint32_t vIndex;   // reg.idx & ~kVirtualBit
    uint16_t width;    // reg.num >= 1
};
```

Collect from all instruction dest/src operands where `reg.isVirtualReg()`.

### 7.3 `ProgramPoint` (M1–M2 timeline)

```cpp
struct ProgramPoint {
    BasicBlock* bb;
    unsigned    instIndex;  // StinkyInstruction order within bb
};
```

**Global order:** RPO block order × increasing `instIndex` (same RPO as dominance / def-use).

**Future (LLVM SlotIndex):** if RA inserts spill/load instructions, upgrade to slot indexing so intervals stay valid.

### 7.4 `LiveInterval`

```cpp
struct LiveInterval {
    VirtualRegId vreg;
    ProgramPoint start;       // first def
    ProgramPoint end;         // last use (inclusive in M2)
    unsigned     align;
    float        spillWeight = 1.0f;  // for future Greedy RA
};
```

**M1 computation (LLVM `LiveRangeCalc` simplified):**

1. Seed at virtual **def** program points.
2. Extend to all **uses** via `inst->getUsers()` / backward walk on def-use graph.
3. At **pseudo-PHI**: treat as def at block head; include values from each predecessor edge.
4. Merge DWORD `RegKey` ranges into one interval per `VirtualRegId`.

### 7.5 `RegAllocConfig`

```cpp
enum class RegAllocMode : uint8_t {
    VirtualOnly,   // M2: only operands with kVirtualBit
};

enum class RegAllocStrategy : uint8_t {
    None,    // disabled
    Fast,    // M2: linear scan (LLVM RegAllocFast)
    Greedy,  // future: split + spill
};

struct PhysicalRange {
    RegType  type;
    unsigned startIdx;
    unsigned count;
};

struct RegAllocConfig {
    bool               enabled = false;
    RegAllocMode       mode = RegAllocMode::VirtualOnly;
    RegAllocStrategy   strategy = RegAllocStrategy::Fast;

    unsigned maxVGPR = 256;
    unsigned maxSGPR = 128;

    std::vector<PhysicalRange> reserved;
    std::optional<unsigned>    prefetchScratchSgpr;  // ModuleOptions

    bool clearSymbolicNamesOnRewrite = true;
    bool rebuildUseDefAfterAlloc = true;
};
```

Wire via `PassFeatureConfig::regAlloc` in `Types.hpp` and `ModuleOptions::EnableRegisterAllocation`.

### 7.6 `VirtRegMap` and rewrite (M2)

```cpp
struct PhysicalRegAllocation {
    VirtualRegId vreg;
    unsigned     physStart;  // base physical index, virtual bit cleared
};

// After LinearScanAllocator:
// operand rewrite: VirtualRegId -> physStart + suboffset per DWORD
// or StinkyRegister::resolveVirtualToPhysical(physStart - vIndex)
```

---

## 8. `LinearScanAllocator` (M2 — LLVM Fast RA)

Per **RegBank**, separately:

1. Sort `LiveInterval`s by `start`, then `end`.
2. Maintain **active** list sorted by `end`.
3. For each interval, remove expired actives (`end < start`).
4. Choose smallest `physStart` such that `[physStart, physStart + width)`:
   - does not overlap **occupiedPhysical** (fixed regs + reserved),
   - does not overlap any **active** assigned range,
   - satisfies `physStart % align == 0`,
   - satisfies `physStart + width <= maxCount`.
5. On failure → `RegAllocStatus::OutOfRegisters` (+ diagnostic).

**Not in M2:** splitting, spilling, coalescing, rematerialization.

---

## 9. `RegisterAllocationPass` (M2)

```cpp
enum class RegAllocStatus {
    Success,
    OutOfRegisters,
    NoVirtualRegs,
    InvalidCFG,
};

// createRegisterAllocationPass()
```

**Steps:**

1. If `!config.enabled` → no-op.
2. `computeDominanceInfo` + `buildUseDefChain(func, domInfo, true)`.
3. `LiveIntervalAnalysis::run` → intervals + `RegKeySet occupiedPhysical`.
4. `LinearScanAllocator::allocate` per bank.
5. Rewrite all virtual operands; clear `kVirtualBit`; optionally clear symbolic names.
6. If `rebuildUseDefAfterAlloc` → `buildUseDefChain` again.
7. Debug: dump interval/assignment table when `DebugLevel` / snapshot enabled.

**Verifier (`AsmVerifierPass`):** after RA, any surviving `isVirtualReg()` is an error.

---

## 10. Integration with Tensile

| Mode | Allocator | Notes |
|------|-----------|-------|
| **Production GEMM** | Tensile `RegisterPool` | RA **off**; zero behavior change. |
| **Intrinsics / logical IR** | StinkyTofu Fast RA | RA **on**; temps can be virtual in patterns. |
| **Hybrid (future)** | Both | Tensile marks ABI/layout as `reserved`; RA fills only short-lived temps. |

This mirrors LLVM’s model of **fixed** vs **allocatable** registers in one function.

---

## 11. File plan

### New files

| File | Milestone |
|------|-----------|
| `include/stinkytofu/transforms/asm/RegAllocConfig.hpp` | M1 |
| `include/stinkytofu/transforms/asm/VirtualRegId.hpp` | M1 |
| `include/stinkytofu/analysis/asm/LiveInterval.hpp` | M1 |
| `include/stinkytofu/analysis/asm/LiveIntervalAnalysis.hpp` | M1 |
| `src/analysis/asm/LiveIntervalAnalysis.cpp` | M1 |
| `include/stinkytofu/transforms/asm/VirtRegMap.hpp` | M2 |
| `include/stinkytofu/transforms/asm/LinearScanAllocator.hpp` | M2 |
| `src/transforms/asm/LinearScanAllocator.cpp` | M2 |
| `include/stinkytofu/transforms/asm/RegisterAllocationPass.hpp` | M2 |
| `src/transforms/asm/RegisterAllocationPass.cpp` | M2 |
| `tests/unit/asm/LiveIntervalAnalysisTest.cpp` | M1 |
| `tests/unit/asm/RegisterAllocationPassTest.cpp` | M2 |
| `tests/filecheck/regalloc_virtual_mov.s` | M2 |

### Modified files (planned)

| File | Change |
|------|--------|
| `include/stinkytofu/core/Types.hpp` | `PassFeatureConfig::regAlloc` |
| `include/stinkytofu/bindings/python/Module.hpp` | `EnableRegisterAllocation` |
| `src/pipeline/backend/Backend.cpp` | Fill limits + scratch from module |
| `src/pipeline/backend/Gfx1250Backend.cpp` | Insert RA pass when enabled |
| `src/analysis/asm/AsmVerifierPass.cpp` | No virtual regs post-RA |
| `tools/stinkytofu-opt/stinkytofu-opt.hpp` | `--enable-regalloc` |
| `src/CMakeLists.txt`, `tests/CMakeLists.txt` | Sources + tests |

---

## 12. Tests (acceptance)

### M1 — `LiveIntervalAnalysisTest`

| Test | Intent |
|------|--------|
| `SingleBB_DefUse` | One virtual def-use → single interval |
| `TwoVirtual_Overlap` | Overlapping intervals same bank |
| `ReservedPhysical` | Physical `v10` in occupied set |
| `PhiJoin` | Diamond CFG + pseudo-PHI |

### M2 — `RegisterAllocationPassTest` + FileCheck

| Test | Intent |
|------|--------|
| `NoVirtual_NoOp` | All physical → unchanged |
| `SimpleMov` | Virtual → assigned low free index |
| `TupleWidth4` | Contiguous 4 regs, align ≥ 2 |
| `PressureFail` | `OutOfRegisters` when pool full |
| FileCheck `regalloc_virtual_mov.s` | No virtual bit in emitted asm |

**Regression:** `EnableRegisterAllocation=false` → existing FileCheck bit-identical.

---

## 13. Roadmap

| Milestone | Deliverable |
|-----------|-------------|
| **M1** | `RegAllocConfig`, `LiveIntervalAnalysis`, unit tests |
| **M2** | `RegisterAllocationPass` + Fast (linear scan) + opt flag + FileCheck |
| **M3** | `SlotIndex` if RA inserts instructions; scratch spill slots |
| **M4** | Copy coalescing (LLVM `RegisterCoalescer` lite) |
| **M5** | Greedy allocator: split intervals at block boundaries |
| **M6** | Memory spill (`scratch_*`), update signature spill counts |

---

## 14. Known limitations

- **VGPR &gt; 255:** M2 uses flat `[0, maxVGPR)`; large kernels need raised limits + existing `InsertVgprMsbPass`.
- **AccVGPR:** Excluded from general VGPR pool until explicitly modeled.
- **Per-DWORD def-use vs tuple allocate:** All DWORDs of one `VirtualRegId` share one `physStart`.
- **Symbolic names:** Cleared on rewrite by default (stale symbols after reindex).
- **Signature `vgpr_count` / `sgpr_count`:** Not updated by RA in M2; Tensile still owns totals for GEMM.

---

## 15. Open questions

1. Primary consumer: intrinsics only, or eventually sub-regions inside `loopWithPrefetch`?
2. M2 on pressure: hard fail vs reserve a scratch pool for minimal spill?
3. Should Greedy be required before enabling RA on large region scopes?

---

## Appendix A: Comparison with rocisa `RegisterPool`

| | rocisa `RegisterPool` | StinkyTofu RA (proposed) |
|--|----------------------|---------------------------|
| **When** | Kernel generation (Python) | Asm IR pass (C++) |
| **Input** | Checkout requests | Virtual operands in IR |
| **Alignment** | `checkOutAligned` | `LiveInterval::align` |
| **Occupancy** | `setOccupancyLimit` | Future spill weights / limits |
| **Scope** | Full kernel | Opt-in function or region |

Long term, both can coexist: Tensile reserves layout; StinkyTofu RA fills compiler-generated snippets (intrinsics, legalized temps).

---

## Appendix B: References

- StinkyTofu: `include/stinkytofu/ir/asm/StinkyRegister.hpp` (`kVirtualBit`, `resolveVirtualToPhysical`)
- StinkyTofu: `include/stinkytofu/transforms/asm/BuildDefUseChain.hpp`
- StinkyTofu: `include/stinkytofu/ir/asm/RegisterKey.hpp`
- Tensile: `rocisa/rocisa/include/register.hpp` (`RegisterPool`)
- LLVM: [Code Generator](https://llvm.org/docs/CodeGenerator.html#register-allocation)
