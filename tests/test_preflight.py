import pytest

from melredact.cli import main
from melredact.pipeline import (
    _issue_page_indices,
    format_preflight_report,
    other_blocked_packets,
    propose_all,
    render_preflight_contact_sheet,
    run_preflight,
)
from melredact.roster import load_roster
from melredact.segment import segment_pdf
from tests.make_fixture import build_main_fixture, build_preflight_fixture, build_rotated_page_copy


@pytest.fixture(scope="module")
def main_fixture(tmp_path_factory):
    return build_main_fixture(tmp_path_factory.mktemp("preflight_main_fixture"))


@pytest.fixture(scope="module")
def roster(main_fixture):
    return load_roster(main_fixture.roster_path)


def test_preflight_clean_file_reports_zero_blocking_issues_and_writes_nothing(main_fixture, roster, tmp_path):
    report = run_preflight(main_fixture.pdf_path, roster)

    # "Blocking" here is the load-bearing claim -- zero structural problems
    # (missing header, bad footer, unresolved orientation). Detection/
    # consensus/advisory flags are informational (routed to "needs a human
    # in the editor", never "cannot be processed"), so this doesn't assert
    # those are zero -- only that nothing here needs a *fix* before it can
    # even be attempted.
    assert report.n_cannot_process == 0
    assert report.n_unsegmentable == 0
    assert len(report.orientation_flags) == 0
    assert report.n_detection_holds == 0
    assert report.n_packets == report.n_clean + report.n_needs_editor + report.n_cannot_process

    text = format_preflight_report(report)
    assert "Verdict:" in text
    assert "0 cannot be processed without a fix" in text

    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"
    rc = main(
        [
            "preflight",
            "--pdf",
            str(main_fixture.pdf_path),
            "--roster",
            str(main_fixture.roster_path),
            "--out",
            str(out_dir),
            "--decisions",
            str(decisions_dir),
            # Isolate "preflight itself writes nothing" (decisions/ledger/
            # out/<teacher>/... redacted output) from the contact sheet,
            # which is an *intentional*, separately-gated write whenever
            # something -- blocking or not, e.g. this fixture's own
            # non-blocking consensus-ink flag -- is actually flagged.
            "--no-contact-sheet",
        ]
    )
    assert rc == 0
    assert not out_dir.exists()
    assert not decisions_dir.exists()


def test_preflight_reports_rotation_unsegmentable_and_no_match_packets(tmp_path):
    pdf_path, roster_path = build_preflight_fixture(tmp_path / "fixture")
    # Rotate packet A's own *continuation* page (physical index 1), left
    # unconfirmed -- an independent orientation signal that doesn't disturb
    # packet A's header (physical index 0), which keeps its native
    # invisible-text layer and reads normally.
    rotated_path = build_rotated_page_copy(pdf_path, tmp_path, 1, 180, "preflight_rotated.pdf")
    roster = load_roster(roster_path)

    report = run_preflight(rotated_path, roster)

    assert len(report.orientation_flags) == 1
    flag = report.orientation_flags[0]
    assert flag.page_index == 1
    assert flag.kind == "pending_confirmation"
    assert flag.detected_angle == 180

    assert report.n_unsegmentable == 1
    orphan = next(p for p in report.packets if p.is_orphan)
    assert orphan.issues

    no_match_packets = [p for p in report.packets if not p.is_orphan and not p.has_plausible_match]
    assert len(no_match_packets) == 2  # packet C ("Zzyzx Qorvath") and packet D ("Riley Osei")
    clean_no_match = next(p for p in no_match_packets if p.n_pages == 1)
    assert not clean_no_match.blocked  # a genuinely segmentable packet, just not on the roster

    # Packet D: header reads fine, but its continuation page's own footer
    # is unreadable -- blocked, but neither an orphan, a page-count
    # mismatch, nor an orientation issue (see build_preflight_fixture's
    # own docstring: found via a real preflight run, this used to fall
    # through every itemized section while still correctly counting
    # toward the verdict).
    other_blocked = other_blocked_packets(report)
    assert len(other_blocked) == 1
    assert other_blocked[0].n_pages == 2
    assert not other_blocked[0].page_count_mismatch
    assert any("unreadable footer" in i for i in other_blocked[0].issues)

    # The rotated (unconfirmed) packet, the orphan packet, and packet D all
    # block processing outright; the no-plausible-match packets do not on
    # their own (both are normal, fully segmentable packets -- just not on
    # the roster).
    assert report.n_cannot_process == 3

    text = format_preflight_report(report)
    assert "rotated 180" in text
    assert "Unsegmentable packets: 1" in text
    assert "no plausible roster match" in text
    assert "Other blocked packets: 1" in text
    assert other_blocked[0].packet_tag in text

    # The contact sheet must actually include and label packet D's own
    # issue-named page (its continuation page, physical index 5), not
    # silently omit it the way it used to (see other_blocked_packets/
    # _issue_page_indices in pipeline.py).
    assert _issue_page_indices(other_blocked[0]) == [5]
    sheet_path = render_preflight_contact_sheet(rotated_path, report, tmp_path / "out")
    assert sheet_path is not None and sheet_path.exists()


def test_preflight_honors_composition_overrides(tmp_path):
    """A packet a human has already confirmed the composition for (see
    pipeline.composition_overrides) must report unblocked in preflight
    too, not just in a real run -- otherwise the verdict a human sees
    before reviewing would disagree with what a real run actually does
    with the confirmation already on record. Packet D (build_preflight_
    fixture's own unreadable-continuation-footer packet) is exactly the
    composition-confirmable shape."""
    pdf_path, roster_path = build_preflight_fixture(tmp_path / "fixture")
    roster = load_roster(roster_path)

    baseline = run_preflight(pdf_path, roster)
    other_blocked = other_blocked_packets(baseline)
    assert len(other_blocked) == 1
    tag = other_blocked[0].packet_tag
    assert other_blocked[0].blocked

    confirmed = run_preflight(pdf_path, roster, composition_overrides={tag})
    packet = next(p for p in confirmed.packets if p.packet_tag == tag)
    assert packet.composition_confirmed
    assert not packet.blocked
    assert confirmed.n_cannot_process == baseline.n_cannot_process - 1

    text = format_preflight_report(confirmed)
    assert tag not in "\n".join(str(p) for p in other_blocked_packets(confirmed))


def test_preflight_populates_the_same_cache_the_real_run_reads(tmp_path, monkeypatch):
    fixture = build_main_fixture(tmp_path / "fixture")
    roster = load_roster(fixture.roster_path)
    # Strips packet A's header page (index 0) of its invisible text layer
    # (degrees=0 -- no actual rotation, see build_rotated_page_copy), so
    # both segmentation's footer/header read and matching's field
    # extraction must go through real OCR the first time, the same as a
    # real scan -- proving cache reuse here means something, unlike the
    # rest of this fixture (native text layer throughout, never OCR'd).
    ocr_forced_path = build_rotated_page_copy(fixture.pdf_path, tmp_path, 0, 0, "ocr_forced.pdf")

    report = run_preflight(ocr_forced_path, roster)
    assert report.n_packets > 0

    import melredact.ocr as ocr_mod

    def _boom(*args, **kwargs):
        raise AssertionError("OCR engine must not be invoked again -- preflight should have warmed the cache")

    monkeypatch.setattr(ocr_mod, "_engine", _boom)

    # The exact calls cli.py run's own segmentation/matching stages make.
    segmented = segment_pdf(ocr_forced_path)
    assert segmented.packets[0].header_page_index == 0
    proposals = propose_all(ocr_forced_path, segmented, roster)
    assert proposals[0].top is not None
