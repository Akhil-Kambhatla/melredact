import numpy as np
import pdfplumber
import pytest
from PIL import Image, ImageDraw

from melredact.config import COLUMN_SPLIT_X, GROUP_ANCHOR, HEADER_BAND_FALLBACK, RENDER_DPI_PREVIEW
from melredact.redact import (
    HeaderBand,
    _draw_redaction_box,
    _invisible_text_op,
    _normalize_bbox,
    _overlaps_bbox,
    _pdf_baseline_y,
    _PdfWriter,
    detect_header_band,
    find_uncovered_group_words,
    redact_bbox_for_band,
    redact_packet,
    render_redaction_preview,
    render_region_preview,
    verify_no_leaked_names,
)
from melredact.roster import load_roster
from melredact.segment import Packet, segment_pdf
from tests.make_fixture import PACKETS, ROSTER, InvisibleText, PdfBuilder, build_main_fixture, render_header_image

DPI = RENDER_DPI_PREVIEW


@pytest.fixture(scope="module")
def main_fixture(tmp_path_factory):
    return build_main_fixture(tmp_path_factory.mktemp("redact_fixture"))


@pytest.fixture(scope="module")
def roster(main_fixture):
    return load_roster(main_fixture.roster_path)


@pytest.fixture(scope="module")
def segmented(main_fixture):
    return segment_pdf(main_fixture.pdf_path)


def _packet_by_tag(segmented, tag):
    return next(p for p, s in zip(segmented.packets, PACKETS) if s.tag == tag)


# --- Header band border detection ---


def test_detects_fixture_border_at_the_fallback_geometry(main_fixture):
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        header_page = pdf.pages[0]
        image = header_page.to_image(resolution=DPI).original.convert("RGB")
    band = detect_header_band(image, dpi=DPI)
    assert band.detected
    # the fixture draws its border at exactly HEADER_BAND_FALLBACK, so
    # detection should land within a couple points of it, not just fall
    # through to the fallback by coincidence
    assert abs(band.top - HEADER_BAND_FALLBACK["top"]) < 3
    assert abs(band.bottom - HEADER_BAND_FALLBACK["bottom"]) < 3
    assert abs(band.left - HEADER_BAND_FALLBACK["left"]) < 3
    assert abs(band.right - HEADER_BAND_FALLBACK["right"]) < 3


def _draw_tilted_header(*, drop_pt: float, title_gap_pt: float | None = None, body_gap_pt: float | None = None) -> tuple[Image.Image, tuple[float, float, float, float]]:
    """A synthetic header border tilted like a skewed real scan: the whole
    rectangle sheared so its right edge sits `drop_pt` lower than its left
    edge, both top and bottom rules moving together. Also draws a small
    ink blob inside the tilted box's Name row (its own y interpolated along
    the same tilt, so it always sits correctly inside the box regardless of
    drop), and returns its page-point bbox so a test can assert the
    computed redaction bbox actually covers it.

    `title_gap_pt`/`body_gap_pt`, when given, additionally draw title-like
    text just above the box and body-like text just below it, at the exact
    gaps measured off the real file (see BORDER_CORNER_SEARCH_SLACK_PT's
    comment in config.py) -- reproducing the two real false-positive modes
    detect_header_band was rewritten to avoid, not just the tilt itself.
    """
    scale = DPI / 72.0
    w, h = int(612 * scale), int(792 * scale)
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)

    fb = HEADER_BAND_FALLBACK
    left, right = fb["left"], fb["right"]
    top_l, top_r = fb["top"], fb["top"] + drop_pt
    bot_l, bot_r = fb["bottom"], fb["bottom"] + drop_pt

    def px(x, y):
        return (x * scale, y * scale)

    width = max(1, int(1.5 * scale))
    draw.line([px(left, top_l), px(right, top_r)], fill="black", width=width)
    draw.line([px(left, bot_l), px(right, bot_r)], fill="black", width=width)
    draw.line([px(left, top_l), px(left, bot_l)], fill="black", width=width)
    draw.line([px(right, top_r), px(right, bot_r)], fill="black", width=width)

    if title_gap_pt is not None:
        title_bottom = top_l - title_gap_pt
        draw.rectangle([px(left + 5, title_bottom - 10), px(left + 180, title_bottom)], fill="black")
        title_bottom_r = top_r - title_gap_pt
        draw.rectangle([px(right - 90, title_bottom_r - 10), px(right, title_bottom_r)], fill="black")
    if body_gap_pt is not None:
        body_top = bot_l + body_gap_pt
        draw.rectangle([px(left + 5, body_top), px(left + 250, body_top + 10)], fill="black")

    # Name-row ink: a filled blob standing in for handwriting, positioned a
    # few points inside the box below the tilted top edge, at an x-range
    # comfortably inside the left (name/teacher/group) column.
    ink_x0, ink_x1 = left + 60, left + 220
    frac0, frac1 = (ink_x0 - left) / (right - left), (ink_x1 - left) / (right - left)
    ink_top = top_l + frac0 * drop_pt + 8
    ink_bottom = ink_top + 10
    ink_top_r_edge = top_l + frac1 * drop_pt + 8
    draw.rectangle([px(ink_x0, ink_top), px(ink_x1, max(ink_bottom, ink_top_r_edge + 10))], fill="black")
    ink_bbox = (ink_x0, ink_top, ink_x1, max(ink_bottom, ink_top_r_edge + 10))
    return img, ink_bbox


@pytest.mark.parametrize("drop_pt", [0, 5, 10, 15, 20, 24])
def test_redaction_box_covers_name_ink_across_a_range_of_skews(drop_pt):
    """The user-reported bug: a skewed header's redaction box computed from
    a single global row scan could sit high/left of the actual tilted
    content. Proven here across a range of tilt magnitudes (0 to 24pt of
    vertical drop across the header's width -- comfortably past the ~15pt
    measured on the real skewed sample page), not just eyeballed on one
    real page."""
    img, ink_bbox = _draw_tilted_header(drop_pt=drop_pt)
    band = detect_header_band(img, dpi=DPI)
    assert band.detected, drop_pt
    bbox = redact_bbox_for_band(band)
    left, top, right, bottom = bbox
    ink_left, ink_top, ink_right, ink_bottom = ink_bbox
    assert left <= ink_left and top <= ink_top and right >= ink_right and bottom >= ink_bottom, (
        drop_pt,
        bbox,
        ink_bbox,
    )


def test_border_detection_is_not_fooled_by_title_above_or_body_below_under_skew():
    """Regression for the real-file failure modes detect_header_band was
    rewritten around: a section title sitting ~5pt above the box, and body
    text starting ~24pt below it (both measured off the real file), must
    not be mistaken for the tilted border itself."""
    img, ink_bbox = _draw_tilted_header(drop_pt=15, title_gap_pt=5, body_gap_pt=24)
    band = detect_header_band(img, dpi=DPI)
    assert band.detected
    bbox = redact_bbox_for_band(band)
    left, top, right, bottom = bbox
    ink_left, ink_top, ink_right, ink_bottom = ink_bbox
    assert left <= ink_left and top <= ink_top and right >= ink_right and bottom >= ink_bottom
    # and it shouldn't have ballooned to swallow the title/body decoys either
    assert top > HEADER_BAND_FALLBACK["top"] - 5 - 3  # well short of the title
    assert bottom < HEADER_BAND_FALLBACK["bottom"] + 15 + 24 - 3  # well short of the body text


def test_no_border_at_all_falls_back_cleanly():
    blank = Image.new("RGB", (int(612 * DPI / 72), int(792 * DPI / 72)), "white")
    band = detect_header_band(blank, dpi=DPI)
    assert not band.detected
    assert band.top == HEADER_BAND_FALLBACK["top"]
    assert band.bottom == HEADER_BAND_FALLBACK["bottom"]
    assert band.left == HEADER_BAND_FALLBACK["left"]
    assert band.right == HEADER_BAND_FALLBACK["right"]


def test_fallback_is_a_floor_not_a_ceiling_when_detected_band_is_larger():
    """A border drawn bigger than the measured fallback must be honored,
    not clamped down to the fallback numbers.

    Delta is kept inside BORDER_CORNER_SEARCH_SLACK_PT (top/bottom are now
    read off the left/right columns' own vertical extent, searched only in
    a tight band around the fallback's own top/bottom -- see
    detect_header_band's docstring for why that band is deliberately much
    tighter than the left/right column search)."""
    scale = DPI / 72.0
    w, h = int(612 * scale), int(792 * scale)
    img = Image.new("RGB", (w, h), "white")
    delta = 6
    big = {"top": HEADER_BAND_FALLBACK["top"] - delta, "bottom": HEADER_BAND_FALLBACK["bottom"] + delta,
           "left": HEADER_BAND_FALLBACK["left"] - delta, "right": HEADER_BAND_FALLBACK["right"] + delta}
    arr = np.array(img)
    t, b = int(big["top"] * scale), int(big["bottom"] * scale)
    l, r = int(big["left"] * scale), int(big["right"] * scale)
    arr[t : t + 2, l:r] = 0
    arr[b - 2 : b, l:r] = 0
    arr[t:b, l : l + 2] = 0
    arr[t:b, r - 2 : r] = 0
    img = Image.fromarray(arr)

    band = detect_header_band(img, dpi=DPI)
    assert band.detected
    assert band.top < HEADER_BAND_FALLBACK["top"]
    assert band.bottom > HEADER_BAND_FALLBACK["bottom"]
    assert band.left < HEADER_BAND_FALLBACK["left"]
    assert band.right > HEADER_BAND_FALLBACK["right"]


def test_redact_bbox_never_crosses_column_split():
    band = HeaderBand(left=30, top=50, right=580, bottom=150, detected=True)
    bbox = redact_bbox_for_band(band)
    assert bbox[2] == COLUMN_SPLIT_X  # clamped, not the band's own (further right) edge


def test_overlaps_bbox_basic_cases():
    bbox = (10, 10, 100, 100)
    inside = {"x0": 20, "x1": 30, "top": 20, "bottom": 30}
    outside_right = {"x0": 200, "x1": 210, "top": 20, "bottom": 30}
    outside_below = {"x0": 20, "x1": 30, "top": 200, "bottom": 210}
    touching_edge = {"x0": 100, "x1": 110, "top": 20, "bottom": 30}  # x0 == right edge
    assert _overlaps_bbox(inside, bbox)
    assert not _overlaps_bbox(outside_right, bbox)
    assert not _overlaps_bbox(outside_below, bbox)
    assert not _overlaps_bbox(touching_edge, bbox)


# --- Coordinate flip: PDF content stream is bottom-left origin, everything
# else in this module is top-down. This is verified by writing a word
# through the real writer and reading it back with pdfplumber's real
# extract_words(), not by re-deriving the flip formula in the test. ---


def test_coordinate_flip_round_trips_through_real_writer_and_reader(tmp_path):
    page_w, page_h = 612.0, 792.0
    word = {"text": "Roundtrip", "x0": 123.4, "x1": 220.0, "top": 88.0, "bottom": 100.0}

    writer = _PdfWriter()
    blank = Image.new("RGB", (int(page_w), int(page_h)), "white")
    writer.add_page(blank, [word], page_w, page_h)
    out = tmp_path / "roundtrip.pdf"
    writer.save(out)

    with pdfplumber.open(out) as pdf:
        words = pdf.pages[0].extract_words()

    assert len(words) == 1
    got = words[0]
    assert got["text"] == "Roundtrip"
    # font-size/baseline approximation introduces a little slop; the flip
    # itself being wrong would be off by ~page_height (700+pt), not a
    # handful of points
    assert abs(got["top"] - word["top"]) < 5
    assert abs(got["x0"] - word["x0"]) < 2


def test_pdf_baseline_y_is_not_an_identity_function():
    """A regression guard against the flip silently degrading into a no-op
    (e.g. someone "simplifying" _pdf_baseline_y to `return word["top"]`)."""
    word = {"top": 88.0, "bottom": 100.0}
    y = _pdf_baseline_y(word, page_height_pt=792.0)
    assert y != word["top"]
    assert y == pytest.approx(792.0 - 88.0 - 12.0 * 0.8)


def test_invisible_text_op_uses_text_render_mode_3():
    word = {"text": "x", "x0": 0.0, "x1": 5.0, "top": 0.0, "bottom": 10.0}
    op = _invisible_text_op(word, page_height_pt=792.0)
    assert b"3 Tr" in op


def test_adjacent_words_with_tight_ocr_boxes_do_not_overlap_and_read_back_in_order(tmp_path):
    """Regression for a real leak-check-corrupting bug found on the actual
    PRT file: real OCR word boxes are measured to the ink, often much
    narrower than Helvetica's own advance width for that text at the font
    size `_font_size_for_word` derives from box height (e.g. real PRT data:
    "A" boxed at x0=40.32/x1=42.48 -- 2.16pt wide -- but Helvetica renders
    "A" at ~10pt for that word's height-derived size). Unscaled, each word's
    Tj run advances well past where the *next* word's own x0 places it, so
    the two overlap in text-space; pdfplumber's word-clustering (which
    groups characters by x-proximity, not by which Tj call produced them)
    then interleaves the overlapping runs into a single garbled token on
    read-back -- this is exactly what turned the real file's clean "A
    Plausibility Ranking Task" into "A PlausibilRitya nkinTga sk" and what
    let `verify_no_leaked_names` see corrupted, name-like fragments it had
    never actually been asked to check. Reproduced here with a fixture
    narrow enough to trigger the same overlap without needing the real
    scan."""
    words = [
        {"text": "A", "x0": 40.32, "x1": 42.48, "top": 26.16, "bottom": 41.28},
        {"text": "Plausibility", "x0": 54.96, "x1": 101.04, "top": 26.16, "bottom": 41.28},
        {"text": "Ranking", "x0": 109.44, "x1": 145.2, "top": 26.16, "bottom": 41.28},
        {"text": "Task", "x0": 153.6, "x1": 170.4, "top": 26.16, "bottom": 41.28},
    ]
    page_w, page_h = 612.0, 792.0
    writer = _PdfWriter()
    blank = Image.new("RGB", (int(page_w), int(page_h)), "white")
    writer.add_page(blank, words, page_w, page_h)
    out = tmp_path / "tight_boxes.pdf"
    writer.save(out)

    with pdfplumber.open(out) as pdf:
        text = pdf.pages[0].extract_text() or ""
        got_words = [w["text"] for w in pdf.pages[0].extract_words()]

    assert got_words == ["A", "Plausibility", "Ranking", "Task"]
    assert "A Plausibility Ranking Task" in text
    assert "PlausibilRitya" not in text


# --- Pixel redaction + text-layer stripping, via the real redact_packet ---


@pytest.fixture(scope="module")
def redacted_clean_match(main_fixture, segmented, tmp_path_factory):
    packet = _packet_by_tag(segmented, "clean_match")
    out = tmp_path_factory.mktemp("redacted") / "clean_match.pdf"
    result = redact_packet(main_fixture.pdf_path, packet, out, dpi=DPI)
    return packet, result


def test_header_ink_region_is_painted_opaque(redacted_clean_match):
    _, result = redacted_clean_match
    with pdfplumber.open(result.out_path) as pdf:
        image = pdf.pages[0].to_image(resolution=DPI).original.convert("RGB")
    scale = DPI / 72.0
    left, top, right, bottom = result.redact_bbox
    arr = np.array(image)
    # sample well inside the box, away from its own border pixels
    region = arr[int((top + 3) * scale) : int((bottom - 3) * scale), int((left + 3) * scale) : int((right - 3) * scale)]
    assert region.mean() < 10  # solid black fill


# --- SID/PD re-identification stamp ---


def test_stamp_defaults_to_redacted_text_when_no_sid_given(redacted_clean_match):
    """Without an explicit stamp_lines (e.g. a bare library call with no
    decision behind it), the box still gets *some* visible stamp -- a
    reviewer must never see a coincidentally-blank-looking field."""
    _, result = redacted_clean_match
    with pdfplumber.open(result.out_path) as pdf:
        image = pdf.pages[0].to_image(resolution=DPI).original.convert("RGB")
    scale = DPI / 72.0
    left, top, right, bottom = result.redact_bbox
    arr = np.array(image)
    region = arr[int(top * scale) : int(bottom * scale), int(left * scale) : int(right * scale)]
    assert (region > 200).any()  # some white stamp pixels present


def test_sid_pd_stamp_is_rendered_left_aligned_inside_the_box(main_fixture, segmented, tmp_path):
    """The actual re-identification stamp: 'SID: <sid>' then 'PD: <period>'
    on their own lines, on the left side of the box (matching what John
    was told) -- not centered, and not the generic REDACTED placeholder."""
    packet = _packet_by_tag(segmented, "clean_match")
    out = tmp_path / "stamped.pdf"
    result = redact_packet(
        main_fixture.pdf_path, packet, out, dpi=DPI, stamp_lines=["SID: 0204150204", "PD: 02"]
    )
    with pdfplumber.open(result.out_path) as pdf:
        image = pdf.pages[0].to_image(resolution=DPI).original.convert("RGB")
    scale = DPI / 72.0
    left, top, right, bottom = result.redact_bbox
    arr = np.array(image)

    # Left third of the box has stamp text (white pixels); the right two
    # thirds -- where a centered single line would have sat -- must not be
    # the only place text shows up. Confirms left alignment, not centering.
    box_w = right - left
    left_third = arr[int(top * scale) : int(bottom * scale), int(left * scale) : int((left + box_w / 3) * scale)]
    assert (left_third > 200).any()

    # Two distinct stamped lines: a horizontal white-pixel band near the
    # top of the box, a gap, then a second band -- not one centered blob.
    col_band = arr[
        int(top * scale) : int(bottom * scale), int((left + 8) * scale) : int((left + 60) * scale)
    ]
    row_has_white = (col_band > 200).any(axis=(1, 2))
    # collapse into contiguous runs of "has white text"
    runs = 0
    prev = False
    for v in row_has_white:
        if v and not prev:
            runs += 1
        prev = v
    assert runs >= 2, "expected at least two separate stamped lines"


def test_date_period_column_is_untouched(redacted_clean_match, main_fixture):
    """Only the *upper* part of the right column -- where Date/Period's
    own values sit, above the overflow strip -- must survive untouched.
    The strip itself (group row height and below) is deliberately new
    territory now; see test_group_row_overflow_strip_is_painted below."""
    _, result = redacted_clean_match
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        before = pdf.pages[0].to_image(resolution=DPI).original.convert("RGB")
    with pdfplumber.open(result.out_path) as pdf:
        after = pdf.pages[0].to_image(resolution=DPI).original.convert("RGB")
    scale = DPI / 72.0
    x0 = int((COLUMN_SPLIT_X + 5) * scale)
    x1 = int((HEADER_BAND_FALLBACK["right"] - 2) * scale)
    y0 = int((HEADER_BAND_FALLBACK["top"] + 2) * scale)
    y1 = int((result.redact_strip_bbox[1] - 2) * scale)  # up to just above the strip's own top
    before_arr = np.array(before)[y0:y1, x0:x1]
    after_arr = np.array(after)[y0:y1, x0:x1]
    assert np.array_equal(before_arr, after_arr)


def test_group_row_overflow_strip_is_painted(redacted_clean_match):
    """The new full-width strip (group row height and below, right column)
    must actually be painted opaque -- the other half of the leak fix
    alongside test_date_period_column_is_untouched keeping the row above
    it untouched."""
    _, result = redacted_clean_match
    with pdfplumber.open(result.out_path) as pdf:
        image = pdf.pages[0].to_image(resolution=DPI).original.convert("RGB")
    scale = DPI / 72.0
    left, top, right, bottom = result.redact_strip_bbox
    arr = np.array(image)
    region = arr[int((top + 2) * scale) : int((bottom - 2) * scale), int((left + 2) * scale) : int((right - 2) * scale)]
    assert region.mean() < 10


def test_name_and_teacher_words_are_dropped_from_text_layer(redacted_clean_match):
    _, result = redacted_clean_match
    with pdfplumber.open(result.out_path) as pdf:
        text = pdf.pages[0].extract_text() or ""
    assert "Jordan" not in text
    assert "Ames" not in text
    assert "Hannel" not in text


def test_date_and_period_words_survive_in_text_layer(redacted_clean_match):
    _, result = redacted_clean_match
    with pdfplumber.open(result.out_path) as pdf:
        text = pdf.pages[0].extract_text() or ""
    assert "10/03/2025" in text
    assert "02" in text


def test_continuation_page_text_layer_is_kept(redacted_clean_match):
    _, result = redacted_clean_match
    with pdfplumber.open(result.out_path) as pdf:
        words = {w["text"] for w in pdf.pages[1].extract_words()}
    assert "continued," in words


def test_flatten_flag_produces_zero_text_layer_on_every_page(main_fixture, segmented, tmp_path):
    packet = _packet_by_tag(segmented, "clean_match")
    out = tmp_path / "flattened.pdf"
    redact_packet(main_fixture.pdf_path, packet, out, dpi=DPI, flatten=True)
    with pdfplumber.open(out) as pdf:
        for page in pdf.pages:
            assert page.chars == []
            assert (page.extract_text() or "") == ""


def test_group_row_name_is_also_stripped(main_fixture, segmented, tmp_path):
    """Shaw/Nuzhat-style trap: Nadia's name sits in the group row, inside
    the same redacted left column, and must not survive either."""
    packet = _packet_by_tag(segmented, "group_row_trap")
    out = tmp_path / "group_row_trap.pdf"
    result = redact_packet(main_fixture.pdf_path, packet, out, dpi=DPI)
    with pdfplumber.open(result.out_path) as pdf:
        text = pdf.pages[0].extract_text() or ""
    assert "Nadia" not in text
    assert "Casey" not in text
    assert "Shaw" not in text


def test_blank_rows_still_get_the_full_band_destroyed(main_fixture, segmented, tmp_path):
    packet = _packet_by_tag(segmented, "blank_rows_leak")
    out = tmp_path / "blank_rows.pdf"
    result = redact_packet(main_fixture.pdf_path, packet, out, dpi=DPI)
    with pdfplumber.open(result.out_path) as pdf:
        text = pdf.pages[0].extract_text() or ""
    assert "Morgan" not in text
    assert "Lee" not in text


def test_group_row_overflow_past_column_split_is_fully_redacted(tmp_path):
    """Regression for the real leak on SID 0204150202: a group-members
    list handwritten wide enough to run past COLUMN_SPLIT_X ("King, Sfoh,
    Braydeh, Ganik" -- the last name landing entirely in the Date/Period
    column) must be fully redacted, not just the portion left of the
    split. Built as a standalone one-page fixture (not through PACKETS)
    so the long group value's exact word positions can be pinned down and
    asserted on directly, mirroring the real file: the last word here
    ("Ganiktest") lands entirely to the right of COLUMN_SPLIT_X, same as
    "Ganik" did.
    """
    long_group_text = "Aaaaaaaaaa Bbbbbbbbbb Cccccccccc Ddddddddddd Eeeeeeeeeee Ganiktest"
    img = render_header_image(
        name_text="Alex Rivera",
        teacher_text="Hannel",
        group_text=long_group_text,
        date_text="10/03/2025",
        period_text="02",
        worksheet_type="pcMEL MPR+ADR (06/2025)",
        page_marker="Page 1 of 1",
        shade_blank_rows=False,
    )
    items = [
        InvisibleText("Name:", GROUP_ANCHOR["x0"], 68, 9),
        InvisibleText("Alex Rivera", 150, 68),
        InvisibleText("Group members, if any:", GROUP_ANCHOR["x0"], GROUP_ANCHOR["top"], 9),
        InvisibleText(long_group_text, 150, GROUP_ANCHOR["top"]),
        InvisibleText("10/03/2025", 450, 68),
        InvisibleText("02", 450, 87),
        InvisibleText("Page 1 of 1", 513, 747, 9),
    ]
    builder = PdfBuilder()
    builder.add_page(img, items)
    pdf_path = tmp_path / "overflow.pdf"
    builder.save(pdf_path)

    packet = Packet(packet_index=1, page_indices=[0], header_page_index=0, declared_total=1, is_orphan=False, issues=[])
    out = tmp_path / "overflow_redacted.pdf"
    result = redact_packet(pdf_path, packet, out, dpi=DPI)

    assert result.uncovered_group_words == [], result.uncovered_group_words
    with pdfplumber.open(out) as pdf:
        text = pdf.pages[0].extract_text() or ""
    for word in long_group_text.split():
        assert word not in text, word
    # Date/Period, sitting above the overflow strip, must still survive.
    assert "10/03/2025" in text
    assert "02" in text


def test_group_row_overflow_is_covered_when_header_block_sits_lower_on_the_page(tmp_path):
    """Regression for a second real worksheet type, PRT: its title+subtitle
    is taller than MPR's, so the *same* bordered block -- same printed
    labels, same internal row spacing -- sits ~37-44pt further down the
    real page than it does on MPR. Before detect_header_band became
    anchor-relative (searching around this page's own located labels
    instead of a fixed absolute position), this pushed the detected box up
    into blank/title space and left it short of the true bottom border,
    shipping a real file (SID 0204150201) with "Group members, if any:
    Asael, Chevron, Deehiya" fully legible below an incorrectly-placed box.

    Built with the same overflow-past-COLUMN_SPLIT_X shape as the sibling
    MPR-shaped test above, just at `block_offset_pt=40` (comfortably past
    the real 37-44pt shift) -- the group row must still end up fully
    covered, and the box must not have crept up into the title/blank space
    well above the block.
    """
    block_offset_pt = 40.0
    long_group_text = "Aaaaaaaaaa Bbbbbbbbbb Cccccccccc Ddddddddddd Eeeeeeeeeee Ganiktest"
    img = render_header_image(
        name_text="Alex Rivera",
        teacher_text="Hannel",
        group_text=long_group_text,
        date_text="10/03/2025",
        period_text="02",
        worksheet_type="PRT (01/2024)",
        page_marker="Page 1 of 1",
        shade_blank_rows=False,
        block_offset_pt=block_offset_pt,
    )
    items = [
        InvisibleText("Name:", GROUP_ANCHOR["x0"], 68 + block_offset_pt, 9),
        InvisibleText("Alex Rivera", 150, 68 + block_offset_pt),
        InvisibleText("Teacher:", GROUP_ANCHOR["x0"], 87 + block_offset_pt, 9),
        InvisibleText("Hannel", 150, 87 + block_offset_pt),
        InvisibleText("Group members, if any:", GROUP_ANCHOR["x0"], GROUP_ANCHOR["top"] + block_offset_pt, 9),
        InvisibleText(long_group_text, 150, GROUP_ANCHOR["top"] + block_offset_pt),
        InvisibleText("10/03/2025", 450, 68 + block_offset_pt),
        InvisibleText("02", 450, 87 + block_offset_pt),
        InvisibleText("Page 1 of 1", 513, 747, 9),
    ]
    builder = PdfBuilder()
    builder.add_page(img, items)
    pdf_path = tmp_path / "prt_shaped_overflow.pdf"
    builder.save(pdf_path)

    packet = Packet(packet_index=1, page_indices=[0], header_page_index=0, declared_total=1, is_orphan=False, issues=[])
    out = tmp_path / "prt_shaped_overflow_redacted.pdf"
    result = redact_packet(pdf_path, packet, out, dpi=DPI)

    assert result.band is not None and result.band.detected, result.band
    # Must not have been clamped up toward the (unshifted) MPR fallback --
    # the box has to follow the block down the page, not sit near the top.
    assert result.band.top > HEADER_BAND_FALLBACK["top"] + block_offset_pt - 15, result.band

    assert result.uncovered_group_words == [], result.uncovered_group_words
    with pdfplumber.open(out) as pdf:
        text = pdf.pages[0].extract_text() or ""
    for word in long_group_text.split():
        assert word not in text, word
    # Date/Period, sitting above the overflow strip, must still survive.
    assert "10/03/2025" in text
    assert "02" in text


def test_find_uncovered_group_words_actually_catches_a_miss():
    """Proves find_uncovered_group_words has teeth: a Group-row word that
    sits outside both redaction rectangles must be reported, not silently
    passed -- built directly against the function rather than through a
    full redact_packet call, to isolate the check itself."""
    from melredact.segment import HeaderAnchors

    anchors = HeaderAnchors(
        name_top=68, teacher_top=87, group_top=111, name_found=True, teacher_found=True, group_found=True
    )
    left_bbox = (38, 54, 400, 148)
    # Deliberately narrower than the page (right edge at 450, not band.right)
    # to simulate a coverage bug: this word sits in the group row's own
    # vertical window (nearest anchor is group_top=111) but past both
    # rectangles' right edges.
    right_bbox = (400, 113, 450, 148)
    escaped_word = {"text": "Ganik", "x0": 460.0, "x1": 500.0, "top": 118.0, "bottom": 128.0}
    header_words = [
        {"text": "Group", "x0": 46.0, "x1": 71.0, "top": 111.0, "bottom": 120.0},
        escaped_word,
    ]
    escaping = find_uncovered_group_words(header_words, anchors, [left_bbox, right_bbox])
    assert escaped_word in escaping


def test_group_row_vertical_overflow_below_the_header_border_is_not_silently_missed(tmp_path):
    """Regression for a real leak (PRT packet 14, CLAUDE.md): Group-row
    handwriting can overflow *downward*, past the header's own printed/
    detected bottom border, not just sideways past COLUMN_SPLIT_X (the
    class test_group_row_overflow_past_column_split_is_fully_redacted
    already covers). Before this fix, find_uncovered_group_words bounded
    its own word-collection window at band_bottom + GROUP_ROW_BAND_SLACK_PT
    (1.5pt), so ink genuinely below the detected border never entered the
    check as a candidate "group" word at all -- neither redaction
    rectangle covers anything below band.bottom either, so this class of
    overflow shipped fully legible with the check reporting nothing wrong.
    Fictional names stand in for the real ones (never commit real student
    PII -- see CLAUDE.md/data/README.md)."""
    overflow_text = "Priya Xavier Noor"
    img = render_header_image(
        name_text="Alex Rivera",
        teacher_text="Hannel",
        group_text="",  # drawn via the invisible layer below, not here
        date_text="10/03/2025",
        period_text="02",
        worksheet_type="PRT (01/2024)",
        page_marker="Page 1 of 1",
        shade_blank_rows=False,
    )
    overflow_top_pt = HEADER_BAND_FALLBACK["bottom"] + 12  # past the border, short of the printed instruction at 170
    items = [
        InvisibleText("Name:", GROUP_ANCHOR["x0"], 68, 9),
        InvisibleText("Alex Rivera", 150, 68),
        InvisibleText("Group members, if any:", GROUP_ANCHOR["x0"], GROUP_ANCHOR["top"], 9),
        InvisibleText(overflow_text, 150, overflow_top_pt),
        InvisibleText("10/03/2025", 450, 68),
        InvisibleText("02", 450, 87),
        InvisibleText("Page 1 of 1", 513, 747, 9),
    ]
    builder = PdfBuilder()
    builder.add_page(img, items)
    pdf_path = tmp_path / "vertical_overflow.pdf"
    builder.save(pdf_path)

    packet = Packet(packet_index=1, page_indices=[0], header_page_index=0, declared_total=1, is_orphan=False, issues=[])
    out = tmp_path / "vertical_overflow_redacted.pdf"
    result = redact_packet(pdf_path, packet, out, dpi=DPI)

    assert result.band is not None and result.band.detected, result.band
    assert result.uncovered_group_words, "vertical overflow past the border must be flagged, not silently missed"
    escaped = " ".join(w["text"] for w in result.uncovered_group_words)
    assert any(name in escaped for name in ("Priya", "Xavier", "Noor"))


# --- Full-document verify pass ---


def test_verify_no_leaked_names_is_clean_across_every_fixture_packet(main_fixture, roster, segmented, tmp_path):
    for packet, spec in zip(segmented.packets, PACKETS):
        out = tmp_path / f"{spec.tag}.pdf"
        result = redact_packet(main_fixture.pdf_path, packet, out, dpi=DPI)
        findings = verify_no_leaked_names(result.out_path, roster)
        assert findings == [], (spec.tag, findings)


def test_verify_no_leaked_names_checks_every_page_not_just_the_header(main_fixture, roster, segmented, tmp_path):
    packet = _packet_by_tag(segmented, "variable_length")  # 3 pages
    out = tmp_path / "variable_length.pdf"
    result = redact_packet(main_fixture.pdf_path, packet, out, dpi=DPI)
    with pdfplumber.open(result.out_path) as pdf:
        assert len(pdf.pages) == 3
    findings = verify_no_leaked_names(result.out_path, roster)
    assert findings == []


def test_verify_no_leaked_names_actually_catches_a_leak(tmp_path):
    """Proves the check has teeth: if a name-bearing word slips past
    filtering for any reason, verify_no_leaked_names must flag it -- this
    builds a page directly with the writer, bypassing redact_packet's own
    filtering, to simulate exactly that failure mode."""
    page_w, page_h = 612.0, 792.0
    leaked_word = {"text": "Ames", "x0": 150.0, "x1": 190.0, "top": 70.0, "bottom": 82.0}
    writer = _PdfWriter()
    writer.add_page(Image.new("RGB", (int(page_w), int(page_h)), "white"), [leaked_word], page_w, page_h)
    out = tmp_path / "leaky.pdf"
    writer.save(out)

    roster_csv = tmp_path / "roster.csv"
    with roster_csv.open("w", newline="") as f:
        f.write("SID,Last Name,First Name\n")
        f.write(f"{ROSTER[0][0]},{ROSTER[0][1]},{ROSTER[0][2]}\n")

    r = load_roster(roster_csv)
    findings = verify_no_leaked_names(out, r)
    assert len(findings) == 1
    assert findings[0].token == "ames"
    assert findings[0].exact


def test_verify_no_leaked_names_catches_ocr_garbled_near_miss(tmp_path):
    """Regression for the real leak (SID 0204150202): OCR read the
    handwritten roster surname "Gonik" as "Ganik" -- a real word, just not
    an *exact* match for anything on the roster, so a plain set
    intersection (and Cmd+F) both miss it even though the ink is fully
    legible. verify_no_leaked_names's fuzzy pass must still catch it."""
    page_w, page_h = 612.0, 792.0
    garbled_word = {"text": "Ganik", "x0": 460.0, "x1": 500.0, "top": 118.0, "bottom": 128.0}
    writer = _PdfWriter()
    writer.add_page(Image.new("RGB", (int(page_w), int(page_h)), "white"), [garbled_word], page_w, page_h)
    out = tmp_path / "garbled.pdf"
    writer.save(out)

    roster_csv = tmp_path / "roster.csv"
    with roster_csv.open("w", newline="") as f:
        f.write("SID,Last Name,First Name\n")
        f.write("0204150203,Salla,Gonik\n")

    r = load_roster(roster_csv)
    findings = verify_no_leaked_names(out, r)
    assert len(findings) == 1
    assert findings[0].token == "ganik"
    assert not findings[0].exact


# --- Inverted-coordinate boxes (2026-08-15 real-reviewer-reported crash) ---
#
# A manually resized box (review_app.py's drag-corner editor) can arrive
# with its two corners in the "wrong" order -- dragging a resize handle
# past the object's opposite handle flips the rectangle, reporting a
# negative scaleX/scaleY rather than a re-normalized positive one. PIL's
# ImageDraw.rectangle raises ValueError on inverted coordinates rather than
# silently swapping them, which is what a reviewer saw as an intermittent
# crash while resizing boxes. _normalize_bbox is the fix; these tests prove
# it doesn't just avoid a crash but also doesn't change *where* anything
# gets redacted.


@pytest.mark.parametrize(
    "corners",
    [
        (38.0, 58.0, 400.0, 148.0),  # ordinary, already-normalized
        (400.0, 58.0, 38.0, 148.0),  # left/right flipped (negative scaleX)
        (38.0, 148.0, 400.0, 58.0),  # top/bottom flipped (negative scaleY)
        (400.0, 148.0, 38.0, 58.0),  # both flipped (180-degree drag)
    ],
    ids=["normal", "flip-x", "flip-y", "flip-both"],
)
def test_normalize_bbox_handles_every_corner_order(corners):
    """Regardless of which order the two corners arrive in, _normalize_bbox
    must recover the exact same (left, top, right, bottom) rectangle --
    same absolute region, not just "some" ordered box."""
    assert _normalize_bbox(corners) == (38.0, 58.0, 400.0, 148.0)


def test_draw_redaction_box_does_not_crash_on_inverted_coordinates_and_paints_the_same_region():
    """A box handed to _draw_redaction_box with inverted corners must not
    raise (the real crash: PIL's ImageDraw.rectangle on right<left or
    bottom<top), and must paint the *identical* region a pre-normalized
    call would -- proof this is a real fix, not just a swallowed error."""
    dpi = DPI
    normal_bbox = (38.0, 58.0, 400.0, 148.0)
    inverted_bbox = (400.0, 148.0, 38.0, 58.0)  # both corners flipped

    img_normal = Image.new("RGB", (600, 800), "white")
    _draw_redaction_box(img_normal, normal_bbox, dpi, draw_stamp=False)

    img_inverted = Image.new("RGB", (600, 800), "white")
    _draw_redaction_box(img_inverted, inverted_bbox, dpi, draw_stamp=False)  # must not raise

    assert np.array_equal(np.array(img_normal), np.array(img_inverted))


@pytest.mark.parametrize(
    "bbox",
    [
        (400.0, 58.0, 38.0, 148.0),
        (38.0, 148.0, 400.0, 58.0),
        (400.0, 148.0, 38.0, 58.0),
    ],
    ids=["flip-x", "flip-y", "flip-both"],
)
def test_overlaps_bbox_agrees_regardless_of_corner_order(bbox):
    """A word's overlap result against an inverted bbox must match the
    result against its normalized equivalent -- otherwise the raster (drawn
    via the normalized box) and the kept-text-layer stripping/leak-coverage
    check (which reads the raw box) could silently disagree about what's
    actually covered."""
    normalized = _normalize_bbox(bbox)
    inside_word = {"text": "Inside", "x0": 100.0, "x1": 150.0, "top": 90.0, "bottom": 110.0}
    outside_word = {"text": "Outside", "x0": 450.0, "x1": 500.0, "top": 90.0, "bottom": 110.0}
    assert _overlaps_bbox(inside_word, bbox) == _overlaps_bbox(inside_word, normalized) is True
    assert _overlaps_bbox(outside_word, bbox) == _overlaps_bbox(outside_word, normalized) is False


def test_redact_packet_handles_a_flipped_header_box_without_crashing(main_fixture, segmented, tmp_path):
    """End-to-end: redact_packet's header_bbox_override, given one ordinary
    box and one flipped (both-corners-inverted) box -- exactly the shape a
    resize-past-the-opposite-handle produces -- must not raise, and must
    produce byte-identical output to the pre-normalized equivalent call."""
    packet = _packet_by_tag(segmented, "clean_match")
    normal_boxes = [(38.0, 58.0, 400.0, 148.0), (38.0, 148.0, 574.0, 178.0)]
    flipped_boxes = [(38.0, 58.0, 400.0, 148.0), (574.0, 178.0, 38.0, 148.0)]  # second box flipped

    out_normal = tmp_path / "flip_normal.pdf"
    result_normal = redact_packet(
        main_fixture.pdf_path, packet, out_normal, dpi=DPI, header_bbox_override=normal_boxes
    )
    out_flipped = tmp_path / "flip_flipped.pdf"
    result_flipped = redact_packet(  # must not raise
        main_fixture.pdf_path, packet, out_flipped, dpi=DPI, header_bbox_override=flipped_boxes
    )

    assert out_normal.read_bytes() == out_flipped.read_bytes()
    assert result_normal.header_bboxes == result_flipped.header_bboxes


def test_render_redaction_preview_handles_inverted_header_bbox_override_without_crashing(main_fixture):
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        image = pdf.pages[0].to_image(resolution=DPI).original.convert("RGB")
    preview, band = render_redaction_preview(
        image,
        dpi=DPI,
        header_bbox_override=[(400.0, 148.0, 38.0, 58.0)],  # flipped
    )
    assert band.left == 38.0 and band.right == 400.0
    assert band.top == 58.0 and band.bottom == 148.0


def test_render_region_preview_handles_inverted_bboxes_without_crashing(main_fixture):
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        image = pdf.pages[0].to_image(resolution=DPI).original.convert("RGB")
    render_region_preview(image, dpi=DPI, bboxes=[(400.0, 148.0, 38.0, 58.0)])  # must not raise


def test_redact_packet_accepts_more_than_two_header_boxes(main_fixture, segmented, tmp_path):
    """Closes the loop on the manual editor's own "not capped at two"
    fix (2026-08-15): a reviewer drawing a third header box -- e.g. to
    cover a name written above the printed border, outside the automatic
    left-column/overflow-strip pair -- must have all three actually
    applied, not just the first two."""
    packet = _packet_by_tag(segmented, "clean_match")
    three_boxes = [
        (38.0, 58.0, 400.0, 148.0),
        (38.0, 148.0, 574.0, 178.0),
        (38.0, 20.0, 574.0, 55.0),  # a third box, above the other two
    ]
    out = tmp_path / "three_boxes.pdf"
    result = redact_packet(main_fixture.pdf_path, packet, out, dpi=DPI, header_bbox_override=three_boxes)
    assert len(result.header_bboxes) == 3
    with pdfplumber.open(out) as pdf:
        image = pdf.pages[0].to_image(resolution=DPI).original.convert("RGB")
    scale = DPI / 72.0
    arr = np.array(image)
    for left, top, right, bottom in three_boxes:
        region = arr[int((top + 3) * scale) : int((bottom - 3) * scale), int((left + 3) * scale) : int((right - 3) * scale)]
        assert region.mean() < 10  # every one of the three boxes is actually painted black


def test_redact_packet_header_bbox_override_empty_list_falls_back_to_automatic_detection(main_fixture, segmented, tmp_path):
    """An empty list is functionally "no override" -- must fall through to
    automatic detection rather than crashing on min()/max() of an empty
    sequence (found while hardening header_bbox_override's entry points)."""
    packet = _packet_by_tag(segmented, "clean_match")
    out_auto = tmp_path / "auto.pdf"
    result_auto = redact_packet(main_fixture.pdf_path, packet, out_auto, dpi=DPI)
    out_empty = tmp_path / "empty_override.pdf"
    result_empty = redact_packet(main_fixture.pdf_path, packet, out_empty, dpi=DPI, header_bbox_override=[])
    assert out_auto.read_bytes() == out_empty.read_bytes()
    assert result_empty.band.detected == result_auto.band.detected
