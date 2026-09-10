"""Fallback parser for locations without a defined column spec (Mandaluyong, Pampanga).

Emits the raw OCR text line-by-line plus any money-looking tokens, so the user
still gets something structured to work from until a real spec is provided.
"""
from __future__ import annotations

import re

from ..ocr import Document
from .base import Parser

_MONEY = re.compile(r"\(?-?[\d,]+\.\d{2}\)?")


class GenericParser(Parser):
    ocr_mode = "plain"
    columns = ["Page no.", "Line", "Text", "Amounts found"]

    def __init__(self, location: str):
        self.location = location

    def parse_document(self, doc: Document) -> list[dict]:
        rows: list[dict] = []
        for page in doc.pages:
            for i, ln in enumerate(page.lines, 1):
                text = ln.text.strip()
                if not text:
                    continue
                amounts = ", ".join(_MONEY.findall(text))
                rows.append({
                    "Page no.": page.number,
                    "Line": i,
                    "Text": text,
                    "Amounts found": amounts,
                })
        return rows
