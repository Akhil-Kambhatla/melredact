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
arbitrary.

**Detect-and-ask, not detect-and-apply (2026-08-14).** The original version
of this module auto-applied any confidently-classified rotation
(`score >= ORIENTATION_MIN_SCORE`) with no human in the loop. Re-diagnosed
this session after a reviewer flagged pages p084/p085 of the real
`010406_PD1_PRT.pdf` as a suspected false positive: rendered both pages at
their raw, as-scanned orientation and at the detector's corrected
orientation, saved to `out/.diagnostics/orientation_p084_p085/` for direct
inspection. **Finding: this was not a false positive.** Both pages'
`/Rotate` is 0 in the source file (no metadata compensation anywhere), the
raw render is genuinely upside-down (illegible), the classifier's own
confidence (0.9238/0.9252) sits in the exact same band as every other
correctly-classified real page in this codebase's 176-page rotation audit,
and the corrected render is a perfectly legible upright header page --
"Gio Barisciano", date 10/24/25, teacher Talbert, period 1/HR. The detector
was right.

That finding is still the reason this module no longer auto-applies a
nonzero rotation, not a reason to leave it as detect-and-apply: it shows
there is **no score-based separation available between a confident-and-
correct rotation and a hypothetical confident-and-wrong one** -- every real
rotated page measured (these two included) and every real upright page
land in the identical 0.91-0.93 band. Raising `ORIENTATION_MIN_SCORE`
anywhere within its current headroom (0.6 up to just under 0.91) would not
change which real pages auto-apply, since none of them fall in that range
-- the gap simply isn't where the risk is. Given that, deriving "a
confidence threshold above which auto-apply is safe" honestly from this
data yields one answer for any *nonzero* rotation: never auto-apply one
unreviewed, because the score cannot tell a right guess from a wrong one.
A page needing zero correction has nothing to get wrong, so that path
alone stays automatic. This is the literal, data-grounded reading of "a
detector that silently rotates a page it misread is worse than no
detector" -- not a specific number, but a structural conclusion the data
actually supports.

**Design: three outcomes per page, not two.**
1. `angle == 0` and confident -- nothing to do, proceeds automatically.
2. Confident and `angle != 0` -- held for a human to confirm or correct
   (`PageOrientation.needs_confirmation`), never silently rotated. The
   containing packet's `issues` names the page, the detector's guessed
   angle, and its score (see `segment.segment_pdf`), so a reviewer sees
   exactly what to look at and why -- reusing the existing "packet with
   unresolved issues is refused" gate in `pipeline.run_dispositions`
   rather than inventing a parallel hold mechanism.
3. Not confident at all (e.g. a blank/near-blank page, ~0.26 in the real
   audit) -- held the same way, but with no guess to show, since guessing
   here is exactly the "confidently wrong" scenario this module exists to
   avoid, just with no confidence to even be wrong about.

**Human overrides, never applied without a record.** A human can supply
`overrides: dict[int, int]` (page_index -> one of 0/90/180/270) to
`normalize_pdf`/`orientation_for` -- this always wins over the detector,
for ANY page (not just one the detector flagged: a reviewer can rotate a
page the detector was confident was already upright, if they disagree).
`review_app.py` persists a human's rotation choice to
`decisions/<pdf-stem>.orientation.json` (see `pipeline.
load_orientation_overrides`/`save_orientation_overrides`) so a re-run
reproduces the exact same correction without asking again -- the
overrides dict is the *only* record of a human decision here; nothing is
ever baked into the source file itself.

**Detection is content-based, never PDF `/Rotate` metadata.** Real scans
arrive from Box after passing through Google Sheets exports, Box's own
processing, and (see `pdfio.py`) at least one prior re-save through a
qpdf-incompatible tool -- any of which can drop or rewrite `/Rotate`
without touching the actual embedded pixels. Trusting it would mean
trusting a value with no guaranteed relationship to what the page actually
looks like (confirmed directly on the real file: `/Rotate` is 0 on every
one of pages 83-86, including the two that are genuinely upside-down).
Detection instead renders the page exactly as it currently displays and
classifies *that*.

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
call with orientation classification enabled.

**Skew (non-cardinal tilt) is measured, not corrected.** See
`estimate_skew_deg`'s own docstring and CLAUDE.md's rotation-audit section
for the real evidence: existing corner-based border/anchor detection
(`redact.detect_header_band`) is already tested, with a passing regression
test, across skew up to ~2.56 degrees (24pt of drop across the header's
own width) -- and real measured skew across all 176 real pages this
session had access to never exceeded 1.48 degrees. A page whose residual
skew (after cardinal correction) exceeds `ORIENTATION_MAX_TOLERATED_SKEW_
DEG` is held the same way an unconfident classification is -- a shape
nothing in the real dataset or the existing skew regression test has
actually validated, so it gets a human rather than an untested guess. A
human-supplied override skips this check entirely: a person looking
directly at the rotated result has already made the judgment this check
exists to approximate.

**Persistence: one normalized PDF + one JSON manifest per (source file,
override set), disk-cached by the source file's own content hash plus a
fingerprint of the override set** (`ocr.file_content_hash`, the same
hasher `pdfio.py`'s own repair cache already shares) -- so re-running the
same file with the same overrides never re-detects or re-writes. The raw
per-page *detections* (the classifier's own output, independent of any
human override) are cached separately, keyed on content hash alone, so
supplying or changing an override never forces a re-classification, only
a cheap re-resolution and (only when a rotation actually needs to change)
a re-save.

**Correction mechanism: PDF `/Rotate`, not re-rasterizing page content.**
Setting `/Rotate` is lossless (no resampling of the actual scanned image)
and cheap (a metadata write, not a re-encode) -- pdfplumber, which every
downstream module in this codebase already reads pages through, resolves
`/Rotate` into `page.width`/`page.height` and `page.to_image()` output
consistently. The new value is `(current_effective_rotate -
detected_angle) % 360`, not `+` -- verified empirically (see git history
for the three hand-built round-trip cases this was checked against).

Every module in this codebase that opens a caller-supplied source PDF
already goes through `pdfio.open_pdf` (see that module's own docstring),
which chains this module's normalization after its own xref/trailer
repair -- so every downstream stage (`segment.py`, `redact.py`,
`pipeline.py`, `blocks.py`, `consensus.py`, `review_app.py`) sees an
upright page with zero rotation-awareness of its own, as long as it
threads through whatever `orientation_overrides` dict a human has
supplied (see `pdfio.open_pdf`'s own parameter).
"""

from __future__ import annotations

import hashlib
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
class PageDetection:
    """The classifier's own raw output for one page -- independent of any
    human decision, so it can be cached (and reused across different
    override sets) purely by the source file's content hash."""

    page_index: int
    angle: int  # 0/90/180/270, the classifier's raw guess -- only meaningful when confident
    score: float
    confident: bool  # score >= ORIENTATION_MIN_SCORE
    skew_deg: float | None  # measured against the hypothetically-corrected image, only when confident


@dataclass
class PageOrientation:
    page_index: int
    # The classifier's own guess, independent of whether it was ever
    # applied or confirmed -- 0 when the classifier wasn't confident at
    # all (nothing to guess). Always shown to a reviewer alongside `score`
    # so a hold's reason names both, per the detect-and-ask design.
    detected_angle: int
    score: float
    confident: bool
    skew_deg: float | None
    # Rotation actually baked into the normalized PDF's /Rotate, one of
    # 0/90/180/270. 0 both when nothing was needed and when a nonzero
    # rotation is still pending human confirmation -- callers must check
    # `resolved`/`needs_confirmation`, never infer "nothing to do" from
    # applied_angle == 0 alone.
    applied_angle: int
    # "auto": angle==0, classifier confident, applied with no human input
    # (nothing to get wrong). "human": a human explicitly supplied this
    # page's rotation (confirming the detector's guess, correcting it, or
    # rotating a page the detector never flagged at all) -- always wins,
    # always resolved, skew/confidence checks don't apply. "none": neither
    # of the above -- either a confident nonzero guess awaiting
    # confirmation, or a classification too weak to guess from at all.
    source: str

    @property
    def needs_confirmation(self) -> bool:
        """True only for the detect-and-ask case: the classifier is
        confident about a *nonzero* rotation but no human has approved or
        corrected it yet. False once a human supplies any override for
        this page (even one that agrees with the detector), since that's
        no longer merely a guess."""
        return self.source == "none" and self.confident and self.detected_angle != 0

    @property
    def resolved(self) -> bool:
        """False means: don't trust this page's geometry-dependent output
        at all. A human-sourced page is always resolved -- a person who
        rotated it and saw the result has already made the judgment the
        confidence/skew checks below only approximate."""
        if self.source == "human":
            return True
        if not self.confident or self.needs_confirmation:
            return False
        if self.skew_deg is not None and abs(self.skew_deg) > ORIENTATION_MAX_TOLERATED_SKEW_DEG:
            return False
        return True


@dataclass
class OrientationResult:
    normalized_path: Path
    pages: list[PageOrientation]

    def unresolved_page_indices(self) -> list[int]:
        """Pages that are neither safely resolved nor merely awaiting a
        confirmation click -- e.g. too ambiguous to even guess from, or
        confidently upright but too skewed to trust. See `segment.
        segment_pdf` for how this becomes a packet issue."""
        return [p.page_index for p in self.pages if not p.resolved and not p.needs_confirmation]

    def pending_confirmation_page_indices(self) -> list[int]:
        """Pages with a confident, specific, nonzero rotation guess a
        human hasn't approved yet -- see PageOrientation.needs_
        confirmation. Kept distinct from unresolved_page_indices so a
        caller (segment.segment_pdf) can word the packet issue
        differently: "here's my guess, confirm or correct it" vs. "I
        can't tell at all"."""
        return [p.page_index for p in self.pages if p.needs_confirmation]

    def rotated_page_indices(self) -> list[int]:
        return [p.page_index for p in self.pages if p.source in ("auto", "human") and p.applied_angle != 0]

    def by_page(self) -> dict[int, PageOrientation]:
        return {p.page_index: p for p in self.pages}


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


def _overrides_fingerprint(overrides: dict[int, int] | None) -> str:
    if not overrides:
        return "auto"
    items = sorted((int(k), int(v) % 360) for k, v in overrides.items())
    raw = ",".join(f"{k}:{v}" for k, v in items)
    return "ov" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def _cache_paths(source_path: Path, overrides: dict[int, int] | None) -> tuple[Path, Path, Path]:
    """(detections_path, resolved_manifest_path, normalized_pdf_path).
    `detections_path` is keyed on the source file's content hash alone --
    the classifier's own output never depends on any human override, so
    it's reusable across every override set tried against this file. The
    other two are keyed on content hash *and* the override set's own
    fingerprint, since a different override set can resolve to a
    genuinely different normalized PDF."""
    from melredact.ocr import file_content_hash

    h = file_content_hash(source_path)
    base = Path(CACHE_DIR) / _CACHE_SUBDIR
    detections_path = base / f"{h}.detections.json"
    resolved_base = base / f"{h}_{_overrides_fingerprint(overrides)}"
    return detections_path, resolved_base.with_suffix(".json"), resolved_base.with_suffix(".pdf")


def _detect_pages(source_path: Path) -> list[PageDetection]:
    detections: list[PageDetection] = []
    with pdfplumber.open(source_path) as pdf:
        for idx, page in enumerate(pdf.pages):
            image = page.to_image(resolution=ORIENTATION_DETECT_DPI).original.convert("RGB")
            angle, score = classify_orientation(image)
            confident = score >= ORIENTATION_MIN_SCORE
            skew = estimate_skew_deg(normalize_page_image(image, angle)) if confident else None
            detections.append(PageDetection(page_index=idx, angle=angle, score=score, confident=confident, skew_deg=skew))
    return detections


def resolve_pages(detections: list[PageDetection], overrides: dict[int, int] | None) -> list[PageOrientation]:
    """Combine the classifier's raw output with a human's explicit
    per-page overrides (page_index -> 0/90/180/270) into the final,
    detect-and-ask resolution -- see module docstring for the three
    outcomes. An override always wins, for any page, regardless of what
    (or whether) the classifier guessed for it."""
    overrides = overrides or {}
    pages: list[PageOrientation] = []
    for d in detections:
        if d.page_index in overrides:
            angle = int(overrides[d.page_index]) % 360
            pages.append(
                PageOrientation(
                    page_index=d.page_index,
                    detected_angle=d.angle if d.confident else 0,
                    score=d.score,
                    confident=d.confident,
                    skew_deg=d.skew_deg,
                    applied_angle=angle,
                    source="human",
                )
            )
        elif d.confident and d.angle == 0:
            pages.append(
                PageOrientation(
                    page_index=d.page_index,
                    detected_angle=0,
                    score=d.score,
                    confident=True,
                    skew_deg=d.skew_deg,
                    applied_angle=0,
                    source="auto",
                )
            )
        elif d.confident:
            pages.append(
                PageOrientation(
                    page_index=d.page_index,
                    detected_angle=d.angle,
                    score=d.score,
                    confident=True,
                    skew_deg=d.skew_deg,
                    applied_angle=0,
                    source="none",
                )
            )
        else:
            pages.append(
                PageOrientation(
                    page_index=d.page_index,
                    detected_angle=0,
                    score=d.score,
                    confident=False,
                    skew_deg=None,
                    applied_angle=0,
                    source="none",
                )
            )
    return pages


def _write_normalized_pdf(source_path: Path, pages: list[PageOrientation], norm_path: Path) -> None:
    norm_path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.open(source_path) as pdf:
        for p in pages:
            if p.applied_angle != 0:
                pdf_page = pdf.pages[p.page_index]
                current = int(pdf_page.get("/Rotate", 0)) % 360
                pdf_page.Rotate = (current - p.applied_angle) % 360
        pdf.save(norm_path)


def normalize_pdf(source_path: str | Path, *, overrides: dict[int, int] | None = None) -> OrientationResult:
    """Detect and resolve every page's cardinal orientation in
    `source_path` (already resolved through `pdfio.resolved_source_path`
    by every real caller -- see that module), disk-cached so a second call
    with the *same* override set never re-detects or re-writes (see
    `_cache_paths`). `source_path` itself is returned as `normalized_path`
    when no page needed a rotation actually applied -- see module
    docstring for why this avoids a wasted resave on the common
    all-upright case.

    `overrides` (page_index -> 0/90/180/270) is a human's explicit
    per-page rotation choice -- see `resolve_pages`. Passing a different
    override set is cheap even on a cold cache for the *detection* step
    (that part is cached on content hash alone, unaffected by overrides);
    only the resolution + (if needed) the pikepdf resave repeat.
    """
    source_path = Path(source_path)
    detect_path, manifest_path, norm_path = _cache_paths(source_path, overrides)

    if detect_path.exists():
        detections = [PageDetection(**d) for d in json.loads(detect_path.read_text())]
    else:
        detections = _detect_pages(source_path)
        detect_path.parent.mkdir(parents=True, exist_ok=True)
        detect_path.write_text(json.dumps([asdict(d) for d in detections], indent=2))

    if manifest_path.exists():
        pages = [PageOrientation(**p) for p in json.loads(manifest_path.read_text())]
        resolved_path = norm_path if norm_path.exists() else source_path
        return OrientationResult(normalized_path=resolved_path, pages=pages)

    pages = resolve_pages(detections, overrides)
    needs_write = any(p.applied_angle != 0 for p in pages)
    if needs_write:
        _write_normalized_pdf(source_path, pages, norm_path)
        resolved_path = norm_path
    else:
        resolved_path = source_path

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps([asdict(p) for p in pages], indent=2))
    return OrientationResult(normalized_path=resolved_path, pages=pages)


def orientation_for(pdf_path: str | Path, *, overrides: dict[int, int] | None = None) -> OrientationResult:
    """`normalize_pdf`, resolved from a caller-facing source path the same
    way `pdfio.open_pdf` resolves it (through the xref/trailer repair step
    first) -- so a caller like `segment.segment_pdf`, which only has the
    original path in scope, gets the identical per-page result `open_pdf`
    itself produced (and cached) when it opened the same file with the
    same overrides."""
    from melredact.pdfio import resolved_source_path

    return normalize_pdf(resolved_source_path(pdf_path), overrides=overrides)


def unresolved_page_indices(pdf_path: str | Path, *, overrides: dict[int, int] | None = None) -> list[int]:
    """Page indices (into the original file's own page order -- normalization
    never adds, removes, or reorders pages) whose orientation could not be
    confidently determined at all -- see `OrientationResult.
    unresolved_page_indices`. Empty for the overwhelming majority of files."""
    return orientation_for(pdf_path, overrides=overrides).unresolved_page_indices()


def pending_confirmation_page_indices(pdf_path: str | Path, *, overrides: dict[int, int] | None = None) -> list[int]:
    """Page indices with a confident, specific, nonzero rotation guess
    that no human has approved yet -- see `OrientationResult.
    pending_confirmation_page_indices`."""
    return orientation_for(pdf_path, overrides=overrides).pending_confirmation_page_indices()
