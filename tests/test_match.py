import pdfplumber
import pytest

from melredact.match import assign_all, propose, score_pair
from melredact.roster import load_roster
from melredact.segment import extract_header_fields, segment_pdf
from tests.make_fixture import (
    HEAVY_PACKETS,
    PACKETS,
    build_main_fixture,
    build_packet_heavy_fixture,
)


def _propose_all(fixture_result, packets_spec):
    roster = load_roster(fixture_result.roster_path)
    seg = segment_pdf(fixture_result.pdf_path)
    proposals = []
    with pdfplumber.open(fixture_result.pdf_path) as pdf:
        for packet, spec in zip(seg.packets, packets_spec):
            fields = extract_header_fields(pdf.pages[packet.header_page_index])
            proposals.append(propose(spec.tag, fields.name_text, roster))
    return proposals, roster


@pytest.fixture(scope="module")
def main_fixture(tmp_path_factory):
    return build_main_fixture(tmp_path_factory.mktemp("main_fixture"))


@pytest.fixture(scope="module")
def heavy_fixture(tmp_path_factory):
    return build_packet_heavy_fixture(tmp_path_factory.mktemp("heavy_fixture"))


class _FakeEntry:
    def __init__(self, first, last):
        self.first_name = first
        self.last_name = last


def test_illegible_scrawl_scores_zero_against_every_entry():
    roster_entries = [
        _FakeEntry("Casey", "Shaw"),
        _FakeEntry("Nadia", "Shaikh"),
        _FakeEntry("Sam", "Salla"),
    ]
    for entry in roster_entries:
        assert score_pair("S 8", entry) == 0.0


def test_short_probe_floor_applies_before_variants_not_after():
    """Bug (a) regression, reached through the variant path: a short probe
    must score 0 even against a roster entry whose bare last name is itself
    short enough to look like a substring match via WRatio."""
    entry = _FakeEntry("Jo", "Li")
    assert score_pair("S 8", entry) == 0.0


def test_group_row_name_does_not_win_the_packet(main_fixture):
    proposals, roster = _propose_all(main_fixture, PACKETS)
    proposal = next(p for p in proposals if p.packet_tag == "group_row_trap")
    casey_sid = next(e.sid for e in roster if e.full_name == "Casey Shaw")
    nadia_sid = next(e.sid for e in roster if e.full_name == "Nadia Shaikh")
    assert proposal.top.sid == casey_sid
    nadia_score = next(c.score for c in proposal.candidates if c.sid == nadia_sid)
    assert nadia_score < proposal.top.score


def test_ocr_noise_still_surfaces_the_right_top_candidate(main_fixture):
    """The garbled packet shouldn't auto-assign (see expected_auto_assign),
    but the top candidate must still be correct so review has something
    right to approve."""
    proposals, roster = _propose_all(main_fixture, PACKETS)
    proposal = next(p for p in proposals if p.packet_tag == "ocr_garbled_name")
    expected_sid = next(e.sid for e in roster if e.full_name == "Priya Chandra")
    assert proposal.top.sid == expected_sid


def test_below_threshold_candidate_surfaces_but_does_not_auto_assign(main_fixture):
    """The propose/auto-assign split, end to end: a packet whose top score
    lands below MIN_SCORE despite a clean margin must still surface the
    correct candidate for review, but must not be auto-assigned."""
    proposals, roster = _propose_all(main_fixture, PACKETS)
    proposal = next(p for p in proposals if p.packet_tag == "below_threshold_correct_candidate")
    expected_sid = next(e.sid for e in roster if e.full_name == "Alex Rivera")
    assert proposal.top.sid == expected_sid
    assert proposal.top.score < 82

    assignments = assign_all(proposals)
    assert assignments["below_threshold_correct_candidate"] is None


def test_auto_assign_matches_expected_for_main_fixture(main_fixture):
    proposals, roster = _propose_all(main_fixture, PACKETS)
    assignments = assign_all(proposals)
    for spec in PACKETS:
        assert assignments[spec.tag] == main_fixture.expected_auto_assign_sid[spec.tag], spec.tag


def test_auto_assign_matches_expected_for_heavy_fixture(heavy_fixture):
    proposals, roster = _propose_all(heavy_fixture, HEAVY_PACKETS)
    assignments = assign_all(proposals)
    for spec in HEAVY_PACKETS:
        assert assignments[spec.tag] == heavy_fixture.expected_auto_assign_sid[spec.tag], spec.tag


def test_no_sid_assigned_twice(heavy_fixture):
    proposals, roster = _propose_all(heavy_fixture, HEAVY_PACKETS)
    assignments = assign_all(proposals)
    assigned = [sid for sid in assignments.values() if sid is not None]
    assert len(assigned) == len(set(assigned))


def test_every_assigned_sid_is_on_the_roster(heavy_fixture):
    proposals, roster = _propose_all(heavy_fixture, HEAVY_PACKETS)
    assignments = assign_all(proposals)
    for sid in assignments.values():
        if sid is not None:
            assert sid in roster


def test_extra_packets_beyond_roster_size_go_unmatched_not_forced(heavy_fixture):
    """14 packets against 6 roster entries: the 8 extras (illegible,
    not-on-roster, decoys) must not get forced onto an already-claimed or
    unrelated entry."""
    proposals, roster = _propose_all(heavy_fixture, HEAVY_PACKETS)
    assignments = assign_all(proposals)
    unmatched_tags = {spec.tag for spec in HEAVY_PACKETS if spec.expected_sid is None}
    for tag in unmatched_tags:
        assert assignments[tag] is None, tag


def test_decoy_does_not_outrank_the_genuine_match_for_same_entry(heavy_fixture):
    proposals, roster = _propose_all(heavy_fixture, HEAVY_PACKETS)
    by_tag = {p.packet_tag: p for p in proposals}
    genuine = by_tag["heavy_clean_match"]
    decoy = by_tag["heavy_decoy_jordan_james"]
    assert genuine.top.score >= decoy.top.score
