"""Page-orientation normalization: an early pipeline stage so every
downstream stage (segmentation, OCR, matching, redaction) sees an upright
page and never needs rotation logic of its own.

Motivation, from a real file: `data/PRT/010406_PD1_PRT.pdf` has two pages
(0-indexed 84, 85) rotated 180 degrees -- a real duplex-scanner artifact
(the supervisor flagged this early: "the scanner flips the document on
two-sided scans"). Redaction geometry (`redact.detect_header_band`,
`segment.locate_header_anchors`) is unconditional and page-content-blind --
on a rotated page it covers the wrong region relative to the actual ink,
which both leaves the real name exposed and stamps a SID somewhere
arbitrary. Before this module, those two pages happened to get caught only
by accident: upside-down text doesn't match "Name:"/"Page X of Y", so
segment.py's footer/header search failed and the page fell out as a
flagged orphan. That is not a real safeguard -- it depends on OCR failing
in a way that happens to look like a different, unrelated problem, and
there's no reason a 90-degree rotation (where a header's own vertical text
line can still coincidentally satisfy a narrow word search) would fail the
same way.

**Detection is content-based, never PDF `/Rotate` metadata.** Real scans
arrive from Box after passing through Google Sheets exports, Box's own
processing, and (see `pdfio.py`) at least one prior re-save through a
qpdf-incompatible tool -- any of which can drop or rewrite `/Rotate`
without touching the actual embedded pixels. Trusting it would mean
trusting a value with no guaranteed relationship to what the page actually
looks like. Detection instead renders the page exactly as it would
currently display (respecting whatever `/Rotate` happens to be set, right
or wrong) and classifies *that*.

**Detector: PaddleOCR's `DocImgOrientationClassification`, not the
full-pipeline `use_doc_orientation_classify` flag `ocr.py` already
disables.** `ocr.py`'s own OCR calls turn full-document orientation
classification off deliberately (see its docstring) because it breaks
detection on narrow header/footer crops. That constraint doesn't apply
here: this module always classifies the *whole rendered page*, and a
dedicated classification-only submodule (no text detection/recognition
pass) is both the more correct tool for "what angle is this page" and
dramatically cheaper -- measured directly, ~0.02s/page after a ~0.4s
one-time model load, versus ~9s/page for a full `PaddleOCR().predict()`
call with orientation classification enabled. Rejected: reusing the
full-pipeline `PaddleOCR(...).predict(..., use_doc_orientation_classify=
True)` (does the same job at ~450x the per-page cost, since it also runs
text detection/recognition it doesn't need to answer this question), and a
purely geometric approach (analyzing the raster for a dominant text-line
axis without a model) which has no way to distinguish the four 90-degree
turns without additional heuristics the OCR stack already solves.

**Confidence, not a coin flip: `ORIENTATION_MIN_SCORE` (config.py) is
calibrated against 176 real pages, not the abstract.** See config.py's own
comment for the real score distribution (0.91-0.93 for any page with real
content, 0.26 for a blank one) -- the gap is wide, so this is a low-risk
threshold, but it is still a threshold a genuinely ambiguous or already-
corrupted page could fall under, and a wrong guess here is exactly the
scenario CLAUDE.md's "abstain, never guess" posture exists for: guessing a
cardinal rotation wrong doesn't produce a visibly broken page, it produces
a *confidently* wrong redaction, since detect_header_band's anchor search
would then be run against the wrong axis entirely and (per the real
010406 evidence) can still occasionally find *something* to anchor to.
Below the threshold, this module applies no rotation at all and reports
the page unresolved -- see `segment.segment_pdf`'s own handling below.

**Skew (non-cardinal tilt) is measured, not corrected.** See
`estimate_skew_deg`'s own docstring and CLAUDE.md's rotation-audit section
for the real evidence: existing corner-based border/anchor detection
(`redact.detect_header_band`) is already tested, with a passing regression
test, across skew up to ~2.56 degrees (24pt of drop across the header's
own width) -- and real measured skew across all 176 real pages this
session had access to never exceeded 1.48 degrees. Building a deskew
(image-rotation) stage on top of that would add real risk (any rotation
step introduces resampling/interpolation, which a purely axis-aligned
redaction box does not need to tolerate) to fix a problem the evidence
says doesn't exist on real data. What this module *does* do with a skew
measurement is treat it as a second, independent confidence signal: a page
whose residual skew (after cardinal correction) exceeds
`ORIENTATION_MAX_TOLERATED_SKEW_DEG` is reported unresolved even if the
cardinal classification itself was confident -- a shape nothing in the
real dataset or the existing skew regression test has actually validated,
so it gets a human rather than an untested guess.

**Persistence: one normalized PDF + one JSON manifest per distinct source
file, disk-cached by content hash (`ocr.file_content_hash`, the same
hasher `pdfio.py`'s own repair cache already shares) -- so re-running the
same file never re-detects.** A file where every page is already upright
never gets a rewritten copy at all (`OrientationResult.normalized_path`
is just the input path) -- there's nothing to normalize, and building a
redundant pikepdf resave for the common case would cost real time for
zero benefit, the same reasoning `pdfio.open_pdf` already applies to an
unaffected file.

**Correction mechanism: PDF `/Rotate`, not re-rasterizing page content.**
Setting `/Rotate` is lossless (no resampling of the actual scanned image)
and cheap (a metadata write, not a re-encode) -- pdfplumber, which every
downstream module in this codebase already reads pages through, resolves
`/Rotate` into `page.width`/`page.height` and `page.to_image()` output
consistently (verified directly: rendering a `/Rotate`-tagged page
reproduces the expected upright pixels exactly, byte for byte, not just
approximately). The new value is `(current_effective_rotate -
detected_angle) % 360`, not `+` -- verified empirically against three real
mechanisms this session actually built and round-tripped, not assumed:
(1) a page whose `/Rotate` was wrongly set on already-upright content, (2)
a page whose embedded pixels were physically pre-rotated at `/Rotate=0`,
and (3) all three of 90/180/270 via the plain `PIL.Image.rotate` sign
convention `normalize_page_image` below also uses. `-` was the sign that
reconstructed the original pixels exactly in every case tested; `+` did
not.

Every module in this codebase that opens a caller-supplied source PDF
already goes through `pdfio.open_pdf` (see that module's own docstring),
which now chains this module's normalization after its own xref/trailer
repair -- so every downstream stage (`segment.py`, `redact.py`,
`pipeline.py`, `blocks.py`, `consensus.py`, `review_app.py`) sees an
upright page with zero code changes of its own, exactly as intended.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pdfplumber
import pikepdf
from PIL import Image

from melredact.config import CACHE_DIR, ORIENTATION_DETECT_DPI, ORIENTATION_MAX_TOLERATED_SKEW_DEG, ORIENTATION_MIN_SCORE

_CACHE_SUBDIR = "orientation"
_CARDINAL_ANGLES = (0, 90, 180, 270)

_classifier = None


def _engine():
    """Lazily construct PaddleOCR's dedicated doc-orientation classifier --
    see the module docstring for why this submodule, not the full pipeline.
    Deferred import/construction for the same reason ocr.py defers
    PaddleOCR: importing paddleocr at module scope would tax every caller
    of pdfio.open_pdf, including fixture-based tests that never touch a
    rotated page."""
    global _classifier
    if _classifier is None:
        from paddleocr import DocImgOrientationClassification

        _classifier = DocImgOrientationClassification()
    return _classifier


@dataclass
class PageOrientation:
    page_index: int
    # Rotation actually applied to reach upright, one of 0/90/180/270. 0
    # when no correction was needed OR when orientation couldn't be
    # confidently determined (see `confident` -- a caller must check that
    # flag, not infer "nothing to do" from applied_angle == 0).
    applied_angle: int
    confident: bool
    score: float
    # Residual skew in degrees after cardinal correction, or None when
    # orientation itself wasn't confident enough to measure skew against
    # (an unresolved page's skew relative to an unknown axis is meaningless).
    skew_deg: float | None

    @property
    def resolved(self) -> bool:
        """False means: don't trust this page's geometry-dependent output
        at all -- neither the cardinal classification nor (if that was
        confident) the residual skew measurement cleared their thresholds.
        `segment.segment_pdf` turns this into a packet-level issue."""
        if not self.confident:
            return False
        if self.skew_deg is not None and abs(self.skew_deg) > ORIENTATION_MAX_TOLERATED_SKEW_DEG:
            return False
        return True


@dataclass
class OrientationResult:
    normalized_path: Path
    pages: list[PageOrientation]

    def unresolved_page_indices(self) -> list[int]:
        return [p.page_index for p in self.pages if not p.resolved]

    def rotated_page_indices(self) -> list[int]:
        return [p.page_index for p in self.pages if p.confident and p.applied_angle != 0]


def classify_orientation(image: Image.Image) -> tuple[int, float]:
    """Whole-page cardinal orientation (0/90/180/270) plus the
    classifier's own confidence score, via PaddleOCR's dedicated
    DocImgOrientationClassification submodule -- see module docstring for
    why this and not the full-pipeline flag."""
    result = list(_engine().predict(np.array(image.convert("RGB"))))[0]
    angle = int(result["label_names"][0])
    score = float(result["scores"][0])
    return angle, score


def estimate_skew_deg(image: Image.Image) -> float | None:
    """Content-based residual skew estimate, in degrees, via a Hough-line
    scan for near-horizontal edges (Canny -> HoughLinesP, angles within 20
    degrees of horizontal to exclude vertical rules). None when no
    sufficiently long line is found (e.g. a mostly-blank page) -- never a
    best-effort guess of 0. See the module docstring for why this is
    reported, not corrected: existing evidence shows real skew this small
    is already tolerated by border/anchor detection."""
    import cv2

    gray = np.array(image.convert("L"))
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 720, threshold=150, minLineLength=max(1, int(gray.shape[1] * 0.15)), maxLineGap=10
    )
    if lines is None:
        return None
    angles = []
    for line in lines[:, 0]:
        x1, y1, x2, y2 = line
        dx, dy = x2 - x1, y2 - y1
        if dx == 0:
            continue
        angle = float(np.degrees(np.arctan2(dy, dx)))
        if abs(angle) <= 20:
            angles.append(angle)
    if not angles:
        return None
    return float(np.median(angles))


def normalize_page_image(image: Image.Image, applied_angle: int) -> Image.Image:
    """Rotate a rendered page image to upright given a detected
    `applied_angle` -- `PIL.Image.rotate(applied_angle, expand=True)`,
    verified by exact pixel round-trip (see module docstring) against the
    real classifier's own angle convention. Used for post-cardinal-
    correction skew measurement here; `_write_normalized_pdf` below applies
    the equivalent correction losslessly via `/Rotate` instead of
    re-rasterizing actual output."""
    if applied_angle == 0:
        return image
    return image.rotate(applied_angle, expand=True)


def _cache_paths(source_path: Path) -> tuple[Path, Path]:
    from melredact.ocr import file_content_hash

    h = file_content_hash(source_path)
    base = Path(CACHE_DIR) / _CACHE_SUBDIR / h
    return base.with_suffix(".json"), base.with_suffix(".pdf")


def _detect_pages(source_path: Path) -> list[PageOrientation]:
    pages: list[PageOrientation] = []
    with pdfplumber.open(source_path) as pdf:
        for idx, page in enumerate(pdf.pages):
            image = page.to_image(resolution=ORIENTATION_DETECT_DPI).original.convert("RGB")
            angle, score = classify_orientation(image)
            confident = score >= ORIENTATION_MIN_SCORE
            applied = angle if confident else 0
            skew = estimate_skew_deg(normalize_page_image(image, applied)) if confident else None
            pages.append(PageOrientation(page_index=idx, applied_angle=applied, confident=confident, score=score, skew_deg=skew))
    return pages


def _write_normalized_pdf(source_path: Path, pages: list[PageOrientation], norm_path: Path) -> None:
    norm_path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.open(source_path) as pdf:
        for p in pages:
            if p.confident and p.applied_angle != 0:
                pdf_page = pdf.pages[p.page_index]
                current = int(pdf_page.get("/Rotate", 0)) % 360
                pdf_page.Rotate = (current - p.applied_angle) % 360
        pdf.save(norm_path)


def normalize_pdf(source_path: str | Path) -> OrientationResult:
    """Detect and correct every page's cardinal orientation in
    `source_path` (already resolved through `pdfio.resolved_source_path`
    by every real caller -- see that module), disk-cached by the source
    file's own content hash so a second call (a second pipeline stage
    within one run, or a later `cli.py run`/Streamlit rerun) never
    re-detects. `source_path` itself is returned as `normalized_path` when
    no page needed correction -- see module docstring for why this avoids
    a wasted resave on the common all-upright case.
    """
    source_path = Path(source_path)
    manifest_path, norm_path = _cache_paths(source_path)
    if manifest_path.exists():
        pages = [PageOrientation(**p) for p in json.loads(manifest_path.read_text())]
        resolved_path = norm_path if norm_path.exists() else source_path
        return OrientationResult(normalized_path=resolved_path, pages=pages)

    pages = _detect_pages(source_path)
    needs_write = any(p.confident and p.applied_angle != 0 for p in pages)
    if needs_write:
        _write_normalized_pdf(source_path, pages, norm_path)
        resolved_path = norm_path
    else:
        resolved_path = source_path

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps([asdict(p) for p in pages], indent=2))
    return OrientationResult(normalized_path=resolved_path, pages=pages)


def orientation_for(pdf_path: str | Path) -> OrientationResult:
    """`normalize_pdf`, resolved from a caller-facing source path the same
    way `pdfio.open_pdf` resolves it (through the xref/trailer repair step
    first) -- so a caller like `segment.segment_pdf`, which only has the
    original path in scope, gets the identical per-page result `open_pdf`
    already produced (and cached) when it opened the same file."""
    from melredact.pdfio import resolved_source_path

    return normalize_pdf(resolved_source_path(pdf_path))


def unresolved_page_indices(pdf_path: str | Path) -> list[int]:
    """Page indices (into the original file's own page order -- normalization
    never adds, removes, or reorders pages) whose orientation could not be
    confidently determined -- see `PageOrientation.resolved`. Empty for the
    overwhelming majority of files (every page either upright or
    confidently corrected)."""
    return orientation_for(pdf_path).unresolved_page_indices()
