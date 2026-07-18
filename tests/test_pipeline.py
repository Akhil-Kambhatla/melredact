import pytest

from melredact.config import RENDER_DPI_PREVIEW
from melredact.pipeline import (
    DispositionResult,
    decisions_path,
    load_decisions,
    output_path,
    packet_tag,
    propose_all,
    run_dispositions,
    save_decisions,
)
from melredact.roster import load_roster
from melredact.segment import segment_pdf
from tests.make_fixture import PACKETS, build_footer_edge_case_fixture, build_main_fixture

DPI = RENDER_DPI_PREVIEW


@pytest.fixture(scope="module")
def main_fixture(tmp_path_factory):
    return build_main_fixture(tmp_path_factory.mktemp("pipeline_fixture"))


@pytest.fixture(scope="module")
def roster(main_fixture):
    return load_roster(main_fixture.roster_path)


@pytest.fixture(scope="module")
def segmented(main_fixture):
    return segment_pdf(main_fixture.pdf_path)


def _sid_for(roster, full_name):
    return next(e.sid for e in roster if e.full_name == full_name)


# --- packet_tag stability ---


def test_packet_tag_is_stable_across_calls(main_fixture, segmented):
    packet = segmented.packets[0]
    assert packet_tag(main_fixture.pdf_path, packet) == packet_tag(main_fixture.pdf_path, packet)


def test_packet_tag_derived_from_first_physical_page_not_list_position(main_fixture, segmented):
    tags = [packet_tag(main_fixture.pdf_path, p) for p in segmented.packets]
    assert len(tags) == len(set(tags))
    for packet, tag in zip(segmented.packets, tags):
        assert tag.endswith(f"_p{packet.page_indices[0]:03d}")


# --- decisions file IO ---


def test_save_and_load_decisions_round_trip(tmp_path, main_fixture):
    d = tmp_path / "decisions"
    decisions = {"tag_a": "0204150201", "tag_b": None}
    save_decisions(main_fixture.pdf_path, decisions, decisions_dir=d)
    assert decisions_path(main_fixture.pdf_path, d).exists()
    loaded = load_decisions(main_fixture.pdf_path, decisions_dir=d)
    assert loaded == decisions


def test_load_decisions_missing_file_returns_empty(tmp_path, main_fixture):
    assert load_decisions(main_fixture.pdf_path, decisions_dir=tmp_path / "nope") == {}


# --- propose_all ---


def test_propose_all_keys_by_packet_tag_not_fixture_tag(main_fixture, segmented, roster):
    proposals = propose_all(main_fixture.pdf_path, segmented, roster)
    tags = {p.packet_tag for p in proposals}
    expected_tags = {packet_tag(main_fixture.pdf_path, p) for p in segmented.packets}
    assert tags == expected_tags


def test_propose_all_gets_the_right_top_candidate(main_fixture, segmented, roster):
    proposals = propose_all(main_fixture.pdf_path, segmented, roster)
    clean_match_packet = segmented.packets[0]  # PACKETS[0] == clean_match
    tag = packet_tag(main_fixture.pdf_path, clean_match_packet)
    proposal = next(p for p in proposals if p.packet_tag == tag)
    assert proposal.top.sid == _sid_for(roster, "Jordan Ames")


# --- The delete rule itself ---


def test_pending_packet_is_left_alone(main_fixture, segmented, roster, tmp_path):
    """No decisions entry at all -- not yet reviewed -- must not write or
    delete anything."""
    out_dir = tmp_path / "out"
    results = run_dispositions(main_fixture.pdf_path, segmented, {}, roster, out_dir=out_dir, dpi=DPI)
    assert all(r.pending for r in results)
    assert all(r.out_path is None and r.deleted_path is None for r in results)
    assert not out_dir.exists() or list(out_dir.iterdir()) == []


def test_consented_packet_gets_redacted_output(main_fixture, segmented, roster, tmp_path):
    packet = segmented.packets[0]  # clean_match
    tag = packet_tag(main_fixture.pdf_path, packet)
    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"

    results = run_dispositions(main_fixture.pdf_path, segmented, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)
    assert result.sid == sid
    assert not result.pending
    assert result.out_path.exists()
    assert result.deleted_path is None


def test_consented_packet_is_stamped_with_its_own_sid_and_period(main_fixture, segmented, roster, tmp_path, monkeypatch):
    """run_dispositions must actually pass the approved packet's own
    SID/period through to the redaction stamp -- the box getting drawn at
    all doesn't mean re-identification is happening if this wiring is
    missing (the bug: it silently fell back to no stamp_lines)."""
    import melredact.pipeline as pipeline_mod

    captured = {}
    real_redact_packet = pipeline_mod._redact_packet

    def spy(*args, **kwargs):
        captured["stamp_lines"] = kwargs.get("stamp_lines")
        return real_redact_packet(*args, **kwargs)

    monkeypatch.setattr(pipeline_mod, "_redact_packet", spy)

    packet = segmented.packets[0]  # clean_match
    tag = packet_tag(main_fixture.pdf_path, packet)
    entry = roster.by_sid[_sid_for(roster, "Jordan Ames")]
    out_dir = tmp_path / "out"

    run_dispositions(main_fixture.pdf_path, segmented, {tag: entry.sid}, roster, out_dir=out_dir, dpi=DPI)

    assert captured["stamp_lines"] == [f"SID: {entry.sid}", f"PD: {entry.period_display}"]


def test_output_path_is_teacher_period_sid_not_packet_tag(main_fixture, segmented, roster, tmp_path):
    packet = segmented.packets[0]  # clean_match
    tag = packet_tag(main_fixture.pdf_path, packet)
    sid = _sid_for(roster, "Jordan Ames")
    entry = roster.by_sid[sid]
    out_dir = tmp_path / "out"

    results = run_dispositions(main_fixture.pdf_path, segmented, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)
    assert result.out_path == out_dir / entry.teacher_code / entry.period_display / f"{sid}.pdf"
    assert result.out_path == output_path(out_dir, entry)


def test_explicit_non_consent_deletes_existing_output_not_just_skips_it(main_fixture, segmented, roster, tmp_path):
    """The reversed rule: a packet whose decision flips to non-consent
    must have its prior output actively removed, not left in place. Since
    output is now named by SID rather than packet_tag, the removal shows up
    as a reconciliation-sweep result (packet_tag=None, keyed by the stale
    SID) rather than attached to the rejecting packet's own tag -- see
    CLAUDE.md's "present in the output tree iff has a confirmed, approved
    SID" invariant."""
    packet = segmented.packets[0]  # clean_match
    tag = packet_tag(main_fixture.pdf_path, packet)
    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"

    # First pass: consented, output written.
    first = run_dispositions(main_fixture.pdf_path, segmented, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    out_path = next(r for r in first if r.packet_tag == tag).out_path
    assert out_path.exists()

    # Second pass: a reviewer rejects the match -- decision flips to None.
    second = run_dispositions(main_fixture.pdf_path, segmented, {tag: None}, roster, out_dir=out_dir, dpi=DPI)
    tag_result = next(r for r in second if r.packet_tag == tag)
    assert tag_result.sid is None
    assert not tag_result.pending
    assert tag_result.out_path is None

    swept = [r for r in second if r.deleted_path == out_path]
    assert len(swept) == 1
    assert swept[0].sid == sid
    assert swept[0].packet_tag is None
    assert not out_path.exists()


def test_non_consented_packet_with_no_prior_output_stays_absent(main_fixture, segmented, roster, tmp_path):
    packet = segmented.packets[0]
    tag = packet_tag(main_fixture.pdf_path, packet)
    out_dir = tmp_path / "out"
    results = run_dispositions(main_fixture.pdf_path, segmented, {tag: None}, roster, out_dir=out_dir, dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)
    assert result.deleted_path is None
    assert not any(out_dir.rglob("*.pdf"))


def test_corrected_decision_writes_new_sid_and_removes_old(main_fixture, segmented, roster, tmp_path):
    """A human correcting an earlier decision to a different SID must end
    up with exactly the corrected SID's file present, and the superseded
    SID's file gone -- the "corrected" state of the output-tree invariant."""
    packet = segmented.packets[0]  # clean_match
    tag = packet_tag(main_fixture.pdf_path, packet)
    old_sid = _sid_for(roster, "Jordan Ames")
    new_entry = next(e for e in roster if e.sid != old_sid)
    out_dir = tmp_path / "out"

    first = run_dispositions(main_fixture.pdf_path, segmented, {tag: old_sid}, roster, out_dir=out_dir, dpi=DPI)
    old_path = next(r for r in first if r.packet_tag == tag).out_path
    assert old_path.exists()

    second = run_dispositions(main_fixture.pdf_path, segmented, {tag: new_entry.sid}, roster, out_dir=out_dir, dpi=DPI)
    tag_result = next(r for r in second if r.packet_tag == tag)
    assert tag_result.sid == new_entry.sid
    assert tag_result.out_path == output_path(out_dir, new_entry)
    assert tag_result.out_path.exists()
    assert not old_path.exists()

    remaining = sorted(out_dir.rglob("*.pdf"))
    assert remaining == [tag_result.out_path]


def test_unknown_sid_in_decisions_raises(main_fixture, segmented, roster, tmp_path):
    packet = segmented.packets[0]
    tag = packet_tag(main_fixture.pdf_path, packet)
    with pytest.raises(ValueError, match="not on roster"):
        run_dispositions(main_fixture.pdf_path, segmented, {tag: "9999999999"}, roster, out_dir=tmp_path / "out", dpi=DPI)


def test_packet_with_unresolved_issues_is_refused_even_with_a_decision(tmp_path, roster):
    edge_pdf = build_footer_edge_case_fixture(tmp_path / "edge")
    seg = segment_pdf(edge_pdf)
    flagged = next(p for p in seg.packets if p.issues)
    tag = packet_tag(edge_pdf, flagged)
    any_sid = next(iter(roster)).sid
    with pytest.raises(ValueError, match="unresolved issues"):
        run_dispositions(edge_pdf, seg, {tag: any_sid}, roster, out_dir=tmp_path / "out", dpi=DPI)


def test_leak_finding_deletes_output_and_raises(main_fixture, segmented, roster, tmp_path, monkeypatch):
    """If the verify pass ever finds a leak, run_dispositions must not
    leave the leaking file sitting in out_dir."""
    import melredact.pipeline as pipeline_mod
    from melredact.redact import LeakFinding

    monkeypatch.setattr(
        pipeline_mod,
        "verify_no_leaked_names",
        lambda out_path, roster: [LeakFinding(page_index=0, sid="x", token="leaked")],
    )
    packet = segmented.packets[0]
    tag = packet_tag(main_fixture.pdf_path, packet)
    sid = _sid_for(roster, "Jordan Ames")
    entry = roster.by_sid[sid]
    out_dir = tmp_path / "out"
    with pytest.raises(RuntimeError, match="leaks"):
        run_dispositions(main_fixture.pdf_path, segmented, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    assert not output_path(out_dir, entry).exists()
