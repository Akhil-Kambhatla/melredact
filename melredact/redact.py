"""Redaction: destroy identifying header ink on the raster while keeping a
searchable OCR text layer everywhere else.

Two coordinate systems are in play and they are NOT related by a flip:

- Page-point space (top-down: `top`/`bottom` measured from the page top) is
  used everywhere else in this codebase -- config.py's measured anchors,
  pdfplumber's `extract_words()`, melredact.ocr's word boxes. Rasterized
  image-pixel space is also top-down (y increases downward), so converting
  between the two is a uniform `dpi/72` scale, never an axis flip. This is
  what `detect_header_band` and `_draw_redaction_box` use.
- Raw PDF content-stream coordinates (`cm`, `Tm`, and everything else this
  module writes directly into a page's content stream) are bottom-left
  origin -- the one place in this pipeline where page-point-space `top` has
  to become `page_height - top`. That happens in exactly one function,
  `_pdf_baseline_y`. Getting this wrong doesn't crash anything: it silently
  writes a word's invisible text token to the wrong place on the page,
  which could as easily land it outside the redacted region as in it.
  `test_redact.py` verifies this by round-tripping a word through the real
  writer and pdfplumber's real reader, not by re-deriving the formula.

Because the output keeps a text layer, "no text layer" is no longer a
trivially-true property of every redacted file the way it was when pages
were flattened to images. `verify_no_leaked_names` is the actual proof:
it extracts text from every page of the *finished* file and checks it
against the full roster, not just the header region of the page that was
supposedly redacted -- with a fuzzy second pass (LEAK_FUZZY_MIN_RATIO),
since exact-token matching alone missed the real "Ganik"/"Gonik" leak
(OCR garbled a real roster name into a token that just isn't an exact
match for anything on the roster).

Two rectangles are redacted per header page, not one (see
`redact_bboxes_for_band`): the original left column (Name/Teacher/Group),
plus a full-width strip covering the Group row's own height and below,
added after that same leak showed group-member lists are handwritten
across the *entire page width*, not just the left column.
`find_uncovered_group_words` is the geometric proof that these two
rectangles actually cover the Group row's ink -- independent of whatever
OCR thinks that ink says, which is exactly what the text-based check
can't offer.

The pre-reversal flatten-to-image behavior (no text layer at all, ever) is
kept behind `redact_packet(..., flatten=True)` -- John is re-checking the
keep-the-text-layer decision with Doug and it may reverse again.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pdfplumber
import pikepdf
from rapidfuzz import fuzz
from PIL import Image, ImageDraw, ImageFont
from pikepdf import Dictionary, Name

from melredact.config import (
    BORDER_BOTTOM_ANCHOR_BACK_SLACK_PT,
    BORDER_BOTTOM_ANCHOR_FORWARD_SLACK_PT,
    BORDER_CORNER_SEARCH_SLACK_PT,
    BORDER_CORNER_WINDOW_PT,
    BORDER_DARK_THRESHOLD,
    BORDER_LINE_FRACTION,
    BORDER_SEARCH_SLACK_PT,
    BORDER_TOP_ANCHOR_SLACK_PT,
    COLUMN_SPLIT_X,
    GROUP_ANCHOR,
    GROUP_ROW_SPLIT_OFFSET_PT,
    HEADER_BAND_FALLBACK,
    HEADER_SEARCH_MAX_TOP,
    LEAK_FUZZY_MIN_RATIO,
    LEAK_FUZZY_MIN_TOKEN_LEN,
    MIN_NAME_CHARS,
    REDACTION_FILL_COLOR,
    REDACTION_STAMP_COLOR,
    REDACTION_STAMP_TEXT,
    RENDER_DPI_FINAL,
    STAMP_FONT_SIZE_PT,
    STAMP_LINE_SPACING_PT,
    STAMP_PADDING_PT,
)
from melredact.roster import Roster
from melredact.segment import (
    HeaderAnchors,
    Packet,
    _assign_words_to_rows,
    header_row_height,
    locate_header_anchors,
    page_words,
)

Word = dict
Bbox = tuple[float, float, float, float]  # (left, top, right, bottom) in page points, top-down


@dataclass
class HeaderBand:
    left: float
    top: float
    right: float
    bottom: float
    detected: bool  # True only if the raster search found all four edges


def detect_header_band(
    image: Image.Image,
    *,
    dpi: int,
    anchors: HeaderAnchors | None = None,
    row_height: float | None = None,
    fallback: dict = HEADER_BAND_FALLBACK,
    search_slack_pt: float = BORDER_SEARCH_SLACK_PT,
    dark_threshold: int = BORDER_DARK_THRESHOLD,
    line_fraction: float = BORDER_LINE_FRACTION,
    corner_window_pt: float = BORDER_CORNER_WINDOW_PT,
    corner_search_slack_pt: float = BORDER_CORNER_SEARCH_SLACK_PT,
    top_anchor_slack_pt: float = BORDER_TOP_ANCHOR_SLACK_PT,
    bottom_anchor_back_slack_pt: float = BORDER_BOTTOM_ANCHOR_BACK_SLACK_PT,
    bottom_anchor_forward_slack_pt: float = BORDER_BOTTOM_ANCHOR_FORWARD_SLACK_PT,
) -> HeaderBand:
    """Locate the drawn header border by scanning the raster for rows/
    columns that are mostly dark pixels (a printed rule).

    Left/right (vertical rules) barely move between worksheet templates --
    same page width, same margins -- so those are still found with a global
    column scan using a generous fixed slack around `fallback`.

    Top/bottom (horizontal rules) are a different story: their absolute
    page position depends entirely on how long *this worksheet's* title/
    instructions block is above the header table, which varies by
    worksheet type (confirmed on two real files: PRT's two-line title
    pushes name_top ~37-44pt further down the page than MPR's). A fixed
    absolute-position search window (the old behavior, still used below
    when `anchors` is omitted) only finds the right rule when a worksheet
    happens to share MPR's own title length -- on PRT it either clamped the
    box up into blank/title space or clipped its own search short of the
    real bottom border, leaving the Group row's ink outside the redaction
    box entirely.

    When `anchors` (this page's own located Name/Teacher/Group label
    positions -- worksheet-agnostic, found by literal OCR text search, not
    position) is given, the top/bottom search is instead centered on this
    page's own block: the top rule near `anchors.name_top`
    (BORDER_TOP_ANCHOR_SLACK_PT either side -- both real files' true border
    sits within ~2pt of name_top), and the bottom rule near
    `anchors.group_top + row_height` (one more row's height past Group,
    the same self-relative measure segment.py already uses for the
    matching-assignment window), with back/forward slack from config.
    This adapts to wherever the block actually sits, instead of assuming
    it's wherever MPR's own title happened to put it.

    Without `anchors` (or when none of its three labels were found), this
    falls back to the old fixed-window behavior entirely, searching around
    the static HEADER_BAND_FALLBACK numbers and clamping outward toward
    them -- the safest available behavior when there's no page-specific
    information to anchor to at all.

    A skewed scan tilts the whole rectangle, top/bottom rules included --
    but confirmed against the real file, no row-based scan can find a
    tilted top/bottom rule cleanly: generous enough to tolerate the tilt,
    it also reaches the section title sitting close above the box; narrow
    enough to exclude the title, it can't span a rule that occupies a
    different row at each x under tilt. So top/bottom are instead read off
    the *already-found* left/right columns' own vertical extent: a narrow
    window right at each detected column gives that corner's own top/
    bottom hit row directly. top/bottom are then the envelope across the
    two corners (min top, max bottom) -- the AABB of the tilted rectangle.
    """
    scale = dpi / 72.0
    gray = np.asarray(image.convert("L"))
    h, w = gray.shape

    def to_px(v: float) -> int:
        return int(round(v * scale))

    anchored = anchors is not None and (anchors.name_found or anchors.teacher_found or anchors.group_found)
    if anchored:
        expected_top = anchors.name_top
        expected_bottom = anchors.group_top + (row_height if row_height is not None else header_row_height(anchors))
        col_y_top = min(fallback["top"], expected_top - top_anchor_slack_pt)
        col_y_bottom = max(fallback["bottom"], expected_bottom + bottom_anchor_forward_slack_pt)
    else:
        expected_top = fallback["top"]
        expected_bottom = fallback["bottom"]
        col_y_top = fallback["top"]
        col_y_bottom = fallback["bottom"]

    x0 = max(0, to_px(fallback["left"] - search_slack_pt))
    x1 = min(w, to_px(fallback["right"] + search_slack_pt))

    dark = gray < dark_threshold

    # Vertical rules (left/right edges) first: a global column scan over a
    # y-span wide enough to cover wherever this page's block actually is
    # (tolerates tilt fine, since even a tilted rule stays close to
    # vertical -- confirmed against the real file).
    col_slice = dark[max(0, to_px(col_y_top)) : min(h, to_px(col_y_bottom)), x0:x1]
    col_frac = col_slice.mean(axis=0) if col_slice.size else np.array([])
    col_hits = np.nonzero(col_frac >= line_fraction)[0]
    left_col = int(col_hits[0]) + x0 if len(col_hits) else None
    right_col = int(col_hits[-1]) + x0 if len(col_hits) else None

    # Horizontal rules (top/bottom edges), read off each vertical rule's own
    # extent -- see the docstring above for why this replaces a row-based
    # scan entirely. Top and bottom get their own independent windows
    # (anchor-relative, when available) rather than one shared window, since
    # the two rules can sit far apart on the page and a single window wide
    # enough to reach both risks grabbing an internal row divider instead.
    if anchored:
        top_cy0, top_cy1 = expected_top - top_anchor_slack_pt, expected_top + top_anchor_slack_pt
        bot_cy0, bot_cy1 = expected_bottom - bottom_anchor_back_slack_pt, expected_bottom + bottom_anchor_forward_slack_pt
    else:
        top_cy0 = top_cy1 = bot_cy0 = bot_cy1 = None
    if not anchored:
        cy0 = fallback["top"] - corner_search_slack_pt
        cy1 = fallback["bottom"] + corner_search_slack_pt
        top_cy0 = bot_cy0 = cy0
        top_cy1 = bot_cy1 = cy1

    top_py0, top_py1 = max(0, to_px(top_cy0)), min(h, to_px(top_cy1))
    bot_py0, bot_py1 = max(0, to_px(bot_cy0)), min(h, to_px(bot_cy1))
    corner_px = max(1, to_px(corner_window_pt))
    top_hits: list[int] = []
    bottom_hits: list[int] = []
    for col in (left_col, right_col):
        if col is None:
            continue
        cx0 = max(0, col - corner_px)
        cx1 = min(w, col + corner_px + 1)
        top_slice = dark[top_py0:top_py1, cx0:cx1]
        if top_slice.size:
            top_frac = top_slice.mean(axis=1)
            hits = np.nonzero(top_frac >= line_fraction)[0]
            if len(hits):
                top_hits.append(int(hits[0]) + top_py0)
        bottom_slice = dark[bot_py0:bot_py1, cx0:cx1]
        if bottom_slice.size:
            bottom_frac = bottom_slice.mean(axis=1)
            hits = np.nonzero(bottom_frac >= line_fraction)[0]
            if len(hits):
                bottom_hits.append(int(hits[-1]) + bot_py0)
    top_row = min(top_hits) if top_hits else None
    bottom_row = max(bottom_hits) if bottom_hits else None

    detected = None not in (top_row, bottom_row, left_col, right_col)

    if anchored:
        # No clamping toward the (worksheet-specific) fallback numbers here
        # -- that clamp is exactly what used to override a correctly
        # detected PRT border back toward MPR's absolute position. Absent a
        # clean detection, the honest anchor-derived expectation is a
        # better-informed placeholder than a fixed constant from a
        # different template, but `detected=False` still means "don't trust
        # this" to callers (see redact_packet/run_dispositions).
        final_top = top_row / scale if top_row is not None else expected_top
        final_bottom = bottom_row / scale if bottom_row is not None else expected_bottom
        final_left = left_col / scale if left_col is not None else fallback["left"]
        final_right = right_col / scale if right_col is not None else fallback["right"]
    else:
        final_top = min(top_row / scale, fallback["top"]) if top_row is not None else fallback["top"]
        final_bottom = max(bottom_row / scale, fallback["bottom"]) if bottom_row is not None else fallback["bottom"]
        final_left = min(left_col / scale, fallback["left"]) if left_col is not None else fallback["left"]
        final_right = max(right_col / scale, fallback["right"]) if right_col is not None else fallback["right"]

    return HeaderBand(left=final_left, top=final_top, right=final_right, bottom=final_bottom, detected=detected)


def redact_bbox_for_band(band: HeaderBand) -> Bbox:
    """Left column only -- Name/Teacher/Group. Never crosses COLUMN_SPLIT_X,
    so the Date/Period column on the right is always left untouched even if
    a detected band's right edge extends past the split."""
    return (band.left, band.top, min(COLUMN_SPLIT_X, band.right), band.bottom)


def group_row_split_top(band: HeaderBand, group_top: float) -> float:
    """Where the right column's untouched Date/Period zone ends and the
    full-width overflow strip begins -- this page's own located Group-row
    anchor (`group_top`, from segment.locate_header_anchors: OCR'd per
    page, so it survives skew the same way name_top/teacher_top already
    do) plus GROUP_ROW_SPLIT_OFFSET_PT, not a fixed y. See config.py for
    the real-file measurement (a clean, genuinely-blank ~6pt gap between
    Period's own ink and the group-row overflow) that offset is centered
    in. Clamped to band's own extent so a wildly-off anchor (label not
    found at all, fallback_top used) can't push the split outside the
    detected band entirely.
    """
    top = group_top + GROUP_ROW_SPLIT_OFFSET_PT
    return max(band.top, min(top, band.bottom))


def redact_bboxes_for_band(band: HeaderBand, group_top: float) -> tuple[Bbox, Bbox]:
    """Two rectangles, not one: the unchanged full-height left column
    (Name/Teacher/Group), plus a new full-width strip covering the Group
    row's own height and everything below it down to the header's own
    bottom border.

    Real-file leak (SID 0204150202, see CLAUDE.md): a group-members list
    is handwritten across the *entire page width*, not just the left
    column -- "King, Sfoh, Braydeh, Ganik" ran from x=184 to x=488, well
    past COLUMN_SPLIT_X=400, into the Date/Period column, at the Group
    row's own height (comfortably below where Date/Period's own values
    sit -- see group_row_split_top). The old single left-column box never
    had a chance to catch the part of that line past x=400. This second
    rectangle closes that gap without touching Date/Period, which stay
    visible in the untouched strip above group_row_split_top.
    """
    left = redact_bbox_for_band(band)
    strip_top = group_row_split_top(band, group_top)
    right = (min(COLUMN_SPLIT_X, band.right), strip_top, band.right, band.bottom)
    return left, right


def _overlaps_bbox(word: Word, bbox: Bbox) -> bool:
    left, top, right, bottom = bbox
    return not (word["x1"] <= left or word["x0"] >= right or word["bottom"] <= top or word["top"] >= bottom)


def _draw_redaction_box(
    image: Image.Image, bbox_pt: Bbox, dpi: int, stamp_lines: list[str] | None = None, *, draw_stamp: bool = True
) -> None:
    """Paint an opaque box over the redacted region in the raster, plus
    (when `draw_stamp`) a visible stamp so a reviewer sees redaction
    happened rather than a coincidentally-blank field -- and so the
    packet can still be traced back to its approved student. `stamp_lines`
    is the packet's re-identification key, e.g. ["SID: 0204150204", "PD:
    02"], rendered left-aligned top to bottom (not centered -- multiple
    lines centered as a block reads worse, and left alignment matches how
    the destroyed Name/Teacher/Group text itself was aligned). Defaults to
    a single REDACTION_STAMP_TEXT line when no sid is known to stamp yet.
    `draw_stamp=False` is for the second (full-width overflow strip) box
    redact_packet also paints -- one stamp per packet is enough; a second
    copy on a thin strip would just be visual noise. Same top-down,
    uniform-scale conversion as detect_header_band -- no axis flip here,
    this is still image-pixel space, not a PDF content stream.
    """
    scale = dpi / 72.0
    left, top, right, bottom = (v * scale for v in bbox_pt)
    draw = ImageDraw.Draw(image)
    draw.rectangle([left, top, right, bottom], fill=REDACTION_FILL_COLOR)

    if not draw_stamp:
        return
    if not stamp_lines:
        stamp_lines = [REDACTION_STAMP_TEXT]

    font = ImageFont.load_default(size=max(1, int(STAMP_FONT_SIZE_PT * scale)))
    pad = STAMP_PADDING_PT * scale
    line_spacing = STAMP_LINE_SPACING_PT * scale
    x = left + pad
    y = top + pad
    for line in stamp_lines:
        draw.text((x, y), line, fill=REDACTION_STAMP_COLOR, font=font)
        text_bbox = draw.textbbox((0, 0), line, font=font)
        y += (text_bbox[3] - text_bbox[1]) + line_spacing


def render_redaction_preview(
    header_page_image: Image.Image,
    *,
    dpi: int,
    anchors: HeaderAnchors | None = None,
    group_top: float | None = None,
    stamp_lines: list[str] | None = None,
    band_override: HeaderBand | None = None,
) -> tuple[Image.Image, HeaderBand]:
    """Non-destructive preview of what redact_packet would do to this
    header page: same detection + box-drawing (including the same
    `stamp_lines`, when the caller has a candidate sid to preview), on a
    copy, so a reviewer can see it before approving anything. Sharing the
    exact mechanism (rather than a simplified stand-in) guarantees the
    preview and the real output can't drift apart.

    `anchors` should be this page's own located anchors (e.g.
    `extract_header_fields(...).anchors`, which the caller typically
    already has for the field table) so border detection is anchored to
    *this* page's block the same way redact_packet's is -- see
    detect_header_band's docstring for why a fixed-position search doesn't
    generalize across worksheet types. `group_top` is still accepted
    separately for a caller that has that one number but not full anchors;
    defaults to `anchors.group_top` when anchors are given, else the fixed
    GROUP_ANCHOR measurement.

    `band_override`, when given, skips auto-detection entirely and previews
    that exact geometry instead -- review_app.py's manual-redaction queue
    uses this so a human can see what a proposed corrected band would
    actually cover *before* calling `pipeline.release_from_manual_queue`,
    the same way the ordinary decision preview lets a reviewer see a
    candidate match before confirming it.
    """
    preview = header_page_image.copy()
    row_height = header_row_height(anchors) if anchors is not None else None
    band = band_override if band_override is not None else detect_header_band(preview, dpi=dpi, anchors=anchors, row_height=row_height)
    effective_group_top = group_top
    if effective_group_top is None:
        effective_group_top = anchors.group_top if anchors is not None else GROUP_ANCHOR["top"]
    left_bbox, right_bbox = redact_bboxes_for_band(band, effective_group_top)
    _draw_redaction_box(preview, left_bbox, dpi, stamp_lines)
    _draw_redaction_box(preview, right_bbox, dpi, draw_stamp=False)
    return preview, band


def _font_size_for_word(word: Word) -> float:
    return max(4.0, word["bottom"] - word["top"])


# Standard Helvetica advance widths (1/1000 em, StandardEncoding/WinAnsiEncoding
# agree on all of these), the built-in metrics any PDF reader falls back to for
# one of the 14 standard fonts when the font dict carries no Widths array (ours
# doesn't -- see _PdfWriter._font). These are the numbers pdfplumber actually
# uses to compute each character's on-page position when it reconstructs text,
# so they're what the real-world overlap bug below has to be measured against,
# not eyeballed.
_HELVETICA_WIDTHS = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667, "'": 191,
    "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333, ".": 278, "/": 278,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556, "7": 556,
    "8": 556, "9": 556, ":": 278, ";": 278, "<": 584, "=": 584, ">": 584, "?": 556,
    "@": 1015,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722,
    "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722, "O": 778, "P": 667,
    "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944, "X": 667,
    "Y": 667, "Z": 611,
    "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556, "`": 333,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556, "h": 556,
    "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556, "o": 556, "p": 556,
    "q": 556, "r": 333, "s": 500, "t": 278, "u": 556, "v": 500, "w": 722, "x": 500,
    "y": 500, "z": 500,
    "{": 334, "|": 260, "}": 334, "~": 584,
}
_HELVETICA_DEFAULT_WIDTH = 556
_HSCALE_MIN = 0.05
_HSCALE_MAX = 20.0


def _helvetica_advance_width(text: str, size: float) -> float:
    """Width Helvetica would actually render `text` at, in page points --
    the number that matters for _horizontal_scale_for_word below, not the
    OCR-measured word box, since it's Helvetica's own metrics (not the
    box) that a reader with no Widths array falls back to."""
    return sum(_HELVETICA_WIDTHS.get(ch, _HELVETICA_DEFAULT_WIDTH) for ch in text) / 1000.0 * size


def _horizontal_scale_for_word(word: Word, size: float) -> float:
    """Real scans (see melredact/ocr.py) produce OCR word boxes measured to
    the actual ink -- often narrower than Helvetica's own advance width for
    the same text at the font size `_font_size_for_word` derives from box
    height. Positioning every word at its own absolute x0 (already true,
    see _invisible_text_op) is not enough on its own: with no horizontal
    scale correction, Helvetica renders each word wider than its measured
    box, so it advances past the *next* word's x0 and the two overlap in
    text-space. pdfplumber's word-clustering (which groups characters by
    x-proximity, not by which Tj call produced them) then interleaves the
    overlapping characters into a single garbled token on read-back --
    confirmed on the real PRT file: "A Plausibility Ranking Task" (correct,
    in-order OCR words) extracted back as "A PlausibilRitya nkinTga sk".
    Note this is a *positional* corruption, not a content one -- the OCR
    words themselves were never wrong; the bug is entirely in the writer.

    Scaling the text matrix's horizontal component so each word's rendered
    advance equals its own measured box width (`x1 - x0`) fixes this at the
    source: every word ends exactly where the next one's independently-set
    x0 begins, so nothing can overlap regardless of how tight or wide the
    original OCR box was.
    """
    target_width = max(word["x1"] - word["x0"], 0.01)
    natural_width = _helvetica_advance_width(word["text"], size)
    if natural_width <= 0:
        return 1.0
    return min(_HSCALE_MAX, max(_HSCALE_MIN, target_width / natural_width))


def _pdf_baseline_y(word: Word, page_height_pt: float) -> float:
    """The one place page-point `top` (distance from page top) becomes a
    PDF content-stream y (distance from page bottom). Mirrors the inverse
    of what pdfplumber does when it turns glyph geometry back into `top`/
    `bottom` -- verified by round-tripping through the real writer and
    pdfplumber's real reader in test_redact.py, not trusted from inspection.
    """
    size = _font_size_for_word(word)
    return page_height_pt - word["top"] - size * 0.8


def _escape_pdf_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _invisible_text_op(word: Word, page_height_pt: float) -> bytes:
    """Each word is its own absolutely-positioned Tj run (so a redacted
    word can simply be omitted), which drops the real space *character*
    that would otherwise separate it from its neighbor. pdfplumber's word
    splitter treats an explicit space glyph as a hard break independent of
    gap distance -- without it, two genuinely separate words with a small
    on-page gap silently re-merge into one token on read-back. A trailing
    space is invisible either way (Tr 3), so appending one costs nothing
    and keeps the output's word boundaries matching the input's.

    The text matrix's horizontal component is `_horizontal_scale_for_word`,
    not a bare 1 -- see its docstring for why an unscaled Helvetica advance
    overlaps adjacent words and scrambles read-back text on real scans.
    """
    size = _font_size_for_word(word)
    y = _pdf_baseline_y(word, page_height_pt)
    hscale = _horizontal_scale_for_word(word, size)
    text = _escape_pdf_text(word["text"] + " ")
    return f"BT /F1 {size:.2f} Tf 3 Tr {hscale:.4f} 0 0 1 {word['x0']:.2f} {y:.2f} Tm ({text}) Tj ET".encode()


class _PdfWriter:
    """Assembles the redacted output page by page: one full-page raster
    (scan background, with the redaction box already baked in for header
    pages) plus, unless flattened, an invisible OCR text layer positioned
    from the same top-down word boxes used everywhere else in this
    codebase."""

    def __init__(self) -> None:
        self.pdf = pikepdf.Pdf.new()
        self._font_ref = None

    def _font(self):
        if self._font_ref is None:
            self._font_ref = self.pdf.make_indirect(
                Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
            )
        return self._font_ref

    def add_page(self, image: Image.Image, words: list[Word], page_width: float, page_height: float) -> None:
        page = self.pdf.add_blank_page(page_size=(page_width, page_height))

        compressed = zlib.compress(image.tobytes())
        im_obj = pikepdf.Stream(self.pdf, compressed)
        im_obj.Type = Name.XObject
        im_obj.Subtype = Name.Image
        im_obj.Width = image.width
        im_obj.Height = image.height
        im_obj.ColorSpace = Name.DeviceRGB
        im_obj.BitsPerComponent = 8
        im_obj.Filter = Name.FlateDecode

        page.Resources = self.pdf.make_indirect(
            Dictionary(
                XObject=Dictionary(Im0=self.pdf.make_indirect(im_obj)),
                Font=Dictionary(F1=self._font()),
            )
        )

        parts = [f"q {page_width} 0 0 {page_height} 0 0 cm /Im0 Do Q".encode()]
        for word in words:
            parts.append(_invisible_text_op(word, page_height))
        page.Contents = self.pdf.make_indirect(pikepdf.Stream(self.pdf, b"\n".join(parts) + b"\n"))

    def save(self, path: str | Path) -> None:
        self.pdf.save(path)


@dataclass
class RedactResult:
    out_path: Path
    band: HeaderBand | None
    redact_bbox: Bbox | None
    flattened: bool
    # None only when there's no header page to redact (is_orphan). The
    # full-width overflow strip added for the group-row leak (see
    # redact_bboxes_for_band); kept as a separate field rather than folded
    # into redact_bbox so existing callers reading redact_bbox (the
    # stamped left column, unchanged) don't need to change.
    redact_strip_bbox: Bbox | None = None
    # Group-row words (from this same OCR pass, so this costs nothing
    # extra) that neither redact_bbox nor redact_strip_bbox actually
    # covers -- see find_uncovered_group_words. Non-empty means the
    # redaction rectangles failed to cover real overflow ink; a caller
    # (run_dispositions) must treat this exactly like a verify_no_leaked_
    # names finding, not just log it.
    uncovered_group_words: list[Word] = field(default_factory=list)


def find_uncovered_group_words(
    header_words: list[Word], anchors: HeaderAnchors, left_bbox: Bbox, right_bbox: Bbox
) -> list[Word]:
    """Proof that the two redaction rectangles actually cover the Group
    row's ink, independent of whatever OCR thinks that ink says.

    Reuses segment._assign_words_to_rows -- the same vertical-nearest-
    anchor logic segment.py already relies on to keep Group-row content
    out of the Name field -- to pick out just the words assigned to the
    Group row, then checks each one overlaps *some* redaction rectangle.
    This is deliberately scoped to the Group row rather than every word in
    the header band: Date/Period's own (occasionally OCR-merged, printed-
    label-plus-handwritten-value) word boxes can be taller than the
    printed row itself, and a containment check across the whole band
    would flag those as false "leaks" even when nothing is actually
    exposed. Scoping to Group-row words sidesteps that entirely, since
    Date/Period words are assigned to the name/teacher buckets by the same
    row-assignment logic, never to "group".

    **Deliberately does NOT bound the word-collection window to the
    detected header border (`band.bottom`) the way an earlier version of
    this check did (fixed 2026-07-21, PRT packet 14, CLAUDE.md).** That
    version passed `band_bottom` through to `_assign_words_to_rows` so a
    packet's own row-value window anchored to the real, rasterized border
    instead of the self-relative row_height estimate whose own margin over
    real body text ranged from -10pt to +38pt across the real dataset (see
    ROW_ASSIGNMENT_BOTTOM_SLACK_PT in config.py) -- a real fix for a real
    false positive (SID 0204150202, the original Ganik incident packet).
    But it created a worse blind spot: real Group-row handwriting can
    overflow *downward*, past the header's own printed/detected bottom
    border, not just sideways past COLUMN_SPLIT_X (the class already
    covered by `redact_bboxes_for_band`'s second rectangle) -- confirmed
    on the real PRT file (packet 14: a group-member list that didn't fit
    the printed row's height, left fully legible below an otherwise
    correctly-drawn box). Anchoring this check's own word-window to
    `band.bottom` meant that overflow was excluded from `rows["group"]`
    *before* the coverage check ever ran, since the box and the check
    shared the exact same (in this case too-short) idea of where the
    header ends -- a check that can never disagree with the geometry it
    exists to verify isn't independent of it. There is also no safe fixed
    slack past `band.bottom` that could have caught this without
    reintroducing the Ganik-class false positive: measured on the real
    file, printed body text can start as little as +1.92pt below
    `band.bottom` on some pages (see CLAUDE.md), closer than most
    plausible handwriting-overflow allowances, so no fixed number
    separates "real overflow ink" from "safe printed text" reliably.

    Given that, this check now uses the full `HEADER_SEARCH_MAX_TOP` bound
    instead -- the same generous limit already used for *finding* labels in
    the first place, and the outer bound `header_words` was already
    fetched under (see `redact_packet`'s own `page_words` call), so this
    widens what counts as a candidate "group" word, not what gets read off
    the page at all. This does mean a page whose printed body text sits
    unusually close below the header (SID 0204150202/0204150203, ~4.8pt
    below `band.bottom`) will flag as held-back again -- a known, accepted
    false positive. A held-back false positive costs a human a few seconds
    in the manual-redaction queue (see pipeline.py's `MANUAL_QUEUE_DIR`); a
    silently shipped leak does not have a symmetric cost, so this trades
    toward the former on purpose -- see CLAUDE.md's "packet 14" section.
    """
    rows = _assign_words_to_rows(header_words, anchors, band_bottom=HEADER_SEARCH_MAX_TOP)
    return [w for w in rows["group"] if not (_overlaps_bbox(w, left_bbox) or _overlaps_bbox(w, right_bbox))]


def redact_packet(
    pdf_path: str | Path,
    packet: Packet,
    out_path: str | Path,
    *,
    dpi: int = RENDER_DPI_FINAL,
    flatten: bool = False,
    stamp_lines: list[str] | None = None,
    band_override: HeaderBand | None = None,
) -> RedactResult:
    """Produce a redacted single-packet PDF.

    The header page's Name/Teacher/Group column, plus the full-width
    Group-row-and-below overflow strip (see redact_bboxes_for_band), is
    destroyed with an opaque box -- the left one stamped with `stamp_lines`
    (the packet's re-identification key, e.g. ["SID: ...", "PD: ..."]);
    see `_draw_redaction_box`. Unless `flatten` is set, every word from
    `page_words` (the same native/OCR dispatch segment.py uses) is
    re-emitted as an invisible text token on its own page -- except words
    overlapping either redacted region, which are dropped before they ever
    reach the writer, not merely hidden visually. `flatten=True` reproduces
    the pre-reversal all-image behavior with no text layer at all, for any
    packet in the batch.

    `band_override`, when given, is used in place of `detect_header_band`'s
    own automatic detection -- the manual-redaction queue's release path
    (`pipeline.release_from_manual_queue`), for a human-supplied corrected
    geometry after an automated detection or coverage-check hold. Every
    downstream step (the two redaction rectangles, the coverage check
    against the *actual* header words) runs exactly the same way against
    an override band as against an auto-detected one -- the override
    changes where the geometry comes from, never what gets verified against
    it, since the coverage check must still have the final say (see
    CLAUDE.md's "the manual-redaction queue is a backstop" section).

    Packets missing a header page (`is_orphan`) have nothing to redact --
    `band`/`redact_bbox`/`redact_strip_bbox` come back None. This function
    is purely mechanical; callers must gate on `packet.issues` themselves
    before treating output as safe to release (see segment.py), and must
    treat a non-empty `RedactResult.uncovered_group_words` as a failure,
    the same way they already do verify_no_leaked_names findings.
    """
    with pdfplumber.open(pdf_path) as pdf:
        band: HeaderBand | None = None
        left_bbox: Bbox | None = None
        right_bbox: Bbox | None = None
        uncovered: list[Word] = []
        if packet.header_page_index is not None:
            header_page = pdf.pages[packet.header_page_index]
            header_image = header_page.to_image(resolution=dpi).original.convert("RGB")
            header_words = page_words(header_page, (0, 0, header_page.width, HEADER_SEARCH_MAX_TOP))
            anchors = locate_header_anchors(header_words)
            row_height = header_row_height(anchors)
            if band_override is not None:
                band = band_override
            else:
                band = detect_header_band(header_image, dpi=dpi, anchors=anchors, row_height=row_height)
            left_bbox, right_bbox = redact_bboxes_for_band(band, anchors.group_top)
            uncovered = find_uncovered_group_words(header_words, anchors, left_bbox, right_bbox)

        writer = _PdfWriter()
        for idx in packet.page_indices:
            page = pdf.pages[idx]
            image = page.to_image(resolution=dpi).original.convert("RGB")
            is_header = idx == packet.header_page_index
            if is_header and left_bbox is not None:
                _draw_redaction_box(image, left_bbox, dpi, stamp_lines)
                _draw_redaction_box(image, right_bbox, dpi, draw_stamp=False)

            if flatten:
                writer.add_page(image, [], page.width, page.height)
                continue

            words = page_words(page, (0, 0, page.width, page.height))
            if is_header and left_bbox is not None:
                words = [w for w in words if not (_overlaps_bbox(w, left_bbox) or _overlaps_bbox(w, right_bbox))]
            writer.add_page(image, words, page.width, page.height)

    writer.save(out_path)
    return RedactResult(
        out_path=Path(out_path),
        band=band,
        redact_bbox=left_bbox,
        redact_strip_bbox=right_bbox,
        flattened=flatten,
        uncovered_group_words=uncovered,
    )


_TOKEN_PATTERN = re.compile(r"[A-Za-z']+")


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_PATTERN.findall(text)}


@dataclass
class LeakFinding:
    page_index: int
    sid: str
    token: str
    exact: bool = True


def verify_no_leaked_names(pdf_path: str | Path, roster: Roster) -> list[LeakFinding]:
    """Full-document safety net for the kept-text-layer approach.

    Under the old flatten-to-image design, "the output has no text layer"
    was trivially true and trivially checkable. A surgical strip is a much
    weaker claim -- this proves it directly, by extracting text from every
    page of the *finished* file (not just the header band that was
    supposedly redacted) and checking it against the whole roster, not just
    the one student this packet was matched to. A leak anywhere -- wrong
    region redacted, a coordinate bug, a name elsewhere in the body -- shows
    up here regardless of which mechanism let it through.

    Tokens shorter than MIN_NAME_CHARS are skipped, same illegible-ink floor
    match.py applies, so a stray one-or-two-letter fragment (initials,
    OCR noise) doesn't manufacture false leak findings.

    Exact set-intersection alone isn't enough, though: the real "Ganik"
    leak (SID 0204150202, see CLAUDE.md) was OCR'd from the handwritten
    surname "Gonik" -- a real token, just not an *exact* match for
    anything on the roster, so Cmd+F and a plain set intersection both
    miss it even though the ink is fully legible to a human. A second,
    fuzzy pass (fuzz.ratio >= LEAK_FUZZY_MIN_RATIO, calibrated against
    that exact case) catches near-misses like it; only run against tokens
    that didn't already hit exactly, so a clean match isn't reported
    twice. `exact=False` on the finding says which pass caught it.

    The fuzzy pass additionally requires both tokens be at least
    LEAK_FUZZY_MIN_TOKEN_LEN long -- found the hard way, running this
    against the real file: every page's printed footer ("Page X of Y")
    fuzzy-matched roster first name "Paige" (fuzz.ratio("page","paige") ==
    88.9) on *every single page*, which makes the check fail everywhere
    and carry no signal at all. Short tokens are simply too likely to land
    within one edit of some short name by chance. This floor only applies
    to the fuzzy pass -- an exact match on a short token is still caught
    at MIN_NAME_CHARS.
    """
    roster_tokens: dict[str, set[str]] = {}
    for entry in roster:
        toks = {t for t in _tokenize(entry.first_name) | _tokenize(entry.last_name) if len(t) >= MIN_NAME_CHARS}
        if toks:
            roster_tokens[entry.sid] = toks

    findings: list[LeakFinding] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            page_tokens = {t for t in _tokenize(page.extract_text() or "") if len(t) >= MIN_NAME_CHARS}
            if not page_tokens:
                continue
            for sid, toks in roster_tokens.items():
                exact_hits = toks & page_tokens
                for hit in sorted(exact_hits):
                    findings.append(LeakFinding(page_index=page_index, sid=sid, token=hit))
                remaining = {t for t in page_tokens - exact_hits if len(t) >= LEAK_FUZZY_MIN_TOKEN_LEN}
                fuzzy_roster_toks = [t for t in sorted(toks) if len(t) >= LEAK_FUZZY_MIN_TOKEN_LEN]
                for roster_tok in fuzzy_roster_toks:
                    for page_tok in sorted(remaining):
                        if fuzz.ratio(roster_tok, page_tok) >= LEAK_FUZZY_MIN_RATIO:
                            findings.append(LeakFinding(page_index=page_index, sid=sid, token=page_tok, exact=False))
    return findings
