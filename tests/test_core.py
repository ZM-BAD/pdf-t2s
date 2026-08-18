"""pdf_t2s 核心逻辑测试。

设计要点:
  - 无外部依赖的环境（裸 python3）下全部跳过，保证
    `python3 -m unittest discover` 作为 CI/本地门槛始终可执行;
    在安装依赖的 venv 中则完整运行（README 有说明）。
  - 不依赖真实招股书样本（敏感文件不入库），用纯函数 + 合成 PDF 验证。
"""

import importlib.util
import os
import tempfile
import unittest

HAS_DEPS = all(importlib.util.find_spec(m) for m in ("fitz", "opencc"))
FONTS_READY = HAS_DEPS and os.path.exists(
    os.path.join(os.path.dirname(__file__), "..", "scripts", "t2s-serif-regular.ttf")
)
ALL_FONTS_READY = HAS_DEPS and all(
    os.path.exists(os.path.join(os.path.dirname(__file__), "..", "scripts", n))
    for n in (
        "t2s-sans-regular.ttf",
        "t2s-sans-bold.ttf",
        "t2s-serif-regular.ttf",
        "t2s-serif-bold.ttf",
        "t2s-times.ttf",
        "t2s-times-bold.ttf",
    )
)


@unittest.skipUnless(
    HAS_DEPS, "未安装 pymupdf/opencc（pip install -r requirements.txt）"
)
class TestHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts import pdf_t2s, prepare_fonts, verify

        cls.m = pdf_t2s
        cls.v = verify
        cls.pf = prepare_fonts

    def test_is_cjk(self):
        self.assertTrue(self.m.is_cjk("繁"))
        self.assertTrue(self.m.is_cjk("简"))
        self.assertTrue(self.m.is_cjk("。"))  # U+3002 全角标点
        self.assertTrue(self.m.is_cjk("Ａ"))  # U+FF21 全角拉丁
        self.assertFalse(self.m.is_cjk("A"))
        self.assertFalse(self.m.is_cjk("1"))
        self.assertFalse(self.m.is_cjk(" "))

    def test_font_kind(self):
        self.assertEqual(self.m.font_kind("MHeiHK-Medium"), "sans")
        self.assertEqual(self.m.font_kind("HiraginoSansGB-W3"), "sans")
        self.assertEqual(self.m.font_kind("GothicBBB-Medium"), "sans")
        self.assertEqual(self.m.font_kind("MSungHK-Light"), "serif")
        self.assertEqual(self.m.font_kind("SongtiSC-Regular"), "serif")
        self.assertEqual(self.m.font_kind("TimesNewRomanPSMT"), "serif")

    def test_int2rgb(self):
        self.assertEqual(self.m.int2rgb(0xFF0000), (1.0, 0.0, 0.0))
        self.assertEqual(self.m.int2rgb(0x00FF00), (0.0, 1.0, 0.0))
        self.assertEqual(self.m.int2rgb(0x0000FF), (0.0, 0.0, 1.0))
        self.assertEqual(self.m.int2rgb(0x808080), (128 / 255,) * 3)

    def test_font_resource_names(self):
        # 资源名约定: ZS/ZR/ZT + 粗体标志，绝不与 PDF 原字体名撞名
        self.assertEqual(self.m.sys_font({}, "MHeiHK", 0)[0], "ZS0")
        self.assertEqual(self.m.sys_font({}, "MHeiHK", 16)[0], "ZS16")
        self.assertEqual(self.m.sys_font({}, "MSungHK", 0)[0], "ZR0")
        self.assertEqual(self.m.latin_font({}, "TimesNewRomanPSMT", 0)[0], "ZT0")
        self.assertEqual(self.m.latin_font({}, "TimesNewRomanPSMT", 16)[0], "ZT16")

    def test_convert_t2s(self):
        cc = __import__("opencc").OpenCC("t2s")
        self.assertEqual(cc.convert("繁體中文測試"), "繁体中文测试")  # 繁本身同形不转换
        self.assertEqual(cc.convert("開會"), "开会")
        self.assertEqual(cc.convert("頭髮"), "头发")
        self.assertEqual(cc.convert("皇后"), "皇后")  # t2s 方向无歧义

    def test_convert_patch_yu(self):
        # 踩坑 15: OpenCC 对独立"於"不转换，必须手动补丁
        cc = __import__("opencc").OpenCC("t2s")
        self.assertEqual(cc.convert("基於").replace("於", "于"), "基于")
        self.assertEqual(cc.convert("滿足於").replace("於", "于"), "满足于")

    def test_trad_feature_string(self):
        # 踩坑 21: 繁体残留检测的特征串必须只含简繁字形不同的字
        cc = __import__("opencc").OpenCC("t2s")
        same_shape = [c for c in self.v.TRAD if cc.convert(c) == c]
        self.assertEqual(same_shape, [], f"特征串混入同形字: {same_shape}")

    def test_norm(self):
        self.assertEqual(self.v.norm("a\xadb"), "a-b")
        self.assertEqual(self.v.norm("a・b"), "a•b")
        self.assertEqual(self.v.norm("a\u00a0b"), "ab")
        self.assertEqual(self.v.norm("a b\nc"), "abc")

    def test_font_key(self):
        # subset 前缀归一: get_fonts 带前缀, rawdict span 不带, 必须归一后匹配
        self.assertEqual(self.m.font_key("SPNAJD+MHeiHK-Bold"), "MHeiHK-Bold")
        self.assertEqual(self.m.font_key("MHeiHK-Bold"), "MHeiHK-Bold")
        self.assertEqual(self.m.font_key("Times-Roman"), "Times-Roman")
        self.assertEqual(
            self.m.font_key("ZT2S-STSongti-SC-Regular"), "ZT2S-STSongti-SC-Regular"
        )

    def test_find_doc_font_truncated(self):
        # MuPDF rawdict 把超长字体名截断到 24 字符（TimesNewRomanPS-BoldItalicMT
        # → TimesNewRomanPS-BoldItal），ZO 复用查找必须前缀回退，否则真斜体
        # 字体回退成正体系统字体
        doc_fonts = {"TimesNewRomanPS-BoldItalicMT": "e"}
        self.assertEqual(
            self.m.find_doc_font(doc_fonts, "TimesNewRomanPS-BoldItal"),
            "TimesNewRomanPS-BoldItalicMT",
        )
        # 完整名精确匹配优先于前缀回退
        self.assertEqual(
            self.m.find_doc_font(doc_fonts, "TimesNewRomanPS-BoldItalicMT"),
            "TimesNewRomanPS-BoldItalicMT",
        )
        # 无匹配（不足 24 字符或前缀不匹配）返回 None
        self.assertIsNone(self.m.find_doc_font(doc_fonts, "Times-Roman"))
        self.assertIsNone(self.m.find_doc_font(doc_fonts, "NoSuchFont-ABCDEFGHIJ"))
        # subset 前缀照常归一后再截断回退
        self.assertEqual(
            self.m.find_doc_font(doc_fonts, "SPNAJD+TimesNewRomanPS-BoldItal"),
            "TimesNewRomanPS-BoldItalicMT",
        )

    def test_default_output(self):
        # 默认输出命名: 大小写不敏感 .pdf 后缀（Windows 文件系统），无后缀不吞名
        self.assertEqual(self.m.default_output("x.pdf"), "x_简体.pdf")
        self.assertEqual(self.m.default_output("X.PDF"), "X_简体.pdf")
        self.assertEqual(self.m.default_output("dir/a.pdf"), "dir/a_简体.pdf")
        self.assertEqual(self.m.default_output("noext"), "noext_简体.pdf")

    @unittest.skipUnless(os.environ.get("PDF_T2S_CI"), "仅 CI 运行")
    def test_ci_fonts_ready(self):
        # CI 门禁（004 review）: prepare_fonts 未产出全部 6 个字体槽位时禁止
        # 字体测试静默 SKIP 让 CI 照样绿——字体缺失必须显式失败
        self.assertTrue(
            ALL_FONTS_READY,
            "CI 门禁: scripts/ 下 t2s-*.ttf 不全（prepare_fonts 未产出 6 个槽位）——"
            "检查该平台 prepare_fonts 步骤的日志",
        )

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_fallback_font_maps_all_chars(self):
        # 回退字体必须能映射全部字符。'‧'(U+2027) 的落位取决于平台字体覆盖:
        # macOS 上 Times/Hiragino 无此字形 → 宋体(ZR0)；Linux 上 Noto Sans 也有
        # 此字形 → ZS0。断言性质（必能映射、槽位合法）而非具体槽位。
        fonts = self.m.load_fonts()
        rn, _, fobj = self.m.fallback_font(fonts, "MSungHK-Light", 0, "‧", False)
        self.assertIn(rn, ("ZR0", "ZS0"), f"回退槽位异常: {rn}")
        self.assertTrue(self.m.can_map(fobj, "‧"))
        # 含 CJK 的回退按黑体/宋体类（sans 原字体 → sans 槽位），且必能映射
        rn2, _, fobj2 = self.m.fallback_font(fonts, "MHeiHK", 0, "测试中", True)
        self.assertEqual(rn2, "ZS0")
        self.assertTrue(all(self.m.can_map(fobj2, c) for c in "测试中"))

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_fallback_degrades_missing_bold(self):
        # 跨平台降级（004）: 粗体槽位缺失 -> 同族常规 face（资源名仍带粗体标志）；
        # 整族缺失 -> 回退另一族
        fonts = self.m.load_fonts()
        try:
            self.m._LOADED_PATHS.pop(
                ("cjk", "serif", 16), None
            )  # 模拟 Windows 无宋体粗体
            rn, fp, _ = self.m.fallback_font(fonts, "MSungHK", 16, "测试中", True)
            self.assertEqual(rn, "ZR16")
            self.assertEqual(fp, self.m.CJK_FONTS[("serif", 0)])
            self.m._LOADED_PATHS.pop(("cjk", "serif", 0), None)  # 整族缺失 -> sans
            rn2, fp2, _ = self.m.fallback_font(fonts, "MSungHK", 0, "测试中", True)
            self.assertEqual(rn2, "ZS0")
            self.assertEqual(fp2, self.m.CJK_FONTS[("sans", 0)])
        finally:
            self.m.load_fonts()  # 恢复 _LOADED_PATHS 模块全局状态（断言失败也要恢复）

    def test_resolve_slot(self):
        # prepare_fonts 槽位解析: 系统候选优先；下载禁用且无系统字体 -> None
        # （download_allowed 恒为 False，测试不触发网络）
        _, path, hint = self.pf.resolve_slot("sans-reg", [("/nope/a.ttc", "sc")], False)
        self.assertIsNone(path)
        with tempfile.NamedTemporaryFile(suffix=".ttc") as tf:
            _, path, hint = self.pf.resolve_slot("sans-reg", [(tf.name, "sc")], False)
            self.assertEqual(path, tf.name)
            self.assertEqual(hint, "sc")

    def test_pick_face_by_name(self):
        # ttc 多 face 按 PS 名（nameID 6）子串选择（不区分大小写），未命中回退 face 0

        class Rec:
            def __init__(self, s):
                self.nameID, self._s = 6, s

            def toUnicode(self):
                return self._s

        class Font:
            def __init__(self, ps):
                class NT:
                    def __init__(self, ps2):
                        self.names = [Rec(ps2)]

                self.ps = ps
                self._nt = NT(ps)

            def __getitem__(self, key):
                return self._nt

        class Coll:
            def __init__(self, ps_list):
                self.fonts = [Font(p) for p in ps_list]

        coll = Coll(
            ["NotoSansCJKjp-Regular", "NotoSansCJKkr-Regular", "NotoSansCJKsc-Regular"]
        )
        self.assertEqual(self.pf.pick_face(coll, "sc").ps, "NotoSansCJKsc-Regular")
        self.assertEqual(
            self.pf.pick_face(coll, "SC").ps, "NotoSansCJKsc-Regular"
        )  # 大小写不敏感
        self.assertEqual(self.pf.pick_face(coll, "nope").ps, "NotoSansCJKjp-Regular")
        self.assertEqual(
            self.pf.pick_face(coll, 2).ps, "NotoSansCJKsc-Regular"
        )  # int 索引

    def test_whitelisted_diff(self):
        # 於/于 与标点变体 → 白名单
        self.assertTrue(self.v.whitelisted_diff(["於"], ["于"], self._cc()))
        self.assertTrue(self.v.whitelisted_diff(["…"], ["⋯"], self._cc()))
        # 常用字符缺失 → 非白名单（如 '‧' 丢失属真实异常）
        self.assertFalse(self.v.whitelisted_diff(["‧"], ["\x00"], self._cc()))
        # 固有乱码（安永信）: 缺失为非常用字符 + 输出 \x00 → 白名单
        self.assertTrue(self.v.whitelisted_diff(["⭰", "Ṳ"], ["\x00"], self._cc()))

    def _cc(self):
        return __import__("opencc").OpenCC("t2s")

    def test_original_font_deterministic(self):
        # ZO 资源名由 load_doc_fonts 按文档内顺序分配（确定性、无碰撞）
        doc_fonts = {"TestOrig": ("font", "/tmp/f.ttf", "ZO42")}
        a = self.m.original_font("TestOrig", doc_fonts)
        b = self.m.original_font("TestOrig", doc_fonts)
        self.assertEqual(a, ("ZO42", "/tmp/f.ttf"))
        self.assertEqual(a, b)
        with self.assertRaises(KeyError):
            self.m.original_font("Missing", doc_fonts)

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_rebuild_preserves_original_font(self):
        # 合成 PDF: 嵌入 PS 名不含 Sans/Hei 的原字体（与系统字体可区分）
        # 未转换 span 必须复用原字体；转换 span 必须用系统字体
        # 注: PyMuPDF 嵌入字体的 get_fonts basefont 取 family 名，而真实 PDF
        # 的 BaseFont 是 PS 名（rawdict span 字体名）——此处按真实 PDF 的
        # 匹配方式构造 doc_fonts（键 = span 字体名），load_doc_fonts 的提取
        # 归一由验收文档全量验证覆盖。
        import fitz
        import opencc
        from fontTools.ttLib import TTFont

        from scripts import pdf_t2s

        renamed = os.path.join(tempfile.mkdtemp(), "orig.ttf")
        f = TTFont(os.path.join(pdf_t2s.BASE, "t2s-serif-regular.ttf"))
        for r in f["name"].names:
            if r.nameID == 6:
                r.string = "TestOrigSerif"
        f.save(renamed)

        doc = fitz.open()
        page = doc.new_page()
        # 未转换 span 文本不能含 年 等有兼容区码点（U+F98E）的字——Noto 系字体
        # 经 MuPDF 提取会把 年 映射为 年，OpenCC 归一化后 span 被判为"已转换"，
        # 复用断言失效（004 跨平台，input 字体派生自平台 t2s-serif-regular）
        page.insert_text(
            (72, 72), "测试20211202", fontname="F0", fontfile=renamed, fontsize=12
        )  # 同形字，转换前后相同（避开 U+2011 连字符与 U+F98E 兼容区映射怪癖）
        page.insert_text(
            (72, 92), "繁體字測試", fontname="F0", fontfile=renamed, fontsize=12
        )  # 会转换
        span_font = None
        for b in page.get_text("rawdict")["blocks"]:
            if b["type"] != 0:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    span_font = s["font"].split("+")[-1]
        self.assertEqual(span_font, "TestOrigSerif")  # span 字体名 = PS 名

        cc = opencc.OpenCC("t2s")
        fonts = pdf_t2s.load_fonts()
        orig_xref = page.get_fonts(full=True)[0][0]  # 该页嵌入字体的原始 xref
        doc_fonts = {
            span_font: (fitz.Font(fontfile=renamed), renamed, "ZO0", orig_xref)
        }
        # 原字体可映射 → 复用；不可映射 → 回退
        self.assertTrue(pdf_t2s.can_map(doc_fonts[span_font][0], "中"))
        pdf_t2s.rebuild_page(page, cc, fonts, doc_fonts)

        out_fonts = {}
        for b in page.get_text("rawdict")["blocks"]:
            if b["type"] != 0:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    t = "".join(c["c"] for c in s["chars"])
                    if t.strip():
                        out_fonts[t] = s["font"].split("+")[-1]
        self.assertEqual(
            out_fonts.get("测试20211202"),
            "TestOrigSerif",
            "未转换 span 必须复用原 PDF 字体",
        )
        # 转换 span 的系统字体 PS 名因平台而异（Songti/Noto Serif/SimSun），
        # 只断言 ZT2S- 前缀；用子串匹配防提取分 span
        changed = {f for t, f in out_fonts.items() if "繁体字测试" in t}
        self.assertTrue(changed, f"未找到转换后的 span: {list(out_fonts)}")
        self.assertTrue(
            all(f.startswith("ZT2S-") for f in changed),
            f"转换 span 必须用系统简体字体（ZT2S- 前缀）: {changed}",
        )
        doc.close()

    def test_bold_name_detection(self):
        # 004 fix: MuPDF span 粗体位只认 'Bold'——'Xbold'/'Heavy'/'Black' 等
        # 加粗档位不设位（MHeiHK-Xbold、SourceHanSansCN-Heavy 实测），
        # 名字级补充检测，否则这些粗体回退常规字重
        self.assertTrue(self.m.BOLD_NAME_RE.search("MHeiHK-Xbold"))
        self.assertTrue(self.m.BOLD_NAME_RE.search("SourceHanSansCN-Heavy"))
        self.assertTrue(self.m.BOLD_NAME_RE.search("FuturaStd-Heavy"))
        self.assertTrue(self.m.BOLD_NAME_RE.search("MHeiHK-Bold"))
        self.assertTrue(self.m.BOLD_NAME_RE.search("NotoSansCJK-Black"))
        self.assertFalse(self.m.BOLD_NAME_RE.search("MSungHK-Light"))
        self.assertFalse(self.m.BOLD_NAME_RE.search("TimesLTStd-Roman"))
        self.assertFalse(self.m.BOLD_NAME_RE.search("MHeiHK-Medium"))
        # _span_bold 必须返回 0/16 整数（资源名拼接 ZS0/ZS16 与槽位索引
        # 依赖整数；布尔 True 会产出 ZSTrue/ZSFalse 且查不到粗体槽位）
        self.assertEqual(self.m._span_bold(4, "SourceHanSansCN-Heavy"), 16)
        self.assertEqual(self.m._span_bold(16, "SourceHanSansCN-Heavy"), 16)
        self.assertEqual(self.m._span_bold(4, "MSungHK-Light"), 0)
        self.assertEqual(self.m._span_bold(0, "MHeiHK-Bold"), 16)

    def test_collect_rows_roundtrip(self):
        import fitz

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "繁體測試", fontname="china-s", fontsize=12)
        rows = self.m.collect_rows(page)
        texts = [sp[2] for _, spans in rows for sp in spans]
        self.assertTrue(
            any("繁體測試" in t for t in texts),
            f"collect_rows 未解析出插入文本: {texts}",
        )
        doc.close()

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_pick_font_choice(self):
        # v1.1: pick_font 统一横排/竖排字体选择
        # 未转换 + 原字体可映射 -> 原字体; 转换过 -> 系统字体; 不可映射 -> 回退
        import fitz
        from fontTools.ttLib import TTFont

        from scripts import pdf_t2s

        renamed = os.path.join(tempfile.mkdtemp(), "orig.ttf")
        f = TTFont(os.path.join(pdf_t2s.BASE, "t2s-serif-regular.ttf"))
        for r in f["name"].names:
            if r.nameID == 6:
                r.string = "TestOrigSerif"
        f.save(renamed)

        fonts = pdf_t2s.load_fonts()
        doc_fonts = {"TestOrigSerif": (fitz.Font(fontfile=renamed), renamed, "ZO0", 1)}
        # 未转换且原字体可映射 -> 原字体
        rn, fp, _ = pdf_t2s.pick_font(
            fonts, doc_fonts, "TestOrigSerif", 0, "2021年12月", "2021年12月"
        )
        self.assertEqual(rn, "ZO0")
        self.assertEqual(fp, renamed)
        # 转换过 -> 系统宋体（原字体名 TestOrigSerif 归 serif 类）
        rn2, _, _ = pdf_t2s.pick_font(
            fonts, doc_fonts, "TestOrigSerif", 0, "繁體", "繁体"
        )
        self.assertEqual(rn2, "ZR0")
        # 未转换但原字体不可映射（如 CID CFF）-> 回退系统字体
        rn3, _, _ = pdf_t2s.pick_font(fonts, {}, "TestOrigSerif", 0, "2021年", "2021年")
        self.assertEqual(rn3, "ZR0")
        # 竖排逐字符走同一函数：单字符选择一致
        rn4, fp4, _ = pdf_t2s.pick_font(
            fonts, doc_fonts, "TestOrigSerif", 0, "年", "年"
        )
        self.assertEqual((rn4, fp4), ("ZO0", renamed))

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_pick_font_truncated_name_reuse(self):
        # MuPDF 截断 24 字符: span 字体名 TimesNewRomanPS-BoldItal 必须复用
        # 完整名 TimesNewRomanPS-BoldItalicMT 的原字体（否则回退正体丢斜体）
        import fitz
        from fontTools.ttLib import TTFont

        from scripts import pdf_t2s

        renamed = os.path.join(tempfile.mkdtemp(), "orig.ttf")
        f = TTFont(os.path.join(pdf_t2s.BASE, "t2s-serif-regular.ttf"))
        for r in f["name"].names:
            if r.nameID == 6:
                r.string = "TimesNewRomanPS-BoldItalicMT"
        f.save(renamed)

        fonts = pdf_t2s.load_fonts()
        doc_fonts = {
            "TimesNewRomanPS-BoldItalicMT": (
                fitz.Font(fontfile=renamed),
                renamed,
                "ZO0",
                1,
            )
        }
        # 未转换 span（rawdict 名被截断到 24 字符）→ 必须命中完整名复用 ZO
        rn, fp, _ = pdf_t2s.pick_font(
            fonts, doc_fonts, "TimesNewRomanPS-BoldItal", 0, "2B", "2B"
        )
        self.assertEqual((rn, fp), ("ZO0", renamed), "截断字体名未复用原字体")

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_italic_flag_preserved(self):
        # 假斜体（FontDescriptor 斜体位）重绘后必须补到新字体对象，否则
        # Chrome/Foxit 等理会标志的查看器中原文斜体、输出正体
        import fitz
        from fontTools.ttLib import TTFont

        from scripts import pdf_t2s

        renamed = os.path.join(tempfile.mkdtemp(), "orig.ttf")
        f = TTFont(os.path.join(pdf_t2s.BASE, "t2s-serif-regular.ttf"))
        for r in f["name"].names:
            if r.nameID == 6:
                r.string = "TestOrigSerif"
        f.save(renamed)

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(
            (72, 72), "ABC2021", fontname="F0", fontfile=renamed, fontsize=12
        )
        xref = page.get_fonts(full=True)[0][0]
        # insert_text 嵌入的 TTF 是 Type0/CID 结构，描述符在 DescendantFonts 内
        df = int(doc.xref_get_key(xref, "DescendantFonts")[1].strip("[]").split()[0])
        fdx = int(doc.xref_get_key(df, "FontDescriptor")[1].split()[0])
        doc.xref_set_key(fdx, "Flags", "34")  # 斜体(32)+衬线(2): 假斜体
        cc = __import__("opencc").OpenCC("t2s")
        fonts = pdf_t2s.load_fonts()
        doc_fonts = {
            "TestOrigSerif": (fitz.Font(fontfile=renamed), renamed, "ZO0", xref)
        }
        pdf_t2s.rebuild_page(page, cc, fonts, doc_fonts)

        zo_flags = []
        for x, _e, _t, _bf, rn, _enc, _fl in page.get_fonts(full=True):
            if rn == "ZO0":
                # 与 pdf_t2s._descriptor_target 相同导航（Type0/CID）
                df2 = int(
                    doc.xref_get_key(x, "DescendantFonts")[1].strip("[]").split()[0]
                )
                fdx2 = int(doc.xref_get_key(df2, "FontDescriptor")[1].split()[0])
                zo_flags.append(int(doc.xref_get_key(fdx2, "Flags")[1]))
        self.assertTrue(zo_flags, "未找到重绘后的 ZO0 字体对象")
        self.assertTrue(
            all(fl & 32 for fl in zo_flags),
            f"重绘字体未保留斜体位: Flags={zo_flags}",
        )
        doc.close()

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_subset_renderable(self):
        # 004 fix: fontTools Subsetter 默认 GID 重编号会破坏 Hiragino 的
        # CID-CFF 内部引用——子集"成功"但字形全空（渲染 0 墨迹），封面/目录/
        # 粗体文字静默消失。retain_gids=True 保留 GID 后子集必须可渲染。
        # 回归: 6 个槽位子集化后均须有墨迹（MuPDF 实测）。
        import fitz

        def render_ink(fontfile, text):
            doc = fitz.open()
            pg = doc.new_page()
            sh = fitz.Shape(pg)
            sh.insert_text(
                (50, 100), text, fontname="F", fontfile=fontfile, fontsize=11
            )
            sh.commit()
            pix = pg.get_pixmap(matrix=fitz.Matrix(1, 1))
            n = pix.n
            s = pix.samples
            ink = sum(1 for i in range(0, len(s), n) if sum(s[i : i + 3]) / 3 < 128)
            doc.close()
            return ink

        for fp in sorted(self.m.SYSTEM_FONT_PATHS):
            # Times 槽位纯拉丁（真实管线只给纯拉丁 span 用 Times），CJK 槽位混排
            chars = (
                "ABCabcXYZ0123-./()"
                if "times" in os.path.basename(fp)
                else "预期时间表测试ABC123"
            )
            mapping = self.m.subset_system_fonts({fp: set(chars)})
            self.assertIn(fp, mapping, f"{fp} 子集化失败")
            ink = render_ink(mapping[fp], chars)
            # 坏子集渲染为精确 0 墨迹；Times 纯拉丁槽位仅拉丁字形有墨迹，阈值放宽
            self.assertGreater(
                ink, 10, f"{os.path.basename(fp)} 子集字形为空（GID 重编号 bug）"
            )

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_subset_system_fonts(self):
        # v1.1: 子集化只作用于系统字体; 体积缩小、PS 名 ZT2S- 保留
        import fitz
        from fontTools.ttLib import TTFont

        from scripts import pdf_t2s

        times = pdf_t2s.LATIN_SERIF[0]
        mapping = pdf_t2s.subset_system_fonts({times: set("ABCabcXYZ0123")})
        self.assertIn(times, mapping)
        sub = mapping[times]
        self.assertLess(os.path.getsize(sub), os.path.getsize(times) / 10)
        ps = [r.toUnicode() for r in TTFont(sub)["name"].names if r.nameID == 6]
        self.assertTrue(any(p.startswith("ZT2S-") for p in ps), f"PS 名丢失: {ps}")
        # MuPDF 可加载子集字体且字形可用
        fobj = fitz.Font(fontfile=sub)
        self.assertTrue(fobj.has_glyph(ord("A")))
        self.assertFalse(fobj.has_glyph(ord("中")))  # 未请求的字形不在子集
        # 非系统字体路径不子集化
        self.assertEqual(
            pdf_t2s.subset_system_fonts({"/tmp/nonexistent.ttf": set("A")}), {}
        )

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_purge_unused_fonts(self):
        # v1.1: purge 对间接 Resources（PyMuPDF 路径不可操作）稳健——不抛异常、
        # 活字体不受影响；真实死字体清理由全量验收覆盖（30 页冒烟实测清 80 个）
        import fitz

        from scripts import pdf_t2s

        serif = pdf_t2s.CJK_FONTS[("serif", 0)]
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(
            (72, 72), "测试ABC", fontname="F0", fontfile=serif, fontsize=12
        )
        # 无论 Resources 是直接还是间接对象，purge 都不抛异常
        n = pdf_t2s.purge_unused_fonts(doc)
        self.assertGreaterEqual(n, 0)
        # 活字体 F0 仍在
        fonts_after = [row[4] for row in page.get_fonts(full=True)]
        self.assertIn("F0", fonts_after)
        doc.close()

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_rebuild_uses_subset_font(self):
        # v1.1 端到端: 子集字体重绘 -> 保存 -> 提取正确（无控制字符/无缺字）
        import fitz
        import opencc

        from scripts import pdf_t2s

        serif = pdf_t2s.CJK_FONTS[("serif", 0)]
        # 字符集必须是转换后文本（重绘字符）；若给原始繁体字符集会缺 体/测/试 字形
        mapping = pdf_t2s.subset_system_fonts({serif: set("繁体字测试2021年")})
        self.assertIn(serif, mapping)

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(
            (72, 72), "繁體字測試2021年", fontname="F0", fontfile=serif, fontsize=12
        )
        cc = opencc.OpenCC("t2s")
        fonts = pdf_t2s.load_fonts()
        pdf_t2s.rebuild_page(page, cc, fonts, {}, mapping)
        out = os.path.join(tempfile.mkdtemp(), "sub.pdf")
        doc.save(out, garbage=3, deflate=True)
        doc.close()

        d2 = fitz.open(out)
        got = d2[0].get_text()
        d2.close()
        ctrl = sorted({c for c in got if ord(c) < 32 and c not in "\n\r\t"})
        self.assertEqual(ctrl, [], f"子集字体嵌入后提取出控制字符: {ctrl}")
        self.assertEqual(self.v.norm(got), "繁体字测试2021年", f"提取文本不符: {got!r}")
        # 回归（004 跨平台）: Noto 系字体未子集化时 MuPDF 会把 年 映射为兼容区
        # U+F98E（提取变 年，OpenCC 归一化后误判"已转换"）；子集化剪掉兼容区
        # 映射后必须仍是常规码点——Linux 生产路径（先子集化再重绘）的保障
        compat = [hex(ord(c)) for c in got if 0xF900 <= ord(c) <= 0xFAFF]
        self.assertEqual(compat, [], f"子集重绘后出现兼容区码点: {compat}")
        # 体积: 子集嵌入应远小于完整 serif (20MB)
        self.assertLess(os.path.getsize(out) / 1e6, 1.0, f"子集嵌入体积过大: {out}")

    def test_split_blocks(self):
        # 003: 块划分均分、末块含余数、jobs>n 时块不重叠
        self.assertEqual(
            self.m._split_blocks(100, 4),
            [(0, 0, 25), (1, 25, 50), (2, 50, 75), (3, 75, 100)],
        )
        self.assertEqual(
            self.m._split_blocks(10, 4), [(0, 0, 2), (1, 2, 4), (2, 4, 6), (3, 6, 10)]
        )
        self.assertEqual(
            self.m._split_blocks(3, 8),
            [
                (0, 0, 0),
                (1, 0, 0),
                (2, 0, 0),
                (3, 0, 0),
                (4, 0, 0),
                (5, 0, 0),
                (6, 0, 0),
                (7, 0, 3),
            ],
        )
        # 覆盖全部页且不重叠
        for n, jobs in [(100, 4), (10, 4), (3, 8), (50, 1)]:
            blocks = self.m._split_blocks(n, jobs)
            pages = []
            for _, s, e in blocks:
                pages.extend(range(s, e))
            self.assertEqual(sorted(pages), list(range(n)))

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_parallel_degradation_small(self):
        # 003: 页数 < PARALLEL_MIN_PAGES -> 降级串行，输出正确
        import fitz
        import opencc

        from scripts import pdf_t2s

        serif = pdf_t2s.CJK_FONTS[("serif", 0)]
        doc = fitz.open()
        for _ in range(3):
            page = doc.new_page()
            page.insert_text(
                (72, 72), "繁體字測試", fontname="F0", fontfile=serif, fontsize=12
            )
        src = os.path.join(tempfile.mkdtemp(), "small.pdf")
        doc.save(src)
        doc.close()
        cc = opencc.OpenCC("t2s")
        fonts = pdf_t2s.load_fonts()
        out = pdf_t2s.process_pdf_parallel(src, cc, fonts, jobs=4)
        d2 = fitz.open(out)
        got = "".join(d2[p].get_text() for p in range(len(d2)))
        d2.close()
        self.assertEqual(self.v.norm(got), "繁体字测试" * 3, f"降级输出不符: {got!r}")

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_parallel_end_to_end(self):
        # 003: 60 页合成文档 jobs=2 并行 -> 文本正确、无 \x00
        import fitz
        import opencc

        from scripts import pdf_t2s

        serif = pdf_t2s.CJK_FONTS[("serif", 0)]
        doc = fitz.open()
        for _ in range(60):
            page = doc.new_page()
            page.insert_text(
                (72, 72), "繁體字測試2021年", fontname="F0", fontfile=serif, fontsize=12
            )
        src = os.path.join(tempfile.mkdtemp(), "par.pdf")
        doc.save(src)
        doc.close()
        cc = opencc.OpenCC("t2s")
        fonts = pdf_t2s.load_fonts()
        out = pdf_t2s.process_pdf_parallel(src, cc, fonts, jobs=2)
        d2 = fitz.open(out)
        self.assertEqual(len(d2), 60)
        bad = 0
        for p in range(len(d2)):
            t = d2[p].get_text()
            if "\x00" in t or self.v.norm(t) != "繁体字测试2021年":
                bad += 1
        d2.close()
        self.assertEqual(bad, 0, f"并行输出 {bad} 页文本异常")

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_parallel_preserves_toc(self):
        # 003 fix: insert_pdf 合并不拷贝 outline，并行路径必须手动 set_toc
        # （书签标题同时转简体）
        import fitz
        import opencc

        from scripts import pdf_t2s

        serif = pdf_t2s.CJK_FONTS[("serif", 0)]
        doc = fitz.open()
        for _ in range(60):
            page = doc.new_page()
            page.insert_text(
                (72, 72), "繁體字測試2021年", fontname="F0", fontfile=serif, fontsize=12
            )
        toc = [[1, "封面", 1], [1, "重要提示", 2], [2, "第一節", 5], [1, "第二節", 30]]
        doc.set_toc(toc)
        src = os.path.join(tempfile.mkdtemp(), "par_toc.pdf")
        doc.save(src)
        doc.close()
        cc = opencc.OpenCC("t2s")
        fonts = pdf_t2s.load_fonts()
        out = pdf_t2s.process_pdf_parallel(src, cc, fonts, jobs=2)
        d2 = fitz.open(out)
        d2_toc = d2.get_toc()
        d2.close()
        self.assertEqual(len(d2_toc), len(toc), f"并行输出丢失书签: {d2_toc}")
        # 标题转简体（節→节），页码不变
        expected = [
            [1, "封面", 1],
            [1, "重要提示", 2],
            [2, "第一节", 5],
            [1, "第二节", 30],
        ]
        self.assertEqual(d2_toc, expected, f"书签内容不符: {d2_toc}")


if __name__ == "__main__":
    unittest.main()
