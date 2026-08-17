---
status: shipped # proposed → approved → in-progress → shipped
target: 并行转换（多进程分块重建）
date: 2026-08-17
---

# 并行转换（多进程分块重建）

## Background

[001 核心繁转简](./001-pdf-t2s-core.md) 与 [002 体积优化](./002-output-size-optimization.md) 的转换是**逐页串行**的：智谱 504 页约 153s、MiniMax 716 页约 210s（8 核 Mac 实测）。页面间处理无逻辑依赖（每页独立 redact + 重绘），理论上可以并行。

**调研结论（2026-08-17）**：

- PyMuPDF 官方明确**不支持多线程**（全局 context 共享，加锁即串行化，无收益）——[官方立场](https://github.com/pymupdf/PyMuPDF/issues/107)
- 官方推荐的并行模式是**多进程分页范围**（每进程独立打开文档），预期 2x+ 加速——[官方 multiprocessing 指南](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html)
- `insert_pdf()` 合并分块输出后，**`garbage=4 + deflate_fonts=True`** 合并块间重复字体流（每块嵌入同一全局子集；`garbage=2` 合并行为不稳定，`deflate_fonts` 使流比较快 ~7 倍——实测 504 页 ~6s，ZR0 从 8 份合并为 1 份）——[PyMuPDF PDF 操作文档](https://deepwiki.com/pymupdf/PyMuPDF/3.5-pdf-manipulation-operations)

**语言分析（为什么不用 C/C++/Go/Rust 重写）**：cProfile 实测 30 页 17.9s 中，~80% 耗时在 MuPDF C 内核（字体注册 3.15s、redact 应用 3.4s、注释 O(n²) 遍历 2.9s 等）与 OpenCC（本身是 C++）——**与语言无关**；仅 ~20%（PyMuPDF Python 层的重复字体检查 3.6s）可经语言重写消除，但需从零重写全部 PDF 处理逻辑（MuPDF C API 集成、竖排、字体选择、收缩），收益 1.2x 不匹配成本。**结论：不换语言，并行化是主要加速手段。**

**可行性实验（智谱 504 页，8 核，2026-08-17）**：

| 指标 | 串行 | 分块并行（8 进程） | 提升 |
|---|---|---|---|
| 耗时 | 153s | 88.2s | 1.74x |
| 体积 | 16.6MB | 11.4MB | -31%（块间字体去重） |
| 文本一致性 | — | 差异 0/504 | ✅ |
| 字体保真（抽样） | — | 复用 100% | ✅ |

**正式实现验收（含手动字体去重，2026-08-17）**：

| 文档 | 串行 | 并行 | 加速 | 体积（串行→并行） |
|---|---|---|---|---|
| 智谱 504 页 | 153s | 68s | 2.25x | 15.8 → 14.3MB |
| MiniMax 716 页 | 210s | 88s | 2.39x | 14.6 → 14.0MB |

五维验证全部通过，内容级与串行产物完全一致（改字 span 22579 / 25430 相同）。

## Goal (one sentence)

在不改变输出正确性（001 五维验证全过）的前提下，通过多进程分块重建 + 合并，把单文档转换提速 ≥ 2x（实测智谱 2.25x / MiniMax 2.39x），并保持输出体积不增（实测更小）。

## Scope (what it does)

1. `scripts/pdf_t2s.py` 新增 `process_pdf_parallel(src, cc, fonts, out, jobs)` 三阶段流水线：
   - **阶段 1（串行）**：`load_doc_fonts` + `collect_needed_chars`（全文档字符集是子集化的前提，必须全局一次）+ `subset_system_fonts` → 子集映射表写临时文件供子进程读取
   - **阶段 2（并行）**：`multiprocessing.Pool(jobs)` 分块执行 `_block_worker`——每进程独立打开原 PDF、`load_doc_fonts`、块内逐页 `rebuild_page`（复用同一 `pick_font`/子集路径）、`doc.select(块内页)` 裁剪、保存块文件（garbage=3）
   - **阶段 3（串行）**：`insert_pdf` 逐块合并（annots/links=False）→ `dedup_fonts` 手动去重 → `purge_unused_fonts` → `doc.save(out, garbage=3, deflate=True, deflate_fonts=True)`
2. **降级规则**：页数 < 50 或 jobs=1 → 走现有串行 `process_pdf`（避免分块固定开销，小文档串行更快）
3. **CLI**：`main()` 默认自动并行（`jobs = min(cpu_count, 页数)`，页数 < 50 自动降级串行）；`--jobs N` 显式覆盖（1 = 强制串行）
4. **失败处理**：任一块失败 → 整体报错退出（非零），临时块文件/子集目录由 `tempfile` 清理（mkdtemp 每进程独立已保证）
5. **确定性**：块划分固定（页数均分，末块取余）、块内页序固定、合并顺序固定 → 同输入同输出
6. 验收：两份招股书并行产物全量五维验证（001 标准 1-8）+ 加速与体积报告

## 分块与合并细节

- **块数**：`jobs = min(cpu_count, 页数)`；块边界 = 页数均分（`per = 页数 // jobs`，末块含余数）
- **子进程间共享**：子集字体文件（阶段 1 产出，路径经映射表文件传递）、原 PDF（各自 open）、ORIG_FONT_DIR/SUBSET_DIR（mkdtemp 每进程独立，路径字符串传递）
- **worker 必须模块级可 pickle**（macOS spawn 模式）；worker 内 `load_fonts`（~0.1s）与 `load_doc_fonts`（~0.7s）每块一次，可接受
- **块输出体积**：每块 ~63 页 ≈ 1-2MB（含该块用到的字体子集）
- **合并去重**：`insert_pdf` 合并后各块的相同字体（同 PS 名同内容）以不同 xref 存在；`dedup_fonts` 按 PS 名+流哈希统一 xref（O(n)，`garbage=4` 的流合并是 O(n²) 实测 45s+，哈希去重 <1s）

## 验收标准

对两份验收文档（504 页 / 716 页）的**并行产物**全量执行：

1. 五维验证（001 标准 1-8）全部通过，与串行产物结果一致（复用率 ≥ 99%、像素 0 异常、竖排 0 缺失、繁体残留 0、内容级非白名单差异 0）
2. 加速比：单文档耗时相对串行 ≥ 2x（8 核实测智谱 2.25x / MiniMax 2.39x）
3. 输出体积 ≤ 串行产物（实测更小，块间字体去重）
4. 可重复执行：同输入同输出（并行模式自重复——页数、文本、字体、体积一致；字节级确定性由 garbage 回收行为决定，不设门槛）
5. 降级路径：页数 < 50 走串行，行为与 001/002 一致；`--jobs 1` 强制串行
6. 失败路径：模拟块失败（损坏输入）→ 非零退出且不产生半成品输出文件

## Risks

- **块负载不均**（表格/竖排密集页耗时高）：8 块实测 36.6s vs 理论 22s（8 核）——可接受；后续可优化为页粒度池调度（`imap_unordered`）
- **内存峰值**：8 进程 × ~300MB ≈ 2-3GB（README 注明）；`jobs` 可被用户调低
- **macOS spawn 模式**：worker 函数与参数必须可 pickle（块号/页范围/路径均为基本类型 ✓）
- **临时文件**：块文件写 `/tmp`，异常时可能残留（`finally` 清理 + mkdtemp 目录随进程回收）
- **insert_pdf 链接/注释复制开销**：转换产物无链接/注释需求，`annots=False, links=False` 已规避
- **并行模式下的 `_glyph_cache`/`_TF_RE` 等模块级状态**：每进程独立副本，无跨进程共享 ✓

## Implementation Plan

1. **Slice 6 — 并行流水线实现**（scripts/pdf_t2s.py）：
   - `process_pdf_parallel(src, cc, fonts, out, jobs)`：阶段 1 全局 collect+subset（串行）→ 阶段 2 分块并行重建（`multiprocessing.Pool`）→ 阶段 3 insert_pdf 合并 + `dedup_fonts` + `garbage=3` 保存
   - `_block_worker(args)`：模块级可 pickle（macOS spawn）；独立 open 原 PDF → `load_doc_fonts` → 块内 `rebuild_page` → `doc.select(块内页)` → 保存块文件
   - `dedup_fonts()`：按 PS 名+流哈希统一重复字体 xref（O(n)，替代 garbage=4 的 O(n²) 流合并）
   - 降级规则：页数 < 50 或 jobs=1 → 串行 `process_pdf`；jobs 默认 = min(cpu_count, 页数)
   - CLI：`--jobs/-j` 参数（默认自动）
   - 验证：块划分单测、小文档降级单测、合成 60 页端到端并行单测（文本/无 \x00）

2. **Slice 7 — 验收**：
   - 两份验收文档并行产物全量五维验证（001 标准 1-8）
   - 加速比 ≥ 2x、体积 ≤ 串行产物
   - 验收标准 1-6 全部通过，spec status → shipped
