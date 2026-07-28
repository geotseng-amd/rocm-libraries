# SwInstructionPrefetchAbs — design proposal (Gfx1250)

**Status:** Implemented — P0 (ISA `s_prefetch_inst`), P1 (`SwInstructionPrefetchAbsStaticPass`: single-label + koffset entry burst, bare-label + temp-SGPR address → **3 SGPRs**), P4 (Gfx1250 pipeline wiring + PC-rel mutual exclusion), P5 (Tensile YAML `SwInstructionPrefetchAbs` + auto-allocated 3 SGPRs: even base pair + scratch). **Not yet implemented:** P2/P3 `SwInstructionPrefetchAbsDynamicPass` (>64 KiB) — stub no-ops with a debug log. Static pass verified by unit tests; end-to-end Tensile build/numeric validation pending. (A 2-SGPR `@rel32` address variant was tried and reverted.) See §16.5.

**Related:** [StinkyTofu-Prefetch-Passes-Report.md](StinkyTofu-Prefetch-Passes-Report.md) (PC-rel grid pass, sizing, pipeline). **Two-pass plan (one page):** [SwPrefetchAbs-TwoPass-Plan.md](SwPrefetchAbs-TwoPass-Plan.md). **PC-rel dynamic (Phi-max vs layout, CFG accumulate):** [SwInstructionPrefetchRelDynamicPass-Design.md](SwInstructionPrefetchRelDynamicPass-Design.md).

---

## Summary

**Replace** **`SwInstructionPrefetchRelStaticPass`** (PC-rel grid) with **`SwInstructionPrefetchAbsStaticPass`** / **`SwInstructionPrefetchAbsDynamicPass`** (two-pass split, §16.2), emitting **`s_prefetch_inst`** with a label-fixed base (getpc + bare-label PC-rel reloc; see §5). Same byte grid as today; **site** (`label_Do_SW_PrefetchAbs_<k>`) may sit **earlier** than **target** (`label_SW_PrefetchAbs_<k>`) to hide **issue latency**.

| Grid | |
|------|---|
| `P(k) = 128×255 + k×(32×128) = 32640 + k×4096` | k = 0, 1, 2, … |
| Offsets | 32640, 36736, 40832, 44928, … |

| Byte range | Who prefetches |
|------------|----------------|
| **0 … 32639** | **CP / shader preload only** (`INST_PREF_SIZE` ≤ 255×128). Includes typical **prolog + unroll / hot-loop entry** if it lies in this window. **No** software abs/rel prefetch here. |
| **≥ P(0) = 32640** | Software abs (or legacy PC-rel) at each `P(k)` while `P(k) < totalBytes` |

**Do not** use `regionStart("loopWithPrefetch")` for target placement — only **`align128(P(k))`** via the same whole-kernel walk as PC-rel.

**Labels:** **Two abs passes** by kernel size — see [SwPrefetchAbs-TwoPass-Plan.md](SwPrefetchAbs-TwoPass-Plan.md) and §16. See also §5.1 (single vs multiple targets within a pass).

---

## 0. Problem, solution, and vocabulary

### 0.1 Problem

Large GEMM kernels have **more instruction bytes** than hardware can keep ready at once.

| Layer | Limit | What goes wrong |
|-------|-------|-----------------|
| **CP / shader preload** (`INST_PREF_SIZE`) | **32640 B** max (255×128) at wave start | Bytes **≥ 32640** are not preloaded by the driver |
| **Hardware fetch-ahead** | ~192 B ahead of PC | Negligible vs kernel size |
| **Instruction cache (~64 KiB)** | Finite | Streaming execution **evicts** lines; fetch may stall (**issue latency**) |
| **PC-rel software prefetch (today)** | Grid at **P(k) = 32640 + k×4096** | Hint issued **at the boundary PC** — little room to hide fetch delay before that code runs |

**Unroll / hot loop** that starts **before byte 32640** is expected to be covered by **CP preload only** — software prefetch does not target that range.

**When software prefetch matters:** `totalInstBytes > 32640`, and especially when `totalInstBytes > 64 KiB` (must think about **cache replacement**, not just “prefetch once”).

### 0.2 Solution

**Replace** PC-rel grid prefetch with **absolute** prefetch (`s_prefetch_inst`):

1. Keep the **same grid** `P(k)` as today (one boundary every 4096 B after 32640).
2. At each `P(k)`, place a **target label** on the instruction that owns that byte offset.
3. Run the prefetch **bundle** (getpc + address of target + `s_prefetch_inst`) at a **site** that may be **earlier** in the kernel than the target.
4. Use **issue latency** (instructions executed between site and target) so SQC can fill lines before fetch needs them.

PC-rel and abs are **mutually exclusive** for the same kernel.

### 0.3 Core concepts

```text
Kernel image (bytes)
|---- CP covers 0..32639 ----|---- software P(0), P(1), P(2) ... ----|
                              ^
                              P(0) = 32640

Program order (time) -->
  [entry / prolog]
       label_Do_SW_PrefetchAbs_k     <-- SITE (Do = execute prefetch here)
       s_getpc / s_add / s_prefetch_inst
       ... more instructions ...     <-- issue latency gap
       label_SW_PrefetchAbs_k        <-- TARGET (what address to prefetch)
       ... hot code ...
```

| Term | Meaning |
|------|---------|
| **P(k)** | Global byte offset in the linked kernel: `32640 + k×4096`. Grid index **k = 0, 1, 2, …** |
| **Anchor** | The **instruction** in the IR walk where boundary **P(k)** falls inside its byte span (`globalPcBefore < P(k) ≤ globalPcAfter`). Same rule as `SwInstructionPrefetchRelStaticPass` / `label_SWprefetch_<k>` debug. **Target** label goes **before** this instruction. |
| **Target** | **What** memory (instruction bytes) to prefetch. Fixed at **P(k)** via label **`label_SW_PrefetchAbs_<k>`**. getpc+add uses this label’s address as **base** for `s_prefetch_inst`. |
| **Site** | **Where** the prefetch bundle runs in program order. Label **`label_Do_SW_PrefetchAbs_<k>`** (“Do” = perform prefetch here). May be **before** anchor/target to increase **issue latency**. |
| **Issue latency** | Time / instruction distance between **site** (hint issued) and **target** (fetch PC reaches that code). Goal: enough dynamic work so I-cache lines are present before execution, reducing fetch stalls. Measured as `labelOff[target] − labelOff[site]` bytes or estimated dynamic insn count. **Not** the same as `s_sched_barrier`. |
| **Burst** | One site’s emitted sequence: `s_getpc_b64` + `s_add_u32`/`s_addc_u32` + one or more `s_prefetch_inst`. |
| **k** | Grid step index. Each **k** gets its own target (and usually its own site), while `P(k) < totalBytes`. |

**Anchor vs site vs target (one line each):**

- **Anchor** = geometry (“which insn spans byte P(k)?”) — from layout walk, not a printed label unless coincident with target.
- **Target** = “prefetch **this** address” (label on code at P(k)).
- **Site** = “issue the hint **here**” (label before getpc burst, often earlier than anchor).

### 0.4 Naming map

**Pattern:** `Sw` = software (vs CP hardware). `Instruction` = I-cache, not global/LDS. `Prefetch` = hint. `Abs` = absolute base (`s_prefetch_inst`), not PC-relative (`s_prefetch_inst_pc_rel`).

| Name | Kind | Meaning |
|------|------|---------|
| **`SwInstructionPrefetch`** | Tensile YAML | Enable PC-rel pass → **`EnableSwInstructionPrefetchRelStatic`** (no scratch SGPR) |
| **`SwInstructionPrefetchAbs`** | Tensile YAML | Enable **abs** pass (replaces PC-rel) |
| **`EnableSwInstructionPrefetchRelStatic`** | Module | Run `SwInstructionPrefetchRelStaticPass` |
| **`EnableSwInstructionPrefetchAbs`** | Module | Run `SwInstructionPrefetchAbsStaticPass` (≤64 KiB) / `SwInstructionPrefetchAbsDynamicPass` (>64 KiB) |
| **`SwInstructionPrefetchAbsBaseSgpr`** | Module | Low index of abs base pair, **auto-allocated** in `KernelWriter._initKernel`; −1 = off |
| **`SwInstructionPrefetchRelStaticPass`** | Pass | Inserts `s_prefetch_inst_pc_rel` on grid |
| **`SwInstructionPrefetchAbsStaticPass`** | Pass (static, ≤64 KiB) | Inserts `s_prefetch_inst` + labels; single-label + koffset burst at entry |
| **`SwInstructionPrefetchAbsDynamicPass`** | Pass (dynamic, >64 KiB) | Per-k targets + CFG-aware sites (P2/P3, not yet implemented) |
| **`label_SWprefetch_<k>`** | Debug only (PC-rel) | Pseudo name in `sw_prefetch_pass.txt`; not emitted |
| **`label_SW_PrefetchAbs_<k>`** | Emitted label | **Target** at P(k) |
| **`label_Do_SW_PrefetchAbs_<k>`** | Emitted label | **Site** (prefetch bundle below label) |

**Why `Do` not `To`:** “Do” = execute the prefetch at this label; “To” was ambiguous (direction vs action). Target keeps **`SW_PrefetchAbs`** = the byte region being prefetched.

### 0.5 PC-rel vs abs (one table)

| | PC-rel (old) | Abs (new) |
|---|--------------|-----------|
| Instruction | `s_prefetch_inst_pc_rel` | `s_prefetch_inst` |
| Address | PC + 8 + koffset | getpc + add → **target label** |
| Site | **Same as anchor** (boundary) | **Movable** (`label_Do_*` earlier) |
| Target label | None (debug pseudo only) | **`label_SW_PrefetchAbs_<k>`** |
| Issue latency | Minimal | Tunable via early site |
| SGPRs | 0 (PC-rel) | 3 (even pair + scratch, auto-allocated + freed after entry burst) |

---

## 1. Problem statement

| Mechanism | Coverage |
|-----------|----------|
| **Shader preload** (`INST_PREF_SIZE`) | First **32640 B** max per wave/WGP |
| **Unroll / hot loop at low offset** | Covered by CP if `.text` start + loop entry **< 32640** |
| **PC-rel grid (legacy)** | Software at **P(k) ≥ 32640**; site stuck at boundary PC |
| **Abs (this pass)** | Same **P(k)**; site movable; target = label at boundary |

**Goal:** Software prefetch only for bytes **past CP limit**, with better **issue-latency** control than PC-rel.

---

## 2. Pass identity and naming (canonical)

| Layer | PC-rel (existing) | Absolute (replacement) |
|-------|-------------------|-------------------------|
| **Kernel / YAML** | `SwInstructionPrefetch` | **`SwInstructionPrefetchAbs`** |
| **Module enable** | `EnableSwInstructionPrefetchRelStatic` ← `SwInstructionPrefetch` | **`EnableSwInstructionPrefetchAbs`** |
| **SGPR (pass)** | none | 3 (even pair + scratch) — **auto-allocated** in `KernelWriter._initKernel`, freed after entry burst |
| **Module base SGPR** | — | **`SwInstructionPrefetchAbsBaseSgpr`** (set by Tensile auto-alloc; −1 = off) |
| **Pass (static)** | `SwInstructionPrefetchRelStaticPass` | **`SwInstructionPrefetchAbsStaticPass`** |
| **Pass (dynamic)** | `SwInstructionPrefetchRelDynamicPass` (implemented; CFG-gated) | **`SwInstructionPrefetchAbsDynamicPass`** (P2/P3, not yet implemented) |
| **Factory (static)** | `createSwInstructionPrefetchRelStaticPass` | `createSwInstructionPrefetchAbsStaticPass` |
| **Factory (dynamic)** | `createSwInstructionPrefetchRelDynamicPass` | `createSwInstructionPrefetchAbsDynamicPass` (planned) |
| **Debug target** | `label_SWprefetch_<k>` (pseudo) | **`label_SW_PrefetchAbs_<k>`** |
| **Debug / site label** | — | **`label_Do_SW_PrefetchAbs_<k>`** |

**Mutually exclusive:** when `SwInstructionPrefetchAbs` is on, **`SwInstructionPrefetch` / PC-rel pass is off** (`Gfx1250Backend.cpp` selects abs over PC-rel via `else if`).

**PC-rel enable quirk:** there is **one** PC-rel module flag, `EnableSwInstructionPrefetchRelStatic` (from YAML `SwInstructionPrefetch`). In the current Gfx1250 backend it drives the **`SwInstructionPrefetchRelDynamicPass`** (the static PC-rel pass call is commented out). So "RelStatic" in the flag name is historical; the pass that actually runs is the **PC-rel dynamic** pass.

Implementation files: **`SwInstructionPrefetchAbsStaticPass.{hpp,cpp}`** (static policy, implemented) and **`SwInstructionPrefetchAbsDynamicPass.{hpp,cpp}`** (dynamic policy, planned). The legacy single-file name `SwInstructionPrefetchAbsPass` has been superseded by the two-pass split in §16.2.

---

## 3. Practical policy (canonical)

1. **Enable** only if `totalBytes > P(0)` (32640) — same gate as starting PC-rel grid.
2. **For each k** while `P(k) < totalBytes`:
   - **Target:** `label_SW_PrefetchAbs_<k>` at **`align128(P(k))`** — first real insn with `globalPcBefore ≥ align128(P(k))` (same rule as `insertSwPrefetchLabels` / debug at line ~492 in `SwInstructionPrefetchRelStaticPass.cpp`). **No** `loopWithPrefetch` region override.
   - **Site:** see loop rules below.
3. **Site per k:**
   - Anchor BB **∉** natural loop → insert burst at **anchor** (1:1 PC-rel replacement), or earlier per site policy.
   - Anchor BB **∈** loop → **target** stays at anchor; **one burst per loop** in **preheader** (first non-loop pred of header) targeting all `label_SW_PrefetchAbs_<k>` whose `P(k)` fall in that loop’s byte range (amortize getpc).
4. **Disable** `SwInstructionPrefetchRelStaticPass` when abs is on.
5. **`AccumulateInstructionSizePass`** after all inserts (unchanged).

### 3.1 I-cache size and replacement policy

Assume **~64 KiB** first-level instruction cache (design constant; tune per uarch).

| Condition | Policy |
|-----------|--------|
| **`totalBytes ≤ 64 KiB`** | Entire kernel may stay resident; **no** sophisticated **cache replacement** planning. Still apply **P(0)** gate for software prefetch (CP already handles prefix). |
| **`totalBytes > 64 KiB`** | Software prefetches **compete** for I-cache/SQC with streaming execution — apply **replacement-aware** site policy (§20): avoid clustering all bursts at entry; prefer **preheader** + **per-k** sites; cap **`MAX_AHEAD_BYTES`**; do not re-prefetch every loop iteration. |

Bytes **0–32639** are **not** software-prefetched; unroll/hot loop lying entirely in that range relies on **CP preload only**.

### 3.2 Two regimes: static policy (≤64 KiB kernel) vs dynamic policy (>64 KiB)

**Do not confuse three different “64K” ideas:**

| Constant | Meaning |
|----------|---------|
| **32640 B** (`P(0)`) | CP / shader preload limit — software grid starts here |
| **65536 B** | Design **I-cache** size — **static** vs **dynamic** pass split |
| **Site in first 64 KiB of `.text`** | Often true for preloop **Do** — side effect, not the policy gate |

**Policy gate (static vs dynamic):** `totalInstBytes` (whole kernel), not “site offset &lt; 64 KiB”.

#### Regime A — `totalBytes ≤ 64 KiB` (static / whole kernel)

| Aspect | Policy |
|--------|--------|
| **Mental model** | Whole kernel can stay in I-cache; **no LRU thrashing** planning between regions |
| **CFG** | Treat as **static flow** (like relying on CP for the prefix) |
| **Targets** | **Single** `label_SW_PrefetchAbs_0` at `P(0)` **or** full grid — MVP may still use per-k targets; **site** can use **one label + koffsets** (0, 4096, …) |
| **Site** | **One** `label_Do_*` in **preloop**, **before first hot branch** (entry burst). Correct when **Do runs once** on the path that matters |
| **Goal** | Hide **issue latency** (scalar work between **Do** and fetch) without fighting **replacement** |
| **Avoid** | Still **no** software prefetch for bytes **&lt; 32640**; CP only |

“Continuously issue without stall” = enough **dynamic insns** between **Do** and target, not “zero I-cache misses.” Replacement pressure is low.

#### Regime B — `totalBytes > 64 KiB` (dynamic / streaming)

| Aspect | Policy |
|--------|--------|
| **Mental model** | Only a **~64 KiB window** is “hot” at once; later bytes **evict** earlier lines (assume LRU-ish) |
| **CFG** | **Remaining** kernel (especially loop-carried stream) is **dynamic** — site must respect **paths** and **loops** |
| **Targets** | **Multiple** `label_SW_PrefetchAbs_<k>` — always at **anchor** `align128(P(k))` (layout grid unchanged) |
| **Site** | **Never** on loop **latch** / every iteration; see **§3.3** |
| **Goal** | Prefetch **just before** the window that will be fetched, without **polluting** cache far ahead |

Optional **phase 2** (precise ahead distance): per-BB **`accumByte`** = instruction bytes from kernel entry along a path; at CFG merge **`accumByte = min(incoming)`** (conservative: use the path that has progressed **least**); site only if `target_k - accumByte ≤ 64 KiB` (and ≥ issue-latency min). Simpler **MVP** uses **`MaxAheadBytes`** + preheader batching instead of a full Phi walk. **CFG vs layout:** prefetch hints always use **layout** addresses; `accum*` only schedules **sites** — see **§16.4.1**.

#### What stays the same in both regimes

- **Target** placement: **only** `align128(P(k))` anchor rule (§4).
- **Enable** gate: `totalBytes > 32640`.
- **Loop body:** **target** at anchor inside loop; **site** not in loop body (§10).

---

### 3.3 Site insertion when `totalBytes > 64 KiB` (multiple targets)

**`detectLoops` / `SkipSwPrefetchInNaturalLoopBodies`:** applies to **site (Do)**, **not** to **target** labels.

| Object | In natural loop body? |
|--------|---------------------|
| **`label_SW_PrefetchAbs_<k>`** (target) | **Yes** — must sit at byte anchor `P(k)` |
| **`label_Do_SW_PrefetchAbs_*` + burst** (site) | **No** — do not insert on latch / inner loop BB |

PC-rel optional flag skips **all** insertion while walking loop BBs; abs pass should still **emit targets** in loop BBs and place **sites** outside.

**“Move forward” — two meanings:**

| Meaning | Regime A (≤64K) | Regime B (&gt;64K) |
|---------|-----------------|---------------------|
| Move **site earlier** than anchor (more issue latency) | OK, one early preloop **Do** | OK **if** `T_k - siteByte ≤ MaxAheadBytes` (~64 KiB) |
| **Multiple** early sites for same **k** | Usually unnecessary | **Bad** — duplicate hints → **thrashing** |
| Move site **later** (closer to target) | Wastes abs benefit (PC-rel-like) | Safer for replacement, worse issue latency |

**Rule:** **at most one site per (loop, batch)** or **per k outside loops**; never site on back-edge.

**Algorithm (`choose_site` for k, target `T_k`):**

```text
if anchor BB for T_k is NOT in any natural loop:
  site = earliest insn in BB(anchor) or its non-loop preds such that:
         issue_latency ≥ MIN_ISSUE_LATENCY
     and (totalBytes ≤ 64K  OR  T_k - siteByte ≤ MaxAheadBytes)
  prefer prolog if anchor is far into kernel
else:
  L = innermost loop containing anchor
  siteBB = preheader(L)   // pred of header not in L.body
  merge all k with anchors in L into ONE label_Do_SW_PrefetchAbs_Loop_<id>
  at site: one getpc + for each k in batch: add(T_k) + s_prefetch_inst
```

**Multiple labels, one site (typical loop case):**

```text
preheader:
  label_Do_SW_PrefetchAbs_loop42:
  s_getpc_b64 s[base:base+1]
  ; for each k with P(k) in [loopByteMin, loopByteMax]:
  s_add_i32 sTmp, label_SW_PrefetchAbs_k, 4   ; bare-label PC-rel reloc (not @pc), then add to base pair
  s_prefetch_inst ...
  ; (reuse base pair; reload getpc only if needed between targets)
branch → loop header
  label_SW_PrefetchAbs_k:   ; targets on anchors inside body
  ...
```

**Anti-pattern (your “multiple move forward” thrashing):** entry **Do** prefetches k=0..N, **and** preheader **Do** prefetches same k again, **and** per-anchor sites in prolog — triple hints evict lines before use when **totalBytes &gt; 64K**.

**MVP vs Phi `min(accumByte)`:** MVP = preheader batch + `MaxAheadBytes` + no loop-body sites. Phi/min is optional when profiling shows wrong path picks site too early on one predecessor.

---

## 4. Target label placement (single rule)

**Only rule (no Option A/B/C split):**

```text
T_k = align128(P(k))     // P(k) = 32640 + k * 4096

Find first real instruction (skip PHI/LABEL) with globalPcBefore >= T_k
  (equivalently: globalPcBefore < T_k <= globalPcAfter → label before that insn)

Emit label_SW_PrefetchAbs_<k> immediately before that instruction.
```

**Why:** First byte of kernel image **not fully covered** by CP preload; same anchor PC-rel used today.

**Not used:** `regionStart("loopWithPrefetch")`, `max(32640, loopStart)`, or region-tagged targets.

---

## 5. Site label and prefetch bundle

```asm
label_Do_SW_PrefetchAbs_entry:   ; SITE — entry burst (before any branch)
s_getpc_b64 s[64:65]                          ; s[64:65] = PC of next instruction
s_add_i32  s66, label_SW_PrefetchAbs_0, 4     ; s66 = PC-rel offset to target (+4 getpc corr.)
s_add_u32  s64, s64, s66                       ; low32 of target addr
s_addc_u32 s65, s65, 0                          ; high32 + carry
s_prefetch_inst s[64:65], 0,     null, 0x1f   ; [target, target+4096)   (P(0))
s_prefetch_inst s[64:65], 4096,  null, 0x1f   ; [target+4096, target+8192) (P(1))
s_prefetch_inst s[64:65], 8192,  null, 0x1f   ; …
; s_sched_barrier 0   — see §6 (NOT emitted: pass runs post-schedule)

; … scalar / setup work (issue latency hiding) …

label_SW_PrefetchAbs_0:          ; TARGET — byte offset P(0)
; first instruction at or after 32640
```

**SGPRs:** even-aligned base pair `s[64:65]` + scratch `s66` (= base+2) for the PC-rel offset —
**3 SGPRs total**. Auto-allocated in Tensile, freed after the burst (body reuses → net ~0 pressure).

**Address materialization (rocisa long-branch idiom — bare label + temp SGPR):**
`s_getpc_b64` stores the address of the **next** instruction. `s_add_i32 s66, label, 4` puts the
**bare label** as an operand → the assembler emits a **PC-relative relocation**; `+4` corrects for
getpc pointing one instruction ahead. `s_add_u32`/`s_addc_u32` fold the offset into the pair.
**Do not** write `label@pc+N` — `@pc` is **not a valid AMDGCN relocation variant** (assembler error
`invalid variant 'pc'`). A 2-SGPR `@rel32@lo+4` / `@rel32@hi+12` variant (no temp) was tried and
reverted; this codebase uses the proven bare-label + temp-SGPR idiom (same as `s_getpc`/`s_setpc`
long branches).

**klength via simm5 immediate:** `s_prefetch_inst …, null, 0x1f` →
`length = ((slength=null=0) + (klength imm=31)) & 31 + 1 = 32 lines = 4096 B`. No SGPR for the length.

| Label prefix | Role |
|--------------|------|
| **`label_SW_PrefetchAbs_<k>`** | **Target** — address prefetched (fixed at `P(k)`) |
| **`label_Do_SW_PrefetchAbs_<k>`** | **Site** — where getpc + `s_prefetch_inst` run (movable before target) |

Default **one** `s_prefetch_inst` per **k** (`koffset=k*4096`, `slength=null`, `klength=0x1f` = 4 KiB). See **§5.1** for when to use one label + offsets vs many labels.

---

## 5.1 Single label + offsets vs multiple labels (design choice)

Two ways to extend coverage beyond one 4 KiB `s_prefetch_inst`. They differ in **control-flow** vs **static linear** layout, and in **code size / scalar issue cost**.

### Comparison

| | **Single label + koffsets** | **Multiple labels** (`label_SW_PrefetchAbs_<k>` per `P(k)`) |
|---|------------------------------|-----------------------------------------------------------|
| **Prefetch shape** | **Continuous** byte range from one address: base + 0, +4096, +8192, … | **Grid steps**: one 4 KiB chunk per **P(k)** |
| **Control-flow** | **Static path** — one site runs once; hints apply regardless of which branch is taken later (may prefetch bytes never executed) | **Can follow control-flow** — site/target per anchor; preheader vs loop body; skip paths that never reach `P(k)` |
| **Dynamic path** | Weak — does not naturally attach prefetch to “only if we enter hot loop” | Stronger — **Do** in preheader prefetches loop targets; burst not on every latch iteration |
| **Code / latency** | **Less** — one getpc+add chain, N `s_prefetch_inst` | **More** — repeat getpc+add per **k** (or batched preheader) |
| **Replaces PC-rel grid** | Only if offsets cover all needed bytes from one anchor | **Yes** — 1:1 with `P(0), P(1), …` |

### Pros / cons (summary)

**Single label + offsets**

| Pros | Cons |
|------|------|
| Less code and scalar **issue** cost (one address setup) | **Continuous** prefetch only — must be contiguous from one base |
| Simple prolog burst before long straight-line run | **Static-path** bias — prefetches bytes that might be skipped by a branch |
| Good for one big linear block after P(0) | Does not map cleanly to **multiple** grid boundaries unless offsets duplicate P(1), P(2), … from P(0) only |

**Multiple labels (per P(k))**

| Pros | Cons |
|------|------|
| Prefetch aligned to **layout** (each **anchor** at `P(k)`) | More instructions (getpc + add + prefetch per **k**) |
| Can respect **control-flow** (site in preheader, target on loop header; not inside latch) | Higher **issue latency** cost from extra scalar work |
| Prefetch on **dynamic path** — hints issued only on paths that execute the **Do** site | More labels and pass logic |
| Matches **PC-rel replacement** and non-contiguous kernel layout | |

### Control-flow picture

```text
                    ┌── branch not taken ──► cold / epilog (never hits P(2))
                    │
entry ──► Do_0 ──► prolog ──► branch ──┬── taken ──► preheader ──► Do_loop ──► loop header
                                       │                              │
                                       │                    label_SW_PrefetchAbs_k (target)
                                       │                    (only on path that enters loop)
                                       └── (other path may never need P(k) in loop range)

Single label at entry + offsets 0,4096,8192:
  ──► prefetches ALL those byte ranges even if branch skips the loop (static / wasted hints)
```

### Pass policy (canonical)

| Goal | Choice |
|------|--------|
| **Replace PC-rel grid** (`P(k)` for all k) | **Multiple labels** — default |
| **Optional optimization** | **Single label + offsets** for one **k** only (e.g. contiguous 12 KiB from P(0)) when layout is linear and CFG is simple |
| **Loop body** | **Multiple** targets at `P(k)` in loop; **one Do** per loop in **preheader** (dynamic path, amortize getpc) — not single label at entry for whole kernel when `totalBytes > 64K` |

**MVP:** multiple labels per **k**, one `s_prefetch_inst` per target (`koffset=0`). Single-label multi-offset remains a future knob (e.g. `SwInstructionPrefetchAbsMergeK=false` per site).

### Quick example reference

| k | Multiple labels (default) | Single label + offsets (optional) |
|---|---------------------------|-------------------------------------|
| Coverage | `label_SW_PrefetchAbs_0` @ 32640, `_1` @ 36736, `_2` @ 40832, … | Only `label_SW_PrefetchAbs_0` + `koffset` 0, 4096, 8192 from same base |
| CFG | Preheader **Do** before loop; targets on anchors | One **Do_0** at entry before any branch |

---

## 6. `s_sched_barrier 0` — not required (post-schedule insertion)

| | |
|---|---|
| **What it does** | Limits **compiler / scheduler** reordering of instructions **after** the barrier relative to the prefetch bundle. |
| **What it does *not* do** | Does **not** wait for I-cache fill; does **not** fix SQC latency by itself. |

**Not required for this pass — by pipeline construction.** Tensilelite is the compiler **frontend**; StinkyTofu is the **backend**. The abs prefetch pass runs **after** instruction scheduling is complete (`StinkyDAGSchedulerPass` in the region pass manager, then `SetMatrixReusePass`), on the **final instruction order**, in the outer kernel pass manager (see §7 / `Gfx1250Backend.cpp`). There is **no later reordering pass** that could hoist hot fetch above the prefetch hints before asm emit.

Because insertion happens on the already-scheduled stream:

- Site/target distance set by the pass is **preserved verbatim** into the emitted `.s` — no scheduler can shrink the issue-latency gap afterward.
- A guard against "scheduler moving hot code above the hint" is **structurally unnecessary** — there is no such scheduler downstream of this pass.
- Matches the existing PC-rel pass, which also runs post-schedule and emits **no** barrier.

**Recommendation:** **do not emit `s_sched_barrier`.** The `SwInstructionPrefetchAbsSchedBarrier` option (§16.7) defaults **`false`** and exists only as an escape hatch if a future pipeline reorders instructions *after* prefetch insertion. With the current backend ordering, leave it off.

---

## 7. Pipeline placement

```text
… → SetMatrixReusePass
  → [optional] SwInstructionPrefetchAbsStaticPass   ← static (≤64 KiB); replaces PC-rel
  → [optional] SwInstructionPrefetchAbsDynamicPass  ← dynamic (>64 KiB); not yet implemented
  → [SwInstructionPrefetchRelStaticPass / RelDynamic OFF when abs on]
  → AccumulateInstructionSizePass
```

Whole-kernel only (not inside `loopWithPrefetch` region scheduler). Share **P(k) walk**, getpc window, and anchor search with PC-rel via **`SwPrefetchCommon`** (refactor from `SwInstructionPrefetchRelStaticPass.cpp`).

---

## 8. Module options

| Option | Type | Default | Meaning |
|--------|------|---------|---------|
| `EnableSwInstructionPrefetchAbs` | bool | false | Run abs pass; disable PC-rel |
| `SwInstructionPrefetchAbsBaseSgpr` | int | −1 | Low SGPR of the 3-reg block (even pair + scratch); **auto-allocated by Tensile** (`KernelWriter._initKernel`), not user-set; −1 = off |
| `SwInstructionPrefetchAbsMaxBursts` | int | 1 | `s_prefetch_inst` per k (each ≤ 4 KiB) |
| `SwInstructionPrefetchAbsKoffsetStep` | int | 4096 | Extra koffsets within one target (rare) |
| `SwInstructionPrefetchAbsSchedBarrier` | bool | **false** | Emit `s_sched_barrier 0` after burst. Default off — pass runs post-schedule (§6), so normally unnecessary; escape hatch only. |
| `SkipSwPrefetchInNaturalLoopBodies` | bool | mirror PC-rel | Site not in loop body; target at `P(k)` |
| `SwInstructionPrefetchAbsMinIssueLatencyBytes` | int | tunable | Min `target − site` for early site policy |
| `SwInstructionPrefetchAbsMaxAheadBytes` | int | 32768 | Cap early site when `totalBytes > 64K` |

**Tensile:** `SwInstructionPrefetchAbs: [False]` is the only user knob. The 3 SGPRs (even-aligned base pair + scratch) are **auto-allocated** in `KernelWriter._initKernel` (`checkOutAligned(3, 2)` after the kernarg/preload region — past all hardware live-ins, with an explicit preload-region guard — then freed so the body reuses them; net 0 SGPR cost). No manual `SwPrefetchAbsScratch` knob. **`SwInstructionPrefetch`** unchanged for legacy PC-rel path.

---

## 9. Minimizing issue latency (not “lead time”)

### 9.1 Problem

When the I-cache / SQC is **not full** for the fetch PC, the wave may stall (**issue latency** bubble — often ~1 cycle per miss in tuning, uarch-dependent). `s_prefetch_inst` is a **hint**; lines must be in SQC **before** fetch needs them.

**CP covers 0–32639.** Software abs only helps for **`P(k) ≥ 32640`**.

### 9.2 Abs vs PC-rel for issue latency

| PC-rel at boundary anchor | Abs with early `label_Do_*` site |
|---------------------------|--------------------------------|
| Prefetch issued at **same PC** as boundary | Prefetch issued **earlier**; target still at `P(k)` |
| Minimal static distance before fetch | **Maximize** dynamic instructions between **Do** and **target** labels |

### 9.3 Issue-latency rules

```text
issue_latency_bytes = labelOff[target_k] - (labelOff[site_k] + size(burst))
issue_latency_insns = estimated dynamic insns from site to target (walk or constant)
```

| Goal | Rule |
|------|------|
| Hide SQC fill before fetch | Prefer large **`issue_latency_bytes`** or **`issue_latency_insns`** |
| **`totalBytes ≤ 64K`** | Simple early site OK; limited replacement pressure |
| **`totalBytes > 64K`** | Apply **replacement policy**: don’t prefetch all k from entry; use preheader + per-k sites; cap **`MaxAheadBytes`** |
| Hide scalar prefetch cost | Place burst in **low-issue** prolog, not immediately before dense WMMA/MFMA |

**Default site policy (replacement mode):**

1. **Target** fixed at `P(k)`.
2. **Site:** anchor if BB ∉ loop; else **preheader** (one site per loop for all k in loop range).
3. Optionally move site earlier: `max(entry, P(k) - MaxAheadBytes)` when `totalBytes > 64K`.
4. Debug metric: **`issue_latency_insns`** in `sw_prefetch_abs_pass.txt`.

### 9.4 Anti-patterns

| Anti-pattern | Why |
|--------------|-----|
| Site **after** target | Too late |
| All k bursts at **entry** only when **`.text` > 64K** | Evicts earlier lines before use |
| Burst on **every loop latch** | I-cache pollution |
| Site = target − 1 insn | Same as PC-rel; no issue-latency win |
| Software prefetch **before 32640** | CP’s job; wastes hints |

---

## 10. Loop-aware sites (`detectLoops`)

Reuse `detectLoops` / `findLoopForBB` from `LoopDetection.hpp` (same as `SwInstructionPrefetchRelStaticPass`).

| Anchor BB at `P(k)` | Target `label_SW_PrefetchAbs_<k>` | Site `label_Do_SW_PrefetchAbs_<k>` |
|---------------------|-----------------------------------|-------------------------------------|
| **Not in loop** | Before insn at `P(k)` | At anchor or earlier (issue latency) |
| **In loop body** | Before insn at `P(k)` | **Preheader** (batch all k in loop range) |
| **Loop header** | On header | Preheader |

Unroll loop **entirely below 32640 B** → **no** abs targets for that loop; CP preload only.

---

## 11. High-level pass algorithm

```text
CP_LIMIT = 128 * 255
ICACHE_SIZE = 65536   // design assumption

walk → totalBytes, labelOff, detectLoops()
if totalBytes <= CP_LIMIT: return

for k = 0, 1, … while P(k) < totalBytes:
  T_k = align128(P(k))
  it_T = first real insn with globalPcBefore >= T_k
  insert label_SW_PrefetchAbs_<k> before it_T if missing

  it_S = choose_site(k, it_T, loops, totalBytes > ICACHE_SIZE)
  insert label_Do_SW_PrefetchAbs_<k> before it_S if missing
  insert getpc + add(target label) + s_prefetch_inst×N before it_S
  [optional s_sched_barrier 0 if SwInstructionPrefetchAbsSchedBarrier]

rewalk sizes (getpc redirect)
```

---

## 12. MI400 fetch context (brief)

| Layer | Limit | Software abs? |
|-------|-------|----------------|
| **Shader preload** | 255×128 = **32640 B** | No (driver) |
| **Fetch-ahead** | 3×64 B | No |
| **IS** | 4 KiB/SIMD | No |
| **SQC / I-cache** | ~**64 KiB** (design) | Replacement matters if kernel larger |
| **`s_prefetch_inst`** | 4 KiB/insn | Yes, for **`P(k) ≥ 32640`** |

Branch taken flushes instruction buffer; **preheader** site helps when target is loop header. See prior §22 detail in git history if needed; key gates above are sufficient for implementation.

---

## 13. MVP (superseded by §16 phased plan)

Phase 0 only — see **§16.5** for the full rollout. Original single-pass MVP:

1. Add ISA `s_prefetch_inst`.
2. Gate: `totalBytes > 32640`.
3. Per k: target at `P(k)` only; site = anchor (loop preheader deferred).
4. One `s_prefetch_inst` per k: `null, 0x1f`.
5. **`SwInstructionPrefetchAbsBaseSgpr`** pair; PC-rel **off**.
6. **No** sched barrier by default.
7. No `loopWithPrefetch` target override.

---

## 16. Two-pass abs architecture — **static** vs **dynamic** policy

> **One-page summary:** [SwPrefetchAbs-TwoPass-Plan.md](SwPrefetchAbs-TwoPass-Plan.md) — full detail below.

### 16.0 Policy names: static vs dynamic (not “large” / “replacement”)

| Term | Meaning |
|------|---------|
| **Static policy** | `totalBytes ≤ 64 KiB` — treat kernel as **static flow**; whole `.text` may stay in I-cache; **no** replacement-aware site planning |
| **Dynamic policy** | `totalBytes > 64 KiB` — **dynamic flow** (paths, loops); sites respect CFG; **replacement** (LRU-ish ~64 KiB window) limits how far ahead to prefetch |
| **Replacement** | **Why** dynamic policy caps ahead distance — **not** a separate pass name |

| Pass | Policy | When active |
|------|--------|-------------|
| `SwInstructionPrefetchAbsStaticPass` | **static** | `32640 < totalBytes ≤ 65536` |
| `SwInstructionPrefetchAbsDynamicPass` | **dynamic** | `totalBytes > 65536` |

### 16.1 Proposal (why two passes)

| Single pass with `if (totalBytes …)` | Two passes |
|--------------------------------------|------------|
| One file grows with two policies | **Separate** static vs dynamic site/target policies |
| Easy to mix static + dynamic rules | Static pass never calls preheader batching / `MaxAheadBytes` |
| Harder to A/B perf | Perf tags: **`abs-static`** vs **`abs-dynamic`** |

Both passes share **`SwPrefetchAbsCommon`**. They differ only in **`chooseSite`** and **burst shape**.

**User-visible enable stays one knob:** `SwInstructionPrefetchAbs` / `EnableSwInstructionPrefetchAbs`. Backend registers **both** passes; each no-ops unless enabled and size matches.

### 16.2 Pass names and responsibilities

| Pass | File (proposed) | Runs when | Policy |
|------|-----------------|-----------|--------|
| **`SwInstructionPrefetchAbsStaticPass`** | `SwInstructionPrefetchAbsStaticPass.cpp` | `32640 < totalBytes ≤ 65536` | **Static** — static CFG; preloop burst; single target + koffsets |
| **`SwInstructionPrefetchAbsDynamicPass`** | `SwInstructionPrefetchAbsDynamicPass.cpp` | `totalBytes > 65536` | **Dynamic** — CFG + loops; multiple targets; replacement-aware sites |

**Do not** add a third pass for `≤ 32640` — both abs passes **no-op** (CP only).

*(Avoid naming the second pass “Large” or “Replacement” — size triggers it, but the policy is **dynamic**.)*

### 16.3 Shared infrastructure (`SwPrefetchAbsCommon`)

Extract from `SwInstructionPrefetchRelStaticPass.cpp` + new abs helpers:

| API | Purpose |
|-----|---------|
| `kSwPrefetchFirstGlobalByte`, `kSwPrefetchSpacingBytes` | Grid constants |
| `walkAnchors(func) → vector<Anchor{k, it_T, globalPc, bb}>` | Target placement |
| `instructionIsSGetpcB64`, `getpcForwardWindowGuard` | Same as PC-rel |
| `emitTargetLabel(k, it_T)` | `label_SW_PrefetchAbs_<k>` |
| `emitBurst(site, targetLabels, baseSgpr)` | getpc + add + `s_prefetch_inst` |
| `computeTotalInstBytes(func)` | Gate for which pass runs |

**Analysis / size:** Run **`AccumulateInstructionSizePass` before both abs passes** (pipeline change) so `totalBytes` is authoritative without a duplicate full walk. If that reorder is too invasive for phase 1, static/dynamic passes **first BB walk** computes `totalBytes` once and stores on `PassContext` or module attr `sw.prefetch.abs.totalBytes`.

### 16.4 Policy per pass (canonical)

#### Pass A — `SwInstructionPrefetchAbsStaticPass` (`totalBytes ≤ 64 KiB`)

| Item | Policy |
|------|--------|
| **Mental model** | Static CFG; kernel resident in I-cache |
| **Targets** | **One** `label_SW_PrefetchAbs_0` at `P(0)` **or** per-k targets (debug-friendly: per-k OK) |
| **Site** | **One** `label_Do_SW_PrefetchAbs_entry` in **preloop**, **before first branch** |
| **Burst** | Single getpc + `s_prefetch_inst` with **koffsets** `0, 4096, 8192, …` until `P(k) ≥ totalBytes` **or** per-k prefetches if simpler for MVP |
| **Loop** | No special case required (whole kernel static); still **avoid** site on latch if loop exists (cheap: `detectLoops` skip body for site only) |
| **Site vs anchor** | Prefer **preloop entry**, not “slightly before anchor” (§9 anti-pattern) |
| **Replacement** | **None** — no `MaxAheadBytes`, no Phi/`accumByte` |
| **Sched barrier** | Default **off** |

```text
entry:
  label_Do_SW_PrefetchAbs_entry:
  getpc + add(label_SW_PrefetchAbs_0) + prefetch × N (koffsets)
  … prolog … branch …
  label_SW_PrefetchAbs_0:   ; at P(0)
  …
  label_SW_PrefetchAbs_1:   ; at P(1) if per-k targets
```

#### Pass B — `SwInstructionPrefetchAbsDynamicPass` (`totalBytes > 64 KiB`, **dynamic** policy)

| Item | Policy |
|------|--------|
| **Mental model** | Streaming + **~64 KiB** working set; hints compete with execution |
| **Targets** | **Multiple** `label_SW_PrefetchAbs_<k>` at every `align128(P(k))` |
| **Site (outside loop)** | Default: **just after previous anchor** (`Do` for **k** after target **k−1**); **k=0** → preloop or capped early site |
| **Site (anchor in loop)** | **Preheader** — **one** `label_Do_SW_PrefetchAbs_loop<id>` per natural loop; batch all **k** with anchors in loop byte range |
| **Ahead cap** | `T_k - siteByte ≤ SwInstructionPrefetchAbsMaxAheadBytes` (default 32768–65536) |
| **Loop** | **Target** in body; **site never** on latch (mirror `SkipSwPrefetchInNaturalLoopBodies` for **site only**) |
| **Anti-patterns** | No entry burst for all **k**; no per-iteration site; no duplicate **Do** for same **k** |
| **Phase 2** | Optional `accumByte` + `min` at CFG merges (§3.2) if profiling needs finer ahead control |

```text
preheader:
  label_Do_SW_PrefetchAbs_loop0:
  getpc + (add target_k + prefetch)* for k in loop
→ header
  label_SW_PrefetchAbs_k: … body …
```

##### 16.4.1 CFG vs layout coordinates (`s_prefetch_inst` semantics)

The **dynamic** pass uses **two different coordinate systems**; confusing them causes “missed” prefetch coverage.

| Coordinate system | What it models | Examples in this repo |
|-------------------|----------------|----------------------|
| **CFG / path** | **When** a hint runs and **which paths** execute the **site** (`label_Do_*`). Merge at joins (`max` of predecessors in `SwPrefetchRelCommon.cpp` phase-1 `accumByte`). | `accumByte`, `accumExit`, `accumBeforeGlobal` / `accumAfterGlobal` in dynamic rel debug — **path progress in post–P(0) bytes**, not raw `layoutGlobal`. |
| **Layout (static image)** | **Which instruction bytes** a hint touches. The linker places BBs in a **linear `.text` order**; hardware fetches by **address**, not by graph edge. | `layoutGlobal`, `labelOff[target]`, grid `P(k)` anchors. |

**`s_prefetch_inst` (MI400):** Each instruction is a **hint** to load a **contiguous** range of **instruction memory** from a **scalar base address** (plus `koffset` steps within one encoding). The SQC does **not** interpret the CFG: it only brings lines for **that address range** in the **linked** kernel image. See the architecture instruction set guide for **`s_prefetch_inst`** / **`s_prefetch_inst_pc_rel`** (length / `koffset` / `SCALAR_PREFETCH_EN`). CK smoke: `projects/composablekernel/test/s_prefetch_inst_op/`.

**Implications for the dynamic pass**

1. **Sites** can follow **dynamic control-flow** (preheader, capped ahead, no latch) — *when* the hint runs.
2. **Each prefetch** still reads **layout-contiguous** bytes from the **target** address — *what* is prefetched. It does **not** mean “prefetch the CFG successor block” unless that successor’s code lies **inside** the hinted window in `.text`.
3. **Coverage gaps** appear when the **next hot code** is **far in layout** from the last hinted range: add **another** target / `koffset` / site, **branch-pair** hints (optional, costly), or improve **block layout** (PGO / reorder) so hot successors sit near each other.

**Cross-reference:** Rel-dynamic debug lines (`accum*` vs `layoutGlobal` on the same LABEL) are implemented in `shared/stinkytofu/src/transforms/asm/SwPrefetchRelCommon.cpp` (`debugPrintPhase1PerInstruction`, `cfgAccumBeforeGlobal` / `cfgAccumAfterGlobal`). Abs dynamic pass reuses the **same mental model** for site policy; emission uses **`s_prefetch_inst`** + label base instead of `s_prefetch_inst_pc_rel`.

##### 16.4.2 Verification: dynamic hints vs static instruction range

Each **`s_prefetch_inst`** or **`s_prefetch_inst_pc_rel`** names a **byte range in the linked instruction image** (abs: scalar base plus `koffset` steps; PC-rel: a PC-relative neighborhood from the prefetch instruction’s PC). The hardware brings **layout-contiguous** cache lines into the SQC (typical tuning pattern: on the order of **4 KiB** per instruction with the common immediates used here)—not “the next basic block in the CFG” as an abstract graph object.

**What CFG tracking actually does:** RPO, `accumByte`, preheader **sites**, merges, **`MaxAheadBytes`**, loop avoidance, etc. answer **when** and **where in the program** we **issue** the hint (dynamic semantics of the pass). **What** gets prefetched is still **only** that contiguous slice of `.text` at the encoded base and range.

*Dynamic path tracking controls where/when we emit hints; each hint still only pulls layout-contiguous instruction bytes into the I-cache, not “the CFG successor” unless that successor’s code lies inside that address range in the linked image.*

**Nuance:** Prefetch does **not** stop at basic-block boundaries. It stops at **address range** boundaries. Basic blocks are a compiler concept; the I-cache and scalar prefetch see **linear virtual addresses**.

**Why “misses” or weak patches happen** (often mixed):

- Not enough hints for **layout-distant** hot code (grid, extra targets, stacked `koffset`).
- Hint on a **cold path** (good layout coverage, wrong **path**).
- Hints **too early** and lines **evicted** before use (large kernel / replacement pressure).
- **Layout vs hot-CFG mismatch:** a frequent successor is far in `.text` from the predecessor’s covered window.

**Coverage** for the dynamic pass should be treated as **two orthogonal layers**:

| Layer | Question |
|-------|----------|
| **Layout coverage** | For every byte offset that may execute soon, is there **some** hint whose window (4 KiB or stacked offsets) **contains** it before fetch pressure? |
| **Path coverage** | On every **hot** dynamic path, is there a **site** on that path that issues those hints at the right time (not only on cold paths; not every latch iteration unless intended)? |

Closing layout holes needs **more or smarter layout-aligned hints** and/or **better `.text` order** (PGO / block placement); closing path holes needs **site** policy (§3.2, §10, §20). Neither replaces the other.

**Practical directions** (details elsewhere in this doc or in the PC-rel dynamic design):

- **A.** Keep grid **targets** at `P(k)`; improve **sites** (preheader batch, cap `MaxAheadBytes`, avoid latch) — §3.2, Pass B table in §16.4, §10.
- **B.** **Multi-hint / multi-target** where hot code is farther than one window — §5.1.
- **C.** **Branch-sensitive** prefetch (optional, expensive): different small bursts for taken vs not-taken.
- **D.** **Profile-driven** sites — extra hints only on hot edges.
- **E.** **Improve layout** so frequent successors sit near each other in `.text` (often best ROI).
- **F.** **`accumByte` / merge refinements** — merge rule depends on **which pass** and **which quantity**; see Phi note below. The **invariant** is independent of min vs max.

> **One-line verification:** Even with full dynamic CFG tracking for **sites**, each software prefetch is still “prefetch **this contiguous range** in the **static instruction map**.” It does **not** prefetch “the next BB in the CFG” unless that BB’s instructions lie in that range in layout. Closing coverage gaps means more or smarter **layout-aligned** hints and/or better **`.text` order**, not making one instruction follow CFG edges.

**Phi `min` vs `max`:** §3.2 describes Phi-**`min`** at merges for **conservative ahead-distance** toward targets (abs dynamic site policy). PC-rel dynamic accumulation in [SwInstructionPrefetchRelDynamicPass-Design.md](SwInstructionPrefetchRelDynamicPass-Design.md) uses Phi-**`max`** for **post–P(0) path progress** along merges including latch—a different quantity. The shared invariant is: **hints always name layout-contiguous instruction bytes.**

**Discussion provenance (not in git):** An extended Cursor agent walk-through of this model (including Rel-dynamic, grid, and dual-offset ideas) is in session `99f44525-6ef4-414b-9c87-a500c7f1fd52` — JSONL: `/home/geotseng/.cursor/projects/home-geotseng-workspace-fork-rocm-libraries/agent-transcripts/99f44525-6ef4-414b-9c87-a500c7f1fd52/99f44525-6ef4-414b-9c87-a500c7f1fd52.jsonl`. On other machines, look under your workspace’s `.cursor/projects/.../agent-transcripts/<uuid>.jsonl`.

### 16.5 Implementation plan (phases)

| Phase | Deliverable | Pass(es) | Notes |
|-------|-------------|----------|-------|
| **P0** | `SwPrefetchAbsCommon` + ISA `s_prefetch_inst` + module options | — | Refactor from `SwInstructionPrefetchRelStaticPass.cpp` |
| **P1** | **Static pass** only | `SwInstructionPrefetchAbsStaticPass` | Preloop single site + single target + koffsets; gate `32640 < total ≤ 65536` |
| **P2** | **Dynamic pass** | `SwInstructionPrefetchAbsDynamicPass` | Per-k targets; site after prev anchor; `k=0` preloop; no loop batch yet |
| **P3** | Loop batching | Dynamic pass | `detectLoops` + preheader **Do**; site not in body |
| **P4** | Pipeline + exclusivity | Gfx1250 backend | `AccumulateInstructionSize` before abs; PC-rel off when abs on; debug `sw_prefetch_abs_static.txt` / `sw_prefetch_abs_dynamic.txt` |
| **P5** | Tensile | `KernelWriter` | `SwInstructionPrefetchAbs` YAML knob + **auto-allocated** 3 SGPRs (even base pair + scratch) (done: `_initKernel` `checkOutAligned(3,2)` reserve-after-kernarg + preload guard + free-after-entry) |
| **P6** | Tuning / optional | Dynamic pass | `MaxAheadBytes`, `accumByte` Phi, sched barrier flag |

**Dependency graph:**

```text
P0 (common + ISA)
 ├── P1 static pass  ──┐
 └── P2 dynamic pass ──┼── P3 loops ── P4 pipeline ── P5 Tensile ── P6 tune
```

**MVP for first PR:** **P0 + P1** (static pass only) + stub **dynamic** pass that **no-ops** with debug log “kernel > 64K, dynamic pass not implemented”. Second PR: **P2–P3**.

### 16.6 Pipeline (target)

```text
… → SetMatrixReusePass
  → AccumulateInstructionSizePass          ← moved up (totalBytes known)
  → SwInstructionPrefetchAbsStaticPass    ← static policy; no-op if total > 64K or ≤ 32640
  → SwInstructionPrefetchAbsDynamicPass   ← dynamic policy; no-op if total ≤ 64K or ≤ 32640
  → [SwInstructionPrefetchRelStaticPass OFF]
  → AccumulateInstructionSizePass        ← re-run if IR size changed (existing pattern)
```

If double accumulate is unacceptable, **one** accumulate after both passes only, and static/dynamic passes use **internal** `computeTotalInstBytes` in P0 only.

### 16.7 Module / debug options

| Option | Applies to | Default |
|--------|------------|---------|
| `EnableSwInstructionPrefetchAbs` | Both | false |
| `SwInstructionPrefetchAbsBaseSgpr` | Both | −1 (auto-allocated by Tensile; not user-set) |
| `SwInstructionPrefetchAbsStaticMaxKoffsets` | Static | auto (cover all k with one base) |
| `SwInstructionPrefetchAbsMaxAheadBytes` | Dynamic | 32768 |
| `SwInstructionPrefetchAbsSchedBarrier` | Both | false |
| `SkipSwPrefetchInNaturalLoopBodies` | Dynamic (site) | true recommended |
| Debug path | Static | `sw_prefetch_abs_static_pass.txt` |
| Debug path | Dynamic | `sw_prefetch_abs_dynamic_pass.txt` |

### 16.8 Testing matrix

| `totalBytes` | Static pass | Dynamic pass | Expected |
|--------------|-------------|--------------|----------|
| ≤ 32640 | no-op | no-op | CP only |
| 32641 – 65536 | **runs** | no-op | Static policy: one preloop Do + prefetches |
| > 65536 | no-op | **runs** | Dynamic policy: per-k targets + CFG-aware sites |
| Boundary 65536/65537 | unit test both gates | | |

Perf: compare **PC-rel**, **abs-static**, **abs-dynamic**, **none** on representative hipBLASLt kernels in each bucket.

### 16.9 File map (updated)

| Path | Role |
|------|------|
| `SwInstructionPrefetchAbsStaticPass.{hpp,cpp}` | Pass A — **static** policy |
| `SwInstructionPrefetchAbsDynamicPass.{hpp,cpp}` | Pass B — **dynamic** policy |
| `SwPrefetchAbsCommon.{hpp,cpp}` | Shared walk + emit |
| `Gfx1250Backend.cpp` | Register both; mutual exclusion with PC-rel |
| `Module.hpp` | Options §16.7 |
| `SwPrefetchAbsInsertionPass-Design.md` | This doc |

### 16.10 Decision log

| Decision | Choice |
|----------|--------|
| Two passes vs one pass + branches | **Two passes** (user request; clearer policies) |
| Who computes `totalBytes` | Prefer **early AccumulateInstructionSize**; fallback internal walk in P0 |
| Static policy targets | **Single label + koffsets** (minimal code); per-k targets optional |
| Dynamic policy default site | **After previous anchor**; loops → **preheader** |
| YAML | **One** `SwInstructionPrefetchAbs` enables **both** passes |
| Address materialization | **Bare label + temp SGPR** (3 SGPRs), the proven rocisa long-branch idiom. The 2-SGPR `@rel32@lo/@hi` form was tried and reverted — kept as a **future optimization** (§17.1), not current behavior |

---

## 14. Testing

| Level | Check |
|-------|--------|
| Unit | getpc + prefetches; `P(k)` labels; kernel ≤32640 no-op |
| Integration | `.s` shows `label_Do_*`, `label_SW_PrefetchAbs_*`, `s_prefetch_inst` |
| Perf | abs vs PC-rel vs neither; kernels **<64K** and **>64K** `.text` |
| Correctness | Hint only; padding + total bytes still valid |

---

## 15. File map

See **§16.9** for the canonical two-pass file map. The legacy single-file name `SwInstructionPrefetchAbsPass` has been superseded; use `SwInstructionPrefetchAbsStaticPass` (implemented) and `SwInstructionPrefetchAbsDynamicPass` (planned).

---

## 17. Future optimization directions

> Ideas evaluated or deferred. **None of these are in the current implementation** — they are recorded so the next iteration does not have to rediscover them. Current behavior is unchanged.

### 17.1 2-SGPR `@rel32` address materialization (deferred)

**Current (implemented):** the address of `label_SW_PrefetchAbs_0` is built with the rocisa
**bare-label + temp-SGPR** idiom — **3 SGPRs** (even pair `s[base:base+1]` + scratch `s[base+2]`):

```asm
s_getpc_b64 s[base:base+1]
s_add_i32   s[base+2], label_SW_PrefetchAbs_0, 4   ; offset into scratch
s_add_u32   s[base],   s[base],   s[base+2]
s_addc_u32  s[base+1], s[base+1], 0
```

**Candidate optimization (2 SGPRs, no scratch):** encode the PC-relative offset directly in the
add immediates via the `@rel32` relocation — the canonical amdhsa form LLVM itself emits:

```asm
s_getpc_b64 s[base:base+1]
s_add_u32   s[base],   s[base],   label_SW_PrefetchAbs_0@rel32@lo+4
s_addc_u32  s[base+1], s[base+1], label_SW_PrefetchAbs_0@rel32@hi+12
```

| | Current (temp idiom) | Candidate (`@rel32`) |
|---|---|---|
| SGPRs | 3 (pair + scratch) | **2 (pair only)** |
| Address instrs | getpc + `s_add_i32` + `s_add_u32` + `s_addc_u32` | getpc + `s_add_u32` + `s_addc_u32` |
| Offset lives in | scratch SGPR | instruction immediate (relocation) |
| Final `s[base:base+1]` | `address(label)` | `address(label)` — **same** |

`@rel32@lo+4` / `@rel32@hi+12` are valid AMDGCN relocation variants (`R_AMDGPU_REL32_LO` / `_HI`);
`@pc` is **not** valid. The `+12` HI addend accounts for the LO add's 32-bit literal pushing the HI
reloc field to `getpc_PC + 12` (LLVM fix D86938). For an in-kernel forward target (always the case
here) the two forms compute the **identical** address.

**Why deferred:** it was applied, then reverted (the surrounding perf investigation flagged the
2-SGPR build; the regression was **not** isolated to this change — see §17.2 — but the temp idiom is
the proven path, so it was kept). Net SGPR pressure difference is ~0 anyway because the base is
**freed after the entry burst** (the body reuses it). Revisit `@rel32` only if reserved-SGPR count
(not pressure) becomes a constraint, and gate it behind a build/numeric verification on amdhsa
(disassemble the `.o`, confirm the prefetch base resolves to `address(label_SW_PrefetchAbs_0)`).

### 17.2 Entry-burst replacement awareness (the real perf lever)

The static pass issues **one burst at entry that prefetches the whole post-CP window** (N capped to
`(I-cache − P(0)) / 4096`). On kernels **> 64 KiB** (where the static `≤ 64 KiB` gate is currently
disabled for testing) this can **thrash**: prefetching ~32 KiB ahead at entry competes with the
demand-fetch of the imminent prolog/loop and can evict soon-to-run lines (§3.3 / §9.4 anti-patterns).
This — not the SGPR count — is the likely dominant perf factor. Directions:

- **Re-enable the `> 64 KiB` no-op gate** so large kernels fall to the (future) dynamic pass instead
  of the naive entry burst.
- **Dynamic pass (P2/P3):** movable / preheader sites, `MaxAheadBytes` cap, per-k targets, no entry
  flood — the proper fix for streaming kernels.
- **Smaller / tunable N** or a later site for the static burst, to reduce startup fetch contention.

---

<!-- Design doc: abs replaces PC-rel at P(k)≥32640; CP covers prefix including unroll loop if <32640. -->
