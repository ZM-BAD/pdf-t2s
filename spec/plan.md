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

## Verification

- Per slice: 单元测试通过（venv 全量、裸 python 优雅跳过）
- Acceptance: 验收标准 1-8（spec.md）对两份真实文档全量执行
- Rollback: `git restore scripts/pdf_t2s.py scripts/verify.py tests/`（当前无提交改动，直接还原）
