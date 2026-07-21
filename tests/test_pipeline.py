import pytest

from melredact.config import RENDER_DPI_PREVIEW
from melredact.pipeline import (
    DispositionResult,
    decisions_path,
    load_decisions,
    load_detection_overrides,
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
    assert (
        result.out_path
        == out_dir / entry.teacher_code / entry.period_display / packet.worksheet_type / f"{sid}.pdf"
    )
    assert result.out_path == output_path(out_dir, entry, packet.worksheet_type)


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
    assert tag_result.out_path == output_path(out_dir, new_entry, packet.worksheet_type)
    assert tag_result.out_path.exists()
    assert not old_path.exists()

    remaining = sorted(out_dir.rglob("*.pdf"))
    assert remaining == [tag_result.out_path]


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
    assert not output_path(out_dir, entry, packet.worksheet_type).exists()


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


def test_detection_override_does_not_release_an_uncovered_ink_hold(
    main_fixture, segmented, roster, tmp_path, monkeypatch
):
    """The boundary the fix must not cross: a detection-confidence override
    only ever answers "is the border confidently located", never "did
    anything actually leak". A packet that also has real uncovered
    group-row ink must stay held back even when a human has approved the
    detection-confidence override for it."""
    import dataclasses

    import melredact.pipeline as pipeline_mod

    real_redact_packet = pipeline_mod._redact_packet
    bad_packet = segmented.packets[0]
    bad_tag = packet_tag(main_fixture.pdf_path, bad_packet)

    def fake_undetected_band_with_leak(*args, **kwargs):
        result = real_redact_packet(*args, **kwargs)
        if packet_tag(main_fixture.pdf_path, args[1]) == bad_tag:
            fake_word = {"text": "Leaked", "x0": 0, "x1": 10, "top": 0, "bottom": 10}
            return dataclasses.replace(
                result,
                band=dataclasses.replace(result.band, detected=False),
                uncovered_group_words=[fake_word],
            )
        return result

    monkeypatch.setattr(pipeline_mod, "_redact_packet", fake_undetected_band_with_leak)

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
    assert "uncovered group-row ink" in result.reason


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


def test_vertical_group_row_overflow_is_auto_held_not_shipped_as_clean(tmp_path):
    """End-to-end proof for the real PRT packet 14 bug: a packet whose
    Group-row ink overflows past the header's own detected border must
    never be written as if clean. This runs the real segment_pdf ->
    run_dispositions path against a decided packet -- no monkeypatching --
    the same path a real reviewer's approval goes through, to prove the
    fix actually reaches run_dispositions and not just find_uncovered_
    group_words in isolation (see tests/test_redact.py for that unit-level
    proof)."""
    from melredact.pipeline import list_manual_queue, output_path

    pdf_path, roster, seg, tag, sid, _ = _build_packet14_style_fixture(tmp_path)
    out_dir = tmp_path / "out"
    results = run_dispositions(pdf_path, seg, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    result = next(r for r in results if r.packet_tag == tag)

    assert result.held_back, "vertical group-row overflow must hold the packet back, not ship it as clean"
    assert "uncovered group-row ink" in result.reason
    assert result.out_path is None

    # Nothing is present at the real, servable out/ path -- the held-back
    # draft only exists in the manual-redaction queue, never in out/ itself.
    entry = roster.by_sid[sid]
    assert not output_path(out_dir, entry, seg.packets[0].worksheet_type).exists()
    queued = list_manual_queue(out_dir)
    assert len(queued) == 1
    assert queued[0]["packet_tag"] == tag
    assert queued[0]["sid"] == sid
    assert "uncovered group-row ink" in queued[0]["reason"]


def test_manual_queue_release_with_a_corrected_band_writes_and_clears_the_queue(tmp_path):
    """The backstop working as intended: a human looks at the queued
    packet 14-style draft, supplies a corrected band whose bottom actually
    reaches past the overflow ink, and release_from_manual_queue re-checks
    coverage with that geometry before writing anything -- since it now
    passes, the file lands in the real out/ tree and the queue entry is
    cleared."""
    from melredact.pipeline import list_manual_queue, output_path, release_from_manual_queue
    from melredact.redact import HeaderBand

    pdf_path, roster, seg, tag, sid, overflow_top_pt = _build_packet14_style_fixture(tmp_path)
    out_dir = tmp_path / "out"
    run_dispositions(pdf_path, seg, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    assert len(list_manual_queue(out_dir)) == 1

    corrected_band = HeaderBand(left=38, top=58, right=574, bottom=overflow_top_pt + 20, detected=True)
    release = release_from_manual_queue(pdf_path, seg.packets[0], tag, sid, roster, corrected_band, out_dir=out_dir, dpi=DPI)

    assert release.released, release.reason
    entry = roster.by_sid[sid]
    expected_path = output_path(out_dir, entry, seg.packets[0].worksheet_type)
    assert release.out_path == expected_path
    assert expected_path.exists()
    assert list_manual_queue(out_dir) == []


def test_manual_queue_release_with_a_still_insufficient_band_stays_queued(tmp_path):
    """The boundary that keeps this a backstop, not an override: a human
    can supply a *wrong* corrected band too (e.g. one that still doesn't
    reach the overflow ink) -- release_from_manual_queue must refuse to
    write anything in that case and leave the packet queued, since the
    coverage check, not the human's say-so alone, is what actually gates a
    write."""
    from melredact.pipeline import list_manual_queue, output_path, release_from_manual_queue
    from melredact.redact import HeaderBand

    pdf_path, roster, seg, tag, sid, overflow_top_pt = _build_packet14_style_fixture(tmp_path)
    out_dir = tmp_path / "out"
    run_dispositions(pdf_path, seg, {tag: sid}, roster, out_dir=out_dir, dpi=DPI)
    assert len(list_manual_queue(out_dir)) == 1

    still_short_band = HeaderBand(left=38, top=58, right=574, bottom=overflow_top_pt - 5, detected=True)
    release = release_from_manual_queue(
        pdf_path, seg.packets[0], tag, sid, roster, still_short_band, out_dir=out_dir, dpi=DPI
    )

    assert not release.released
    assert "uncovered group-row ink" in release.reason
    entry = roster.by_sid[sid]
    assert not output_path(out_dir, entry, seg.packets[0].worksheet_type).exists()
    assert len(list_manual_queue(out_dir)) == 1
