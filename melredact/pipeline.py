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

**A packet can also land in a fourth state that isn't part of the
`decisions` contract at all: a consent hold.** roster.py's `held_names`
covers a student who is genuinely consented but whose SID can't be
trusted (e.g. a corrupted, duplicated SID run in the source export). For a
*pending* packet (tag absent from `decisions`), `run_dispositions` checks
whether match.py's `propose` says this packet's single best-scoring match
overall is a held name rather than a roster candidate
(`MatchProposal.is_held_match`) -- if so, the packet is reported as
`DispositionResult.consent_hold=True` instead of `pending=True`, is fully
redacted (to a scratch file that's discarded either way, never `out_dir`)
to prove the redaction geometry itself is sound, and is never written to
`out_dir` and never deleted, since there's no SID to name a file with and
no confirmed non-consent to act on. This check only ever applies to a
still-pending tag: a human who has already recorded an explicit decision
for that tag (a real SID via the review UI's roster search, or an explicit
non-consent rejection) has already looked at the packet and made a call
that should win over an automatic name-similarity signal, in either
direction.

**A packet whose decision names a SID can still fail to redact safely --
that packet is held back, the rest of the run is not.** `DispositionResult.
held_back` (with a human-readable `reason`) covers: the named SID isn't on
the roster, the packet still has unresolved segmentation `issues`,
`detect_header_band` couldn't confidently locate this page's own border,
`find_uncovered_group_words` found Group-row ink the redaction boxes
missed, or `verify_no_leaked_names` found a leak in the written file. Each
of these was previously a raised exception that aborted `run_dispositions`
entirely -- which meant one packet with a bad header (real incident,
2026-07-20: SID 0204150204's page, where OCR simply didn't find the
printed "Group" label) blocked every *other* already-reviewed, already-
approved packet in the same file from being written, even though their
own redaction was completely unaffected. `run_dispositions` now catches
each of these per packet, appends a `held_back` result with `reason` set,
and moves on to the next packet -- a held-back packet produces no output
(any partially-written file for it is deleted, same as before) and leaves
any *prior* output for that tag untouched (a human hasn't confirmed a
replacement is safe, so nothing about the old file changes). It remains
exactly what CLAUDE.md's "Non-negotiable design decisions" calls for --
abstain-and-flag, never silently guess -- just scoped to the one packet
that actually has the problem instead of the whole run.

**One of these five holds is human-overridable; the other four are not
(2026-07-21).** SID 0204150204 exposed a gap in the fix above: it holds
back every packet in that state forever, with no way for a human to ever
release it, even after visually confirming (in review_app.py's own preview,
which draws the exact fallback/anchor-derived box `detect_header_band`
computed even when `detected=False`) that the box fully covers the name.
That's backwards -- the entire reason a low-confidence packet routes to a
human is so a person can make the call the geometry alone couldn't, and an
unreleasable hold makes review decorative for exactly the packets that
need it most. But this is true of exactly one of the five hold reasons:
"header border not confidently detected" is a *confidence* question about
otherwise-real geometry a human can look at and judge -- it is not true of
the other four (unknown SID, unresolved segmentation issues,
`find_uncovered_group_words` finding actual uncovered ink in the pixels, or
`verify_no_leaked_names` finding an actual leak in the written text layer),
which are findings of an actual problem, not a confidence gap, and staying
non-overridable is exactly the point of them existing at all.

`run_dispositions` takes a `detection_overrides: set[str]` of packet_tags a
human has explicitly approved for release from *only* the detection-
confidence hold (see `overrides_path`/`load_detection_overrides`/
`save_detection_overrides` below -- a separate per-(pdf, decisions_dir)
file, not a richer `decisions` value, since `decisions`' sid|None|absent
three-state contract is depended on by every test and every existing
`decisions/*.json` file already on disk). When `tag` is in
`detection_overrides`, an undetected-border result falls through instead of
deleting `out_path` and holding back -- but falls through *into* the
uncovered-group-words and `verify_no_leaked_names` checks, which still run
unconditionally and still hold back (un-overridably) if either finds a
real problem. The override only ever answers "is this page's border
confidently located", never "did anything actually leak" -- those are
different questions with different answers, and only the first one is a
human's call to make. A packet written this way still carries a `reason`
noting the override, so it's visible in review_app.py's and cli.py's
output, not silently indistinguishable from a clean, confidently-detected
write.

**find_uncovered_group_words' finding is advisory, not a hold (2026-08-14).**
Real-data evidence, gathered while sizing up review_app.py's general
per-packet editor (see its own module docstring): across 41 real packets
this check held back on two teachers (020415, 010406), rendering every
single flagged region confirmed it was printed body text near the header
border -- zero genuine uncovered handwriting. Meanwhile, the reviewer now
opens every packet's own multi-page editor regardless of whether this
check fires (see CLAUDE.md's "From detection-gates-workflow to human-
reviews-everything" section) -- a geometric proof with a 0/41 real-world
true-positive rate is more useful as something to point the reviewer's
eyes at than as a gate nothing on real data can ever cleanly pass. A
non-empty `redact_result.uncovered_group_words` is carried onto the
written `DispositionResult`/`ManualReleaseResult` as `advisory_uncovered_
words`; it never queues the packet, never blocks a write, and is never
consulted by `detection_overrides` (there is nothing to override -- it was
never a hold to begin with). The other three unconditional checks
(detection confidence, consensus-ink anomaly, verify_no_leaked_names) are
completely unaffected by this change and still hold exactly as documented
below.

Packet identity across runs of the *same* source PDF is grounded in the
packet's first physical page index (see `packet_tag`), not its position in
the packets list (shifts if an earlier packet's page count changes) or a
generated SID (doesn't exist before a decision is made).

**Output layout is `out/<teacher_code>/<period>/<worksheet_type>/<SID>.pdf`,
one file per (worksheet_type, SID) pair, not per packet_tag** (John,
2026-07-18; worksheet_type segment added 2026-07-20). The load-bearing
invariant is: "present in the output tree" iff "has a confirmed, approved
SID" -- non-consented and pending packets are never in the tree under any
name, including a placeholder.

**Two worksheet types for the same student share the same teacher_code and
period** (both are read off the SID alone -- see roster.py), so without a
worksheet_type segment in the path, an MPR and a PRT packet for the same
student would collide on the exact same `<SID>.pdf`, and whichever gets
redacted+written *second* would silently overwrite the first (a real
incident: an MPR run's 11 approved outputs were clobbered by a later PRT
run). `worksheet_type` is read off each packet's own header-page footer
(`segment.read_footer`/`Packet.worksheet_type`, e.g. "PRT (01/2024)" vs.
"pcMEL MPR+ADR (06/2025)" -- both real, distinct forms) -- never guessed or
defaulted, same as `declared_total`; a header page whose footer
worksheet-type label can't be parsed is flagged as an `issues` packet, same
as an unreadable page marker, and refused by `run_dispositions` the same
way.

**Deletion is ledger-based and per-tag, never a whole-directory sweep.** An
earlier design reconciled a *shared* directory (`out/<teacher>/<period>/`)
against only the current pdf's `decisions` values at the end of every run --
safe only under the unstated assumption that exactly one pdf/decisions-store
ever writes into that directory. Once two different scan files (an MPR scan
and a PRT scan for the same class) both wrote into what was, pre-
worksheet_type-segment, the *same* directory, that assumption broke:
running dispositions for a PRT file -- even with every PRT packet still
pending, no decision at all -- swept the directory against PRT's own
(mostly empty) `decisions.values()` and deleted the *other* file's already-
approved MPR output, since nothing in the sweep logic scoped it to "files
this pdf's own decisions store actually produced." Adding the
worksheet_type path segment closes the immediate collision, but the sweep
itself was the deeper bug: nothing should ever delete a file this run's own
decisions didn't explicitly say to.

The fix: `run_dispositions` persists a small per-(out_dir, pdf) ledger
(`ledger_path`/`_load_ledger`/`_save_ledger`, colocated under
`out_dir/.ledger/<pdf-stem>.json`, not `decisions_dir` -- it's bookkeeping
about what *this* output tree contains, not a human-editable decision) of
`packet_tag -> last SID successfully written for it`. A deletion now only
ever happens for a `packet_tag` this same pdf's own ledger says it
previously wrote a file for, when *this run's own* decision for that exact
tag says the old SID is no longer correct -- either an explicit `None`
(confirmed non-consent: the ledger's old SID's file is removed) or a
different SID (a correction: the ledger's *old* SID's file is removed once
the *new* SID's file is confirmed written). A pending tag (absent from
`decisions`) is never consulted against the ledger at all -- pending can
never trigger a delete, of its own output or anyone else's, since nothing
about processing it touches any path but its own. Deletion is now strictly
a function of an explicit decision for a *specific* tag this run actually
decided, never a background reconciliation against directory contents that
might belong to an entirely different pdf.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from melredact.blocks import (
    UNDATED_ROUND,
    RoundGroup,
    collect_packet_dates,
    collect_packet_rounds,
    format_round_report,
    group_into_rounds,
    round_disagreeing_tags,
    round_labels_by_tag,
)
from melredact.config import HEADER_SEARCH_MAX_TOP, MIN_MARGIN, MIN_SCORE, RENDER_DPI_FINAL
from melredact.consensus import AnomalyHold, analyze_consensus_anomalies
from melredact.match import HeldCandidate, MatchProposal, assign_all, propose
from melredact.pdfio import open_pdf
from melredact.redact import (
    Bbox,
    HeaderBand,
    detect_header_band,
    find_uncovered_group_words,
    redact_bboxes_for_band,
    verify_no_leaked_names,
)
from melredact.redact import redact_packet as _redact_packet
from melredact.roster import Roster, RosterEntry
from melredact.segment import (
    Packet,
    SegmentResult,
    extract_header_fields,
    header_row_height,
    locate_header_anchors,
    page_words,
    segment_pdf,
)

DECISIONS_DIR = Path("decisions")
OUT_DIR = Path("out")
MANUAL_QUEUE_DIRNAME = ".manual_queue"

# A worksheet-type collision can still happen *within* one worksheet type,
# once one teacher's students legitimately complete the same worksheet type
# more than once (teacher 010406: several PRT sessions, one per topic). Each
# session is its own scan file named <teacher>_PD<n>_<TYPE>[_<TOPIC>].pdf, so
# the topic -- read from the *filename*, not the footer, since a topic isn't
# part of the worksheet's own printed content -- disambiguates sessions the
# same way worksheet_type already disambiguates worksheet types. NO_TOPIC is
# a stable literal, not an omitted segment, so every teacher's output sits at
# the same path depth regardless of whether their filenames carry a topic.
NO_TOPIC = "NA"
_TOPIC_FROM_FILENAME = re.compile(r"^[^_]+_PD\d+_[^_]+_([A-Za-z0-9]+)$", re.IGNORECASE)

# Same literal blocks.round_label() returns for a packet whose own date
# couldn't be confidently parsed (blocks.UNDATED_ROUND) -- reused here as
# output_path's default `round_label` so a caller that doesn't care about
# rounds (most direct callers outside run_dispositions/release_from_
# manual_queue, which always compute and pass a real one) still gets a
# stable, constant-depth path rather than an omitted segment.
NO_ROUND = UNDATED_ROUND


def packet_tag(pdf_path: str | Path, packet: Packet) -> str:
    return f"{Path(pdf_path).stem}_p{packet.page_indices[0]:03d}"


def topic_from_filename(pdf_path: str | Path) -> str:
    """Best-effort topic code from a source scan's own filename, e.g.
    "010406_PD1_PRT_EW.pdf" -> "EW". Returns NO_TOPIC, never raises, when the
    filename doesn't carry a fourth underscore-separated segment (every
    teacher except the ones with per-topic worksheets) or doesn't match the
    expected <teacher>_PD<n>_<TYPE>[_<TOPIC>] shape at all (e.g. the older
    "Hannel MPR PD2.pdf" naming) -- a missing topic is the overwhelmingly
    common case, not a data problem to fail loudly over."""
    m = _TOPIC_FROM_FILENAME.match(Path(pdf_path).stem)
    return m.group(1).upper() if m else NO_TOPIC


def output_path(
    out_dir: str | Path, entry: RosterEntry, worksheet_type: str, topic: str = NO_TOPIC, round_label: str = NO_ROUND
) -> Path:
    """Where a confirmed packet for this roster entry lands:
    out/<teacher_code>/<period>/<worksheet_type>/<topic>/<round>/<SID>.pdf.
    `entry.teacher_code` and `entry.period_display` are the SID's own digits
    (positions 0:6 and 6:8), not anything read off the packet, so those two
    segments are stable and derivable from the SID alone. `worksheet_type`
    is *not* derivable from the SID -- a student has one SID but multiple
    worksheet types (MPR, PRT, ...) -- so it must come from the packet's own
    footer (see Packet.worksheet_type); omitting it is exactly the bug that
    let an MPR and a PRT packet for the same student collide on one path.
    `topic` (see topic_from_filename) defaults to NO_TOPIC so every caller
    that hasn't been updated for per-topic worksheets keeps computing the
    exact same path as before that segment existed.

    `round_label` (see blocks.group_into_rounds/round_labels_by_tag) is the
    same story one segment deeper: a student can legitimately complete the
    *same* worksheet+topic more than once, in different collection sessions
    (the real motivating file, 010406_PD1_PRT.pdf, is three concatenated
    PRT administrations of the same class), and without a round segment
    those sessions collide on one path the same way an MPR/PRT collision
    used to. Defaults to NO_ROUND so a caller that genuinely has no round
    information at all still gets a stable, constant-depth path."""
    return (
        Path(out_dir)
        / entry.teacher_code
        / entry.period_display
        / worksheet_type
        / topic
        / round_label
        / f"{entry.sid}.pdf"
    )


def filter_packets_by_round(
    pdf_path: str | Path, segmented: SegmentResult, round_labels: dict[str, str], round_label: str
) -> SegmentResult:
    """Restrict a SegmentResult to just the packets belonging to one round
    group (see blocks.group_into_rounds) -- the mechanism behind --round,
    for piloting or otherwise scoping a run to a single collection session
    inside a larger concatenated scan (the real motivating file,
    010406_PD1_PRT.pdf, is three concatenated PRT administrations).

    Round grouping is necessarily whole-file (see blocks.py's own module
    docstring on why grouping trusts a contiguous run, not a single
    packet's own date) -- this filters *after* segmentation and round
    grouping have already run over the full file, rather than trying to
    segment only part of it. Every caller downstream of this (propose_all,
    assign_all, run_dispositions, analyze_redaction_holds) only ever
    iterates `segmented.packets`, so a packet excluded here is not scored,
    not matched, not redacted, not written, and never looked up in the
    ledger -- an earlier or later round's already-shipped output is never
    touched by a run scoped to a different round, since nothing about
    processing an excluded packet's tag ever runs at all.
    """
    kept = [p for p in segmented.packets if round_labels.get(packet_tag(pdf_path, p), UNDATED_ROUND) == round_label]
    return SegmentResult(packets=kept, page_count=segmented.page_count)


def ledger_path(out_dir: str | Path, pdf_path: str | Path) -> Path:
    """Where run_dispositions persists, for this (out_dir, source pdf) pair,
    which SID and exact path it last wrote output to under each packet_tag
    -- see the module docstring's "Deletion is ledger-based" section.
    Colocated under out_dir itself (a hidden `.ledger` subdirectory), not
    decisions_dir: this is bookkeeping about what's actually sitting in
    *this* output tree, derived, not a human-editable decision."""
    return Path(out_dir) / ".ledger" / f"{Path(pdf_path).stem}.json"


def _load_ledger(out_dir: str | Path, pdf_path: str | Path) -> dict[str, dict]:
    """Each entry is {"sid": ..., "path": ...} -- the literal path this tag
    last wrote to, not just the SID. Storing the path (not recomputing it
    from the SID at delete time) is what lets deletion correctly target a
    suffixed file (see _claim_output_path) rather than the un-suffixed path
    a fresh recomputation would guess."""
    path = ledger_path(out_dir, pdf_path)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_ledger(out_dir: str | Path, pdf_path: str | Path, ledger: dict[str, dict]) -> None:
    path = ledger_path(out_dir, pdf_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True))


def _claim_output_path(
    ledger: dict[str, dict],
    tag: str,
    out_dir: str | Path,
    entry: RosterEntry,
    worksheet_type: str,
    topic: str,
    round_label: str,
) -> tuple[Path, str | None]:
    """The natural output path for this packet, unless that exact path
    already exists on disk *and* this run's own ledger attributes it to a
    different packet_tag -- in which case a numbered-suffix alternative
    (`<SID>_2.pdf`, `_3.pdf`, ...) is returned instead, so one packet's
    output can never silently replace another's. This is a backstop for the
    case the topic and round path segments (see output_path) don't fully
    disambiguate on their own: two distinct packets in the *same* scan
    file, decided to the same student, worksheet type, *and* round (e.g.
    two packets whose own dates both landed in the same round group by
    honest majority vote, but are nonetheless different physical packets).
    Re-running the same tag against its own previously-claimed path is not
    a collision -- the ledger lookup excludes `tag` itself, so a packet
    re-processed after a decision change keeps overwriting its own prior
    file exactly as before.
    """
    base_path = output_path(out_dir, entry, worksheet_type, topic, round_label)
    owner_tag = next((t for t, e in ledger.items() if t != tag and e.get("path") == str(base_path)), None)
    if owner_tag is None or not base_path.exists():
        return base_path, None

    n = 2
    while True:
        candidate = base_path.with_name(f"{base_path.stem}_{n}{base_path.suffix}")
        if not candidate.exists():
            note = (
                f"{base_path} is already this run's output for packet {owner_tag!r} -- "
                f"wrote {candidate.name} instead of silently overwriting it"
            )
            return candidate, note
        n += 1


def manual_queue_dir(out_dir: str | Path, pdf_path: str | Path) -> Path:
    """Where a held-back packet's drafted (not-safe-to-ship) redaction
    attempt waits for a human to fix by hand -- see the module docstring's
    "The manual-redaction queue is a backstop" section. Colocated under
    `out_dir` (a hidden `.manual_queue` subdirectory, same gitignored tree
    as `out/` itself, never synced) since a queued draft can be exactly as
    unsafe as whatever held it back in the first place."""
    return Path(out_dir) / MANUAL_QUEUE_DIRNAME / Path(pdf_path).stem


def manual_queue_draft_path(out_dir: str | Path, pdf_path: str | Path, tag: str) -> Path:
    return manual_queue_dir(out_dir, pdf_path) / f"{tag}.pdf"


def manual_queue_meta_path(out_dir: str | Path, pdf_path: str | Path, tag: str) -> Path:
    return manual_queue_dir(out_dir, pdf_path) / f"{tag}.json"


def _queue_for_manual_redaction(
    out_dir: str | Path,
    pdf_path: str | Path,
    tag: str,
    sid: str,
    worksheet_type: str,
    reason: str,
    drafted_path: Path,
    flagged_regions: dict[int, list] | None = None,
) -> None:
    """Move (never copy) a held-back packet's already-drawn draft into the
    manual-redaction queue instead of just deleting it -- the backstop a
    human needs to actually see what's wrong and fix the geometry by hand,
    rather than starting over from nothing. Moved, not copied: the draft
    can be exactly as unsafe as the reason it was held back for (that's the
    whole reason it's here), so it must exist in at most one place on
    disk, never both the queue and (however briefly) somewhere else.

    `flagged_regions` (packet page offset -> list of Bbox), when given, is
    persisted into the queue entry's own metadata so review_app.py's manual
    editor can seed the exact flagged region directly on the correct page --
    see consensus.py's AnomalyHold, the only current caller that passes
    this. Absent for the other two queueable hold reasons (undetected
    header border, uncovered group-row ink), which already show a human
    where to look via the header page's own seeded rectangles."""
    qdir = manual_queue_dir(out_dir, pdf_path)
    qdir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(drafted_path), manual_queue_draft_path(out_dir, pdf_path, tag))
    meta = {
        "packet_tag": tag,
        "sid": sid,
        "worksheet_type": worksheet_type,
        "reason": reason,
        "pdf_path": str(pdf_path),
        "flagged_regions": (
            {str(offset): [list(b) for b in boxes] for offset, boxes in flagged_regions.items()}
            if flagged_regions
            else None
        ),
    }
    manual_queue_meta_path(out_dir, pdf_path, tag).write_text(json.dumps(meta, indent=2))


def list_manual_queue(out_dir: str | Path) -> list[dict]:
    """Enumerate every packet currently sitting in the manual-redaction
    queue, across every source pdf under this out_dir -- metadata only
    (packet_tag, sid, worksheet_type, reason, pdf_path), not the drafted
    PDFs themselves, so a caller (review_app.py, a headless script) can see
    what needs a human's attention without opening every file."""
    root = Path(out_dir) / MANUAL_QUEUE_DIRNAME
    if not root.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(root.glob("*/*.json"))]


def _clear_manual_queue_entry(out_dir: str | Path, pdf_path: str | Path, tag: str) -> None:
    manual_queue_draft_path(out_dir, pdf_path, tag).unlink(missing_ok=True)
    manual_queue_meta_path(out_dir, pdf_path, tag).unlink(missing_ok=True)


def _overlaps_bbox_pair(a: Bbox, b: Bbox) -> bool:
    """bbox-vs-bbox overlap, the same test redact._overlaps_bbox applies to
    a word against a redaction rectangle, generalized to two rectangles --
    used to check a human-drawn region actually reaches a flagged
    consensus-ink bbox (see release_from_manual_queue's
    `flagged_regions_to_verify`)."""
    al, at, ar, ab = a
    bl, bt, br, bb = b
    return not (ar <= bl or al >= br or ab <= bt or at >= bb)


@dataclass
class ManualReleaseResult:
    packet_tag: str
    sid: str
    released: bool
    out_path: Path | None = None
    reason: str | None = None
    # Advisory only, same as DispositionResult.advisory_uncovered_words --
    # never refuses a release on its own (see CLAUDE.md's "From detection-
    # gates-workflow to human-reviews-everything" section).
    advisory_uncovered_words: list = field(default_factory=list)
    geometry_source: str = "manual"


def release_from_manual_queue(
    pdf_path: str | Path,
    packet: Packet,
    tag: str,
    sid: str,
    roster: Roster,
    band_override: HeaderBand | None = None,
    *,
    out_dir: str | Path = OUT_DIR,
    dpi: int = RENDER_DPI_FINAL,
    flatten: bool = False,
    round_label: str | None = None,
    header_bbox_override: tuple[Bbox, Bbox] | None = None,
    extra_page_regions: dict[int, list[Bbox]] | None = None,
    flagged_regions_to_verify: dict[int, list[Bbox]] | None = None,
    decisions_dir: str | Path = DECISIONS_DIR,
    orientation_overrides: dict[int, int] | None = None,
) -> ManualReleaseResult:
    """The human side of the manual-redaction queue: re-redacts `packet`
    using a human-supplied, corrected `band_override` instead of whatever
    `detect_header_band` computed automatically, then re-runs the exact
    same two unconditional checks every other write goes through --
    `find_uncovered_group_words` and `verify_no_leaked_names`. This is
    deliberately a backstop for a genuine automated-check miss, not a way
    around the check itself (see CLAUDE.md's "the manual-redaction queue is
    a backstop, not a substitute" section): only a redaction that *still*
    passes both checks with the corrected geometry gets written to the
    real out/ tree and cleared from the queue. A packet that still fails
    either check with the human's own corrected geometry stays queued, with
    no file written anywhere -- the automated checks always have the final
    say, regardless of who supplied the geometry.

    Goes through the same `_claim_output_path` collision check as an
    ordinary write in `run_dispositions` -- a manually-released packet is
    just as capable of colliding with another packet's already-claimed
    output path as an automatic one.

    `round_label` (see blocks.group_into_rounds) is normally left to be
    computed here -- this function re-segments and re-reads dates for the
    whole file to get it, since a lone queued packet has no group context
    of its own to derive a round from. This is a comparatively rare,
    human-driven action (clicking "Release to out/" in the manual queue
    panel), not something in a hot per-packet loop, so paying for a fresh
    `collect_packet_rounds` call here -- OCR-cached, so a warm re-run is
    cheap regardless -- is the simpler choice over threading the whole
    file's round groups through the manual-queue call chain. A caller that
    already has it (none currently do) can still pass it directly.

    `header_bbox_override`/`extra_page_regions` are the drag-corner
    editor's geometry (see redact_packet's own docstring for what each
    means) -- passed straight through to the real `redact_packet` call
    below, which still runs both unconditional checks against them
    unconditionally, same as `band_override`. Once a release actually
    succeeds with either of them set, the geometry is persisted via
    `save_manual_geometry` so a later run hitting the same hold for the
    same packet_tag can reproduce this exact result without a human
    drawing the same boxes again (see run_dispositions' `manual_geometry`
    parameter).

    `flagged_regions_to_verify` (packet page offset -> list[Bbox], see the
    manual-queue entry's own `flagged_regions` metadata -- consensus.
    AnomalyHold's bboxes, keyed the same way `extra_page_regions` is) is
    the consensus-ink check's own re-verification: a packet queued for a
    consensus-ink anomaly must have its human-drawn `extra_page_regions`
    actually overlap every one of the flagged bboxes it was held for, same
    overlap-based coverage test `redact.find_uncovered_group_words` already
    uses for the Group row -- release refuses (packet stays queued, nothing
    written) if any flagged region is left uncovered by what was drawn,
    regardless of whether `find_uncovered_group_words`/
    `verify_no_leaked_names` happen to pass. This is what makes "draw a
    region over the flagged ink" the actual, checked resolution path for
    this hold, not merely a plausible-looking one -- the automated checks
    still have the final say, same as every other hold reason.
    """
    if sid not in roster:
        return ManualReleaseResult(packet_tag=tag, sid=sid, released=False, reason=f"sid {sid!r} not on roster")
    entry = roster.by_sid[sid]
    topic = topic_from_filename(pdf_path)
    if round_label is None:
        round_label = round_labels_by_tag(
            collect_packet_rounds(pdf_path, orientation_overrides=orientation_overrides)
        ).get(tag, UNDATED_ROUND)
    ledger = _load_ledger(out_dir, pdf_path)
    out_path, collision_note = _claim_output_path(ledger, tag, out_dir, entry, packet.worksheet_type, topic, round_label)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_lines = [f"SID: {entry.sid}", f"PD: {entry.period_display}"]
    redact_result = _redact_packet(
        pdf_path,
        packet,
        out_path,
        dpi=dpi,
        flatten=flatten,
        stamp_lines=stamp_lines,
        band_override=band_override,
        header_bbox_override=header_bbox_override,
        extra_page_regions=extra_page_regions,
        orientation_overrides=orientation_overrides,
    )
    # find_uncovered_group_words' finding is advisory only here too, same
    # as run_dispositions -- see CLAUDE.md's "From detection-gates-workflow
    # to human-reviews-everything" section. A human is drawing this exact
    # geometry by hand and looking at the live preview while doing it; a
    # geometric proof that's zero-for-41 on real held packets shouldn't
    # refuse to release what the reviewer just confirmed by eye.
    advisory_uncovered_words = redact_result.uncovered_group_words
    if flagged_regions_to_verify:
        drawn = extra_page_regions or {}
        still_uncovered = [
            (offset, bbox)
            for offset_key, bboxes in flagged_regions_to_verify.items()
            for offset in [int(offset_key)]
            for bbox in bboxes
            if not any(_overlaps_bbox_pair(bbox, drawn_bbox) for drawn_bbox in drawn.get(offset, []))
        ]
        if still_uncovered:
            out_path.unlink()
            return ManualReleaseResult(
                packet_tag=tag,
                sid=sid,
                released=False,
                reason=f"consensus-ink anomaly still not covered by a drawn region: {still_uncovered}",
            )
    findings = verify_no_leaked_names(out_path, roster)
    if findings:
        out_path.unlink()
        return ManualReleaseResult(
            packet_tag=tag, sid=sid, released=False, reason=f"verify_no_leaked_names still finds leaks: {findings}"
        )

    _clear_manual_queue_entry(out_dir, pdf_path, tag)
    ledger[tag] = {"sid": sid, "path": str(out_path)}
    _save_ledger(out_dir, pdf_path, ledger)
    if header_bbox_override is not None or extra_page_regions:
        save_manual_geometry(
            pdf_path,
            tag,
            band_override=band_override,
            header_bbox_override=header_bbox_override,
            extra_page_regions=extra_page_regions,
            decisions_dir=decisions_dir,
        )
    return ManualReleaseResult(
        packet_tag=tag,
        sid=sid,
        released=True,
        out_path=out_path,
        reason=collision_note,
        advisory_uncovered_words=advisory_uncovered_words,
    )


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


def overrides_path(pdf_path: str | Path, decisions_dir: Path = DECISIONS_DIR) -> Path:
    """Where a human's approval to release a packet from *only* the
    detection-confidence hold is recorded -- see run_dispositions'
    `detection_overrides` parameter and the module docstring's "One of
    these five holds is human-overridable" section. A separate file from
    decisions_path, not a richer decisions value: decisions' sid|None|absent
    three-state contract is depended on by every existing decisions/*.json
    file and every test that reads one, and overloading its value shape to
    also carry this is a needless way to put that at risk."""
    return Path(decisions_dir) / f"{Path(pdf_path).stem}.overrides.json"


def load_detection_overrides(pdf_path: str | Path, decisions_dir: Path = DECISIONS_DIR) -> set[str]:
    path = overrides_path(pdf_path, decisions_dir)
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def save_detection_overrides(
    pdf_path: str | Path, overrides: set[str], decisions_dir: Path = DECISIONS_DIR
) -> None:
    path = overrides_path(pdf_path, decisions_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(overrides), indent=2))


def manual_geometry_path(pdf_path: str | Path, decisions_dir: Path = DECISIONS_DIR) -> Path:
    """Where a human's manually-drawn redaction geometry (see review_app.py's
    manual-queue editor and `release_from_manual_queue`'s `header_bbox_
    override`/`extra_page_regions` parameters) is persisted per packet_tag,
    once it has actually released a packet -- so a later run of the same
    pdf that hits the same hold for the same packet can reproduce the exact
    same redaction without a human drawing the same boxes again. A separate
    file from decisions_path, same reasoning as overrides_path: decisions'
    sid|None|absent contract shouldn't also carry this."""
    return Path(decisions_dir) / f"{Path(pdf_path).stem}.manual_geometry.json"


def _serialize_geometry_entry(
    band_override: HeaderBand | None,
    header_bbox_override: tuple[Bbox, Bbox] | None,
    extra_page_regions: dict[int, list[Bbox]] | None,
) -> dict:
    return {
        "band_override": (
            {"left": band_override.left, "top": band_override.top, "right": band_override.right, "bottom": band_override.bottom}
            if band_override is not None
            else None
        ),
        "header_bbox_override": [list(b) for b in header_bbox_override] if header_bbox_override else None,
        "extra_page_regions": (
            {str(offset): [list(b) for b in boxes] for offset, boxes in extra_page_regions.items()}
            if extra_page_regions
            else None
        ),
    }


def _deserialize_geometry_entry(entry: dict) -> dict:
    band = entry.get("band_override")
    header_bboxes = entry.get("header_bbox_override")
    extra = entry.get("extra_page_regions")
    kwargs: dict = {}
    if band:
        kwargs["band_override"] = HeaderBand(
            left=band["left"], top=band["top"], right=band["right"], bottom=band["bottom"], detected=True
        )
    if header_bboxes:
        kwargs["header_bbox_override"] = tuple(tuple(b) for b in header_bboxes)
    if extra:
        kwargs["extra_page_regions"] = {int(offset): [tuple(b) for b in boxes] for offset, boxes in extra.items()}
    return kwargs


def load_manual_geometry(pdf_path: str | Path, decisions_dir: Path = DECISIONS_DIR) -> dict[str, dict]:
    """packet_tag -> kwargs ready to splat into redact_packet (band_override/
    header_bbox_override/extra_page_regions, whichever were actually
    recorded) -- already deserialized from JSON's lists back into the
    tuples redact_packet expects. An absent file (the overwhelmingly common
    case: no packet in this pdf has ever needed a manual correction) returns
    an empty dict, same convention as load_detection_overrides."""
    path = manual_geometry_path(pdf_path, decisions_dir)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {tag: _deserialize_geometry_entry(entry) for tag, entry in raw.items()}


def save_manual_geometry(
    pdf_path: str | Path,
    tag: str,
    *,
    band_override: HeaderBand | None = None,
    header_bbox_override: tuple[Bbox, Bbox] | None = None,
    extra_page_regions: dict[int, list[Bbox]] | None = None,
    decisions_dir: Path = DECISIONS_DIR,
) -> None:
    """Records this tag's manually-supplied geometry, merged into whatever
    was already recorded for other tags in this pdf -- called by
    `release_from_manual_queue` only once a release actually succeeds
    (there is nothing worth reproducing about geometry that still failed
    both unconditional checks)."""
    path = manual_geometry_path(pdf_path, decisions_dir)
    all_geometry = json.loads(path.read_text()) if path.exists() else {}
    all_geometry[tag] = _serialize_geometry_entry(band_override, header_bbox_override, extra_page_regions)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(all_geometry, indent=2, sort_keys=True))


def orientation_overrides_path(pdf_path: str | Path, decisions_dir: Path = DECISIONS_DIR) -> Path:
    """Where a human's per-page rotation choice is recorded -- see
    orientation.py's detect-and-ask design and review_app.py's rotate
    controls. A separate file from decisions_path, same reasoning as
    overrides_path/manual_geometry_path: decisions' sid|None|absent
    three-state contract shouldn't also carry this. Keyed by page_index
    (int, JSON-serialized as a string key) -> applied angle
    (0/90/180/270), always winning over the detector's own guess for that
    page, whether confirming it, correcting it, or rotating a page the
    detector never flagged at all."""
    return Path(decisions_dir) / f"{Path(pdf_path).stem}.orientation.json"


def load_orientation_overrides(pdf_path: str | Path, decisions_dir: Path = DECISIONS_DIR) -> dict[int, int]:
    path = orientation_overrides_path(pdf_path, decisions_dir)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {int(k): int(v) % 360 for k, v in raw.items()}


def save_orientation_overrides(
    pdf_path: str | Path, overrides: dict[int, int], decisions_dir: Path = DECISIONS_DIR
) -> None:
    path = orientation_overrides_path(pdf_path, decisions_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({str(k): int(v) % 360 for k, v in overrides.items()}, indent=2, sort_keys=True))


def propose_all(
    pdf_path: str | Path,
    segmented: SegmentResult,
    roster: Roster,
    *,
    orientation_overrides: dict[int, int] | None = None,
) -> list[MatchProposal]:
    """One ranked candidate list per packet, keyed by the stable packet_tag
    rather than segment.py's positional packet_index, so callers (the
    review UI, run_dispositions) can key off something that survives
    across runs. Orphan packets (no header page -- see segment.py) have no
    Name field to score at all, so they always abstain with an empty
    candidate list rather than being skipped. `orientation_overrides` is
    pure plumbing through to `open_pdf` -- see orientation.py's
    detect-and-ask design; it never changes match.py's own scoring."""
    proposals = []
    with open_pdf(pdf_path, orientation_overrides=orientation_overrides) as pdf:
        for packet in segmented.packets:
            tag = packet_tag(pdf_path, packet)
            if packet.header_page_index is None:
                proposals.append(MatchProposal(packet_tag=tag, candidates=[]))
                continue
            fields = extract_header_fields(pdf.pages[packet.header_page_index])
            proposals.append(propose(tag, fields.name_text, roster))
    return proposals


def _held_match_for_packet(
    pdf_path: str | Path, packet: Packet, roster: Roster, *, orientation_overrides: dict[int, int] | None = None
) -> HeldCandidate | None:
    """Re-runs match.py's scoring for one packet (the same name_text/
    propose call propose_all already does for every packet during review)
    to answer one narrow question: is this packet's single best match, out
    of both the roster and the held names, actually a held name? Orphan
    packets (no header page, nothing to score) never match a held name.
    Cheap to call per-packet even outside review_app.py's caching, since
    extract_header_fields' own OCR call is disk-cached (see CLAUDE.md's
    "OCR is disk-cached" section) -- this isn't a second expensive pass."""
    if packet.header_page_index is None:
        return None
    with open_pdf(pdf_path, orientation_overrides=orientation_overrides) as pdf:
        fields = extract_header_fields(pdf.pages[packet.header_page_index])
    proposal = propose(packet_tag(pdf_path, packet), fields.name_text, roster)
    return proposal.top_held if proposal.is_held_match else None


def _draft_consent_hold_redaction(
    pdf_path: str | Path,
    packet: Packet,
    dpi: int,
    flatten: bool,
    *,
    orientation_overrides: dict[int, int] | None = None,
) -> None:
    """Redaction is unconditional, never dependent on whether a SID could
    ever be resolved (see CLAUDE.md) -- a consent-hold packet still gets
    fully redacted, proving the geometry is sound, even though the result
    is never written to out_dir. The draft lands in a real temporary
    directory that's removed the moment this returns, so nothing about a
    consent hold ever leaves a file sitting anywhere on disk."""
    with tempfile.TemporaryDirectory() as scratch_dir:
        _redact_packet(
            pdf_path,
            packet,
            Path(scratch_dir) / "scratch.pdf",
            dpi=dpi,
            flatten=flatten,
            orientation_overrides=orientation_overrides,
        )


@dataclass
class HoldAnalysis:
    packet_tag: str
    round_label: str
    detection_hold: bool = False
    # Advisory only, since 2026-08-14 -- see run_dispositions'
    # `advisory_uncovered_words` field and CLAUDE.md's "From detection-
    # gates-workflow to human-reviews-everything" section. Never part of
    # `clean`'s gating and never gates the consensus/leak checks below it.
    uncovered_ink_advisory: bool = False
    consensus_hold: bool = False
    leak_hold: bool = False
    reason: str | None = None

    @property
    def clean(self) -> bool:
        # uncovered_ink_advisory deliberately excluded -- it no longer
        # holds a packet back (see run_dispositions), so a packet with
        # only an advisory finding and nothing else is still "clean" in
        # the sense that a real run would ship it without human
        # intervention.
        return not (self.detection_hold or self.consensus_hold or self.leak_hold)


def analyze_redaction_holds(
    pdf_path: str | Path,
    segmented: SegmentResult,
    roster: Roster,
    round_labels: dict[str, str] | None = None,
    consensus_holds: dict[str, list[AnomalyHold]] | None = None,
    *,
    dpi: int = RENDER_DPI_FINAL,
    flatten: bool = False,
    orientation_overrides: dict[int, int] | None = None,
) -> list[HoldAnalysis]:
    """Read-only redaction analysis: reports which of run_dispositions'
    three unconditional per-packet HOLDS (detection confidence, consensus-
    ink anomaly, verify_no_leaked_names) each packet would trigger, or that
    it would pass cleanly, plus whether find_uncovered_group_words'
    ADVISORY (non-blocking since 2026-08-14, see CLAUDE.md) fires -- all
    WITHOUT ever writing to out_dir, touching decisions, the ledger, or the
    manual-redaction queue, or deleting anything. Exists so a real,
    never-before-processed file (see CLAUDE.md's bug #7 trade-off history)
    can be sized up before committing to a real run.

    Each packet's own redaction is drafted to a scratch file inside a
    TemporaryDirectory removed the instant this function returns -- the
    same pattern `_draft_consent_hold_redaction` already uses for a consent
    hold, for the same reason: prove the real geometry/leak checks actually
    ran, without ever leaving a file sitting on disk anywhere.

    Mirrors run_dispositions' own hold precedence exactly -- detection
    confidence checked first, then consensus-ink anomaly, then a full
    verify_no_leaked_names pass, each gating the next the same way
    run_dispositions' `continue`s do -- rather than checking all three
    independently, so a packet reported here as "held for detection
    confidence" is the same packet a real run (absent a detection_
    overrides entry for it) would actually hold for that reason, not a
    looser union of every check that happens to fire on it. The uncovered-
    ink advisory is recorded independently of this precedence chain, since
    it never gates anything.

    `consensus_holds` (see consensus.analyze_consensus_anomalies) is
    computed once for the whole file, the same way `round_labels` already
    is -- left as None, it's computed here from `segmented`; a caller
    (cli.py's analyze command) that's already computed it for its own
    report should pass it through rather than paying for the group
    alignment pass twice.

    Orphan packets (no header page) have nothing to redact and are
    skipped -- a real run would refuse them via `packet.issues` before ever
    reaching redaction, so they carry no redaction-hold signal to report
    here. `roster` should be scoped the same way a real run's would be
    (see roster.load_roster); this function never assigns or reads a SID,
    it only needs a roster to run the same full-document leak check
    verify_no_leaked_names always runs.
    """
    results: list[HoldAnalysis] = []
    labels = round_labels or {}
    consensus = (
        consensus_holds
        if consensus_holds is not None
        else analyze_consensus_anomalies(pdf_path, segmented, orientation_overrides=orientation_overrides).holds
    )
    with tempfile.TemporaryDirectory() as scratch_dir:
        for packet in segmented.packets:
            if packet.header_page_index is None:
                continue
            tag = packet_tag(pdf_path, packet)
            scratch_path = Path(scratch_dir) / f"{tag}.pdf"
            redact_result = _redact_packet(
                pdf_path, packet, scratch_path, dpi=dpi, flatten=flatten, orientation_overrides=orientation_overrides
            )
            analysis = HoldAnalysis(packet_tag=tag, round_label=labels.get(tag, UNDATED_ROUND))
            tag_holds = consensus.get(tag, [])
            if redact_result.uncovered_group_words:
                analysis.uncovered_ink_advisory = True
            if redact_result.band is not None and not redact_result.band.detected:
                analysis.detection_hold = True
                analysis.reason = f"header border not confidently detected: {redact_result.band}"
            elif tag_holds:
                analysis.consensus_hold = True
                analysis.reason = "; ".join(h.reason for h in tag_holds)
            else:
                findings = verify_no_leaked_names(scratch_path, roster)
                if findings:
                    analysis.leak_hold = True
                    analysis.reason = f"verify_no_leaked_names found leaks: {findings}"
            scratch_path.unlink(missing_ok=True)
            results.append(analysis)
    return results


def format_hold_analysis_report(results: list[HoldAnalysis]) -> str:
    """Per-round-group summary of analyze_redaction_holds' output: how many
    packets would be held for each reason, and how many would pass
    cleanly, grouped and sorted the same way blocks.format_round_report
    groups its own table, for a human comparing the two side by side
    before deciding whether to actually run anything."""
    by_round: dict[str, list[HoldAnalysis]] = {}
    for r in results:
        by_round.setdefault(r.round_label, []).append(r)
    lines = ["Redaction hold analysis (read-only -- nothing written, redacted to disk, or deleted):"]
    for label in sorted(by_round):
        group = by_round[label]
        n_detection = sum(1 for r in group if r.detection_hold)
        n_ink = sum(1 for r in group if r.uncovered_ink_advisory)
        n_consensus = sum(1 for r in group if r.consensus_hold)
        n_leak = sum(1 for r in group if r.leak_hold)
        n_clean = sum(1 for r in group if r.clean)
        lines.append(
            f"  {label}: {len(group)} packet(s) -- {n_clean} clean, {n_detection} detection-confidence "
            f"hold(s), {n_consensus} consensus-ink anomaly hold(s), {n_leak} leak hold(s), "
            f"{n_ink} carrying a non-blocking uncovered-ink advisory"
        )
    return "\n".join(lines)


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
    # True for a packet whose decision names a SID but couldn't be safely
    # redacted this run (header border not confidently detected, group-row
    # ink left uncovered, a leak found, the named SID not on the roster, or
    # unresolved segmentation issues). Held back, not written -- and
    # deliberately does NOT abort the rest of the run: one packet's
    # geometry or data problem is a reason for a human to look at *that*
    # packet, not a reason to block every other already-approved packet in
    # the same file. `reason` is a human-readable explanation, always set
    # together with held_back=True.
    held_back: bool = False
    reason: str | None = None
    # True for a still-pending packet whose single best match overall is a
    # held name (see roster.py's Roster.held_names and match.py's
    # MatchProposal.is_held_match) -- a known-consented student with an
    # unresolvable SID. Distinct from held_back: this is never a data or
    # geometry problem to fix, it's a permanent structural state -- there
    # is no decision that ever turns this packet into a write, so it's
    # never counted alongside held_back in a caller's "needs attention"
    # bucket. `reason` is set together with this, same convention as
    # held_back.
    consent_hold: bool = False
    # Set only when this packet's natural output path was already claimed
    # (per this run's own ledger) by a different packet_tag -- see
    # _claim_output_path. The write still happened, just to a numbered-
    # suffix path instead of the natural one, and this must be surfaced
    # prominently (cli.py's run summary, review_app.py's sidebar) rather
    # than looking like an ordinary clean write.
    collision_note: str | None = None
    # True whenever `allow_delete=False` suppressed a deletion this result
    # would otherwise have performed (confirmed non-consent, or a
    # correction superseding an old SID) -- see run_dispositions'
    # `allow_delete` parameter. `reason` explains what was left in place.
    # A caller must surface this explicitly (it fits none of written/
    # deleted/pending/held_back/consent_hold) rather than let it go
    # silently unreported.
    deletion_skipped: bool = False
    # find_uncovered_group_words' own finding, carried onto a WRITTEN
    # result rather than gating it (2026-08-14 -- see CLAUDE.md's "From
    # detection-gates-workflow to human-reviews-everything" section).
    # Real-data evidence: across 41 real packets this check held back on
    # two teachers, every single one was printed body text near the
    # header border, zero genuine uncovered handwriting -- and the
    # reviewer now looks at every page of every packet regardless (see
    # review_app.py's per-packet editor), so a false-positive-prone
    # geometric proof is more useful as something to point the reviewer's
    # eyes at than as a gate nothing can ever pass through cleanly on real
    # data. Non-empty means the same finding find_uncovered_group_words
    # always produced; it just no longer holds the packet back on its own
    # (a consensus-ink anomaly or a verify_no_leaked_names finding still
    # does -- this is the only one of the four unconditional checks this
    # applies to).
    advisory_uncovered_words: list = field(default_factory=list)
    # "manual" when this write applied a stored correction from
    # `manual_geometry` (see load_manual_geometry/save_manual_geometry) --
    # a human has, at some point, opened review_app.py's editor for this
    # exact packet_tag and the geometry they left behind is what produced
    # this file. "automatic" (the default) covers every packet nobody has
    # ever manually edited -- the overwhelming majority. Purely for the
    # per-run summary (see cli.py/review_app.py); never affects behavior.
    geometry_source: str = "automatic"


def run_dispositions(
    pdf_path: str | Path,
    segmented: SegmentResult,
    decisions: dict[str, str | None],
    roster: Roster,
    *,
    out_dir: str | Path = OUT_DIR,
    dpi: int = RENDER_DPI_FINAL,
    flatten: bool = False,
    detection_overrides: set[str] = frozenset(),
    round_labels: dict[str, str] | None = None,
    allow_delete: bool = True,
    manual_geometry: dict[str, dict] | None = None,
    consensus_holds: dict[str, list[AnomalyHold]] | None = None,
    orientation_overrides: dict[int, int] | None = None,
) -> list[DispositionResult]:
    """Apply final per-packet decisions. See module docstring for the
    three-state `decisions` contract -- this is where "confirmed
    non-consent" actually becomes a deletion, not just a skipped write --
    and for why output is named by SID under a worksheet_type subdirectory
    (out/<teacher>/<period>/<worksheet_type>/<SID>.pdf) rather than by
    packet_tag, and why deletion is ledger-based rather than a directory
    sweep.

    A packet with unresolved segment.py `issues` is refused even if
    `decisions` names a SID for it: those issues mean a human hasn't
    actually confirmed this packet is what its footer claims, and
    `decisions` entries are assumed to come from a review flow built on
    top of confirmed packets, not to override that. A missing/unreadable
    worksheet_type is one such issue (see segment.segment_pdf), so a
    packet ever reaches output_path below only once its worksheet_type is
    known -- never guessed.

    A packet absent from `decisions` (pending) is never looked up in the
    ledger and never touches any path but its own -- see the module
    docstring's "Deletion is ledger-based" section for why this is the
    fix for a pending packet in one pdf being able to delete another pdf's
    already-approved output.

    `detection_overrides` is a set of packet_tags a human has explicitly
    approved for release from the detection-confidence hold specifically
    (see the module docstring's "One of these five holds is human-
    overridable" section) -- it does not, and must not, affect whether the
    unrelated uncovered-group-words or verify_no_leaked_names holds fire.

    `round_labels` (packet_tag -> "YYYY-MM"|"undated", see blocks.
    group_into_rounds/round_labels_by_tag) is the round path segment for
    each packet (see output_path) -- a student can legitimately complete
    the same worksheet+topic more than once, in different collection
    sessions, and the round segment is what keeps those sessions from
    colliding in out/. Left as None (the default), it's computed here from
    `segmented` -- already paid for by the caller, so no re-segmentation --
    via a fresh date-OCR pass; a caller that's already computed it for its
    own report (cli.py, review_app.py, both of which print the round
    report before ever calling this) should pass it through directly
    rather than paying for that pass twice. Round labelling never touches
    matching, scoring, or claiming -- it is output-path metadata only.

    `allow_delete` (default True, unchanged behavior) is a blanket safety
    switch for a pilot or first-ever run against a real file that hasn't
    been through this code before: when False, every deletion this
    function would otherwise perform -- a confirmed non-consent packet's
    prior output, or a correction's stale old-SID file -- is skipped
    entirely, and the ledger entry for that tag is left exactly as it was.
    Nothing else changes: matching, redaction, and writing new output all
    proceed normally, so a pilot can still see what *would* happen on
    write, just with deletion categorically off regardless of what any
    individual decision says. The skipped deletion is still surfaced (a
    `DispositionResult.reason` noting the file that was left in place),
    never silently dropped.

    `manual_geometry` (packet_tag -> kwargs, see `load_manual_geometry`) is
    a human's previously-successful manual-queue correction for this exact
    packet_tag (see `release_from_manual_queue`'s own persistence of it) --
    applied to this run's *first* redaction attempt for that tag, not only
    on a second manual-queue pass, so a packet a human already corrected
    once reproduces the same clean write on every later run without being
    re-queued or redrawn. It never weakens either unconditional check: a
    stored geometry that no longer covers the ink (e.g. after a source
    file changed) is held back and re-queued exactly like any other
    failing geometry, with the drafted attempt available for the queue
    editor same as always. Left as None (the default), no packet in this
    run gets any geometry override beyond band_override/header_bbox_
    override/extra_page_regions this function never itself constructs.

    `orientation_overrides` (page_index -> 0/90/180/270, see orientation.py's
    detect-and-ask design and `load_orientation_overrides`/`save_
    orientation_overrides` above) is a human's explicit per-page rotation
    choice, threaded straight through to every OCR/matching/redaction call
    this function makes (and to the round/consensus passes it computes when
    not given them directly) so a confirmed or corrected rotation reaches
    every stage the same way, not just a preview. Left as None (the
    default), a page the detector couldn't confidently rotate on its own
    stays exactly as found and its packet is held via the ordinary
    `packet.issues` gate above -- never guessed.

    `consensus_holds` (packet_tag -> list[consensus.AnomalyHold], see
    consensus.analyze_consensus_anomalies) is a fourth unconditional check,
    alongside detection confidence, uncovered group-row ink, and
    verify_no_leaked_names: template-agnostic handwriting found on a
    *non-header* page that only a few packets in the group share -- ink
    redaction never reaches (it only ever touches the header page) and that
    verify_no_leaked_names cannot catch if the name isn't on the roster at
    all (see consensus.py's module docstring for the real find this closes:
    a freehand page-2 name for a student, "Ollie Maduro", who was never on
    the roster to begin with). Like uncovered_group_words, this is never
    overridable via `detection_overrides` -- it's a finding of real
    anomalous ink, not a confidence question about otherwise-sound
    geometry -- and a held packet is queued to the manual-redaction queue,
    not just deleted, since a human drawing a region over the flagged ink
    is exactly what resolves it (see review_app.py's manual editor). Left
    as None (the default), it's computed here from `segmented` via a fresh
    consensus pass; a caller that's already computed it for its own report
    (cli.py, review_app.py) should pass it through directly rather than
    paying for the group alignment pass twice -- see CLAUDE.md's "cost"
    measurement for why that pass is worth avoiding twice.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = _load_ledger(out_dir, pdf_path)
    ledger_dirty = False
    results: list[DispositionResult] = []
    topic = topic_from_filename(pdf_path)
    if round_labels is None:
        round_labels = round_labels_by_tag(
            collect_packet_rounds(pdf_path, segmented=segmented, orientation_overrides=orientation_overrides)
        )
    if consensus_holds is None:
        consensus_holds = analyze_consensus_anomalies(pdf_path, segmented, orientation_overrides=orientation_overrides).holds

    def _delete_stale_output(stale_path: Path, stale_sid: str) -> None:
        # Deliberately doesn't touch `ledger` -- callers own that, since
        # what the ledger entry should become afterward differs (removed
        # entirely for a rejection, replaced with the new SID for a
        # correction). Deletes the *literal* path this tag's ledger entry
        # recorded, not a path recomputed from the SID -- a recomputed path
        # would miss a suffixed file written by _claim_output_path.
        if stale_path.exists():
            stale_path.unlink()
            results.append(DispositionResult(packet_tag=None, sid=stale_sid, pending=False, deleted_path=stale_path))

    for packet in segmented.packets:
        tag = packet_tag(pdf_path, packet)

        if tag not in decisions:
            held = _held_match_for_packet(pdf_path, packet, roster, orientation_overrides=orientation_overrides)
            if held is not None:
                _draft_consent_hold_redaction(
                    pdf_path, packet, dpi, flatten, orientation_overrides=orientation_overrides
                )
                reason = (
                    f"best match is a held name ({held.full_name}) -- consent-known, SID unresolvable; "
                    "never auto-assigned a roster SID, never deleted"
                )
                results.append(
                    DispositionResult(packet_tag=tag, sid=None, pending=False, consent_hold=True, reason=reason)
                )
                continue
            results.append(DispositionResult(packet_tag=tag, sid=None, pending=True))
            continue

        sid = decisions[tag]
        prior_entry = ledger.get(tag)
        prior_sid = prior_entry["sid"] if prior_entry else None

        if sid is None:
            # Confirmed non-consent. Delete only the file *this exact tag*
            # previously wrote (per the ledger's own recorded path), never
            # anything else in out_dir -- this is the whole point of the
            # ledger over a directory sweep.
            if prior_entry is not None:
                if allow_delete:
                    _delete_stale_output(Path(prior_entry["path"]), prior_sid)
                    ledger.pop(tag, None)
                    ledger_dirty = True
                else:
                    results.append(
                        DispositionResult(
                            packet_tag=tag,
                            sid=None,
                            pending=False,
                            reason=f"deletion disabled for this run -- {prior_entry['path']} left in place",
                            deletion_skipped=True,
                        )
                    )
                    continue
            results.append(DispositionResult(packet_tag=tag, sid=None, pending=False))
            continue

        if sid not in roster:
            # A held-back result, not a raise -- this is a problem with
            # this one decision entry (e.g. a typo'd SID), not a reason to
            # abort every other packet's already-confirmed output in the
            # same run.
            results.append(
                DispositionResult(
                    packet_tag=tag,
                    sid=sid,
                    pending=False,
                    held_back=True,
                    reason=f"decision names sid {sid!r}, not on roster",
                )
            )
            continue
        if packet.issues:
            results.append(
                DispositionResult(
                    packet_tag=tag,
                    sid=sid,
                    pending=False,
                    held_back=True,
                    reason=f"refusing to process a packet with unresolved issues: {packet.issues}",
                )
            )
            continue

        entry = roster.by_sid[sid]
        round_label = round_labels.get(tag, UNDATED_ROUND)
        out_path, collision_note = _claim_output_path(
            ledger, tag, out_dir, entry, packet.worksheet_type, topic, round_label
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_lines = [f"SID: {entry.sid}", f"PD: {entry.period_display}"]
        geometry_kwargs = (manual_geometry or {}).get(tag, {})
        redact_result = _redact_packet(
            pdf_path,
            packet,
            out_path,
            dpi=dpi,
            flatten=flatten,
            stamp_lines=stamp_lines,
            orientation_overrides=orientation_overrides,
            **geometry_kwargs,
        )
        detection_note: str | None = None
        if redact_result.band is not None and not redact_result.band.detected:
            # detect_header_band couldn't confidently locate this page's
            # own header border (see its docstring). This is the one hold
            # reason a human can actually clear: a real box was still drawn
            # (the anchor- or fallback-derived geometry detect_header_band
            # falls back to, not a null box), and `tag in detection_overrides`
            # means a human has looked at review_app.py's own preview of
            # that exact box and confirmed it covers the name. Absent that
            # override, this is still held back exactly as before -- never
            # ship a box whose position we're not sure of with nobody
            # having looked at it. Held back, not raised, either way: this
            # page's own geometry problem shouldn't block every other
            # approved packet in the same run (a real incident -- see
            # CLAUDE.md -- was exactly this: one packet's failure nuking
            # unrelated already-approved output).
            if tag not in detection_overrides:
                reason = f"header border not confidently detected: {redact_result.band}"
                _queue_for_manual_redaction(out_dir, pdf_path, tag, sid, packet.worksheet_type, reason, out_path)
                results.append(DispositionResult(packet_tag=tag, sid=sid, pending=False, held_back=True, reason=reason))
                continue
            detection_note = f"shipped despite undetected header border (human-approved override): {redact_result.band}"
        # find_uncovered_group_words' finding is advisory only, not a hold
        # (2026-08-14 -- see CLAUDE.md's "From detection-gates-workflow to
        # human-reviews-everything" section). Real-data evidence: across 41
        # real packets this check held back on two teachers (020415,
        # 010406), every single one was printed body text near the header
        # border -- zero genuine uncovered handwriting -- and the reviewer
        # now looks at every page of every packet via review_app.py's
        # per-packet editor regardless of whether this check fires. Carried
        # onto the written result (DispositionResult.advisory_uncovered_
        # words) so a reviewer or a per-run summary can still see it; it no
        # longer queues the packet or blocks the write the way a consensus-
        # ink or verify_no_leaked_names finding still does.
        advisory_uncovered_words = redact_result.uncovered_group_words
        tag_consensus_holds = consensus_holds.get(tag, [])
        if tag_consensus_holds:
            # Template-agnostic handwriting on a non-header page that only
            # a few packets in its group share -- see consensus.py's module
            # docstring. Never overridable via detection_overrides, same
            # reasoning as uncovered_group_words above: a finding of real
            # anomalous ink, not a confidence gap. Queued, not just
            # deleted, since a human drawing a region over the flagged ink
            # is exactly what resolves it.
            reason = "; ".join(h.reason for h in tag_consensus_holds)
            flagged_by_offset: dict[int, list] = {}
            for h in tag_consensus_holds:
                flagged_by_offset.setdefault(h.page_offset, []).append(h.bbox_pt)
            _queue_for_manual_redaction(
                out_dir, pdf_path, tag, sid, packet.worksheet_type, reason, out_path, flagged_regions=flagged_by_offset
            )
            results.append(DispositionResult(packet_tag=tag, sid=sid, pending=False, held_back=True, reason=reason))
            continue
        findings = verify_no_leaked_names(out_path, roster)
        if findings:
            # The verify pass exists precisely so this can't happen
            # silently -- never leave a leaking file sitting in out_dir.
            # Never overridable, same reasoning as uncovered_group_words
            # above: an actual leak finding, not a confidence gap.
            out_path.unlink()
            results.append(
                DispositionResult(
                    packet_tag=tag,
                    sid=sid,
                    pending=False,
                    held_back=True,
                    reason=f"verify_no_leaked_names found leaks: {findings}",
                )
            )
            continue

        results.append(
            DispositionResult(
                packet_tag=tag,
                sid=sid,
                pending=False,
                out_path=out_path,
                reason=detection_note,
                collision_note=collision_note,
                advisory_uncovered_words=advisory_uncovered_words,
                geometry_source="manual" if geometry_kwargs else "automatic",
            )
        )

        # A correction (this tag's approved SID changed from a previously
        # written one) supersedes the old file -- delete it as a direct
        # consequence of *this* explicit new decision, once the new file is
        # confirmed written and clean, not as a background sweep. Deletes
        # the ledger's own recorded path, not a recomputed one, same as the
        # non-consent case above.
        if prior_entry is not None and prior_sid != sid:
            if allow_delete:
                _delete_stale_output(Path(prior_entry["path"]), prior_sid)
            else:
                stale_note = f"deletion disabled for this run -- stale {prior_entry['path']} left in place"
                results[-1].reason = f"{results[-1].reason}; {stale_note}" if results[-1].reason else stale_note
                results[-1].deletion_skipped = True
        ledger[tag] = {"sid": sid, "path": str(out_path)}
        ledger_dirty = True

    if ledger_dirty:
        _save_ledger(out_dir, pdf_path, ledger)

    return results


# ---------------------------------------------------------------------------
# Preflight: a read-only, whole-file report of everything wrong with a scan
# before any review, before `run`/`analyze`, before anything touches out_dir,
# decisions_dir, or the ledger -- meant to be runnable the moment a file
# comes off Box, so a human can decide whether to review it now or hand it
# back for a rescan/re-export without first paying for a full cold-OCR run
# and then discovering the problem 20 minutes in.
#
# Deliberately does NOT call `analyze_redaction_holds` (the per-packet, real
# `redact_packet` draft that also runs `verify_no_leaked_names`): that pass
# exists to preserve the kept text layer at write time (see CLAUDE.md's "OCR
# is disk-cached" section -- full-page OCR measured at ~29s/page, the
# dominant cost of a real run) and preflight has no text layer to preserve.
# Detection-hold and the uncovered-ink advisory only need the header page's
# own raster (`detect_header_band` -- a pixel scan, no OCR involved at all)
# plus the header-band OCR crop matching already made and disk-cached (see
# `_header_geometry_check` below) -- so preflight computes both directly, at
# a fraction of the cost, instead of drafting a full redaction per packet.
# The cost that *is* unavoidable is the same one `segment_pdf`'s own
# footer/header-label pass and `propose_all`'s own field-extraction pass
# already require on a cold cache -- small header/footer-band OCR calls, not
# full-page ones (see CLAUDE.md's "Speed finding" section: ~4.8s/call on the
# real 44-page file, vs. ~29s/page for a full-page pass) -- `elapsed_seconds`
# on the returned report says plainly whether that cost was actually paid.
#
# Every OCR/rasterize/orientation call preflight makes goes through the
# exact same disk caches (ocr.py, orientation.py, consensus.py) the real
# `segment_pdf`/`propose_all`/`analyze_consensus_anomalies`/`run_
# dispositions` calls use -- so a `cli.py run` immediately following a
# preflight run against the same file starts warm, not cold. Nothing here
# is throwaway work.
# ---------------------------------------------------------------------------


@dataclass
class OrientationFlag:
    page_index: int
    packet_tag: str | None
    page_offset: int | None  # 1-indexed within its packet, when known
    kind: str  # "unresolved" or "pending_confirmation"
    detected_angle: int
    score: float


@dataclass
class PreflightPacket:
    packet_tag: str
    packet_index: int
    header_page_index: int | None
    page_indices: list[int]
    n_pages: int
    declared_total: int | None
    page_count_mismatch: bool
    is_orphan: bool
    issues: list[str]
    worksheet_type: str | None
    round_label: str | None
    round_disagrees: bool
    top_score: float | None
    top_margin: float | None
    has_plausible_match: bool
    would_auto_assign: bool
    detection_hold: bool = False
    detection_reason: str | None = None
    uncovered_ink_advisory: bool = False
    consensus_hold: bool = False
    consensus_reason: str | None = None
    consensus_hold_pages: list[int] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """A structural problem (segmentation issue -- missing header,
        unreadable footer, page-count mismatch, unresolved/unconfirmed
        orientation) that has to be fixed or reviewed via the rotate
        controls before *anything* -- matching, redaction -- can even be
        attempted for this packet. Mirrors run_dispositions' own "a packet
        with unresolved issues is refused even if decisions names a SID
        for it" rule."""
        return bool(self.issues)

    @property
    def clean(self) -> bool:
        """No structural problem, no detection/consensus/advisory flag, and
        would auto-assign with zero human input -- a one-click "looks
        right, confirm" packet."""
        return (
            not self.blocked
            and not self.detection_hold
            and not self.consensus_hold
            and not self.uncovered_ink_advisory
            and self.would_auto_assign
        )


@dataclass
class PreflightReport:
    pdf_path: Path
    page_count: int
    packets: list[PreflightPacket]
    round_groups: list[RoundGroup]
    orientation_flags: list[OrientationFlag]
    roster_period: str | None
    roster_entry_count: int
    elapsed_seconds: float

    @property
    def n_packets(self) -> int:
        return len(self.packets)

    @property
    def n_page_count_mismatch(self) -> int:
        return sum(1 for p in self.packets if p.page_count_mismatch)

    @property
    def n_unsegmentable(self) -> int:
        return sum(1 for p in self.packets if p.is_orphan)

    @property
    def n_no_plausible_match(self) -> int:
        return sum(1 for p in self.packets if not p.is_orphan and not p.has_plausible_match)

    @property
    def n_detection_holds(self) -> int:
        return sum(1 for p in self.packets if p.detection_hold)

    @property
    def n_uncovered_ink_advisories(self) -> int:
        return sum(1 for p in self.packets if p.uncovered_ink_advisory)

    @property
    def n_consensus_holds(self) -> int:
        return sum(1 for p in self.packets if p.consensus_hold)

    @property
    def n_cannot_process(self) -> int:
        return sum(1 for p in self.packets if p.blocked)

    @property
    def n_clean(self) -> int:
        return sum(1 for p in self.packets if p.clean)

    @property
    def n_needs_editor(self) -> int:
        # Deliberately "everything else", not a second independent count --
        # every packet is in exactly one of the three verdict buckets, so
        # this guarantees n_clean + n_needs_editor + n_cannot_process ==
        # n_packets always, rather than risking two buckets silently
        # double-counting (or missing) the same packet through separately
        # maintained conditions.
        return self.n_packets - self.n_cannot_process - self.n_clean


def _header_geometry_check(page, *, dpi: int = RENDER_DPI_FINAL) -> tuple[HeaderBand, list]:
    """band + uncovered_group_words for one header page, without a full
    per-page OCR pass or a real redact_packet draft -- see the preflight
    section's own module note above for why. Mirrors redact_packet's own
    header-page logic exactly (same functions, same order, same
    header_words bbox/dpi any matching call already made and disk-cached),
    so a packet flagged here is flagged for the identical reason a real
    run would flag it for -- not a looser, preflight-only approximation."""
    header_words = page_words(page, (0, 0, page.width, HEADER_SEARCH_MAX_TOP))
    anchors = locate_header_anchors(header_words)
    row_height = header_row_height(anchors)
    image = page.to_image(resolution=dpi).original.convert("RGB")
    band = detect_header_band(image, dpi=dpi, anchors=anchors, row_height=row_height)
    left_bbox, right_bbox = redact_bboxes_for_band(band, anchors.group_top)
    uncovered = find_uncovered_group_words(header_words, anchors, left_bbox, right_bbox)
    return band, uncovered


def _collect_orientation_flags(
    pdf_path: str | Path, segmented: SegmentResult, *, orientation_overrides: dict[int, int] | None = None
) -> list[OrientationFlag]:
    """Every page whose orientation is nonzero-and-unconfirmed or too
    ambiguous to guess from at all, matched back to the packet (and
    within-packet page offset) it belongs to, so a human can locate it as
    "page N of packet X" rather than a bare physical page index. Reuses
    orientation.orientation_for, already disk-cached by segment_pdf's own
    call moments earlier in run_preflight -- this costs nothing extra."""
    from melredact.orientation import orientation_for

    result = orientation_for(pdf_path, overrides=orientation_overrides)
    by_page = result.by_page()
    located: dict[int, tuple[str, int]] = {}
    for packet in segmented.packets:
        tag = packet_tag(pdf_path, packet)
        for offset, idx in enumerate(packet.page_indices):
            located[idx] = (tag, offset + 1)

    flagged = sorted(set(result.unresolved_page_indices()) | set(result.pending_confirmation_page_indices()))
    flags: list[OrientationFlag] = []
    for idx in flagged:
        po = by_page[idx]
        tag, offset = located.get(idx, (None, None))
        flags.append(
            OrientationFlag(
                page_index=idx,
                packet_tag=tag,
                page_offset=offset,
                kind="pending_confirmation" if po.needs_confirmation else "unresolved",
                detected_angle=po.detected_angle,
                score=po.score,
            )
        )
    return flags


def run_preflight(
    pdf_path: str | Path,
    roster: Roster,
    *,
    period: str | None = None,
    orientation_overrides: dict[int, int] | None = None,
    dpi: int = RENDER_DPI_FINAL,
) -> PreflightReport:
    """The preflight entry point -- see the module note above this section
    for what this deliberately does and doesn't do, and why. Writes
    nothing, deletes nothing, never touches decisions/ledger/manual-queue.
    `roster` is the already-scoped roster (see roster.load_roster) the
    real run would use -- `period` is carried onto the report purely for
    display, not re-derived here."""
    start = time.monotonic()
    pdf_path = Path(pdf_path)

    segmented = segment_pdf(pdf_path, orientation_overrides=orientation_overrides)
    dates = collect_packet_dates(pdf_path, segmented=segmented, orientation_overrides=orientation_overrides)
    round_groups = group_into_rounds(segmented.packets, dates)
    round_labels = round_labels_by_tag(round_groups)
    disagreeing = round_disagreeing_tags(round_groups, dates)

    proposals = propose_all(pdf_path, segmented, roster, orientation_overrides=orientation_overrides)
    proposals_by_tag = {p.packet_tag: p for p in proposals}
    assignments = assign_all(proposals, round_labels)

    consensus_analysis = analyze_consensus_anomalies(pdf_path, segmented, orientation_overrides=orientation_overrides)
    orientation_flags = _collect_orientation_flags(pdf_path, segmented, orientation_overrides=orientation_overrides)

    packets: list[PreflightPacket] = []
    with open_pdf(pdf_path, orientation_overrides=orientation_overrides) as pdf:
        for packet in segmented.packets:
            tag = packet_tag(pdf_path, packet)
            proposal = proposals_by_tag.get(tag)
            top = proposal.top if proposal is not None else None

            detection_hold = False
            detection_reason = None
            uncovered_ink_advisory = False
            if packet.header_page_index is not None:
                band, uncovered = _header_geometry_check(pdf.pages[packet.header_page_index], dpi=dpi)
                if not band.detected:
                    detection_hold = True
                    detection_reason = f"header border not confidently detected: {band}"
                uncovered_ink_advisory = bool(uncovered)

            tag_holds = consensus_analysis.holds.get(tag, [])

            packets.append(
                PreflightPacket(
                    packet_tag=tag,
                    packet_index=packet.packet_index,
                    header_page_index=packet.header_page_index,
                    page_indices=list(packet.page_indices),
                    n_pages=packet.n_pages,
                    declared_total=packet.declared_total,
                    page_count_mismatch=packet.declared_total is not None and packet.n_pages != packet.declared_total,
                    is_orphan=packet.is_orphan,
                    issues=list(packet.issues),
                    worksheet_type=packet.worksheet_type,
                    round_label=round_labels.get(tag),
                    round_disagrees=tag in disagreeing,
                    top_score=top.score if top is not None else None,
                    top_margin=proposal.margin if proposal is not None else None,
                    has_plausible_match=top is not None and top.score >= MIN_SCORE,
                    would_auto_assign=assignments.get(tag) is not None,
                    detection_hold=detection_hold,
                    detection_reason=detection_reason,
                    uncovered_ink_advisory=uncovered_ink_advisory,
                    consensus_hold=bool(tag_holds),
                    consensus_reason="; ".join(h.reason for h in tag_holds) if tag_holds else None,
                    consensus_hold_pages=sorted({h.physical_page_index for h in tag_holds}),
                )
            )

    return PreflightReport(
        pdf_path=pdf_path,
        page_count=segmented.page_count,
        packets=packets,
        round_groups=round_groups,
        orientation_flags=orientation_flags,
        roster_period=period,
        roster_entry_count=len(roster),
        elapsed_seconds=time.monotonic() - start,
    )


_ISSUE_PAGE_RE = re.compile(r"^page (\d+):")


def other_blocked_packets(report: PreflightReport) -> list[PreflightPacket]:
    """Blocked packets not already itemized by the unsegmentable, page-
    count-mismatch, or orientation sections of the preflight report --
    shared by `format_preflight_report` and `render_preflight_contact_
    sheet` so the two can never drift on which packets this covers (found
    the hard way, 2026-08-14: a real packet, an unreadable footer on a
    continuation page, fell through every section of the *text* report
    before this existed as its own itemized category)."""
    oriented_tags = {f.packet_tag for f in report.orientation_flags if f.packet_tag is not None}
    return [
        p
        for p in report.packets
        if p.blocked and not p.is_orphan and not p.page_count_mismatch and p.packet_tag not in oriented_tags
    ]


def _issue_page_indices(packet: PreflightPacket) -> list[int]:
    """Physical page indices named by this packet's own issue strings
    (segment.py's issues always lead with "page N: ..."), so a report
    consumer can point a human at the *specific* page an issue is about
    rather than a generic stand-in. Falls back to the header page (or, for
    an orphan, the packet's own first page) when no issue names a page at
    all -- every packet has at least one of those."""
    found = {int(m.group(1)) for i in packet.issues if (m := _ISSUE_PAGE_RE.match(i))}
    if found:
        return sorted(found)
    if packet.header_page_index is not None:
        return [packet.header_page_index]
    return packet.page_indices[:1]


def format_preflight_report(report: PreflightReport) -> str:
    """Human-readable preflight report -- everything wrong with the file,
    then a plain verdict at the end (see PreflightReport.n_clean/n_needs_
    editor/n_cannot_process) that's the actual number a human uses to
    decide whether to run this file now or hand it back for a fix."""
    lines = [
        f"Preflight report: {report.pdf_path}",
        f"  {report.page_count} page(s), {report.n_packets} packet(s)",
    ]
    for p in report.packets:
        lines.append(f"    {p.packet_tag}: {p.n_pages} page(s) (footer declared {p.declared_total})")

    mismatched = [p for p in report.packets if p.page_count_mismatch]
    lines.append(f"\nPage-count vs. footer: {len(mismatched)} packet(s) disagree")
    for p in mismatched:
        lines.append(f"    {p.packet_tag}: {p.n_pages} actual page(s), footer declared {p.declared_total}")

    lines.append("")
    lines.append(format_round_report(report.round_groups))
    round_disagreeing = [p.packet_tag for p in report.packets if p.round_disagrees]
    if round_disagreeing:
        lines.append(f"  {len(round_disagreeing)} packet(s) disagree with their own round group: {round_disagreeing}")

    lines.append(f"\nOrientation: {len(report.orientation_flags)} page(s) flagged")
    for f in report.orientation_flags:
        loc = f"page {f.page_index} (packet {f.packet_tag}, page {f.page_offset})" if f.packet_tag else f"page {f.page_index} (no containing packet)"
        if f.kind == "pending_confirmation":
            lines.append(f"    {loc}: detected rotated {f.detected_angle}° (confidence {f.score:.2f}), not yet confirmed")
        else:
            lines.append(f"    {loc}: orientation could not be confidently determined (score {f.score:.2f})")

    detection_holds = [p for p in report.packets if p.detection_hold]
    lines.append(f"\nHeader detection: {len(detection_holds)} packet(s) with an undetected header border")
    for p in detection_holds:
        lines.append(f"    {p.packet_tag}: {p.detection_reason}")

    no_match = [p for p in report.packets if not p.is_orphan and not p.has_plausible_match]
    lines.append(f"\nRoster: period {report.roster_period!r}, {report.roster_entry_count} entries")
    lines.append(f"  {len(no_match)} packet(s) with no plausible roster match: {[p.packet_tag for p in no_match]}")

    lines.append(f"\nConsensus-ink anomalies: {report.n_consensus_holds} packet(s) flagged")
    for p in report.packets:
        if p.consensus_hold:
            lines.append(f"    {p.packet_tag}: {p.consensus_reason}")

    lines.append(f"\nUncovered-ink advisory (non-blocking): {report.n_uncovered_ink_advisories} packet(s) flagged")

    unsegmentable = [p for p in report.packets if p.is_orphan]
    lines.append(f"\nUnsegmentable packets: {len(unsegmentable)}")
    for p in unsegmentable:
        lines.append(f"    {p.packet_tag}: {'; '.join(p.issues)}")

    # A packet can be blocked (has segmentation issues) for a reason
    # neither an orphan nor a page-count mismatch already itemizes above --
    # e.g. an unreadable footer on a continuation page whose header page
    # read fine (found on a real file, 2026-08-14: a continuation page's
    # own footer unreadable mid-packet, not the header page's). Without
    # this, such a packet still correctly counted toward `n_cannot_
    # process` in the verdict below, but had nowhere in the printed report
    # naming *which* packet or why -- a silent gap between an accurate
    # count and an exhaustive listing.
    other_blocked = other_blocked_packets(report)
    if other_blocked:
        lines.append(f"\nOther blocked packets: {len(other_blocked)}")
        for p in other_blocked:
            lines.append(f"    {p.packet_tag}: {'; '.join(p.issues)}")

    lines.append(
        f"\nVerdict: {report.n_clean} would process cleanly, {report.n_needs_editor} would need a human in "
        f"the editor, {report.n_cannot_process} cannot be processed without a fix"
    )
    lines.append(f"Elapsed: {report.elapsed_seconds:.1f}s")
    return "\n".join(lines)


def _diagnostics_dir(out_dir: str | Path, pdf_path: str | Path) -> Path:
    return Path(out_dir) / ".diagnostics" / f"{Path(pdf_path).stem}_preflight"


def render_preflight_contact_sheet(
    pdf_path: str | Path,
    report: PreflightReport,
    out_dir: str | Path,
    *,
    orientation_overrides: dict[int, int] | None = None,
    dpi: int = 150,
    thumb_width: int = 360,
    columns: int = 4,
) -> Path | None:
    """One thumbnail per flagged page -- an orientation issue, an
    undetected header border, uncovered-ink advisory ink, a consensus-ink
    anomaly, an unsegmentable/orphan packet's own first page, or any other
    blocked packet's own issue-named page (see other_blocked_packets) --
    laid out into a single contact-sheet PNG under
    `out_dir/.diagnostics/<pdf-stem>_preflight/contact_sheet.png`, each
    thumbnail labelled with the specific reason(s) it was flagged. Meant
    to be eyeballed in one place rather than paging through review_app.py
    packet by packet. Returns None (writes nothing) when nothing was
    flagged -- an empty contact sheet has no use.

    Rendered at a modest preview DPI (150 by default), not the real
    redaction DPI -- this is for a human to skim, not a geometry source of
    truth for anything downstream."""
    from PIL import Image, ImageDraw

    notes: dict[int, list[str]] = {}

    def flag(idx: int, note: str) -> None:
        notes.setdefault(idx, []).append(note)

    for f in report.orientation_flags:
        if f.kind == "pending_confirmation":
            flag(f.page_index, f"orientation: rotated {f.detected_angle}° (unconfirmed, score {f.score:.2f})")
        else:
            flag(f.page_index, f"orientation: unresolved (score {f.score:.2f})")

    for p in report.packets:
        if p.detection_hold and p.header_page_index is not None:
            flag(p.header_page_index, f"{p.packet_tag}: header border not detected")
        if p.uncovered_ink_advisory and p.header_page_index is not None:
            flag(p.header_page_index, f"{p.packet_tag}: uncovered-ink advisory")
        for idx in p.consensus_hold_pages:
            flag(idx, f"{p.packet_tag}: consensus-ink anomaly")
        if p.is_orphan and p.page_indices:
            flag(p.page_indices[0], f"{p.packet_tag}: unsegmentable ({'; '.join(p.issues)})")
        if p.page_count_mismatch and p.header_page_index is not None:
            flag(p.header_page_index, f"{p.packet_tag}: page count vs. footer mismatch")

    # Anything blocked for a reason not already covered above (see
    # other_blocked_packets' own docstring) -- labelled on the specific
    # page its own issue text names, not a generic stand-in, so a real
    # unreadable-footer/segmentation problem shows up on the actual page
    # that has it, not silently absent from the contact sheet the way it
    # was before this existed.
    for p in other_blocked_packets(report):
        for idx in _issue_page_indices(p):
            flag(idx, f"{p.packet_tag}: blocked ({'; '.join(p.issues)})")

    if not notes:
        return None

    page_indices = sorted(notes)
    with open_pdf(pdf_path, orientation_overrides=orientation_overrides) as pdf:
        thumbs = []
        for idx in page_indices:
            image = pdf.pages[idx].to_image(resolution=dpi).original.convert("RGB")
            scale = thumb_width / image.width
            image = image.resize((thumb_width, max(1, int(image.height * scale))))
            thumbs.append((idx, image))

    label_height = 90
    cell_h = max(img.height for _, img in thumbs) + label_height
    rows = (len(thumbs) + columns - 1) // columns
    sheet = Image.new("RGB", (thumb_width * columns, cell_h * rows), color="white")
    draw = ImageDraw.Draw(sheet)
    for i, (idx, image) in enumerate(thumbs):
        col, row = i % columns, i // columns
        x, y = col * thumb_width, row * cell_h
        sheet.paste(image, (x, y))
        label = f"page {idx}\n" + "\n".join(notes[idx])
        draw.multiline_text((x + 4, y + image.height + 4), label, fill="black")

    out_path = _diagnostics_dir(out_dir, pdf_path) / "contact_sheet.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return out_path
