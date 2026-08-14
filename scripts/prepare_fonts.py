#!/usr/bin/env python3
"""从 macOS 系统字体提取简体中文字体 face，供 pdf_t2s.py 重绘使用。

生成 6 个文件（默认输出到本脚本所在目录，即 pdf_t2s.py 查找字体的位置）:
  t2s-sans-regular.ttf / t2s-sans-bold.ttf   <- Hiragino Sans GB W3/W6
  t2s-serif-regular.ttf / t2s-serif-bold.ttf <- Songti SC Regular/Bold
  t2s-times.ttf / t2s-times-bold.ttf         <- Times New Roman + Bold

关键点（见 handoff.md 踩坑 18）:
  - 系统字体的 PostScript 名（name 表 nameID 6）必须加 "ZT2S-" 前缀，
    防止与 PDF 原嵌入字体撞名导致 MuPDF 字体资源复用错误
  - 这些字体来自 Apple/微软版权字体，禁止提交进 git（.gitignore 已排除 *.ttf）

用法:
  python3 prepare_fonts.py [--out-dir 目录]
"""

import argparse
import os
import sys

from fontTools.ttLib import TTCollection, TTFont

BASE = os.path.dirname(os.path.abspath(__file__))

SOURCES = [
    # (输出文件名, 源文件, 源文件内 face 索引, 描述)
    (
        "t2s-sans-regular.ttf",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        0,
        "黑体常规",
    ),
    ("t2s-sans-bold.ttf", "/System/Library/Fonts/Hiragino Sans GB.ttc", 2, "黑体粗体"),
    (
        "t2s-serif-regular.ttf",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        6,
        "宋体常规",
    ),
    (
        "t2s-serif-bold.ttf",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        1,
        "宋体粗体",
    ),
    (
        "t2s-times.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        None,
        "拉丁宋体",
    ),
    (
        "t2s-times-bold.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
        None,
        "拉丁宋体粗体",
    ),
]

PS_PREFIX = "ZT2S-"


def extract_face(path, idx):
    if idx is None:
        return TTFont(path)
    return TTCollection(path).fonts[idx]


def rename_ps_name(font):
    """给 PostScript 名（nameID 6）加 ZT2S- 前缀，防与 PDF 原字体撞名"""
    for record in font["name"].names:
        if record.nameID == 6 and not record.toUnicode().startswith(PS_PREFIX):
            record.string = PS_PREFIX + record.toUnicode()


def main():
    parser = argparse.ArgumentParser(
        description="从 macOS 系统字体提取 pdf_t2s 所需的中文字体"
    )
    parser.add_argument(
        "--out-dir", default=BASE, help="输出目录（默认: 本脚本所在目录）"
    )
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    for out_name, src, idx, desc in SOURCES:
        if not os.path.exists(src):
            print(f"!! 跳过 {desc}: 找不到 {src}（仅 macOS 可用？）", flush=True)
            continue
        out = os.path.join(args.out_dir, out_name)
        font = extract_face(src, idx)
        before = [r.toUnicode() for r in font["name"].names if r.nameID == 6][:1]
        rename_ps_name(font)
        after = [r.toUnicode() for r in font["name"].names if r.nameID == 6][:1]
        font.save(out)
        print(
            f"  {desc}: {before[0] if before else '?'} -> {after[0] if after else '?'} -> {out_name}",
            flush=True,
        )


if __name__ == "__main__":
    sys.exit(main())
