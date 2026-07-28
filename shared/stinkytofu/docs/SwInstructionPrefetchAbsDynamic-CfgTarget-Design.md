# SwInstructionPrefetchAbsDynamic (CFG-target) — design proposal (Gfx1250)

**Status:** Proposal. New algorithm. **Supersedes** the byte-grid `SwInstructionPrefetchAbsDynamicPass` sketch in
[SwPrefetchAbs-TwoPass-Plan.md](SwPrefetchAbs-TwoPass-Plan.md) §Pass B. **Reuses** the emission mechanism
(entry burst, getpc + 3-SGPR label-base, `s_prefetch_inst`) of the implemented
[`SwInstructionPrefetchAbsStaticPass`](SwInstructionPrefetchAbsInsertionPass-Design.md) verbatim — only the
**target-selection policy** changes.

**One-line idea:** keep the abs **site at kernel entry**, but pick the prefetch **target by the kernel's
control-flow / branch logic** (a specific hot global-write basic block, e.g. `label_GW_B1_GSU1`), **not** by the
fixed grid `P(k) = 32640 + k×4096`. No `MaxAheadBytes` / replacement modeling.

**Related:** [SwInstructionPrefetchAbsInsertionPass-Design.md](SwInstructionPrefetchAbsInsertionPass-Design.md)
(abs static, emission), [SwInstructionPrefetchRelDynamicPass-Design.md](SwInstructionPrefetchRelDynamicPass-Design.md)
(old CFG-gated PC-rel grid), [StinkyTofu-Prefetch-Passes-Report.md](StinkyTofu-Prefetch-Passes-Report.md).

---

## 0. Motivation — why grid prefetch is wrong for large kernels

Worked example: `Cijk_Alik_Bljk_BBS_BH_UserArgs_MT256x256x128_…` (gfx1250).

| Fact                                | Value                                                     | Source                             |
| ----------------------------------- | --------------------------------------------------------- | ---------------------------------- |
| Total instruction bytes             | **306508 B** (~306 KB)                              | `STINKY_TOTAL_INST_BYTES` header |
| CP preload window                   | **32640 B** (`255×128`)                          | `.amdhsa_inst_pref_size 255`     |
| Un-preloaded tail                   | **~273 KB**                                         | total − CP window                 |
| Hot epilogue (`label_GW_B1_GSU1`) | line**33340** … `label_GW_End_2` **55783** | label map                          |

The CP keeps only `[0, 32640)` resident. Everything after — including the entire **B1 (Beta≠0, GSU==1)**
global-write batch, which is ~22k lines deep — is fetched on demand and stalls.

The legacy abs **static** pass is grid-driven: it places one target per `P(k)` (every 4096 B) and bursts them all
at entry (see `SwInstructionPrefetchAbsStaticPass.cpp`, `countN` / `kMaxStaticPrefetchN`). For a 306 KB kernel that
is both (a) capped to the I-cache-resident window and (b) **semantically blind**: it prefetches dead Edge paths,
GSU>1 partial-store paths, and OptNLL fast-path code with equal weight, even though at runtime a single
configuration (e.g. `GSU==1, Beta≠0, NonEdge`) is the one that actually executes.

**This pass instead prefetches the basic block the branch logic says will run.**

---

## 1. Vocabulary (inherited, one delta)

Same **site / target / anchor / burst** terms as
[SwInstructionPrefetchAbsInsertionPass-Design.md §0.3](SwInstructionPrefetchAbsInsertionPass-Design.md). The only
change is how **anchor/target** are chosen:

| Term                       | Static (grid)                             | **This pass (CFG-target)**                                                                    |
| -------------------------- | ----------------------------------------- | --------------------------------------------------------------------------------------------------- |
| **Target**           | instruction owning byte`P(k)`           | **entry label of a hot GW basic block** (e.g. `label_GW_B1_GSU1`) chosen by branch analysis |
| **Anchor selection** | layout walk vs byte grid                  | **CFG walk** over global-write dispatch tree                                                  |
| **Site**             | entry BB                                  | entry BB (**unchanged**)                                                                      |
| **Coverage N**       | `count P(k) < total`, capped to I-cache | `ceil(batchBytes / 4096)`, capped by `AbsDynamicMaxCoverageBytes` (no I-cache/MaxAhead cap)     |

---

## 2. Target-selection policy (the new algorithm)

The global-write epilogue is a 3-level dispatch tree (verified on the worked-example kernel):

```text
toPGR1 guard chain (OptNLL)         ── all of {GSU==1, β==0, α==1, ¬edge, no-tail} ─▶ OptNLL fast body
   │ any guard fails
   ▼
label_GSU_3 / GW entry              ── GSU>1 ─▶ label_GW_B0_MB        (split-K partial store, β-independent)
   │ GSU==1
   ▼
label_GSU_4                         ── β==0 ─▶ label_GW_B0_GSU1
   │ β≠0
   ▼
label_GW_B1_GSU1  ──┬─ ¬edge ─▶ label_GW_B1_FD0_VW4_GSU1_NonEdge   ◀── bulk / hottest
                    ├─ M-edge ─▶ ..._Else
                    └─ N-edge ─▶ ..._Then
```

### 2.1 Candidate set

Collect emitted labels whose name matches the global-write batch prefixes (`label_GW_B0*`, `label_GW_B1*`, and their
`_GSU1` / `_MB` / `_NonEdge` / `_Edge` variants). These names are stable, produced by `KernelWriter`. For each
candidate, read its global layout byte offset from `SwPrefetchRelPhase1Accum::layoutGlobal` (same Phase-1 accumulate
the abs static pass already runs).

**Fleet-wide stability (verified, 2706 kernels).** Scanning **2706** emitted kernels under `comparison_output/`
(~70 problem-type families: BBS/S_B/F6SS/D_B/F8*/F6*/MX*/I8/SPMM/…, all MT/MI/layout combos) shows the GlobalWrite
template is **not byte-uniform** — there are ~8 distinct dispatch-skeleton patterns. What matters for this pass is
which labels are **universal**:

| Dispatch label                 | Present in                                                 |
| ------------------------------ | ---------------------------------------------------------- |
| **`label_GW_B0_GSU1`** | **2706 / 2706 (100%)**                               |
| **`label_GW_B1_GSU1`** | **2706 / 2706 (100%)** ← this pass's default target |
| `label_GW_B0_MB`             | 99%                                                        |
| `label_GSU_4`                | 96.5%                                                      |
| `label_GW_B0_OptNLL_MB`      | 90% (not universal)                                        |

The **GSU1 beta-split** (`GW_B0_GSU1` / `GW_B1_GSU1`) is present in **100%** of kernels — so targeting it is robust
fleet-wide. `OptNLL`, `GSU_4`, `MB`, the M/N `Edge/NonEdge store path check` count (6 / 8 / 12), and the `Beta == 0`
check count (1–3) **vary** by problem type and are **not** guaranteed. **Implication:** candidate collection (§2.1)
and ranking (§2.3) must treat `OptNLL` / `MB` as *optional* (absent in ~10% / ~1% of kernels) and degrade
gracefully; the GSU1 NonEdge/Edge labels are the only ones safe to assume. CLS-looped store bodies appear in ~11%
of the fleet (CompactLoopStore families).

**CLS builds (note).** When `CompactLoopStore=True`, the candidate `label_GW_B*_…_NonEdge` / `_Then` / `_Else`
labels are **dispatch stubs** placed immediately before the actual store body, which is now an `label_CLS_*`
countdown loop (one per beta×edge×impl variant). Targeting the `GW_*` label therefore lands at the entry of the
following `CLS_*` loop body — the desired effect. Coverage is still computed from `layoutGlobal` offsets
(`nextBatchLabel − target`), so it automatically tracks whether the body is unrolled (`GW_*`) or looped (`CLS_*`).

### 2.2 Filter

Drop candidates with `offset ≤ P(0) = 32640` (CP already covers them).

### 2.3 Rank (branch-semantics hotness)

Order candidates by a hotness key derived from the dispatch tree, **highest first**:

1. **GSU dimension:** `*_GSU1` (single-kernel, common) **>** `*_MB` (GSU>1 split-K partials — runs only in split-K, and is a β-independent throwaway store reduced later).
2. **Beta dimension:** select per the target use case. Default: **B1** (`Beta≠0`) — the accumulate/`D = αAB + βC` case that dominates inference. Configurable to B0 (`Beta==0`) for pure-GEMM workloads.
3. **Edge dimension:** `NonEdge` **>** `Edge` (`_Then` / `_Else`) — NonEdge is the full-tile bulk; edges are boundary remainders.
4. **Depth tiebreak:** larger layout offset wins (deeper = more likely evicted from CP/I-cache, more to gain).

The winner for the worked example is **`label_GW_B1_FD0_VW4_GSU1_NonEdge`** (line 33360), with
`label_GW_B1_GSU1` (33340) as the batch entry. Either is an acceptable target; the batch entry is preferred so the
β-compare/setpc prologue lines are also warmed.

### 2.4 Coverage

Cover `[target, min(nextBatchLabelOffset, target + AbsDynamicMaxCoverageBytes))`:

```
batchBytes = layoutGlobal[nextBatchLabel] − layoutGlobal[target]
N          = ceil(min(batchBytes, AbsDynamicMaxCoverageBytes) / kSwPrefetchSpacingBytes)   // 4096 B steps
```

`AbsDynamicMaxCoverageBytes` is a **soft** knob only to bound `N` (instruction count), **not** an eviction model.
Default: cover the whole selected batch (`AbsDynamicMaxCoverageBytes = INT64_MAX`). For the worked example the
B1 batch is ~22k lines; in practice set the knob to warm the **leading** portion (e.g. 32–64 KB) that executes first.

### 2.5 Multiple targets (optional)

The policy may emit the top-`M` ranked candidates (default `M = 1`). Each target gets its own getpc+add base
(or shares one base when contiguous and reachable by koffset). Example `M = 2`: warm both `B1_GSU1_NonEdge` and the
edge fall-throughs. Keep `M` small; this pass intentionally does not try to cover the whole kernel.

---

## 3. Site policy (unchanged from abs static)

- **One site, at the very beginning of the entry BB** — reuse `SwInstructionPrefetchAbsStaticPass`'s entry-BB
  insertion (`func.getEntryBlock()`, insert before `entryBB->begin()`).
- Maximum issue latency: the entire prolog + main unroll loop executes between the hint and the epilogue, so the
  SQC has the whole compute phase to fill the lines.
- **No `MaxAheadBytes` / replacement gate** (explicit non-goal, §6). Entry site + deep target is the design intent.

> Optional refinement (future): a second site at `label_GSU_3` (the general-path join) to re-issue closer to the
> branch. Not required for v1.

---

## 4. Emission (reuse abs static verbatim)

Identical 3-SGPR idiom as
[`SwInstructionPrefetchAbsStaticPass.cpp`](../src/transforms/asm/SwInstructionPrefetchAbsStaticPass.cpp) lines
264–317. Only the **label operand** of `s_add_i32` differs.

### 4.1 Variant B (recommended) — reference the existing GW label directly

```asm
; ---- entry BB (site) ----
label_Do_SW_PrefetchAbs_entry:
s_getpc_b64 s[base:base+1]
s_add_i32   s[base+2], label_GW_B1_GSU1, 4     ; <-- existing emitted label, +4 getpc correction
s_add_u32   s[base],   s[base],   s[base+2]
s_addc_u32  s[base+1], s[base+1], 0
s_prefetch_inst s[base:base+1], 0,    null, 0x1f
s_prefetch_inst s[base:base+1], 4096, null, 0x1f
...                                            ; k = 0 .. N-1
; ---- original body follows ----
```

No new label inserted. The pass only needs the target label **name** to exist in the IR (it always does for B1
kernels). Robust to layout shifts because the address is resolved by the assembler's PC-rel relocation.

### 4.2 Variant A (fallback) — insert a dedicated target label

If a stable internal name is preferred, insert `label_SW_PrefetchAbs_0:` immediately before the chosen anchor
instruction (0 bytes, no layout change) and keep the `s_add_i32 …, label_SW_PrefetchAbs_0, 4` form. Use this when the
chosen anchor is mid-block (no existing label) or when emitting multiple synthetic targets.

### 4.3 SGPRs

Same auto-allocated even base pair + scratch (`SwInstructionPrefetchAbsBaseSgpr`) reserved in
`KernelWriter._initKernel` and freed after the entry burst. Net ~0 SGPR pressure. `klength = 0x1f` (31 ⇒ 32 lines ⇒
4096 B/prefetch); `slength = null`.

---

## 5. Pipeline & wiring

```text
SetMatrixReusePass
→ AccumulateInstructionSizePass
→ SwInstructionPrefetchAbsDynamicPass (CFG-target)   ← when EnableSwInstructionPrefetchAbs && total > P(0)=32640
→ AccumulateInstructionSizePass
```

- **Gate = post-CP region, NOT the 64 KiB I-cache split.** CP preload covers only `[0, P(0)=32640)`; this pass
  runs whenever any code lives past `P(0)` (`totalLayoutBytes > 32640`), because the semantic GW targets are chosen
  by branch logic regardless of total kernel size. It no-ops only when the whole kernel fits the CP window. (This
  supersedes the earlier static-vs-dynamic `65536` boundary — that split was about I-cache residency, which the
  CFG-target policy does not model.) **Implementation note:** the shipped code still keys pass *activation* on the
  `65536` regime split (abs-static owns `(32640, 65536]`, this dynamic pass owns `> 65536`, §13.1) so exactly one
  abs pass emits per kernel; the post-CP framing here describes target *selection*, not the activation boundary.
- Same enable knob as abs static, now selected via the unified **`SwInstructionPrefetch`** bitmask (Tensile YAML):
  `-1` Auto, `0` Off, `1` Relative, `2` Absolute. **Absolute (`2`, or Auto on gfx1250 non-Stream-K) → module
  `EnableSwInstructionPrefetchAbs`** (the module option name is unchanged). The legacy separate
  `SwInstructionPrefetchAbs` boolean has been removed; the front-end resolver
  (`Tensile/Common/ValidParameters.py::resolveSwInstructionPrefetch`) maps the bitmask to the
  `EnableSwInstructionPrefetch{RelStatic,Abs}` module options the passes read. Legacy YAML booleans are still
  accepted as a deprecated alias (`true`→Relative, `false`→Off). The backend registers a **stub**
  `SwInstructionPrefetchAbsDynamicPass`; D0 replaces its `run()` body with the CFG-target detector (debug dump,
  no IR mutation), D1 adds the Variant-1 emission. **Resolved (D1, §13.1 regime split):** abs-**static** owns
  `(32640, 65536]` and this CFG-target pass owns `> 65536`; they are mutually exclusive (strict `>` on 65536),
  so exactly one emits per kernel.
- Mutually exclusive with PC-rel (abs wins via the existing `else if`).
- Phase-1 layout (`computeSwPrefetchRelPhase1Accum`) is reused read-only to get `layoutGlobal` for ranking; run it
  once before insertion and once after (re-accumulate) exactly like the static pass.

New module/YAML knobs:

| Knob                             | Layer       | Default     | Meaning                                            |
| -------------------------------- | ----------- | ----------- | -------------------------------------------------- |
| `AbsDynamicTargetBeta`         | YAML        | `B1`      | which beta batch to favor (`B0` / `B1`)        |
| `AbsDynamicMaxCoverageBytes`   | YAML/Module | whole batch | soft cap on`N` (instruction count), not eviction |
| `AbsDynamicNumTargets` (`M`) | YAML/Module | 1           | top-M ranked batches to warm                       |

---

## 6. Non-goals (explicit)

- **No `MaxAheadBytes` / I-cache replacement modeling.** Targets are chosen by branch hotness, not residency.
- **No byte-grid `P(k)` target placement.** Grid is only used as the 4096 B *coverage step* within a chosen batch.
- **No CFG-gated per-BB sweep** like the PC-rel dynamic pass (`insertSwPrefetchLabelsDynamicPerBbAnchor`). This pass
  picks a small set of semantic anchors and bursts them once at entry.

---

## 7. Debug output

`<outputDir>/<kernel>/sw_prefetch_abs_dynamic_pass.txt`:

- candidate list with `name, layoutOffset, hotnessKey, selected?`
- chosen target(s), coverage `[target, target+N×4096)`, `N`
- site label, emitted burst, base SGPR
- perf tag: `abs-dyn-cfg`

---

## 8. Worked example (expected output)

| Field                      | Value                                                                                                                                                                                   |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| total                      | 306508 B (> P(0)=32640 ⇒ this pass runs; not gated on 64 KiB)                                                                                                                          |
| CP window                  | `[0, 32640)`                                                                                                                                                                          |
| candidate batches (line #) | `GW_B0_MB`@6269, `GW_B0_GSU1`@17897, `GW_B1_GSU1`@33340, `GW_B1_…_NonEdge`@33360, `…_Then`@37045, `…_Else`@42131 (filter by **byte** offset > P(0); rank by §2.3) |
| ranked#1                   | `label_GW_B1_GSU1` (GSU1 > MB, B1 default, batch entry)                                                                                                                               |
| target                     | `label_GW_B1_GSU1` (Variant B, direct reference)                                                                                                                                      |
| site                       | `label_Do_SW_PrefetchAbs_entry` at entry BB                                                                                                                                           |
| coverage                   | leading`AbsDynamicMaxCoverageBytes` of `[33340 … 55783]`                                                                                                                           |

Emitted at entry: one getpc + `N` × `s_prefetch_inst s[base:base+1], k*4096, null, 0x1f`, base = address of
`label_GW_B1_GSU1`.

---

## 9. Rollout

| Phase | Deliverable                                                                                                   |
| ----- | ------------------------------------------------------------------------------------------------------------- |
| D0    | Candidate collection + Phase-1`layoutGlobal` ranking (debug dump only, no IR mutation)                      |
| D1    | Variant B emission (single target, entry site), wire into`Gfx1250Backend` (post-CP gate: `total > 32640`) |
| D2    | `AbsDynamicMaxCoverageBytes` + multi-target (`M`) + `AbsDynamicTargetBeta`                              |
| D3    | Numeric validation (no math change) + perf A/B vs grid static +`none`                                       |

---

## 10. Condition-predicated prefetch (runtime target selection) — PROPOSAL

**Status:** Proposal only. No code yet. Extends §2 from a *static* (compile-time) target pick to a *runtime*
pick: replicate the kernel's own GSU/beta dispatch conditions at entry and prefetch **the block that will actually
execute**, instead of always betting on `B1`.

### 10.1 Why predicate at all

§2 picks one target at compile time (default `B1`). But the runtime epilogue is data-dependent on **GSU** and
**Beta** (kernel-arg scalars). If we mis-bet (e.g. the launch is `GSU>1`, or `Beta==0`), the prefetched lines are
dead and the block that *does* run is cold. Since both selectors are cheap scalar SGPRs already resident near entry,
we can compute the real branch outcome up front and prefetch the correct block — turning a static guess into an
exact hit.

The image-confirmed fleet statistics motivate the candidate set:

| Block pair                                        | Provenance                  | Present in fleet                                                                                       |
| ------------------------------------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------ |
| `GW_B0_GSU1` / `GW_B1_GSU1` (GSU1 beta-split) | built into every store path | **100 %** (2755/2755 kernels)                                                                    |
| `GW_B0_{MB,MBSK}` + `GSU_4`/`GSU_3`         | GSU>1 branching arm         | **MB 99.1 %**, MBSK covers the rest ⇒ Case A present ~100 %; true `noGSUBranch` = 0 in sample |

So the **GSU1 beta-split** target is universal — the predicated scheme is well-defined on the entire fleet, with
the `MB` arm present in the common (but not universal) GSU-branching case.

### 10.2 The three runtime cases (measured on the worked-example kernel)

| Case        | Runtime condition                    | Target label         | Block start (B) | Block size (B)                |
| ----------- | ------------------------------------ | -------------------- | --------------- | ----------------------------- |
| **A** | `(GSU & 0x3fff) > 1`               | `label_GW_B0_MB`   | 32924           | 58596                         |
| **B** | `(GSU & 0x3fff) == 1 && Beta == 0` | `label_GW_B0_GSU1` | 91556           | 81944                         |
| **C** | `(GSU & 0x3fff) == 1 && Beta != 0` | `label_GW_B1_GSU1` | 173500          | 133004 (NonEdge bulk = 21304) |

All three targets sit **past** the CP window (32640 B); case C is the deepest (173500 B in) and the hottest for
`D = αAB + βC` accumulation.

### 10.3 Algorithm — Part 1: detect the selector conditions (offline, in the pass)

Recognize the dispatch idioms by structural match over the IR (names are stable `KernelWriter` output):

1. **GSU restore** (prolog): `s_and_b32 s[sgprGSU], <preloadSrc>, 0xffff` → records that `s[sgprGSU]` is live from
   this point. The *site* must be inserted **after** this instruction (liveness gate).
2. **GSU==1 test** (at `label_GSU_3` / GW entry and `label_toPGR1`):
   `s_and_b32 t, s[sgprGSU], 0x3fff ; s_cmp_eq_u32 t, 1`. Stable fleet-wide (mask `0x3fff`, pivot `1`,
   `// branch if GSU == 1`). Extracts: source SGPR = `sgprGSU`, mask `0x3fff`.
3. **Beta==0 test** — anchor by **branch target, not by a `,0` literal.** Fleet survey (2755 kernels) shows the
   literal `s_cmp_eq_u32 s[sgprBeta], 0` occurs **0 times**; the real forms are register-vs-register
   (`s_cmp_eq_u32 s[sgprBeta], sR` where `sR` was just set to 0; f64/1024_vgpr even use non-`sgprBeta` regs). The
   test also sits at **`label_GSU_4` *or* `label_GSU_3`** (96 mxf8 kernels have no `GSU_4` and split at `GSU_3`).
   **Robust matcher:** find the compare whose taken branch / `s_setpc` targets `label_GW_B1_GSU1` (optionally
   confirmed by the `// Beta == 0` comment); its non-pivot SGPR operand is the beta selector.

**Fleet-generality guards (peer review, 2755-kernel sample):**

- `label_GW_B0_GSU1` + `label_GW_B1_GSU1` are present in **100 %** of kernels and **100 %** past the CP window once
  size-gated (644/644) — Cases B/C are rock-solid.
- The GSU>1 arm is matched by **prefix family** `label_GW_B0_{MB,MBSK}` (+ `…OptNLL_{MB,MBSK}`). **`MBSK` is the
  `GlobalSplitUAlgorithm == MultipleBufferSingleKernel` reduction arm — NOT Stream-K** (corrected by generator
  review: `KernelWriterAssembly.globalWriteElements` line ~15517 selects the mode string from
  `GlobalSplitUAlgorithm`; true Stream-K (`StreamK>0`) actually forces `["MB"]`). 24 fleet kernels emit `MBSK`
  instead of `MB`; they still HAVE a Case A arm, so exact-match on `label_GW_B0_MB` alone would wrongly declare it
  absent — match the `{MB,MBSK}` family.
- **Zero genuine `noGSUBranch` kernels exist** in the sample (none lack both `MB` and `MBSK`). The "~1 % MB absent"
  earlier framing is the `MBSK` (MultipleBufferSingleKernel) naming variant, not a missing-Case-A condition.

**Generator-confirmed caveats (verified against `KernelWriterAssembly.globalWriteElements` + helpers).** The §10.3
idioms are config-dependent, so the detector must NOT hard-code them:

- **GSU mask is not always `0x3fff`.** `gsuMaskHex()` returns `0x0FFF` for `KernArgsVersion ≥ 3` (AdaptiveGemmNTAB),
  else `0x3FFF`. Match the `s_and …; s_cmp …,1` structure, not the literal mask.
- **Stream-K uses a different GSU==1 idiom** (`s_cmp_eq_u64 AddressFlags,0` synchronizer + `s_cmp_eq_u32 skTiles,1`),
  not `s_and 0x3fff; s_cmp ,1`. SK kernels need a separate matcher arm.
- **Beta compare form is arch-gated by `HasSCMPK`:** archs *with* it emit the literal `s_cmpk_eq_u32 Beta,0`; archs
  *without* (incl. gfx1250) emit reg-vs-reg. ⇒ the "0 literal compares fleet-wide" stat is a gfx1250 property, not a
  generator invariant. **Anchor on the taken-branch target → `GW_B1_GSU1` (§10.3 step 3); that holds on both.**
- **`GW_B1_GSU1` exists only when `ProblemType.UseBeta`.** No-beta problems emit only `GW_B0_GSU1` ⇒ **Case C target
  absent**; the predicated ladder must drop the β sub-pick and target `GW_B0_GSU1` for such kernels.
- **`AdaptiveGemmGSUA==1` emits BOTH `MB` and `MBSK`** Case-A arms with a runtime synchronizer pick — Case A is not a
  single label there.
- **OptNLL fast body emits `OptNLL_{MB,MBSK,SB}` (not `GSU1`)** when `noGSUBranch && GSU>0` — explains the ~90 %
  `GW_B0_OptNLL_MB` presence; it is inside the CP window here so not a prefetch target anyway.
- **Controlling state** (for the pass to read instead of pattern-matching where possible): `GlobalSplitU`,
  `GlobalSplitUAlgorithm`, `_GlobalAccumulation`, `AdaptiveGemmGSUA`, `StreamK*`, `ProblemType.UseBeta`,
  `InternalSupportParams.KernArgsVersion`.

Output of Part 1: a `SelectorSpec { gsuSgpr, gsuMask, betaSgpr, caseTargets[A,B,C] }` plus each target's layout
offset and block size (from `SwPrefetchRelPhase1Accum::layoutGlobal`, §2). **No IR mutation.**

### 10.4 Algorithm — Part 2: emit predicated prefetch at entry

Two emit variants. **Both compute the same selection**; they differ in correctness-risk vs code size.

#### Variant 1 — branch ladder (RECOMMENDED, provably correct)

Each arm is the **verbatim abs-static burst** (getpc + `s_add_i32 label,4` + `s_add_u32`/`s_addc_u32` +
`N × s_prefetch_inst`) targeting its own label — i.e. the proven adjacency (getpc immediately followed by the
label add) from `SwInstructionPrefetchAbsStaticPass.cpp` is preserved in every arm. Runs once, at entry.

```asm
; ---- entry, AFTER prolog computes s[sgprGSU] (≈ byte 308); s[sgprBeta] is preloaded ----
label_Do_SW_PrefetchAbs_sel:
s_and_b32   sT, s[sgprGSU], 0x3fff
s_cmp_eq_u32 sT, 1                        ; GSU == 1 ?
s_cbranch_scc0 label_Do_PF_caseA          ; GSU>1  -> Case A (MB)
s_cmp_eq_u32 s[sgprBeta], 0               ; Beta == 0 ?
s_cbranch_scc0 label_Do_PF_caseC          ; Beta!=0 -> Case C (B1_GSU1)
; fall-through: Case B (B0_GSU1)
label_Do_PF_caseB:
s_getpc_b64 s[base:base+1]
s_add_i32   s[base+2], label_GW_B0_GSU1, 4
s_add_u32   s[base],   s[base],   s[base+2]
s_addc_u32  s[base+1], s[base+1], 0
s_prefetch_inst s[base:base+1], 0,    null, 0x1f
s_prefetch_inst s[base:base+1], 4096, null, 0x1f
...                                       ; N steps
s_branch label_Do_PF_end
label_Do_PF_caseA:
s_getpc_b64 s[base:base+1]
s_add_i32   s[base+2], label_GW_B0_MB, 4
... (same 3 adds + N prefetch) ...
s_branch label_Do_PF_end
label_Do_PF_caseC:
s_getpc_b64 s[base:base+1]
s_add_i32   s[base+2], label_GW_B1_GSU1, 4
... (same 3 adds + N prefetch) ...
label_Do_PF_end:
; ---- original prolog body continues ----
```

- **Correctness:** each arm reuses the exact getpc→add-label adjacency that is already validated on amdhsa, so no
  new relocation reasoning is required.
- **Cost:** ~3× a single burst (tens of bytes) + 2 compares + 2 branches, executed **once**. Negligible vs 306 KB.
- **SGPRs:** reuses the single reserved 3-SGPR group (`base`, `base+1`, `base+2`) — same as abs static. `sT` can be
  `base+2` (dead before getpc). **Net SGPR cost unchanged** from abs static.

#### Variant 2 — branchless cselect (COMPACT, needs a relocation check before use)

Single getpc, materialize the three PC-rel offsets, `s_cselect_b32` down to one, add once:

```asm
label_Do_SW_PrefetchAbs_sel:
s_getpc_b64 s[base:base+1]
s_add_i32 sMB, label_GW_B0_MB,   4
s_add_i32 sB0, label_GW_B0_GSU1, 4
s_add_i32 sB1, label_GW_B1_GSU1, 4
s_cmp_eq_u32 s[sgprBeta], 0
s_cselect_b32 sB0, sB0, sB1               ; GSU1 sub-pick: beta==0? B0 : B1
s_and_b32 sT, s[sgprGSU], 0x3fff
s_cmp_eq_u32 sT, 1
s_cselect_b32 sMB, sB0, sMB               ; gsu==1? (GSU1 pick) : MB
s_add_u32  s[base],   s[base],   sMB
s_addc_u32 s[base+1], s[base+1], 0
s_prefetch_inst s[base:base+1], 0, null, 0x1f
...
```

- **RESOLVED (peer review, confirmed against `InstructionSizeCosting.cpp` lines 285–353 and
  `tests/filecheck/cfg_long_branch.s`):** the `label` operand is a **per-instruction `FK_PCRel_4`** relocation =
  `label − PC_of_that_add`. The abs-static idiom telescopes to `label` **only because getpc deposits the address of
  the immediately-following add**. With one shared getpc and three contiguous 8-byte adds, only the first base is
  correct; the 2nd/3rd are short by 8 and 16 B. **Fix:** bump each add's literal by `8·k`:

  ```asm
  s_add_i32 sMB, label_GW_B0_MB,   4    ; k=0 : 4 + 8*0
  s_add_i32 sB0, label_GW_B0_GSU1, 12   ; k=1 : 4 + 8*1
  s_add_i32 sB1, label_GW_B1_GSU1, 20   ; k=2 : 4 + 8*2
  ```

  This hard-codes the 8 B add size and back-to-back ordering, so it is **fragile** — must be guarded by an
  `llvm-mc -show-encoding` round-trip test. Prefer Variant 1 unless the entry-burst code-size saving is needed.
- **SGPRs:** needs `base` pair + `{sMB, sB0, sB1, sT}` transiently (≈4 scratch). More than Variant 1; acceptable at
  entry where SGPRs are free, but requires widening the reserved alloc.

### 10.5 Coverage `N` under prediction

**RECOMMENDED (peer review): per-arm `N`, sized to each arm's prologue + NonEdge bulk.** Variant 1 emits a separate
burst per arm anyway, so per-arm `N` is free:

| Arm                | prologue + NonEdge bulk | per-arm`N` | dead-fetch saved vs fixed`N=6` |
| ------------------ | ----------------------- | ------------ | -------------------------------- |
| A (`GW_B0_MB`)   | 64 + 4604  = 4668       | **2**  | −4 hints (~16 KB)               |
| B (`GW_B0_GSU1`) | 112 + 10216 = 10328     | **3**  | −3 hints (~12 KB)               |
| C (`GW_B1_GSU1`) | 64 + 21304 = 21368      | **6**  | — (default arm)                 |

Because exactly one arm runs, per-arm `N` strictly dominates a single fixed `N`: identical coverage for the hot case
C, fewer dead hints for A/B, and no chance of spilling into sibling Edge paths that won't execute.

**No-overrun ceiling:** the smallest *full* block is A (58596 B), so an arm does not cross into the next case's
block until `N ≈ floor((58596−64)/4096) = 14`. Any per-arm `N ≤ 14` is safe; `{2,3,6}` has wide margin.

> Note: a single fixed `N=6` is also *safe* (well under the 14 ceiling) and correctly sized for C, but it
> over-fetches ~16 KB / ~12 KB of A's / B's sibling-edge code per `GSU>1` / `β==0` launch. The earlier
> `min(58596, 81944, 21304)` framing was apples-to-oranges (two full blocks vs one sub-block); the correct readings
> are: hot-bulk fit for C ⇒ 6, and no-overrun ceiling ⇒ 14. Arm-size spread on NonEdge bulk is **~4.6×**
> (21304 / 4604), not 6×.

Case A starts only **284 B past the CP window** (32924 vs 32640), so the CP already warms A's head — A gains least
from prefetch and is the natural candidate to drop entirely if hint budget is tight; C (141 KB past CP) gains most.

### 10.6 Correctness caveats (all variants)

1. **Liveness gate (CORRECTED by peer review — was a correctness blocker):**
   - **GSU is fine:** `s[sgprGSU]` (s54) is written exactly once, on the common path
     (`s_and_b32 s[sgprGSU], s57, 0xffff`, ≈byte 220), and only read thereafter — entry value == dispatch value.
   - **Beta is NOT preloaded on all paths.** `s[sgprBeta]` (s53) is materialized 3 ways by ArgType: only the
     ArgType-0/3 *bypass* path moves it from a preloaded pair before the GSU restore; the **HBM-args** and
     **external-struct** paths `s_load` it from kernarg memory at ≈byte 1552/1596 — *after* a byte-308 site. Reading
     Beta at byte 308 there reads **uninitialized s53** → the B-vs-C sub-pick can mispredict.
   - **Fix:** place the predicated site **after the Beta kernarg load completes** — i.e. after
     `label_LoadExternalStructEnd` **and** its `s_waitcnt kmcnt(0)` (≈byte 1596), not after the GSU restore. This is
     still deep inside the prolog (≪ 32640 CP window) and still leaves the entire main loop as issue-latency lead.
   - Both selectors are persistent once loaded (no second write, no scratch reuse of s53/s54), so a correctly-gated
     early read is value-identical to the late dispatch read.
2. **SCC adjacency:** every `s_cselect` / `s_cbranch` must immediately follow its `s_cmp` (no SCC clobber between).
   The sketches above satisfy this.
3. **`s_prefetch_inst` is a hint:** mispredict (impossible here, since we replicate the exact condition) would only
   waste a hint, never change results. No numeric impact in any case.
4. **Re-accumulate** layout after insertion (as abs static does) so downstream byte costing stays exact.

### 10.7 Case division for review (maps to §10.2)

| Review case | Branch path replicated | Target         | Reviewer focus                                                                 |
| ----------- | ---------------------- | -------------- | ------------------------------------------------------------------------------ |
| A           | `GSU>1`              | `GW_B0_MB`   | selector detect +`MB` may be absent (`noGSUBranch`) → arm must no-op-skip |
| B           | `GSU==1 ∧ β==0`    | `GW_B0_GSU1` | universal target; fall-through arm correctness                                 |
| C           | `GSU==1 ∧ β≠0`    | `GW_B1_GSU1` | deepest block, coverage`N`, NonEdge vs Edge sub-split                        |

### 10.8 Open questions for peer review

1. ~~Variant 2 relocation semantics (§10.4)~~ **RESOLVED:** per-instruction `FK_PCRel_4`; branchless needs `4/12/20` literal fix + round-trip test. **Decision: ship Variant 1 first** (provably correct, no SGPR/alloc change).
2. ~~`noGSUBranch` → collapse to 2-case ladder~~ **REVISED:** the 2-case fallback trigger must be **"`MB` AND `MBSK` both absent"** (exact-match on `MB` alone mis-fires on ~5 gated Stream-K `MBSK` kernels that DO have a Case A arm). True `noGSUBranch` did not occur in the 2755-kernel sample, so this path is currently untested by any real config.
3. ~~Fixed `N=6` vs per-arm `N`~~ **RESOLVED:** use **per-arm `N = {A:2, B:3, C:6}`** (free under Variant 1); no-overrun ceiling is `N≈14`.
4. ~~Replicate compares vs unconditional 3×N~~ **RESOLVED:** keep the **branch ladder**. Unconditional 3×N fetches ~48 KB of provably-dead code and triples SQC traffic on an I-cache-bound kernel; the one-time 2-cmp/2-branch cost is negligible.
5. ~~Entry-site issue-latency~~ **RESOLVED:** confirmed — the full summation loop (`LoopBeginL`@7536 → `LoopEndL`@11396, tail loop ending @32764) precedes all targets; deepest target is ~141 KB past loop exit. Maximal lead.

---

## 11. Fleet fit & required family extensions (peer review)

> Gate note: the survey below used the `> 65536` size gate, which matches the **shipped** dynamic-pass activation
> gate (regime split: abs-static owns `(32640, 65536]`, this dynamic pass owns `> 65536`, §13.1). An earlier draft of
> this design proposed a `post-CP` (`total > 32640`) activation gate; that was not adopted — the CFG-target policy
> governs target *selection*, not the activation boundary. The "size-gated" figures here describe the large-kernel
> (`> 65536`) population the dynamic pass actually fires on; structural fit (B/C anchors, ordering) holds
> independent of the gate.

**Structural fit:** sampling **589 kernels** evenly across all 68 families, the §10 3-case model holds in **100 %**:
B/C anchors present in 589/589, Case A present in every kernel (577 `MB` + 12 Stream-K `MBSK`, **0** genuine
`noGSUBranch`), layout order `A < B0_GSU1 < B1_GSU1` with **B1 deepest in 100 %** (zero "C-not-hottest" outliers).

**CP-window gate (size-gated >65536 B, 160 sampled):** Case **C past CP = 100 %** (the default hot target is always
deep); Case B = 98.8 % (2 `bf16` exceptions); Case **A only 75 %** (38 kernels have `MB` inside the CP window,
heavy in `mxf8`/`*_tdm`). Consistent with §10.5 designating **A as the droppable arm** — not a model failure.

### 11.1 Families that FIT as-is

Plain GEMM (all dtypes), CLS (GW labels are stubs in front of `CLS_*` loops, §2.1), **bias/bgrad** (bias setup is
inline in the batch), **LSU/largeLds** (cross-wave reduce runs pre-dispatch on the common path), **activation**
(target valid; see coverage caveat), **1024_vgpr** (`s_setpc` dispatch already handled by the branch-target matcher).
Grouped-GEMM appears only as `*_UserArgs_*` (identical GW structure); WaveSplitK absent from the fleet.

### 11.2 Families needing design EXTENSIONS (record for D2+)

| Family                                                                                                        | Issue                                                                                                                                                                                    | Extension                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **True Stream-K** (`StreamK>0`; *not in this fleet*, but in generator)                              | hot work is a separate`SK_Fixup`/`writePartials`/`partialsWriteBatch` path, **not** the GW batch; `GW_B1_GSU1` is only the non-partial fallback                            | add a 4th/priority target for the fixup-partials path (`StreamK.py:674/981/1398/1614`); detect via `skPartialsLabel` long-branch                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| **MBSK reduction** (24 `f32`/`fp8` MBSK kernels; **4 are >64 KiB → dynamic pass DOES fire**) | a deep`Reduction_Start … Reduction_End` block (`GSU.py:822 reductionProcedure`, `1168 partialWriteBatch`) precedes `GW_B0_MBSK`; Case-A target lands *after* the hottest code | **Required, NOT moot (correction).** A 2026-06 byte-scan of the full 2755-kernel fleet found **4 `f32` MBSK kernels at 90 312 / 102 712 / 135 792 / 156 800 B** — all `>64 KiB`, so the dynamic pass fires and the Case-A arm (`GSU>1` launches) prefetches the cold code *after* `Reduction_Start…End`. Fix: point Case A at `Reduction_Start` (not `GW_B0_MBSK`) for MBSK `>64 KiB`, or drop the Case-A arm there. Perf-only (`s_prefetch_inst` is a hint; default Case-C unaffected). The earlier "<64 KB so pass wouldn't fire" claim was wrong. |
| **f64 (64-bit beta)**                                                                                   | β compare is`s8 = Beta[0]\|Beta[1]; s_cmp_eq_u32 s8,0` over transient temps — neither operand is `sgprBeta`                                                                         | §10.3 Part-1 must trace the 64-bit`mov+or` reduce back to the `sgprBeta:sgprBeta+1` pair (branch-target detector already finds the block; only operand provenance needs work)                                                                                                                                                                                                                                                                                                                                                                                                   |
| **Activation** (`*_activation`, `*dgelu`)                                                           | activation bodies are a deferred block emitted*after* `GW_B1_GSU1` (`KWA.py:15600`), reached by `s_setpc`; they are the deepest hot code                                         | size coverage`N` to reach into the deferred `label_Activation_*` block, not stop at the NonEdge bulk                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |

---

## 12. D0 / D1 implementation checklist (design spec, not code)

Distilled from §10–§11 + four peer reviews. D0 = detect + debug dump (no IR mutation); D1 = emit Variant 1 +
wire into `Gfx1250Backend`.

### 12.1 D0 — detector (read-only)

**Step 0 — read kernel state (prefer over pattern-matching):** `GlobalSplitU`, `GlobalSplitUAlgorithm`,
`_GlobalAccumulation`, `AdaptiveGemmGSUA`, `StreamK*` (`requiresWorkspaceReductionStorePath`, `StreamKForceDPOnly`),
`ProblemType.UseBeta`, `InternalSupportParams.KernArgsVersion`.

> Footnote (generator-verified): **MBSK-arm presence** additionally requires `not (UseDotInstruction | StreamK | NumElementsPerBatchStore==1 | UseScaleCD | UseE | BiasSrc!='D' | DataType.isDouble)` (`KWA.py:15517-15538`); any of
> these forces single-arm `["MB"]`. `debugConfig.splitGSU` forces `gsuLimit=1`, collapsing the GSU branch
> (`KWA.py:14877`). These only affect *which* Case-A label exists, so the "first of {MBSK,MB}" rule degrades
> gracefully; listed for completeness.

**Step 1 — anchor labels (by name, from `KernelWriterAssembly` `GW_B%u_%s` scheme):**

| Want          | Rule                                                                                            | Guard                                                                                                                                                                                                                                                                                      |
| ------------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Case C target | `label_GW_B1_GSU1`                                                                            | **only if `UseBeta`** — else drop the β sub-pick, Case C ≡ Case B                                                                                                                                                                                                               |
| Case B target | `label_GW_B0_GSU1`                                                                            | universal (2755/2755)                                                                                                                                                                                                                                                                      |
| Case A target | first of`label_GW_B0_{MBSK,MB}` (prefer `MBSK` when both exist via `AdaptiveGemmGSUA==1`) | absent ⇒**only if BOTH `MB` and `MBSK` absent** → 2-case (B/C) ladder. **MBSK + `>64 KiB`:** use `Reduction_Start` (precedes `GW_B0_MBSK`) as the Case-A target, else the arm warms cold post-reduction code (4 fleet `f32` kernels; see §12.2 guard + §11.2). |

**Step 2 — selector detection (structural, NOT literal):**

| Selector        | Match                                                                                                                          | Caveat                                                                                                                                                                                                                |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GSU==1 (non-SK) | `s_and_b32 t, s[sgprGSU], <mask>` + `s_cmp_eq_u32 t, 1`; accept mask ∈ {`0x3fff` (KernArgsVersion<3), `0x0fff` (≥3)} | **Stream-K differs**: `s_cmp_eq_u64 AddressFlags,0` + `s_cmp_eq_u32 skTiles,1` → separate SK matcher (or skip SK in D1)                                                                                    |
| β==0           | the compare whose taken`s_cbranch`/`s_setpc` targets `label_GW_B1_GSU1` (confirm via `// Beta == 0`)                   | reg-vs-reg (no`HasSCMPK`, gfx1250) **or** literal `s_cmpk_eq_u32 Beta,0` (`HasSCMPK`); **64-bit β (f64)**: trace `mov+or` reduce of `Beta[0]\|Beta[1]` back to the `sgprBeta:sgprBeta+1` pair |

**Step 3 — layout + filter:** from `SwPrefetchRelPhase1Accum::layoutGlobal`, record each target's byte offset + block
size (`nextLabelOffset − targetOffset`); **drop any target with offset ≤ P(0)=32640** (CP already covers it — note
Case A is inside CP in ~25 % of size-gated kernels → typically dropped).

**Step 4 — site anchor (FINAL — see §14.2):** insert **after `label_MultiGemmEnd`** (the ArgType-merge join; both
arms branch to it, so insert *after* the label). It is the only must-pass site that is both (a) post-ArgType-merge
(Beta live) and (b) before `defineVariableSgprs` reuses the base triple as ShadowLimit (SGPR +0). `openLoopL`/
`ShadowInitStart` are later → either clobber the triple or need a +3 reservation that overflows MaxSgpr.

**Step 5 — debug dump** `sw_prefetch_abs_dynamic_pass.txt`: `SelectorSpec{gsuSgpr,gsuMask,betaSgpr/pair,caseTargets}`,
per-case `{label, offset, size, N, past_CP?, selected?}`, chosen site, and the no-op reason if any.

### 12.2 D1 — emit (Variant 1 branch ladder)

1. Insert at the Step-4 gated site. Replicate GSU then β compares (SCC-adjacent), `s_cbranch` to per-case arms.
2. Each arm = verbatim abs-static burst (`s_getpc_b64` → `s_add_i32 labelX,4` → `s_add_u32`/`s_addc_u32` →
   `N × s_prefetch_inst sbase, k*4096, null, 0x1f`). **Reuse the reserved 3-SGPR group** (no alloc change).
3. **Per-arm `N`** sized to each arm's prologue+NonEdge bulk; default `{A:2, B:3, C:6}`; never exceed the no-overrun
   ceiling (`N ≤ floor((blockBytes−prologue)/4096)`; ~14 here).
4. **Guards:** `UseBeta`==false → 2-arm (GSU-only) ladder targeting `GW_B0_GSU1`; MB&MBSK absent → 2-case (B/C);
   Stream-K → skip in D1 (D2 fixup target); drop Case A arm when its target is ≤ P(0);
   **MBSK + `>64 KiB` → point Case A at `Reduction_Start` (the hot block *before* `GW_B0_MBSK`), not `GW_B0_MBSK`, or drop the Case-A arm.** Required for the **4 fleet `f32` MBSK kernels** (90 312 / 102 712 / 135 792 / 156 800 B) that exceed the 64 KiB gate and hit the dynamic pass — otherwise the Case-A arm warms cold post-`Reduction_End` code on `GSU>1` launches (perf-only; §11.2/§13.1).
5. **Re-accumulate** layout after insertion (mirror abs static) so byte costing stays exact.
6. Wire into `Gfx1250Backend` `EnableSwInstructionPrefetchAbs` branch. **Shipped activation gate is the `> 65536`
   regime split** (abs-static owns `(32640, 65536]`, this dynamic pass owns `> 65536`, §13.1); the earlier
   `post-CP` (`total > 32640`) proposal was not adopted.

### 12.3 Worked example (the `bbs_pgr2_sia0` MT256x256x128 kernel)

State: `GlobalSplitUAlgorithm=MultipleBuffer` (`GSUAMB`), `UseBeta=1`, `KernArgsVersion=2` (mask `0x3fff`), non-SK.
Detected (byte offsets from `accumulate_instruction_size_pass_debug.txt`, total=306508, CP=32640):

| Case | target               | offset | block size | past CP?               | per-arm N |
| ---- | -------------------- | ------ | ---------- | ---------------------- | --------- |
| A    | `label_GW_B0_MB`   | 32924  | 58632      | yes (+284, droppable)  | 2         |
| B    | `label_GW_B0_GSU1` | 91556  | 81944      | yes                    | 3         |
| C    | `label_GW_B1_GSU1` | 173500 | 133008     | yes (+141 KB, hottest) | 6         |

> Block sizes are **next-case-anchor minus offset** (what the D0 dump prints): A = 91556−32924 = 58632,
> C = 306508−173500 = 133008. These differ slightly from §10.2's earlier figures (58596/133004), which sized
> A to `label_GSU_4` and C to `label_GW_End_2`. Coverage is driven by fixed `N`, not block size, so the
> difference is cosmetic.

Emitted ladder (site after Beta load + `s_waitcnt`):

```asm
label_Do_SW_PrefetchAbs_sel:
s_and_b32   s[base+2], s[sgprGSU], 0x3fff
s_cmp_eq_u32 s[base+2], 1                 ; GSU == 1 ?
s_cbranch_scc0 label_Do_PF_caseA          ; GSU>1  -> A (GW_B0_MB)
s_mov_b32   s[base+2], 0                   ; gfx1250 lacks HasSCMPK -> materialize 0 in a tmp
s_cmp_eq_u32 s[sgprBeta], s[base+2]        ; Beta == 0 ?  (reg-vs-reg, mirrors getSCMPKInstruction)
s_cbranch_scc0 label_Do_PF_caseC          ; Beta!=0 -> C (GW_B1_GSU1)
label_Do_PF_caseB:                        ; Beta==0 -> B (GW_B0_GSU1), N=3
s_getpc_b64 s[base:base+1]
s_add_i32   s[base+2], label_GW_B0_GSU1, 4
s_add_u32   s[base],   s[base],   s[base+2]
s_addc_u32  s[base+1], s[base+1], 0
s_prefetch_inst s[base:base+1], 0,    null, 0x1f
s_prefetch_inst s[base:base+1], 4096, null, 0x1f
s_prefetch_inst s[base:base+1], 8192, null, 0x1f
s_branch label_Do_PF_end
label_Do_PF_caseA:                        ; N=2 (or drop: A only +284 past CP)
s_getpc_b64 s[base:base+1]
s_add_i32   s[base+2], label_GW_B0_MB, 4
s_add_u32   s[base],   s[base],   s[base+2]
s_addc_u32  s[base+1], s[base+1], 0
s_prefetch_inst s[base:base+1], 0,    null, 0x1f
s_prefetch_inst s[base:base+1], 4096, null, 0x1f
s_branch label_Do_PF_end
label_Do_PF_caseC:                        ; N=6 (hot default)
s_getpc_b64 s[base:base+1]
s_add_i32   s[base+2], label_GW_B1_GSU1, 4
s_add_u32   s[base],   s[base],   s[base+2]
s_addc_u32  s[base+1], s[base+1], 0
s_prefetch_inst s[base:base+1], 0,     null, 0x1f
s_prefetch_inst s[base:base+1], 4096,  null, 0x1f
s_prefetch_inst s[base:base+1], 8192,  null, 0x1f
s_prefetch_inst s[base:base+1], 12288, null, 0x1f
s_prefetch_inst s[base:base+1], 16384, null, 0x1f
s_prefetch_inst s[base:base+1], 20480, null, 0x1f
label_Do_PF_end:
; ---- original prolog body continues ----
```

For `α=2, β=1` (β≠0, GSU==1): the ladder takes `label_Do_PF_caseC`, prefetching `[173500, 173500+24576)` — i.e.
the `GW_B1_GSU1` entry + the full 21304 B NonEdge bulk — exactly the block the runtime store will branch to.

### 12.4 Validation status (peer review)

- **D0 detector: 100 % correct on 547 kernels** across all 68 fleet families (zero miss / mis-classify). Anchors A/B/C
  100 % present; GSU idiom 100 % (mask always `0x3fff` in this fleet; `0x0fff` never appears); β branch-target rule
  hit 547/547 (542 direct `s_cbranch`, 5 indirect `s_setpc`, 11 f64 OR-reduce).
- **CP filter (163 size-gated):** Case C past CP **100 %**; Case A inside CP **19.6 %** (mxf8/`*_tdm`/mxf4 — the
  designated droppable arm).
- **Checklist vs generator:** 5/6 claims confirmed verbatim; worked example faithful. Corrections folded
  (secondary MBSK-arm modifiers in Step-0 footnote; fresh-`tmp=0` β compare in §12.3).
- **Untested-here (absent from fleet, not wrong):** the `0x0fff` mask arm (KernArgsVersion≥3), the Stream-K GSU
  matcher, and the `noGSUBranch` 2-case fallback. **D1 follow-up:** f64 β-operand provenance through the `mov+or`
  reduce (§11.2).

---

## 13. Multi-agent validation (2755-kernel fleet + generator + doc) — corrections folded

Three independent readonly reviews (full-fleet data scan, `KernelWriterAssembly` generator audit, doc self-consistency)
re-verified the design. Headline: **the 3-case model fits the fleet and the generator; one factual error and several
guards were corrected.**

### 13.1 Fleet fit — exact counts (all 2755 `.s`, not a sample)

| Property                                           | Result                                                                                                                                                                                                                                                                                                                                                                  |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GW_B0_GSU1` present                             | **2755/2755 (100%)**                                                                                                                                                                                                                                                                                                                                              |
| `GW_B1_GSU1` present                             | **2755/2755 (100%)**                                                                                                                                                                                                                                                                                                                                              |
| Case A present (`MB` **or** `MBSK`)      | **2755/2755** (2731 `MB` + 24 `MBSK`; **0** "neither"). **Of the 24 MBSK, 4 (`f32`) are >64 KiB** (90 312 / 102 712 / 135 792 / 156 800 B) → they **do** hit the dynamic pass; their Case-A arm needs the §11.2 `Reduction_Start` extension (perf-only). The earlier "MBSK all <64 KB ⇒ pass won't fire" framing is **wrong**. |
| Layout`A < B0_GSU1 < B1_GSU1`, B1 deepest        | **2755/2755 (100%)** (independently re-verified 2026-06: 0 order violations across all 2755)                                                                                                                                                                                                                                                                      |
| Case C past CP (size-gated > 65536 B: 644 kernels) | **644/644 (100%)** — **byte-level re-verified 2026-06**: for each of the 644 `>64 KiB` kernels, `label_GW_B1_GSU1`'s `addr=` offset in `accumulate_instruction_size_pass_debug.txt` is `> 32640`; **0** exceptions, **0** missing-debug. (Previously inferred from label line-order; now confirmed on real byte offsets.)            |
| Case A**inside** CP (size-gated)             | **232/644 = 36%** — *update*: higher than the §11/§12.4 ~20–25% figure; concentrated in `mxf8` (114), `bf16` (22), `*_tdm`/`mxf4`                                                                                                                                                                                                                   |
| UseBeta=false (B0_GSU1 w/o B1_GSU1)                | **0/2755** — the Case-C→Case-B drop guard is correct but **unexercised by this fleet**                                                                                                                                                                                                                                                                    |

### 13.2 Generator corrections (vs §10.3 / §12.1)

1. **`MBSK` ≠ Stream-K (FACTUAL FIX, §10.3 patched).** `MBSK` ⇐ `GlobalSplitUAlgorithm == MultipleBufferSingleKernel`
   (`KWA.globalWriteElements` ~15517); true Stream-K forces `["MB"]`. The 24 fleet `MBSK` kernels are MBSK-reduction,
   not Stream-K.
2. **GSU==1 branch is not always `s_cbranch_scc1`.** Under `MultipleBufferSingleKernel` / `AdaptiveGemmGSUA==1` it is a
   `longBranchScc1` macro (KWA ~14898). The selector matcher must accept the long-branch form too.
3. **`HasSCMPK` is an assembler-probed capability, not kernel state** (`hardware_caps.hpp`). So §12.1 Step 0's
   "read state" cannot decide the β-compare form; the detector must accept **both** `s_cmpk_eq_u32 Beta,0` (HasSCMPK)
   **and** `s_mov tmp,0; s_cmp_eq_u32 Beta,tmp` (gfx1250). Anchor on the taken-branch → `GW_B1_GSU1` (already §10.3 step 3).
4. **`gsuLimit=1` is also forced by `debugConfig.splitGSU`** (not only `noGSUBranch`) — already noted in §12.1 footnote.
5. **Architectural note:** this pass is a **stinkytofu IR pass** (post-codegen), so it cannot read Tensile's Python
   `kernel[...]` dict directly — §12.1 Step 0 "read state" applies only if those fields are surfaced via module
   options/attrs; otherwise the pass relies on the §12.1 Step-2 **structural** matchers (which are sufficient).

### 13.3 Doc-consistency fixes to fold into §12 (D1)

- **S1 — Case-A drop rule:** §12.2 Step 4 currently drops Case A only when `offset ≤ P(0)`. Add the §10.5 soft rule:
  **also droppable under hint-budget when its head sits within ~1 grid step of CP** (worked example: A at +284 B).
- **S2 — liveness gate fallback:** §12.1 Step 4 site = "after the Beta kernarg load + `s_waitcnt`" assumes the
  external-struct path; for **ArgType-0/3 bypass** kernels (Beta live before the GSU restore, no
  `label_LoadExternalStructEnd`), the site falls back to "after the GSU restore". State: *site = latest point Beta is
  live across all ArgType paths.*
- **S4 — f64 β provenance is D0 detector work** (trace the `Beta[0]|Beta[1]` `mov+or` reduce to the
  `sgprBeta:sgprBeta+1` pair), not a D1 follow-up — reconcile §12.1 Step 2 ↔ §12.4.
- **N1 — debug filename:** canonical = **`sw_prefetch_abs_dynamic_pass.txt`** (the pass is the abs dynamic pass).
- **Activation (D2):** §11.1 lists `*_activation`/`*dgelu` as "fits", but coverage `N` (§12.2 Step 3) stops at the
  NonEdge bulk while the hottest code is a **deferred `label_Activation_*` block after `GW_B1_GSU1`** — size `N` into it
  in D2 (perf-only; target is still valid).

### 13.4 Net verdict

**D0/D1 (§12) is coding-ready** after folding §13.2.1 (MBSK fix, done), §13.2.2–3 (matcher accepts long-branch + both
β forms), and §13.3 S1/S2/S4/N1. No correctness blockers — `s_prefetch_inst` is a hint, so every residual mismatch is
perf-only. The Case-C (`GW_B1_GSU1`) default target is the safest possible choice: 100% present, 100% deepest, 100%
past CP fleet-wide.

---

## 14. Site selection — selector value sourcing, liveness, and burst placement (multi-agent verified)

Two independent reviews (full-fleet `.s` scan + `KernelWriterAssembly.defineAndResources` generator audit) pinned
**where the GSU/Beta selectors get their values** and **which basic block the predicated burst must go in**.

### 14.1 How the two selectors are materialized (verified)

**`sgprGSU` — one-shot, common path, never the constraint.**

- Unpacked once from a preloaded packed SGPR: `s_and_b32 s[sgprGSU], <packed>, 0xffff` (comment "Restore GSUConfig
  and GSU") — `KernelWriterAssembly.py:2521`, emitted in `moduleRegInit` on the common path **before** the ArgType
  split (`:2778`). **Sole writer** in the whole generator; all later uses are reads masked with `gsuMaskHex` into
  temps. Fleet: **2755/2755** have exactly this single write, and it precedes the first `label_GW_` / the beta
  convergence in **100%**.

**`sgprBeta` — a kernarg, materialized by ArgType; usable only after the kernarg waitcnt.**

- **Not** a single 3-way join. Two regions:
  1. **Bypass `s_mov`** of the preloaded alpha:beta pair (`SMovB64`, `:2499`), gated by `numSgprPreload>0`
     (`PreloadKernArgs`). Separate, earlier; Beta live at common entry on this sub-path. Present in **63.5%** of fleet.
  2. **2-way ArgType join** at `label_LoadExternalStructEnd`: the normal grouped kernarg `s_load` (alpha+beta together)
     vs the **external-struct** path (`ArgType==2`, separate beta `s_load` at a user-args offset). The labels +
     `s_cmp_eq_u32 s[sgprArgType],2` exist **only when `ProblemType.SupportUserArgs`** (`:2927–2976`). Fleet: **100%**
     have `label_LoadExternalStructEnd` (this corpus is all UserArgs); the non-UserArgs/bypass-only case is real in
     the generator but **absent here**.
- The kernarg `s_load`s are **not** waited inline; Beta becomes usable only after the `s_waitcnt kmcnt=0` from
  `waitForArgsToLoad()` (`:2731/2734`), emitted right after the load region. Beta's first real use is in the epilogue
  (β==0 compare `:14458`), far later.

### 14.2 Primary site (FINAL DECISION — multi-agent verified, supersedes the openLoopL pick)

**Anchor the predicated burst immediately AFTER `label_MultiGemmEnd`** (insert before the first instruction following
the label — both ArgType arms branch *to* the label, so the ladder must sit after it to run on every path).

**Why MultiGemmEnd, not openLoopL — two coupled constraints intersect here only:**

- **Value liveness** needs the site *after* the ArgType merge (Beta is only live on all paths past
  `label_MultiGemmEnd`). ✔ at MultiGemmEnd / ShadowInitStart / openLoopL.
- **SGPR safety** needs the site *before* `defineVariableSgprs`, which re-allocates the abs base triple
  `s[base..base+2]` as `ShadowLimitA/B` **immediately after `label_MultiGemmEnd`**. Keeping the triple reserved past
  that point (to ShadowInitStart/openLoopL) shifts the persistent ShadowLimit/global-read block **+3 SGPR → 103 >
  MaxSgpr=102**, overflowing the `sgpr_count=100` kernels. Only `[MultiGemmEnd, defineVariableSgprs)` keeps the
  triple free at **+0 cost**.

So although `label_openLoopL` is the *latest* universally-safe anchor (max latency, 100% present, never a branch
target), the SGPR-reservation overflow rules it out. `label_MultiGemmEnd` (also 100% present, exactly once,
post-dominates the ArgType split, GSU/Beta live) is the **only +0-cost must-pass site**, and the whole main loop
still runs after it ⇒ effectively the same issue-latency lead. `label_ShadowInitStart` is rejected (only 91.1% —
absent in 246 MX-scaled FP8 kernels, generator-gated); `label_NoBranch_<hash>` rejected (random suffix); the kernarg
waitcnt rejected (two copies on exclusive ArgType arms).

**Detection rule:** anchor on `label_MultiGemmEnd`, insert after the label. (Earlier-considered single-join key.)

Constraints (all met by inserting after `label_MultiGemmEnd`):

1. **Common path, runs once** — it is the ArgType-merge join (both arms `s_branch` here), before `label_LoopBeginL`;
   not a back-edge target ⇒ executes once per wavefront.
2. **Both selectors live:** `sgprGSU` (sole write before the ArgType split, dominates everything); `sgprBeta` live on
   every path once the arms converge here.
3. **Issue latency:** the whole K-loop + tail loop run after this point ⇒ deep targets get the full loop as lead.
4. **CP-resident site** (≪ 32640 B) → the burst itself never stalls.
5. **SGPR +0:** the base triple is still reserved here (with the §13 checkIn-defer) and freed before
   `defineVariableSgprs` reclaims it. **Fleet-verified 100%** (1253 observable-base kernels, zero exceptions): the
   first `defineVariableSgprs` allocation right after `MultiGemmEnd` reclaims `s[B..B+2]` — via `ShadowLimitA/B`
   (~34%), `sgprSrdMetadata` (sparse), or TDM/MX group vars, **config-dependent** (not always ShadowLimit; 362
   kernels have none). The reclaim is a net-0 pool checkout/checkin invariant regardless of which var lands there.

**Detection rule for the pass:** anchor on `label_MultiGemmEnd`, **insert after the label** (it's a branch target).
The current code does exactly this (`SwInstructionPrefetchAbsDynamicPass.cpp`).

**Avoid:** before the ArgType convergence (Beta not yet usable on the load arms → B-vs-C mispredict); after
`defineVariableSgprs` / at ShadowInitStart/openLoopL (triple is now ShadowLimit → clobber, or +3 SGPR overflow if
reserved); inside the main/tail loop (re-issues per iteration → I-cache pollution); at the GW-dispatch point itself.

### 14.3 Single site — second/epilogue-join burst REJECTED (multi-agent verified)

A second, C-only re-burst at the epilogue join was considered and **rejected by the eviction/latency analysis**:

- **The "loop-length eviction" premise is mostly false.** The main-loop body is tiny (~3860 B) and **resident** —
  eviction is footprint-driven, not iteration-driven; a long K-loop re-fetches the same resident lines, it does not
  progressively evict C. And the entire pre-epilogue path lives in `[0, ~32764)` ≈ the CP window, so there are
  almost no deep demand fetches to evict C between the burst and the epilogue.
- **The join site is structurally too late.** For case C the dispatch *branches over* the A and B blocks, so only
  **~11 instructions** execute between the epilogue join and landing on `C` — far less than the hundreds of cycles an
  SQC fill needs. A re-burst there cannot hide latency; it is pure added traffic.

**DECISION: one predicated burst at `label_openLoopL`.** Predication means only one arm runs, so the live footprint is
~CP(32.6 KB) + one arm's coverage (C: N=6 ⇒ 24.6 KB) ≈ **57 KB < 64 KiB** — fits with margin (only the *unconditional*
3-arm footprint would self-evict, which predication avoids).

- **Drop Case A** (`GW_B0_MB`, +284 B past CP → CP already warms its head; GSU>1 split-K only; throwaway partial
  store). Reallocate budget to B/C.
- **Per-arm `N`: B=3, C=6.**
- **If (and only if) profiling shows C still fetch-stalls:** do **not** add a join re-burst — instead **move the
  single C burst later** (loop-exit / `label_toPGR1`, ~11–15 KB), shrinking the eviction window while still giving
  thousands of cycles of lead.

---

## 13. D1 implementation status + SGPR-lifetime blocker (peer review)

**D1 code landed (detector live, emission gated OFF).** The Variant-1 ladder emitter `emitVariant1Ladder`
(`SwInstructionPrefetchAbsDynamicPass.cpp`) is implemented and compiles, and the backend invokes the dynamic pass
(D0 detector dump runs for all post-CP kernels). Three peer reviews:

- **IR construction: CORRECT** — `create(desc,anchor)` preserves creation order; getpc→`s_add_i32 label,4`
  adjacency intact per arm (Variant-1 reloc holds); SCC adjacency + polarity correct (GSU≠1→A, β≠0→C, fall→B);
  `tmp`=base+2 reused safely; branch/label modifiers match the cluster-barrier idiom; prefetch operands match
  abs-static. Non-blocking: dead `kSel` label; ladder clobbers SCC at the site (liveness assumption).
- **Regime split: SAFE** — static and dynamic both key off `kSwPrefetchAbsStaticIcacheSizeBytes=65536` with strict
  `>`, so exactly one emits per kernel; no shared-SGPR double-use; dynamic-before-static ordering fine. Caveat:
  large **non-GSU1** kernels (Stream-K/custom) would get zero prefetch once static no-ops `>65536` — close via an
  in-pass static-style fallback burst in D2.
- **SGPR lifetime: UNSAFE → emission gated OFF.** `KernelWriter._initKernel` `checkOutAligned(3,2)`s the abs base
  triple then **immediately `checkIn`s it** ("free after entry, net 0"). So at the body site `label_openLoopL` those
  physical SGPRs hold live values; emitting the ladder there would **clobber them (miscompile)**. The abs-static
  pass is safe only because it bursts at *entry-begin* while the triple is still dead. `.s` confirms `label_openLoopL`
  is a single post-kernarg point (GSU/beta value-liveness fine, runs once, K-loop back-edge targets
  `label_LoopBeginL`) — the defect is purely **base-SGPR liveness**.

**Status — D1 ENABLED (in-tree; pending hardware numeric validation):** `kD1LadderEmissionEnabled = true`; the
dynamic pass emits the predicated ladder after `label_MultiGemmEnd` for `>65536` kernels; static re-enabled its
`>65536` no-op (regime split); and `KernelWriter._initKernel` now **defers the abs-base `checkIn` to
`label_MultiGemmEnd`** (KernelWriterAssembly, before `defineVariableSgprs`) with a `PreLoop`-disabled fallback to the
immediate checkIn. **Required pre-merge gate: a gfx1250 device-lib build + numeric gtest** — the IR construction,
relocation, SGPR window, and site are all multi-agent + fleet verified, but the emitted assembly has not been
hardware-run in this environment.

**Unblock fix (DECIDED — verified by the SGPR reservation-window review):** the base triple is **`s[56:57]+s58`**
and is reused as arg-unpack scratch (~line 331), then **re-allocated as `ShadowLimitA/B` by `defineVariableSgprs`
immediately after `label_MultiGemmEnd`**. So the only +0-cost window is `[MultiGemmEnd, defineVariableSgprs)`.

> **In `KernelWriter._initKernel`, defer the abs-base `checkIn` to `label_MultiGemmEnd`:** drop the immediate
> `self.sgprPool.checkIn(absBaseIdx)`; stash the index on `self.states`; perform the `checkIn` in
> `KernelWriterAssembly` right at `module.add(labelMultiGemmEnd)` (`KWA.py:3012`) — i.e. **immediately before**
> `defineVariableSgprs` (`:3025`). This keeps the triple reserved across the entry→MultiGemmEnd window (where the D1
> ladder is inserted) and frees it before `defineVariableSgprs`'s first `checkOutAligned` reclaims it (lowest-free
> index ⇒ reuses `s[B..B+2]`). **Cost: +0 to the kernel SGPR high-water** — fleet-verified across 2755 kernels;
> deferring to ShadowInitStart/openLoopL is +3 → 103–105, overflowing 67 high-pressure (`sgpr_count≥100`) kernels.
>
> **Two caveats to encode in the fix (generator review):**
>
> 1. `label_MultiGemmEnd` is emitted inside the always-true `self.do["PreLoop"]` debug gate while `defineVariableSgprs`
>    is just outside it. **Guard the deferred `checkIn` on the label actually being emitted** (assert / fall back to
>    the immediate checkIn if `PreLoop` is ever disabled) so the triple can't leak.
> 2. "+0" is a *body*-peak guarantee (reclaim by the next `checkOutAligned`, not ShadowLimit-specific). It assumes the
>    kernel's SGPR high-water is at/after `defineVariableSgprs`, not in the prolog (holding the triple +3 across the
>    prolog). True for every sampled config, but add a `MaxSgpr` sanity assert in the fix.

Then flip `kD1LadderEmissionEnabled = true` **and** re-enable static's `>65536` no-op **together**. (DONE in-tree.)

**Hardware gate — required before merge (static review cannot settle these device-observable risks):**

- [ ] gfx1250 device library **builds (assemble-clean)** for a `>65536`, 3-arm (A+B+C) kernel → proves `s_prefetch_inst`
  encoding, the ladder's intra-BB label uniqueness + branch displacement range (labels/branches injected AFTER
  CFGBuilder/LongBranchLowering), the `FK_PCRel_4` relocation at the far-forward GW target distance, and that
  `sgprBeta`/`sgprGSU` symbols resolve at `label_MultiGemmEnd`.
- [ ] **Numeric correctness** for that kernel across GSU=1/GSU>1 × beta=0/beta≠0 → proves SCC join-point safety at MGE
  and that the ShadowLimit reuse doesn't clobber a live value.
- [ ] `>65536` **Stream-K / non-GSU1** kernel builds + is numerically correct with **no** prefetch emitted (documented
  D2 coverage gap — not a miscompile).
- [ ] `(32640, 65536]` kernel: static still emits, dynamic only dumps the detector (regime exclusivity at runtime).
- [ ] **KernArgsVersion≥3** kernel (mask `0x0fff`), if present: confirm the hard-coded `0x3fff` GSU mask only causes a
  hint-only arm misselect (perf), never a miscompile — or gate/parameterize the mask.

Rejected alternatives: emit at entry-begin (reserved, but GSU/Beta not yet live → arm mispredict); emit at
openLoopL/ShadowInitStart (triple already ShadowLimit → clobber, or +3 reservation → MaxSgpr overflow); give D1 its
own body-reserved triple (more SGPR pressure than the deferred-checkIn, which is net +0).

---

## 15. CP-Range-extend prefetch — unconditional near-boundary cover (static-idiom, N ≤ 2) — PROPOSAL

**Status:** Proposal (multi-agent verified, no code yet). **Additive** to §10–§14: it does **not** change the 3-arm
CFG-target ladder — it prepends **one extra unconditional burst** of **N ≤ 2 (default 2)** `s_prefetch_inst` covering
`N×4096 B` **immediately past the CP boundary** `P(0) = 32640`. Reuses the **abs-static idiom** verbatim (getpc +
`s_add_i32 label,4` + carry adds + `N × s_prefetch_inst`), embedded inside the dynamic pass. **Current decision:
default N = 2, hard cap N ≤ 2** (derived from the concurrent-arm I-cache budget — see §15.7/§15.8).

> Numbering note: this doc already has two `§13`s (line 676 "Multi-agent validation", line 824 "D1 status"). This
> section is the next unused top-level number (§15) and slots logically **after §14** (same site policy).

### 15.1 Gap this closes

For **extreme** kernels (`> 64 KiB`, dynamic regime) the region **just past** `P(0)=32640` is **blind on both
passes**:

- **CP hardware** preloads only `[0, 32640)` (`.amdhsa_inst_pref_size = 255×128`).
- **abs-static** no-ops for `> 65536` (regime split, §5 / `SwInstructionPrefetchAbsStaticPass.cpp:155`), so it does
  **not** place its usual `label_SW_PrefetchAbs_0` boundary cover.
- **abs-dynamic (§10–§14)** deliberately targets **deep** hot GW blocks and **drops any candidate `offset ≤ P(0)`**
  (§2.2, §12.1 Step-3) — so it never warms the near-boundary bytes.

Concretely, the **OptNLL fast-path store** family (`label_GW_B0_OptNLL_MB`) and the continuing tail-loop / epilogue
dispatch that sit just after `32640` are **not** a D1 3-arm target and go blind for large OptNLL kernels.

> **Regime note (corrected — multi-agent + docker verified).** The static/dynamic split is decided **purely by TOTAL
> instruction bytes** and is mutually exclusive (static `(32640, 65536]`, dynamic `> 65536`;
> `SwInstructionPrefetchAbsStaticPass.cpp:155` / `SwInstructionPrefetchAbsDynamicPass.cpp:142`). The figures
> `f8f8s_pgr2_sia0 @ 38196 B` / `f64 @ 110208 B` are the **OptNLL region byte offsets**, *not* the kernel total.
> Built in-container (`geotseng_ffm_para0`, gfx1250), `f8f8s_pgr2_sia0` (MT256x256x256) emits
> `STINKY_TOTAL_INST_BYTES = 375200` (≫ 65536) → it is a **dynamic-only** kernel; its `label_GW_B0_OptNLL_MB` sits
> between `label_MultiGemmEnd` and the GSU-branch GW blocks and is not one of the 3-arm targets
> (`GW_B0_MB` / `GW_B0_GSU1` / `GW_B1_GSU1`). So the OptNLL cover applies in the **dynamic** regime — an earlier
> "f8f8s is static-regime" framing was wrong.

### 15.2 Idea (one line)

At the **same site** as the ladder (right after `label_MultiGemmEnd`), emit **one unconditional** abs-static-style
burst of **N `s_prefetch_inst`** (default **N = 2**) targeting a new `label_SW_PrefetchAbs_CpBoundary` anchored at the
**final-layout** CP boundary, **then** the existing 3-arm ladder verbatim. With N = 2 it covers
`[P(0), P(0)+2×4096) = [32640, 40832)` — enough to reach an OptNLL region at ~38196 (N = 1's `[32640, 36736)` would
not). **N is hard-capped at 2** (I-cache budget, §15.7).

### 15.3 Emission (static idiom, N = 2) — before the 3-arm ladder

```asm
; ---- inserted right after label_MultiGemmEnd, BEFORE the §12.3 sel ladder ----
; (1) UNCONDITIONAL CP-boundary cover (N=2, static idiom) — NEW
s_getpc_b64 s[base:base+1]
s_add_i32   s[base+2], label_SW_PrefetchAbs_CpBoundary, 4
s_add_u32   s[base],   s[base],   s[base+2]
s_addc_u32  s[base+1], s[base+1], 0
s_prefetch_inst s[base:base+1], 0,    null, 0x1f       ; [32640, 36736)
s_prefetch_inst s[base:base+1], 4096, null, 0x1f       ; [36736, 40832)  -> reaches OptNLL @ ~38196

; (2) existing GSU/beta 3-arm ladder — UNCHANGED (see §12.3)
label_Do_SW_PrefetchAbs_sel:
s_and_b32   s[base+2], s[sgprGSU], 16383               ; emitted DECIMAL (mask 0x3fff); GSU==1 ?
s_cmp_eq_u32 s[base+2], 1
s_cbranch_scc0 label_Do_PF_caseA
...                                                     ; caseB (fall-through) / caseA / caseC, N per §10.5
label_Do_PF_end:
; ---- original body continues ----

; ... later, at FINAL-layout offset ≤ 32640, in the tail-loop/epilogue body ...
label_SW_PrefetchAbs_CpBoundary:                        ; 0 bytes (alignment 1); anchored post-insertion
<original boundary instruction>
```

Notes on **verbatim-emission fidelity** (multi-agent verified against `StinkyAsmEmitter`):

- The GSU mask prints in **decimal `16383`** (LiteralInt), not `0x3fff` — the `0x3fff` in §12.3 sketches is
  conceptual.
- `s_prefetch_inst` operands render as `s[base:base+1], 0, null, 31` (koffset decimal, `null` slength, klength
  `0x1f`=31). `koffset=0` alone covers the full 4096 B window.
- Mnemonic + one space; `s[sgprGSU]`/`s[sgprBeta]` render symbolically only under `useSymbolicNames=true` (the
  Tensile production path).

### 15.4 Anchoring the boundary label — **must be post-insertion** (correctness)

The ladder + CP burst insert **≈ 276 B** *before* the boundary instruction (site is after `label_MultiGemmEnd`, which
is ≪ 32640; §14.3 puts the whole pre-epilogue path in `[0, ~32764)`). Byte breakdown (from
`getEffectiveBaseSizeInBytes` / `getLiteralExtraBytes`): CP burst ≈ 36 B (getpc 4 + `s_add_i32 label,4` = 8 + add_u32
4 + addc 4 + **2** prefetch 16) + 3 arms ≈ 204 B (each 20 + 6×8) + sel predication ≈ 36 B + labels 0 B ≈ **276 B**.

Therefore anchor `label_SW_PrefetchAbs_CpBoundary` on **post-insertion** layout, exactly as the **static** pass anchors
`label_SW_PrefetchAbs_0` (`SwInstructionPrefetchAbsStaticPass.cpp:321-372`):

1. Insert the CP burst (forward-referencing the label by name — layout-safe: a `label`-prefixed operand is
   unconditionally costed `+4`/FK_PCRel_4, `InstructionSizeCosting.cpp:342`) + the 3-arm ladder.
2. Re-run `computeSwPrefetchRelPhase1Accum` (a `phase2`) to get post-insertion `layoutGlobal`.
3. Insert the 0-byte `label_SW_PrefetchAbs_CpBoundary` (alignment 1) **before the last real instruction with
   post-insertion offset ≤ 32640**.
4. `return PreservedAnalyses::none();`

A **pre-insertion** anchor would resolve to real offset ≈ 32916 → shift the whole cover window forward and leave
`[32640, 32916)` (the hottest just-missed bytes) **blind** — defeating the feature. The in-pass `phase2` is needed for **label
placement**; downstream `AccumulateInstructionSizePass` (pipeline, after the pass) re-derives **byte costing**
independently — two distinct purposes.

**Why the label must move *earlier* in the original code (CP-coverage loss).** The CP preloads the first 32640 B of the
**final** image. Because the ~276 B ladder+cover is inserted at `label_MultiGemmEnd` (`≪ 32640`), it pushes all
following original code forward by ~276 B — so the CP window now covers **~276 B *less* original code** than before.
Post-insertion anchoring therefore lands `label_SW_PrefetchAbs_CpBoundary` at an **earlier** original instruction (by
the full inserted size), recapturing exactly the original slice CP just lost. Worked case (`MGE ≈ 700`, ladder = 276 B):

| item                                | pre-insertion (no ladder) | post-insertion (ladder 276 B @ MGE≈700)                              |
| ----------------------------------- | ------------------------- | --------------------------------------------------------------------- |
| `label_MultiGemmEnd`              | 700                       | 700 (unchanged)                                                       |
| inserted ladder+cover               | —                        | `[700, 976)`                                                        |
| instruction CP last covers          | orig**32640**       | orig**32364** → final 32640                                    |
| `label_SW_PrefetchAbs_CpBoundary` | (naive) 32640             | orig**32364** = final 32640 — **moved earlier by 276 B** |
| CP-covered*original* range        | `[0, 32640)`            | `[0, 32364)` — **276 B less original code**                  |
| OptNLL (`GW_B0_OptNLL_MB`)        | orig 38196                | final 38472                                                           |
| N=2 cover window (final addrs)      | —                        | `[32640, 40832)` ⊇ lost `[32640, 32916)` + OptNLL@38472          |

So the label is re-anchored on the **post-insertion** layout (mirror static `:321-372`), which absorbs **any** inserted
byte-shift exactly. The observation "after inserting the CP-extend code, CP covers less original code, so the boundary
label must move forward" is **precisely** what this achieves — no extra adjustment is needed. (At 276 B the shift is
≪ one 4096 B grid step; but the rule holds for any inserted size because the anchor is always recomputed post-insertion.)

### 15.5 Guards — decoupled from the 3-arm GSU/beta guard

The CP cover uses **only** the base triple + a label — **neither** `s[sgprGSU]` **nor** `s[sgprBeta]`. So restructure
the current bail (`SwInstructionPrefetchAbsDynamicPass.cpp:381-392`, which today aborts the whole emission when GSU or
beta is undefined) so that GSU/beta guards gate **only the 3-arm ladder**:

| Condition                                       | CP-boundary cover (N=2)                                                                                                               | 3-arm ladder |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| `total > 65536` (dynamic regime)              | required                                                                                                                              | required     |
| `baseSgpr ≥ 0`                               | required                                                                                                                              | required     |
| **not Stream-K**                          | required (for SK the triple is never reserved →`baseSgpr = −1` → CP cover is gated off by `baseSgpr ≥ 0`, same as the ladder) | required     |
| `sgprGSU` **and** `sgprBeta` defined  | **not needed**                                                                                                                  | required     |
| boundary anchor ahead of site (`MGE < 32640`) | required (else skip: backward prefetch is wasted)                                                                                     | —           |

This maximizes near-boundary coverage even on `GSU0` / no-beta kernels where the ladder bails. **SGPR-safety is
verified in Tensile:** the base triple is reserved & deferred-checked-in at `label_MultiGemmEnd` for any
**non-Stream-K gfx1250** abs kernel *regardless of GSU/beta* (reservation + pending-checkIn in
`KernelWriter.py:9611-9648`, the deferred checkIn at `label_MultiGemmEnd` in `KernelWriterAssembly.py:3026-3034`;
comment explicitly anticipates the "GSU0 / no-beta non-Stream-K" case). **Implementation note:** the emit function
must return `true` when the CP cover emits even if the ladder bails, so `run()` yields `PreservedAnalyses::none()`.

### 15.6 Emit-helper change

`emitBurst` (`SwInstructionPrefetchAbsDynamicPass.cpp:515-543`) is **hard-wired to `N = kFixedPrefetchN = 6`** (the
lambda captures the enclosing `N`). The N≤2 CP cover cannot reuse it as-is. **Recommended:** parametrize
`emitBurst(target, n = 6)` — the 3 arms keep their verbatim shape by passing 6, the CP cover passes N (default 2).
(Alternative: a dedicated helper; marginally safer for a literal "arms unchanged" reading, at the cost of duplication.)

### 15.7 Coverage & the N ≤ 2 cap (I-cache budget) — multi-agent verified

> **SUPERSEDED by §16 (for the dynamic width).** The `N ≤ 2` cap below is the **special case `armN = 6`** of the
> general invariant `coverN + armN ≤ 8` (§16.4). §16 lets `coverN` grow to 4 by shrinking the resident arm to
> `armFloor = 4`. This §15.7 text remains valid **only while the arm is pinned at 6** — which is exactly what the
> *currently shipped code* does (`kCpBoundaryCoverN = 2`). Read §16 for the computed-width behavior.

- **Default N = 2** → covers `[32640, 40832)` (koffset 0 and 4096). This reaches an OptNLL region at ~38196; **N = 1**
  (`[32640, 36736)`) would not.
- **Hard cap N ≤ 2** (special case `armN = 6`; general cap is `coverN + armN ≤ 8`, §16.4) — NOT the static pass's `kMaxStaticPrefetchN = (65536−32640)/4096 = 8`
  (`SwInstructionPrefetchAbsStaticPass.cpp:214`). The dynamic cap is lower because the CP cover is resident
  **concurrently with one predicated ladder arm**: each arm bursts `kFixedPrefetchN = 6` → `6×4096 = 24576 B`, and
  exactly one arm executes at runtime (GSU/beta-predicated, §14.3). Budget:

  ```
  CP head 32640 + one arm 24576 + N×4096 ≤ 65536 (I-cache)
    ⇒ N×4096 ≤ 8320  ⇒  N ≤ 2      (N=2 → 65408 B, ~128 B slack;  N=3 → 69504 B, overflow)
  ```

  The static pass allows 8 only because it has **no** concurrent arm to co-reside with.
- **Fully covering OptNLL → tail-loop end** would want `N = ceil((tailEnd − 32640)/4096)`, but that is **clamped to 2**
  by the concurrent-arm budget above. So the cover reaches `[32640, 40832)`; anything past 40832 (a farther OptNLL or
  the deep tail) stays a **future follow-up** — free budget by dropping the +284 B Case-A arm (§14.3), or add an
  OptNLL-anchored ladder arm.
- **Not a §2.2/§12.1 violation.** Those drop *GW-block targets* with `offset ≤ P(0)`; the CP cover is a different
  mechanism (a boundary hint whose window extends *past* `P(0)`). Orthogonal — keep both.

    ### 15.8 Footprint / thrash

The cover adds `N×4096 B` (default **8 KiB**) on top of the predicated live footprint. §14.3 gives CP head + one arm
≈ `32640 + 24576 = 57216 B (~57 KB)`; with N = 2 → **65408 B**, just under the 65536 I-cache (**~128 B slack** — this
is exactly why N is capped at 2). Issued at `label_MultiGemmEnd` (CP-resident, whole K-loop as issue-latency lead)
targeting soon-to-run near-boundary code — **not** an entry-burst thrash case (the "§17.2" thrash warning lives in the
sibling `SwPrefetchAbsInsertionPass-Design.md` and is about the abs-static full-window ~32 KiB burst at *entry-begin*).

### 15.9 Status

Multi-agent verified (design soundness + emitted-assembly fidelity + refined re-review): **GO**, no correctness
blockers — `s_prefetch_inst` is a hint, so any residual mismatch is perf-only. Falls under the same §13 **hardware
gate** (gfx1250 device-lib assemble-clean + numeric gtest) before merge; the new unconditional CP hints are covered by
that gate. The **N ≤ 2 cap** (concurrent-arm I-cache budget) is multi-agent verified **for the fixed-width `armN = 6`
implementation shipped today**; the "coverage past `40832`" follow-up is **resolved by §16** (dynamic `coverN` up to 4
by trading the arm down to `armFloor = 4`). Remaining open follow-up: whether to also emit the CP cover in the
`(32640, 65536]` static regime (currently the static pass's `label_SW_PrefetchAbs_0` already covers `P(0)` there).

---

## 16. Dynamic CP-extend cover width — size to `label_TailLoopBeginL` (supersedes the fixed N = 2)

> **Status: DESIGN (proposal). Multi-agent reviewed — GO-WITH-FIXES.** This section replaces §15's fixed `N = 2`
> with a **computed** `coverN` that self-adjusts to each kernel's once-through fast-path length, and lets `coverN`
> trade against the ladder arm width `armN` under a single I-cache budget. It is the resolution of the §15.7 "coverage
> past 40832" follow-up.
>
> **The shipped code still implements §15**, not this section: `kCpBoundaryCoverN = 2` (hard cap 2) at
> `SwInstructionPrefetchAbsDynamicPass.cpp:182`, `kFixedPrefetchN = 6` at `:178`, and `coverEmitted` driven by
> `entryBB != nullptr` at `:574`. Every `:NNN` citation below points at that §15 implementation that §16.7's "required
> fixes" will modify — `coverN` / `armN` / `armFloor` do **not** exist in code yet. No code has been changed.

### 16.1 Why fixed N = 2 is not enough

§15's `N = 2` covers only `[32640, 40832)`. But the **OptNLL fast-store body** can be much larger. Concrete case
(`f8f8s_pgr2_sia0.yaml`, `Cijk_Alik_Bljk_F8F8S_BH_UserArgs_MT256x256x256`, DepthU=256, gfx1250 — byte offsets from
the parsed real objdump cached in `traces/scripts/optnll_cache.pkl`, total 413264 B; consistent with the hard-coded
table in `traces/scripts/optnll_verdict.py:21-32`):

| label                                                                        | offset                 | hint N to reach`floor((off−P0)/4096)+1` | note                                           |
| ---------------------------------------------------------------------------- | ---------------------- | ------------------------------------------ | ---------------------------------------------- |
| `label_GW_B0_OptNLL_MB` (OptNLL body **entry**)                      | 20504                  | 0                                          | inside CP                                      |
| `label_SW_PrefetchAbs_CpBoundary` (cover anchor)                           | 32636                  | —                                         | last insn ≤ P0 (4 B below P0)                 |
| `label_GW_End` / `label_OptNLL_End` (body **exit** / `s_endpgm`) | 46940 /**46952** | **4**                                | ≫ CP window`32640`, ≫ N=2 window `40832` |
| `label_TailLoopBeginL` (tail loop begin)                                   | **50976**        | 5 (clamped→4)                             | measured in`optnll_cache.pkl`                |
| `label_GW_B0_MB` (deep general store)                                      | 51980                  | 5 (clamped→4)                             | ladder Case-A target                           |

The once-through OptNLL fast path is `[20504, 46952]`; a fixed `N = 2` (reaching `40832`) leaves **`[40832, 46952)`
blind** (~6.1 KiB) — straight-line code that, unlike a loop, does not self-warm. This is the extreme regime that
motivates a computed width. `rawCover = floor((46952−32640)/4096)+1 = 4` closes it exactly (reaches `49024`).

### 16.2 Key layout fact (multi-agent verified) — `label_TailLoopBeginL` sits *after* the OptNLL body

In `KernelWriter.kernelBody`, the OptNLL container (self-contained: its own store epilogue + `s_endpgm` +
`label_OptNLL_End`) is emitted at `KernelWriter.py:5925`, **strictly before** the regular tail-loop block
(`if not kernel["NoTailLoop"]:` at `:5984`, which opens the tail loop and emits `TailLoopBegin<idx>` via
`openLoop(..., -1, ...)` at `:6346` → `KernelWriterAssembly.py:7577/7593`). Therefore, in byte order:

```
label_OptNLL_End (46952)  <  label_TailLoopBeginL (50976, measured)  <  label_GW_B0_MB (51980)
```

**Consequence:** the byte gap `[0, label_TailLoopBeginL)` **captures the entire OptNLL body**. And the tail-loop-begin
offset is **dynamic**: with a *small* OptNLL it sits inside the CP window (→ `coverN = 0`, nothing to cover); with a
*large* OptNLL it is pushed past `P(0)` (→ `coverN` grows to exactly span the fast path). So measuring to
`label_TailLoopBeginL` is **self-adjusting** and needs no per-kernel tuning.

### 16.2.1 Two-level GSU dispatch + OptNLL fast path (worked example: `f8f8s_pgr2_sia4`)

Program-order map of a representative dynamic-regime kernel
(`Cijk_Alik_Bljk_F8F8S_..._MT256x256x256_MpgMlgs4...`; line = source line, byte = `layoutGlobal`):

```
753   label_MultiGemmEnd              ← abs-prefetch site (per-gemm join)
754   label_Do_SW_PrefetchAbs_sel     ← StinkyTofu 3-arm selector (inserted)
      3372  label_toPGR1  GSU==1? ──scc0(GSU≠1)──────────────→ label_GSU_3      (skip OptNLL)
      3375-3396 OptNLL gate: Beta==0? Alpha==1? M-edge? N-edge? tail? ─any miss─→ label_OptNLL_End
3933  label_GW_B0_OptNLL_MB   (byte 20216, < P0)   ← OptNLL fast-store body ENTRY (CP-resident)
6100  label_SW_PrefetchAbs_CpBoundary (≈ byte 32640 = P0)
8491  label_GW_End → s_endpgm                       ← OptNLL fast path EXITS here
8494  label_OptNLL_End   ┐  (both at byte 46664 — OptNLL_End is a 0-byte
8495  label_GSU_3        ┘   fall-through label INTO GSU_3)  ← GENERAL (non-OptNLL) path entry
9206  label_TailLoopBeginL   (byte 50688)           ← self-warming tail loop begins
9398  label_GW_B0_MB     (byte 51692)               ← Case A: GSU>1 store  (store dispatch @9395: GSU==1?→GSU_4)
20011 label_GSU_4                                   ← GSU==1 Beta-split dispatch
20022 label_GW_B0_GSU1                              ← Case B: GSU==1 & Beta==0
42548 label_GW_B1_GSU1                              ← Case C: GSU==1 & Beta≠0
```

**Two-level store dispatch** (mirrored by the StinkyTofu selector's A/B/C):

1. `@9395`: `GSU>1 → GW_B0_MB (A)` ; `GSU==1 → label_GSU_4`.
2. `label_GSU_4`: `Beta==0 → GW_B0_GSU1 (B)` ; `Beta≠0 → GW_B1_GSU1 (C)`.

**Five structural insights (multi-agent verified) that drive the OptNLL optimization:**

1. **OptNLL is a self-contained straight-line fast path** entered from the prologue (`@3372`), body `[3933, 8491]`,
   with its own store + `s_endpgm` — it exits **before** any deep GW block.
2. **The OptNLL body straddles `P(0)`**: entry `20216 < P0`, so `[0, P0)` is CP-resident; only the tail
   `[P0, OptNLL_End=46664)` needs SW prefetch.
3. **OptNLL ⊂ Case B.** The OptNLL gate requires `GSU==1 & Beta==0`, so OptNLL **never** overlaps Case A (`GSU>1`)
   or Case C (`Beta≠0`). Only the selector's **Case B** is OptNLL-relevant; A and C are untouched.
4. **On an OptNLL launch, none of the three ladder arms execute** (the launch `s_endpgm`s at `@8491`). So the Case-B
   `GW_B0_GSU1` prefetch is wasted *and* competes for I-cache with the OptNLL body — the target of the §16.10 skip.
5. **CP hardware is maxed** (`.amdhsa_inst_pref_size 255 = 32640`), so everything past `P(0)` is *only* reachable by
   SW prefetch — this is why the cover/ladder exist at all.

**Critical corollary — the general (non-OptNLL) path runs through `label_GSU_3`.** `label_OptNLL_End` and
`label_GSU_3` are the **same byte** (46664: OptNLL_End is a 0-byte fall-through into GSU_3). **Every** non-OptNLL
launch reaches GSU_3 — either directly (`GSU≠1` at `@3372`) or by falling through `label_OptNLL_End` (any
Beta/Alpha/edge/tail miss). The region `[GSU_3=46664, TailLoopBeginL=50688)` (4024 B) is **one-shot straight-line
code** (last-iteration WMMA drain + tail store/index glue), **not** a self-warming loop. This is why the CP-extend
cover boundary must be `label_TailLoopBegin*` (50688), which covers **both** the OptNLL body **and** the GSU_3 glue and
stops exactly at the self-warming tail loop — see §16.10 (Case-B predicate) and §16.12 (boundary must NOT be shrunk to
`OptNLL_End`).

### 16.3 Boundary selection (with fallbacks)

```
boundary =
    min{ offsetOf(l) : l startswith "label_TailLoopBegin" }   # primary — prefix match, MIN offset (16.7 #1)
    else offsetOf("label_OptNLL_End")                        # fallback #1 (NoTailLoop && isOptNLL)
    else min{ offsetOf(l) > P(0) : l startswith "label_GW_" } # fallback #2 (GW_B0_GSU1 universal, 2755/2755)
    else (none)
```

- **Primary** `label_TailLoopBegin*`: the emitted name is `label_TailLoopBeginL` (**no underscore, no nta/ntb suffix** —
  verified across **2900 real kernels'** objdump + `.s` in `/data0/geotseng/comparison_output`, 68252 occurrences, zero
  underscore forms). `KernelWriterAssembly.py:7699` builds `"TailLoopBegin"+loopChar`, and the other construction site
  `:7575-7577` **forces `bStrNta = bStrNtb = ""` for the tail loop**, so the suffix is ONLY the summation index char
  (`L` for batched `Cijk_...`, `K` for non-batched) — never an nta/ntb tag (those only appear on the *main* loop's
  `LoopBegin`). **Match by the prefix `label_TailLoopBegin`, not the literal `label_TailLoopBeginL`.** If multiple
  summation loops emit more than one, take the **min offset** (shallowest = earliest once-through boundary). `NoTailLoop`
  (`ASEM % DepthU == 0`, `Solution.py:4205-4209`) omits the tail loop, so the label can be absent — hence the fallbacks.
- **Fallback #1** `label_OptNLL_End`: present only under `isOptNLL` (`KernelWriterAssembly.py:9688/9891`), which is
  **orthogonal** to `NoTailLoop`. A `NoTailLoop && !isOptNLL` kernel has neither → falls through to #2.
- **Fallback #2** shallowest past-CP `label_GW_*`: `GW_B0_GSU1` is present on all non-Stream-K GSU1 kernels
  (**2755/2755**, §13.1). Residual "all fallbacks miss ⇒ `coverN = 0`" case (every `GW_*` ≤ `P(0)`) is an intended
  no-op — such a kernel has no past-CP once-through code to warm.

### 16.4 Width formula and the cover↔arm budget split

```
P0 = 32640,  SPACING = 4096,  armFloor = 4        # armFloor = 4 is the accepted budget policy
budget:  coverN + armN ≤ 8                          # (65536 − 32640) / 4096 = 8.03  (one arm resident, §15.7)

if boundary is none or boundary ≤ P0:
    coverN = 0;  armN = 6                            # fully CP-resident → skip cover entirely; keep arm at 6 (not 8)
else:
    rawCover = floor((boundary + INSERT_UB − P0) / SPACING) + 1  # INCLUSIVE, NOT ceil (§16.6); +INSERT_UB per §16.7 #2
    coverN   = clamp(rawCover, 0, 8 − armFloor)       # = clamp(.., 0, 4)
    armN     = min(kFixedPrefetchN=6, 8 − coverN)     # 0/1/2 → 6, 3 → 5, 4 → 4
```

| coverN                   | 0 | 1 | 2 | 3 | 4 |
| ------------------------ | - | - | - | - | - |
| armN = min(6, 8−coverN) | 6 | 6 | 6 | 5 | 4 |

`f8f8s_pgr2_sia0` lands at **coverN = 4 / armN = 4**. The **primary** boundary is `label_TailLoopBeginL = 50976`, so
`rawCover = floor((50976 + 320 − 32640)/4096)+1 = floor(18656/4096)+1 = floor(4.55)+1 = 5` (the `+320` INSERT_UB of
§16.7 #2 does not cross a block boundary here, so it matches the bare `floor(4.476)+1 = 5`); the clamp **binds** →
`coverN = clamp(5,0,4) = 4`. That
covers `[32640, 49024)`, which ⊇ `OptNLL_End = 46952` (the whole OptNLL fast path) **but not** `TailLoopBeginL = 50976`
itself — the `[49024, 50976)` remainder is tail-loop/`endSummation` code and is an accepted shortfall (§16.8;
self-warms on the tail loop's first iteration). `armN = 4` still fully covers the deep Case-A `GW_B0_MB` block
(≤ 16 KiB). This is why `armFloor = 4` (not §15.7's implicit `armFloor = 6` ⇒ `coverN ≤ 2`).

> NB: had the primary label been absent (`NoTailLoop`), fallback #1 `OptNLL_End = 46952` gives
> `rawCover = floor((46952−32640)/4096)+1 = 4` → same `coverN = 4` (no clamp). Both boundaries land on `coverN = 4`
> here; they are only computed from *different* offsets.

**Budget note (updates §15.7).** §15.7 capped `N ≤ 2` by pinning `armN = 6`. The general invariant is
`coverN + armN ≤ 8` (one predicated arm resident at runtime — the ladder's arms are mutually exclusive, so runtime
residency is `coverN + armN`, **not** `coverN + 3·armN`). `armFloor = 4` opts to let `coverN` grow to 4 by shrinking
the resident arm to 4. The cover window (near `P0`) and the resident arm window (deep, e.g. `GW_B0_MB @ 51980`) are
**disjoint**, so footprints are additive.

### 16.5 Trade-off (must be acknowledged)

Stealing 2 arm blocks (6→4) to fund the cover **pessimizes deep-path kernels** (GSU>1 / Case A executes) where the
near-`P0` cover is unused. It **pays off on OptNLL-fast-path kernels** (the launch runs straight through the OptNLL
body). Before enabling `armFloor = 4` fleet-wide, confirm fast-path kernels dominate the gated set, or make the
cover↔arm split conditional on the kernel's favored path rather than always stealing 2 blocks.

### 16.6 Off-by-one — use `floor(..)+1`, not `ceil`

`ceil((boundary − P0)/4096)` is **one block short** when `(boundary − P0)` is an exact multiple of 4096 (last hint
lands at `boundary − 4096`, excluding byte `boundary`). Use `rawCover = floor((boundary − P0)/4096) + 1`, which is
inclusive of the block *containing* `boundary`. Precisely: for a non-multiple gap `floor+1 == ceil` (so there is **no
extra top margin** — the cover ends at the block boundary just past `boundary`); only at an exact multiple does
`floor+1` add the one block `ceil` drops. This mirrors the static pass's inclusive `countN` **result** (it reaches the
same inclusivity by inflating `total` with the burst size, `SwInstructionPrefetchAbsStaticPass.cpp:190-194,196-200`),
though not by a literal `+1`. Separately, the burst **base** is anchored at the last insn ≤ `P0` (§15.4), i.e.
slightly below `P0` by `δ (< one insn)`; that `δ` shifts the whole covered window down by `<` one instruction, well
within the ≤ 1-block over-coverage that §16.7 #2's `INSERT_UB` already grants — so no separate `+1` is needed for it.

### 16.7 Required fixes before implementation (multi-agent GO-WITH-FIXES)

1. **Prefix label match.** Look up by the prefix `label_TailLoopBegin` (emitted name is `label_TailLoopBeginL` —
   **no underscore, no nta/ntb suffix**, verified across 2900 kernels; suffix is only the summation index char
   `L`/`K`/…), matching the prefix rather than assuming the exact `label_TailLoopBeginL`. If more than one matches, take
   the **min offset**. Explicitly route `NoTailLoop && !isOptNLL` through the `GW_*` backstop (§16.3) and document the
   "all fallbacks miss ⇒ coverN = 0" no-op as intended.
2. **Conservative one-shot shift — do NOT iterate.** `coverN` is derived from the **pre-insertion** `boundary`
   (`buildLabelOffsets` ← phase1), but the entry cover **and** the 3-arm ladder are inserted *before* `boundary`, so
   the final-layout `boundary` shifts forward by the inserted byte count. Do **not** run a fixed-point iteration:
   because `armN = 8 − coverN` couples arm size to insert size (raising `coverN` *shrinks* total inserted bytes ~16 B —
   **negative feedback**), the map is monotone-*decreasing* and a naive 2-iteration settle can 2-cycle / under-cover at
   an alignment knife-edge (unlike the static pass's monotone-increasing `SwInstructionPrefetchAbsStaticPass.cpp:196-200`).
   Instead, shift `boundary` by a **constant conservative UPPER BOUND** on inserted bytes and compute `coverN` **once**:

   ```
   INSERT_UB = coverScaffold(20) + coverNmax(4)·8            # cover ≤ 52 B
             + selectorScaffold(≈40) + 3·(armScaffold(20) + armNmax(6)·8)   # ladder ≤ ~244 B
             ≈ 320 B                                          # a fixed compile-time constant, ≪ 4096
   rawCover  = floor((boundary + INSERT_UB − P0)/4096) + 1
   coverN    = clamp(rawCover, 0, 8 − armFloor)
   ```

   Since the true inserted bytes ≤ `INSERT_UB`, the true final `boundary` ≤ `boundary + INSERT_UB`, so this **never
   under-covers**; it may over-emit **at most one** 4096-block when the pre-insertion gap sits within `INSERT_UB` of a
   block multiple. This is a single pass — trivially terminating, deterministic, and independent of the `coverN↔armN`
   coupling. (The ladder emits **three** arms into the binary, so `INSERT_UB` counts `3·armN`, even though runtime
   residency is only `coverN + armN`.) The existing phase-2 re-accumulate (`:625-658`) is still used to *anchor
   `label_SW_PrefetchAbs_CpBoundary` at the last insn ≤ P0*, but it no longer feeds back into `coverN`.
3. **`coverN = 0` skips the whole scaffold, not just the k-loop.** `emitBurst`
   (`SwInstructionPrefetchAbsDynamicPass.cpp:515-543`) emits getpc + 3 adds that forward-reference
   `label_SW_PrefetchAbs_CpBoundary` *before* the `for k < n` loop. Guard the **entire** `emitBurst` + phase-2 label
   creation on `coverN > 0` (drive `coverEmitted` from `coverN > 0`, not merely from `entryBB != nullptr` at `:574`) —
   else a skipped burst leaves a dangling label, or a skipped label leaves `s_add_i32` referencing an undefined symbol
   (assembler failure).
4. **Thread dynamic `armN` into the 4 arm `emitBurst` calls** (`:601 / :605 / :608 / :614`). `:486`, the debug print
   `:662`, and `detectAndDumpD0` (`:272/312/319`) consume `kFixedPrefetchN` **cosmetically only** (detector comment
   `:292-293`: "affects the dumped value, never behavior") — update for telemetry fidelity, not correctness.
5. **`armN = 0` cannot occur** under `armFloor = 4` (`armN ≥ 4`); no degenerate all-scaffold-no-hint burst. If a future
   policy allows `armN → 0`, skip that arm's burst (mirror fix #3) rather than emitting an orphan scaffold.

### 16.8 Coverage gap at the clamp (known limitation) — quantified fleet audit

`coverN ≤ 8 − armFloor = 4` reaches at most `[32640, 49024)`. A kernel whose once-through fast path extends **beyond
49024** (`OptNLL_End > 49024`) is under-covered in straight-line code. `f8f8s` (`46952`) fits with ~2 KiB slack;
farther-OptNLL kernels remain a follow-up (raise the budget by dropping the +284 B Case-A arm, §14.3, or add an
OptNLL-anchored ladder arm). The `[49024, TailLoopBeginL)` remainder (tail-loop/`endSummation` code) is acceptable:
the tail loop self-warms on its first iteration.

**Fleet audit (multi-agent verified, `/data0/geotseng/comparison_output`, 2856 abs-dynamic dumps / 2837 kernels).**
Behavior is uniform and size-costing is complete — the audit found **no** state-machine deviation:

| terminal decision                                         | count | note                                                      |
| --------------------------------------------------------- | ----- | --------------------------------------------------------- |
| no-op (`totalLayoutBytes ≤ 32640`)                     | 1556  | whole kernel in CP window                                 |
| static regime`(32640, 65536]` — detector-only, no emit | 647   | confirmed 32752–65456                                    |
| emitted**`ladder(3-arm)`** (`> 65536`)          | 649   | 100 % 3-arm;`armN ∈ {4,5,6}`, `coverN ∈ {0..4}`     |
| Stream-K bail                                             | 4     | sole bail reason; none of the 24 Stream-K kernels emitted |
| other skip / guard contradiction                          | 0     | —                                                        |

All 2837 `instruction_size_comparison_report.md` show **Success 100.00 %** with **"Not covered cases = None"**. Of the
649 emits, `coverN` distribution is `0 → 369, 1 → 60, 2 → 30, 3 → 30, 4 → 160` — i.e. **160 kernels hit the
`coverN = 4` clamp**. Classifying those 160 by the hot OptNLL fast-path exit `label_OptNLL_End` (cover window ends at
`49024`):

| bucket                                  | condition                        | count        | meaning                                                                                                                   |
| --------------------------------------- | -------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------- |
| **(a) benign**                    | `OptNLL_End ≤ 49024`          | **77** | fast-store body fully inside the cover;`[49024, …)` is slow-path/tail-loop that self-warms. Clamp costs nothing.       |
| **(b) real but bounded**          | `49025 ≤ OptNLL_End ≤ 65536` | **53** | hot fast-path exit beyond the window but within one I-cache —**coverable if the cap is relaxed** (§16.11).        |
| **(c) fundamentally uncoverable** | `OptNLL_End > 65536`           | **30** | fast path larger than the whole 64 KiB I-cache —**no `coverN` helps**; root cause is kernel size, not the clamp. |

Category makeup — **(b)**: `f64_gfx1250 (16)`, `spmm_f8_ml (7)`, `spmm_f8hs (4)`, `spmm_f8 (4)`, `spmm_b8bs (4)`,
`spmm_f8b8 (3)`, `f8f8s_pk8_gfx1250 (3)`, `spmm_b8hs (2)`, `1024_vgpr_gfx1250 (2)`, and 1 each of `spmm_f8bs`,
`spmm_f8_sb`, `spmm_f16_sb`, `spmm_bf16_sb`, `spmm_b8f8`, `spmm_b8`, `f8f8s_sr_gfx1250`, `bf16_CLS_gfx1250`.
**(c)**: `f64_gfx1250 (16)`, `spmm_f8_ml (6)`, and the 8 streaming-B variants `spmm_{f8hs,f8bs,f8b8,f8,b8hs,b8f8,b8bs,b8}_sb`
(1 each). The `f64_gfx1250` family is bimodal — 16 at `OptNLL_End ≈ 55–59 KB` (bucket b) and 16 at `≈ 108–111 KB`
(bucket c, fast-store body physically placed ~1.7× the I-cache).

**Boundary-selection nuance for f64 (follow-up).** On some f64 kernels the dump shows `boundary (≈111 KB) < OptNLL_End (≈135 KB)`, i.e. the `label_TailLoopBegin*` primary boundary sits *before* the deepest OptNLL body — evidence the f64
layout order differs from `f8f8s` (OptNLL body after the tail loop). The §16.3 boundary selection has not been validated
for f64; treat f64 cover sizing as unverified pending a dedicated check.

### 16.9 Correctness stance

No **hard** correctness bug: `s_prefetch_inst` is a pure hint, so any off-path or under/over-cover is at most wasted
I-cache lines / fetch bandwidth, never a functional fault. The fixes in §16.7 make it **robust** (not kernel-tuned)
and assembler-safe. Ships under the same §13 hardware gate.

### 16.10 Dependency: this width computation is a PREREQUISITE for the OptNLL-aware Case-B ladder skip

A separate proposed optimization makes the **ladder's Case B** (`GSU==1 & Beta==0`, fall-through, prefetches
`label_GW_B0_GSU1` at `SwInstructionPrefetchAbsDynamicPass.cpp:601`) OptNLL-aware: when the runtime `Alpha==1.0`
(OptNLL predicted), **skip** Case B's 6-hint burst because "the OptNLL fast path is already resident via CP preload +
the §15 CpBoundary cover". That premise is **only true once `coverN` is computed by this section**:

- With §15's fixed `N = 2`, the OptNLL tail `[40832, 46952)` is **not** covered (§16.1). Skipping Case B there would
  leave the OptNLL fast-store partially cold → demand fetch on the hottest launch path. So the Case-B skip is
  **unsafe** on top of `N = 2`.
- With this section's computed width, `f8f8s` gets `coverN = 4` → `[32640, 49024) ⊇ OptNLL_End (46952)`, so the OptNLL
  fast path is fully resident and the Case-B burst is genuinely redundant → safe to skip.

**Ordering (record for D2):** land the dynamic `coverN` (§16) **first**; only then enable the OptNLL-aware Case-B
skip. They are two halves of the same intent — the **cover** warms the OptNLL fast path near `P0`, so the **arm**
must not also spend 6 hints prefetching the mutually-exclusive non-OptNLL `GW_B0_GSU1`. Both are hint-only, so neither
can cause a functional fault; the dependency is about **not** re-introducing the cold-tail regression the cover just
fixed. The Case-B skip's own gates (`emitCover && has("label_GW_B0_OptNLL_MB") && sgprAlpha defined`) must additionally
require `coverN` to actually reach `OptNLL_End` (i.e. `coverN*4096 + P0 ≥ OptNLL_End`), else fall back to emitting the
Case-B burst.

**Scope narrowing (multi-agent verified, §16.2.1): OptNLL ⊂ Case B.** The OptNLL gate requires `GSU==1 & Beta==0`, so
the OptNLL fast path can only pre-empt the selector's **Case B**. Case A (`GSU>1`) and Case C (`Beta≠0`) never overlap
OptNLL and are **left unchanged** — the OptNLL-aware transform touches only Case B.

**Predicate must include the tail check, not Alpha alone (multi-agent verified — perf regression otherwise).** The true
OptNLL gate is a 5-clause AND: `GSU==1 & Beta==0 & Alpha==1 & M-edge-clear & N-edge-clear & tail-clear`
(`SizesSum % DepthU == 0`). The selector already tests GSU and Beta; approximating the rest with **`Alpha==1` only**
mispredicts on any `GSU1&Beta0&Alpha1` launch that is actually an **edge tile** or needs a **tail loop** — that launch
runs the general path (`label_GSU_3 → … → GW_B0_GSU1`), but the Alpha-only skip already dropped the `GW_B0_GSU1`
prefetch ⇒ demand-fetch (I-cache miss). This is **perf-only** (the store still executes), but the exposure is not
uniform: **M/N-edge is per-workgroup** (grid perimeter, small on large grids) while **tail (unaligned K) is
whole-grid** — *every* workgroup mispredicts when `K` is not a multiple of `DepthU`. Therefore the Case-B skip must
replicate **at least the tail check** (ideally the full edge+tail set, ~15 scalar ops mirroring the gate); `Alpha==1`
alone is acceptable only when the shape is guaranteed `DepthU`-aligned (and edge-free). When the extra checks are not
worth emitting, keep the unconditional `GW_B0_GSU1` burst (always correct for the demand path).

### 16.11 Scope, guards, and assumptions to validate (multi-agent review folded)

Explicit boundaries of this design, recorded so no reader over-reads its reach:

- **Stream-K: not covered, by construction.** The pass bails **before** any emission for Stream-K
  (`m_asmSetSymbols.count("sgprSrdWS") || "sgprSynchronizer"`, `SwInstructionPrefetchAbsDynamicPass.cpp:393-401`),
  so neither the ladder nor the CP cover is ever reached on Stream-K kernels — the dynamic `coverN` here is moot for
  them. Stream-K instruction prefetch remains a §11.2 deferred family.
  **API-layer note (unified `SwInstructionPrefetch` bitmask):** at the Tensile front end, Stream-K never selects
  Absolute — Auto(`-1`) resolves to Relative on Stream-K, and an *explicit* Absolute(`2`) on a Stream-K (or
  non-gfx1250) solution is rejected in `Solution.assignProblemIndependentDerivedParameters` (pick Auto or Relative
  instead). The C++ bail above is therefore defense-in-depth rather than the sole handling.
- **f64 (double): not covered, rejected at the API layer.** `f64` GEMMs are excluded from Absolute — Auto(`-1`)
  resolves to Relative for `DataType.isDouble()` (`resolveSwInstructionPrefetch(..., isF64=True)`), and an *explicit*
  Absolute(`2`) on an f64 solution is rejected in `Solution.assignProblemIndependentDerivedParameters` alongside the
  Stream-K / non-gfx1250 rejects. Rationale (§16.8/§16.13): f64's OptNLL epilogue routinely exceeds the 64 KiB I-cache
  (bucket-c fleet-wide, 16/32 emitters), so the abs cover/ladder yields no reliable benefit; f64's `sgprAlpha` is a
  2-dword pair so the OptNLL-aware Case-B fp32 predicate cannot apply; and the f64 boundary-selection order is
  unverified. Excluding f64 up front also removes the §16.13.2 dead-zone regressions that were f64-dominated.
- **GSU0 / no-beta (ladder skipped, cover decoupled): `coverN` STILL clamped to 4 — accepted under-cover.** When the
  ladder is not emitted (`!wantLadder`: no `label_GW_B0_GSU1`, or `sgprGSU`/`sgprBeta` undefined), the CP cover is
  decoupled and still emits (§15.5). With **no resident ladder arm**, the I-cache budget could in principle give the
  cover the full 8 blocks (`coverN + 0 ≤ 8`). This design **deliberately keeps the unconditional `clamp(.., 0, 4)`**
  anyway (accepted decision), trading a possible extra 4 blocks of near-`P0` coverage on GSU0/no-beta kernels for a
  single, arm-independent clamp. Revisit only if GSU0/no-beta kernels with a `> 49024` fast path prove material.
- **Coordinate frame: sound but under-validated.** `coverN` is computed entirely in the pass's `phase1.layoutGlobal`
  frame (accumulated encoded sizes in list order); `P0 = 32640` is applied as the CP-boundary anchor in that **same**
  frame (`:625-658`, "last insn ≤ P0"), so a constant origin delta between the pass frame and the loaded-binary frame
  cancels in `boundary − P0`. **There is no `PASS_DELTA` constant in the repo** — do not rely on a literal "+288".
  The residual risk is *accumulated layout-model drift* out to the ~51 KB boundary; the geometry has been checked on
  **one** kernel (`f8f8s_pgr2_sia0`). Before enabling fleet-wide, validate `coverN` against emitted offsets on several
  real gfx1250 kernels spanning small/medium/large OptNLL (expect `coverN ∈ {0,2,4}` respectively).
- **`Alpha == 1` (§16.10) is necessary, not sufficient** for the OptNLL-taken path (edge/tail tiles can still take the
  general store). It remains a safe *hint* predicate; §16.10 stays a D2 record, not a blocker for §16.

**Fleet dry-run (2900 kernels, `/data0/geotseng/comparison_output`).** Applying the §16 formula to the measured
`label_TailLoopBeginL` offset of every kernel (all 2900 have it; 0 missing, 0 parse fails) gives the `coverN`
distribution: **0 → 2576 (88.8%)**, 1 → 64, 2 → 44, 3 → 41, **4 → 175 (6.0%)** (`armN = 6` for 92.6%). Spot-checks
match: `bbs_pgr2_sia0 = 32704 → coverN 1`; large-OptNLL `f8f8s`/f64 kernels → `coverN 4` with the `armFloor` clamp
binding (162 kernels). Versus the shipped fixed `coverN = 2`, §16 differs on **98.5%** of kernels — **over-covering
~91%** (the 88.8% that should be 0 plus the 1/2-vs-2 cases) and **under-covering ~7.4%** (the coverN 3/4 kernels the
fixed 2 strands). This is the core empirical justification for a computed width: the fixed value is simultaneously too
big for the common case and too small for the OptNLL tail.

- **NoTailLoop fallback is UNVALIDATED on this fleet (doc gap acknowledged).** All 2900 kernels have
  `label_TailLoopBeginL`, so §16.3's fallback #1 (`OptNLL_End`) and #2 (`GW_*`) and the "all-fallbacks-miss ⇒ coverN=0"
  no-op are exercised by **zero** real kernels here (this set never hits `NoTailLoop`, consistent with
  `ASEM ≤ 128 < DepthU = 256`). Before relying on the fallback chain, construct a synthetic `NoTailLoop`
  (`ASEM % DepthU == 0`) kernel — covering both `NoTailLoop && isOptNLL → OptNLL_End` and `NoTailLoop && !isOptNLL → GW_*` —
  and validate it.
- **§15↔§16 offset frame mismatch for `f8f8s_pgr2_sia0` (annotate, don't panic).** §15 (pre-cover-extend measurement)
  cites `GW_B0_OptNLL_MB ≈ 38196` with `STINKY_TOTAL_INST_BYTES = 375200`; §16.1 cites `20504` with objdump total
  `413264`. These are **different builds / coordinate frames** (§15 = in-container final-layout dump; §16.1 = the parsed
  objdump in `optnll_cache.pkl`), not a contradiction in the algorithm — `coverN` is computed per-kernel in one frame.
  Treat §16.1's objdump numbers as the reference for the worked example; the §15 figures are historical.

### 16.12 Proposal: relax the `coverN` cap for bucket-(b) (DESIGN — no code changed)

Target: the **53 bucket-(b)** kernels (§16.8) whose hot OptNLL fast-path exit is `(49024, 65536]` — real, *recoverable*
straight-line misses that the current `armFloor = 4` (`maxCover = 4`) clamp strands. Buckets (a) and (c) are out of
scope (already covered / fundamentally uncoverable).

**Coverage-vs-arm trade (measured over the 53).** `need = ceil((OptNLL_End − P0)/4096)` per kernel:

| relaxed`armFloor` | `maxCover = 8 − armFloor` | window end      | fully covers             | resident`armN` at max |
| ------------------- | ---------------------------- | --------------- | ------------------------ | ----------------------- |
| 4 (current)         | 4                            | 49024           | 0 / 53                   | 4                       |
| 3                   | 5                            | 53120           | 19 / 53                  | 3                       |
| **2**         | **6**                  | **57216** | **39 / 53 (74 %)** | **2**             |
| 1                   | 7                            | 61312           | 47 / 53                  | 1                       |
| 0                   | 8                            | 65408           | 53 / 53                  | 0 (degenerate arm)      |

**Recommendation: `armFloor = 2` (`maxCover = 6`).** It recovers **39/53** while keeping the resident deep arm at
`armN ≥ 2` (≥ 8 KiB). `armFloor ≤ 1` buys only 8 more kernels but collapses the arm to 1/0 blocks — `armN = 0` emits the
degenerate all-scaffold-no-hint burst (§16.7 #5) and drops deep-path prefetch entirely; not worth it as a default.

**Why this is self-targeting (does not regress the common case).** `armFloor` only participates in
`maxCover = 8 − armFloor`, and the clamp binds **only** when `rawCover > 4`. The 489 emits with `coverN ≤ 4` (incl. all
369 `coverN = 0` and the `armN = 6` majority) are **unchanged** — a kernel only trades arm→cover when it *has* a large
OptNLL fast path (`> 49024`), which is precisely when the OptNLL launch is the hot path worth covering. So lowering the
floor to 2 touches only the deep-OptNLL subset, not the deep-general-store (GSU>1 / Case A) kernels the earlier review
worried about.

**Two coordinated changes (both in `SwInstructionPrefetchAbsDynamicPass.cpp`):**

1. **Relax the clamp.** `kCpBoundaryArmFloor` (`:187`) `4 → 2`, so `computeCoverN`'s `maxCover = kPostCpHintBudget − kCpBoundaryArmFloor` (`:290`) becomes `6`. `armN` already derives as `min(kFixedPrefetchN, 8 − coverN)`
   (§16.4 / `:503`), so `coverN = 5 → armN = 3`, `6 → 2` fall out automatically — no other arithmetic change.
2. **KEEP the `label_TailLoopBegin*` boundary — do NOT shrink it to `OptNLL_End`.** ⚠️ **Correction (multi-agent
   verified, §16.2.1): an earlier draft proposed sizing the cover to `label_OptNLL_End`; that is WRONG and is reverted.**
   `label_OptNLL_End` and `label_GSU_3` are the **same byte** (OptNLL_End is a 0-byte fall-through into GSU_3), so
   `[OptNLL_End, TailLoopBeginL)` is **not** slow-path tail-setup — it is the **general (non-OptNLL) path's one-shot
   straight-line code** (last-iteration WMMA drain + tail store/index glue) that **every non-OptNLL launch executes**
   (worked example: 4024 B, `[46664, 50688)`). Sizing the cover to `OptNLL_End` would cover **zero** of it and strand
   the whole general path on cold I-cache misses. The current `computeCoverBoundary` (`:266-280`) preference
   `label_TailLoopBegin* → OptNLL_End → GW_*` is correct: `TailLoopBeginL ≥ OptNLL_End`, so it covers **both** the
   OptNLL body **and** the GSU_3 general-path glue, and stops exactly at the self-warming tail loop. Keep it (a
   defensive `max(OptNLL_End, TailLoopBegin*)` reduces to `TailLoopBegin*` here anyway). Only the clamp (change #1) is
   relaxed; the boundary is unchanged.

**Guards / limits to keep:**

- **Do not spend budget on bucket-(c).** When `OptNLL_End > 65536` (fast path exceeds the whole I-cache), no `coverN`
  can hold it resident with kernel entry; cap `coverN` at `maxCover` as today (or, better, set `coverN = 0` for these to
  avoid evicting for a hint that can never complete the cover, keeping `armN = 6` for the deep path). Detect via
  `OptNLL_End > kSwPrefetchAbsStaticIcacheSizeBytes`.
- **Keep the §16.6 inclusive `floor+1` and the §16.7 #2 `+INSERT_UB` one-shot** — relaxing the cap does not change the
  count formula, only the clamp ceiling.
- **`armN ≥ 1` invariant.** With `armFloor = 2`, `armN ≥ 2`, so the degenerate `armN = 0` burst (§16.7 #5) stays
  unreachable; if a future policy lowers the floor further, skip the arm burst when `armN = 0`.

**Residual after `armFloor = 2`:** 14/53 bucket-(b) kernels (`need ∈ {7,8}`) plus all 30 bucket-(c) remain under-covered
— tracked as the §14.3 "free an arm's budget or add an OptNLL-anchored arm" follow-up. Net: `armFloor = 2` closes 74 %
of the recoverable gap with a 2-block arm reduction that only applies to deep-OptNLL kernels. **Correctness is
unaffected either way** (§16.9: the burst is a hint).

### 16.13 Whole-fleet simulation of Option 4 + Option 1, and the dead-zone guard (DESIGN — no code changed)

Simulating the combined change — **Option 4** (§16.12: `armFloor 4→2`, cover boundary = `label_OptNLL_End`) **+
bucket-c guard** (`OptNLL_End > 65536 ⇒ coverN = 0`, restore arm) **+ Option 1** (§16.10 OptNLL-aware Case-B skip when
`Alpha==1` and OptNLL fully covered; fp32-alpha only) — over all **2856 dumps / 649 emitters** (multi-agent
re-derived, three independent simulations agree exactly).

#### 16.13.1 Uplift / downgrade tally

| outcome                               | count | % of 2856 | note                                                                                                                                                                     |
| ------------------------------------- | ----- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **UPLIFT — skip waste**        | 479   | 16.8 %    | OptNLL already covered; Case-B skip drops the cold`GW_B0_GSU1` burst, `armN` unchanged. Saves ≈ **10.5 MiB** total (2690 hints, ~22.5 KiB/kernel). Zero risk. |
| **UPLIFT — deep-arm restored** | 30    | 1.1 %     | bucket-c guard sets`coverN 4→0`, `armN 4→6` (the old cover never warmed the >64 KiB OptNLL anyway).                                                                |
| **TRADE — favorable**          | 39    | 1.4 %     | `OptNLL_End ≤ 57216`: cover-grow *completes* the OptNLL warm; net uplift on the OptNLL launch, deep-block head still held by the shrunk arm.                        |
| **REAL-RISK downgrade**         | 14    | 0.5 %     | the**dead-zone** — see §16.13.2.                                                                                                                                 |
| **NEUTRAL — no OptNLL**        | 87    | 3.0 %     | `mxf8 / mxf4 / f8f8s_cls`; transform is a no-op.                                                                                                                       |
| **NEUTRAL — non-emitter**      | 2207  | 77.3 %    | no ladder emitted.                                                                                                                                                       |

**With no code change, 0 kernels strictly regress** *only if* the dead-zone guard (§16.13.2) is added; without it, 14
kernels drop `armN 4→2` for a cover that never reaches `OptNLL_End`.

#### 16.13.2 The dead-zone defect and its fix

`armFloor = 2` lets the cover grow to `coverN = 6 → 57216`, but the bucket-c guard only fires at `OptNLL_End > 65536`.
That leaves an **unguarded dead-zone `(57216, 65536]`**: 14 kernels with `OptNLL_End ∈ [58116, 63432]` grow to
`coverN = 6` (still `0.9–6.2 KiB` short of `OptNLL_End`, so the OptNLL warm is **not** completed) yet still sacrifice the
full 8 KiB deep arm (`armN 4→2`). 8 of the 14 are `f64_gfx1250` (which additionally cannot use the Case-B skip).

**Fix — coverage-reaches-boundary guard.** Only grow `coverN` past the original `armFloor = 4` when the grown cover
*actually reaches* `OptNLL_End`:

```
maxCoverGrow = (P0 + 6*4096 >= optEnd) ? 6 : 4     // if even coverN=6 can't reach the boundary, don't grow
coverN_new   = min( floor((optEnd + INSERT_UB - P0)/4096) + 1 , maxCoverGrow )
```

This extends the bucket-c "don't chase an unreachable OptNLL" logic down into the dead-zone. Effect (multi-agent
verified): the **14 REAL-RISK kernels stay at `coverN = 4 / armN = 4` (zero deep-arm loss)** while **all 39 favorable
trades are preserved**. It is strictly better than the blunt `coverN ≤ 5` cap (which would under-cover 18 of the 39).

**Post-fix tally: 509 clean uplift + 39 favorable trade, 0 real downgrade, 2294 neutral.**

#### 16.13.3 Per-family verdict

- **NET-UPLIFT** — all `spmm_*`, `bf16*`, `f16*`, `fp4*`, `f6*` families (dominated by skip-waste with `armN`
  untouched; only isolated deep-bucket-b trades, none systematic).
- **NEUTRAL** — `mxf8_gfx1250`, `mxf4_gfx1250`, `f8f8s_cls_gfx1250` (no OptNLL block).
- **f64_gfx1250 — needs care but not a blocker.** Cannot Case-B skip (alpha is a 2-dword pair, not fp32); 8/32
  sit in the dead-zone. The §16.13.2 guard alone removes those 8 regressions (they keep `armN = 4`); a dedicated f64
  boundary-order re-check (§16.8 nuance) and any f64-specific `armFloor` choice are **deferred follow-ups**, not
  required for the change to be non-regressing.

#### 16.13.4 Implementable spec (for when code lands)

All in `SwInstructionPrefetchAbsDynamicPass.cpp`; each gated so any unmet condition falls back to current behavior:

1. `kCpBoundaryArmFloor 4 → 2` (`:187`).
2. `computeCoverBoundary` (`:266`): prefer `label_OptNLL_End` when present (else `TailLoopBegin* → GW_*`).
3. `computeCoverN` (`:286`): add the bucket-c guard (`optEnd > 65536 ⇒ 0`) **and** the §16.13.2 dead-zone guard
   (`maxCoverGrow = (P0+6*4096 >= optEnd) ? 6 : 4`).
4. Case-B (`:647-648`): OptNLL-aware skip, gated on `emitLadder && has("label_GW_B0_OptNLL_MB") && alphaFp32 && alphaDefined && coverEmitted && optEnd ≤ P0 + coverN*4096`; the `Alpha==1.0f` test is `s_cmp_eq_u32 s[sgprAlpha], 0x3f800000` (fp32 only — **f64 carve-out** via `alphaFp32`).
5. Keep §16.6 `floor+1`, §16.7 #2 `+INSERT_UB`, and the `armN ≥ 1` invariant.

Correctness unaffected throughout (§16.9: `s_prefetch_inst` is a hint). Ships under the §13 hardware gate; measure per
the §16.8 buckets (roccap/perf A/B: skip-waste on OptNLL-taken kernels; `armFloor 4 vs 2` on bucket-b; confirm the
dead-zone 14 hold `armN = 4`).
