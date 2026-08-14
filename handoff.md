# Handoff: pdf-t2s Skill 开发交接文档

> 本文档由主会话（/Users/zm_bad/Downloads 下的 Claude 会话）交接给在本仓库（/Users/zm_bad/workstation/pdf-t2s）开发的 coding agent。
> 内容涵盖：项目目标、调研结论、**最终技术方案**、**全部踩坑经历（22 条）**、验证方法、已知限制、现有资产位置、后续开发指引。

---

## 1. 项目概况

**目标**：开发一个 Claude Code skill，将 PDF 文件中的**繁体中文替换为简体中文**，**完整保留原排版**（坐标、字号、颜色、表格、图片、竖排文本均不动），全程本地处理（不上传云端）。

**已用本项目方案成功转换的真实样例**（可作验收基准）：
- `/Users/zm_bad/Downloads/智谱招股书_中文.pdf`（504 页，8.9MB）→ `智谱招股书_中文_简体.pdf`（62.2MB）
- `/Users/zm_bad/Downloads/MiniMax招股书.pdf`（716 页，7.5MB）→ `MiniMax招股书_简体.pdf`（61.0MB）

**方案一句话**：OpenCC(t2s) 做字符转换 + PyMuPDF 逐行重建文本层（redaction 删旧字 → 每视觉行一个内容流重绘），**不使用** subset_fonts、**不使用** TextWriter（两者都有致命 bug，见踩坑 16/18）。

## 2. 环境与依赖（macOS 已验证）

| 项 | 值 |
|---|---|
| 系统 | macOS (Darwin 25.6.0)，Apple Silicon（Homebrew 在 /opt/homebrew） |
| Python | 3.14.6（Homebrew 安装，**PEP 668 外部管理，pip 直装会被拒 → 必须 venv**） |
| 依赖 | `pymupdf` 1.28.0（Python bindings for MuPDF 1.29）、`opencc`、`fonttools` |
| 已验证的 venv | `/Users/zm_bad/Downloads/.pdfzh-venv`（本次开发使用的，可直接复用） |
| Node | v26.5.1（未使用，备选 opencc-js） |
| 系统工具 | tesseract、qpdf、ghostscript 已装（本次未用） |

**字体资产**（重要，见踩坑 7/18 与第 9 节）：
- macOS **没有** `/System/Library/Fonts/PingFang.ttc`！
- 黑体来源：`/System/Library/Fonts/Hiragino Sans GB.ttc`（face 0=W3 常规，face 2=W6 粗体）
- 宋体来源：`/System/Library/Fonts/Supplemental/Songti.ttc`（face 6=SC Regular，face 1=SC Bold）
- 拉丁宋体：`/System/Library/Fonts/Supplemental/Times New Roman.ttf` 及 Bold 版

## 3. 调研结论（为什么是这个方案）

- ❌ **提取→转换→重排**（pdfplumber + OpenCC + reportlab）：社区实测排版丢失严重、多页乱码，不可用。
- ✅ **OpenCC 是繁简转换事实标准**：词级转换（分词处理"頭髮/發財"、"皇后/後台"等一字多义）；`t2s` 方向无歧义问题（繁体 發/髮 都归简体的 发，s2t 才有一对多）；Python 包 `opencc`；CLI `brew install opencc`；JS 版 `opencc-js`。
- ✅ **PyMuPDF 原地重建文本层**：唯一能保住排版的做法。商业方案（WPS 等）要钱且需上传云端，招股书等敏感文件不建议。
- 扫描版 PDF 需要 OCR（tesseract chi_tra/chi_sim、ocrmypdf、MinerU），本次两份都是文字版（有文本层），**skill 应先检测文本层，无文本层则告知需 OCR 方案**。

## 4. 最终技术方案（逐页处理流程）

```python
# 伪代码：每页
1. rawdict = page.get_text("rawdict")
   # ⚠️ rawdict 是字符级：span 没有 "text" 键！必须 "".join(c["c"] for c in span["chars"])
   # ⚠️ 竖排检测：line["dir"] == (0, ±1) 且 wmode==0！不能用 wmode==1 过滤（见踩坑 13）
2. 收集全部 span：(bbox, origin, text, size, color, fontname, bold=flags&16, chars)
   - 跳过空/纯空白 span；跳过无效 bbox
   - 竖排 span 记录 chars = [(c, Point(origin)) for c in span["chars"]]
3. 按视觉行分组（rawdict 的 line），行排序 key = line["bbox"][1]（1 位小数容差）
4. OpenCC t2s 转换：new = cc.convert(text).replace("於", "于")   # 於→于 是必须的补丁（踩坑 15）
5. 全部 span（含竖排！）加 redact 注释：
   page.add_redact_annot(bbox)   # 只传 rect，不留 fill text
   page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                         graphics=fitz.PDF_REDACT_LINE_ART_NONE)   # 显式保留图片和矢量线
6. 逐行重绘：每视觉行创建一个 fitz.Shape(page)，
   行内 span 按 x0 排序后逐个 shape.insert_text(origin, new, fontname=资源名, fontfile=字体路径, fontsize=size, color=int2rgb(color))
   shape.commit()   # ⚠️ 必须每行 commit，产生独立内容流 → 提取顺序 = 视觉行序（踩坑 10/12）
7. 竖排 span：对 chars 逐字符 insert_text（转换每个字符，坐标用 char 的 origin）→ 保持竖排排列
8. 字体选择（踩坑 18）：
   - span 文本有变化（含繁体）→ 系统简体字体（资源名 ZS0/ZS1/ZR0/ZR1/ZT0/ZT1，见第 9 节字体映射）
   - span 文本无变化 → 原 PDF 内嵌字体（basefont → buffer 提取写临时文件，资源名 ZO<idx>）
   - 判定"变化"：cc.convert(text) != text 或文本含 CJK（含 CJK 的 span 一律系统字体，避免原字体子集缺字形）
9. 宽度收缩防溢出：font.text_length(new, fontsize=size) > bbox.width*1.02 时
   size = max(size * bbox.width / fit_w, size * 0.6)
10. 保存：doc.save(out, garbage=3, deflate=True)
    ⚠️ 绝不调用 doc.subset_fonts()（ToUnicode bug，见踩坑 16）
```

**输出文件命名**：`原名_简体.pdf`（放在输入同目录）。

## 5. 字体映射（第 4 节第 8 步的细节）

原始字体名 → 系统字体（macOS）：

| 原始字体名包含 | 目标 | 资源名 | 文件 |
|---|---|---|---|
| MHei / Hei / Sans / Gothic（黑体类） | Hiragino Sans GB W3/W6 | ZS0/ZS1 | t2s-sans-regular.ttf / t2s-sans-bold.ttf |
| 其余（MSung / Song / Serif 等宋体类） | Songti SC Regular/Bold | ZR0/ZR1 | t2s-serif-regular.ttf / t2s-serif-bold.ttf |
| Times（纯拉丁，且文本无 CJK） | Times New Roman | ZT0/ZT1 | t2s-times.ttf / t2s-times-bold.ttf |
| 原 PDF 内嵌字体（未转换 span） | 原字体 buffer | ZO<idx> | /tmp/pdf-t2s-origfonts/f<idx>.<ext> |

- 系统字体文件是 `fontTools` 从系统 ttc **提取 face 为独立 ttf**，且 **PS 名（name[6]）已加 `ZT2S-` 前缀**（防与 PDF 原字体撞名，踩坑 18）。
- **版权警告**：这些字体（Songti/Hiragino/Times）来自 Apple/微软版权字体，**禁止提交进 git**，由 `prepare_fonts.py` 在目标机器生成 + `.gitignore` 排除。
- 从 ttc 提取 face 的方法：`fontTools.ttLib.TTCollection(path).fonts[idx]`，`f.save("out.ttf")`。
- 注意：`fitz.Shape.insert_text` 的 `fontfile` 参数只接受**文件路径**，不接受 buffer；原字体 buffer 需写临时文件（踩坑 19）。

## 6. 验证方法（skill 必须内置，不验证不算完成）

```python
# verify.py 三级验证（对 原始.pdf / 输出_简体.pdf 逐页对比）
1. 内容级（最重要）：norm 后字符 multiset（Counter）对比
   - norm: 去全部空白 + 已知变体归一化（\xad→"-", "・"→"•", U+00A0→" "）
   - 查丢字/漏转；允许的差异：於/于（转换补丁）、标点变体、原 PDF 固有乱码
2. 顺序级（参考指标）：SequenceMatcher ratio
   - 表格头/竖排页与原始流顺序不同是预期的（重建后更符合阅读顺序）
3. 像素级：1.0x 渲染暗像素密度对比（抽样 20 页，阈值 0.02）
   - ⚠️ 0.5x 渲染对细字抗锯齿严重，密度检测会失效（几乎全 0），必须 1.0x
4. 繁体残留：用"仅简繁字形不同"的特征字串统计（⚠️ 别混入同形字，见踩坑 21）
```

**真实验证结果（可作验收基准）**：
- 智谱 504 页：内容级仅剩 於/于×3 页、…→⋯×4 页；像素级 0 异常；改字 span 22579
- MiniMax 716 页：内容级仅剩 於/于×1 页、安永信头页×2；像素级 0 异常；改字 span 25430

## 7. 已知限制 / 最终瑕疵（视觉均无影响，属提取层细节）

1. **标点 unicode 变体**：`-`(U+002D)→`\xad`(软连字符)、`•`(U+2022)→`・`(U+30FB)、`…`(U+2026)→`⋯`(U+22EF)。字体 ToUnicode 一对多映射所致，**页面显示完全一样**，复制文本时得到变体字符。子集化/不子集化、改名/不改名都复现，目前无法根治（PyMuPDF 嵌入字体生成 ToUnicode 的固有行为）。
2. **安永信头页**（MiniMax 537/606）：原始 PDF 该字体 ToUnicode 本就乱码（提取出 `⭰㰟㛪姯…` 怪字符），重绘用原字体后**视觉 100% 还原**（Ernst & Young 字样正常显示），但提取仍有 8 个字符为 `\x00`。这是原始 PDF 固有乱码，非转换回归。
3. **字体风格**：原始细宋体（MSungHK-Light）正文替换为标准宋体（Songti Regular），笔画略粗，墨迹可增加 30%~135%（MiniMax 第 7 页实测），属可接受的字形替换。
4. **提取顺序**：个别竖排/表格密集页的提取顺序与原始内容流顺序不同（重建后更符合视觉阅读顺序）。竖排段落因其 line bbox y0 取顶部会排到页首。
5. **文件体积**：不子集化导致输出约 60MB+（原始 8MB 左右），字体完整嵌入所致。

## 8. 踩坑全记录（22 条，按主题分组）

### A. 环境与 API（第 1-5 条）
1. **PEP 668**：Homebrew Python 3.14 拒绝 `pip install` 全局安装 → 必须 `python3 -m venv .venv`。
2. **rawdict 是字符级结构**：`get_text("rawdict")` 的 span **没有 "text" 键**（dict 模式才有），需 `"".join(c["c"] for c in span["chars"])`。
3. **TextWriter.append 无颜色参数**（PyMuPDF 1.28）：签名只有 `(pos, text, font, fontsize, language, right_to_left, small_caps)`。颜色在 `TextWriter(page.rect, color=...)` 或 `write_text(page, color=...)` 设置 → 必须按颜色分组。
4. **颜色格式**：`add_redact_annot(text_color=...)` 和 `Shape.insert_text(color=...)` 都要求 **0-1 浮点元组**，不是 int（`((c>>16)&255)/255, ...`）。
5. **PyMuPDF 1.28 API 改名**：没有 `doc.add_stream`/`doc.add_xref`（旧版）——创建新流是 `xref = doc.get_new_xref()`，且 `update_stream` 要求 xref 对象已是 PDF dict（需先 `xref_set_key` 或用其他方式初始化）。最终没走这条路，改用 Shape（见踩坑 12）。

### B. 文本顺序与 MuPDF 提取机制（第 6-12 条）
6. **TextWriter 整体重绘 → 提取顺序乱**：TextWriter 把所有文本追加到内容流**末尾**，MuPDF 提取按内容流顺序分块 → 未改文本（旧流）全部在前、改写文本（新流）全部在后 → 复制/搜索顺序错乱（视觉正确）。
7. **跨字体行合并失败**：MuPDF 提取的行排序依赖**字体 ascent/descent**（行 bbox y0 = baseline - ascent×size）。Times（ascent≈0.68）与 CJK 字体混排时，行 y0 估算错位 → "香港结算EIPO渠道"中的 EIPO 被拆成独立行并排到句尾。**用原字体度量一致时行合并行为与原始一致**（这也解释了为何优先用原字体）。
8. **整行单字体方案**：同一视觉行所有 span 用同一字体重绘 → 行合并成功（EIPO 归位）→ 但表格头仍乱。
9. **表格头乱序根因**：表格头行距 ~14pt 落在 MuPDF 行合并容差内，相邻行合并后行内按 x0 排序 → 提取顺序由"列优先"变"行优先"。内容完整、视觉正确，**可接受**，无需修复。
10. **原始 PDF 提取顺序本身有怪癖**：页脚"– iii –"物理在页面底部但在内容流开头 → 原始提取就在文本最前。重建后按位置排序页脚在最后，**更合理**。验证时不要追求与原始顺序完全一致。
11. **逐行独立流方案（思路验证）**：MuPDF 提取**块间按内容流顺序、块内按位置排序** → 只要每个视觉行一个内容流，提取顺序就完全可控。这是最终方案的原理基础。
12. **Shape.commit() = 每行一个内容流的正道**：`fitz.Shape(page)` + 行内 insert_text + `shape.commit()`，每次 commit 追加一个独立内容流，实现简单可靠（替代手写内容流/append_stream，后者 API 不存在）。

### C. 竖排文本（第 13-14 条）
13. **竖排检测陷阱**：竖排文本（沿 y 轴排列）的 `line["dir"] = (0, -1)` 但 **wmode = 0**！用 `wmode==1` 过滤会漏掉。竖排 span 的 bbox 是竖条（宽≈字号、高几十到几百 pt）。**第一版把它们横排重绘 + 宽度收缩到 6pt 微小字 = 视觉严重错误**（"第一階段："变蚂蚁字）。修复：`vdir[0]==0 and abs(vdir[1])==1` 判定竖排，**逐字符**按 `span["chars"]` 中每字符的 origin 重绘，保持竖排排列。
14. **竖排 span 必须也 redact**：竖排 bbox 高数百 pt，会与多个横排 span 的 redact 矩形重叠 → 不 redact 竖排的话其文本会被横排的 redact 误切（第 135 页实测丢 10 字符）。正确做法：**所有 span（含竖排）全部 redact，然后全部重绘**。

### D. OpenCC（第 15 条）
15. **"於"不转换**：OpenCC t2s 词表对独立"於"（介词）不转换（"基於"、"滿足於"、"撥備於"实测残留）→ 必须手动补丁 `text.replace("於", "于")`。t2s 方向无"皇后/后"类歧义（那是 s2t 的问题）。

### E. PyMuPDF 保存层两个致命 bug（第 16-18 条）
16. **`doc.subset_fonts()` ToUnicode bug**：子集化后部分字符提取成控制字符（"财务资料"→`_\x08\x08\x16\x07\x9d§`）。**禁用 subset_fonts**（代价：文件 35MB→62MB，值得）。
17. **0.5x 渲染密度检测失效**：细字在 0.5x 下抗锯齿后暗像素阈值检测不到 → 像素级验证必须用 1.0x 渲染。
18. **TextWriter 字体资源 bug（最隐蔽）**：每行 write_text 重复注册字体资源（F0-F99），且按 **PS 名**复用页面/文档已有字体——系统宋体 PS 名恰好是 `STSongti-SC-Regular`，与智谱 PDF 原嵌入字体**撞名** → 复用了原 PDF 的**子集字体**（缺简体字形）→ notdef → 保存后提取乱码（span font=''）。**双重修复**：① 放弃 TextWriter 改回 Shape（自定义资源名 ZS0/ZS1/ZR0/ZR1，绝不撞名）；② 系统字体用 fontTools 把 PS 名加 `ZT2S-` 前缀。注意改名前先验证"保存前提取正常、保存后乱"来定位。

### F. 原字体与杂项（第 19-22 条）
19. **Shape.insert_text 的 fontfile 只收路径**（不收 buffer）→ 原 PDF 内嵌字体 buffer（`doc.extract_font(xref)[3]`）需写临时文件（`/tmp/pdf-t2s-origfonts/f<idx>.<ext>`）。`fitz.Font(fontbuffer=...)` 加载 buffer 正常（用于测量），但绘制必须走文件路径。
20. **原始 PDF 固有乱码字体**（安永信头页）：原字体 ToUnicode 本身就是错的（提取出 `⭰㰟㛪姯⸒Ṳ⋀㈧`），用原字体重绘后视觉还原、提取仍乱。非回归，验证时白名单。
21. **繁体残留检测的特征串必须只含简繁字形不同的字**：曾把"申/理/定/事/市/基/技/算/根/相/交/服/客/董/透/品/系/目/用"等**同形字**误入特征串 → 误报 8500+ 残留。正确特征串（验证用）：`發臺灣與車裡後臺為資產數據網絡體現覺說歷時閣於頻號註冊透過會計審計財務總經理頁碼連結項目標準基礎根據規定經營業務範圍開發技術系統資訊產品用戶關`。
22. **验证时无法目视图片**：Read 工具在此环境不支持显示图片（"Unsupported Image"）→ 视觉检查只能靠墨迹密度/条带分析（1.0x 渲染）。

## 9. 现有资产位置（本次开发产物，可直接复用）

| 资产 | 路径 | 说明 |
|---|---|---|
| 最终转换脚本 | `/Users/zm_bad/Downloads/pdf-t2s/pdf_t2s.py` | 含全部修复逻辑，`INPUTS` 写死为两份 PDF，需参数化 |
| 验证脚本 | `/Users/zm_bad/Downloads/pdf-t2s/verify.py` | 三级验证，`PAIRS` 写死，需参数化 |
| 字体（改名版，无版权风险→不入库） | `/Users/zm_bad/Downloads/pdf-t2s/t2s-*.ttf` | ZT2S- 前缀版（t2s-sans-regular/bold、t2s-serif-regular/bold、t2s-times、t2s-times-bold） |
| 字体（原始提取版） | `/Users/zm_bad/Downloads/pdf-t2s/sans-regular.ttf` 等 | 未改名版，**仅作中间产物** |
| venv | `/Users/zm_bad/Downloads/.pdfzh-venv` | pymupdf+opencc+fonttools，可直接复用 |
| 原字体临时目录 | `/tmp/pdf-t2s-origfonts/` | 每文档提取的原字体临时文件（运行时生成） |
| 输出样例 | `/Users/zm_bad/Downloads/智谱招股书_中文_简体.pdf`、`MiniMax招股书_简体.pdf` | 验收基准 |
| 原始 PDF | `/Users/zm_bad/Downloads/智谱招股书_中文.pdf`、`MiniMax招股书.pdf` | 测试素材 |
| 测试产物 | `/Users/zm_bad/Downloads/pdf-t2s/smoke_test.py`、`orig_p6.png` 等 | 开发期调试产物，可清理 |

**复制进仓库时**：脚本需参数化输入路径（`sys.argv` / argparse），`INPUTS`/`PAIRS` 常量改为命令行参数；字体文件**不入库**（gitignore），新增 `prepare_fonts.py` 负责在目标机器用 fontTools 从系统 ttc 提取 face + PS 名改名。

## 10. 后续开发指引（给 coding agent）

1. **仓库结构**（规划）：
   ```
   pdf-t2s/
   ├── SKILL.md              # 核心（必需）：frontmatter name/description + 流程 + 踩坑表
   ├── scripts/
   │   ├── pdf_t2s.py        # 转换主脚本（从现有资产参数化）
   │   ├── verify.py         # 验证脚本（参数化）
   │   └── prepare_fonts.py  # 字体准备（提取 face + ZT2S- 改名）
   ├── requirements.txt      # pymupdf opencc fonttools
   ├── README.md             # 人类使用说明
   ├── .gitignore            # 至少排除: *.ttf、.venv/、__pycache__/、*.pdf（样本）
   └── tests/                # skill 验收场景
   ```
2. **SKILL.md 要点**（按 writing-skills 规范）：
   - `name: pdf-t2s`（仅字母数字连字符）；`description` 以 "Use when..." 开头，只写触发条件不写工作流，铺满检索关键词（繁体、简体、繁体中文 PDF、简繁转换、OpenCC、PyMuPDF、招股书、保持排版、local、non-OCR 等，中英文都放）
   - 正文：Overview → When to Use（含"扫描版/无文本层 → 告知需 OCR，不硬转"）→ 核心流程 → **常见坑表（本文第 8 节是唯一权威来源）** → 验证门槛（必须跑 verify.py）
   - 铁律：**不跑验证不算完成**；**不 subset_fonts**；**不用 TextWriter**；**字体不入库**
3. **TDD 流程**（writing-skills 规范要求）：先跑基线压力场景（subagent 无 skill 时大概率选"在线工具"或"提取重排"）→ 写 skill → 验证 agent 按流程走完并跑验证。
4. **git 提交规范**：conventional commits（feat/fix/docs/refactor），提交信息末尾加 `Co-Authored-By: Claude <noreply@anthropic.com>`。
5. 验收测试素材：上述两份 PDF 在 /Users/zm_bad/Downloads（敏感文件，**勿提交 git**）。
6. 若需支持扫描版 PDF：另议 OCR 方案（ocrmypdf/tesseract chi_tra→chi_sim 或 MinerU），不在本 skill v1 范围。

## 11. 参考资料

- OpenCC: https://github.com/BYVoid/OpenCC（pip 包名 `opencc`，配置 `t2s`）
- PyMuPDF 文本替换官方指南: https://artifex.com/blog/how-to-search-and-replace-text-in-pdfs-using-pymupdf
- PyMuPDF 红action 讨论（保持布局）: https://github.com/pymupdf/PyMuPDF/discussions/3499
- 现成参考实现: https://github.com/ShawnJim/PDFTextReplacer（PyMuPDF，但未处理本 handoff 中的各坑）
- Agent 技能规范: https://agentskills.io/specification
