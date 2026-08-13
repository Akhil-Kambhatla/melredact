"""Read-only diagnostic for a leak class redact_packet does not address at
all: `redact.redact_packet` only ever draws a redaction box on a packet's
own `header_page_index` (see redact.py's `add_page`/`redact_packet` -- the
`is_header` check gates every `_draw_redaction_box` call). Every other page
in the packet -- for the real PRT/MPR worksheets, always exactly one more
page, page 2 -- is written through to output completely untouched: full
raster, full kept OCR text layer, no redaction attempted at all.

Motivating case: packet p026 of `data/PRT/010406_PD1_PRT.pdf`. Its page 2
carries a handwritten name ("Brian Lu") and date in the blank top margin,
above the printed "A. Plausibility Ranking Task" section title -- no
printed "Name:"/"Date:" label anywhere on the page, no drawn box. This was
only ever caught because `verify_no_leaked_names` (which reads every page
of the finished file, not just the header page) happened to OCR "brian"
cleanly and it happened to exact-match a roster token. That is a lucky
catch, not a structural guarantee: `verify_no_leaked_names` only fires when
OCR reads the handwriting AND it lands on an exact or fuzzy roster-token
hit (see redact.py's LEAK_FUZZY_MIN_RATIO/LEAK_FUZZY_MIN_TOKEN_LEN) -- it
systematically undercounts real exposure, since illegible ink or a name
whose OCR garble misses the fuzzy threshold produces no finding at all even
though the page is exactly as exposed to a human reader as p026's was.

This script answers three questions without writing to out/, redacting
anything, or deleting anything:

1. Is page-2 ink inside a printed template field (a "Name:"/"Date:" label,
   or a drawn box) the way page-1 ink is, or is it freehand at whatever
   position a student happened to write? Answered by running the *same*
   label search (`segment.locate_header_anchors`) and border detection
   (`redact.detect_header_band`) page 1's own redaction logic depends on,
   against page 2 -- not a new, separate heuristic.
2. Ink presence, not name recognition: a pixel dark-fraction scan (the same
   BORDER_DARK_THRESHOLD-style mechanism `detect_header_band` already uses
   to find a printed rule) over the region where p026's own name sits,
   applied to every PRT packet's page 2 in both real files -- independent
   of whether OCR can read anything there at all.
3. Whether any file already shipped to `out/` for teacher 020415 (MPR or
   PRT -- both worksheet types are 2-page packets, see segment_pdf) has
   page-2 ink in that same region that `verify_no_leaked_names` did not
   flag.

Every redacted/rendered artifact this script produces goes under
`out/.diagnostics/`, already fully gitignored (the crops render real
handwritten student ink) -- nothing is written to `out/`'s real tree,
`decisions/`, or the ledger, and nothing already in `out/` is modified or
deleted.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from melredact.config import BORDER_DARK_THRESHOLD, HEADER_SEARCH_MAX_TOP, RENDER_DPI_FINAL
from melredact.pdfio import open_pdf
from melredact.pipeline import packet_tag
from melredact.redact import HeaderBand, detect_header_band, verify_no_leaked_names
from melredact.roster import RosterError, load_full_roster
from melredact.segment import Packet, SegmentResult, locate_header_anchors, page_words, segment_pdf

# Measured directly off packet p026's own page 2 (data/PRT/010406_PD1_PRT.pdf,
# physical page index 27): OCR found "Brian" at (x0=36.0, top=11.28, x1=96.72,
# bottom=48.72) and "Lu" at (x0=123.12, top=15.12, x1=138.72, bottom=38.64).
# Left/right (15 to 250pt) is generous horizontal margin around Brian/Lu's
# own x0/x1 (36.0-138.72), comfortably short of the date field (x0=484.56 on
# this same page) on the right, so a name written a bit wider or positioned a
# bit differently on a different student's page 2 still falls inside it
# without also picking up the date.
NAME_REGION_X0 = 15.0
NAME_REGION_X1 = 250.0

# The bottom bound is deliberately NOT a fixed page-point constant, the same
# lesson this codebase already learned the hard way for the header border
# and row-assignment windows (see config.py's ROW_ASSIGNMENT_BOTTOM_SLACK_PT/
# GROUP_ROW_BAND_SLACK_PT commentary): a first attempt here used a fixed
# bottom=42pt (measured off p026's own page alone), and it produced false
# "ink" hits on four other real packets (p064/p068/p070/p076) whose scans
# are skewed enough that the printed section title -- literally identical
# across every packet in both real PRT files, "A Plausibility Ranking Task
# Carefully read the following..." -- itself starts as high as top=35.04pt,
# well inside that fixed cutoff, versus 44.64pt on p026's own page. A
# region reaching that far down registers "ink" on a genuinely blank page 2
# just from the printed title, not handwriting. Anchored instead to this
# page's own OCR-located position of a distinctive, always-present title
# word ("plausibility") -- confirmed identical on every sampled page across
# both real PRT files, so it's a reliable per-page anchor -- with a small
# fixed slack above it (TITLE_ANCHOR_SLACK_PT), the same anchor-relative
# fix already applied elsewhere in this codebase to the header border and
# row-assignment windows.
TITLE_ANCHOR_WORD = "plausibil"  # substring match: OCR may read "Plausibility" or "plausibility"
TITLE_ANCHOR_SLACK_PT = 3.0
# Used only when the title anchor can't be found on a given page at all
# (OCR miss on printed text -- rare, but not impossible): falls back to the
# lowest title_top actually observed across the real dataset (p064:
# 35.04pt) minus the same slack, the most conservative (smallest) region
# bottom seen, rather than risk reaching into a title positioned even
# higher on some unobserved page.
TITLE_ANCHOR_FALLBACK_TOP_PT = 35.04

# Fraction of dark pixels within the name region above which it counts as
# carrying ink. Calibrated directly: p026's own (anchor-relative) region
# scored 0.0266; blank real page 2s sampled across both files scored well
# under 0.01 once the title-anchor fix above removed the false hits it was
# otherwise picking up -- comfortably separated by an order of magnitude.
INK_PRESENCE_FRACTION = 0.01


def _title_anchor_top(pdf_path: Path, page2_index: int) -> tuple[float, bool]:
    """This page's own OCR-located top of the printed section title -- see
    the module-level comment above NAME_REGION_X0 for why this replaces a
    fixed page-point cutoff. Returns (top, found)."""
    with open_pdf(pdf_path) as pdf:
        page2 = pdf.pages[page2_index]
        words = page_words(page2, (0, 0, page2.width, HEADER_SEARCH_MAX_TOP))
    candidates = [w for w in words if w["text"].lower().startswith(TITLE_ANCHOR_WORD)]
    if not candidates:
        return TITLE_ANCHOR_FALLBACK_TOP_PT - TITLE_ANCHOR_SLACK_PT, False
    return min(w["top"] for w in candidates), True


def _name_region_for_page(pdf_path: Path, page2_index: int) -> tuple[float, float, float, float]:
    title_top, found = _title_anchor_top(pdf_path, page2_index)
    bottom = title_top if not found else title_top - TITLE_ANCHOR_SLACK_PT
    return NAME_REGION_X0, 0.0, NAME_REGION_X1, bottom


@dataclass
class Page2InkResult:
    packet_tag: str
    ink_present: bool
    dark_fraction: float
    name_region: tuple
    title_anchor_found: bool
    crop_path: str | None = None


def _dark_fraction(image: Image.Image, bbox_pt: tuple[float, float, float, float], dpi: int) -> float:
    scale = dpi / 72.0
    left, top, right, bottom = (max(0, v * scale) for v in bbox_pt)
    gray = np.asarray(image.convert("L"))
    h, w = gray.shape
    region = gray[int(top) : min(h, int(bottom)), int(left) : min(w, int(right))]
    if region.size == 0:
        return 0.0
    return float((region < BORDER_DARK_THRESHOLD).mean())


def _render_page2(pdf_path: Path, page2_index: int, dpi: int) -> Image.Image:
    with open_pdf(pdf_path) as pdf:
        page = pdf.pages[page2_index]
        return page.to_image(resolution=dpi).original.convert("RGB")


def _annotate_and_save(image: Image.Image, dpi: int, region: tuple, out_path: Path) -> None:
    scale = dpi / 72.0
    annotated = image.copy()
    draw = ImageDraw.Draw(annotated)
    left, top, right, bottom = (v * scale for v in region)
    draw.rectangle([left, top, right, bottom], outline=(255, 0, 0), width=4)
    crop_bottom_px = min(annotated.height, int((HEADER_SEARCH_MAX_TOP + 40) * scale))
    annotated.crop((0, 0, annotated.width, crop_bottom_px)).save(out_path)


def template_field_check(pdf_path: Path, packet: Packet, dpi: int) -> dict:
    """Runs page-1's own label search + border detection against page 2,
    unmodified -- if page 2 really had a printed Name field the way page 1
    does, this would find it exactly the same way redact_packet finds page
    1's. Returns the raw evidence, not just a verdict, so a human can see
    why."""
    page2_index = packet.page_indices[1]
    with open_pdf(pdf_path) as pdf:
        page2 = pdf.pages[page2_index]
        words = page_words(page2, (0, 0, page2.width, HEADER_SEARCH_MAX_TOP))
    anchors = locate_header_anchors(words)
    image = _render_page2(pdf_path, page2_index, dpi)
    band = detect_header_band(image, dpi=dpi, anchors=anchors)
    return {
        "name_label_found": anchors.name_found,
        "teacher_label_found": anchors.teacher_found,
        "group_label_found": anchors.group_found,
        "border_detected": band.detected,
        "border_band": asdict(band) if isinstance(band, HeaderBand) else None,
    }


def scan_ink_presence(
    pdf_path: Path, segmented: SegmentResult, dpi: int, crop_dir: Path | None, crop_tags: set[str]
) -> list[Page2InkResult]:
    results = []
    for packet in segmented.packets:
        if packet.n_pages < 2:
            continue
        tag = packet_tag(pdf_path, packet)
        page2_index = packet.page_indices[1]
        region = _name_region_for_page(pdf_path, page2_index)
        title_top, title_found = _title_anchor_top(pdf_path, page2_index)
        image = _render_page2(pdf_path, page2_index, dpi)
        frac = _dark_fraction(image, region, dpi)
        present = frac >= INK_PRESENCE_FRACTION
        crop_path = None
        if crop_dir is not None and tag in crop_tags:
            crop_dir.mkdir(parents=True, exist_ok=True)
            full_path = crop_dir / f"{tag}_page2_full.png"
            image.save(full_path)
            crop_path = crop_dir / f"{tag}_page2_namefield.png"
            _annotate_and_save(image, dpi, region, crop_path)
            crop_path = str(crop_path)
        results.append(
            Page2InkResult(
                packet_tag=tag,
                ink_present=present,
                dark_fraction=round(frac, 5),
                name_region=tuple(round(v, 2) for v in region),
                title_anchor_found=title_found,
                crop_path=crop_path,
            )
        )
    return results


@dataclass
class ShippedExposure:
    out_path: str
    ink_present: bool
    dark_fraction: float
    verify_findings_on_page2: list


def check_shipped_output(out_dir: Path, roster) -> list[ShippedExposure]:
    """For every finished file already sitting in out_dir (real, already-
    reviewed/approved output -- not a diagnostic draft), render page 2 (page
    index 1, the only other page a 2-page packet has), check ink presence in
    NAME_REGION, and independently re-run verify_no_leaked_names against
    just that page. A file with ink_present=True and an empty
    verify_findings_on_page2 is exposed and was not caught."""
    results = []
    pdfs = sorted(p for p in out_dir.rglob("*.pdf") if not any(part.startswith(".") for part in p.relative_to(out_dir).parts))
    for pdf_path in pdfs:
        with open_pdf(pdf_path) as pdf:
            if len(pdf.pages) < 2:
                continue
            page2 = pdf.pages[1]
            image = page2.to_image(resolution=RENDER_DPI_FINAL).original.convert("RGB")
        region = _name_region_for_page(pdf_path, 1)
        frac = _dark_fraction(image, region, RENDER_DPI_FINAL)
        present = frac >= INK_PRESENCE_FRACTION
        all_findings = verify_no_leaked_names(pdf_path, roster)
        page2_findings = [asdict(f) for f in all_findings if f.page_index == 1]
        results.append(
            ShippedExposure(
                out_path=str(pdf_path),
                ink_present=present,
                dark_fraction=round(frac, 5),
                verify_findings_on_page2=page2_findings,
            )
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-diagnostics", default="out/.diagnostics/page2_exposure")
    parser.add_argument("--out", default="out", help="the real out/ tree to check for already-shipped 020415 files")
    parser.add_argument(
        "--roster-020415",
        default="data/teacher_codes/Teacher Codes_Student Codes_Demographics - 020415.csv",
    )
    args = parser.parse_args()

    diag_dir = Path(args.out_diagnostics)
    diag_dir.mkdir(parents=True, exist_ok=True)
    dpi = RENDER_DPI_FINAL

    prt_files = [Path("data/PRT/010406_PD1_PRT.pdf"), Path("data/PRT/Hannel PRT PD2.pdf")]

    summary: dict = {
        "name_region_x0": NAME_REGION_X0,
        "name_region_x1": NAME_REGION_X1,
        "title_anchor_slack_pt": TITLE_ANCHOR_SLACK_PT,
        "files": {},
    }

    sample_tags_by_file = {
        "010406_PD1_PRT.pdf": {
            "010406_PD1_PRT_p026",
            "010406_PD1_PRT_p000",
            "010406_PD1_PRT_p002",
            "010406_PD1_PRT_p004",
            "010406_PD1_PRT_p012",
            "010406_PD1_PRT_p018",
            "010406_PD1_PRT_p020",
            "010406_PD1_PRT_p024",
            "010406_PD1_PRT_p028",
            "010406_PD1_PRT_p040",
            "010406_PD1_PRT_p060",
            "010406_PD1_PRT_p090",
        },
        "Hannel PRT PD2.pdf": {
            "Hannel PRT PD2_p002",
            "Hannel PRT PD2_p008",
            "Hannel PRT PD2_p016",
            "Hannel PRT PD2_p020",
            "Hannel PRT PD2_p022",
            "Hannel PRT PD2_p024",
            "Hannel PRT PD2_p028",
            "Hannel PRT PD2_p032",
            "Hannel PRT PD2_p036",
            "Hannel PRT PD2_p038",
        },
    }

    for pdf_path in prt_files:
        if not pdf_path.exists():
            print(f"warning: {pdf_path} not found, skipping", file=sys.stderr)
            continue
        segmented = segment_pdf(pdf_path)
        non_orphan = [p for p in segmented.packets if p.n_pages >= 2]
        p026 = next((p for p in non_orphan if packet_tag(pdf_path, p) == "010406_PD1_PRT_p026"), None)
        template_check = None
        if p026 is not None:
            template_check = template_field_check(pdf_path, p026, dpi)
        elif non_orphan:
            template_check = template_field_check(pdf_path, non_orphan[0], dpi)

        crop_dir = diag_dir / pdf_path.stem
        crop_tags = sample_tags_by_file.get(pdf_path.name, set())
        ink_results = scan_ink_presence(pdf_path, segmented, dpi, crop_dir, crop_tags)

        n_ink = sum(1 for r in ink_results if r.ink_present)
        print(f"{pdf_path.name}: {len(ink_results)} page-2(s) scanned, {n_ink} with ink present in the name region")
        for r in ink_results:
            if r.ink_present:
                print(f"  INK: {r.packet_tag}  dark_fraction={r.dark_fraction}")

        summary["files"][pdf_path.name] = {
            "template_field_check_reference_packet": template_check,
            "n_page2_scanned": len(ink_results),
            "n_ink_present": n_ink,
            "results": [asdict(r) for r in ink_results],
        }

    print()
    out_dir = Path(args.out)
    roster_020415_path = Path(args.roster_020415)
    if roster_020415_path.exists():
        try:
            roster = load_full_roster(roster_020415_path)
        except RosterError as exc:
            print(f"error loading 020415 roster: {exc}", file=sys.stderr)
            return 1
        shipped = check_shipped_output(out_dir / "020415", roster)
        n_exposed_uncaught = sum(1 for s in shipped if s.ink_present and not s.verify_findings_on_page2)
        print(f"Shipped 020415 output: {len(shipped)} file(s) checked, {n_exposed_uncaught} with page-2 ink NOT caught by verify_no_leaked_names")
        for s in shipped:
            flag = ""
            if s.ink_present and not s.verify_findings_on_page2:
                flag = "  <-- EXPOSED, UNCAUGHT"
            elif s.ink_present and s.verify_findings_on_page2:
                flag = "  (ink present, but verify already caught it)"
            print(f"  {s.out_path}  ink_present={s.ink_present} dark_fraction={s.dark_fraction}{flag}")
        summary["shipped_020415"] = [asdict(s) for s in shipped]
    else:
        print(f"warning: {roster_020415_path} not found, skipping shipped-output check", file=sys.stderr)

    summary_path = diag_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nfull summary written to {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
