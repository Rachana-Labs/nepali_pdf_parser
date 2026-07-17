# nepali-pdf-parser

Extract clean Unicode Devanagari text from Nepali PDFs — whether they use
legacy fonts (Preeti, Gorkhapatra, etc.) or are scanned/image-only pages.

## How it works

1. **Font-based extraction** — reads selectable text via PyMuPDF, detects the
   legacy font per span, and maps glyphs to Unicode using the rules in
   `map.json`.
2. **OCR fallback** — for pages with no selectable text (scanned PDFs), renders
   the page to an image and runs Tesseract with the Nepali language pack.
3. **Mixed documents** — each page is handled independently, so a single PDF
   can have some text-extracted and some OCR'd pages, combined in order.
4. **Parallel processing** — every page (text or OCR) is processed in a
   shared pool of worker processes, so cores stay busy regardless of the
   text/OCR mix on a given document.

## Prerequisites

OCR fallback needs the **Tesseract OCR engine** installed as a system
binary, plus its **Nepali language data**. `pip install` only installs
`pytesseract`, a thin Python wrapper that shells out to a `tesseract`
executable on your system — it does not install Tesseract itself. If
Tesseract isn't found (or the `nep` language pack isn't installed), OCR
fallback pages will fail; font-based text extraction still works fine
without it.

**Debian / Ubuntu:**
```bash
sudo apt install tesseract-ocr tesseract-ocr-nep
```

**macOS (Homebrew):**
```bash
brew install tesseract tesseract-lang
```

**Windows:**
Install Tesseract via the [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki),
then download `nep.traineddata` from the
[tessdata repo](https://github.com/tesseract-ocr/tessdata) and place it in
`C:\Program Files\Tesseract-OCR\tessdata`. Make sure the Tesseract install
directory is on your `PATH`.

Verify both are working with:
```bash
tesseract --list-langs
```
`nep` should appear in the list. You can also run
`nepali-pdf-parser --list-fonts` to confirm the package itself is set up
correctly (this checks `map.json`, not Tesseract).

## Installation

### pip

```bash
pip install -e .
```

### Docker

```bash
docker build -t nepali-pdf-parser .
docker run -v $(pwd):/data nepali-pdf-parser /data/input.pdf
```

> **Note:** the Docker image is a snapshot of the source at build time. If
> you change any code under `nepali_pdf_parser/`, you must re-run
> `docker build` before those changes take effect — the `-v` volume mount
> only shares the input/output data directory, not your source files. A
> local `pip install -e .` picks up code changes immediately with no
> rebuild step.

## Usage

### Python API (recommended)

```python
from nepali_pdf_parser import parse_pdf

# Auto-detect text vs OCR per page — returns all text as a single string
text = parse_pdf("document.pdf")
print(text)

# With options
text = parse_pdf(
    "scan.pdf",
    ocr_lang="nep+eng",          # multi-language OCR
    ocr_dpi=400,                  # higher DPI for better accuracy
    force_ocr=True,               # skip text extraction, OCR everything
    progress_callback=lambda p, m, d, t: print(f"Page {p+1}: {m} ({d}/{t})"),
)
```

#### `parse_pdf` signature

```python
def parse_pdf(
    pdf_path: str,
    *,
    map_path: str | None = None,
    ocr_lang: str = "nep",
    ocr_dpi: int = 300,
    ocr_preprocess: str = "auto",
    force_ocr: bool = False,
    force_text: bool = False,
    max_workers: int | None = None,
    progress_callback=None,
) -> str:
```

| Argument | Default | Description |
|---|---|---|
| `pdf_path` | — | Path to the input PDF (required) |
| `map_path` | bundled `map.json` | Path to custom font-mapping JSON |
| `ocr_lang` | `"nep"` | Tesseract language code. Use `"nep+eng"` for mixed Nepali/English documents |
| `ocr_dpi` | `300` | Render DPI for OCR pages |
| `ocr_preprocess` | `"auto"` | Image preprocessing: `"auto"` (grayscale + autocontrast), `"binarize"` (adds Otsu threshold), or `"grayscale"` (no contrast adjustment) |
| `force_ocr` | `False` | Skip text extraction; OCR every page |
| `force_text` | `False` | Skip OCR; extract text from every page |
| `max_workers` | auto (`min(pages, CPUs)`) | Number of parallel worker processes |
| `progress_callback` | `None` | Called as `fn(page_num, mode, done, total)` after each page |

### CLI

```bash
nepali-pdf-parser INPUT.pdf [-o OUTPUT.txt] [--map map.json] \
    [--ocr-lang nep] [--ocr-dpi 300] [--ocr-preprocess auto] \
    [--workers N] [--debug] [--force-ocr] [--force-text]
```

#### CLI options

| Flag | Default | Description |
|---|---|---|
| `INPUT.pdf` | — | Path to the input PDF |
| `-o, --output` | `<INPUT>.txt` | Output text file path |
| `--map` | bundled `map.json` | Path to font mapping JSON |
| `--ocr-lang` | `nep` | Tesseract language code. For documents mixing Nepali and English (e.g. English headlines, bylines, page numbers), pass `--ocr-lang "nep+eng"` to run both language models in a single combined pass — this is not a fallback (Nepali fails → try English), it considers both dictionaries for every word at once. Any Tesseract-supported language code or `+`-joined combination works here. |
| `--ocr-dpi` | `300` | Render DPI for OCR pages |
| `--ocr-preprocess` | `auto` | Image preprocessing before OCR: `auto` (grayscale + autocontrast, no hard binarization — best default across varied/mixed-quality scans), `binarize` (adds an adaptive per-page Otsu threshold, useful for heavily degraded scans), or `grayscale` (no contrast adjustment at all) |
| `--workers` | auto (`min(CPU count, page count)`) | Number of parallel worker processes used for both OCR and text extraction/conversion |
| `--debug` | off | Save intermediate raw text and font logs to `debug/` |
| `--force-ocr` | off | Skip text extraction; OCR every page |
| `--force-text` | off | Skip OCR; extract text from every page |
| `--list-fonts` | — | Print the supported legacy font keys and exit |

## Supported legacy fonts

Run `nepali-pdf-parser --list-fonts` to see the current set — this reads
directly from `nepali_pdf_parser/data/map.json`. Add new entries to that
file to extend support.

## Acknowledgements

The font-mapping approach in this project was modified from
[npttf2utf](https://github.com/casualsnek/npttf2utf).

## Disclaimer

This tool is not perfect. Legacy font mapping can still produce occasional
character-level errors (particularly on mixed/unusual font usage or
malformed PDFs), and OCR fallback for scanned pages is inherently
imperfect and will make mistakes, especially on low-quality or degraded
scans. Always spot-check the output before relying on it, particularly for
anything where accuracy matters.

## License

GPLv3 — see [LICENSE](LICENSE). This project was originally modified from
[npttf2utf](https://github.com/casualsnek/npttf2utf) (also GPLv3), so this
project carries the same license forward.
