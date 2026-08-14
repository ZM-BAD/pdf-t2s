"""pdf_t2s 核心逻辑测试。

设计要点:
  - 无外部依赖的环境（裸 python3）下全部跳过，保证
    `python3 -m unittest discover` 作为 CI/本地门槛始终可执行;
    在安装依赖的 venv 中则完整运行（README 有说明）。
  - 不依赖真实招股书样本（敏感文件不入库），用纯函数 + 合成 PDF 验证。
"""

import importlib.util
import unittest

HAS_DEPS = all(importlib.util.find_spec(m) for m in ("fitz", "opencc"))


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
