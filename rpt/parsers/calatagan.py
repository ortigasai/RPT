"""Calatagan, Batangas — Statement of Real Property Tax Liabilities (bordered table).

Layout per property:
    DECLARED OWNER: <name(s), may span several lines>
    <column header row>
    <TD> <title> <kind> <qtr> <year> <location> <assessed> <taxdue/yr> <principal> <penalty/discount> <total>
    <area> CALATAGAN
    Subtotal: <total>
"""
from __future__ import annotations

import re

from ..ocr import Document
from .base import Parser, norm_qtr, to_num

TD_RE = re.compile(r"\b\d{2}-\d{4}-\d{4,6}\b")
TITLE_RE = re.compile(r"\b\d{3}-\d{8,10}\b")
KIND_RE = re.compile(r"\bL[-/ ]?A/?R?\b|\bL[-/ ]?R\b|\bB[-/ ]?[A-Z]{1,3}\b|\bM[-/ ]?[A-Z]{1,3}\b")
OWNER_RE = re.compile(r"DECLAR\w*\s*OWNER\s*[:;]?\s*(.+)", re.I)
_MONEY = re.compile(r"\(?-?[\d,]+\.\d{2}\)?")
_HEADER = re.compile(r"\bNUMBER\b|BASIC ?/? ?SEF|DISCOUNT|KIND OF|ASSESSED|"
                     r"TAX ?DUE|PENALTY", re.I)
_SKIP = re.compile(r"SUBTOTAL|TOTAL TAX DUE|STATEMENT OF REAL|REPUBLIC OF THE|"
                   r"PROVINCE OF BATANGAS|OFFICE OF THE MUNICIPAL|MUNICIPALITY OF|"
                   r"Page \d+ ?of|Online RPT|preferential attention|Computation valid|"
                   r"Remittances|disregard this notice|Prepared by|Verified by|"
                   r"Below please find|Sir|CALDERON|AYALA ST", re.I)


class CalataganParser(Parser):
    location = "Calatagan"
    ocr_mode = "grid"
    columns = [
        "Page no", "Declared Owner", "TD number", "Title Number", "QTR", "Year",
        "Location", "Assessed Value", "Principal Basic/SEF", "Penalty/Discount",
        "Total Due",
    ]

    def parse_document(self, doc: Document) -> list[dict]:
        rows: list[dict] = []
        owner = ""
        collecting = False        # still gathering multi-line owner text
        owner_just_seen = False    # previous meaningful line was (part of) an owner
        for page in doc.pages:
            for ln in page.lines:
                text = ln.text.strip()
                if not text:
                    continue

                mo = OWNER_RE.search(text)
                if mo:
                    owner = _tidy_owner(mo.group(1))
                    collecting = True
                    owner_just_seen = True
                    continue

                is_header = bool(_HEADER.search(text)) and not TD_RE.search(text)
                if is_header:
                    if collecting or owner_just_seen:
                        collecting = False              # header ends the owner
                    else:
                        owner = ""                      # new block, owner not OCR'd
                    owner_just_seen = False
                    continue

                if collecting and not TD_RE.search(text) and not _SKIP.search(text):
                    owner = _tidy_owner(owner + " " + text)
                    owner_just_seen = True
                    continue
                collecting = False

                if _SKIP.search(text):
                    owner_just_seen = False
                    continue

                mtd = TD_RE.search(text)
                money = _MONEY.findall(text)
                if not mtd or len(money) < 3:
                    owner_just_seen = False
                    continue
                owner_just_seen = False

                td = mtd.group(0)
                mtitle = TITLE_RE.search(text)
                title = mtitle.group(0) if mtitle else ""
                mkind = KIND_RE.search(text.replace(td, "").replace(title, ""))
                kind = mkind.group(0).replace(" ", "-").replace("LA", "L-A") if mkind else ""
                myr = re.search(r"\b20\d{2}\b", text)
                year = myr.group(0) if myr else ""
                mq = re.search(r"[1I4]ST[- ]?4TH", text, re.I)
                qtr = norm_qtr(mq.group(0)) if mq else "1ST-4TH"

                loc = ""
                mloc = re.search(r"\b20\d{2}\b\s+([A-Z][A-Za-z. ]+?)[\s,.]+\(?-?[\d,]+\.\d{2}",
                                 text)
                if mloc:
                    loc = mloc.group(1).strip(" .,")
                if loc and "CALATAGAN" not in loc.upper():
                    loc = f"{loc}, CALATAGAN"
                elif not loc:
                    loc = "CALATAGAN"

                nums = [to_num(x) for x in money]
                if len(nums) >= 5:
                    assessed, principal, penalty, total = (
                        nums[-5], nums[-3], nums[-2], nums[-1])
                elif len(nums) == 4:
                    assessed, principal, penalty, total = (
                        nums[-4], nums[-3], nums[-2], nums[-1])
                else:
                    assessed, principal, total = None, nums[-2], nums[-1]
                    penalty = None

                # The "Penalty/Discount" column is structurally  Total - Principal.
                # OCR frequently drops the parentheses, so trust the arithmetic.
                if isinstance(principal, (int, float)) and isinstance(total, (int, float)):
                    delta = round(total - principal, 2)
                    if abs(delta) >= 0.01:
                        penalty = delta

                rows.append({
                    "Page no": page.number,
                    "Declared Owner": owner,
                    "TD number": td,
                    "Title Number": title,
                    "QTR": qtr,
                    "Year": year,
                    "Location": loc,
                    "Assessed Value": assessed,
                    "Principal Basic/SEF": principal,
                    "Penalty/Discount": penalty,
                    "Total Due": total,
                })
        return self.dedupe(rows, ["TD number", "Total Due"])


def _tidy_owner(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip().strip(";,. ")
    s = re.sub(r"\s*,\s*", ", ", s)
    s = re.sub(r"\s*;\s*", "; ", s)
    s = re.sub(r"\.(?=[A-Za-z])", ". ", s)
    s = re.sub(r"(\d)\s*[Il]\s*(\d)", r"\1/\2", s)                 # 4I7 -> 4/7
    s = re.sub(r"\bM(?:\.?D|/)\.?\s*TO\b", "MARRIED TO", s, flags=re.I)
    s = re.sub(r"(MARRIED TO)(?=[A-Z])", r"\1 ", s)
    s = re.sub(r"([A-Z])(?=\d/\d\s*SHARE)", r"\1 ", s, flags=re.I) # ...E1/4 -> E 1/4
    s = re.sub(r"([A-Z])-(WIDOW(?:ER)?|SINGLE|MARRIED)\b", r"\1 -\2", s)
    s = re.sub(r"([A-Za-z])(CORPORATION|CORP|INCORPORATED|INC|COMPANY|"
               r"ENTERPRISES?|REALTY|HOLDINGS?)\b", r"\1 \2", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,;.])", r"\1", s)
    return s.strip(";,. ")
