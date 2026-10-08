"""notice-hub 品牌图标生成器（纯 Python + Pillow，确定性、可重跑）。

用法：
    .venv\\Scripts\\python.exe assets\\make_icon.py

产物：
    assets\\icon.ico        真多尺寸 ICO（16/24/32/48/64/128/256）
    assets\\icon-16.png     自检小尺寸
    assets\\icon-256.png    托盘图标源
    assets\\icon-512.png
    .reports\\logo-candidate-1.png / -2.png / -3.png   512×512，白底 + 深色底各一块
    .reports\\logo-final-light-dark.png                正式 mark 的深浅底预览
    .reports\\logo-final-16x-upscaled.png              icon-16.png 的 16× 最近邻放大（自检）

设计语义：QQ 群通知 → 待办 / 日历。mark 只用 1–2 色、粗笔画，保证 16×16 可认。
三个候选只是「造型 / 配色 / 语义」三种取向；当前正式 mark = 候选 3（圆＋勾＋琥珀点，用户选定）。
要换取向，改 DEFAULT_STYLE 一处即可，其余全部重算。
"""

from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPORTS = ROOT / ".reports"

# 与 launcher.py 现有内联图标同源的品牌主色，ui-taste.md 第 9/13 条：单一主强调色。
BRAND = (18, 105, 91, 255)  # #12695b
NAVY = (23, 59, 52, 255)  # #173b34
AMBER = (18, 105, 91, 255)  # #12695b
WHITE = (255, 255, 255, 255)

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
DEFAULT_STYLE = "dot"

STYLES = {
    "bubble": {"fill": BRAND, "accent": WHITE, "check": WHITE},
    "calendar": {"fill": BRAND, "accent": WHITE, "check": WHITE},
    "dot": {"fill": NAVY, "accent": AMBER, "check": WHITE},
}


def _render_base(style: str, size: int) -> Image.Image:
    """在 size×size 上以超采样绘制 mark，返回抗锯齿后的 RGBA 图。"""
    ss = 8 if size < 64 else 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    c = STYLES[style]

    def px(v: float) -> float:
        return v * s

    def stroke(points, width: int, fill) -> None:
        """折线 + 圆头端点：Pillow 的 line() 只圆化拐点、端头是平口，
        这里补两个圆端，和手写 SVG 的 stroke-linecap="round" 逐像素对齐。"""
        pts = [(px(x), px(y)) for x, y in points]
        draw.line(pts, fill=fill, width=width, joint="curve")
        r = width / 2.0
        for cx, cy in (pts[0], pts[-1]):
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)

    if style == "bubble":
        # 圆角气泡（群通知）+ 左下小尾巴，内部粗勾（待办完成）。
        draw.rounded_rectangle((px(0.10), px(0.10), px(0.90), px(0.68)), radius=px(0.18), fill=c["fill"])
        draw.polygon([(px(0.20), px(0.56)), (px(0.46), px(0.60)), (px(0.24), px(0.94))], fill=c["fill"])
        stroke([(0.30, 0.40), (0.42, 0.52), (0.70, 0.25)], int(px(0.11)), c["check"])
    elif style == "calendar":
        # 日历本（日期）+ 顶部装订条 + 粗勾（待办）。
        draw.rounded_rectangle((px(0.12), px(0.16), px(0.88), px(0.92)), radius=px(0.14), fill=c["fill"])
        draw.rectangle((px(0.12), px(0.335), px(0.88), px(0.375)), fill=c["accent"])
        stroke([(0.30, 0.52), (0.42, 0.64), (0.70, 0.44)], int(px(0.10)), c["check"])
    elif style == "dot":
        # 圆形通知 + 右上未读角标（通知点）+ 粗勾（已处理）。
        # 16px 调优：勾加粗到 0.13（≈2.1px@16）、角标整体放大且白描边加厚到 0.06，
        # 并内收避免右/上边缘被裁。勾整体压在圆的左下象限，长臂末端 (0.54,0.46)
        # 停在角标白环外 —— 否则小尺寸下勾尖会贴到白环、整条勾与角标连成一道斜杠。
        draw.ellipse((px(0.05), px(0.15), px(0.85), px(0.95)), fill=c["fill"])
        draw.ellipse((px(0.57), px(0.00), px(0.99), px(0.42)), fill=WHITE)
        draw.ellipse((px(0.63), px(0.06), px(0.93), px(0.36)), fill=c["accent"])
        stroke([(0.22, 0.60), (0.36, 0.74), (0.54, 0.46)], int(px(0.13)), c["check"])
    else:
        raise ValueError("unknown style: " + style)

    return img.resize((size, size), Image.LANCZOS)


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 老 Pillow 没有 size 参数
        return ImageFont.load_default()


def _ico_entries(path: Path) -> list[tuple[int, int, int]]:
    """解析 ICO 目录项，返回 [(w, h, bytes)]，用于证明是真多尺寸。"""
    data = path.read_bytes()
    count = struct.unpack("<H", data[4:6])[0]
    out = []
    for i in range(count):
        base = 6 + i * 16
        w, h = data[base], data[base + 1]
        size = struct.unpack("<I", data[base + 8:base + 12])[0]
        out.append((w or 256, h or 256, size))
    return out


def _sheet(style: str, title: str, label: str) -> Image.Image:
    """512×512：左白底、右深色底，各一块 256×512 面板，中间放 160px mark。"""
    sheet = Image.new("RGBA", (512, 512), (255, 255, 255, 255))
    sheet.paste(Image.new("RGBA", (256, 512), (22, 24, 29, 255)), (256, 0))
    mark = _render_base(style, 160)
    for i, bg_dark in enumerate((False, True)):
        x0 = i * 256
        sheet.alpha_composite(mark, (x0 + 48, 150))
        ink = (255, 255, 255, 255) if bg_dark else (17, 24, 39, 255)
        draw = ImageDraw.Draw(sheet)
        draw.text((x0 + 16, 16), title, font=_font(22), fill=ink)
        draw.text((x0 + 16, 448), label, font=_font(18), fill=ink)
        accent = (138, 180, 255, 255) if bg_dark else BRAND
        draw.text((x0 + 16, 480), "notice hub", font=_font(18), fill=accent)
    return sheet


def _candidate_sheet(style: str, index: int) -> Image.Image:
    labels = {"bubble": "1  bubble + check", "calendar": "2  calendar + check", "dot": "3  dot + check"}
    return _sheet(style, "Candidate %d" % index, labels[style])


def _final_sheet(style: str) -> Image.Image:
    labels = {"bubble": "1  bubble + check", "calendar": "2  calendar + check", "dot": "3  dot + check"}
    return _sheet(style, "final mark", labels[style])


def _upscale_16x() -> Image.Image:
    """把已生成的 icon-16.png 用最近邻放大 16 倍（256×256），肉眼核查小尺寸可读性。"""
    with Image.open(HERE / "icon-16.png") as small:
        return small.convert("RGBA").resize((256, 256), Image.NEAREST)


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    frames = {size: _render_base(DEFAULT_STYLE, size) for size in ICO_SIZES}
    large = max(ICO_SIZES)
    ico_path = HERE / "icon.ico"
    frames[large].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=[frames[s] for s in ICO_SIZES if s != large],
    )

    outputs = [ico_path]
    for size in (16, 256, 512):
        p = HERE / ("icon-%d.png" % size)
        _render_base(DEFAULT_STYLE, size).save(p, format="PNG")
        outputs.append(p)

    for i, style in enumerate(("bubble", "calendar", "dot"), start=1):
        p = REPORTS / ("logo-candidate-%d.png" % i)
        _candidate_sheet(style, i).save(p, format="PNG")
        outputs.append(p)

    final_sheet = REPORTS / "logo-final-light-dark.png"
    _final_sheet(DEFAULT_STYLE).save(final_sheet, format="PNG")
    outputs.append(final_sheet)
    upscaled = REPORTS / "logo-final-16x-upscaled.png"
    _upscale_16x().save(upscaled, format="PNG")
    outputs.append(upscaled)

    print("style = %s" % DEFAULT_STYLE)
    for p in outputs:
        print("%-46s %8d bytes" % (str(p.relative_to(ROOT)), p.stat().st_size))
    for w, h, n in _ico_entries(ico_path):
        print("icon.ico entry %3d x %-3d %6d bytes" % (w, h, n))
    print("done: %d files" % len(outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
