"""Builds assets/sric_signer.ico (16-256 px) from the IIT Roorkee crest in the repository's fonts/IITR_Logo.png."""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "..", "..", "fonts", "IITR_Logo.png")
OUT = os.path.join(HERE, "..", "assets", "sric_signer.ico")

img = Image.open(SRC).convert("RGBA")
side = max(img.size)
square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
square.save(OUT, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"wrote {os.path.normpath(OUT)}")
