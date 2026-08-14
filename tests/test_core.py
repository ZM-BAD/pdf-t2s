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


@unittest.skipUnless(
    HAS_DEPS, "未安装 pymupdf/opencc（pip install -r requirements.txt）"
)
class TestHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts import pdf_t2s, verify

        cls.m = pdf_t2s
        cls.v = verify

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

    @unittest.skipUnless(FONTS_READY, "需要 prepare_fonts.py 生成的 t2s-*.ttf")
    def test_fallback_font_maps_all_chars(self):
        # 回退字体必须能映射全部字符: '‧'(U+2027) 仅 Songti 有字形
        fonts = self.m.load_fonts()
        rn, _, fobj = self.m.fallback_font(fonts, "MSungHK-Light", 0, "‧", False)
        self.assertEqual(rn, "ZR0")  # Times/Hiragino 无此字形 → 落到 Songti
        self.assertTrue(self.m.can_map(fobj, "‧"))
        # 含 CJK 的回退按黑体/宋体类，且必能映射
        rn2, _, fobj2 = self.m.fallback_font(fonts, "MHeiHK", 0, "测试中", True)
        self.assertEqual(rn2, "ZS0")
        self.assertTrue(all(self.m.can_map(fobj2, c) for c in "测试中"))

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
        page.insert_text(
            (72, 72), "2021年12月2日", fontname="F0", fontfile=renamed, fontsize=12
        )  # 同形字，转换前后相同
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
            out_fonts.get("2021年12月2日"),
            "TestOrigSerif",
            "未转换 span 必须复用原 PDF 字体",
        )
        self.assertEqual(
            out_fonts.get("繁体字测试"),
            "ZT2S-STSongti-SC-Regular",
            "转换 span 必须用系统简体字体",
        )
        doc.close()

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


if __name__ == "__main__":
    unittest.main()
