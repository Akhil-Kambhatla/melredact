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

Working end to end on the real 44-page file: review-first workflow,
SID-named output in `out/<teacher>/<period>/<SID>.pdf`, `verify` passes
unscoped.

**Speed finding:** full-page OCR at 300 DPI costs 10-25 min/page cold —
~13h projected on a 50-page file. The disk cache (file-hash+page+dpi+bbox)
makes a warm re-run of the *same* file 7.5s, but does nothing for a new,
never-seen file. Full-page OCR only exists to preserve the kept text
layer for John's later data extraction — de-identification itself only
needs the header strip.

**Next task:** build a header-only OCR path as an alternative to
full-page, and compare cold-cache runtime on unseen files. Both paths
must pass verify. Open question: does header-only give the matcher
enough to work with, while still catching group-row overflow the way
full-page OCR's word list currently does (see `find_uncovered_group_words`
in "Two rectangles are redacted per header page" below)?

**Deferred:** a hosted web app (upload/review/download) instead of the
local Streamlit tool. Needs John/Doug sign-off first — moves identifiable
minor data off-device, a different IRB posture than the local tool.

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
  `verify_no_leaked_names` finding: delete the output, raise loudly.
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
  but measured on the real 44-page file a full-page OCR call took
  10-25+ minutes on content-dense worksheet pages vs. a couple of
  seconds for a small header/footer crop — forcing every page through a
  full-page OCR up front (during segmentation, for all 44 pages, most of
  which never get redacted) would turn a ~2-minute segmentation step into
  20+ minutes for no benefit. Keying on the exact bbox keeps each call's
  cost where it already was while still collapsing every *repeat* of the
  same call to one, disk-persisted so it survives a Streamlit restart or
  a second `cli.py run` (confirmed: a warm-cache re-run of the real file
  that originally took ~1h33m cold dropped to ~7.5s). `review_app.py`
  additionally wraps `extract_header_fields` in `st.cache_data` so a
  Streamlit rerun (a button click, Prev/Next) doesn't even repeat the
  in-memory anchor-location work on top of a cache hit.

## Four regression-tested bugs

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
combined PDF per class** — `out/<teacher_code>/<period>/<SID>.pdf` (e.g.
`out/020415/02/0204150204.pdf`), where `teacher_code` and `period` are the
SID's own digits (`RosterEntry.teacher_code`, `.period_display`), not
anything read off the packet. (Reworked by John, 2026-07-18 from an
earlier `out/<pdf-stem>_p<page>.pdf` naming keyed off `packet_tag` — de-
identified output is now named and organized entirely by the identity a
reviewer confirmed, not by anything that traces back to the source scan.)
`cli.py run`'s `--out` therefore has to be a directory, never a `.pdf`
path; it errors out early (before touching the filesystem) if given one,
rather than silently creating a directory with a misleading `.pdf`-looking
name.

**The load-bearing invariant is "present in the output tree" iff "has a
confirmed, approved SID."** Non-consented and pending packets are never in
the tree under any name, including a placeholder — only a packet with a
decision naming a SID ever produces a file. Because the file name is now
the SID rather than the `packet_tag` that produced it, a single tag-keyed
delete is no longer enough to enforce this on its own: a *corrected*
decision (a human overriding an earlier match to a different SID) orphans
its old SID's file exactly the same way a rejection does, just without an
explicit "delete this" step naming that old file. `run_dispositions`
therefore closes the loop with a reconciliation pass at the end of every
run — since `roster` is always already narrowed to one teacher+period
block (see roster.py), every SID any decision for this pdf can legally
name lands in exactly one `out/<teacher>/<period>/` directory, so sweeping
that one directory against the current `decisions.json` values (deleting
any `<SID>.pdf` whose SID is no longer an approved value) catches both a
straight rejection and a correction with the same code path, with no
separate history of "what used to be written" to keep in sync. This holds
for all four packet states: approved-with-SID (written), non-consented
(deleted, absent), pending (absent, never written in the first place), and
corrected (old SID's file swept, new SID's file written).

**`verify` walks that same tree, not a flat `<pdf-stem>_p*.pdf` glob.**
Since output file names no longer trace back to a source scan, `verify`
takes no `--pdf` — it globs `<out>/*/*/*.pdf` and checks every file it
finds against the full (unscoped) roster. Finding *no* files under `--out`
is treated as a hard failure, not a vacuous pass: silently checking
nothing because `--out` pointed at the wrong place is more dangerous than
erroring loudly, since a clean silent run looks identical to a genuinely
clean one.

**The review UI's preview and the real output share one drawing path.**
`review_app.py` calls the exact same `render_redaction_preview` (which
itself shares `detect_header_band`/`_draw_redaction_box` with
`redact_packet`) that produces the final file, stamped with whichever
candidate is *currently selected* in the decision radio — read from
Streamlit's own session state ahead of the radio being instantiated, so
the preview reflects a live "if you confirm this, this is what you get"
rather than a fixed placeholder that could read differently from what
Confirm actually produces.

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
