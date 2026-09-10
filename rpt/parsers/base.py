"""Shared parser plumbing: number parsing, row helpers, base class."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..ocr import Document, Line, Page, Word

MONEY_RE = re.compile(r"\(?\s*-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?\s*\)?-?")
_NUM_ONLY = re.compile(r"-?\d+(?:\.\d+)?")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def to_num(raw) -> float | None:
    """'1,234.50' -> 1234.5 ; '(38.06)' -> -38.06 ; junk/'' -> None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return None
    neg = ("(" in s and ")" in s) or s.strip().startswith("-") or s.strip().endswith("-")
    s = s.replace(",", "").replace(" ", "")
    m = _NUM_ONLY.search(s)
    if not m:
        return None
    val = float(m.group())
    if neg:
        val = -abs(val)
    return val


def money_tokens(text: str) -> list[str]:
    return [t.strip() for t in MONEY_RE.findall(text) if _NUM_ONLY.search(t or "")]


def money_after(text: str, label_regex: str) -> float | None:
    m = re.search(label_regex + r"[^0-9()\-]*(\(?-?[\d,]+(?:\.\d{1,2})?\)?)",
                  text, re.I)
    return to_num(m.group(1)) if m else None


def first_match(text: str, pattern: str, group: int = 1, flags=re.I) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else None


def norm_qtr(raw: str | None) -> str | None:
    if not raw:
        return raw
    s = raw.upper().replace(" ", "")
    s = s.replace("IST", "1ST").replace("4ST", "1ST").replace("1SF", "1ST")
    if re.fullmatch(r"1-?4|14", s):
        return "1-4"
    if re.fullmatch(r"\d[- ]?\d", raw.strip()):
        a, b = re.findall(r"\d", raw)
        return f"{a}-{b}"
    return s or raw


@dataclass
class ParseResult:
    location: str
    columns: list[str]
    rows: list[dict]
    notes: list[str] = field(default_factory=list)
    low_confidence: bool = False


class Parser:
    location: str = ""
    columns: list[str] = []          # display headers (what the user sees)
    # row-dict keys, when a header is duplicated / differs from its key.
    # Same length/order as `columns`; falls back to `columns`.
    column_keys: list[str] | None = None
    ocr_mode: str = "plain"
    dpi: int = 400
    psm: int = 6

    def keys(self) -> list[str]:
        return self.column_keys if self.column_keys else self.columns

    def parse_document(self, doc: Document) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def page_lines(page: Page) -> list[Line]:
        return page.lines

    @staticmethod
    def dedupe(rows: list[dict], keys: list[str]) -> list[dict]:
        seen: set[tuple] = set()
        out: list[dict] = []
        for r in rows:
            sig = tuple(str(r.get(k, "")).strip() for k in keys)
            if any(sig) and sig in seen:
                continue
            seen.add(sig)
            out.append(r)
        return out
