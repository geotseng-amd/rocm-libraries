---
title: "PR #9317 Reviewer Report — gfx1250 SW Instruction Prefetch"
tags: [rocm, hipblaslt, stinkytofu, gfx1250, code-review]
description: Reviewer-friendly, multi-agent review report for ROCm/rocm-libraries PR #9317
robots: noindex, nofollow
lang: en
---

# PR #9317 — Reviewer Report
## `feat(stinkytofu): add instruction prefetch for gfx1250 (relative & absolute)`

> :memo: **Meta** — OPEN → `ROCm:develop` · **42 files, +5,261 / −865, 8 commits** · Author `geotseng-amd` · JIRA `AIHPBLAS-3621`
> Synthesized from a **3-agent review** (architecture / correctness-risk / tests-perf), each verified against the live branch `users/geotseng/develop-swp-branch` (HEAD `9d596ff`, merge-base `13d842a`).

[TOC]

---

## 1. TL;DR for the reviewer

gfx1250 (MI450) has **no hardware instruction prefetch**. Large kernels keep missing the 128 B-line I-cache (~1000-cycle miss), and the command processor (CP) can only preload the first **32,640 B** of `.text`. This PR makes the compiler insert **software prefetch hints** (`s_prefetch_inst` / `s_prefetch_inst_pc_rel`) on a byte grid `P(k) = 32640 + k·4096`, so fetch runs ahead of the program counter.

:::success
**The single most important fact:** the hints are **pure no-ops** to the hardware (`Gfx1250Instructions.def:540-546`: "No-op when SCALAR_PREFETCH_EN==0; no completion/error status"). A *wrong hint* only wastes I-cache bandwidth — it **cannot** change numerical results.
:::

So the review burden is **not** "are the results correct" but **"does inserting these hints perturb the real code layout / registers?"** That narrows to **three** load-bearing invariants (see §4).

**Headline result:** on B5-1 (bf16 GEMM TN, 256×256, DU128, PGR2, under I-cache pressure), Absolute-Dynamic gives **+59% (91.96 µs → 57.83 µs; 186.8 → 294.9 TFLOP/s)**; cache-resident case shows all variants within ~1% (no harm when there's nothing to hide).

---

## 2. What ships ON by default (read this first)

The headline is "relative & absolute", but the **default production path is Relative**, not Absolute.

| Knob | Default | What runs |
| --- | --- | --- |
| `SwInstructionPrefetch` (PC-rel) | **`True`** | `SwInstructionPrefetchRelDynamicPass` — **on for every gfx1250 kernel** |
| `SwInstructionPrefetchAbs` | `False` | Absolute passes (GEMM-only), opt-in |

:::warning
Reviewers focusing only on the (headline) absolute path are missing the code that actually ships. The Rel path lives mostly in `SwPrefetchRelCommon.cpp` (**+1,338 lines, the single biggest file**) and deserves equal scrutiny.
Also: in `Gfx1250Backend.cpp:234-235` the `RelStatic` factory is **commented out** — the "RelStatic" knob dispatches the **Rel*Dynamic*** pass. Confirm that's intentional (likely, but looks like a stale line).
:::

---

## 3. Architecture at a glance

```
TensileLite knob (SwInstructionPrefetch / …Abs)
   └─ KernelWriter.py: set ModuleOptions + (abs only) reserve 3 SGPRs
        └─ StinkyTofu Gfx1250Backend pass order:
             … → FlattenCallees → [ if Abs: AbsDynamic → AbsStatic
                                    else if Rel: RelDynamic ]
             → AccumulateInstructionSize (recompute byte totals)
                  └─ hint opcodes emitted by StinkyAsmEmitter
```

**Two families × {static, dynamic}:**

- **Relative** (`s_prefetch_inst_pc_rel`) — no SGPRs, full coverage (pure/fused GEMM, StreamK, sparse). Static = layout-offset insertion (kept for `stinkytofu-opt`/tests); **Dynamic = CFG-gated per-BB, the default.**
- **Absolute** (`getpc` + label + `s_prefetch_inst`) — no per-hint transfer delay, uses a **3-SGPR base** auto-allocated in `KernelWriter._initKernel`, freed at `label_MultiGemmEnd` (claimed net ~0). GEMM-only. Static for `32640 < .text ≤ 64 KB` (N≤8); Dynamic for `.text > 64 KB` (CFG-target predicated ladder picking the hot global-write block, 6 hints/arm).

### File map (grouped)

| Group | Key files | +/− | What & why |
| --- | --- | --- | --- |
| ISA / opcodes | `Gfx1250Instructions.def` | +30 | the two hint opcodes + operand shapes |
| | `StinkyAsmEmitter.cpp`, `IRConverter.cpp` | +27 / +5 | `null` slength emission; `.align` awareness |
| **Sizing (load-bearing)** | `InstructionSizeCosting.cpp` | +49 | byte-exact sizing; new **label = +4 B** rule |
| | `AccumulateInstructionSizePass.cpp` | +11 | whole-kernel byte totals |
| Rel passes | `SwPrefetchRelCommon.cpp` | **+1338** | shared engine (grid, getpc window, CFG accum) |
| | `SwInstructionPrefetchRel{Static,Dynamic}Pass.cpp` | +206 / +231 | thin passes; Dynamic is default |
| Abs passes | `SwInstructionPrefetchAbsStaticPass.cpp` | +454 | entry burst, 32640<.text≤64 KB |
| | `SwInstructionPrefetchAbsDynamicPass.cpp` | +720 | 3-arm predicated ladder, >64 KB |
| Wiring | `Gfx1250Backend.cpp` | +30 | pass order + mutual exclusion (abs wins) |
| | `Module.hpp` | +99/−45 | module-option surface |
| TensileLite | `KernelWriter.py` | +84 | 3-SGPR reservation, gfx1250-only, non-StreamK |
| | `KernelWriterAssembly.py` | +8 | deferred SGPR check-in at MGE |
| | `GlobalParameters.py`, `ValidParameters.py` | +16 / +16 | the two knobs + defaults |
| Rename/delete | `SwPrefetchInsertionPass.cpp` | **−734** | split → `SwPrefetchRelCommon` + `RelStaticPass`; scratch-SGPR removed |

---

## 4. Where to spend your review time (prioritized)

Because hints can't corrupt results, effort should concentrate on the **3 things that touch real code/registers**.

### :red_circle: HIGH

:::danger
**H1 — Byte-exact size model is load-bearing.** Every pass re-lays-out the kernel; a few bytes off shifts the P(k) grid, the CP boundary, and label offsets. Verify against `llvm-mc -mcpu=gfx1250`: VALU 4→8 B promotion (`InstructionSizeCosting.cpp:201-234`), literal tail / SMEM early-out (`:294-374`), and the new **"label operand = always +4 B"** rule (`:342-354`).
**Best evidence to request:** the `STINKY_TOTAL_INST_BYTES` marker diffed against `readelf .text` on a real `.o` — it directly proves/refutes the whole model.
:::

:::danger
**H2 — 3-SGPR abs base: lifetime & pressure.** Reserved in `_initKernel` (`KernelWriter.py:9593-9619`), freed at MGE (`KernelWriterAssembly.py:3026-3034`). "Net +0" is true at steady state, but the triple is **held across the whole prolog**, so **peak** SGPR usage rises by 3; overflow only *warns*. Verify (a) near-max-SGPR kernels don't cross `MaxSgpr` and lose occupancy; (b) every path that sets `pendingCheckIn` (on `PreLoop`) actually reaches the MGE emit site, else the triple **leaks (+3 permanent)**. This C++/Python coupling is fragile and undocumented as an invariant.
:::

:::danger
**H3 — getpc chain integrity.** The abs bursts build `getpc → s_add_i32 label,4 → s_add_u32 → s_addc_u32` as a unit; nothing may be interposed. The Rel path has an explicit getpc-window (size 5) guard (`SwPrefetchRelCommon.cpp:461-560`). `s_addc_u32 …, 0` also assumes **the target label is always at a higher address than the getpc** — true today, a latent invariant otherwise.
:::

### :large_orange_circle: MEDIUM

- **M1** — "front-edge Phi-max avoids double-hinting loops" is overstated: natural-loop-body skip is **disabled** (`RelDynamicPass.cpp:194`), and the default per-BB anchor grid gates on raw layout coords, not the Phi-max accum. Harmless (hint-only) but the loop guarantee is weaker than the description, and there is **no loop unit test**.
- **M3** — `coverN` slack uses a hand-computed magic `320 B` bound (`AbsDynamicPass.cpp:188`); if the ladder grows, it can silently under-cover. Consider an assert.
- **M4** — Arm selection reads `sgprGSU`/`sgprBeta` at MGE assuming they're live/waited; GSU mask hardcoded `0x3fff` (KernArgsVersion<3; ≥3 "untested"). Only mis-targets a hint if wrong, but unverified in software.
- **M5** — Mutual exclusion (abs wins; dynamic-before-static; regime split at exactly 65536): agents traced it as correct (at 65536 static emits, dynamic is detector-only, no double-emit) — worth a boundary confirm.

### :white_circle: LOW

Tiny/boundary kernels (≤32640 no-op everywhere), Stream-K double-guarded (Python + C++ name-based bail), non-GEMM abs fallback, `.align` padding accounting.

---

## 5. Tests, coverage & CI — how much to trust

**Strong where it counts (structure):** the 4 new ST pass tests + 2 FileCheck `.stir` cover the emission contract — P(0)=32640 and 64 KB boundaries, the `coverN/armN` formula across regimes, the 3-arm ladder, ordering, entry placement, Stream-K bail. The **Rel byte-exactness is proven** via `sw_instruction_prefetch_rel_static.stir` (exact running totals + operands at P(0), P(2), P(4), P(11)). ST suite: **381/381**.

:::danger
**Two real blind spots — do not take on faith:**
1. **Absolute-address arithmetic is NOT unit-tested** — no FileCheck for the abs passes; abs tests assert counts/labels/order but never the computed `getpc+add` target address. Matches the author's own "abs numeric validation ongoing" caveat.
2. **SGPR alloc / deferred-check-in glue has zero automated coverage at any level** (`KernelWriter.py:9593-9619`, `KernelWriterAssembly.py:3032`).
:::

**Codecov 9.68% patch coverage — mostly benign.** Missing lines concentrate in TensileLite Python glue (`KernelWriter.py` 7.69%, `KernelWriterAssembly.py` 0.00%) that only runs in an end-to-end device build; the pass logic it feeds *is* covered by the C++ suite. Project 76.84% < 80% target is FAIL-but-non-blocking, same cause.

**Tox:** 1 failed / 302 passed / 929 skipped — the failure is `test_config[subtile_bf16_gfx1250_cluster.yaml]`, which the author calls a pre-existing "known issue." **Verify it also fails on `develop`** rather than taking it on faith.

---

## 6. Reviewer verification checklist

- [ ] **H1 evidence:** request `STINKY_TOTAL_INST_BYTES` vs `readelf .text` diff on real gfx1250 kernels.
- [ ] **Abs numeric (acknowledged gap):** build with `SwInstructionPrefetchAbs=True`, disassemble a `>64 KB` 3-arm kernel, confirm `getpc+add/addc` lands at the intended `label_SW_PrefetchAbs_*`.
- [ ] **H2 SGPR:** confirm `absBaseIdx ≥ MaxSgprPreload` assert holds on a preload kernel and the MGE check-in fires exactly once (no leak/double-free).
- [ ] **Perf reproduce:** B5-1 under I-cache pressure ≈ +58/59% for Rel/Abs Dynamic; **confirm cache-resident within ~1%**; Abs Static (+14%) ≈ CP-only shows the dynamic ladder is the real win.
- [ ] **Tox:** confirm `subtile_bf16_gfx1250_cluster.yaml` fails on `develop` too (unrelated).
- [ ] **Mutual exclusion:** a kernel with both knobs `True` must take the abs path only.
- [ ] **Nit:** confirm the commented-out `RelStatic` factory in `Gfx1250Backend.cpp:234` is intentional.

---

## 7. Bottom line

:::info
Well-structured, low-blast-radius change: the design confines nearly all failure modes to **hint quality** (perf), not result correctness, **provided** H1 (size model exact), H2 (SGPR triple never overlaps a live value / never overflows) and H3 (no bytes split a getpc chain) hold. Suggested merge gate: the **H1 byte-diff evidence** + an **assemble-clean + numeric test on gfx1250 for at least one `>64 KB` 3-arm abs kernel** (plus one GSU0/no-beta cover-only kernel) — the only untested correctness-relevant paths, aligned with the author's own "validation ongoing" note.
:::

<small>Report generated from a multi-agent review of the live branch. See the companion canvas for the interactive risk matrix, file-map, and perf chart.</small>
