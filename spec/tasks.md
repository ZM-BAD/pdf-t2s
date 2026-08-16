# 字体保真的 PDF 繁简转换 — tasks

## Slice 1 — 字体选择规范实现

- [x] pdf_t2s.py: 新增 `font_key()`（去 subset 前缀）
- [x] pdf_t2s.py: `load_doc_fonts` 以去前缀名为键 + 跳过无 buffer 字体
      （按页匹配该页实际引用的子集字体）
- [x] pdf_t2s.py: 横排字体选择四分支（转换/未转换 × 原字体可用性）
- [x] pdf_t2s.py: 宽度收缩以实际绘制字体度量
- [x] pdf_t2s.py: 竖排逐字符同一规则 + ZO 确定性命名（crc32）
- [x] tests: font_key 单测 + 合成 PDF 重建保字体单测
- [x] 运行测试全绿（12 tests）

## Slice 2 — verify.py 字体保真维度

- [x] verify.py: `extractable_fonts()`（按 xref 去重）
- [x] verify.py: 未转换 span 字体核对 + 复用/替换/回退/未匹配/收缩统计
- [x] verify.py: 内容级白名单分类
- [x] verify.py: 竖排位置核对维度
- [x] verify.py: 像素抽样含竖排页与收缩页
- [x] 旧输出实测：复用率 63.5% → 验收 ❌（维度有效检出）

## Slice 3 — 验收

- [x] 智谱 504 页转换 + 五维验证：✅（复用率 99.52%，内容级 3 页全白名单，像素 0/120，竖排 0 缺失）
- [x] MiniMax 716 页转换 + 五维验证：✅（复用率 99.95%，内容级 3 页全白名单，像素 0/254，竖排 0 缺失）
- [x] 验收标准 1-8 全部通过
- [x] spec status → shipped

## Slice 4 — 两阶段子集化（v1.1 输出体积优化）

- [x] pdf_t2s.py: 提取 `pick_font()`（横排/竖排统一字体选择，两阶段复用）
- [x] pdf_t2s.py: `collect_needed_chars()` 阶段 1 字符收集（全文档，竖排逐字符）
- [x] pdf_t2s.py: `subset_system_fonts()` 阶段 2（fontTools subset，仅系统字体，失败回退）
- [x] pdf_t2s.py: `rebuild_page` 经 font_path_map 替换 fontfile（测量仍用完整字体）
- [x] tests: pick_font 等价 + 子集化（体积缩小 / PS 名保留 / 提取无乱码）
- [x] 运行测试全绿（18 tests）+ lint/format 通过
- [x] 30 页冒烟：64.5MB → 13.1MB，五维 ✅（复用率 100%、像素 0 异常）
- [x] 修复并发竞争：ORIG/SUBSET 目录改 `tempfile.mkdtemp` 每进程独立（并发转换互相覆盖子集文件 → MiniMax 大范围 `\x00`，100 页复验 0 `\x00`）
- [x] purge_unused_fonts 健壮版：兼容 Resources/Font 直接字典与间接引用（`xref_get_keys` + 正则），资源名正则含下划线；单测覆盖间接引用场景

## Slice 5 — 验收（v1.1）

- [x] 智谱 504 页转换 + 五维验证：✅（复用率 99.38%，像素 0/120，竖排 0 缺失）+ 体积 15.9MB（减 74.4%）
- [x] MiniMax 716 页转换 + 五维验证：✅（复用率 99.94%，像素 0/254，竖排 0 缺失）+ 体积 14.6MB（减 76.1%）
- [x] 验收标准 1-10 全部通过（含并发竞争修复复验：100 页 0 `\x00`）
- [x] spec status → shipped
