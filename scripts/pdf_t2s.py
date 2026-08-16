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
  6. 体积优化（v1.1）: 两阶段子集化——重绘前全文档收集每个系统字体实际
     用到的字符集，fontTools 预子集化后嵌入（绕开 doc.subset_fonts 的
     ToUnicode bug：MuPDF 嵌入的已是合法子集，ToUnicode 由 cmap 正常生成）。
     原 PDF 内嵌字体（ZO）已是子集，不子集化。
  7. 不使用 doc.subset_fonts()（其 ToUnicode 生成对部分字体映射错误），
     不用 TextWriter（其字体资源重复注册导致保存后部分字体提取乱码）

用法:
  python3 pdf_t2s.py 输入.pdf [输入2.pdf ...] [--output 输出.pdf]
  字体文件（t2s-*.ttf）由 prepare_fonts.py 生成，必须与脚本同目录。
"""

import argparse
import os
import tempfile
import time

import fitz
import opencc
from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

BASE = os.path.dirname(os.path.abspath(__file__))
# 每进程独立临时目录（踩坑: 固定 /tmp 目录会被并发转换互相覆盖，
# 导致 rebuild 阶段读到别的文档的子集/原字体文件 -> notdef/\x00）
ORIG_FONT_DIR = tempfile.mkdtemp(prefix="pdf-t2s-origfonts-")
SUBSET_DIR = tempfile.mkdtemp(prefix="pdf-t2s-subsets-")

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
# 可子集化的系统字体路径集（原 PDF 内嵌字体 ZO 不在此列，保持完整嵌入）
SYSTEM_FONT_PATHS = set(CJK_FONTS.values()) | set(LATIN_SERIF.values())


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


def font_key(name):
    """字体名归一：去除 subset 前缀（SPNAJD+MHeiHK-Bold → MHeiHK-Bold）。
    get_fonts(full=True) 的 basefont 带前缀，rawdict span 字体名不带，
    统一去前缀后匹配。"""
    return name.split("+")[-1]


_glyph_cache = {}


def can_map(font, ch):
    """该字体能否映射此字符。CID CFF 等无 Unicode cmap 的字体返回 False
    （insert_text 无法为其做 Unicode→字形映射，复用会导致 notdef/\x00）。
    结果按 (font, 字符) 缓存。"""
    key = (id(font), ord(ch))
    if key not in _glyph_cache:
        _glyph_cache[key] = font.has_glyph(ord(ch))
    return _glyph_cache[key]


def load_doc_fonts(doc):
    """提取文档全部内嵌字体: {pno: {去前缀PS名: (fitz.Font, 临时文件路径, ZO资源名, 原始xref)}}
    按页匹配该页实际引用的子集字体——同 PS 名不同子集（不同前缀）时以页为准，
    保证未转换 span 的字形在该子集中必然存在。ZO 资源名按文档内顺序分配，
    确定性且无碰撞。"""
    os.makedirs(ORIG_FONT_DIR, exist_ok=True)
    xref_cache = {}
    page_fonts = {}
    idx = 0
    for pno in range(len(doc)):
        pf = {}
        for row in doc[pno].get_fonts(full=True):
            xref, ext, _, basefont, _, _, _ = row
            key = font_key(basefont)
            if xref not in xref_cache:
                try:
                    buf = doc.extract_font(xref)[3]
                    if not buf:
                        continue  # 非嵌入字体（base-14 等）无 buffer，走回退
                    path = os.path.join(ORIG_FONT_DIR, f"f{idx}.{ext or 'bin'}")
                    with open(path, "wb") as fh:
                        fh.write(buf)
                    xref_cache[xref] = (fitz.Font(fontbuffer=buf), path, f"ZO{idx}")
                    idx += 1
                except Exception:  # noqa: BLE001, S112 — 单个损坏字体不中断整文档转换
                    continue
            if key not in pf:
                pf[key] = (*xref_cache[xref], xref)  # (font, path, zo, 该页原始xref)
        page_fonts[pno] = pf
    return page_fonts


def original_font(fname, doc_fonts):
    """原字体 -> (资源名, 文件路径)"""
    return doc_fonts[font_key(fname)][2], doc_fonts[font_key(fname)][1]


def fallback_font(fonts, fname, bold, new, has_cjk):
    """系统字体回退: 返回 (资源名, 文件路径, 字体对象)，选择第一个能映射
    `new` 全部字符的字体（无命中用首选）。原字体不可提取/不可映射的
    未转换 span，以及转换过的 span，都经此选择——保证字形可渲染、提取不丢。"""
    kind = font_kind(fname)
    cands = []
    if has_cjk:
        cands.append(("S" if kind == "sans" else "R", kind))
        other = "sans" if kind == "serif" else "serif"
        cands.append(("S" if other == "sans" else "R", other))
    else:
        if "Times" in fname:
            cands.append(("T", "latin"))
        cands.append(("S", "sans"))
        cands.append(("R", "serif"))
    for tag, k2 in cands:
        if k2 == "latin":
            fp, fobj = LATIN_SERIF[bold], fonts[("latin_serif", bold)]
        else:
            fp, fobj = CJK_FONTS[(k2, bold)], fonts[("cjk", k2, bold)]
        if all(can_map(fobj, c) for c in new):
            return f"Z{tag}{bold}", fp, fobj
    tag, k2 = cands[0]
    if k2 == "latin":
        return f"Z{tag}{bold}", LATIN_SERIF[bold], fonts[("latin_serif", bold)]
    return f"Z{tag}{bold}", CJK_FONTS[(k2, bold)], fonts[("cjk", k2, bold)]


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


def pick_font(fonts, doc_fonts, fname, bold, orig_text, new_text):
    """字体选择（横排/竖排统一，v1.1 从 rebuild_page 提取）:
    返回 (资源名, 字体文件路径, 测量用 fitz.Font)。
    - 文本转换过 → 系统字体（fallback_font，按黑体/宋体类与字形可用性）
    - 未转换且原字体可映射 → 原 PDF 内嵌字体（字形 100% 还原）
    - 未转换但原字体不可提取/不可映射 → 系统字体回退
    字符收集（阶段 1）与重绘（阶段 3）复用同一函数，保证子集覆盖全部绘制字符。"""
    if new_text != orig_text:
        rn, fp, meas = fallback_font(
            fonts, fname, bold, new_text, any(is_cjk(c) for c in new_text)
        )
    elif font_key(fname) in doc_fonts and all(
        can_map(doc_fonts[font_key(fname)][0], c) for c in new_text
    ):
        rn, fp = original_font(fname, doc_fonts)
        meas = doc_fonts[font_key(fname)][0]
    else:
        rn, fp, meas = fallback_font(
            fonts, fname, bold, new_text, any(is_cjk(c) for c in new_text)
        )
    return rn, fp, meas


def collect_needed_chars(doc, cc, fonts, doc_fonts_all):
    """阶段 1: 全文档扫描，按字体文件路径收集实际绘制的字符集。
    与重绘使用同一 pick_font（同输入同输出），子集必然覆盖全部绘制字符。
    竖排文本逐字符收集。"""
    needed = {}
    for pno in range(len(doc)):
        doc_fonts = doc_fonts_all[pno]
        for _, spans in collect_rows(doc[pno]):
            for bbox, origin, text, size, color, fname, bold, chars in spans:
                new = cc.convert(text).replace("於", "于")
                if chars is not None:
                    for ch, _ in chars:
                        ch_new = cc.convert(ch).replace("於", "于")
                        _, fp, _ = pick_font(fonts, doc_fonts, fname, bold, ch, ch_new)
                        needed.setdefault(fp, set()).add(ch_new)
                else:
                    _, fp, _ = pick_font(fonts, doc_fonts, fname, bold, text, new)
                    needed.setdefault(fp, set()).update(new)
    return needed


def subset_system_fonts(needed):
    """阶段 2: 仅子集化系统字体（SYSTEM_FONT_PATHS），输出到 SUBSET_DIR。
    子集化失败（异常字体）回退完整字体，不中断转换。返回 {原路径: 子集路径}。
    用 unicodes_requested（fontTools 4.63 API）保证字符覆盖；子集化保留
    ZT2S- 前缀 PS 名（MuPDF 嵌入时资源命名依赖它防撞名）。"""
    os.makedirs(SUBSET_DIR, exist_ok=True)
    mapping = {}
    for fp, chars in needed.items():
        if fp not in SYSTEM_FONT_PATHS or not chars:
            continue
        out = os.path.join(SUBSET_DIR, os.path.basename(fp))
        try:
            font = TTFont(fp)
            ss = Subsetter(options=Options())
            ss.unicodes_requested = {ord(c) for c in chars}
            ss.subset(font)
            font.save(out)
            mapping[fp] = out
        except Exception:  # noqa: BLE001, S112 — 子集化失败回退完整字体，不中断转换
            continue
    return mapping


_TF_RE = None
_FONT_KEY_RE = None


def purge_unused_fonts(doc):
    """保存前清理（v1.1 补充）: 删除页面/Form 的 Resources/Font 中内容流未
    引用的字体条目。redaction 后原字体对象残留且被 Resources 持有引用，
    garbage=3 无法回收（智谱实测 72 个死字体 / 10.1MB）。扫描范围含页面内容
    流与全部 Form XObject 内容流（Form 引用的字体不删，防视觉破坏）。
    兼容 Resources/Font 为直接字典或间接引用两种结构。返回清理条目数。"""
    import re

    global _TF_RE, _FONT_KEY_RE
    if _TF_RE is None:
        _TF_RE = re.compile(rb"/([A-Za-z0-9_]+)\s+[\d.]+\s+Tf")  # 资源名可含下划线
    if _FONT_KEY_RE is None:
        _FONT_KEY_RE = re.compile(r"/([A-Za-z0-9_]+)\s+\d+\s+0\s+R")
    used = set()
    forms = []
    for pno in range(len(doc)):
        for xref in doc[pno].get_contents():
            data = doc.xref_stream(xref)
            if data:
                used |= set(_TF_RE.findall(data))
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Subtype")[1].strip("/") == "Form":
            forms.append(xref)
            data = doc.xref_stream(xref)
            if data:
                used |= set(_TF_RE.findall(data))
    used = {n.decode() for n in used}

    def purge_obj(obj_xref):
        """清理单个对象（页面/Form）的 Resources/Font 死条目"""
        n = 0
        t, v = doc.xref_get_key(obj_xref, "Resources")
        if t == "xref":  # Resources 是间接对象
            res_xref = int(v.split()[0])
            t2, v2 = doc.xref_get_key(res_xref, "Font")
            if t2 == "xref":  # Font 是间接对象 -> 在其上直接置 null
                fd = int(v2.split()[0])
                for key in doc.xref_get_keys(fd):
                    if key not in used:
                        doc.xref_set_key(fd, key, "null")
                        n += 1
            elif t2 == "dict":  # Font 是直接字典 -> 路径写法
                for key in _FONT_KEY_RE.findall(v2):
                    if key not in used:
                        doc.xref_set_key(res_xref, f"Font/{key}", "null")
                        n += 1
        elif t == "dict":  # Resources 是直接字典
            t2, v2 = doc.xref_get_key(obj_xref, "Resources/Font")
            if t2 == "dict":
                for key in _FONT_KEY_RE.findall(v2):
                    if key not in used:
                        doc.xref_set_key(obj_xref, f"Resources/Font/{key}", "null")
                        n += 1
        return n

    purged = 0
    for pno in range(len(doc)):
        purged += purge_obj(doc[pno].xref)
    for fx in forms:
        purged += purge_obj(fx)
    return purged


def collect_blank_bboxes(page):
    """纯空白 span 的 bbox（v1.1 体积优化）: redact 掉它们使内容流不再引用
    其原字体资源（C0_0 等残留引用，占体积数 MB），保存时由 purge/garbage 回收。
    空白字符提取为空白（norm 后无差异），视觉不可见，删除无影响。"""
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for s in line["spans"]:
                t = "".join(c["c"] for c in s["chars"])
                if t and not t.strip():
                    bbox = fitz.Rect(s["bbox"])
                    if not bbox.is_empty and bbox.width > 0 and bbox.height > 0:
                        out.append(bbox)
    return out


def rebuild_page(page, cc, fonts, doc_fonts, font_path_map=None):
    """逐页重绘。font_path_map: 系统字体 {原路径: 子集路径}（v1.1），
    insert_text 的 fontfile 经映射替换；测量仍用完整字体（度量一致）。"""
    font_path_map = font_path_map or {}
    rows = collect_rows(page)
    if not rows:
        return 0
    # 1) 删除全部旧文本（图片/矢量线保留）——含纯空白 span（v1.1: 清字体残留引用）
    for _, spans in rows:
        for bbox, *_ in spans:
            page.add_redact_annot(bbox)
    for bbox in collect_blank_bboxes(page):
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
                    rn, fp, _ = pick_font(fonts, doc_fonts, fname, bold, ch, ch_new)
                    shape.insert_text(
                        ch_origin,
                        ch_new,
                        fontname=rn,
                        fontfile=font_path_map.get(fp, fp),
                        fontsize=size,
                        color=int2rgb(color),
                    )
            else:
                rn, fp, meas = pick_font(fonts, doc_fonts, fname, bold, text, new)
                # 宽度收缩: 以实际绘制字体度量（未转换 span 用原字体度量一致，不触发）
                fit_w = meas.text_length(new, fontsize=size)
                if fit_w > bbox.width * 1.02 and fit_w > 0:
                    size = max(size * bbox.width / fit_w, size * 0.6)
                shape.insert_text(
                    origin,
                    new,
                    fontname=rn,
                    fontfile=font_path_map.get(fp, fp),
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
    n_fonts = len({f for pf in doc_fonts.values() for f in pf})
    print(f"  可用原字体 {n_fonts} 个", flush=True)
    # v1.1 体积优化: 阶段 1 收集字符集 -> 阶段 2 子集化系统字体
    # （绕开 doc.subset_fonts 的 ToUnicode bug，MuPDF 嵌入的已是合法子集）
    print("  收集所需字符集…", flush=True)
    needed = collect_needed_chars(doc, cc, fonts, doc_fonts)
    font_path_map = subset_system_fonts(needed)
    if font_path_map:
        saved = sum(
            os.path.getsize(k) - os.path.getsize(v) for k, v in font_path_map.items()
        )
        print(
            f"  子集化系统字体 {len(font_path_map)} 个 (节省 {saved / 1e6:.1f}MB)",
            flush=True,
        )
    total_changed = changed_pages = 0
    for pno in range(n):
        changed = rebuild_page(doc[pno], cc, fonts, doc_fonts[pno], font_path_map)
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
    # 注意: 不使用 doc.subset_fonts()（ToUnicode 生成对部分字体映射错误，导致
    # "财务资料"等提取成控制字符）；体积由 fontTools 预子集化控制（见上）
    n_purged = purge_unused_fonts(doc)
    if n_purged:
        print(f"  清理未引用字体条目 {n_purged} 个", flush=True)
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
