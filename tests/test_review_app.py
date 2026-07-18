import json
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from melredact.pipeline import packet_tag
from melredact.segment import segment_pdf
from tests.make_fixture import PACKETS, build_footer_edge_case_fixture, build_main_fixture

APP_PATH = str(Path(__file__).resolve().parent.parent / "review_app.py")


def _launch(pdf_path, roster_path, out_dir, decisions_dir, timeout=60):
    sys.argv = [
        "review_app.py",
        str(pdf_path),
        str(roster_path),
        "--out-dir",
        str(out_dir),
        "--decisions-dir",
        str(decisions_dir),
    ]
    at = AppTest.from_file(APP_PATH, default_timeout=timeout).run()
    assert not at.exception, at.exception
    return at


@pytest.fixture(scope="module")
def main_fixture(tmp_path_factory):
    return build_main_fixture(tmp_path_factory.mktemp("review_ui_fixture"))


def test_app_loads_and_shows_all_packets(main_fixture, tmp_path):
    at = _launch(main_fixture.pdf_path, main_fixture.roster_path, tmp_path / "out", tmp_path / "decisions")
    assert at.sidebar.metric[0].value == str(len(PACKETS))


def test_default_decision_matches_auto_assign_for_every_packet(main_fixture, tmp_path):
    """The radio's pre-selected option must mirror match.assign_all's
    result exactly -- a reviewer who blindly clicks Confirm through every
    packet should reproduce auto-assign, not silently diverge from it."""
    at = _launch(main_fixture.pdf_path, main_fixture.roster_path, tmp_path / "out", tmp_path / "decisions")
    packet_tags = [packet_tag(main_fixture.pdf_path, p) for p in segment_pdf(main_fixture.pdf_path).packets]
    for tag, spec in zip(packet_tags, PACKETS):
        at.selectbox[0].set_value(tag).run()  # re-fetch/re-issue every iteration: stale refs raise KeyError
        assert not at.exception, (spec.tag, at.exception)
        radio = at.radio[0]
        selected_sid = None if radio.value == "Not on roster (no consent)" else radio.value.split(" — ")[0]
        assert selected_sid == main_fixture.expected_auto_assign_sid[spec.tag], spec.tag


def test_next_button_advances_without_lag(main_fixture, tmp_path):
    """Regression: the packet selectbox previously auto-generated its
    widget key from an `index=` argument that itself depended on the very
    session-state it was seeding, so a selection only took effect one
    rerun late. Prev/Next must land on the packet actually requested, not
    the one before it."""
    at = _launch(main_fixture.pdf_path, main_fixture.roster_path, tmp_path / "out", tmp_path / "decisions")
    packet_tags = [packet_tag(main_fixture.pdf_path, p) for p in segment_pdf(main_fixture.pdf_path).packets]

    next_btn = next(b for b in at.button if b.label == "Next >")
    prev_btn = next(b for b in at.button if b.label == "< Prev")

    assert at.selectbox[0].value == packet_tags[0]
    next_btn.click().run()
    assert at.selectbox[0].value == packet_tags[1]
    next_btn = next(b for b in at.button if b.label == "Next >")  # re-fetch: stale ref across reruns
    next_btn.click().run()
    assert at.selectbox[0].value == packet_tags[2]
    prev_btn = next(b for b in at.button if b.label == "< Prev")
    prev_btn.click().run()
    assert at.selectbox[0].value == packet_tags[1]


def test_confirm_persists_decision_to_disk(main_fixture, tmp_path):
    decisions_dir = tmp_path / "decisions"
    at = _launch(main_fixture.pdf_path, main_fixture.roster_path, tmp_path / "out", decisions_dir)
    at.button(key="confirm_packets_p000").click().run()
    assert not at.exception

    decisions_file = decisions_dir / f"{Path(main_fixture.pdf_path).stem}.json"
    saved = json.loads(decisions_file.read_text())
    assert saved["packets_p000"] == main_fixture.expected_auto_assign_sid["clean_match"]


def test_reject_after_approve_deletes_prior_output(main_fixture, tmp_path):
    from melredact.pipeline import output_path
    from melredact.roster import load_roster

    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"

    at = _launch(main_fixture.pdf_path, main_fixture.roster_path, out_dir, decisions_dir)
    at.button(key="confirm_packets_p000").click().run()
    at.sidebar.button[0].click().run()

    roster = load_roster(main_fixture.roster_path, infer_period_from=main_fixture.pdf_path)
    sid = main_fixture.expected_auto_assign_sid["clean_match"]
    out_file = output_path(out_dir, roster.by_sid[sid])
    assert out_file.exists()

    at2 = _launch(main_fixture.pdf_path, main_fixture.roster_path, out_dir, decisions_dir)
    at2.radio(key="decision_packets_p000").set_value("Not on roster (no consent)").run()
    at2.button(key="confirm_packets_p000").click().run()
    at2.sidebar.button[0].click().run()
    assert not out_file.exists()


def test_preview_stamp_reflects_the_currently_selected_candidate(main_fixture, tmp_path, monkeypatch):
    """The review UI's preview must be driven by the exact same
    render_redaction_preview call the real output would use, stamped with
    whichever candidate is currently selected in the radio -- not a fixed
    'REDACTED' stand-in that could read differently from what Confirm
    would actually produce."""
    import melredact.redact as redact_mod

    # AppTest execs review_app.py fresh each .run(), re-evaluating its
    # `from melredact.redact import render_redaction_preview` every time --
    # so patching the *source* module's attribute (rather than an
    # already-imported `review_app.render_redaction_preview` reference,
    # which AppTest's separate execution namespace wouldn't see) is what
    # actually gets picked up.
    captured = []
    real_render = redact_mod.render_redaction_preview

    def spy(*args, **kwargs):
        captured.append(kwargs.get("stamp_lines"))
        return real_render(*args, **kwargs)

    monkeypatch.setattr(redact_mod, "render_redaction_preview", spy)

    at = _launch(main_fixture.pdf_path, main_fixture.roster_path, tmp_path / "out", tmp_path / "decisions")
    assert captured, "render_redaction_preview was never called on initial load"
    at.selectbox[0].set_value("packets_p000").run()  # clean_match: has a clear auto-assign candidate
    assert captured[-1] == [f"SID: {main_fixture.expected_auto_assign_sid['clean_match']}", "PD: 02"]

    radio = at.radio[0]
    other_option = next(o for o in radio.options if o != radio.value)
    radio.set_value(other_option).run()
    other_sid = None if other_option == "Not on roster (no consent)" else other_option.split(" — ")[0]
    expected = None if other_sid is None else [f"SID: {other_sid}", "PD: 02"]
    assert captured[-1] == expected


def test_issue_flagged_packet_blocks_sid_confirmation(tmp_path):
    edge_pdf = build_footer_edge_case_fixture(tmp_path / "edge")
    main = build_main_fixture(tmp_path / "main")  # borrow a roster with real candidates
    at = _launch(edge_pdf, main.roster_path, tmp_path / "out", tmp_path / "decisions")

    flagged = next(p for p in segment_pdf(edge_pdf).packets if p.issues and p.header_page_index is not None)
    at.selectbox[0].set_value(packet_tag(edge_pdf, flagged)).run()
    assert at.warning  # unresolved-issues warning shown

    sid_option = next(o for o in at.radio[0].options if o != "Not on roster (no consent)")
    at.radio[0].set_value(sid_option).run()
    confirm_btn = next(b for b in at.button if b.label == "Confirm decision")
    assert confirm_btn.disabled

    # rejecting (non-consent) must still be allowed on a flagged packet
    at.radio[0].set_value("Not on roster (no consent)").run()
    confirm_btn = next(b for b in at.button if b.label == "Confirm decision")
    assert not confirm_btn.disabled
