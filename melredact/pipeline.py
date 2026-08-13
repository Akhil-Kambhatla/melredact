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
from dataclasses import dataclass, field
from pathlib import Path

from melredact.config import RENDER_DPI_FINAL
from melredact.match import HeldCandidate, MatchProposal, propose
from melredact.pdfio import open_pdf
from melredact.redact import HeaderBand, verify_no_leaked_names
from melredact.redact import redact_packet as _redact_packet
from melredact.roster import Roster, RosterEntry
from melredact.segment import Packet, SegmentResult, extract_header_fields

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


def output_path(out_dir: str | Path, entry: RosterEntry, worksheet_type: str, topic: str = NO_TOPIC) -> Path:
    """Where a confirmed packet for this roster entry lands:
    out/<teacher_code>/<period>/<worksheet_type>/<topic>/<SID>.pdf. `entry.
    teacher_code` and `entry.period_display` are the SID's own digits
    (positions 0:6 and 6:8), not anything read off the packet, so those two
    segments are stable and derivable from the SID alone. `worksheet_type`
    is *not* derivable from the SID -- a student has one SID but multiple
    worksheet types (MPR, PRT, ...) -- so it must come from the packet's own
    footer (see Packet.worksheet_type); omitting it is exactly the bug that
    let an MPR and a PRT packet for the same student collide on one path.
    `topic` (see topic_from_filename) defaults to NO_TOPIC so every caller
    that hasn't been updated for per-topic worksheets keeps computing the
    exact same path as before this segment existed."""
    return Path(out_dir) / entry.teacher_code / entry.period_display / worksheet_type / topic / f"{entry.sid}.pdf"


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
    ledger: dict[str, dict], tag: str, out_dir: str | Path, entry: RosterEntry, worksheet_type: str, topic: str
) -> tuple[Path, str | None]:
    """The natural output path for this packet, unless that exact path
    already exists on disk *and* this run's own ledger attributes it to a
    different packet_tag -- in which case a numbered-suffix alternative
    (`<SID>_2.pdf`, `_3.pdf`, ...) is returned instead, so one packet's
    output can never silently replace another's. This is a backstop for the
    case the topic path segment (see output_path) doesn't fully disambiguate
    on its own: two distinct packets in the *same* scan file, decided to the
    same student and worksheet type (a teacher whose students genuinely
    complete the same worksheet+topic more than once). Re-running the same
    tag against its own previously-claimed path is not a collision -- the
    ledger lookup excludes `tag` itself, so a packet re-processed after a
    decision change keeps overwriting its own prior file exactly as before.
    """
    base_path = output_path(out_dir, entry, worksheet_type, topic)
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
) -> None:
    """Move (never copy) a held-back packet's already-drawn draft into the
    manual-redaction queue instead of just deleting it -- the backstop a
    human needs to actually see what's wrong and fix the geometry by hand,
    rather than starting over from nothing. Moved, not copied: the draft
    can be exactly as unsafe as the reason it was held back for (that's the
    whole reason it's here), so it must exist in at most one place on
    disk, never both the queue and (however briefly) somewhere else."""
    qdir = manual_queue_dir(out_dir, pdf_path)
    qdir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(drafted_path), manual_queue_draft_path(out_dir, pdf_path, tag))
    meta = {
        "packet_tag": tag,
        "sid": sid,
        "worksheet_type": worksheet_type,
        "reason": reason,
        "pdf_path": str(pdf_path),
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


@dataclass
class ManualReleaseResult:
    packet_tag: str
    sid: str
    released: bool
    out_path: Path | None = None
    reason: str | None = None


def release_from_manual_queue(
    pdf_path: str | Path,
    packet: Packet,
    tag: str,
    sid: str,
    roster: Roster,
    band_override: HeaderBand,
    *,
    out_dir: str | Path = OUT_DIR,
    dpi: int = RENDER_DPI_FINAL,
    flatten: bool = False,
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
    """
    if sid not in roster:
        return ManualReleaseResult(packet_tag=tag, sid=sid, released=False, reason=f"sid {sid!r} not on roster")
    entry = roster.by_sid[sid]
    topic = topic_from_filename(pdf_path)
    ledger = _load_ledger(out_dir, pdf_path)
    out_path, collision_note = _claim_output_path(ledger, tag, out_dir, entry, packet.worksheet_type, topic)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_lines = [f"SID: {entry.sid}", f"PD: {entry.period_display}"]
    redact_result = _redact_packet(
        pdf_path, packet, out_path, dpi=dpi, flatten=flatten, stamp_lines=stamp_lines, band_override=band_override
    )
    if redact_result.uncovered_group_words:
        out_path.unlink()
        return ManualReleaseResult(
            packet_tag=tag,
            sid=sid,
            released=False,
            reason=f"still uncovered group-row ink with this geometry: {redact_result.uncovered_group_words}",
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
    return ManualReleaseResult(packet_tag=tag, sid=sid, released=True, out_path=out_path, reason=collision_note)


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


def propose_all(pdf_path: str | Path, segmented: SegmentResult, roster: Roster) -> list[MatchProposal]:
    """One ranked candidate list per packet, keyed by the stable packet_tag
    rather than segment.py's positional packet_index, so callers (the
    review UI, run_dispositions) can key off something that survives
    across runs. Orphan packets (no header page -- see segment.py) have no
    Name field to score at all, so they always abstain with an empty
    candidate list rather than being skipped."""
    proposals = []
    with open_pdf(pdf_path) as pdf:
        for packet in segmented.packets:
            tag = packet_tag(pdf_path, packet)
            if packet.header_page_index is None:
                proposals.append(MatchProposal(packet_tag=tag, candidates=[]))
                continue
            fields = extract_header_fields(pdf.pages[packet.header_page_index])
            proposals.append(propose(tag, fields.name_text, roster))
    return proposals


def _held_match_for_packet(pdf_path: str | Path, packet: Packet, roster: Roster) -> HeldCandidate | None:
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
    with open_pdf(pdf_path) as pdf:
        fields = extract_header_fields(pdf.pages[packet.header_page_index])
    proposal = propose(packet_tag(pdf_path, packet), fields.name_text, roster)
    return proposal.top_held if proposal.is_held_match else None


def _draft_consent_hold_redaction(pdf_path: str | Path, packet: Packet, dpi: int, flatten: bool) -> None:
    """Redaction is unconditional, never dependent on whether a SID could
    ever be resolved (see CLAUDE.md) -- a consent-hold packet still gets
    fully redacted, proving the geometry is sound, even though the result
    is never written to out_dir. The draft lands in a real temporary
    directory that's removed the moment this returns, so nothing about a
    consent hold ever leaves a file sitting anywhere on disk."""
    with tempfile.TemporaryDirectory() as scratch_dir:
        _redact_packet(pdf_path, packet, Path(scratch_dir) / "scratch.pdf", dpi=dpi, flatten=flatten)


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
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = _load_ledger(out_dir, pdf_path)
    ledger_dirty = False
    results: list[DispositionResult] = []
    topic = topic_from_filename(pdf_path)

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
            held = _held_match_for_packet(pdf_path, packet, roster)
            if held is not None:
                _draft_consent_hold_redaction(pdf_path, packet, dpi, flatten)
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
                _delete_stale_output(Path(prior_entry["path"]), prior_sid)
                ledger.pop(tag, None)
                ledger_dirty = True
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
        out_path, collision_note = _claim_output_path(ledger, tag, out_dir, entry, packet.worksheet_type, topic)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_lines = [f"SID: {entry.sid}", f"PD: {entry.period_display}"]
        redact_result = _redact_packet(pdf_path, packet, out_path, dpi=dpi, flatten=flatten, stamp_lines=stamp_lines)
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
        if redact_result.uncovered_group_words:
            # Geometric proof (see find_uncovered_group_words) that the
            # redaction rectangles didn't actually cover real Group-row
            # ink -- independent of, and checked before, the text-based
            # verify pass below, since this is exactly the class of leak
            # (real ink, OCR-garbled into a non-matching token) that a
            # text check alone can miss. Never overridable, detection
            # override or not: this is a finding of actual uncovered ink in
            # the pixels, not a confidence question about the geometry.
            reason = f"uncovered group-row ink: {redact_result.uncovered_group_words}"
            _queue_for_manual_redaction(out_dir, pdf_path, tag, sid, packet.worksheet_type, reason, out_path)
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
            )
        )

        # A correction (this tag's approved SID changed from a previously
        # written one) supersedes the old file -- delete it as a direct
        # consequence of *this* explicit new decision, once the new file is
        # confirmed written and clean, not as a background sweep. Deletes
        # the ledger's own recorded path, not a recomputed one, same as the
        # non-consent case above.
        if prior_entry is not None and prior_sid != sid:
            _delete_stale_output(Path(prior_entry["path"]), prior_sid)
        ledger[tag] = {"sid": sid, "path": str(out_path)}
        ledger_dirty = True

    if ledger_dirty:
        _save_ledger(out_dir, pdf_path, ledger)

    return results
