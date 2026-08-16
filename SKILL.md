---
name: pdf-t2s
description: Use when the user wants to convert a Traditional Chinese (繁體中文) PDF to Simplified Chinese (简体中文) while preserving the original layout exactly — fonts, coordinates, sizes, colors, tables, images and vertical text stay untouched — fully locally (no upload). Works on text-layer PDFs like prospectuses (招股书), annual reports, contracts. 用于把繁体中文 PDF 转成简体中文并完整保留排版，本地处理，不上传云端。
---

# PDF 繁转简（排版保真 · 本地处理）

## Overview

把繁体中文 PDF 原地转换为简体中文，**完整保留原排版**（坐标、字号、颜色、表格、图片、竖排文本均不动），全程本地处理。方案：**OpenCC(t2s) 字符转换 + PyMuPDF 逐行重建文本层**（redaction 删旧字 → 每视觉行一个内容流重绘）。

铁律（违反会导致视觉错误或提取乱码，见常见坑）:

1. **不跑验证不算完成** —— 每次转换后必须运行 `scripts/verify.py`（见验证门槛）。
2. **绝不调用 `doc.subset_fonts()`** —— ToUnicode 生成有 bug，子集化后部分字符提取成控制字符。
3. **绝不用 TextWriter** —— 每行重复注册字体资源且按 PS 名复用文档已有字体，与 PDF 原字体撞名导致保存后提取乱码。
4. **字体不入库** —— 系统字体提取物（Apple/微软版权）由 `prepare_fonts.py` 在目标机器生成，`.gitignore` 已排除。

## When to Use

**适用**: 文字版 PDF（有文本层）——招股书、年报、合同、法律文件等；敏感文件不想上传云端时；需要保留原始排版时。

**不适用 / 直接拒绝**: 扫描版 PDF（无文本层）→ 项目**明确放弃扫描版支持、无 OCR 方案/计划**，直接告知用户不支持并停止，**不硬转**（转换结果是空白页）。

## 仓库结构

```text
pdf-t2s/
├── SKILL.md               # 本技能说明
├── scripts/
│   ├── pdf_t2s.py         # 转换主脚本
│   ├── verify.py          # 三级验证脚本
│   └── prepare_fonts.py   # 从 macOS 系统字体提取重绘用字体（ZT2S- 前缀）
├── requirements.txt       # pymupdf opencc fonttools
└── tests/                 # 单元测试（无依赖环境自动跳过）
```

## 核心流程

```sh
# 1. 准备环境（一次性，macOS）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python3 scripts/prepare_fonts.py      # 生成 t2s-*.ttf 到 scripts/（不入库）

# 2. 转换（本地，逐页处理）
.venv/bin/python3 scripts/pdf_t2s.py 输入.pdf [更多输入.pdf...]

# 3. 验证（必须！）
.venv/bin/python3 scripts/verify.py 输入.pdf 输入_简体.pdf
```

转换算法（每页）:

1. `page.get_text("rawdict")` 提取全部 span——注意 rawdict 是**字符级**：span 没有 `"text"` 键，必须 `"".join(c["c"] for c in span["chars"])`。
2. 竖排检测：`line["dir"] == (0, ±1)` 且 **wmode==0**（不能用 wmode==1 过滤，会漏掉竖排）。
3. 按视觉行分组（rawdict 的 line），行排序 key = `line["bbox"][1]`（1 位小数容差）。
4. OpenCC t2s 转换：`cc.convert(text).replace("於", "于")`——**於→于 是必须的补丁**（OpenCC 词表不转换独立"於"）。
5. 全部 span（**含竖排**）加 redact 注释并应用：`apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)`——显式保留图片和矢量线。
6. 逐行重绘：每视觉行一个 `fitz.Shape(page)`，行内 span 按 x0 排序逐个 `shape.insert_text(...)`，然后 **`shape.commit()`（必须每行 commit，产生独立内容流 → 提取顺序 = 视觉行序）**。
7. 竖排 span：对 chars **逐字符**转换并按其 origin 重绘，保持竖排排列。
8. 字体选择（防缺字形/撞名，规范见 spec/spec.md）:
   - span 文本有变化（含繁体）→ 系统简体字体（资源名 `ZS0/ZS1`/`ZR0/ZR1`/`ZT0/ZT1`，见下节字体映射）。
   - span 无变化 → 原 PDF 内嵌字体（`doc.extract_font(xref)[3]` 写临时文件，资源名 `ZO<idx>`）。匹配键为**去 subset 前缀后的 PS 名**（`SPNAJD+MHeiHK-Bold` ≡ `MHeiHK-Bold`），且**复用前逐字符 `has_glyph` 探测映射可用性**——CID CFF 等无 Unicode cmap 的字体（SourceHanSans/MSungHK）`insert_text` 无法映射，回退系统字体；TrueType 字体（TimesLTStd 等）正常复用。
   - 原字体不可提取或不可映射 → 纯拉丁用 Times、含 CJK 用系统字体。
9. 宽度收缩防溢出：`font.text_length(new, fontsize=size) > bbox.width*1.02` 时 `size = max(size * bbox.width / fit_w, size * 0.6)`。
10. 字体子集化（v1.1 体积优化）：重绘前全文档收集每个系统字体实际用到的字符集（`collect_needed_chars`，与重绘共用 `pick_font`），fontTools 预子集化（`subset_system_fonts`，仅系统字体，失败回退完整字体），经 `font_path_map` 替换 fontfile。**绕开 `doc.subset_fonts()` 的 ToUnicode bug**——MuPDF 嵌入的已是合法子集，ToUnicode 由 cmap 正常生成（实验验证：20.9MB serif → 9KB，提取无乱码）。原 PDF 内嵌字体（ZO）已是子集，不子集化。
11. 保存：`doc.save(out, garbage=3, deflate=True)`——绝不 `doc.subset_fonts()`。输出体积约 12-18MB（v1.0 为 ~60MB）。

输出文件命名：`原名_简体.pdf`（与输入同目录）。

### 字体映射（macOS）

| 原始字体名包含 | 目标 | 资源名 | 文件 |
|---|---|---|---|
| MHei / Hei / Sans / Gothic（黑体类） | Hiragino Sans GB W3/W6 | ZS0/ZS1 | t2s-sans-regular/bold.ttf |
| 其余（MSung / Song / Serif 等宋体类） | Songti SC Regular/Bold | ZR0/ZR1 | t2s-serif-regular/bold.ttf |
| Times（纯拉丁且无 CJK） | Times New Roman | ZT0/ZT1 | t2s-times(-bold).ttf |
| 原 PDF 内嵌字体（未转换 span） | 原字体 buffer（PS 名去 subset 前缀匹配） | ZO\<idx\> | /tmp/pdf-t2s-origfonts/f\<idx\>.\<ext\> |

- 系统字体由 fontTools 从 ttc 提取 face 为独立 ttf，**PS 名（name[6]）加 `ZT2S-` 前缀**防与 PDF 原字体撞名。
- `Shape.insert_text` 的 `fontfile` 只接受**文件路径**（不接受 buffer）——原字体 buffer 必须写临时文件。

## 常见坑（权威表 —— 本项目全部踩坑，违反即出问题）

| # | 坑 | 说明 |
|---|---|---|
| 1 | PEP 668 | Homebrew Python 拒绝 pip 全局安装 → 必须 venv |
| 2 | rawdict 字符级 | span 没有 "text" 键，需 `"".join(c["c"] ...)` |
| 3 | TextWriter 无颜色参数 | 颜色只能在构造/写盘时设置 → 需按颜色分组（已弃用 TextWriter） |
| 4 | 颜色格式 | `add_redact_annot`/`insert_text` 的 color 要求 0-1 浮点元组 |
| 5 | 旧 API 不存在 | `doc.add_stream`/`append_stream` 都不存在（1.28），走 Shape 方案 |
| 6 | TextWriter 提取顺序乱 | 所有文本追加到内容流末尾 → 复制/搜索顺序错乱（视觉正确） |
| 7 | 跨字体行合并失败 | MuPDF 按字体 ascent 估算行 y0 → Times 与 CJK 混排被拆行（用原字体度量一致则行为一致） |
| 8 | 整行单字体 | 同一视觉行统一字体重绘可解决行合并，但表格头仍乱 |
| 9 | 表格头顺序 | 行距 ~14pt 在行合并容差内 → 提取顺序列优先变行优先；内容完整视觉正确，可接受 |
| 10 | 原 PDF 顺序怪癖 | 页脚物理在底部但内容流在最前 → 重建后更合理，验证不追求与原顺序一致 |
| 11 | 逐行独立流原理 | 块间按内容流顺序、块内按位置排序 → 每行一个流则顺序完全可控 |
| 12 | Shape.commit() | 每 commit 一个独立内容流，实现简单可靠（最终方案基石） |
| 13 | 竖排检测陷阱 | 竖排 `dir=(0,±1)` 但 **wmode==0**；不识别会横排重绘成蚂蚁字 |
| 14 | 竖排必须 redact | 竖排 bbox 与多个横排 redact 重叠，不 redact 会被误切丢字 |
| 15 | "於"不转换 | OpenCC 不转独立"於"→ 必须补丁 `replace("於","于")` |
| 16 | subset_fonts bug | ToUnicode 映射错误 → "财务资料"提取成控制字符；禁用。v1.1 用 fontTools **预子集化**绕开（嵌入前子集化，MuPDF 只嵌入合法子集，ToUnicode 正常），体积 60→12-18MB |
| 17 | 0.5x 渲染失效 | 细字抗锯齿后密度检测几乎全 0 → 像素级验证必须 1.0x |
| 18 | TextWriter 撞名 bug | 按 PS 名复用文档字体 → 系统宋体与智谱原字体撞名 → 子集字体缺字形乱码；双重修复：Shape 自定义资源名 + PS 名 ZT2S- 前缀 |
| 19 | fontfile 只收路径 | 原字体 buffer 需写临时文件（fitz.Font 可收 buffer 用于测量） |
| 20 | 原 PDF 固有乱码 | 个别字体 ToUnicode 本身错误（安永信头页）→ 视觉还原、提取仍乱，非回归，验证白名单 |
| 21 | 特征串混入同形字 | 残留检测特征串必须只含简繁字形不同的字（见 verify.py TRAD），否则误报 8500+ |
| 22 | 无法目视图片 | 视觉检查只能靠墨迹密度分析（1.0x 渲染） |
| 23 | 并发转换竞争 | 固定 `/tmp` 子集/原字体目录会被并发转换互相覆盖 → rebuild 读到别的文档的子集 → 大范围 notdef/`\x00`（症状：常用字变 `\x00`、嵌入流却含字形）。修复：`tempfile.mkdtemp` 每进程独立目录 |
| 24 | 死字体回收 | redaction 后原字体残留于页面/Form 的 Resources，`garbage=3` 不回收（智谱实测 10.1MB）→ `purge_unused_fonts()` 保存前清理；需兼容 Resources/Font 直接字典与间接引用两种结构，资源名正则要含下划线 |

## 验证门槛（必须跑 verify.py，不验证不算完成）

`scripts/verify.py` 对原始/输出逐页做五维验证（规范见 spec/spec.md）:

1. **内容级（最重要）**：norm 后字符 multiset（Counter）对比——查丢字/漏转。差异按白名单分类（於/于 补丁、标点变体、原始固有乱码），**非白名单差异必须为 0**。
2. **顺序级（参考）**：SequenceMatcher ratio——输出为视觉阅读序，与原始内容流序不同是预期。
3. **字体保真**：未转换 span 逐 span 核对输出字体 = 原字体（去前缀 PS 名）——原字体可提取的复用率必须 ≥ 99%；回退与收缩计数写入报告。
4. **像素级**：1.0x 渲染暗像素密度对比（均匀抽样 + 全部竖排页 + 收缩页，阈值 0.02）。
5. **竖排**：竖排列字符位置逐点核对——0 缺失。
6. **繁体残留**：仅简繁字形不同的特征字串（TRAD）计数，必须为 0。

验收判定由 verify.py 末尾输出（✅/❌）。

验收基准（真实样例，可复现对比）:

- 智谱招股书 504 页：内容级仅剩 於/于×3 页、…→⋯×4 页；像素级 0 异常；改字 span 22579。
- MiniMax 招股书 716 页：内容级仅剩 於/于×1 页、安永信头页×2；像素级 0 异常；改字 span 25430。

## 已知限制

- **标点 unicode 变体**：`-`→软连字符、`•`→`・`、`…`→`⋯`（字体 ToUnicode 一对多映射，页面显示一样，复制得到变体字符，无法根治）。
- **字体风格**：转换过的 span 及原字体不可映射的 span（CID CFF 无 Unicode cmap）存在字形替换（原始细宋体→标准宋体，笔画略粗，墨迹可增 30%~135%）；未转换 span 复用原字体（TrueType 类），无此问题。
- **横向拉伸丢失**：原始约 2% 的 span 使用非等比横向拉伸（text matrix 1.28× 等，常见于页眉），`insert_text` 不支持非等比缩放，重绘后字距归为正常 em（该行文字变窄约 20%，位置不漂移）。
- **提取顺序**：个别竖排/表格密集页与原始内容流顺序不同（重建后更符合阅读顺序）。
- **文件体积**：v1.1 起系统字体预子集化 + ZO 原字体完整嵌入，输出约 12-18MB（v1.0 为 ~60MB）；ZO 原字体不子集化是为保证字形保真。

## 参考

- OpenCC: <https://github.com/BYVoid/OpenCC>（pip 包 `opencc`，配置 `t2s`）
- PyMuPDF 文本替换指南: <https://artifex.com/blog/how-to-search-and-replace-text-in-pdfs-using-pymupdf>
- PyMuPDF redaction 讨论: <https://github.com/pymupdf/PyMuPDF/discussions/3499>
