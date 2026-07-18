"""Command-line entry point.

    python -m melredact.cli run --pdf <scan.pdf> --roster <roster.csv> [--out out] [--decisions decisions] [--period 2]
    python -m melredact.cli verify --roster <roster.csv> [--out out]

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
also does not take `--pdf`: output is named out/<teacher>/<period>/<SID>.pdf
(see pipeline.py), which carries no trace of which scan produced it, so
there is nothing scan-specific left for verify to filter by -- it walks
every file under `--out` and checks it against the whole roster.

`verify` independently re-checks whatever is already sitting in `out/`
against the roster -- the same check `run_dispositions` already runs
automatically before writing each file (see RUNBOOK.md), just invocable on
demand. If nothing is found under `--out`, that's treated as a hard
failure (not a vacuous pass) -- a verify pass that silently checks nothing
because it was pointed at the wrong directory is more dangerous than one
that errors loudly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from melredact.pipeline import decisions_path, load_decisions, packet_tag, run_dispositions
from melredact.redact import verify_no_leaked_names
from melredact.roster import RosterError, load_full_roster, load_roster
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
            f"approved packet, named by SID under a <teacher>/<period> subdirectory (e.g. "
            f"{out_dir.stem}/020415/02/0204150204.pdf), written into --out as a directory -- pass "
            f"a directory name instead (e.g. --out {out_dir.stem})",
            file=sys.stderr,
        )
        return 1
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1
    if not roster_path.exists():
        print(f"Roster CSV not found: {roster_path}", file=sys.stderr)
        return 1

    try:
        roster = load_roster(roster_path, period=args.period, infer_period_from=pdf_path)
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    segmented = segment_pdf(pdf_path)
    decisions = load_decisions(pdf_path, decisions_dir=Path(args.decisions))

    if not decisions:
        print(
            f"No decisions recorded yet at {decisions_path(pdf_path, Path(args.decisions))} -- "
            "every packet is pending. Review packets first:\n"
            f'  streamlit run review_app.py -- "{pdf_path}" "{roster_path}"'
        )

    try:
        results = run_dispositions(pdf_path, segmented, decisions, roster, out_dir=out_dir, flatten=args.flatten)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    written = [r for r in results if r.out_path is not None]
    deleted = [r for r in results if r.deleted_path is not None]
    pending = [r for r in results if r.pending]

    for r in written:
        print(f"wrote   {r.out_path}")
    for r in deleted:
        print(f"deleted {r.deleted_path}")
    for r in pending:
        print(f"pending {r.packet_tag} (not yet reviewed)")

    print(f"\n{len(written)} written, {len(deleted)} deleted, {len(pending)} still pending review")
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
    # New layout: out/<teacher_code>/<period>/<SID>.pdf -- exactly two
    # directory levels under --out, no scan-name prefix to filter by (see
    # module docstring for why this dropped --pdf).
    pdfs = sorted(out_dir.glob("*/*/*.pdf"))
    if not pdfs:
        print(
            f"error: no output files found under {out_dir} (expected "
            f"{out_dir}/<teacher>/<period>/<SID>.pdf) -- nothing to verify. "
            "If this is unexpected, check --out points at the right directory "
            "rather than trusting a vacuous pass.",
            file=sys.stderr,
        )
        return 1

    failed = False
    for pdf in pdfs:
        findings = verify_no_leaked_names(pdf, roster)
        if findings:
            failed = True
            print(f"FAIL {pdf.relative_to(out_dir)}: {findings}")
        else:
            print(f"ok   {pdf.relative_to(out_dir)}")

    return 1 if failed else 0


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
    p_run.set_defaults(func=_cmd_run)

    p_verify = sub.add_parser("verify", help="re-check files already in --out for leaked roster names")
    p_verify.add_argument("--roster", required=True, help="consent roster CSV (every period, unscoped)")
    p_verify.add_argument("--out", default="out", help="output directory to scan (default: out)")
    p_verify.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
