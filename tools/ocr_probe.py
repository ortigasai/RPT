"""Dev helper: OCR a sample PDF page and print recognized text (+ optionally word boxes)."""
import sys
import pymupdf
import pytesseract
from PIL import Image

pytesseract.pytesseract.tesseract_cmd = r"C:\Users\villegaskmp\AppData\Local\Tesseract-OCR\tesseract.exe"

path = sys.argv[1]
page_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1
dpi = int(sys.argv[3]) if len(sys.argv) > 3 else 300
psm = sys.argv[4] if len(sys.argv) > 4 else "6"

doc = pymupdf.open(path)
page = doc[page_no - 1]
pix = page.get_pixmap(dpi=dpi)
img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
img = img.convert("L")
print(f"# {path} p{page_no} dpi={dpi} psm={psm} size={img.size}")
print("-" * 70)
print(pytesseract.image_to_string(img, config=f"--psm {psm}"))
