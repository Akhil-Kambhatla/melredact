import pdfplumber
import pytest

from melredact.config import GROUP_ANCHOR, HEADER_SEARCH_MAX_TOP, NAME_ANCHOR, TEACHER_ANCHOR
from melredact.segment import HeaderAnchors, _assign_words_to_rows, extract_header_fields, is_header_page, segment_pdf
from tests.make_fixture import (
    HEAVY_PACKETS,
    PACKETS,
    build_footer_edge_case_fixture,
    build_main_fixture,
    build_packet_heavy_fixture,
)


@pytest.fixture(scope="module")
def main_fixture(tmp_path_factory):
    return build_main_fixture(tmp_path_factory.mktemp("main_fixture"))


@pytest.fixture(scope="module")
def heavy_fixture(tmp_path_factory):
    return build_packet_heavy_fixture(tmp_path_factory.mktemp("heavy_fixture"))


@pytest.fixture(scope="module")
def edge_case_pdf(tmp_path_factory):
    return build_footer_edge_case_fixture(tmp_path_factory.mktemp("edge_cases"))


def test_packet_count_matches_fixture(main_fixture):
    result = segment_pdf(main_fixture.pdf_path)
    assert len(result.packets) == len(PACKETS)


def test_page_counts_come_from_footer_not_hardcoded(main_fixture):
    result = segment_pdf(main_fixture.pdf_path)
    for packet, spec in zip(result.packets, PACKETS):
        assert packet.n_pages == spec.n_pages
    # the fixture deliberately varies length; a hardcoded "2" would pass
    # the loop above by accident if every packet happened to be 2 pages
    assert {p.n_pages for p in result.packets} == {1, 2, 3}


def test_clean_packets_have_no_issues(main_fixture):
    result = segment_pdf(main_fixture.pdf_path)
    for packet, spec in zip(result.packets, PACKETS):
        assert packet.issues == [], (spec.tag, packet.issues)


def test_heavy_fixture_segments_correctly(heavy_fixture):
    result = segment_pdf(heavy_fixture.pdf_path)
    assert len(result.packets) == len(HEAVY_PACKETS)
    for packet, spec in zip(result.packets, HEAVY_PACKETS):
        assert packet.n_pages == spec.n_pages
        assert packet.issues == []


def test_missing_page_one_is_flagged_not_dropped(edge_case_pdf):
    result = segment_pdf(edge_case_pdf)
    assert len(result.packets) == 3
    orphan = result.packets[1]
    assert orphan.is_orphan
    assert orphan.page_indices == [2]
    assert any("missing page 1" in issue for issue in orphan.issues)


def test_unreadable_footer_is_flagged_not_silently_dropped(edge_case_pdf):
    result = segment_pdf(edge_case_pdf)
    unreadable_packet = result.packets[2]
    assert unreadable_packet.page_indices == [3]  # present, not dropped
    assert any("unreadable" in issue for issue in unreadable_packet.issues)


def test_complete_packet_before_an_edge_case_is_unaffected(edge_case_pdf):
    result = segment_pdf(edge_case_pdf)
    normal = result.packets[0]
    assert normal.page_indices == [0, 1]
    assert normal.issues == []


def test_orphan_page_does_not_get_merged_into_prior_complete_packet(edge_case_pdf):
    """Regression: an orphan continuation page arriving right after a
    packet that already has all its declared pages must start a new
    packet, not silently extend the one before it."""
    result = segment_pdf(edge_case_pdf)
    assert result.packets[0].n_pages == 2
    assert 2 not in result.packets[0].page_indices


def test_group_row_name_does_not_reach_name_field(main_fixture):
    """Regression for the Shaw/Nuzhat trap: a roster student named in
    someone else's group row must not appear in that packet's name_text."""
    result = segment_pdf(main_fixture.pdf_path)
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        packet = next(p for p, s in zip(result.packets, PACKETS) if s.tag == "group_row_trap")
        fields = extract_header_fields(pdf.pages[packet.header_page_index])
    assert fields.name_text == "Casey Shaw"
    assert "Nadia" not in fields.name_text
    assert fields.group_text == "Nadia"


def test_blank_rows_produce_empty_fields_not_contamination(main_fixture):
    result = segment_pdf(main_fixture.pdf_path)
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        packet = next(p for p, s in zip(result.packets, PACKETS) if s.tag == "blank_rows_leak")
        fields = extract_header_fields(pdf.pages[packet.header_page_index])
    assert fields.name_text == "Morgan Lee"
    assert fields.teacher_text == ""
    assert fields.group_text == ""


def test_date_and_period_isolated_from_name_and_teacher(main_fixture):
    result = segment_pdf(main_fixture.pdf_path)
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        packet = result.packets[0]  # clean_match
        fields = extract_header_fields(pdf.pages[packet.header_page_index])
    assert fields.date_text == "10/03/2025"
    assert fields.period_text == "02"
    assert "10/03/2025" not in fields.name_text
    assert "02" not in fields.teacher_text


def test_header_anchors_located_dynamically(main_fixture):
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        anchors = extract_header_fields(pdf.pages[0]).anchors
    assert anchors.name_found
    assert anchors.teacher_found
    assert anchors.group_found


def test_continuation_pages_are_not_misdetected_as_headers(main_fixture):
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        assert is_header_page(pdf.pages[0])  # clean_match header
        assert not is_header_page(pdf.pages[1])  # clean_match continuation


# --- row-assignment window: body text below the header must not bleed in ---


def _standard_anchors() -> HeaderAnchors:
    return HeaderAnchors(
        name_top=NAME_ANCHOR["top"],
        teacher_top=TEACHER_ANCHOR["top"],
        group_top=GROUP_ANCHOR["top"],
        name_found=True,
        teacher_found=True,
        group_found=True,
    )


def test_body_text_well_below_the_header_does_not_reach_group_row():
    """Regression for the real-file bleed: body text ('1. Please work on
    this individually:' and the paragraph below it) sitting well below the
    header must not be swept into group_text just because it fell inside
    the wide, skew-tolerant HEADER_SEARCH_MAX_TOP slack used for *finding*
    labels. Real gap measured at ~172pt with anchors at their fallback
    positions (group_top=111); this uses a body word further out than
    that, comfortably below the tightened window."""
    anchors = _standard_anchors()
    body_word = {"text": "individually:", "x0": 50, "x1": 120, "top": 172, "bottom": 182}
    rows = _assign_words_to_rows([body_word], anchors)
    assert body_word not in rows["group"]
    assert body_word not in rows["name"]
    assert body_word not in rows["teacher"]


def test_group_row_handwriting_just_past_the_label_still_gets_assigned():
    """The tightened window must not be so tight it clips legitimate
    multi-line group-member handwriting sitting a bit below the printed
    label's own top."""
    anchors = _standard_anchors()
    row_height = anchors.group_top - anchors.teacher_top
    handwriting = {"text": "Nadia,", "x0": 150, "x1": 190, "top": anchors.group_top + row_height - 2, "bottom": anchors.group_top + row_height + 6}
    rows = _assign_words_to_rows([handwriting], anchors)
    assert handwriting in rows["group"]


def test_body_text_bleed_can_never_reach_name_row_even_within_the_wider_search_window():
    """Structural guarantee, not just an empirical one: name_top is always
    the anchor farthest from anything below the header, so even a word
    that *does* fall inside the (still wider than the row-value window)
    HEADER_SEARCH_MAX_TOP bound used elsewhere for label search can never
    be nearest to the name row -- group (or teacher) is always closer."""
    anchors = _standard_anchors()
    body_word = {"text": "relevant?", "x0": 50, "x1": 120, "top": HEADER_SEARCH_MAX_TOP - 1, "bottom": HEADER_SEARCH_MAX_TOP + 5}
    rows = _assign_words_to_rows([body_word], anchors)
    assert body_word not in rows["name"]


def test_illegible_name_is_still_captured_verbatim_for_matching(main_fixture):
    """segment.py's job is just to extract the text -- refusing to match
    illegible scrawl is match.py's job, not segment.py's."""
    result = segment_pdf(main_fixture.pdf_path)
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        packet = next(p for p, s in zip(result.packets, PACKETS) if s.tag == "illegible_scrawl")
        fields = extract_header_fields(pdf.pages[packet.header_page_index])
    assert fields.name_text == "S 8"
