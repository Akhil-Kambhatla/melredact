"""End-to-end orchestration: segment -> propose matches -> apply final
per-packet decisions (auto-assign, as corrected by a human reviewer) ->
redact and write, or delete.

`decisions` is a `packet_tag -> sid | None` mapping with a deliberate
three-state contract, since "not yet reviewed" and "confirmed non-consent"
must never be conflated:

- key absent entirely: packet hasn't been decided yet (pending human
  review). `run_dispositions` does nothing for it -- no output written, no
  output deleted.
- key present, value a SID: the packet's final, human-approved (or
  clearly-confident auto-assigned) match. Gets redacted and written.
- key present, value None: reviewed and confirmed *not* consenting -- the
  roster **is** the consent list (see roster.py), so this is not an error
  state or something to leave for later, it's the definition of non-
  consent. Any existing output for this packet is deleted, not left in
  place. (Reversed by John, 2026-07-17 -- the original design only skipped
  *writing new* output for a non-consented packet, leaving anything from a
  prior run sitting untouched in out_dir. Consent can flip between runs --
  e.g. a reviewer rejects a previously-auto-assigned candidate -- and
  out_dir is not append-only.)

Packet identity across runs of the *same* source PDF is grounded in the
packet's first physical page index (see `packet_tag`), not its position in
the packets list (shifts if an earlier packet's page count changes) or a
generated SID (doesn't exist before a decision is made).

**Output layout is `out/<teacher_code>/<period>/<SID>.pdf`, one file per
SID, not per packet_tag** (John, 2026-07-18). The load-bearing invariant is:
"present in the output tree" iff "has a confirmed, approved SID" --
non-consented and pending packets are never in the tree under any name,
including a placeholder. Since a file's name is now the *SID* rather than
the packet_tag that produced it, a packet_tag alone is no longer enough to
know which file (if any) needs deleting when a decision is rejected or
corrected to a different SID -- `run_dispositions` therefore reconciles
the whole target directory against the current `decisions` values at the
end of each run (see the reconciliation pass below) rather than deleting a
single tag-derived path, so a corrected decision's stale SID file is
cleaned up the same way a rejected one is, with no separate code path
needed. This only has to be one directory because `roster` passed in here
is always already narrowed to a single teacher+period block (see
roster.py) -- every SID `decisions` can legally name shares that same
teacher_code/period.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

from melredact.config import RENDER_DPI_FINAL
from melredact.match import MatchProposal, propose
from melredact.redact import verify_no_leaked_names
from melredact.redact import redact_packet as _redact_packet
from melredact.roster import Roster, RosterEntry
from melredact.segment import Packet, SegmentResult, extract_header_fields

DECISIONS_DIR = Path("decisions")
OUT_DIR = Path("out")


def packet_tag(pdf_path: str | Path, packet: Packet) -> str:
    return f"{Path(pdf_path).stem}_p{packet.page_indices[0]:03d}"


def output_path(out_dir: str | Path, entry: RosterEntry) -> Path:
    """Where a confirmed packet for this roster entry lands:
    out/<teacher_code>/<period>/<SID>.pdf. `entry.teacher_code` and
    `entry.period_display` are the SID's own digits (positions 0:6 and
    6:8), not anything read off the packet -- so this is stable and
    derivable from the SID alone, same as the file name itself."""
    return Path(out_dir) / entry.teacher_code / entry.period_display / f"{entry.sid}.pdf"


def decisions_path(pdf_path: str | Path, decisions_dir: Path = DECISIONS_DIR) -> Path:
    return Path(decisions_dir) / f"{Path(pdf_path).stem}.json"


def load_decisions(pdf_path: str | Path, decisions_dir: Path = DECISIONS_DIR) -> dict[str, str | None]:
    path = decisions_path(pdf_path, decisions_dir)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_decisions(
    pdf_path: str | Path, decisions: dict[str, str | None], decisions_dir: Path = DECISIONS_DIR
) -> None:
    path = decisions_path(pdf_path, decisions_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decisions, indent=2, sort_keys=True))


def propose_all(pdf_path: str | Path, segmented: SegmentResult, roster: Roster) -> list[MatchProposal]:
    """One ranked candidate list per packet, keyed by the stable packet_tag
    rather than segment.py's positional packet_index, so callers (the
    review UI, run_dispositions) can key off something that survives
    across runs. Orphan packets (no header page -- see segment.py) have no
    Name field to score at all, so they always abstain with an empty
    candidate list rather than being skipped."""
    proposals = []
    with pdfplumber.open(pdf_path) as pdf:
        for packet in segmented.packets:
            tag = packet_tag(pdf_path, packet)
            if packet.header_page_index is None:
                proposals.append(MatchProposal(packet_tag=tag, candidates=[]))
                continue
            fields = extract_header_fields(pdf.pages[packet.header_page_index])
            proposals.append(propose(tag, fields.name_text, roster))
    return proposals


@dataclass
class DispositionResult:
    # None only for a synthetic result produced by the end-of-run
    # reconciliation sweep (see run_dispositions), which finds a stale
    # output file by SID, not by the packet_tag that originally wrote it.
    packet_tag: str | None
    sid: str | None
    pending: bool
    out_path: Path | None = None
    deleted_path: Path | None = None
    leak_findings: list = field(default_factory=list)


def run_dispositions(
    pdf_path: str | Path,
    segmented: SegmentResult,
    decisions: dict[str, str | None],
    roster: Roster,
    *,
    out_dir: str | Path = OUT_DIR,
    dpi: int = RENDER_DPI_FINAL,
    flatten: bool = False,
) -> list[DispositionResult]:
    """Apply final per-packet decisions. See module docstring for the
    three-state `decisions` contract -- this is where "confirmed
    non-consent" actually becomes a deletion, not just a skipped write --
    and for why output is named by SID (out/<teacher>/<period>/<SID>.pdf)
    rather than by packet_tag.

    A packet with unresolved segment.py `issues` is refused even if
    `decisions` names a SID for it: those issues mean a human hasn't
    actually confirmed this packet is what its footer claims, and
    `decisions` entries are assumed to come from a review flow built on
    top of confirmed packets, not to override that.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[DispositionResult] = []

    for packet in segmented.packets:
        tag = packet_tag(pdf_path, packet)

        if tag not in decisions:
            results.append(DispositionResult(packet_tag=tag, sid=None, pending=True))
            continue

        sid = decisions[tag]

        if sid is None:
            results.append(DispositionResult(packet_tag=tag, sid=None, pending=False))
            continue

        if sid not in roster:
            raise ValueError(f"{tag}: decision names sid {sid!r}, not on roster")
        if packet.issues:
            raise ValueError(f"{tag}: refusing to process a packet with unresolved issues: {packet.issues}")

        entry = roster.by_sid[sid]
        out_path = output_path(out_dir, entry)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_lines = [f"SID: {entry.sid}", f"PD: {entry.period_display}"]
        redact_result = _redact_packet(pdf_path, packet, out_path, dpi=dpi, flatten=flatten, stamp_lines=stamp_lines)
        if redact_result.uncovered_group_words:
            # Geometric proof (see find_uncovered_group_words) that the
            # redaction rectangles didn't actually cover real Group-row
            # ink -- independent of, and checked before, the text-based
            # verify pass below, since this is exactly the class of leak
            # (real ink, OCR-garbled into a non-matching token) that a
            # text check alone can miss.
            out_path.unlink()
            raise RuntimeError(
                f"{tag}: uncovered group-row ink, output deleted: {redact_result.uncovered_group_words}"
            )
        findings = verify_no_leaked_names(out_path, roster)
        if findings:
            # The verify pass exists precisely so this can't happen
            # silently -- never leave a leaking file sitting in out_dir.
            out_path.unlink()
            raise RuntimeError(f"{tag}: verify_no_leaked_names found leaks, output deleted: {findings}")

        results.append(DispositionResult(packet_tag=tag, sid=sid, pending=False, out_path=out_path))

    # Reconciliation: enforce "present in the output tree" iff "has a
    # confirmed, approved SID" directly, rather than trying to track which
    # packet_tag used to own which now-stale SID file. `roster` is always
    # already narrowed to one teacher+period block (see roster.py), so
    # every SID any decision for this pdf can legally name -- past or
    # present -- lands in this one directory; sweeping it against the
    # *current* decisions.json values catches a rejected packet's old file
    # and a corrected packet's superseded file the same way, with no extra
    # state to keep in sync.
    if roster.entries:
        first = roster.entries[0]
        target_dir = out_dir / first.teacher_code / first.period_display
        if target_dir.exists():
            approved_sids = {v for v in decisions.values() if v is not None}
            for existing in sorted(target_dir.glob("*.pdf")):
                if existing.stem not in approved_sids:
                    existing.unlink()
                    results.append(
                        DispositionResult(packet_tag=None, sid=existing.stem, pending=False, deleted_path=existing)
                    )

    return results
