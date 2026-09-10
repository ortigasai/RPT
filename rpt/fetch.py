"""Turn a pasted share link into a list of (filename, pdf_bytes).

Works best with a link to a SINGLE shared PDF that is shared as
"Anyone with the link". Folder links and sign-in-required links cannot be
enumerated without an org app registration, so the UI also accepts direct
file uploads as the reliable path.
"""
from __future__ import annotations

import base64
import io
import re
import zipfile
from urllib.parse import parse_qs, urlparse

import requests

UA = {"User-Agent": "Mozilla/5.0 (RPT-Assessment-Extractor)"}
TIMEOUT = 60
MAX_BYTES = 80 * 1024 * 1024


class FetchError(RuntimeError):
    pass


def _get(url: str, **kw) -> requests.Response:
    kw.setdefault("headers", UA)
    kw.setdefault("timeout", TIMEOUT)
    kw.setdefault("allow_redirects", True)
    return requests.get(url, **kw)


def _looks_like_pdf(b: bytes) -> bool:
    return b[:5] == b"%PDF-"


def _sharepoint_direct(url: str) -> str:
    """Anonymous SharePoint/OneDrive-for-Business share -> direct download URL."""
    p = urlparse(url)
    q = parse_qs(p.query)
    q.pop("web", None)
    if "download" not in q:
        sep = "&" if p.query else "?"
        return url + sep + "download=1"
    return url


def _onedrive_personal_direct(url: str) -> str:
    b64 = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://api.onedrive.com/v1.0/shares/u!{b64}/root/content"


def _gdrive_file_id(url: str) -> str | None:
    m = re.search(r"/file/d/([A-Za-z0-9_-]{20,})", url) or \
        re.search(r"[?&]id=([A-Za-z0-9_-]{20,})", url)
    return m.group(1) if m else None


def _candidates(url: str) -> list[str]:
    host = urlparse(url).netloc.lower()
    out: list[str] = []
    if "sharepoint.com" in host:
        out.append(_sharepoint_direct(url))
    elif "1drv.ms" in host or "onedrive.live.com" in host:
        out += [_onedrive_personal_direct(url), _sharepoint_direct(url)]
    elif "drive.google.com" in host:
        fid = _gdrive_file_id(url)
        if fid:
            out.append(f"https://drive.google.com/uc?export=download&id={fid}")
        out.append(url)
    elif "dropbox.com" in host:
        out.append(re.sub(r"[?&]dl=0", "", url) + ("&" if "?" in url else "?") + "dl=1")
    out.append(url)
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _filename_from(resp: requests.Response, fallback: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
    name = m.group(1) if m else fallback
    name = requests.utils.unquote(name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def fetch_pdfs(link: str) -> list[tuple[str, bytes]]:
    link = (link or "").strip()
    if not link:
        raise FetchError("No link provided.")

    # Prompters API broker (Permits & Registrations) — a library path or its URL
    from . import broker
    if broker.is_broker_ref(link):
        try:
            return broker.fetch_pdfs(link)
        except broker.BrokerError as e:
            raise FetchError(str(e))

    if not re.match(r"https?://", link, re.I):
        raise FetchError("That does not look like a web link or a Permits & Reg "
                         "path (start it with '/', e.g. /RPT Assessment/2026/...).")

    low = link.lower()
    if any(k in low for k in (":f:/", "/:f:", "folderview", "/folders/")):
        raise FetchError(
            "That looks like a link to a FOLDER. Folder links can't be opened "
            "automatically without an IT app-registration. Either share each PDF "
            "individually as 'Anyone with the link', or use the file-upload box "
            "below."
        )

    last_err = "could not download the file"
    for url in _candidates(link):
        try:
            r = _get(url, stream=True)
        except requests.RequestException as e:
            last_err = str(e)
            continue
        if r.status_code >= 400:
            last_err = f"server returned HTTP {r.status_code}"
            continue
        ctype = r.headers.get("content-type", "").lower()
        body = r.raw.read(MAX_BYTES + 1, decode_content=True) if r.raw else r.content
        if len(body) > MAX_BYTES:
            raise FetchError("File is larger than 80 MB.")

        if _looks_like_pdf(body):
            return [(_filename_from(r, "download.pdf"), body)]
        if "zip" in ctype or body[:2] == b"PK":
            try:
                zf = zipfile.ZipFile(io.BytesIO(body))
                pdfs = [(n.split("/")[-1], zf.read(n)) for n in zf.namelist()
                        if n.lower().endswith(".pdf")]
                if pdfs:
                    return pdfs
            except zipfile.BadZipFile:
                pass
        if "text/html" in ctype:
            last_err = ("the link opened a web page, not a file — it probably "
                        "needs sign-in. Download the PDF and use the upload box.")
            continue
        last_err = f"unexpected content type: {ctype or 'unknown'}"

    raise FetchError(f"Couldn't get a PDF from that link ({last_err}).")
