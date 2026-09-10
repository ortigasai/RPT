"""Pasig City — Office of the City Treasurer 'Statement of Account' (multi-line blocks)."""
from __future__ import annotations

import re

from ..ocr import Document
from .base import Parser, to_num

TD_RE = re.compile(r"\b[A-Z]-\d{3}-\d{4,6}\b")
_MONEY = re.compile(r"\(?-?[\d,]+\.\d{2}\)?")


class PasigParser(Parser):
    location = "Pasig City"
    ocr_mode = "plain"
    columns = [
        "Page no.", "Account No.", "Barangay", "Tax Declaration (TD) No.",
        "Owner's name", "PIN", "Bldg. Area (sq. m.)", "Assessed Value",
        "Period Covered", "Idle Land Tax", "Idle Land Tax Coverage",
        "Basic Tax", "Interest/(Discount)", "SEF Tax", "Interest/(Discount) (SEF)",
        "Amount Due",
    ]

    def parse_document(self, doc: Document) -> list[dict]:
        # flatten to (page_number, line_text)
        flat: list[tuple[int, str]] = []
        for page in doc.pages:
            for ln in page.lines:
                flat.append((page.number, ln.text))
        full = "\n".join(t for _, t in flat)

        account = None
        for i, (_, t) in enumerate(flat):
            m = re.search(r"Account\s*No\.?\s*:?\s*(\d{4,})", t)
            if m:
                account = m.group(1)
                break
            if re.search(r"\bAccount\b", t, re.I):
                for _, nxt in flat[i:i + 3]:
                    m = re.search(r"No\.?\s*:?\s*(\d{4,})", nxt)
                    if m:
                        account = m.group(1)
                        break
            if account:
                break

        # split into TD blocks
        idxs = [i for i, (_, t) in enumerate(flat) if re.search(r"\bTD\s*No\b", t, re.I)]
        rows: list[dict] = []
        for b, start in enumerate(idxs):
            end = idxs[b + 1] if b + 1 < len(idxs) else len(flat)
            block = flat[start:end]
            btext = "\n".join(t for _, t in block)
            page_no = block[0][0]

            mtd = TD_RE.search(btext)
            if not mtd:
                continue
            td = mtd.group(0)
            barangay = _grab(btext, r"Barangay\s*:?\s*([A-Za-z][A-Za-z .]+?)(?:\s{2,}|\s*OR\b|$)")
            owner = _grab(btext, r"Owner'?s?\s*Name\s*[:]?\s*([A-Z][A-Z0-9 .,&'\-]+?)\s*(?:OR\s*Date|OR\s*No|Period|$)")
            pins = re.findall(r"PIN\s*:?\s*([0-9][0-9\-]{9,})", btext)
            pin = pins[-1].strip("-") if pins else ""
            area = _grab(btext, r"Bldg\.?\s*Area\s*:?\s*([\d,]+(?:\.\d+)?)")

            # detail line: starts with the TD id (2nd occurrence)
            detail = ""
            for _, t in block:
                if t.strip().startswith(td) and _MONEY.search(t):
                    detail = t
                    break
            subtotal = next((t for _, t in block if re.search(r"SUBTOTAL", t, re.I)), "")
            disc_line = ""
            seen_sub = False
            for _, t in block:
                if seen_sub and _MONEY.search(t) and "SUBTOTAL" not in t.upper():
                    disc_line = t
                    break
                if re.search(r"SUBTOTAL", t, re.I):
                    seen_sub = True

            period = _grab(detail, r"([12]\d{3},\s*\d?\s*(?:st|nd|rd|th)?\s*-?\s*\d?\s*(?:st|nd|rd|th)?\s*Qtr\.?)")
            if not period:
                period = _grab(btext, r"Period\s*Covered\s*([12]\d{3},[^\n]+)")

            # strip the leading "2026, 1st - 4th Qtr" period out of the detail line
            detail_wo_period = re.sub(
                r"[12]\d{3},?\s*\d?\s*(?:st|nd|rd|th)?\s*-?\s*\d?\s*(?:st|nd|rd|th)?\s*Qtr\.?",
                " ", detail, flags=re.I)
            d_nums = [to_num(x) for x in _MONEY.findall(detail_wo_period)]
            s_nums = [to_num(x) for x in _MONEY.findall(subtotal)]
            disc_nums = [to_num(x) for x in _MONEY.findall(disc_line)]

            # ---- authoritative figures = the BOLD row on/under "SUBTOTAL:" -----
            #   SUBTOTAL: Basic/SEF: <BASIC> 0.00 <SEF> 0.00 <AMOUNT DUE>
            #                        (<basic disc>)      (<sef disc>)
            basic = sef = amount_due = b_disc = s_disc = None
            if len(s_nums) >= 5:
                basic, _z1, sef, _z2, amount_due = s_nums[-5:]
            elif len(s_nums) == 3:
                basic, sef, amount_due = s_nums
            if len(disc_nums) >= 2:
                b_disc, s_disc = disc_nums[0], disc_nums[1]
            elif len(disc_nums) == 1:
                b_disc = s_disc = disc_nums[0]

            # detail row (non-bold): assessed value + a cross-check on basic/SEF
            #   <TD> <KIND> <ASSESSED> <BASIC> (<bd>) <SEF> (<sd>) <AMOUNT DUE>
            assessed = _assessed_from_detail(detail_wo_period, td)
            if len(d_nums) >= 6:
                d_basic, d_bd, d_sef, d_sd, d_due = d_nums[-5:]
                if assessed is None:
                    assessed = max(d_nums[:-5]) if len(d_nums) > 6 else d_nums[0]
                # if the bold row didn't OCR, fall back to the detail row
                if basic is None:
                    basic, sef, amount_due = d_basic, d_sef, d_due
                if b_disc is None:
                    b_disc, s_disc = d_bd, d_sd
                # if the bold row ties up worse than the detail row, prefer detail
                basic, sef, amount_due, b_disc, s_disc = _reconcile(
                    (basic, sef, amount_due, b_disc, s_disc),
                    (d_basic, d_sef, d_due, d_bd, d_sd))
            if assessed is None and d_nums:
                assessed = max(d_nums)
            b_disc = -abs(b_disc) if isinstance(b_disc, (int, float)) else None
            s_disc = -abs(s_disc) if isinstance(s_disc, (int, float)) else None

            rows.append({
                "Page no.": page_no,
                "Account No.": account or "",
                "Barangay": (barangay or "").strip(),
                "Tax Declaration (TD) No.": td,
                "Owner's name": (owner or "").strip(),
                "PIN": pin,
                "Bldg. Area (sq. m.)": to_num(area) if area else "",
                "Assessed Value": assessed,
                "Period Covered": (period or "").strip(),
                "Idle Land Tax": "",
                "Idle Land Tax Coverage": "",
                "Basic Tax": basic,
                "Interest/(Discount)": b_disc,
                "SEF Tax": sef,
                "Interest/(Discount) (SEF)": s_disc,
                "Amount Due": amount_due,
            })
        # front/back copies repeat each TD; keep the copy with the most data
        best: dict[str, dict] = {}
        for r in rows:
            k = r["Tax Declaration (TD) No."]
            score = sum(1 for v in r.values() if v not in (None, ""))
            if k not in best or score > best[k]["_score"]:
                r["_score"] = score
                best[k] = r
        out = []
        for r in best.values():
            r.pop("_score", None)
            out.append(r)
        return out


def _grab(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.I)
    return m.group(1).strip() if m else None


def _pick(seq, i):
    try:
        return seq[i]
    except (IndexError, TypeError):
        return None


def _assessed_from_detail(detail: str, td: str) -> float | None:
    if not detail:
        return None
    rest = detail.split(td, 1)[-1]
    # first money-looking token after the KIND code
    m = re.search(r"[A-Za-z\-]*?(\d[\d,]*\.\d{2})", rest)
    return to_num(m.group(1)) if m else None


def _ties(basic, sef, due, bd, sd) -> float:
    """Absolute mismatch of (Basic-|bd|)+(SEF-|sd|) vs Amount Due; inf if unusable."""
    vals = [basic, sef, due, bd, sd]
    if any(not isinstance(v, (int, float)) for v in vals):
        return float("inf")
    want = (basic - abs(bd)) + (sef - abs(sd))
    return abs(want - due)


def _reconcile(bold, detail):
    """Keep the BOLD subtotal figures unless the DETAIL row reconciles better."""
    if _ties(*detail) + 0.5 < _ties(*bold):
        return detail
    return bold
