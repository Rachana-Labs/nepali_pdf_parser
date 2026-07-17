"""nepali-pdf-parser - Extract Nepali text from PDFs using font-mapping or OCR fallback."""

import logging
import os

import fitz

from .extractor import page_has_text
from .ocr import check_tesseract
from .cli import _process_pages_parallel

logger = logging.getLogger(__name__)


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
    """Parse a Nepali PDF and return extracted text.

    Each page is automatically classified:
    - Pages with selectable text using legacy Nepali fonts → font-mapping
      conversion to Unicode Devanagari.
    - Pages without text (scanned images) → Tesseract OCR fallback.

    Args:
        pdf_path: Path to the input PDF file.
        map_path: Path to custom font-mapping JSON. Defaults to bundled
            ``data/map.json``.
        ocr_lang: Tesseract language code(s). Default ``"nep"``.
        ocr_dpi: Render DPI for OCR pages. Default ``300``.
        ocr_preprocess: Image preprocessing before OCR — ``"auto"``
            (grayscale + autocontrast), ``"binarize"`` (adds Otsu threshold),
            or ``"grayscale"`` (no contrast adjustment). Default ``"auto"``.
        force_ocr: Use OCR for every page, ignoring text layers.
        force_text: Use text extraction for every page (skip OCR).
        max_workers: Number of parallel worker processes. Defaults to
            ``min(page_count, cpu_count)``.
        progress_callback: Optional ``(page_num, mode, done, total)``
            called after each page completes.

    Returns:
        Extracted text with pages separated by two newlines.

    Raises:
        FileNotFoundError: If ``pdf_path`` or ``map_path`` doesn't exist.
        ValueError: If ``force_ocr`` is set but Tesseract is unavailable.
    """
    resolved_map = _resolve_map_path(map_path)

    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    if not force_text:
        ok, err = check_tesseract(ocr_lang)
        if not ok:
            if force_ocr:
                raise ValueError(f"OCR requested but Tesseract unavailable: {err}")
            logger.warning("OCR not available (%s) — text-only extraction", err)

    doc = fitz.open(pdf_path)
    total_pages = len(doc)

    page_modes = {}
    for page_num in range(total_pages):
        page = doc[page_num]
        if force_ocr or (not force_text and not page_has_text(page)):
            page_modes[page_num] = "ocr"
        else:
            page_modes[page_num] = "text"
    doc.close()

    if not page_modes:
        return ""

    results = _process_pages_parallel(
        pdf_path=pdf_path,
        map_path=resolved_map,
        page_modes=page_modes,
        ocr_lang=ocr_lang,
        ocr_dpi=ocr_dpi,
        ocr_preprocess=ocr_preprocess,
        debug=False,
        max_workers=max_workers,
        progress_callback=progress_callback,
    )

    pages_text = [None] * total_pages
    for page_num in range(total_pages):
        text, *_ = results[page_num]
        pages_text[page_num] = text

    return "\n\n".join(pages_text)


def _resolve_map_path(map_arg: str | None) -> str:
    if map_arg:
        if not os.path.isfile(map_arg):
            raise FileNotFoundError(f"Font map file not found: {map_arg}")
        return map_arg
    bundled = os.path.join(os.path.dirname(__file__), "data", "map.json")
    if os.path.isfile(bundled):
        return bundled
    raise FileNotFoundError(
        "Bundled map.json not found — use map_path= to specify a path"
    )
