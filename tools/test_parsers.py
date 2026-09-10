"""Run each parser against its local sample PDF and print the resulting rows."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rpt.ocr import build_document
from rpt.parsers import get_parser

SAMPLES = {
    "San Juan City": "San Juan.pdf",
    "Quezon City": "Quezon city.pdf",
    "Pasig City": "Pasig.pdf",
    "Calatagan": "Calatagan.pdf",
    "Rizal": "Rizal.pdf",
}

root = Path(__file__).resolve().parents[1]
targets = sys.argv[1:] or list(SAMPLES)

for loc in targets:
    fn = SAMPLES[loc]
    parser = get_parser(loc)
    t0 = time.time()
    doc = build_document(fn, (root / fn).read_bytes(), mode=parser.ocr_mode,
                         dpi=parser.dpi, psm=parser.psm)
    rows = parser.parse_document(doc)
    dt = time.time() - t0
    print(f"\n{'=' * 100}\n{loc}  —  {fn}  ({len(doc.pages)} pages, {dt:.1f}s, "
          f"{len(rows)} rows)  ocr={doc.used_ocr}")
    for w in doc.warnings[:5]:
        print("  ! ", w)
    print("-" * 100)
    cols = parser.columns
    print(" | ".join(c[:14] for c in cols))
    for r in rows[:40]:
        print(" | ".join(str(r.get(c, ""))[:14] for c in cols))
