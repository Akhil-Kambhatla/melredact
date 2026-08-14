"""Read-only diagnostic wrapper around melredact.consensus -- the
template-agnostic, position-agnostic handwriting detector that used to live
entirely in this script. The detection logic (block-median voting, group
occurrence-frequency classification) now lives in melredact/consensus.py as
a first-class pipeline check (see pipeline.run_dispositions's
`consensus_holds` parameter); this script is a thin, read-only caller
around it for sizing up a file's real findings without running the
pipeline, the same role it played before the promotion.

Writes nothing to out/'s real tree, decisions/, or the ledger. Redacts
nothing, deletes nothing -- consensus.analyze_consensus_anomalies only ever
rasterizes and reads source pages, never touches the header page's own
redaction path at all. Findings are written as JSON under
out/.diagnostics/consensus_ink/, already fully gitignored (see
data/README.md) -- the report itself names real handwritten ink positions
and packet tags, which is identifiable in the same sense the crops the
original version of this script rendered were.

The original version of this script also rendered annotated PNG crops of
each finding and classified findings by page-position zone ("top-margin"
(no printed content expected) vs "body" (worksheet content area)). Both are
gone: the zone heuristic is exactly what melredact.consensus's second pass
replaced (see its module docstring for why -- position on the page was
never actually the signal that mattered, how many packets share a position
is), and the crops were a stand-in for what review_app.py's manual editor
now does directly and far more usefully -- seed the exact flagged bbox on
the exact page of a held packet, live, in the tool a human actually
resolves the hold in (see review_app.py's `_seed_manual_regions`).

Original validation, kept for the record (unchanged by the promotion; see
melredact/consensus.py and config.py's CONSENSUS_* constants for the
current, promoted parameters): positive control (BLOCK_PX=16 reaches
104/104 real header pages correctly flagging known-present handwriting,
after BLOCK_PX=24 first missed 37/46 -- ink thin enough to never form a
connected run of MIN_CONNECTED_BLOCKS at the coarser grid). Real finding
that motivated the promotion: two page-2 freehand names in
data/PRT/010406_PD1_PRT.pdf, 010406_PD1_PRT_p026 ("Brian Lu") and
010406_PD1_PRT_p034 ("Ollie Maduro", not on the roster at all -- the case
verify_no_leaked_names structurally cannot catch). See CLAUDE.md's own
section on this leak class for the full real re-run results across every
real file.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from melredact.consensus import analyze_consensus_anomalies, format_consensus_report
from melredact.segment import segment_pdf


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-diagnostics", default="out/.diagnostics/consensus_ink")
    parser.add_argument("--out", default="out")
    args = parser.parse_args()

    diag_dir = Path(args.out_diagnostics)
    diag_dir.mkdir(parents=True, exist_ok=True)

    source_files = [
        Path("data/PRT/010406_PD1_PRT.pdf"),
        Path("data/MPR/Hannel MPR PD2.pdf"),
        Path("data/PRT/Hannel PRT PD2.pdf"),
    ]

    full_report: dict[str, dict] = {}
    for pdf_path in source_files:
        if not pdf_path.exists():
            continue
        segmented = segment_pdf(pdf_path)
        analysis = analyze_consensus_anomalies(pdf_path, segmented)
        print(f"{pdf_path.name}:")
        print(format_consensus_report(analysis))
        print()
        full_report[pdf_path.name] = {
            "holds": {tag: [asdict(h) for h in holds] for tag, holds in analysis.holds.items()},
            "skipped_groups": [asdict(g) for g in analysis.skipped_groups],
        }

    # Re-checking already-shipped output (16 real 020415 files across MPR
    # and PRT) for this same leak class is a one-off verification, not part
    # of this script's normal operating input -- consensus.py's real
    # integration point is a source scan via segment_pdf/run_dispositions,
    # the same shape every other check in this pipeline uses. See CLAUDE.md
    # for that one-off real-data re-run's results.

    report_path = diag_dir / "report.json"
    report_path.write_text(json.dumps(full_report, indent=2, default=str))
    print(f"\nfull report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
