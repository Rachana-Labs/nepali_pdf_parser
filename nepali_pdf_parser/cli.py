import argparse
import concurrent.futures
import logging
import os
import sys

import fitz

from .mapper import AutoresearchMapper, match_font_key
from .extractor import extract_line_text, page_has_text, extract_spans
from .ocr import ocr_page, check_tesseract

logger = logging.getLogger(__name__)


def _setup_logging(debug):
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def _find_map_path(map_arg):
    if map_arg:
        if not os.path.isfile(map_arg):
            sys.exit(f"Error: map file '{map_arg}' not found")
        return map_arg
    bundled = os.path.join(os.path.dirname(__file__), "data", "map.json")
    if os.path.isfile(bundled):
        return bundled
    sys.exit("Error: map.json not found — use --map to specify a path")


def _process_text_page(page, mapper, match_fn):
    page_text_lines = []
    unmapped_fonts = set()

    text_dict = page.get_text("rawdict")
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            nepali_text, other_text, fonts_seen, f_unmapped, nepali_words, nepali_word_fonts = extract_line_text(
                line, match_fn
            )
            unmapped_fonts.update(f_unmapped)

            if nepali_words:
                converted_words = []
                for word, font_key in zip(nepali_words, nepali_word_fonts):
                    if font_key is None:
                        continue
                    try:
                        converted = mapper.preeti_to_unicode(word, font=font_key)
                    except Exception:
                        converted = word
                    converted_words.append(converted)
                unicode_text = " ".join(converted_words)
            else:
                unicode_text = ""

            line_text = (unicode_text + " " + other_text).strip() if other_text.strip() else unicode_text
            page_text_lines.append(line_text)

        page_text_lines.append("")

    return "\n".join(page_text_lines).rstrip("\n"), unmapped_fonts


def _debug_info_for_text_page(page, match_fn):
    raw_spans = extract_spans(page)
    raw_lines = []
    font_lines = []
    for block, line, span in raw_spans:
        span_text = span.get("text", "")
        span_font = span.get("font", "unknown")
        matched = match_fn(span_font)
        font_lines.append(matched if matched else f"unknown({span_font})")
        raw_lines.append(span_text)
    return "\n".join(raw_lines), "\n".join(font_lines)


def _list_fonts(map_path):
    mapper = AutoresearchMapper(map_path)
    for key in sorted(mapper.get_known_fonts()):
        print(key)


# ---------------------------------------------------------------------------
# Unified parallel page pool
#
# Both OCR and text-extraction/conversion are CPU-bound work with no shared
# mutable state between pages, and neither benefits from threads (tesseract
# calls hold the GIL for large stretches; the glyph-reconstruction + mapper
# conversion code is pure Python and is fully GIL-bound). Both need a
# process pool -- so instead of running two separate pools back to back
# (which leaves cores idle whenever one page type outnumbers the other),
# every page, OCR or text, is submitted to a single shared pool. Each
# worker opens the PDF and the mapper exactly once via the pool
# initializer and reuses both for every page it's assigned.
# ---------------------------------------------------------------------------

_worker_doc = None
_worker_mapper = None
_worker_known_fonts = None


def _init_worker(pdf_path, map_path, debug):
    global _worker_doc, _worker_mapper, _worker_known_fonts
    _worker_doc = fitz.open(pdf_path)
    _worker_mapper = AutoresearchMapper(map_path)
    _worker_known_fonts = _worker_mapper.get_known_fonts()
    _setup_logging(debug)


def _page_task(task):
    page_num, mode, ocr_lang, ocr_dpi, ocr_preprocess, debug = task
    global _worker_doc, _worker_mapper, _worker_known_fonts
    page = _worker_doc[page_num]

    if mode == "ocr":
        text = ocr_page(page, lang=ocr_lang, dpi=ocr_dpi, preprocess=ocr_preprocess)
        unmapped = set()
        debug_raw, debug_fonts = (text, "ocr") if debug else (None, None)
        return page_num, mode, text, unmapped, debug_raw, debug_fonts

    def match_fn(font_name):
        return match_font_key(font_name, _worker_known_fonts)

    text, unmapped = _process_text_page(page, _worker_mapper, match_fn)
    debug_raw = debug_fonts = None
    if debug:
        debug_raw, debug_fonts = _debug_info_for_text_page(page, match_fn)
    return page_num, mode, text, unmapped, debug_raw, debug_fonts


def _process_pages_parallel(pdf_path, map_path, page_modes, ocr_lang, ocr_dpi,
                             ocr_preprocess, debug, max_workers, progress_callback=None):
    """page_modes: dict {page_num: "ocr" | "text"}. Returns
    {page_num: (text, unmapped_fonts, debug_raw, debug_fonts)}."""
    if not page_modes:
        return {}

    if max_workers is None:
        max_workers = max(1, min(len(page_modes), os.cpu_count() or 4))

    tasks = [
        (page_num, mode, ocr_lang, ocr_dpi, ocr_preprocess, debug)
        for page_num, mode in page_modes.items()
    ]
    total = len(tasks)
    done = 0
    results = {}

    with concurrent.futures.ProcessPoolExecutor(
        max_workers=max_workers,
        initializer=_init_worker,
        initargs=(pdf_path, map_path, debug),
    ) as executor:
        for page_num, mode, text, unmapped, debug_raw, debug_fonts in executor.map(_page_task, tasks):
            results[page_num] = (text, unmapped, debug_raw, debug_fonts)
            done += 1
            if progress_callback:
                progress_callback(page_num, mode, done, total)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Extract Nepali text from PDFs using font-mapping or OCR fallback."
    )
    parser.add_argument("input", nargs="?", help="Path to the input PDF file")
    parser.add_argument("-o", "--output", help="Output path (default: <input_stem>.txt)")
    parser.add_argument("--map", help="Path to font mapping JSON (default: bundled map.json)")
    parser.add_argument("--ocr-lang", default="nep", help="Tesseract language code (default: nep)")
    parser.add_argument("--ocr-dpi", type=int, default=300, help="Render DPI for OCR pages (default: 300)")
    parser.add_argument("--ocr-preprocess", choices=["auto", "binarize", "grayscale"], default="auto",
                         help="Image preprocessing before OCR (default: auto = grayscale + autocontrast, "
                              "no hard binarization -- best for varied/mixed-quality documents; "
                              "'binarize' adds an adaptive per-page threshold, useful for heavily "
                              "degraded scans; 'grayscale' skips all contrast adjustment)")
    parser.add_argument("--workers", type=int, default=None,
                         help="Number of parallel worker processes for OCR + text extraction "
                              "(default: min(CPU count, page count))")
    parser.add_argument("--debug", action="store_true", help="Write intermediate files to debug/ subfolder")
    parser.add_argument("--force-ocr", action="store_true", help="Use OCR for all pages")
    parser.add_argument("--force-text", action="store_true", help="Use text extraction for all pages")
    parser.add_argument("--list-fonts", action="store_true", help="List supported legacy fonts and exit")
    args = parser.parse_args()

    _setup_logging(args.debug)

    map_path = _find_map_path(args.map)

    if args.list_fonts:
        _list_fonts(map_path)
        return

    if not args.input:
        sys.exit("Error: INPUT.pdf is required")

    if not os.path.isfile(args.input):
        sys.exit(f"Error: input file '{args.input}' not found")

    if not args.force_text:
        ok, err = check_tesseract(args.ocr_lang)
        if not ok:
            if args.force_ocr:
                sys.exit(f"Error: {err}")
            logger.warning("OCR not available (%s) — will use text-only extraction", err)

    try:
        doc = fitz.open(args.input)
    except Exception as e:
        sys.exit(f"Error: failed to open PDF '{args.input}': {e}")

    output_path = args.output or os.path.splitext(args.input)[0] + ".txt"

    if args.debug:
        debug_dir = os.path.join(os.path.dirname(output_path) or ".", "debug")
        os.makedirs(debug_dir, exist_ok=True)
        base_stem = os.path.splitext(os.path.basename(args.input))[0]
        raw_path = os.path.join(debug_dir, base_stem + "_raw.txt")
        fonts_path = os.path.join(debug_dir, base_stem + "_fonts.txt")

    total_pages = len(doc)

    # --- Phase 1: classify every page as OCR or text-extraction. ---
    # page_has_text() only checks for an embedded text layer -- it doesn't
    # run OCR or do any conversion, so this pass is cheap and stays serial.
    page_modes = {}
    for page_num in range(total_pages):
        page = doc[page_num]
        if args.force_ocr or (not args.force_text and not page_has_text(page)):
            page_modes[page_num] = "ocr"
        else:
            page_modes[page_num] = "text"

    doc.close()  # workers each open their own handle; main process doesn't need it anymore

    ocr_count = sum(1 for m in page_modes.values() if m == "ocr")
    text_count = total_pages - ocr_count

    # --- Phase 2: process every page (OCR + text extraction/conversion)
    # through one shared pool, so cores stay busy regardless of the mix. ---
    print(f"Processing {total_pages} page(s) — {text_count} via text extraction, "
          f"{ocr_count} via OCR — with {args.workers or 'auto'} worker process(es)...")

    def _progress(page_num, mode, done, total):
        print(f"✓ Page {page_num + 1} done via {mode} ({done}/{total})")

    results = _process_pages_parallel(
        args.input,
        map_path,
        page_modes,
        ocr_lang=args.ocr_lang,
        ocr_dpi=args.ocr_dpi,
        ocr_preprocess=args.ocr_preprocess,
        debug=args.debug,
        max_workers=args.workers,
        progress_callback=_progress,
    )

    # --- Phase 3: assemble output in page order. ---
    all_pages_text = [None] * total_pages
    all_unmapped = set()

    if args.debug:
        raw_file = open(raw_path, "w", encoding="utf-8")
        fonts_file = open(fonts_path, "w", encoding="utf-8")

    for page_num in range(total_pages):
        text, unmapped, debug_raw, debug_fonts = results[page_num]
        all_pages_text[page_num] = text
        all_unmapped.update(unmapped)

        if args.debug:
            raw_file.write((debug_raw or "") + "\n\n")
            fonts_file.write((debug_fonts or "") + "\n\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(all_pages_text))

    print(f"Output written to {output_path}")

    pages_info = f"Pages processed: {total_pages} total — {text_count} via text extraction"
    if ocr_count:
        pages_info += f", {ocr_count} via OCR fallback"
    print(pages_info)

    if all_unmapped:
        names = ", ".join(sorted(all_unmapped))
        print(f"Unmapped fonts encountered: {names} — passed through unconverted")

    if args.debug:
        raw_file.close()
        fonts_file.close()
        print(f"Debug files written to {debug_dir}/")


if __name__ == "__main__":
    main()