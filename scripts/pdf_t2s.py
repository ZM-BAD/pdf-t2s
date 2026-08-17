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
import multiprocessing as mp
import os
import sys
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
# 并行转换（003）: 页数低于该值走串行（分块固定开销 > 并行收益）
PARALLEL_MIN_PAGES = 50

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
# 已加载的系统字体槽位: {fonts 字典键: 路径}，由 load_fonts 填充。
# 跨平台（004）: prepare_fonts 可能缺槽位（如 Windows 无原生宋体粗体），
# 字体选择只在已加载槽位中进行，缺失槽位按 bold→常规、同族→异族降级。
_LOADED_PATHS = {}


def load_fonts():
    """加载系统字体（跨平台降级）: 缺失的槽位跳过并警告，不中断。
    全部缺失时也不抛异常——未转换文档仍可转换（原字体复用路径不依赖系统字体）。"""
    fonts = {}
    _LOADED_PATHS.clear()
    for (kind, bold), path in CJK_FONTS.items():
        key = ("cjk", kind, bold)
        if not os.path.exists(path):
            print(
                f"  ⚠ 缺少字体 {os.path.basename(path)}（{kind}，粗体={bold}）——将降级/回退",
                flush=True,
            )
            continue
        fonts[key] = fitz.Font(fontfile=path)
        _LOADED_PATHS[key] = path
    for bold, path in LATIN_SERIF.items():
        key = ("latin_serif", bold)
        if not os.path.exists(path):
            print(
                f"  ⚠ 缺少字体 {os.path.basename(path)}（拉丁宋体，粗体={bold}）——将降级/回退",
                flush=True,
            )
            continue
        fonts[key] = fitz.Font(fontfile=path)
        _LOADED_PATHS[key] = path
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
    `new` 全部字符的已加载字体（无命中用首选）。原字体不可提取/不可映射的
    未转换 span，以及转换过的 span，都经此选择——保证字形可渲染、提取不丢。
    跨平台降级（004）: 只从 _LOADED_PATHS 中的槽位选择——粗体槽位缺失时
    降级同族常规（bold→0），整族缺失时降级另一族；全部缺失给出明确错误。"""
    kind = font_kind(fname)
    other = "sans" if kind == "serif" else "serif"
    # 候选顺序（保持与原版一致 + 降级追加项）: 每个族内 粗体→常规（bold 为 0 时
    # 去重），族间 同族→异族。全量加载时与原版行为等价——同族粗/常规字形覆盖
    # 相同，映射选择不会发散（504 页基线吻合）；粗体缺失时同族常规先于异族
    # 粗体（视觉降级更小）。
    b_vals = (bold,) if bold == 0 else (bold, 0)
    if has_cjk:
        cands = [
            (("S" if k == "sans" else "R"), k, b) for k in (kind, other) for b in b_vals
        ]
    else:
        # 与原版一致: 仅 Times 类字体尝试拉丁 Times 槽位，其余纯拉丁走 sans/serif
        cands = [("T", "latin", b) for b in b_vals] if "Times" in fname else []
        cands += [
            (("S" if k == "sans" else "R"), k, b)
            for k in ("sans", "serif")
            for b in b_vals
        ]
    first = None
    for tag, k2, b2 in cands:
        key = ("latin_serif", b2) if k2 == "latin" else ("cjk", k2, b2)
        if key not in _LOADED_PATHS:
            continue
        if first is None:
            first = key
        if all(can_map(fonts[key], c) for c in new):
            return f"Z{tag}{bold}", _LOADED_PATHS[key], fonts[key]
    if first is not None:
        k2 = "latin" if first[0] == "latin_serif" else first[1]
        tag = {"latin": "T", "sans": "S", "serif": "R"}[k2]
        return f"Z{tag}{bold}", _LOADED_PATHS[first], fonts[first]
    raise RuntimeError(
        "未找到任何 t2s-*.ttf 系统字体（scripts/ 目录）——"
        "请先运行 scripts/prepare_fonts.py 生成字体后再转换"
    )


# 资源名约定函数（ZS/ZR/ZT + 粗体标志，见 SKILL.md 字体映射表）——仅测试
# 引用（test_font_resource_names）作为命名约定锚点；转换路径走 pick_font/
# fallback_font（带缺槽位降级）。注意: 二者直接索引 CJK_FONTS/LATIN_SERIF，
# 无降级保护，生产代码不应调用。
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
        out = default_output(src)
    # 注意: 不使用 doc.subset_fonts()（ToUnicode 生成对部分字体映射错误，导致
    # "财务资料"等提取成控制字符）；体积由 fontTools 预子集化控制（见上）
    n_purged = purge_unused_fonts(doc)
    if n_purged:
        print(f"  清理未引用字体条目 {n_purged} 个", flush=True)
    try:
        save_output(doc, out, garbage=3, deflate=True)
    finally:
        doc.close()  # 保存失败也释放句柄（Windows 批量转换防文件占用）
    size_mb = os.path.getsize(out) / 1024 / 1024
    print(
        f"完成 {os.path.basename(src)} -> {os.path.basename(out)} "
        f"({size_mb:.1f}MB) | 改字 span {total_changed}，涉及页 {changed_pages}/{n}，"
        f"耗时 {time.time() - t0:.0f}s",
        flush=True,
    )
    return out


def default_output(src):
    """默认输出路径: <原名>_简体.pdf（大小写不敏感的 .pdf 后缀，Windows 兼容）"""
    root, ext = os.path.splitext(src)
    if ext.lower() != ".pdf":
        root = src
    return root + "_简体.pdf"


def save_output(doc, out, **kwargs):
    """保存输出；目标文件被占用/只读（Windows 常见 PermissionError）时报清晰错误"""
    try:
        doc.save(out, **kwargs)
    except OSError as e:
        raise RuntimeError(
            f"无法写入输出文件 {out}: {e}。"
            "若文件已存在，请先关闭占用它的程序（如 PDF 阅读器），"
            "或改用 --output 指定其他路径"
        ) from e


def dedup_fonts(doc):
    """合并后手动去重（003）: 同 PS 名且字体流相同的对象统一指向首个 xref。
    garbage=4 的流合并是 O(n²) 比较（504 页实测 45s+），哈希去重 O(n) 且
    确定性（实测 <1s）。被替换对象由后续 garbage=3 回收。"""
    import hashlib

    seen = {}
    redirects = []
    for pno in range(len(doc)):
        page = doc[pno]
        for row in page.get_fonts(full=True):
            xref, _, _, basefont, rn, _, _ = row
            try:
                buf = doc.extract_font(xref)[3]
                size = len(buf) if buf else -1
                md5 = hashlib.md5(buf).hexdigest() if buf else ""
            except Exception:  # noqa: BLE001, S112 — 单字体提取失败跳过
                continue
            key = (font_key(basefont), size, md5)
            if key not in seen:
                seen[key] = xref
            else:
                redirects.append((page.xref, rn, xref, seen[key]))
    n = 0
    for px, rn, old, new in redirects:
        if old == new:
            continue
        try:
            doc.xref_set_key(px, f"Resources/Font/{rn}", f"{new} 0 R")
            n += 1
        except Exception:  # noqa: BLE001, S112 — 间接 Resources 路径不可操作，跳过
            continue
    return n


def _split_blocks(n, jobs):
    """页数均分块边界: [(块号, 起始页, 结束页)]，末块含余数"""
    per = n // jobs
    out = []
    for i in range(jobs):
        s = i * per
        e = n if i == jobs - 1 else (i + 1) * per
        out.append((i, s, e))
    return out


def _block_worker(args):
    """块 worker（003 并行转换）: 每进程独立打开原 PDF，处理块内页，
    只保留块内页后保存块文件。必须模块级可 pickle（macOS spawn）。"""
    block_idx, start, end, src, submap_path, block_dir = args
    try:
        cc = opencc.OpenCC("t2s")
        fonts = load_fonts()
        with open(submap_path) as fh:
            font_path_map = dict(
                line.strip().split("\t") for line in fh if line.strip()
            )
        doc = fitz.open(src)
        doc_fonts = load_doc_fonts(doc)
        changed = 0
        for pno in range(start, end):
            changed += rebuild_page(doc[pno], cc, fonts, doc_fonts[pno], font_path_map)
        doc.select(list(range(start, end)))  # 只保留块内页
        out = os.path.join(block_dir, f"block-{block_idx}.pdf")
        doc.save(out, garbage=3, deflate=True)
        doc.close()
        return out, changed
    except Exception as e:  # noqa: BLE001 — 返回异常由主进程统一报告
        return e


def process_pdf_parallel(src, cc, fonts, out=None, jobs=None):
    """多进程分块转换（003）: 阶段 1 全局字符收集+子集化（串行）→
    阶段 2 分块并行重建 → 阶段 3 insert_pdf 合并 + garbage=2 保存。
    jobs=None 自动 = min(cpu_count, 页数)；页数 < PARALLEL_MIN_PAGES 或
    jobs=1 降级串行（process_pdf）。"""
    t0 = time.time()
    doc = fitz.open(src)
    n = len(doc)
    if jobs is None:
        jobs = max(1, min(mp.cpu_count(), n))
    if n < PARALLEL_MIN_PAGES or jobs <= 1:
        doc.close()
        return process_pdf(src, cc, fonts, out)
    print(f"  并行转换 {jobs} 进程 × {n} 页…", flush=True)
    # 阶段 1: 全局字符收集（子集化依赖全文档字符集，必须串行一次）
    doc_fonts = load_doc_fonts(doc)
    n_fonts = len({f for pf in doc_fonts.values() for f in pf})
    print(f"  可用原字体 {n_fonts} 个", flush=True)
    needed = collect_needed_chars(doc, cc, fonts, doc_fonts)
    font_path_map = subset_system_fonts(needed)
    doc.close()
    if font_path_map:
        saved = sum(
            os.path.getsize(k) - os.path.getsize(v) for k, v in font_path_map.items()
        )
        print(
            f"  子集化系统字体 {len(font_path_map)} 个 (节省 {saved / 1e6:.1f}MB)",
            flush=True,
        )
    # 子集映射写临时文件供子进程读取
    submap = os.path.join(SUBSET_DIR, "font_path_map.txt")
    with open(submap, "w") as fh:
        fh.writelines(f"{k}\t{v}\n" for k, v in font_path_map.items())
    # 阶段 2: 分块并行重建
    block_dir = tempfile.mkdtemp(prefix="pdf-t2s-blocks-")
    chunks = [(*b, src, submap, block_dir) for b in _split_blocks(n, jobs)]
    with mp.Pool(jobs) as pool:
        results = pool.map(_block_worker, chunks)
    total_changed = 0
    for r in results:
        if isinstance(r, Exception):
            raise RuntimeError(f"块转换失败: {r}")  # noqa: TRY004 — 块失败是业务错误非类型错误
        total_changed += r[1]
    # 阶段 3: 合并 + 手动字体去重 + 清理 + 保存。
    # dedup_fonts: 块间重复字体流（每块嵌入同一全局子集）统一 xref——
    # garbage=4 的流合并是 O(n²) 比较（504 页实测 45s+），哈希去重 O(n)
    merged = fitz.open()
    for i in range(jobs):
        blk = fitz.open(os.path.join(block_dir, f"block-{i}.pdf"))
        merged.insert_pdf(blk, annots=False, links=False)
        blk.close()
    n_dedup = dedup_fonts(merged)
    n_purged = purge_unused_fonts(merged)
    if n_dedup:
        print(f"  合并去重字体 {n_dedup} 处", flush=True)
    if n_purged:
        print(f"  清理未引用字体条目 {n_purged} 个", flush=True)
    if out is None:
        out = default_output(src)
    try:
        save_output(merged, out, garbage=3, deflate=True, deflate_fonts=True)
    finally:
        merged.close()  # 保存失败也释放句柄（Windows 批量转换防文件占用）
    size_mb = os.path.getsize(out) / 1024 / 1024
    print(
        f"完成 {os.path.basename(src)} -> {os.path.basename(out)} "
        f"({size_mb:.1f}MB) | 改字 span {total_changed}，涉及页 {n}/{n}，"
        f"耗时 {time.time() - t0:.0f}s（并行 {jobs} 进程）",
        flush=True,
    )
    return out


def main():
    # Windows 上 stdout 重定向（管道/CI 日志）时用 locale 编码会中文乱码/报错
    # （Python 3.15 起默认 UTF-8，此处兼容 3.10-3.14）
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="繁体 PDF → 简体 PDF（保留原排版，本地处理）"
    )
    parser.add_argument("srcs", nargs="+", help="输入繁体 PDF 路径（可多个）")
    parser.add_argument(
        "--output", "-o", help="输出路径（仅单个输入时可用；默认 <原名>_简体.pdf）"
    )
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=None,
        help="并行进程数（默认: min(CPU核数, 页数)；1=强制串行；页数<50 自动串行）",
    )
    args = parser.parse_args()
    if args.output and len(args.srcs) > 1:
        parser.error("--output 仅支持单个输入文件")
    cc = opencc.OpenCC("t2s")
    fonts = load_fonts()
    failures = 0
    for src in args.srcs:
        if not os.path.exists(src):
            print(f"跳过（不存在）: {src}", flush=True)
            continue
        try:
            process_pdf_parallel(src, cc, fonts, out=args.output, jobs=args.jobs)
        except (RuntimeError, OSError) as e:  # 单文件失败不中断批量
            failures += 1
            print(f"转换失败 {src}: {e}", flush=True)
    return 1 if failures else 0  # 全部失败时退出码必须非 0（review 004）


if __name__ == "__main__":
    sys.exit(main())
