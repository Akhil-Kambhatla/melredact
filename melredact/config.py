"""Tunable constants. Geometry values are measured off three real header pages
(US Letter, 612x792pt, origin top-left) — see the build spec for the source
measurements. Anchors are starting points for locating printed labels via
pdfplumber word search; they are not used as fixed redaction coordinates.
"""

# --- Page geometry ---
PAGE_WIDTH_PT = 612
PAGE_HEIGHT_PT = 792

# --- Printed label anchors (x0, x1, top, bottom) in points ---
# Used to locate the printed "Name:" / "Teacher:" / "Group members" labels via
# text search, then assign nearby handwriting to the nearest anchor row.
NAME_ANCHOR = {"x0": 45, "x1": 90, "top": 68, "bottom": 86}
TEACHER_ANCHOR = {"x0": 45, "x1": 94, "top": 87, "bottom": 109}
GROUP_ANCHOR = {"x0": 46, "x1": 80, "top": 111, "bottom": 134}
DATE_ANCHOR = {"x0": 413, "x1": 416}
PERIOD_ANCHOR = {"x0": 413, "x1": 416}

# --- Left/right column split ---
# Handwriting in the left (Name/Teacher/Group) fields tops out at x=383;
# the Date/Period column starts at x0=413. Split down the middle, with slack
# on both sides. Anything left of the split is destroyed; right of it is
# left untouched.
LEFT_FIELD_MAX_X = 383
RIGHT_COLUMN_MIN_X = 413
COLUMN_SPLIT_X = 400

# --- Header band fallback ---
# Primary source of truth is the drawn border, detected in the raster (see
# redact.py). These are used only when border detection fails to find both
# bracketing rules, and always act as a floor (redact at least this much),
# never a ceiling.
HEADER_BAND_FALLBACK = {"top": 58, "bottom": 148, "left": 38, "right": 574}

# --- Printed label vocabulary ---
# Used two ways: a narrow, distinctive subset locates each row's anchor
# (must not collide with ordinary body text), while the fuller set excludes
# the label's own words when pulling out the handwritten value next to it.
NAME_ANCHOR_WORDS = {"name:", "name"}
TEACHER_ANCHOR_WORDS = {"teacher:", "teacher"}
GROUP_ANCHOR_WORDS = {"group"}

NAME_LABEL_WORDS = {"name:", "name"}
TEACHER_LABEL_WORDS = {"teacher:", "teacher"}
GROUP_LABEL_WORDS = {"group", "members,", "members", "if", "any:", "any"}
DATE_LABEL_WORDS = {"date:", "date"}
PERIOD_LABEL_WORDS = {"period:", "period"}

# Anchor search is restricted to the upper portion of the page (generous
# slack past the measured header band) so a stray body-text word can't be
# mistaken for a row anchor. This slack is deliberately generous -- it only
# has to tolerate a label being found lower than the flat-page measurement
# (skew, scan variance), not stay tight -- so it's *not* reused as the
# bound for which words get assigned into a row's value (see
# ROW_ASSIGNMENT_BOTTOM_SLACK_PT): confirmed against the real file, that
# would let the printed "1. Please work on this individually:" instruction
# line and the paragraph below it bleed into the group row, since both can
# sit well inside this wider slack.
HEADER_SEARCH_MAX_TOP = HEADER_BAND_FALLBACK["bottom"] + 40

# Once anchors are actually located (locate_header_anchors), the row
# *value* window's bottom bound is instead self-relative: group_top plus
# one more row's worth of height (group_top - teacher_top, i.e. whatever
# spacing this specific page's own anchors show) plus this slack, capped at
# HEADER_SEARCH_MAX_TOP. This was originally documented as measured off
# "the real file" with "~24pt of room to spare" over where body text
# starts -- that number came from a single reference page and was never
# checked against the rest of the real dataset. Measured properly
# (2026-07-20) across all 42 real header pages we have (both worksheet
# types): this self-relative estimate's own margin over the real first
# line of body text ranges from -10.0pt to +38.36pt, median +5.0pt -- 3 of
# 42 pages already negative, independent of any single incident packet.
# `row_height` itself is the reason: it's `group_top - teacher_top` (or a
# fallback chain when that's degenerate, e.g. the printed "Group" label
# not being OCR-located at all), an indirect proxy for "how far down does
# the header content go" that varies with per-page OCR noise, whereas the
# header block's actual bottom edge is already measured directly and far
# more reliably by `redact.detect_header_band` (same real-file check:
# `first_body_top - band.bottom` was positive on all 42 pages, min
# +1.92pt, median +5.28pt). This constant (and the self-relative formula
# it slacks) is kept only for callers with no rendered band to anchor to
# (segment.py's field-extraction/matching path never rasterizes the page).
# See GROUP_ROW_BAND_SLACK_PT below for the band-anchored replacement used
# wherever a caller *does* have the real border -- in particular
# `redact.find_uncovered_group_words`, the leak-check backstop, which is
# exactly where this self-relative formula's thin/negative margin turned
# into real false-positive flags on real packets (including the original
# "Ganik" incident packet, SID 0204150202).
ROW_ASSIGNMENT_BOTTOM_SLACK_PT = 10

# Band-anchored bottom slack for `_assign_words_to_rows` when a caller
# passes `band_bottom` (the real, rasterized header border's own bottom
# edge -- see `redact.detect_header_band`) instead of relying on the
# self-relative `row_height` estimate above. Deliberately small, not a
# swapped-in bigger constant: `band_bottom` is already a direct
# measurement of the header block's true bottom, not a proxy, so it only
# needs enough slack to (a) still catch real handwritten ink that
# genuinely overflows a few points past the drawn border -- the scenario
# this window exists to catch in the first place -- and (b) absorb OCR's
# own re-measurement noise at a boundary (~0.7-1pt observed between two
# independent OCR passes over the same word on the real file). It does
# *not* need to absorb any assumption about where body text starts, the
# way the old formula did -- that assumption is exactly what went wrong.
# Measured across the same 42 real pages: `first_body_top - band.bottom`
# never went below +1.92pt, so this slack has to stay under that with
# real room to spare, not just barely under it.
GROUP_ROW_BAND_SLACK_PT = 1.5

# --- Footer ---
FOOTER_BAND_TOP = 700
FOOTER_PAGE_MARKER = {"x0": 513, "x1": 570, "top": 747}
FOOTER_WORKSHEET_TYPE = {"x0": 29, "x1": 160, "top": 736}
# Regex-extracted from the footer text region, e.g. "Page 1 of 2".
PAGE_MARKER_PATTERN = r"Page\s+(\d+)\s+of\s+(\d+)"

# Regex-extracted from the *same* already-read footer-band text as
# PAGE_MARKER_PATTERN (segment.read_footer crops/OCRs the whole footer band
# once, at FOOTER_BAND_TOP, and both patterns search that one blob) -- this
# deliberately does not issue a second OCR call at FOOTER_WORKSHEET_TYPE's
# own narrower bbox, since that would double segmentation's OCR cost (see
# CLAUDE.md's OCR-caching section on why bbox-keyed calls are kept at their
# existing granularity). Matches the printed worksheet-type label up to its
# trailing mm/yyyy revision date, e.g. "PRT (01/2024)" or "pcMEL MPR+ADR
# (06/2025)" -- both real, distinct worksheet types confirmed on real
# scans that must never collide in out/ (see "Output layout" in
# pipeline.py). The parens and slash are made optional deliberately: real
# scans have no text layer at all (see CLAUDE.md's "Real scans have no
# text layer" section), so this string only ever reaches segment.py via
# PaddleOCR, and measured directly on both real files, PaddleOCR drops
# that punctuation outright -- "pcMEL MPR ADR 06 2025 Page 1 of 2" and
# "PRT 01 2024 Page 1 of 2", not "(06/2025)"/"(01/2024)" as the printed
# form actually reads. A pattern requiring the literal punctuation only
# ever matched a punctuated text-layer fixture, never a real scan, and
# would have flagged every real header page's worksheet_type as
# unreadable. The date itself is a form revision, not part of the
# worksheet's identity, so it's excluded from the captured group either
# way.
WORKSHEET_TYPE_PATTERN = r"(.+?)\s*\(?\d{2}[/\s]\d{4}\)?"

# --- Rendering ---
RENDER_DPI_FINAL = 300
RENDER_DPI_PREVIEW = 150
STAMP_FONT_SIZE_PT = 22

# --- Redaction ---
# Solid fill drawn over the destroyed region, plus a visible stamp so a
# reviewer can see at a glance that redaction happened (vs. a blank field).
# The stamp is the packet's re-identification key -- "SID: <sid>" then
# "PD: <period>" on their own left-aligned lines -- not the field's
# original content, so a redacted packet can still be traced back to its
# approved student without the header itself carrying a name. REDACTION_
# STAMP_TEXT is the fallback used only when no sid is known to stamp (e.g.
# a bare call to redact_packet with no decision behind it yet).
REDACTION_FILL_COLOR = (0, 0, 0)
REDACTION_STAMP_TEXT = "REDACTED"
REDACTION_STAMP_COLOR = (255, 255, 255)
STAMP_PADDING_PT = 8
STAMP_LINE_SPACING_PT = 4

# --- Header border detection ---
# The bordered header band is detected in the raster by scanning for rows/
# columns that are mostly dark pixels (a drawn rule), within a search window
# around HEADER_BAND_FALLBACK. Fraction is deliberately well under 1.0: the
# search window has slack past the box edges (so a line shorter than the
# full window still registers), and a drawn rule is rarely a perfectly solid
# scan line edge to edge.
BORDER_DARK_THRESHOLD = 128  # grayscale 0-255; below this counts as ink
BORDER_LINE_FRACTION = 0.4  # fraction of a row/column that must be dark
BORDER_SEARCH_SLACK_PT = 25  # how far past HEADER_BAND_FALLBACK to search

# A skewed scan tilts the top/bottom rules along with the whole box -- on
# the real file, no single row is ever BORDER_LINE_FRACTION dark edge to
# edge for the top/bottom rule, tilted or not, because the printed section
# title sits close enough above the box (measured ~5pt gap on the real
# file) that a row-based scan generous enough to tolerate scan variance
# also reaches the title, and a row-based scan narrow enough to exclude the
# title can't find a rule that occupies a different row at each x under
# tilt. The left/right rules don't have this problem -- even tilted, they
# stay close enough to vertical for a global column scan to find reliably
# (unchanged, below). So top/bottom are instead read off the *already-found*
# left/right columns' own vertical extent: a narrow window right at each
# detected column (width here), searched over a tight band around
# HEADER_BAND_FALLBACK's own top/bottom (slack below), gives each corner's
# own hit row directly -- confirmed on the real file to land within a point
# of the same numbers regardless of the exact slack/width chosen in this
# range, i.e. these aren't finicky. top/bottom are then the envelope (min
# top, max bottom) across the two corners -- the AABB of the tilted
# rectangle.
BORDER_CORNER_WINDOW_PT = 3

# Deliberately much tighter than BORDER_SEARCH_SLACK_PT (used for the
# column x-search, which still needs generous slack): the real file's
# section title ends only ~5pt above the true border row, and body text
# below the box starts ~24pt below it, so this has to stay inside that
# narrow safe band to read the border's own row without either neighbor
# bleeding in.
BORDER_CORNER_SEARCH_SLACK_PT = 8

# Anchor-relative corner search (see detect_header_band's `anchors`
# parameter): a fixed absolute-position search window (BORDER_CORNER_
# SEARCH_SLACK_PT around HEADER_BAND_FALLBACK) only works when a worksheet's
# title/instructions block is the same length as MPR's -- confirmed broken
# on a real second worksheet type, PRT, whose two-line title+subtitle pushes
# name_top ~37-44pt further down the page than on MPR: the fixed window
# either drags the detected box up into blank/title space (its own "clamp
# outward to fallback" logic overriding a *correctly* detected border back
# toward MPR's absolute position) or clips its search off before reaching
# the real bottom border. The located label anchors (name_top/group_top --
# already found per-page via OCR text search, independent of which
# worksheet template this is) are a page-specific proxy for "where the
# block actually is" that a fixed absolute position can never be. Measured
# on both real files: the top border sits within ~2pt of name_top (MPR
# +1.7pt, PRT -0.2pt) -- BORDER_TOP_ANCHOR_SLACK_PT gives this a wide
# margin either side (OCR-located anchors carry a little sub-point jitter
# of their own, so this can't be pinned exactly to the measured offset).
BORDER_TOP_ANCHOR_SLACK_PT = 15

# The bottom border sits some distance past group_top + one more row's
# height (header_row_height(anchors) -- the same self-relative measure
# segment.py already uses for the matching-assignment window): +8.4pt on
# MPR, +0.7pt on PRT. This is a *different* boundary than segment.py's
# ROW_ASSIGNMENT_BOTTOM_SLACK_PT (word-row assignment) -- that one is tuned
# tight specifically to keep body text out of group_text, whereas this one
# only has to stay clear of body text becoming a false *border* hit, a
# looser bar since a false hit needs a solid run at the narrow left/right
# corner columns specifically, not just any ink in the row. Widened to 15
# after the fixture's own drawn border (+13pt past this same formula) came
# up short with a tighter number -- 15 still leaves a few pt of clearance
# before body text on the real PRT file (~9-11pt gap there). The backward
# (upward) slack only needs to be small since every real/fixture border
# sits at or after the expected point, never before it.
BORDER_BOTTOM_ANCHOR_BACK_SLACK_PT = 5
BORDER_BOTTOM_ANCHOR_FORWARD_SLACK_PT = 15

# --- Matching ---
# Auto-assign only when the top candidate's score clears MIN_SCORE *and*
# beats the runner-up by at least MIN_MARGIN *and* the roster entry is still
# unclaimed. All three conditions are required; otherwise abstain.
#
# Originally calibrated against the real 3-student sample alone (two correct
# matches at 90/margin 30 and 91/margin 31, no near-miss data). Now validated
# against the real ~22-packet file (OCR'd via melredact.ocr, PaddleOCR): both
# thresholds sit in a genuine gap, not an arbitrary one.
#
# Confidently-correct packets clustered at score >= 90 (several at 100),
# margin >= 12.9, with exactly one exception: "Asael Roldan-Martinez", OCR'd
# as "Asuel Rolvan- man tinez", scored 81.8/margin 21.8 -- a real match, just
# under MIN_SCORE by 0.2, on a hyphenated name that took heavier-than-usual
# garble. Correctly deferred to review rather than loosening the floor to
# admit it; this is the exact "below_threshold_correct_candidate" pattern
# the fixture already covers.
#
# Confidently-wrong or illegible packets (not on roster, single common first
# names, scrawl) topped out at score 80/margin 8 ("Katc O Neal" ->
# "Katherine O'Neal", also correctly a below-threshold correct candidate)
# and 77.1/margin 25.7 ("Jack", a bare common first name whose huge margin
# shows margin alone isn't sufficient -- MIN_SCORE still has to hold).
#
# MIN_MARGIN=12 also caught a genuine roster collision, not just noise:
# "Genton shaw" scored 100 against its correct entry but only margin 10,
# because the roster separately contains a different student, "Charlie
# Shaw" (same surname), scoring 90 off the bare-surname variant alone.
# Abstaining here is the point, not a false negative.
MIN_SCORE = 82
MIN_MARGIN = 12

# Below this many normalized characters, a name is treated as illegible ink,
# not a candidate for matching. Guards against partial_ratio-style false
# positives on scrawl (e.g. "S 8" scoring 100 against everyone) even though
# we no longer use partial_ratio.
MIN_NAME_CHARS = 3

# Real-file leak (SID 0204150202, "Ganik" OCR'd for roster surname "Gonik"):
# fuzz.ratio("ganik", "gonik") == 80.0 -- an exact-token check (Cmd+F,
# verify_no_leaked_names's original set-intersection) cannot catch this,
# since OCR turned a real handwritten roster name into a token that simply
# isn't an exact match for anything on the roster. verify_no_leaked_names
# runs a second, fuzzy pass at this threshold specifically to catch that
# class of miss -- calibrated to the real near-miss, not picked in the
# abstract. plain fuzz.ratio (edit-distance based), not partial_ratio --
# partial_ratio's substring-containment is exactly the failure mode
# MIN_NAME_CHARS above already guards against for match.py's scorer, and
# would reintroduce it here (e.g. "king" inside "kingston" scoring 100).
LEAK_FUZZY_MIN_RATIO = 80

# Below this many characters, a token is excluded from the *fuzzy* pass
# only (the exact pass still applies at MIN_NAME_CHARS). Measured on the
# real 44-page file's own printed footer: every single page prints "Page
# X of Y", and fuzz.ratio("page", "paige") == 88.9 -- comfortably above
# LEAK_FUZZY_MIN_RATIO, against a real (different-period) roster entry
# "Paige Riker". Left at MIN_NAME_CHARS, the fuzzy pass would fail every
# file in the batch on that one word alone, which makes verify useless in
# practice (a check that always fails carries no signal). Short tokens are
# simply too likely to land within one edit of *some* short name by
# chance -- also caught for real: "sath" vs. roster first name "Santha"
# scored 80.0, same file. This floor doesn't touch the *exact* check
# (MIN_NAME_CHARS=3), so a short name OCR'd correctly is still caught;
# only a short-token *near miss* is now left to a human, the same
# below-threshold-abstain tradeoff match.py already makes elsewhere.
LEAK_FUZZY_MIN_TOKEN_LEN = 5

# --- Cache ---
# Rendered page images are identifiable scanned data (raw side especially).
# Keep this outside data/, gitignored, and never sync/upload the directory.
CACHE_DIR = ".cache/melredact"

# --- Group-row overflow (full-width redaction strip) ---
# Real-file leak (SID 0204150202): the "Group members, if any:" row is
# handwritten across the *entire page width*, not just the left column --
# "King, Sfoh, Braydeh, Ganik" ran from x=184 (just right of the printed
# label) to x=488, well past COLUMN_SPLIT_X=400, into the Date/Period
# column. redact_bboxes_for_band therefore adds a second, full-width
# rectangle spanning the Group row's own height and everything below it
# down to the header's bottom border, on top of the unchanged left-column
# box. Its top edge is *this page's own* located Group-row anchor (from
# segment.locate_header_anchors -- OCR'd per page, survives skew the same
# way name_top/teacher_top/group_top already do for row assignment) plus
# this offset, not a fixed y.
#
# The offset is measured, not guessed: a row-by-row dark-pixel-fraction
# scan of the real leak's own header band (x 420-560pt) found a clean,
# genuinely-blank gap from y=105pt to y=111pt (dark fraction exactly
# 0.000 every row) separating Period's own ink (fading out by ~104pt)
# from the group-overflow ink beginning (~112pt) -- the label itself
# (group_top) lands at 105.8pt, right at the top edge of that gap. +2
# lands the split at ~108pt, the middle of the measured clean band, so it
# clears Period's real ink with room on one side and the overflow ink
# with room on the other, instead of sitting right on either edge.
GROUP_ROW_SPLIT_OFFSET_PT = 2

# --- Consensus-ink detection (template-agnostic handwriting finder) ---
# See melredact/consensus.py's module docstring for the full two-pass
# method (per-block density-vs-median, then group occurrence-frequency) and
# CLAUDE.md's "A leak class verify_no_leaked_names cannot catch" section for
# the real find this was built for: two page-2 freehand names in
# data/PRT/010406_PD1_PRT.pdf (010406_PD1_PRT_p026 "Brian Lu",
# 010406_PD1_PRT_p034 "Ollie Maduro") that redaction never reaches (only the
# header page is redacted) and that verify_no_leaked_names cannot flag
# either -- Maduro is not on the roster at all, so there is no roster token
# for a text-based check to match against.
#
# DPI/block/density/connected-block values are unchanged from the original
# diagnostic script's own real-data validation (positive control: 104/104
# real header pages correctly flagged known-present handwriting; see
# scripts/diagnose_consensus_ink.py's module docstring for the full
# before/after at BLOCK_PX 24 vs 16).
CONSENSUS_DPI = 200
CONSENSUS_BLOCK_PX = 16
CONSENSUS_DENSITY_DIFF_THRESHOLD = 0.15
CONSENSUS_MIN_CONNECTED_BLOCKS = 3
CONSENSUS_ECC_DOWNSCALE = 0.25
CONSENSUS_ECC_ITERS = 200
CONSENSUS_ECC_EPS = 1e-8

# A (worksheet_type, page_offset) group needs at least this many
# successfully-aligned packets before a per-block median -- and therefore
# the occurrence-frequency vote below -- is trustworthy at all. Below this,
# the check holds nothing and says so explicitly in the run report, rather
# than voting on a sample too small to mean anything. Unchanged from the
# original diagnostic script's own calibration: "the practical floor given
# the real 020415 shipped-output re-check only has 5-11 files per group."
CONSENSUS_MIN_GROUP_SIZE = 5

# Pass one's registration tolerance (2026-08-14 fix). Diagnosed on real
# data, not assumed: comparing each packet's own block density to the exact
# same-position group median (the original pass one) flagged 135 regions on
# Hannel MPR PD2.pdf and 401 on Hannel PRT PD2.pdf, with no known real leak
# in either file. Two pieces of evidence ruled out per-copy darkness/
# contrast as the cause: each packet's *whole-page* mean block density
# varied by only ~0.0006-0.0015 across the entire 20-22-packet group (a
# ~1-3% relative spread -- there is no meaningful "some copies are just
# darker scans" effect to normalize away), and the correlation between a
# flagged region's own peak density-diff and its packet's whole-page
# darkness offset was weak to nonexistent (r=0.32 on MPR, r=0.02 on PRT).
# What the flagged regions actually looked like instead: 58-66% of them
# were exactly one block tall (a single text-line row), most sitting
# directly on top of a position where the group median already shows
# substantial ink (thin-region median-density-there: 0.156 on MPR, 0.150 on
# PRT) -- i.e. the same printed line nearly every other packet also has,
# just quantized into a different block by a sub-block registration
# difference between copies. This matches consensus.py's own already-
# documented ECC finding that alignment leaves small, smoothly-varying
# local distortion behind (Dice overlap only 0.29-0.44 even post-alignment)
# -- registration jitter, not darkness, confirmed by direct rendering of
# five spot-checked flagged regions (two on 010406_PD1_PRT.pdf, three on
# Hannel PRT PD2.pdf): every one was a printed word, table-border rule, or
# footer "Page X of Y" marker, never a blank patch of elevated density.
#
# Fix: before diffing, the group median is dilated (per-block max filter,
# radius CONSENSUS_REGISTRATION_TOLERANCE_BLOCKS) so a packet's block is
# only flagged when its density exceeds the *highest* median density in its
# own immediate neighborhood, not just its exact-position counterpart --
# tolerating a sub-block registration shift in either direction without
# tolerating genuinely new ink, which by definition has no corresponding
# elevated median density nearby at all. A per-copy density-normalization
# fix (matching each packet's ink density to a common reference before
# comparing) was considered and rejected: the darkness-correlation
# evidence above shows there is no real per-copy darkness effect to
# normalize, so it would have added a step that does nothing on the actual
# failure mode while risking flattening genuine differences in how heavily
# a student presses a pen.
#
# radius=1 (the smallest possible correction) already measured a 93-90%
# reduction in flagged regions (135->9 on MPR, 401->39 on PRT) while both
# known real leaks (010406_PD1_PRT_p026, _p034 -- see "A leak class
# verify_no_leaked_names cannot catch" in CLAUDE.md) stayed flagged with
# dozens of surviving regions each, nowhere close to the drop-to-zero a
# too-aggressive radius would risk. Larger radii (2, 3) were measured and
# rejected: they reduce the remaining noise further but with sharply
# diminishing returns relative to the growing risk of tolerating a
# genuinely misplaced word of real content, so the minimal value that
# already achieves the bulk of the real reduction was kept.
CONSENSUS_REGISTRATION_TOLERANCE_BLOCKS = 1

# Pass two, replaced 2026-08-14: the previous CONSENSUS_ANOMALY_MAX_GROUP_
# FRACTION frequency threshold (a cluster shared by more than X% of the
# group was an ordinary field, otherwise anomalous) was calibrated on a
# single file (010406_PD1_PRT.pdf's page-2 group, whose two-clearly-
# separated-bands gap was documented in this constant's own prior
# docstring, kept in git history) and did not generalize: re-run against
# the two Hannel files (with the registration fix above already applied,
# to isolate this problem from the one it fixed) the same occurrence-
# fraction distribution was *continuous* at every candidate cutoff --
# no step of more than ~5 points anywhere on either file, so no fraction
# threshold could separate "shared field" from "isolated mark" on this
# data the way it cleanly did on 010406's structured-answer worksheet.
# Root cause: real free-response handwriting varies in *position*, not
# just presence -- two students answering the same prompt rarely land on
# the exact same blocks, so their marks never cluster into one
# high-occurrence group under bbox-overlap clustering even though they are
# both, correctly, ordinary answer ink in the same general area.
#
# Replacement: a spatial writing-zone mask. A block position is in the
# zone when at least CONSENSUS_WRITING_ZONE_MIN_SHARE *other* packets in
# the group have their own (registration-tolerant) flagged ink within
# CONSENSUS_WRITING_ZONE_DILATION_PT of it -- corroboration from nearby
# ink, not exact-position overlap, is what marks an area as a place the
# class writes. A flagged region is held only when no such corroboration
# exists anywhere in its own footprint, regardless of how many total
# packets have *some* ink *somewhere* on the page (see consensus.py's
# `_zone_params`/`_analyze_group`).
#
# Dilation margin, from real data (post-registration-fix hold counts,
# dilation swept at 0 / 11.5pt / 23.0pt / 34.6pt, i.e. 0/2/4/6 blocks,
# CONSENSUS_WRITING_ZONE_MIN_SHARE=2 throughout):
#   010406_PD1_PRT.pdf (PRT, 44 scored packets): 42 -> 16 -> 7 -> 6 held.
#     Clear knee at 23.0pt: the 0->23pt step absorbs the bulk of the
#     ragged-answer-position noise (42->7), while the next 11.6pt of
#     dilation buys only one more packet (7->6) -- diminishing returns,
#     not a continuing clean trend, so pushing further isn't justified by
#     the data. Both real leaks (010406_PD1_PRT_p026, _p034) stayed held
#     at every radius tested, including the largest.
#   Hannel PRT PD2.pdf (PRT, 20 packets): 13 -> 10 -> 8 -> 6 held. No knee
#     as clean as 010406's -- a steadier decline, consistent with this
#     file's free-response prose having no single characteristic answer-
#     field size the way 010406's structured digit answers do. Reported
#     plainly rather than picking a number that pretends to a gap that
#     isn't there: 23.0pt was kept anyway, both because it is the value
#     010406's real gap justifies and because -- see the collision note
#     below -- both files parse to the identical worksheet_type "PRT" and
#     therefore share one config entry regardless.
#   Hannel MPR PD2.pdf (PCMEL_MPR_ADR, 22 packets): stuck at 6 held at
#     *every* dilation radius tested, 0 through 34.6pt. Direct cause: after
#     the registration fix, MPR's remaining flagged regions formed zero
#     occurrence>=2 clusters at any radius -- there is no shared-position
#     signal on this file for a spatial mask to find at all, clean gap or
#     not. Spot-checking the 6 residual holds found a mix already
#     consistent with CLAUDE.md's three real categories: one confirmed
#     genuine stray mark (a hand-drawn doodle in the top margin, correctly
#     held -- the check cannot distinguish a doodle from a name, and a
#     human clearing it via the manual queue is the intended, accepted
#     cost) and the remainder further residual printed-text/table-border
#     registration noise the tolerance fix above didn't fully absorb.
#
# worksheet_type collision, the reason there is no genuine *per-type* split
# in the values below despite the dict-keyed config shape: 010406_PD1_
# PRT.pdf and Hannel PRT PD2.pdf both parse to the identical footer-read
# worksheet_type "PRT" (WORKSHEET_TYPE_PATTERN strips the trailing
# "(mm/yyyy)" the same way for both) even though their real page-2 content
# is structurally very different -- one a structured digit-answer table,
# the other free-response prose. worksheet_type is not a fine enough key
# to separate them, so one config entry necessarily governs both real
# files; the value below is 010406's own real-gap-justified number, which
# also happens to give Hannel PRT a real, if less dramatic, reduction
# (13->8 held at this same dilation). CONSENSUS_WRITING_ZONE_DILATION_PT/
# CONSENSUS_WRITING_ZONE_MIN_SHARE are still dict-keyed by worksheet_type
# (via `consensus._zone_params`), not hardcoded constants, so a future
# teacher's worksheet_type that genuinely does have its own clean gap can
# get its own calibrated entry without a code change -- there just isn't
# real evidence yet to put a *different* number in either of today's two
# real keys.
CONSENSUS_WRITING_ZONE_DILATION_PT: dict[str, float] = {}
CONSENSUS_WRITING_ZONE_DILATION_DEFAULT_PT = 23.0
CONSENSUS_WRITING_ZONE_MIN_SHARE: dict[str, int] = {}
CONSENSUS_WRITING_ZONE_MIN_SHARE_DEFAULT = 2
