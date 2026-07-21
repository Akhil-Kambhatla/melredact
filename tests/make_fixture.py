"""Synthetic fixture generator.

Builds fake worksheet PDFs that reproduce the structure of the real scans
(header block with drawn border, printed labels, invisible OCR text layer,
footer with worksheet type + "Page X of Y") without containing any real
student's name. Real PDFs/CSVs can never be committed; this generates safe
stand-ins at test time instead.

Three fixtures:

- `build_main_fixture` — 9 packets against a 12-name roster, covering the
  traps documented in the build spec: OCR-garbled name, group-row naming a
  different roster student, blank Teacher/Group fields with a shaded band,
  illegible scrawl, near-collision surnames, variable packet length, extra
  packets with no roster match, and roster entries with no packet at all.
- `build_packet_heavy_fixture` — 14 packets against a 6-name roster, the
  ratio the real files actually run (more packets than consented students,
  not fewer). Stresses the "each roster entry claimed once" invariant with
  decoys built to be close-but-wrong for an entry a different packet
  legitimately claims.
- `build_footer_edge_case_fixture` — small fixture isolating footer/packet
  segmentation failures: an unreadable footer, and a packet missing its
  first page.

Run directly to write all fixtures to a directory for manual inspection:

    python -m tests.make_fixture /path/to/out
"""

from __future__ import annotations

import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
from pikepdf import Dictionary, Name
from PIL import Image, ImageDraw, ImageFont

from melredact.config import (
    DATE_ANCHOR,
    FOOTER_PAGE_MARKER,
    FOOTER_WORKSHEET_TYPE,
    GROUP_ANCHOR,
    HEADER_BAND_FALLBACK,
    NAME_ANCHOR,
    PAGE_HEIGHT_PT,
    PAGE_WIDTH_PT,
    PERIOD_ANCHOR,
    TEACHER_ANCHOR,
)

IMG_SCALE = 2  # pixels per point in the rasterized background
INK_COLOR = (20, 20, 20)
SHADE_COLOR = (222, 222, 222)
BORDER_COLOR = (0, 0, 0)

WORKSHEET_TYPE_TEXT = "pcMEL MPR+ADR (06/2025)"


def _font(size_pt: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size_pt * IMG_SCALE)


def _px(pt: float) -> float:
    return pt * IMG_SCALE


def _pdf_y(top_pt: float, font_size: int) -> float:
    """Convert a spec-style 'top' (distance from page top) to a PDF baseline
    y (distance from page bottom), approximating font ascent."""
    return PAGE_HEIGHT_PT - top_pt - font_size * 0.8


def _escape_pdf_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


@dataclass
class InvisibleText:
    text: str
    x: float  # pt
    top: float  # pt, spec convention (distance from page top)
    size: int = 10


def _draw_border(draw: ImageDraw.ImageDraw, band: dict) -> None:
    left, top, right, bottom = band["left"], band["top"], band["right"], band["bottom"]
    draw.rectangle(
        [_px(left), _px(top), _px(right), _px(bottom)],
        outline=BORDER_COLOR,
        width=max(1, int(_px(1.5))),
    )


def _draw_label_row(draw: ImageDraw.ImageDraw, anchor: dict, label: str) -> None:
    draw.text((_px(anchor["x0"]), _px(anchor["top"])), label, fill=BORDER_COLOR, font=_font(9))


def render_header_image(
    *,
    name_text: str,
    teacher_text: str,
    group_text: str,
    date_text: str,
    period_text: str,
    worksheet_type: str,
    page_marker: str,
    shade_blank_rows: bool,
    block_offset_pt: float = 0.0,
) -> Image.Image:
    """`block_offset_pt` shifts the whole bordered block (border + Name/
    Teacher/Group/Date/Period rows) down the page, independent of the fixed
    title-text position above it -- simulating a worksheet type whose own
    title/instructions block is taller than MPR's own (confirmed on a real
    second worksheet type, PRT: its two-line title pushes the header block
    ~37-44pt further down the page than MPR's). Zero (the default)
    reproduces the exact original MPR-shaped layout."""
    w, h = int(_px(PAGE_WIDTH_PT)), int(_px(PAGE_HEIGHT_PT))
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)

    draw.text((_px(45), _px(20)), "B. Model Plausibility Ratings", fill=BORDER_COLOR, font=_font(11))

    def _shift(anchor: dict) -> dict:
        return {**anchor, "top": anchor["top"] + block_offset_pt}

    band = {**HEADER_BAND_FALLBACK, "top": HEADER_BAND_FALLBACK["top"] + block_offset_pt, "bottom": HEADER_BAND_FALLBACK["bottom"] + block_offset_pt}
    name_anchor = _shift(NAME_ANCHOR)
    teacher_anchor = _shift(TEACHER_ANCHOR)
    group_anchor = _shift(GROUP_ANCHOR)

    if shade_blank_rows:
        shade_top = teacher_anchor["top"] - 2
        draw.rectangle([_px(band["left"] + 1), _px(shade_top), _px(band["right"] - 1), _px(band["bottom"] - 1)], fill=SHADE_COLOR)
    _draw_border(draw, band)

    _draw_label_row(draw, name_anchor, "Name:")
    _draw_label_row(draw, teacher_anchor, "Teacher:")
    _draw_label_row(draw, group_anchor, "Group members, if any:")
    draw.text((_px(DATE_ANCHOR["x0"]), _px(name_anchor["top"])), "Date:", fill=BORDER_COLOR, font=_font(9))
    draw.text((_px(PERIOD_ANCHOR["x0"]), _px(teacher_anchor["top"])), "Period:", fill=BORDER_COLOR, font=_font(9))

    value_x = 150.0
    if name_text:
        draw.text((_px(value_x), _px(name_anchor["top"])), name_text, fill=INK_COLOR, font=_font(10))
    if teacher_text:
        draw.text((_px(value_x), _px(teacher_anchor["top"])), teacher_text, fill=INK_COLOR, font=_font(10))
    if group_text:
        draw.text((_px(value_x), _px(group_anchor["top"])), group_text, fill=INK_COLOR, font=_font(10))
    draw.text((_px(450), _px(name_anchor["top"])), date_text, fill=INK_COLOR, font=_font(10))
    draw.text((_px(450), _px(teacher_anchor["top"])), period_text, fill=INK_COLOR, font=_font(10))

    draw.text((_px(45), _px(170 + block_offset_pt)), "1. Please work on this individually:", fill=BORDER_COLOR, font=_font(10))
    _draw_footer(draw, worksheet_type, page_marker)
    return img


def render_continuation_image(*, worksheet_type: str, page_marker: str, body: str = "") -> Image.Image:
    w, h = int(_px(PAGE_WIDTH_PT)), int(_px(PAGE_HEIGHT_PT))
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    draw.text((_px(45), _px(40)), body or "(continued)", fill=BORDER_COLOR, font=_font(10))
    _draw_footer(draw, worksheet_type, page_marker)
    return img


def _draw_footer(draw: ImageDraw.ImageDraw, worksheet_type: str, page_marker: str) -> None:
    if worksheet_type:
        draw.text(
            (_px(FOOTER_WORKSHEET_TYPE["x0"]), _px(FOOTER_WORKSHEET_TYPE["top"])),
            worksheet_type,
            fill=BORDER_COLOR,
            font=_font(9),
        )
    if page_marker:
        draw.text(
            (_px(FOOTER_PAGE_MARKER["x0"]), _px(FOOTER_PAGE_MARKER["top"])),
            page_marker,
            fill=BORDER_COLOR,
            font=_font(9),
        )


class PdfBuilder:
    """Thin wrapper for assembling a pikepdf document page by page: one
    full-page raster background (the "scan") plus an invisible OCR text
    layer at spec'd coordinates. Mirrors how the real files arrive."""

    def __init__(self) -> None:
        self.pdf = pikepdf.Pdf.new()
        self._font_ref = None

    def _font(self):
        if self._font_ref is None:
            self._font_ref = self.pdf.make_indirect(
                Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
            )
        return self._font_ref

    def add_page(self, image: Image.Image, invisible_items: list[InvisibleText]) -> None:
        page = self.pdf.add_blank_page(page_size=(PAGE_WIDTH_PT, PAGE_HEIGHT_PT))

        raw = image.tobytes()
        compressed = zlib.compress(raw)
        im_obj = pikepdf.Stream(self.pdf, compressed)
        im_obj.Type = Name.XObject
        im_obj.Subtype = Name.Image
        im_obj.Width = image.width
        im_obj.Height = image.height
        im_obj.ColorSpace = Name.DeviceRGB
        im_obj.BitsPerComponent = 8
        im_obj.Filter = Name.FlateDecode

        page.Resources = self.pdf.make_indirect(
            Dictionary(
                XObject=Dictionary(Im0=self.pdf.make_indirect(im_obj)),
                Font=Dictionary(F1=self._font()),
            )
        )

        parts = [f"q {PAGE_WIDTH_PT} 0 0 {PAGE_HEIGHT_PT} 0 0 cm /Im0 Do Q".encode()]
        for item in invisible_items:
            y = _pdf_y(item.top, item.size)
            text = _escape_pdf_text(item.text)
            parts.append(
                f"BT /F1 {item.size} Tf 3 Tr 1 0 0 1 {item.x} {y} Tm ({text}) Tj ET".encode()
            )
        content = b"\n".join(parts) + b"\n"
        page.Contents = self.pdf.make_indirect(pikepdf.Stream(self.pdf, content))

    def save(self, path: Path) -> None:
        self.pdf.save(path)


@dataclass
class PacketSpec:
    tag: str
    name_text: str
    teacher_text: str
    group_text: str
    n_pages: int
    expected_sid: str | None  # correct final answer, after human review. None means: should end up unmatched.
    date_text: str = "10/03/2025"
    period_text: str = "02"
    shade_blank_rows: bool = False
    # Whether match.py's fully-automatic assignment (no human input) should
    # reach expected_sid on its own. False for packets whose OCR noise is
    # real but too heavy to clear the auto-assign score threshold on its
    # own -- match.py should still surface the right candidate for a human
    # to approve/correct, it just shouldn't self-approve. See the
    # ocr_garbled_name packets: real garble quoted in the build spec
    # ("D iya Chama" vs "Divyasree Chama") scores 77 with
    # min(WRatio, token_sort_ratio), below the 82 auto-assign floor, even
    # though it's unambiguously the right candidate. Auto-assign and
    # "correct top candidate for review" are different bars; only the
    # former is gated by MIN_SCORE/MIN_MARGIN.
    auto_assign_expected: bool = True


@dataclass
class FixtureResult:
    pdf_path: Path
    roster_path: Path
    # Ground truth for end-to-end tests (pipeline + review + verify): the
    # correct SID after a human has reviewed every packet.
    expected_final_sid: dict[str, str | None] = field(default_factory=dict)
    # Ground truth for match.py's assignment algorithm run with zero human
    # input. Differs from expected_final_sid only where auto_assign_expected
    # is False on the packet spec.
    expected_auto_assign_sid: dict[str, str | None] = field(default_factory=dict)
    roster_sids_with_no_packet: list[str] = field(default_factory=list)
    packet_page_counts: dict[str, int] = field(default_factory=dict)
    packet_tags_in_order: list[str] = field(default_factory=list)


TEACHER_CODE = "020415"
PERIOD = "02"


def _sid(index: int) -> str:
    return f"{TEACHER_CODE}{PERIOD}{index:02d}"


ROSTER = [
    (_sid(1), "Ames", "Jordan"),
    (_sid(2), "Chandra", "Priya"),
    (_sid(3), "Shaw", "Casey"),
    (_sid(4), "Lee", "Morgan"),
    (_sid(5), "Shaikh", "Nadia"),
    (_sid(6), "Kim", "Taylor"),
    (_sid(7), "Rivera", "Alex"),
    (_sid(8), "Patel", "Sam"),
    (_sid(9), "Chen", "Jamie"),
    (_sid(10), "Nguyen", "Drew"),
    (_sid(11), "Diaz", "Emerson"),
    (_sid(12), "Shah", "Reese"),
]

# Roster entries 08-12 deliberately have no packet in the fixture, to
# exercise "roster entry with no packet" reporting. 07 (Alex Rivera) is
# used by below_threshold_correct_candidate below.
ROSTER_NO_PACKET = [_sid(i) for i in range(8, 13)]

PACKETS = [
    PacketSpec(
        tag="clean_match",
        name_text="Jordan Ames",
        teacher_text="Hannel",
        group_text="none",
        n_pages=2,
        expected_sid=_sid(1),
    ),
    PacketSpec(
        tag="ocr_garbled_name",
        # Real OCR measured off the actual file stacks multiple error types
        # at once: "Divyasree Chama" -> "D iya Chama" (dropped letters, split
        # word), "Genton Shaw" -> "Ginton shaw" (vowel substitution, case
        # lost). True name is "Priya Chandra": drop letters + split word in
        # the first name, substitute + lowercase the surname.
        name_text="P iya chondra",
        teacher_text="Hannel",
        group_text="none",
        n_pages=2,
        expected_sid=_sid(2),
        # Scores 77 with min(WRatio, token_sort_ratio) on the plain "first
        # last" string -- below MIN_SCORE, correctly abstains under that
        # scorer. But match.py's actual scorer (max over 4 name-order
        # variants, per calibration against the real file) recovers this to
        # 84.6/margin 17.1, clearing the bar. Left auto_assign_expected at
        # its True default to match what match.py actually does; the
        # weaker-scorer non-clearing case is documented here, not asserted.
    ),
    PacketSpec(
        tag="below_threshold_correct_candidate",
        # Heavier garble than ocr_garbled_name, calibrated (via
        # score_pair against this exact roster) to land at 80.0/margin 28.6
        # with match.py's actual scorer -- below MIN_SCORE=82 despite a
        # clean margin. Exercises the propose/auto-assign split end to end:
        # the top candidate must still be correct, but assign_all must
        # leave it unmatched for a human to approve.
        name_text="A ex Rvra",
        teacher_text="Hannel",
        group_text="none",
        n_pages=2,
        expected_sid=_sid(7),
        auto_assign_expected=False,
    ),
    PacketSpec(
        tag="group_row_trap",
        # Group row names a different roster student (Nadia Shaikh). Must
        # not let that student win this packet.
        name_text="Casey Shaw",
        teacher_text="Hannel",
        group_text="Nadia",
        n_pages=2,
        expected_sid=_sid(3),
    ),
    PacketSpec(
        tag="blank_rows_leak",
        # Teacher/Group left blank with a shaded band across them: the
        # redaction floor must still cover the full bordered band, not just
        # down to where handwriting happens to stop.
        name_text="Morgan Lee",
        teacher_text="",
        group_text="",
        n_pages=2,
        expected_sid=_sid(4),
        shade_blank_rows=True,
    ),
    PacketSpec(
        tag="illegible_scrawl",
        name_text="S 8",
        teacher_text="Hannel",
        group_text="none",
        n_pages=1,
        expected_sid=None,
    ),
    PacketSpec(
        tag="near_collision_surname",
        name_text="Nadia Shaikh",
        teacher_text="Hannel",
        group_text="none",
        n_pages=2,
        expected_sid=_sid(5),
    ),
    PacketSpec(
        tag="not_on_roster",
        # A real packet exists but this student never consented, so they
        # have no roster entry. Must stay unmatched, not forced onto anyone.
        name_text="Riley Fox",
        teacher_text="Hannel",
        group_text="none",
        n_pages=2,
        expected_sid=None,
    ),
    PacketSpec(
        tag="variable_length",
        # Page count must come from the footer, not a hardcoded constant.
        name_text="Taylor Kim",
        teacher_text="Hannel",
        group_text="none",
        n_pages=3,
        expected_sid=_sid(6),
    ),
]


def _write_roster_csv(path: Path, rows: list[tuple[str, str, str]]) -> None:
    with path.open("w", newline="") as f:
        f.write("SID,Last Name,First Name\n")
        for sid, last, first in rows:
            f.write(f"{sid},{last},{first}\n")


def _build_packets_pdf(
    packets: list[PacketSpec],
    roster_rows: list[tuple[str, str, str]],
    roster_sids_with_no_packet: list[str],
    pdf_path: Path,
    roster_path: Path,
) -> FixtureResult:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    builder = PdfBuilder()

    result = FixtureResult(
        pdf_path=pdf_path,
        roster_path=roster_path,
        roster_sids_with_no_packet=list(roster_sids_with_no_packet),
    )

    for packet in packets:
        result.expected_final_sid[packet.tag] = packet.expected_sid
        result.expected_auto_assign_sid[packet.tag] = (
            packet.expected_sid if packet.auto_assign_expected else None
        )
        result.packet_page_counts[packet.tag] = packet.n_pages
        result.packet_tags_in_order.append(packet.tag)

        for page_num in range(1, packet.n_pages + 1):
            page_marker = f"Page {page_num} of {packet.n_pages}"
            if page_num == 1:
                img = render_header_image(
                    name_text=packet.name_text,
                    teacher_text=packet.teacher_text,
                    group_text=packet.group_text,
                    date_text=packet.date_text,
                    period_text=packet.period_text,
                    worksheet_type=WORKSHEET_TYPE_TEXT,
                    page_marker=page_marker,
                    shade_blank_rows=packet.shade_blank_rows,
                )
                items = [InvisibleText("B. Model Plausibility Ratings", 45, 20)]
                items.append(InvisibleText("Name:", NAME_ANCHOR["x0"], NAME_ANCHOR["top"], 9))
                if packet.name_text:
                    items.append(InvisibleText(packet.name_text, 150, NAME_ANCHOR["top"]))
                # Printed labels are physically on the page regardless of
                # whether the student filled in a value, so OCR reads them
                # either way. Only the handwritten value is conditional.
                items.append(InvisibleText("Teacher:", TEACHER_ANCHOR["x0"], TEACHER_ANCHOR["top"], 9))
                if packet.teacher_text:
                    items.append(InvisibleText(packet.teacher_text, 150, TEACHER_ANCHOR["top"]))
                items.append(InvisibleText("Group members, if any:", GROUP_ANCHOR["x0"], GROUP_ANCHOR["top"], 9))
                if packet.group_text:
                    items.append(InvisibleText(packet.group_text, 150, GROUP_ANCHOR["top"]))
                items.append(InvisibleText("Date:", DATE_ANCHOR["x0"], NAME_ANCHOR["top"], 9))
                items.append(InvisibleText(packet.date_text, 450, NAME_ANCHOR["top"]))
                items.append(InvisibleText("Period:", PERIOD_ANCHOR["x0"], TEACHER_ANCHOR["top"], 9))
                items.append(InvisibleText(packet.period_text, 450, TEACHER_ANCHOR["top"]))
                items.append(InvisibleText(WORKSHEET_TYPE_TEXT, FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9))
                items.append(InvisibleText(page_marker, FOOTER_PAGE_MARKER["x0"], FOOTER_PAGE_MARKER["top"], 9))
            else:
                body = f"({packet.tag} continued, page {page_num})"
                img = render_continuation_image(worksheet_type=WORKSHEET_TYPE_TEXT, page_marker=page_marker, body=body)
                items = [
                    InvisibleText(body, 45, 40),
                    InvisibleText(WORKSHEET_TYPE_TEXT, FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
                    InvisibleText(page_marker, FOOTER_PAGE_MARKER["x0"], FOOTER_PAGE_MARKER["top"], 9),
                ]
            builder.add_page(img, items)

    builder.save(result.pdf_path)
    _write_roster_csv(result.roster_path, roster_rows)
    return result


def build_main_fixture(out_dir: Path) -> FixtureResult:
    return _build_packets_pdf(
        PACKETS,
        ROSTER,
        ROSTER_NO_PACKET,
        out_dir / "packets.pdf",
        out_dir / "roster.csv",
    )


# --- Packet-heavy variant: 14 packets against a 6-name roster ---
#
# The real files run ~22 packets against ~12 roster entries (roughly half
# unmatched), not the other way around. This matters specifically because it
# stresses the "each roster entry claimed at most once" invariant: as the
# pool of unclaimed entries shrinks, a merely eager matcher starts forcing
# marginal candidates onto whatever's left. Includes decoys built to be
# close-but-wrong for a roster entry that a different packet legitimately
# claims, so a matcher that doesn't hold the line loses this data.

HEAVY_ROSTER = ROSTER[:6]
HEAVY_ROSTER_NO_PACKET: list[str] = []  # every entry here has a real packet

HEAVY_PACKETS = [
    PacketSpec("heavy_clean_match", "Jordan Ames", "Hannel", "none", 2, _sid(1)),
    PacketSpec("heavy_ocr_garbled_name", "P iya chondra", "Hannel", "none", 2, _sid(2)),
    PacketSpec("heavy_group_row_trap", "Casey Shaw", "Hannel", "Nadia", 2, _sid(3)),
    PacketSpec("heavy_blank_rows_leak", "Morgan Lee", "", "", 2, _sid(4), shade_blank_rows=True),
    PacketSpec("heavy_near_collision_surname", "Nadia Shaikh", "Hannel", "none", 2, _sid(5)),
    PacketSpec("heavy_variable_length", "Taylor Kim", "Hannel", "none", 3, _sid(6)),
    PacketSpec("heavy_illegible_1", "S 8", "Hannel", "none", 1, None),
    PacketSpec("heavy_illegible_2", "M 3", "Hannel", "none", 1, None),
    PacketSpec("heavy_not_on_roster_1", "Riley Fox", "Hannel", "none", 2, None),
    PacketSpec("heavy_not_on_roster_2", "Alex Rivera", "Hannel", "none", 2, None),
    # Decoy: close to Jordan Ames' first name, wrong surname. Roster 01 is
    # already legitimately claimed by heavy_clean_match; this must not
    # bump that claim or get placed elsewhere.
    PacketSpec("heavy_decoy_jordan_james", "Jordan James", "Hannel", "none", 2, None),
    # Decoy: phonetically close to "Morgan Lee" but a different student.
    # Constructed to score lower than the real Morgan Lee packet, so the
    # correct pairing wins the claim and this one is left to abstain.
    PacketSpec("heavy_decoy_morgan_leigh", "Morgan Leigh", "Hannel", "none", 2, None),
    # Decoy: first name matches roster 03 (Casey Shaw), surname matches
    # roster 05 (Nadia Shaikh) -- a genuine blend that should score
    # mediocre against both full names and clear neither threshold.
    PacketSpec("heavy_decoy_composite", "Casey Shaikh", "Hannel", "none", 2, None),
    # Decoy: close to "Casey Shaw" but not a roster name at all.
    PacketSpec("heavy_decoy_casey_shah", "Casey Shah", "Hannel", "none", 2, None),
]


def build_packet_heavy_fixture(out_dir: Path) -> FixtureResult:
    return _build_packets_pdf(
        HEAVY_PACKETS,
        HEAVY_ROSTER,
        HEAVY_ROSTER_NO_PACKET,
        out_dir / "packets_heavy.pdf",
        out_dir / "roster_heavy.csv",
    )


def build_footer_edge_case_fixture(out_dir: Path) -> Path:
    """Isolated 4-page fixture for segmentation edge cases: a normal packet,
    a packet missing its first page, and an unreadable footer."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / "footer_edge_cases.pdf"
    builder = PdfBuilder()

    def header_page(name: str, marker: str):
        img = render_header_image(
            name_text=name,
            teacher_text="Hannel",
            group_text="none",
            date_text="10/03/2025",
            period_text="02",
            worksheet_type=WORKSHEET_TYPE_TEXT,
            page_marker=marker,
            shade_blank_rows=False,
        )
        items = [
            InvisibleText("Name:", NAME_ANCHOR["x0"], NAME_ANCHOR["top"], 9),
            InvisibleText(name, 150, NAME_ANCHOR["top"]),
            InvisibleText(WORKSHEET_TYPE_TEXT, FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9),
        ]
        if marker:
            items.append(InvisibleText(marker, FOOTER_PAGE_MARKER["x0"], FOOTER_PAGE_MARKER["top"], 9))
        return img, items

    def continuation_page(marker: str):
        img = render_continuation_image(worksheet_type=WORKSHEET_TYPE_TEXT, page_marker=marker)
        items = [InvisibleText(WORKSHEET_TYPE_TEXT, FOOTER_WORKSHEET_TYPE["x0"], FOOTER_WORKSHEET_TYPE["top"], 9)]
        if marker:
            items.append(InvisibleText(marker, FOOTER_PAGE_MARKER["x0"], FOOTER_PAGE_MARKER["top"], 9))
        return img, items

    # Packet A: normal, page 1 of 2 then page 2 of 2 (control case).
    builder.add_page(*header_page("Edge Case One", "Page 1 of 2"))
    builder.add_page(*continuation_page("Page 2 of 2"))

    # Packet B: missing page 1 -- first physical page for this packet has
    # no header block and its footer starts at "Page 2 of 2".
    builder.add_page(*continuation_page("Page 2 of 2"))

    # Packet C: header page present but footer page marker is unreadable
    # (no page marker text drawn or extracted at all).
    builder.add_page(*header_page("Edge Case Three", ""))

    builder.save(pdf_path)
    return pdf_path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/melredact_fixture")
    main = build_main_fixture(out)
    heavy = build_packet_heavy_fixture(out)
    edge = build_footer_edge_case_fixture(out)
    print(f"main fixture: {main.pdf_path} ({sum(main.packet_page_counts.values())} pages)")
    print(f"roster: {main.roster_path}")
    print(f"expected final sid: {main.expected_final_sid}")
    print(f"expected auto-assign sid: {main.expected_auto_assign_sid}")
    print(f"roster with no packet: {main.roster_sids_with_no_packet}")
    print(f"heavy fixture: {heavy.pdf_path} ({sum(heavy.packet_page_counts.values())} pages)")
    print(f"heavy roster: {heavy.roster_path}")
    print(f"heavy expected final sid: {heavy.expected_final_sid}")
    print(f"heavy expected auto-assign sid: {heavy.expected_auto_assign_sid}")
    print(f"footer edge cases: {edge}")
