#!/usr/bin/env python3
"""转换结果验证：
1. 内容级：每页字符 multiset 对比（OpenCC(原文) vs 输出，不敏感于顺序）——查丢字/漏转
2. 顺序级：每页归一化序列对比 ratio（参考指标，表格头可能有 MuPDF 行合并导致的顺序差异）
3. 繁体残留：输出页繁体特征字计数
4. 像素级：抽样页墨迹覆盖率对比（查红action 黑块、字体失效）

用法:
  python3 verify.py 原始.pdf 输出_简体.pdf [原始2.pdf 输出2.pdf ...]
"""

import argparse
import os
import re
from collections import Counter
from difflib import SequenceMatcher

import fitz
import opencc

TRAD = (
    "發臺灣與車裡後臺為資產數據網絡體現覺說歷時閣於頻號註冊過會計"
    "審計財務總經頁碼連結項標準礎據規經營業務範圍開發術"
    "統資訊戶關"
)  # 仅简繁字形不同的字（踩坑 21：混入同形字会误报）


def norm(s):
    # 已知提取映射瑕疵归一化: 连字符/项目符号被字体 ToUnicode 映射为变体
    s = s.replace("\xad", "-").replace("・", "•").replace(" ", " ")
    return re.sub(r"\s+", "", s)


def ink_density(pixmap):
    samples = pixmap.samples
    n = pixmap.n
    total = pixmap.width * pixmap.height
    dark = 0
    for i in range(0, len(samples), n * 7):
        r, g, b = samples[i], samples[i + 1], samples[i + 2]
        if 0.299 * r + 0.587 * g + 0.114 * b < 128:
            dark += 1
    return dark / (total / 7 + 1)


def check_pair(src, dst):
    cc = opencc.OpenCC("t2s")
    d1, d2 = fitz.open(src), fitz.open(dst)
    if len(d1) != len(d2):
        print(f"!! 页数不一致: {len(d1)} vs {len(d2)}", flush=True)
    multiset_bad, order_bad, trad_left = [], [], 0
    for pno in range(len(d1)):
        a = norm(cc.convert(d1[pno].get_text()))
        b = norm(d2[pno].get_text())
        if Counter(a) != Counter(b):
            # 找出差异字符
            ca, cb = Counter(a), Counter(b)
            miss = list((ca - cb).elements())[:10]
            extra = list((cb - ca).elements())[:10]
            multiset_bad.append((pno, miss, extra))
        r = SequenceMatcher(None, a, b).ratio()
        if r < 0.99:
            order_bad.append((pno, round(r, 4)))
        trad_left += sum(1 for c in b if c in TRAD)
    print(
        f"内容级: {len(multiset_bad)}/{len(d1)} 页异常 "
        f"-> {multiset_bad[:8] if multiset_bad else ''}",
        flush=True,
    )
    print(
        f"顺序级: {len(order_bad)}/{len(d1)} 页 ratio<0.99 "
        f"-> {order_bad[:10] if order_bad else ''}",
        flush=True,
    )
    print(f"繁体残留特征字总数: {trad_left}", flush=True)
    pix_bad = []
    for pno in range(0, len(d1), max(1, len(d1) // 20))[:20]:
        pix1 = d1[pno].get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
        pix2 = d2[pno].get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
        d_ink = abs(ink_density(pix1) - ink_density(pix2))
        if d_ink > 0.02:
            pix_bad.append((pno, round(d_ink, 4)))
    print(
        f"像素级: {len(pix_bad)}/20 页墨迹差异异常 -> {pix_bad if pix_bad else ''}",
        flush=True,
    )
    print(
        f"输出: {os.path.basename(dst)} {os.path.getsize(dst) / 1024 / 1024:.1f}MB",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(
        description="验证 pdf_t2s.py 的转换结果（三级验证）"
    )
    parser.add_argument(
        "pairs", nargs="+", help="成对的 <原始.pdf> <输出_简体.pdf>，可多对"
    )
    args = parser.parse_args()
    if len(args.pairs) % 2 != 0:
        parser.error("参数必须是成对的 <原始.pdf> <输出_简体.pdf>")
    for i in range(0, len(args.pairs), 2):
        src, dst = args.pairs[i], args.pairs[i + 1]
        print(f"=== {src.split('/')[-1]}", flush=True)
        try:
            check_pair(src, dst)
        except Exception as e:  # noqa: BLE001 — 逐对隔离错误，全部验完再退出
            print(f"  验证失败: {e}", flush=True)


if __name__ == "__main__":
    main()
