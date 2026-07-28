# SwInstructionPrefetchAbs — two-pass plan (one page)

**Status:** Planning only. Full design: [SwPrefetchAbsInsertionPass-Design.md](SwPrefetchAbsInsertionPass-Design.md).

**Goal:** Replace PC-rel `SwInstructionPrefetchRelStaticPass` with **`s_prefetch_inst`** (getpc + label base). Split by **kernel `.text` size** into **static** vs **dynamic** policies (two passes, one enable knob).

---

## Static vs dynamic (policy names)

|                           | **Static policy**                       | **Dynamic policy**                                    |
| ------------------------- | --------------------------------------------- | ----------------------------------------------------------- |
| **Pass**            | `SwInstructionPrefetchAbsStaticPass`        | `SwInstructionPrefetchAbsDynamicPass`                     |
| **When**            | `32640 < totalBytes ≤ 65536`               | `totalBytes > 65536`                                      |
| **Size assumption** | Whole`.text` fits **~64 KiB** I-cache | Kernel**streams**; only a window is hot               |
| **CFG model**       | **Static flow** — one preloop burst OK | **Dynamic flow** — paths, loops, preheaders          |
| **I-cache**         | No**replacement** planning              | **Replacement** (LRU-ish) — why ahead must be capped |
| **Targets**         | One label +**koffsets**                 | **Multiple** labels per `P(k)`                      |
| **Site**            | One**preloop** Do (before branch)       | After prev anchor /**preheader** batch                |
| **Perf tag**        | `abs-static`                                | `abs-dynamic`                                             |

**Replacement** is not a third policy — it is the **mechanism** inside the **dynamic** policy when the kernel is larger than the I-cache.

---

## Gates

| Limit            | Value                      | Meaning                                          |
| ---------------- | -------------------------- | ------------------------------------------------ |
| CP preload       | **32640** (`P(0)`) | No software prefetch below this                  |
| I-cache (design) | **65536**            | **Static** vs **dynamic** pass split |
| Grid             | `P(k) = 32640 + k×4096` | Target anchors (both passes)                     |

| `totalInstBytes` | Static pass    | Dynamic pass   |
| ------------------ | -------------- | -------------- |
| ≤ 32640           | no-op          | no-op          |
| 32641 – 65536     | **runs** | no-op          |
| ≥ 65537           | no-op          | **runs** |

**Enable:** `SwInstructionPrefetchAbs` / `EnableSwInstructionPrefetchAbs` → register **both** passes; PC-rel **off**.

---

## Pass A — static — `SwInstructionPrefetchAbsStaticPass`

**Policy:** **static** — whole kernel fits I-cache; static CFG.

|                  |                                                                                |
| ---------------- | ------------------------------------------------------------------------------ |
| **Target** | One`label_SW_PrefetchAbs_0` at `P(0)` (per-k targets optional)             |
| **Site**   | One`label_Do_SW_PrefetchAbs_entry` — **preloop, before first branch** |
| **Burst**  | One getpc +`s_prefetch_inst` with **koffsets** 0, 4096, 8192, …       |
| **Skip**   | `MaxAheadBytes`, Phi/`accumByte`, preheader batching                       |

```text
entry:  label_Do_* → getpc + prefetch(koffsets) → prolog → branch
        label_SW_PrefetchAbs_0 @ P(0)  …
```

---

## Pass B — dynamic — `SwInstructionPrefetchAbsDynamicPass`

**Policy:** **dynamic** — streaming kernel + I-cache **replacement**; CFG-aware sites.

|                         |                                                                   |
| ----------------------- | ----------------------------------------------------------------- |
| **Target**        | `label_SW_PrefetchAbs_<k>` at every `align128(P(k))`          |
| **Site (linear)** | **After previous anchor**; **k=0** → preloop         |
| **Site (loop)**   | **Preheader** — one Do per loop, batch all k in loop range |
| **Cap**           | `T_k - siteByte ≤ MaxAheadBytes` (default 32768)               |
| **Avoid**         | Site on latch; entry burst for all k                              |

```text
preheader: label_Do_loop* → getpc + (add target_k + prefetch)*
→ body: label_SW_PrefetchAbs_k @ anchors
```

---

## Shared — `SwPrefetchAbsCommon`

Grid, `walkAnchors`, getpc guard, `emitTargetLabel`, `emitBurst`, `computeTotalInstBytes`.

---

## Pipeline

```text
SetMatrixReusePass
→ AccumulateInstructionSizePass
→ SwInstructionPrefetchAbsStaticPass    (static policy)
→ SwInstructionPrefetchAbsDynamicPass   (dynamic policy)
→ [SwInstructionPrefetchRelStaticPass OFF]
→ AccumulateInstructionSizePass
```

---

## Rollout

| Phase            | Deliverable                                              |
| ---------------- | -------------------------------------------------------- |
| **P0**     | Common + ISA + options                                   |
| **P1**     | **Static** pass (first PR)                         |
| **P2**     | **Dynamic**: per-k targets, site after prev anchor |
| **P3**     | **Dynamic**: preheader loop batch                  |
| **P4–P6** | Backend, Tensile, tuning                                 |

**First PR:** P0 + P1; **dynamic** pass stub (no-op if `> 64K`).

---

## Options / files / tests

| Option                                        | Policy         |
| --------------------------------------------- | -------------- |
| `SwInstructionPrefetchAbsStaticMaxKoffsets` | static         |
| `SwInstructionPrefetchAbsMaxAheadBytes`     | dynamic        |
| `SkipSwPrefetchInNaturalLoopBodies`         | dynamic (site) |

Files: `SwInstructionPrefetchAbsStaticPass.*`, `SwInstructionPrefetchAbsDynamicPass.*`, `SwPrefetchAbsCommon.*`.

Debug: `sw_prefetch_abs_static_pass.txt`, `sw_prefetch_abs_dynamic_pass.txt`.

Perf: **PC-rel**, **abs-static**, **abs-dynamic**, **none**.
