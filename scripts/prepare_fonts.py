#!/usr/bin/env python3
"""从系统字体提取简体中文字体 face，供 pdf_t2s.py 重绘使用（跨平台）。

支持 macOS / Windows / Linux，策略为系统字体优先 + OFL 下载兜底 + 降级:
  - 系统字体优先: 各平台源表见 platform_sources()——macOS 用 Hiragino/Songti/Times，
    Windows 用微软雅黑/宋体/Times，Linux 多路径探测 Noto CJK + Liberation/DejaVu，
    并以 fc-match 兜底（发行版路径各异）
  - 下载兜底: 槽位在系统找不到字体时（--download 默认开启），从开源字体仓库下载
    Noto CJK SC（黑体/宋体）与 Tinos（Times 公制兼容，OFL）；--no-download 禁用
  - 降级: 下载不可用/禁用时，粗体槽位复用同风格常规 face（视觉降级）；
    拉丁槽位缺失由 pdf_t2s.py 的 fallback_font 自动回退 sans/serif

生成 6 个文件（默认输出到本脚本所在目录，即 pdf_t2s.py 查找字体的位置）:
  t2s-sans-regular.ttf / t2s-sans-bold.ttf   <- 黑体
  t2s-serif-regular.ttf / t2s-serif-bold.ttf <- 宋体
  t2s-times.ttf / t2s-times-bold.ttf         <- Times 类拉丁

关键点:
  - 系统字体的 PostScript 名（name 表 nameID 6）必须加 "ZT2S-" 前缀，
    防止与 PDF 原嵌入字体撞名导致 MuPDF 字体资源复用错误
  - 字体来源（Apple/微软版权 + Noto/Tinos 开源）一律不入库（.gitignore 已排除
    *.ttf/*.otf），均在目标机器生成

用法:
  python3 prepare_fonts.py [--out-dir 目录] [--no-download]
"""

import argparse
import atexit
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

from fontTools.ttLib import TTCollection, TTFont, TTLibError

BASE = os.path.dirname(os.path.abspath(__file__))
PS_PREFIX = "ZT2S-"
# 平台名（小写: darwin/windows/linux）。用 platform.system() 而非 sys.platform，
# 避免静态分析器按宿主平台窄化导致跨平台分支被误报为不可达。
SYSTEM = platform.system().lower()

NOTO_DIRS = [
    "/usr/share/fonts/opentype/noto",  # Ubuntu/Debian
    "/usr/share/fonts/noto-cjk",  # Arch
    "/usr/share/fonts/google-noto-cjk",  # Fedora
    "/usr/share/fonts/truetype/noto",
]
LIB_DIRS = [
    "/usr/share/fonts/truetype/liberation",  # Ubuntu/Debian
    "/usr/share/fonts/liberation-sans",  # Fedora
    "/usr/share/fonts/TTF",  # Arch
]
DEJAVU_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"]

NOTO_BASE = "https://github.com/notofonts/noto-cjk/raw/main"
GOOGLE_FONTS = "https://github.com/google/fonts/raw/main"

# 下载兜底（开源许可字体）: 槽位 -> (URL, 描述)
DOWNLOAD_SOURCES = {
    "sans-reg": (
        f"{NOTO_BASE}/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
        "Noto Sans CJK SC Regular（OFL）",
    ),
    "sans-bold": (
        f"{NOTO_BASE}/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Bold.otf",
        "Noto Sans CJK SC Bold（OFL）",
    ),
    "serif-reg": (
        f"{NOTO_BASE}/Serif/OTF/SimplifiedChinese/NotoSerifCJKsc-Regular.otf",
        "Noto Serif CJK SC Regular（OFL）",
    ),
    "serif-bold": (
        f"{NOTO_BASE}/Serif/OTF/SimplifiedChinese/NotoSerifCJKsc-Bold.otf",
        "Noto Serif CJK SC Bold（OFL）",
    ),
    "times-reg": (
        f"{GOOGLE_FONTS}/ofl/tinos/Tinos-Regular.ttf",
        "Tinos Regular（OFL，Times 公制兼容）",
    ),
    "times-bold": (
        f"{GOOGLE_FONTS}/ofl/tinos/Tinos-Bold.ttf",
        "Tinos Bold（OFL，Times 公制兼容）",
    ),
}

SLOTS = [
    ("t2s-sans-regular.ttf", "sans-reg", "黑体常规"),
    ("t2s-sans-bold.ttf", "sans-bold", "黑体粗体"),
    ("t2s-serif-regular.ttf", "serif-reg", "宋体常规"),
    ("t2s-serif-bold.ttf", "serif-bold", "宋体粗体"),
    ("t2s-times.ttf", "times-reg", "拉丁宋体"),
    ("t2s-times-bold.ttf", "times-bold", "拉丁宋体粗体"),
]
BOLD_FALLBACK = {
    "sans-bold": "sans-reg",
    "serif-bold": "serif-reg",
    "times-bold": "times-reg",
}


def platform_sources():
    """按平台返回系统字体候选: {槽位: [(路径, face_hint)]}
    face_hint: None=单 face 文件；str=按 PS 名先精确后边界子串选 face；
    int=固定索引（pick_face 保留的遗留能力，当前平台源表已不用）。"""
    if SYSTEM == "darwin":
        return _macos_sources()
    if SYSTEM == "windows":
        return _windows_sources()
    return _linux_sources()


def _macos_sources():
    fdir = "/System/Library/Fonts"
    sup = os.path.join(fdir, "Supplemental")
    return {
        # PS 名精确匹配选 face（review 004 fix: 原硬编码索引 0/2/6/1 在系统
        # 升级改变 TTC face 顺序时会静默选错字体；PS 名稳定且 pick_face
        # 先精确匹配，未命中才回退 face 0 并警告）
        "sans-reg": [(os.path.join(fdir, "Hiragino Sans GB.ttc"), "HiraginoSansGB-W3")],
        "sans-bold": [
            (os.path.join(fdir, "Hiragino Sans GB.ttc"), "HiraginoSansGB-W6")
        ],
        "serif-reg": [(os.path.join(sup, "Songti.ttc"), "STSongti-SC-Regular")],
        "serif-bold": [(os.path.join(sup, "Songti.ttc"), "STSongti-SC-Bold")],
        "times-reg": [(os.path.join(sup, "Times New Roman.ttf"), None)],
        "times-bold": [(os.path.join(sup, "Times New Roman Bold.ttf"), None)],
    }


def _windows_sources():
    fdir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    return {
        "sans-reg": [(os.path.join(fdir, "msyh.ttc"), "YaHei")],
        "sans-bold": [(os.path.join(fdir, "msyhbd.ttc"), "YaHei")],
        "serif-reg": [(os.path.join(fdir, "simsun.ttc"), "SimSun")],
        "serif-bold": [],  # Windows 无原生宋体粗体 -> 下载兜底/降级
        "times-reg": [(os.path.join(fdir, "times.ttf"), None)],
        "times-bold": [(os.path.join(fdir, "timesbd.ttf"), None)],
    }


def _linux_sources():
    # Linux: 静态路径探测 + fc-match 兜底
    out = {
        "sans-reg": [
            (os.path.join(d, "NotoSansCJK-Regular.ttc"), "sc") for d in NOTO_DIRS
        ],
        "sans-bold": [
            (os.path.join(d, "NotoSansCJK-Bold.ttc"), "sc") for d in NOTO_DIRS
        ],
        "serif-reg": [
            (os.path.join(d, "NotoSerifCJK-Regular.ttc"), "sc") for d in NOTO_DIRS
        ],
        "serif-bold": [
            (os.path.join(d, "NotoSerifCJK-Bold.ttc"), "sc") for d in NOTO_DIRS
        ],
        "times-reg": [
            (os.path.join(d, "LiberationSerif-Regular.ttf"), None) for d in LIB_DIRS
        ]
        + [(os.path.join(d, "DejaVuSerif.ttf"), None) for d in DEJAVU_DIRS],
        "times-bold": [
            (os.path.join(d, "LiberationSerif-Bold.ttf"), None) for d in LIB_DIRS
        ]
        + [(os.path.join(d, "DejaVuSerif-Bold.ttf"), None) for d in DEJAVU_DIRS],
    }
    for slot, pattern in {
        "sans-reg": "Noto Sans CJK SC",
        "sans-bold": "Noto Sans CJK SC:weight=bold",
        "serif-reg": "Noto Serif CJK SC",
        "serif-bold": "Noto Serif CJK SC:weight=bold",
        "times-reg": "Liberation Serif",
        "times-bold": "Liberation Serif:weight=bold",
    }.items():
        path = fc_file(pattern)
        if path:
            out[slot].append(
                (path, "sc" if slot.startswith(("sans-", "serif-")) else None)
            )
    return out


def fc_file(pattern):
    """fc-match 查字体文件路径（Linux，fontconfig 不可用时返回 None）"""
    try:
        r = subprocess.run(
            ["fc-match", "-f", "%{file}", pattern],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        path = r.stdout.strip()
        return path if path and os.path.exists(path) else None
    except Exception:  # noqa: BLE001 — 无 fontconfig 时静默跳过
        return None


def pick_face(collection, hint):
    """从 ttc 按 PS 名（nameID 6）选 face，未命中回退 face 0（附警告）。
    hint 为 int 时按索引（macOS 遗留已验证索引）；为 str 时先精确匹配 PS 名
    （防 "SimSun" 被 "NSimSun" 抢先——子串匹配在 ttc face 顺序变化时不稳），
    再按边界子串匹配——子串必须收尾在边界（- 或结尾），否则 "sc" 会误中所有
    Noto face 的 "NotoS**ans**CJK" 内部子串（jp/kr/tc/hk 全命中），选错 region face。"""
    if isinstance(hint, int):
        return collection.fonts[hint]
    if isinstance(hint, str):
        exact = hint.lower()
        pattern = re.compile(re.escape(exact) + r"(?:-|$|\b)")
        for font in collection.fonts:
            for record in font["name"].names:
                if record.nameID == 6 and record.toUnicode().lower() == exact:
                    return font
        for font in collection.fonts:
            for record in font["name"].names:
                if record.nameID == 6 and pattern.search(record.toUnicode().lower()):
                    return font
        print(
            f"  ⚠ face hint {hint!r} 未命中任何 face，回退 face 0"
            "（字形可能非目标 region）",
            flush=True,
        )
    return collection.fonts[0]


def rename_ps_name(font):
    """给 PostScript 名（nameID 6）加 ZT2S- 前缀，防与 PDF 原字体撞名"""
    for record in font["name"].names:
        if record.nameID == 6 and not record.toUnicode().startswith(PS_PREFIX):
            record.string = PS_PREFIX + record.toUnicode()


_download_cache = {}


def _cleanup_downloads():
    """进程退出时清理下载临时文件（review 004 fix: mkstemp 文件 /tmp 泄漏）"""
    for tmp in _download_cache.values():
        try:
            os.unlink(tmp)
        except OSError:
            pass


def download(url):
    """下载到临时文件（URL 级缓存），失败返回 None"""
    if url in _download_cache:
        return _download_cache[url]
    tmp = None
    try:
        # Accept 头必须带——GitHub 对无 Accept 的请求返回 404（边缘节点行为，
        # curl 默认带 */* 而 urllib 默认不带；实测缺此头必 404）
        req = urllib.request.Request(
            url, headers={"User-Agent": "pdf-t2s", "Accept": "*/*"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            fd, tmp = tempfile.mkstemp(prefix="pdf-t2s-dl-", suffix=".bin")
            with os.fdopen(fd, "wb") as fh:
                shutil.copyfileobj(resp, fh)
        _download_cache[url] = tmp
        return tmp
    except Exception as e:  # noqa: BLE001 — 下载失败走降级路径
        print(f"    ! 下载失败（{e}），该槽位将降级", flush=True)
        return None


def resolve_slot(key, candidates, download_allowed):
    """返回 (源描述, 字体路径, face_hint)；系统候选优先，其次下载兜底"""
    for path, hint in candidates:
        if os.path.exists(path):
            return f"系统字体: {path}", path, hint
    if not download_allowed:
        return None, None, None
    url, desc = DOWNLOAD_SOURCES[key]
    path = download(url)
    if path:
        return f"下载 {desc}", path, None
    return None, None, None


def extract_and_save(src_path, hint, out_path):
    """提取 face（ttc 按 hint 选 face）→ PS 名加 ZT2S- 前缀 → 保存。
    返回 PS 名变化描述。fc-match 可能返回单 face 文件（非 ttc），容错为单字体。"""
    if hint is None:
        font = TTFont(src_path)
    else:
        try:
            font = pick_face(TTCollection(src_path), hint)
        except TTLibError:  # 单 face 文件（非 ttc）按 TTFont 处理
            font = TTFont(src_path)
    before = [r.toUnicode() for r in font["name"].names if r.nameID == 6][:1]
    rename_ps_name(font)
    after = [r.toUnicode() for r in font["name"].names if r.nameID == 6][:1]
    font.save(out_path)
    return f"{before[0] if before else '?'} -> {after[0] if after else '?'}"


def main():
    atexit.register(_cleanup_downloads)  # 下载临时文件进程退出即清理
    # Windows 上 stdout 重定向（管道/CI 日志）时用 locale 编码会中文乱码/报错
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="从系统字体提取 pdf_t2s 所需的中文字体（macOS/Windows/Linux）"
    )
    parser.add_argument(
        "--out-dir", default=BASE, help="输出目录（默认: 本脚本所在目录）"
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="禁用开源字体下载兜底（缺槽位时降级/跳过）",
    )
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    sources = platform_sources()
    print(f"平台: {SYSTEM}，系统字体候选就绪", flush=True)
    ok = set()
    for out_name, key, desc in SLOTS:
        src_desc, src_path, hint = resolve_slot(key, sources[key], not args.no_download)
        if src_path is None:
            # 下载禁用或下载失败（download() 已打印具体原因）
            print(f"  {desc}: !! 槽位空缺（无系统字体，且下载禁用或失败）", flush=True)
            continue
        out = os.path.join(args.out_dir, out_name)
        try:
            ps_note = extract_and_save(src_path, hint, out)
            ok.add(key)
            print(f"  {desc}: {src_desc}（{ps_note}）-> {out_name}", flush=True)
        except Exception as e:  # noqa: BLE001 — 单槽位失败不中断，走降级
            if not args.no_download and src_desc and src_desc.startswith("系统字体"):
                # 系统字体文件存在但损坏/不可读: 绕过候选直接走下载兜底
                # （resolve_slot 会再次命中损坏路径，不能重试候选列表）
                dl_url, dl_desc = DOWNLOAD_SOURCES[key]
                dl_path = download(dl_url)
                if dl_path:
                    try:
                        ps_note = extract_and_save(dl_path, None, out)
                        ok.add(key)
                        print(
                            f"  {desc}: 系统字体损坏，改用下载 {dl_desc}"
                            f"（{ps_note}）-> {out_name}",
                            flush=True,
                        )
                        continue
                    except Exception as dl_e:  # noqa: BLE001 — 下载兜底也失败
                        print(
                            f"    ! 下载兜底 {dl_desc} 也提取失败（{dl_e}）",
                            flush=True,
                        )
            print(f"  {desc}: !! 提取失败（{e}），槽位空缺", flush=True)
    # 降级: 粗体槽位复用同风格常规 face（视觉降级，保证 6 文件齐全）
    for bold_key, reg_key in BOLD_FALLBACK.items():
        if bold_key in ok or reg_key not in ok:
            continue
        reg_out = os.path.join(args.out_dir, slot_file(reg_key))
        bold_out = os.path.join(args.out_dir, slot_file(bold_key))
        shutil.copyfile(reg_out, bold_out)
        ok.add(bold_key)
        print(
            f"  粗体降级: {slot_file(bold_key)} 复用常规 face（粗体 span 视觉降级）",
            flush=True,
        )
    missing = [name for name, key, _ in SLOTS if key not in ok]
    print(
        f"完成: {len(ok)}/6 个字体可用" + (f"，缺: {missing}" if missing else ""),
        flush=True,
    )
    if missing:
        print(
            "  提示: 缺槽位会导致转换质量下降或失败——可安装系统字体包"
            "（Linux: fonts-noto-cjk + fonts-liberation）或启用下载（去掉 --no-download）",
            flush=True,
        )
    # 一个字体都没产出视为失败（退出码 1，供 CI/脚本检测）；部分降级（如
    # 粗体复用常规）是既定设计，仍为成功
    return 1 if not ok else 0


def slot_file(key):
    return next(name for name, k, _ in SLOTS if k == key)


if __name__ == "__main__":
    sys.exit(main())
