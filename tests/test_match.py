import pdfplumber
import pytest

from melredact.config import MIN_SCORE
from melredact.match import Candidate, MatchProposal, assign_all, propose, propose_held, score_pair
from melredact.roster import HeldName, Roster, RosterEntry, load_roster
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


# --- held names: a known-consented student with an unresolvable SID (see
# roster.py's Roster.held_names) is scored with the exact same scorer as a
# roster entry, and a packet whose best match is a held name must never be
# auto-assigned a roster SID.


def _roster_with_held(entries, held_names):
    return Roster(entries=entries, by_sid={e.sid: e for e in entries}, held_names=held_names)


def test_propose_held_uses_the_same_scorer_as_roster_entries():
    held = [HeldName(last_name="Osman", first_name="Jad")]
    candidates = propose_held("Jad Osman", held)
    assert candidates[0].full_name == "Jad Osman"
    assert candidates[0].score == score_pair("Jad Osman", held[0])


def test_propose_held_sorts_descending_by_score():
    held = [HeldName(last_name="Osman", first_name="Jad"), HeldName(last_name="Zephyr", first_name="Xin")]
    candidates = propose_held("Jad Osman", held)
    assert [c.score for c in candidates] == sorted((c.score for c in candidates), reverse=True)
    assert candidates[0].full_name == "Jad Osman"


def test_proposal_is_held_match_when_a_held_name_scores_highest():
    held = [HeldName(last_name="Osman", first_name="Jad")]
    entries = [RosterEntry(sid="0104060401", last_name="Ghavami", first_name="Gavin")]
    roster = _roster_with_held(entries, held)
    proposal = propose("packets_p000", "Jad Osman", roster)
    assert proposal.is_held_match
    assert proposal.top_held.full_name == "Jad Osman"


def test_proposal_is_not_held_match_when_a_roster_entry_scores_highest():
    held = [HeldName(last_name="Osman", first_name="Jad")]
    entries = [RosterEntry(sid="0104060401", last_name="Ames", first_name="Jordan")]
    roster = _roster_with_held(entries, held)
    proposal = propose("packets_p000", "Jordan Ames", roster)
    assert not proposal.is_held_match


def test_proposal_is_not_held_match_with_no_holds_file(main_fixture):
    """held_names defaults to empty -- ordinary rosters with no holds
    sidecar must behave exactly as before."""
    proposals, roster = _propose_all(main_fixture, PACKETS)
    assert roster.held_names == []
    for proposal in proposals:
        assert not proposal.is_held_match


# --- round-scoped claiming: a student legitimately has one packet per
# collection round (see blocks.py's round grouping), so claim-and-remove
# must be scoped *within* a round group, never across the whole file.


def test_same_student_matched_in_three_round_groups_auto_assigns_in_all_three():
    proposals = [
        MatchProposal(packet_tag="p1", candidates=[Candidate(sid="S1", score=95.0)]),
        MatchProposal(packet_tag="p2", candidates=[Candidate(sid="S1", score=95.0)]),
        MatchProposal(packet_tag="p3", candidates=[Candidate(sid="S1", score=95.0)]),
    ]
    round_labels = {"p1": "2025-10", "p2": "2026-02", "p3": "2026-03"}
    assignments = assign_all(proposals, round_labels=round_labels)
    assert assignments["p1"] == "S1"
    assert assignments["p2"] == "S1"
    assert assignments["p3"] == "S1"


def test_two_packets_in_the_same_round_group_matching_same_student_second_abstains():
    proposals = [
        MatchProposal(packet_tag="p1", candidates=[Candidate(sid="S1", score=97.0)]),
        MatchProposal(packet_tag="p2", candidates=[Candidate(sid="S1", score=90.0)]),
    ]
    round_labels = {"p1": "2026-03", "p2": "2026-03"}
    assignments = assign_all(proposals, round_labels=round_labels)
    assert assignments["p1"] == "S1"
    assert assignments["p2"] is None


def test_assign_all_never_assigns_a_sid_when_the_best_match_is_a_held_name():
    """The packet's top roster candidate clears the auto-assign bar on its
    own (same name, same score) -- assign_all must still abstain, because
    match.py's own scoring says the single best match overall is the held
    name, not this roster entry."""
    held = [HeldName(last_name="Osman", first_name="Jad")]
    entries = [RosterEntry(sid="0104060401", last_name="Osman", first_name="Jad")]
    roster = _roster_with_held(entries, held)
    proposal = propose("packets_p000", "Jad Osman", roster)

    assert proposal.top.score >= MIN_SCORE
    assert proposal.is_held_match

    assignments = assign_all([proposal])
    assert assignments["packets_p000"] is None
