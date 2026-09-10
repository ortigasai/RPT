"""Fetch PDFs from the Prompters API broker (Permits & Registrations scope).

The user pastes a path inside the granted SharePoint library, e.g.
    /RPT Assessment/2026/Ortigas Properties/Pasig
or a single file, or the full broker URL.  Everything under a folder that is a
PDF is downloaded (subfolders included).

Config (first found wins):
  * env  RPT_BROKER_URL / RPT_BROKER_KEY / RPT_BROKER_SCOPE
  * file broker_config.json  next to app.py  -> {"endpoint","api_key","scope"}
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests

_CFG_FILE = Path(__file__).resolve().parent.parent / "broker_config.json"
TIMEOUT = 90
MAX_FILES = 300
MAX_TOTAL = 500 * 1024 * 1024


class BrokerError(RuntimeError):
    pass


def _config() -> dict | None:
    endpoint = os.environ.get("RPT_BROKER_URL", "").strip()
    key = os.environ.get("RPT_BROKER_KEY", "").strip()
    scope = os.environ.get("RPT_BROKER_SCOPE", "").strip()
    if not (endpoint and key):
        if _CFG_FILE.exists():
            try:
                d = json.loads(_CFG_FILE.read_text(encoding="utf-8"))
            except Exception as e:
                raise BrokerError(f"broker_config.json is not valid JSON: {e}")
            endpoint = endpoint or d.get("endpoint", "")
            key = key or d.get("api_key", "")
            scope = scope or d.get("scope", "")
    if not (endpoint and key):
        return None
    return {"endpoint": endpoint.rstrip("/"), "key": key, "scope": scope or "perm-reg-api"}


def configured() -> bool:
    try:
        return _config() is not None
    except BrokerError:
        return True  # present but broken — surface the error later


def is_broker_ref(link: str) -> bool:
    s = (link or "").strip()
    if not s:
        return False
    if s.lower().startswith(("broker:", "perm-reg-api:")):
        return True
    host = urlparse(s).netloc.lower()
    if host:
        cfg_host = ""
        try:
            c = _config()
            cfg_host = urlparse(c["endpoint"]).netloc.lower() if c else ""
        except BrokerError:
            pass
        return host == cfg_host or "paba.ortigasland" in host
    # no scheme, no host -> treat a plain path as a broker path when configured
    return configured() and (s.startswith("/") or "/" in s)


# --------------------------------------------------------------------------- #
def _normalise_path(link: str, cfg: dict) -> str:
    s = link.strip()
    s = re.sub(r"^(broker:|perm-reg-api:)", "", s, flags=re.I).strip()
    if re.match(r"https?://", s, re.I):
        q = parse_qs(urlparse(s).query)
        if "path" in q:
            return unquote(q["path"][0])
        m = re.search(r"/scopes/[^/]+/(?:items|content|metadata)/?(.*)$", urlparse(s).path)
        s = unquote(m.group(1)) if m else "/"
    if not s.startswith("/"):
        s = "/" + s
    return re.sub(r"/{2,}", "/", s)


class _Client:
    def __init__(self, cfg: dict):
        self.base = cfg["endpoint"]
        self.scope = cfg["scope"]
        self.h = {"X-API-Key": cfg["key"], "User-Agent": "RPT-Assessment-Extractor"}

    def _get(self, kind: str, path: str, **kw):
        url = f"{self.base}/scopes/{self.scope}/{kind}/"
        return requests.get(url, headers=self.h, params={"path": path},
                            timeout=TIMEOUT, **kw)

    def metadata(self, path: str) -> dict:
        r = self._get("metadata", path)
        _raise_for(r, path)
        return r.json()

    def listing(self, path: str) -> list[dict]:
        r = self._get("items", path)
        _raise_for(r, path)
        return r.json().get("items", [])

    def download(self, path: str) -> bytes:
        r = self._get("content", path, stream=True)
        _raise_for(r, path)
        return r.content


def _raise_for(r: requests.Response, path: str) -> None:
    if r.status_code == 200:
        return
    try:
        msg = r.json().get("error", {}).get("message") or r.text[:200]
    except Exception:
        msg = r.text[:200]
    if r.status_code in (401, 403):
        raise BrokerError(f"the API key was rejected ({r.status_code}). "
                          f"Check broker_config.json. [{msg}]")
    if r.status_code == 404:
        raise BrokerError(f"path not found in the library: {path}")
    raise BrokerError(f"broker returned HTTP {r.status_code} for {path}: {msg}")


def _is_pdf(item: dict) -> bool:
    return (item.get("mime_type") == "application/pdf"
            or str(item.get("name", "")).lower().endswith(".pdf"))


def fetch_pdfs(link: str) -> list[tuple[str, bytes]]:
    cfg = _config()
    if cfg is None:
        raise BrokerError(
            "No API broker is configured. Add broker_config.json next to app.py "
            'with {"endpoint": "...", "api_key": "...", "scope": "perm-reg-api"}.')

    path = _normalise_path(link, cfg)
    client = _Client(cfg)
    meta = client.metadata(path)

    targets: list[str] = []
    if meta.get("type") == "file":
        if not _is_pdf(meta):
            raise BrokerError(f"that path is not a PDF: {path}")
        targets.append(path)
    else:
        stack = [path]
        while stack and len(targets) < MAX_FILES:
            cur = stack.pop()
            for it in client.listing(cur):
                if it.get("type") == "folder":
                    stack.append(it["path"])
                elif _is_pdf(it):
                    targets.append(it["path"])
        if not targets:
            raise BrokerError(f"no PDF files found under {path}")

    out: list[tuple[str, bytes]] = []
    total = 0
    for p in sorted(targets):
        data = client.download(p)
        if data[:5] != b"%PDF-":
            continue
        total += len(data)
        if total > MAX_TOTAL:
            raise BrokerError("selected folder holds more than 500 MB of PDFs — "
                              "point at a narrower subfolder.")
        out.append((p.split("/")[-1], data))
    if not out:
        raise BrokerError(f"downloaded 0 readable PDFs from {path}")
    return out
