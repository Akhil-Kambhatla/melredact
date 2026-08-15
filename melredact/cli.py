"""Command-line entry point.

    python -m melredact.cli preflight --pdf <scan.pdf> --roster <roster.csv> [--period 2]
    python -m melredact.cli run --pdf <scan.pdf> --roster <roster.csv> [--out out] [--decisions decisions] [--period 2]
    python -m melredact.cli analyze --pdf <scan.pdf> --roster <roster.csv> [--period 2]
    python -m melredact.cli verify --roster <roster.csv> [--out out]

`preflight` is read-only, whole-file, and meant to run first -- the moment
a file comes off Box, before any review, before `run`/`analyze`, before
anything touches `out/`, `decisions/`, or the ledger (see pipeline.
run_preflight). It reports, in one pass: page/packet counts and each
packet's footer-declared length, any page-count-vs-footer disagreement,
round groups with their date histograms and disagreeing packets, every
page with a nonzero-or-low-confidence orientation (located as "page N of
packet X"), every packet with an undetected header border, the roster
block in use and how many packets have no plausible match, consensus-ink
anomaly counts, uncovered-ink advisory counts, and any unsegmentable
packet with its reason -- ending in a plain verdict: how many packets
would process cleanly, how many would need a human in the editor, and how
many can't be processed without a fix. It deliberately skips the one
expensive thing `analyze` does (a real per-packet redaction draft with a
full-page OCR pass, just to run verify_no_leaked_names) -- detection-hold
and the uncovered-ink advisory only need the header page's own raster
plus the header-band OCR crop matching already makes, so preflight stays
bounded by the same small-crop OCR cost `run`'s own segmentation stage
already pays on a cold cache, not the full-page cost. Every OCR/
orientation/consensus call it makes is disk-cached the identical way
`run`'s own calls are, so a `run` immediately following a `preflight`
against the same file starts warm. Unless `--no-contact-sheet`, it also
renders a contact sheet of every flagged page's thumbnail (labelled with
why it was flagged) to `out/.diagnostics/<pdf-stem>_preflight/contact_
sheet.png`, to eyeball in one place instead of paging through
review_app.py.

`analyze` is read-only: it segments the file, prints the round grouping
report, then drafts (never writes) a redaction attempt for every header
packet to report how many packets, per round group, would be held for
detection confidence, held for uncovered group-row ink, held for a
text-layer leak, or would pass cleanly -- see pipeline.analyze_redaction_
holds. Nothing is written to `--out`, nothing is deleted, and no decision
or ledger file is touched. Meant for sizing up a real file (especially one
that has never been through this code before) before running anything for
real -- e.g. CLAUDE.md's bug #7 uncovered-ink trade-off, which real-data
runs have shown firing far more broadly than first measured.

`run` accepts `--round <label>` (e.g. '2025-10', from the round grouping
report both `run` and `analyze` print) to restrict a run to a single
collection session inside a larger concatenated scan -- packets outside it
are not segmented for matching, not redacted, not written, and never
looked up in the ledger, so a run scoped to one round can never delete or
disturb another round's already-shipped output (see pipeline.
filter_packets_by_round). `run` also accepts `--no-delete`, a blanket
safety switch that suppresses every deletion the run would otherwise
perform regardless of what any individual decision says -- useful for a
pilot against a file that hasn't been through this code before.

`run`'s summary line also reports a "consent-held" count, separate from
"held back for review": a packet whose best match is a held name (see
roster.py's Roster.held_names, pipeline.py's consent_hold) is a permanent
structural state, not something a fix or a human decision ever turns into
a write -- it's counted on its own so it doesn't get conflated with
held_back packets that genuinely need attention.

`run` does not review anything itself -- it applies whatever `decisions`
already exist on disk (see pipeline.py's three-state contract), exactly
like review_app.py's "Run redaction pipeline" button. Reviewing packets
and recording decisions still happens in the Streamlit UI
(`streamlit run review_app.py -- <pdf> <roster>`); this command is for
re-running dispositions headlessly once decisions are recorded, e.g. after
the roster or a decision changes, without relaunching the browser UI.
`--period` should match whatever period review_app.py was scoped to for
this same scan (explicit or inferred) -- see roster.py's module docstring
for why a scan is only ever matched against one period's roster block.

`verify` intentionally does *not* take `--period` and always checks
against the whole roster, every period included -- it's a safety net, and
narrowing its search space would only make it worse at its one job. It
also does not take `--pdf`: output is named
out/<teacher>/<period>/<worksheet_type>/<topic>/<round>/<SID>.pdf (see pipeline.py), which
carries no trace of which scan produced it, so there is nothing
scan-specific left for verify to filter by -- it walks every file under
`--out` and checks it against the whole roster.

`verify` independently re-checks whatever is already sitting in `out/`
against the roster -- the same check `run_dispositions` already runs
automatically before writing each file (see RUNBOOK.md), just invocable on
demand. If nothing is found under `--out`, that's treated as a hard
failure (not a vacuous pass) -- a verify pass that silently checks nothing
because it was pointed at the wrong directory is more dangerous than one
that errors loudly.

`run` also does date-driven block resolution (melredact/blocks.py), but
ONLY when the roster has a `<roster_stem>_blocks.json` sidecar -- a
teacher whose blocks encode collection round as well as class period
(e.g. 010406), where filename-based `--period` inference alone can
silently pick the wrong one of two identically-named blocks. When that
sidecar is absent (every other teacher), none of this applies and `run`
behaves exactly as documented above. When it's present, `--period` is
never read; `--class-period` (or 'PDn' in --pdf's filename) picks the
class period, the packets' own OCR'd dates resolve which block/round,
and `--confirm-block <NN>` is a mandatory human confirmation gate -- see
blocks.py's module docstring for why no automated check can ever catch a
wrong block on its own.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from melredact.blocks import (
    collect_packet_dates,
    collect_packet_rounds,
    decisions_scope_mismatches,
    disagreeing_packets,
    format_month_histogram,
    format_resolution_report,
    format_round_report,
    load_block_metadata,
    normalize_block,
    resolve_block,
    round_labels_by_tag,
    save_resolved_block_record,
)
from melredact.consensus import analyze_consensus_anomalies, format_consensus_report
from melredact.pipeline import (
    analyze_redaction_holds,
    decisions_path,
    filter_packets_by_round,
    format_hold_analysis_report,
    format_preflight_report,
    load_composition_overrides,
    load_decisions,
    load_detection_overrides,
    duplicate_decisions_within_round,
    format_duplicate_decisions_report,
    load_manual_geometry,
    load_orientation_overrides,
    load_page_order,
    packet_tag,
    render_preflight_contact_sheet,
    run_dispositions,
    run_preflight,
)
from melredact.redact import verify_no_leaked_names
from melredact.roster import RosterError, infer_period_from_filename, load_full_roster, load_roster
from melredact.segment import find_reversed_continuation_header_pairs, segment_pdf


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pdf", required=True, help="scanned packet PDF")
    parser.add_argument("--roster", required=True, help="consent roster CSV")


def _cmd_run(args: argparse.Namespace) -> int:
    pdf_path = Path(args.pdf)
    roster_path = Path(args.roster)
    out_dir = Path(args.out)
    if out_dir.suffix.lower() == ".pdf":
        print(
            f"error: --out {out_dir} looks like a single file, but output is one redacted PDF per "
            f"approved packet, named by SID under a <teacher>/<period>/<worksheet_type>/<topic>/<round> "
            f"subdirectory (e.g. {out_dir.stem}/020415/02/PRT/NA/2026-03/0204150204.pdf), written into "
            f"--out as a directory -- pass a directory name instead (e.g. --out {out_dir.stem})",
            file=sys.stderr,
        )
        return 1
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1
    if not roster_path.exists():
        print(f"Roster CSV not found: {roster_path}", file=sys.stderr)
        return 1

    # Loaded before segmenting -- a human's per-page rotation choice (see
    # orientation.py's detect-and-ask design and review_app.py's rotate
    # controls) has to reach segmentation itself, not just redaction, so
    # every packet built from a confirmed/corrected page reads correctly
    # from the start rather than being (re-)flagged as unresolved here.
    orientation_overrides = load_orientation_overrides(pdf_path, decisions_dir=Path(args.decisions))
    # A human-confirmed page-order correction (see review_app.py's page
    # composition editor and segment.segment_pdf's own `page_sequence`
    # parameter) -- None (the overwhelmingly common case) means "natural
    # physical order", segment_pdf's own existing default.
    page_sequence = load_page_order(pdf_path, decisions_dir=Path(args.decisions))

    # Segmented once, up front, and reused everywhere else in this command
    # (block-date collection, round grouping, run_dispositions itself) --
    # segmentation is cheap on its own, but re-segmenting needlessly would
    # also mean re-deriving packet identity (packet_tag) from scratch each
    # time, which is only ever safe when it's the exact same SegmentResult.
    segmented = segment_pdf(pdf_path, orientation_overrides=orientation_overrides, page_sequence=page_sequence)

    pending_rotation = [
        p for p in segmented.packets if any("not yet confirmed by a reviewer" in i for i in p.issues)
    ]
    if pending_rotation:
        print(
            f"note: {len(pending_rotation)} packet(s) have a page with a confident but unconfirmed "
            "rotation guess -- use review_app.py's rotate control to confirm or correct before this "
            f"run can process them: {[Path(pdf_path).stem + f'_p{p.page_indices[0]:03d}' for p in pending_rotation]}"
        )

    reversal_suggestions = find_reversed_continuation_header_pairs(segmented)
    if reversal_suggestions:
        print(
            f"note: {len(reversal_suggestions)} continuation-before-header page pair(s) look reversed "
            "(consistent footer sequence read in the other order) -- use review_app.py's page "
            f"composition editor to review and apply: "
            f"{[(s.continuation_page_index, s.header_page_index) for s in reversal_suggestions]}"
        )

    round_groups = collect_packet_rounds(pdf_path, segmented=segmented, orientation_overrides=orientation_overrides)
    print(format_round_report(round_groups))
    round_labels = round_labels_by_tag(round_groups)

    # --round restricts this run to one collection session inside a larger
    # concatenated scan (see pipeline.filter_packets_by_round). Round
    # grouping itself is always computed over the *whole* file above --
    # grouping is inherently file-level (see blocks.py) -- but everything
    # from here on that actually matches, redacts, writes, or deletes
    # (run_dispositions below) only ever sees `run_segmented`, so a packet
    # outside the chosen round is never touched: not segmented for
    # matching, not redacted, not written, and never looked up in the
    # ledger, which is what lets a later round's run never disturb an
    # earlier round's already-shipped output.
    run_segmented = segmented
    if args.round is not None:
        known_labels = sorted({g.label for g in round_groups})
        if args.round not in known_labels:
            print(
                f"error: --round {args.round!r} is not one of this file's round groups ({known_labels})",
                file=sys.stderr,
            )
            return 1
        run_segmented = filter_packets_by_round(pdf_path, segmented, round_labels, args.round)
        print(f"  restricting to round {args.round!r}: {len(run_segmented.packets)} of {len(segmented.packets)} packet(s)")

    # Computed once here (same "whole file, up front" posture as round
    # grouping above), never re-derived inside run_dispositions below --
    # see pipeline.run_dispositions's `consensus_holds` parameter and
    # consensus.py's own module docstring for the cost this pass carries
    # (a full-group rasterize + ECC alignment pass, disk-cached, but still
    # worth computing once, not twice).
    consensus_analysis = analyze_consensus_anomalies(pdf_path, segmented, orientation_overrides=orientation_overrides)
    print()
    print(format_consensus_report(consensus_analysis))

    metadata = load_block_metadata(roster_path)
    resolved_block = None
    period_for_roster = args.period

    if metadata is not None:
        if args.class_period is not None:
            class_period = args.class_period
        else:
            inferred = infer_period_from_filename(pdf_path)
            if inferred is None:
                print(
                    f"error: block metadata exists at {roster_path.parent}/{roster_path.stem}_blocks.json for "
                    "this roster, but no --class-period was given and none could be inferred from --pdf's "
                    "filename (expected something like 'PD1') -- pass --class-period explicitly",
                    file=sys.stderr,
                )
                return 1
            class_period = int(inferred)

        dates = collect_packet_dates(pdf_path, segmented=segmented, orientation_overrides=orientation_overrides)
        resolution = resolve_block(dates, class_period, metadata)
        print(format_resolution_report(resolution))

        chosen_block = None
        if args.block is not None:
            block_code = normalize_block(args.block)
            if block_code not in metadata.blocks:
                print(
                    f"error: --block {args.block!r} is not defined in this roster's block metadata "
                    f"(known blocks: {sorted(metadata.blocks)})",
                    file=sys.stderr,
                )
                return 1
            chosen_block = metadata.blocks[block_code]
            print(f"  explicit --block override in use: {chosen_block.describe()}")
        elif resolution.resolved:
            chosen_block = resolution.chosen_block

        if chosen_block is None:
            print(
                "\nerror: could not resolve a block from packet dates and no --block override was given -- "
                "pass --block <NN> explicitly (with --confirm-block <NN> matching) to proceed",
                file=sys.stderr,
            )
            return 1

        if args.confirm_block is None:
            print(
                f"\nerror: block metadata exists for this roster -- pass --confirm-block {chosen_block.block} "
                f"to confirm you have read the report above and agree with {chosen_block.describe()} before "
                "anything is processed",
                file=sys.stderr,
            )
            return 1

        confirm_code = normalize_block(args.confirm_block)
        if confirm_code != chosen_block.block:
            print(
                f"error: --confirm-block {args.confirm_block!r} does not match the resolved block "
                f"({chosen_block.describe()}) -- refusing to process. If the resolution itself is wrong, "
                "pass --block <NN> to override it explicitly, with --confirm-block matching that override.",
                file=sys.stderr,
            )
            return 1

        disagreeing = disagreeing_packets(resolution)
        if disagreeing:
            print(
                f"\nnote: {len(disagreeing)} packet(s) have their own date disagreeing with the file's "
                f"resolved majority (flagged for review, not blocked): {disagreeing}"
            )

        resolved_block = chosen_block
        period_for_roster = chosen_block.block
    else:
        # No block sidecar for this roster -- date resolution is purely
        # informational here (see blocks.format_month_histogram): a sanity
        # signal for a human skimming the run, never something that gates
        # or alters this run. Only a `_blocks.json` sidecar makes date
        # resolution load-bearing (the branch above).
        print(
            format_month_histogram(
                collect_packet_dates(pdf_path, segmented=segmented, orientation_overrides=orientation_overrides)
            )
        )

    try:
        roster = load_roster(roster_path, period=period_for_roster, infer_period_from=pdf_path)
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    decisions = load_decisions(pdf_path, decisions_dir=Path(args.decisions))
    detection_overrides = load_detection_overrides(pdf_path, decisions_dir=Path(args.decisions))
    composition_overrides = load_composition_overrides(pdf_path, decisions_dir=Path(args.decisions))
    manual_geometry = load_manual_geometry(pdf_path, decisions_dir=Path(args.decisions))

    if resolved_block is not None:
        mismatches = decisions_scope_mismatches(decisions, resolved_block.block)
        if mismatches:
            offending = ", ".join(f"{tag} (sid {sid})" for tag, sid in mismatches)
            print(
                f"error: {decisions_path(pdf_path, Path(args.decisions))} already contains decision(s) scoped "
                f"to a different block than this run's ({resolved_block.describe()}): {offending} -- refusing "
                "to process. Either this run's block is wrong, or these decisions were recorded under a "
                "different block; resolve the discrepancy before re-running.",
                file=sys.stderr,
            )
            return 1
        save_resolved_block_record(pdf_path, resolved_block, decisions_dir=Path(args.decisions))

    if not decisions:
        print(
            f"No decisions recorded yet at {decisions_path(pdf_path, Path(args.decisions))} -- "
            "every packet is pending. Review packets first:\n"
            f'  streamlit run review_app.py -- "{pdf_path}" "{roster_path}"'
        )

    duplicate_decisions = duplicate_decisions_within_round(decisions, round_labels)
    print()
    print(format_duplicate_decisions_report(duplicate_decisions))

    results = run_dispositions(
        pdf_path,
        run_segmented,
        decisions,
        roster,
        out_dir=out_dir,
        flatten=args.flatten,
        detection_overrides=detection_overrides,
        composition_overrides=composition_overrides,
        round_labels=round_labels,
        allow_delete=not args.no_delete,
        manual_geometry=manual_geometry,
        consensus_holds=consensus_analysis.holds,
        orientation_overrides=orientation_overrides,
    )

    written = [r for r in results if r.out_path is not None]
    deleted = [r for r in results if r.deleted_path is not None]
    pending = [r for r in results if r.pending]
    held_back = [r for r in results if r.held_back]
    consent_held = [r for r in results if r.consent_hold]
    deletion_skipped = [r for r in results if r.deletion_skipped]

    collided = [r for r in written if r.collision_note]
    manual_geometry_writes = [r for r in written if r.geometry_source == "manual"]
    advisory_writes = [r for r in written if r.advisory_uncovered_words]

    for r in written:
        note = f"  ({r.reason})" if r.reason else ""
        geom_note = "  [manual geometry]" if r.geometry_source == "manual" else ""
        advisory_note = f"  [advisory: {len(r.advisory_uncovered_words)} uncovered-ink word(s)]" if r.advisory_uncovered_words else ""
        print(f"wrote   {r.out_path}{note}{geom_note}{advisory_note}")
    for r in collided:
        print(f"COLLISION AVOIDED for {r.packet_tag}: {r.collision_note}")
    for r in deleted:
        print(f"deleted {r.deleted_path}")
    for r in pending:
        print(f"pending {r.packet_tag} (not yet reviewed)")
    for r in held_back:
        print(f"held back {r.packet_tag} (sid {r.sid}): {r.reason}")
    for r in consent_held:
        print(f"consent hold {r.packet_tag} (no sid): {r.reason}")
    for r in deletion_skipped:
        print(f"deletion disabled -- kept {r.packet_tag}: {r.reason}")

    print(
        f"\n{len(written)} written ({len(collided)} collision(s) avoided), {len(deleted)} deleted, "
        f"{len(held_back)} held back for review, {len(consent_held)} consent-held (no SID), "
        f"{len(pending)} still pending review"
        + (f", {len(deletion_skipped)} deletion(s) skipped (--no-delete)" if deletion_skipped else "")
    )
    # Geometry provenance + advisory volume, per this run -- see CLAUDE.md's
    # "From detection-gates-workflow to human-reviews-everything" section:
    # whether the uncovered-ink advisory is actually earning its place (real
    # manual edits track it) or is noise (fires on nearly every write
    # regardless) is exactly what this line is for.
    print(
        f"geometry: {len(written) - len(manual_geometry_writes)} automatic, {len(manual_geometry_writes)} "
        f"manually edited -- {len(advisory_writes)} write(s) carried an uncovered-ink advisory"
    )
    return 1 if held_back else 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    pdf_path = Path(args.pdf)
    roster_path = Path(args.roster)
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1
    if not roster_path.exists():
        print(f"Roster CSV not found: {roster_path}", file=sys.stderr)
        return 1

    orientation_overrides = load_orientation_overrides(pdf_path, decisions_dir=Path(args.decisions))
    page_sequence = load_page_order(pdf_path, decisions_dir=Path(args.decisions))
    segmented = segment_pdf(pdf_path, orientation_overrides=orientation_overrides, page_sequence=page_sequence)
    round_groups = collect_packet_rounds(pdf_path, segmented=segmented, orientation_overrides=orientation_overrides)
    print(format_round_report(round_groups))
    round_labels = round_labels_by_tag(round_groups)

    try:
        roster = load_roster(roster_path, period=args.period, infer_period_from=pdf_path)
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    consensus_analysis = analyze_consensus_anomalies(pdf_path, segmented, orientation_overrides=orientation_overrides)
    print()
    print(format_consensus_report(consensus_analysis))

    print(
        "\nDrafting a redaction attempt for every header packet to check hold reasons -- nothing "
        "is written to out/, redacted output is discarded immediately after inspection, and "
        "nothing is deleted.\n"
    )
    results = analyze_redaction_holds(
        pdf_path, segmented, roster, round_labels, consensus_analysis.holds, orientation_overrides=orientation_overrides
    )
    print(format_hold_analysis_report(results))
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    pdf_path = Path(args.pdf)
    roster_path = Path(args.roster)
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1
    if not roster_path.exists():
        print(f"Roster CSV not found: {roster_path}", file=sys.stderr)
        return 1

    # Meant to be runnable the moment this file comes off Box -- so a
    # missing decisions directory (no review has happened yet, the normal
    # case for a brand-new file) just means no orientation overrides yet,
    # not an error.
    orientation_overrides = load_orientation_overrides(pdf_path, decisions_dir=Path(args.decisions))
    page_sequence = load_page_order(pdf_path, decisions_dir=Path(args.decisions))
    composition_overrides = load_composition_overrides(pdf_path, decisions_dir=Path(args.decisions))

    try:
        roster = load_roster(roster_path, period=args.period, infer_period_from=pdf_path)
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report = run_preflight(
        pdf_path,
        roster,
        period=args.period,
        orientation_overrides=orientation_overrides,
        page_sequence=page_sequence,
        composition_overrides=composition_overrides,
    )
    print(format_preflight_report(report))

    if not args.no_contact_sheet:
        sheet_path = render_preflight_contact_sheet(
            pdf_path, report, Path(args.out), orientation_overrides=orientation_overrides
        )
        if sheet_path is not None:
            print(f"\nContact sheet of flagged pages: {sheet_path}")
        else:
            print("\nNo pages flagged -- no contact sheet written.")

    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    roster_path = Path(args.roster)
    if not roster_path.exists():
        print(f"Roster CSV not found: {roster_path}", file=sys.stderr)
        return 1

    try:
        roster = load_full_roster(roster_path)
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    # Layout: out/<teacher_code>/<period>/<worksheet_type>/<topic>/<round>/<SID>.pdf
    # -- but the exact depth isn't verify's business to assume (a topic
    # segment was added after this layout was first fixed at four levels,
    # and nothing guarantees another segment is never added later), so this
    # walks recursively rather than globbing a fixed depth. Hidden
    # directories (`.ledger/`, `.manual_queue/` -- derived bookkeeping, not
    # servable output) are excluded explicitly, since a manual-queue draft
    # is a not-yet-safe-to-ship attempt, not something verify should ever
    # check as if it were finished output.
    pdfs = sorted(
        p for p in out_dir.rglob("*.pdf") if not any(part.startswith(".") for part in p.relative_to(out_dir).parts)
    )
    if not pdfs:
        print(
            f"error: no output files found under {out_dir} (expected "
            f"{out_dir}/<teacher>/<period>/<worksheet_type>/<topic>/<round>/<SID>.pdf) -- nothing to verify. "
            "If this is unexpected, check --out points at the right directory "
            "rather than trusting a vacuous pass.",
            file=sys.stderr,
        )
        return 1

    n_failed = 0
    for pdf in pdfs:
        findings = verify_no_leaked_names(pdf, roster)
        if findings:
            n_failed += 1
            print(f"FAIL {pdf.relative_to(out_dir)}: {findings}")
        else:
            print(f"ok   {pdf.relative_to(out_dir)}")

    # Explicit checked-count on every path, pass or fail -- so "0 files
    # checked" is never confusable with "N files checked, all clean"; the
    # hard failure above already refuses to run at all when pdfs is empty,
    # this is the count for whenever it did run.
    print(f"\n{len(pdfs)} file(s) checked, {n_failed} failed")
    return 1 if n_failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m melredact.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="apply recorded decisions: redact+write approved packets, delete rejected ones")
    _add_common_args(p_run)
    p_run.add_argument(
        "--out",
        default="out",
        help="output directory (default: out) -- one redacted PDF per approved packet is written "
        "here, not a single combined file, so this must be a directory name, not a .pdf path",
    )
    p_run.add_argument("--decisions", default="decisions", help="decisions directory (default: decisions)")
    p_run.add_argument(
        "--period",
        default=None,
        help="restrict the roster to this period's block (e.g. '2' or '02'); inferred from --pdf's "
        "filename (e.g. 'PD2') if omitted, required only if the roster spans multiple periods and "
        "inference fails",
    )
    p_run.add_argument("--flatten", action="store_true", help="flatten pages to images instead of keeping the OCR text layer")
    p_run.add_argument(
        "--class-period",
        type=int,
        default=None,
        dest="class_period",
        help="class period this scan belongs to, ONLY meaningful when the roster has a "
        "<roster_stem>_blocks.json sidecar (see blocks.py) -- in that case --pdf's own 'PDn' filename "
        "means class period, never roster block, and this overrides that inference. Ignored entirely "
        "when no block metadata sidecar exists.",
    )
    p_run.add_argument(
        "--confirm-block",
        default=None,
        dest="confirm_block",
        help="required whenever block metadata exists for this roster: the block code (e.g. '02') you "
        "have read the printed resolution report and confirm this run should use. Must match the "
        "resolved (or --block-overridden) block exactly, or the run refuses to process anything.",
    )
    p_run.add_argument(
        "--block",
        default=None,
        help="explicit block override, ONLY meaningful when block metadata exists -- for when packet "
        "dates can't be resolved automatically (too few readable dates, no clear majority). Still "
        "requires --confirm-block to match this value.",
    )
    p_run.add_argument(
        "--round",
        default=None,
        help="restrict this run to one round group's packets (e.g. '2025-10' or 'undated', see the "
        "'Round grouping report' printed above) -- packets outside it are not segmented for "
        "matching, not redacted, not written, and never looked up in the ledger, so a run scoped "
        "to one round can never delete or disturb another round's already-shipped output. Useful "
        "for a small pilot against one session inside a larger concatenated scan.",
    )
    p_run.add_argument(
        "--no-delete",
        action="store_true",
        dest="no_delete",
        help="disable every deletion this run would otherwise perform (confirmed non-consent, or a "
        "correction superseding an old SID) -- a blanket safety switch for a pilot or a file that "
        "hasn't been through this code before. Matching, redaction, and writing new output all "
        "proceed normally; only deletion is suppressed, and a suppressed deletion is still reported.",
    )
    p_run.set_defaults(func=_cmd_run)

    p_analyze = sub.add_parser(
        "analyze",
        help="read-only redaction hold analysis: report per-round-group hold counts (detection "
        "confidence, uncovered group-row ink, text-layer leaks) without writing, redacting to "
        "disk, or deleting anything",
    )
    _add_common_args(p_analyze)
    p_analyze.add_argument(
        "--period",
        default=None,
        help="restrict the roster to this period's block (e.g. '2' or '02'); inferred from --pdf's "
        "filename (e.g. 'PD2') if omitted",
    )
    p_analyze.add_argument(
        "--decisions",
        default="decisions",
        help="decisions directory (default: decisions) -- read here only for any already-recorded "
        "per-page orientation confirmations (see orientation.py's detect-and-ask design), so a page "
        "a reviewer has already rotated is analyzed at its confirmed orientation rather than being "
        "reported unresolved a second time",
    )
    p_analyze.set_defaults(func=_cmd_analyze)

    p_preflight = sub.add_parser(
        "preflight",
        help="read-only whole-file report of everything wrong with a scan before any review or run: "
        "page/packet counts vs. footer, round-date histograms, orientation issues, undetected header "
        "borders, roster match coverage, consensus-ink/uncovered-ink counts, and unsegmentable "
        "packets, ending in a plain clean/needs-editor/cannot-process verdict. Writes nothing, "
        "deletes nothing -- meant to be run the moment a file comes off Box, before deciding whether "
        "to review it now or hand it back. Populates the same OCR/orientation/consensus disk caches "
        "the real run reads, so nothing here is wasted work.",
    )
    _add_common_args(p_preflight)
    p_preflight.add_argument(
        "--period",
        default=None,
        help="restrict the roster to this period's block (e.g. '2' or '02'); inferred from --pdf's "
        "filename (e.g. 'PD2') if omitted",
    )
    p_preflight.add_argument(
        "--decisions",
        default="decisions",
        help="decisions directory (default: decisions) -- read here only for any already-recorded "
        "per-page orientation confirmations, so a page a reviewer already rotated is reported at its "
        "confirmed orientation rather than flagged all over again",
    )
    p_preflight.add_argument(
        "--out",
        default="out",
        help="output directory (default: out) -- preflight never writes redacted output here, only "
        "(unless --no-contact-sheet) a contact sheet of flagged pages under out/.diagnostics/",
    )
    p_preflight.add_argument(
        "--no-contact-sheet",
        action="store_true",
        dest="no_contact_sheet",
        help="skip rendering the contact-sheet image of flagged pages under out/.diagnostics/",
    )
    p_preflight.set_defaults(func=_cmd_preflight)

    p_verify = sub.add_parser("verify", help="re-check files already in --out for leaked roster names")
    p_verify.add_argument("--roster", required=True, help="consent roster CSV (every period, unscoped)")
    p_verify.add_argument("--out", default="out", help="output directory to scan (default: out)")
    p_verify.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
