# RPT Assessment Extractor

A small web app: choose an LGU, point it at the real-property-tax PDF(s),
and get a downloadable Excel file plus an on-screen summary.

## Run it

Double-click **`Start RPT Extractor.bat`**.

The first run installs everything (needs internet once). After that it just
starts. A browser opens at `http://127.0.0.1:5000`.

The console window prints a second address like `http://192.168.1.23:5000` —
open that on any other laptop **on the same Wi-Fi** to use the app from there.
Keep the console window open while anyone is using it.

If Windows Firewall asks, allow access on **Private** networks so other
laptops can reach it.

## Using it

1. Pick the **Location**.
2. Either paste a **path / link**, or **upload** the PDF file(s).
   - **Permits & Reg path** (preferred): e.g. `/RPT Assessment/2026/Ortigas
     Properties/Pasig` — every PDF in that folder and its subfolders is pulled
     in via the API broker. A single `.pdf` path works too. The key is in
     `broker_config.json` (keep that file local — it is git-ignored).
   - A plain **"Anyone with the link"** SharePoint file link also works.
     Sign-in-required links and folder *share* links do not — upload instead.
3. Click **Extract data**. OCR runs ~2–5 s per page.
4. A reviewer pass then checks every row (do the component taxes add up to the
   total? are required fields present? is the tax rate sane?) and repairs what it
   safely can. Rows that still look wrong are **highlighted yellow**, with the
   reason in a **Review** column — on the page and in the Excel. The header shows
   how many need a look.
5. Check the highlighted rows against the source, then **Download Excel**.

## Important

The sample PDFs are **scanned images**, so the app reads them with OCR. It
uses **PaddleOCR**, a deep-learning engine that runs fully on this machine —
no API keys, no cost, and nothing is uploaded. The model files (~15 MB)
download once on the first extraction; after that it works offline. (If
PaddleOCR is missing it falls back to Tesseract.)

OCR is still not perfect — always check the numbers against the source
document. Rough reliability on the sample files:

| Location | Reads |
|---|---|
| San Juan | well; a few cramped pages scramble columns |
| Calatagan | well |
| Pasig | discounts / SEF / amount-due well; Assessed Value often off |
| Rizal | SOA no. / IDs / Basic well; SEF & Total are derived (Basic = SEF on this form) |
| Quezon City | **partial** — the diagonal watermark still hides many value cells |

Mandaluyong City and Pampanga have no defined layout yet — they return the
raw OCR text so you can still work from it. Send a sample PDF and the list
of columns you need to get a proper extractor for them.

## What's where

| Path | Purpose |
|------|---------|
| `app.py` | web server (Flask), binds `0.0.0.0:5000` |
| `rpt/ocr.py` | backend picker + Tesseract path + shared data model |
| `rpt/ocr_paddle.py` | PaddleOCR path (default when installed) |
| `rpt/parsers/` | one module per location |
| `rpt/fetch.py` | turn a share link into PDF bytes |
| `rpt/excel.py` | build the `.xlsx` |
| `templates/index.html` | the page |
| `tools/` | dev helpers (not needed to run) |

## Requirements

- Windows, Python 3.12 (installed at `%LOCALAPPDATA%\Programs\Python\Python312`)
- PaddleOCR + paddlepaddle (installed by `requirements.txt`) — the OCR engine
- Optional fallback: Tesseract OCR at `%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe`
  (or on `PATH`, or `TESSERACT_CMD`)

Force a backend with the `RPT_OCR_BACKEND` env var (`paddle` or `tesseract`);
default is auto (PaddleOCR if importable).
