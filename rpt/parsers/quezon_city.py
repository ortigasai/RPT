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

# OCR variants of the right-column labels seen on the samples. "SHA[RF]E"
# tolerates the same R<->F confusion Barangay's pattern already allows for
# (seen e.g. as "CITYSHAFE").
LBL = {
    "City Share": r"C[1I]TY\s*[- ]?\s*SHA[RF]E",
    "Barangay Share": r"BA[RF]ANGAY\s*SHA[RF]E",
    "Special Education Fund": r"SPEC[1I]AL\s*EDUC\w*",
    "Garbage Fee": r"GA[RF]BAGE\s*FEE",
    "Discount": r"D[1I]SCOUNT",
    "Penalty": r"PENA[LI][ JT]?Y|PENAL",
    "SHTTC Applied": r"SH\w?T+C\s*AP[PF]L[1I]ED|SHTTC",
    "Net Tax": r"NET\s*TAX",
    "Amount Due": r"A\w?M\w?O\w?[UJ]NT\s*DUE",
    "Total": r"TOTAL\s*=",
}

# SHTTC Applied's value is almost always glued directly onto its own label
# with no separating whitespace (e.g. "APPLIEDO.OO", "APPLIED(O.OO"), so the
# normal word-boundary money search never isolates a standalone token for it.
# Pull it straight out of the line text instead. Tolerates the same P<->F
# OCR confusion seen on "APFLIED", and D<->Q on "APPLIEQ".
SHTTC_VAL_RE = re.compile(
    r"AP[PF]L[1I]E[DQ]\s*\(?\s*([0-9OoDdQq]{1,3}(?:,[0-9OoDdQq]{3})*\.[0-9OoDdQq]{2})", re.I)


_OD_ZERO = re.compile(r"\(?-?[0-9OoDdQq]{1,3}(?:,[0-9OoDdQq]{3})*\.[0-9OoDdQq]{2}\)?")


def _norm_money(tok: str) -> str:
    tok = tok.replace("{", "(").replace("}", ")").replace(":", ".").replace(" ", "")
    # OCR often reads a '0' as 'O', 'D' or 'Q' inside an otherwise
    # money-shaped token (e.g. "D.00", "{0.0D}", "O.OQ") — safe to fix only
    # once the token already looks like a money value (has the right shape
    # around a literal dot).
    if _OD_ZERO.fullmatch(tok):
        tok = re.sub(r"[OoDdQq]", "0", tok)
    # "1.589.79" (dot as thousands sep) -> "1589.79"
    m = re.fullmatch(r"(\(?)(-?)(\d{1,3})\.(\d{3})\.(\d{2})(\)?)", tok)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}.{m.group(5)}{m.group(6)}"
    # "1,78228" (decimal point dropped before the cents) -> "1,782.28".
    # Requires an actual thousands comma so this can't misfire on an
    # unrelated 3-5 digit token (a page/reference number, say).
    m = re.fullmatch(r"(\(?-?\d{1,3}(?:,\d{3})+)(\d{2})(\)?)", tok)
    if m:
        return f"{m.group(1)}.{m.group(2)}{m.group(3)}"
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
            shttc = _shttc(page)
            net = _val(page, LBL["Net Tax"])
            due = _val(page, LBL["Amount Due"]) or _val(page, LBL["Total"])

            # Net Tax and Amount Due print the same total twice on this bill
            # (confirmed across samples with zero AND nonzero Penalty), so a
            # missing one can be recovered from the other. Only fill in a gap
            # — never override a value OCR actually read, even if the two
            # disagree: on one sample Net Tax and the Total line agreed with
            # each other against a differently-misread Amount Due, so "prefer
            # Amount Due" would have clobbered the more-corroborated figure.
            if net is None and due is not None:
                net = due
            elif due is None and net is not None:
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
    """Money value for a label. The QC bill's rows are packed only ~10-16px
    apart, so a wide y-tolerance around the label line can pick up the
    adjacent row's value instead (e.g. City Share grabbing Tax(Advance)'s
    amount, or Discount grabbing Net Tax's). Prefer the money token that sits
    on the label's OWN line — true for nearly every row here — and only fall
    back to a very close neighbouring line when the label's line has none."""
    rx = re.compile(label_rx, re.I)
    right_x = page.width * 0.55
    lh = _line_h(page)

    def money_on(ln) -> str | None:
        best = None
        for w in ln.words:
            tok = _norm_money(w.text)
            if PCT.match(tok) or not MONEY.fullmatch(tok) or w.x0 < right_x:
                continue
            if best is None or w.x0 > best[0]:
                best = (w.x0, tok)
        return best[1] if best else None

    for ln in page.lines:
        if not rx.search(ln.text):
            continue
        lw = next((w for w in ln.words if rx.search(w.text)), ln.words[0] if ln.words else None)
        if not lw or lw.x0 >= page.width * 0.6:
            continue
        tok = money_on(ln)
        if tok is not None:
            return to_num(tok)
        label_y = (ln.top + ln.bottom) / 2
        best = None
        for ln2 in page.lines:
            if ln2 is ln:
                continue
            dy = abs((ln2.top + ln2.bottom) / 2 - label_y)
            if dy > 0.4 * lh:
                continue
            tok2 = money_on(ln2)
            if tok2 is None:
                continue
            if best is None or dy < best[0]:
                best = (dy, tok2)
        if best:
            return to_num(best[1])
    return None


def _shttc(page: Page) -> float | None:
    for ln in page.lines:
        if not re.search(LBL["SHTTC Applied"], ln.text, re.I):
            continue
        m = SHTTC_VAL_RE.search(ln.text)
        if m:
            return to_num(_norm_money(m.group(1)))
    return None


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
    """Name + postal address, concatenated across every line of the box (the
    box is literally labelled "Name and Postal Address of Owner" and the
    Taxpayer Name column is expected to hold that whole block). The label's
    own line is skipped — its right-hand cell is the Tax(Advance) row, not
    part of the name, but a 0.5*width cutoff let that value bleed in (e.g.
    "TAX(Advance" was returned as the owner). The cutoff is now set below the
    right-column label start (~x0 316-320 on this form)."""
    left_x = page.width * 0.45
    for i, ln in enumerate(page.lines):
        if re.search(r"Name and Postal|Postal Address of Own", ln.text, re.I):
            parts = []
            for nxt in page.lines[i + 1:i + 5]:
                if re.search(r"Location of Property|Lot\s*&\s*Block", nxt.text, re.I):
                    break
                cand = " ".join(w.text for w in nxt.words if w.x0 < left_x).strip()
                if not cand:
                    continue
                if re.search(r"LOCATION|LOT ?&|BLOCK|VERIFIED", cand, re.I):
                    break
                parts.append(cand)
            joined = " ".join(parts).strip()
            if len(joined) >= 5 and re.search(r"[A-Z]{3}", joined):
                return joined
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
