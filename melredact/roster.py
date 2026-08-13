"""Roster loading. The roster only lists students with assent/consent --
"not on this roster" is the definition of "not consented", not an error
state. Anything wrong with the roster itself (a malformed or duplicate SID)
is a data integrity problem that must fail loudly at load time: the whole
assignment guarantee downstream ("each roster entry claimed at most once")
depends on the roster being trustworthy.

The source Google Sheet holds every period a teacher teaches on one tab,
with a blank row separating each period's block of students (John
maintains it this way; it survives every export). A blank row is
structure, not corruption -- it's the only signal in the CSV of where one
period's block ends and the next begins, so it's parsed as a delimiter,
never rejected. A row with only *some* of its three fields blank is still
a real error (neither a valid delimiter nor a valid entry) and still fails
loudly, same as always.

Since a scan file (e.g. "Hannel MPR PD2.pdf") only ever contains one
period's packets, matching a packet's OCR'd name against the *whole* tab
(every period a teacher teaches) needlessly inflates the candidate pool
and makes a wrong-but-confident match more likely -- the margin-based
abstain logic in match.py is calibrated against one period's worth of
surnames, not several periods stacked together. `load_roster(..., period=
..., infer_period_from=...)` narrows the returned `Roster` to one block so
callers (review_app.py, cli.py's `run`) can match against only the relevant
period. Narrowing is skipped entirely when the roster only has one period
in it (nothing to narrow); it's required (raises `RosterError`) only when
the roster spans multiple periods and neither an explicit period nor a
successful filename inference is available -- silently matching against
every period in that case would reintroduce the exact inflated-pool risk
this exists to prevent.

`verify` has the opposite need and gets its own loader, `load_full_roster`:
its job is to catch a leaked name *anywhere*, so it must always check
against every period, never just the one a scan was matched against.
Scoping verify's search the way matching's is scoped would only make it
worse at its one job. Both loaders share `_parse_roster_csv` for the
actual CSV parsing/validation (a malformed or duplicate SID is a data
problem regardless of who's asking); only the narrowing step differs.

The SID's own period digits (`RosterEntry.period_display`, positions 6:8)
are cross-checked against block position while parsing: every entry in a
block must agree with the rest of that block on its period code. A
disagreement means either the blank-row block boundaries were misread or
the sheet itself has a data error (e.g. a mistyped SID) -- both are worth
surfacing immediately rather than silently trusting one signal over the
other.

A roster export can also contain a student who is genuinely consented (on
the source sheet, a real person) but whose SID cannot be trusted -- e.g. a
corrupted numbering run where a SID is duplicated across two different
students, or a number that sits inside such a corrupted run even though
it isn't itself duplicated. Deleting that student's worksheet would be
wrong (they consented); shipping it under a possibly-wrong SID would be
worse (mislabeled real student data). Neither of the roster's existing two
states -- "on the roster" (has a trustworthy SID) or "not on the roster"
(no consent) -- fits, so there's a third: `held_names`
(`data/teacher_codes/<teacher_code>_holds.csv`, columns "Last Name",
"First Name", no SID column -- there's nothing trustworthy to put there).
`load_roster`/`load_full_roster` load this sidecar file when it sits next
to the roster CSV (`holds_path`) and expose it on `Roster.held_names`;
`match.py` scores a packet against held names with the same scorer used
for roster entries, and a packet whose best match is a held name produces
a hold (never a proposal, never a delete) in `pipeline.py`. Held names
aren't period-scoped (the sidecar carries no SID to derive a period from),
so the same full list is attached to every `Roster` returned for this
teacher, scoped or not.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

SID_PATTERN = re.compile(r"^\d{10}$")
REQUIRED_COLUMNS = ("SID", "Last Name", "First Name")
HOLDS_REQUIRED_COLUMNS = ("Last Name", "First Name")


class RosterError(ValueError):
    pass


@dataclass(frozen=True)
class RosterEntry:
    sid: str
    last_name: str
    first_name: str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def teacher_code(self) -> str:
        return self.sid[:6]

    @property
    def period_display(self) -> str:
        """The middle two digits: the period this SID encodes. Shown to a
        reviewer as the likely period, and used by `load_roster`'s period
        scoping (both to cross-check against the sheet's blank-row block
        boundaries, and to narrow the roster a packet is matched against)
        -- but never as a name-matching signal itself, e.g. it plays no
        part in match.py's score_pair."""
        return self.sid[6:8]

    @property
    def student_index(self) -> str:
        return self.sid[8:10]


@dataclass(frozen=True)
class HeldName:
    """A known-consented student whose SID cannot be trusted -- see the
    module docstring's "third state" section. No `sid` field: that's the
    entire point, there's nothing trustworthy to put there. Deliberately
    shares the `first_name`/`last_name` attribute names `RosterEntry` uses
    so match.py's `score_pair` (which only ever reads those two attributes)
    works identically against either kind of entry with no special-casing."""

    last_name: str
    first_name: str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass
class Roster:
    entries: list[RosterEntry]
    by_sid: dict[str, RosterEntry]
    # Known-consented, SID-unresolvable students -- see the module
    # docstring. Not period-scoped (no SID to derive a period from), so the
    # same list is carried through unchanged by both load_roster's period
    # narrowing (filter_by_period) and load_full_roster.
    held_names: list[HeldName] = field(default_factory=list)

    def __contains__(self, sid: str) -> bool:
        return sid in self.by_sid

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


PERIOD_FROM_FILENAME = re.compile(r"PD\s*0*(\d+)", re.IGNORECASE)


def _normalize_period(period: str | int) -> str:
    s = str(period).strip()
    return s.zfill(2) if s.isdigit() else s


def infer_period_from_filename(path: str | Path) -> str | None:
    """Best-effort period guess from a scan filename like "Hannel MPR
    PD2.pdf" -> "02". Returns None (never raises) on no match -- this is
    an inference to fall back on, not something to fail loudly over;
    `load_roster` is what decides whether a failed inference is actually a
    problem, since that depends on whether the roster even spans more
    than one period."""
    m = PERIOD_FROM_FILENAME.search(Path(path).stem)
    return _normalize_period(m.group(1)) if m else None


def filter_by_period(roster: Roster, period: str | int) -> Roster:
    code = _normalize_period(period)
    entries = [e for e in roster if e.period_display == code]
    if not entries:
        found = sorted({e.period_display for e in roster})
        raise RosterError(f"no roster entries found for period {code!r} (roster has periods: {found})")
    return Roster(entries=entries, by_sid={e.sid: e for e in entries}, held_names=roster.held_names)


def holds_path(roster_path: str | Path) -> Path:
    """Where a roster CSV's held-names sidecar would live -- always
    <roster_stem>_holds.csv next to the roster itself (e.g.
    data/teacher_codes/010406_holds.csv for data/teacher_codes/010406.csv).
    Loading is optional: a roster with no corrupted SIDs simply has no
    sidecar file, and `_load_held_names` treats that as "no held names",
    not an error."""
    roster_path = Path(roster_path)
    return roster_path.with_name(f"{roster_path.stem}_holds.csv")


def _parse_holds_csv(path: Path) -> list[HeldName]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [c for c in HOLDS_REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise RosterError(f"holds CSV {path} missing required column(s): {missing}")

        held: list[HeldName] = []
        for row_num, row in enumerate(reader, start=2):  # header is row 1
            last = (row["Last Name"] or "").strip()
            first = (row["First Name"] or "").strip()
            if not last and not first:
                # Tolerate a trailing blank line the same way the roster
                # itself tolerates a period-block delimiter -- not
                # structural here (holds aren't period-blocked), just a
                # harmless blank row a spreadsheet export can leave behind.
                continue
            if not (last and first):
                raise RosterError(
                    f"row {row_num} of {path}: partially blank held-name row "
                    f"(Last Name={last!r}, First Name={first!r}) -- a valid row needs both fields"
                )
            held.append(HeldName(last_name=last, first_name=first))
    return held


def _load_held_names(roster_path: str | Path) -> list[HeldName]:
    path = holds_path(roster_path)
    return _parse_holds_csv(path) if path.exists() else []


def _name_key(last: str, first: str) -> tuple[str, str]:
    return (last.strip().lower(), first.strip().lower())


def _check_no_roster_holds_overlap(entries: list[RosterEntry], held_names: list[HeldName]) -> None:
    """A name that appears in both the roster and its holds sidecar is a
    contradiction, not something to resolve by picking one silently: the
    roster says this SID is trustworthy, the sidecar says this same student's
    SID is not -- see the module docstring's "third state" section. Whoever
    maintains these two files made a mistake (the roster was fixed and the
    stale hold was never removed, or a hold was added for a student who
    already has a valid roster row), and only a human looking at both files
    can say which one is right; guessing here risks exactly the kind of
    silent mislabeling `held_names` exists to prevent."""
    roster_keys = {_name_key(e.last_name, e.first_name): e for e in entries}
    for held in held_names:
        key = _name_key(held.last_name, held.first_name)
        if key in roster_keys:
            entry = roster_keys[key]
            raise RosterError(
                f"{held.full_name!r} appears in both the roster (SID {entry.sid}) and its holds "
                f"sidecar -- a name cannot be both a trustworthy roster entry and a held, "
                "SID-unresolvable one; fix whichever file is stale before loading either"
            )


def _parse_roster_csv(path: str | Path) -> Roster:
    """Parse and validate the full roster CSV -- every period, every
    block -- with no narrowing. This is the one thing `load_roster` (scoped,
    for matching) and `load_full_roster` (unscoped, for verify) share: the
    data-integrity checks (malformed/duplicate SID, partially-blank row,
    SID-period/block-position cross-check) apply identically either way,
    since those are about whether the roster itself is trustworthy, not
    about which period a caller wants. Also the one place `held_names` is
    loaded (see `_load_held_names`) -- both loaders get it for free."""
    held_names = _load_held_names(path)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise RosterError(f"roster CSV missing required column(s): {missing}")

        entries: list[RosterEntry] = []
        seen_sids: set[str] = set()
        block_period: str | None = None
        block_start_row: int | None = None
        for row_num, row in enumerate(reader, start=2):  # header is row 1
            sid = (row["SID"] or "").strip()
            last = (row["Last Name"] or "").strip()
            first = (row["First Name"] or "").strip()

            if not sid and not last and not first:
                # A blank row is a period-block delimiter in the source
                # sheet (John maintains it this way), not an error.
                block_period = None
                block_start_row = None
                continue
            if not (sid and last and first):
                raise RosterError(
                    f"row {row_num}: partially blank row (SID={sid!r}, Last Name={last!r}, "
                    f"First Name={first!r}) -- a valid row needs all three fields; a valid "
                    "period-block delimiter needs all three blank"
                )

            if not SID_PATTERN.match(sid):
                raise RosterError(f"row {row_num}: malformed SID {sid!r} (must be exactly 10 digits)")
            if sid in seen_sids:
                raise RosterError(f"row {row_num}: duplicate SID {sid!r}")
            seen_sids.add(sid)

            entry = RosterEntry(sid=sid, last_name=last, first_name=first)

            if block_period is None:
                block_period = entry.period_display
                block_start_row = row_num
            elif entry.period_display != block_period:
                raise RosterError(
                    f"row {row_num}: SID {sid!r} encodes period {entry.period_display!r}, but this "
                    f"block (starting row {block_start_row}) is period {block_period!r} -- block "
                    "position and SID disagree on this student's period. Either the blank-row "
                    "block boundaries were misread, or the sheet has a data error (e.g. a mistyped "
                    "SID or a missing separator row); check the source sheet before trusting either"
                )

            entries.append(entry)

    _check_no_roster_holds_overlap(entries, held_names)
    return Roster(entries=entries, by_sid={e.sid: e for e in entries}, held_names=held_names)


def load_roster(
    path: str | Path,
    *,
    period: str | int | None = None,
    infer_period_from: str | Path | None = None,
) -> Roster:
    """Parse the full roster CSV, then narrow it to one period's block --
    see the module docstring for why a scan is only ever matched against
    one period. `period` takes precedence when given; otherwise
    `infer_period_from` (typically the scan's own path) is tried. Narrowing
    is skipped when the roster only has one period in it, and raises
    `RosterError` when it has more than one and neither `period` nor a
    successful inference is available -- silently falling through to the
    whole roster in that case would reintroduce the inflated-candidate-pool
    problem this exists to prevent.

    This is the *matching* loader -- `run`/review_app.py use this. It is
    deliberately not what `verify` uses; see `load_full_roster`."""
    roster = _parse_roster_csv(path)

    periods = {e.period_display for e in roster}
    if len(periods) <= 1:
        return roster
    if period is not None:
        return filter_by_period(roster, period)
    if infer_period_from is not None:
        inferred = infer_period_from_filename(infer_period_from)
        if inferred is not None:
            return filter_by_period(roster, inferred)
    raise RosterError(
        f"roster spans multiple periods ({sorted(periods)}) but no period was given and none could "
        f"be inferred from {infer_period_from!r} (expected something like 'PD2' in the filename) -- "
        "pass an explicit period rather than silently matching against every period in the roster"
    )


def load_full_roster(path: str | Path) -> Roster:
    """Parse the full roster CSV and return it entirely unscoped -- every
    period, every student -- with no period argument and no narrowing,
    ever.

    This is the *verify* loader, and it is not interchangeable with
    `load_roster`: `verify_no_leaked_names`'s whole job is to catch a
    leaked name anywhere in a finished file, so it must check against
    every student who might conceivably appear, not just the one period a
    given scan was matched against. Narrowing verify's search space the
    same way `load_roster` narrows matching's would only make verify worse
    at its one job -- it exists to catch mistakes, including a mistake in
    which period a packet actually belongs to. Still raises `RosterError`
    for a genuinely malformed roster (bad SID, duplicate, block/SID period
    mismatch) -- unscoped means "don't narrow by period," not "don't
    validate."
    """
    return _parse_roster_csv(path)
