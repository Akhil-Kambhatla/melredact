import pytest

from melredact.blocks import round_label
from melredact.config import RENDER_DPI_PREVIEW
from melredact.pipeline import (
    DispositionResult,
    analyze_redaction_holds,
    composition_overrides_path,
    decisions_path,
    duplicate_decisions_within_round,
    filter_packets_by_round,
    format_duplicate_decisions_report,
    format_hold_analysis_report,
    load_composition_overrides,
    load_decisions,
    load_detection_overrides,
    load_orientation_overrides,
    output_path,
    packet_tag,
    propose_all,
    run_dispositions,
    save_composition_overrides,
    save_decisions,
    save_orientation_overrides,
)
from melredact.roster import load_roster
from melredact.segment import segment_pdf
from tests.make_fixture import (
    PACKETS,
    ROSTER,
    PacketSpec,
    _build_packets_pdf,
    build_footer_edge_case_fixture,
    build_main_fixture,
    build_rotated_page_copy,
    build_unreadable_continuation_footer_fixture,
)

DPI = RENDER_DPI_PREVIEW

# Every packet spec in the main fixture (and the packet14/held-name
# fixtures built inline below) uses PacketSpec's default date_text,
# "10/03/2025" -- a single, uniform date, so the whole file resolves to one
# contiguous round group with this label (see blocks.group_into_rounds).
# Computed via the real round_label(), not hardcoded, so this stays correct
# if the fixture's default date_text ever changes.
FIXTURE_ROUND = round_label("10/03/2025")


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
    assert (
        result.out_path
        == out_dir
        / entry.teacher_code
        / entry.period_display
        / packet.worksheet_type
        / "NA"
        / FIXTURE_ROUND
        / f"{sid}.pdf"
    )
    assert result.out_path == output_path(out_dir, entry, packet.worksheet_type, round_label=FIXTURE_ROUND)


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
    assert tag_result.out_path == output_path(out_dir, new_entry, packet.worksheet_type, round_label=FIXTURE_ROUND)
    assert tag_result.out_path.exists()
    assert not old_path.exists()

    remaining = sorted(out_dir.rglob("*.pdf"))
    assert remaining == [tag_result.out_path]


# --- allow_delete: a blanket safety switch for a pilot or a file that
# hasn't been through this code before (2026-08-13) ---


def test_allow_delete_false_leaves_confirmed_non_consent_output_in_place(main_fixture, segmented, roster, tmp_path):
    packet = segmented.packets[0]  # clean_match
    tag = packet_tag(main_fixture.pdf_path, packet)
    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"

    first = run_dispositions(main_fixture.pdf_path, segmented, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    out_path = next(r for r in first if r.packet_tag == tag).out_path
    assert out_path.exists()

    second = run_dispositions(
        main_fixture.pdf_path, segmented, {tag: None}, roster, out_dir=out_dir, dpi=DPI, allow_delete=False
    )
    tag_result = next(r for r in second if r.packet_tag == tag)
    assert tag_result.sid is None
    assert not tag_result.pending
    assert tag_result.out_path is None
    assert "deletion disabled" in (tag_result.reason or "")
    assert tag_result.deletion_skipped
    assert not any(r.deleted_path for r in second)
    assert out_path.exists()


def test_allow_delete_false_leaves_stale_correction_file_in_place(main_fixture, segmented, roster, tmp_path):
    packet = segmented.packets[0]  # clean_match
    tag = packet_tag(main_fixture.pdf_path, packet)
    old_sid = _sid_for(roster, "Jordan Ames")
    new_entry = next(e for e in roster if e.sid != old_sid)
    out_dir = tmp_path / "out"

    first = run_dispositions(main_fixture.pdf_path, segmented, {tag: old_sid}, roster, out_dir=out_dir, dpi=DPI)
    old_path = next(r for r in first if r.packet_tag == tag).out_path
    assert old_path.exists()

    second = run_dispositions(
        main_fixture.pdf_path,
        segmented,
        {tag: new_entry.sid},
        roster,
        out_dir=out_dir,
        dpi=DPI,
        allow_delete=False,
    )
    tag_result = next(r for r in second if r.packet_tag == tag)
    assert tag_result.sid == new_entry.sid
    assert tag_result.out_path.exists()
    assert "deletion disabled" in (tag_result.reason or "")
    assert tag_result.deletion_skipped
    assert not any(r.deleted_path for r in second)
    assert old_path.exists()  # stale file left in place, never removed


def test_stale_suffixed_path_is_cleaned_up_when_a_different_packets_collision_clears(
    main_fixture, segmented, roster, tmp_path
):
    """Real bug found auditing the real 010406 output tree (2026-08-15): a
    packet's own claimed output path can change between runs even when its
    own SID never changes -- purely because a *different* packet's
    collision status with this tag's natural path shifted (that other
    packet's decision was corrected away from the SID it used to share).
    The old `prior_sid != sid` check only looked at whether *this* tag's
    SID changed, so it never caught this, leaving a stale numbered-suffix
    file in out/ that no current ledger entry pointed at."""
    tag_a = packet_tag(main_fixture.pdf_path, segmented.packets[0])
    tag_b = packet_tag(main_fixture.pdf_path, segmented.packets[1])
    sid_shared = _sid_for(roster, "Jordan Ames")
    sid_other = _sid_for(roster, "Priya Chandra")
    out_dir = tmp_path / "out"

    # Both packets decided to the same SID (a real, if mistaken, human
    # decision -- see CLAUDE.md's real duplicate-decision-within-round
    # finding) -- A claims the natural path, B collides and gets a
    # numbered-suffix path instead.
    first = run_dispositions(
        main_fixture.pdf_path, segmented, {tag_a: sid_shared, tag_b: sid_shared}, roster, out_dir=out_dir, dpi=DPI
    )
    a1 = next(r for r in first if r.packet_tag == tag_a)
    b1 = next(r for r in first if r.packet_tag == tag_b)
    assert a1.out_path.name == f"{sid_shared}.pdf"
    assert b1.collision_note is not None
    stale_suffixed_path = b1.out_path
    assert stale_suffixed_path.exists()
    assert stale_suffixed_path != a1.out_path

    # A's decision is corrected to a different student -- A's own old file
    # is cleaned up (pre-existing behavior) and the natural path frees up.
    # B's own decision never changes.
    second = run_dispositions(
        main_fixture.pdf_path, segmented, {tag_a: sid_other, tag_b: sid_shared}, roster, out_dir=out_dir, dpi=DPI
    )
    b2 = next(r for r in second if r.packet_tag == tag_b)
    assert b2.out_path.name == f"{sid_shared}.pdf"  # B now claims the natural path, no collision left
    assert b2.out_path != stale_suffixed_path
    assert not stale_suffixed_path.exists(), "B's own now-abandoned suffixed file must not be left orphaned"


def test_duplicate_decisions_within_round_flags_the_real_010406_shape():
    """Real shape found auditing 010406's own decisions (2026-08-15): the
    same SID decided for two different packet_tags within one round must
    be reported, keyed by (round, sid); different rounds or different SIDs
    must never be flagged."""
    decisions = {
        "scan_p040": "SID_A",
        "scan_p066": "SID_A",  # same round, same sid as p040 -- duplicate
        "scan_p004": "SID_A",  # different round -- not a duplicate
        "scan_p042": "SID_B",
        "scan_p999": None,  # non-consent, never counted
    }
    round_labels = {
        "scan_p040": "2026-02",
        "scan_p066": "2026-02",
        "scan_p004": "2026-03",
        "scan_p042": "2026-02",
        "scan_p999": "2026-02",
    }
    duplicates = duplicate_decisions_within_round(decisions, round_labels)
    assert duplicates == {("2026-02", "SID_A"): ["scan_p040", "scan_p066"]}

    report = format_duplicate_decisions_report(duplicates)
    assert "2026-02" in report and "SID_A" in report and "scan_p040" in report and "scan_p066" in report
    assert format_duplicate_decisions_report({}) == (
        "Duplicate-decision check: no SID decided for more than one packet within the same round."
    )


# --- round-scoped processing: --round restricts a run to one round group's
# packets, and that restriction must be enough on its own to protect every
# other round's already-shipped output (2026-08-13) ---


def test_filter_packets_by_round_keeps_only_matching_tags(main_fixture, segmented):
    tags = [packet_tag(main_fixture.pdf_path, p) for p in segmented.packets]
    round_labels = {t: ("2025-10" if i % 2 == 0 else "2026-03") for i, t in enumerate(tags)}
    filtered = filter_packets_by_round(main_fixture.pdf_path, segmented, round_labels, "2025-10")
    filtered_tags = {packet_tag(main_fixture.pdf_path, p) for p in filtered.packets}
    assert filtered_tags == {t for t, label in round_labels.items() if label == "2025-10"}
    assert filtered.page_count == segmented.page_count


def test_round_scoped_run_never_touches_another_rounds_ledger(main_fixture, segmented, roster, tmp_path):
    """The actual safety property --round exists for: a run filtered to one
    round group must never delete, disturb, or even look up another round's
    already-shipped output, because a packet outside the chosen round is
    never iterated by run_dispositions at all."""
    tags = [packet_tag(main_fixture.pdf_path, p) for p in segmented.packets]
    assert len(tags) >= 2
    round_labels = {tags[0]: "round_a", **{t: "round_b" for t in tags[1:]}}
    out_dir = tmp_path / "out"

    sid_a = _sid_for(roster, "Jordan Ames")
    first = run_dispositions(main_fixture.pdf_path, segmented, {tags[0]: sid_a}, roster, out_dir=out_dir, dpi=DPI)
    path_a = next(r for r in first if r.packet_tag == tags[0]).out_path
    assert path_a.exists()

    # A later "round_b" invocation rejects round_a's own tag (as if a
    # reviewer had confirmed non-consent for it) -- but this run is scoped
    # to round_b only, so tags[0] is filtered out and never iterated at
    # all, and its already-shipped output must survive completely
    # untouched, exactly as if this run had never seen its tag.
    round_b_segmented = filter_packets_by_round(main_fixture.pdf_path, segmented, round_labels, "round_b")
    assert tags[0] not in {packet_tag(main_fixture.pdf_path, p) for p in round_b_segmented.packets}
    second = run_dispositions(
        main_fixture.pdf_path, round_b_segmented, {tags[0]: None}, roster, out_dir=out_dir, dpi=DPI
    )
    assert not any(r.packet_tag == tags[0] for r in second)
    assert not any(r.deleted_path for r in second)
    assert path_a.exists()


# --- analyze_redaction_holds: read-only hold-volume reporting, never
# writes/redacts to disk or deletes anything (2026-08-13) ---


def test_analyze_redaction_holds_never_writes_anything(main_fixture, segmented, roster, tmp_path):
    out_dir = tmp_path / "out"
    results = analyze_redaction_holds(main_fixture.pdf_path, segmented, roster, dpi=DPI)
    assert not out_dir.exists()

    tags_with_header = {
        packet_tag(main_fixture.pdf_path, p) for p in segmented.packets if p.header_page_index is not None
    }
    assert {r.packet_tag for r in results} == tags_with_header


def test_analyze_redaction_holds_agrees_with_a_real_run_for_a_clean_packet(main_fixture, segmented, roster, tmp_path):
    clean_match_packet = segmented.packets[0]
    tag = packet_tag(main_fixture.pdf_path, clean_match_packet)

    results = analyze_redaction_holds(main_fixture.pdf_path, segmented, roster, dpi=DPI)
    analysis = next(r for r in results if r.packet_tag == tag)
    assert analysis.clean
    assert not analysis.detection_hold
    assert not analysis.uncovered_ink_advisory
    assert not analysis.leak_hold

    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"
    real = run_dispositions(main_fixture.pdf_path, segmented, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    real_result = next(r for r in real if r.packet_tag == tag)
    assert not real_result.held_back
    assert real_result.out_path is not None


def test_format_hold_analysis_report_groups_by_round(main_fixture, segmented, roster):
    round_labels = {packet_tag(main_fixture.pdf_path, p): FIXTURE_ROUND for p in segmented.packets}
    results = analyze_redaction_holds(main_fixture.pdf_path, segmented, roster, round_labels, dpi=DPI)
    report = format_hold_analysis_report(results)
    assert FIXTURE_ROUND in report
    assert "clean" in report


def test_pending_packet_in_a_different_pdf_never_deletes_another_pdfs_approved_output(tmp_path):
    """The actual production incident: running dispositions for a second
    scan file whose packets are still pending -- no decisions recorded at
    all -- must never touch another pdf's already-approved, already-written
    output sitting in the same out_dir. The old reconciliation swept the
    whole shared <teacher>/<period>/<type> directory against only the
    *current* pdf's decisions.values(), so a pending-only run (empty
    decisions) deleted every file already approved by a different pdf's own
    decisions store, since nothing scoped the sweep to files that pdf
    itself had ever written."""
    pdf_a = build_main_fixture(tmp_path / "a")
    pdf_b = build_main_fixture(tmp_path / "b")  # a second, independent scan
    roster_a = load_roster(pdf_a.roster_path)
    roster_b = load_roster(pdf_b.roster_path)
    seg_a = segment_pdf(pdf_a.pdf_path)
    seg_b = segment_pdf(pdf_b.pdf_path)
    tag_a = packet_tag(pdf_a.pdf_path, seg_a.packets[0])
    sid = _sid_for(roster_a, "Jordan Ames")
    out_dir = tmp_path / "out"

    results_a = run_dispositions(pdf_a.pdf_path, seg_a, {tag_a: sid}, roster_a, out_dir=out_dir, dpi=DPI)
    out_path = next(r for r in results_a if r.packet_tag == tag_a).out_path
    assert out_path.exists()

    # pdf B: every packet still pending -- no decisions recorded at all.
    results_b = run_dispositions(pdf_b.pdf_path, seg_b, {}, roster_b, out_dir=out_dir, dpi=DPI)
    assert all(r.pending for r in results_b)
    assert all(r.deleted_path is None for r in results_b)
    assert out_path.exists()


def test_two_worksheet_types_for_same_student_do_not_collide_in_out(tmp_path, monkeypatch):
    """Regression: an MPR packet and a PRT packet for the same student share
    the same teacher_code/period (both come from the SID alone -- see
    roster.py), so without worksheet_type in the output path they'd collide
    on the exact same <SID>.pdf, and the second write would silently
    clobber the first. This is the real incident: an MPR run's approved
    output was clobbered/deleted by a later PRT run."""
    import tests.make_fixture as fixture_mod

    mpr = build_main_fixture(tmp_path / "mpr")
    mpr_roster = load_roster(mpr.roster_path)
    mpr_seg = segment_pdf(mpr.pdf_path)
    mpr_tag = packet_tag(mpr.pdf_path, mpr_seg.packets[0])
    sid = _sid_for(mpr_roster, "Jordan Ames")

    monkeypatch.setattr(fixture_mod, "WORKSHEET_TYPE_TEXT", "PRT (01/2024)")
    prt = fixture_mod.build_main_fixture(tmp_path / "prt")
    prt_roster = load_roster(prt.roster_path)
    prt_seg = segment_pdf(prt.pdf_path)
    prt_tag = packet_tag(prt.pdf_path, prt_seg.packets[0])

    assert mpr_seg.packets[0].worksheet_type != prt_seg.packets[0].worksheet_type

    out_dir = tmp_path / "out"

    mpr_results = run_dispositions(mpr.pdf_path, mpr_seg, {mpr_tag: sid}, mpr_roster, out_dir=out_dir, dpi=DPI)
    mpr_out = next(r for r in mpr_results if r.packet_tag == mpr_tag).out_path
    assert mpr_out.exists()

    prt_results = run_dispositions(prt.pdf_path, prt_seg, {prt_tag: sid}, prt_roster, out_dir=out_dir, dpi=DPI)
    prt_out = next(r for r in prt_results if r.packet_tag == prt_tag).out_path
    assert prt_out.exists()

    assert mpr_out != prt_out
    assert mpr_out.exists()  # writing PRT's output must not have clobbered MPR's


# --- topic path segment, and the no-silent-overwrite backstop ---


def test_topic_from_filename_extracts_the_trailing_segment():
    from melredact.pipeline import topic_from_filename

    assert topic_from_filename("010406_PD1_PRT_EW.pdf") == "EW"
    assert topic_from_filename("010406_PD1_PRT_fo.pdf") == "FO"


def test_topic_from_filename_defaults_to_NA_with_constant_path_depth(tmp_path, roster):
    """No topic in the filename (the overwhelmingly common case, e.g. every
    non-010406 teacher, or 010406's own filenames before a topic session)
    must resolve to the stable NO_TOPIC literal, not an omitted segment --
    otherwise output path depth would vary per teacher."""
    from melredact.pipeline import NO_TOPIC, output_path, topic_from_filename

    assert topic_from_filename("010406_PD1_PRT.pdf") == NO_TOPIC
    assert topic_from_filename("Hannel MPR PD2.pdf") == NO_TOPIC

    entry = next(iter(roster))
    no_topic_path = output_path(tmp_path, entry, "PRT")
    with_topic_path = output_path(tmp_path, entry, "PRT", "EW")
    assert len(no_topic_path.relative_to(tmp_path).parts) == len(with_topic_path.relative_to(tmp_path).parts)
    # One level deeper than the topic segment itself now (see the round
    # segment, output_path's fifth component) -- parent is the round dir,
    # parent.parent is the topic dir.
    assert no_topic_path.parent.parent.name == NO_TOPIC


def test_different_topics_in_the_source_filename_do_not_collide(tmp_path, roster):
    """Two scan files for the same teacher/period/worksheet_type but
    different topics (the real motivating scenario: a teacher whose
    students complete several PRT sessions, one per topic) must land at
    distinct output paths purely from the topic segment, before the
    no-silent-overwrite backstop is ever needed."""
    from melredact.pipeline import output_path, topic_from_filename

    entry = next(iter(roster))
    ew_topic = topic_from_filename("010406_PD1_PRT_EW.pdf")
    fr_topic = topic_from_filename("010406_PD1_PRT_FR.pdf")
    ew_path = output_path(tmp_path, entry, "PRT", ew_topic)
    fr_path = output_path(tmp_path, entry, "PRT", fr_topic)
    assert ew_path != fr_path
    assert ew_path.parent.parent.name == "EW"
    assert fr_path.parent.parent.name == "FR"


def test_second_packet_claiming_an_owned_path_gets_suffixed_not_overwritten(main_fixture, segmented, roster, tmp_path):
    """The no-silent-overwrite backstop: two distinct packets in the same
    scan file, both decided to the same student and worksheet type (the
    within-file version of the multi-PRT-per-student scenario topic alone
    doesn't disambiguate), must never let the second write clobber the
    first -- it gets a numbered-suffix path instead, reported prominently."""
    tag0 = packet_tag(main_fixture.pdf_path, segmented.packets[0])
    tag1 = packet_tag(main_fixture.pdf_path, segmented.packets[1])
    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"

    results = run_dispositions(main_fixture.pdf_path, segmented, {tag0: sid, tag1: sid}, roster, out_dir=out_dir, dpi=DPI)
    r0 = next(r for r in results if r.packet_tag == tag0)
    r1 = next(r for r in results if r.packet_tag == tag1)

    assert r0.out_path.name == f"{sid}.pdf"
    assert r0.collision_note is None
    assert r1.out_path.name == f"{sid}_2.pdf"
    assert r1.collision_note is not None
    assert tag0 in r1.collision_note
    assert r0.out_path.exists()
    assert r1.out_path.exists()
    assert r0.out_path != r1.out_path


def test_deleting_a_tags_output_removes_the_exact_suffixed_file_it_wrote(main_fixture, segmented, roster, tmp_path):
    """The ledger stores the literal path each tag wrote (not just its SID),
    specifically so a rejection or correction deletes the exact -- possibly
    suffixed -- file that tag produced, never a path recomputed from the
    SID (which would guess the un-suffixed path and delete nothing, or
    worse, the *other* packet's file)."""
    tag0 = packet_tag(main_fixture.pdf_path, segmented.packets[0])
    tag1 = packet_tag(main_fixture.pdf_path, segmented.packets[1])
    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"

    first = run_dispositions(main_fixture.pdf_path, segmented, {tag0: sid, tag1: sid}, roster, out_dir=out_dir, dpi=DPI)
    r0 = next(r for r in first if r.packet_tag == tag0)
    r1 = next(r for r in first if r.packet_tag == tag1)
    assert r0.out_path.exists()
    assert r1.out_path.exists()

    second = run_dispositions(
        main_fixture.pdf_path, segmented, {tag0: sid, tag1: None}, roster, out_dir=out_dir, dpi=DPI
    )
    assert r0.out_path.exists(), "an unrelated tag's own output must survive another tag's rejection"
    assert not r1.out_path.exists(), "the exact suffixed file this tag wrote must be removed"
    deleted = [r for r in second if r.deleted_path == r1.out_path]
    assert len(deleted) == 1
    assert deleted[0].sid == sid


def test_unknown_sid_in_decisions_is_held_back_not_raised(main_fixture, segmented, roster, tmp_path):
    """A bad decision entry (e.g. a typo'd SID) holds back only that one
    packet -- see pipeline.py's module docstring -- rather than aborting
    the whole run and blocking every other already-approved packet."""
    tag0 = packet_tag(main_fixture.pdf_path, segmented.packets[0])
    tag1 = packet_tag(main_fixture.pdf_path, segmented.packets[1])
    good_sid = _sid_for(roster, "Jordan Ames")
    results = run_dispositions(
        main_fixture.pdf_path,
        segmented,
        {tag0: "9999999999", tag1: good_sid},
        roster,
        out_dir=tmp_path / "out",
        dpi=DPI,
    )
    bad = next(r for r in results if r.packet_tag == tag0)
    assert bad.held_back
    assert bad.out_path is None
    assert "not on roster" in bad.reason
    good = next(r for r in results if r.packet_tag == tag1)
    assert not good.held_back
    assert good.out_path.exists()


def test_packet_with_unresolved_issues_is_held_back_even_with_a_decision(tmp_path, roster):
    edge_pdf = build_footer_edge_case_fixture(tmp_path / "edge")
    seg = segment_pdf(edge_pdf)
    flagged = next(p for p in seg.packets if p.issues)
    tag = packet_tag(edge_pdf, flagged)
    any_sid = next(iter(roster)).sid
    results = run_dispositions(edge_pdf, seg, {tag: any_sid}, roster, out_dir=tmp_path / "out", dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)
    assert result.held_back
    assert result.out_path is None
    assert "unresolved issues" in result.reason


def test_composition_override_releases_an_unreadable_footer_hold_and_writes_the_packet(tmp_path, roster):
    """The real p086 shape: a packet whose continuation page's footer is
    unreadable but whose redaction is otherwise correct must become
    assignable once a human confirms the page composition is right --
    without the override, it's held exactly like any other unresolved-
    issues packet (see the previous test)."""
    pdf_path = build_unreadable_continuation_footer_fixture(tmp_path / "unreadable_footer")
    seg = segment_pdf(pdf_path)
    packet = next(p for p in seg.packets if p.issues)
    assert "unreadable footer, cannot verify sequence" in packet.issues[0]
    tag = packet_tag(pdf_path, packet)
    sid = _sid_for(roster, "Jordan Ames")

    without_override = run_dispositions(pdf_path, seg, {tag: sid}, roster, out_dir=tmp_path / "out1", dpi=DPI)
    held = next(r for r in without_override if r.packet_tag == tag)
    assert held.held_back
    assert held.out_path is None

    released = run_dispositions(
        pdf_path, seg, {tag: sid}, roster, out_dir=tmp_path / "out2", dpi=DPI, composition_overrides={tag}
    )
    result = next(r for r in released if r.packet_tag == tag)
    assert not result.held_back
    assert result.out_path is not None and result.out_path.exists()
    assert "human-confirmed page composition" in result.reason


def test_composition_override_does_not_release_an_orientation_issue(main_fixture, roster, tmp_path):
    """An orientation issue has its own real fix (rotate the page) --
    confirming page composition must never silently wave one through, even
    if the same packet's other issues would otherwise qualify."""
    rotated_path = build_rotated_page_copy(main_fixture.pdf_path, tmp_path, 0, 90, "rotated_composition.pdf")
    seg = segment_pdf(rotated_path)
    packet = next(p for p in seg.packets if any("orientation" in i for i in p.issues))
    tag = packet_tag(rotated_path, packet)
    sid = _sid_for(roster, "Jordan Ames")

    results = run_dispositions(
        rotated_path, seg, {tag: sid}, roster, out_dir=tmp_path / "out", dpi=DPI, composition_overrides={tag}
    )
    result = next(r for r in results if r.packet_tag == tag)
    assert result.held_back
    assert result.out_path is None
    assert "composition override does not cover" in result.reason


def test_composition_override_does_not_release_a_consensus_ink_or_verify_hold(main_fixture, segmented, roster, tmp_path, monkeypatch):
    """A composition override is never even consulted by the two real-
    finding holds (consensus-ink anomaly, verify_no_leaked_names) -- those
    stay non-overridable by any confirmation, composition included."""
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
    results = run_dispositions(
        main_fixture.pdf_path, segmented, {tag: sid}, roster, out_dir=tmp_path / "out", dpi=DPI,
        composition_overrides={tag},
    )
    result = next(r for r in results if r.packet_tag == tag)
    assert result.held_back
    assert "leaks" in result.reason


def test_composition_overrides_persist_and_reload(tmp_path):
    decisions_dir = tmp_path / "decisions"
    save_composition_overrides("some_scan.pdf", {"some_scan_p000", "some_scan_p010"}, decisions_dir=decisions_dir)
    assert composition_overrides_path("some_scan.pdf", decisions_dir).exists()
    reloaded = load_composition_overrides("some_scan.pdf", decisions_dir=decisions_dir)
    assert reloaded == {"some_scan_p000", "some_scan_p010"}
    assert load_composition_overrides("never_saved.pdf", decisions_dir=decisions_dir) == set()


def test_leak_finding_deletes_output_and_holds_back_not_raises(main_fixture, segmented, roster, tmp_path, monkeypatch):
    """If the verify pass ever finds a leak, run_dispositions must not
    leave the leaking file sitting in out_dir -- and must hold back only
    this packet, not abort the whole run."""
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
    results = run_dispositions(main_fixture.pdf_path, segmented, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)
    assert result.held_back
    assert "leaks" in result.reason
    assert not output_path(out_dir, entry, packet.worksheet_type, round_label=FIXTURE_ROUND).exists()


def test_undetected_header_border_holds_back_only_that_packet_not_the_whole_run(
    main_fixture, segmented, roster, tmp_path, monkeypatch
):
    """Regression for the real incident: SID 0204150204's page had a
    header border detect_header_band couldn't confidently locate. Before
    this fix, run_dispositions raised and aborted the entire run, which
    meant every *other* already-approved packet in the same file was
    silently skipped too. It must now hold back only the one packet with
    the bad header and still write everything else."""
    import dataclasses

    import melredact.pipeline as pipeline_mod

    real_redact_packet = pipeline_mod._redact_packet
    bad_packet = segmented.packets[0]
    bad_tag = packet_tag(main_fixture.pdf_path, bad_packet)

    def fake_undetected_band(*args, **kwargs):
        result = real_redact_packet(*args, **kwargs)
        if packet_tag(main_fixture.pdf_path, args[1]) == bad_tag:
            return dataclasses.replace(result, band=dataclasses.replace(result.band, detected=False))
        return result

    monkeypatch.setattr(pipeline_mod, "_redact_packet", fake_undetected_band)

    good_packet = segmented.packets[1]
    good_tag = packet_tag(main_fixture.pdf_path, good_packet)
    bad_sid = _sid_for(roster, "Jordan Ames")
    good_sid = next(e.sid for e in roster if e.sid != bad_sid)
    decisions = {bad_tag: bad_sid, good_tag: good_sid}

    out_dir = tmp_path / "out"
    results = run_dispositions(main_fixture.pdf_path, segmented, decisions, roster, out_dir=out_dir, dpi=DPI)

    bad_result = next(r for r in results if r.packet_tag == bad_tag)
    assert bad_result.held_back
    assert "header border" in bad_result.reason
    assert bad_result.out_path is None

    good_result = next(r for r in results if r.packet_tag == good_tag)
    assert not good_result.held_back
    assert good_result.out_path is not None
    assert good_result.out_path.exists()


def test_180_degree_rotated_header_page_is_held_pending_confirmation_end_to_end(main_fixture, roster, tmp_path):
    """Detect-and-ask (2026-08-14, see orientation.py's own module
    docstring and CLAUDE.md's rotation-audit correction): a confident but
    unconfirmed nonzero rotation is never auto-applied, even at 180
    degrees where the old auto-apply design would have silently corrected
    it. segment_pdf reads the still-rotated page exactly as found, and
    run_dispositions refuses it via the ordinary "unresolved issues" gate
    -- no separate hold mechanism needed. See the sibling test below for
    the human-confirmed path that actually ships."""
    rotated_path = build_rotated_page_copy(main_fixture.pdf_path, tmp_path, 0, 180, "rotated_180_unconfirmed.pdf")

    seg = segment_pdf(rotated_path)
    packet_with_page0 = next(p for p in seg.packets if 0 in p.page_indices)
    assert any("page 0: orientation detected as rotated 180" in i for i in packet_with_page0.issues)
    assert any("not yet confirmed by a reviewer" in i for i in packet_with_page0.issues)

    tag = packet_tag(rotated_path, packet_with_page0)
    sid = _sid_for(roster, "Jordan Ames")

    out_dir = tmp_path / "out"
    results = run_dispositions(rotated_path, seg, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)
    assert result.held_back
    assert "orientation detected as rotated 180" in result.reason
    assert result.out_path is None


def test_180_degree_rotated_header_page_redacts_correctly_once_a_human_confirms_the_rotation(
    main_fixture, roster, tmp_path
):
    """Regression for the real incident this session audited: two real
    pages in data/PRT/010406_PD1_PRT.pdf are rotated a clean 180 degrees
    by a duplex-scanner flip (see CLAUDE.md's page-orientation section).
    Before orientation normalization existed, detect_header_band/
    locate_header_anchors assumed an upright page and would cover the
    wrong region on a rotated one. A human's override -- the same shape
    review_app.py's rotate controls persist via pipeline.save_orientation_
    overrides -- makes this packet processable and redacts it correctly,
    through the actual production path (segment_pdf -> run_dispositions),
    the same path a real run uses, with no monkeypatching."""
    rotated_path = build_rotated_page_copy(main_fixture.pdf_path, tmp_path, 0, 180, "rotated_180_confirmed.pdf")
    overrides = {0: 180}

    save_orientation_overrides(rotated_path, overrides, decisions_dir=tmp_path / "decisions")
    reloaded = load_orientation_overrides(rotated_path, decisions_dir=tmp_path / "decisions")
    assert reloaded == overrides

    seg = segment_pdf(rotated_path, orientation_overrides=reloaded)
    header_packet = seg.packets[0]
    assert header_packet.header_page_index == 0
    assert not any("orientation" in issue for issue in header_packet.issues)

    tag = packet_tag(rotated_path, header_packet)
    sid = _sid_for(roster, "Jordan Ames")

    out_dir = tmp_path / "out"
    results = run_dispositions(
        rotated_path, seg, {tag: sid}, roster, out_dir=out_dir, dpi=DPI, orientation_overrides=reloaded
    )
    result = next(r for r in results if r.packet_tag == tag)

    assert not result.held_back, result.reason
    assert result.out_path is not None
    assert result.out_path.exists()

    from melredact.redact import verify_no_leaked_names

    assert verify_no_leaked_names(result.out_path, roster) == []


def test_orientation_override_persists_and_reproduces_on_rerun(main_fixture, tmp_path):
    """A human's rotation choice, once saved via pipeline.save_orientation_
    overrides, must reproduce the identical resolved orientation on a
    later, independent load -- the same "record it once, never ask again"
    contract detection_overrides/manual_geometry already provide, applied
    to orientation.py's own sidecar (decisions/<pdf-stem>.orientation.json)."""
    from melredact.orientation import normalize_pdf

    rotated_path = build_rotated_page_copy(main_fixture.pdf_path, tmp_path, 0, 90, "rotated_90_persist.pdf")
    decisions_dir = tmp_path / "decisions"

    assert load_orientation_overrides(rotated_path, decisions_dir=decisions_dir) == {}
    save_orientation_overrides(rotated_path, {0: 90}, decisions_dir=decisions_dir)

    reloaded = load_orientation_overrides(rotated_path, decisions_dir=decisions_dir)
    assert reloaded == {0: 90}

    first = normalize_pdf(rotated_path, overrides=reloaded)
    assert first.pages[0].applied_angle == 90
    assert first.pages[0].source == "human"

    # A completely independent reload of the sidecar (simulating a fresh
    # `cli.py run`/Streamlit session) must resolve to the exact same thing.
    second_overrides = load_orientation_overrides(rotated_path, decisions_dir=decisions_dir)
    second = normalize_pdf(rotated_path, overrides=second_overrides)
    assert second.pages[0].applied_angle == 90
    assert second.normalized_path == first.normalized_path


def test_detection_override_releases_the_hold_and_writes_the_packet(
    main_fixture, segmented, roster, tmp_path, monkeypatch
):
    """The bug this fixes: SID 0204150204 approving the decision in the
    review UI did nothing, because the detection-confidence hold fired
    unconditionally regardless of human approval, permanently blocking the
    packet from ever being written. A human who has looked at the preview
    (which draws the exact fallback/anchor-derived box even when
    detected=False) and explicitly released the hold via
    `detection_overrides` must get a written file, using the geometry
    already drawn -- not another hold."""
    import dataclasses

    import melredact.pipeline as pipeline_mod

    real_redact_packet = pipeline_mod._redact_packet
    bad_packet = segmented.packets[0]
    bad_tag = packet_tag(main_fixture.pdf_path, bad_packet)

    def fake_undetected_band(*args, **kwargs):
        result = real_redact_packet(*args, **kwargs)
        if packet_tag(main_fixture.pdf_path, args[1]) == bad_tag:
            return dataclasses.replace(result, band=dataclasses.replace(result.band, detected=False))
        return result

    monkeypatch.setattr(pipeline_mod, "_redact_packet", fake_undetected_band)

    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"
    results = run_dispositions(
        main_fixture.pdf_path,
        segmented,
        {bad_tag: sid},
        roster,
        out_dir=out_dir,
        dpi=DPI,
        detection_overrides={bad_tag},
    )

    result = next(r for r in results if r.packet_tag == bad_tag)
    assert not result.held_back
    assert result.out_path is not None
    assert result.out_path.exists()
    assert "override" in result.reason


def test_detection_override_releases_despite_an_uncovered_ink_advisory(
    main_fixture, segmented, roster, tmp_path, monkeypatch
):
    """find_uncovered_group_words' finding is advisory only (2026-08-14,
    see CLAUDE.md) -- a packet approved for release from the detection-
    confidence hold must still write even when it also carries an
    uncovered-ink finding, with that finding surfaced as an advisory on the
    written result rather than blocking it. (Formerly this scenario stayed
    held back, back when uncovered-ink was itself a hold; the detection-
    confidence-vs-verify_no_leaked_names boundary this test used to also
    guard is still covered separately by
    test_detection_override_does_not_release_a_verify_leak_hold below.)"""
    import dataclasses

    import melredact.pipeline as pipeline_mod

    real_redact_packet = pipeline_mod._redact_packet
    bad_packet = segmented.packets[0]
    bad_tag = packet_tag(main_fixture.pdf_path, bad_packet)

    def fake_undetected_band_with_advisory(*args, **kwargs):
        result = real_redact_packet(*args, **kwargs)
        if packet_tag(main_fixture.pdf_path, args[1]) == bad_tag:
            fake_word = {"text": "Leaked", "x0": 0, "x1": 10, "top": 0, "bottom": 10}
            return dataclasses.replace(
                result,
                band=dataclasses.replace(result.band, detected=False),
                uncovered_group_words=[fake_word],
            )
        return result

    monkeypatch.setattr(pipeline_mod, "_redact_packet", fake_undetected_band_with_advisory)

    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"
    results = run_dispositions(
        main_fixture.pdf_path,
        segmented,
        {bad_tag: sid},
        roster,
        out_dir=out_dir,
        dpi=DPI,
        detection_overrides={bad_tag},
    )

    result = next(r for r in results if r.packet_tag == bad_tag)
    assert not result.held_back
    assert result.out_path is not None
    assert result.out_path.exists()
    assert "override" in result.reason
    assert result.advisory_uncovered_words


def test_detection_override_does_not_release_a_verify_leak_hold(
    main_fixture, segmented, roster, tmp_path, monkeypatch
):
    """Same boundary as above, exercised through the other unconditional
    check (verify_no_leaked_names) rather than uncovered_group_words --
    both leak-type holds must stay non-overridable even when the packet
    also carries an approved detection-confidence override."""
    import dataclasses

    from melredact.redact import LeakFinding
    import melredact.pipeline as pipeline_mod

    real_redact_packet = pipeline_mod._redact_packet
    bad_packet = segmented.packets[0]
    bad_tag = packet_tag(main_fixture.pdf_path, bad_packet)

    def fake_undetected_band(*args, **kwargs):
        result = real_redact_packet(*args, **kwargs)
        if packet_tag(main_fixture.pdf_path, args[1]) == bad_tag:
            return dataclasses.replace(result, band=dataclasses.replace(result.band, detected=False))
        return result

    monkeypatch.setattr(pipeline_mod, "_redact_packet", fake_undetected_band)
    monkeypatch.setattr(
        pipeline_mod,
        "verify_no_leaked_names",
        lambda out_path, roster: [LeakFinding(page_index=0, sid="x", token="leaked")],
    )

    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"
    results = run_dispositions(
        main_fixture.pdf_path,
        segmented,
        {bad_tag: sid},
        roster,
        out_dir=out_dir,
        dpi=DPI,
        detection_overrides={bad_tag},
    )

    result = next(r for r in results if r.packet_tag == bad_tag)
    assert result.held_back
    assert result.out_path is None
    assert "leaks" in result.reason


def test_detection_overrides_round_trip(tmp_path, main_fixture):
    from melredact.pipeline import overrides_path, save_detection_overrides

    d = tmp_path / "decisions"
    overrides = {"tag_a", "tag_b"}
    save_detection_overrides(main_fixture.pdf_path, overrides, decisions_dir=d)
    assert overrides_path(main_fixture.pdf_path, d).exists()
    assert load_detection_overrides(main_fixture.pdf_path, decisions_dir=d) == overrides


def test_load_detection_overrides_missing_file_returns_empty_set(tmp_path, main_fixture):
    assert load_detection_overrides(main_fixture.pdf_path, decisions_dir=tmp_path / "nope") == set()


def _build_packet14_style_fixture(tmp_path):
    """Reproduces the real PRT packet 14 bug's shape: a Group-row word list
    ("Priya Xavier Noor" stands in for the real, fictional names -- never
    commit real student PII, see CLAUDE.md/data/README.md) handwritten far
    enough below the printed row that it overflows past the header's own
    detected bottom border, not just sideways past COLUMN_SPLIT_X. Returns
    (pdf_path, roster, seg, tag, sid, overflow_top_pt) for callers to drive
    run_dispositions / release_from_manual_queue against."""
    from melredact.config import FOOTER_WORKSHEET_TYPE, GROUP_ANCHOR, HEADER_BAND_FALLBACK
    from tests.make_fixture import InvisibleText, PdfBuilder, _write_roster_csv, render_header_image

    overflow_text = "Priya Xavier Noor"
    img = render_header_image(
        name_text="Alex Rivera",
        teacher_text="Hannel",
        group_text="",
        date_text="10/03/2025",
        period_text="02",
        worksheet_type="PRT (01/2024)",
        page_marker="Page 1 of 1",
        shade_blank_rows=False,
    )
    overflow_top_pt = HEADER_BAND_FALLBACK["bottom"] + 12
    items = [
        InvisibleText("Name:", GROUP_ANCHOR["x0"], 68, 9),
        InvisibleText("Alex Rivera", 150, 68),
        InvisibleText("Group members, if any:", GROUP_ANCHOR["x0"], GROUP_ANCHOR["top"], 9),
        InvisibleText(overflow_text, 150, overflow_top_pt),
        InvisibleText("10/03/2025", 450, 68),
        InvisibleText("02", 450, 87),
        InvisibleText("PRT 01 2024", FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
        InvisibleText("Page 1 of 1", 513, 747, 9),
    ]
    builder = PdfBuilder()
    builder.add_page(img, items)
    pdf_path = tmp_path / "packet14.pdf"
    builder.save(pdf_path)

    sid = "0204159901"
    roster_path = tmp_path / "roster.csv"
    _write_roster_csv(roster_path, [(sid, "Rivera", "Alex")])
    roster = load_roster(roster_path)

    seg = segment_pdf(pdf_path)
    tag = packet_tag(pdf_path, seg.packets[0])
    return pdf_path, roster, seg, tag, sid, overflow_top_pt


def test_vertical_group_row_overflow_is_advisory_not_held(tmp_path):
    """2026-08-14: find_uncovered_group_words' finding is advisory, not a
    hold (see CLAUDE.md's "From detection-gates-workflow to human-reviews-
    everything" section -- real-data evidence found zero true positives
    across 41 real held packets on two teachers, and the reviewer now looks
    at every page of every packet regardless via review_app.py's per-packet
    editor). The real PRT packet 14 shape -- Group-row ink overflowing past
    the header's own detected border -- must still be *flagged*
    (find_uncovered_group_words itself is unchanged, see test_redact.py's
    own unit-level regression fixtures for bugs 4/6/7), but it must no
    longer hold the packet back or queue it: this runs the real
    segment_pdf -> run_dispositions path against a decided packet -- no
    monkeypatching -- the same path a real reviewer's approval goes
    through."""
    from melredact.pipeline import list_manual_queue, output_path

    pdf_path, roster, seg, tag, sid, _ = _build_packet14_style_fixture(tmp_path)
    out_dir = tmp_path / "out"
    results = run_dispositions(pdf_path, seg, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)

    assert not result.held_back, result.reason
    assert result.out_path is not None
    assert result.out_path.exists()
    assert result.advisory_uncovered_words, "the finding itself must still fire -- only its consequence changed"
    assert result.geometry_source == "automatic"

    entry = roster.by_sid[sid]
    expected_path = output_path(out_dir, entry, seg.packets[0].worksheet_type, round_label=FIXTURE_ROUND)
    assert result.out_path == expected_path
    assert list_manual_queue(out_dir) == [], "an advisory finding must never land the packet in the manual queue"


def _detection_hold_fake(pdf_path, bad_tag):
    """Returns a `_redact_packet` replacement that forces run_dispositions'
    undetected-header-border hold for exactly `bad_tag`, regardless of what
    the real detector found -- the reusable pattern the detection_override
    tests above already use, factored out here so the manual-queue release
    tests below (which need a genuinely queueable hold, now that
    uncovered-ink no longer queues anything -- see CLAUDE.md) can force one
    without depending on the packet14 overflow fixture at all. Apply via
    `with monkeypatch.context() as mp: mp.setattr(pipeline_mod,
    "_redact_packet", fake)` so it's active only for the call that needs
    the forced hold, never for a later release_from_manual_queue call that
    should see the real, unpatched redact_packet."""
    import dataclasses

    import melredact.pipeline as pipeline_mod

    real_redact_packet = pipeline_mod._redact_packet

    def fake_undetected_band(*args, **kwargs):
        result = real_redact_packet(*args, **kwargs)
        if packet_tag(pdf_path, args[1]) == bad_tag:
            return dataclasses.replace(result, band=dataclasses.replace(result.band, detected=False))
        return result

    return fake_undetected_band


def test_manual_queue_release_with_a_corrected_band_writes_and_clears_the_queue(
    main_fixture, segmented, roster, tmp_path, monkeypatch
):
    """The backstop working as intended: a packet is genuinely held for
    detection confidence (forced here, since real-data-informed
    find_uncovered_group_words no longer queues anything on its own -- see
    CLAUDE.md), a human supplies a corrected band, and release_from_
    manual_queue re-checks the packet before writing anything -- since it
    now passes, the file lands in the real out/ tree and the queue entry
    is cleared."""
    from melredact.pipeline import list_manual_queue, output_path, release_from_manual_queue
    from melredact.redact import HeaderBand

    bad_packet = segmented.packets[0]
    bad_tag = packet_tag(main_fixture.pdf_path, bad_packet)
    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"

    import melredact.pipeline as pipeline_mod

    with monkeypatch.context() as mp:
        mp.setattr(pipeline_mod, "_redact_packet", _detection_hold_fake(main_fixture.pdf_path, bad_tag))
        run_dispositions(main_fixture.pdf_path, segmented, {bad_tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    assert len(list_manual_queue(out_dir)) == 1

    corrected_band = HeaderBand(left=38, top=58, right=574, bottom=148, detected=True)
    release = release_from_manual_queue(
        main_fixture.pdf_path, bad_packet, bad_tag, sid, roster, corrected_band, out_dir=out_dir, dpi=DPI
    )

    assert release.released, release.reason
    entry = roster.by_sid[sid]
    expected_path = output_path(out_dir, entry, bad_packet.worksheet_type, round_label=FIXTURE_ROUND)
    assert release.out_path == expected_path
    assert expected_path.exists()
    assert list_manual_queue(out_dir) == []


def test_manual_queue_release_with_a_geometry_that_still_leaks_the_name_stays_queued(
    main_fixture, segmented, roster, tmp_path, monkeypatch
):
    """The boundary that keeps this a backstop, not an override: a human
    can supply geometry that still fails an unconditional check -- here,
    verify_no_leaked_names, since a header_bbox_override too small to
    reach the real Name ink leaves it sitting in the kept text layer.
    release_from_manual_queue must refuse to write anything in that case
    and leave the packet queued -- the automated check, not the human's
    say-so alone, is what actually gates a write (see CLAUDE.md's "Keep
    verify unchanged and unconditional" -- this is exactly that guarantee,
    exercised through the manual-editor release path)."""
    from melredact.pipeline import list_manual_queue, output_path, release_from_manual_queue

    bad_packet = segmented.packets[0]
    bad_tag = packet_tag(main_fixture.pdf_path, bad_packet)
    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"

    import melredact.pipeline as pipeline_mod

    with monkeypatch.context() as mp:
        mp.setattr(pipeline_mod, "_redact_packet", _detection_hold_fake(main_fixture.pdf_path, bad_tag))
        run_dispositions(main_fixture.pdf_path, segmented, {bad_tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    assert len(list_manual_queue(out_dir)) == 1

    tiny = (0.0, 0.0, 1.0, 1.0)
    release = release_from_manual_queue(
        main_fixture.pdf_path, bad_packet, bad_tag, sid, roster, None,
        out_dir=out_dir, dpi=DPI, header_bbox_override=(tiny, tiny),
    )

    assert not release.released
    assert "leaks" in release.reason
    entry = roster.by_sid[sid]
    assert not output_path(out_dir, entry, bad_packet.worksheet_type, round_label=FIXTURE_ROUND).exists()
    assert len(list_manual_queue(out_dir)) == 1


def _build_two_page_page2_name_fixture(tmp_path):
    """A clean, auto-detectable header page (page 1) plus a second page
    carrying its own extra handwritten name -- the real shape this fixture
    exists to reproduce: a PRT packet's page 2 can carry its own name (see
    CLAUDE.md's manual-redaction-editor section), which redact_packet's
    header-page-only geometry has no way to reach on its own. Fictional
    names throughout, never real student PII (see CLAUDE.md/data/
    README.md)."""
    from melredact.config import FOOTER_WORKSHEET_TYPE, GROUP_ANCHOR
    from tests.make_fixture import InvisibleText, PdfBuilder, _write_roster_csv, render_continuation_image, render_header_image

    page1_img = render_header_image(
        name_text="Jamie Chen",
        teacher_text="Hannel",
        group_text="",
        date_text="10/03/2025",
        period_text="02",
        worksheet_type="PRT (01/2024)",
        page_marker="Page 1 of 2",
        shade_blank_rows=False,
    )
    page2_extra_name = "Morgan Lee"
    page2_extra_top = 120.0
    page2_extra_x = 100.0
    page2_img = render_continuation_image(worksheet_type="PRT (01/2024)", page_marker="Page 2 of 2", body="(continued)")

    page1_items = [
        InvisibleText("Name:", GROUP_ANCHOR["x0"], 68, 9),
        InvisibleText("Jamie Chen", 150, 68),
        InvisibleText("Group members, if any:", GROUP_ANCHOR["x0"], GROUP_ANCHOR["top"], 9),
        InvisibleText("10/03/2025", 450, 68),
        InvisibleText("02", 450, 87),
        InvisibleText("PRT 01 2024", FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
        InvisibleText("Page 1 of 2", 513, 747, 9),
    ]
    page2_items = [
        InvisibleText("(continued)", 45, 40),
        InvisibleText(page2_extra_name, page2_extra_x, page2_extra_top),
        InvisibleText("PRT 01 2024", FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
        InvisibleText("Page 2 of 2", 513, 747, 9),
    ]
    builder = PdfBuilder()
    builder.add_page(page1_img, page1_items)
    builder.add_page(page2_img, page2_items)
    pdf_path = tmp_path / "page2name.pdf"
    builder.save(pdf_path)

    sid = "0204159902"
    roster_path = tmp_path / "roster.csv"
    _write_roster_csv(roster_path, [(sid, "Chen", "Jamie"), ("0204159903", "Lee", "Morgan")])
    roster = load_roster(roster_path)
    seg = segment_pdf(pdf_path)
    tag = packet_tag(pdf_path, seg.packets[0])
    return pdf_path, roster, seg, tag, sid, page2_extra_name, page2_extra_top, page2_extra_x


def test_manual_header_region_releases_the_packet_with_the_same_output_shape_as_automatic(tmp_path):
    """The editor's `header_bbox_override` path (two independently-dragged
    rectangles, not a single HeaderBand) must produce a release that's
    indistinguishable in shape from what the automatic path would have
    produced had detection succeeded cleanly: a single-page PDF, a clean
    verify_no_leaked_names pass, and a real file at the natural output
    path."""
    from melredact.config import GROUP_ANCHOR
    from melredact.pipeline import release_from_manual_queue
    from melredact.redact import HeaderBand, redact_bboxes_for_band, verify_no_leaked_names
    from melredact.pdfio import open_pdf as _open_pdf

    pdf_path, roster, seg, tag, sid, overflow_top_pt = _build_packet14_style_fixture(tmp_path)
    out_dir = tmp_path / "out"
    corrected_band = HeaderBand(left=38, top=58, right=574, bottom=overflow_top_pt + 20, detected=True)
    header_bbox_override = redact_bboxes_for_band(corrected_band, GROUP_ANCHOR["top"])

    release = release_from_manual_queue(
        pdf_path, seg.packets[0], tag, sid, roster, None,
        out_dir=out_dir, dpi=DPI, header_bbox_override=header_bbox_override,
    )
    assert release.released, release.reason
    assert release.geometry_source == "manual"
    entry = roster.by_sid[sid]
    expected_path = output_path(out_dir, entry, seg.packets[0].worksheet_type, round_label=FIXTURE_ROUND)
    assert release.out_path == expected_path
    with _open_pdf(expected_path) as pdf:
        assert len(pdf.pages) == seg.packets[0].n_pages == 1
    assert verify_no_leaked_names(expected_path, roster) == []


def test_manual_header_region_with_uncovered_ink_still_releases_as_advisory(tmp_path):
    """2026-08-14: find_uncovered_group_words' finding is advisory, not a
    hold (see CLAUDE.md). A manually-drawn region that still doesn't reach
    the real overflow ink must still release the packet -- the finding is
    carried onto the result (ManualReleaseResult.advisory_uncovered_words),
    not used to refuse the write. This also demonstrates the editor is
    reachable for a packet that was never held/queued in the first place
    -- release_from_manual_queue is called here directly against a packet
    fresh out of segment_pdf, no prior run_dispositions call at all (see
    CLAUDE.md's "Make the editor reachable for any packet" section)."""
    from melredact.config import GROUP_ANCHOR
    from melredact.pipeline import list_manual_queue, output_path, release_from_manual_queue
    from melredact.redact import HeaderBand, redact_bboxes_for_band

    pdf_path, roster, seg, tag, sid, overflow_top_pt = _build_packet14_style_fixture(tmp_path)
    out_dir = tmp_path / "out"

    still_short_band = HeaderBand(left=38, top=58, right=574, bottom=overflow_top_pt - 5, detected=True)
    header_bbox_override = redact_bboxes_for_band(still_short_band, GROUP_ANCHOR["top"])

    release = release_from_manual_queue(
        pdf_path, seg.packets[0], tag, sid, roster, None,
        out_dir=out_dir, dpi=DPI, header_bbox_override=header_bbox_override,
    )
    assert release.released, release.reason
    assert release.advisory_uncovered_words, "the finding must still be surfaced, just no longer blocking"
    entry = roster.by_sid[sid]
    expected_path = output_path(out_dir, entry, seg.packets[0].worksheet_type, round_label=FIXTURE_ROUND)
    assert release.out_path == expected_path
    assert expected_path.exists()
    assert list_manual_queue(out_dir) == []


def test_page_2_region_redacts_page_2_and_leaves_page_1_unchanged(tmp_path):
    """extra_page_regions must reach a page redact_packet's own header-page
    geometry has no way to touch -- redacting a name on page 2 must not
    perturb page 1's own output at all."""
    from melredact.redact import redact_packet

    pdf_path, roster, seg, tag, sid, extra_name, extra_top, extra_x = _build_two_page_page2_name_fixture(tmp_path)
    packet = seg.packets[0]
    assert packet.n_pages == 2

    baseline_path = tmp_path / "baseline.pdf"
    redact_packet(pdf_path, packet, baseline_path, dpi=DPI)
    import pdfplumber

    with pdfplumber.open(baseline_path) as pdf:
        baseline_page1_text = pdf.pages[0].extract_text() or ""
        baseline_page2_text = pdf.pages[1].extract_text() or ""
    assert "lee" in baseline_page2_text.lower(), "fixture sanity: the extra name must survive when no page-2 region is given"

    page2_bbox = (extra_x - 10, extra_top - 5, extra_x + 200, extra_top + 15)
    edited_path = tmp_path / "edited.pdf"
    result = redact_packet(pdf_path, packet, edited_path, dpi=DPI, extra_page_regions={1: [page2_bbox]})
    assert result.uncovered_group_words == []

    with pdfplumber.open(edited_path) as pdf:
        edited_page1_text = pdf.pages[0].extract_text() or ""
        edited_page2_text = pdf.pages[1].extract_text() or ""

    assert edited_page1_text == baseline_page1_text, "page 1 must be byte-identical to the automatic path's own output"
    assert "morgan" not in edited_page2_text.lower()
    assert "lee" not in edited_page2_text.lower()


def _build_two_page_two_names_on_page2_fixture(tmp_path):
    """Same shape as _build_two_page_page2_name_fixture, but page 2 carries
    TWO extra handwritten names at well-separated positions -- the real
    shape the manual editor's canvas needs to redact with two independently
    drawn rectangles on the same page (see the drag-corner editor's own
    "draw multiple rectangles" requirement), not the one-region-per-page
    shape every existing extra_page_regions test covers. Fictional names
    throughout, never real student PII."""
    from melredact.config import FOOTER_WORKSHEET_TYPE, GROUP_ANCHOR
    from tests.make_fixture import InvisibleText, PdfBuilder, _write_roster_csv, render_continuation_image, render_header_image

    page1_img = render_header_image(
        name_text="Jamie Chen",
        teacher_text="Hannel",
        group_text="",
        date_text="10/03/2025",
        period_text="02",
        worksheet_type="PRT (01/2024)",
        page_marker="Page 1 of 2",
        shade_blank_rows=False,
    )
    name_a, top_a, x_a = "Morgan Lee", 120.0, 100.0
    name_b, top_b, x_b = "Priya Osei", 400.0, 300.0
    page2_img = render_continuation_image(worksheet_type="PRT (01/2024)", page_marker="Page 2 of 2", body="(continued)")

    page1_items = [
        InvisibleText("Name:", GROUP_ANCHOR["x0"], 68, 9),
        InvisibleText("Jamie Chen", 150, 68),
        InvisibleText("Group members, if any:", GROUP_ANCHOR["x0"], GROUP_ANCHOR["top"], 9),
        InvisibleText("10/03/2025", 450, 68),
        InvisibleText("02", 450, 87),
        InvisibleText("PRT 01 2024", FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
        InvisibleText("Page 1 of 2", 513, 747, 9),
    ]
    page2_items = [
        InvisibleText("(continued)", 45, 40),
        InvisibleText(name_a, x_a, top_a),
        InvisibleText(name_b, x_b, top_b),
        InvisibleText("PRT 01 2024", FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
        InvisibleText("Page 2 of 2", 513, 747, 9),
    ]
    builder = PdfBuilder()
    builder.add_page(page1_img, page1_items)
    builder.add_page(page2_img, page2_items)
    pdf_path = tmp_path / "page2twonames.pdf"
    builder.save(pdf_path)

    sid = "0204159906"
    roster_path = tmp_path / "roster.csv"
    _write_roster_csv(roster_path, [(sid, "Chen", "Jamie"), ("0204159907", "Lee", "Morgan"), ("0204159908", "Osei", "Priya")])
    roster = load_roster(roster_path)
    seg = segment_pdf(pdf_path)
    tag = packet_tag(pdf_path, seg.packets[0])
    return pdf_path, roster, seg, tag, sid, (name_a, top_a, x_a), (name_b, top_b, x_b)


def test_two_rectangles_on_one_page_both_redact_their_intended_regions(tmp_path):
    """The manual editor draws one fabric.js rect per click-and-drag, and a
    reviewer draws as many as the page needs -- redact_packet's
    extra_page_regions already accepts a *list* of Bbox per page offset,
    but no existing test actually exercises two independently-positioned
    regions landing on the same page at once. Both names must be gone,
    and each region must not accidentally cover (or miss) the other's ink."""
    from melredact.redact import redact_packet, verify_no_leaked_names

    pdf_path, roster, seg, tag, sid, (name_a, top_a, x_a), (name_b, top_b, x_b) = (
        _build_two_page_two_names_on_page2_fixture(tmp_path)
    )
    packet = seg.packets[0]

    bbox_a = (x_a - 10, top_a - 5, x_a + 200, top_a + 15)
    bbox_b = (x_b - 10, top_b - 5, x_b + 200, top_b + 15)
    out_path = tmp_path / "redacted.pdf"
    result = redact_packet(pdf_path, packet, out_path, dpi=DPI, extra_page_regions={1: [bbox_a, bbox_b]})
    assert result.uncovered_group_words == []

    import pdfplumber

    with pdfplumber.open(out_path) as pdf:
        page2_text = (pdf.pages[1].extract_text() or "").lower()
    assert "morgan" not in page2_text
    assert "lee" not in page2_text
    assert "priya" not in page2_text
    assert "osei" not in page2_text
    assert verify_no_leaked_names(out_path, roster) == []


def test_resized_rectangle_redacts_its_new_bounds_not_original(tmp_path):
    """A reviewer drags a corner handle to enlarge a seeded rectangle so it
    actually reaches ink past its original bounds -- review_app.py's own
    _canvas_rect_to_bbox folds fabric.js's scaleX/scaleY into the
    recovered bbox (see test_canvas_rect_to_bbox_folds_in_scalex_scaley_
    for_a_resized_object), but nothing end-to-end proves the *redaction*
    actually uses the resized region rather than silently falling back to
    the rectangle's own pre-resize width/height. Built so the original,
    un-resized box (just the name's own first few characters) leaves ink
    exposed, and only the resized box (scaleX/scaleY > 1, reaching the
    rest of the word) covers all of it."""
    import review_app
    from melredact.redact import redact_packet, verify_no_leaked_names

    pdf_path, roster, seg, tag, sid, extra_name, extra_top, extra_x = _build_two_page_page2_name_fixture(tmp_path)
    packet = seg.packets[0]

    original_bbox = (extra_x - 10, extra_top - 5, extra_x + 20, extra_top + 15)  # deliberately too narrow to cover "Morgan Lee"
    baseline_path = tmp_path / "baseline.pdf"
    redact_packet(pdf_path, packet, baseline_path, dpi=DPI, extra_page_regions={1: [original_bbox]})
    import pdfplumber

    with pdfplumber.open(baseline_path) as pdf:
        baseline_text = (pdf.pages[1].extract_text() or "").lower()
    assert "lee" in baseline_text, "fixture sanity: the original, un-resized bbox must leave part of the name exposed"

    canvas_rect = review_app._bbox_to_canvas_rect(original_bbox, review_app.DPI)
    canvas_rect["scaleX"] = 5.0
    canvas_rect["scaleY"] = 2.0
    resized_bbox = review_app._canvas_rect_to_bbox(canvas_rect, review_app.DPI)
    assert resized_bbox != original_bbox

    resized_path = tmp_path / "resized.pdf"
    result = redact_packet(pdf_path, packet, resized_path, dpi=DPI, extra_page_regions={1: [resized_bbox]})
    assert result.uncovered_group_words == []
    with pdfplumber.open(resized_path) as pdf:
        resized_text = (pdf.pages[1].extract_text() or "").lower()
    assert "morgan" not in resized_text
    assert "lee" not in resized_text
    assert verify_no_leaked_names(resized_path, roster) == []


def _build_three_page_page3_name_fixture(tmp_path):
    """Same shape as _build_two_page_page2_name_fixture, one page deeper:
    a clean header page (page 1), a plain continuation (page 2), and a
    third page carrying its own extra handwritten name -- proves the
    editor (see CLAUDE.md's "Show every page of the packet in the editor"
    section) reaches a page beyond the first continuation page too, not
    just page 2 specifically. Fictional names throughout, never real
    student PII."""
    from melredact.config import FOOTER_WORKSHEET_TYPE, GROUP_ANCHOR
    from tests.make_fixture import InvisibleText, PdfBuilder, _write_roster_csv, render_continuation_image, render_header_image

    page1_img = render_header_image(
        name_text="Taylor Kim",
        teacher_text="Hannel",
        group_text="",
        date_text="10/03/2025",
        period_text="02",
        worksheet_type="PRT (01/2024)",
        page_marker="Page 1 of 3",
        shade_blank_rows=False,
    )
    page2_img = render_continuation_image(worksheet_type="PRT (01/2024)", page_marker="Page 2 of 3", body="(continued)")
    page3_extra_name = "Casey Diaz"
    page3_extra_top = 130.0
    page3_extra_x = 90.0
    page3_img = render_continuation_image(worksheet_type="PRT (01/2024)", page_marker="Page 3 of 3", body="(continued)")

    page1_items = [
        InvisibleText("Name:", GROUP_ANCHOR["x0"], 68, 9),
        InvisibleText("Taylor Kim", 150, 68),
        InvisibleText("Group members, if any:", GROUP_ANCHOR["x0"], GROUP_ANCHOR["top"], 9),
        InvisibleText("10/03/2025", 450, 68),
        InvisibleText("02", 450, 87),
        InvisibleText("PRT 01 2024", FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
        InvisibleText("Page 1 of 3", 513, 747, 9),
    ]
    page2_items = [
        InvisibleText("(continued)", 45, 40),
        InvisibleText("PRT 01 2024", FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
        InvisibleText("Page 2 of 3", 513, 747, 9),
    ]
    page3_items = [
        InvisibleText("(continued)", 45, 40),
        InvisibleText(page3_extra_name, page3_extra_x, page3_extra_top),
        InvisibleText("PRT 01 2024", FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
        InvisibleText("Page 3 of 3", 513, 747, 9),
    ]
    builder = PdfBuilder()
    builder.add_page(page1_img, page1_items)
    builder.add_page(page2_img, page2_items)
    builder.add_page(page3_img, page3_items)
    pdf_path = tmp_path / "page3name.pdf"
    builder.save(pdf_path)

    sid = "0204159904"
    roster_path = tmp_path / "roster.csv"
    _write_roster_csv(roster_path, [(sid, "Kim", "Taylor"), ("0204159905", "Diaz", "Casey")])
    roster = load_roster(roster_path)
    seg = segment_pdf(pdf_path)
    tag = packet_tag(pdf_path, seg.packets[0])
    return pdf_path, roster, seg, tag, sid, page3_extra_name, page3_extra_top, page3_extra_x


def test_page_3_region_redacts_page_3_and_leaves_other_pages_unchanged(tmp_path):
    """extra_page_regions must reach a page two continuation pages deep,
    not just an immediate page 2 -- redacting a name on page 3 must not
    perturb page 1's or page 2's own output at all (see CLAUDE.md's "Show
    every page of the packet in the editor" section)."""
    from melredact.redact import redact_packet

    pdf_path, roster, seg, tag, sid, extra_name, extra_top, extra_x = _build_three_page_page3_name_fixture(tmp_path)
    packet = seg.packets[0]
    assert packet.n_pages == 3

    baseline_path = tmp_path / "baseline.pdf"
    redact_packet(pdf_path, packet, baseline_path, dpi=DPI)
    import pdfplumber

    with pdfplumber.open(baseline_path) as pdf:
        baseline_page1_text = pdf.pages[0].extract_text() or ""
        baseline_page2_text = pdf.pages[1].extract_text() or ""
        baseline_page3_text = pdf.pages[2].extract_text() or ""
    assert "diaz" in baseline_page3_text.lower(), "fixture sanity: the extra name must survive when no page-3 region is given"

    page3_bbox = (extra_x - 10, extra_top - 5, extra_x + 200, extra_top + 15)
    edited_path = tmp_path / "edited.pdf"
    result = redact_packet(pdf_path, packet, edited_path, dpi=DPI, extra_page_regions={2: [page3_bbox]})
    assert result.uncovered_group_words == []

    with pdfplumber.open(edited_path) as pdf:
        edited_page1_text = pdf.pages[0].extract_text() or ""
        edited_page2_text = pdf.pages[1].extract_text() or ""
        edited_page3_text = pdf.pages[2].extract_text() or ""

    assert edited_page1_text == baseline_page1_text, "page 1 must be byte-identical to the automatic path's own output"
    assert edited_page2_text == baseline_page2_text, "page 2 must be byte-identical to the automatic path's own output"
    assert "diaz" not in edited_page3_text.lower()
    assert "casey" not in edited_page3_text.lower()


def test_reviewer_cannot_supply_a_sid_directly_only_a_name_resolved_through_roster(main_fixture, roster):
    """filter_roster_by_name is the ONLY function review_app.py's manual
    editor (and its pre-existing roster-search expander) uses to turn a
    reviewer's typed text into a SID -- typing a real SID string must not
    resolve to anything, since no roster entry's full_name contains a SID,
    while typing the actual name resolves to exactly that student."""
    from melredact.roster import filter_roster_by_name

    target = next(iter(roster))
    by_name = filter_roster_by_name(roster, target.full_name)
    assert [e.sid for e in by_name] == [target.sid]

    by_sid_text = filter_roster_by_name(roster, target.sid)
    assert by_sid_text == [], "typing a SID string must never resolve to a roster entry"


def test_stored_manual_geometry_reproduces_on_rerun(tmp_path):
    """A packet a human has already edited once (via the general editor --
    see CLAUDE.md's "Make the editor reachable for any packet" section,
    this packet was never held/queued at all) must reproduce the same
    clean write on a later run without being re-queued or redrawn -- see
    pipeline.py's `manual_geometry` parameter and `save_manual_geometry`."""
    from melredact.config import GROUP_ANCHOR
    from melredact.pipeline import list_manual_queue, load_manual_geometry, release_from_manual_queue
    from melredact.redact import HeaderBand, redact_bboxes_for_band

    pdf_path, roster, seg, tag, sid, overflow_top_pt = _build_packet14_style_fixture(tmp_path)
    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"

    corrected_band = HeaderBand(left=38, top=58, right=574, bottom=overflow_top_pt + 20, detected=True)
    header_bbox_override = redact_bboxes_for_band(corrected_band, GROUP_ANCHOR["top"])
    release = release_from_manual_queue(
        pdf_path, seg.packets[0], tag, sid, roster, None,
        out_dir=out_dir, dpi=DPI, decisions_dir=decisions_dir, header_bbox_override=header_bbox_override,
    )
    assert release.released, release.reason

    geometry = load_manual_geometry(pdf_path, decisions_dir=decisions_dir)
    assert tag in geometry
    assert geometry[tag]["header_bbox_override"] == header_bbox_override

    entry = roster.by_sid[sid]
    expected_path = output_path(out_dir, entry, seg.packets[0].worksheet_type, round_label=FIXTURE_ROUND)
    expected_path.unlink()
    (out_dir / ".ledger" / f"{pdf_path.stem}.json").unlink()

    results = run_dispositions(
        pdf_path, seg, {tag: sid}, roster, out_dir=out_dir, dpi=DPI, manual_geometry=geometry,
    )
    result = next(r for r in results if r.packet_tag == tag)
    assert not result.held_back, result.reason
    assert result.out_path == expected_path
    assert expected_path.exists()
    assert result.geometry_source == "manual"
    assert list_manual_queue(out_dir) == []


def test_no_manual_geometry_follows_the_existing_automatic_path_unchanged(main_fixture, roster, segmented, tmp_path):
    """A packet with no stored manual geometry must produce byte-identical
    output whether or not `manual_geometry` is passed at all -- the new
    parameter must be strictly additive, never change behavior for the
    overwhelming majority of packets that never needed a manual
    correction."""
    tag = packet_tag(main_fixture.pdf_path, segmented.packets[0])
    sid = _sid_for(roster, "Jordan Ames")

    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    results_a = run_dispositions(main_fixture.pdf_path, segmented, {tag: sid}, roster, out_dir=out_a, dpi=DPI)
    results_b = run_dispositions(
        main_fixture.pdf_path, segmented, {tag: sid}, roster, out_dir=out_b, dpi=DPI, manual_geometry={}
    )

    result_a = next(r for r in results_a if r.packet_tag == tag)
    result_b = next(r for r in results_b if r.packet_tag == tag)
    assert not result_a.held_back and not result_b.held_back
    assert result_a.geometry_source == result_b.geometry_source == "automatic"
    assert result_a.out_path.read_bytes() == result_b.out_path.read_bytes()


# --- consent hold: a packet whose best match is a held name (see
# roster.py's Roster.held_names) is a known-consented student with an
# unresolvable SID -- must never be written or deleted, see
# pipeline.py's module docstring.


def _build_held_name_fixture(tmp_path):
    """A single-header-page packet whose handwritten name matches a held
    name exactly, against a roster with one unrelated entry (so the held
    name is unambiguously the best match). Fictional name, never real
    student PII."""
    from melredact.config import FOOTER_WORKSHEET_TYPE, NAME_ANCHOR
    from tests.make_fixture import InvisibleText, PdfBuilder, _write_roster_csv, render_header_image

    img = render_header_image(
        name_text="Jad Osman",
        teacher_text="Hannel",
        group_text="",
        date_text="10/03/2025",
        period_text="04",
        worksheet_type="PRT (01/2024)",
        page_marker="Page 1 of 1",
        shade_blank_rows=False,
    )
    items = [
        InvisibleText("Name:", NAME_ANCHOR["x0"], NAME_ANCHOR["top"], 9),
        InvisibleText("Jad Osman", 150, NAME_ANCHOR["top"]),
        InvisibleText("PRT 01 2024", FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
        InvisibleText("Page 1 of 1", 513, 747, 9),
    ]
    builder = PdfBuilder()
    builder.add_page(img, items)
    pdf_path = tmp_path / "held.pdf"
    builder.save(pdf_path)

    roster_path = tmp_path / "010406.csv"
    _write_roster_csv(roster_path, [("0104060401", "Ghavami", "Gavin")])
    holds_file = roster_path.with_name("010406_holds.csv")
    with holds_file.open("w", newline="") as f:
        f.write("Last Name,First Name\nOsman,Jad\n")

    roster = load_roster(roster_path)
    seg = segment_pdf(pdf_path)
    tag = packet_tag(pdf_path, seg.packets[0])
    return pdf_path, roster, seg, tag


def test_pending_packet_matching_a_held_name_is_a_consent_hold_not_pending_or_written(tmp_path):
    """The primary consent-hold behavior: a packet whose best match is a
    held name must produce a hold -- not a normal pending state, not a
    roster proposal, and above all not a write or a delete."""
    pdf_path, roster, seg, tag = _build_held_name_fixture(tmp_path)
    out_dir = tmp_path / "out"

    results = run_dispositions(pdf_path, seg, {}, roster, out_dir=out_dir, dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)

    assert result.consent_hold
    assert not result.pending
    assert not result.held_back
    assert result.out_path is None
    assert result.deleted_path is None
    assert "Jad Osman" in result.reason
    assert not any(out_dir.rglob("*.pdf")), "a consent-held packet must never leave a file in out_dir"


def test_consent_hold_never_deletes_prior_output_for_the_same_tag(tmp_path):
    """A held-name match must not trigger the non-consent delete rule --
    there is no confirmed non-consent here, just an unresolvable SID."""
    pdf_path, roster, seg, tag = _build_held_name_fixture(tmp_path)
    out_dir = tmp_path / "out"

    results = run_dispositions(pdf_path, seg, {}, roster, out_dir=out_dir, dpi=DPI)
    assert not [r for r in results if r.deleted_path is not None]


def test_explicit_decision_overrides_the_consent_hold(tmp_path):
    """A human who has already recorded a decision for this tag -- here, an
    explicit non-consent rejection -- must win over the automatic held-name
    signal; the hold only ever applies to a still-pending tag."""
    pdf_path, roster, seg, tag = _build_held_name_fixture(tmp_path)
    out_dir = tmp_path / "out"

    results = run_dispositions(pdf_path, seg, {tag: None}, roster, out_dir=out_dir, dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)

    assert not result.consent_hold
    assert result.sid is None
    assert not result.pending


# --- round path segment (see CLAUDE.md's "A round segment" section): a
# student can legitimately complete the same worksheet+topic more than
# once, in different collection sessions -- round is what keeps those from
# colliding in out/. Round is derived purely from each packet's own OCR'd
# Date field (blocks.group_into_rounds), never the source filename, and
# must never influence matching/scoring/claiming -- only the output path.


def _one_student_multi_round_fixture(tmp_path, date_texts):
    """A single-page-per-packet PDF, one packet per date in `date_texts`,
    every packet naming the *same* roster student (ROSTER[0], "Jordan
    Ames") -- isolates the round segment as the only thing that can
    distinguish these packets' output paths (same sid, same worksheet_type,
    same NO_TOPIC filename)."""
    name = f"{ROSTER[0][2]} {ROSTER[0][1]}"
    specs = [
        PacketSpec(f"pkt{i}", name, "Hannel", "none", 1, ROSTER[0][0], date_text=date_text)
        for i, date_text in enumerate(date_texts)
    ]
    pdf_path = tmp_path / "multi_round.pdf"
    roster_path = tmp_path / "roster.csv"
    return _build_packets_pdf(specs, ROSTER, [], pdf_path, roster_path)


def test_three_contiguous_groups_produce_three_round_labels_and_distinct_paths(tmp_path):
    fx = _one_student_multi_round_fixture(tmp_path, ["10/01/2025", "2/01/2026", "3/01/2026"])
    roster = load_roster(fx.roster_path)
    seg = segment_pdf(fx.pdf_path)
    sid = ROSTER[0][0]
    decisions = {packet_tag(fx.pdf_path, p): sid for p in seg.packets}
    out_dir = tmp_path / "out"

    results = run_dispositions(fx.pdf_path, seg, decisions, roster, out_dir=out_dir, dpi=DPI)
    written = [r for r in results if r.out_path is not None]

    assert len(written) == 3
    assert {r.out_path.parent.name for r in written} == {"2025-10", "2026-02", "2026-03"}
    assert len({r.out_path for r in written}) == 3, "three distinct paths for the same SID, one per round"
    assert all(r.collision_note is None for r in written), "distinct round dirs need no suffix backstop"


def test_undated_group_still_writes_under_the_undated_round_segment(tmp_path):
    fx = _one_student_multi_round_fixture(tmp_path, ["not a real date"])
    roster = load_roster(fx.roster_path)
    seg = segment_pdf(fx.pdf_path)
    sid = ROSTER[0][0]
    tag = packet_tag(fx.pdf_path, seg.packets[0])
    out_dir = tmp_path / "out"

    results = run_dispositions(fx.pdf_path, seg, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)

    assert not result.held_back, "an unparseable date is not a reason to withhold otherwise-approved output"
    assert result.out_path is not None
    assert result.out_path.exists()
    from melredact.blocks import UNDATED_ROUND

    assert result.out_path.parent.name == UNDATED_ROUND


def test_single_round_file_with_no_topic_has_constant_path_depth(main_fixture, segmented, roster, tmp_path):
    """A file with no topic segment (every teacher except one with per-topic
    filenames) and a single round group must still produce the full,
    constant-depth path -- teacher/period/worksheet_type/topic/round/sid.pdf --
    not a shallower path just because there's nothing round- or
    topic-specific to say."""
    packet = segmented.packets[0]  # clean_match
    tag = packet_tag(main_fixture.pdf_path, packet)
    sid = _sid_for(roster, "Jordan Ames")
    out_dir = tmp_path / "out"

    results = run_dispositions(main_fixture.pdf_path, segmented, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)

    rel_parts = result.out_path.relative_to(out_dir).parts
    assert len(rel_parts) == 6  # teacher / period / worksheet_type / topic / round / sid.pdf
    assert rel_parts[3] == "NA"
    assert rel_parts[4] == FIXTURE_ROUND
    assert rel_parts[5] == f"{sid}.pdf"


def test_round_label_does_not_alter_match_proposals(tmp_path):
    """Two packets differing only in their own date_text (and therefore
    only in which round they'll eventually be assigned to) must produce
    byte-identical match proposals -- round labelling is output-path
    metadata only, and must never leak into candidate scoring, ranking, or
    claiming."""
    fx = _one_student_multi_round_fixture(tmp_path, ["3/01/2026", "10/01/2025"])
    roster = load_roster(fx.roster_path)
    seg = segment_pdf(fx.pdf_path)

    proposals = propose_all(fx.pdf_path, seg, roster)
    assert len(proposals) == 2
    p0, p1 = proposals
    assert [(c.sid, c.score) for c in p0.candidates] == [(c.sid, c.score) for c in p1.candidates]
    assert [(c.full_name, c.score) for c in p0.held_candidates] == [(c.full_name, c.score) for c in p1.held_candidates]


# --- Consensus-ink anomaly check integration (see melredact/consensus.py) ---


@pytest.fixture(scope="module")
def consensus_pipeline_fixture(tmp_path_factory):
    from tests.make_fixture import build_consensus_fixture

    return build_consensus_fixture(tmp_path_factory.mktemp("consensus_pipeline_fixture"))


@pytest.fixture(scope="module")
def consensus_pipeline_roster(consensus_pipeline_fixture):
    return load_roster(consensus_pipeline_fixture.roster_path)


@pytest.fixture(scope="module")
def consensus_pipeline_segmented(consensus_pipeline_fixture):
    return segment_pdf(consensus_pipeline_fixture.pdf_path)


@pytest.fixture
def consensus_pipeline_decisions(consensus_pipeline_fixture):
    return dict(consensus_pipeline_fixture.sid_by_tag)


def test_consensus_hold_is_held_back_end_to_end(
    consensus_pipeline_fixture, consensus_pipeline_segmented, consensus_pipeline_roster, consensus_pipeline_decisions, tmp_path
):
    tag = consensus_pipeline_fixture.anomaly_tag
    out_dir = tmp_path / "out"
    results = run_dispositions(
        consensus_pipeline_fixture.pdf_path,
        consensus_pipeline_segmented,
        consensus_pipeline_decisions,
        consensus_pipeline_roster,
        out_dir=out_dir,
        dpi=DPI,
    )
    result = next(r for r in results if r.packet_tag == tag)
    assert result.held_back
    assert "consensus-ink anomaly" in result.reason
    assert result.out_path is None


def test_consensus_hold_is_not_releasable_via_detection_overrides(
    consensus_pipeline_fixture, consensus_pipeline_segmented, consensus_pipeline_roster, consensus_pipeline_decisions, tmp_path
):
    """detection_overrides only ever releases the *detection-confidence*
    hold (see pipeline.py's module docstring, "One of these five holds is
    human-overridable"). A consensus-ink anomaly is a finding of real
    anomalous ink, not a confidence gap -- putting this packet's own tag in
    detection_overrides must have zero effect on it."""
    tag = consensus_pipeline_fixture.anomaly_tag
    out_dir = tmp_path / "out"
    results = run_dispositions(
        consensus_pipeline_fixture.pdf_path,
        consensus_pipeline_segmented,
        consensus_pipeline_decisions,
        consensus_pipeline_roster,
        out_dir=out_dir,
        dpi=DPI,
        detection_overrides={tag},
    )
    result = next(r for r in results if r.packet_tag == tag)
    assert result.held_back
    assert "consensus-ink anomaly" in result.reason
    assert result.out_path is None


def test_consensus_hold_is_queued_and_released_by_drawing_a_manual_region_over_the_flagged_ink(
    consensus_pipeline_fixture, consensus_pipeline_segmented, consensus_pipeline_roster, consensus_pipeline_decisions, tmp_path
):
    """The actual, checked resolution path for this hold: a human draws a
    region over the flagged ink in review_app.py's manual editor, which
    calls release_from_manual_queue with that region as `extra_page_
    regions` and the hold's own bbox as `flagged_regions_to_verify` -- only
    a region that actually reaches the flagged ink releases the packet."""
    from melredact.consensus import analyze_consensus_anomalies
    from melredact.pipeline import list_manual_queue, release_from_manual_queue

    tag = consensus_pipeline_fixture.anomaly_tag
    sid = consensus_pipeline_fixture.sid_by_tag[tag]
    out_dir = tmp_path / "out"
    run_dispositions(
        consensus_pipeline_fixture.pdf_path,
        consensus_pipeline_segmented,
        consensus_pipeline_decisions,
        consensus_pipeline_roster,
        out_dir=out_dir,
        dpi=DPI,
    )
    queued = [e for e in list_manual_queue(out_dir) if e["packet_tag"] == tag]
    assert len(queued) == 1
    flagged_regions = queued[0]["flagged_regions"]
    assert flagged_regions is not None
    offset_key, bboxes = next(iter(flagged_regions.items()))
    assert int(offset_key) == 1
    flagged_bbox = tuple(bboxes[0])

    packet = next(p for p in consensus_pipeline_segmented.packets if packet_tag(consensus_pipeline_fixture.pdf_path, p) == tag)
    release = release_from_manual_queue(
        consensus_pipeline_fixture.pdf_path,
        packet,
        tag,
        sid,
        consensus_pipeline_roster,
        out_dir=out_dir,
        dpi=DPI,
        extra_page_regions={1: [flagged_bbox]},
        flagged_regions_to_verify={1: [flagged_bbox]},
    )
    assert release.released, release.reason
    assert release.out_path.exists()
    assert list_manual_queue(out_dir) == []


def test_consensus_hold_release_refused_when_drawn_region_misses_the_flagged_ink(
    consensus_pipeline_fixture, consensus_pipeline_segmented, consensus_pipeline_roster, consensus_pipeline_decisions, tmp_path
):
    from melredact.pipeline import list_manual_queue, release_from_manual_queue

    tag = consensus_pipeline_fixture.anomaly_tag
    sid = consensus_pipeline_fixture.sid_by_tag[tag]
    out_dir = tmp_path / "out"
    run_dispositions(
        consensus_pipeline_fixture.pdf_path,
        consensus_pipeline_segmented,
        consensus_pipeline_decisions,
        consensus_pipeline_roster,
        out_dir=out_dir,
        dpi=DPI,
    )
    packet = next(p for p in consensus_pipeline_segmented.packets if packet_tag(consensus_pipeline_fixture.pdf_path, p) == tag)
    wrong_region = (0.0, 0.0, 10.0, 10.0)  # nowhere near CONSENSUS_ANOMALY_BOX
    release = release_from_manual_queue(
        consensus_pipeline_fixture.pdf_path,
        packet,
        tag,
        sid,
        consensus_pipeline_roster,
        out_dir=out_dir,
        dpi=DPI,
        extra_page_regions={1: [wrong_region]},
        flagged_regions_to_verify={1: [(400.0, 500.0, 430.0, 520.0)]},
    )
    assert not release.released
    assert "consensus-ink anomaly" in release.reason
    assert len(list_manual_queue(out_dir)) == 1


def test_packets_without_anomalous_ink_follow_the_unchanged_path(
    consensus_pipeline_fixture, consensus_pipeline_segmented, consensus_pipeline_roster, consensus_pipeline_decisions, tmp_path
):
    """The shared-answer-ink packets and the plain clean packets must both
    write normally -- the consensus-ink check must never hold a packet it
    has no anomaly finding for."""
    out_dir = tmp_path / "out"
    results = run_dispositions(
        consensus_pipeline_fixture.pdf_path,
        consensus_pipeline_segmented,
        consensus_pipeline_decisions,
        consensus_pipeline_roster,
        out_dir=out_dir,
        dpi=DPI,
    )
    unaffected_tags = set(consensus_pipeline_fixture.answer_tags) | set(consensus_pipeline_fixture.clean_tags)
    for tag in unaffected_tags:
        result = next(r for r in results if r.packet_tag == tag)
        assert not result.held_back, f"{tag} should not be held: {result.reason}"
        assert result.out_path is not None
        assert result.out_path.exists()
