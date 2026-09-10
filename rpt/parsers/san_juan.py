"""San Juan City — Office of the City Treasurer delinquency bill (borderless table)."""
from __future__ import annotations

import re

from ..ocr import Document
from .base import Parser, norm_qtr, to_num

PIN_RE = re.compile(r"-?\d{2,3}-\d{2}-\d{3}-\d{3,4}")
TDN_RE = re.compile(r"\b\d{2}-\d{3}-\d{4,6}\b")
YEAR_RE = re.compile(r"^20\d{2}$")
MONEY_RE = re.compile(r"^\(?-?[\d,]+\.\d{2}\)?$")
_SKIP = re.compile(r"TOTALS|GRAND|NAME OF TAXPAYER|ADDRESS|PLEASE PAY|REPUBLIC|"
                   r"CITY OF SAN JUAN|OFFICE OF THE|Tel\b|NOTE\b|AVOID DELAY|"
                   r"Disregard|Certified|Chief Land", re.I)


class SanJuanParser(Parser):
    location = "San Juan City"
    ocr_mode = "plain"
    columns = [
        "Page Number", "PIN", "TDN", "Assessed Value", "Year", "QTR",
        "Basic", "%B DSC", "Penalty", "SEF", "%S DSC", "Penalty (SEF)", "Total",
    ]

    def parse_document(self, doc: Document) -> list[dict]:
        rows: list[dict] = []
        for page in doc.pages:
            pin = tdn = None
            for ln in page.lines:
                text = ln.text
                if _SKIP.search(text) or text.strip().upper().startswith("PIN "):
                    continue

                mp = PIN_RE.search(text)
                if mp:
                    pin = mp.group(0)
                rest = PIN_RE.sub("", text)
                mt = TDN_RE.search(rest)
                if mt:
                    tdn = mt.group(0)

                toks = [w.text for w in ln.words]
                year_idx = [i for i, t in enumerate(toks) if YEAR_RE.match(t)]
                if not year_idx:
                    continue

                for k, yi in enumerate(year_idx):
                    stop = year_idx[k + 1] if k + 1 < len(year_idx) else len(toks)
                    if yi < 1:
                        continue
                    assessed = to_num(toks[yi - 1])
                    qtr = norm_qtr(toks[yi + 1]) if yi + 1 < stop else None
                    nums = [to_num(t) for t in toks[yi + 2:stop] if MONEY_RE.match(t)]
                    # BASIC %B_DSC PenB B_TOTAL SEF %S_DSC PenS S_TOTAL G_TOTAL
                    if len(nums) < 8:
                        continue
                    basic, b_dsc, pen_b, _bt, sef, s_dsc, pen_s = nums[:7]
                    total = nums[-1]
                    rows.append({
                        "Page Number": page.number,
                        "PIN": pin or "",
                        "TDN": tdn or "",
                        "Assessed Value": assessed,
                        "Year": toks[yi],
                        "QTR": qtr,
                        "Basic": basic,
                        "%B DSC": b_dsc,
                        "Penalty": pen_b,
                        "SEF": sef,
                        "%S DSC": s_dsc,
                        "Penalty (SEF)": pen_s,
                        "Total": total,
                    })
        return rows
