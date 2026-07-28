---
title: "PR #9317 審查報告 — gfx1250 軟體指令預取"
tags: [rocm, hipblaslt, stinkytofu, gfx1250, code-review]
description: 給 reviewer 的多 agent 審查報告（繁體中文版）— ROCm/rocm-libraries PR #9317
robots: noindex, nofollow
lang: zh-TW
---
# PR #9317 — 審查報告（給 Reviewer）

## `feat(stinkytofu): add instruction prefetch for gfx1250 (relative & absolute)`

> 📝 **基本資訊** — OPEN → `ROCm:develop` · **42 檔案，+5,261 / −865，8 commits** · 作者 `geotseng-amd` · JIRA `AIHPBLAS-3621`
> 本報告由 **3 個 agent 交叉審查**（架構 / 正確性風險 / 測試與效能）彙整，全部對照實際分支 `users/geotseng/develop-swp-branch`（HEAD `9d596ff`，merge-base `13d842a`）驗證。

[toc]

---

## 1. 一句話總結

gfx1250（MI450）**沒有硬體指令預取**。大型 kernel 會不斷 miss 掉 128B cache line 的 I-cache（每次 miss 約 1000 cycle），而 command processor（CP）最多只能預載 `.text` 的前 **32,640 B**。這個 PR 讓編譯器在 byte grid `P(k) = 32640 + k·4096` 上插入 **軟體預取提示**（`s_prefetch_inst` / `s_prefetch_inst_pc_rel`），讓抓取進度跑在 program counter 前面。

:::success
**最關鍵的一件事：** 這些 hint 對硬體而言是**純 no-op**（`Gfx1250Instructions.def:540-546`：「SCALAR_PREFETCH_EN==0 時為 no-op；不回傳完成或錯誤狀態」）。就算 hint 位置**錯了**，也只是浪費 I-cache 頻寬 —— **不會改變數值結果**。
:::

因此審查重點**不是**「結果對不對」，而是「插入這些 hint 會不會擾動真正的程式碼佈局 / 暫存器？」這收斂成 **三個承重不變量**（見 §4）。

**代表性效能：** 在 B5-1（bf16 GEMM TN、256×256、DU128、PGR2、I-cache 壓力下），Absolute-Dynamic 提升 **+59%（91.96 µs → 57.83 µs；186.8 → 294.9 TFLOP/s）**；cache 常駐情境下所有變體差異都在 ~1% 內（沒有可隱藏的延遲時就不會有副作用）。

---

## 2. 預設實際跑的是哪條路（先看這個）

標題寫「relative & absolute」，但**預設上線的是 Relative**，不是 Absolute。

| 旋鈕                                | 預設               | 實際執行                                                                          |
| ----------------------------------- | ------------------ | --------------------------------------------------------------------------------- |
| `SwInstructionPrefetch`（PC-rel） | **`True`** | `SwInstructionPrefetchRelDynamicPass` —— **每顆 gfx1250 kernel 都會跑** |
| `SwInstructionPrefetchAbs`        | `False`          | Absolute passes（僅 GEMM），需手動開啟                                            |

:::warning
只盯著（標題的）absolute 路徑的 reviewer，會漏掉真正上線的程式碼。Rel 路徑主要在 `SwPrefetchRelCommon.cpp`（**+1,338 行，本 PR 最大檔案**），應同等審查。
另外：`Gfx1250Backend.cpp:234-235` 的 `RelStatic` factory 被**註解掉**了 —— 「RelStatic」旋鈕實際派送的是 **Rel*Dynamic*** pass。請確認這是刻意的（很可能是，但看起來像殘留行）。
:::

---

## 3. 架構快覽

```
TensileLite 旋鈕（SwInstructionPrefetch / …Abs）
   └─ KernelWriter.py：設定 ModuleOptions +（僅 abs）保留 3 個 SGPR
        └─ StinkyTofu Gfx1250Backend pass 順序：
             … → FlattenCallees → [ 若 Abs: AbsDynamic → AbsStatic
                                    否則若 Rel: RelDynamic ]
             → AccumulateInstructionSize（重算 byte 總數）
                  └─ 由 StinkyAsmEmitter 發出 hint opcode
```

**兩個家族 × {static, dynamic}：**

- **Relative**（`s_prefetch_inst_pc_rel`）—— 不需 SGPR，覆蓋最廣（純/融合 GEMM、StreamK、sparse）。Static = 依 layout byte offset 插入（保留給 `stinkytofu-opt`/單元測試）；**Dynamic = CFG-gated 逐 BB 插入，為預設。**
- **Absolute**（`getpc` + label + `s_prefetch_inst`）—— 無 per-hint 傳輸延遲，使用**3-SGPR base**，在 `KernelWriter._initKernel` 自動配置、於 `label_MultiGemmEnd` 釋放（宣稱淨增 ~0）。目前僅 GEMM。Static 用於 `32640 < .text ≤ 64 KB`（N≤8）；Dynamic 用於 `.text > 64 KB`（CFG 目標的 predicated ladder，挑執行期會跳進的熱區 global-write block，每臂 6 個 hint）。

### 檔案地圖（分組）

| 分組                       | 主要檔案                                             | +/−            | 內容與目的                                                                 |
| -------------------------- | ---------------------------------------------------- | --------------- | -------------------------------------------------------------------------- |
| ISA / opcode               | `Gfx1250Instructions.def`                          | +30             | 兩個 hint opcode 與運算元格式                                              |
|                            | `StinkyAsmEmitter.cpp`, `IRConverter.cpp`        | +27 / +5        | `null` slength 輸出；`.align` 感知                                     |
| **尺寸模型（承重）** | `InstructionSizeCosting.cpp`                       | +49             | byte 精確計算；新規則**label = 恆 +4 B**                             |
|                            | `AccumulateInstructionSizePass.cpp`                | +11             | 整顆 kernel 的 byte 總數                                                   |
| Rel passes                 | `SwPrefetchRelCommon.cpp`                          | **+1338** | 共用引擎（grid、getpc window、CFG accum）                                  |
|                            | `SwInstructionPrefetchRel{Static,Dynamic}Pass.cpp` | +206 / +231     | 薄封裝；Dynamic 為預設                                                     |
| Abs passes                 | `SwInstructionPrefetchAbsStaticPass.cpp`           | +454            | 入口 burst，32640<.text≤64 KB                                             |
|                            | `SwInstructionPrefetchAbsDynamicPass.cpp`          | +720            | 3-arm predicated ladder，>64 KB                                            |
| 管線接線                   | `Gfx1250Backend.cpp`                               | +30             | pass 順序 + 互斥（abs 優先）                                               |
|                            | `Module.hpp`                                       | +99/−45        | module-option 介面                                                         |
| TensileLite                | `KernelWriter.py`                                  | +84             | 3-SGPR 保留，僅 gfx1250、排除 StreamK                                      |
|                            | `KernelWriterAssembly.py`                          | +8              | 於 MGE 延遲歸還 SGPR                                                       |
|                            | `GlobalParameters.py`, `ValidParameters.py`      | +16 / +16       | 兩個旋鈕與預設值                                                           |
| 改名/刪除                  | `SwPrefetchInsertionPass.cpp`                      | **−734** | 拆分 →`SwPrefetchRelCommon` + `RelStaticPass`；移除 scratch-SGPR 需求 |

---

## 4. 審查火力該集中在哪（依優先序）

因為 hint 不會弄壞結果，火力應集中在**會動到真正程式碼/暫存器的三件事**。

### 🔴 高風險

:::danger
**H1 —— byte 精確的尺寸模型是承重結構。** 每個 pass 都會重新排 kernel 佈局；只要差幾個 byte，P(k) grid、CP 邊界、label offset 全都會偏。請對照 `llvm-mc -mcpu=gfx1250` 驗證：VALU 4→8 B 升級（`InstructionSizeCosting.cpp:201-234`）、literal 尾巴 / SMEM early-out（`:294-374`）、以及新規則 **「label 運算元 = 恆 +4 B」**（`:342-354`）。
**最有力的證據：** 用 `STINKY_TOTAL_INST_BYTES` 標記對 `.o` 的 `readelf .text` 做 diff —— 能直接證明或推翻整個模型。
:::

:::danger
**H2 —— 3-SGPR abs base 的生命週期與壓力。** 在 `_initKernel` 保留（`KernelWriter.py:9593-9619`），於 MGE 釋放（`KernelWriterAssembly.py:3026-3034`）。「淨增 +0」在穩態成立，但這 3 個暫存器**橫跨整個 prolog 都被佔用**，所以**尖峰** SGPR 用量會 +3；溢位時**只會 warning**。請驗證：(a) 接近 SGPR 上限的 kernel 不會越過 `MaxSgpr` 而掉 occupancy；(b) 所有會設定 `pendingCheckIn`（在 `PreLoop`）的路徑，都真的走到 MGE 的歸還點，否則這 3 個暫存器會**永久洩漏（+3）**。這段 C++/Python 耦合脆弱且未以不變量形式明文記錄。
:::

:::danger
**H3 —— getpc chain 完整性。** abs burst 會把 `getpc → s_add_i32 label,4 → s_add_u32 → s_addc_u32` 當成一個整體插入，中間不可被插進其他指令。Rel 路徑有明確的 getpc-window（大小 5）保護（`SwPrefetchRelCommon.cpp:461-560`）。`s_addc_u32 …, 0` 還假設**目標 label 的位址永遠高於 getpc** —— 目前成立，但屬於潛在不變量。
:::

### :large_orange_circle: 中風險

- **M1** —— 「front-edge Phi-max 避免迴圈重複 hint」被高估了：natural-loop-body 跳過其實**被停用**（`RelDynamicPass.cpp:194`），且預設的 per-BB anchor grid 是以原始 layout 座標判斷，而非 Phi-max accum。無害（僅 hint 品質），但保證比描述弱，而且**沒有迴圈的單元測試**。
- **M3** —— `coverN` 的裕度用了手算魔術數 `320 B`（`AbsDynamicPass.cpp:188`）；若 ladder 變大可能悄悄覆蓋不足。建議加 assert。
- **M4** —— arm 選擇在 MGE 讀 `sgprGSU`/`sgprBeta`，假設其為 live/已 wait；GSU mask 寫死 `0x3fff`（KernArgsVersion<3；≥3「未測試」）。錯了也只是打錯 hint，但軟體層未驗證。
- **M5** —— 互斥（abs 優先；dynamic 先於 static；剛好 65536 的 regime 分界）：agent 追過認定正確（65536 時 static 發、dynamic 僅偵測、不重複發），建議做邊界確認。

### ⚪️ 低風險

極小/邊界 kernel（≤32640 各 pass 皆 no-op）、StreamK 雙重防護（Python + C++ 以符號名 bail）、非 GEMM 的 abs fallback、`.align` padding 計算。

---

## 5. 測試、覆蓋率與 CI —— 可信度多少

**結構面很扎實：** 4 個新的 ST pass 測試 + 2 個 FileCheck `.stir` 涵蓋了發射契約 —— P(0)=32640 與 64 KB 邊界、各 regime 的 `coverN/armN` 公式、3-arm ladder、順序、入口放置、StreamK bail。**Rel 的 byte 精確度有被證明**（`sw_instruction_prefetch_rel_static.stir` 驗到 P(0)、P(2)、P(4)、P(11) 的精確累計與運算元）。ST 測試套件：**381/381 通過**。

:::danger
**兩個不能盡信的盲點：**

1. **Absolute 位址算術「沒有」單元測試** —— abs pass 沒有 FileCheck；abs 測試只驗數量/label/順序，從不驗算出來的 `getpc+add` 目標位址。這正對應作者自己的「abs 數值驗證進行中」聲明。
2. **SGPR 配置 / 延遲歸還這段膠水在任何層級都零覆蓋**（`KernelWriter.py:9593-9619`、`KernelWriterAssembly.py:3032`）。
   :::

**Codecov patch 覆蓋率 9.68% —— 多屬良性。** 缺的行集中在 TensileLite Python 膠水（`KernelWriter.py` 7.69%、`KernelWriterAssembly.py` 0.00%），這些只在完整裝置建置時才會跑；它們餵進去的 pass 邏輯**已被 C++ 測試涵蓋**。專案 76.84% < 80% 目標為 FAIL 但不擋合併，成因相同。

**Tox：** 1 失敗 / 302 通過 / 929 略過 —— 失敗是 `test_config[subtile_bf16_gfx1250_cluster.yaml]`，作者稱為既有「known issue」。**請自行確認它在 `develop` 上也會失敗**，不要只憑作者說法。

---

## 6. Reviewer 驗證清單

- [ ] **H1 證據：** 索取 `STINKY_TOTAL_INST_BYTES` 對真實 gfx1250 kernel 的 `readelf .text` diff。
- [ ] **Abs 數值（已知缺口）：** 以 `SwInstructionPrefetchAbs=True` 建置，反組譯一顆 `>64 KB` 的 3-arm kernel，確認 `getpc+add/addc` 落在預期的 `label_SW_PrefetchAbs_*`。
- [ ] **H2 SGPR：** 在 preload kernel 上確認 `absBaseIdx ≥ MaxSgprPreload` assert 成立，且 MGE 歸還恰好觸發一次（無洩漏/重複釋放）。
- [ ] **效能重現：** B5-1 在 I-cache 壓力下 Rel/Abs Dynamic ≈ +58/59%；**確認 cache 常駐時退化在 ~1% 內**；Abs Static（+14%）≈ CP-only，證明 dynamic ladder 才是勝負手。
- [ ] **Tox：** 確認 `subtile_bf16_gfx1250_cluster.yaml` 在 `develop` 上同樣失敗（與本 PR 無關）。
- [ ] **互斥：** 兩個旋鈕都設 `True` 的 kernel 必須只走 abs 路徑。
- [ ] **小瑕疵：** 確認 `Gfx1250Backend.cpp:234` 被註解掉的 `RelStatic` factory 是刻意的。

---

## 7. 結論

:::info
結構良好、影響半徑小：此設計把幾乎所有失效模式都限縮在 **hint 品質**（效能），而非結果正確性 —— **前提是** H1（尺寸模型精確）、H2（3-SGPR 永不覆蓋 live 值、永不溢位）與 H3（插入的 byte 不切斷 getpc chain）都成立。建議合併門檻：**H1 的 byte-diff 證據** + 針對至少一顆 `>64 KB` 3-arm abs kernel 的 **組譯乾淨 + gfx1250 數值測試**（外加一顆 GSU0/no-beta 的 cover-only kernel）—— 這些正是唯一未被測試涵蓋、又與正確性相關的路徑，也對齊作者自己的「驗證進行中」註記。
:::

<small>本報告由對實際分支的多 agent 審查產生。互動式風險矩陣、檔案地圖與效能圖請見隨附 canvas。</small>
