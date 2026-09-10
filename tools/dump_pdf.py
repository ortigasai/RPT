"""Dev helper: dump text + word coordinates from a sample PDF page for parser building."""
import sys
import pdfplumber

path = sys.argv[1]
pages = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [1]
mode = sys.argv[3] if len(sys.argv) > 3 else "text"  # text | words | lines

with pdfplumber.open(path) as pdf:
    for pno in pages:
        page = pdf.pages[pno - 1]
        print(f"\n===== PAGE {pno}  (w={page.width:.0f} h={page.height:.0f}) =====")
        if mode == "text":
            print(page.extract_text(x_tolerance=1.5))
        elif mode == "words":
            for w in page.extract_words(x_tolerance=1.5, keep_blank_chars=False):
                print(f"  x0={w['x0']:7.1f} x1={w['x1']:7.1f} top={w['top']:7.1f}  {w['text']!r}")
        elif mode == "lines":
            words = page.extract_words(x_tolerance=1.5, use_text_flow=True)
            rows = {}
            for w in words:
                key = round(w["top"] / 3) * 3
                rows.setdefault(key, []).append(w)
            for key in sorted(rows):
                line = " | ".join(f"{w['text']}@{w['x0']:.0f}" for w in sorted(rows[key], key=lambda w: w["x0"]))
                print(f" top~{key:6.0f}: {line}")
