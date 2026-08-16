# pdf-t2s

[![AI-Native 10.0/10](assets/badge.svg)](assets/audit-report.md)

将**繁体中文 PDF 转换为简体中文**并**完整保留原排版**（坐标、字号、颜色、表格、图片、竖排文本均不动），全程**本地处理**（不上传云端）。适用于招股书、年报、合同等敏感或需要保真的文件。

技术方案：OpenCC(t2s) 字符转换 + PyMuPDF 逐行重建文本层（redaction 删旧字 → 每视觉行一个内容流重绘）。

## 特性

- **排版保真**：逐行重建文本层，坐标、字号、颜色、表格、图片、矢量线、竖排文本均保持不变
- **字形保真**：未转换的文本（数字、日期、同形字、标点、纯拉丁）复用原 PDF 内嵌字体，字形 100% 还原；转换过的文本使用系统简体字体，杜绝原字体子集缺字形导致的乱码
- **竖排支持**：竖排文本逐字符重绘，保持竖排排列与坐标
- **本地处理**：全程不离开本机，敏感文件无需上传云端
- **内置验证**：`verify.py` 五维验证（内容 / 顺序 / 字体保真 / 像素 / 竖排），不验证不算完成
- **批量转换**：一次命令处理多个 PDF

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

`verify.py` 对原始/输出逐页做五维验证：内容级（字符 multiset 对比，查丢字/漏转）、顺序级（序列比对，参考指标）、字体保真（未转换 span 复用原字体率 ≥ 99%）、像素级（1.0x 墨迹密度对比，查黑块/字体失效）、竖排（字符位置逐点核对），并统计繁体残留特征字，验收判定由脚本末尾输出 ✅/❌。

> 扫描版 PDF（无文本层）**明确不支持**：直接放弃，无 OCR 方案/计划——本工具只处理有文本层的 PDF，扫描版输入请改用其他 OCR 工具。

## 目录结构

```text
├── SKILL.md               # Claude Code skill 定义（完整使用说明）
├── scripts/
│   ├── pdf_t2s.py         # 转换主脚本
│   ├── verify.py          # 五维验证脚本
│   └── prepare_fonts.py   # 字体准备（提取 face + ZT2S- 改名）
├── requirements.txt       # pymupdf opencc fonttools
└── tests/                 # 单元测试（无依赖环境自动跳过）
```

## 开发

```sh
# 一次性：安装 dev 依赖（ruff / pytest / pre-commit，版本见 pyproject.toml dev extra）
.venv/bin/pip install -e ".[dev]"        # 或 uv sync --extra dev

.venv/bin/python3 -m unittest discover   # 运行测试（venv 中全量运行）
python3 -m unittest discover             # 无依赖环境下测试自动跳过
.venv/bin/python3 -m pytest -q           # 单元测试（等价，推荐）
make lint                                # ruff 静态检查
make format                              # ruff 格式化
.venv/bin/pre-commit run --all-files     # 全部 hooks（UT + lint + format + 安全扫描）
```

pre-commit 与 CI 均包含 UT、lint、formatter 三关；CI 硬门槛见 `.github/workflows/ci.yml`。

## 已知限制

- 输出体积约 12-18MB（v1.1 起用 fontTools 预子集化系统字体；原 PDF 内嵌字体 ZO 完整嵌入以保证字形保真——`doc.subset_fonts()` 有 ToUnicode bug 不可用，预子集化绕开该路径）。
- 个别标点复制时得到 unicode 变体（页面显示完全一样）。
- 原始细宋体替换为标准宋体，笔画略粗。

## License

MIT（脚本与文档）。注意：`scripts/t2s-*.ttf` 由 Apple/微软版权字体提取，**禁止提交进 git**（`.gitignore` 已排除），由 `prepare_fonts.py` 在目标机器生成。
