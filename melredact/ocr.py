"""OCR stage: rasterize -> OCR, producing pdfplumber-shaped words.

Real scans arrive with no text layer at all (verified against the actual
44-page file: zero chars, one image per page). segment.py's anchor-finding
and row-assignment logic was built against pdfplumber's `extract_words()`
output -- a list of {text, x0, x1, top, bottom} dicts in page-point space --
so rather than growing a second parallel code path, this module's only job
is to reproduce that same shape from a rasterized crop, sourced from OCR
instead of a native text layer. Everything downstream (segment.py, match.py)
stays unaware of which source a word came from.

Engine choice (PaddleOCR over Tesseract) was decided by measurement, not
assumption: benchmarked against the 3-student sample, which has both the
scanned images and a known-good (if itself noisy) text layer to score
against. On the load-bearing Name field, PaddleOCR scored 90.9 and 100.0
(match.py's own WRatio/token_sort_ratio scorer) against Tesseract's 34.8 and
80.0 on the same two legible header pages. Both correctly produce
near-nothing on the one illegible-scrawl page, which is fine -- that's
supposed to fall below MIN_NAME_CHARS and abstain regardless of engine.

Word-level boxes come from PaddleOCR's `return_word_box=True`, not a
line-splitting heuristic -- it already splits each detected line into real
word tokens with their own boxes. It also splits off bare punctuation as its
own token sometimes (e.g. "Name" / ": " / "Divya" / " " / "Chama"); those
carry no matching signal either way, so they're dropped at this boundary
rather than special-cased against the label vocabulary.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pdfplumber

from melredact.config import CACHE_DIR, RENDER_DPI_FINAL

Word = dict
BBox = tuple[float, float, float, float]  # (x0, top, x1, bottom) in page points

_ocr_engine = None
_file_hash_cache: dict[str, str] = {}


def _file_content_hash(path: str | Path) -> str:
    """SHA-256 of the file's actual bytes, not its path/mtime -- a scan
    copied or renamed with identical content should still hit the cache,
    and a path that's reused for genuinely different content (a rescan)
    must not collide with the old cache entry. Memoized in-process since
    segment_pdf/redact_packet each touch every page of the same file
    repeatedly within one run."""
    path = str(path)
    cached = _file_hash_cache.get(path)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    result = digest.hexdigest()[:16]
    _file_hash_cache[path] = result
    return result


def file_content_hash(path: str | Path) -> str:
    """Public wrapper around _file_content_hash -- melredact.consensus keys
    its own disk cache (consensus masks) the same way this module keys OCR
    words, off the source file's actual content, and shares this single
    hashing implementation rather than duplicating it."""
    return _file_content_hash(path)


def _stable_page_identity(pdf_path: str | Path, page_index: int) -> str:
    """Cache-directory identity for one page's OCR results -- see
    orientation.stable_ocr_identity's own docstring for the bug this
    exists to avoid: `pdf_path` here is `page.pdf.path`, which is a
    *whole-file* resave the instant any page in the file has an
    orientation override applied, so hashing it directly (this module's
    prior behavior) invalidated every page's OCR cache the moment a
    single, unrelated page's rotation changed -- confirmed the dominant
    cost of the review UI's rotate control (a cold re-OCR of the entire
    file on every click). Deferred import: orientation.py already imports
    from this module (file_content_hash), so importing it back here has to
    stay inside the function to avoid a circular import at module load."""
    from melredact.orientation import stable_ocr_identity

    return stable_ocr_identity(Path(pdf_path), page_index)


def _ocr_cache_path(pdf_path: str | Path, page_index: int, dpi: int, bbox: BBox) -> Path:
    # bbox is part of the key, not just file+page+dpi: segmentation's small
    # header/footer crops and redact_packet's full-page request are
    # genuinely different OCR calls (different rasterized image, different
    # cost), so collapsing them into one cache entry would mean either
    # always paying full-page cost up front (measured on the real 44-page
    # file: ~30s/page full-page vs. a couple seconds for a header/footer
    # crop -- segmentation alone would balloon from ~2 minutes to 20+)  or
    # inventing a second cache dimension anyway. Keying on the exact bbox
    # keeps each call's cost where it already was while still collapsing
    # every *repeat* of that same call (the actual duplication this exists
    # to fix -- see cached_ocr_words_in_region's docstring) to one.
    bbox_key = "_".join(f"{v:.1f}" for v in bbox)
    identity = _stable_page_identity(pdf_path, page_index)
    return Path(CACHE_DIR) / "ocr" / identity / f"page_{page_index:04d}_{dpi}_{bbox_key}.json"


def cached_ocr_words_in_region(page: pdfplumber.page.Page, bbox: BBox, dpi: int = RENDER_DPI_FINAL) -> list[Word]:
    """ocr_words_in_region, disk-cached and keyed on the source file's own
    content + page index + dpi + the exact bbox requested.

    This is the actual fix for "OCR runs 2-4x per page across the
    pipeline's lifetime" (see CLAUDE.md/RUNBOOK.md): segmentation's
    header/footer crops, field extraction's header crop, and review_app's
    live re-render of that same field table were each issuing their own,
    uncached OCR call against the *identical* region of the *identical*
    page -- confirmed by inspection, is_header_page and extract_header_
    fields crop the exact same (0, 0, page.width, HEADER_SEARCH_MAX_TOP)
    bbox at the same dpi. Caching at this exact call boundary collapses
    all of those into one OCR call, ever, per distinct (file, page, dpi,
    bbox) -- disk-persisted, so it survives a Streamlit server restart or
    a second `cli.py run`, not just one process's lifetime.
    """
    pdf_path = page.pdf.path
    page_index = page.page_number - 1
    cache_file = _ocr_cache_path(pdf_path, page_index, dpi, bbox)
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    words = ocr_words_in_region(page, bbox, dpi=dpi)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(words))
    return words


def _engine():
    """Lazily construct the PaddleOCR engine and reuse it. Construction
    loads model weights and takes real time; importing paddleocr at module
    scope would tax every caller of segment.py, even the (fixture-based)
    tests that never touch the OCR path at all."""
    global _ocr_engine
    if _ocr_engine is None:
        from paddleocr import PaddleOCR

        _ocr_engine = PaddleOCR(use_textline_orientation=True, lang="en")
    return _ocr_engine


def _has_signal(token: str) -> bool:
    return any(ch.isalnum() for ch in token)


def _words_from_result(res: dict[str, Any], *, origin_x: float, origin_top: float, dpi: int) -> list[Word]:
    scale = 72.0 / dpi
    words: list[Word] = []
    per_line_tokens = res.get("text_word") or []
    per_line_boxes = res.get("text_word_boxes") or []
    for tokens, boxes in zip(per_line_tokens, per_line_boxes):
        for token, box in zip(tokens, boxes):
            text = token.strip()
            if not _has_signal(text):
                continue
            px_x0, px_top, px_x1, px_bottom = (float(v) for v in box)
            words.append(
                {
                    "text": text,
                    "x0": origin_x + px_x0 * scale,
                    "x1": origin_x + px_x1 * scale,
                    "top": origin_top + px_top * scale,
                    "bottom": origin_top + px_bottom * scale,
                }
            )
    return words


def ocr_words_in_region(page: pdfplumber.page.Page, bbox: BBox, dpi: int = RENDER_DPI_FINAL) -> list[Word]:
    """OCR just the given page-point-space region, returning words in the
    same page-point coordinate space pdfplumber's extract_words() uses.
    Cropping before rasterizing (rather than rasterizing the full page and
    slicing pixels) keeps every OCR call small and fast -- this only ever
    needs to look at a header band or a footer band, never the handwritten
    body content in between."""
    x0, top, x1, bottom = bbox
    cropped = page.crop(bbox)
    image = cropped.to_image(resolution=dpi).original.convert("RGB")
    # Doc-orientation classification and dewarping are for photographed
    # pages at an angle. Ours are rasterized directly from a known page
    # geometry -- already flat and upright -- and leaving these on actively
    # breaks detection on our narrow, wide crops (e.g. the footer band came
    # out as page-width but a couple hundred px tall): confirmed by direct
    # comparison, "Page 1 of 2" lost everything but "Page" with them on,
    # recovered in full with them off.
    results = _engine().predict(
        np.array(image),
        return_word_box=True,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
    )
    words: list[Word] = []
    for res in results:
        words.extend(_words_from_result(res, origin_x=x0, origin_top=top, dpi=dpi))
    return words
