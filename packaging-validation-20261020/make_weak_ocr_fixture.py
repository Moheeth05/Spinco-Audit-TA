"""pf_weak_ocr.pdf = the picture-only fixture plus one page of pure noise, so local Tesseract reads that page
weakly/empty - the only situation in which a (disabled) hosted-OCR fallback would ever be attempted."""
import random, sys
from pathlib import Path

import fitz
from PIL import Image

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
doc = fitz.open(src)
rng = random.Random(5)
img = Image.new("L", (900, 1200))
img.putdata([rng.randint(0, 255) for _ in range(900 * 1200)])
tmp = dst.with_suffix(".noise.png")
img.save(tmp)
page = doc.new_page(width=595, height=842)
page.insert_image(page.rect, filename=str(tmp))
doc.save(dst)
tmp.unlink()
print("wrote", dst, "pages", len(doc))
