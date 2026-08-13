"""Read-only diagnostic for the uncovered-group-row-ink hold (CLAUDE.md bug
#7's accepted trade-off): for every packet that analyze_redaction_holds
would classify as an uncovered-ink or leak hold, capture the actual
evidence -- detected header border, both redaction rectangle edges, every
flagged word's own bbox, and its signed distance to the nearest redaction
edge -- plus a cropped, annotated render of the header region, so a human
can look at all of them directly instead of trusting a boolean.

Mirrors analyze_redaction_holds' own hold precedence exactly (detection
confidence first, then uncovered-ink, then a full verify_no_leaked_names
pass) by calling the same production functions (redact_packet,
verify_no_leaked_names) it does -- never a reimplementation of the
detection/coverage logic itself, so a packet this script calls "held for
uncovered ink" is the same packet a real run would hold for that reason.

Writes nothing to out/, decisions/, or the ledger. Every redacted draft is
written to a TemporaryDirectory removed at the end of the run, the same
pattern analyze_redaction_holds itself uses. Evidence JSON and annotated
PNG crops are written under out/.diagnostics/<pdf-stem>/ -- inside out/,
which is already fully gitignored, since these crops render real
handwritten student ink (the Name row sits inside the same window this
check scans) and must never be committed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import ImageDraw

from melredact.blocks import UNDATED_ROUND, collect_packet_rounds, round_labels_by_tag
from melredact.config import RENDER_DPI_FINAL
from melredact.pdfio import open_pdf
from melredact.pipeline import packet_tag
from melredact.redact import HeaderBand, redact_packet, verify_no_leaked_names
from melredact.roster import RosterError, load_roster
from melredact.segment import segment_pdf


@dataclass
class WordEvidence:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    distance_to_left_bbox: float
    distance_to_right_bbox: float
    distance_to_band_bottom: float


@dataclass
class PacketEvidence:
    packet_tag: str
    round_label: str
    hold_reason: str  # "clean" | "detection" | "uncovered_ink" | "leak"
    band_left: float | None = None
    band_top: float | None = None
    band_right: float | None = None
    band_bottom: float | None = None
    band_detected: bool | None = None
    left_bbox: tuple | None = None
    right_bbox: tuple | None = None
    uncovered_words: list = None
    leak_findings: list = None
    crop_path: str | None = None


def _bbox_gap(bbox: tuple, word: dict) -> float:
    """Minimum perpendicular gap between `word` and `bbox` along whichever
    axis they don't overlap on -- 0 would mean overlapping (never returned
    here, since these are always the words _overlaps_bbox already said
    don't overlap this bbox). Positive means genuinely outside; the sign
    convention matches "how far past the edge," not a general Euclidean
    distance, since these boxes are axis-aligned and the interesting
    question is always "how far below/beside did this ink land.\""""
    left, top, right, bottom = bbox
    gaps = []
    if word["x1"] <= left:
        gaps.append(left - word["x1"])
    if word["x0"] >= right:
        gaps.append(word["x0"] - right)
    if word["bottom"] <= top:
        gaps.append(top - word["bottom"])
    if word["top"] >= bottom:
        gaps.append(word["top"] - bottom)
    return min(gaps) if gaps else 0.0


def _render_crop(pdf_path: Path, header_page_index: int, band: HeaderBand, left_bbox, right_bbox, uncovered, dpi: int, out_path: Path) -> None:
    with open_pdf(pdf_path) as pdf:
        page = pdf.pages[header_page_index]
        image = page.to_image(resolution=dpi).original.convert("RGB")

    scale = dpi / 72.0
    draw = ImageDraw.Draw(image)

    def rect(bbox, color, width):
        left, top, right, bottom = (v * scale for v in bbox)
        draw.rectangle([left, top, right, bottom], outline=color, width=width)

    rect((band.left, band.top, band.right, band.bottom), (0, 120, 255), 4)
    if left_bbox is not None:
        rect(left_bbox, (255, 0, 0), 3)
    if right_bbox is not None:
        rect(right_bbox, (255, 0, 0), 3)

    max_bottom = band.bottom
    for w in uncovered:
        rect((w["x0"], w["top"], w["x1"], w["bottom"]), (255, 165, 0), 3)
        max_bottom = max(max_bottom, w["bottom"])

    crop_bottom_pt = max_bottom + 40
    crop_bottom_px = min(image.height, int(round(crop_bottom_pt * scale)))
    cropped = image.crop((0, 0, image.width, crop_bottom_px))
    cropped.save(out_path)


def diagnose(pdf_path: Path, roster_path: Path, period: str | None, out_dir: Path, dpi: int = RENDER_DPI_FINAL) -> list[PacketEvidence]:
    segmented = segment_pdf(pdf_path)
    round_groups = collect_packet_rounds(pdf_path, segmented=segmented)
    round_labels = round_labels_by_tag(round_groups)

    roster = load_roster(roster_path, period=period, infer_period_from=pdf_path)

    results: list[PacketEvidence] = []
    pdf_stem = pdf_path.stem
    crop_dir = out_dir / pdf_stem
    crop_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as scratch_dir:
        for packet in segmented.packets:
            if packet.header_page_index is None:
                continue
            tag = packet_tag(pdf_path, packet)
            label = round_labels.get(tag, UNDATED_ROUND)
            scratch_path = Path(scratch_dir) / f"{tag}.pdf"
            rr = redact_packet(pdf_path, packet, scratch_path, dpi=dpi)

            ev = PacketEvidence(packet_tag=tag, round_label=label, hold_reason="clean")
            if rr.band is not None:
                ev.band_left, ev.band_top, ev.band_right, ev.band_bottom = (
                    rr.band.left,
                    rr.band.top,
                    rr.band.right,
                    rr.band.bottom,
                )
                ev.band_detected = rr.band.detected
            ev.left_bbox = rr.redact_bbox
            ev.right_bbox = rr.redact_strip_bbox

            if rr.band is not None and not rr.band.detected:
                ev.hold_reason = "detection"
            elif rr.uncovered_group_words:
                ev.hold_reason = "uncovered_ink"
                ev.uncovered_words = []
                for w in rr.uncovered_group_words:
                    ev.uncovered_words.append(
                        WordEvidence(
                            text=w["text"],
                            x0=w["x0"],
                            x1=w["x1"],
                            top=w["top"],
                            bottom=w["bottom"],
                            distance_to_left_bbox=_bbox_gap(rr.redact_bbox, w) if rr.redact_bbox else None,
                            distance_to_right_bbox=_bbox_gap(rr.redact_strip_bbox, w) if rr.redact_strip_bbox else None,
                            distance_to_band_bottom=w["top"] - rr.band.bottom if rr.band else None,
                        )
                    )
                crop_path = crop_dir / f"{tag}.png"
                _render_crop(pdf_path, packet.header_page_index, rr.band, rr.redact_bbox, rr.redact_strip_bbox, rr.uncovered_group_words, dpi, crop_path)
                ev.crop_path = str(crop_path)
            else:
                findings = verify_no_leaked_names(scratch_path, roster)
                if findings:
                    ev.hold_reason = "leak"
                    ev.leak_findings = [asdict(f) for f in findings]
                    crop_path = crop_dir / f"{tag}_leak.png"
                    _render_crop(pdf_path, packet.header_page_index, rr.band, rr.redact_bbox, rr.redact_strip_bbox, [], dpi, crop_path)
                    ev.crop_path = str(crop_path)

            scratch_path.unlink(missing_ok=True)
            results.append(ev)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--roster", required=True)
    parser.add_argument("--period", default=None)
    parser.add_argument("--out", default="out/.diagnostics")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    roster_path = Path(args.roster)
    out_dir = Path(args.out)
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1
    if not roster_path.exists():
        print(f"Roster CSV not found: {roster_path}", file=sys.stderr)
        return 1

    try:
        results = diagnose(pdf_path, roster_path, args.period, out_dir)
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    held = [r for r in results if r.hold_reason != "clean"]
    print(f"{len(results)} header packet(s) scored, {len(held)} held (nothing written/redacted/deleted in out/)")
    for r in held:
        print(f"  {r.packet_tag} [{r.round_label}] -- {r.hold_reason} -- crop: {r.crop_path}")

    evidence_path = out_dir / f"{pdf_path.stem}_evidence.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with open(evidence_path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2, default=str)
    print(f"\nfull evidence written to {evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
