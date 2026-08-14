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
import zlib

import pdfplumber
import pikepdf
import pytest
from pikepdf import Dictionary, Name
from PIL import Image

from melredact import orientation
from melredact.redact import redact_packet, verify_no_leaked_names
from melredact.roster import load_roster
from melredact.segment import segment_pdf
from tests.make_fixture import build_main_fixture


@pytest.fixture(scope="module")
def main_fixture(tmp_path_factory):
    return build_main_fixture(tmp_path_factory.mktemp("orientation_fixture"))


def _replace_page_content(pdf_path, page_index: int, tmp_path, out_name: str, image: Image.Image, page_size):
    """Rebuild one page of `pdf_path` from a caller-supplied raster image
    (and page size), copying every other page across unchanged (including
    their own invisible OCR text layer -- see make_fixture.PdfBuilder) via
    pikepdf's own cross-document page copy. The replaced page carries no
    text layer at all, same as a real scan (see CLAUDE.md's "Real scans
    have no text layer at all")."""
    pw, ph = page_size
    with pikepdf.open(pdf_path) as src:
        out_pdf = pikepdf.Pdf.new()
        for idx, page in enumerate(src.pages):
            if idx != page_index:
                out_pdf.pages.append(page)
                continue
            new_page = out_pdf.add_blank_page(page_size=(pw, ph))
            compressed = zlib.compress(image.tobytes())
            im_obj = pikepdf.Stream(out_pdf, compressed)
            im_obj.Type = Name.XObject
            im_obj.Subtype = Name.Image
            im_obj.Width = image.width
            im_obj.Height = image.height
            im_obj.ColorSpace = Name.DeviceRGB
            im_obj.BitsPerComponent = 8
            im_obj.Filter = Name.FlateDecode
            new_page.Resources = out_pdf.make_indirect(
                Dictionary(XObject=Dictionary(Im0=out_pdf.make_indirect(im_obj)))
            )
            new_page.Contents = out_pdf.make_indirect(
                pikepdf.Stream(out_pdf, f"q {pw} 0 0 {ph} 0 0 cm /Im0 Do Q".encode())
            )
        out_path = tmp_path / out_name
        out_pdf.save(out_path)
    return out_path


def _build_rotated_copy(main_fixture, tmp_path, page_index: int, degrees: int, out_name: str):
    """A copy of the main fixture with `page_index`'s own embedded content
    physically pre-rotated by `degrees` (simulating a real scanner flip,
    not a /Rotate-metadata-only mislabel) -- the sign convention (`-degrees`
    to simulate a page that needs `degrees` of correction) is the same one
    validated against the real classifier before writing orientation.py."""
    with pdfplumber.open(main_fixture.pdf_path) as pdf:
        page = pdf.pages[page_index]
        image = page.to_image(resolution=150).original.convert("RGB")
        pw, ph = page.width, page.height
    rotated = image.rotate(-degrees, expand=True) if degrees else image
    new_size = (ph, pw) if degrees in (90, 270) else (pw, ph)
    return _replace_page_content(main_fixture.pdf_path, page_index, tmp_path, out_name, rotated, new_size)


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
    return _replace_page_content(main_fixture.pdf_path, page_index, tmp_path, out_name, blank, (pw, ph))


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_rotated_header_page_is_normalized_upright_and_redacted_correctly(main_fixture, tmp_path, degrees):
    rotated_path = _build_rotated_copy(main_fixture, tmp_path, 0, degrees, f"rotated_{degrees}.pdf")

    result = orientation.normalize_pdf(rotated_path)
    page0 = result.pages[0]
    assert page0.confident
    assert page0.applied_angle == degrees
    assert page0.resolved

    segmented = segment_pdf(rotated_path)
    header_packet = segmented.packets[0]
    assert header_packet.header_page_index == 0
    # Scoped to page 0 specifically (the page this test actually rotated) --
    # the fixture's own stock continuation page (page 1, "(continued)" plus
    # a footer line) is sparse enough to independently score under
    # ORIENTATION_MIN_SCORE on its own content alone, same as a blank page
    # (see test_orientation_that_cannot_be_confidently_determined_holds_
    # the_packet_naming_the_page below) -- a real, expected consequence of
    # this fixture's minimal continuation-page content, not something this
    # rotation test is exercising. Real continuation pages (see CLAUDE.md's
    # rotation-audit section) always classified confidently.
    assert not any("page 0: orientation" in issue for issue in header_packet.issues)

    roster = load_roster(main_fixture.roster_path)
    out_path = tmp_path / f"redacted_{degrees}.pdf"
    redact_result = redact_packet(rotated_path, header_packet, out_path, stamp_lines=["SID: 0204150201", "PD: 02"])
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

    first = orientation.normalize_pdf(rotated_path)
    assert first.pages[0].applied_angle == 180

    manifest_path, norm_path = orientation._cache_paths(rotated_path)
    assert manifest_path.exists()
    assert norm_path.exists()
    stored = json.loads(manifest_path.read_text())
    assert stored[0]["applied_angle"] == 180

    def _boom(*a, **k):
        raise AssertionError("classifier called again -- cache was not reused")

    monkeypatch.setattr(orientation, "classify_orientation", _boom)
    second = orientation.normalize_pdf(rotated_path)
    assert second.pages[0].applied_angle == 180
    assert second.normalized_path == first.normalized_path


def test_upright_file_is_not_rewritten(main_fixture):
    result = orientation.normalize_pdf(main_fixture.pdf_path)
    assert result.rotated_page_indices() == []
    assert result.normalized_path == main_fixture.pdf_path
