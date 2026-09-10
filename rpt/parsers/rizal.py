"""Rizal (Angono) — Provincial Treasurer's RPT receipt, Accountable Form No. 56.

Scanned pre-printed receipts with a faint printed overlay. With PaddleOCR the
overlay is mostly legible; the key rows per page are:
    <owner>  ... Tax Due  Discount  Penalty  Total
    <pin> <td> <arp>  <year>  <taxdue>  <discount>  <penalty>  <net>
    <LAND|BLDG> <location>
    BASIC:<n>  SEF:<n>  TOTAL:<n>
"""
from __future__ import annotations

import re

from ..ocr import Document
from .base import Parser, to_num

SOA_RE = re.compile(r"RZL\s*[.:]?\s*(\d{5,})", re.I)
PIN_RE = re.compile(r"\b\d{3}-\d{3}-\d{3}(?:-\d{3,4})?\b")
TD_RE = re.compile(r"\b\d{2}-[A-Z0-9]-\d{3}-\d{4,6}\b")
ARP_RE = re.compile(r"\b\d{2}-\d{4}-\d{7,}\b")
NUM_RE = re.compile(r"\d[\d.,]*\d|\d")


def _num(tok: str) -> float | None:
    """OCR often prints thousands separators as '.', e.g. '3.816.32' -> 3816.32."""
    if tok is None:
        return None
    s = tok.strip().strip(")(").replace(" ", "")
    if not s:
        return None
    s = s.replace(",", ".")
    parts = s.split(".")
    if len(parts) >= 2 and len(parts[-1]) == 2:
        s = "".join(parts[:-1]) + "." + parts[-1]
    else:
        s = "".join(parts)
    try:
        return float(s)
    except ValueError:
        return None


class RizalParser(Parser):
    location = "Rizal"
    ocr_mode = "plain"
    columns = [
        "Page no", "SOA No.", "PIN / TDN", "Kind / Class", "Assessed Value",
        "Basic", "Basic Discount", "SEF", "SEF Discount", "Total",
    ]

    def parse_document(self, doc: Document) -> list[dict]:
        rows: list[dict] = []
        for page in doc.pages:
            text = page.text
            up = text.upper()
            if "RZL" not in up and "PROVINCE OF RIZAL" not in up:
                continue
            m = SOA_RE.search(text)
            soa = m.group(1) if m else ""

            lines = [ln.text for ln in page.lines]
            data_ln = next((l for l in lines
                            if ARP_RE.search(l) and re.search(r"\b20\d{2}\b", l)), "")
            basic_ln = next((l for l in lines if re.search(r"BASIC\s*[:.]", l, re.I)), "")
            kind_ln = next((l for l in lines
                            if re.match(r"\s*(LAND|BLDG|BUILDING|MACHINE)", l, re.I)), "")

            pin = _first(PIN_RE, data_ln) or _first(PIN_RE, up)
            td = _first(TD_RE, data_ln.upper()) or _first(TD_RE, up)
            arp = _first(ARP_RE, data_ln)
            pin_tdn = " / ".join(x for x in (pin, td, arp) if x)

            kind = ""
            mk = re.match(r"\s*(LAND|BLDG|BUILDING|MACHINE\w*)", kind_ln, re.I)
            if mk:
                kind = {"BLDG": "BUILDING"}.get(mk.group(1).upper(), mk.group(1).upper())

            # data line tail after the year: taxdue  discount  penalty  net
            tail = re.split(r"\b20\d{2}\b", data_ln, maxsplit=1)
            tax_due = discount = None
            if len(tail) == 2:
                nums = [_num(t) for t in NUM_RE.findall(tail[1])]
                nums = [n for n in nums if n is not None]
                if len(nums) >= 2:
                    tax_due, discount = nums[0], nums[1]

            bnums = [_num(t) for t in NUM_RE.findall(re.sub(r"[A-Za-z:]+", " ", basic_ln))]
            bnums = [n for n in bnums if n is not None]
            basic = sef = total = None
            if len(bnums) >= 3:
                basic, sef, total = bnums[0], bnums[1], bnums[2]
            elif len(bnums) == 2:
                basic, total = bnums[0], bnums[1]
                sef = basic

            b_disc = -abs(discount) if isinstance(discount, (int, float)) else None

            # On this form Basic and SEF are always equal, and Total = Basic + SEF.
            # OCR mangles the SEF / Total tokens far more than the "BASIC:" one,
            # so derive them and repair obvious x100 / missing-decimal errors.
            if isinstance(basic, (int, float)):
                sef = basic
                if not isinstance(total, (int, float)) or total <= 0:
                    total = round(basic * 2, 2)
                elif abs(total - basic * 2) > 0.02 * basic * 2:
                    for factor in (1, 100, 0.01, 10, 0.1):
                        if abs(total * factor - basic * 2) <= 0.02 * basic * 2:
                            total = round(total * factor, 2)
                            break
                    else:
                        total = round(basic * 2, 2)

            rows.append({
                "Page no": page.number,
                "SOA No.": soa,
                "PIN / TDN": pin_tdn,
                "Kind / Class": kind,
                "Assessed Value": "",  # not printed on these receipts
                "Basic": basic,
                "Basic Discount": b_disc,
                "SEF": sef,
                "SEF Discount": b_disc,
                "Total": total,
            })
        return self.dedupe(rows, ["SOA No.", "Total"])


def _first(rx, text):
    m = rx.search(text or "")
    return m.group(0) if m else ""
