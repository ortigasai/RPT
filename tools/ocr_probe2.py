"""Dev helper 2: preprocess (grid removal / threshold) then OCR with word-box row reconstruction."""
import sys
import cv2
import numpy as np
import pymupdf
import pytesseract
from PIL import Image

pytesseract.pytesseract.tesseract_cmd = r"C:\Users\villegaskmp\AppData\Local\Tesseract-OCR\tesseract.exe"

path = sys.argv[1]
page_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1
dpi = int(sys.argv[3]) if len(sys.argv) > 3 else 400
remove_grid = (sys.argv[4] == "grid") if len(sys.argv) > 4 else False

doc = pymupdf.open(path)
page = doc[page_no - 1]
pix = page.get_pixmap(dpi=dpi)
img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if pix.n >= 3 else img.copy()

bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15)

if remove_grid:
    inv = 255 - bw
    h_k = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    v_k = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    lines = cv2.add(cv2.morphologyEx(inv, cv2.MORPH_OPEN, h_k),
                    cv2.morphologyEx(inv, cv2.MORPH_OPEN, v_k))
    lines = cv2.dilate(lines, np.ones((3, 3), np.uint8), iterations=1)
    bw = cv2.bitwise_or(bw, lines)

pil = Image.fromarray(bw)
data = pytesseract.image_to_data(pil, config="--psm 6", output_type=pytesseract.Output.DICT)

rows = {}
for i, txt in enumerate(data["text"]):
    if not txt.strip() or int(data["conf"][i]) < 30:
        continue
    y = data["top"][i]
    key = None
    for k in rows:
        if abs(k - y) < 22:
            key = k
            break
    key = key if key is not None else y
    rows.setdefault(key, []).append((data["left"][i], txt))

for k in sorted(rows):
    line = "  ".join(t for _, t in sorted(rows[k]))
    print(f"{k:5d} | {line}")
