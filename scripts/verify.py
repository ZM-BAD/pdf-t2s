#!/usr/bin/env python3
"""转换结果验证（五维，见 spec/001-pdf-t2s-core.md）：
1. 内容级：每页字符 multiset 对比（OpenCC(原文) vs 输出）——查丢字/漏转；
   差异按白名单分类（於/于 补丁、标点变体、原始固有乱码）
2. 顺序级：每页归一化序列对比 ratio（参考指标——输出 = 视觉阅读序，
   与原始内容流序不同是预期）
3. 字体保真：未转换 span 的输出字体必须等于原字体（去前缀 PS 名），
   统计复用 / 替换 / 回退 / 收缩
4. 像素级：抽样页（均匀 + 全部竖排页 + 收缩页）墨迹覆盖率对比
5. 竖排：竖排列字符位置逐点核对（0 缺失）

用法:
  python3 verify.py 原始.pdf 输出_简体.pdf [原始2.pdf 输出2.pdf ...]
"""

import argparse
import os
import re
import sys
from collections import Counter
from difflib import SequenceMatcher

import fitz
import opencc

TRAD = (
    "發臺灣與車裡後臺為資產數據網絡體現覺說歷時閣於頻號註冊過會計"
    "審計財務總經頁碼連結項標準礎據規經營業務範圍開發術"
    "統資訊戶關"
)  # 仅简繁字形不同的字（混入同形字会误报）

# 内容级白名单：转换补丁与已知提取映射差异（页面显示一致）
WHITELIST_CHARS = {"於", "于", "\xad", "-", "・", "•", "…", "⋯", "\u00a0", " ", "\x00"}


def is_common_char(c):
    """常用字符范围（ASCII、拉丁扩展、通用标点、CJK 标点/汉字、全角）"""
    o = ord(c)
    return (
        0x20 <= o <= 0x7E
        or 0xA0 <= o <= 0x24F
        or 0x2000 <= o <= 0x206F
        or 0x2E80 <= o <= 0x9FFF
        or 0x3000 <= o <= 0x303F
        or 0xF900 <= o <= 0xFAFF
        or 0xFF00 <= o <= 0xFFEF
    )


def whitelisted_diff(miss, extra, cc):
    """差异分类：白名单（於/于、标点变体、固有乱码页）。
    固有乱码：输出侧 \\x00，且缺失字符全部为不可转换、非 TRAD、
    非常用字符（原始字体 ToUnicode 本身错误，如安永信信头页）。"""
    diffs = set(miss) | set(extra)
    return diffs <= WHITELIST_CHARS or (
        "\x00" in extra
        and all(
            cc.convert(c) == c and c not in TRAD and not is_common_char(c) for c in miss
        )
    )  # 固有乱码: 输出 \x00 且缺失为非常用字符（安永信信头页）


PIXEL_THRESHOLD = 0.02
ORDER_THRESHOLD = 0.99
FONT_REUSE_TARGET = 0.99  # 原字体可提取的未转换 span 复用率目标


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


def doc_fonts_map(doc):
    """文档中可映射的内嵌字体: {去前缀PS名: fitz.Font}
    按 xref 去重；可映射 = 至少一个字符能通过 has_glyph 探测
    （CID CFF 等无 Unicode cmap 的字体探测失败，转换器会回退系统字体）。"""
    seen = {}
    for pno in range(len(doc)):
        for row in doc[pno].get_fonts(full=True):
            seen.setdefault(row[0], row[3])
    out = {}
    for xref, basefont in seen.items():
        try:
            buf = doc.extract_font(xref)[3]
            if not buf:
                continue
            f = fitz.Font(fontbuffer=buf)
            if f.has_glyph(ord("A")) or f.has_glyph(ord("中")):
                out[basefont.split("+")[-1]] = f
        except Exception:  # noqa: BLE001, S110 — 单个字体提取失败不中断验证
            pass
    return out


def vcolumns(page, min_run=3):
    """竖排列: {x: [(y, char) 按 y 排序]}，仅 ≥min_run 字符的连续列"""
    cols = {}
    for b in page.get_text("rawdict")["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            for s in l["spans"]:
                for c in s["chars"]:
                    x, y = c["origin"]
                    cols.setdefault(round(x, 1), []).append((y, c["c"]))
    out = {}
    for x, lst in cols.items():
        lst.sort()
        runs, run = [], [lst[0]]
        for i in range(1, len(lst)):
            if lst[i][0] - lst[i - 1][0] <= 2.5:
                run.append(lst[i])
            else:
                runs.append(run)
                run = [lst[i]]
        runs.append(run)
        for r in runs:
            if len(r) >= min_run:
                out[x] = r
    return out


def check_font_fidelity(d1, d2, cc):
    """字体保真: 未转换 span（转换前后文本相同）核对输出字体。
    输出字体 != 原字体时分类：原字体可映射该 span 全部字符 → 替换（异常）；
    原字体不可映射 → 预期回退。"""
    fonts_map = doc_fonts_map(d1)
    reused = substituted = fallback = shrink = unmatched = 0
    shrink_pages = set()
    examples = []
    for pno in range(len(d1)):
        # 原始页未转换 span: 文本 -> {(字体, 字号)}
        orig = {}
        for b in d1[pno].get_text("rawdict")["blocks"]:
            if b["type"] != 0:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    t = "".join(c["c"] for c in s["chars"])
                    if not t.strip():
                        continue
                    if cc.convert(t).replace("於", "于") != t:
                        continue  # 只核对未转换 span
                    orig.setdefault(t, set()).add((s["font"].split("+")[-1], s["size"]))
        unmatched += len(orig)
        matched_texts = set()
        for b in d2[pno].get_text("rawdict")["blocks"]:
            if b["type"] != 0:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    t = "".join(c["c"] for c in s["chars"])
                    if not t.strip() or t not in orig:
                        continue
                    matched_texts.add(t)
                    fonts_, sizes_ = zip(*orig[t])
                    fout = s["font"].split("+")[-1]
                    if any(fout == f for f in fonts_):
                        reused += 1
                    elif any(f in fonts_map for f in fonts_):
                        # 原字体可映射全部字符才应复用；否则是预期回退
                        could_reuse = any(
                            all(fonts_map[f].has_glyph(ord(c)) for c in t)
                            for f in fonts_
                            if f in fonts_map
                        )
                        if could_reuse:
                            substituted += 1
                            if len(examples) < 8:
                                examples.append((pno + 1, t[:16], set(fonts_), fout))
                        else:
                            fallback += 1
                    else:
                        fallback += 1  # 原字体不可提取/不可映射，回退系统字体（预期）
                    if s["size"] < min(sizes_) * 0.95:
                        shrink += 1
                        shrink_pages.add(pno)
        unmatched -= len(matched_texts)
    rate = reused / max(1, reused + substituted)
    return {
        "reused": reused,
        "substituted": substituted,
        "fallback": fallback,
        "unmatched": unmatched,
        "shrink": shrink,
        "shrink_pages": shrink_pages,
        "rate": rate,
        "examples": examples,
    }


def check_vertical(d1, d2):
    """竖排: 原始竖排字符位置在输出中逐点存在（容差 1pt）"""
    missing = 0
    vpages = 0
    for pno in range(len(d1)):
        v1 = vcolumns(d1[pno])
        if not v1:
            continue
        vpages += 1
        v2 = vcolumns(d2[pno])
        for x, lst in v1.items():
            for y, ch in lst:
                found = any(
                    abs(y2 - y) <= 1.0
                    for x2, lst2 in v2.items()
                    if abs(x2 - x) <= 1.0
                    for y2, _ in lst2
                )
                if not found:
                    missing += 1
    return vpages, missing


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
            # 找出差异字符并按白名单分类
            ca, cb = Counter(a), Counter(b)
            miss = list((ca - cb).elements())[:10]
            extra = list((cb - ca).elements())[:10]
            whitelisted = whitelisted_diff(miss, extra, cc)
            multiset_bad.append((pno, miss, extra, whitelisted))
        r = SequenceMatcher(None, a, b).ratio()
        if r < ORDER_THRESHOLD:
            order_bad.append((pno, round(r, 4)))
        trad_left += sum(1 for c in b if c in TRAD)

    not_white = [(p, m, e) for p, m, e, w in multiset_bad if not w]
    white = [p for p, m, e, w in multiset_bad if w]
    print(
        f"内容级: {len(multiset_bad)}/{len(d1)} 页差异 "
        f"(白名单 {len(white)} 页, 非白名单 {len(not_white)} 页)",
        flush=True,
    )
    if not_white:
        print(f"  ⚠️ 非白名单差异: {not_white[:8]}", flush=True)
    print(
        f"顺序级: {len(order_bad)}/{len(d1)} 页 ratio<{ORDER_THRESHOLD} "
        f"-> {order_bad[:10] if order_bad else ''}（参考指标）",
        flush=True,
    )
    print(f"繁体残留特征字总数: {trad_left}", flush=True)

    ff = check_font_fidelity(d1, d2, cc)
    print(
        f"字体保真: 未转换 span 复用率 {ff['rate'] * 100:.2f}% "
        f"(复用 {ff['reused']}, 替换 {ff['substituted']}, "
        f"回退 {ff['fallback']}, 未匹配 {ff['unmatched']})",
        flush=True,
    )
    print(
        f"  收缩 span: {ff['shrink']} 个 (页: {sorted(p + 1 for p in ff['shrink_pages'])[:10]})",
        flush=True,
    )
    if ff["examples"]:
        print(f"  替换示例: {ff['examples'][:5]}", flush=True)

    vpages, v_missing = check_vertical(d1, d2)
    print(f"竖排: {vpages} 页含竖排, 位置缺失 {v_missing} 个", flush=True)

    # 像素级: 均匀抽样 + 全部竖排页 + 收缩页
    step = max(1, len(d1) // 20)
    sample = (
        set(range(0, len(d1), step))
        | ff["shrink_pages"]
        | {pno for pno in range(len(d1)) if vcolumns(d1[pno])}
    )
    pix_bad = []
    for pno in sorted(sample):
        pix1 = d1[pno].get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
        pix2 = d2[pno].get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
        d_ink = abs(ink_density(pix1) - ink_density(pix2))
        if d_ink > PIXEL_THRESHOLD:
            pix_bad.append((pno, round(d_ink, 4)))
    print(
        f"像素级: {len(pix_bad)}/{len(sample)} 页墨迹差异异常 "
        f"-> {pix_bad if pix_bad else ''}",
        flush=True,
    )
    print(
        f"输出: {os.path.basename(dst)} {os.path.getsize(dst) / 1024 / 1024:.1f}MB",
        flush=True,
    )

    # 验收判定（spec.md 验收标准 1-6）
    ok = (
        not not_white
        and trad_left == 0
        and ff["rate"] >= FONT_REUSE_TARGET
        and not pix_bad
        and v_missing == 0
    )
    print(
        f"验收: {'✅ 通过' if ok else '❌ 未通过'} "
        f"(非白名单差异 {len(not_white)} + 繁体残留 {trad_left} + "
        f"复用率 {ff['rate'] * 100:.1f}%(≥{FONT_REUSE_TARGET * 100:.0f}%) + "
        f"像素异常 {len(pix_bad)} + 竖排缺失 {v_missing})",
        flush=True,
    )


def main():
    # Windows 上 stdout 重定向（管道/CI 日志）时用 locale 编码会中文乱码/报错
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="验证 pdf_t2s.py 的转换结果（五维验证）"
    )
    parser.add_argument(
        "pairs", nargs="+", help="成对的 <原始.pdf> <输出_简体.pdf>，可多对"
    )
    args = parser.parse_args()
    if len(args.pairs) % 2 != 0:
        parser.error("参数必须是成对的 <原始.pdf> <输出_简体.pdf>")
    for i in range(0, len(args.pairs), 2):
        src, dst = args.pairs[i], args.pairs[i + 1]
        print(f"=== {os.path.basename(src)}", flush=True)
        try:
            check_pair(src, dst)
        except Exception as e:  # noqa: BLE001 — 逐对隔离错误，全部验完再退出
            print(f"  验证失败: {e}", flush=True)


if __name__ == "__main__":
    main()
