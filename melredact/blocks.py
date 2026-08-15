"""Date-driven roster block resolution, for a teacher whose roster blocks
encode more than class period alone.

Most teachers' rosters are one block per class period (see roster.py), and
`--period`/filename PD-inference is enough to pick the right one. Teacher
010406 breaks that: their four blocks encode class period *and* collection
round together (block 01 = period 1/February, 02 = period 1/March, 03 =
period 2/February, 04 = period 2/March), and the two blocks for the same
class period contain the *same 14 students*. A scan named
"010406_PD1_PRT.pdf" is unambiguous about class period (1) but says nothing
about which collection round -- the filename's "PD1" would, under the old
single-signal inference, resolve straight to block 01, silently wrong for a
March scan. Nothing downstream would catch this: names still score
perfectly (the two blocks share names), review still shows a plausible
match, and `verify` only ever checks for a leaked name, never for "assigned
to the correct one of two identically-named students." Every SID in the
file would ship wrong by exactly 100 (block 01 vs 02's own numbering).

This module is entirely additive and only ever activates for a teacher who
opts in by having a `<roster_stem>_blocks.json` sidecar
(`load_block_metadata`) -- a teacher with no sidecar (e.g. 020415) is
untouched: every caller (cli.py, review_app.py) checks for `None` first and
falls straight through to the existing period-inference path with no
change to its behavior or command line.

**Resolution is file-level, never per-packet.** One scanned PDF is one
collection session -- a teacher scans a whole class's worksheets from a
single sitting, so the file has exactly one round, and every packet in it
belongs to the same block. `resolve_block` takes the majority parsed month
across *every* packet in the file and requires it to be both well-supported
(at least `MIN_DATED_PACKETS` packets with a readable date) and dominant
(at least `MAJORITY_FRACTION` of the parsed dates) before resolving
anything -- otherwise it resolves nothing and a human must pass `--block`
explicitly. A *per-packet* resolution scheme was deliberately rejected: a
single misread handwritten date would silently route that one packet into
whichever block its own (possibly wrong) date pointed at, and because the
two class-period blocks share identical names, a wrongly-routed packet
would find its own name waiting for it in the wrong block and match with
perfect, silent confidence -- the exact failure mode this module exists to
prevent, just moved from "whole file" to "one unlucky packet." A packet
whose own parsed month disagrees with the file's resolved majority is
still surfaced (`disagreeing_packets`) so a reviewer can see it next to
that packet's date, but it is never used to move that one packet to a
different block; students write the wrong date on their own worksheet
often enough that per-packet dates are not a signal to act on, only a
signal to flag (see CLAUDE.md's "Date-driven block resolution" section).

**The confirmation gate is the only real defense here, and it is
deliberately not skippable.** No match-quality signal can ever distinguish
a packet correctly assigned to block 02 from the same packet wrongly
assigned to block 01, because the two blocks' students have the same
names -- a wrong resolution looks exactly as confident as a right one. See
cli.py's `--confirm-block` and review_app.py's confirmation checkbox for
where that gate actually lives; this module only supplies the report they
both show.
"""

from __future__ import annotations

import calendar
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from melredact.pdfio import open_pdf
from melredact.segment import Packet, SegmentResult, extract_header_fields, segment_pdf

# Require at least this many packets with a parseable date before resolving
# anything -- a file-level majority computed from one or two dates carries
# essentially no signal, and this is exactly the situation where "abstain
# and require a human to pass --block" is the only honest answer.
MIN_DATED_PACKETS = 3

# The majority month must hold at least this fraction of all *parsed*
# dates (not all packets -- a packet with no parseable date simply doesn't
# vote either way). Below this, the file's dates don't actually agree on a
# single round and resolving anyway would be a guess dressed up as a
# calculation.
MAJORITY_FRACTION = 0.6

_MONTH_NAMES = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTH_ABBR = {abbr.lower(): i for i, abbr in enumerate(calendar.month_abbr) if abbr}
_NUMERIC_DATE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\s*$")


def blocks_path(roster_path: str | Path) -> Path:
    """Where a roster CSV's block-metadata sidecar would live -- always
    <roster_stem>_blocks.json next to the roster itself, mirroring
    roster.holds_path. Loading is optional: a roster with no round/period
    ambiguity simply has no sidecar, and `load_block_metadata` treats that
    as "this feature doesn't apply here," not an error."""
    roster_path = Path(roster_path)
    return roster_path.with_name(f"{roster_path.stem}_blocks.json")


def normalize_block(block: str | int) -> str:
    """Zero-pad a block code the same way roster.py's `_normalize_period`
    does for a period code -- block codes and period codes share the same
    two-digit convention (a block's own code *is* the SID period digits it
    filters the roster to), so `--block 2` and `--confirm-block 2` behave
    the same as the roster's own `--period 2` already does."""
    s = str(block).strip()
    return s.zfill(2) if s.isdigit() else s


@dataclass(frozen=True)
class BlockMeaning:
    block: str
    class_period: int
    month: int

    @property
    def month_name(self) -> str:
        return calendar.month_name[self.month]

    def describe(self) -> str:
        return f"block {self.block}, class period {self.class_period}, {self.month_name}"


@dataclass(frozen=True)
class BlockMetadata:
    teacher_code: str
    blocks: dict[str, BlockMeaning]

    def blocks_for_class_period(self, class_period: int) -> list[BlockMeaning]:
        return [b for b in self.blocks.values() if b.class_period == class_period]


def load_block_metadata(roster_path: str | Path) -> BlockMetadata | None:
    """Parse <roster_stem>_blocks.json if it exists, else None -- the
    signal every caller checks first to decide whether any of this module's
    behavior applies at all. Malformed JSON or a missing required key is
    still a data-integrity problem and fails loudly (per CLAUDE.md's
    "fail loudly on data-integrity problems" working preference), same as
    a missing file is not an error."""
    path = blocks_path(roster_path)
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    blocks = {
        block: BlockMeaning(block=normalize_block(block), class_period=int(v["class_period"]), month=int(v["month"]))
        for block, v in raw["blocks"].items()
    }
    return BlockMetadata(teacher_code=raw["teacher_code"], blocks=blocks)


def parse_month(date_text: str | None) -> int | None:
    """Best-effort month extraction from a handwritten, OCR'd date field.
    Handles M/D/YYYY, M/D/YY, M-D-YYYY, and written month names (full or
    abbreviated, e.g. "March 31, 2026" or "Mar 31 2026").

    Handwriting OCR is noisy, and this feeds a decision (which of two
    identically-named blocks a student's data lands in) with no downstream
    signal that could ever catch a wrong guess -- so this returns None far
    more readily than it guesses. An out-of-range month (13, 0), an
    out-of-range day, or a numeric string that doesn't fully match the
    expected shape (garbled OCR, extra stray characters) all return None
    rather than a best-effort partial parse.
    """
    if not date_text:
        return None
    text = date_text.strip()
    if not text:
        return None

    m = _NUMERIC_DATE.match(text)
    if m:
        month, day, _year = (int(g) for g in m.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return month
        return None

    lowered = text.lower()
    for name, num in _MONTH_NAMES.items():
        if name and re.search(rf"\b{re.escape(name)}\b", lowered):
            return num
    for abbr, num in _MONTH_ABBR.items():
        if abbr and re.search(rf"\b{re.escape(abbr)}\b", lowered):
            return num
    return None


@dataclass(frozen=True)
class PacketDate:
    packet_tag: str
    raw_date_text: str
    month: int | None


def collect_packet_dates(
    pdf_path: str | Path,
    segmented: SegmentResult | None = None,
    *,
    orientation_overrides: dict[int, int] | None = None,
    page_sequence: list[int] | None = None,
) -> list[PacketDate]:
    """One (packet_tag, raw OCR'd date text, parsed month) per packet in the
    file -- the file-level resolution rule's raw material. Deliberately
    takes no roster: segmentation and header-field extraction (segment.py)
    never needed one in the first place (footer page markers and header
    anchors are both roster-independent), so this can run standalone,
    before a block -- and therefore a roster scope -- has even been chosen.
    A packet with no header page (an orphan continuation page, see
    segment.py) has no Date field to read; it's still included, with an
    empty raw text and month=None, so it's counted in the file's total
    packet count without ever voting on the majority month.

    `segmented` lets a caller that already has a `SegmentResult` (run_
    dispositions, the round-grouping report below) skip a redundant re-
    segmentation of the same file -- segmentation itself is cheap, but
    callers that already paid for it shouldn't pay again. Defaults to
    segmenting fresh, same as before this parameter existed.
    """
    from melredact.pipeline import packet_tag as _packet_tag

    if segmented is None:
        segmented = segment_pdf(pdf_path, orientation_overrides=orientation_overrides, page_sequence=page_sequence)
    dates: list[PacketDate] = []
    with open_pdf(pdf_path, orientation_overrides=orientation_overrides) as pdf:
        for packet in segmented.packets:
            tag = _packet_tag(pdf_path, packet)
            if packet.header_page_index is None:
                dates.append(PacketDate(packet_tag=tag, raw_date_text="", month=None))
                continue
            fields = extract_header_fields(pdf.pages[packet.header_page_index])
            dates.append(PacketDate(packet_tag=tag, raw_date_text=fields.date_text, month=parse_month(fields.date_text)))
    return dates


def parse_year_month(date_text: str | None) -> tuple[int, int] | None:
    """Full (year, month) from a packet's OCR'd date, for round labelling
    (round_label below). parse_month alone is enough for block resolution,
    which only ever has to disambiguate two same-numbered months within one
    file's own block metadata -- but a round label has to distinguish
    collection sessions that can span different *years* (the real 010406
    PRT file spans October 2025, February 2026, and March 2026), so it
    needs the full year too. Same conservative posture as parse_month:
    returns None -- never a best-effort guess -- on anything out of range,
    partially matched, or with no recognizable 4-digit year nearby.
    """
    if not date_text:
        return None
    text = date_text.strip()
    if not text:
        return None

    m = _NUMERIC_DATE.match(text)
    if m:
        month, day, year = (int(g) for g in m.groups())
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        if year < 100:
            year += 2000
        return year, month

    lowered = text.lower()
    year_match = re.search(r"\b(19|20)\d{2}\b", text)
    if year_match is None:
        return None
    year = int(year_match.group(0))
    for name, num in _MONTH_NAMES.items():
        if name and re.search(rf"\b{re.escape(name)}\b", lowered):
            return year, num
    for abbr, num in _MONTH_ABBR.items():
        if abbr and re.search(rf"\b{re.escape(abbr)}\b", lowered):
            return year, num
    return None


def round_label(date_text: str | None) -> str:
    """The raw, per-packet "YYYY-MM" round label from one packet's own
    OCR'd date text, or the literal "undated" when it can't be confidently
    parsed (parse_year_month returns None far more readily than it
    guesses -- see its own docstring). This is deliberately per-packet, not
    the label a packet should actually be shipped under: a single misread
    date inside an otherwise-uniform run must inherit the *run's* label,
    not its own (see group_into_rounds below) -- callers building an
    output path must always go through group_into_rounds/round_labels_by_
    tag, never this function alone, except when computing the raw per-
    packet vote group_into_rounds itself needs.
    """
    parsed = parse_year_month(date_text)
    if parsed is None:
        return "undated"
    year, month = parsed
    return f"{year:04d}-{month:02d}"


UNDATED_ROUND = "undated"


@dataclass(frozen=True)
class RoundGroup:
    label: str
    packet_tags: list[str]
    first_page: int  # 1-indexed physical page number, for the report
    last_page: int
    n_disagreeing: int  # packets in this group whose own parsed date != label

    @property
    def n_packets(self) -> int:
        return len(self.packet_tags)


def group_into_rounds(packets: list[Packet], dates: list[PacketDate]) -> list[RoundGroup]:
    """Group packets into contiguous "rounds" (collection sessions) by their
    own OCR'd Date field, in physical page order -- the raw material for the
    round path segment (see CLAUDE.md's "A round segment" section for the
    full real-file motivation: one file, `010406_PD1_PRT.pdf`, turned out to
    be three concatenated PRT administrations of the same ~14 students,
    dated October 2025 / February 2026 / March 2026, with nothing in the
    filename to tell them apart).

    `packets` and `dates` must be the same length and in the same order --
    both come from iterating the same `SegmentResult.packets` list (see
    collect_packet_rounds below, the normal entry point), so index i in one
    always describes the same physical packet as index i in the other.

    **Grouping trusts a contiguous run, never a single packet's own date.**
    A boundary between two rounds is only recognized when the new month
    actually *sticks* -- confirmed by whatever the *next* dated packet
    parses to not reverting back to the current run's own label. A single
    misread date (real handwriting OCR noise, the same class of error
    `blocks.disagreeing_packets` already exists to tolerate for block
    resolution) looks like a brief detour that immediately snaps back to
    where it was -- exactly what "reverts back" catches -- and is absorbed
    into whichever run it physically sits inside, counted in that run's
    `n_disagreeing` instead of splitting the run into two. A change that
    doesn't revert (including one where the next value is a *third*,
    different label, or where there's no further dated packet to check
    against at all) is trusted as a real boundary -- this is what lets a
    file with only one packet per session, not just a file with several
    packets per session, still produce one round group per session, while
    still refusing to be fooled by a single-packet blip inside a longer
    run. This mirrors the same lesson date-driven block resolution already
    learned the hard way (see this module's own docstring on why
    resolution is file-level, not per-packet): a signal this noisy has no
    business making a structural decision -- moving a packet to a
    different output path -- on its own, single-packet say-so.

    An undated packet (no parseable date at all) never itself creates or
    breaks a boundary -- it simply rides along inside whichever run it
    falls positionally within. A run with no parseable dates at all (every
    packet inside it undated) is labelled "undated" via UNDATED_ROUND,
    and still ships -- an unreadable date is not a reason to withhold
    otherwise-approved output, only a reason the round segment in its path
    can't be more specific.
    """
    n = len(packets)
    raw = [round_label(d.raw_date_text) for d in dates]
    raw = [None if label == UNDATED_ROUND else label for label in raw]

    next_nonnone: list[int | None] = [None] * n
    nxt: int | None = None
    for i in range(n - 1, -1, -1):
        next_nonnone[i] = nxt
        if raw[i] is not None:
            nxt = i

    groups: list[RoundGroup] = []
    current_indices: list[int] = []

    def flush() -> None:
        if not current_indices:
            return
        labels_in_group = [raw[i] for i in current_indices if raw[i] is not None]
        majority_label = Counter(labels_in_group).most_common(1)[0][0] if labels_in_group else UNDATED_ROUND
        n_disagreeing = sum(1 for i in current_indices if raw[i] is not None and raw[i] != majority_label)
        first_page = packets[current_indices[0]].page_indices[0] + 1
        last_page = packets[current_indices[-1]].page_indices[-1] + 1
        groups.append(
            RoundGroup(
                label=majority_label,
                packet_tags=[dates[i].packet_tag for i in current_indices],
                first_page=first_page,
                last_page=last_page,
                n_disagreeing=n_disagreeing,
            )
        )

    current_anchor: str | None = None
    for i in range(n):
        label = raw[i]
        if label is None:
            current_indices.append(i)
            continue
        if current_anchor is None or label == current_anchor:
            current_anchor = label
            current_indices.append(i)
            continue
        # label differs from the current run's anchor -- a candidate
        # boundary. Only rejected (treated as a single misread, absorbed
        # into the current run) when the *next* dated packet reverts back
        # to the current run's own label -- confirming this was a brief
        # detour, not a real change. Anything else (no further dated
        # packet to check, or the next one differs too) confirms the move.
        j = next_nonnone[i]
        if j is None or raw[j] != current_anchor:
            flush()
            current_indices = [i]
            current_anchor = label
        else:
            current_indices.append(i)
    flush()

    return groups


def round_labels_by_tag(groups: list[RoundGroup]) -> dict[str, str]:
    return {tag: g.label for g in groups for tag in g.packet_tags}


def duplicate_round_labels(groups: list[RoundGroup]) -> list[str]:
    """Labels shared by more than one *group* -- since group_into_rounds
    never merges non-adjacent groups (each confirmed boundary always starts
    a fresh RoundGroup, even if its label matches an earlier, already-closed
    one), any duplicate found here is inherently non-adjacent. That means
    the file isn't simply "N sessions concatenated back to back" the way
    the round segment assumes -- e.g. a scan interleaving two sessions, or
    a re-scanned page reinserted out of order -- and is worth a human's
    attention, not a silent merge back into one round."""
    counts = Counter(g.label for g in groups if g.label != UNDATED_ROUND)
    return sorted(label for label, count in counts.items() if count > 1)


def round_disagreeing_tags(groups: list[RoundGroup], dates: list[PacketDate]) -> frozenset[str]:
    """packet_tags whose own parsed round label differs from their group's
    label -- mirrors disagreeing_packets (block resolution) but for round
    grouping, so a caller (review_app.py) can flag the specific packet next
    to its own date field. Never used to hold, reroute, or otherwise act on
    the packet (see group_into_rounds' own docstring): the group's majority
    label is what's trusted; a single packet's own date is only ever a flag
    for a human, the same lesson block resolution already learned."""
    by_tag = {d.packet_tag: d for d in dates}
    result: set[str] = set()
    for g in groups:
        for tag in g.packet_tags:
            d = by_tag.get(tag)
            if d is None:
                continue
            label = round_label(d.raw_date_text)
            if label != UNDATED_ROUND and label != g.label:
                result.add(tag)
    return frozenset(result)


def collect_packet_rounds(
    pdf_path: str | Path,
    segmented: SegmentResult | None = None,
    *,
    orientation_overrides: dict[int, int] | None = None,
    page_sequence: list[int] | None = None,
) -> list[RoundGroup]:
    """The normal entry point: segment (unless already done), read every
    packet's own Date field, and group into contiguous rounds. See
    group_into_rounds for the grouping rule itself."""
    if segmented is None:
        segmented = segment_pdf(pdf_path, orientation_overrides=orientation_overrides, page_sequence=page_sequence)
    dates = collect_packet_dates(pdf_path, segmented=segmented, orientation_overrides=orientation_overrides)
    return group_into_rounds(segmented.packets, dates)


def format_round_report(groups: list[RoundGroup]) -> str:
    """Human-readable round-group table shown by both cli.py and
    review_app.py *before* anything is written -- so a human can eyeball
    the detected rounds (label, packet count, page range, and how many
    packets in that group had their own date disagree with the group's
    majority label) before approving any packet into one. See CLAUDE.md's
    "A round segment" section for why grouping, not a single packet's own
    date, is what a reviewer should trust here."""
    lines = ["Round grouping report:"]
    for g in groups:
        lines.append(
            f"  {g.label}: {g.n_packets} packet(s), pages {g.first_page}-{g.last_page}, "
            f"{g.n_disagreeing} disagreeing"
        )
    dupes = duplicate_round_labels(groups)
    if dupes:
        lines.append(
            f"  NOTE: {len(dupes)} label(s) appear in more than one non-adjacent group -- "
            f"this file may not be simply concatenated sessions: {dupes}"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class BlockResolution:
    class_period: int
    resolved: bool
    chosen_block: BlockMeaning | None
    month_histogram: dict[int, int]
    n_parsed: int
    n_total: int
    majority_month: int | None
    confidence: float | None  # fraction of parsed dates the majority month holds
    per_packet_months: dict[str, int | None] = field(default_factory=dict)
    reason: str | None = None  # set whenever resolved is False


def resolve_block(dates: list[PacketDate], class_period: int, metadata: BlockMetadata) -> BlockResolution:
    """The file-level majority rule (see module docstring for why this is
    file-level, not per-packet): resolves a block only when at least
    MIN_DATED_PACKETS packets had a parseable date *and* the majority month
    among them holds at least MAJORITY_FRACTION of all parsed dates. Either
    threshold failing means "resolve nothing" (`resolved=False`, `reason`
    explains why) -- a human must then pass `--block` explicitly rather
    than the pipeline silently picking a weakly-supported guess.
    """
    per_packet_months = {d.packet_tag: d.month for d in dates}
    n_total = len(dates)
    histogram = Counter(m for m in per_packet_months.values() if m is not None)
    n_parsed = sum(histogram.values())

    def _abstain(majority_month: int | None, confidence: float | None, reason: str) -> BlockResolution:
        return BlockResolution(
            class_period=class_period,
            resolved=False,
            chosen_block=None,
            month_histogram=dict(histogram),
            n_parsed=n_parsed,
            n_total=n_total,
            majority_month=majority_month,
            confidence=confidence,
            per_packet_months=per_packet_months,
            reason=reason,
        )

    if n_parsed < MIN_DATED_PACKETS:
        return _abstain(
            None,
            None,
            f"only {n_parsed} packet(s) had a parseable date, need at least {MIN_DATED_PACKETS}",
        )

    majority_month, majority_count = histogram.most_common(1)[0]
    confidence = majority_count / n_parsed
    if confidence < MAJORITY_FRACTION:
        return _abstain(
            majority_month,
            confidence,
            f"no month holds at least {MAJORITY_FRACTION:.0%} of parsed dates "
            f"(best: {calendar.month_name[majority_month]} at {confidence:.0%})",
        )

    candidates = [b for b in metadata.blocks_for_class_period(class_period) if b.month == majority_month]
    if not candidates:
        return _abstain(
            majority_month,
            confidence,
            f"no block is defined for class period {class_period}, month {calendar.month_name[majority_month]}",
        )
    if len(candidates) > 1:
        return _abstain(
            majority_month,
            confidence,
            f"ambiguous block metadata: multiple blocks match class period {class_period}, "
            f"month {calendar.month_name[majority_month]} ({[b.block for b in candidates]})",
        )

    return BlockResolution(
        class_period=class_period,
        resolved=True,
        chosen_block=candidates[0],
        month_histogram=dict(histogram),
        n_parsed=n_parsed,
        n_total=n_total,
        majority_month=majority_month,
        confidence=confidence,
        per_packet_months=per_packet_months,
        reason=None,
    )


def disagreeing_packets(resolution: BlockResolution) -> list[str]:
    """packet_tags whose own parsed month differs from the file's resolved
    majority month -- surfaced to a reviewer (next to that packet's own
    date field), never used to hold or reroute the packet. See the module
    docstring: students get their own date wrong often enough that a
    single packet's date is a flag, not a signal to act on."""
    if resolution.majority_month is None:
        return []
    return sorted(
        tag
        for tag, month in resolution.per_packet_months.items()
        if month is not None and month != resolution.majority_month
    )


def format_month_histogram(dates: list[PacketDate]) -> str:
    """Informational-only counterpart to format_resolution_report, for a
    teacher with no `_blocks.json` sidecar (see load_block_metadata) --
    every teacher except one with genuine round/period ambiguity. Prints the
    same month histogram a resolution report would, purely as a sanity
    signal for a human skimming the run output (e.g. "this file's dates
    span three different months, is that expected?") -- it never gates or
    alters anything, since there's no block metadata here to resolve
    against in the first place. Only `resolve_block` (which requires
    `BlockMetadata`) is load-bearing; this function never calls it."""
    histogram = Counter(d.month for d in dates if d.month is not None)
    n_parsed = sum(histogram.values())
    if histogram:
        hist = ", ".join(f"{calendar.month_name[m]}: {c}" for m, c in sorted(histogram.items()))
    else:
        hist = "(no packet had a parseable date)"
    return (
        "Date sanity check (informational only -- no block metadata sidecar for this roster, "
        f"nothing here gates the run): month histogram: {hist} ({n_parsed}/{len(dates)} packets "
        "had a parseable date)"
    )


def format_resolution_report(resolution: BlockResolution) -> str:
    """Human-readable report shown by both cli.py (before --confirm-block)
    and review_app.py (before the reviewer's confirmation checkbox) -- the
    same text either way, since the resolution itself doesn't depend on
    which surface is asking."""
    lines = ["Block resolution report:"]
    lines.append(f"  class period used: {resolution.class_period}")
    if resolution.month_histogram:
        hist = ", ".join(
            f"{calendar.month_name[m]}: {c}" for m, c in sorted(resolution.month_histogram.items())
        )
    else:
        hist = "(no packet had a parseable date)"
    lines.append(f"  month histogram: {hist}")
    lines.append(f"  packets with a parsed date: {resolution.n_parsed}/{resolution.n_total}")
    if resolution.resolved:
        assert resolution.chosen_block is not None
        lines.append(
            f"  resolved: {resolution.chosen_block.describe()} "
            f"(confidence {resolution.confidence:.0%})"
        )
    else:
        lines.append(f"  resolved: NONE -- {resolution.reason}")
    return "\n".join(lines)


def decisions_scope_mismatches(decisions: dict[str, str | None], block: str) -> list[tuple[str, str]]:
    """Every (packet_tag, sid) already in a decisions file whose SID's own
    period digits (positions 6:8, same slice as roster.RosterEntry.
    period_display) disagree with `block` -- e.g. a decisions file built
    against block 01 being re-run under a resolved/confirmed block 02.
    Doesn't require the roster or any migration of existing decision files
    (see CLAUDE.md): this reads the check straight off the SID string
    itself, the one thing every decisions file already has."""
    return [(tag, sid) for tag, sid in decisions.items() if sid is not None and sid[6:8] != block]


def resolved_block_record_path(pdf_path: str | Path, decisions_dir: str | Path) -> Path:
    """Where the block a run actually resolved (or was told to use via
    --block) is recorded, once a run gets past the --confirm-block gate --
    a small provenance record, not a decision. Kept separate from
    decisions/<pdf-stem>.json (mirroring overrides_path in pipeline.py):
    decisions' sid|None|absent contract needs no migration to support this
    feature (per the build spec), so block provenance lives in its own
    file rather than being folded into every decision's value shape."""
    return Path(decisions_dir) / f"{Path(pdf_path).stem}.block.json"


def save_resolved_block_record(pdf_path: str | Path, block: BlockMeaning, decisions_dir: str | Path) -> None:
    path = resolved_block_record_path(pdf_path, decisions_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"block": block.block, "class_period": block.class_period, "month": block.month}, indent=2))


def load_resolved_block_record(pdf_path: str | Path, decisions_dir: str | Path) -> dict | None:
    path = resolved_block_record_path(pdf_path, decisions_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text())
