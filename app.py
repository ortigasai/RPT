"""RPT Assessment extractor — web app (Flask backend, PostgreSQL, IIS/NSSM prod).

Run:  python app.py       (then open the printed http://<your-ip>:5000 URL)
Config comes from the environment / a local .env file (see .env.example).
"""
from __future__ import annotations

import hmac
import io
import logging
import os
import pathlib
import socket
import time
import traceback
import uuid

# Load .env (dev laptop and the server both keep real config there, not in git)
try:
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).resolve().parent / ".env")
except Exception:
    pass

from flask import (Flask, Response, jsonify, render_template, request,
                   send_file, send_from_directory)

from rpt import db, ocr
from rpt.excel import build_workbook
from rpt.fetch import FetchError, fetch_pdfs
from rpt.parsers import LOCATIONS, get_parser
from rpt.review import review

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

db.init()

# --------------------------------------------------------------------------- #
# Optional shared-login gate (matches cwt-tax-portal). Enabled only when
# AUTH_USERNAME + AUTH_PASSWORD_HASH (bcrypt) are set in the environment.
# --------------------------------------------------------------------------- #
_AUTH_USER = os.environ.get("AUTH_USERNAME", "").strip()
_AUTH_HASH = os.environ.get("AUTH_PASSWORD_HASH", "").strip().encode()
_AUTH_ON = bool(_AUTH_USER and _AUTH_HASH)
_OPEN_PATHS = ("/health", "/favicon.ico")


def _check_pw(pw: str) -> bool:
    try:
        import bcrypt
        return bcrypt.checkpw(pw.encode(), _AUTH_HASH)
    except Exception:
        return False


@app.before_request
def _require_login():
    if not _AUTH_ON or request.path in _OPEN_PATHS:
        return None
    a = request.authorization
    if a and hmac.compare_digest(a.username or "", _AUTH_USER) and _check_pw(a.password or ""):
        return None
    return Response("Authentication required.", 401,
                    {"WWW-Authenticate": 'Basic realm="RPT Assessment"'})

# token -> {"bytes":..., "name":..., "ts":...}
_RESULTS: dict[str, dict] = {}
_RESULT_TTL = 3600


def _gc_results() -> None:
    now = time.time()
    for k in [k for k, v in _RESULTS.items() if now - v["ts"] > _RESULT_TTL]:
        _RESULTS.pop(k, None)


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/")
def index():
    return render_template("index.html", locations=LOCATIONS,
                           health=ocr.health())


@app.get("/favicon.ico")
def favicon():
    return ("", 204)


@app.get("/health")
def health():
    ok_db, db_msg = db.check()
    h = ocr.health()
    return jsonify(
        status="ok",
        ocr_backend=h.get("backend"),
        ocr_ready=h.get("ocr_ready"),
        database=("ok" if ok_db else "unavailable"),
        database_detail=db_msg if not ok_db else db_msg[:60],
        auth=("on" if _AUTH_ON else "off"),
    )


SAMPLE_DIR = pathlib.Path(__file__).resolve().parent


@app.get("/samples")
def samples():
    return jsonify(sorted(p.name for p in SAMPLE_DIR.glob("*.pdf")))


@app.get("/samples/<path:name>")
def sample_file(name: str):
    p = (SAMPLE_DIR / name).resolve()
    if p.suffix.lower() != ".pdf" or p.parent != SAMPLE_DIR or not p.is_file():
        return "not found", 404
    return send_file(p, mimetype="application/pdf")


@app.post("/extract")
def extract():
    _gc_results()
    location = (request.form.get("location") or "").strip()
    link = (request.form.get("link") or "").strip()
    if location not in LOCATIONS:
        return jsonify(error="Please choose a valid location."), 400

    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    sources: list[tuple[str, bytes]] = []
    try:
        if uploads:
            for f in uploads:
                data = f.read()
                if data[:5] != b"%PDF-":
                    return jsonify(error=f"{f.filename} is not a PDF."), 400
                sources.append((f.filename, data))
        elif link:
            sources = fetch_pdfs(link)
        else:
            return jsonify(error="Paste a link or upload at least one PDF."), 400
    except FetchError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:  # pragma: no cover
        return jsonify(error=f"Could not read the input: {e}"), 400

    parser = get_parser(location)
    all_rows: list[dict] = []
    notes: list[str] = []
    t0 = time.time()
    try:
        for name, data in sources:
            doc = ocr.build_document(name, data, mode=parser.ocr_mode,
                                     dpi=parser.dpi, psm=parser.psm)
            rows = parser.parse_document(doc)
            for r in rows:
                r.setdefault("Source file", name)
            all_rows.extend(rows)
            notes.extend(f"{name}: {w}" for w in doc.warnings)
            if not doc.used_ocr:
                notes.append(f"{name}: used the PDF text layer (no OCR needed).")
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    except Exception as e:  # pragma: no cover
        traceback.print_exc()
        return jsonify(error=f"Extraction failed: {e}"), 500

    took = time.time() - t0

    # --- reviewer pass (repair + flag) BEFORE producing any output ----------
    result = review(location, parser.keys(), all_rows)
    all_rows = result.rows
    for r, iss in zip(all_rows, result.issues_by_row):
        r["Review"] = "; ".join(iss)
    notes = result.summary + notes

    extra = ["Review"] + (["Source file"] if len(sources) > 1 else [])
    columns = parser.columns + extra        # display headers
    column_keys = parser.keys() + extra     # row-dict keys (== columns unless dup'd)

    if location in ("Mandaluyong City", "Pampanga"):
        notes.insert(0, "No column layout is defined for this location yet — "
                        "showing raw OCR lines. Send a sample PDF + field list "
                        "to get a proper extractor.")
    notes.append("OCR-read from scanned PDFs — verify every figure against the "
                 "source before use.")

    xlsx = build_workbook(location, columns, column_keys, all_rows, notes,
                          [n for n, _ in sources], flagged=result.rows_flagged)
    fname = f"RPT_{location.replace(' ', '_')}_{time.strftime('%Y%m%d_%H%M')}.xlsx"
    token = uuid.uuid4().hex
    _RESULTS[token] = {"bytes": xlsx, "name": fname, "ts": time.time()}

    src_label = link.strip() if link else "upload: " + ", ".join(n for n, _ in sources)
    run_id = db.record_run(
        location=location, source=src_label, files=[n for n, _ in sources],
        row_count=len(all_rows), rows_flagged=result.rows_flagged,
        seconds=round(took, 1), columns=columns, rows=all_rows, notes=notes,
        xlsx=xlsx, xlsx_name=fname,
    )

    return jsonify(
        location=location,
        columns=columns,
        column_keys=column_keys,
        rows=all_rows[:1000],
        row_count=len(all_rows),
        rows_flagged=result.rows_flagged,
        seconds=round(took, 1),
        notes=notes,
        files=[n for n, _ in sources],
        download=f"/download/{token}",
        run_id=run_id,
        saved=run_id is not None,
    )


@app.get("/runs")
def runs():
    return jsonify(runs=db.list_runs(limit=int(request.args.get("limit", 100))),
                   database=("ok" if db.enabled() else "off"))


@app.get("/runs/<int:run_id>")
def run_detail(run_id: int):
    r = db.get_run(run_id)
    return (jsonify(r), 200) if r else (jsonify(error="run not found"), 404)


@app.get("/runs/<int:run_id>.xlsx")
def run_xlsx(run_id: int):
    got = db.get_xlsx(run_id)
    if not got:
        return "Not found (no database, or run has no stored workbook).", 404
    name, data = got
    return send_file(io.BytesIO(data), as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet")


@app.get("/download/<token>")
def download(token: str):
    item = _RESULTS.get(token)
    if not item:
        return "This download has expired. Please run the extraction again.", 404
    return send_file(io.BytesIO(item["bytes"]), as_attachment=True,
                     download_name=item["name"],
                     mimetype="application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet")


if __name__ == "__main__":
    # Port/host are configurable so the same app.py runs behind IIS on the
    # server (RPT_PORT=7373, RPT_HOST=127.0.0.1) and standalone on a laptop.
    port = int(os.environ.get("RPT_PORT", os.environ.get("PORT", "5000")))
    host = os.environ.get("RPT_HOST", "0.0.0.0")
    ip = lan_ip()
    try:
        from rpt import ocr_paddle
        print("  Preparing the OCR engine (first run downloads ~15 MB)...")
        ocr_paddle.warmup()
    except Exception:
        pass
    ok_db, _ = db.check()
    print("\n" + "=" * 62)
    print("  RPT Assessment extractor is running.")
    print(f"  Local:    http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        print(f"  Network:  http://{ip}:{port}")
    print(f"  Database: {'connected' if ok_db else 'not connected (runs not saved)'}")
    print(f"  Login:    {'required' if _AUTH_ON else 'open'}")
    print("  Press CTRL+C to stop.")
    print("=" * 62 + "\n")
    app.run(host=host, port=port, debug=False, threaded=True)
