"""Compose the live-site hero (skyline + 5 cutouts + title) into a single PNG
suitable for use as a README / portfolio cover. Mirrors the CSS layout in
`frontend/src/App.css` (.landing-hero / .cutout-*) at a fixed 1600x900 canvas.

Usage:
    python scripts/compose_hero.py
Outputs:
    imageAssets/hero.png
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageEnhance


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "imageAssets"

CANVAS_W, CANVAS_H = 1600, 900


def load(name: str) -> Image.Image:
    return Image.open(ASSETS / name).convert("RGBA")


def cover_resize(img: Image.Image, w: int, h: int) -> Image.Image:
    """object-fit: cover; object-position: center."""
    src_ratio = img.width / img.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        new_h = h
        new_w = int(round(h * src_ratio))
    else:
        new_w = w
        new_h = int(round(w / src_ratio))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return img.crop((left, top, left + w, top + h))


def fit_height(img: Image.Image, target_h: int) -> Image.Image:
    ratio = target_h / img.height
    return img.resize(
        (int(round(img.width * ratio)), target_h), Image.LANCZOS
    )


def vertical_dim_gradient(w: int, h: int) -> Image.Image:
    """Match .landing-dim — dark top + bottom, clear middle."""
    grad = Image.new("RGBA", (1, h))
    px = grad.load()
    stops = [
        (0.00, 0.55),
        (0.28, 0.10),
        (0.55, 0.00),
        (1.00, 0.55),
    ]
    for y in range(h):
        t = y / (h - 1)
        for i in range(len(stops) - 1):
            t0, a0 = stops[i]
            t1, a1 = stops[i + 1]
            if t0 <= t <= t1:
                k = (t - t0) / (t1 - t0) if t1 > t0 else 0
                a = a0 + (a1 - a0) * k
                break
        else:
            a = stops[-1][1]
        px[0, y] = (12, 8, 10, int(round(a * 255)))
    return grad.resize((w, h))


def paste_cutout(canvas: Image.Image, cut: Image.Image, *,
                 height_vh: float, x_pct: float | None = None,
                 right_pct: float | None = None, center: bool = False,
                 brightness: float = 1.0,
                 shadow_offset: int = 10, shadow_blur: int = 14,
                 shadow_alpha: int = 115) -> None:
    """Place cutout anchored to bottom of canvas, sized by vh fraction.

    Reproduces:
      .cutout { bottom: 0; height: <h>vh; }
      .cutout-* { left: <x>% | right: <x>% | translateX(-50%); }
      filter: drop-shadow(0 10px 14px rgba(0,0,0,0.45));
    """
    target_h = int(round(CANVAS_H * height_vh))
    cut = fit_height(cut, target_h)
    if brightness != 1.0:
        cut = ImageEnhance.Brightness(cut).enhance(brightness)

    if center:
        x = (CANVAS_W - cut.width) // 2
    elif right_pct is not None:
        x = CANVAS_W - int(round(CANVAS_W * right_pct / 100)) - cut.width
    else:
        x = int(round(CANVAS_W * (x_pct or 0) / 100))
    y = CANVAS_H - cut.height

    # Drop shadow: alpha mask of cut, blurred, offset down.
    from PIL import ImageFilter
    alpha = cut.split()[-1]
    shadow = Image.new("RGBA", cut.size, (0, 0, 0, 0))
    shadow.putalpha(alpha)
    shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_layer.paste(shadow, (x, y + shadow_offset), shadow)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(shadow_blur))
    # Tint shadow to black with given alpha
    sa = shadow_layer.split()[-1].point(lambda v: int(v * shadow_alpha / 255))
    shadow_solid = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_solid.putalpha(sa)
    canvas.alpha_composite(shadow_solid)
    canvas.alpha_composite(cut, dest=(x, y))


def find_font(size: int, candidates: list[str]) -> ImageFont.FreeTypeFont:
    win_fonts = Path(r"C:\Windows\Fonts")
    for name in candidates:
        for p in [win_fonts / name, Path(name)]:
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def draw_title(canvas: Image.Image) -> None:
    draw = ImageDraw.Draw(canvas)

    title = "CONDOSIM"
    subtitle = "CONDOMINIO  VIA  GARIBALDI"
    tagline = "Cinque residenti. Una chat. Un amministratore."

    title_font = find_font(180, ["impact.ttf", "Impact.ttf", "Anton-Regular.ttf",
                                 "BebasNeue-Regular.ttf"])
    sub_font = find_font(28, ["bahnschrift.ttf", "BebasNeue-Regular.ttf",
                              "impact.ttf", "arialbd.ttf"])
    tag_font = find_font(20, ["seguisbi.ttf", "segoeuii.ttf", "arial.ttf"])

    # Title — drop shadow + body
    tx = CANVAS_W // 2
    ty = int(CANVAS_H * 0.06)

    def centered(text, font, y, fill, *, stroke=0, stroke_fill=None,
                 letter_spacing=0):
        if letter_spacing:
            # Manually space characters
            widths = [font.getbbox(ch)[2] - font.getbbox(ch)[0] for ch in text]
            total = sum(widths) + letter_spacing * (len(text) - 1)
            cx = (CANVAS_W - total) // 2
            for ch, w in zip(text, widths):
                draw.text((cx, y), ch, font=font, fill=fill,
                          stroke_width=stroke, stroke_fill=stroke_fill)
                cx += w + letter_spacing
        else:
            bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
            w = bbox[2] - bbox[0]
            draw.text(((CANVAS_W - w) // 2, y), text, font=font, fill=fill,
                      stroke_width=stroke, stroke_fill=stroke_fill)

    # Soft shadow under title
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    bbox = sd.textbbox((0, 0), title, font=title_font, stroke_width=3)
    tw = bbox[2] - bbox[0]
    sd.text(((CANVAS_W - tw) // 2, ty + 6), title, font=title_font,
            fill=(0, 0, 0, 115))
    from PIL import ImageFilter
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    canvas.alpha_composite(shadow)

    centered(title, title_font, ty, (255, 243, 217, 255),
             stroke=3, stroke_fill=(18, 10, 6, 220))
    centered(subtitle, sub_font, ty + 200, (255, 240, 215, 235),
             letter_spacing=8)
    centered(tagline, tag_font, ty + 244, (255, 235, 210, 220))


def main() -> None:
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (42, 28, 31, 255))

    bg = cover_resize(load("gta_skyline_backdrop.png"), CANVAS_W, CANVAS_H)
    canvas.alpha_composite(bg)
    canvas.alpha_composite(vertical_dim_gradient(CANVAS_W, CANVAS_H))

    # z-order matches CSS z-index (low → high).
    paste_cutout(canvas, load("gta_marchetti_cutout.png"),
                 height_vh=0.46, x_pct=18, brightness=0.88,
                 shadow_offset=8, shadow_blur=10, shadow_alpha=128)
    paste_cutout(canvas, load("gta_conti_cutout.png"),
                 height_vh=0.48, x_pct=2)
    paste_cutout(canvas, load("gta_ferrari_cutout.png"),
                 height_vh=0.50, x_pct=60)
    paste_cutout(canvas, load("gta_greco_cutout.png"),
                 height_vh=0.56, right_pct=3)
    paste_cutout(canvas, load("gta_romano_cutout.png"),
                 height_vh=0.68, center=True)

    draw_title(canvas)

    out = ROOT / "imageAssets" / "hero.png"
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
