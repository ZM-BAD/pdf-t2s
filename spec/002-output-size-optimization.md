---
status: shipped # proposed → approved → in-progress → shipped
target: 输出体积优化（fontTools 预子集化）
date: 2026-08-16
---

# 输出体积优化（fontTools 预子集化）

## Background

[001 核心繁转简](./001-pdf-t2s-core.md) 的输出约 60MB（原始 ~8MB），根因是**系统字体全量嵌入**：Songti SC Regular/Bold 各 20-22MB、Hiragino Sans GB W3/W6 各 11-12MB（30 页实测 64.5MB 中系统字体占 ~67MB 原始流）。ZO 原字体是原 PDF 已有子集（合计 ~7MB），非大头。

`doc.subset_fonts()` 有 ToUnicode bug（见 SKILL.md 踩坑 16）不可用，但**在嵌入前用 fontTools 预子集化**可绕过该路径：MuPDF 嵌入的已是合法子集字体，ToUnicode 由 cmap 正常生成（2026-08-16 实验：20.9MB serif → 9KB、11.6MB sans → 12KB、0.8MB times → 29KB，纯 CJK/拉丁/混合文本提取全部正确、无控制字符、PS 名 `ZT2S-` 前缀保留）。

## Goal (one sentence)

在五维验证（001 验收标准 1-8）全部保持通过的前提下，把输出体积从 ~60MB 降至原始量级（相对减幅 ≥ 70%），且不引入 `doc.subset_fonts()` 的 ToUnicode 乱码。

## Scope (what it does)

1. **提取 `pick_font()`**：横排/竖排统一的字体选择函数，字符收集与重绘两阶段复用，保证子集字符覆盖与实际绘制完全一致
2. **阶段 1 字符收集 `collect_needed_chars()`**：全文档扫描 span → 转换 → `pick_font` → 按字体文件路径收集所需字符集（含竖排逐字符）
3. **阶段 2 子集化 `subset_system_fonts()`**：仅对系统字体（CJK_FONTS/LATIN_SERIF 路径集）fontTools subset，输出到每进程独立临时目录（`tempfile.mkdtemp`，防并发转换互相覆盖）；子集化失败回退完整字体（转换照常完成）
4. **阶段 3 重绘**：`rebuild_page` 以子集路径替换 fontfile（`font_path_map.get(fp, fp)`），测量仍用完整字体（hmtx 度量一致，收缩行为不变）
5. ZO 原字体**不子集化**（已是子集且为 CID CFF 风险高，体积占比小）
6. **死字体清理 `purge_unused_fonts()`**：redaction 后个别字体对象残留且被页面/Form 的 Resources 持有引用——保存前删除 Resources/Font 中内容流未引用的条目；扫描范围含页面内容流与全部 Form XObject 内容流（防误删 Form 引用的字体）；兼容 Resources/Font 直接字典与间接引用结构，资源名正则含下划线（防把 `C0_0` 误判为 `C0`）
7. **空白 span redact**：纯空白 span（提取为空白、视觉不可见）一并 redact，释放其字体资源残留引用（MiniMax 100 页实测 -0.7MB）；空白字符经 norm 后与原始无内容级差异
8. 验收：两份招股书全量转换，五维验证标准 1-8 全部通过 + 体积达标

## 验收标准（在 001 标准 1-8 之上追加）

- **9.** 输出体积相对全量嵌入输出（约 60MB）减少 ≥ 70%
- **10.** 五维验证（001 标准 1-8）对两份验收文档全部通过，结果与 001 一致（复用率 ≥ 99%、像素 0 异常、竖排 0 缺失、繁体残留 0）

## 已知残留

智谱原 PDF 含隐藏图层（/Layer/OCG）文本（MuPDF 不提取不渲染，rawdict 无对应 span），其字体（~5MB）无法经 redact/purge 清理——属原始 PDF 固有结构，输出保留属正确行为。

## Risks

- 子集覆盖依赖两阶段使用同一 `pick_font`（同输入同输出）：字符收集与重绘选择必须一致，否则绘制 notdef（回归由五维验证捕获）
- 并发转换竞争：子集/原字体临时文件被并发覆盖会导致大范围 `\x00`（症状：常用字变 `\x00`、嵌入流却含字形）——每进程独立目录（`tempfile.mkdtemp`）为硬性要求
- 资源名正则必须含下划线：原始 PDF 资源名形如 `C0_0`/`T1_0`，漏下划线会把活字体误判为死字体（或漏清）
- 死字体清理对间接 Resources 路径不可操作（PyMuPDF `xref_set_key` 限制）：跳过该条目，不影响转换完成

## Implementation Plan

1. **Slice 4 — 两阶段子集化**（scripts/pdf_t2s.py）：
   - 提取 `pick_font()`：横排/竖排统一字体选择（字符收集与重绘复用）
   - `collect_needed_chars()`：阶段 1 字符收集（全文档，竖排逐字符）
   - `subset_system_fonts()`：阶段 2（fontTools subset，仅系统字体，失败回退；每进程独立临时目录）
   - `rebuild_page` 经 font_path_map 替换 fontfile（测量仍用完整字体）
   - `purge_unused_fonts()`：保存前清理页面/Form Resources 中内容流未引用的字体条目（兼容直接/间接字典，正则含下划线）
   - 空白 span redact：释放残留字体引用
   - 验证：pick_font 等价 + 子集化（体积/PS 名/提取）+ purge 健壮性

2. **Slice 5 — 验收**：
   - 两份验收文档全量转换 + 五维验证
   - 验收标准 1-10 全部通过（1-8 与 001 一致；9 体积减 ≥70%；10 全过）
   - 体积报告写入转换输出，spec status → shipped
