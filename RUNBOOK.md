# RUNBOOK — running melredact against the real Hannel MPR PD2 file

Assumes a fresh terminal, cwd = repo root, venv not activated.

## 0. Files involved

- Scan: `data/MPR/Hannel MPR PD2.pdf` (44 pages, ~77MB, ~22 packets)
- Roster: `data/teacher_codes/Teacher Codes_Student Codes_Demographics - 020415.csv`

Everything identifiable lives under `data/`, no exceptions — both files above are gitignored
real PII. `data/` is organized by subfolder: `MPR/` and `PRT/` for scanned worksheet PDFs by
type, `teacher_codes/` for roster CSVs, `samples/` for small smoke-test PDFs. There's also
`data/samples/3 Sample Hannel MPR PD2.pdf` (6 pages) if you ever want a faster smoke test
before running the full 44-page file.

**About the blank rows in the roster CSV:** this teacher's tab covers multiple periods (02
through 06 in the current export), with a blank row separating each period's block — that's how
John maintains the sheet, and it survives every export. `load_roster` parses those blank rows as
period-block delimiters, not errors (see CLAUDE.md's "The roster is one tab, many periods"). It
also automatically narrows matching to period 02 for this file, inferred from "PD2" in the
filename — you don't need to pass anything extra for the commands below to do the right thing.
If you ever run against a scan whose filename doesn't spell out its period that clearly, both
`review_app.py` and `cli.py run` take an explicit `--period` (e.g. `--period 2`).

## 1. Activate the venv / first-run setup

```
source .venv/bin/activate
pip install -r requirements.txt
```

The `pip install` is idempotent — safe to run even if the venv is already populated. It'll just
confirm everything's satisfied.

**PaddleOCR model download.** `melredact/ocr.py` builds the OCR engine lazily, on the *first*
call that actually OCRs something (i.e. the first time you point either tool at a real scan —
not on `import`, not on `pytest`). The first construction downloads model weights to
`~/.paddlex/official_models` (and some state under `~/.cache/paddle`) and needs network access.
On this machine those directories already exist, so you likely won't see a download at all — but
if you do, expect it to happen once, silently-ish (some paddlex log lines in the terminal), taking
anywhere from ~10s to a couple minutes depending on your connection. It won't repeat on later runs
as long as those cache dirs stick around.

## 2. Processing the file

There is no headless "process this PDF" command, and that's deliberate: nothing gets redacted or
deleted without a human confirming a decision per packet (see CLAUDE.md's decisions-store
section). "Processing the file" *is* launching the review app — segmentation and candidate
scoring happen automatically when it loads, on top of the real scan and real roster:

```
streamlit run review_app.py -- "data/MPR/Hannel MPR PD2.pdf" "data/teacher_codes/Teacher Codes_Student Codes_Demographics - 020415.csv"
```

(Quote both paths — both have spaces.) This opens a browser tab. Streamlit will print a local
URL in the terminal too, in case the tab doesn't auto-open.

### What happens on load, and how long each part takes

All OCR (not just these two `st.cache_data`-memoized stages) is now also disk-cached by
`melredact/ocr.py`, keyed on the source file's own content hash + page + dpi + the exact region
requested (`.cache/melredact/ocr/`) — so unlike `st.cache_data`, it survives a `streamlit run`
restart or a second `cli.py run`, not just one process's lifetime. The first time you ever point
either tool at a given scan, expect the full cost below; every time after that, each of these
stages reads from disk instead of re-running OCR.

1. **"Segmenting PDF into packets..."** (spinner) — OCRs a small header-region crop and a
   small footer-region crop on *every* page (44 pages × 2 crops = 88 small OCR calls) to find
   packet boundaries from the footer's "Page X of Y". Measured cold on the real file: 7.01 min
   (420.6s across all 88 calls, ~4.8s/call); near-instant once cached.
2. **"Scoring name candidates against the roster..."** (spinner) — OCRs the header band on each
   of the ~22 header pages to pull out Name/Teacher/Group/Date/Period, then fuzzy-scores Name
   against the roster. This is the *same* header-crop OCR call segmentation's own header check
   already made (identical bbox, identical dpi) — with the cache in place this stage is a cache
   hit, not a second OCR pass. Measured on the real file: 0s — confirms this is a genuine cache
   hit off step 1, not a second OCR pass.
3. Opening each packet in the UI renders and disk-caches its header-page preview image
   (`.cache/melredact/...`) the first time you view it, and now also reuses the same OCR cache
   for the field table (`extract_header_fields`, itself wrapped in `st.cache_data`) — both a
   second or two the first time, instant after that. Before this cache existed, re-opening a
   previously-viewed packet re-ran OCR on its header every single time (the ~20s/packet review
   slowdown); it doesn't anymore.
4. **"Run redaction pipeline"** (sidebar button, see below) — for every packet with a decision
   recorded, this OCRs *every page in that packet* at full 300 DPI (vs. 150 DPI for the
   on-screen preview) to rebuild the invisible text layer, then redacts and verifies. Measured
   cold on the real file: ~29s/page (635.2s across the 22 pages in 11 approved packets) — more
   than a header/footer crop's ~4.8s/call, but nowhere near the 10-25 min/page this section
   previously (incorrectly) cited. A full cold run of all three stages above end to end
   (segmenting all 44 pages, scoring, then redacting 11 approved packets/22 pages) took 17.6 min
   total, not the ~1h30m previously logged here — that older figure was inflated by the same
   miscalculation. A second run against the same file (decisions unchanged, cache warm) is
   expected to be near-instant, since every OCR call becomes a disk-cache hit. In practice this
   cost is paid once, incrementally, as you approve packets across a review session, not all at
   once at the end — but the real per-file cold budget is minutes, not hours.

**Hung vs. slow:** there's no per-page progress bar during OCR, just the spinner text above (or,
for step 4, no progress indicator at all — it's a plain function call, not a spinner-wrapped
one). If you want a heartbeat, check CPU: PaddleOCR on CPU should pin a core near 100% while a
stage is running (`top` in another terminal, or Activity Monitor). A spinner (or a quiet
terminal) sitting still with CPU also sitting near 0% for a long stretch is a better sign of
"actually stuck" than that alone — brief dips between OCR calls are normal, and step 4's per-page
cost above is normal too, not a hang.

## 3. Using the review UI

Sidebar (always visible):
- Scan name, roster size, packet count.
- ⏳ Pending / ✅ Approved / 🚫 Rejected counts.
- "All packets" expander — one line per packet, prefixed with its status icon (⚠️ issues /
  ⏳ pending / ✅ approved / 🚫 rejected).
- "Run redaction pipeline" button — disabled only if there are zero packets total; otherwise
  live the whole time (it reloads decisions from disk fresh each click, so you can review a few
  packets, run it, review more, and run it again).

Main panel, per packet:
- Prev/Next buttons and a packet dropdown (`<pdf-stem>_p<page>` tags — these are the stable
  identity keyed off first physical page, not position in the list).
- Side-by-side images: original header-page scan vs. the redaction preview, captioned either
  **"border detected"** or **"fallback band used"** — this is your direct signal for the
  border-detection question below. The preview box is stamped "SID: ... / PD: ..." for whichever
  candidate is currently selected in the decision radio below (live -- changing the radio updates
  it), so what you see here is what Confirm will actually produce, not a placeholder.
- A field table showing OCR'd Name/Teacher/Group/Date/Period text.
- A candidate table (top 5 roster matches, score, whether each clears the auto-assign bar,
  whether it's the one actually auto-assigned).
- A "Search the full roster" expander to hand-pick a SID regardless of the candidate list.
- A radio (candidates + "Not on roster (no consent)") pre-selected to the auto-assign suggestion
  when there is one, plus a "Confirm decision" button. Nothing is written to `decisions/` until
  you click Confirm — the pre-selection is just a suggestion.
- Packets with unresolved segmentation issues show a warning banner and can only be rejected
  (marked not-on-roster), never assigned a SID, until the issue is resolved out of band.

Click "Run redaction pipeline" when you've confirmed what you want processed this round. Expect
a sidebar result like `18 written, 2 deleted, 0 held back for review, 4 still pending review`. If
a packet fails one of §6's checks (or its decision names a SID not on the roster, or it still has
unresolved segmentation issues), that one packet is held back — its bad output is deleted, not
left sitting in `out/`, and a separate `Held back: <tag> (sid ...): <reason>` warning appears in
the sidebar for it — but every other packet in the same run still gets processed; one bad packet
no longer blocks the rest (see CLAUDE.md's "A per-packet failure holds back only that packet").

## 4. Running the CLI instead of clicking the button

`melredact/cli.py` gives you `run`/`verify` from a plain terminal, no browser tab required. It
does **not** replace the review UI — packets still only get decided in `review_app.py`, since
that's where a human actually looks at each one. What the CLI gives you is a way to (re-)apply
whatever's already in `decisions/<pdf-stem>.json` without relaunching Streamlit — useful after
editing a decision by hand, after a roster correction, or just to re-run headlessly once review is
done for the day.

```
python -m melredact.cli run --pdf "data/MPR/Hannel MPR PD2.pdf" --roster "data/teacher_codes/Teacher Codes_Student Codes_Demographics - 020415.csv"
```

(Quote both paths — both have spaces.) Defaults: `--out out`, `--decisions decisions`. Add
`--flatten` to flatten to images instead of keeping the OCR text layer (see CLAUDE.md). Output
looks like:

```
wrote   out/020415/02/0204150204.pdf
deleted out/020415/02/0204150211.pdf
pending Hannel MPR PD2_p024 (not yet reviewed)
held back Hannel MPR PD2_p002 (sid 0204150204): header border not confidently detected: HeaderBand(...)
...
17 written, 2 deleted, 1 held back for review, 4 still pending review
```

If nothing has been reviewed yet, it says so and every packet reports pending — that's not a bug,
it means run the review UI first.

**A held-back packet does not stop the rest of the run.** Before 2026-07-20, any one packet
failing one of the checks in §6 below (bad header detection, uncovered group-row ink, a leak, an
unresolved segmentation issue, or a decision naming a SID not on the roster) raised and aborted
`run_dispositions` for the *whole* pdf — every packet after the failing one in page order was
silently never attempted, even packets that had nothing wrong with them. That's fixed: each of
those five conditions now holds back only its own packet (no output written for it, any prior
output for that same tag left untouched) and the run continues to every other packet. The CLI
exits 1 whenever anything was held back, so a script can tell "some packets need a human to look
at them" apart from "clean run" without parsing output. See CLAUDE.md's "A per-packet failure
holds back only that packet" for the mechanics.

```
python -m melredact.cli verify --roster "data/teacher_codes/Teacher Codes_Student Codes_Demographics - 020415.csv"
```

Re-checks every `out/<teacher_code>/<period>/<worksheet_type>/<SID>.pdf` against the roster (same check §6 below
describes, just invoked directly instead of pasted as a Python snippet). `--out` overrides the
directory scanned in either subcommand if you're not using the default `out/`. `verify` takes no
`--pdf` — output file names no longer trace back to a source scan (see CLAUDE.md), so there's
nothing scan-specific to filter by; it walks the whole tree under `--out`. If it finds nothing
there at all, that's an error (exit 1), not a silent pass — see §6.

## 5. Where output lands

- `out/<teacher_code>/<period>/<worksheet_type>/<SID>.pdf` — one redacted PDF per approved packet,
  e.g. `out/020415/02/PRT/0204150204.pdf` (teacher code + period are the SID's own digits;
  worksheet_type is read off the packet's own footer, e.g. "PRT" or "PCMEL_MPR_ADR", since a
  student has one SID but multiple worksheet types that must never collide on one path). Rejected
  (non-consent) packets never get a file written; pending packets are untouched either way. A
  *corrected* decision (reviewer overrides an earlier match to a different SID) removes the old
  SID's file and writes the new one — tracked per-tag via a small ledger under
  `out/.ledger/<pdf-stem>.json`, not a directory-wide sweep (see CLAUDE.md). See CLAUDE.md's
  "present in the output tree iff has a confirmed, approved SID" invariant.
- `decisions/<pdf-stem>.json` — e.g. `decisions/Hannel MPR PD2.json`. The three-state
  `packet_tag -> sid | null` mapping. Safe to inspect directly; it's just JSON.
- `.cache/melredact/<pdf-stem>/page_<idx>_<dpi>.png` — rendered preview images. Never sync this
  directory anywhere (see `data/README.md`) — it's identifiable scanned content even though it's
  gitignored.
- `.cache/melredact/ocr/<file-content-hash>/page_<idx>_<dpi>_<bbox>.json` — cached OCR word
  lists (see CLAUDE.md's OCR-caching bullet). Same gitignore/never-sync rule applies — this is
  extracted text from identifiable scanned content, same sensitivity as the preview PNGs.

## 6. Running verify afterward

`run_dispositions` runs two checks on every packet it redacts, *before* declaring success — if
either were going to fail for a given packet, that packet's bad file would already have been
deleted and the packet held back with a reason (see §2's "held back" line and §5's note) rather
than written. So an "N written" result already means both checks passed for those N files
specifically — a held-back packet elsewhere in the same run doesn't put those N files in doubt:

1. `find_uncovered_group_words` — geometric, not text-based: did the redaction rectangles
   actually cover every word OCR assigned to the Group row, regardless of what OCR thinks that
   word says. This is the check that would have caught the real "Ganik" leak directly, since it
   doesn't depend on text matching at all.
2. `verify_no_leaked_names` — extracts text from the finished file and checks it against the
   roster, both exactly and fuzzily (`LEAK_FUZZY_MIN_RATIO`) — the fuzzy pass exists because the
   real leak's ink ("Gonik") was OCR'd into a real but non-matching token ("Ganik"), which an
   exact-only check can't catch even when the coverage bug above is separately fixed.

If you want to independently re-check everything currently sitting in `out/` (e.g. after closing
and reopening the terminal), run:

```
python -m melredact.cli verify --roster "data/teacher_codes/Teacher Codes_Student Codes_Demographics - 020415.csv"
```

Note this only re-runs check 2 above (text-based) — check 1 needs the original scan and this
page's located anchors, which aren't available from the finished file alone (see CLAUDE.md); it
only ever runs at write time, inside `run_dispositions`.

**Pass** looks like a plain `ok   <teacher>/<period>/<worksheet_type>/<SID>.pdf` line for every file,
followed by a summary line: `N file(s) checked, 0 failed`. The summary always prints, pass or fail —
it's the explicit "this is how many files were actually checked" signal, so a clean-looking run
can't be confused with a vacuous one that silently checked nothing (the separate hard failure when
`--out` contains no matching files at all is still there too — see above — this is the count for
whenever it did run). `verify` covers every worksheet type under `--out` in one unscoped pass, so
a single invocation's summary count includes both MPR (`PCMEL_MPR_ADR/`) and PRT (`PRT/`) output
sitting side by side under the same `<teacher>/<period>/`.

**Fail** looks like `FAIL <filename>: [LeakFinding(page_index=..., sid=..., token=..., exact=...)]`
— one entry per (page, roster student, matched token) triple; `exact=False` means the fuzzy pass
caught it, not a literal match. A fail here on a file already sitting in `out/` would itself be
surprising/bad news, since `run_dispositions` should have caught and deleted it already — if you
ever see this, something's inconsistent (e.g. a file written by a different code path, or file
left over from before a fix) and worth investigating rather than re-running.

## 7. What a *wrong* result looks like (not just "didn't crash")

Both of these are silent-failure modes by construction — they don't throw, and in the flip case,
`verify_no_leaked_names` genuinely can't catch it either (it only checks whether banned tokens
are present as text *anywhere* on a page, not where — a repositioned-but-still-present word looks
identical to a correctly-positioned one to that check, and `find_uncovered_group_words` doesn't
help here either, since the *text layer's* position is what's wrong, not the raster paint job).
So this is what to actually look for.

### If the coordinate flip in `_pdf_baseline_y` were wrong

The redaction box itself (the raster paint job) doesn't go through this code path, so the visual
output — original scan vs. redaction preview vs. the final blacked-out box — would look completely
normal. The bug only affects *where the invisible OCR text layer lands in the PDF's content
stream*, so the corruption is invisible to the eye and invisible to `verify_no_leaked_names`.

What breaks: a word whose visual position is near the *top* of a page (small `top` in
page-point space, e.g. anything in the header band, `top` roughly 58–150) would get written with
a baseline near the *bottom* of the page instead, and vice versa — a flipped-but-unshifted
`page_height - top` becomes `top` directly, which mirrors everything about the vertical
midline. Concretely, look for:

- **Cmd+F search in Preview/Acrobat.** Search an output PDF for a word you know sits near the
  top of a page — e.g. "Date" or "Period" (the printed labels get OCR'd and re-emitted like
  everything else, since real scans have zero native text layer at all). If the match highlight
  jumps to the *bottom* of the page instead of near the header band where "Date:"/"Period:" are
  visually printed, that's the flip.
- **Read order via pdfplumber.** `page.extract_text()` groups words into lines using their `top`
  values read back out of the PDF. If those are all mirrored, the extracted line order for a page
  would read bottom-to-top relative to what's visually on the scan (footer text would appear
  first, header-row content last) instead of matching the visual top-to-bottom order.
- This is exactly what `test_coordinate_flip_round_trips_through_real_writer_and_reader` in
  `tests/test_redact.py` exists to pin down mechanically — if you ever suspect this in the real
  file, that test (not a fresh manual derivation) is the thing to trust.

### If border detection floors to the fallback band on a skewed page

Watch the caption under the redaction-preview image for that packet in the review UI:
**"fallback band used"** means live border detection didn't find all four bracketing rules (a
skewed, faint, or partially-scanned border) and it fell back to `HEADER_BAND_FALLBACK`'s fixed
coordinates (top 58 / bottom 148 / left 38 / right 574) rather than the actual drawn border.

The flooring logic itself is safe by design — a *detected* box can only be clamped outward toward
the fallback, never inward, so partial detection can't under-redact relative to the floor. The
actual risk on a skewed page is different: the fallback box is a fixed rectangle measured off
three flat, unskewed sample pages. If a real page is skewed enough that detection gives up
entirely, that fixed rectangle might not be big enough for *that particular* page's actual
(rotated) handwriting extent — the flooring guarantee only protects you relative to the
fallback's own numbers, not relative to what a skewed page actually needs.

So: whenever you see "fallback band used," don't just trust the black box — zoom into that
packet's redaction-preview image and check the edges. What a failure looks like: a sliver of
handwritten ink (part of a name, a stray descender) visible *outside* the black box's edges,
especially at the left/right/bottom edge closest to the skew direction. A correct fallback case
looks like the black box fully swallowing the Name/Teacher/Group column with clean margins on all
sides, same as a "border detected" packet would.
