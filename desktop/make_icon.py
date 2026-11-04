"""Render the Cairn app icon (the favicon's three-node graph on a rounded tile) to .ico/.png.

    uv run --project cairn --group build python desktop/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "cairn" / "src" / "cairn" / "desktop" / "assets"
SIZE = 1024  # draw large, downsample for smooth edges
S = SIZE / 64  # the favicon's 64-unit coordinate space

TILE = (15, 23, 42, 255)      # slate-900
EDGE = (148, 163, 184, 255)   # slate-400
NODE = (59, 130, 246, 255)    # blue-500
RING = (241, 245, 249, 255)   # slate-100


def render() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=int(SIZE * 0.22), fill=TILE)

    def p(x, y):
        return (x * S, y * S)

    # Slightly enlarged and centred version of the favicon geometry.
    scale, ox, oy = 1.18, -6.3, -5.8
    def q(x, y):
        return p(x * scale + ox, y * scale + oy)

    for a, b in (((23.2, 29.98), (42.3, 21.05)), ((23.2, 34.02), (42.3, 42.95))):
        d.line([q(*a), q(*b)], fill=EDGE, width=int(4.2 * S * scale))
        for end in (a, b):  # round caps
            cx, cy = q(*end)
            r = 2.1 * S * scale
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=EDGE)
    for cx, cy in ((18, 32), (47, 18), (47, 46)):
        x, y = q(cx, cy)
        outer, inner = 7.2 * S * scale, 4.3 * S * scale
        d.ellipse([x - outer, y - outer, x + outer, y + outer], fill=RING)
        d.ellipse([x - inner, y - inner, x + inner, y + inner], fill=NODE)
    return img


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    big = render()
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    big.resize((256, 256), Image.LANCZOS).save(ASSETS / "cairn.ico", sizes=[(s, s) for s in sizes])
    big.resize((512, 512), Image.LANCZOS).save(ASSETS / "cairn.png")
    # Installer wizard images (Inno Setup): small top-right logo.
    small = Image.new("RGB", (138, 140), (255, 255, 255))
    logo = big.resize((110, 110), Image.LANCZOS)
    small.paste(logo, (14, 15), logo)
    small.save(ROOT / "desktop" / "wizard-small.bmp")
    print("wrote", ASSETS / "cairn.ico")


if __name__ == "__main__":
    main()
