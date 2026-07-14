import concurrent.futures
import logging
import os
import subprocess
import sys

import fitz
from PIL import Image, ImageOps
import pytesseract

logger = logging.getLogger(__name__)


def check_tesseract(lang="nep"):
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        return False, "Tesseract is not installed or not found in PATH"

    try:
        pytesseract.image_to_string(Image.new("RGB", (1, 1)), lang=lang)
    except Exception as e:
        msg = str(e)
        if "lang" in msg.lower() and "not" in msg.lower():
            return False, f"Tesseract language pack '{lang}' is not installed"
        return False, f"Tesseract error: {msg}"

    return True, None


def _otsu_threshold(gray_img):
    """Computes a per-image binarization threshold via Otsu's method
    (maximizes between-class variance of the grayscale histogram), instead
    of using a single hard-coded cutoff for every page regardless of that
    page's actual brightness/contrast. Pure Python over a 256-bin
    histogram -- cheap regardless of image resolution since PIL computes
    the histogram itself in C."""
    histogram = gray_img.histogram()
    total = sum(histogram)
    if total == 0:
        return 128

    sum_total = sum(i * histogram[i] for i in range(256))
    sum_background = 0
    weight_background = 0
    max_variance = -1
    threshold = 128

    for i in range(256):
        weight_background += histogram[i]
        if weight_background == 0:
            continue
        weight_foreground = total - weight_background
        if weight_foreground == 0:
            break

        sum_background += i * histogram[i]
        mean_background = sum_background / weight_background
        mean_foreground = (sum_total - sum_background) / weight_foreground

        variance_between = (
            weight_background * weight_foreground
            * (mean_background - mean_foreground) ** 2
        )
        if variance_between > max_variance:
            max_variance = variance_between
            threshold = i

    return threshold


def render_page_to_image(page, dpi=300, preprocess="auto"):
    """Renders a PDF page to a PIL image for OCR.

    preprocess controls how the image is prepared before hitting Tesseract:
      - "auto" (default): grayscale + autocontrast, no hard binarization.
        Safest default across varied document types -- Tesseract's LSTM
        engine (--oem 1, which is what this pipeline uses) works directly
        on grayscale and does its own internal normalization, so this
        avoids throwing away information a fixed threshold would destroy
        on low-contrast, unevenly lit, faded, or colored-background scans.
      - "binarize": grayscale + autocontrast + adaptive per-page Otsu
        threshold. Can help on genuinely degraded scans (heavy noise,
        very low contrast) where a clean black/white image outperforms
        grayscale, but should be opted into per-document rather than
        forced globally, since it can just as easily hurt.
      - "grayscale": grayscale only, no contrast adjustment at all.
    """
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    img = img.convert("L")

    if preprocess == "grayscale":
        return img

    # autocontrast normalizes brightness/contrast per page before anything
    # else happens, which helps both "auto" and "binarize" generalize
    # across scans with different exposure, aging, or paper color.
    img = ImageOps.autocontrast(img, cutoff=1)

    if preprocess == "binarize":
        threshold = _otsu_threshold(img)
        img = img.point(lambda x: 0 if x < threshold else 255, "1")

    return img


def ocr_page(page, lang="nep", dpi=300, preprocess="auto"):
    img = render_page_to_image(page, dpi=dpi, preprocess=preprocess)
    custom_config = r'--psm 3 --oem 1 -c preserve_interword_spaces=1'
    text = pytesseract.image_to_string(img, lang=lang, config=custom_config)
    return text


# ---------------------------------------------------------------------------
# Parallel batch OCR
#
# cli.py previously OCR'd pages one at a time in its main loop, which is why
# the package was slow compared to the standalone "fast code" script -- the
# actual rendering/preprocessing/tesseract logic above is identical, the
# missing piece was parallelism across pages.
#
# fitz.Page / fitz.Document objects are not picklable, so we can't just
# hand pages to a ProcessPoolExecutor. Instead, each worker process opens
# its own fitz.Document from the pdf path exactly once (via the pool
# initializer) and reuses it for every page that worker is assigned --
# same pattern as the fast script.
# ---------------------------------------------------------------------------

_worker_doc = None


def _init_ocr_worker(pdf_path):
    global _worker_doc
    _worker_doc = fitz.open(pdf_path)


def _ocr_worker(task):
    page_num, lang, dpi = task
    global _worker_doc
    page = _worker_doc[page_num]
    text = ocr_page(page, lang=lang, dpi=dpi)
    return page_num, text


def ocr_pages_parallel(pdf_path, page_nums, lang="nep", dpi=300, max_workers=None,
                        progress_callback=None):
    """OCRs the given 0-indexed page numbers of pdf_path across multiple
    processes and returns {page_num: text}.

    progress_callback(page_num, total_done, total), if given, is called
    from the main process as each page finishes -- results can arrive out
    of order, so callers needing page order should index into the
    returned dict rather than relying on call order.
    """
    if not page_nums:
        return {}

    if max_workers is None:
        max_workers = max(1, min(len(page_nums), os.cpu_count() or 4))

    results = {}
    tasks = [(p, lang, dpi) for p in page_nums]
    total = len(tasks)
    done = 0

    with concurrent.futures.ProcessPoolExecutor(
        max_workers=max_workers,
        initializer=_init_ocr_worker,
        initargs=(pdf_path,),
    ) as executor:
        for page_num, text in executor.map(_ocr_worker, tasks):
            results[page_num] = text
            done += 1
            if progress_callback:
                progress_callback(page_num, done, total)
            else:
                logger.debug("OCR finished for page %d (%d/%d)", page_num + 1, done, total)

    return results