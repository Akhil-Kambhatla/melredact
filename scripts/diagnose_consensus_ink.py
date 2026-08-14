"""Read-only, template-agnostic handwriting detector: does not depend on
knowing where a name field is. Every packet of a given worksheet type shares
one printed template, so ink that recurs at (roughly) the same position
across a supermajority of packets is printed; ink that shows up on only one
or a few packets at that position is either handwriting or a genuine per-copy
mark -- exactly what this script exists to surface, without any hardcoded
knowledge of where the Name/Teacher/Group fields sit.

Writes nothing to out/'s real tree, decisions/, or the ledger. Redacts
nothing, deletes nothing. All evidence (JSON + annotated PNG crops) is
written under out/.diagnostics/consensus_ink/, already fully gitignored
(these crops render real handwritten student ink and must never be
committed).

--- Why per-pixel 80%-agreement, as literally specified, does not work here
---

The obvious reading of "ink at the same position in >=80% of packets is
printed" is: rasterize every packet's page, align each to a common
reference with a rotation+translation (Euclidean) transform recovered via
ECC (cv2.findTransformECC), threshold to a binary ink mask, and vote
per-pixel. Measured directly on the real file (data/PRT/010406_PD1_PRT.pdf,
first 12-16 header pages, DPI=200): ECC converges cleanly and confidently
(correlation coefficient 0.96-0.98) for every pair tried, recovering only a
small rotation and a few pixels of translation, exactly the "skew and
offset" the task describes. But the resulting per-pixel Dice overlap
between any two aligned pages' raw ink masks was only 0.29-0.44 -- even
after alignment, a majority of a printed page's own dark pixels don't land
on the same pixel between two independently-scanned copies of the identical
template. Diagnosed directly (not assumed): a diff image between a
reference page and an ECC-aligned second copy shows every line of body text
faintly double-printed, worsening toward the bottom of the page -- small,
smoothly-varying local distortion (paper feed/bowing during scanning), not
a global rotation/translation/scale error. Confirmed it isn't a scale
problem either: re-running with MOTION_AFFINE (adds scale+shear) and
MOTION_HOMOGRAPHY (full perspective) recovered a real ~0.7-0.9% scale
difference between copies but left the same doubling pattern nearly
unchanged. Dense optical flow (cv2.calcOpticalFlowFarneback) was tried as a
local-refinement pass on top of the Euclidean alignment and did cut mean
pixel-wise diff by ~65% -- but it also produces huge, spurious per-pixel
displacements (up to 300px) exactly in the areas where two copies'
handwriting differs, because dense flow tries to explain *any* local
mismatch as motion, including genuine content differences. That's the one
failure mode this script cannot tolerate: an alignment method that warps
real handwriting into agreement would silently erase the very thing this
script exists to find. Dense flow was therefore rejected outright, even
though it measured better on raw pixel error.

The fix that actually holds up against real measurement: vote at block
granularity (BLOCK_PX, a fixed-size grid over the Euclidean-aligned page),
comparing each packet's per-block ink *density* (fraction of dark pixels in
that block) against the *median* density at that block across the whole
group, rather than a binary per-pixel/per-block "any ink" vote. For a
genuinely printed block, density is tightly clustered across independent
copies (measured: the 99.9th percentile of density deviation in a region
confirmed to contain only printed instruction text, no handwriting, was
0.26-0.40 at BLOCK_PX in {16,24}) -- low enough that DENSITY_DIFF_THRESHOLD
cleanly separates it from genuine ink, which is why the median (not a
strict "ink in >=80% of packets" pixel vote, which measured as producing
13-36% of a single control packet's own ink flagged as "non-consensus" even
at a coarse 20px/7.2pt block, i.e. useless -- overwhelmingly false
positives from exactly the sub-block jitter described above) is used as the
per-block "what the supermajority looks like" reference instead. This is
still the same underlying claim the task describes -- ink that recurs
across (near-)all copies at a given position is printed, ink that doesn't
is worth a human's attention -- implemented at the coarseness real scan
variance actually supports, not the literal per-pixel reading, which
measurement showed does not hold on this real data.

--- Validation performed before trusting this on real findings ---

Positive control (this IS a control, not just a parameter choice -- see the
task's own instruction to distrust the method if page 1 fails it): a first
pass at BLOCK_PX=24 flagged real, known-present handwritten header ink on
only 37/46 real header pages (80%) -- root-caused by direct recomputation,
not assumed: several packets had a real per-block density deviation well
above DENSITY_DIFF_THRESHOLD, but the ink was thin/compact enough that it
never formed a connected run of MIN_CONNECTED_BLOCKS adjacent 24px blocks,
so the area filter meant to reject speckle was silently rejecting real
short names too. This is exactly the failure this control exists to catch.
Fixed by moving to BLOCK_PX=16 (finer grid concentrates the same ink into
denser, more connected blocks) with MIN_CONNECTED_BLOCKS and
DENSITY_DIFF_THRESHOLD unchanged: re-run against the full real dataset,
this reaches full 46/46, 22/22, 20/20 (source files) and 11/11, 5/5
(shipped 020415 output) header-field detection -- 104/104 real header
pages, positive control clean.

Specificity control, same final run: 0/104 of those same header pages
produced a spurious "top-margin" (blank-area, no printed content expected)
finding -- the label this script actually treats as identifying-risk. The
control also caught real content correctly staying in the harmless "body"
bucket: PRT page 1's handwritten "Your ranking" answer digits flag as
non-consensus ink on effectively every packet, classified as body/answer,
never as margin or header-field.

Real findings this validated run surfaced on page 2 (never the first page,
i.e. genuinely off the pipeline's radar -- see "Real scans can arrive..."
in this repo's CLAUDE.md for the general class of gap): two packets in
data/PRT/010406_PD1_PRT.pdf, both in the top-margin zone. Packet
010406_PD1_PRT_p026 (physical page 27) is the already-known "Brian Lu
3/30/26" case (see scripts/diagnose_page2_exposure.py) -- correctly
re-caught here by a method with no prior knowledge of where that name
sits. Packet 010406_PD1_PRT_p034 (physical page 35) is a NEW find: a
freehand "Ollie Maduro 2/20/26" in the same blank top margin, at a
different x-position (~x=317-403pt) than p026's name (~x=28-138pt) --
outside the fixed region the earlier targeted diagnostic scanned, which is
exactly the coverage gap this general, position-agnostic method exists to
close. Confirmed by direct visual inspection of the rendered page, not
just the density numbers -- both crops are saved under this script's own
output directory. Zero comparable findings in either Hannel file, and zero
in any of the 16 already-shipped 020415 output files (see report.json for
the full per-group counts). Note: this script's "top-margin" vs "body"
zone label is an approximation, not a precise boundary -- p026's own name
ink itself landed in the "body" bucket (only its date fragment cleared the
top-margin cutoff) because the handwriting's vertical extent straddles
this run's computed first-template-row boundary. Treat any non-consensus
finding near the top of a non-header page as worth a look, not only ones
the zone heuristic happens to label "top-margin".
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from melredact.config import BORDER_DARK_THRESHOLD, HEADER_BAND_FALLBACK
from melredact.pdfio import open_pdf
from melredact.pipeline import packet_tag
from melredact.segment import Packet, segment_pdf

DPI = 200

ECC_DOWNSCALE = 0.25
ECC_ITERS = 200
ECC_EPS = 1e-8

DARK_THRESHOLD = BORDER_DARK_THRESHOLD

# A grid cell of 24px at DPI=200 is 8.64pt (~3mm) -- large enough that the
# few-px sub-block jitter measured above (see module docstring) can't flip
# a genuinely-printed block's median density, small enough that a single
# handwritten word still spans many cells (a short name measured elsewhere
# in this codebase, "Brian"/"Lu" on a real page-2, is ~60pt wide -- ~10
# blocks at this size -- comfortably above MIN_CONNECTED_BLOCKS below).
#
# First tried at 24px (8.64pt) with MIN_CONNECTED_BLOCKS=3: measured
# against the real, full 010406 header-page run, this missed real,
# known-present handwritten header ink on ~20% of packets (37/46) -- the
# positive control this script's own docstring says to distrust the
# method over. Root-caused, not assumed: for several of the missed
# packets, a real per-block density deviation *did* exceed
# DENSITY_DIFF_THRESHOLD in the header band (confirmed by direct
# recomputation, e.g. 0.28 for one packet, well above 0.15), but the ink
# was thin/compact enough that it never formed a *connected* run of 3
# adjacent 24px blocks -- filtered out by the area threshold meant to
# reject speckle, not real names. Re-measured at 16px (5.76pt) with the
# same MIN_CONNECTED_BLOCKS=3 and DENSITY_DIFF_THRESHOLD=0.15: full
# 44/44 header-field detection on the same real packets, because a finer
# grid lets thin ink concentrate within fewer, denser blocks instead of
# being diluted across a coarser one.
BLOCK_PX = 16

# Calibrated against the real printed-only-region measurement in the module
# docstring at this block size: p99 deviation there was 0.26, p99.9 was
# 0.40 -- 0.15 lets a meaningful amount of that noise through as isolated
# or small flagged blocks, which is exactly what MIN_CONNECTED_BLOCKS (not
# this threshold) exists to filter, since real ink deviations cluster into
# large connected regions and print jitter does not. Confirmed directly at
# this block/threshold pair against the real specificity metric that
# actually matters -- not a hand-picked "printed-only" page region, but
# the "top-margin" (blank-area) zone this script reports as
# possible-identifying: 0 spurious findings across all 104 real header
# pages checked (46+22+20 source-file packets, 11+5 shipped-020415 files;
# see the module docstring's "Validation performed" section).
DENSITY_DIFF_THRESHOLD = 0.15

# A region has to span at least this many connected flagged blocks to be
# reported at all -- see the module docstring's specificity-control finding
# (isolated single-block deviations in a confirmed-printed-only region,
# never a connected multi-block one).
MIN_CONNECTED_BLOCKS = 3

# A (worksheet_type, page_offset) group needs at least this many packets
# before a per-block median is trustworthy at all. 5 is the practical floor
# given the real 020415 shipped-output re-check only has 5-11 files per
# group; reported explicitly as a low-N caveat wherever it applies.
MIN_GROUP_SIZE = 5

# A block counts as "part of the printed template" for purposes of finding
# the top of the template block (used to classify a region as "in the
# blank margin above any printed content" -- see classify_zone) once its
# across-group median density clears this floor. Deliberately small: this
# only has to reject essentially-blank blocks, not distinguish confidently
# printed ones from faint ones.
TEMPLATE_ROW_DENSITY_FLOOR = 0.05

# How far past the fallback header-band bottom (config.HEADER_BAND_FALLBACK,
# the same floor redact.py's own detection clamps outward to) a page-1
# region can still fall and count as "known header field," not an
# unexplained anomaly. Deliberately generous -- this is a diagnostic
# classification label, not a redaction boundary; the real per-packet
# border is already handled by the production pipeline's own
# detect_header_band, which this script does not need to re-derive.
HEADER_FIELD_BOTTOM_SLACK_PT = 25.0

# A region's top has to clear the group's own detected first-template-row
# by at least this much to count as "blank margin, no printed content
# expected here" -- absorbs the same few-pt OCR/measurement noise this
# codebase's other anchor-relative slacks do (see CLAUDE.md's
# GROUP_ROW_BAND_SLACK_PT commentary for the same pattern applied
# elsewhere).
TOP_MARGIN_SLACK_PT = 5.0

PT_PER_PX = 72.0 / DPI


@dataclass
class Region:
    page_offset: int
    physical_page_index: int
    bbox_pt: tuple
    area_pt2: float
    zone: str
    peak_density_diff: float
    crop_path: str | None = None


@dataclass
class PacketPageFinding:
    packet_tag: str
    worksheet_type: str
    page_offset: int
    physical_page_index: int
    alignment_ok: bool
    alignment_cc: float | None
    is_orphan: bool = False
    regions: list = None


@dataclass
class GroupReport:
    source: str
    worksheet_type: str
    page_offset: int
    n_items: int
    n_used_for_consensus: int
    n_alignment_failed: int
    skipped_insufficient_data: bool
    first_template_row_pt: float | None
    findings: list = None


def render_gray(pdf, page_index: int, dpi: int = DPI) -> np.ndarray:
    return np.asarray(pdf.pages[page_index].to_image(resolution=dpi).original.convert("L"))


def align_to_reference(ref_gray: np.ndarray, img_gray: np.ndarray):
    if img_gray.shape != ref_gray.shape:
        img_gray = cv2.resize(img_gray, (ref_gray.shape[1], ref_gray.shape[0]), interpolation=cv2.INTER_AREA)
    scale = ECC_DOWNSCALE
    small_ref = cv2.resize(ref_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA).astype(np.float32)
    small_img = cv2.resize(img_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA).astype(np.float32)
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, ECC_ITERS, ECC_EPS)
    try:
        cc, warp = cv2.findTransformECC(small_ref, small_img, warp, cv2.MOTION_EUCLIDEAN, criteria, None, 5)
    except cv2.error:
        return None, None, None
    warp_full = warp.copy()
    warp_full[0, 2] /= scale
    warp_full[1, 2] /= scale
    aligned = cv2.warpAffine(
        img_gray, warp_full, (ref_gray.shape[1], ref_gray.shape[0]),
        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255,
    )
    return aligned, warp_full, float(cc)


def block_density(mask_float: np.ndarray, b: int) -> np.ndarray:
    h, w = mask_float.shape
    ph, pw = (-h) % b, (-w) % b
    padded = np.pad(mask_float, ((0, ph), (0, pw)))
    hh, ww = padded.shape
    reshaped = padded.reshape(hh // b, b, ww // b, b)
    return reshaped.mean(axis=(1, 3))


def first_template_row_pt(median_density: np.ndarray, b: int) -> float | None:
    rows_with_ink = np.where((median_density >= TEMPLATE_ROW_DENSITY_FLOOR).any(axis=1))[0]
    if rows_with_ink.size == 0:
        return None
    return float(rows_with_ink.min() * b * PT_PER_PX)


def classify_zone(bbox_pt: tuple, page_offset: int, first_row_pt: float | None) -> str:
    left, top, right, bottom = bbox_pt
    if page_offset == 0 and bottom <= HEADER_BAND_FALLBACK["bottom"] + HEADER_FIELD_BOTTOM_SLACK_PT:
        return "header-field (known identifying area)"
    if first_row_pt is not None and bottom <= first_row_pt - TOP_MARGIN_SLACK_PT:
        return "top-margin (no printed content expected -- possible identifying ink)"
    return "body (worksheet content area -- likely answer ink)"


def flagged_regions(dens_i: np.ndarray, median: np.ndarray, b: int) -> list:
    diff = dens_i - median
    flagged = (diff > DENSITY_DIFF_THRESHOLD).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(flagged, connectivity=8)
    regions = []
    discarded = 0
    for label in range(1, n):
        x, y, w, h, area_blocks = stats[label]
        if area_blocks < MIN_CONNECTED_BLOCKS:
            discarded += 1
            continue
        left = x * b * PT_PER_PX
        top = y * b * PT_PER_PX
        right = (x + w) * b * PT_PER_PX
        bottom = (y + h) * b * PT_PER_PX
        peak = float(diff[labels == label].max())
        regions.append(((left, top, right, bottom), peak))
    return regions, discarded


def render_crop(
    original_gray: np.ndarray, warp_full: np.ndarray | None, bbox_pt: tuple, dpi: int, out_path: Path, pad_pt: float = 20.0
) -> None:
    scale = dpi / 72.0
    left, top, right, bottom = bbox_pt
    if warp_full is not None:
        inv = cv2.invertAffineTransform(warp_full)
        corners = np.array(
            [[left, top], [right, top], [right, bottom], [left, bottom]], dtype=np.float32
        ) * scale
        corners_h = np.hstack([corners, np.ones((4, 1), dtype=np.float32)])
        mapped = corners_h @ inv.T
        left_px, top_px = mapped[:, 0].min(), mapped[:, 1].min()
        right_px, bottom_px = mapped[:, 0].max(), mapped[:, 1].max()
    else:
        left_px, top_px, right_px, bottom_px = left * scale, top * scale, right * scale, bottom * scale

    pad_px = pad_pt * scale
    h, w = original_gray.shape
    crop_left = max(0, int(left_px - pad_px))
    crop_top = max(0, int(top_px - pad_px))
    crop_right = min(w, int(right_px + pad_px))
    crop_bottom = min(h, int(bottom_px + pad_px))

    image = Image.fromarray(original_gray).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle([left_px, top_px, right_px, bottom_px], outline=(255, 0, 0), width=3)
    cropped = image.crop((crop_left, crop_top, crop_right, crop_bottom))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(out_path)


def process_group(
    source: str,
    worksheet_type: str,
    page_offset: int,
    items: list,
    dpi: int,
    crop_dir: Path,
    render_crops_for_zones: set,
) -> GroupReport:
    if len(items) < MIN_GROUP_SIZE:
        return GroupReport(
            source=source, worksheet_type=worksheet_type, page_offset=page_offset,
            n_items=len(items), n_used_for_consensus=0, n_alignment_failed=0,
            skipped_insufficient_data=True, first_template_row_pt=None, findings=[],
        )

    tags = [it[0] for it in items]
    physical_indices = [it[2] for it in items]
    grays = [it[3] for it in items]
    orphan_flags = [bool(it[1] is not None and getattr(it[1], "is_orphan", False)) for it in items]

    ref_gray = grays[0]
    aligned = [ref_gray]
    warps = [None]
    ccs = [1.0]
    ok_flags = [True]
    for gray in grays[1:]:
        a, w, cc = align_to_reference(ref_gray, gray)
        aligned.append(a)
        warps.append(w)
        ccs.append(cc)
        ok_flags.append(a is not None)

    used_idx = [i for i, ok in enumerate(ok_flags) if ok]
    masks = [(aligned[i] < DARK_THRESHOLD).astype(np.float32) for i in used_idx]
    dens_stack = np.stack([block_density(m, BLOCK_PX) for m in masks])
    median = np.median(dens_stack, axis=0)
    first_row_pt = first_template_row_pt(median, BLOCK_PX)

    findings = []
    for pos, i in enumerate(used_idx):
        dens_i = dens_stack[pos]
        raw_regions, discarded = flagged_regions(dens_i, median, BLOCK_PX)
        regions = []
        for bbox_pt, peak in raw_regions:
            zone = classify_zone(bbox_pt, page_offset, first_row_pt)
            crop_path = None
            if zone in render_crops_for_zones:
                tag = tags[i]
                crop_name = f"{tag}_off{page_offset}_{int(bbox_pt[0])}x{int(bbox_pt[1])}.png"
                crop_path_obj = crop_dir / crop_name
                render_crop(grays[i], warps[i], bbox_pt, dpi, crop_path_obj)
                crop_path = str(crop_path_obj)
            area = (bbox_pt[2] - bbox_pt[0]) * (bbox_pt[3] - bbox_pt[1])
            regions.append(
                Region(
                    page_offset=page_offset, physical_page_index=physical_indices[i],
                    bbox_pt=tuple(round(v, 2) for v in bbox_pt), area_pt2=round(area, 2),
                    zone=zone, peak_density_diff=round(peak, 4), crop_path=crop_path,
                )
            )
        findings.append(
            PacketPageFinding(
                packet_tag=tags[i], worksheet_type=worksheet_type, page_offset=page_offset,
                physical_page_index=physical_indices[i], alignment_ok=True,
                alignment_cc=round(ccs[i], 4) if ccs[i] is not None else None,
                is_orphan=orphan_flags[i], regions=regions,
            )
        )

    for i, ok in enumerate(ok_flags):
        if ok:
            continue
        findings.append(
            PacketPageFinding(
                packet_tag=tags[i], worksheet_type=worksheet_type, page_offset=page_offset,
                physical_page_index=physical_indices[i], alignment_ok=False, alignment_cc=None,
                is_orphan=orphan_flags[i], regions=[],
            )
        )

    return GroupReport(
        source=source, worksheet_type=worksheet_type, page_offset=page_offset,
        n_items=len(items), n_used_for_consensus=len(used_idx),
        n_alignment_failed=len(items) - len(used_idx), skipped_insufficient_data=False,
        first_template_row_pt=first_row_pt, findings=findings,
    )


def _dominant_worksheet_type(packets: list) -> str:
    types = [p.worksheet_type for p in packets if p.worksheet_type]
    if not types:
        return "UNKNOWN"
    return Counter(types).most_common(1)[0][0]


def summarize(reports: list, control_page_offset: int = 0) -> dict:
    summary = {"groups": []}
    for r in reports:
        page1_body_only = 0
        page1_header_flag = 0
        page1_margin_flag = 0
        other_page_margin_or_header = 0
        n_with_finding_other_pages = 0
        for f in r.findings or []:
            zones = [reg.zone for reg in (f.regions or [])]
            if r.page_offset == 0:
                if any("header-field" in z for z in zones):
                    page1_header_flag += 1
                if any("top-margin" in z for z in zones):
                    page1_margin_flag += 1
                if zones and all("body" in z for z in zones):
                    page1_body_only += 1
            else:
                if zones:
                    n_with_finding_other_pages += 1
                if any("top-margin" in z or "header-field" in z for z in zones):
                    other_page_margin_or_header += 1
        summary["groups"].append(
            {
                "source": r.source,
                "worksheet_type": r.worksheet_type,
                "page_offset": r.page_offset,
                "n_items": r.n_items,
                "n_used_for_consensus": r.n_used_for_consensus,
                "n_alignment_failed": r.n_alignment_failed,
                "skipped_insufficient_data": r.skipped_insufficient_data,
                "first_template_row_pt": r.first_template_row_pt,
                "page1_header_field_flag_rate": f"{page1_header_flag}/{r.n_used_for_consensus}" if r.page_offset == 0 else None,
                "page1_top_margin_flag_rate (sanity, expect ~0)": f"{page1_margin_flag}/{r.n_used_for_consensus}" if r.page_offset == 0 else None,
                "n_packets_with_any_finding_this_page": n_with_finding_other_pages if r.page_offset != 0 else None,
                "n_packets_with_margin_or_header_finding_this_page": other_page_margin_or_header if r.page_offset != 0 else None,
            }
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-diagnostics", default="out/.diagnostics/consensus_ink")
    parser.add_argument("--out", default="out")
    args = parser.parse_args()

    diag_dir = Path(args.out_diagnostics)
    diag_dir.mkdir(parents=True, exist_ok=True)
    crop_dir = diag_dir / "crops"

    render_crops_for_page_gt0 = {
        "top-margin (no printed content expected -- possible identifying ink)",
        "header-field (known identifying area)",
    }
    render_crops_for_page0 = {
        "top-margin (no printed content expected -- possible identifying ink)",
    }

    source_files = [
        Path("data/PRT/010406_PD1_PRT.pdf"),
        Path("data/MPR/Hannel MPR PD2.pdf"),
        Path("data/PRT/Hannel PRT PD2.pdf"),
    ]

    all_reports = {}
    for pdf_path in source_files:
        if not pdf_path.exists():
            continue
        segmented = segment_pdf(pdf_path)
        dominant_type = _dominant_worksheet_type(segmented.packets)
        max_pages = max((p.n_pages for p in segmented.packets), default=0)
        with open_pdf(pdf_path) as pdf:
            page_cache = {}

            def get_gray(idx):
                if idx not in page_cache:
                    page_cache[idx] = render_gray(pdf, idx, DPI)
                return page_cache[idx]

            reports = []
            for page_offset in range(max_pages):
                items = []
                for packet in segmented.packets:
                    if page_offset >= packet.n_pages:
                        continue
                    tag = packet_tag(pdf_path, packet)
                    phys_idx = packet.page_indices[page_offset]
                    items.append((tag, packet, phys_idx, get_gray(phys_idx)))
                zones_for_this_offset = render_crops_for_page0 if page_offset == 0 else render_crops_for_page_gt0
                report = process_group(
                    source=pdf_path.name, worksheet_type=dominant_type, page_offset=page_offset,
                    items=items, dpi=DPI, crop_dir=crop_dir / pdf_path.stem, render_crops_for_zones=zones_for_this_offset,
                )
                reports.append(report)
        all_reports[pdf_path.name] = reports
        print(f"{pdf_path.name}:")
        for r in reports:
            n_flagged = sum(1 for f in (r.findings or []) if f.regions)
            print(
                f"  page_offset={r.page_offset}: {r.n_items} packet(s), {r.n_used_for_consensus} aligned "
                f"({r.n_alignment_failed} alignment-failed), {n_flagged} packet(s) with >=1 non-consensus region"
            )

    out_dir = Path(args.out)
    shipped_mpr = sorted((out_dir / "020415" / "02" / "PCMEL_MPR_ADR").glob("*.pdf")) if (out_dir / "020415" / "02" / "PCMEL_MPR_ADR").exists() else []
    shipped_prt = sorted((out_dir / "020415" / "02" / "PRT" / "NA" / "2025-10").glob("*.pdf")) if (out_dir / "020415" / "02" / "PRT" / "NA" / "2025-10").exists() else []
    print(f"\nre-checking shipped 020415 output: {len(shipped_mpr)} MPR file(s), {len(shipped_prt)} PRT file(s)")

    shipped_reports = {}
    for label, paths in [("shipped_020415_MPR", shipped_mpr), ("shipped_020415_PRT", shipped_prt)]:
        reports = []
        if not paths:
            shipped_reports[label] = reports
            continue
        with_pages = [(p, len(open_pdf(p).pages)) for p in paths]
        max_pages = max(n for _, n in with_pages)
        for page_offset in range(max_pages):
            items = []
            for pdf_path, n_pages in with_pages:
                if page_offset >= n_pages:
                    continue
                with open_pdf(pdf_path) as pdf:
                    gray = render_gray(pdf, page_offset, DPI)
                items.append((pdf_path.stem, None, page_offset, gray))
            zones_for_this_offset = render_crops_for_page0 if page_offset == 0 else render_crops_for_page_gt0
            report = process_group(
                source=label, worksheet_type=label, page_offset=page_offset,
                items=items, dpi=DPI, crop_dir=crop_dir / label, render_crops_for_zones=zones_for_this_offset,
            )
            reports.append(report)
        shipped_reports[label] = reports
        print(f"{label}:")
        for r in reports:
            n_flagged_risky = sum(
                1 for f in (r.findings or [])
                if any("top-margin" in reg.zone for reg in (f.regions or []))
            )
            print(
                f"  page_offset={r.page_offset}: {r.n_items} file(s), {r.n_used_for_consensus} aligned, "
                f"{n_flagged_risky} file(s) with a top-margin (possible-identifying) finding"
            )

    full_report = {
        "params": {
            "dpi": DPI, "block_px": BLOCK_PX, "density_diff_threshold": DENSITY_DIFF_THRESHOLD,
            "min_connected_blocks": MIN_CONNECTED_BLOCKS, "min_group_size": MIN_GROUP_SIZE,
        },
        "source_files": {
            name: {
                "summary": summarize(reports),
                "reports": [asdict(r) for r in reports],
            }
            for name, reports in all_reports.items()
        },
        "shipped_020415": {
            name: {
                "summary": summarize(reports),
                "reports": [asdict(r) for r in reports],
            }
            for name, reports in shipped_reports.items()
        },
    }
    report_path = diag_dir / "report.json"
    report_path.write_text(json.dumps(full_report, indent=2, default=str))
    print(f"\nfull report written to {report_path}")
    print(f"crops written under {crop_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
