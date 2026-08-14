"""Regression tests for melredact.orientation -- content-based page-
orientation normalization, wired in as an early pipeline stage via
pdfio.open_pdf (see that module and orientation.py's own docstrings).

Real motivation: data/PRT/010406_PD1_PRT.pdf has two pages rotated 180
degrees by a real duplex-scanner flip (see CLAUDE.md's rotation-audit
section). All tests here build synthetic fixtures only -- never real
data -- by physically pre-rotating one page's own embedded raster content
(not just /Rotate metadata) via pikepdf/PIL, the same construction this
session validated by hand against the real correction formula before
writing orientation.py.
"""

from __future__ import annotations

import json

import pdfplumber
import pytest
from PIL import Image

from melredact import orientation
from melredact.redact import redact_packet, verify_no_leaked_names
from melredact.roster import load_roster
from melredact.segment import segment_pdf
from tests.make_fixture import build_main_fixture, build_rotated_page_copy, replace_page_content


@pytest.fixture(scope="module")
def main_fixture(tmp_path_factory):
    return build_main_fixture(tmp_path_factory.mktemp("orientation_fixture"))


def _build_rotated_copy(main_fixture, tmp_path, page_index: int, degrees: int, out_name: str):
    return build_rotated_page_copy(main_fixture.pdf_path, tmp_path, page_index, degrees, out_name)


def _build_blank_copy(main_fixture, tmp_path, page_index: int, out_name: str):
    """A copy of the main fixture with `page_index` replaced by a blank
    white page -- no printed content for the orientation classifier to
    read at all, the deliberately-unresolvable case (see config.py's
    ORIENTATION_MIN_SCORE: a blank page scores ~0.26, well under the 0.6
    floor)."""
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        page = pdf.pages[page_index]
        pw, ph = page.width, page.height
        px_w, px_h = page.to_image(resolution=150).original.size
    blank = Image.new("RGB", (px_w, px_h), "white")
    return replace_page_content(main_fixture.pdf_path, page_index, tmp_path, out_name, blank, (pw, ph))


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_confident_nonzero_rotation_is_held_for_confirmation_not_auto_applied(main_fixture, tmp_path, degrees):
    """Detect-and-ask (2026-08-14): a confident, specific, nonzero rotation
    guess is never applied automatically -- see orientation.py's own module
    docstring for why real data (the p084/p085 diagnostic) shows no score-
    based way to separate a confident-and-correct guess from a
    confident-and-wrong one. The packet is held, naming the page, the
    guessed angle, and the score -- exactly what a reviewer needs to
    confirm or correct it."""
    rotated_path = _build_rotated_copy(main_fixture, tmp_path, 0, degrees, f"rotated_{degrees}.pdf")

    result = orientation.normalize_pdf(rotated_path)
    page0 = result.pages[0]
    assert page0.confident
    assert page0.detected_angle == degrees
    assert page0.applied_angle == 0
    assert not page0.resolved
    assert page0.needs_confirmation
    assert 0 in result.pending_confirmation_page_indices()

    # segment_pdf reads a still-unconfirmed, genuinely-rotated page exactly
    # as found (never a guessed rotation) -- for some angles the printed
    # "Name:" label happens to still be findable by pdfplumber's own
    # rotation-aware text extraction, for others it isn't, so which packet
    # ends up containing page 0 (a real header packet vs. an orphan) isn't
    # what this test is checking. What matters is that *whichever* packet
    # contains page 0 carries the pending-confirmation issue naming it.
    segmented = segment_pdf(rotated_path)
    packet_with_page0 = next(p for p in segmented.packets if 0 in p.page_indices)
    matching_issues = [
        i for i in packet_with_page0.issues if f"page 0: orientation detected as rotated {degrees}" in i
    ]
    assert matching_issues, packet_with_page0.issues
    assert any("not yet confirmed by a reviewer" in i for i in matching_issues)


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_human_confirmed_rotation_is_applied_and_redacted_correctly(main_fixture, tmp_path, degrees):
    """A human's override (page_index -> angle) always wins, for any page,
    and flows all the way through segmentation, matching, and redaction --
    see orientation.py's `resolve_pages` and pdfio.open_pdf's
    `orientation_overrides` parameter."""
    rotated_path = _build_rotated_copy(main_fixture, tmp_path, 0, degrees, f"rotated_confirmed_{degrees}.pdf")
    overrides = {0: degrees}

    result = orientation.normalize_pdf(rotated_path, overrides=overrides)
    page0 = result.pages[0]
    assert page0.applied_angle == degrees
    assert page0.source == "human"
    assert page0.resolved
    assert not page0.needs_confirmation

    segmented = segment_pdf(rotated_path, orientation_overrides=overrides)
    header_packet = segmented.packets[0]
    assert header_packet.header_page_index == 0
    assert not any("orientation" in issue for issue in header_packet.issues)

    roster = load_roster(main_fixture.roster_path)
    out_path = tmp_path / f"redacted_confirmed_{degrees}.pdf"
    redact_result = redact_packet(
        rotated_path,
        header_packet,
        out_path,
        stamp_lines=["SID: 0204150201", "PD: 02"],
        orientation_overrides=overrides,
    )
    assert redact_result.band is not None
    assert redact_result.band.detected

    findings = verify_no_leaked_names(out_path, roster)
    assert findings == []

    with pdfplumber.open(out_path) as pdf:
        assert pdf.pages[0].width == pytest.approx(612, abs=1)
        assert pdf.pages[0].height == pytest.approx(792, abs=1)


def test_orientation_that_cannot_be_confidently_determined_holds_the_packet_naming_the_page(main_fixture, tmp_path):
    blank_path = _build_blank_copy(main_fixture, tmp_path, 1, "blank_continuation.pdf")

    result = orientation.normalize_pdf(blank_path)
    page1 = result.pages[1]
    assert not page1.confident
    assert not page1.resolved
    assert not page1.needs_confirmation
    # Scoped to "page 1 is among the unresolved pages," not "is the only
    # one" -- this fixture's own other stock continuation pages ("(continued)"
    # plus a short footer line) are independently sparse enough to also
    # score under ORIENTATION_MIN_SCORE on their own content, same as this
    # test's deliberately-blank page 1 (see the real-vs-synthetic-fixture
    # note in the rotation test above). What this test actually checks is
    # that a page THIS TEST made unresolvable gets held, naming that page.
    assert 1 in result.unresolved_page_indices()

    segmented = segment_pdf(blank_path)
    header_packet = segmented.packets[0]
    assert 1 in header_packet.page_indices
    matching_issues = [i for i in header_packet.issues if "orientation" in i and " 1:" in i]
    assert matching_issues, header_packet.issues


def test_applied_rotation_is_persisted_and_reproduces_without_redetecting(main_fixture, tmp_path, monkeypatch):
    rotated_path = _build_rotated_copy(main_fixture, tmp_path, 0, 180, "rotated_persist.pdf")
    overrides = {0: 180}

    first = orientation.normalize_pdf(rotated_path, overrides=overrides)
    assert first.pages[0].applied_angle == 180
    assert first.pages[0].source == "human"

    detect_path, manifest_path, norm_path = orientation._cache_paths(rotated_path, overrides)
    assert detect_path.exists()
    assert manifest_path.exists()
    assert norm_path.exists()
    stored = json.loads(manifest_path.read_text())
    assert stored[0]["applied_angle"] == 180

    def _boom(*a, **k):
        raise AssertionError("classifier called again -- cache was not reused")

    monkeypatch.setattr(orientation, "classify_orientation", _boom)
    second = orientation.normalize_pdf(rotated_path, overrides=overrides)
    assert second.pages[0].applied_angle == 180
    assert second.normalized_path == first.normalized_path

    # A *different* override set must not reuse the first one's resolved
    # cache entry (it can resolve to genuinely different content) -- only
    # the raw detection step (already asserted not to re-run above) is
    # shared across override sets.
    third = orientation.normalize_pdf(rotated_path, overrides={0: 90})
    assert third.pages[0].applied_angle == 90
    assert third.normalized_path != first.normalized_path


def test_upright_file_is_not_rewritten(main_fixture):
    result = orientation.normalize_pdf(main_fixture.pdf_path)
    assert result.rotated_page_indices() == []
    assert result.normalized_path == main_fixture.pdf_path
