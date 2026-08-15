import json
import re
import sys
from pathlib import Path

import pdfplumber
import pytest
from streamlit.testing.v1 import AppTest

import streamlit as st

import review_app
from melredact.config import RENDER_DPI_FINAL
from melredact.pipeline import load_orientation_overrides, packet_tag, run_dispositions
from melredact.redact import redact_packet, verify_no_leaked_names
from melredact.roster import load_roster
from melredact.segment import segment_pdf
from tests.make_fixture import (
    PACKETS,
    build_footer_edge_case_fixture,
    build_main_fixture,
    build_reversed_pair_fixture,
    build_rotated_page_copy,
    build_unreadable_continuation_footer_fixture,
)

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
    from melredact.blocks import round_label
    from melredact.pipeline import output_path
    from melredact.roster import load_roster

    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"

    at = _launch(main_fixture.pdf_path, main_fixture.roster_path, out_dir, decisions_dir)
    at.button(key="confirm_packets_p000").click().run()
    at.sidebar.button[0].click().run()

    roster = load_roster(main_fixture.roster_path, infer_period_from=main_fixture.pdf_path)
    sid = main_fixture.expected_auto_assign_sid["clean_match"]
    packet = next(p for p in segment_pdf(main_fixture.pdf_path).packets if packet_tag(main_fixture.pdf_path, p) == "packets_p000")
    # Every fixture packet shares PacketSpec's default date_text
    # ("10/03/2025"), one contiguous round group for the whole file.
    out_file = output_path(out_dir, roster.by_sid[sid], packet.worksheet_type, round_label=round_label("10/03/2025"))
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


def test_confirm_and_next_advances_to_the_next_packet(main_fixture, tmp_path):
    """The 'Confirm & Next >' button (added alongside 'Confirm decision')
    must both persist the decision and advance the packet selector in one
    click -- the whole point of adding it was to remove the extra click a
    reviewer otherwise needs between confirming one packet and moving to
    the next."""
    decisions_dir = tmp_path / "decisions"
    at = _launch(main_fixture.pdf_path, main_fixture.roster_path, tmp_path / "out", decisions_dir)
    packet_tags = [packet_tag(main_fixture.pdf_path, p) for p in segment_pdf(main_fixture.pdf_path).packets]
    assert at.selectbox[0].value == packet_tags[0]

    at.button(key="confirm_next_packets_p000").click().run()
    assert not at.exception
    assert at.selectbox[0].value == packet_tags[1]

    decisions_file = decisions_dir / f"{Path(main_fixture.pdf_path).stem}.json"
    saved = json.loads(decisions_file.read_text())
    assert saved["packets_p000"] == main_fixture.expected_auto_assign_sid["clean_match"]


def _build_vertical_overflow_pdf(tmp_path):
    """Same shape as tests/test_pipeline.py's packet-14 regression fixture
    (real PRT leak: Group-row ink overflowing past the header's own
    detected border) -- duplicated locally rather than shared, matching
    how test_redact.py's own one-off geometry fixtures are built inline.
    Fictional names, never real student PII."""
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
    return pdf_path, roster_path, sid, overflow_top_pt


def test_manual_queue_panel_lists_a_queued_packet_and_can_release_it(tmp_path, monkeypatch):
    """End-to-end through the review UI: a packet held for detection
    confidence (forced here via monkeypatch -- find_uncovered_group_words'
    own finding no longer queues anything on its own, see CLAUDE.md's "From
    detection-gates-workflow to human-reviews-everything" section, so this
    no longer reaches the queue via the packet-14 vertical-overflow shape
    alone) lands in the manual-redaction queue and shows up in
    review_app.py's own queue editor with a working release path --
    releasing with a corrected region must write the file and clear the
    queue; the panel must not be a dead end.

    AppTest drives real Streamlit widgets but not the drawable-canvas
    component's own drag interaction (a third-party iframe, not something
    AppTest can simulate) -- so this seeds `st.session_state`'s region
    dict the same way a reviewer's drag would have populated it (see
    `_render_manual_editor`'s `regions_key`) before the first run, then
    drives the real name-search and Apply widgets exactly as a human would
    click them. This is the same corrected geometry
    test_manual_queue_release_with_a_corrected_band_writes_and_clears_the_
    queue already proves at the pipeline.py level -- this test's own job is
    proving the *UI* actually reaches release_from_manual_queue with it,
    not re-proving the redaction geometry itself."""
    import dataclasses

    from melredact.blocks import round_label
    from melredact.config import GROUP_ANCHOR, HEADER_BAND_FALLBACK
    from melredact.pipeline import list_manual_queue, output_path, run_dispositions
    from melredact.redact import HeaderBand, redact_bboxes_for_band
    from melredact.roster import load_roster
    from melredact.segment import segment_pdf as _segment_pdf

    pdf_path, roster_path, sid, overflow_top_pt = _build_vertical_overflow_pdf(tmp_path)
    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"

    roster = load_roster(roster_path)
    seg = _segment_pdf(pdf_path)
    tag = packet_tag(pdf_path, seg.packets[0])

    import melredact.pipeline as pipeline_mod

    real_redact_packet = pipeline_mod._redact_packet

    def fake_undetected_band(*args, **kwargs):
        result = real_redact_packet(*args, **kwargs)
        if packet_tag(pdf_path, args[1]) == tag:
            return dataclasses.replace(result, band=dataclasses.replace(result.band, detected=False))
        return result

    with monkeypatch.context() as mp:
        mp.setattr(pipeline_mod, "_redact_packet", fake_undetected_band)
        run_dispositions(pdf_path, seg, {tag: sid}, roster, out_dir=out_dir)
    assert len(list_manual_queue(out_dir)) == 1

    corrected_band = HeaderBand(
        left=HEADER_BAND_FALLBACK["left"],
        top=HEADER_BAND_FALLBACK["top"],
        right=HEADER_BAND_FALLBACK["right"],
        bottom=overflow_top_pt + 20,
        detected=True,
    )
    left_bbox, right_bbox = redact_bboxes_for_band(corrected_band, GROUP_ANCHOR["top"])

    sys.argv = [
        "review_app.py",
        str(pdf_path),
        str(roster_path),
        "--out-dir",
        str(out_dir),
        "--decisions-dir",
        str(decisions_dir),
    ]
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["show_manual_queue"] = True
    at.session_state[f"mq_regions_{tag}"] = {0: [left_bbox, right_bbox]}
    at.run()
    assert not at.exception
    assert any(tag in expander.label for expander in at.expander)

    name_input = at.text_input(key=f"mq_name_{tag}")
    assert name_input.value == "Alex Rivera"  # pre-filled from the packet's already-decided sid
    assert not any(w.key == f"mq_sid_{tag}" for w in at.text_input)  # no widget ever accepts a raw sid
    name_input.set_value("Alex Rivera").run()

    sid_select = at.selectbox(key=f"mq_sidselect_{tag}")
    assert sid_select.options == [f"{sid} — Alex Rivera"]
    sid_select.set_value(f"{sid} — Alex Rivera").run()

    apply_btn = next(b for b in at.button if b.key == f"mq_apply_{tag}")
    assert not apply_btn.disabled
    apply_btn.click().run()
    assert not at.exception

    entry = roster.by_sid[sid]
    assert output_path(
        out_dir, entry, seg.packets[0].worksheet_type, round_label=round_label("10/03/2025")
    ).exists()
    assert list_manual_queue(out_dir) == []


def test_edit_redaction_reachable_for_a_non_held_packet_and_resolves_by_name(main_fixture, tmp_path):
    """The editor is reachable for ANY packet, not just one an automated
    check held (see CLAUDE.md's "Make the editor reachable for any packet"
    section) -- opening it on an ordinary, never-held packet, applying with
    the automatically-seeded geometry unchanged (AppTest can't simulate a
    canvas drag, so this is exactly the "one glance and a confirm" common
    case), must write the same clean output the automatic path would have,
    record the decision (editing IS the review decision), and never accept
    a typed SID anywhere in the process."""
    import json

    from melredact.blocks import round_label
    from melredact.pipeline import list_manual_queue, output_path
    from melredact.roster import load_roster

    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"
    at = _launch(main_fixture.pdf_path, main_fixture.roster_path, out_dir, decisions_dir)

    tag = "packets_p000"  # clean_match -- never held by any check
    at.selectbox[0].set_value(tag).run()

    edit_expanders = [e for e in at.expander if "Edit redaction" in e.label]
    assert edit_expanders, "the editor must be reachable for a packet that was never held"
    assert "currently held" not in edit_expanders[0].label

    assert not any(w.key == f"mq_sid_{tag}" for w in at.text_input), "no widget anywhere accepts a raw SID"
    name_input = at.text_input(key=f"mq_name_{tag}")
    roster = load_roster(main_fixture.roster_path, infer_period_from=main_fixture.pdf_path)
    sid = main_fixture.expected_auto_assign_sid["clean_match"]
    assert name_input.value == roster.by_sid[sid].full_name  # pre-filled from the live decision preview

    apply_btn = next(b for b in at.button if b.key == f"mq_apply_{tag}")
    assert not apply_btn.disabled
    apply_btn.click().run()
    assert not at.exception

    entry = roster.by_sid[sid]
    packet = next(
        p for p in segment_pdf(main_fixture.pdf_path).packets if packet_tag(main_fixture.pdf_path, p) == tag
    )
    expected_path = output_path(out_dir, entry, packet.worksheet_type, round_label=round_label("10/03/2025"))
    assert expected_path.exists()
    assert list_manual_queue(out_dir) == [], "a never-held packet must never end up in the manual queue"

    decisions_file = decisions_dir / f"{Path(main_fixture.pdf_path).stem}.json"
    saved = json.loads(decisions_file.read_text())
    assert saved[tag] == sid, "applying a manual edit must record the decision, same as Confirm decision would"


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


@pytest.mark.parametrize("dpi", [100, 150, 300])
def test_canvas_rect_bbox_round_trip_at_multiple_dpis(dpi):
    """_bbox_to_canvas_rect (page points -> canvas pixel-space fabric.js
    rect) and _canvas_rect_to_bbox (its inverse) must round-trip exactly at
    any DPI the editor might render the background image at -- the drag-
    corner editor renders at review_app.DPI (RENDER_DPI_PREVIEW, 150) while
    the real redaction that consumes the resulting bbox runs at
    RENDER_DPI_FINAL (300); the conversion has to be correct at both, not
    just whichever one happens to be exercised by an end-to-end test."""
    original = (38.0, 58.0, 300.0, 148.0)
    rect = review_app._bbox_to_canvas_rect(original, dpi)
    recovered = review_app._canvas_rect_to_bbox(rect, dpi)
    for a, b in zip(original, recovered):
        assert a == pytest.approx(b, abs=1e-6)


def test_canvas_rect_to_bbox_folds_in_scalex_scaley_for_a_resized_object():
    """fabric.js reports a corner-dragged resize as scaleX/scaleY factors
    on top of the object's original width/height, not as new width/height
    values -- _canvas_rect_to_bbox must fold both in, or a reviewer
    resizing an existing box (as opposed to drawing a new one) would
    silently redact the box's ORIGINAL, unresized extent."""
    dpi = 150
    scale = dpi / 72.0
    obj = {
        "left": 38.0 * scale,
        "top": 58.0 * scale,
        "width": 100.0 * scale,
        "height": 20.0 * scale,
        "scaleX": 2.0,  # dragged twice as wide
        "scaleY": 1.5,  # and 1.5x as tall
    }
    bbox = review_app._canvas_rect_to_bbox(obj, dpi)
    assert bbox == pytest.approx((38.0, 58.0, 38.0 + 200.0, 58.0 + 30.0), abs=1e-6)


def test_canvas_drawn_rectangle_at_a_different_render_scale_redacts_the_intended_region(main_fixture, tmp_path):
    """End-to-end proof that a box drawn on the editor's own preview-scale
    canvas (review_app.DPI, 150) still redacts the correct region once fed
    into the real redaction, which rasterizes at a DIFFERENT dpi
    (RENDER_DPI_FINAL, 300) -- exactly the "canvas rendered at a different
    scale than its PDF page size" case. Simulates the fabric.js object
    st_canvas would return for a box a reviewer dragged over the header's
    known Name/Teacher/Group column (AppTest cannot drive the canvas
    component's own drag interaction -- see the manual-queue test above for
    why every test here works this way), converts it with
    _canvas_rect_to_bbox at the editor's own DPI, and applies it as
    redact_packet's header_bbox_override at a different DPI entirely."""
    packet = next(p for p in segment_pdf(main_fixture.pdf_path).packets if p.header_page_index == 0)
    roster = load_roster(main_fixture.roster_path, infer_period_from=main_fixture.pdf_path)

    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        page = pdf.pages[0]
        preview_width, preview_height = page.to_image(resolution=review_app.DPI).original.size
    assert (preview_width, preview_height) != (612, 792)  # genuinely a different pixel scale than the PDF's own points

    # A generous box covering the whole left header column at the editor's
    # own preview DPI, as fabric.js's canvas_result.json_data would report
    # it (pixel-space left/top/width/height, no resize applied).
    drawn_rect_obj = {
        "type": "rect",
        "left": 30.0 * (review_app.DPI / 72.0),
        "top": 55.0 * (review_app.DPI / 72.0),
        "width": 370.0 * (review_app.DPI / 72.0),
        "height": 95.0 * (review_app.DPI / 72.0),
        "scaleX": 1,
        "scaleY": 1,
    }
    bbox = review_app._canvas_rect_to_bbox(drawn_rect_obj, review_app.DPI)

    out_path = tmp_path / "canvas_redacted.pdf"
    result = redact_packet(
        main_fixture.pdf_path,
        packet,
        out_path,
        dpi=RENDER_DPI_FINAL,
        stamp_lines=["SID: 0204150201", "PD: 02"],
        header_bbox_override=(bbox, bbox),
    )
    assert result.band is not None

    findings = verify_no_leaked_names(out_path, roster)
    assert findings == [], f"box drawn at DPI {review_app.DPI} failed to redact under real DPI {RENDER_DPI_FINAL}: {findings}"


# --- Rotation performance: preview vs. commit (see CLAUDE.md's rotation
# performance section) ---


def test_page_stack_preview_rotation_does_not_rerasterize_or_reocr(main_fixture, monkeypatch):
    """Rotating a page in the page-stack preview (Left/Right/180 -- see
    review_app._render_page_stack) must be visually immediate: it should
    never call back into the PDF (pdfplumber's own Page.to_image) or OCR
    (melredact.ocr's engine), only rotate the already-rendered base image
    in memory. Warms the base image once (the way an ordinary first view
    of the packet already would), then blocks any further PDF/OCR access
    and cycles through several candidate rotations exactly the way
    _render_page_stack's own preview path does."""
    pdf_path = str(main_fixture.pdf_path)
    page_idx = 0

    base_image = review_app._page_image(pdf_path, page_idx, review_app.DPI, {})

    calls = {"to_image": 0}
    orig_to_image = pdfplumber.page.Page.to_image

    def counting_to_image(self, *args, **kwargs):
        calls["to_image"] += 1
        return orig_to_image(self, *args, **kwargs)

    monkeypatch.setattr(pdfplumber.page.Page, "to_image", counting_to_image)

    import melredact.ocr as ocr_mod

    def _boom(*args, **kwargs):
        raise AssertionError("OCR engine must not be invoked while only previewing a rotation")

    monkeypatch.setattr(ocr_mod, "_engine", _boom)

    for angle in (90, 180, 270, 0, 90):
        # Same two calls _render_page_stack makes on every rerun to build
        # what it displays: the cached as-scanned image, rotated in memory
        # to the candidate preview angle.
        base_again = review_app._page_image(pdf_path, page_idx, review_app.DPI, {})
        preview = review_app._rotate_image_for_display(base_again, angle)
        assert preview is not None

    assert calls["to_image"] == 0, "previewing a rotation re-rasterized the page from the PDF"

    # The rotation itself is correct, not just cheap: a 90 or 270 rotation
    # (expand=True) swaps width/height; 0 and 180 don't.
    rotated_90 = review_app._rotate_image_for_display(base_image, 90)
    assert rotated_90.size == (base_image.height, base_image.width)
    rotated_180 = review_app._rotate_image_for_display(base_image, 180)
    assert rotated_180.size == base_image.size


def test_preview_rotation_flows_into_redaction_correctly_once_applied(main_fixture, tmp_path):
    """The preview mechanism (_set_pending_rotation) must never itself
    reach orientation_overrides -- only the explicit Apply/Reset actions
    (_set_page_rotation) do. Proves both halves: previewing several
    candidate angles leaves the committed state untouched, and applying
    one commits it correctly and reproduces through the real production
    path (segment_pdf -> run_dispositions), the same path a real run uses,
    with zero monkeypatching -- the same guarantee CLAUDE.md's rotation-
    performance fix has to preserve, not just speed up."""
    rotated_path = build_rotated_page_copy(main_fixture.pdf_path, tmp_path, 0, 180, "preview_then_apply.pdf")
    decisions_dir = tmp_path / "decisions"

    st.session_state.clear()
    st.session_state.orientation_overrides = {}
    st.session_state.pending_rotation = {}

    # Cycling through candidate angles in the preview must not touch the
    # real, committed orientation state at all.
    review_app._set_pending_rotation(0, 90)
    review_app._set_pending_rotation(0, 270)
    review_app._set_pending_rotation(0, 180)
    assert st.session_state.orientation_overrides == {}
    assert st.session_state.pending_rotation == {0: 180}

    # Apply: commits the previewed angle for real, and clears the preview.
    review_app._set_page_rotation(str(rotated_path), str(decisions_dir), 0, 180)
    assert st.session_state.orientation_overrides == {0: 180}
    assert st.session_state.pending_rotation == {}

    reloaded = load_orientation_overrides(rotated_path, decisions_dir=decisions_dir)
    assert reloaded == {0: 180}

    seg = segment_pdf(rotated_path, orientation_overrides=reloaded)
    header_packet = seg.packets[0]
    assert header_packet.header_page_index == 0
    assert not any("orientation" in issue for issue in header_packet.issues)

    roster = load_roster(main_fixture.roster_path, infer_period_from=main_fixture.pdf_path)
    tag = packet_tag(rotated_path, header_packet)
    sid = next(e.sid for e in roster if e.full_name == "Jordan Ames")

    out_dir = tmp_path / "out"
    results = run_dispositions(
        rotated_path, seg, {tag: sid}, roster, out_dir=out_dir, orientation_overrides=reloaded
    )
    result = next(r for r in results if r.packet_tag == tag)
    assert not result.held_back, result.reason
    assert result.out_path is not None and result.out_path.exists()
    assert verify_no_leaked_names(result.out_path, roster) == []


# --- Phase 2: roster search must be able to select a decision the matcher
# never proposed (see CLAUDE.md's "a roster entry chosen by search cannot
# be selected" section) ---


def _build_no_name_pdf(tmp_path):
    """A single header packet whose Name field was left blank by the
    student -- the real "packet 70" shape this session's bug report
    described: OCR reads the printed 'Name:' label but no handwritten
    value at all, so the matcher has zero candidates to propose and a
    reviewer has to resolve the student entirely through roster search.
    Two real roster entries so a search can pick one that was never a
    matcher candidate. Fictional names, never real student PII."""
    from melredact.config import (
        DATE_ANCHOR,
        FOOTER_PAGE_MARKER,
        FOOTER_WORKSHEET_TYPE,
        GROUP_ANCHOR,
        NAME_ANCHOR,
        PERIOD_ANCHOR,
        TEACHER_ANCHOR,
    )
    from tests.make_fixture import InvisibleText, PdfBuilder, _write_roster_csv, render_header_image

    worksheet_type_text = "PRT (01/2024)"
    page_marker = "Page 1 of 1"
    img = render_header_image(
        name_text="",
        teacher_text="Hannel",
        group_text="",
        date_text="10/03/2025",
        period_text="02",
        worksheet_type=worksheet_type_text,
        page_marker=page_marker,
        shade_blank_rows=False,
    )
    items = [
        InvisibleText("Name:", NAME_ANCHOR["x0"], NAME_ANCHOR["top"], 9),
        InvisibleText("Teacher:", TEACHER_ANCHOR["x0"], TEACHER_ANCHOR["top"], 9),
        InvisibleText("Hannel", 150, TEACHER_ANCHOR["top"]),
        InvisibleText("Group members, if any:", GROUP_ANCHOR["x0"], GROUP_ANCHOR["top"], 9),
        InvisibleText("Date:", DATE_ANCHOR["x0"], NAME_ANCHOR["top"], 9),
        InvisibleText("10/03/2025", 450, NAME_ANCHOR["top"]),
        InvisibleText("Period:", PERIOD_ANCHOR["x0"], TEACHER_ANCHOR["top"], 9),
        InvisibleText("02", 450, TEACHER_ANCHOR["top"]),
        InvisibleText(worksheet_type_text, FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
        InvisibleText(page_marker, FOOTER_PAGE_MARKER["x0"], FOOTER_PAGE_MARKER["top"], 9),
    ]
    builder = PdfBuilder()
    builder.add_page(img, items)
    pdf_path = tmp_path / "no_name.pdf"
    builder.save(pdf_path)

    # match.propose always scores and returns *every* roster entry (see its
    # own docstring: "review needs the top candidate regardless"), so a
    # roster small enough that the matcher's own top5 slice would include
    # the target anyway can't actually exercise "a decision search added
    # that the matcher never proposed" -- every entry scores identically
    # against an empty probe (score_pair against blank text), so a stable
    # sort keeps them in roster order. Six decoys ahead of the real target
    # guarantees the target falls outside the top5 slice review_app.py
    # actually renders as matcher candidates. SIDs follow make_fixture's
    # own <teacher_code><period><index> convention -- roster.py cross-
    # checks the SID's own period digits against the rest of its block.
    def _test_sid(index: int) -> str:
        return f"020415" f"02" f"{index:02d}"

    decoys = [(_test_sid(i), f"Decoy{i}", "Student") for i in range(1, 7)]
    target_sid = _test_sid(7)
    roster_path = tmp_path / "roster.csv"
    _write_roster_csv(roster_path, decoys + [(target_sid, "Lu", "Brian")])
    return pdf_path, roster_path, target_sid


def test_roster_search_selection_survives_a_later_confirm_click(tmp_path):
    """Real bug, reported 2026-08-15 (packet 70): search-selecting a
    student not among the matcher's own candidates recorded the right SID
    immediately, but the Decision radio's own stale default ("Not on
    roster", since OCR read no name at all) silently overwrote it back to
    a non-consent rejection the moment a reviewer clicked the big, obvious
    "Confirm decision" button afterward. This drives that exact sequence
    -- search, select, use, THEN click Confirm decision -- and asserts the
    searched SID survives, proving the radio and the search action can no
    longer disagree about what's selected."""
    pdf_path, roster_path, target_sid = _build_no_name_pdf(tmp_path)
    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"
    at = _launch(pdf_path, roster_path, out_dir, decisions_dir)

    tag = packet_tag(pdf_path, segment_pdf(pdf_path).packets[0])
    assert at.session_state.decisions.get(tag) is None or tag not in at.session_state.decisions

    query = next(w for w in at.text_input if w.key == f"search_{tag}")
    query.set_value("Brian").run()
    select = next(w for w in at.selectbox if w.key == f"search_select_{tag}")
    assert select.options == [f"{target_sid} — Brian Lu"]
    use_btn = next(b for b in at.button if b.key == f"search_use_{tag}")
    use_btn.click().run()
    assert not at.exception

    # The search action alone already recorded the right decision --
    # this much worked even before the fix.
    assert at.session_state.decisions[tag] == target_sid

    # The radio must now show the searched entry selected, clearly marked
    # as having come from search rather than the matcher.
    radio = next(w for w in at.radio if w.key == f"decision_{tag}")
    assert "found via roster search" in radio.value
    assert target_sid in radio.value

    # The real regression: clicking Confirm decision afterward -- exactly
    # what a reviewer who doesn't fully trust the search action alone
    # would naturally do next -- must reaffirm the same SID, not silently
    # revert it to a non-consent rejection.
    confirm_btn = next(b for b in at.button if b.key == f"confirm_{tag}")
    assert not confirm_btn.disabled
    confirm_btn.click().run()
    assert not at.exception
    assert at.session_state.decisions[tag] == target_sid, "Confirm decision silently reverted a search-selected SID"


def test_packet_with_no_ocrd_name_has_no_preselected_decision(tmp_path):
    """A confident-looking default on zero evidence is itself a hazard (see
    CLAUDE.md): when OCR reads no name at all and nothing has been decided
    yet, the Decision radio must show a genuine "nothing chosen" placeholder
    -- never "Not on roster" pre-selected and one accidental Confirm click
    away from silently rejecting a real, unreviewed student."""
    pdf_path, roster_path, _target_sid = _build_no_name_pdf(tmp_path)
    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"
    at = _launch(pdf_path, roster_path, out_dir, decisions_dir)
    tag = packet_tag(pdf_path, segment_pdf(pdf_path).packets[0])

    radio = next(w for w in at.radio if w.key == f"decision_{tag}")
    assert radio.options[0].startswith("—"), "expected a genuine no-selection placeholder as the first option"
    assert radio.value == radio.options[0], "nothing should be preselected when OCR read no name at all"

    for key in (f"confirm_{tag}", f"confirm_next_{tag}"):
        btn = next(b for b in at.button if b.key == key)
        assert btn.disabled, f"{key} must stay disabled until a reviewer actually chooses a decision"


# --- Phase 1: manual editor canvas reliability (see CLAUDE.md's "the
# canvas is unusable" section) ---


def test_manual_editor_canvas_state_survives_a_simulated_rerun_without_losing_boxes(main_fixture, tmp_path):
    """A rerun this editor itself causes (a page-selector change, an
    unrelated widget, or -- what a real drag actually triggers -- st_canvas's
    own debounced sync back to Streamlit) must never discard boxes a
    reviewer already has in progress. AppTest can't drive a live canvas
    drag (third-party iframe), so this seeds `mq_regions_{tag}` the same
    way a completed drag would have left it (see the manual-queue release
    test above for the same convention), runs the app twice in a row --
    the second run is the "unrelated rerun" -- and asserts the exact same
    boxes are still there afterward: not reset to the auto-seeded
    detection pair, not cleared, not silently mutated."""
    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"
    tag = "packets_p000"

    seeded_boxes = [(40.0, 60.0, 200.0, 90.0), (210.0, 60.0, 380.0, 140.0)]

    sys.argv = [
        "review_app.py",
        str(main_fixture.pdf_path),
        str(main_fixture.roster_path),
        "--out-dir",
        str(out_dir),
        "--decisions-dir",
        str(decisions_dir),
    ]
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state[f"mq_regions_{tag}"] = {0: list(seeded_boxes)}
    at.run()
    assert not at.exception
    at.selectbox[0].set_value(tag).run()
    assert at.session_state[f"mq_regions_{tag}"][0] == seeded_boxes

    # A second, unrelated rerun (e.g. what st_canvas's own debounced sync
    # or an unrelated widget click would trigger) -- must not re-seed or
    # otherwise touch the boxes already recorded for this page.
    at.run()
    assert not at.exception
    assert at.session_state[f"mq_regions_{tag}"][0] == seeded_boxes

    # The canvas widget's own key must be stable across both reruns above
    # (never regenerated from anything that changes run to run) -- a
    # changing key is exactly what would force the frontend component to
    # remount and discard in-progress state.
    assert f"mq_canvas_{tag}_0" in at.session_state


@pytest.mark.parametrize("dpi_source", ["header_page"])
def test_editor_dpi_differs_from_module_dpi_and_round_trips_exactly(main_fixture, dpi_source):
    """The manual editor renders at a page-derived DPI (see
    config.MANUAL_EDITOR_TARGET_WIDTH_PX / review_app._editor_dpi_for_page),
    not the module-level review_app.DPI every other preview in this file
    uses -- proves the two are genuinely different for a real page (so this
    is a real behavior change, not a no-op), and that the pixel<->point
    conversion functions still round-trip exactly at whatever DPI that
    computation actually picks, the same guarantee test_canvas_rect_bbox_
    round_trip_at_multiple_dpis already gives at a fixed list of DPIs."""
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        page_width_pt = pdf.pages[0].width

    editor_dpi = review_app._editor_dpi_for_page(page_width_pt)
    assert editor_dpi != review_app.DPI

    rendered_width_px = round(page_width_pt * editor_dpi / 72.0)
    # Within a couple of pixels of the configured target width -- rounding
    # the DPI to an integer, then the width to an integer again, can't land
    # exactly on the target every time.
    from melredact.config import MANUAL_EDITOR_TARGET_WIDTH_PX

    assert abs(rendered_width_px - MANUAL_EDITOR_TARGET_WIDTH_PX) <= 5

    original = (38.0, 58.0, 300.0, 148.0)
    rect = review_app._bbox_to_canvas_rect(original, editor_dpi)
    recovered = review_app._canvas_rect_to_bbox(rect, editor_dpi)
    for a, b in zip(original, recovered):
        assert a == pytest.approx(b, abs=1e-6)


def test_manual_editor_both_panes_report_identical_pixel_dimensions(main_fixture, tmp_path):
    """The canvas pane (a fixed-pixel-size custom component) and the
    preview pane (a plain st.image, which -- unless given an explicit
    width -- silently shrinks to fit a narrower container) must render at
    the exact same pixel size, or the two visibly drift apart (see
    CLAUDE.md's Phase 1 root-cause writeup). Both panes' own captions
    embed their rendered `WxH` in pixels (see _render_manual_editor)
    specifically so this is directly observable without reaching into
    Streamlit's custom-component protocol, which AppTest doesn't expose
    pixel dimensions for."""
    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"
    tag = "packets_p000"
    at = _launch(main_fixture.pdf_path, main_fixture.roster_path, out_dir, decisions_dir)
    at.selectbox[0].set_value(tag).run()

    dims_pattern = re.compile(r"(\d+)×(\d+)px")
    captions_with_dims = [c.value for c in at.caption if dims_pattern.search(c.value)]
    assert len(captions_with_dims) >= 2, "expected both the original and redacted panes to report their pixel size"

    dims = [dims_pattern.search(c).groups() for c in captions_with_dims]
    assert len(set(dims)) == 1, f"panes reported different pixel dimensions: {dims}"


# --- Phase 3: page composition editor for out-of-order scans (see
# CLAUDE.md's "pages scanned out of order" section) ---


def test_reversed_continuation_header_pair_is_proposed_not_auto_applied(tmp_path):
    """A scanner-reversed continuation-before-header pair (see segment.
    find_reversed_continuation_header_pairs) must never be silently fixed
    -- the header-started packet still shows its stale "footer declared 2"
    segmentation issue the moment the app loads, before any reviewer
    action, proving detection alone never mutates anything."""
    pdf_path = build_reversed_pair_fixture(tmp_path / "reversed")
    main = build_main_fixture(tmp_path / "main")
    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"
    at = _launch(pdf_path, main.roster_path, out_dir, decisions_dir)

    tag = "reversed_pair_p003"
    at.selectbox[0].set_value(tag).run()
    assert not at.exception
    assert at.session_state.page_order is None, "detecting a reversal must never write a page-order override on its own"
    assert any("footer declared 2" in w.value for w in at.warning), "the stale segmentation issue must still be showing"

    apply_btn = next((b for b in at.button if b.key and b.key.startswith(f"pc_apply_reversal_{tag}_")), None)
    assert apply_btn is not None, "the reversal fix must be offered as a one-click proposal, not applied automatically"


def test_page_composition_editor_reorders_and_clears_stale_issue(tmp_path):
    """Applying the page composition editor's proposed fix must actually
    re-run segmentation for the affected packets -- clicking it saves a
    corrected page order and the packet's own stale "footer declared 2"
    issue (and its unresolved-issues warning) must be gone afterward, not
    merely hidden."""
    pdf_path = build_reversed_pair_fixture(tmp_path / "reversed")
    main = build_main_fixture(tmp_path / "main")
    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"
    at = _launch(pdf_path, main.roster_path, out_dir, decisions_dir)

    tag = "reversed_pair_p003"
    at.selectbox[0].set_value(tag).run()
    apply_btn = next(b for b in at.button if b.key and b.key.startswith(f"pc_apply_reversal_{tag}_"))
    apply_btn.click().run()
    assert not at.exception

    assert at.session_state.page_order == [0, 1, 3, 2]

    seg = segment_pdf(pdf_path, page_sequence=at.session_state.page_order)
    fixed = next(p for p in seg.packets if p.page_indices and p.page_indices[0] == 3)
    assert fixed.page_indices == [3, 2]
    assert fixed.issues == [], "the composition fix must clear the stale footer/orphan issues, not just paper over them"

    # And the UI itself reflects the fix on the very next render, not just
    # the underlying segment_pdf call -- re-selecting the same tag (stable
    # here, since physical page 3 is still page_indices[0] after the fix)
    # must no longer show the unresolved-issues warning.
    at.selectbox[0].set_value(tag).run()
    assert not any("footer declared 2" in w.value for w in at.warning)


# --- Phase 4: human-confirmable footer/page-count holds (see CLAUDE.md's
# "unreadable footers should not block a human who can see the page"
# section) ---


def test_unreadable_footer_packet_becomes_assignable_after_confirming_composition(tmp_path):
    """The real p086 shape end to end through the review UI: a packet
    blocked only by its continuation page's unreadable footer must have
    Confirm-with-a-SID disabled until a reviewer ticks the composition-
    override checkbox, and enabled (and actually recordable) immediately
    after."""
    pdf_path = build_unreadable_continuation_footer_fixture(tmp_path / "unreadable_footer")
    main = build_main_fixture(tmp_path / "main")
    out_dir = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"
    at = _launch(pdf_path, main.roster_path, out_dir, decisions_dir)

    tag = "unreadable_continuation_footer_p000"
    at.selectbox[0].set_value(tag).run()
    assert any("unreadable footer, cannot verify sequence" in w.value for w in at.warning)
    assert any("Fix page composition" in w.value for w in at.warning), "the warning must name the specific clearing action"

    jordan_option = next(o for o in at.radio[0].options if "Jordan Ames" in o)
    at.radio[0].set_value(jordan_option).run()
    confirm_btn = next(b for b in at.button if b.key == f"confirm_{tag}")
    assert confirm_btn.disabled, "Confirm must stay disabled until composition is confirmed"

    override_cb = next(cb for cb in at.checkbox if cb.key == f"composition_override_{tag}")
    override_cb.set_value(True).run()
    assert not at.exception

    confirm_btn = next(b for b in at.button if b.key == f"confirm_{tag}")
    assert not confirm_btn.disabled, "confirming composition must make the packet assignable"
    confirm_btn.click().run()
    assert not at.exception

    roster = load_roster(main.roster_path, infer_period_from=main.pdf_path)
    sid = next(e.sid for e in roster if e.full_name == "Jordan Ames")
    assert at.session_state.decisions[tag] == sid
