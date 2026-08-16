# 字体保真的 PDF 繁简转换 — plan

## Slices

1. **Slice 1 — 字体选择规范实现**（scripts/pdf_t2s.py）：
   - `font_key(name)`：字体名去除 subset 前缀（`SPNAJD+MHeiHK-Bold` → `MHeiHK-Bold`）
   - `load_doc_fonts`：以去前缀 PS 名为键；非嵌入字体（无 buffer）跳过
   - 横排字体选择：转换过 → 系统字体（含 CJK 按类 / 纯拉丁 Times）；未转换 → 原字体（去前缀匹配）；原字体不可提取 → 回退（纯拉丁 Times / 含 CJK 系统字体）
   - 宽度收缩：以实际绘制字体度量（原字体 span 不触发）
   - 竖排逐字符：同一选择规则
   - ZO 资源名改用确定性命名（crc32），保证同输入同输出
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

4. **Slice 4 — 两阶段子集化（v1.1，scripts/pdf_t2s.py）**：
   - 提取 `pick_font(fonts, doc_fonts, fname, bold, orig_text, new_text)`：横排/竖排统一的字体选择（转换过 → fallback；未转换 + 原字体可映射 → original；否则 → fallback），返回 (资源名, 文件路径, 测量用 fitz.Font)
   - `collect_needed_chars(doc, cc, fonts, doc_fonts_all)`：阶段 1 字符收集——全文档逐页 collect_rows + 转换 + pick_font，按字体文件路径累积字符集（竖排逐字符）
   - `subset_system_fonts(needed)`：阶段 2 子集化——仅系统字体路径（CJK_FONTS/LATIN_SERIF 值集），fontTools `Subsetter(unicodes_requested=...)` 输出到 `/tmp/pdf-t2s-subsets/`，异常回退原路径
   - `rebuild_page` 增加 font_path_map 参数：insert_text 的 fontfile 经映射替换；测量仍用完整字体对象
   - 验证：pick_font 与原逻辑等价（合成 PDF 断言字体选择不变）；子集化后体积缩小、PS 名保留 `ZT2S-` 前缀、提取无乱码

5. **Slice 5 — 验收（v1.1）**：
   - 两份验收文档全量转换 + 五维验证
   - 验收标准 1-10 全部通过（1-8 与 v1.0 一致；9 体积 ≤ 15MB 且减 ≥ 70%；10 全过）
   - 体积报告写入转换输出，spec status → shipped

## Verification

- Per slice: 单元测试通过（venv 全量、裸 python 优雅跳过）
- Acceptance: 验收标准 1-10（spec.md）对两份真实文档全量执行
- Rollback: `git restore scripts/pdf_t2s.py scripts/verify.py tests/ spec/`（当前无提交改动，直接还原）
