"""Quezon City — Real Property Tax Bill (one bill per page).

Layout (confirmed against the sample):
  top row .............. Tax Declaration No. | Property Index No. | Tax Year | Installment No.
  owner box ............ first ALL-CAPS line under "Name and Postal Address of Owner"
  assessed box ......... number in the LEFT column under "BUILDING/LAND/MACHINERY"
  right money column ... CITY SHARE / BARANGAY SHARE / SPECIAL EDUCATIONAL / IDLE LAND TAX /
                         GARBAGE FEE / DISCOUNT / NET TAX / PENALTY / AMOUNT DUE  and, near
                         the bottom, "*SHTTC APPLIED (n)" and "TOTAL = n"

A heavy diagonal "Great Green Growing" watermark sits over that money column, so
some values (often CITY SHARE, GARBAGE FEE) don't OCR — those are left blank and
the reviewer flags them.
"""
from __future__ import annotations

import re

from ..ocr import Document, Page
from .base import Parser, to_num

TDN_RE = re.compile(r"\bG[-\s]?0?\d{2,3}[-\s]?\d{3,6}\b")
PIN_RE = re.compile(r"\b1[13][-/]\d{3}[-/]\d{3}[-/]\d{3}[-/]\d[-/]\d{3}[-/]\d{3}\b")
MONEY = re.compile(r"[({]?-?\d{1,3}(?:,\d{3})*\.\d{2}[)}]?|[({]?-?\d+\.\d{2}[)}]?")
PCT = re.compile(r"^[({]?-?\d{1,3}\s*%[)}]?$")

# OCR variants of the right-column labels seen on the samples
LBL = {
    "City Share": r"C[1I]TY\s*[- ]?\s*SHARE",
    "Barangay Share": r"BA[RF]ANGAY\s*SHARE",
    "Special Education Fund": r"SPEC[1I]AL\s*EDUC\w*",
    "Garbage Fee": r"GA[RF]BAGE\s*FEE",
    "Discount": r"D[1I]SCOUNT",
    "Penalty": r"PENA[LI][ JT]?Y|PENAL",
    "SHTTC Applied": r"SH\w?T+C\s*APPL[1I]ED|SHTTC",
    "Net Tax": r"NET\s*TAX",
    "Amount Due": r"A\w?M\w?O\w?[UJ]NT\s*DUE",
    "Total": r"TOTAL\s*=",
}


def _norm_money(tok: str) -> str:
    tok = tok.replace("{", "(").replace("}", ")").replace(":", ".").replace(" ", "")
    # "1.589.79" (dot as thousands sep) -> "1589.79"
    m = re.fullmatch(r"(\(?)(-?)(\d{1,3})\.(\d{3})\.(\d{2})(\)?)", tok)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}.{m.group(5)}{m.group(6)}"
    return tok


class QuezonCityParser(Parser):
    location = "Quezon City"
    ocr_mode = "plain"
    columns = [
        "Page no.", "Tax Declaration No.", "Taxpayer Name", "Property Index No.",
        "Tax Year", "Installment No", "Assessed Value", "City Share",
        "Barangay Share", "Special Education Fund", "Garbage Fee", "Discount",
        "Penalty", "SHTTC Applied", "Net Tax", "Amount Due",
    ]

    def parse_document(self, doc: Document) -> list[dict]:
        rows: list[dict] = []
        for page in doc.pages:
            up = page.text.upper()
            if "REAL PROPERTY TAX BILL" not in up and "CITY TREASURER" not in up:
                continue
            t = page.text.replace(" ", "")

            tdn = _fix_tdn(_search(TDN_RE, page.text) or _search(TDN_RE, t))
            pin = _search(PIN_RE, page.text) or _search(PIN_RE, t)
            pin = pin.replace("/", "-") if pin else ""

            year = _grp(page.text, r"\b(20[23]\d)\b")
            inst = _grp(page.text, r"\b([1-4]\s*-\s*4)\b")
            if not inst and re.search(r"\bFULL\b", page.text, re.I):
                inst = "FULL"
            inst = (inst or "").replace(" ", "")

            city = _val(page, LBL["City Share"])
            brgy = _val(page, LBL["Barangay Share"])
            sef = _val(page, LBL["Special Education Fund"])
            garbage = _val(page, LBL["Garbage Fee"])
            discount = _neg(_val(page, LBL["Discount"]))
            penalty = _val(page, LBL["Penalty"])
            shttc = _val(page, LBL["SHTTC Applied"])
            net = _val(page, LBL["Net Tax"])
            due = _val(page, LBL["Amount Due"]) or _val(page, LBL["Total"])

            # Net Tax OCR's very badly under the watermark; when it's missing or
            # clearly garbled and Penalty / SHTTC are ~0, it equals Amount Due.
            if due is not None and _bad(net, due) and _z(penalty) and _z(shttc):
                net = due
            if net is not None and due is None and _z(penalty) and _z(shttc):
                due = net

            rows.append({
                "Page no.": page.number,
                "Tax Declaration No.": tdn,
                "Taxpayer Name": _owner(page),
                "Property Index No.": pin,
                "Tax Year": year or "",
                "Installment No": inst,
                "Assessed Value": _assessed(page),
                "City Share": city,
                "Barangay Share": brgy,
                "Special Education Fund": sef,
                "Garbage Fee": garbage,
                "Discount": discount,
                "Penalty": penalty,
                "SHTTC Applied": shttc,
                "Net Tax": net,
                "Amount Due": due,
            })
        return rows


# -- geometry helpers ------------------------------------------------------- #
def _val(page: Page, label_rx: str) -> float | None:
    """Right-column money value on (or vertically very near) the label's line.
    The label text must itself start in the left half — avoids matching stray
    words like 'TAX' / 'TOTAL' in header rows."""
    rx = re.compile(label_rx, re.I)
    right_x = page.width * 0.55
    label_ys = []
    for ln in page.lines:
        m = rx.search(ln.text)
        if not m:
            continue
        lw = next((w for w in ln.words if rx.search(w.text)), ln.words[0] if ln.words else None)
        if lw and lw.x0 < page.width * 0.6:
            label_ys.append((ln.top + ln.bottom) / 2)
    if not label_ys:
        return None
    lh = _line_h(page)
    best = None
    for ln in page.lines:
        cy = (ln.top + ln.bottom) / 2
        if min(abs(cy - y) for y in label_ys) > 0.9 * lh:
            continue
        for w in ln.words:
            tok = _norm_money(w.text)
            if PCT.match(tok) or not MONEY.fullmatch(tok) or w.x0 < right_x:
                continue
            if best is None or w.x0 > best[0]:
                best = (w.x0, tok)
    return to_num(best[1]) if best else None


def _assessed(page: Page) -> float | None:
    lines = page.lines
    for i, ln in enumerate(lines):
        if re.search(r"\b(BUILDING|LAND|MACHINER)\w*", ln.text, re.I):
            for cand in lines[i:i + 6]:
                for w in cand.words:
                    if w.x0 > page.width * 0.45:
                        continue
                    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{4,}(?:\.\d{2})?",
                                    w.text):
                        v = to_num(w.text)
                        if v and v >= 1000:
                            return v
    return None


def _owner(page: Page) -> str:
    for i, ln in enumerate(page.lines):
        if re.search(r"Name and Postal|Postal Address of Own", ln.text, re.I):
            for nxt in page.lines[i:i + 3]:
                cand = " ".join(w.text for w in nxt.words if w.x0 < page.width * 0.5)
                cand = re.sub(r"^.*?(?:OWNER|OWNE\??)\s*\d*\s*", "", cand, flags=re.I).strip()
                if len(cand) >= 5 and re.search(r"[A-Z]{3}", cand) \
                        and not re.search(r"LOCATION|LOT ?&|BLOCK|VERIFIED", cand, re.I):
                    return cand
    m = re.search(r"\b([A-Z][A-Z0-9 .,&'\-]{6,}?(?:CORPORATION|PARTNERSHIP|"
                  r"CORP|INC|COMPANY|\(SINGLE\)|\(MARRIED\)|SPS\.?))",
                  page.text)
    return m.group(1).strip() if m else ""


def _line_h(page: Page) -> float:
    hs = sorted(ln.bottom - ln.top for ln in page.lines if ln.words)
    return hs[len(hs) // 2] if hs else 12.0


def _search(rx, text):
    m = rx.search(text)
    return m.group(0) if m else None


def _grp(text, pat):
    m = re.search(pat, text, re.I)
    return m.group(1).strip() if m else None


def _fix_tdn(s):
    if not s:
        return ""
    s = s.replace(" ", "").upper()
    m = re.search(r"G-?0?(\d{2,3})-?(\d{3,6})", s)
    return f"G-0{m.group(1)[-2:]}-{m.group(2)}" if m else s


def _neg(v):
    return -abs(v) if isinstance(v, (int, float)) else v


def _z(v):
    return v is None or (isinstance(v, (int, float)) and abs(v) < 0.005)


def _bad(net, due):
    if net is None:
        return True
    if not isinstance(net, (int, float)) or not isinstance(due, (int, float)):
        return False
    return abs(net - due) > 0.05 + 0.01 * abs(due)
