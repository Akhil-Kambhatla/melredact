"""Page parsing: footer reading, header-row anchoring, packet segmentation.

The footer's printed "Page X of Y" is the only ground truth for where one
student's packet ends and the next begins. Everything here is built around
that: page counts are read per packet, never assumed, and anything that
can't be read cleanly (an unreadable footer, a packet missing its first
page, a page count that doesn't add up) is recorded as an issue on the
packet rather than silently guessed or dropped. A packet with issues is
still returned -- it just isn't safe to redact/stamp/generate against until
a human has looked at it.

Row-anchoring here (locate_header_anchors / extract_header_fields) exists
for exactly one reason: only the Name row may reach the matcher. A roster
student named in someone else's Group row must never contribute to that
packet's score. Anchors are located dynamically per page (never fixed
coordinates), so this survives scan skew.

Word source is dispatched per page (see page_words): pdfplumber's native
text layer when a page has one, OCR (melredact.ocr) when it doesn't. Real
scans have no text layer at all -- the synthetic fixtures embed an
invisible one specifically so this module's logic can be tested without
needing OCR in the loop. The OCR import only happens on the path that
actually needs it, so fixture-based tests never pay for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

from melredact.config import (
    COLUMN_SPLIT_X,
    DATE_LABEL_WORDS,
    FOOTER_BAND_TOP,
    GROUP_ANCHOR,
    GROUP_ANCHOR_WORDS,
    GROUP_LABEL_WORDS,
    GROUP_ROW_BAND_SLACK_PT,
    HEADER_SEARCH_MAX_TOP,
    NAME_ANCHOR,
    NAME_ANCHOR_WORDS,
    NAME_LABEL_WORDS,
    PAGE_MARKER_PATTERN,
    PERIOD_LABEL_WORDS,
    ROW_ASSIGNMENT_BOTTOM_SLACK_PT,
    TEACHER_ANCHOR,
    TEACHER_ANCHOR_WORDS,
    TEACHER_LABEL_WORDS,
    WORKSHEET_TYPE_PATTERN,
)
from melredact.pdfio import open_pdf

Word = dict


@dataclass
class FooterInfo:
    page_num: int | None
    page_total: int | None
    raw_text: str
    readable: bool
    # Which worksheet this packet is (e.g. "PRT" vs "PCMEL_MPR_ADR"), read
    # from the same footer band as page_num/page_total, independently of
    # `readable` -- a page marker being unreadable and a worksheet-type
    # label being unreadable are separate failure modes, not one signal.
    # None means the label couldn't be parsed at all.
    worksheet_type: str | None = None


@dataclass
class HeaderAnchors:
    name_top: float
    teacher_top: float
    group_top: float
    name_found: bool
    teacher_found: bool
    group_found: bool


@dataclass
class HeaderFields:
    name_text: str
    teacher_text: str
    group_text: str
    date_text: str
    period_text: str
    anchors: HeaderAnchors


@dataclass
class Packet:
    packet_index: int
    page_indices: list[int]
    header_page_index: int | None
    declared_total: int | None
    is_orphan: bool  # missing its first (header) page
    # Read from the header page's own footer (see FooterInfo.worksheet_type)
    # -- None for an orphan (no header page to read it from) or a header
    # page whose worksheet-type label couldn't be parsed, either of which
    # already lands this packet in `issues` below.
    worksheet_type: str | None = None
    issues: list[str] = field(default_factory=list)

    @property
    def n_pages(self) -> int:
        return len(self.page_indices)


@dataclass
class SegmentResult:
    packets: list[Packet]
    page_count: int


def _normalize_word(text: str) -> str:
    return text.strip().lower()


def page_words(page: pdfplumber.page.Page, bbox: tuple[float, float, float, float]) -> list[Word]:
    """Word source for a page-point-space region: pdfplumber's native words
    if the page actually has a text layer, OCR otherwise. `page.chars` is a
    whole-page property (confirmed against real files: either a page has a
    text layer everywhere, from being born digital or previously OCR'd, or
    it has none at all, being a raw scan) so checking it once per page is
    enough to decide the source for every region on that page.

    The OCR path goes through ocr.cached_ocr_words_in_region, disk-cached
    per file-content+page+dpi+bbox -- so a region OCR'd once (by
    segmentation, field extraction, or a prior pipeline run) is never
    OCR'd again for the identical request (see ocr.py's docstring).

    Public: redact.py reuses this same dispatch to pull words across a
    whole page (not just the header/footer bands this module cares about)
    when building the kept OCR text layer for redacted output."""
    if page.chars:
        return page.crop(bbox).extract_words()
    from melredact import ocr

    return ocr.cached_ocr_words_in_region(page, bbox)


def _words_to_text(words: list[Word]) -> str:
    """Reconstruct a reading-order text blob from a word list, for regex
    matching against footer text. Whitespace-only join is enough here --
    PAGE_MARKER_PATTERN's \\s+ tolerates however words fall on lines."""
    return " ".join(w["text"] for w in sorted(words, key=lambda w: (w["top"], w["x0"])))


def _parse_worksheet_type(text: str) -> str | None:
    """Extract and normalize the printed worksheet-type label (e.g. "PRT
    (01/2024)", "pcMEL MPR+ADR (06/2025)") from the footer band's own text
    -- the same blob PAGE_MARKER_PATTERN searches, not a separate OCR call
    (see WORKSHEET_TYPE_PATTERN in config.py). The trailing "(mm/yyyy)"
    revision date is dropped; what's left is slugified into a directory-safe
    segment so distinct worksheet types (which otherwise share the same
    teacher_code/period, since both come from the SID) land in separate
    out/ subdirectories instead of colliding on the same <SID>.pdf path."""
    m = re.search(WORKSHEET_TYPE_PATTERN, text)
    if not m:
        return None
    slug = re.sub(r"[^A-Za-z0-9]+", "_", m.group(1).strip()).strip("_").upper()
    return slug or None


def read_footer(page: pdfplumber.page.Page) -> FooterInfo:
    """Read the printed 'Page X of Y' and worksheet-type label from the
    footer band only (not the whole page), so body text elsewhere can never
    be mistaken for either."""
    bbox = (0, FOOTER_BAND_TOP, page.width, page.height)
    if page.chars:
        text = page.crop(bbox).extract_text() or ""
    else:
        text = _words_to_text(page_words(page, bbox))
    matches = list(re.finditer(PAGE_MARKER_PATTERN, text))
    worksheet_type = _parse_worksheet_type(text)

    if len(matches) != 1:
        # Zero matches: unreadable. More than one: ambiguous. Neither is
        # something to guess through -- both mean "don't trust this".
        return FooterInfo(page_num=None, page_total=None, raw_text=text, readable=False, worksheet_type=worksheet_type)

    num, total = int(matches[0].group(1)), int(matches[0].group(2))
    readable = 1 <= num <= total
    if not readable:
        return FooterInfo(page_num=None, page_total=None, raw_text=text, readable=False, worksheet_type=worksheet_type)
    return FooterInfo(page_num=num, page_total=total, raw_text=text, readable=True, worksheet_type=worksheet_type)


def is_header_page(page: pdfplumber.page.Page) -> bool:
    """A page is a header page if the printed 'Name:' label is present in
    the upper portion of the page. This is the coarse structural signal
    used for packet segmentation -- fine-grained row anchoring happens
    separately in locate_header_anchors."""
    words = page_words(page, (0, 0, page.width, HEADER_SEARCH_MAX_TOP))
    for w in words:
        if w["top"] <= HEADER_SEARCH_MAX_TOP and _normalize_word(w["text"]) in NAME_ANCHOR_WORDS:
            return True
    return False


def _find_label_top(words: list[Word], anchor_words: set[str], fallback_top: float) -> tuple[float, bool]:
    candidates = [
        w for w in words if w["top"] <= HEADER_SEARCH_MAX_TOP and _normalize_word(w["text"]) in anchor_words
    ]
    if not candidates:
        return fallback_top, False
    return min(w["top"] for w in candidates), True


def locate_header_anchors(words: list[Word]) -> HeaderAnchors:
    """Find the actual on-page position of each row's printed label. Falls
    back to the measured config anchors (flagged via *_found=False) only if
    a label can't be found at all -- this should be rare, since these are
    printed, not handwritten, and OCR reads printed text reliably."""
    name_top, name_found = _find_label_top(words, NAME_ANCHOR_WORDS, NAME_ANCHOR["top"])
    teacher_top, teacher_found = _find_label_top(words, TEACHER_ANCHOR_WORDS, TEACHER_ANCHOR["top"])
    group_top, group_found = _find_label_top(words, GROUP_ANCHOR_WORDS, GROUP_ANCHOR["top"])
    return HeaderAnchors(
        name_top=name_top,
        teacher_top=teacher_top,
        group_top=group_top,
        name_found=name_found,
        teacher_found=teacher_found,
        group_found=group_found,
    )


def header_row_height(anchors: HeaderAnchors) -> float:
    """One row's worth of vertical space on *this* page, from this page's
    own located anchors -- not a fixed constant, so it survives a worksheet
    template whose rows are taller/shorter than MPR's. Shared by
    `_assign_words_to_rows` (bounding the group row's value-collection
    window) and `redact.detect_header_band` (bounding the anchor-relative
    border search) -- both need the same "how tall is one row here" measure.
    """
    row_height = anchors.group_top - anchors.teacher_top
    if row_height <= 0:
        row_height = anchors.teacher_top - anchors.name_top
    if row_height <= 0:
        row_height = GROUP_ANCHOR["top"] - TEACHER_ANCHOR["top"]
    return row_height


def _assign_words_to_rows(
    words: list[Word], anchors: HeaderAnchors, *, band_bottom: float | None = None
) -> dict[str, list[Word]]:
    """Assign each word within the header band to whichever row anchor it's
    vertically closest to. Not x-aware by design: Date/Period share
    Name/Teacher's row-tops, so this groups both columns of a row together.
    Column separation happens afterward in _row_text via x_min/x_max.

    Words outside the header band (the section title above it, the footer
    far below) are excluded entirely rather than assigned to the nearest
    anchor -- without this bound, three closely-clustered anchors all look
    "nearest" to something far away, and whichever anchor is at the extreme
    (topmost or bottommost) silently absorbs unrelated text.

    The bottom bound has two modes. Without `band_bottom` (segment.py's own
    matching/field-extraction callers, which never rasterize the page, so
    there's no detected border to anchor to): self-relative -- group_top
    plus one more row's worth of height (this page's own teacher-to-group
    spacing, not a fixed constant) plus ROW_ASSIGNMENT_BOTTOM_SLACK_PT --
    rather than reusing HEADER_SEARCH_MAX_TOP's wide, skew-tolerant slack.
    HEADER_SEARCH_MAX_TOP is generous on purpose for *finding a label*;
    reusing that same generous bound for *collecting a row's value words*
    is what let the printed numbered instruction directly below the header
    bleed into group_text on a real packet. This self-relative estimate's
    own margin over real body text turned out to range from -10pt to
    +38pt across the real dataset (see ROW_ASSIGNMENT_BOTTOM_SLACK_PT in
    config.py) -- good enough for group_text display, where the cost of
    getting it wrong is a messier field a reviewer can still see through,
    but not trustworthy enough for a security check.

    With `band_bottom` (the real, rasterized header border's own bottom
    edge -- see `redact.detect_header_band`, whose caller, `redact.
    find_uncovered_group_words`, always has this available): `band_bottom
    + GROUP_ROW_BAND_SLACK_PT`, a direct measurement instead of a proxy.
    This is the anchor-relative fix, mirroring how `detect_header_band`
    itself moved from a fixed search window to one centered on this page's
    own located anchors -- see GROUP_ROW_BAND_SLACK_PT in config.py for
    why its slack is deliberately small rather than a bigger constant.

    Neither bound can ever reach name_text, self-relative or band-
    anchored: name_top is always the row anchor farthest from anything
    below the header, so nearest-anchor assignment can't route there --
    but the group field a reviewer sees, and what the leak-check treats as
    "group ink," should both reflect only the group row, not a body-text
    fragment.
    """
    anchor_tops = {"name": anchors.name_top, "teacher": anchors.teacher_top, "group": anchors.group_top}
    window_min = min(anchor_tops.values()) - 20
    if band_bottom is not None:
        window_max = min(HEADER_SEARCH_MAX_TOP, band_bottom + GROUP_ROW_BAND_SLACK_PT)
    else:
        row_height = header_row_height(anchors)
        window_max = min(HEADER_SEARCH_MAX_TOP, anchors.group_top + row_height + ROW_ASSIGNMENT_BOTTOM_SLACK_PT)
    rows: dict[str, list[Word]] = {"name": [], "teacher": [], "group": []}
    for w in words:
        if not (window_min <= w["top"] <= window_max):
            continue
        nearest = min(anchor_tops, key=lambda k: abs(w["top"] - anchor_tops[k]))
        rows[nearest].append(w)
    return rows


def _row_text(
    words: list[Word],
    exclude: set[str],
    x_min: float | None = None,
    x_max: float | None = None,
) -> str:
    filtered = []
    for w in words:
        if _normalize_word(w["text"]) in exclude:
            continue
        if x_min is not None and w["x0"] < x_min:
            continue
        if x_max is not None and w["x0"] >= x_max:
            continue
        filtered.append(w)
    filtered.sort(key=lambda w: w["x0"])
    return " ".join(w["text"] for w in filtered)


def extract_header_fields(page: pdfplumber.page.Page) -> HeaderFields:
    """Split the header block into its five fields by anchor row, not by
    fixed coordinates. Only name_text should ever reach the matcher --
    group_text exists here purely so callers can confirm they're NOT using
    it, and so review_app.py can display it for context."""
    words = page_words(page, (0, 0, page.width, HEADER_SEARCH_MAX_TOP))
    anchors = locate_header_anchors(words)
    rows = _assign_words_to_rows(words, anchors)

    name_exclude = NAME_LABEL_WORDS | DATE_LABEL_WORDS
    teacher_exclude = TEACHER_LABEL_WORDS | PERIOD_LABEL_WORDS

    return HeaderFields(
        name_text=_row_text(rows["name"], name_exclude, x_max=COLUMN_SPLIT_X),
        teacher_text=_row_text(rows["teacher"], teacher_exclude, x_max=COLUMN_SPLIT_X),
        group_text=_row_text(rows["group"], GROUP_LABEL_WORDS),
        date_text=_row_text(rows["name"], name_exclude, x_min=COLUMN_SPLIT_X),
        period_text=_row_text(rows["teacher"], teacher_exclude, x_min=COLUMN_SPLIT_X),
        anchors=anchors,
    )


def segment_pdf(pdf_path: str | Path) -> SegmentResult:
    """Group pages into packets using the footer as ground truth. A header
    page always starts a new packet. A continuation-looking page with no
    open packet (missing page 1) becomes its own flagged, orphaned packet
    rather than being silently merged into whatever came before or dropped.
    Nothing here infers a page count that isn't printed on the page."""
    with open_pdf(pdf_path) as pdf:
        pages_info = [(idx, is_header_page(page), read_footer(page)) for idx, page in enumerate(pdf.pages)]

    packets: list[Packet] = []
    current: Packet | None = None
    packet_counter = 0

    def close_current() -> None:
        nonlocal current
        if current is None:
            return
        if current.declared_total is not None and current.n_pages != current.declared_total:
            current.issues.append(
                f"packet has {current.n_pages} page(s) but footer declared {current.declared_total}"
            )
        packets.append(current)
        current = None

    for idx, header, footer in pages_info:
        if header:
            close_current()
            packet_counter += 1
            current = Packet(
                packet_index=packet_counter,
                page_indices=[idx],
                header_page_index=idx,
                declared_total=footer.page_total if footer.readable else None,
                is_orphan=False,
                worksheet_type=footer.worksheet_type,
            )
            if not footer.readable:
                current.issues.append(f"page {idx}: header page footer unreadable, cannot verify page count")
            elif footer.page_num != 1:
                current.issues.append(
                    f"page {idx}: header page footer claims page {footer.page_num}, expected 1"
                )
            if footer.worksheet_type is None:
                current.issues.append(
                    f"page {idx}: header page footer worksheet type unreadable, cannot classify output"
                )
            continue

        if current is not None:
            # Does this page actually continue the open packet? Two
            # independent signals say no: the packet already has all the
            # pages its own footer declared, or this page's own footer
            # number doesn't match where it would fall in sequence. Either
            # one means this page belongs to something else -- close the
            # current packet as-is and let it fall through to become its
            # own (orphaned) packet below, rather than silently absorbing
            # a page that isn't actually part of it.
            expected_num = current.n_pages + 1
            already_complete = (
                current.declared_total is not None and expected_num > current.declared_total
            )
            sequence_break = footer.readable and footer.page_num != expected_num
            if already_complete or sequence_break:
                close_current()

        if current is None:
            packet_counter += 1
            current = Packet(
                packet_index=packet_counter,
                page_indices=[idx],
                header_page_index=None,
                declared_total=footer.page_total if footer.readable else None,
                is_orphan=True,
            )
            current.issues.append(f"page {idx}: continuation page with no preceding header (missing page 1)")
            if not footer.readable:
                current.issues.append(f"page {idx}: unreadable footer, cannot verify page count")
            continue

        current.page_indices.append(idx)
        if not footer.readable:
            current.issues.append(f"page {idx}: unreadable footer, cannot verify sequence")
            continue
        if footer.page_num != current.n_pages:
            current.issues.append(
                f"page {idx}: footer claims page {footer.page_num}, expected {current.n_pages} in sequence"
            )
        if current.declared_total is not None and footer.page_total != current.declared_total:
            current.issues.append(
                f"page {idx}: footer declares total {footer.page_total}, "
                f"packet started with total {current.declared_total}"
            )

    close_current()
    return SegmentResult(packets=packets, page_count=len(pages_info))
