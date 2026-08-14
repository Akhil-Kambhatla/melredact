# melredact

Pipeline for a classroom research study (MEL MPR+ADR — "Model Plausibility
Ratings") that processes scanned student worksheets: segment multi-page PDF
scans into per-student packets, OCR/read the header block, fuzzy-match the
handwritten name against a consent roster, and redact identifying header
fields so a human reviewer can approve or correct before anything downstream
sees the packets. Real inputs are scanned PDFs pulled from Box and roster
CSVs exported from a Google Sheet — both contain real student PII and are
gitignored (see `data/README.md`); never commit or sync them anywhere.

## Status (handoff)

Working end to end on the real files: review-first workflow, SID-named
output in `out/<teacher>/<period>/<worksheet_type>/<SID>.pdf`, `verify`
passes unscoped and reports an explicit checked-file count (currently 11:
10 MPR + 1 PRT, never 0).

**`data/` reorganized 2026-07-20** into `MPR/`, `PRT/`, `teacher_codes/`,
`samples/` subfolders (was a flat directory). All code that takes a path
(`cli.py`, `review_app.py`) already took `--pdf`/`--roster` as explicit
arguments with no hardcoded default, so nothing in `melredact/` needed to
change — only RUNBOOK.md's example commands and `data/README.md` did. The
OCR disk cache is keyed on file content hash (`ocr.py`), not path, so the
move didn't invalidate it.

**Text layer scramble, found and fixed 2026-07-20:** the kept OCR text
layer was silently corrupted on real scans — see "Each word's text-matrix
horizontal component has to be scaled..." above for the full mechanics and
fix (`melredact.redact._horizontal_scale_for_word`). Regenerating the real
PRT and MPR files with the fix surfaced two further, unrelated bugs, both
now resolved:

- SID 0204150202 (`Hannel MPR PD2_p006`, the original "Ganik" incident
  packet) and SID 0204150203 (`Hannel MPR PD2_p016`): `find_uncovered_
  group_words` flagged the printed "1. Please work on this individually:"
  instruction as unredacted group-row ink — confirmed a false positive by
  rendering the actual pages (real, printed body text, clearly separated
  from the header box; the real redaction box, independently driven by
  `detect_header_band`, already covered every real word of handwritten ink
  in both cases). **Fixed:** the row-assignment window's documented "room
  to spare" margin never held across the real dataset (measured: -10pt to
  +38pt across 42 real pages, 3 negative). Now anchored to the real
  detected header border (`band_bottom`) instead of the indirect
  row_height proxy that produced that swing — see "Correction, 2026-07-20"
  under "Only the Name row may reach the matcher" above and bug #6 under
  "Six regression-tested bugs" for the full before/after. Re-measured: 0 of
  42 pages negative, min +0.42pt. **Regenerated and shipped**:
  `out/020415/02/PCMEL_MPR_ADR/0204150202.pdf` now exists and passes
  `verify`. (0204150203 was already shipped pre-fix and re-verified clean
  after regeneration.)
- SID 0204150204 (`Hannel MPR PD2_p002`): `detect_header_band` can't
  confidently locate the header border — OCR didn't find the printed
  "Group" label on this page. Unrelated to the fix above, genuinely needs
  a human review pass, not more code — **still held back**, no output.
  This is no longer a reason the *other* MPR packets fail to ship, though
  (see the next paragraph): it holds back only its own packet.
  **Update, 2026-07-21:** a real reviewer confirming this packet's decision
  in `review_app.py` and clicking "Run redaction pipeline" found it *still*
  held back — approval couldn't release a detection-confidence hold at
  all, code fixed the same day (see "One of those five holds is
  human-overridable" below). 0204150204 itself is still not shipped: the
  code now supports a human explicitly checking the new "release this
  packet" override after looking at the preview box, but nobody has
  actually done that for this real packet yet, and doing so is a review
  decision, not something this fix does automatically.

**Fail-fast bug found and fixed 2026-07-20, in the same session that
surfaced the two bugs above.** `run_dispositions` used to raise and abort
the *entire* run on the first packet failure — which meant, concretely,
that SID 0204150204's held-back page (first in this file's page order)
blocked every other already-approved MPR packet, including 0204150202,
from ever being attempted at all. Fixed: a per-packet failure (SID not on
roster, unresolved segmentation issues, undetected header border,
uncovered group-row ink, or a leak finding) now produces a `held_back`
result with a `reason` and the run continues to the next packet — see "A
per-packet failure holds back only that packet" below for the full
mechanics. `cli.py run`'s summary line and `review_app.py`'s sidebar both
report held-back packets explicitly now, separate from written/deleted/
pending.

**Current real result, both files, this fix in place:** MPR run — 10
written (9 previously-approved + newly-unblocked 0204150202), 0 deleted, 1
held back (0204150204, `detect_header_band` — a genuine human-review item,
not a bug), 0 pending. PRT run — 1 written (0204150201, re-verified clean
under both fixes), 0 held back, 19 pending (no human has reviewed those
packets yet — correct, not a gap; nothing auto-approves consent). `verify`
run unscoped against the whole out tree: `11 file(s) checked, 0 failed`,
covering both `PCMEL_MPR_ADR/` and `PRT/` subtrees side by side under the
same `<teacher>/<period>/`.

**What's actually still open:** SID 0204150204 needs a human to actually
open `review_app.py`, look at the preview box on that page, and — if it
genuinely covers the name, the same visual check already done informally
earlier — check the new detection-hold override checkbox before
re-running the pipeline; the code path exists now, but nobody has done
that for this real packet yet, and it's a review decision no code should
make on its own. The 19 unreviewed PRT packets need a human review pass
through `review_app.py` before they can produce any output — also not a
code task; nothing should auto-decide consent for real students.

**Incident, 2026-07-20:** running dispositions for a PRT scan deleted all
11 already-approved MPR outputs for the same class. Two compounding bugs,
both fixed same-day (see "Output is deliberately one redacted PDF..." and
"Packet identity and the decisions store" below for the full mechanics):
output was named `out/<teacher>/<period>/<SID>.pdf` with no worksheet-type
segment, so an MPR and a PRT packet for the same student collided on the
identical path; and `run_dispositions` reconciled deletions by sweeping the
whole shared `<teacher>/<period>/` directory against only the *current*
pdf's own `decisions.values()`, so a PRT run with every packet still
pending (empty decisions) deleted every file a *different* pdf's decisions
store had approved, since the sweep had no notion of which pdf actually
wrote which file. Fixed by adding a `worksheet_type` path segment (read off
each packet's own footer, never guessed) and replacing the sweep with a
precise, per-tag ledger (`out/.ledger/<pdf-stem>.json`) that only ever
deletes a file the *same* packet_tag's own prior decision wrote, driven by
an explicit rejection or correction of *that* tag — never by another pdf's
run, and never by a pending packet. The 11 deleted MPR files were fully
recoverable, not lost: `decisions/Hannel MPR PD2.json` (the human-approved
match for each packet) was never touched by the bug — only files already
sitting in `out/` were — so re-running the pipeline against the existing
decisions regenerates byte-equivalent output. The OCR disk cache was also
untouched, so the regenerating run costs seconds, not the original 17.6 min
cold-cache run (see "Speed finding" below).

**Speed finding:** measured cold (cache fully cleared, `data/MPR/Hannel MPR
PD2.pdf`, 44 pages, 22 packets, 11 approved): **17.6 min machine time
end to end**, not the 10-25 min/page (~13h/50-page-file) figure this
section previously cited — that estimate was miscalculated. Real
breakdown: 7.01 min segmentation (88 small header/footer-band OCR calls
@300dpi across all 44 pages, ~4.8s/call), 0s matching (exact cache hit
off segmentation's own header-band call), 10.6 min redaction (22
full-page OCR calls @300dpi, one per page in an approved packet, ~29s/
page). The disk cache (file-hash+page+dpi+bbox) makes a warm re-run of
the *same* file even faster, but does nothing for a new, never-seen
file — 17.6 min is the real cold-file cost. Full-page OCR only exists to
preserve the kept text layer for John's later data extraction — de-
identification itself only needs the header strip.

**Low priority:** a header-only OCR path (as an alternative to
full-page) was previously flagged as the next task, motivated by the
since-corrected 10-25 min/page estimate implying a ~13h cost on a
50-page file. At the real ~29s/page, that problem doesn't exist —
not worth building unless a much larger real file's own measured cold
cost says otherwise.

**Deferred:** a hosted web app (upload/review/download) instead of the
local Streamlit tool. Needs John/Doug sign-off first — moves identifiable
minor data off-device, a different IRB posture than the local tool.

**Real leak found in review, 2026-07-21: PRT packet 14, still in review
(not approved, not shipped) at the time it was caught.** A reviewer
spotted a Group-row list ("Jyoshika, Mohammed, Divya" in the real packet)
sitting fully visible below a redaction box that was correctly positioned
per the printed border but too short for what the student actually wrote
— genuinely new failure shape (vertical overflow past the header's own
detected bottom border), not the sideways-past-`COLUMN_SPLIT_X` overflow
the "Ganik" incident already fixed. Root-caused to `find_uncovered_
group_words`'s own bug #6 fix (anchoring its word-collection window to
`band.bottom`) creating exactly the blind spot bug #6 was trying to avoid
elsewhere: ink genuinely below the border was excluded from consideration
before the coverage check ever ran, so the pipeline would have shipped
this as clean had it been approved and run. Fixed same day — see "Seven
regression-tested bugs" #7 and the "Correction, 2026-07-21" note under
"Two rectangles are redacted per header page" above for the full
mechanics and the fixed check's accepted trade-off (0204150202/
0204150203 will flag again as a known false positive on regeneration).
Regression fixture built fictional ("Priya Xavier Noor"), not the real
names, per the no-real-PII-in-code rule below. **Not regenerated or
shipped as part of this fix** — same "verified, not automatically
re-shipped" posture as the 2026-07-20 fixes below.

Also added this session, as the intended remedy for the accepted
false-positive trade-off above (and as a general backstop for a genuine
detection/coverage miss): the manual-redaction queue (see its own section
below) and a "Confirm & Next" button in `review_app.py` next to "Confirm
decision", so confirming a packet and advancing to the next one is one
click instead of two.

**Round path segment added 2026-08-13** — see "A round segment: the same
worksheet completed more than once, dated" below for the full design.
Output is now `out/<teacher>/<period>/<worksheet_type>/<topic>/<round>/
<SID>.pdf`, one level deeper than the topic segment above. Verified
against the real `data/PRT/010406_PD1_PRT.pdf` (read-only diagnostic:
correctly recovered all 3 real administrations — March 2026/20 packets,
February 2026/19 packets, October 2025/7 packets — 0 disagreeing) and
against real teacher 020415 output (`Hannel MPR PD2.pdf`/`Hannel PRT
PD2.pdf`, regenerated at the new depth): the real PRT file wrote 5
packets cleanly and verified clean; the real MPR file's 11 packets and
the PRT file's other 6 all held back on the already-documented,
already-accepted bug #7 uncovered-ink trade-off (see "Seven
regression-tested bugs" #7) — confirmed this session to be firing far
more broadly on real data than the two packets it was previously measured
against, still not a round-segment regression, still not fixed this
session (needs either manual-queue review time or a follow-up
recalibration, out of scope for a path-layout change). Regenerating also
surfaced and fixed one unrelated pre-existing bug: both real ledgers
(`out/.ledger/Hannel MPR PD2.json`, `.../Hannel PRT PD2.json`) were still
in the bare-SID-string schema from before an earlier session's ledger
migration, which crashed `run_dispositions` outright — reconstructed from
`decisions/*.json` paired against what was actually sitting in `out/`,
into the current `{"sid", "path"}` schema. **What's actually still
open, unchanged from before this session:** SID 0204150204 still needs a
human to review and release its detection-confidence hold; the 19
unreviewed PRT packets (020415) still need a human review pass; and now
also, the bug #7 uncovered-ink trade-off firing broadly on real MPR/PRT
data means most of 020415's real output needs either manual-queue review
or a recalibration before it can ship at all — not something this
session's round-segment work was scoped to fix.

## Consent rule

The roster **is** the consent list. A student not on the roster is not
"unmatched" — they did not consent, full stop. `melredact/roster.py`
enforces this: `RosterError` on any malformed/duplicate SID (the whole
"each roster entry claimed once" guarantee depends on the roster being
trustworthy), but "not found on roster" is never an error, just the
definition of non-consent.

**Non-consented worksheets are deleted, not left in place.** (Reversed by
John, 2026-07-17 — original design left them in place untouched.)

## The roster is one tab, many periods

The Google Sheet roster holds every period a teacher teaches on a single
tab, with a blank row separating each period's block of students (John
maintains it this way; it survives every export). `melredact/roster.py`
parses a blank row as a period-block delimiter, not an error — it's
structure, not corruption. A row with only *some* of its three fields
blank is still a real error and still fails loudly the same as a
malformed or duplicate SID always has.

A scan file only ever contains one period's packets, so matching against
the *whole* tab needlessly inflates the candidate pool and makes a
wrong-but-confident match more likely (see `MIN_SCORE`/`MIN_MARGIN` below
— they're calibrated against one period's surnames, not several stacked
together). `load_roster(path, period=..., infer_period_from=...)` narrows
the returned roster to one block. Narrowing is skipped when the roster
only has one period in it; it's required — `RosterError`, not a silent
fallback to the whole roster — when the roster spans multiple periods and
neither an explicit `period` nor inference from the scan's own filename
(e.g. "PD2" → `"02"`) resolves it.

`verify` has the opposite need and gets its own loader, `load_full_roster`
— every period, no `period` argument, never narrows. Its whole job is to
catch a leaked name *anywhere* in a finished file, so scoping its search
the way matching's is scoped would only make it worse at that job (it
used to call the scoped `load_roster` with no period, which simply raised
`RosterError` the moment the roster spanned more than one period — a
safety bug, since `verify` never accepted a `--period` flag to fix that
with in the first place). Both loaders share `_parse_roster_csv` for the
actual parsing/validation — a malformed or duplicate SID is a data
problem regardless of who's asking; only the narrowing step differs.

The SID's own period digits (`RosterEntry.period_display`, positions
6:8) are cross-checked against block position while parsing every row: an
entry whose SID-encoded period disagrees with the rest of its block
raises `RosterError` immediately, since that means either the blank-row
block boundaries were misread or the sheet itself has a data error — both
worth surfacing, neither worth silently picking a winner between.

## Held names: a third state, for a consented student with no trustworthy SID

**Superseded for 010406 specifically, 2026-08-13 (same day, later): the
supervisor reissued `data/teacher_codes/010406.csv` as a clean two-block
roster** (block 01/02 = plain class periods 1/2, unique SIDs, no gaps, no
repeated names) — see "Teacher 010406 roster reissue" near the end of this
file. `010406_holds.csv` (the sidecar the corrupted export below motivated)
and `010406_blocks.json` (see "Date-driven block resolution" below) were
both retired: the new roster has nothing to hold and its blocks are plain
periods, not period+round pairs. The held-names *feature* stays fully
intact in the codebase for any other teacher whose roster arrives
corrupted the same way — only this specific teacher's sidecar is gone.
The original incident is kept below verbatim, since it's still the
motivating case for why this feature exists at all.

**Added 2026-08-13, motivated by a real corrupted export: `data/
teacher_codes/010406.csv`.** Block 04 of that roster had two SIDs each
claimed by two different students (`0104060410`: Matheuir/Ailer *and*
Reeves/Taylor; `0104060412`: Monterroso/Brayan *and* Sun/Adam) and a gap
where `0104060411` should have been — a corrupted numbering run, not a
one-off typo. `roster.py`'s duplicate-SID check (see "Consent rule" above
and `RosterError` on any duplicate) correctly refuses to load a roster in
that state, and correctly so: there is no way to know which of two
students actually owns a duplicated SID, and guessing would silently
mislabel a real student's data.

But the roster's existing two states don't fit these students either. "On
the roster" means a trustworthy SID; these four rows don't have one.
"Not on the roster" means no consent (see "Consent rule" above) and
triggers `run_dispositions`'s delete rule — and these are real, consented
students (they're in the source sheet; only their SID numbering is
broken). Deleting their worksheets would be actively wrong. A third state
was needed: **held** — known-consented, SID-unresolvable.

**The holds sidecar: `data/teacher_codes/<teacher_code>_holds.csv`,
columns `Last Name`, `First Name`, deliberately no SID column** — there is
nothing trustworthy to put in one. `roster.py`'s `HeldName` dataclass
mirrors this (no `sid` field) but deliberately keeps the same
`first_name`/`last_name` attribute names `RosterEntry` uses, so
`match.py`'s `score_pair` — which only ever reads those two attributes —
works identically against either kind of entry with zero special-casing.
`load_roster`/`load_full_roster` both load this file automatically when it
sits next to the roster CSV (`roster.holds_path`, always
`<roster_stem>_holds.csv`) via the same `_parse_roster_csv` entry point
both loaders already share, and expose it as `Roster.held_names`. A
missing sidecar is not an error — it just means no held names, the
overwhelmingly common case. Held names are **not** period-scoped (there's
no SID to derive a period from), so `filter_by_period` (load_roster's
period-narrowing step) carries the same full `held_names` list through to
every scoped `Roster` unchanged, same as `load_full_roster`'s unscoped
one.

**Matching: a held name is scored with the exact same scorer as a roster
entry, and a packet whose single best match overall is a held name must
never become a normal proposal or an auto-assignment.** `match.propose`
now also scores a packet's name text against `roster.held_names`
(`propose_held`, using `score_pair` unmodified) and
`MatchProposal.is_held_match` says whether the top-scoring candidate
across *both* pools is a held name rather than a roster candidate (ties go
to the held name — of the two possible wrong guesses, silently assigning
a real roster SID to what might actually be an unresolvable-SID student is
the more dangerous one). `assign_all` excludes any proposal with
`is_held_match=True` from the auto-assignable pool entirely, the same way
it already excludes a proposal with no candidates at all — a held-name
packet must never be auto-assigned a roster SID just because some roster
entry also happened to score reasonably well against it.

**Pipeline: a consent hold is fully redacted, but never written to `out/`
and never deleted.** For a still-*pending* packet (tag absent from
`decisions` — see "Packet identity and the decisions store" below for that
three-state contract), `run_dispositions` checks `_held_match_for_packet`
before falling through to the ordinary pending case; if the packet's best
match is a held name, it reports `DispositionResult.consent_hold=True`
(distinct from both `pending` and `held_back` — see below) with a
human-readable `reason` naming the held name, and is excluded from that
run's normal decision handling entirely. Redaction still runs
unconditionally (`_draft_consent_hold_redaction` calls the real
`redact_packet`, proving the geometry itself is sound) — the draft lands
in a real temporary directory that's removed the instant the call
returns, so a consent hold never leaves a file sitting anywhere on disk,
in `out/` or otherwise. This check only ever fires for a *pending* tag: a
human who has already recorded an explicit decision for that tag — a real
roster SID via the review UI's full-roster search, or an explicit
non-consent rejection — has already looked at the packet and made a call
that overrides the automatic name-similarity signal, in either direction.
A `consent_hold` is deliberately **not** folded into `held_back`: the
existing `held_back` bucket means "a data or geometry problem a human
might be able to fix" (see "Packet identity and the decisions store"
below), and `cli.py run` exits 1 whenever it's non-empty. A consent hold
is the opposite — a permanent structural state that no decision or fix
ever turns into a write — so it gets its own count in `cli.py`'s summary
line and `review_app.py`'s sidebar ("N consent-held (no SID)") and does
not affect the exit code.

`review_app.py` surfaces this as a distinct disposition, not silently
folded into "pending": a 🔒 status icon (alongside the existing ⏳/✅/🚫/⚠️
ones) in the "All packets" sidebar list and the packet subheader, plus an
inline `st.info` banner on the packet's own page naming the held name and
explaining that recording a decision (a real SID via roster search, or an
explicit rejection) overrides the hold.

`scripts/prepare_roster.py` is the one-off cleanup this was actually
built for: given a roster CSV export, it (1) drops trailing columns with
no header (a colour legend from the source spreadsheet survived the CSV
export as unlabeled columns), (2) strips trailing punctuation from name
cells (the real file had `Matheuir?`, a spreadsheet artifact), and (3)
finds every SID that appears more than once and moves *all* rows sharing
that SID into the holds sidecar — never guessing or renumbering which row
"really" owns the SID, and never touching a blank period-block delimiter
row. It's idempotent (re-running merges into an existing holds file
rather than overwriting it) specifically so it doesn't clobber a name a
human added by hand afterward — which is exactly what happened for SID
`0104060413` (Osman, Jad): not itself duplicated, so the script's
duplicate-count scan correctly left it alone, but it sits inside the same
corrupted numbering run as the actual duplicates, which makes it
untrustworthy for the same underlying reason. Identifying *that* row
needed a human looking at the shape of the corruption, not a mechanical
scan — so it was added to `data/teacher_codes/010406_holds.csv` by hand,
and removed from the main roster CSV (an SID flagged as untrustworthy
can't also sit in the roster as if it were a normal, trustworthy entry —
that would defeat the entire point of holding it).

## Date-driven block resolution: a block can encode more than period

**Superseded for 010406 specifically, 2026-08-13 (same day, later): the
reissued roster's two blocks are plain class periods, not period+round
pairs, so `010406_blocks.json` was retired and this feature no longer
applies to this teacher at all** — see "Teacher 010406 roster reissue" near
the end of this file, which also downgrades date-driven resolution to a
purely informational month histogram (never gating) for any teacher with
no `_blocks.json` sidecar. The feature itself, and everything below, stays
exactly as built for the next teacher whose roster blocks really do encode
more than period.

**Added 2026-08-13, motivated by a real teacher, 010406, whose four roster
blocks encode class period *and* collection round together, not class
period alone:** block 01 = class period 1/February, 02 = class period
1/March, 03 = class period 2/February, 04 = class period 2/March. Blocks
01 and 02 — and separately, 03 and 04 — contain the **identical 14
students**, since the same class was scanned twice, once per collection
round. A scan named `010406_PD1_PRT.pdf` is unambiguous about class period
(1) but says nothing about which round — the old filename-only inference
(`roster.infer_period_from_filename`, "PD1" → block "01") would resolve
straight to block 01, silently wrong for a March scan. This is severe in a
way ordinary abstain-and-flag can't catch: because the two blocks share
names, a wrong block still produces perfect-looking high-confidence
matches, a review UI showing correct names, human approval, and every SID
in the file wrong by exactly 100 — and `verify` only ever checks for a
*leaked* name, never for "assigned to the correct one of two identically-
named students," so nothing downstream would ever object.

**Block metadata sidecar, entirely additive.** A roster CSV may have an
optional `data/teacher_codes/<teacher_code>_blocks.json`
(`melredact/blocks.py`'s `load_block_metadata`/`blocks_path`, mirroring
`roster.py`'s `_holds.csv` sidecar pattern) mapping each block code to
what it actually means: `{"teacher_code": "010406", "blocks": {"01":
{"class_period": 1, "month": 2}, "02": {"class_period": 1, "month": 3},
...}}`. When this file doesn't exist for a teacher (every teacher except
010406, as of this writing), `load_block_metadata` returns `None` and
every caller (`cli.py run`, `review_app.py`) checks that first and falls
straight through to the existing `--period`/filename-inference path with
*zero* change to behavior, output, or command line — this was a hard
requirement, verified by a dedicated regression test
(`test_no_block_metadata_sidecar_behaves_exactly_as_before`,
`tests/test_blocks.py`) that runs `cli.py run` against the ordinary
synthetic fixture with no `--confirm-block`/`--class-period`/`--block`
flags at all and asserts `rc == 0`, exactly as before this feature
existed.

**Resolution is file-level, never per-packet — this is the actual safety
property, not an implementation detail.** One scanned PDF is one
collection session: a teacher scans a whole class's worksheets from a
single sitting, so the file has exactly one round, and every packet in it
belongs to the same block. `blocks.resolve_block` takes the **majority**
parsed month across *every* packet in the file (`blocks.
collect_packet_dates`, which segments the PDF and OCR's each header page's
already-extracted `date_text` field — the same `extract_header_fields`
call `propose_all` already makes, no new OCR bbox) and only resolves a
block when both hold: at least `MIN_DATED_PACKETS = 3` packets had a
parseable date, and the majority month holds at least `MAJORITY_FRACTION =
60%` of the parsed dates. Either threshold failing means "resolve
nothing" (`BlockResolution.resolved = False`, with a human-readable
`reason`) — a human must then pass `--block <NN>` explicitly rather than
the pipeline silently picking a weakly-supported guess.

A **per-packet** resolution scheme was deliberately rejected, not just not
implemented: a single misread handwritten date would silently route that
one packet into whichever block its own (possibly wrong) date pointed at,
and because the two class-period blocks share identical names, a
wrongly-routed packet would find its own name waiting for it in the wrong
block and match with perfect, silent confidence — the exact failure mode
this feature exists to prevent, just moved from "whole file" to "one
unlucky packet." A packet whose own parsed month disagrees with the
file's resolved majority is still surfaced
(`blocks.disagreeing_packets`) — flagged in `cli.py run`'s output and, in
`review_app.py`, as an `st.warning` next to that specific packet's own
date field — but never used to move that one packet to a different block,
or to hold it back. Students get their own written date wrong often
enough (see `verify_no_leaked_names`'s existing `LEAK_FUZZY_MIN_TOKEN_LEN`
false-positive story for the same general lesson: real handwritten/OCR'd
input is noisy) that a single packet's date is a flag for a reviewer to
notice, not a signal the pipeline should act on.

`parse_month` (`blocks.py`) is deliberately conservative: handles
`M/D/YYYY`, `M/D/YY`, `M-D-YYYY`, and written month names (full or
abbreviated, e.g. "March 31, 2026" or "Mar. 1, 2025"), and returns `None`
— never a best-effort guess — for an out-of-range month/day, a numeric
string that doesn't fully match the expected shape, or plain unparseable
text. This feeds a decision (which of two identically-named blocks a
student's data lands in) with no downstream signal that could ever catch
a wrong guess, so it returns `None` far more readily than most parsing
code would.

**The confirmation gate is the only real defense available, and it is
deliberately not skippable.** No match-quality signal can ever
distinguish a packet correctly assigned to block 02 from the same packet
wrongly assigned to block 01, because the two blocks' students have the
same names — a wrong resolution looks exactly as confident as a right
one. `cli.py run`, whenever block metadata exists for the roster, resolves
first and *always* prints the report (month histogram, packets
parsed/total, class period used, resolved block, and what that block
means in words, e.g. "block 02, class period 1, March") before touching
anything else — then requires an explicit `--confirm-block <NN>` matching
the resolved (or `--block`-overridden) block. Absent `--confirm-block`,
or given one that disagrees with the resolution, `run` prints the report,
explains the disagreement, and exits 1 **before** loading the roster,
segmenting, or touching `out_dir`/`decisions_dir` in any way — verified by
`test_run_without_confirm_block_exits_nonzero_and_writes_nothing` and
`test_run_with_wrong_confirm_block_refuses` (`tests/test_blocks.py`), both
asserting `out_dir` was never even created. `--block <NN>` is the override
for when dates can't be resolved automatically (too few readable dates, no
clear majority) — it still requires `--confirm-block` to match it, so a
human can't accidentally skip confirming just because they supplied the
block directly. `--class-period` (or the scan's own filename `PDn`, same
inference `roster.infer_period_from_filename` already provided) supplies
the *class period* input to resolution — when block metadata exists, the
filename's `PDn` means class period, **never** roster block, and is never
allowed to flow into the block value the way it silently did before this
feature existed.

`review_app.py` mirrors this exactly, as a UI gate rather than a flag: a
"Block resolution" banner (the identical report text `format_
resolution_report` produces for the CLI) followed by a mandatory
confirmation checkbox, shown *before* the sidebar, packet selector, or any
packet is rendered — `_render_block_gate` returns `None` (telling `main()`
to stop rendering anything else) until the box is ticked, keyed per
(pdf stem, resolved block) so a different resolved block gets its own
fresh confirmation rather than silently inheriting a stale tick.  Once
confirmed, every packet's page shows a caption naming the resolved block
in words ("Approving into: block 02, class period 1, March"), so a
reviewer approving a name can see which round they're approving it into,
and a disagreeing packet's own date field carries its own inline warning.

**Stored-decision scope guard.** Both `cli.py run` and `review_app.py`'s
gate check every SID already recorded in `decisions/<pdf-stem>.json`
against the run's resolved block by reading the SID's own period digits
(positions 6:8 — the same slice as `roster.RosterEntry.period_display`,
since a block code *is* the SID period digits it filters the roster to;
see `blocks.decisions_scope_mismatches`) and abort, naming every offending
packet tag and SID, on any mismatch — before any processing happens. This
needs no migration of any existing `decisions/*.json` file: the check
reads straight off the SID string every decisions file already has, not a
new field. Once a run passes the confirmation gate, the resolved block
(not the full decision) is separately recorded to
`decisions/<pdf-stem>.block.json` (`blocks.save_resolved_block_record`) —
a small provenance record, not a decision, kept out of `decisions/
<pdf-stem>.json` itself for the same reason `detection_overrides` lives in
its own file (see "One of those five holds is human-overridable" below):
`decisions`' `sid | None | absent` three-state contract is depended on by
every existing decisions file and every test that reads one, and this
feature doesn't need to put that at risk to add provenance.

## Real scans can arrive in a PDF encoding one of our two readers
mis-parses (`melredact/pdfio.py`)

**Found and fixed 2026-08-13, while attempting to demonstrate the
block-resolution feature above against the real `data/PRT/
010406_PD1_PRT.pdf`.** `segment_pdf` (and every other call site that used
to call `pdfplumber.open` directly on a source scan) silently returned
zero packets for this real, 92-page file — `pdfplumber.open(path).pages`
came back `[]`, no exception raised. Confirmed pre-existing and unrelated
to the block-resolution feature itself: `git stash`-ing every change from
this session and re-running `segment_pdf` against the same file on
unmodified `main` reproduced the identical `0 packets, 0 page_count` —
this would have blocked `cli.py run` on this file regardless, the very
first time anyone tried to process it end to end.

Root cause, confirmed by direct inspection (not assumed): `pikepdf` (a
different, qpdf-based library) opens the same file without complaint —
92 pages, a completely ordinary page tree (`/Root` → `/Pages` → 92
`/Kids`, each a normal `/Type /Page` dict). The file's own xref table and
trailer use bare `\r` (old-Mac-style) line endings instead of `\n`/
`\r\n` — a valid PDF EOL convention, just an unusual one — and
`pdfminer.six` (what `pdfplumber` wraps) mis-tokenizes the `\r`-delimited
xref subsection: its own parsed offsets dict came back keyed `2..280` for
a 280-object file (should be `0..279`), so `trailer`'s `/Root 278 0 R`
resolves to whatever object 278's *shifted* slot actually points at, not
the real catalog — `doc.catalog` (and therefore `PDFPage.create_pages`)
comes back empty, silently, with nothing raised to signal a failure. This
is a library compatibility gap, not a data-integrity problem this
codebase's usual "fail loudly, abstain, never guess" posture applies to
(see "Working preferences" below) — the PDF itself is well-formed, just
written with a valid-but-unusual EOL choice one of our two readers doesn't
handle; there is nothing ambiguous about the file's actual content to
guess about, only a parser bug to route around.

**Fix: `melredact/pdfio.py`'s `open_pdf` is a drop-in replacement for
`pdfplumber.open`,** used at every call site across the codebase that
opens a caller-supplied *source* PDF (`segment.py`, `redact.py`,
`pipeline.py`, `blocks.py`, `review_app.py` — never on this codebase's
own already-pikepdf-written output in `out/` or a manual-queue draft,
which never has this problem in the first place, since we write those
files ourselves). It checks whether `pdfplumber.open(path).pages` comes
back empty and, only then, falls back to a `pikepdf`-resaved copy of the
same file — `pikepdf` already reads the file correctly, and re-saving
through it normalizes the xref/trailer into a form `pdfminer.six` handles,
with no change to any page's content, image data, or metadata. The
repaired copy is disk-cached by the *source* file's own content hash
(`CACHE_DIR/normalized/<hash>.pdf`, gitignored the same as the rest of
`CACHE_DIR` — a repaired copy of a real scan is exactly as identifiable as
the scan itself), the same pattern `melredact.ocr` already uses for the
same reason: pay the one-time resave cost once per distinct input file,
not once per call. Confirmed directly against the real file: `open_pdf`
recovers all 92 pages; cost of the repair itself (one `pikepdf` resave of
a 77MB file) is a few seconds, paid once.

Reproducing the exact `\r`-xref parser bug byte-for-byte in a small
synthetic fixture turned out not to be practical (a minimal pikepdf-
written file with every `\n` swapped for `\r` did not reproduce it — the
real file's own export tool triggers a more specific pdfminer.six code
path this session didn't fully chase down). `tests/test_pdfio.py`
instead tests `open_pdf`'s actual, observable contract by simulating the
symptom directly (monkeypatching `pdfplumber.open` to return zero pages
for a specific path, the same observable behavior the real bug produced)
and asserting the repair mechanism recovers real pages from a
pikepdf-resaved cached copy, plus that an ordinary, unaffected file never
touches `pikepdf` or the cache at all.

## Non-negotiable design decisions

- **Abstain by default.** Auto-assign a name match only when the top
  candidate clears `MIN_SCORE`, beats the runner-up by `MIN_MARGIN`, *and*
  the roster entry is still unclaimed (`melredact/match.py`). Every packet
  still gets a ranked candidate list for human review regardless — "safe to
  auto-assign" and "right candidate to show a reviewer" are different bars.
- **Footer is the only ground truth for segmentation.** "Page X of Y" from
  the printed footer decides packet boundaries; page counts are never
  hardcoded or inferred. Anything that can't be read cleanly (unreadable
  footer, missing header page, page count that doesn't add up) becomes a
  flagged `issue` on the packet, surfaced to a human — never silently
  guessed, dropped, or merged (`melredact/segment.py`).
- **Only the Name row may reach the matcher.** Header rows are anchored
  dynamically per page (never fixed coordinates, so this survives scan
  skew), and Group-row text is extracted only so it can be displayed and
  confirmed *not* used — a roster student named in someone else's Group
  row must never win that packet's match. The row-*value* collection
  window (`segment._assign_words_to_rows`) is deliberately tighter than
  the window used to *find* the printed row labels in the first place: the
  label search stays generous (`HEADER_SEARCH_MAX_TOP`, tolerant of a
  label printing lower than the flat-page measurement under skew), but
  reusing that same generous bound to decide which words belong to a row's
  value let body text below the header (confirmed on the real file: the
  numbered "1. Please work on this individually:" instruction and the
  paragraph under it) bleed into `group_text`. The value window is instead
  self-relative — one more row's height past `group_top`, measured from
  this page's own anchors, plus a small fixed slack
  (`ROW_ASSIGNMENT_BOTTOM_SLACK_PT`) — which stays clear of body text on
  the real file with room to spare. This was never a path to `name_text`
  even before the tightening (name is always the anchor farthest from
  anything below the header, so nearest-anchor assignment can't route
  there), but the group field a reviewer sees should reflect the group row
  and nothing else.

  **Correction, 2026-07-20: "room to spare" does not hold across the real
  dataset, and this is the reason `find_uncovered_group_words` (which
  reuses this same window — see "Two rectangles are redacted per header
  page" below) flagged SID 0204150202 — the original Ganik incident packet
  — as having uncovered group-row ink when regenerating it with this
  session's text-layer fix.** That flag was independently confirmed a
  false positive by rendering the actual page (the flagged words are
  genuinely printed body text, clearly separated from the header box by
  eye), but the *margin* — not this one instance — is the actual finding.

  First, what this session's two other changes are **not** responsible
  for: `_assign_words_to_rows`'s window formula and `ROW_ASSIGNMENT_
  BOTTOM_SLACK_PT` (10pt) are byte-identical between `git show HEAD:
  melredact/config.py` and the current working tree — neither the
  Widths/horizontal-scale text-layer fix (a different module entirely,
  only touches how words are *written* to output, never how they're read
  or assigned to rows) nor the anchor-relative header/border-detection
  change (which *reuses* `header_row_height`'s existing formula for a new
  purpose, border search, but doesn't alter what that formula computes)
  touched this code path. Whatever this margin actually is, this session
  didn't shrink it.

  What it actually is: measured directly (`check_margin` = first real
  body-text word's `top`, from a fresh OCR pass, minus this window's own
  `group_top + row_height + ROW_ASSIGNMENT_BOTTOM_SLACK_PT` cutoff) across
  all 42 real header pages we have — every packet in both `Hannel MPR
  PD2.pdf` (22) and `Hannel PRT PD2.pdf` (20), not just approved ones, so
  this isn't cherry-picked around 0204150202:

  | | min | median | max | mean |
  |---|---|---|---|---|
  | MPR (n=22) | -10.0pt | 5.48pt | 15.92pt | — |
  | PRT (n=20) | -0.16pt | 3.8pt | 38.36pt | — |
  | combined (n=42) | -10.0pt | 5.0pt | 38.36pt | 5.67pt |

  21 of 42 real pages (half) have less than 5pt of margin. **Three already
  have a *negative* margin — the window already overlaps real body
  text — with no involvement from 0204150202 at all:** `Hannel MPR
  PD2_p016` (-10.0pt, SID 0204150203 — the other packet this session's
  regeneration also hit this exact flag on), `Hannel MPR PD2_p036`
  (-4.48pt, decided non-consent, never shipped, so latent rather than
  live), and `Hannel PRT PD2_p020` (-0.16pt, not yet reviewed, also
  latent). 0204150202 itself measured at −0.4pt in the initial
  investigation and +0.32pt in this fuller pass — the sign flip between
  two independent OCR reads of the same word is itself the finding: the
  margin on this page is smaller than OCR's own re-measurement noise
  (~0.7pt between two crops of the same page), so whether this specific
  check fires is closer to a coin flip than a reliable pass.

  Conclusion: the ~24pt "room to spare" figure documented above was real
  for whichever single page it was measured against, but was never
  checked against the rest of the real dataset, and doesn't generalize —
  real per-page variance in the whitespace between the header box and the
  first line of body text (title length, scan skew, how tall PaddleOCR's
  merged bounding box for a line of handwriting comes out) routinely eats
  most or all of the assumed margin. 0204150202's thin margin is not an
  outlier this session introduced; it's confirmation the calibration never
  covered the cases that needed it.

  **Were the already-shipped files actually leaking, or just diagnosed by
  an unreliable check?** Checked directly, not inferred: the three
  negative-margin pages (`Hannel MPR PD2_p016` / SID 0204150203, `_p036`,
  `Hannel PRT PD2_p020`) were rendered — both the real ink underneath and,
  for 0204150203 (the only one of the three actually shipped; `_p036` is
  confirmed non-consent and was never output, PRT `_p020` is still
  pending review), the already-redacted file sitting in `out/`. In all
  three, the real header border (`detect_header_band`'s `band.bottom`) was
  correctly detected and the drawn redaction box fully covers every word
  of real handwritten ink — `Carter, Kingston, Brayden` on 0204150203,
  `Sydney, Grace, Priscilla` on PRT `_p020`, nothing at all on `_p036`
  (empty group row). The negative margin was *only* in the diagnostic
  window that decides which OCR words to check for coverage — the
  redaction rectangles themselves come from `detect_header_band` directly
  and never depended on this window at all. Confirmed: `physical_gap`
  (first real body-text word's `top` minus the *actual detected*
  `band.bottom`, i.e. the real page geometry the redaction box itself
  uses) was positive on **all 42** real pages — min +1.92pt, median
  +5.28pt, never negative — while `check_margin` (the self-relative
  window's own, indirect estimate) swung from -10pt to +38pt. The
  self-relative window was the wrong number in both directions: too wide
  on these three pages, too narrow relative to the box's own real extent
  on others (e.g. PRT `_p014`, where `band.bottom` sits *below*
  `window_max` — that page's window could in principle have clipped real
  overflow ink before this fix, a mirror-image risk this same finding also
  closes off). **Conclusion: "verified clean" was real, not lucky, for
  every already-shipped file — the two systems (border detection, which
  drives the actual pixel redaction, and the row-assignment window, which
  only drove this one diagnostic) are independent, and only the diagnostic
  was ever wrong.**

  **Fixed, not padded: the window is now anchor-relative, the same fix
  applied to the header border itself.** `_assign_words_to_rows`
  (`segment.py`) takes an optional `band_bottom` parameter; when given (the
  real, rasterized header border's own bottom edge, always available at
  `find_uncovered_group_words`'s call site since a band is already
  computed there), the window's bottom bound becomes `band_bottom +
  GROUP_ROW_BAND_SLACK_PT` — a direct measurement, not the `group_top +
  row_height + ROW_ASSIGNMENT_BOTTOM_SLACK_PT` proxy that produced the -10
  to +38pt swing. `GROUP_ROW_BAND_SLACK_PT = 1.5` (config.py) is
  deliberately small, not a bigger version of the same guess: `band_bottom`
  is already close to the truth, so its slack only has to absorb OCR's own
  re-measurement noise at a boundary (~0.7-1pt observed between two
  independent OCR passes over the same word) and a little real overflow
  tolerance, not an assumption about where body text starts. `_assign_
  words_to_rows`'s old self-relative formula is kept, unchanged, as the
  fallback for callers with no rasterized band to anchor to — segment.py's
  own matching/field-extraction path never renders the page, so it has
  nothing else to anchor to, and the cost of that path getting the group
  row wrong is a messier display field, not a leak. Re-measured across the
  same 42 real pages with the new anchor: **zero negative margins** (was
  3), min +0.42pt (PRT `_p020` again — its real `physical_gap` is only
  +1.92pt, so no bottom-window design gets much more headroom than this on
  that specific page), median +3.8pt, max +32.34pt. Regression test:
  `test_band_bottom_anchor_excludes_body_text_the_self_relative_window_
  missed` (`tests/test_segment.py`), built from SID 0204150202's own real
  measured numbers.

  **`0204150202.pdf` and `0204150203.pdf` were not regenerated as part of
  this fix** — the fix is verified (128 tests passing, re-measured margins
  above), but shipping them is a separate, deliberate step pending explicit
  go-ahead, not an automatic consequence of the check passing now. SID
  0204150204 is unrelated to this fix (a different check — `detect_header_
  band` itself failing to locate the "Group" label — see the correction
  above) and stays held back regardless.
- **Redaction floor is the drawn border, not fixed coordinates.** The
  bordered header band detected in the raster (`melredact.redact.
  detect_header_band`) is the primary source of truth. `HEADER_BAND_
  FALLBACK` in `config.py` is a floor on the *result*, not merely a
  substitute used when detection fails outright: whichever edges the
  raster scan actually finds are still clamped outward (never inward)
  toward the fallback numbers, so a smaller-than-expected detected box —
  border detection succeeding on a genuinely faint or partial scan — can
  never under-redact relative to the measured floor.
  Top/bottom detection is corner-based, not a row scan, specifically to
  survive skew: a skewed scan tilts the whole rectangle, and on the real
  file no row-based scan window threads the needle between wide enough to
  find a tilted rule (which occupies a different row at each x) and narrow
  enough to exclude the section title sitting only ~5pt above the box.
  Left/right rules stay close enough to vertical under skew for a global
  column scan to find reliably (unchanged); top/bottom are instead read
  off *those* columns' own vertical extent, searched in a tight band
  around the fallback's own top/bottom (`BORDER_CORNER_SEARCH_SLACK_PT` —
  much tighter than the column search's own slack, since the real file
  leaves only ~5pt of headroom above the box and ~24pt below it before
  hitting title/body text respectively). The envelope across both corners
  (min top, max bottom) is the AABB of the tilted rectangle. Proven across
  a parametrized range of synthetic skew angles in `test_redact.py`
  (`test_redaction_box_covers_name_ink_across_a_range_of_skews`), not just
  eyeballed on one real page.
- **Two rectangles are redacted per header page, not one.** The Group
  row is handwritten across the *entire page width*, not just the left
  (Name/Teacher/Group) column — confirmed on the real file (SID
  0204150202): "King, Sfoh, Braydeh, Ganik" ran from x=184 to x=488,
  well past `COLUMN_SPLIT_X`, into the Date/Period column, at the Group
  row's own height. The original single left-column box never had a
  chance to catch the part of that line past the split — "Ganik" was
  fully legible, unredacted, in the shipped file. `melredact.redact.
  redact_bboxes_for_band` adds a second, full-width rectangle covering
  the Group row's own height and everything below it down to the
  header's own bottom border (`group_row_split_top`), on top of the
  unchanged left-column box. The split between "Date/Period stay
  visible" and "the strip below gets redacted" is this page's own
  located Group-row anchor (`segment.locate_header_anchors`, OCR'd per
  page, not a fixed y) plus `GROUP_ROW_SPLIT_OFFSET_PT` — see config.py
  for the real, row-by-row pixel measurement (a genuinely blank ~6pt gap
  between Period's own ink and the overflow) that offset is centered in.
  `melredact.redact.find_uncovered_group_words` is the geometric proof
  this actually works: it reuses `segment._assign_words_to_rows` to pick
  out just the words OCR assigned to the Group row, and checks each one
  is covered by *some* redaction rectangle — independent of whatever OCR
  thinks that ink says, which matters because the leak that motivated
  this was never about text matching being wrong (see the next bullet).
  `run_dispositions` treats a non-empty result the same as a
  `verify_no_leaked_names` finding: move the draft into the manual-
  redaction queue instead of shipping it, hold the packet back with a
  reason (see "A per-packet failure holds back only that packet" below —
  this used to raise and abort the whole run instead — and "The manual-
  redaction queue is a backstop" below for where the drafted file actually
  goes now instead of being deleted outright).

  **Correction, 2026-07-21 (real leak, PRT packet 14): this same check
  missed Group-row ink that overflowed *downward*, past the header's own
  detected bottom border, not sideways past `COLUMN_SPLIT_X`.** A
  student's group-member list ("Jyoshika, Mohammed, Divya" in the real
  packet; fictional names stand in everywhere in code/tests, never the
  real ones) didn't fit the printed row's height and was handwritten
  below the box entirely — the drawn box itself was correctly positioned
  per the printed border, it was simply too short for what the student
  actually wrote. `find_uncovered_group_words` used to bound its own
  word-collection window at `band.bottom + GROUP_ROW_BAND_SLACK_PT`
  (1.5pt, via `_assign_words_to_rows(..., band_bottom=band.bottom)`) — the
  anchor-relative fix from bug #6 below. That fix was real (it closed a
  real false positive, SID 0204150202/0204150203's printed instruction
  text), but it created a worse blind spot: ink genuinely below
  `band.bottom` was excluded from `rows["group"]` *before* the coverage
  check ever ran, since the box and the check shared the same (in this
  case too-short) idea of where the header ends. A check that can never
  disagree with the geometry it exists to verify isn't independent of
  it — this packet was reviewed and would have shipped as clean.

  There is also no fixed slack past `band.bottom` that could have caught
  this without reintroducing the bug #6 false positive: measured on the
  real file, printed body text can start as little as +1.92pt below
  `band.bottom` on some pages — closer than most plausible handwriting-
  overflow allowances, so no single number reliably separates "real
  overflow ink" from "safe printed text" below the border. Given that,
  `find_uncovered_group_words` now uses the full `HEADER_SEARCH_MAX_TOP`
  bound instead (the same generous limit already used for *finding*
  labels, and the outer bound `header_words` was already fetched under —
  this widens what counts as a *candidate* group word, not what gets read
  off the page at all). This is a deliberate trade: SID 0204150202 and
  0204150203 will flag as held-back again if regenerated (a known,
  accepted false positive — their printed instruction text sits only
  ~4.8pt below `band.bottom`), and that cost is intentional — see "The
  manual-redaction queue is a backstop" below for why a held-back false
  positive is now cheap to clear, versus a silently shipped leak, which is
  not. Regression fixture: `test_group_row_vertical_overflow_below_the_
  header_border_is_not_silently_missed` (`tests/test_redact.py`, unit
  level) and `test_vertical_group_row_overflow_is_auto_held_not_shipped_
  as_clean` (`tests/test_pipeline.py`, end-to-end through the real
  `segment_pdf` → `run_dispositions` path, no monkeypatching).
- **`verify_no_leaked_names` has a fuzzy pass, not just exact-token
  matching.** The reason Cmd+F and the original set-intersection missed
  "Ganik" in the first place: OCR read the handwritten surname "Gonik" as
  "Ganik" — a real, legible token, just not an *exact* match for anything
  on the roster. `fuzz.ratio("ganik", "gonik") == 80.0`, so a second pass
  at `LEAK_FUZZY_MIN_RATIO` (only against tokens that didn't already hit
  exactly) catches this class of miss. That pass turned out to need its
  own floor, `LEAK_FUZZY_MIN_TOKEN_LEN`: run against the real file
  unguarded, every page's printed footer ("Page X of Y") fuzzy-matched a
  real (different-period) roster first name, "Paige" — `fuzz.ratio("page",
  "paige") == 88.9` — failing every single file for no real reason. Short
  tokens are simply too likely to land within one edit of *some* short
  name by chance; the floor only excludes short tokens from the *fuzzy*
  pass, not the exact one.
- **The redaction box carries a re-identification stamp**, not just a
  generic "REDACTED" label: `"SID: <sid>"` then `"PD: <period>"` on their
  own left-aligned lines (`melredact.redact._draw_redaction_box`, wired
  in from `pipeline.run_dispositions` once a decision names a sid). A
  destroyed header still needs to be traceable back to its approved
  student. `REDACTION_STAMP_TEXT` ("REDACTED") remains the fallback for
  any caller that draws a box with no sid to stamp yet (e.g. review_app's
  preview when "Not on roster" is the live selection, or a bare library
  call with no decision behind it).
- **Keep the OCR text layer** in the redacted output rather than
  flattening pages to images (`melredact.redact.redact_packet`). Since the
  output has an OCR-derived text layer, "no text leaked" is no longer
  trivially true the way it was for an all-image page — `verify_no_leaked_
  names` extracts text from *every* page of the finished file and checks
  it against the *whole* roster, not just the header region of the packet
  that was supposedly redacted. `redact_packet(..., flatten=True)` keeps
  the pre-reversal all-image, zero-text-layer behavior available behind a
  flag (John is re-checking the decision with Doug; it may reverse again).
  (Reversed by John, 2026-07-17 — original design flattened to images.)
  **`flatten=True` only gets half of `run_dispositions`'s leak protection.**
  `find_uncovered_group_words` (the geometric, pixel-based check — see "Two
  rectangles are redacted per header page" below) runs unconditionally,
  flatten or not, since it only needs the raster image and the located
  anchors, never the text layer. `verify_no_leaked_names` needs a text
  layer to search, though — a flattened, zero-text-layer page has nothing
  for it to find, so it passes *vacuously*, not because nothing leaked. A
  flattened verify pass and a non-flattened one are not equivalent; don't
  treat a clean flattened result as the same guarantee as a normal one. If
  `flatten=True` ever becomes a real production path rather than a
  fallback flag, `verify`/`run_dispositions` need to assert the pixel
  check specifically ran and passed for that file, not just that
  `verify_no_leaked_names` found no text — right now a vacuous pass and a
  real one look identical from the caller's side.
  One coordinate subtlety worth knowing before touching this: page-point
  space (pdfplumber `top`/`bottom`) and rasterized image-pixel space are
  both top-down, related by a plain `dpi/72` scale — no axis flip. The
  *only* bottom-left-origin coordinate in this module is the raw PDF
  content stream `redact_packet` writes the kept text layer into
  (`_pdf_baseline_y`); getting that flip wrong doesn't crash anything, it
  silently repositions text, so it's covered by a round-trip test through
  the real writer and pdfplumber's real reader
  (`test_coordinate_flip_round_trips_through_real_writer_and_reader`), not
  just re-derived and trusted.
- **Each word's text-matrix horizontal component has to be scaled to its
  own OCR-measured box width, not left at 1** (`melredact.redact.
  _horizontal_scale_for_word`, wired into `_invisible_text_op`). Found on
  the real PRT file, and this is a correctness bug, not a cosmetic one: the
  font dict `_PdfWriter._font` declares has no `Widths` array, so any PDF
  reader — including pdfplumber, which is what `verify_no_leaked_names`
  reads with — falls back to Helvetica's own built-in metrics to compute
  where each character actually sits. Those metrics don't agree with a
  real OCR word box (real scans measure to the ink, which is frequently
  *narrower* than Helvetica's advance width for the same text at the
  font-size `_font_size_for_word` derives from box height — e.g. the real
  file: "A" boxed at 2.16pt wide, Helvetica renders it ~10pt at that
  height). Left unscaled, each word's Tj run advances past the *next*
  word's independently-set x0, so adjacent words overlap in text-space;
  pdfplumber's word-clustering (which groups characters by x-proximity,
  not by which Tj call produced them) then interleaves the overlapping
  runs into a single garbled token on read-back. This turned the real
  file's clean "A Plausibility Ranking Task" into "A PlausibilRitya
  nkinTga sk" — the words themselves were always correct and in order,
  the corruption is entirely positional, introduced by the writer, not by
  OCR. This matters beyond cosmetics for two reasons: `verify_no_leaked_
  names` reads the same corrupted layer it's supposed to be the safety net
  for (a scrambled name could in principle land on a false-positive
  fuzzy-match *or* fail to land on a true one — the check was never wrong
  on its own terms, its input was), and the text layer only exists in the
  first place to survive for John's later data extraction (see "Keep the
  OCR text layer" above), which a scrambled layer can't serve either. Fix:
  scale the text matrix's horizontal component (`sx 0 0 1 x y Tm`, not `1
  0 0 1 x y Tm`) so each word's rendered advance equals its own measured
  `x1 - x0`, using a hardcoded standard-Helvetica AFM width table (no
  reader/writer dependency in this repo ships real font metrics) —
  every word then ends exactly where the next one's own x0 begins,
  regardless of how tight or wide its OCR box was. Regression test:
  `test_adjacent_words_with_tight_ocr_boxes_do_not_overlap_and_read_back_
  in_order`, built from the real file's own measured word boxes for this
  exact line.
  **Re-running the real files after this fix surfaced two more, unrelated,
  pre-existing bugs** (not fixed as of this writing — see "Status
  (handoff)"): regenerating `Hannel MPR PD2.pdf`'s already-approved output
  hit `header border not confidently detected` for SID 0204150204 (OCR
  didn't locate the printed "Group" label on that page, so the anchor-
  relative bottom-border search fell back to a generic constant instead of
  this page's own position — the abstain fired correctly, but why OCR
  missed a printed label there is unexplained) and `uncovered group-row
  ink` for SID 0204150202 (the file at the center of the original "Ganik"
  incident) and SID 0204150203, both flagging the printed "1. Please work
  on this individually:" instruction as unredacted group-row ink. A
  rendered crop of the 0204150202 page confirms that text is genuinely
  printed body copy with a clear visual gap below the header box, not
  missed handwriting — `_assign_words_to_rows`'s self-relative bottom
  window (`group_top + row_height + ROW_ASSIGNMENT_BOTTOM_SLACK_PT`) has
  only ~0.4pt of margin over that instruction's own top on these two real
  pages, not the "room to spare" this window was documented against
  elsewhere in this file. Both failures are the pipeline's own safety
  design working as intended — refuse and delete rather than ship a
  guess — but the *net effect* of running that intended behavior against
  real data was `run_dispositions` deleting two already-approved,
  previously-shipped output files (0204150202.pdf, 0204150204.pdf) as a
  side effect of this session's regeneration. Both are fully recoverable,
  not lost, the same way the 2026-07-20 incident's 11 files were:
  `decisions/Hannel MPR PD2.json` still names their SIDs, the roster and
  source scan are untouched, and the OCR disk cache is still warm — but
  they are *not currently present* in `out/` and won't be until whichever
  of these two bugs blocks each one is actually fixed. Deliberately left
  unfixed this session (out of scope for the text-layer bug this was
  chasing, and 0204150202/0204150203's `ROW_ASSIGNMENT_BOTTOM_SLACK_PT`
  margin needs checking against more real pages before being changed, per
  "calibrate against real measured data" under Working preferences — not a
  one-line guess from a single crop).
- **Real scans have no text layer at all** (confirmed against actual
  files — zero chars, one image per page). `melredact/ocr.py` reproduces
  pdfplumber's `extract_words()` shape from PaddleOCR output so
  `segment.py`/`match.py` never need to know or care which source a word
  came from. PaddleOCR was chosen over Tesseract by direct measurement on
  real scans, not assumption (see `ocr.py` docstring for the numbers).
- **OCR is disk-cached per (file content hash, page, dpi, bbox), not
  re-run per pipeline stage.** `segment.page_words` and `redact.
  redact_packet` both go through `ocr.cached_ocr_words_in_region`, which
  writes/reads `.cache/melredact/ocr/<hash>/page_<n>_<dpi>_<bbox>.json`.
  Before this, is_header_page/read_footer (segmentation), extract_
  header_fields (proposal scoring *and* every review_app rerender of the
  on-screen packet), and redact_packet's own full-page pass each issued
  their own, uncached OCR call — confirmed by inspection, segmentation
  and field extraction crop the *identical* `(0, 0, page.width,
  HEADER_SEARCH_MAX_TOP)` bbox at the same dpi, so review re-opening a
  packet re-ran the exact same OCR every time. Keyed on bbox, not just
  file+page+dpi, deliberately: collapsing header/footer crops and
  redact_packet's full-page request into one cache entry sounds simpler,
  but measured cold on the real 44-page file a full-page OCR call costs
  ~29s/page (635.2s across the 22 pages in 11 approved packets) vs.
  ~4.8s for a small header/footer crop (420.6s across 88 narrow-band
  calls, header+footer × all 44 pages) — forcing every page through a
  full-page OCR up front (during segmentation, for all 44 pages, most of
  which never get redacted) would turn a ~7-minute segmentation step into
  ~21 minutes for no benefit. Keying on the exact bbox keeps each call's
  cost where it already was while still collapsing every *repeat* of the
  same call to one, disk-persisted so it survives a Streamlit restart or
  a second `cli.py run` (confirmed: a warm-cache re-run of the real file
  that originally took ~1h33m cold dropped to ~7.5s). `review_app.py`
  additionally wraps `extract_header_fields` in `st.cache_data` so a
  Streamlit rerun (a button click, Prev/Next) doesn't even repeat the
  in-memory anchor-location work on top of a cache hit.

## Seven regression-tested bugs

1. **Illegible scrawl matched confidently.** Short ink like `"S 8"` could
   score ~100 against an unrelated roster entry via `partial_ratio`-style
   substring containment. Fix: a `MIN_NAME_CHARS` floor is applied to the
   *probe* before trying any name-order variant — checking after the fact
   lets a short candidate variant (a bare last name) let a scrawl score
   high through the same containment behavior, just reached a different
   way. Test: `test_short_probe_floor_applies_before_variants_not_after`.
2. **Group-row contamination ("Shaw/Nuzhat trap").** A roster student's
   name written in someone else's "Group members" row could win that
   packet's match. Fix: words are assigned to the nearest anchored row by
   vertical position, and only `name_text` (never `group_text`) is passed
   to the matcher. Test: `test_group_row_name_does_not_reach_name_field`.
3. **Orphan continuation page silently merged into the prior packet.** A
   continuation page arriving right after a packet that already had all
   its declared pages was at risk of being silently absorbed into that
   (already-complete) packet. Fix: `segment_pdf` checks two independent
   signals — packet already has its declared total, or this page's footer
   number breaks the expected sequence — and closes the current packet
   before falling through to start a new, flagged orphan. Test:
   `test_orphan_page_does_not_get_merged_into_prior_complete_packet`.
4. **Group-row overflow past the redaction column, shipped in real output
   ("Ganik").** A student's group-member list, handwritten across the
   full page width, ran past `COLUMN_SPLIT_X` into the untouched Date/
   Period column — the redacted output that shipped (SID 0204150202) had
   a fully legible group-member name sitting to the right of the black
   box. Compounding the problem, `verify_no_leaked_names`'s exact-token
   check couldn't have caught it even if the coverage bug were fixed
   first: OCR read the handwritten "Gonik" as "Ganik", a real but non-
   matching token. Fix: two redaction rectangles per header page
   (`redact.redact_bboxes_for_band` — see "Two rectangles are redacted
   per header page" above) plus a fuzzy, geometry-independent pass in
   `verify_no_leaked_names`. Tests: `test_group_row_overflow_past_column_
   split_is_fully_redacted`, `test_find_uncovered_group_words_actually_
   catches_a_miss`, `test_verify_no_leaked_names_catches_ocr_garbled_
   near_miss`.
5. **Kept text layer scrambled on read-back, corrupting the leak check's
   own input.** No `Widths` array on the declared font meant any reader —
   including pdfplumber, which `verify_no_leaked_names` reads with —
   fell back to Helvetica's own metrics to place each character, which
   don't match a real OCR word box (see "Each word's text-matrix
   horizontal component..." above). Adjacent words overlapped in
   text-space and read back interleaved: "A Plausibility Ranking Task"
   extracted as "A PlausibilRitya nkinTga sk" on the real PRT file. Fix:
   scale each word's text-matrix horizontal component to its own measured
   box width (`redact._horizontal_scale_for_word`). Test:
   `test_adjacent_words_with_tight_ocr_boxes_do_not_overlap_and_read_
   back_in_order`.
6. **Group-row leak backstop calibrated against one page, wrong on plenty
   of others.** `find_uncovered_group_words`'s window for "which OCR words
   count as group-row ink" used a self-relative estimate (`group_top +
   row_height + ROW_ASSIGNMENT_BOTTOM_SLACK_PT`) documented as having
   "room to spare" over real body text. Measured across all 42 real header
   pages we have: that margin actually ranged from -10pt to +38pt, 3 pages
   already negative — on two of them (SID 0204150202, the original Ganik
   incident packet, and SID 0204150203) the window swept the printed "1.
   Please work on this individually:" instruction into the group bucket
   and `find_uncovered_group_words` flagged it as unredacted ink, a false
   positive (confirmed by rendering the actual pages: the real redaction
   box, driven independently by `detect_header_band`, already fully
   covered the real handwritten ink in every case checked). Fix: anchor
   the window to the real, rasterized header border (`band.bottom`, from
   `detect_header_band`) instead of the indirect row_height proxy, the
   same anchor-relative approach already applied to the border itself —
   `_assign_words_to_rows(..., band_bottom=...)`. Re-measured: 0 of 42
   pages negative, min +0.42pt. See the "Correction, 2026-07-20" note
   under "Only the Name row may reach the matcher" above for the full
   before/after tables. Test: `test_band_bottom_anchor_excludes_body_
   text_the_self_relative_window_missed`.
7. **Group-row leak backstop's own fix for bug #6 created a new blind spot:
   vertical overflow past the border (real leak, PRT packet 14, 2026-07-
   21).** Anchoring `find_uncovered_group_words`'s window to `band.bottom`
   (bug #6's fix) closed the false-positive risk from body text just below
   the header, but meant real Group-row ink handwritten *below* the
   header's own detected border — not caught by the two-rectangle fix
   above, which only ever addressed sideways overflow past
   `COLUMN_SPLIT_X` — was excluded from the word-collection window before
   the coverage check ever ran, and shipped as a silent miss: a fully
   legible group-member list below a correctly-positioned-but-too-short
   box, reviewed and about to ship as clean. Fix: the window now uses the
   full `HEADER_SEARCH_MAX_TOP` bound (the same one already used to *find*
   labels) instead of `band.bottom + GROUP_ROW_BAND_SLACK_PT` — see the
   "Correction, 2026-07-21" note under "Two rectangles are redacted per
   header page" above for why no fixed slack threads this needle safely,
   and for the accepted trade-off (0204150202/0204150203 will flag again
   as a known false positive, cleared via the manual-redaction queue
   rather than silently passed). Tests: `test_group_row_vertical_
   overflow_below_the_header_border_is_not_silently_missed`
   (`tests/test_redact.py`), `test_vertical_group_row_overflow_is_auto_
   held_not_shipped_as_clean` (`tests/test_pipeline.py`, end-to-end, no
   monkeypatching).

## Packet identity and the decisions store

`melredact/pipeline.py` orchestrates segment → propose → apply-decisions.
Two things here are load-bearing for the delete rule and easy to get wrong:

- **Packet identity across runs is the packet's first physical page index**
  (`packet_tag`, e.g. `packets_p008`), not its position in `segment_pdf`'s
  packets list (shifts if an earlier packet's page count changes on a
  re-scan) and not a generated SID (doesn't exist before a decision is
  made). `review_app.py` and `run_dispositions` both key off this tag, so
  a decision recorded in one run still applies to "the same packet" in the
  next.
- **`decisions` (`decisions/<pdf-stem>.json`) is a three-state mapping,
  not a two-state one.** A packet's tag being *absent* from the dict means
  "not yet reviewed" — `run_dispositions` writes and deletes nothing for
  it. A tag present with a SID means an approved match. A tag present with
  `None` means a human has confirmed non-consent — *this* is what actually
  triggers the delete rule, and it deletes any existing output for that
  packet even if a prior run wrote one, since consent can flip between
  runs (a reviewer rejecting a previously-auto-assigned candidate) and
  `out/` is not append-only. Conflating "pending" with "confirmed
  non-consent" would delete output for packets nobody has looked at yet —
  `review_app.py`'s auto-assign suggestion is therefore only ever a
  pre-selected radio default, never written into `decisions` until a human
  clicks Confirm.
- A packet with unresolved `issues` (segment.py) is refused by
  `run_dispositions` even if `decisions` names a SID for it — those issues
  mean a human hasn't confirmed the packet is what its footer claims.
  Rejecting it as non-consent (`None`) is still allowed, since that's a
  human declining to treat it as a valid packet at all, not approving it.

**Output is deliberately one redacted PDF per approved packet, not one
combined PDF per class** —
`out/<teacher_code>/<period>/<worksheet_type>/<SID>.pdf` (e.g.
`out/020415/02/PRT/0204150204.pdf`), where `teacher_code` and `period` are
the SID's own digits (`RosterEntry.teacher_code`, `.period_display`), not
anything read off the packet. (Reworked by John, 2026-07-18 from an
earlier `out/<pdf-stem>_p<page>.pdf` naming keyed off `packet_tag` — de-
identified output is now named and organized entirely by the identity a
reviewer confirmed, not by anything that traces back to the source scan.)
`cli.py run`'s `--out` therefore has to be a directory, never a `.pdf`
path; it errors out early (before touching the filesystem) if given one,
rather than silently creating a directory with a misleading `.pdf`-looking
name.

**`worksheet_type` is its own path segment, not folded into the SID.** A
student has one SID but multiple worksheet types (MPR, PRT, ...), and
`teacher_code`/`period` are both read off the SID alone — so without a
worksheet_type segment, an MPR packet and a PRT packet for the same student
land on the *identical* path and the second one written silently clobbers
the first (a real incident, 2026-07-20: a PRT run overwrote/deleted 11
already-approved MPR outputs — see "Status (handoff)"). `worksheet_type` is
read off each packet's own header-page footer
(`segment.read_footer`/`_parse_worksheet_type`, e.g. "PRT (01/2024)" or
"pcMEL MPR+ADR (06/2025)" — both real, distinct forms; the trailing
"(mm/yyyy)" revision date is stripped, and what's left is slugified into a
directory-safe segment) from the *same* already-read footer-band text
`PAGE_MARKER_PATTERN` searches, not a second OCR call — see "OCR is
disk-cached..." above for why that distinction matters (a second bbox
would mean a second, uncached OCR call across every page of segmentation).
A header page whose
worksheet-type label can't be parsed gets `worksheet_type=None` and an
`issues` entry, the same treatment as an unreadable page marker — never
guessed, never defaulted, since guessing here is exactly what would put a
packet back at risk of the collision above.

**The load-bearing invariant is "present in the output tree" iff "has a
confirmed, approved SID."** Non-consented and pending packets are never in
the tree under any name, including a placeholder — only a packet with a
decision naming a SID ever produces a file. Because the file name is the
SID rather than the `packet_tag` that produced it, a single tag-keyed
delete is not enough to enforce this on its own: a *corrected* decision (a
human overriding an earlier match to a different SID) orphans its old
SID's file exactly the same way a rejection does, just without an explicit
"delete this" step naming that old file.

An earlier design closed this gap with a reconciliation pass that swept
the whole `out/<teacher>/<period>/` directory at the end of every run,
deleting any `<SID>.pdf` whose SID wasn't in the *current* pdf's
`decisions.values()`. This was safe only under the unstated assumption
that exactly one pdf/decisions-store ever writes into that directory — an
assumption the worksheet_type collision above already breaks (two scans,
one directory), and which the 2026-07-20 incident exposed directly: a PRT
run with every packet still *pending* (empty `decisions`) swept the shared
directory against its own near-empty approved-set and deleted every file a
*different* pdf's decisions store had approved, since the sweep had no
notion of which pdf actually wrote which file. A pending packet producing
no output of its own is not the same as a pending-only run being safe to
execute at all, if the run's side effect is a sweep of state it doesn't
own.

**Fix: deletion is now ledger-based and per-tag, never a directory sweep.**
`run_dispositions` persists a small per-`(out_dir, pdf)` ledger
(`ledger_path`/`_load_ledger`/`_save_ledger`, at
`out/.ledger/<pdf-stem>.json` — colocated under `out_dir`, not
`decisions_dir`, since it's derived bookkeeping about what this output tree
contains, not a human-editable decision) of `packet_tag -> last SID
successfully written for it`. A deletion now only ever happens for a
`packet_tag` this same pdf's own ledger says it previously wrote a file
for, when *this run's own* decision for that exact tag says the old SID is
no longer correct: an explicit `None` (confirmed non-consent — the
ledger's old SID's file is removed) or a different SID (a correction — the
ledger's old SID's file is removed once the new SID's file is confirmed
written and clean). A tag absent from `decisions` (pending) is never
consulted against the ledger at all, so it can never trigger a delete of
its own output or anyone else's — nothing about processing a pending
packet touches any path but its own. This holds for all four packet
states: approved-with-SID (written, ledger updated), non-consented
(ledger's prior file deleted if one existed, absent), pending (untouched,
ledger untouched), and corrected (ledger's old SID's file deleted, new
SID's file written, ledger updated to the new SID).

**`verify` walks that same tree, not a flat `<pdf-stem>_p*.pdf` glob.**
Since output file names no longer trace back to a source scan, `verify`
takes no `--pdf` — it globs `<out>/*/*/*/*.pdf` (teacher/period/
worksheet_type/SID) and checks every file it finds against the full
(unscoped) roster. Finding *no* files under `--out` is treated as a hard
failure, not a vacuous pass: silently checking nothing because `--out`
pointed at the wrong place is more dangerous than erroring loudly, since a
clean silent run looks identical to a genuinely clean one. Beyond that
all-or-nothing guard, `_cmd_verify` also prints an explicit `N file(s)
checked, M failed` summary on every run, pass or fail — the per-file `ok`/
`FAIL` lines alone don't make "checked 11 files, all clean" visually
distinct from some subset silently never having been globbed at all.

**The review UI's preview and the real output share one drawing path.**
`review_app.py` calls the exact same `render_redaction_preview` (which
itself shares `detect_header_band`/`_draw_redaction_box` with
`redact_packet`) that produces the final file, stamped with whichever
candidate is *currently selected* in the decision radio — read from
Streamlit's own session state ahead of the radio being instantiated, so
the preview reflects a live "if you confirm this, this is what you get"
rather than a fixed placeholder that could read differently from what
Confirm actually produces.

**A per-packet failure holds back only that packet, never the whole run
(fixed 2026-07-20).** All five reasons a packet with an approved SID can
still fail to redact safely — the SID isn't on the roster, the packet
still has unresolved segmentation `issues`, `detect_header_band` couldn't
confidently locate this page's own border, `find_uncovered_group_words`
found Group-row ink the redaction boxes missed, or `verify_no_leaked_
names` found a leak in the written file — used to be a raised exception
that aborted `run_dispositions` for the *entire* pdf, mid-loop, before
every packet after the failing one was even attempted. This was fail-fast
in the wrong place: a data or geometry problem specific to one packet
(this session's real trigger — SID 0204150204's page, where OCR simply
never found the printed "Group" label) blocked every *other*
already-reviewed, already-approved packet in the same file from being
written, even though nothing about their own redaction was affected.
Blocking N-1 good packets because packet N has a bad header defeats the
review-first design the same way the 2026-07-20 ledger-sweep incident did,
just via a different mechanism (a raised exception instead of an
over-broad directory sweep). Fixed: each of these five conditions now
appends a `DispositionResult` with `held_back=True` and a human-readable
`reason` and `continue`s to the next packet, instead of raising. A
held-back packet produces no output (any partially-written file for it is
still deleted, same as before) and leaves any *prior* output for that tag
completely untouched — a human hasn't confirmed a replacement is safe, so
nothing about an old file changes. `cli.py run`'s summary line grew a
third count for it (`N written, M deleted, K held back for review, J
still pending review`; the CLI now exits 1 if `K > 0`) and `review_app.py`
surfaces one `st.sidebar.warning` per held-back packet_tag/sid/reason so a
reviewer knows exactly which packet to look at and why. This is still
abstain-and-flag, never silently guess — see "Working preferences" below —
just scoped to the one packet that actually has the problem instead of
treating one packet's problem as a reason to distrust every other
packet's already-confirmed decision. Tests: `test_undetected_header_
border_holds_back_only_that_packet_not_the_whole_run`, `test_unknown_sid_
in_decisions_is_held_back_not_raised`, `test_packet_with_unresolved_
issues_is_held_back_even_with_a_decision`, `test_leak_finding_deletes_
output_and_holds_back_not_raises` (all in `tests/test_pipeline.py`).

**One of those five holds is human-overridable; the other four are not
(fixed 2026-07-21).** The fix above stopped one packet's failure from
blocking every other packet, but it left the detection-confidence hold
permanently unreleasable — a reviewer who approved SID 0204150204's
decision and clicked "Run redaction pipeline" in `review_app.py` found the
packet still held back, with no action left that could ever change that.
That's backwards: the entire reason a low-confidence packet routes to a
human is so a person can make the call the geometry alone couldn't, and a
hold nothing can clear makes review decorative for exactly the packets
that need it. But only the detection-confidence hold is actually a
*confidence* question — "header border not confidently detected" still
draws a real box (the anchor- or fallback-derived geometry
`detect_header_band` falls back to even when `detected=False`, not a null
one), and a human looking at `review_app.py`'s own preview of that exact
box can judge whether it covers the name. The other four (unknown SID,
unresolved segmentation issues, `find_uncovered_group_words` finding real
uncovered ink in the pixels, `verify_no_leaked_names` finding a real leak
in the written text layer) are findings of an actual problem, not a
confidence gap — staying non-overridable is the entire point of them
existing, and letting a human wave one of those through would silently
reopen the exact leak (SID 0204150202/"Ganik") this file's other fixes
exist to close.

Fixed: `run_dispositions` takes `detection_overrides: set[str]`, a set of
packet_tags a human has explicitly approved for release from *only* the
detection-confidence hold (`melredact/pipeline.py`). When a tag is in
`detection_overrides`, an undetected-border result no longer deletes
`out_path` and holds back — it falls through instead, but *into* the
uncovered-group-words and `verify_no_leaked_names` checks, which still run
unconditionally on every packet regardless of the override and still hold
back (un-overridably) if either finds a real problem. A written packet
that used the override still carries a `reason` noting it, so it's visibly
distinct from a clean, confidently-detected write, not silently
indistinguishable from one.

`detection_overrides` is persisted separately from `decisions`
(`overrides_path`/`load_detection_overrides`/`save_detection_overrides`,
`decisions/<pdf-stem>.overrides.json`), not folded into a richer
`decisions` value: `decisions`' `sid | None | absent` three-state contract
is depended on by every existing `decisions/*.json` file on disk and every
test that reads one (see "Packet identity and the decisions store" above),
and overloading its value shape to also carry an unrelated override is a
needless way to put that at risk.

`review_app.py` exposes this as its own explicit checkbox next to the
"header border not confidently detected" warning — a separate control from
the Decision radio and Confirm button, deliberately: confirming a SID
match answers "who is this", not "I've looked at the fallback box and it
covers the name", and conflating the two would mean every ordinary
approval silently carried this override too, even for packets where a
human never actually looked at the geometry. Checking it calls
`save_detection_overrides` immediately (same pattern as `_confirm` for
decisions); the sidebar's "Run redaction pipeline" button reloads overrides
fresh from disk the same way it already reloads decisions fresh, and
reports an overridden write with `st.sidebar.info`, separately from a
plain "written" count. `cli.py run` reads the same overrides file and
prints the same per-file `reason` note next to `wrote`. Tests:
`test_detection_override_releases_the_hold_and_writes_the_packet`,
`test_detection_override_does_not_release_an_uncovered_ink_hold`,
`test_detection_override_does_not_release_a_verify_leak_hold` (all in
`tests/test_pipeline.py`).

## The manual-redaction queue is a backstop, not a substitute

**Added 2026-07-21, alongside the PRT packet 14 fix above.** A held-back
packet used to just have its drafted attempt deleted — safe, but a dead
end: a human who wants to actually *fix* a genuine geometry miss (as
opposed to a data problem like an unknown SID) had nothing to work from
and no way to ship a corrected version without re-running the whole
pipeline by hand. The manual-redaction queue
(`pipeline.manual_queue_dir`/`_queue_for_manual_redaction`/
`list_manual_queue`/`release_from_manual_queue`) is the fix, scoped to
exactly the two hold reasons that are actually a *geometry* problem a
human can look at and correct — a detection-confidence hold (`detect_
header_band` couldn't confidently locate the border) and a coverage-check
hold (`find_uncovered_group_words` found real uncovered ink). The other
three hold reasons (unknown SID, unresolved segmentation `issues`, a
`verify_no_leaked_names` text-layer finding) are data or already-final-text
problems, not something a corrected redaction band fixes, and are never
queued — they're still just deleted, held back with a reason, same as
before.

**Queued, never just deleted: the drafted (not-safe-to-ship) attempt is
moved, not copied, into `out_dir/.manual_queue/<pdf-stem>/<packet_tag>.pdf`
plus a `<packet_tag>.json` sidecar (sid, worksheet_type, reason,
pdf_path).** Moved rather than copied because the draft can be exactly as
unsafe as the reason it was held back for — it must exist in at most one
place on disk, never both the queue and (however briefly) anywhere else.
Colocated under `out_dir` (the same gitignored, never-synced tree `out/`
itself already lives in — see `.gitignore`'s existing `out/` entry, which
already covers this), not `decisions_dir`: this is derived bookkeeping
about what this output tree currently can't safely produce, not a
human-editable decision.

**Release re-runs the real redaction with a human-supplied corrected
band, then re-runs the exact same two unconditional checks before writing
anything — the checks are never bypassed, only re-parameterized.**
`redact_packet` (and `render_redaction_preview`, for the UI's live
preview) both grew a `band_override` parameter: when given, it's used in
place of `detect_header_band`'s own automatic detection, but every
downstream step — the two redaction rectangles, `find_uncovered_group_
words` against the *actual* header words — runs exactly the same way
against an override band as an auto-detected one. `pipeline.
release_from_manual_queue` calls `redact_packet` with the human's
proposed band, then checks `uncovered_group_words` and
`verify_no_leaked_names` again; only if *both* still pass does it write to
the real `output_path` and clear the queue entry (`_clear_manual_queue_
entry`) and update the ledger. A wrong correction (still doesn't cover the
ink) writes nothing and leaves the packet queued — the automated checks
always have the final say, regardless of who supplied the geometry. This
is the literal meaning of "backstop, not substitute": a human can release
a packet the automated pipeline couldn't confidently redact on its own,
but cannot release one that's still actually leaking, no matter what band
they supply.

**`review_app.py`'s "Manual redaction queue" panel** (a sidebar checkbox
toggles it in place of the normal per-packet view) lists every queued
entry for the currently-open pdf, shows the drafted attempt that was held
back, and gives four number inputs (left/top/right/bottom) for a
corrected band — previewed live through the same `render_redaction_
preview` mechanism the ordinary decision preview uses, before a "Release
to out/" button actually calls `release_from_manual_queue`. A release that
still doesn't pass surfaces the checks' own reason via `st.error` rather
than silently doing nothing. Tests: `test_manual_queue_release_with_a_
corrected_band_writes_and_clears_the_queue`, `test_manual_queue_release_
with_a_still_insufficient_band_stays_queued` (`tests/test_pipeline.py`),
`test_manual_queue_panel_lists_a_queued_packet_and_can_release_it`
(`tests/test_review_app.py`, full Streamlit AppTest round trip).

## Measured geometry

All of it lives in `melredact/config.py`, measured off three real header
pages (US Letter, 612×792pt, origin top-left):

- Header labels (Name/Teacher/Group/Date/Period) have measured anchor
  boxes, but these are **starting points for locating printed labels via
  pdfplumber word search — never fixed redaction coordinates**, since scan
  skew moves everything.
- Left/right column split at `COLUMN_SPLIT_X = 400`: handwriting in
  Name/Teacher/Group tops out at x=383, the Date/Period column starts at
  x0=413. Split down the middle with slack on both sides — left of the
  split gets destroyed, right of it is left untouched.
- `HEADER_BAND_FALLBACK` (top 58 / bottom 148 / left 38 / right 574) is the
  floor used only when live border detection fails.
- `BORDER_CORNER_WINDOW_PT` / `BORDER_CORNER_SEARCH_SLACK_PT` govern the
  corner-based top/bottom detection that replaces a row scan under skew
  (see "Redaction floor is the drawn border" above) — both measured off
  the real file's actual gaps (~5pt to the title above, ~24pt to body text
  below), not picked in the abstract.
- `ROW_ASSIGNMENT_BOTTOM_SLACK_PT` bounds how far past one row-height below
  `group_top` a word can still be assigned into the group row's value —
  see "Only the Name row may reach the matcher" above.
- Footer band starts at `FOOTER_BAND_TOP = 700`; page marker regex is
  `Page\s+(\d+)\s+of\s+(\d+)`.
- `MIN_SCORE = 82`, `MIN_MARGIN = 12` — calibrated against the real
  ~22-packet file's actual score distribution, not picked in the abstract.
  See the long comment above them in `config.py` before changing either;
  it documents the specific near-miss cases (a hyphenated name at 81.8, a
  genuine roster surname collision caught by the margin) that pin these
  values.

## Teacher 010406 roster reissue, multi-topic PRT worksheets, and output path collisions (2026-08-13)

**The supervisor reissued `data/teacher_codes/010406.csv` as a completely
different shape.** The earlier corrupted-export/dual-round roster (see
"Held names" and "Date-driven block resolution" above) is dead for this
teacher: the new file has exactly two blocks (01/02 = plain class periods
1/2, nothing else encoded), every SID unique, no gaps, no repeated names
across blocks. Loaded and verified directly (`load_roster`/
`load_full_roster` against the real file): 14 entries in block 01, 16 in
block 02, no SID or (first, last) name pair repeats across either block,
loads without raising. `010406_blocks.json` and `010406_holds.csv` do not
exist on disk for this teacher any more (confirmed absent, not just
unreferenced) — `load_block_metadata`/held-names loading both fall
through to their "no sidecar" path for 010406 now, exactly like every
other teacher.

**New guard: a name cannot appear in both a roster and its own holds
sidecar.** `roster._check_no_roster_holds_overlap` (wired into
`_parse_roster_csv`, so both `load_roster` and `load_full_roster` get it
for free) raises `RosterError` if any held name's (first, last) pair
--- compared case-insensitively, since a spreadsheet re-export is exactly
the kind of place casing drifts --- also appears as a roster entry. This
wasn't reachable before today (010406 was the only teacher with a holds
sidecar, and its roster and holds files were built from the same
duplicate-SID scan, so they were disjoint by construction), but it's a
real risk going forward: a roster CSV can be fixed/reissued without its
now-stale holds sidecar being cleaned up at the same time, and a name
sitting in both files is a contradiction — trustworthy enough for a
roster row, but also SID-unresolvable — that only a human can resolve.
Guessing which file is right (e.g. preferring the roster silently) risks
exactly the kind of mislabeling `held_names` exists to prevent in the
first place.

**The real motivating problem: one student, one SID, several legitimate
PRT worksheets.** Diagnostic run (segmentation and header-field
extraction only — no matching, no redaction, no writes, no deletes — 
against the real `data/PRT/010406_PD1_PRT.pdf`, per this session's own
request) found **92 pages, 46 packets**, and the packets are not one
round: the OCR'd Date field groups cleanly into three blocks by
packet_index — packets 1–13 read as March 2026 ("3 30 26", "3-30-26",
...), 14–28 as February 2026 ("2 20 26", "2-20-26", ...), 29/30–46 as
October 2025 ("10 24 25", "10-24-25", ...) — and the OCR'd Name field
shows the *same* roughly-14 students recurring once per block (e.g. a
name OCR'd as "Andew Ferrucio" in the March block, "tndrew Ferrueio" in
the February block, "Andrew Ferrusio" in the October block — three
independent OCR reads of the same real student's handwriting on three
separate worksheets). Two packets (index 29, first_page=56; index 43,
first_page=84) are orphans with no header page (`worksheet_type=None`,
flagged `issues`) and one more (index 44) has an unreadable-footer
`issues` entry mid-sequence — none of that blocks segmentation itself,
each is just its own flagged packet per the usual "abstain and flag,
never guess" rule. **This settles the question this diagnostic was run
to answer: the repeated-PRT problem lives *within* this one file, not
just across separate scan files** — three collection rounds concatenated
into a single PDF, all under one filename with no topic segment in it at
all (`010406_PD1_PRT.pdf`, not `..._PRT_EW.pdf`), so every one of a given
student's three packets computes the exact same `topic_from_filename`
result (`NA`) and therefore the exact same natural `output_path`. The
topic path segment below does nothing to disambiguate *this specific
file* — it's the no-silent-overwrite ledger backstop (also below) that
actually protects it.

**Fix, part one: a `topic` path segment, read from the *source filename*,
not the footer.** Real per-topic scans follow
`<teacher>_PD<n>_<TYPE>[_<TOPIC>].pdf` (e.g. `010406_PD1_PRT_EW.pdf`),
where `TOPIC` is a short code (`EW`, `FR`, `FO`, `WL`, ...) naming which
worksheet session this file is. `pipeline.topic_from_filename` extracts
the trailing underscore-separated segment when the filename matches that
shape and returns the stable literal `NO_TOPIC = "NA"` otherwise — never
guessed, and deliberately a literal rather than an omitted segment, so
`output_path`'s depth (`out/<teacher>/<period>/<worksheet_type>/<topic>/
<SID>.pdf`, one level deeper than before today) stays constant across
every teacher regardless of whether their filenames carry a topic. Topic
comes from the filename and not the footer because it isn't part of the
worksheet's own printed content (unlike `worksheet_type`, which — see
"Packet identity and the decisions store" below — is read off the footer
specifically because it *is* printed, on every page, and must never be
guessed from the source filename the way an earlier design guessed
`--period` from `PDn`). `output_path` gained `topic` as an optional
fourth parameter defaulting to `NO_TOPIC`, so every existing caller that
hasn't been touched (including every existing test) keeps computing the
exact path it always did.

**Fix, part two: writing refuses to overwrite silently, backstopping the
case above where the topic segment alone doesn't help.**
`pipeline._claim_output_path(ledger, tag, out_dir, entry, worksheet_type,
topic)` computes the natural `output_path` and checks this run's own
ledger: if that exact path already exists on disk *and* the ledger
attributes it to a *different* packet_tag, it returns a numbered-suffix
alternative (`<SID>_2.pdf`, `_3.pdf`, ...) instead and a human-readable
collision note; re-processing the *same* tag against its own
previously-claimed path is excluded from the check (not a collision) so
a packet re-run after a decision change keeps overwriting its own prior
file exactly as before. `run_dispositions` and `release_from_manual_
queue` (the two places that ever write to `out_dir`) both go through
this before writing. `DispositionResult` gained `collision_note: str |
None`, surfaced prominently and separately from an ordinary "written"
count: `cli.py run`'s summary line now reads "N written (K collision(s)
avoided), ..." with one `COLLISION AVOIDED for <tag>: ...` line per
occurrence, and `review_app.py`'s sidebar shows one `st.sidebar.warning`
per occurrence the same way it already does for `held_back`.

**The ledger itself changed shape to make this possible.** Deletion has
to remove the *exact* file a tag wrote — including a suffixed one — not a
path recomputed from the SID (recomputing would always land on the
un-suffixed path, deleting nothing for a suffixed file, or silently
"succeeding" against a path that was never the one this tag actually
wrote). `out/.ledger/<pdf-stem>.json` entries are now `{"sid": ...,
"path": ...}` instead of a bare SID string; every deletion (confirmed
non-consent, and a correction superseding an old SID) now unlinks the
literal `ledger[tag]["path"]`, never a freshly-called `output_path`.

**Matching is deliberately unchanged: greedy claim-and-remove stays
exactly as it is.** `match.assign_all` still processes proposals in
descending top-score order and marks a roster entry claimed the instant
one packet auto-assigns to it (see "Non-negotiable design decisions"
above); a second packet in the same file that best-matches an
already-claimed student still abstains to human review rather than being
auto-assigned anywhere else. For a teacher whose students genuinely have
several worksheets each — the exact real shape confirmed above — this is
the *correct* outcome, not a gap to loosen: review_app.py's roster search
lets a human explicitly confirm the same SID against each of that
student's several packets, same as any other decision, and the ledger/
collision-avoidance fix above is what makes it safe to write more than
one packet to the same student without any of them clobbering another.
Loosening claim-and-remove to auto-assign a second, third, ... packet to
an already-claimed SID would reintroduce exactly the risk it exists to
prevent (a merely-similar decoy for an already-claimed entry getting
auto-assigned) for every other teacher, to save a few clicks for one
teacher whose repeated worksheets a human already has to review the
group/date fields of anyway.

**Date-driven block resolution is downgraded to informational for any
teacher with no `_blocks.json` sidecar** (010406 included, now that its
own sidecar is retired). `blocks.format_month_histogram(dates)` prints
the same per-month count `format_resolution_report` would, purely as a
sanity signal in `cli.py run`'s own output — it never calls
`resolve_block` and never gates or alters anything; only a teacher with
an actual `_blocks.json` sidecar goes through the load-bearing
`--confirm-block` gate described above. Wired into `cli.py`'s `_cmd_run`
only (the "run report" this was actually asked for); `review_app.py`'s
sidebar was deliberately left untouched for this — the underlying
`collect_packet_dates` call is a real OCR pass over the whole file, and
`cli.py run` already pays a comparable cost when block metadata exists,
but adding it unconditionally to every Streamlit rerun for every teacher
without metadata risked a real, easy-to-miss perf regression for no
correctness benefit (the checked-in histogram is purely a sanity print,
not something a reviewer is blocked on). Confirmed against the real
010406 PRT file's own dates (via the diagnostic above): only 5 of 46
packets have a numeric date OCR'd cleanly enough for `parse_month` to
accept (e.g. "3 3112026", "3021", "3130126" all fail the strict
`M/D/YYYY`-family regex — real handwriting OCR noise, not a bug) — a
concrete demonstration of why this stays informational-only rather than
becoming a second, weaker gate: a signal this noisy has no business
blocking a run.

## A round segment: the same worksheet completed more than once, dated

**Added 2026-08-13, motivated by the same real file the topic segment
above was built for: `data/PRT/010406_PD1_PRT.pdf` turned out to be three
concatenated PRT administrations of the same ~14 students, not one.** The
topic segment (see above) disambiguates *sessions with different topic
codes in the filename* — it does nothing for this file, since
`010406_PD1_PRT.pdf` carries no topic segment at all, so every one of a
given student's three packets computed the exact same `topic_from_filename`
result (`NA`) and therefore the exact same output path before this
feature, saved only by the no-silent-overwrite numbered-suffix backstop
(`_1.pdf`, `_2.pdf`, `_3.pdf` — safe, but gives a downstream consumer no
way to tell which physical file is which round). What actually
distinguishes the three sessions is each packet's own handwritten Date
field — already OCR'd by `segment.extract_header_fields` for every packet
(`date_text`), with no new OCR bbox required.

**New path segment, one level deeper, constant depth for every teacher:
`out/<teacher>/<period>/<worksheet_type>/<topic>/<round>/<SID>.pdf`.**
`round` is a `"YYYY-MM"` label (`blocks.round_label`, e.g. `"2026-03"`) or
the literal `"undated"` (`blocks.UNDATED_ROUND`) when a packet's own date
can't be confidently parsed — never an omitted segment, same reasoning as
`NO_TOPIC` above: omitting it would make path depth vary by how legible a
class's handwriting happened to be. `pipeline.output_path` gained a fifth
parameter, `round_label`, defaulting to `NO_ROUND` (`= blocks.
UNDATED_ROUND`) so a caller that doesn't care about rounds still gets a
stable, constant-depth path. The no-silent-overwrite suffix backstop
(`_claim_output_path`) still sits underneath this unchanged — it's now the
last line of defense rather than the primary mechanism, exactly the way
the topic segment described it would end up: even two packets that
legitimately land in the *same* round group (see below) but are genuinely
different physical packets still can't silently clobber each other.

**`blocks.parse_year_month` is a new sibling to `parse_month`, not a
replacement — it returns the full `(year, month)`, not just the month.**
Block resolution only ever needs to disambiguate two same-numbered months
*within one roster's own block metadata* (e.g. "is this February or
March"), so `parse_month` dropping the year was fine there. A round label
has to distinguish sessions that can span different *years* — the real
010406 PRT file alone spans October 2025, February 2026, and March
2026 — so round labelling needed the year too. Same conservative posture
as `parse_month`: returns `None`, never a best-effort guess, on anything
out of range, partially matched, or (for a written month name) with no
recognizable 4-digit year nearby.

**Round grouping is contiguous-run majority, the same file-level-not-
per-packet lesson date-driven block resolution already learned the hard
way (see that section above) — just applied one level more granular.**
`blocks.group_into_rounds(packets, dates)` walks a file's packets in
physical page order and looks for *boundaries*: a point where the parsed
year-month changes and the change actually sticks, rather than a single
packet's own date being trusted on its own. Every packet inside a
confirmed contiguous run gets that run's own **majority** label
(`RoundGroup.label`) — not its own individually-parsed value — with the
count of packets whose own parse actually disagreed with the group's
majority tracked separately (`RoundGroup.n_disagreeing`), for a human to
see, never for the pipeline to act on.

**Why grouping beats trusting each packet's own date, concretely:** a
single misread digit in one student's handwritten date (the same class of
noise `disagreeing_packets` already exists to tolerate for block
resolution) would, under a naive per-packet scheme, silently route that
one packet's *output file* into a different round directory than every
other packet from the same physical scanning session — invisible to a
reviewer, since nothing about the packet's name or content looks wrong,
only its date. Confirmed directly against the real file (see the
diagnostic below): only 5 of 010406 PRT's 46 packets have a date OCR'd
cleanly enough for `parse_year_month` to accept at all — most raw text is
things like `"3 3112026"`, `"3021"`, `"2120 26"` (no `/` or `-` separator,
so the strict numeric pattern correctly refuses rather than guesses). A
per-packet scheme fed this input would have most packets landing in
whatever bucket "unparseable" defaults to, with no structure at all. The
grouping scheme instead only needs a *few* successfully-parsed dates near
each true boundary to correctly place all 46 packets — proven on the real
file: the diagnostic run (step below) correctly recovered all three real
administrations, 0 disagreeing, despite the 41 unparseable dates riding
along inside whichever run they physically sat in.

**Boundary confirmation rule, and the one real correction made while
building it:** a boundary at packet *i* (whose own parsed label differs
from the run's current label) is only accepted when the *next* dated
packet's own parse does **not** revert back to the run's old label — not,
as first implemented, only when the next dated packet's parse exactly
repeats packet *i*'s new label. The first version failed a straightforward
case that turned out to matter immediately: a file with exactly one
packet per round (no repeated value to confirm against) had every single
transition treated as an unconfirmed blip and absorbed into the first
round, collapsing three real rounds into one — caught by
`test_three_contiguous_groups_produce_three_round_labels_and_distinct_paths`
(`tests/test_pipeline.py`) before this ever reached real data. The fixed
rule — reject only a change that snaps straight back to the *old* value —
correctly treats "the next value differs *again*, to a third label" or
"there's no more dated data at all" as confirmation, while still catching
the actual failure mode (a lone misread flanked by the same label on both
sides). Both directions are regression-tested:
`test_single_misread_date_inside_a_run_inherits_the_runs_label`
(`tests/test_blocks.py`, the misread-absorption case) and the
three-contiguous-groups test above (the single-packet-per-round case).

**Non-adjacent groups sharing a label are reported, never silently
merged.** `group_into_rounds` never merges two groups just because they
end up with the same majority label — each confirmed boundary always
starts a fresh `RoundGroup`. `blocks.duplicate_round_labels(groups)`
flags any label that appears in more than one group (necessarily
non-adjacent, since adjacent same-label runs would never have been split
in the first place) — a signal the file may not be simply "N sessions
concatenated back to back" the way the round segment otherwise assumes
(e.g. an interleaved scan, or a page reinserted out of order), worth a
human's attention rather than a silent merge. Surfaced in
`blocks.format_round_report`'s own output whenever it fires. Test:
`test_nonadjacent_groups_sharing_a_label_are_reported_not_silently_merged`.

**A group with no parseable dates at all still ships, labelled
`"undated"`.** An unreadable date is not a reason to withhold otherwise-
approved output — see `test_undated_group_still_writes_under_the_
undated_round_segment` — it's only a reason the round segment in that
packet's path can't be more specific than the literal.

**Reporting happens before anything is written, on both surfaces.**
`cli.py run` calls `blocks.collect_packet_rounds` immediately after
segmenting (reusing that same `SegmentResult`, not re-segmenting) and
prints `blocks.format_round_report` — group label, packet count, page
range, disagreeing count, and the non-adjacent-duplicate-label note if any
— before the block-resolution report, before loading the roster, before
touching `out_dir`. The computed `packet_tag -> round_label` mapping
(`blocks.round_labels_by_tag`) is then threaded straight into
`run_dispositions(..., round_labels=...)` so the report and the actual
write use the *identical* computed rounds, not two independent OCR passes
that could in principle disagree. `review_app.py` shows the identical
report text (`format_round_report`) in a "Round grouping" header, always,
for every teacher — unlike the `_blocks.json`-gated block-resolution
banner, this is purely informational and never a confirmation gate: round
disagreement is never held or blocked (see the next paragraph), so
there's nothing here that needs a human's explicit sign-off before
packets are shown.

**A packet's own date disagreeing with its group is flagged, never held
or blocked — the same posture block resolution already takes toward a
single packet's date, deliberately extended here.** `review_app.py` shows
each packet's assigned round label directly in the existing OCR'd-fields
table (a new "Round (assigned)" row, right under "Date", so a reviewer
approving a name can see which administration they're actually approving
it into) and, when `blocks.round_disagreeing_tags` flags this specific
packet, an `st.warning` naming the packet's own raw date next to the
group's label — informational only. `run_dispositions` never reads this
signal at all; a disagreeing packet is written to its *group's* path
exactly the same as any other packet in that group. Students get their
own written date wrong, and OCR misreads a correctly-written one, often
enough (the same lesson `LEAK_FUZZY_MIN_TOKEN_LEN`'s false-positive story
and block resolution's own `disagreeing_packets` already taught) that a
single packet's date is a flag for a human to notice, never a signal the
pipeline should act on.

**Round labelling has zero influence on matching, scoring, or claiming —
verified, not just asserted.** `match.propose`/`match.assign_all` take a
packet's `name_text` and the roster; neither was touched by this feature,
and neither ever sees a packet's date or round label. Regression test:
`test_round_label_does_not_alter_match_proposals` (`tests/test_
pipeline.py`) builds two packets identical in every field except
`date_text` and asserts `propose_all` returns byte-identical candidate
lists (roster and held-name) for both — round is output-path metadata
only, added after matching has already run, never an input to it.

**`release_from_manual_queue` computes its own round rather than
threading it through the call chain,** since a single queued packet (see
"The manual-redaction queue is a backstop" below) has no group context of
its own to derive a round from — releasing one packet re-reads that
packet's own date via a fresh `collect_packet_rounds(pdf_path)` call. This
is a comparatively rare, human-driven action (clicking "Release to out/"),
not a per-packet hot path, and OCR is disk-cached regardless (see "OCR is
disk-cached" above), so the extra pass costs nothing on a warm cache.

**Real diagnostic, read-only (no matching, no redaction, no writes, no
deletes) against the real `data/PRT/010406_PD1_PRT.pdf` (92 pages, 46
packets):** the grouping correctly recovered all three real
administrations in physical page order —

```
Round grouping report:
  2026-03: 20 packet(s), pages 1-40, 0 disagreeing
  2026-02: 19 packet(s), pages 41-78, 0 disagreeing
  2025-10: 7 packet(s), pages 79-92, 0 disagreeing
```

— with 0 disagreeing in every group despite the heavy OCR noise
documented above (only 5/46 packets parsed at all): the few dates that
*did* parse cleanly (e.g. `"3-30-26"` at page 25, `"2-20-26"` at page 41,
`"10-24-25"` at page 79 — all hyphen-separated, which is exactly why they
cleared the strict numeric pattern when space- or no-separator variants
like `"3 30 26"`/`"3130126"` didn't) landed close enough to each real
boundary to confirm it, and the 41 unparseable dates rode along inside
whichever run they were physically part of without needing to parse at
all. This matches the "same ~14 students, three sessions" shape the
original 92-page/46-packet diagnostic (see "Teacher 010406 roster
reissue" above) already inferred from repeated OCR'd names across three
date clusters — the round-grouping feature turns that same shape into an
actual, load-bearing output-path decision instead of just an observation.

**Regenerating real teacher 020415 output at the new path depth surfaced
one unrelated, pre-existing bug, now fixed, and reconfirmed one already-
documented, still-open limitation — neither is a round-segment
regression.** Running `cli.py run` against the real `data/MPR/Hannel MPR
PD2.pdf` and `data/PRT/Hannel PRT PD2.pdf` (to satisfy exactly this
session's own requirement — "make sure that path still works" — against
real, not synthetic, data) crashed immediately with `TypeError: string
indices must be integers, not 'str'` in `run_dispositions`'s `prior_sid =
prior_entry["sid"]` line: `out/.ledger/Hannel MPR PD2.json` and `out/
.ledger/Hannel PRT PD2.json` were still in the *bare-SID-string* ledger
schema from before the topic-segment session's own ledger migration (see
"The ledger itself changed shape" under "Teacher 010406 roster reissue"
above) — that session added the `{"sid": ..., "path": ...}` shape and the
code to read it, but never actually re-ran `cli.py run` against these two
real files to migrate the ledgers sitting on disk. Not a round-segment bug
(reproduces identically on a `git stash` of this session's own changes),
but it blocked this session's own verification step, so it's fixed here:
both ledgers were reconstructed from `decisions/*.json` (still fully
trustworthy — decisions were never touched) paired against the real files
already sitting in `out/` at their pre-round paths, into the current
`{"sid", "path"}` schema.

With the ledgers fixed, the real PRT file wrote 5 packets cleanly to the
new depth (`out/020415/02/PRT/NA/2025-10/<SID>.pdf`, one round, "October
2025") and held back 6 with `uncovered group-row ink` naming fragments of
the printed title "Plausibility is..." — and the real MPR file held back
all 11 with the *same* check naming fragments of the printed "1. Please
work on this individually: Is extreme weather relevant..." instruction
text. Both are the already-documented, already-accepted bug #7 trade-off
(see "Two rectangles are redacted per header page"'s "Correction,
2026-07-21" and "Seven regression-tested bugs" #7 above): widening
`find_uncovered_group_words`'s window to `HEADER_SEARCH_MAX_TOP` to catch
real vertical Group-row overflow also means printed body text close below
the header re-triggers the same check. CLAUDE.md already named
0204150202/0204150203 as accepted false positives on regeneration; this
session's real run shows the same trade-off firing on effectively *every*
packet in both real files, not just those two — a wider real-world cost
than previously measured, still the accepted trade-off (a silently
shipped leak is worse than a held-back false positive cleared through the
manual-redaction queue), but worth recording plainly rather than
undersold. **Not a task for this session to fix** — clearing it needs
either a human manually releasing each packet through the manual-
redaction queue (real review time, one packet at a time) or a follow-up
recalibration of the coverage-window trade-off itself, out of scope for a
path-layout change. The 5 successfully-written PRT files were verified
clean (`cli.py verify`, 20 files checked across the whole real `out/`
tree including the untouched 11 MPR files at their old, pre-round path,
`0` failed) and the 5 stale duplicate files left behind at the old,
pre-round PRT path were removed (confirmed with a human first, since
deleting real output files is exactly the kind of action this project's
own working agreement requires checking before doing) once the new-depth
copies were confirmed written and verified. The 11 MPR files were left
untouched at their old path, since nothing safely regenerated them this
session — deleting a still-current, still-valid file just because its
path shape predates a later feature would be actively wrong.

## Round-scoped claiming, a --round pilot flag, a deletion safety switch, and read-only redaction analysis (2026-08-13)

**Motivated by the same real file the round segment above was built for:
`data/PRT/010406_PD1_PRT.pdf`, 92 pages / 46 packets, three concatenated
PRT administrations of the same ~14 students (October 2025, February
2026, March 2026) — see "Teacher 010406 roster reissue" and "A round
segment" above.** Round grouping gives each administration its own output
path, but two problems remained before this file could actually be
processed for real, safely, for the first time:

**Problem one: `match.assign_all`'s claim-and-remove was whole-file, and
whole-file claiming is wrong once one file can hold more than one round.**
Within a single collection session, claim-and-remove is real safety, not
an arbitrary restriction — a file routinely has more packets than roster
entries (non-consented students also have worksheets), so an eager
matcher could hand a consented student's SID to a different, merely-
similar packet's worksheet if nothing stopped it once a roster entry was
already claimed. But a student legitimately has *one packet per
administration* — the same real student appears once in each of the three
round groups in `010406_PD1_PRT.pdf`. Whole-file claiming would let the
March packet claim that student's SID and then silently force the
October and February packets for the *same, correctly-matched* student to
abstain to human review, not because anything was ambiguous, but purely
because an unrelated packet in a different round happened to be processed
first. That's a false abstention, not protection against anything — the
risk claim-and-remove exists to prevent (a decoy stealing an already-
claimed SID) doesn't cross round boundaries, since the two packets
competing for the same SID across rounds are, correctly, the *same*
student's own two worksheets.

**Fix: `assign_all(proposals, round_labels=None)`** (`melredact/match.py`)
takes an optional `packet_tag -> round label` mapping (the exact shape
`blocks.round_labels_by_tag` already produces) and scopes the `claimed`
set to *within* each round label rather than one set for the whole file.
Left as `None` (the default), every packet is treated as one single group
— byte-identical to the old whole-file behavior, so every caller that
hasn't been updated for round-scoping (there were none besides
`review_app.py`, now updated) keeps its exact existing behavior with zero
code change required. `round_labels` is computed entirely outside
`match.py` (by `blocks.py`, from OCR'd dates) and passed in as plain
data — match.py stays free of any date/round logic of its own, matching
the existing separation of concerns between the two modules. Tests:
`test_same_student_matched_in_three_round_groups_auto_assigns_in_all_
three`, `test_two_packets_in_the_same_round_group_matching_same_student_
second_abstains` (`tests/test_match.py`). `review_app.py`'s own
`assign_all(proposals, round_labels=round_labels)` call was updated to
pass the round labels it already computes for the round-grouping report —
one line, since the round labels were already sitting right there.

**Problem two: there was no safe way to pilot a small slice of a
46-packet, three-round file that had never been through this code
before, and no way to guarantee a later round's run couldn't disturb an
earlier round's already-shipped output.** Two additions:

- **`--round <label>`** (both `cli.py run` and `review_app.py`, e.g.
  `--round 2025-10`) restricts a run/session to one round group's
  packets, via `pipeline.filter_packets_by_round(pdf_path, segmented,
  round_labels, round_label)` — filters the already-segmented,
  already-round-grouped `SegmentResult` down to just the packets whose
  own tag maps to the chosen round label. Round grouping itself is always
  computed over the *whole* file first (grouping is inherently file-level
  — see blocks.py's own module docstring on why a contiguous run, not a
  single packet's own date, is what's trusted), so this filters *after*
  segmentation and grouping, never by trying to segment only part of the
  file. Every caller downstream of the filtered `SegmentResult`
  (`propose_all`, `assign_all`, `run_dispositions`,
  `analyze_redaction_holds` below) only ever iterates
  `segmented.packets`, so a packet outside the chosen round is not
  segmented for matching, not shown (`review_app.py` restricts its whole
  packet selector, sidebar counts, and "All packets" list to the filtered
  set), not redacted, not written, and — the actual safety property —
  never looked up in the ledger at all, since `run_dispositions` only
  ever consults the ledger for a `packet_tag` it's currently iterating.
  This is what makes "a later round's run can never delete or disturb an
  earlier round's output" true by construction rather than by a separate
  check: an excluded tag is never visited, full stop, so nothing about
  processing round B can read, write, or delete anything belonging to
  round A's own tags, even if round A's tags are sitting in the very same
  `decisions` file. Test:
  `test_round_scoped_run_never_touches_another_rounds_ledger`
  (`tests/test_pipeline.py`) proves this directly: a round-B-scoped run
  is handed an explicit non-consent decision for a round-A tag, and the
  round-A file it had already written survives completely untouched,
  because the round-B-filtered `SegmentResult` never contains that tag to
  begin with.

- **`allow_delete: bool = True`** on `run_dispositions` (and `--no-delete`
  on `cli.py run`, and a "Disable deletion (safety)" sidebar checkbox in
  `review_app.py`, both defaulting to deletion enabled — unchanged
  behavior unless explicitly opted out of) is a blanket switch,
  independent of `--round`: when `False`, every deletion the run would
  otherwise perform — a confirmed non-consent packet's prior output, or a
  correction superseding an old SID — is skipped entirely, with the
  ledger entry for that tag left exactly as it was and the skipped
  deletion surfaced via `DispositionResult.reason` ("deletion disabled
  for this run — `<path>` left in place"), never silently dropped.
  Matching, redaction, and writing new output all proceed completely
  normally; only deletion is suppressed. This exists for exactly the
  situation this session's own pilot was in: a real file that has never
  been through this code before, where the safest way to do a first pass
  is to make the one truly irreversible step (deletion) categorically
  impossible for that run, regardless of what any individual decision
  says. Tests: `test_allow_delete_false_leaves_confirmed_non_consent_
  output_in_place`, `test_allow_delete_false_leaves_stale_correction_
  file_in_place` (`tests/test_pipeline.py`).

**A new read-only diagnostic: `python -m melredact.cli analyze --pdf ...
--roster ...`, backed by `pipeline.analyze_redaction_holds`.** Before this
session, there was no way to size up how many packets in a real file
would hit CLAUDE.md's already-documented bug #7 uncovered-ink trade-off
(see "Two rectangles are redacted per header page"'s "Correction,
2026-07-21" and "Seven regression-tested bugs" #7) without actually
running the pipeline for real. `analyze_redaction_holds` drafts every
header packet's redaction to a scratch file inside a `TemporaryDirectory`
removed the instant the function returns — the same pattern
`_draft_consent_hold_redaction` already uses for a consent hold, for the
same reason: prove the real geometry/leak checks actually ran, without
ever leaving a file on disk — and classifies each packet using
`run_dispositions`' own exact hold precedence (detection confidence
first, then uncovered-ink, then a full `verify_no_leaked_names` pass,
each gating the next the same way `run_dispositions`'s own `continue`s
do), so a packet reported here as "held for detection confidence" is the
same packet a real run would actually hold for that reason, not a looser
union of every check that happens to fire on it. `format_hold_analysis_
report` prints per-round-group counts (clean / detection-confidence hold
/ uncovered-ink hold / leak hold). `analyze` never writes to `--out`,
never touches `decisions` or the ledger, and never deletes anything —
it's pure sizing-up before committing to a real run. Tests:
`test_analyze_redaction_holds_never_writes_anything`, `test_analyze_
redaction_holds_agrees_with_a_real_run_for_a_clean_packet`, `test_format_
hold_analysis_report_groups_by_round` (`tests/test_pipeline.py`).

**Real diagnostic, this session, against the real, never-before-processed
`data/PRT/010406_PD1_PRT.pdf` (92 pages, 46 packets) — `cli.py analyze`,
read-only, nothing written, redacted to disk, or deleted:**

```
Round grouping report:
  2026-03: 20 packet(s), pages 1-40, 0 disagreeing
  2026-02: 19 packet(s), pages 41-78, 0 disagreeing
  2025-10: 7 packet(s), pages 79-92, 0 disagreeing

Redaction hold analysis (read-only):
  2025-10: 6 packet(s) -- 5 clean, 0 detection-confidence, 1 uncovered-ink, 0 leak
  2026-02: 18 packet(s) -- 18 clean, 0 detection-confidence, 0 uncovered-ink, 0 leak
  2026-03: 20 packet(s) -- 11 clean, 0 detection-confidence, 8 uncovered-ink, 1 leak
```

(Each round's hold-analysis count is one lower than its round-grouping
count because `analyze_redaction_holds` skips orphan packets with no
header page — one of the two orphans this teacher's file was already
known to have, at physical page 84, falls inside the October range.) Total
wall time: 67 minutes (`1:07:08`, cold OCR cache) — longer than the
~29s/page figure this file's own size would predict from the 44-page/
22-packet measurement in "Speed finding" above, since that measurement's
per-packet cost was for a *single* full-page redaction pass per approved
packet, while `analyze` drafts one for *every* header packet in the file
(all 46, not just ones with a decision) specifically so it can report a
real hold count before anything is decided. Confirms bug #7 (see "Seven
regression-tested bugs" #7) is real and broad on this file too, not just
020415's: 9 of 44 scored packets (20%) held for uncovered-ink and 1 for a
text-layer leak — consistent with, and not worse than, the already-
documented 020415 finding under "Teacher 010406 roster reissue" above.
This is sizing information only; clearing it is the same already-accepted
manual-queue-or-recalibration work, not something this session did.

**October 2025 round pilot — matching + dry redaction only, `--no-delete`
posture (no decisions exist yet, so nothing would have written or deleted
regardless; see below), nothing written to `out/`, `decisions/`, or the
ledger:**

```
010406_PD1_PRT_p078  Jordan white          -> Jordan White        score=100.0 margin=40.0  auto-assign  HELD (uncovered-ink)
010406_PD1_PRT_p080  Logan Mack            -> Logan Mack          score=100.0 margin=40.0  auto-assign  clean
010406_PD1_PRT_p082  John Colatruslio      -> John Colatruglio    score=93.8  margin=39.8   auto-assign  clean
010406_PD1_PRT_p084  (orphan, no header page -- not scored, not redacted)
010406_PD1_PRT_p086  Elsilia Ring          -> Elsilia Ring        score=100.0 margin=32.5   auto-assign  clean
010406_PD1_PRT_p088  cristelyAuil          -> Ella Curro          score=67.5  margin=7.5    falls to review (below MIN_SCORE)
010406_PD1_PRT_p090  Liliana Gingerell     -> Liliana Gingerelli-Leone score=90.0 margin=12.9  auto-assign  clean
```

5 of 6 scored packets auto-assign cleanly; the sixth (`p088`, OCR'd
`"cristelyAuil"`) correctly abstains to human review — its best candidate
scores 67.5, well under `MIN_SCORE = 82` — exactly the abstain-by-default
behavior this codebase is built around, not a bug. `p078` (Jordan White)
is the one hold, for the same bug #7 uncovered-ink false-positive class:
`{'text': 'Plausibilit', ...}` at `top=180.72`, a fragment of the printed
worksheet title, not real handwriting.

**"Gio Bariciano" (the name asked about) is not on the class period 1
roster.** It's the *first physical packet in the file*
(`010406_PD1_PRT_p000`) — but that packet round-groups into **2026-03**
(March), not October; the file's physical page order is March, then
February, then October (see the round report above), so "first in the
file" and "in the October round" are different packets. OCR'd
`name_text` is `'GioBarisciano'`. Scored against all 14 class-period-1
roster entries, the top candidate is Brian Lu at score 60.0 — well under
`MIN_SCORE = 82` — and no roster row has "gio" in the first name or
"baric-" in the last name anywhere in the block. This packet would
correctly never auto-assign and needs a human decision (most likely
non-consent, pending an actual look at the scan, since no plausible
roster candidate exists).

**Confirmed a way to disable deletion exists for a run, per this
session's own `allow_delete`/`--no-delete` addition above** (this
teacher's data has never been through this code before, so no prior
output exists to accidentally delete yet regardless — but the mechanism
itself is real and tested, not just asserted): `run_dispositions(...,
allow_delete=False)`, `cli.py run --no-delete`, and `review_app.py`'s
"Disable deletion (safety)" sidebar checkbox. Tests: `test_allow_delete_
false_leaves_confirmed_non_consent_output_in_place`, `test_allow_delete_
false_leaves_stale_correction_file_in_place` (`tests/test_pipeline.py`),
`test_run_no_delete_flag_leaves_prior_output_in_place` (`tests/
test_cli.py`).

**Nothing was written, redacted to disk, or deleted for 010406 as part of
this session** — both diagnostics above are read-only by construction
(`analyze_redaction_holds` drafts to a `TemporaryDirectory` removed
immediately after inspection). A real run for this teacher — even just
the October pilot — still needs an actual human review pass through
`review_app.py` (or hand-authored `decisions/*.json` entries) before
`cli.py run` would write anything; this session only sized up what that
run would look like.

## A drag-corner editor for the manual-redaction queue (2026-08-13)

**Added a real editor to the manual-redaction queue, replacing the old
four-number-input band correction.** The manual queue itself is unchanged
in scope: it is still a fallback for exactly the two hold reasons that are
genuinely a *geometry* problem a human can correct (a detection-confidence
hold and an uncovered-group-row-ink hold — see "The manual-redaction queue
is a backstop, not a substitute" above), never a substitute for the
automatic path, which stays the primary route for every packet that passes
cleanly. The old editor asked a reviewer to type four raw numbers (left/
top/right/bottom points) with no visual feedback beyond a static preview
image; the new one is a real side-by-side editor in `review_app.py`
(`_render_manual_editor`): the original scan page on the left, the live
redacted result on the right, both at `review_app.py`'s existing preview
DPI. A reviewer drags the corners of pre-seeded rectangles — or draws new
ones — directly on the image, using
[`streamlit-drawable-canvas`](https://pypi.org/project/streamlit-drawable-canvas/)
(added to `requirements.txt`; chosen over hand-built drag handles per this
session's own instruction to use an existing canvas/cropper component)
rather than typing coordinates blind.

**Compatibility shim, found while wiring the dependency in:**
`streamlit-drawable-canvas==0.9.3`'s own `st_canvas()` calls
`streamlit.elements.image.image_to_url(image, width, clamp, channels,
output_format, image_id)` — a function this project's installed Streamlit
(1.59) moved to `streamlit.elements.lib.image_utils.image_to_url` and
rewrote to take a `LayoutConfig` object instead of a bare pixel width as
its second argument, so the unmodified package raises `AttributeError` the
first time `st_canvas()` is actually called. Confirmed this is a real
upstream compatibility gap (the package is pinned to an older Streamlit
internal API, not a bug in this codebase's own usage), the same class of
issue `melredact/pdfio.py`'s `open_pdf` already routes around for a
pdfplumber/pdfminer.six parsing gap. Fixed the same way: `review_app.py`
shims `streamlit.elements.image.image_to_url` at import time, before
importing `streamlit_drawable_canvas`, to a small wrapper that adapts the
old five-positional-plus-width call shape to the new `LayoutConfig`-based
one — guarded on `hasattr`, so it becomes a no-op the moment the package
(or a future Streamlit version) restores the old attribute or ships its
own fix. This is an environment-compatibility shim, not a data-integrity
concern this codebase's "abstain and flag" posture applies to (see
"Working preferences" below) — there's nothing ambiguous to guess about,
only a moved function to route around.

**Two rectangles are the header page's fixed shape; other pages take an
arbitrary list.** `redact.redact_packet` gained two new, purely additive
parameters (existing `band_override` callers are byte-for-byte unchanged):

- `header_bbox_override: tuple[Bbox, Bbox] | None` — the header page's own
  left-column and full-width-strip rectangles, supplied directly rather
  than derived from a single `HeaderBand` via `redact_bboxes_for_band`.
  The editor seeds the canvas with exactly those two rectangles (the
  automatic `detect_header_band` + `redact_bboxes_for_band` output for
  that page), then a reviewer drags each one's corners independently — the
  common case is nudging an existing pair of boxes, not drawing from
  nothing. These two boxes are passed straight into the *same, unmodified*
  `find_uncovered_group_words(header_words, anchors, left_bbox,
  right_bbox)` call every other path already uses — this session
  deliberately left `find_uncovered_group_words`, `redact.py`'s detection
  geometry, and `match.py` untouched, exactly as instructed. The editor
  supplies geometry; it never gets a different, weaker check.
- `extra_page_regions: dict[int, list[Bbox]] | None` — packet page offset
  (0-based index into `packet.page_indices`) to an arbitrary list of
  rectangles, for a page with no header structure to anchor a check to at
  all. This is what actually reaches page 2: a PRT packet's second page
  can carry its own handwritten name (see the 010406 diagnostics above,
  and CLAUDE.md's general "abstain and flag" posture toward anything a
  check can't prove), and `redact_packet` previously had no mechanism to
  redact anything outside the header page whatsoever. Each region is drawn
  as an opaque box and every word overlapping it is dropped from the kept
  text layer — the exact same draw-and-strip mechanism the header page's
  own two rectangles already use (`_draw_redaction_box`/`_overlaps_bbox`,
  both unchanged). There is no per-region "was this covered" geometric
  proof off the header page the way `find_uncovered_group_words` provides
  for the Group row — `verify_no_leaked_names`'s whole-document text scan
  is what backstops these regions, the same way it backstops everything
  else a per-region check can't reach.

`render_redaction_preview` gained the matching `header_bbox_override`
parameter (same precedence order as `redact_packet`), and a new sibling,
`render_region_preview(image, dpi, bboxes)`, previews an arbitrary region
list on a page with no header structure — both share the exact
`_draw_redaction_box` drawing path the real output uses, so the editor's
live preview and what `release_from_manual_queue` would actually write
can't drift apart, the same guarantee the original preview mechanism
already gave the header page alone (see "The review UI's preview and the
real output share one drawing path" above).

**Field entry never accepts a SID.** The editor resolves the student by
name, not by SID, per this session's own explicit instruction: a mistyped
digit in a directly-typed SID is a silently wrong assignment nothing
downstream could catch, since a typo'd SID that happens to collide with a
different real roster entry looks identical to a correct one. `roster.
filter_roster_by_name(roster, query)` — a substring, case-insensitive
match against `full_name`, factored out of and now shared with the
pre-existing "Search the full roster" expander in `_render_packet` — is
the *only* function anywhere in `review_app.py` that turns typed text into
a SID; a reviewer always picks a specific entry from the filtered results,
never types one directly. `test_reviewer_cannot_supply_a_sid_directly_
only_a_name_resolved_through_roster` (`tests/test_pipeline.py`) proves the
structural guarantee this depends on: querying with a real SID string
returns no matches at all (no roster entry's `full_name` ever contains a
SID), while querying with the actual name resolves to exactly that
student. Period and worksheet type are shown pre-filled from existing
detection (the resolved SID's own `period_display`, and the packet's own
footer-read `worksheet_type`) and worksheet type is editable — a
correction flows into the real write via a `dataclasses.replace`'d copy of
the `Packet` passed to `release_from_manual_queue`, never by mutating the
segmented packet in place. Group members are never a field here at all —
they are only ever something a reviewer draws a redaction region over, per
this session's own instruction; there is nothing to type.

**Applying always goes through `release_from_manual_queue`, which still
runs both unconditional checks against exactly the supplied geometry.** No
new code path skips `find_uncovered_group_words` or `verify_no_leaked_
names` — a manually drawn region that still leaves ink uncovered keeps the
packet held and queued, exactly like a bad four-number band correction
already did. `release_from_manual_queue` gained `header_bbox_override`/
`extra_page_regions` parameters (passed straight through to the real
`redact_packet` call) and a `decisions_dir` parameter (previously implicit
via the module-level default) so the editor can point it at the same
`--decisions-dir` the rest of the session already uses.

**Manual geometry is recorded so a re-run reproduces it without redrawing.**
Once a release actually succeeds with `header_bbox_override`/`extra_page_
regions` set, `pipeline.save_manual_geometry` persists it to `decisions/
<pdf-stem>.manual_geometry.json`, keyed by `packet_tag` — a sibling to
`overrides_path`'s detection-override file, kept out of `decisions/
<pdf-stem>.json` itself for the same reason: that file's `sid | None |
absent` three-state contract is depended on by every existing decisions
file and every test that reads one. `run_dispositions` gained a
`manual_geometry: dict[str, dict] | None` parameter (both `cli.py run` and
`review_app.py`'s "Run redaction pipeline" button load it via `pipeline.
load_manual_geometry` and pass it through, mirroring how both already load
and pass `detection_overrides`) — a packet whose tag has a stored
correction gets that geometry applied on this run's *first* redaction
attempt, not only on a second manual-queue pass, so a packet a human
already corrected once keeps writing cleanly on every later run without
being re-queued or redrawn. This never weakens either unconditional
check: a stored geometry that no longer covers the ink (e.g. after the
source file changed) is held back and re-queued exactly like any other
failing geometry would be. Left unset (`None`, the default) or with no
entry for a given tag, a packet's redaction is byte-for-byte identical to
before this feature existed — proven directly by `test_no_manual_
geometry_follows_the_existing_automatic_path_unchanged`, which runs the
same decided packet through `run_dispositions` with and without an empty
`manual_geometry` dict and asserts the two output files are byte-identical.

**Tests, all against the synthetic fixture, never real data** (`tests/
test_pipeline.py`, `tests/test_review_app.py`):
`test_manual_header_region_releases_the_packet_with_the_same_output_
shape_as_automatic` (a two-rectangle manual header region that covers the
real overflow ink releases the packet and produces a clean, single-page
file — the same shape the automatic path would have produced),
`test_manual_header_region_that_leaves_ink_uncovered_keeps_packet_held`
(a region that still falls short of the ink stays held and queued, same
as a bad band correction always has),
`test_page_2_region_redacts_page_2_and_leaves_page_1_unchanged` (a fresh
two-page fixture with its own extra handwritten name on page 2 — proves
`extra_page_regions` reaches a page `redact_packet` previously had no way
to touch, and that page 1's own output is untouched, byte-for-byte, by a
page-2-only edit),
`test_reviewer_cannot_supply_a_sid_directly_only_a_name_resolved_through_
roster` (the `filter_roster_by_name` structural guarantee above),
`test_stored_manual_geometry_reproduces_on_rerun` (a released packet's
geometry is persisted and, on a later `run_dispositions` call with the
output and ledger entry removed, reproduces the identical clean write with
no queueing and no redrawing), and `test_no_manual_geometry_follows_the_
existing_automatic_path_unchanged` (the byte-identical-output proof
above). `test_manual_queue_panel_lists_a_queued_packet_and_can_release_it`
(`tests/test_review_app.py`) was rewritten for the new UI: AppTest drives
real Streamlit widgets (the name-search text input/selectbox, the Apply
button) but not `streamlit-drawable-canvas`'s own drag interaction — a
third-party iframe component, not something AppTest can simulate — so
that test seeds `st.session_state`'s region dict the same way a reviewer's
drag would have populated it before the first run, then drives the real
name-resolution and Apply widgets exactly as a human would click them.

## A leak class verify_no_leaked_names cannot catch: freehand ink on a page redaction never reaches (2026-08-14)

**`redact_packet` only ever redacts the header page.** Every other check in
this pipeline is built on top of that same assumption:
`find_uncovered_group_words` only checks the header page's own Group row,
and `verify_no_leaked_names` is a *text*-based safety net -- it can only
flag a leak if the leaked name is both extractable as text (the kept OCR
layer) and a token on the roster. A freehand name written in the blank
margin of a *continuation* page defeats both halves at once: redaction
never reaches that page at all (wrong page, structurally), and if the name
belongs to a student who was never on the roster to begin with, there is
no roster token for a text check to compare against -- `verify_no_leaked_
names` would pass *vacuously* on a page that is visibly leaking a real
name, the same "vacuous pass looks identical to a real one" problem
`flatten=True` already creates for the text-layer check (see "Keep the OCR
text layer" above), just from a different direction (no page to check, not
no text to extract).

**Found real, not hypothetical, by `scripts/diagnose_consensus_ink.py`
(then a template-agnostic, position-agnostic diagnostic; now promoted --
see below): two page-2 freehand names in `data/PRT/010406_PD1_PRT.pdf`.**
`010406_PD1_PRT_p026` ("Brian Lu" -- a name that also happens to be on the
roster, so a text check *could* have caught this one if it were ever run
against page-2 content, which it structurally isn't). `010406_PD1_PRT_p034`
is the sharper case: "Ollie Maduro" is not on the roster at all, so no
text-based check, however thorough, has anything to compare it against --
this packet would have shipped as clean under every check that existed
before this session.

## Promoted: a template-agnostic consensus-ink detector, as an unconditional pipeline check (2026-08-14)

**The detection logic moved from `scripts/diagnose_consensus_ink.py` into
`melredact/consensus.py`, a first-class module wired into `run_dispositions`
as a fifth unconditional per-packet check** (alongside detection
confidence, uncovered group-row ink, and `verify_no_leaked_names` -- see
"A packet whose decision names a SID can still fail to redact safely"
under "Packet identity and the decisions store" above). The script itself
is kept as a thin, read-only caller around the module (same real three
source files, same JSON report shape under
`out/.diagnostics/consensus_ink/`) -- it no longer does its own detection,
alignment, or classification. Every worksheet of a given type shares one
printed template: ink recurring at the same position across a
supermajority of packets is printed, ink that shows up on only one or a
few packets at that position is either handwriting or a genuine per-copy
mark. Full method (block-median voting, why per-pixel/optical-flow
alternatives were tried and rejected) lives in `consensus.py`'s own module
docstring, not duplicated here -- this section covers what changed to
promote it into the real pipeline.

**Two passes, not one -- the second pass is new this session, replacing a
page-position heuristic.** Pass one (unchanged from the original
diagnostic, kept exactly as validated: `CONSENSUS_BLOCK_PX=16`,
`CONSENSUS_DENSITY_DIFF_THRESHOLD=0.15`, `CONSENSUS_MIN_CONNECTED_
BLOCKS=3`) finds, per packet, which of *that packet's own* blocks exceed
the group's per-block median ink density -- "ink that isn't part of the
shared template." The original diagnostic then classified each such region
by *where on the page* it fell ("top-margin, no printed content expected"
vs. "body, likely answer ink"), a heuristic that already mislabelled part
of `010406_PD1_PRT_p026`'s own name ink as harmless body content, because
the handwriting's vertical extent straddled the diagnostic's own
approximate margin/body boundary (see the original diagnostic's own
"Note" in its module docstring, kept verbatim in `consensus.py`'s history).
**Pass two (new) replaces that boundary entirely: instead of asking where
on the page a region is, it asks how many *distinct packets in the group*
have ink at that same position.** Every packet's flagged regions are
clustered by bbox overlap across the whole group (`consensus.
_cluster_and_count`); a cluster most of the group shares is an ordinary
answer/response field (expected, harmless on its own); a cluster only one
or a few packets have is anomalous. This is strictly more principled than
the position heuristic it replaces -- position on the page was never
actually the signal that mattered, frequency across the group is -- and it
has no notion of "margin" or "body" to get wrong.

**Threshold picked from real data, not assumed:
`CONSENSUS_ANOMALY_MAX_GROUP_FRACTION = 0.10`** (config.py). Mined from
the original diagnostic's own already-validated output
(`out/.diagnostics/consensus_ink/report.json`) by clustering every
packet's flagged region by bbox overlap and counting distinct packets per
cluster, for `010406_PD1_PRT.pdf`'s real page-2 group (46 packets -- the
file the two real leaks were found in): the real occurrence fractions
split into two clearly separated bands with an *empty gap* between them --
22 of 27 clusters at 1-2 packets (4.3%-8.7%, including both real leaks,
each a singleton at 2.2%) and the remaining 5 at 32.6% or higher (one
genuine shared answer-digit field reaching 100%, every packet). Nothing in
the real data falls between 8.7% and 32.6%. 0.10 sits in that gap with
headroom on both sides. The anomaly ceiling itself is `max(1,
int(0.10 * group_size))` (`consensus._anomaly_ceiling`) -- `max(1, ...)`
matters at the group floor (`CONSENSUS_MIN_GROUP_SIZE=5`, unchanged from
the original diagnostic's own calibration): a bare fraction would round a
singleton occurrence down to a ceiling of 0 at small group sizes, making
the check unable to ever flag the exact case (a lone anomalous packet) it
exists to catch.

**Not every real group shows as clean a gap as the one this was calibrated
against -- re-running against every real file this session surfaced that
plainly, not just as a caveat.** Both real leaks (`010406_PD1_PRT_p026`,
`_p034`) are correctly held on their real page-2 group. But the two Hannel
files' own page-2 groups (free-response prose worksheets, not 010406's
digit-style answer field) show a much smoother, unimodal spread of
low-frequency clusters instead of a bimodal split -- real, individually
varying handwritten prose naturally lands at slightly different block
positions per student (response length, line wrapping), so a meaningful
number of positions are genuinely shared by only 1-2 students out of the
group by chance, not because they're identifying. Measured directly, cold
cache, this session: **`Hannel MPR PD2.pdf`'s page-2 group holds 18 of 22
packets (104 regions total); `Hannel PRT PD2.pdf`'s holds 10 of 20 (97
regions); `010406_PD1_PRT.pdf`'s holds 12 of 46 (24 regions, including both
real leaks).** Re-checking the real, already-shipped, already-`verify`-clean
020415 output for the same leak class (16 files: 11 MPR, 5 PRT) found the
same pattern: **10 of 11 shipped MPR files and 4 of 5 shipped PRT files
would be held by this check on their page-2 content**, none of which are
known real leaks -- this check is considerably more sensitive on
free-response worksheets than on 010406's structured one, an accepted cost
under the same "a held-back false positive is cheap via the manual queue,
a shipped leak is not" trade-off bug #7 already established, not something
this session tuned away. Clearing this for real 020415/010406 output is a
real, sizeable review-time cost (dozens of manual-queue entries), not
committed to happen automatically as part of this promotion -- same
"verified, not automatically re-shipped" posture as every other real-data
finding in this file.

**The header page is structurally excluded from this check, not just
observed to pass it.** `consensus._build_groups` never includes a
packet's own `header_page_index` in any group -- confirmed directly this
session: across all three real source files, every hold's `page_offset`
is 1 (or higher), never 0. This isn't luck: the header page's Name/
Teacher/Group ink is already destroyed unconditionally by `detect_header_
band`'s own geometry, regardless of whether a match ever succeeds, and
every packet's own handwritten name there is -- correctly -- always a
singleton at whatever position it lands (no two students write the same
name), which would make *every* header page flag under this check for a
region the pipeline already fully covers by construction. Excluding it
isn't a weakening; a check that would always fire for a reason already
handled elsewhere carries no signal.

**Never overridable via `detection_overrides`, matching the existing rule
for real-finding holds.** `detection_overrides` releases exactly one hold
reason -- the detection-*confidence* question `detect_header_band` raises
(see "One of those five holds is human-overridable" above) -- and a
consensus-ink anomaly is not that: it's a finding of real, anomalous ink
in the pixels, the same category `find_uncovered_group_words` and
`verify_no_leaked_names` already are. `run_dispositions`'s new check never
consults `detection_overrides` at all.

**Resolved by the manual-redaction queue, the same backstop-not-substitute
posture as the other two queueable holds.** A consensus-ink hold queues
the drafted attempt exactly like a detection-confidence or uncovered-ink
hold (`pipeline._queue_for_manual_redaction`), with one addition: the
flagged bbox itself is persisted into the queue entry's own metadata
(`flagged_regions`), so `review_app.py`'s manual editor can seed a
rectangle directly on the flagged ink -- on the *correct page* -- the
instant a reviewer opens that packet, rather than a reviewer having to
hunt across the packet's pages for what to cover (`_seed_manual_regions`,
`_render_manual_editor`'s page selector defaulting to the flagged offset
and its own warning banner). `pipeline.release_from_manual_queue` gained a
`flagged_regions_to_verify` parameter: a release is refused (packet stays
queued, nothing written) unless the human-drawn `extra_page_regions`
actually overlaps every flagged bbox the packet was held for -- the same
overlap-based coverage test `find_uncovered_group_words` already uses for
the Group row, applied here to make "draw a region over the flagged ink"
the actual, checked resolution path, not merely a plausible-looking one.

**Cost, measured this session: a cold-cache run against all three real
source files (`010406_PD1_PRT.pdf` 92 pages/46 packets, both Hannel
files) took 31 seconds total** for the consensus pass alone (rasterize at
`CONSENSUS_DPI=200` + ECC-align every non-header page against its group's
reference, then block-density/cluster). This is a whole-group pass
(computed once per `(worksheet_type, page_offset)` group, not once per
packet), threaded through `run_dispositions`/`analyze_redaction_holds`/
`cli.py`/`review_app.py` as a precomputed `consensus_holds` mapping (the
same pattern `round_labels` already uses) so it's never paid twice in one
run. **Disk-cached the same way `melredact/ocr.py` keys its own OCR
cache** -- on the source file's actual content hash, not path/mtime, plus
page index, ECC reference page, dpi, and block size
(`consensus._consensus_cache_path`, sharing `ocr.file_content_hash` rather
than duplicating the hashing logic) -- under `.cache/melredact/consensus/`,
already covered by the blanket `.cache/` gitignore rule and called out
explicitly in `data/README.md` (a per-page ink-density grid derived
directly from a real scan is identifiable positional handwriting data, the
same sense the OCR word cache already is, even though it isn't the raster
image itself).

**Rejected alternatives (unchanged from the original diagnostic, carried
forward verbatim into `consensus.py`'s own module docstring, not
re-litigated here):** a strict per-pixel "ink in most packets" vote after
ECC alignment measured 0.29-0.44 Dice overlap even between correctly
-aligned copies of the identical template (small, smoothly-varying local
scan distortion, not a global transform error) and flagged 13-36% of a
single control packet's own ink as false "non-consensus" at a coarse
20px/7.2pt block. Dense optical flow refinement cut mean pixel error by
~65% but produces huge (up to 300px) spurious displacements exactly where
two copies' handwriting differs -- an alignment method that can warp real
handwriting into agreement would silently erase the very thing this
module exists to find, so it was rejected outright despite measuring
better on raw pixel error. Neither was reconsidered as part of this
promotion -- this session's own instruction was to keep the validated
16px block grid and density-vs-median vote unchanged, only replacing the
position-based classification step.

**Tests** (`tests/test_consensus.py`, `tests/test_pipeline.py`, synthetic
fixture only, `tests.make_fixture.build_consensus_fixture`/
`build_small_consensus_group_fixture` -- a group of packets sharing one
page-2 raster template, real filled-rectangle ink rather than rendered
text so the block-density pattern is deterministic across environments):
anomalous ink on page 2 is flagged with the page index in the reason;
ink shared by enough of the group (a synthetic "answer field") is not
flagged; a group below `CONSENSUS_MIN_GROUP_SIZE` holds nothing from this
check and the report says so explicitly rather than silently passing;
the hold is not releasable via `detection_overrides`; drawing a manual
region over the flagged ink (and only over it -- a region that misses it
stays refused) releases the packet through `release_from_manual_queue`;
packets with no anomalous ink (both the shared-answer-ink ones and the
plain ones) follow the ordinary, unaffected write path.

## Consensus-ink: registration tolerance and a writing-zone mask, replacing a frequency threshold that didn't generalize (2026-08-14)

**Motivated by the previous section's own real finding: 87% of packets on
both real 020415 files were held by the consensus-ink check with no
identifying mark on any of them.** Diagnosing the 42 held packets by
rendering their flagged regions found three real, non-identifying causes:
students circling or writing a number at a different position in a fixed
answer table (ragged answer position), per-copy registration/darkness
variation on densely-printed pages reading printed instructional words as
extra ink (printed-text alignment noise), and genuine stray marks (a
doodle, a page-edge scan artifact). Two independent fixes address these,
done in dependency order (pass one first, since it reduces the noise pass
two has to separate).

**Fix 1 -- pass one precision: registration tolerance, not darkness
normalization.** Diagnosed before changing anything, per real measurement,
not assumption: pass one (block-density-vs-median) flagged 135 regions on
`Hannel MPR PD2.pdf`'s page-2 group and 401 on `Hannel PRT PD2.pdf`'s, with
no known real leak in either file. Two pieces of real evidence ruled out
per-copy darkness/contrast as the cause: each packet's own whole-page mean
block density varied by only ~0.0006-0.0015 across its entire 20-22-packet
group (a ~1-3% relative spread — there is no meaningful "some copies are
darker scans" effect to normalize away), and the correlation between a
flagged region's own peak density-diff and its packet's whole-page darkness
offset was weak to nonexistent (r=0.32 on MPR, r=0.02 on PRT). What the
flagged regions actually were instead: 58-66% of them were exactly one
block tall (a single text-line row), most sitting directly on top of a
position where the group median already shows substantial ink (thin-region
median-density-there: 0.156 on MPR, 0.150 on PRT) — the same printed line
nearly every other packet also has, just quantized into a different block
by a sub-block registration difference between independently ECC-aligned
copies. This matches consensus.py's own already-documented finding that
alignment leaves small, smoothly-varying local distortion behind (Dice
overlap only 0.29-0.44 even post-alignment). Confirmed directly by
rendering five spot-checked flagged regions (two on `010406_PD1_
PRT.pdf`, three on `Hannel PRT PD2.pdf`): every one was a printed word, a
printed table-border rule, or the footer's "Page X of Y" marker — never a
blank patch of elevated density. **Registration, not darkness — evidence,
not assumption.**

Fix: `melredact/consensus.py`'s `flagged_regions` now dilates the group
median (per-block max filter, radius `CONSENSUS_REGISTRATION_TOLERANCE_
BLOCKS=1`, `consensus._dilate_float_max`) before diffing against it, so a
packet's block is only flagged when its density exceeds the *highest*
median density in its own immediate neighborhood, not just its
exact-position counterpart. A per-copy density-normalization fix (matching
each packet's ink level to a common reference before comparing) was
considered and **rejected**: the darkness-correlation evidence above shows
there is no real per-copy darkness effect on this data to normalize away,
so it would add a step that does nothing on the actual failure mode while
risking flattening genuine differences in how heavily a student presses a
pen. `radius=1` (the smallest possible correction) already measured a
93-90% reduction (135->9 on MPR, 401->39 on PRT) while both known real
leaks (`010406_PD1_PRT_p026`, `_p034`) stayed flagged with dozens of
surviving regions each — nowhere close to a too-aggressive radius's risk of
suppressing real ink. Larger radii were measured and rejected for
diminishing returns against growing risk; see `CONSENSUS_REGISTRATION_
TOLERANCE_BLOCKS`'s own docstring in `config.py` for the full real numbers.

**Fix 2 -- pass two: a spatial writing-zone mask, replacing the frequency
threshold entirely.** The previous `CONSENSUS_ANOMALY_MAX_GROUP_FRACTION`
threshold (a cluster shared by more than 10% of the group was an ordinary
field) was calibrated against a single file's own real gap
(`010406_PD1_PRT.pdf`'s page-2 group) and **did not generalize**: re-run
against the two Hannel files (Fix 1 already applied, to isolate this
problem from the one it fixes), the occurrence-fraction distribution was
continuous at every candidate cutoff on both files — no step of more than
~5 points anywhere, nothing resembling the single file's own clean gap.
Root cause: real free-response handwriting varies in *position*, not just
presence — two students answering the same prompt rarely land on the exact
same blocks, so their marks never cluster into one high-occurrence group
under bbox-overlap clustering even though both are, correctly, ordinary
answer ink in the same general area. **The frequency threshold's single-
file calibration was the bug, not any one number it picked** — no fraction
could have separated "shared field" from "isolated mark" on data shaped
like this.

Replacement: a block position is "in the writing zone" — an ordinary place
the class writes — when at least `CONSENSUS_WRITING_ZONE_MIN_SHARE` *other*
packets in the group have their own (registration-tolerant) flagged ink
within `CONSENSUS_WRITING_ZONE_DILATION_PT` of it: spatial corroboration
from nearby ink, not exact-position overlap. A flagged region is held only
when its own footprint has no such corroboration anywhere in it, regardless
of how many packets have *some* unrelated ink *somewhere* on the page
(`consensus._analyze_group`, `consensus._zone_params`). Both values are
dict-keyed by `worksheet_type` in `config.py` (`CONSENSUS_WRITING_ZONE_
DILATION_PT`, `CONSENSUS_WRITING_ZONE_MIN_SHARE`), not hardcoded, with a
module-level default (23.0pt / 2) for any type without its own entry.

**Calibration, on both teachers, real numbers (post-Fix-1 hold counts,
dilation swept at 0 / 11.5pt / 23.0pt / 34.6pt, i.e. 0/2/4/6 blocks,
min_share=2 throughout):**

| file (worksheet_type) | 0pt | 11.5pt | 23.0pt | 34.6pt |
|---|---|---|---|---|
| `010406_PD1_PRT.pdf` (PRT, 44 scored) | 42 | 16 | **7** | 6 |
| `Hannel PRT PD2.pdf` (PRT, 20) | 13 | 10 | **8** | 6 |
| `Hannel MPR PD2.pdf` (PCMEL_MPR_ADR, 22) | 6 | 6 | **6** | 6 |

010406 shows a real knee at 23.0pt: the 0->23pt step absorbs the bulk of
ragged-answer-position noise (42->7 held), while the next 11.6pt of
dilation buys only one more packet (7->6) — diminishing returns, not a
continuing clean trend, so pushing further isn't justified by the data.
Hannel PRT shows **no comparable knee** — a steadier decline consistent
with free-response prose having no single characteristic answer-field size
the way 010406's structured digit answers do; reported plainly rather than
picking a number that pretends to a gap that isn't there. Hannel MPR is
stuck at 6 held at *every* radius tested: after Fix 1, MPR's remaining
flagged regions formed zero occurrence>=2 clusters at any radius, so there
is no shared-position signal for a spatial mask to find on this file at
all — clean gap or not, dilation cannot help a file with no clustering
signal to begin with.

**23.0pt / min_share=2 was kept as the one real value in play, not because
every type has its own clean gap, but because a fine enough key to give
them separate ones doesn't exist**: `010406_PD1_PRT.pdf` and `Hannel PRT
PD2.pdf` both parse to the **identical** footer-read `worksheet_type`
`"PRT"` (`WORKSHEET_TYPE_PATTERN` strips the trailing `(mm/yyyy)` the same
way for both) despite very different real page-2 content — one a
structured digit-answer table, the other free-response prose.
`worksheet_type` cannot separate them, so one config entry necessarily
governs both real PRT files; 23.0pt is 010406's own real-gap-justified
number, which also happens to give Hannel PRT a real (if less dramatic)
13->8 reduction. The dict-keyed shape exists so a future teacher's
worksheet_type that genuinely does have its own distinguishable gap can get
its own calibrated entry without a code change — there is just no real
evidence yet to put a *different* number in either of today's two real
keys, and manufacturing one would be exactly the single-file-overfit
mistake this replacement fixes.

**Both real leaks stayed held at every dilation radius tested, including
zero** — `010406_PD1_PRT_p026` and `_p034` never depended on the writing-
zone mask at all; they were already isolated (no corroborating ink
anywhere near them) under Fix 1 alone. At the chosen real config (Fix 1
radius=1, 23.0pt/min_share=2), the pipeline holds `010406_PD1_PRT.pdf` at
7/44, `Hannel PRT PD2.pdf` at 8/20, `Hannel MPR PD2.pdf` at 6/22 — matching
the swept-radius table above exactly, confirmed via the real, non-test
`analyze_consensus_anomalies` entry point, not a reimplementation.

**The stray-mark category, its real cost made explicit.** Spot-rendering
residual holds after both fixes found a genuine mix, not just further
false positives: on Hannel MPR, one residual hold (`Hannel MPR PD2_p022`)
is a confirmed real stray mark — a hand-drawn doodle in the top margin,
correctly held, since the check cannot and should not try to distinguish a
doodle from a name; a human clearing it via the manual-redaction queue is
the intended, accepted cost, not a bug. The remaining residual holds
spot-checked (010406: printed body text, a printed table-border rule, the
footer's own "Page X of Y" marker, and one long curved scan-artifact line
running most of the page width; Hannel PRT: more printed-table-border and
printed-word instances) are further printed-text/scan-artifact noise the
tolerance fix didn't fully absorb — an accepted residual under the same "a
held-back false positive is cheap via the manual queue, a shipped leak is
not" trade-off bug #7 already established, not something further tuned
away this session (a larger registration-tolerance radius would reduce it
further but was rejected above for diminishing returns against growing
risk to genuine ink).

**Nothing was regenerated or shipped for any real teacher as part of this
session** — same "verified, not automatically re-shipped" posture as every
other real-data finding in this file. The real numbers above come from
`analyze_consensus_anomalies` run directly (read-only, the same function
`cli.py analyze`/`run_dispositions` call) against the real source PDFs, not
from any write to `out/`, `decisions/`, or the ledger.

## From detection-gates-workflow to human-reviews-everything (2026-08-14)

**Why: each new leak variant this project found got its own automatic
detector, and each detector needed its own real-data calibration session
— the group-row overflow fix, the vertical-overflow fix, the consensus-ink
detector and its two follow-up recalibrations, all in this same file.
Students write in unpredictable places on paper, so that enumeration does
not terminate.** The reviewer already looks at every packet before
anything is written (see "Non-negotiable design decisions" — abstain and
flag, never silently guess). So the manual editor, not a new detector,
should be the general answer to "a student wrote somewhere the automatic
geometry doesn't reach" — and detection should mostly *inform* the human
rather than *gate* the workflow. This session made that shift concrete in
three ways: the editor is reachable for any packet, not just a held one;
`find_uncovered_group_words`'s finding is now advisory, not a hold; and a
per-run summary reports how often manual editing and the advisory actually
get used, so that judgment can be revisited with real usage data instead
of guessed at again.

**The editor is reachable from the normal review flow, for any packet.**
`review_app.py`'s `_render_packet` now renders an "✏️ Edit redaction
(manual)" expander for every packet, held or not — placed after the
ordinary Decision radio/Confirm buttons so it reads as an opt-in advanced
path, not something that competes with the one-click "looks right,
confirm" flow that covers the overwhelming majority of packets. It calls
the same `_render_manual_editor` the manual-redaction queue panel already
used, generalized to take an optional `default_sid` (the packet's live
decision-preview candidate, not a queue entry's fixed sid) and an optional
`flagged_regions` (populated only when this exact packet_tag also happens
to be sitting in the manual queue right now, via `list_manual_queue`). The
automatic geometry (`_seed_manual_regions`, unchanged) is always seeded as
the starting rectangles, so the common case — automatic detection already
got it right — is one glance at the live preview and a single Apply click,
not a from-scratch drawing exercise. Applying always goes through
`pipeline.release_from_manual_queue` (a no-op to clear a queue entry that
doesn't exist, for a packet that was never queued), immediately preceded
by `_confirm` so the decision is recorded whether or not the release
itself succeeds — this is what keeps "present in `out/`" iff "has a
confirmed decision" true (see "Packet identity and the decisions store")
even for a packet a reviewer decided entirely through the editor, never
through the ordinary radio+Confirm flow. Test:
`test_edit_redaction_reachable_for_a_non_held_packet_and_resolves_by_name`
(`tests/test_review_app.py`) — opens the editor for `packets_p000` (never
held by anything), applies with the seeded geometry unchanged, and checks
the output lands at the same natural path the automatic path would have
used and the decision was recorded.

**Every page of the packet is shown, tabbed, in the editor.** The page
selector (`st.selectbox`, already present before this session for the
manual-queue-only editor) now doubles as the general per-packet page tab
strip: each page keeps its own independent list of rectangles in
`st.session_state`, so a stray name on page 3 is exactly as reachable as
page 1's header — a reviewer does not need a detector to route them to the
right page, only to look. Redaction on a non-header page works exactly the
same destructive way the header page's own rectangles already did before
this session (`redact_packet`'s `extra_page_regions` parameter, added
alongside the original drag-corner editor — see "A drag-corner editor for
the manual-redaction queue" above): an opaque box painted on the raster
plus every word overlapping the region dropped from the kept text layer,
never pixelation or an overlay. Test:
`test_page_3_region_redacts_page_3_and_leaves_other_pages_unchanged`
(`tests/test_pipeline.py`) — a fresh 3-page fixture (page 1 header, page 2
plain continuation, page 3 carrying its own extra handwritten name) proves
a region on page 3 specifically redacts page 3 and leaves both page 1 and
page 2 byte-identical to the automatic path's own output.

**`find_uncovered_group_words` is now advisory, not a hold — the function
and its own regression fixtures are unchanged, only the consequence of a
non-empty result changed.** Evidence: real-data measurement (see "Teacher
010406 roster reissue" and "Consensus-ink: registration tolerance..."
above) found this check holding back roughly 20% of real packets on two
teachers — rendering every one of 41 examined held packets confirmed the
flagged ink was printed body text near the header border (the printed
"1. Please work on this individually:" instruction, a printed
title/table-border rule), never genuine uncovered handwriting. Zero true
positives across 41 real cases is not a threshold-tuning problem the way
the consensus-ink check's writing-zone mask was (see that section above)
— there is no fixed slack past the header border that reliably separates
"real overflow ink" from "safe printed text" on real data (the check's own
docstring already documents this: printed body text can start as little
as +1.92pt below the header border on some pages). Meanwhile, the reviewer
now looks at every page of every packet via the editor above regardless of
whether this check fires — so a geometric proof with a 0/41 real-world
true-positive rate is more useful as something to point a reviewer's eyes
at than as a gate nothing on real data can ever cleanly pass.

`redact.find_uncovered_group_words` itself is byte-for-byte unchanged, and
so are its three regression fixtures for bugs 4, 6, and 7
(`test_group_row_overflow_past_column_split_is_fully_redacted`,
`test_band_bottom_anchor_excludes_body_text_the_self_relative_window_missed`,
`test_group_row_vertical_overflow_below_the_header_border_is_not_silently_
missed`, all still in `tests/test_redact.py`, all still passing unmodified)
— the detection logic stays correct, proven by the exact same fixtures;
only what a non-empty result *means* to `run_dispositions`/
`release_from_manual_queue` changed. `RedactResult.uncovered_group_words`
is unchanged; `DispositionResult`/`ManualReleaseResult` both gained an
`advisory_uncovered_words` field carrying the same finding without
queueing the packet or blocking the write — never overridable via
`detection_overrides`, since there is no longer a hold there to override.
`review_app.py`'s editor draws each advisory word's own box as an orange
outline (`_advisory_outline_image`, never filled, never confused with the
red redaction boxes) computed live against whatever geometry is currently
drawn (`find_uncovered_group_words` called directly against the header
page's own OCR'd words), so a reviewer sees it update as they drag boxes,
optional to act on. Tests: `test_vertical_group_row_overflow_is_advisory_
not_held`, `test_manual_header_region_with_uncovered_ink_still_releases_
as_advisory`, `test_detection_override_releases_despite_an_uncovered_ink_
advisory` (all `tests/test_pipeline.py`) — the real packet-14 shape (and a
forced synthetic case) now writes cleanly with the finding carried onto
the result, never held or queued.

**The consensus-ink writing-zone check stays a real, non-overridable hold
— it has a genuine true-positive record nothing else caught, and a low
false-positive rate on the file it matters most for.** Unlike uncovered-
group-row-ink, this check actually caught two real leaks
(`010406_PD1_PRT_p026` "Brian Lu", `010406_PD1_PRT_p034` "Ollie Maduro" —
see "A leak class verify_no_leaked_names cannot catch" above) that no
other check — not `find_uncovered_group_words` (header-page-only, this
leak class is on continuation pages), not `verify_no_leaked_names` (Maduro
was never on the roster, so there's no roster token to compare against) —
could have caught at all. And after the registration-tolerance and
writing-zone-mask fixes (see "Consensus-ink: registration tolerance..."
above), its real false-positive rate on 010406 — the file both real leaks
came from — is low (7 of 44 real packets held, most of the residual a
confirmed genuine stray mark or further printed-text noise, not a
structural miscalibration the way the uncovered-ink check's 0/41 record
was). A check with a real record of catching leaks nothing else can, on
the exact file class it was built for, earns staying a hard gate; a check
with a 0/41 real true-positive rate does not. `run_dispositions` and
`release_from_manual_queue` are unchanged for this check — still never
consulted by `detection_overrides`, still resolvable only by drawing a
region that actually overlaps every flagged bbox
(`flagged_regions_to_verify`'s overlap test, unchanged). Tests (unchanged,
still passing): `test_consensus_hold_is_held_back_end_to_end`,
`test_consensus_hold_is_not_releasable_via_detection_overrides`,
`test_consensus_hold_is_queued_and_released_by_drawing_a_manual_region_
over_the_flagged_ink` (`tests/test_pipeline.py`).

**`verify_no_leaked_names` is unchanged and still runs unconditionally on
every written file, automatic or manually edited geometry alike** — the
one check this session never touched, on purpose. `release_from_manual_
queue` still calls it after every redaction attempt and still refuses to
write (leaving the packet queued) on any finding, regardless of who
supplied the geometry. Test: `test_manual_queue_release_with_a_geometry_
that_still_leaks_the_name_stays_queued` (`tests/test_pipeline.py`) — a
`header_bbox_override` too small to reach the real Name ink leaves it
sitting in the kept text layer, and release is refused with the packet
left queued, proving the human-supplied-geometry path still can't ship a
real leak no matter how a reviewer draws it.

**A per-run summary reports geometry provenance and advisory volume, so
the advisory's own value can be measured rather than assumed.**
`DispositionResult`/`ManualReleaseResult` both gained a `geometry_source`
field (`"automatic"` — the default, unchanged for every packet nobody has
ever manually edited — or `"manual"`, set whenever a write applied a
stored correction from `manual_geometry`/`release_from_manual_queue`).
`cli.py run`'s summary gained a `geometry: N automatic, M manually edited
-- K write(s) carried an uncovered-ink advisory` line, and each per-file
`wrote` line gets a `[manual geometry]`/`[advisory: ...]` suffix when
applicable; `review_app.py`'s sidebar shows the same counts after "Run
redaction pipeline". The explicit point of this line: if `K` (advisory
count) tracks real manual edits over many runs, the advisory is earning
its place; if it fires on nearly every write regardless of whether a
reviewer ever acts on it, that's a signal to drop it entirely rather than
carry a check nobody uses. `HoldAnalysis`'s own `uncovered_ink_hold` field
was renamed `uncovered_ink_advisory` to match (`analyze_redaction_holds`/
`format_hold_analysis_report`, `cli.py analyze`) — it no longer gates the
consensus/leak checks below it in the precedence chain, and is excluded
from `HoldAnalysis.clean`'s definition (a packet with only an advisory
finding is still "clean" in the sense that a real run ships it without
intervention).

**The editor is built to stay usable across a real multi-dozen-packet
sitting.** `review_app.py` now times each page render it performs
(`time.perf_counter()` around every `_page_image` call, both in
`_render_packet`'s own header preview and in `_render_manual_editor`) and
shows it as a caption (e.g. "Packet rendered in 42 ms (cached)") — a
direct, on-screen answer to "is this session getting slow" rather than a
reviewer having to guess from feel. `_prefetch_next_packet`, called once
per `main()` run right after the current packet finishes rendering, calls
`_page_image` for every page of the *next* packet in the selector order —
since `_page_image` is both `st.cache_data`-cached and disk-cached (see
"OCR is disk-cached" above and `CACHE_DIR`), this means the next packet's
pages are already rendered and cached by the time a reviewer actually
clicks Next, not paid for live after the click. This is a best-effort
shape, not true background prefetching: Streamlit reruns synchronously
end-to-end, so the prefetch cost is paid during the *current* packet's own
script run (after that packet's own content is already computed), not on
a separate thread — the honest trade-off available without introducing a
worker process into a tool whose entire job is being a thin, inspectable
review UI. A page that fails to prefetch (caught and swallowed) simply
renders live, on demand, when the reviewer actually reaches it — prefetch
failure must never break the packet currently on screen.

**Nothing about this session's own real-data findings (the 020415/010406
uncovered-ink hold rates, the 010406 consensus-ink true positives) was
re-measured against the new advisory-only behavior** — the underlying
`find_uncovered_group_words`/consensus checks are unchanged, only their
consequence in `run_dispositions` changed, so the finding rates already
measured and cited above (41 held packets, 0 true positives; 7/44,
6/22, 8/20 consensus holds) remain the accurate real numbers for what each
check *finds*, just no longer describe what each check *blocks*.

## Working preferences

- Calibrate against real measured data, not assumptions — when a
  threshold or engine choice is debatable, measure it against the actual
  files first (see how `MIN_SCORE`/`MIN_MARGIN` and the PaddleOCR-over-
  Tesseract choice are justified in-code with real numbers).
- Docstrings should explain *why*, with concrete numbers/examples where
  the reasoning isn't obvious from the code alone — not what the code does.
- Fail loudly on data-integrity problems, abstain-and-flag on ambiguous
  real-world input; never silently guess.

## Keeping this file honest

When a design decision changes, update this file in the same commit as
the code. This file should never be able to drift from what's actually
true in the repo.
