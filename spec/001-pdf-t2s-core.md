---
status: shipped # proposed → approved → in-progress → shipped
target: 字体保真的 PDF 繁简转换
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
5. redaction：全部 span（含竖排与纯空白 span）加 redact 注释后 `apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)`——显式保留图片与矢量线
6. 重绘：每视觉行一个 `fitz.Shape(page)`，行内逐 span `insert_text`，每行 `commit()`（每行独立内容流 → 提取顺序 = 视觉行序）
7. 竖排：逐字符转换后按原字符 origin 重绘，保持竖排排列
8. 保存：`doc.save(out, garbage=3, deflate=True)`；字体嵌入策略见 [002 输出体积优化](./002-output-size-optimization.md)

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

**截断名前缀回退（004 fix）**：MuPDF rawdict 把超长 span 字体名截断到 24 字符（实测：`TimesNewRomanPS-BoldItalicMT` → `TimesNewRomanPS-BoldItal`、`MicrosoftJhengHeiRegular-ETen-B5-H` → `MicrosoftJhengHeiRegular`），而 `load_doc_fonts` 的键是完整 BaseFont 名。精确匹配失败时按 24 字符前缀回退完整名（`find_doc_font`）——否则真斜体等长名字体会查键失败回退系统正体字体，斜体丢失（MiniMax 第 159 页 `TimesNewRomanPS-BoldItalicMT` 实测受影响）。

**可映射探测**：复用原字体前逐字符 `fitz.Font.has_glyph` 探测 Unicode→字形映射可用性（结果按 字符×字体 缓存）。CID CFF 等无 Unicode cmap 的字体（如 SourceHanSans、MSungHK）探测失败，`insert_text` 无法为其做 Unicode→字形映射（复用会导致 notdef 与提取 \x00），一律回退系统字体；TrueType 等带 cmap 的字体（TimesLTStd、Arial）正常复用。

**宽度拟合**（004 fix 双向化）：以**实际绘制字体**度量新文本宽度，仅系统字体路径（ZO 原字体度量与原始一致，跳过）：

- 收缩（原有）：`fit_w > bbox.width × 1.02` 时 `size = max(size × bbox.width / fit_w, size × 0.6)`
- 放大（004 fix）：`fit_w < bbox.width × 0.98` 时 `size = min(size × bbox.width / fit_w, size × 1.5)`——回退字体更窄（如 MSungHK 字形墨迹 1.107em 而 Songti 1.0em）时补足视觉宽度，否则行尾缩短、字距视觉变紧

**粗体档位检测（004 fix）**：MuPDF 的 span 粗体位（flags bit 4）只对名字含 'Bold' 的字体设位——'Xbold'/'Heavy'/'Black' 等加粗档位不设位（MHeiHK-Xbold、SourceHanSansCN-Heavy 实测），粗体 span 会回退常规字重。`_span_bold` 以名字级正则（`X?Bold|Heavy|Black`）补充检测；返回值必须为 0/16 整数（`fallback_font` 用其拼接资源名 ZS0/ZS16 并索引槽位键——布尔 True 会产出 ZSTrue/ZSFalse 资源名且查不到粗体槽位）。

**斜体保真（004 fix）**：原 PDF 的"假斜体"——字体文件本身正体（如内嵌 `Times-Roman Regular`、`TimesLTStd-Roman`），靠 FontDescriptor /Flags 斜体位（bit 5 = 32）声明斜体，查看器（Chrome/pdfium、Foxit、Acrobat 等理会标志者；MuPDF/Quartz 渲染忽略该位）据此合成倾斜。`insert_text` 重绘创建的新字体对象不带该位（Flags 由字体文件派生），假斜体会在理会标志的查看器中消失。修复：重绘时按页收集原字体描述符斜体位（`_descriptor_flags`，支持简单字体直链与 Type0/CID 的 DescendantFonts 内嵌描述符），`apply_italic_flags` 在页面 commit 后把该位补到对应新字体对象。真斜体（字形本身倾斜的字体文件，如 `TimesNewRomanPS-BoldItalicMT`）经 ZO 复用天然保留。

## 验证门槛（五维，`scripts/verify.py`）

| 维度 | 方法 | 门槛 |
|---|---|---|
| 内容级 | 归一化字符 multiset 对比（norm：去空白 + `\xad`→`-`、`・`→`•`、U+00A0→空格） | 仅允许白名单差异 |
| 顺序级 | SequenceMatcher ratio | 参考指标，不以一致为目标（输出 = 视觉阅读序） |
| 字体保真 | 未转换 span 逐 span 核对输出字体 = 原字体（去前缀 PS 名） | 原字体可提取且可映射的 span ≥ 99% 复用；回退计入报告 |
| 像素级 | 1.0x 渲染墨迹密度差（抽样 ≥20 页，含全部竖排页与收缩页） | ≤ 0.02 |
| 网格级（004 fix） | 12×10 格子墨迹对比（整页渲染；原墨迹 >250px 且损失 >75% 判异常） | 0 异常（回归检测——整页密度对局部缺失不敏感；既有的不可提取文字/竖排偏移损失 <250px 排除，见 SKILL.md 已知限制） |
| 竖排 | 字符位置逐点核对 + 列内提取顺序 | 0 缺失，自上而下 |

白名单（内容级允许差异）：`於→于` 补丁差异、标点 unicode 变体（`-`↔`\xad`、`•`↔`・`、`…`↔`⋯`，页面显示一致）、原始 PDF 固有乱码页（安永信信头 2 页）。

繁体残留：以仅含简繁字形不同之字的特征串统计，输出必须为 0。

## Non-goals (explicitly out)

- 扫描版 PDF（无文本层）：**明确放弃**——无 OCR 方案/计划，输入直接拒绝（2026-08-16 决策，README/AGENTS/SKILL.md 注明）
- 横向拉伸（非等比 text matrix）的保留：`insert_text` 不支持非等比缩放，拉伸文本重绘后字距归为正常 em——已知限制，记录于 SKILL.md
- 标点 unicode 变体根治：字体 ToUnicode 生成行为所致，页面显示一致，接受
- 提取顺序与原始内容流顺序一致：输出以视觉阅读序为准（原始内容流序本身是乱的）
- `於→于` 语义细化：保持显式补丁规则
- 输出体积优化：独立特性，见 [002](./002-output-size-optimization.md)

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

## Risks

- 原字体复用依赖原 PDF 字体可提取且可被 MuPDF 重新嵌入；异常字体（损坏 buffer、非常规 CMap）回退系统字体，由字体保真维度计数报告，不影响转换完成
- 行合并行为随字体度量变化：原字体度量下与原始 PDF 的合并行为一致（顺序级为参考指标）
- 混合 span（部分字符转换）整 span 使用系统字体：字形统一，与原字体存在字重差异，属设计内替换（转换字形必须来自系统字体）
- 宽度收缩下限 0.6× 的极端情况（收缩后字号过小）：由像素级验证捕获并人工复核
- 竖排逐字符重绘后 MuPDF 不再标记垂直方向（提取层差异）：视觉与坐标不受影响，列内提取顺序保持自上而下

## Implementation Plan

1. **Slice 1 — 字体选择规范实现**（scripts/pdf_t2s.py）：
   - `font_key(name)`：字体名去除 subset 前缀（`SPNAJD+MHeiHK-Bold` → `MHeiHK-Bold`）
   - `load_doc_fonts`：以去前缀 PS 名为键；非嵌入字体（无 buffer）跳过
   - 横排字体选择：转换过 → 系统字体（含 CJK 按类 / 纯拉丁 Times）；未转换 → 原字体（去前缀匹配）；原字体不可提取 → 回退（纯拉丁 Times / 含 CJK 系统字体）
   - 宽度收缩：以实际绘制字体度量（原字体 span 不触发）
   - 竖排逐字符：同一选择规则
   - ZO 资源名确定性命名（crc32），保证同输入同输出
   - 验证：单元测试（font_key、合成 PDF 重建后未转换 span 保持原字体）

2. **Slice 2 — verify.py 字体保真维度**：
   - 提取可提取字体集合（按 xref 去重）
   - 逐页核对：未转换 span 的输出字体 == 原字体（去前缀）
   - 统计：复用 / 替换 / 回退 / 未匹配 / 收缩计数 + 示例
   - 内容级白名单分类（於/于、标点变体、\x00 固有乱码）
   - 竖排维度：字符位置逐点核对（0 缺失）
   - 像素抽样：均匀抽样 + 全部竖排页 + 收缩页
   - 验证：对合成样本能检出字体替换

3. **Slice 3 — 验收**：
   - 两份验收文档全量转换（固定脚本）
   - 五维验证全量执行
   - 验收标准 1-8 全部通过，收缩/回退计数写入报告
