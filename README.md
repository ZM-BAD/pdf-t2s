# pdf-t2s

[![AI-Native 10.0/10](assets/badge.svg)](assets/audit-report.md)

将**繁体中文 PDF 转换为简体中文**并**完整保留原排版**（坐标、字号、颜色、表格、图片、竖排文本均不动），全程**本地处理**（不上传云端）。适用于招股书、年报、合同等敏感或需要保真的文件。

技术方案：OpenCC(t2s) 字符转换 + PyMuPDF 逐行重建文本层（redaction 删旧字 → 每视觉行一个内容流重绘）。

## 安装

```sh
# macOS（Apple Silicon 已验证；依赖 PEP 668，必须使用 venv）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 生成重绘用字体（从系统字体提取，Apple/微软版权字体不入库）
.venv/bin/python3 scripts/prepare_fonts.py
```

> 非 macOS 系统需自行准备 `scripts/t2s-*.ttf`（简体黑体/宋体/Times 各常规+粗体，PS 名以 `ZT2S-` 开头），或修改 `prepare_fonts.py` 中的字体来源。

## 使用

```sh
# 转换（可多个文件；输出默认与输入同目录，命名 <原名>_简体.pdf）
.venv/bin/python3 scripts/pdf_t2s.py 智谱招股书_中文.pdf MiniMax招股书.pdf

# 指定输出路径（仅单文件）
.venv/bin/python3 scripts/pdf_t2s.py 输入.pdf --output 输出.pdf

# 验证（必须！不验证不算完成）
.venv/bin/python3 scripts/verify.py 输入.pdf 输出_简体.pdf
```

`verify.py` 对原始/输出逐页做三级验证：内容级（字符 multiset 对比，查丢字/漏转）、顺序级（序列比对比率，参考指标）、像素级（1.0x 墨迹密度对比，查黑块/字体失效），并统计繁体残留特征字。

> 扫描版 PDF（无文本层）本工具不适用，需要 OCR 方案（ocrmypdf/tesseract 或 MinerU）。

## 目录结构

```text
├── SKILL.md               # Claude Code skill 定义（本技能完整说明）
├── scripts/
│   ├── pdf_t2s.py         # 转换主脚本
│   ├── verify.py          # 三级验证脚本
│   └── prepare_fonts.py   # 字体准备（提取 face + ZT2S- 改名）
├── requirements.txt       # pymupdf opencc fonttools
└── tests/                 # 单元测试（无依赖环境自动跳过）
```

## 开发

```sh
.venv/bin/python3 -m unittest discover   # 运行测试（venv 中全量运行）
python3 -m unittest discover             # 无依赖环境下测试自动跳过，作为 CI 门槛
```

开发背景、技术方案细节、22 条踩坑记录见 [handoff.md](handoff.md)。

## 已知限制

- 输出文件约 60MB（不子集化，完整字体嵌入——`subset_fonts` 有 ToUnicode bug）。
- 个别标点复制时得到 unicode 变体（页面显示完全一样）。
- 原始细宋体替换为标准宋体，笔画略粗。

## License

MIT（脚本与文档）。注意：`scripts/t2s-*.ttf` 由 Apple/微软版权字体提取，**禁止提交进 git**（`.gitignore` 已排除），由 `prepare_fonts.py` 在目标机器生成。
