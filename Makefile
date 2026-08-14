# pdf-t2s 常用命令（均为仓库内真实命令，可溯源）
# 示例: make test PYTHON=.venv/bin/python3

PYTHON ?= python3

.PHONY: fonts test lint format convert verify

fonts: ## 生成重绘用字体 t2s-*.ttf（需要 fontTools，建议用 venv 的 python）
	$(PYTHON) scripts/prepare_fonts.py

test: ## 运行单元测试（无依赖环境自动跳过；venv 中全量运行）
	$(PYTHON) -m unittest discover

lint: ## ruff 静态检查（需要 ruff，建议用 venv 的 python）
	$(PYTHON) -m ruff check .

format: ## ruff 格式化全部代码
	$(PYTHON) -m ruff format .

convert: ## 转换 PDF: make convert INPUT=文件.pdf
	$(PYTHON) scripts/pdf_t2s.py $(INPUT)

verify: ## 验证转换: make verify PAIRS="原始.pdf 输出_简体.pdf"
	$(PYTHON) scripts/verify.py $(PAIRS)
