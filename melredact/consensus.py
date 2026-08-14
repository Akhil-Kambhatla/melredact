"""Template-agnostic handwriting detector: finds identifying ink without
knowing where any field is, promoted from the read-only diagnostic
scripts/diagnose_consensus_ink.py (kept as a thin caller around this module)
into an unconditional pipeline check.

Every packet of a given worksheet type shares one printed template. Ink
recurring at (roughly) the same position across a supermajority of packets
is printed; ink that shows up on only one or a few packets at that position
is handwriting, or a genuine per-copy mark -- either way, not something a
text-based check can be trusted to catch (see "Why this exists" below).

--- Two passes, not one ---

Pass one, per packet (`flagged_regions`): for a (worksheet_type, page_offset)
group, rasterize every packet's page, align each to a common reference via
ECC, and vote at block granularity -- each packet's own per-block ink
*density* against the group's per-block *median* density, dilated by
CONSENSUS_REGISTRATION_TOLERANCE_BLOCKS before the comparison (see
config.py's own docstring on that constant for the real-data diagnosis: a
sub-block registration difference between independently re-aligned copies,
not per-copy darkness/contrast, was measured as the cause of most false
flags on printed text). A block whose density exceeds the *dilated* median
by more than DENSITY_DIFF_THRESHOLD is flagged; flagged blocks are then
grouped into connected regions and small ones (speckle, residual alignment
jitter) are dropped via MIN_CONNECTED_BLOCKS. This identifies "ink that
isn't part of the shared template, once a modest registration difference is
tolerated" per packet -- see "Why block-median voting, not per-pixel or
optical flow" below for why this specific method, at this specific
granularity, is what real measurement supports.

Pass two, across the whole group (`_zone_params`/`_build_writing_zone`): a
spatial writing-zone mask, not an occurrence-frequency threshold (replaced
2026-08-14 -- see config.py's CONSENSUS_WRITING_ZONE_DILATION_PT docstring
for the real-data reason the original frequency threshold, calibrated on a
single file, did not generalize to free-response worksheets). A block
position is "in the zone" -- an ordinary place the class writes -- when at
least CONSENSUS_WRITING_ZONE_MIN_SHARE *other* packets in the group have
their own flagged ink within CONSENSUS_WRITING_ZONE_DILATION_PT of it,
spatial corroboration rather than exact-position overlap (real students
answering the same prompt rarely land on the identical blocks, so exact
overlap under-counts genuine shared answer areas). A flagged region is held
only when its own footprint has no such corroboration anywhere in it,
regardless of how many packets have *some* unrelated ink *somewhere* on the
page. This is the second replacement of an earlier, page-position-based
heuristic ("is this in the blank top margin, or somewhere content is
expected?") that mislabelled part of a real leak's own name ink as harmless
body content because the handwriting's vertical extent straddled an
approximate margin/body boundary -- position on the page in the abstract was
never the signal that mattered; whether *other packets* independently wrote
nearby is.

The header page is excluded from this check entirely (see
`_build_groups`): its Name/Teacher/Group ink is already destroyed
unconditionally by redact.py's own border-detection-driven redaction,
regardless of whether a match ever succeeds, and every packet's own
handwritten name there is -- correctly -- always a singleton at the
position it lands (no two students write the same name), which would make
every single header page flag under this check for a region the pipeline
already fully covers by construction. This check exists for ink redaction
never reaches at all: continuation pages.

--- Why this exists: a leak class verify_no_leaked_names cannot catch ---

redact_packet only ever redacts the header page. verify_no_leaked_names is
a text-based safety net -- it extracts text from the finished file and
checks it against the roster. Both of those miss the same real case: a
freehand name written in the blank margin of a *continuation* page.
Redaction never reaches it (wrong page entirely), and if the name belongs
to a student who isn't even on the roster, there is no roster token for a
text check to match against -- verify_no_leaked_names would pass vacuously
on a page that is visibly leaking a real name.

Confirmed real, not hypothetical: data/PRT/010406_PD1_PRT.pdf has two such
packets, both in the top margin of page 2. 010406_PD1_PRT_p026 ("Brian Lu",
a name that also happens to be on the roster -- verify_no_leaked_names
*could* have caught this one, if it were ever run against page 2 content,
which it structurally isn't since redaction and the leak check both only
ever act on/verify what redaction actually touched). 010406_PD1_PRT_p034
("Ollie Maduro") is the sharper case: Maduro is not on the roster at all,
so no text check, however thorough, has anything to compare it against.
This module is the actual backstop for that second case -- it works from
ink position and frequency, never from OCR'd text or roster membership, so
it doesn't care whether the name is a roster student's or not.

--- Why block-median voting, not per-pixel or optical flow ---

The obvious reading of "ink at the same position in most packets is
printed" is a strict per-pixel vote after aligning every page to a common
reference with a rotation+translation (Euclidean) transform recovered via
ECC (cv2.findTransformECC). Measured directly on the real file
(data/PRT/010406_PD1_PRT.pdf, DPI=200): ECC converges cleanly and
confidently (correlation coefficient 0.96-0.98) for every pair tried,
recovering only a small rotation and a few pixels of translation -- exactly
the "skew and offset" a real scan produces. But the per-pixel Dice overlap
between any two aligned pages' raw ink masks was only 0.29-0.44 even after
alignment: small, smoothly-varying local distortion (paper feed/bowing
during scanning), confirmed by direct diff-image inspection to worsen
toward the bottom of the page, not a global rotation/translation/scale
error (re-running with MOTION_AFFINE and MOTION_HOMOGRAPHY recovered a real
~0.7-0.9% scale difference between copies but left the same doubling
pattern nearly unchanged). Dense optical flow
(cv2.calcOpticalFlowFarneback) was tried as a local-refinement pass on top
of the Euclidean alignment and did cut mean pixel-wise diff by ~65% -- but
it also produces huge, spurious per-pixel displacements (up to 300px)
exactly where two copies' handwriting differs, because dense flow tries to
explain *any* local mismatch as motion, including genuine content
differences. That is the one failure mode this can't tolerate: an alignment
method that warps real handwriting into agreement would silently erase the
very thing this module exists to find. Dense flow was rejected outright,
even though it measured better on raw pixel error.

The fix that actually holds up against real measurement: vote at block
granularity (BLOCK_PX, a fixed grid over the Euclidean-aligned page),
comparing each packet's per-block ink density against the group's per-block
*median* density, rather than a binary per-pixel/per-block "any ink" vote.
For a genuinely printed block, density is tightly clustered across
independent copies (measured: the 99.9th percentile of density deviation
in a region confirmed to contain only printed instruction text was
0.26-0.40 at BLOCK_PX in {16,24}) -- low enough that
DENSITY_DIFF_THRESHOLD cleanly separates it from genuine ink, which is why
the median (not a strict "ink in most packets" pixel vote, measured as
producing 13-36% of a single control packet's own ink flagged as
"non-consensus" even at a coarse 20px/7.2pt block -- overwhelmingly false
positives from the sub-block jitter above) is used as the per-block
reference. BLOCK_PX=16, not the first-tried 24: at 24px, real handwritten
header ink was missed on 20% of real packets (37/46) because the ink was
thin/compact enough to never form a connected run of MIN_CONNECTED_BLOCKS
adjacent 24px blocks -- the speckle filter silently rejecting real short
names. A finer 16px grid concentrates the same ink into denser, more
connected blocks and reached full 104/104 real header-page detection with
the same MIN_CONNECTED_BLOCKS/DENSITY_DIFF_THRESHOLD.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from melredact.config import (
    BORDER_DARK_THRESHOLD,
    CACHE_DIR,
    CONSENSUS_BLOCK_PX,
    CONSENSUS_DENSITY_DIFF_THRESHOLD,
    CONSENSUS_DPI,
    CONSENSUS_ECC_DOWNSCALE,
    CONSENSUS_ECC_EPS,
    CONSENSUS_ECC_ITERS,
    CONSENSUS_MIN_CONNECTED_BLOCKS,
    CONSENSUS_MIN_GROUP_SIZE,
    CONSENSUS_REGISTRATION_TOLERANCE_BLOCKS,
    CONSENSUS_WRITING_ZONE_DILATION_DEFAULT_PT,
    CONSENSUS_WRITING_ZONE_DILATION_PT,
    CONSENSUS_WRITING_ZONE_MIN_SHARE,
    CONSENSUS_WRITING_ZONE_MIN_SHARE_DEFAULT,
)
from melredact.ocr import file_content_hash
from melredact.pdfio import open_pdf
from melredact.segment import SegmentResult

Bbox = tuple[float, float, float, float]  # (left, top, right, bottom) in page points, top-down

PT_PER_PX = 72.0 / CONSENSUS_DPI


@dataclass
class AnomalyHold:
    packet_tag: str
    page_offset: int
    physical_page_index: int
    bbox_pt: Bbox
    occurrence_count: int
    group_size: int

    @property
    def reason(self) -> str:
        left, top, right, bottom = (round(v, 1) for v in self.bbox_pt)
        return (
            f"consensus-ink anomaly on page {self.page_offset + 1}: ink at "
            f"({left}, {top})-({right}, {bottom}) pt has only {self.occurrence_count} "
            f"of {self.group_size} packet(s) (counting itself) with any ink nearby -- "
            "not printed template content, and outside the group's shared writing-zone "
            "mask, so not an ordinary answer/response field either"
        )


@dataclass
class ConsensusGroupSkipped:
    worksheet_type: str
    page_offset: int
    n_items: int
    min_group_size: int = CONSENSUS_MIN_GROUP_SIZE


@dataclass
class ConsensusAnalysis:
    holds: dict[str, list[AnomalyHold]] = field(default_factory=dict)
    skipped_groups: list[ConsensusGroupSkipped] = field(default_factory=list)

    def holds_for(self, tag: str) -> list[AnomalyHold]:
        return self.holds.get(tag, [])


def render_gray(pdf, page_index: int, dpi: int = CONSENSUS_DPI) -> np.ndarray:
    return np.asarray(pdf.pages[page_index].to_image(resolution=dpi).original.convert("L"))


def align_to_reference(ref_gray: np.ndarray, img_gray: np.ndarray):
    if img_gray.shape != ref_gray.shape:
        img_gray = cv2.resize(img_gray, (ref_gray.shape[1], ref_gray.shape[0]), interpolation=cv2.INTER_AREA)
    scale = CONSENSUS_ECC_DOWNSCALE
    small_ref = cv2.resize(ref_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA).astype(np.float32)
    small_img = cv2.resize(img_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA).astype(np.float32)
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, CONSENSUS_ECC_ITERS, CONSENSUS_ECC_EPS)
    try:
        cc, warp = cv2.findTransformECC(small_ref, small_img, warp, cv2.MOTION_EUCLIDEAN, criteria, None, 5)
    except cv2.error:
        return None, None
    warp_full = warp.copy()
    warp_full[0, 2] /= scale
    warp_full[1, 2] /= scale
    aligned = cv2.warpAffine(
        img_gray, warp_full, (ref_gray.shape[1], ref_gray.shape[0]),
        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255,
    )
    return aligned, float(cc)


def block_density(mask_float: np.ndarray, b: int) -> np.ndarray:
    h, w = mask_float.shape
    ph, pw = (-h) % b, (-w) % b
    padded = np.pad(mask_float, ((0, ph), (0, pw)))
    hh, ww = padded.shape
    reshaped = padded.reshape(hh // b, b, ww // b, b)
    return reshaped.mean(axis=(1, 3))


def _dilate_float_max(arr: np.ndarray, radius_blocks: int) -> np.ndarray:
    """Per-block max filter: each cell becomes the max of itself and every
    cell within radius_blocks (Chebyshev distance). Used on the group
    median before pass one's diff, tolerating a sub-block registration
    difference between independently re-aligned copies -- see
    CONSENSUS_REGISTRATION_TOLERANCE_BLOCKS's docstring in config.py."""
    if radius_blocks <= 0:
        return arr
    k = 2 * radius_blocks + 1
    kernel = np.ones((k, k), dtype=np.uint8)
    return cv2.dilate(arr.astype(np.float32), kernel)


def _dilate_binary_any(mask: np.ndarray, radius_blocks: int) -> np.ndarray:
    """Same neighborhood expansion as _dilate_float_max, but for a binary
    flagged-block mask: a block becomes 1 if any block within radius_blocks
    was 1. Used to build the writing-zone mask's spatial tolerance -- see
    CONSENSUS_WRITING_ZONE_DILATION_PT's docstring in config.py."""
    if radius_blocks <= 0:
        return mask
    k = 2 * radius_blocks + 1
    kernel = np.ones((k, k), dtype=np.uint8)
    return cv2.dilate(mask, kernel)


def flagged_regions(
    dens_i: np.ndarray,
    median: np.ndarray,
    b: int = CONSENSUS_BLOCK_PX,
    tolerance_blocks: int = CONSENSUS_REGISTRATION_TOLERANCE_BLOCKS,
) -> list[tuple[Bbox, float, tuple[int, int, int, int]]]:
    """Pass one: this packet's own blocks whose density exceeds the group's
    median -- dilated by tolerance_blocks first, see _dilate_float_max --
    by more than DENSITY_DIFF_THRESHOLD, grouped into connected regions
    with speckle (fewer than MIN_CONNECTED_BLOCKS blocks) dropped. Returns
    (bbox_pt, peak_density_diff, block_bbox) triples, block_bbox as
    (x0, y0, x1, y1) in the same block-grid coordinates as `median`/
    `dens_i` -- needed by the writing-zone check in `_analyze_group`, which
    operates on that grid directly rather than re-deriving it from points."""
    ref = _dilate_float_max(median, tolerance_blocks)
    diff = dens_i - ref
    flagged = (diff > CONSENSUS_DENSITY_DIFF_THRESHOLD).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(flagged, connectivity=8)
    regions: list[tuple[Bbox, float, tuple[int, int, int, int]]] = []
    for label in range(1, n):
        x, y, w, h, area_blocks = stats[label]
        if area_blocks < CONSENSUS_MIN_CONNECTED_BLOCKS:
            continue
        left = x * b * PT_PER_PX
        top = y * b * PT_PER_PX
        right = (x + w) * b * PT_PER_PX
        bottom = (y + h) * b * PT_PER_PX
        peak = float(diff[labels == label].max())
        regions.append(((left, top, right, bottom), peak, (x, y, x + w, y + h)))
    return regions


def _consensus_cache_path(pdf_path: str | Path, page_index: int, ref_index: int, dpi: int, block_px: int) -> Path:
    # Keyed the same way ocr.py keys its own disk cache: on the source
    # file's actual content, not its path/mtime. `ref_index` is part of the
    # key (not just file+page+dpi+block) since a page aligned to a
    # different reference page produces a genuinely different aligned
    # result -- see the module docstring's alignment-noise findings.
    return (
        Path(CACHE_DIR) / "consensus" / file_content_hash(pdf_path)
        / f"page_{page_index:04d}_ref{ref_index:04d}_{dpi}_{block_px}.json"
    )


def cached_block_density(
    pdf,
    pdf_path: str | Path,
    page_index: int,
    ref_index: int,
    ref_gray: np.ndarray,
    *,
    dpi: int = CONSENSUS_DPI,
    block_px: int = CONSENSUS_BLOCK_PX,
) -> tuple[np.ndarray | None, bool]:
    """block_density of this page aligned to ref_index's own page, disk-
    cached per (file content hash, page, reference page, dpi, block size) --
    the expensive part (rasterize at DPI + ECC alignment) is paid once per
    distinct (file, page, reference) ever, not once per run. Mirrors ocr.py's
    cached_ocr_words_in_region, same reasoning: this survives a process
    restart or a second `cli.py run`/`analyze`, not just one call's
    lifetime."""
    cache_file = _consensus_cache_path(pdf_path, page_index, ref_index, dpi, block_px)
    if cache_file.exists():
        data = json.loads(cache_file.read_text())
        density = np.array(data["density"], dtype=np.float64) if data["ok"] else None
        return density, data["ok"]

    gray = render_gray(pdf, page_index, dpi)
    if page_index == ref_index:
        aligned, ok = gray, True
    else:
        aligned, _cc = align_to_reference(ref_gray, gray)
        ok = aligned is not None
    density = block_density((aligned < BORDER_DARK_THRESHOLD).astype(np.float64), block_px) if ok else None

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"ok": ok, "density": density.tolist() if density is not None else None}))
    return density, ok


def _zone_params(worksheet_type: str) -> tuple[float, int]:
    """(dilation_pt, min_share) for this worksheet_type's writing-zone mask,
    read from config.py's dict-keyed CONSENSUS_WRITING_ZONE_DILATION_PT/
    CONSENSUS_WRITING_ZONE_MIN_SHARE with a fallback to the module-level
    defaults for any worksheet_type without its own real-data-justified
    entry -- see those constants' own docstrings for why today's two real
    worksheet_types ("PCMEL_MPR_ADR", "PRT") both currently fall through to
    the default rather than getting distinct calibrated entries."""
    dilation_pt = CONSENSUS_WRITING_ZONE_DILATION_PT.get(worksheet_type, CONSENSUS_WRITING_ZONE_DILATION_DEFAULT_PT)
    min_share = CONSENSUS_WRITING_ZONE_MIN_SHARE.get(worksheet_type, CONSENSUS_WRITING_ZONE_MIN_SHARE_DEFAULT)
    return dilation_pt, min_share


def _build_groups(pdf_path: str | Path, segmented: SegmentResult) -> dict[tuple[str, int], list[tuple[str, int]]]:
    """(worksheet_type, page_offset) -> [(packet_tag, physical_page_index), ...].
    The header page is excluded per packet (page_offset whose physical index
    equals packet.header_page_index) -- see the module docstring for why.
    An orphan packet (no header page, so no reliable worksheet_type) is
    excluded entirely, same as analyze_redaction_holds already does.

    Deferred import of packet_tag (not a module-level import) to avoid a
    circular import: pipeline.py imports this module to call
    analyze_consensus_anomalies, so this module can't import pipeline.py at
    load time -- only by the time this function actually runs, when both
    modules are already fully imported."""
    from melredact.pipeline import packet_tag as _packet_tag

    groups: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for packet in segmented.packets:
        if packet.header_page_index is None or packet.worksheet_type is None:
            continue
        tag = _packet_tag(pdf_path, packet)
        for offset, phys in enumerate(packet.page_indices):
            if phys == packet.header_page_index:
                continue
            groups.setdefault((packet.worksheet_type, offset), []).append((tag, phys))
    return groups


def _analyze_group(
    pdf, pdf_path: str | Path, worksheet_type: str, page_offset: int, items: list[tuple[str, int]]
) -> tuple[list[AnomalyHold], ConsensusGroupSkipped | None]:
    if len(items) < CONSENSUS_MIN_GROUP_SIZE:
        return [], ConsensusGroupSkipped(worksheet_type=worksheet_type, page_offset=page_offset, n_items=len(items))

    ref_tag, ref_phys = items[0]
    ref_gray = render_gray(pdf, ref_phys, CONSENSUS_DPI)

    densities: dict[str, np.ndarray] = {}
    phys_by_tag: dict[str, int] = {}
    for tag, phys in items:
        density, ok = cached_block_density(pdf, pdf_path, phys, ref_phys, ref_gray, dpi=CONSENSUS_DPI, block_px=CONSENSUS_BLOCK_PX)
        if ok:
            densities[tag] = density
            phys_by_tag[tag] = phys

    if len(densities) < CONSENSUS_MIN_GROUP_SIZE:
        return [], ConsensusGroupSkipped(worksheet_type=worksheet_type, page_offset=page_offset, n_items=len(densities))

    stacked = np.stack(list(densities.values()))
    median = np.median(stacked, axis=0)

    per_packet_regions: dict[str, list[tuple[Bbox, float, tuple[int, int, int, int]]]] = {}
    per_packet_masks: dict[str, np.ndarray] = {}
    for tag, density in densities.items():
        regions = flagged_regions(density, median, CONSENSUS_BLOCK_PX)
        if not regions:
            continue
        per_packet_regions[tag] = regions
        mask = np.zeros(median.shape, dtype=np.uint8)
        for _bbox, _peak, (x0, y0, x1, y1) in regions:
            mask[y0:y1, x0:x1] = 1
        per_packet_masks[tag] = mask

    n_used = len(densities)
    holds: list[AnomalyHold] = []
    if not per_packet_masks:
        return holds, None

    dilation_pt, min_share = _zone_params(worksheet_type)
    block_pt = CONSENSUS_BLOCK_PX * PT_PER_PX
    zone_radius_blocks = round(dilation_pt / block_pt)

    dilated_masks = {tag: _dilate_binary_any(mask, zone_radius_blocks) for tag, mask in per_packet_masks.items()}
    total_coverage = np.zeros(median.shape, dtype=np.int32)
    for dilated in dilated_masks.values():
        total_coverage += dilated

    for tag, regions in per_packet_regions.items():
        coverage_excl_self = total_coverage - dilated_masks[tag]
        for bbox, _peak, (x0, y0, x1, y1) in regions:
            nearby_other_packets = int(coverage_excl_self[y0:y1, x0:x1].max())
            if nearby_other_packets >= min_share:
                continue
            holds.append(
                AnomalyHold(
                    packet_tag=tag,
                    page_offset=page_offset,
                    physical_page_index=phys_by_tag[tag],
                    bbox_pt=bbox,
                    occurrence_count=nearby_other_packets + 1,
                    group_size=n_used,
                )
            )
    return holds, None


def analyze_consensus_anomalies(pdf_path: str | Path, segmented: SegmentResult) -> ConsensusAnalysis:
    """Whole-file entry point: groups every packet's non-header pages by
    (worksheet_type, page_offset), runs the two-pass consensus check on each
    group, and returns packet_tag -> anomalous regions plus which groups
    were too small to check at all. Computed once per run (see pipeline.
    run_dispositions's `consensus_holds` parameter), not per packet -- the
    whole point of building a group's consensus is that it's shared work
    across every packet in it."""
    groups = _build_groups(pdf_path, segmented)
    holds: dict[str, list[AnomalyHold]] = {}
    skipped: list[ConsensusGroupSkipped] = []
    with open_pdf(pdf_path) as pdf:
        for (worksheet_type, page_offset), items in sorted(groups.items()):
            group_holds, skip = _analyze_group(pdf, pdf_path, worksheet_type, page_offset, items)
            if skip is not None:
                skipped.append(skip)
                continue
            for hold in group_holds:
                holds.setdefault(hold.packet_tag, []).append(hold)
    return ConsensusAnalysis(holds=holds, skipped_groups=skipped)


def format_consensus_report(analysis: ConsensusAnalysis) -> str:
    n_holds = sum(len(v) for v in analysis.holds.values())
    lines = [
        "Consensus-ink anomaly check (template-agnostic handwriting detector, non-header pages):",
        f"  {len(analysis.holds)} packet(s) with {n_holds} anomalous region(s) flagged",
    ]
    for tag in sorted(analysis.holds):
        for hold in analysis.holds[tag]:
            lines.append(f"    {tag}: {hold.reason}")
    if analysis.skipped_groups:
        lines.append(
            "  group(s) below the minimum group size -- this check ran nothing for them, "
            "not silently skipped:"
        )
        for g in analysis.skipped_groups:
            lines.append(
                f"    worksheet_type={g.worksheet_type!r} page_offset={g.page_offset}: "
                f"{g.n_items} packet(s) available, need >= {g.min_group_size}"
            )
    return "\n".join(lines)
