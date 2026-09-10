"""Rasterise scanned tax-bill PDFs and OCR them into positioned words / lines.

All 5 known sample PDFs are image-only scans (no text layer), so OCR via Tesseract
is the primary path. If a PDF *does* carry a text layer we use it directly.
"""
from __future__ import annotations

import io
import os
import shutil
from dataclasses import dataclass, field
from functools import lru_cache

import cv2
import numpy as np
import pymupdf
import pytesseract
from PIL import Image

# --------------------------------------------------------------------------- #
# Tesseract discovery
# --------------------------------------------------------------------------- #
_CANDIDATES = [
    os.environ.get("TESSERACT_CMD", ""),
    shutil.which("tesseract") or "",
    r"C:\Users\%s\AppData\Local\Tesseract-OCR\tesseract.exe" % os.environ.get("USERNAME", ""),
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
]
for _c in _CANDIDATES:
    if _c and os.path.exists(_c):
        pytesseract.pytesseract.tesseract_cmd = _c
        break


def tesseract_available() -> bool:
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    conf: float = 100.0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Line:
    words: list[Word]
    top: float
    bottom: float

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def words_between(self, x0: float, x1: float) -> list[Word]:
        return [w for w in self.words if w.cx >= x0 and w.cx < x1]


@dataclass
class Page:
    index: int                       # 0-based
    width: float
    height: float
    words: list[Word] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    dpi: int = 400
    source_dpi_scale: float = 1.0     # multiply OCR px by this -> PDF points

    @property
    def number(self) -> int:
        return self.index + 1

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


@dataclass
class Document:
    filename: str
    pages: list[Page]
    used_ocr: bool
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Image preprocessing
# --------------------------------------------------------------------------- #
def _to_gray(pix: pymupdf.Pixmap) -> np.ndarray:
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n >= 3:
        return cv2.cvtColor(arr[:, :, :3], cv2.COLOR_RGB2GRAY)
    return arr[:, :, 0].copy()


def _deskew(gray: np.ndarray) -> np.ndarray:
    inv = cv2.bitwise_not(gray)
    thr = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thr > 0))
    if coords.shape[0] < 50:
        return gray
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = 90 + angle
    if abs(angle) < 0.3 or abs(angle) > 15:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def _remove_grid(bw: np.ndarray) -> np.ndarray:
    """Erase long horizontal / vertical rules from a bordered table."""
    inv = 255 - bw
    hor = cv2.morphologyEx(inv, cv2.MORPH_OPEN,
                           cv2.getStructuringElement(cv2.MORPH_RECT, (45, 1)))
    ver = cv2.morphologyEx(inv, cv2.MORPH_OPEN,
                           cv2.getStructuringElement(cv2.MORPH_RECT, (1, 45)))
    grid = cv2.dilate(cv2.add(hor, ver), np.ones((3, 3), np.uint8), 1)
    return cv2.bitwise_or(bw, grid)


def _remove_watermark(gray: np.ndarray) -> np.ndarray:
    """Drop a faint repeating diagonal watermark, keep darker printed glyphs.

    The QC bills carry a light-grey 'Great Green Growing' pattern; genuine text
    is markedly darker, so a hard grey-level cut isolates it fairly well.
    """
    blur = cv2.GaussianBlur(gray, (0, 0), 3)
    # keep only pixels clearly darker than local background
    norm = cv2.divide(gray, blur, scale=255)
    bw = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    bw = cv2.medianBlur(bw, 3)
    return bw


def _binarize(gray: np.ndarray, block: int = 31, c: int = 15) -> np.ndarray:
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, block, c)


def preprocess(gray: np.ndarray, mode: str) -> np.ndarray:
    if os.environ.get("RPT_DESKEW", "0") == "1":
        gray = _deskew(gray)
    if mode == "grid":
        return _remove_grid(_binarize(gray))
    if mode == "dewatermark":
        return _remove_watermark(gray)
    if mode == "hi_contrast":
        g = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        g = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(g)
        g = cv2.bilateralFilter(g, 5, 40, 40)
        return cv2.threshold(g, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    if mode == "gray":
        g = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)
    return _binarize(gray)


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #
def _ocr_lines(img: np.ndarray, psm: int, scale: float) -> tuple[list[Word], list[Line]]:
    """OCR `img` (pixel space); build lines from Tesseract's own line grouping,
    then rescale every coordinate to PDF points via `scale`."""
    cfg = f"--oem 1 --psm {psm} -c preserve_interword_spaces=1"
    data = pytesseract.image_to_data(img, config=cfg,
                                     output_type=pytesseract.Output.DICT)
    buckets: dict[tuple, list[Word]] = {}
    words: list[Word] = []
    for i, txt in enumerate(data["text"]):
        txt = txt.strip()
        if not txt:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 25:
            continue
        x = data["left"][i] * scale
        y = data["top"][i] * scale
        w = Word(txt, x, x + data["width"][i] * scale,
                 y, y + data["height"][i] * scale, conf)
        words.append(w)
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        buckets.setdefault(key, []).append(w)

    lines = _merge_close_lines(
        [Line(sorted(ws, key=lambda w: w.x0),
              min(w.top for w in ws), max(w.bottom for w in ws))
         for ws in buckets.values()]
    )
    return words, lines


def _merge_close_lines(lines: list[Line]) -> list[Line]:
    """Tesseract occasionally splits one visual row (left / right of a wide
    table) into two 'lines'; stitch lines whose vertical spans overlap heavily."""
    lines.sort(key=lambda ln: (ln.top, ln.words[0].x0 if ln.words else 0))
    merged: list[Line] = []
    for ln in lines:
        if merged:
            prev = merged[-1]
            h = min(prev.bottom - prev.top, ln.bottom - ln.top) or 1
            overlap = min(prev.bottom, ln.bottom) - max(prev.top, ln.top)
            if overlap > 0.55 * h:
                prev.words.extend(ln.words)
                prev.words.sort(key=lambda w: w.x0)
                prev.top = min(prev.top, ln.top)
                prev.bottom = max(prev.bottom, ln.bottom)
                continue
        merged.append(Line(list(ln.words), ln.top, ln.bottom))
    merged.sort(key=lambda ln: ln.top)
    return merged


def group_lines(words: list[Word], y_tol: float | None = None) -> list[Line]:
    """Geometric fallback line grouping (used for the text-layer path)."""
    if not words:
        return []
    if y_tol is None:
        heights = sorted(w.bottom - w.top for w in words)
        med_h = heights[len(heights) // 2] or 8.0
        y_tol = max(1.5, 0.6 * med_h)
    rows: list[list[Word]] = []
    mids: list[float] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        mid = (w.top + w.bottom) / 2
        best, best_d = None, y_tol
        for i, rm in enumerate(mids):
            if abs(mid - rm) <= best_d:
                best, best_d = i, abs(mid - rm)
        if best is None:
            rows.append([w])
            mids.append(mid)
        else:
            rows[best].append(w)
            mids[best] = (mids[best] * (len(rows[best]) - 1) + mid) / len(rows[best])
    out = [Line(sorted(r, key=lambda w: w.x0),
               min(w.top for w in r), max(w.bottom for w in r)) for r in rows]
    out.sort(key=lambda ln: ln.top)
    return out


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def has_text_layer(pdf_bytes: bytes) -> bool:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            sample = pdf.pages[: min(3, len(pdf.pages))]
            chars = sum(len(p.chars) for p in sample)
            return chars > 40 * len(sample)
    except Exception:
        return False


def _text_layer_document(filename: str, pdf_bytes: bytes) -> Document:
    import pdfplumber
    pages: list[Page] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for idx, p in enumerate(pdf.pages):
            words = [
                Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"], 100.0)
                for w in p.extract_words(x_tolerance=1.5, keep_blank_chars=False)
            ]
            page = Page(idx, p.width, p.height, words, group_lines(words), dpi=0)
            pages.append(page)
    return Document(filename, pages, used_ocr=False)


def active_backend() -> str:
    """'paddle' | 'tesseract' — env RPT_OCR_BACKEND overrides auto-detection."""
    choice = os.environ.get("RPT_OCR_BACKEND", "auto").lower()
    if choice in ("paddle", "tesseract"):
        return choice
    try:
        from . import ocr_paddle
        if ocr_paddle.paddle_available():
            return "paddle"
    except Exception:
        pass
    return "tesseract"


def build_document(filename: str, pdf_bytes: bytes, mode: str = "plain",
                   dpi: int | None = None, psm: int = 6,
                   max_pages: int = 400) -> Document:
    """Return a `Document` of positioned words/lines, using the best backend."""
    if active_backend() == "paddle":
        try:
            from . import ocr_paddle
            return ocr_paddle.build_document(
                filename, pdf_bytes, mode=mode,
                dpi=dpi or 400, psm=psm, max_pages=max_pages)
        except Exception as e:  # fall back rather than fail the request
            if not tesseract_available():
                raise
            import sys
            print(f"[ocr] PaddleOCR failed ({e}); falling back to Tesseract",
                  file=sys.stderr)
            doc = _build_document_tesseract(filename, pdf_bytes, mode=mode,
                                            dpi=dpi or 400, psm=psm,
                                            max_pages=max_pages)
            doc.warnings.insert(0, "PaddleOCR was unavailable for this run — used "
                                   "the lower-accuracy Tesseract engine instead; "
                                   "re-run to get the better result.")
            return doc
    return _build_document_tesseract(filename, pdf_bytes, mode=mode,
                                     dpi=dpi or 400, psm=psm, max_pages=max_pages)


def _build_document_tesseract(filename: str, pdf_bytes: bytes, mode: str = "plain",
                              dpi: int = 400, psm: int = 6,
                              max_pages: int = 400) -> Document:
    """Return a `Document` of positioned words/lines for `pdf_bytes`."""
    if has_text_layer(pdf_bytes):
        return _text_layer_document(filename, pdf_bytes)

    if not tesseract_available():
        raise RuntimeError(
            "This PDF is a scanned image and needs OCR, but Tesseract was not "
            "found. Install it (winget install tesseract-ocr.tesseract) or set "
            "the TESSERACT_CMD environment variable."
        )

    scale = 72.0 / dpi  # OCR pixel -> PDF point
    pages: list[Page] = []
    warnings: list[str] = []
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    for idx in range(min(len(doc), max_pages)):
        pix = doc[idx].get_pixmap(dpi=dpi)
        gray = _to_gray(pix)
        words, lines = _ocr_lines(preprocess(gray, mode), psm, scale)
        if mode != "plain" and len(words) < 12:
            words, lines = _ocr_lines(preprocess(gray, "plain"), psm, scale)
            warnings.append(f"page {idx + 1}: '{mode}' preprocessing yielded little "
                            f"text, fell back to plain")
        page = Page(idx, pix.width * scale, pix.height * scale,
                    words, lines, dpi=dpi, source_dpi_scale=scale)
        pages.append(page)
    doc.close()
    return Document(filename, pages, used_ocr=True, warnings=warnings)


@lru_cache(maxsize=1)
def health() -> dict:
    try:
        from . import ocr_paddle
        paddle = ocr_paddle.paddle_available()
    except Exception:
        paddle = False
    return {
        "backend": active_backend(),
        "paddle": paddle,
        "tesseract": tesseract_available(),
        "tesseract_cmd": getattr(pytesseract.pytesseract, "tesseract_cmd", None),
        "ocr_ready": paddle or tesseract_available(),
    }
