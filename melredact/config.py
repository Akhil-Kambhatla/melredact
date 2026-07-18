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
# HEADER_SEARCH_MAX_TOP. Measured off the real file: the header's own
# content ends by ~135pt (group row + one row height) while body text below
# doesn't start until ~172pt -- a ~24pt-plus-tail-of-body-content gap, so
# a bottom bound of "group row + one row height + 10pt" stays clear of it
# with room to spare, while still comfortably covering a handwritten
# group-members line that runs a bit taller than the printed label.
ROW_ASSIGNMENT_BOTTOM_SLACK_PT = 10

# --- Footer ---
FOOTER_BAND_TOP = 700
FOOTER_PAGE_MARKER = {"x0": 513, "x1": 570, "top": 747}
FOOTER_WORKSHEET_TYPE = {"x0": 29, "x1": 160, "top": 736}
# Regex-extracted from the footer text region, e.g. "Page 1 of 2".
PAGE_MARKER_PATTERN = r"Page\s+(\d+)\s+of\s+(\d+)"

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
