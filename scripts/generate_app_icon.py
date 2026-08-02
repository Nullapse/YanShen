"""Generate the approved option E raster and Windows application marks."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SIZE = 1024
SCALE = SIZE / 256
BRAND_FONT = Path(r"C:\Windows\Fonts\STSONG.TTF")


def s(value: float) -> int:
    return round(value * SCALE)


def build_icon() -> Image.Image:
    canvas = Image.new("RGBA", (SIZE, SIZE))
    draw = ImageDraw.Draw(canvas)
    ivory = (248, 245, 236, 255)
    green = (53, 105, 92, 255)
    gold = (181, 139, 79, 255)

    # Option E: an ivory outer tile, compact green field, correct 申 glyph,
    # and one small brown-gold registration square.
    draw.rounded_rectangle(
        (s(18), s(18), s(238), s(238)),
        radius=s(34),
        fill=ivory,
    )
    draw.rounded_rectangle(
        (s(49), s(49), s(207), s(207)),
        radius=s(13),
        fill=green,
    )
    brand_font = ImageFont.truetype(str(BRAND_FONT), s(132))
    glyph_box = draw.textbbox((0, 0), "申", font=brand_font, stroke_width=s(1))
    glyph_x = s(128) - (glyph_box[0] + glyph_box[2]) / 2
    glyph_y = s(128) - (glyph_box[1] + glyph_box[3]) / 2
    draw.text(
        (glyph_x, glyph_y),
        "申",
        font=brand_font,
        fill=ivory,
        stroke_width=s(1),
        stroke_fill=ivory,
    )
    draw.rectangle((s(31), s(31), s(48), s(48)), fill=gold)
    return canvas.resize((256, 256), Image.Resampling.LANCZOS)


def main() -> None:
    icon = build_icon()
    png_path = ROOT / "static" / "app-icon.png"
    ico_path = ROOT / "assets" / "app-icon.ico"
    png_temp = png_path.with_name(f".{png_path.name}.tmp")
    ico_temp = ico_path.with_name(f".{ico_path.name}.tmp")
    icon.save(png_temp, format="PNG")
    icon.save(
        ico_temp,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    os.replace(png_temp, png_path)
    os.replace(ico_temp, ico_path)
    print(f"generated {png_path.relative_to(ROOT)} and {ico_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
