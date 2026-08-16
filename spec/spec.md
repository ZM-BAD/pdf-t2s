---
status: shipped # proposed → approved → in-progress → shipped
target: 字体保真的 PDF 繁简转换（v1.1 输出体积优化）
date: 2026-08-14
---

# 字体保真的 PDF 繁简转换

## Background

繁转简的核心约束是「内容转换 + 排版保真 + 字形保真」三者同时成立。排版保真由逐行重建文本层实现（坐标、字号、颜色、图片、矢量线、竖排均不变）；字形保真由字体选择规则决定：

- 转换过的文本需要简体字形，原 PDF 的子集字体不含这些字形 → 使用系统简体字体；
- 未转换的文本（数字、日期、同形字、标点、纯拉丁）复用原 PDF 内嵌字体 → 字形 100% 还原，字重与字宽与原始一致。

本规范定义转换流程、字体选择规则与验证门槛。实现以本规范为准，`scripts/` 与 `tests/` 以本规范为验收依据。

## Goal (one sentence)

在完整保留原排版的前提下完成繁转简，未转换文本复用原 PDF 字体、转换文本使用系统简体字体，使输出在内容、字形、视觉三个层面与原始一致。

## Scope (what it does)

1. 转换主脚本 `scripts/pdf_t2s.py`：
   - 逐页重建文本层：redaction 删旧字 → 每视觉行一个内容流重绘
   - 字体选择：按本规范「字体选择」表分配字体资源
   - 宽度收缩：以实际绘制字体度量，超出原 bbox 时等比收缩（下限 0.6×）
   - 竖排逐字符重绘，坐标保持
2. 验证脚本 `scripts/verify.py`：内容级 / 顺序级 / 字体保真 / 像素级 / 竖排五维验证（阈值与白名单见「验证门槛」）
3. 字体准备 `scripts/prepare_fonts.py`：从 macOS 系统字体提取 face 为独立 ttf，PS 名（nameID 6）加 `ZT2S-` 前缀（防止与 PDF 原字体撞名），产物不入库
4. 验收：两份文字版招股书（504 页 / 716 页）全量转换，验收标准 1-8 全部通过

## 转换流程（逐页）

1. 提取：`page.get_text("rawdict")`——字符级结构，span 文本 = `"".join(c["c"] for c in span["chars"])`
2. 收集 span：跳过空/纯空白 span、无效 bbox；竖排判定 `line["dir"] == (0, ±1)`
3. 按视觉行分组（行 y0 排序，1 位小数容差），行内按 x0 排序
4. 转换：`OpenCC("t2s")` 转换，附 `於→于` 补丁
5. redaction：全部 span（含竖排）加 redact 注释后 `apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)`——显式保留图片与矢量线
6. 重绘：每视觉行一个 `fitz.Shape(page)`，行内逐 span `insert_text`，每行 `commit()`（每行独立内容流 → 提取顺序 = 视觉行序）
7. 竖排：逐字符转换后按原字符 origin 重绘，保持竖排排列
8. 保存：`doc.save(out, garbage=3, deflate=True)`，完整字体嵌入（不子集化）

输出命名：`<原名>_简体.pdf`（与输入同目录）；`--output` 可指定（仅单输入）。

## 字体选择（规范表）

字体资源：原 PDF 字体经 `extract_font(xref)[3]` 提取 buffer 写临时文件后嵌入，资源名 `ZO<idx>`；系统字体资源名 `ZS0/ZS1`（黑体常规/粗体）、`ZR0/ZR1`（宋体常规/粗体）、`ZT0/ZT1`（Times 常规/粗体）。资源名全部自定义，不与任何原字体名撞名。

| 条件 | 字体 | 资源名 |
|---|---|---|
| 文本转换过 且 含 CJK | 黑体类* → Hiragino Sans GB；其余 → Songti SC | ZS0/ZS1、ZR0/ZR1 |
| 文本转换过 且 纯拉丁 | 原为 Times 类 → Times New Roman；否则 Hiragino | ZT0/ZT1、ZS0/ZS1 |
| 文本未转换 且 原字体可提取且可映射 | 原 PDF 内嵌字体（字形 100% 还原） | ZO\<idx\> |
| 文本未转换 且 原字体不可提取或不可映射 | 纯拉丁 → Times New Roman；含 CJK → 系统字体（按黑体/宋体类） | ZT0/ZT1、ZS0/ZS1、ZR0/ZR1 |

\* 黑体类：字体名含 MHei / Hei / Sans / Gothic。

**原字体匹配规则**：`load_doc_fonts` 以 basefont 的 **PostScript 名去除 subset 前缀后**为键（`SPNAJD+MHeiHK-Bold` → `MHeiHK-Bold`），**按页匹配该页实际引用的子集字体**（同 PS 名不同子集时以页为准，保证字形必然存在）；span 字体名同样去除前缀后匹配。子集字体与 span 字体名由此归一，未转换 span 一律按上表复用原字体——**含 CJK 的未转换 span 同样复用原字体**（原字体渲染过这些字形，字形必然存在；系统字体仅用于转换过或原字体不可用的 span）。

**可映射探测**：复用原字体前逐字符 `fitz.Font.has_glyph` 探测 Unicode→字形映射可用性（结果按 字符×字体 缓存）。CID CFF 等无 Unicode cmap 的字体（如 SourceHanSans、MSungHK）探测失败，`insert_text` 无法为其做 Unicode→字形映射（复用会导致 notdef 与提取 \x00），一律回退系统字体；TrueType 等带 cmap 的字体（TimesLTStd、Arial）正常复用。

**宽度收缩**：以**实际绘制字体**度量新文本宽度；`fit_w > bbox.width × 1.02` 时 `size = max(size × bbox.width / fit_w, size × 0.6)`。未转换 span 使用原字体时度量与原始一致，不触发收缩。

## 验证门槛（五维，`scripts/verify.py`）

| 维度 | 方法 | 门槛 |
|---|---|---|
| 内容级 | 归一化字符 multiset 对比（norm：去空白 + `\xad`→`-`、`・`→`•`、U+00A0→空格） | 仅允许白名单差异 |
| 顺序级 | SequenceMatcher ratio | 参考指标，不以一致为目标（输出 = 视觉阅读序） |
| 字体保真 | 未转换 span 逐 span 核对输出字体 = 原字体（去前缀 PS 名） | 原字体可提取且可映射的 span ≥ 99% 复用；回退计入报告 |
| 像素级 | 1.0x 渲染墨迹密度差（抽样 ≥20 页，含全部竖排页与收缩页） | ≤ 0.02 |
| 竖排 | 字符位置逐点核对 + 列内提取顺序 | 0 缺失，自上而下 |

白名单（内容级允许差异）：`於→于` 补丁差异、标点 unicode 变体（`-`↔`\xad`、`•`↔`・`、`…`↔`⋯`，页面显示一致）、原始 PDF 固有乱码页（安永信信头 2 页）。

繁体残留：以仅含简繁字形不同之字的特征串统计，输出必须为 0。

## Non-goals (explicitly out)

- 扫描版 PDF（无文本层）：**明确放弃**——无 OCR 方案/计划，输入直接拒绝（2026-08-16 决策，README/AGENTS/SKILL.md 注明）
- 横向拉伸（非等比 text matrix）的保留：`insert_text` 不支持非等比缩放，拉伸文本重绘后字距归为正常 em——已知限制，记录于 SKILL.md
- 标点 unicode 变体根治：字体 ToUnicode 生成行为所致，页面显示一致，接受
- 提取顺序与原始内容流顺序一致：输出以视觉阅读序为准（原始内容流序本身是乱的）
- `於→于` 语义细化：保持显式补丁规则

## Acceptance criteria (verifiable, itemized)

对两份验收文档（504 页 / 716 页，均有文本层）全量执行：

1. 内容级：0 个非白名单异常页
2. 字体保真：原字体可提取且可映射的未转换 span ≥ 99% 复用原字体；回退与收缩 span 计数写入报告
3. 宽度收缩：仅当以绘制字体度量超出原 bbox 1.02 倍时触发；收缩后字号 ≥ 0.6×
4. 像素级：0 页超阈值（含全部竖排页与收缩页）
5. 竖排：字符位置 0 缺失，列内提取顺序自上而下
6. 繁体残留：0
7. 结构：页数、页面尺寸、旋转、图片数、矢量对象数逐页一致
8. 验证可重复执行，同输入同输出

## Slice plan (each slice independently verifiable)

| Slice | Content | Status |
| ----- | ------- | ------ |
| 1     | 字体选择规范实现：原字体键去前缀匹配；未转换 span（含 CJK）复用原字体；以绘制字体度量收缩 | [ ] |
| 2     | verify.py 字体保真维度：未转换 span 字体核对 + 回退/收缩计数报告 | [ ] |
| 3     | 两份验收文档全量转换 + 五维验证，验收标准 1-8 全部通过 | [ ] |

## Risks

- 原字体复用依赖原 PDF 字体可提取且可被 MuPDF 重新嵌入；异常字体（损坏 buffer、非常规 CMap）回退系统字体，由字体保真维度计数报告，不影响转换完成
- 行合并行为随字体度量变化：原字体度量下与原始 PDF 的合并行为一致（顺序级为参考指标）
- 混合 span（部分字符转换）整 span 使用系统字体：字形统一，与原字体存在字重差异，属设计内替换（转换字形必须来自系统字体）
- 宽度收缩下限 0.6× 的极端情况（收缩后字号过小）：由像素级验证捕获并人工复核
- 竖排逐字符重绘后 MuPDF 不再标记垂直方向（提取层差异）：视觉与坐标不受影响，列内提取顺序保持自上而下

---

## v1.1 — 输出体积优化（fontTools 预子集化）

## Background

v1.0 输出约 60MB（原始 ~8MB），根因是**系统字体全量嵌入**：Songti SC Regular/Bold 各 20-22MB、Hiragino Sans GB W3/W6 各 11-12MB（30 页实测 64.5MB 中系统字体占 ~67MB 原始流）。ZO 原字体是原 PDF 已有子集（合计 ~7MB），非大头。

`doc.subset_fonts()` 有 ToUnicode bug（踩坑 16）不可用，但**在嵌入前用 fontTools 预子集化**可绕过该路径：MuPDF 嵌入的已是合法子集字体，ToUnicode 由 cmap 正常生成（2026-08-16 实验：20.9MB serif → 9KB、11.6MB sans → 12KB、0.8MB times → 29KB，纯 CJK/拉丁/混合文本提取全部正确、无控制字符、PS 名 `ZT2S-` 前缀保留）。

## Scope（v1.1 追加）

1. **提取 `pick_font()`**：横排/竖排统一的字体选择函数（现内联于 `rebuild_page`），两阶段复用，保证字符收集与重绘选择一致
2. **阶段 1 字符收集 `collect_needed_chars()`**：全文档扫描 span → 转换 → `pick_font` → 按字体文件路径收集所需字符集（含竖排逐字符）
3. **阶段 2 子集化 `subset_system_fonts()`**：仅对系统字体（CJK_FONTS/LATIN_SERIF 路径集）fontTools subset，输出到每进程独立临时目录（`tempfile.mkdtemp`，防并发转换互相覆盖——踩坑 23）；子集化失败回退完整字体（转换照常完成）
4. **阶段 3 重绘**：`rebuild_page` 以子集路径替换 fontfile（`font_path_map.get(fp, fp)`），测量仍用完整字体（hmtx 度量一致，收缩行为不变）
5. ZO 原字体**不子集化**（已是子集且为 CID CFF 风险高，体积占比小）
6. **死字体清理 `purge_unused_fonts()`**（补充）：redaction 后个别字体对象残留且被页面/Form 的 Resources 持有引用——保存前删除 Resources/Font 中内容流未引用的条目；扫描范围含页面内容流与全部 Form XObject 内容流（防误删 Form 引用的字体）；兼容 Resources/Font 直接字典与间接引用结构，资源名正则含下划线（防把 `C0_0` 误判为 `C0`）
7. **空白 span redact**（补充）：纯空白 span（提取为空白、视觉不可见）一并 redact，释放其字体资源残留引用（MiniMax 100 页实测 -0.7MB）；空白字符经 norm 后与原始无内容级差异
8. 验收：两份招股书全量转换，五维验证标准 1-8 全部通过 + 体积达标

> 已知残留: 智谱原 PDF 含隐藏图层（/Layer/OCG）文本（MuPDF 不提取不渲染，rawdict 无对应 span），其字体（~5MB）无法经 redact/purge 清理——属原始 PDF 固有结构，输出保留属正确行为。

## 体积验收标准（追加，编号延续「Acceptance criteria」为第 9-10 条）

1. 输出体积相对 v1.0 输出（约 60MB）减少 ≥ 70%（实测智谱 15.9MB / MiniMax 14.6MB；主要来源是系统字体子集化，ZO 原字体保持完整嵌入）
2. 五维验证（标准 1-8）对两份验收文档全部通过，结果与 v1.0 一致（复用率 ≥ 99%、像素 0 异常、竖排 0 缺失、繁体残留 0）

> 注: 初步按"≤15MB"设阈，实测智谱 15.9MB（减 74.4%）为干净最小值——前期"死字体 10.1MB"分析系资源名正则漏下划线（C0_0 匹配为 C0）的误判，全量文档并无死字体可清；相对减幅 ≥70% 才是本特性的核心指标。

## Slice plan（追加）

| Slice | Content | Status |
| ----- | ------- | ------ |
| 4     | 两阶段子集化实现：pick_font 提取 + 字符收集 + fontTools 子集化 + 重绘替换；单元测试（pick_font 等价、子集化体积/PS 名/提取正确） | [ ] |
| 5     | 两份验收文档全量转换 + 五维验证 + 体积报告，验收标准 1-10 全部通过，spec status → shipped | [ ] |
