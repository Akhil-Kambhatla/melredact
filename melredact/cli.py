"""Command-line entry point.

    python -m melredact.cli run --pdf <scan.pdf> --roster <roster.csv> [--out out] [--decisions decisions] [--period 2]
    python -m melredact.cli verify --roster <roster.csv> [--out out]

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
out/<teacher>/<period>/<worksheet_type>/<topic>/<SID>.pdf (see pipeline.py), which
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
    decisions_scope_mismatches,
    disagreeing_packets,
    format_month_histogram,
    format_resolution_report,
    load_block_metadata,
    normalize_block,
    resolve_block,
    save_resolved_block_record,
)
from melredact.pipeline import decisions_path, load_decisions, load_detection_overrides, packet_tag, run_dispositions
from melredact.redact import verify_no_leaked_names
from melredact.roster import RosterError, infer_period_from_filename, load_full_roster, load_roster
from melredact.segment import segment_pdf


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
            f"approved packet, named by SID under a <teacher>/<period>/<worksheet_type> subdirectory "
            f"(e.g. {out_dir.stem}/020415/02/PRT/NA/0204150204.pdf), written into --out as a directory -- "
            f"pass a directory name instead (e.g. --out {out_dir.stem})",
            file=sys.stderr,
        )
        return 1
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1
    if not roster_path.exists():
        print(f"Roster CSV not found: {roster_path}", file=sys.stderr)
        return 1

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

        dates = collect_packet_dates(pdf_path)
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
        print(format_month_histogram(collect_packet_dates(pdf_path)))

    try:
        roster = load_roster(roster_path, period=period_for_roster, infer_period_from=pdf_path)
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    segmented = segment_pdf(pdf_path)
    decisions = load_decisions(pdf_path, decisions_dir=Path(args.decisions))
    detection_overrides = load_detection_overrides(pdf_path, decisions_dir=Path(args.decisions))

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

    results = run_dispositions(
        pdf_path,
        segmented,
        decisions,
        roster,
        out_dir=out_dir,
        flatten=args.flatten,
        detection_overrides=detection_overrides,
    )

    written = [r for r in results if r.out_path is not None]
    deleted = [r for r in results if r.deleted_path is not None]
    pending = [r for r in results if r.pending]
    held_back = [r for r in results if r.held_back]
    consent_held = [r for r in results if r.consent_hold]

    collided = [r for r in written if r.collision_note]

    for r in written:
        note = f"  ({r.reason})" if r.reason else ""
        print(f"wrote   {r.out_path}{note}")
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

    print(
        f"\n{len(written)} written ({len(collided)} collision(s) avoided), {len(deleted)} deleted, "
        f"{len(held_back)} held back for review, {len(consent_held)} consent-held (no SID), "
        f"{len(pending)} still pending review"
    )
    return 1 if held_back else 0


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
    # Layout: out/<teacher_code>/<period>/<worksheet_type>/<topic>/<SID>.pdf
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
            f"{out_dir}/<teacher>/<period>/<worksheet_type>/<topic>/<SID>.pdf) -- nothing to verify. "
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
    p_run.set_defaults(func=_cmd_run)

    p_verify = sub.add_parser("verify", help="re-check files already in --out for leaked roster names")
    p_verify.add_argument("--roster", required=True, help="consent roster CSV (every period, unscoped)")
    p_verify.add_argument("--out", default="out", help="output directory to scan (default: out)")
    p_verify.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
