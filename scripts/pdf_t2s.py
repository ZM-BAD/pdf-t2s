#!/usr/bin/env python3
"""繁体 PDF → 简体 PDF（逐行重建文本层方案 · 最终版）
流程:
  1. 逐页提取全部文本 span，按视觉行分组（rawdict 的 line）
  2. OpenCC(t2s) 转换含繁体字的 span（附 於→于 补丁）
  3. 整页所有 span 红action 真删除（图片/矢量线保留）
  4. 按视觉行排序（行 y0），每行一个 Shape 内容流，行内按 x0 重绘
     —— 提取块顺序 = 内容流顺序 = 视觉行顺序
  5. 字体策略:
     - 转换过的 span: 系统简体字体（宋体→Songti SC、黑体→Hiragino Sans GB、
       拉丁宋体→Times New Roman），PS 名已加 ZT2S- 前缀防与 PDF 原字体冲突
     - 未转换 span: 用原始内嵌字体（字形 100% 还原）
  6. 不使用 subset_fonts（其 ToUnicode 生成对部分字体映射错误），
     不用 TextWriter（其字体资源重复注册导致保存后部分字体提取乱码）

用法:
  python3 pdf_t2s.py 输入.pdf [输入2.pdf ...] [--output 输出.pdf]
  字体文件（t2s-*.ttf）由 prepare_fonts.py 生成，必须与脚本同目录。
"""

import argparse
import os
import time

import fitz
import opencc

BASE = os.path.dirname(os.path.abspath(__file__))
ORIG_FONT_DIR = "/tmp/pdf-t2s-origfonts"

CJK_FONTS = {  # (黑体/宋体, 常规/粗体) -> ttf
    ("sans", 0): os.path.join(BASE, "t2s-sans-regular.ttf"),
    ("sans", 16): os.path.join(BASE, "t2s-sans-bold.ttf"),
    ("serif", 0): os.path.join(BASE, "t2s-serif-regular.ttf"),
    ("serif", 16): os.path.join(BASE, "t2s-serif-bold.ttf"),
}
LATIN_SERIF = {
    0: os.path.join(BASE, "t2s-times.ttf"),
    16: os.path.join(BASE, "t2s-times-bold.ttf"),
}
BOLD_FLAG = 16


def load_fonts():
    fonts = {}
    for (kind, bold), path in CJK_FONTS.items():
        fonts[("cjk", kind, bold)] = fitz.Font(fontfile=path)
    for bold, path in LATIN_SERIF.items():
        fonts[("latin_serif", bold)] = fitz.Font(fontfile=path)
    for key, f in fonts.items():
        print(f"  字体 {key}: {f.name}", flush=True)
    return fonts


def is_cjk(c):
    return (
        0x2E80 <= ord(c) <= 0x9FFF
        or 0xF900 <= ord(c) <= 0xFAFF
        or 0xFF00 <= ord(c) <= 0xFFEF
    )


def font_kind(name):
    if "MHei" in name or "Hei" in name or "Sans" in name or "Gothic" in name:
        return "sans"
    return "serif"


def int2rgb(c):
    return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255


def collect_rows(page):
    """按视觉行返回 [(line_y0, [span...])]，span=(bbox, origin, text, size, color, fontname, bold, chars)
    chars: 竖排时逐字符 (char, origin) 列表；横排为 None"""
    rows = []
    for block in page.get_text("rawdict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            spans = []
            vdir = line.get("dir", (1.0, 0.0))
            vertical = vdir[0] == 0 and abs(vdir[1]) == 1  # 竖排: 文本沿 y 轴排列
            for s in line["spans"]:
                t = "".join(c["c"] for c in s["chars"])
                if not t or not t.strip():
                    continue  # 空/纯空白 span 不动
                bbox = fitz.Rect(s["bbox"])
                if bbox.is_empty or bbox.width <= 0 or bbox.height <= 0:
                    continue
                chars = None
                if vertical:
                    chars = [(c["c"], fitz.Point(c["origin"])) for c in s["chars"]]
                spans.append(
                    (
                        bbox,
                        fitz.Point(s["origin"]),
                        t,
                        s["size"],
                        s["color"],
                        s["font"],
                        s["flags"] & BOLD_FLAG,
                        chars,
                    )
                )
            if spans:
                rows.append((line["bbox"][1], spans))
    rows.sort(key=lambda r: r[0])
    return rows


def load_doc_fonts(doc):
    """提取文档全部内嵌字体: basefont(PostScript名) -> (fitz.Font, 临时文件路径)"""
    os.makedirs(ORIG_FONT_DIR, exist_ok=True)
    fonts = {}
    idx = 0
    for pno in range(len(doc)):
        for row in doc[pno].get_fonts(full=True):
            xref, ext, _, basefont, _, _, _ = row
            if basefont in fonts:
                continue
            try:
                buf = doc.extract_font(xref)[3]
                path = os.path.join(ORIG_FONT_DIR, f"f{idx}.{ext or 'bin'}")
                with open(path, "wb") as fh:
                    fh.write(buf)
                fonts[basefont] = (fitz.Font(fontbuffer=buf), path)
                idx += 1
            except Exception:  # noqa: BLE001, S110 — 单个损坏字体不中断整文档转换
                pass
    return fonts


def sys_font(fonts, fname, bold):
    """系统字体 -> (资源名, 文件路径)"""
    kind = font_kind(fname)
    if kind == "sans":
        return f"ZS{bold}", CJK_FONTS[("sans", bold)]
    return f"ZR{bold}", CJK_FONTS[("serif", bold)]


def latin_font(fonts, fname, bold):
    if "Times" in fname:
        return f"ZT{bold}", LATIN_SERIF[bold]
    return f"ZS{bold}", CJK_FONTS[("sans", bold)]


def rebuild_page(page, cc, fonts, doc_fonts):
    rows = collect_rows(page)
    if not rows:
        return 0
    # 1) 删除全部旧文本（图片/矢量线保留）
    for _, spans in rows:
        for bbox, *_ in spans:
            page.add_redact_annot(bbox)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE
    )
    # 2) 逐行重绘（每行一个 Shape 内容流，保持提取顺序 = 视觉行顺序）
    changed = 0
    for _, spans in rows:
        spans.sort(key=lambda sp: sp[0].x0)
        shape = fitz.Shape(page)
        for bbox, origin, text, size, color, fname, bold, chars in spans:
            new = cc.convert(text).replace("於", "于")
            if new != text:
                changed += 1
            if chars is not None:
                # 竖排文本：逐字符转换后按原坐标重绘，保持竖排排列
                for ch, ch_origin in chars:
                    ch_new = cc.convert(ch).replace("於", "于")
                    if ch_new != ch:
                        changed += 1
                        rn, fp = sys_font(fonts, fname, bold)
                    elif fname in doc_fonts:
                        rn, fp = f"ZO{abs(hash(fname)) % 9000}", doc_fonts[fname][1]
                    else:
                        rn, fp = sys_font(fonts, fname, bold)
                    shape.insert_text(
                        ch_origin,
                        ch_new,
                        fontname=rn,
                        fontfile=fp,
                        fontsize=size,
                        color=int2rgb(color),
                    )
            else:
                has_cjk = any(is_cjk(c) for c in new)
                if new != text or has_cjk:
                    rn, fp = (
                        sys_font(fonts, fname, bold)
                        if has_cjk
                        else latin_font(fonts, fname, bold)
                    )
                elif fname in doc_fonts:
                    rn, fp = f"ZO{abs(hash(fname)) % 9000}", doc_fonts[fname][1]
                else:
                    rn, fp = sys_font(fonts, fname, bold)
                # 简体字通常更窄；若比原矩形宽，等比缩小字号防溢出
                font = fonts[("cjk", font_kind(fname), bold)]
                fit_w = font.text_length(new, fontsize=size)
                if fit_w > bbox.width * 1.02 and fit_w > 0:
                    size = max(size * bbox.width / fit_w, size * 0.6)
                shape.insert_text(
                    origin,
                    new,
                    fontname=rn,
                    fontfile=fp,
                    fontsize=size,
                    color=int2rgb(color),
                )
        shape.commit()
    return changed


def process_pdf(src, cc, fonts, out=None):
    t0 = time.time()
    doc = fitz.open(src)
    n = len(doc)
    print("  提取内嵌字体…", flush=True)
    doc_fonts = load_doc_fonts(doc)
    print(f"  可用原字体 {len(doc_fonts)} 个", flush=True)
    total_changed = changed_pages = 0
    for pno in range(n):
        changed = rebuild_page(doc[pno], cc, fonts, doc_fonts)
        if changed:
            changed_pages += 1
            total_changed += changed
        if pno % 50 == 0 or pno == n - 1:
            print(
                f"  [{os.path.basename(src)}] 页 {pno + 1}/{n} "
                f"已改页 {changed_pages} 累计 span {total_changed}",
                flush=True,
            )
    if out is None:
        out = src.rsplit(".pdf", 1)[0] + "_简体.pdf"
    # 注意: 不使用 subset_fonts（ToUnicode 生成对部分字体映射错误，导致
    # "财务资料"等提取成控制字符），保留完整字体嵌入保证提取正确
    doc.save(out, garbage=3, deflate=True)
    doc.close()
    size_mb = os.path.getsize(out) / 1024 / 1024
    print(
        f"完成 {os.path.basename(src)} -> {os.path.basename(out)} "
        f"({size_mb:.1f}MB) | 改字 span {total_changed}，涉及页 {changed_pages}/{n}，"
        f"耗时 {time.time() - t0:.0f}s",
        flush=True,
    )
    return out


def main():
    parser = argparse.ArgumentParser(
        description="繁体 PDF → 简体 PDF（保留原排版，本地处理）"
    )
    parser.add_argument("srcs", nargs="+", help="输入繁体 PDF 路径（可多个）")
    parser.add_argument(
        "--output", "-o", help="输出路径（仅单个输入时可用；默认 <原名>_简体.pdf）"
    )
    args = parser.parse_args()
    if args.output and len(args.srcs) > 1:
        parser.error("--output 仅支持单个输入文件")
    cc = opencc.OpenCC("t2s")
    fonts = load_fonts()
    for src in args.srcs:
        if not os.path.exists(src):
            print(f"跳过（不存在）: {src}", flush=True)
            continue
        process_pdf(src, cc, fonts, out=args.output)


if __name__ == "__main__":
    main()
