#!/usr/bin/env python3
"""Make the TribePlan logo's black background transparent so it fits any background."""
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Install Pillow: pip install Pillow")
    raise

ROOT = Path(__file__).resolve().parent.parent
LOGO_PATH = ROOT / "static" / "img" / "logo-tribeplan.png"
# Pixels with R,G,B all below this become transparent (keeps blue/teal intact)
BLACK_THRESHOLD = 25


def main():
    if not LOGO_PATH.exists():
        print(f"Logo not found: {LOGO_PATH}")
        return
    img = Image.open(LOGO_PATH).convert("RGBA")
    data = img.getdata()
    new_data = []
    for item in data:
        r, g, b, a = item
        if r <= BLACK_THRESHOLD and g <= BLACK_THRESHOLD and b <= BLACK_THRESHOLD:
            new_data.append((r, g, b, 0))
        else:
            new_data.append(item)
    img.putdata(new_data)
    img.save(LOGO_PATH, "PNG")
    print(f"Saved transparent logo to {LOGO_PATH}")


if __name__ == "__main__":
    main()
