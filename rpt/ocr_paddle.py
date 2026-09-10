"""PaddleOCR backend — a deep-learning OCR engine that runs fully offline.

Model weights download once (to ~/.paddleocr) on first use; after that no
network and no API keys are involved.  Output is adapted into the same
Word / Line / Page / Document structures the Tesseract backend produces, so
the per-location parsers don't care which engine ran.
"""
from __future__ import annotations

import os
import threading
from functools import lru_cache

import cv2
import numpy as np
import pymupdf

from .ocr import (Document, Line, Page, Word, _merge_close_lines, _to_gray,
                  group_lines, has_text_layer)


def paddle_available() -> bool:
    try:
        import paddleocr  # noqa: F401
        return True
    except Exception:
        return False


# PaddlePaddle's CPU predictor is not safe to run from two threads at once
# (Flask serves each request on its own thread), so every call is serialised.
_LOCK = threading.RLock()


def _model_dirs() -> dict:
    """When RPT_PADDLE_MODELS points at a folder holding det/rec/cls subdirs,
    use those explicitly. Lets the Windows service use bundled models instead
    of relying on ~/.paddleocr (which is unwritable for a LocalSystem service)."""
    base = os.environ.get("RPT_PADDLE_MODELS", "").strip()
    if not base or not os.path.isdir(base):
        return {}
    out = {}
    for key, sub in (("det_model_dir", "det"), ("rec_model_dir", "rec"),
                     ("cls_model_dir", "cls")):
        p = os.path.join(base, sub)
        if os.path.isdir(p):
            out[key] = p
    return out


@lru_cache(maxsize=1)
def _engine():
    from paddleocr import PaddleOCR
    md = _model_dirs()
    for kw in (
        dict(use_angle_cls=True, lang="en", show_log=False, cpu_threads=1, **md),
        dict(use_angle_cls=True, lang="en", cpu_threads=1, **md),
        dict(use_angle_cls=True, lang="en", show_log=False, **md),
        dict(use_angle_cls=True, lang="en", **md),
        dict(use_angle_cls=True, lang="en", show_log=False, cpu_threads=1),
        dict(use_angle_cls=True, lang="en", cpu_threads=1),
        dict(use_angle_cls=True, lang="en", show_log=False),
        dict(use_angle_cls=True, lang="en"),
        dict(use_textline_orientation=True, lang="en"),
        dict(lang="en"),
    ):
        try:
            return PaddleOCR(**kw)
        except TypeError:
            continue
    return PaddleOCR(lang="en")


def warmup() -> bool:
    """Build the engine now (downloads models on first run). Returns success."""
    try:
        with _LOCK:
            _engine()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# image prep (lighter than the Tesseract path — the DL model is robust)
# --------------------------------------------------------------------------- #
def _prep(gray: np.ndarray, mode: str) -> np.ndarray:
    if mode in ("dewatermark", "hi_contrast"):
        g = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        g = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(g)
        if mode == "dewatermark":
            # suppress the faint repeating script: keep only clearly dark ink
            blur = cv2.GaussianBlur(g, (0, 0), 3)
            g = cv2.divide(g, blur, scale=255)
        return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _iter_lines(raw) -> list:
    """Normalise the several shapes PaddleOCR.ocr() has returned over versions."""
    if not raw:
        return []
    page = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], list) \
        and raw[0] and isinstance(raw[0][0], (list, tuple)) else raw
    out = []
    for item in page or []:
        try:
            box, (text, conf) = item[0], item[1]
        except Exception:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((text, float(conf) * 100.0,
                    min(xs), max(xs), min(ys), max(ys)))
    return out


def _tokens(text: str, x0: float, x1: float, top: float, bot: float,
            conf: float) -> list[Word]:
    parts = text.split()
    if len(parts) <= 1:
        return [Word(text.strip(), x0, x1, top, bot, conf)] if text.strip() else []
    total = sum(len(p) for p in parts) + (len(parts) - 1)
    span = x1 - x0
    words, cur = [], 0
    for p in parts:
        wx0 = x0 + span * cur / total
        cur += len(p)
        wx1 = x0 + span * cur / total
        cur += 1
        words.append(Word(p, wx0, wx1, top, bot, conf))
    return words


def build_document(filename: str, pdf_bytes: bytes, mode: str = "plain",
                   dpi: int = 400, psm: int = 6, max_pages: int = 400) -> Document:
    if has_text_layer(pdf_bytes):
        from .ocr import _text_layer_document
        return _text_layer_document(filename, pdf_bytes)

    scale = 72.0 / dpi
    pages: list[Page] = []
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    with _LOCK:                       # one PaddleOCR run at a time, whole document
        eng = _engine()
        for idx in range(min(len(doc), max_pages)):
            pix = doc[idx].get_pixmap(dpi=dpi)
            img = _prep(_to_gray(pix), mode)
            try:
                raw = eng.ocr(img, cls=True)
            except TypeError:
                raw = eng.ocr(img)
            words: list[Word] = _page_words(raw, scale)
            lines = _merge_close_lines(group_lines(words))
            pages.append(Page(idx, pix.width * scale, pix.height * scale,
                              words, lines, dpi=dpi, source_dpi_scale=scale))
    doc.close()
    return Document(filename, pages, used_ocr=True,
                    warnings=[] if pages else ["PaddleOCR returned no text"])


def _page_words(raw, scale: float) -> list[Word]:
    words: list[Word] = []
    for text, conf, x0, x1, top, bot in _iter_lines(raw):
        words.extend(_tokens(text, x0 * scale, x1 * scale,
                             top * scale, bot * scale, conf))
    return words
