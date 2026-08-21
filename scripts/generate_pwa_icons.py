"""
Generates the PWA home-screen icons (frontend/icons/icon-*.png) from the
real MutemoOS logo (frontend/icons/logo-source.png -- the navy/teal
footprint mark, transparent background). Not run at app startup or in CI
-- run manually whenever the source logo changes:
`python scripts/generate_pwa_icons.py`. Uses Pillow, already a project
dependency (OCR image handling), so this needs nothing new.

The source is portrait (433x576), not square, so each output is the logo
scaled to fit within a padded safe zone and centered on a square canvas
filled with the manifest's background_color -- a plain background here is
deliberate: several OS home-screen/app-switcher contexts render a
transparent PNG icon oddly (e.g. on a black or white square with no
adaptation), where a filled square always looks correct. The maskable
variant uses a smaller safe zone since the OS may crop it to any shape
(circle, squircle, etc).
"""
import os
from PIL import Image

BACKGROUND = "#faf8f4"  # manifest.json's background_color
SRC = os.path.join(os.path.dirname(__file__), "..", "frontend", "icons", "logo-source.png")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend", "icons")


def make_icon(size: int, path: str, safe_zone: float):
    canvas = Image.new("RGBA", (size, size), BACKGROUND)
    logo = Image.open(SRC).convert("RGBA")

    target = int(size * safe_zone)
    scale = min(target / logo.width, target / logo.height)
    logo = logo.resize((round(logo.width * scale), round(logo.height * scale)), Image.LANCZOS)

    x = (size - logo.width) // 2
    y = (size - logo.height) // 2
    canvas.paste(logo, (x, y), logo)  # logo's own alpha as the paste mask
    canvas.convert("RGB").save(path, "PNG")


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    make_icon(192, os.path.join(OUT_DIR, "icon-192.png"), safe_zone=0.82)
    make_icon(512, os.path.join(OUT_DIR, "icon-512.png"), safe_zone=0.82)
    make_icon(512, os.path.join(OUT_DIR, "icon-512-maskable.png"), safe_zone=0.65)
    print(f"Wrote icons to {OUT_DIR}")
