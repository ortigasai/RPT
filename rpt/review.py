"""Post-extraction reviewer.

Runs BEFORE the Excel is generated.  For each location it:
  * fills / repairs values that can be derived arithmetically,
  * checks internal consistency (component taxes vs. totals),
  * flags rows with missing required fields or figures that don't add up.

Returns the (possibly repaired) rows plus a per-row list of issue strings.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TOL = 0.05  # peso tolerance for "adds up" checks


def _n(v):
    return v if isinstance(v, (int, float)) else None


def _close(a, b, tol=TOL):
    a, b = _n(a), _n(b)
    return a is not None and b is not None and abs(a - b) <= tol + 0.01 * abs(b)


@dataclass
class ReviewResult:
    rows: list[dict]
    issues_by_row: list[list[str]]        # parallel to rows
    summary: list[str] = field(default_factory=list)

    @property
    def rows_flagged(self) -> int:
        return sum(1 for i in self.issues_by_row if i)


def review(location: str, columns: list[str], rows: list[dict]) -> ReviewResult:
    checker = _CHECKERS.get(location, _check_generic)
    issues: list[list[str]] = []
    for r in rows:
        issues.append(checker(r))
    flagged = sum(1 for i in issues if i)
    summary = []
    if rows:
        summary.append(f"Reviewer: {len(rows) - flagged}/{len(rows)} rows passed all "
                       f"checks; {flagged} need a look (see the 'Review' column).")
    return ReviewResult(rows, issues, summary)


# --------------------------------------------------------------------------- #
def _require(r, fields) -> list[str]:
    miss = [f for f in fields if r.get(f) in (None, "")]
    return [f"missing {', '.join(miss)}"] if miss else []


def _check_generic(r) -> list[str]:
    return []


def _check_calatagan(r) -> list[str]:
    out = _require(r, ["Declared Owner", "TD number", "Assessed Value",
                       "Principal Basic/SEF", "Total Due"])
    principal, total = _n(r.get("Principal Basic/SEF")), _n(r.get("Total Due"))
    disc = _n(r.get("Penalty/Discount"))
    if principal is not None and total is not None:
        delta = round(total - principal, 2)
        if disc is None or abs(disc - delta) > TOL:
            r["Penalty/Discount"] = delta        # repair
            if disc is not None:
                out.append(f"discount corrected {disc:.2f}->{delta:.2f} (Total-Principal)")
    assessed = _n(r.get("Assessed Value"))
    if assessed is not None and principal is not None and assessed < principal:
        out.append("assessed value looks too low (below the tax) — OCR digit lost?")
    if assessed is not None and principal is not None:
        rate = principal / assessed if assessed else 0
        if not (0.005 <= rate <= 0.06):
            out.append(f"tax rate {rate*100:.2f}% of assessed is unusual — check figures")
    return out


def _check_san_juan(r) -> list[str]:
    out = _require(r, ["PIN", "TDN", "Assessed Value", "Year", "Total"])
    basic, bdsc, penb = _n(r.get("Basic")), _n(r.get("%B DSC")), _n(r.get("Penalty"))
    sef, sdsc, pens = _n(r.get("SEF")), _n(r.get("%S DSC")), _n(r.get("Penalty (SEF)"))
    total = _n(r.get("Total"))
    if None not in (basic, bdsc, penb, sef, sdsc, pens, total):
        want = (basic - bdsc + penb) + (sef - sdsc + pens)
        if not _close(total, want):
            out.append(f"components sum to {want:,.2f} but Total is {total:,.2f}")
    yr = str(r.get("Year") or "")
    if yr and not (yr.isdigit() and 2015 <= int(yr) <= 2035):
        out.append(f"year '{yr}' out of range")
    return out


def _check_pasig(r) -> list[str]:
    out = _require(r, ["Tax Declaration (TD) No.", "Owner's name", "Basic Tax",
                       "SEF Tax", "Amount Due"])
    basic, bd = _n(r.get("Basic Tax")), _n(r.get("Interest/(Discount)"))
    sef, sd = _n(r.get("SEF Tax")), _n(r.get("Interest/(Discount) (SEF)"))
    due = _n(r.get("Amount Due"))
    # authoritative tie-up: (Basic - discount) + (SEF - discount) == Amount Due
    if None not in (basic, sef, due):
        want = basic - abs(bd or 0) + sef - abs(sd or 0)
        if not _close(due, want):
            out.append(f"does not tie up: (Basic {basic:,.2f} - {abs(bd or 0):,.2f}) "
                       f"+ (SEF {sef:,.2f} - {abs(sd or 0):,.2f}) = {want:,.2f}, "
                       f"but Amount Due = {due:,.2f}")
    if basic is not None and sef is not None and sef > 0 \
            and not _close(sef, basic / 2) and not _close(sef, basic):
        out.append(f"SEF {sef:,.2f} is neither half of nor equal to Basic "
                   f"{basic:,.2f} — check the bold row")
    assessed = _n(r.get("Assessed Value"))
    if assessed is not None and basic is not None and assessed < basic:
        out.append("assessed value below basic tax — OCR likely dropped a digit")
    return out


def _check_rizal(r) -> list[str]:
    out = _require(r, ["SOA No.", "PIN / TDN", "Basic", "Total"])
    basic, sef, total = _n(r.get("Basic")), _n(r.get("SEF")), _n(r.get("Total"))
    if basic is not None and sef is not None and abs(basic - sef) > TOL:
        out.append(f"Basic {basic:,.2f} != SEF {sef:,.2f} (should match on this form)")
    if basic is not None and total is not None and not _close(total, basic * 2):
        out.append(f"Total {total:,.2f} != 2 x Basic ({basic*2:,.2f})")
    return out


def _check_quezon(r) -> list[str]:
    out = _require(r, ["Tax Declaration No.", "Property Index No.", "Amount Due"])
    parts = {k: _n(r.get(k)) for k in ("City Share", "Barangay Share",
                                       "Special Education Fund", "Garbage Fee",
                                       "Discount", "Penalty", "SHTTC Applied",
                                       "Net Tax", "Amount Due")}
    blank = [k for k, v in parts.items() if v is None]
    if blank:
        out.append("value cell(s) not read by OCR: " + ", ".join(blank))
    net = parts["Net Tax"]
    comp = [parts[k] for k in ("City Share", "Barangay Share",
                               "Special Education Fund", "Garbage Fee", "Discount")]
    if net is not None and all(c is not None for c in comp):
        want = sum(comp)
        if not _close(net, want):
            out.append(f"shares+discount = {want:,.2f} but Net Tax is {net:,.2f}")
    due, pen, shttc = parts["Amount Due"], parts["Penalty"], parts["SHTTC Applied"]
    if None not in (due, net, pen, shttc) and not _close(due, net + pen - shttc):
        out.append(f"Net+Penalty-SHTTC != Amount Due")
    return out


_CHECKERS = {
    "Calatagan": _check_calatagan,
    "San Juan City": _check_san_juan,
    "Pasig City": _check_pasig,
    "Rizal": _check_rizal,
    "Quezon City": _check_quezon,
}
