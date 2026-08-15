import pdfplumber
import pytest

from melredact.config import GROUP_ANCHOR, HEADER_SEARCH_MAX_TOP, NAME_ANCHOR, TEACHER_ANCHOR
from melredact.segment import (
    HeaderAnchors,
    _assign_words_to_rows,
    _parse_worksheet_type,
    extract_header_fields,
    find_reversed_continuation_header_pairs,
    is_header_page,
    segment_pdf,
)
from tests.make_fixture import (
    HEAVY_PACKETS,
    PACKETS,
    build_footer_edge_case_fixture,
    build_main_fixture,
    build_packet_heavy_fixture,
    build_reversed_pair_fixture,
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


# --- Worksheet type (out/ path scoping) ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("PRT (01/2024)", "PRT"),
        ("pcMEL MPR+ADR (06/2025)", "PCMEL_MPR_ADR"),
        ("PRT (01/2024) Page 3 of 4", "PRT"),  # same joined footer-band blob as page marker
        ("no parenthetical date here", None),
        ("", None),
    ],
)
def test_parse_worksheet_type_strips_revision_date_and_slugifies(raw, expected):
    assert _parse_worksheet_type(raw) == expected


def test_main_fixture_packets_carry_the_parsed_worksheet_type(main_fixture):
    """Every header packet in the fixture embeds the real "pcMEL MPR+ADR
    (06/2025)" footer text (see make_fixture.WORKSHEET_TYPE_TEXT) -- this
    must come through segment_pdf as the slugified type, not go unread."""
    result = segment_pdf(main_fixture.pdf_path)
    for packet in result.packets:
        if packet.header_page_index is not None:
            assert packet.worksheet_type == "PCMEL_MPR_ADR"


def test_unreadable_worksheet_type_is_flagged_not_silently_ignored(tmp_path, monkeypatch):
    """Same treatment as an unreadable page marker (see
    test_unreadable_footer_is_flagged_not_silently_dropped): a header page
    whose footer worksheet-type label can't be parsed must not silently
    fall through with worksheet_type=None -- that would eventually let
    run_dispositions write to an out/ path missing its type segment, the
    exact class of bug that let an MPR and a PRT packet collide."""
    import tests.make_fixture as fixture_mod

    monkeypatch.setattr(fixture_mod, "WORKSHEET_TYPE_TEXT", "Untitled Form")  # no "(mm/yyyy)" to parse
    fixture = fixture_mod.build_main_fixture(tmp_path)
    result = segment_pdf(fixture.pdf_path)
    header_packets = [p for p in result.packets if p.header_page_index is not None]
    assert header_packets
    for packet in header_packets:
        assert packet.worksheet_type is None
        assert any("worksheet type unreadable" in issue for issue in packet.issues)


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


def test_band_bottom_anchor_excludes_body_text_the_self_relative_window_missed():
    """Regression for a real false positive found regenerating SID
    0204150202 (the original Ganik incident packet) and SID 0204150203:
    real per-page `row_height` varies enough that the self-relative window
    (group_top + row_height + ROW_ASSIGNMENT_BOTTOM_SLACK_PT) swept the
    printed "1. Please work on this individually:" instruction into the
    group row on these two real pages, even though the *general* case is
    already covered by test_body_text_well_below_the_header_does_not_
    reach_group_row above -- measured across all 42 real header pages we
    have, this self-relative estimate's own margin over real body text
    ranged from -10pt to +38pt (see ROW_ASSIGNMENT_BOTTOM_SLACK_PT in
    config.py), not the "room to spare" it was documented as having.

    Real measured numbers from SID 0204150202's header page: name_top=
    43.68, teacher_top=78.24, group_top=105.84 (self-relative row_height=
    27.6, window_max=143.44 -- the "individually" word's real top of
    143.04 sneaks inside by 0.4pt). The real detected header border
    (band.bottom) on the same page is 138.24, well clear of it -- this is
    the anchor-relative fix, mirroring how detect_header_band itself moved
    from a fixed window to one centered on this page's own located
    anchors."""
    anchors = HeaderAnchors(
        name_top=43.68,
        teacher_top=78.24,
        group_top=105.84,
        name_found=True,
        teacher_found=True,
        group_found=True,
    )
    instruction_word = {"text": "individually", "x0": 195.6, "x1": 255.84, "top": 143.04, "bottom": 161.52}

    # Without band_bottom, the self-relative window still lets it through --
    # confirms this is the real, previously-shipped-with bug, not a strawman.
    rows_self_relative = _assign_words_to_rows([instruction_word], anchors)
    assert instruction_word in rows_self_relative["group"]

    # With the real detected border passed through, it's correctly excluded.
    rows_band_anchored = _assign_words_to_rows([instruction_word], anchors, band_bottom=138.24)
    assert instruction_word not in rows_band_anchored["group"]


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


@pytest.fixture(scope="module")
def reversed_pair_pdf(tmp_path_factory):
    return build_reversed_pair_fixture(tmp_path_factory.mktemp("reversed_pair"))


def test_reversed_continuation_header_pair_reported_as_two_broken_packets(reversed_pair_pdf):
    """Baseline, unfixed shape: natural physical order reports the
    continuation page as an orphan (missing its header) and the header
    page as its own packet short one page -- segment_pdf's own scan-order-
    is-document-order assumption (see its module docstring) breaking
    exactly as documented, not silently working around it."""
    result = segment_pdf(reversed_pair_pdf)
    orphan = next(p for p in result.packets if p.is_orphan)
    assert orphan.page_indices == [2]
    assert "continuation page with no preceding header" in " ".join(orphan.issues)

    header_only = next(p for p in result.packets if p.header_page_index == 3)
    assert header_only.page_indices == [3]
    assert "footer declared 2" in " ".join(header_only.issues)


def test_find_reversed_continuation_header_pairs_detects_the_swap(reversed_pair_pdf):
    """The proposal itself: names the exact continuation/header physical
    indices and their agreed-on declared total, computed purely by reading
    segment_pdf's own already-produced orphan/header-started packets --
    never mutates anything."""
    result = segment_pdf(reversed_pair_pdf)
    suggestions = find_reversed_continuation_header_pairs(result)
    assert len(suggestions) == 1
    assert suggestions[0].continuation_page_index == 2
    assert suggestions[0].header_page_index == 3
    assert suggestions[0].declared_total == 2

    # A normal, unaffected file has nothing to suggest.
    assert find_reversed_continuation_header_pairs(segment_pdf(build_main_fixture(reversed_pair_pdf.parent / "control").pdf_path)) == []


def test_page_sequence_override_fixes_the_reversed_pair(reversed_pair_pdf):
    """Applying the proposed fix (process the header page immediately
    before the continuation page) via segment_pdf's own `page_sequence`
    parameter must produce one clean, issue-free packet -- proving the
    override actually re-groups the pages, not just relabels them."""
    fixed = segment_pdf(reversed_pair_pdf, page_sequence=[0, 1, 3, 2])
    assert len(fixed.packets) == 2  # the control packet, plus the now-merged pair
    merged = next(p for p in fixed.packets if p.header_page_index == 3)
    assert merged.page_indices == [3, 2]
    assert merged.is_orphan is False
    assert merged.issues == []
    assert fixed.page_count == 4  # true physical count, unchanged by the override

    # Nothing about this override is auto-discovered -- the identical,
    # unmodified fixture still reports the original break when segmented
    # without it.
    assert segment_pdf(reversed_pair_pdf).packets != fixed.packets


def test_page_sequence_excluding_a_page_leaves_it_unassigned(reversed_pair_pdf):
    """A page left out of `page_sequence` entirely is processed by
    nothing -- the "remove this page" outcome the page composition editor's
    own remove control relies on (see review_app.py's _sequence_remove)."""
    result = segment_pdf(reversed_pair_pdf, page_sequence=[0, 1, 3])
    all_pages = {idx for p in result.packets for idx in p.page_indices}
    assert 2 not in all_pages
    assert result.page_count == 4
