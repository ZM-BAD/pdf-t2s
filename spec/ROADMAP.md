# Spec Roadmap

Spec 可以领先代码几个里程碑，但**越远越轻**：近期（下一个要做的）写完整规范，远期只写目标/边界，轮到时再补全。

## Milestone Overview

| Spec | 里程碑 | 状态 | 验收（门禁） |
| ---- | ------ | ---- | ------------ |
| [001](./001-pdf-t2s-core.md) | 核心繁转简：逐行重建文本层 + 字体保真 + 五维验证 | 🟢 shipped | 智谱 504 页 / MiniMax 716 页五维全过（复用率 99.52% / 99.95%，像素 0 异常，竖排 0 缺失） |
| [002](./002-output-size-optimization.md) | 输出体积优化：fontTools 预子集化 + 死字体清理 | 🟢 shipped | 体积减 ≥70%（智谱 15.8MB / MiniMax 14.6MB）+ 五维保持全过（复用率 99.38% / 99.94%） |
| [003](./003-parallel-conversion.md) | 并行转换：多进程分块重建 + 合并 | 🟢 shipped | 五维保持全过 + 加速 ≥2x（智谱 2.25x / MiniMax 2.39x）+ 体积不增（14.3 / 14.0MB，均小于串行） |

依赖顺序：**001 → 002 → 003**（003 建立在 001 的重建流程与 002 的子集化之上；其全局字符收集阶段依赖 002 的两阶段架构）。

## 原则（所有 spec 通用）

- **Spec = current truth**：决策变更时直接覆盖原文，不保留删除线、不写"我们改过"注记。Git 是历史，spec 只反映当前。
- **One theme per milestone**：一个 spec 一个主题，不捆绑无关工作。
- **每个特性以 spec 开始**：proposed → approved → in-progress → shipped；模板见 [000-spec-template.md](./000-spec-template.md)，Implementation Plan 在各 spec 内，里程碑状态见上表。
- **实现依赖可验证门槛**：验收标准逐条可核对，不以"看起来对"为准。

## 不单独建 spec 的项

- **扫描版 PDF（OCR）**：明确放弃——无 OCR 方案/计划，输入直接拒绝（见 [001](./001-pdf-t2s-core.md) Non-goals）
- **新输入平台/新字体来源**：`prepare_fonts.py` 的字体来源与 `font_kind` 分类是扩展点，不属独立里程碑
- **测试基建 / CI 门槛**：质量门（UT + lint + format + pre-commit）是仓库约定，不进 spec
