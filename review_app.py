"""Human review UI for MEL MPR+ADR packet redaction.

    streamlit run review_app.py -- <scan.pdf> <roster.csv> [--out-dir DIR] [--decisions-dir DIR]
        [--round 2025-10]

`--round` restricts the whole session to one round group's packets (see
pipeline.filter_packets_by_round) -- useful for a small pilot against one
collection session inside a larger concatenated scan. The sidebar's
"Disable deletion (safety)" checkbox suppresses every deletion "Run
redaction pipeline" would otherwise perform, regardless of what any
individual decision says -- see pipeline.run_dispositions' `allow_delete`.

Nothing here writes a redacted PDF or deletes anything on its own -- this
is where a human turns match.py's candidate proposals into the final,
recorded `decisions` mapping pipeline.py's `run_dispositions` actually
acts on (see pipeline.py's module docstring for the three-state contract:
pending / approved SID / confirmed non-consent). Auto-assign's suggestion
is used only to pre-select the right radio option for a fast "looks right,
confirm" pass -- it is never written into `decisions` until a human clicks
Confirm, since nothing should reach a reviewer's eyes as "decided" without
a person having actually looked at the packet.

Rendered page images are cached to disk under CACHE_DIR (see config.py and
data/README.md) -- identifiable scanned data, gitignored, never sync it.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw

# streamlit-drawable-canvas 0.9.3 (the package originally wired in here) was
# broken against this project's installed Streamlit (1.59) two ways at
# once, not just the AttributeError a version-shim could route around: its
# own st_canvas() called streamlit.elements.image.image_to_url(image,
# width, ...) -- a function this Streamlit version moved and rewrote to
# take a LayoutConfig instead of a bare pixel width -- AND its bundled
# frontend JS (a pre-2023 build against an old streamlit-component-lib) no
# longer completed the newer Streamlit's iframe component handshake
# reliably enough for fabric.js's own mouse/drag event wiring to attach --
# observed directly as "Draw a new box"/"Move / resize" doing nothing at
# all, not a crash. The first half was patchable with a version shim (and
# was, in an earlier session); the second is a frontend bundle problem a
# Python-side shim cannot reach. Switched to `streamlit-drawable-canvas-
# fix` (PyPI, same `streamlit_drawable_canvas` import path, a maintained
# fork whose whole purpose is tracking newer Streamlit releases -- pinned
# to `streamlit>=1.49.0` in its own metadata, bracketing this project's
# 1.59) instead of hand-patching either problem here: it already imports
# image_to_url/LayoutConfig from their current real locations, and ships
# its own rebuilt frontend bundle (confirmed different from 0.9.3's, not
# just re-packaged) that responds to draw/resize again. Same "route around
# an upstream incompatibility rather than patch our own code around it"
# posture pdfio.py's open_pdf already takes for a pdfplumber/pdfminer.six
# gap -- see CLAUDE.md.
from streamlit_drawable_canvas import st_canvas

from melredact.blocks import (
    BlockMeaning,
    BlockResolution,
    RoundGroup,
    collect_packet_dates,
    decisions_scope_mismatches,
    disagreeing_packets,
    format_resolution_report,
    format_round_report,
    group_into_rounds,
    load_block_metadata,
    normalize_block,
    resolve_block,
    round_disagreeing_tags,
    round_labels_by_tag,
    save_resolved_block_record,
)
from melredact.config import (
    CACHE_DIR,
    HEADER_SEARCH_MAX_TOP,
    MANUAL_EDITOR_TARGET_WIDTH_PX,
    MIN_MARGIN,
    MIN_SCORE,
    RENDER_DPI_PREVIEW,
)
from melredact.consensus import ConsensusAnalysis, analyze_consensus_anomalies, format_consensus_report
from melredact.match import assign_all
from melredact.orientation import orientation_for
from melredact.pdfio import open_pdf
from melredact.pipeline import (
    duplicate_decisions_within_round,
    filter_packets_by_round,
    is_composition_confirmable_issue,
    list_manual_queue,
    load_composition_overrides,
    load_decisions,
    load_detection_overrides,
    load_manual_geometry,
    load_orientation_overrides,
    load_page_order,
    manual_queue_draft_path,
    packet_tag,
    propose_all,
    release_from_manual_queue,
    run_dispositions,
    save_composition_overrides,
    save_decisions,
    save_detection_overrides,
    save_orientation_overrides,
    save_page_order,
)
from melredact.redact import (
    Bbox,
    detect_header_band,
    find_uncovered_group_words,
    redact_bboxes_for_band,
    render_redaction_preview,
    render_region_preview,
)
from melredact.roster import Roster, RosterError, filter_roster_by_name, infer_period_from_filename, load_roster
from melredact.segment import (
    Packet,
    SegmentResult,
    extract_header_fields,
    find_reversed_continuation_header_pairs,
    header_row_height,
    page_words,
    segment_pdf,
)

DPI = RENDER_DPI_PREVIEW


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_path")
    parser.add_argument("roster_path")
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--decisions-dir", default="decisions")
    parser.add_argument(
        "--period",
        default=None,
        help="restrict matching to this period's block of the roster (e.g. '2' or '02'); "
        "inferred from the scan filename (e.g. 'PD2') if omitted. Ignored when the roster has a "
        "<roster_stem>_blocks.json sidecar -- see --class-period below.",
    )
    parser.add_argument(
        "--class-period",
        type=int,
        default=None,
        dest="class_period",
        help="class period this scan belongs to, ONLY meaningful when the roster has a "
        "<roster_stem>_blocks.json sidecar (see melredact/blocks.py) -- in that case the scan "
        "filename's 'PDn' means class period, never roster block, and this overrides that "
        "inference. Ignored entirely when no block metadata sidecar exists.",
    )
    parser.add_argument(
        "--block",
        default=None,
        help="explicit block override, ONLY meaningful when block metadata exists -- for when "
        "packet dates can't be resolved automatically. Still requires ticking the on-screen "
        "confirmation before any packet is shown.",
    )
    parser.add_argument(
        "--round",
        default=None,
        help="restrict this whole review session to one round group's packets (e.g. '2025-10' or "
        "'undated', see the 'Round grouping' report shown on screen) -- packets outside it are "
        "not segmented for matching, not shown, not redacted, not written, and never looked up in "
        "the ledger when 'Run redaction pipeline' is used, so a session scoped to one round can "
        "never delete or disturb another round's already-shipped output. Useful for a small pilot "
        "against one session inside a larger concatenated scan.",
    )
    return parser.parse_args(sys.argv[1:])


@st.cache_data(show_spinner="Segmenting PDF into packets...")
def _segment(pdf_path: str, orientation_overrides: dict[int, int], page_order: list[int] | None = None) -> SegmentResult:
    return segment_pdf(pdf_path, orientation_overrides=orientation_overrides, page_sequence=page_order)


@st.cache_data(show_spinner=False)
def _orientation_map(pdf_path: str, orientation_overrides: dict[int, int]) -> dict[int, object]:
    """page_index -> orientation.PageOrientation for the whole file, so the
    page-stack UI (see _render_page_stack) can show each page's own
    detected angle/score/needs-confirmation state without re-deriving it
    per page render. Cheap regardless: the expensive part (PaddleOCR
    classification) is disk-cached by content hash in orientation.py
    itself, unaffected by st.cache_data's own in-memory layer here."""
    return orientation_for(pdf_path, overrides=orientation_overrides).by_page()


def _block_metadata(roster_path: str):
    # Not cache_data: cheap (one small JSON read), and load_block_metadata
    # returns None for the overwhelmingly common case -- no reason to pay
    # cache bookkeeping for that.
    return load_block_metadata(roster_path)


@st.cache_data(show_spinner="Reading packet dates for block resolution...")
def _block_resolution(
    pdf_path: str, class_period: int, roster_path: str, orientation_overrides: dict[int, int], page_order: list[int] | None = None
) -> BlockResolution:
    # roster_path is only here to key the cache correctly if the sidecar
    # ever changes between reruns -- resolve_block itself only reads
    # `metadata`, recomputed fresh by the caller (cheap, see _block_metadata).
    metadata = load_block_metadata(roster_path)
    dates = collect_packet_dates(pdf_path, orientation_overrides=orientation_overrides, page_sequence=page_order)
    return resolve_block(dates, class_period, metadata)


@st.cache_data(show_spinner="Reading packet dates for round grouping...")
def _round_data(pdf_path: str, orientation_overrides: dict[int, int], page_order: list[int] | None = None) -> tuple[list, list[RoundGroup]]:
    """(dates, groups) -- computed together and cached once per pdf_path
    (same OCR-cached date-extraction pass as block resolution's own
    _block_resolution, just grouped into contiguous rounds instead of
    reduced to one file-level majority) so every rerun of this Streamlit
    script (a button click, Prev/Next) doesn't repeat it. See blocks.
    group_into_rounds for the grouping rule; round labelling never
    influences matching, so this is entirely independent of _proposals."""
    segmented = _segment(pdf_path, orientation_overrides, page_order)
    dates = collect_packet_dates(pdf_path, segmented=segmented, orientation_overrides=orientation_overrides)
    groups = group_into_rounds(segmented.packets, dates)
    return dates, groups


@st.cache_data(show_spinner="Checking for template-agnostic handwriting anomalies (consensus-ink)...")
def _consensus(pdf_path: str, orientation_overrides: dict[int, int], page_order: list[int] | None = None) -> ConsensusAnalysis:
    """Cached the same way _round_data is: the expensive part (whole-group
    rasterize + ECC alignment, see consensus.py) is itself disk-cached per
    (file, page, reference page, dpi, block size), so a warm rerun is
    cheap -- but there's no reason to repeat even the in-memory clustering
    on every Streamlit rerun (a button click, Prev/Next) within one
    session."""
    return analyze_consensus_anomalies(
        pdf_path, _segment(pdf_path, orientation_overrides, page_order), orientation_overrides=orientation_overrides
    )


@st.cache_data(show_spinner="Loading roster...")
def _roster(roster_path: str, pdf_path: str, period: str | None) -> Roster:
    return load_roster(roster_path, period=period, infer_period_from=pdf_path)


@st.cache_data(show_spinner="Scoring name candidates against the roster...")
def _proposals(
    pdf_path: str, roster_path: str, period: str | None, orientation_overrides: dict[int, int], page_order: list[int] | None = None
):
    return propose_all(
        pdf_path, _segment(pdf_path, orientation_overrides, page_order), _roster(roster_path, pdf_path, period),
        orientation_overrides=orientation_overrides,
    )


@st.cache_data(show_spinner=False)
def _header_fields(pdf_path: str, page_index: int, orientation_overrides: dict[int, int]):
    """Cached wrapper around extract_header_fields, keyed by (pdf_path,
    page_index) rather than a pdfplumber Page object (not hashable in a
    way st.cache_data can use) -- opens the pdf fresh, same pattern as
    _page_image below. Without this, every Streamlit rerun (a button
    click, a radio change, Prev/Next) re-ran OCR-based header field
    extraction for whichever packet was on screen; with melredact.ocr's
    disk cache in place that call is now cheap even on a cold cache, but
    there's no reason to repeat even the in-memory anchor-location work
    on every rerun within one session."""
    with open_pdf(pdf_path, orientation_overrides=orientation_overrides) as pdf:
        return extract_header_fields(pdf.pages[page_index])


@st.cache_data(show_spinner=False)
def _header_words(pdf_path: str, page_index: int, orientation_overrides: dict[int, int]):
    """Cached wrapper around segment.page_words for the header band only --
    the raw word list find_uncovered_group_words needs to compute the live
    uncovered-ink advisory as a reviewer drags boxes in the manual editor
    (see _render_manual_editor). Same disk-cached OCR call segment.py's own
    field extraction already makes (see CLAUDE.md's "OCR is disk-cached"
    section), just also cached in memory across Streamlit reruns."""
    with open_pdf(pdf_path, orientation_overrides=orientation_overrides) as pdf:
        page = pdf.pages[page_index]
        return page_words(page, (0, 0, page.width, HEADER_SEARCH_MAX_TOP))


@st.cache_data(show_spinner=False)
def _page_image(pdf_path: str, page_index: int, dpi: int, orientation_overrides: dict[int, int]) -> Image.Image:
    """Disk-cached rendered page image. The cache filename folds in this
    specific page's own override angle (if any) -- without that, rotating
    a page via the UI's rotate controls (see _render_page_stack) would
    keep serving the pre-rotation PNG sitting at the same path, since
    nothing else about the filename would change."""
    override_angle = orientation_overrides.get(page_index)
    suffix = f"_ov{override_angle}" if override_angle is not None else ""
    cache_file = Path(CACHE_DIR) / Path(pdf_path).stem / f"page_{page_index:04d}_{dpi}{suffix}.png"
    if cache_file.exists():
        return Image.open(cache_file).convert("RGB")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open_pdf(pdf_path, orientation_overrides=orientation_overrides) as pdf:
        image = pdf.pages[page_index].to_image(resolution=dpi).original.convert("RGB")
    image.save(cache_file)
    return image


@st.cache_data(show_spinner=False)
def _page_size_pt(pdf_path: str, page_index: int, orientation_overrides: dict[int, int]) -> tuple[float, float]:
    """This page's own (width, height) in PDF points, post-orientation --
    just enough to pick an editor-specific DPI (see _editor_dpi_for_page)
    without rendering the page itself. Cheap (pdfplumber page-size read,
    no OCR, no rasterization) and cached the same way every other small
    per-page lookup in this file is."""
    with open_pdf(pdf_path, orientation_overrides=orientation_overrides) as pdf:
        page = pdf.pages[page_index]
        return (page.width, page.height)


def _editor_dpi_for_page(page_width_pt: float) -> int:
    """The DPI that renders this page at exactly MANUAL_EDITOR_TARGET_
    WIDTH_PX wide -- see config.py's own docstring on that constant for
    why the manual editor needs a fixed, page-derived DPI rather than
    reusing RENDER_DPI_PREVIEW: a canvas and a plain st.image of the same
    page must land on the identical literal pixel width regardless of
    container size, and computing that width from the page's own point-
    width (rather than picking one DPI for every page) keeps it true even
    for a page whose physical size differs (e.g. a still-unconfirmed 90/
    270-rotated page has swapped width/height). Clamped to a sane DPI
    range so a pathologically narrow or wide page can't produce an
    unreadably-low or absurdly-high resolution render."""
    if page_width_pt <= 0:
        return RENDER_DPI_PREVIEW
    dpi = round(MANUAL_EDITOR_TARGET_WIDTH_PX * 72.0 / page_width_pt)
    return max(72, min(300, dpi))


def _rotate_image_for_display(image: Image.Image, angle: int) -> Image.Image:
    """Pure in-memory rotation of an already-rendered page image, for the
    page-stack preview (see _render_page_stack) -- deliberately never
    touches the PDF, `open_pdf`, or OCR. Same angle convention `orientation.
    normalize_page_image` already established and verified by exact pixel
    round-trip against the classifier's own convention (see orientation.
    py's module docstring): `PIL.Image.rotate(angle, expand=True)`. Used to
    show a *candidate* rotation the reviewer hasn't committed yet -- see
    `_preview_angle`/`_set_pending_rotation` -- so trying several angles
    before settling on one costs nothing beyond this call."""
    angle = angle % 360
    if angle == 0:
        return image
    return image.rotate(angle, expand=True)


def _preview_angle(page_idx: int, committed_angle: int) -> int:
    """The angle currently shown for this page in the page-stack preview:
    an uncommitted candidate rotation if the reviewer is mid-adjustment,
    else whatever's actually committed (a saved override, or the
    detector's own resolved angle)."""
    return st.session_state.pending_rotation.get(page_idx, committed_angle)


def _set_pending_rotation(page_idx: int, angle: int | None) -> None:
    """Update (or clear) this page's *preview-only* candidate rotation --
    never written to disk, never touches orientation_overrides, so it
    cannot invalidate _segment/_proposals/_consensus/_page_image's own
    disk-cached work. This is the mechanism that lets a reviewer cycle
    through Left/Right/180 to find the right orientation without paying
    for a re-segment or re-OCR on every click -- only `_set_page_rotation`
    (called from the explicit Apply/Reset actions) commits anything real."""
    if angle is None:
        st.session_state.pending_rotation.pop(page_idx, None)
    else:
        st.session_state.pending_rotation[page_idx] = angle % 360


def _status_icon(tag: str, packet: Packet, decisions: dict[str, str | None], proposal) -> str:
    if packet.issues:
        return "⚠️"
    if tag not in decisions:
        if proposal is not None and proposal.is_held_match:
            # Consent hold (see pipeline.py's consent_hold): known-
            # consented, SID-unresolvable -- distinct from ordinary
            # pending, since no decision a human records here ever turns
            # this into a write.
            return "🔒"
        return "⏳"
    return "✅" if decisions[tag] is not None else "🚫"


def _decision_label(sid: str | None, roster: Roster) -> str:
    if sid is None:
        return "Not on roster (no consent)"
    entry = roster.by_sid[sid]
    return f"{sid} — {entry.full_name}"


# Sentinel for "no decision has actually been chosen yet" -- distinct from
# `None`, which is a real, confirmable choice ("Not on roster / no
# consent"). Never written to `decisions`; only ever used as a Decision
# radio option's paired value so the radio can genuinely have nothing
# selected instead of always landing on some option by construction (see
# `_decision_options`' own docstring for the real bug this exists to
# prevent: a packet OCR read no name for used to silently default the
# radio to "Not on roster", one accidental Confirm click away from
# rejecting a real student nobody actually looked at).
_NO_SELECTION = object()


def _search_added_key(tag: str) -> str:
    return f"search_added_sid_{tag}"


def _decision_options(
    tag: str, roster: Roster, candidate_options: list[tuple[str, str | None]], has_ocrd_name: bool
) -> list[tuple[str, object]]:
    """Every selectable Decision option for this packet, in the order the
    radio shows them: the matcher's own top candidates first, then any SID
    that reached this decision by a different route, then "Not on roster".

    Real bug this fixes (packet 70, reported 2026-08-15): the Decision
    radio's own options used to be built from *only* `candidate_options`
    (the matcher's top 5) plus "Not on roster" -- a student found via
    "Search the full roster" had no matching option in that list at all.
    Clicking "Use this roster entry" still correctly wrote the chosen SID
    to `decisions` immediately, but the radio widget itself, unaware of
    that write, kept showing whatever its own default was (here: "Not on
    roster", since no matcher candidate cleared MIN_SCORE) -- and the very
    next "Confirm decision" click (the large, obvious call-to-action
    sitting right below) used the RADIO's stale value, silently
    overwriting the correct search-selected decision with a non-consent
    rejection. Folding every route a decision can arrive by into one
    shared option list -- used for both the live preview and the radio
    itself, so they can never independently disagree -- is what makes that
    impossible: whichever SID a reviewer actually picked, by whatever
    route, is always a real, selectable, clearly-labelled option.

    `search_added` (see `_search_added_key`) and an already-`recorded`
    decision are each only added if not already present among the
    matcher's own candidates, and are visibly marked with where they came
    from ("found via roster search" / "previously recorded") -- a
    reviewer should never mistake either for a matcher proposal.

    When nothing has ever been decided for this tag AND OCR read no name
    at all (`has_ocrd_name=False`) and no search selection has been made
    either, a genuine "nothing chosen yet" placeholder (paired with the
    `_NO_SELECTION` sentinel) is prepended -- see `_NO_SELECTION`'s own
    docstring for why a pre-selected "Not on roster" on zero evidence is
    itself a hazard, not just an inconvenience."""
    options: list[tuple[str, object]] = list(candidate_options)
    known_sids = {sid for _, sid in options}

    search_added = st.session_state.get(_search_added_key(tag))
    if search_added is not None and search_added not in known_sids:
        options.append((f"🔍 {_decision_label(search_added, roster)} (found via roster search)", search_added))
        known_sids.add(search_added)

    recorded = st.session_state.decisions.get(tag)
    if recorded is not None and recorded not in known_sids:
        options.append((f"↺ {_decision_label(recorded, roster)} (previously recorded)", recorded))
        known_sids.add(recorded)

    options.append(("Not on roster (no consent)", None))

    if not has_ocrd_name and tag not in st.session_state.decisions and search_added is None:
        options.insert(0, ("— No name was read; choose a decision below —", _NO_SELECTION))

    return options


def _default_decision_sid(tag: str, auto_assignments: dict[str, str | None], has_ocrd_name: bool):
    """Which value the Decision radio should default to on the very first
    render this session (Streamlit ignores this the moment the widget has
    its own stored session_state value, i.e. after any interaction) --
    priority: an explicit already-recorded decision, then a roster-search
    selection, then the matcher's own auto-assignment, then (only when
    there is genuinely nothing to go on) the `_NO_SELECTION` placeholder
    rather than silently landing on "Not on roster"."""
    if not has_ocrd_name and tag not in st.session_state.decisions and st.session_state.get(_search_added_key(tag)) is None:
        return _NO_SELECTION
    if tag in st.session_state.decisions:
        return st.session_state.decisions[tag]
    search_added = st.session_state.get(_search_added_key(tag))
    if search_added is not None:
        return search_added
    return auto_assignments.get(tag)


def _init_state(pdf_path: str, decisions_dir: str) -> None:
    if "decisions" not in st.session_state:
        st.session_state.decisions = load_decisions(pdf_path, decisions_dir=Path(decisions_dir))
    if "detection_overrides" not in st.session_state:
        st.session_state.detection_overrides = load_detection_overrides(pdf_path, decisions_dir=Path(decisions_dir))
    if "composition_overrides" not in st.session_state:
        st.session_state.composition_overrides = load_composition_overrides(pdf_path, decisions_dir=Path(decisions_dir))
    if "orientation_overrides" not in st.session_state:
        st.session_state.orientation_overrides = load_orientation_overrides(pdf_path, decisions_dir=Path(decisions_dir))
    if "page_order" not in st.session_state:
        # A human-confirmed correction to physical page processing order
        # (see the page composition editor, _render_page_composition_
        # editor) -- None (the overwhelmingly common case) means "natural
        # physical order", segment_pdf's own existing default.
        st.session_state.page_order = load_page_order(pdf_path, decisions_dir=Path(decisions_dir))
    if "pending_rotation" not in st.session_state:
        # In-memory-only preview state (see _render_page_stack), never
        # persisted and never part of orientation_overrides -- a reviewer
        # cycling through candidate rotations before settling on one must
        # not repeatedly pay for a re-segment/re-OCR/re-consensus pass on
        # every click, only once, when a rotation is actually applied.
        st.session_state.pending_rotation = {}


def _set_page_rotation(pdf_path: str, decisions_dir: str, page_index: int, angle: int | None) -> None:
    """Record (or clear, when `angle` is None) a human's rotation choice
    for one physical page -- always wins over the detector's own guess for
    that page (see orientation.py's detect-and-ask design), and persists
    immediately so a re-run reproduces it without asking again. Every
    downstream cache (`_segment`, `_proposals`, `_consensus`, `_round_
    data`, ...) is keyed on this exact dict, so mutating it and rerunning
    naturally invalidates every in-memory cache entry that depended on the
    old orientation. This is the actual *commit* -- see `_render_page_
    stack`'s Apply/Reset actions, the only two things that call this;
    everything else (Left/Right/180) only ever touches the uncommitted
    `pending_rotation` preview via `_set_pending_rotation`, never this.
    Committing is disk-cache-cheap even so, since orientation.py's and
    ocr.py's own cache keys are page-scoped (see orientation.
    stable_ocr_identity) -- only the page whose own rotation actually
    changed pays for a fresh OCR pass, not every page in the file."""
    if angle is None:
        st.session_state.orientation_overrides.pop(page_index, None)
    else:
        st.session_state.orientation_overrides[page_index] = angle % 360
    save_orientation_overrides(pdf_path, st.session_state.orientation_overrides, decisions_dir=Path(decisions_dir))
    _set_pending_rotation(page_index, None)


def _current_page_sequence(page_count: int) -> list[int]:
    """The physical page processing order currently in effect: a human's
    saved override (`st.session_state.page_order`) if one exists, else
    natural physical order -- the same convention segment_pdf's own
    `page_sequence=None` default already uses."""
    order = st.session_state.page_order
    return list(order) if order else list(range(page_count))


def _set_page_order(pdf_path: str, decisions_dir: str, sequence: list[int], page_count: int) -> None:
    """Record a corrected page processing order and persist it immediately
    -- the composition-editing sibling of `_set_page_rotation`. Every
    downstream cache (`_segment`, `_proposals`, `_consensus`, `_round_
    data`) is keyed on `page_order`, so mutating it and rerunning naturally
    re-segments with the new order, the same way rotating a page
    invalidates those same caches. Normalizes back to `None` (clearing the
    sidecar) when `sequence` turns out to equal natural physical order --
    e.g. after undoing every edit -- so an unused sidecar never lingers."""
    normalized = None if sequence == list(range(page_count)) else sequence
    st.session_state.page_order = normalized
    save_page_order(pdf_path, normalized, decisions_dir=Path(decisions_dir))


def _sequence_move_within(sequence: list[int], page_idx: int, delta: int) -> list[int]:
    """Swap `page_idx` with its sequence-adjacent neighbor `delta` positions
    away -- the primitive behind the page composition editor's Up/Down
    controls. A no-op (returns `sequence` unchanged) at either end, so
    callers don't need their own bounds-checking beyond disabling the
    button."""
    pos = sequence.index(page_idx)
    new_pos = pos + delta
    if not (0 <= new_pos < len(sequence)):
        return sequence
    sequence = list(sequence)
    sequence[pos], sequence[new_pos] = sequence[new_pos], sequence[pos]
    return sequence


def _sequence_remove(sequence: list[int], page_idx: int) -> list[int]:
    """Drop `page_idx` out of the processing order entirely -- it becomes
    unassigned to any packet (see segment_pdf's own `page_sequence`
    docstring), not deleted from the file. The composition editor's own
    "removed pages" list (see `_render_page_composition_editor`) is what
    lets a reviewer put it back later."""
    return [p for p in sequence if p != page_idx]


def _sequence_insert_before(sequence: list[int], page_idx: int, before_page_idx: int | None) -> list[int]:
    """Move (or re-insert, if `page_idx` was previously removed) `page_idx`
    to sit immediately before `before_page_idx` in the processing order --
    `None` means "at the very start". The shared primitive behind "move to
    previous packet" (insert before that packet's own first page) and
    re-inserting a removed page."""
    sequence = [p for p in sequence if p != page_idx]
    pos = sequence.index(before_page_idx) if before_page_idx is not None else 0
    sequence.insert(pos, page_idx)
    return sequence


def _sequence_insert_after(sequence: list[int], page_idx: int, after_page_idx: int | None) -> list[int]:
    """Mirror of `_sequence_insert_before`: moves/inserts `page_idx` to sit
    immediately after `after_page_idx` -- `None` means "at the very end".
    Backs "move to next packet" (insert after that packet's own last
    page)."""
    sequence = [p for p in sequence if p != page_idx]
    pos = sequence.index(after_page_idx) + 1 if after_page_idx is not None else len(sequence)
    sequence.insert(pos, page_idx)
    return sequence


def _confirm(pdf_path: str, decisions_dir: str, tag: str, sid: str | None) -> None:
    st.session_state.decisions[tag] = sid
    save_decisions(pdf_path, st.session_state.decisions, decisions_dir=Path(decisions_dir))
    st.toast(f"Saved decision for {tag}")


def _set_detection_override(pdf_path: str, decisions_dir: str, tag: str, approved: bool) -> None:
    """Records (or revokes) a human's explicit approval to release `tag`
    from *only* the detection-confidence hold -- see pipeline.py's
    `detection_overrides` and CLAUDE.md's "One of these five holds is
    human-overridable" section. This is deliberately a separate action from
    `_confirm`: confirming a SID match answers "who is this", not "I've
    looked at the fallback box and it covers the name" -- conflating the
    two would mean every ordinary approval silently carried this override
    too, for packets where it was never actually reviewed."""
    if approved:
        st.session_state.detection_overrides.add(tag)
    else:
        st.session_state.detection_overrides.discard(tag)
    save_detection_overrides(pdf_path, st.session_state.detection_overrides, decisions_dir=Path(decisions_dir))


def _set_composition_override(pdf_path: str, decisions_dir: str, tag: str, approved: bool) -> None:
    """Records (or revokes) a human's explicit confirmation that `tag`'s
    page composition (page count/order) is correct despite an unresolved
    footer/sequence issue -- see pipeline.py's `composition_overrides` and
    its own docstring for exactly which issue types this can and can't
    release. Same "separate action from _confirm" reasoning as `_set_
    detection_override`: confirming a SID match answers "who is this", not
    "I've looked at the actual pages and the count/order is right"."""
    if approved:
        st.session_state.composition_overrides.add(tag)
    else:
        st.session_state.composition_overrides.discard(tag)
    save_composition_overrides(pdf_path, st.session_state.composition_overrides, decisions_dir=Path(decisions_dir))


def _render_sidebar(
    args: argparse.Namespace,
    segmented: SegmentResult,
    roster: Roster,
    resolved_block: BlockMeaning | None = None,
    round_labels: dict[str, str] | None = None,
    consensus_holds: dict[str, list] | None = None,
) -> None:
    decisions = st.session_state.decisions
    n_pending = sum(1 for p in segmented.packets if packet_tag(args.pdf_path, p) not in decisions)
    n_consented = sum(1 for sid in decisions.values() if sid is not None)
    n_rejected = sum(1 for sid in decisions.values() if sid is None)

    st.sidebar.header("MEL MPR+ADR review")
    st.sidebar.text(f"Scan: {Path(args.pdf_path).name}")
    period_note = f", period {roster.entries[0].period_display}" if roster.entries else ""
    st.sidebar.text(f"Roster: {len(roster)} students{period_note}")
    if resolved_block is not None:
        st.sidebar.text(f"Block: {resolved_block.describe()}")
    st.sidebar.metric("Packets", len(segmented.packets))
    st.sidebar.write(f"⏳ Pending: {n_pending}  ✅ Approved: {n_consented}  🚫 Rejected: {n_rejected}")

    if round_labels:
        # See pipeline.duplicate_decisions_within_round's own docstring for
        # why this exists: round-scoped claim-and-remove only ever guards
        # *automatic* assignment, so nothing stops a human from confirming
        # the same SID for two different packets in the same round -- a
        # real, repeated finding on the actual 010406 output tree. Purely
        # informational, never held or blocked; a human has to look at the
        # actual pages to know which (if either) confirmation is wrong.
        duplicates = duplicate_decisions_within_round(decisions, round_labels)
        for (label, sid), dup_tags in sorted(duplicates.items()):
            st.sidebar.warning(f"⚠️ SID {sid} decided for {len(dup_tags)} packets in round {label}: {dup_tags}")

    st.sidebar.divider()
    disable_deletion = st.sidebar.checkbox(
        "Disable deletion (safety)",
        key="disable_deletion",
        help="Suppresses every deletion 'Run redaction pipeline' would otherwise perform (confirmed "
        "non-consent, or a correction superseding an old SID), regardless of what any individual "
        "decision says. Matching, redaction, and writing new output still proceed normally -- use "
        "this for a pilot or a file that hasn't been through this code before.",
    )
    if st.sidebar.button("Run redaction pipeline", type="primary", disabled=n_pending == len(segmented.packets) == 0):
        with st.spinner("Redacting approved packets..."):
            fresh_decisions = load_decisions(args.pdf_path, decisions_dir=Path(args.decisions_dir))
            fresh_overrides = load_detection_overrides(args.pdf_path, decisions_dir=Path(args.decisions_dir))
            fresh_composition_overrides = load_composition_overrides(args.pdf_path, decisions_dir=Path(args.decisions_dir))
            fresh_manual_geometry = load_manual_geometry(args.pdf_path, decisions_dir=Path(args.decisions_dir))
            results = run_dispositions(
                args.pdf_path,
                segmented,
                fresh_decisions,
                roster,
                out_dir=Path(args.out_dir),
                detection_overrides=fresh_overrides,
                composition_overrides=fresh_composition_overrides,
                round_labels=round_labels,
                allow_delete=not disable_deletion,
                manual_geometry=fresh_manual_geometry,
                consensus_holds=consensus_holds,
                orientation_overrides=st.session_state.orientation_overrides,
            )
            written = [r for r in results if r.out_path is not None]
            deleted = sum(1 for r in results if r.deleted_path is not None)
            pending = sum(1 for r in results if r.pending)
            held_back = [r for r in results if r.held_back]
            consent_held = [r for r in results if r.consent_hold]
            overridden = [r for r in written if r.reason]
            collided = [r for r in written if r.collision_note]
            deletion_skipped = [r for r in results if r.deletion_skipped]
            n_manual = sum(1 for r in written if r.geometry_source == "manual")
            n_advisory = sum(1 for r in written if r.advisory_uncovered_words)
            st.sidebar.success(
                f"{len(written)} written ({len(collided)} collision(s) avoided), {deleted} deleted, "
                f"{len(held_back)} held back for review, {len(consent_held)} consent-held (no SID), "
                f"{pending} still pending review"
                + (f", {len(deletion_skipped)} deletion(s) skipped (disabled)" if deletion_skipped else "")
            )
            # Geometry provenance + advisory volume, per this run -- see
            # CLAUDE.md's "Make a per-run summary" section. Answers whether
            # the uncovered-ink advisory (see pipeline.py's
            # advisory_uncovered_words) is actually earning its place or
            # should be dropped entirely: if n_advisory tracks real manual
            # edits over many runs, it's doing something; if it fires on
            # nearly every write regardless, it's noise a reviewer has
            # learned to ignore, same as the old hold's false-positive rate.
            st.sidebar.write(
                f"Geometry: {len(written) - n_manual} automatic, {n_manual} manually edited -- "
                f"{n_advisory} write(s) carried an uncovered-ink advisory"
            )
            for r in collided:
                # A different packet's output already claimed this packet's
                # natural path this run -- see pipeline.py's
                # _claim_output_path. Written to a numbered-suffix path
                # instead of silently overwriting; surfaced prominently so
                # a reviewer notices and doesn't assume the natural path is
                # this packet's file.
                st.sidebar.warning(f"Collision avoided: {r.packet_tag}: {r.collision_note}")
            for r in consent_held:
                # Never a "needs fixing" item like held_back -- a permanent
                # structural state (see pipeline.py's consent_hold), shown
                # separately so it doesn't read as something actionable.
                st.sidebar.info(f"Consent hold (no SID): {r.packet_tag}: {r.reason}")
            for r in overridden:
                # Written, not held back -- but only because a human
                # explicitly released the detection-confidence hold (see
                # pipeline.py's detection_overrides). Surfaced separately
                # from a plain "written" so this isn't indistinguishable
                # from a clean, confidently-detected write.
                st.sidebar.info(f"Shipped via override: {r.packet_tag} (sid {r.sid}): {r.reason}")
            for r in held_back:
                # A held-back packet is a data/geometry problem with this
                # one packet, not the whole run -- see pipeline.py's
                # module docstring. Surface it per-packet so a reviewer
                # knows exactly which tag needs a closer look and why.
                st.sidebar.warning(f"Held back: {r.packet_tag} (sid {r.sid}): {r.reason}")
            for r in deletion_skipped:
                # "Disable deletion (safety)" was checked -- this packet's
                # decision would otherwise have deleted a file. Surfaced
                # explicitly so it's never mistaken for an ordinary,
                # untouched pending packet.
                st.sidebar.info(f"Deletion skipped (disabled): {r.packet_tag}: {r.reason}")

    st.sidebar.divider()
    queue_entries = [e for e in list_manual_queue(args.out_dir) if e["pdf_path"] == str(Path(args.pdf_path))]
    st.sidebar.write(f"🛠️ Manual-redaction queue: {len(queue_entries)}")
    st.sidebar.checkbox("Show manual redaction queue", key="show_manual_queue", disabled=not queue_entries)


def _render_field_table(fields, round_label_text: str | None = None) -> None:
    fields_col = ["Name (used for matching)", "Teacher", "Group members (context only)", "Date", "Period"]
    values_col = [fields.name_text, fields.teacher_text, fields.group_text, fields.date_text, fields.period_text]
    if round_label_text is not None:
        # Shown directly below Date, side by side with the raw OCR'd text
        # that produced it -- so a reviewer approving a name can see which
        # collection round (not just which raw date) they're approving it
        # into. This is the *group's* label (see blocks.group_into_rounds),
        # not necessarily what this one packet's own date parses to.
        fields_col.append("Round (assigned)")
        values_col.append(round_label_text)
    st.table({"Field": fields_col, "OCR'd text": values_col})


def _render_candidates(proposal, top5, roster: Roster, auto_assignments: dict[str, str | None], tag: str):
    if not top5:
        st.info("No candidates -- this packet has no header page to score.")
        return
    rows = []
    for c in top5:
        entry = roster.by_sid[c.sid]
        clears_bar = c.score >= MIN_SCORE and proposal.margin >= MIN_MARGIN
        would_auto_assign = auto_assignments.get(tag) == c.sid
        rows.append(
            {
                "SID": c.sid,
                "Name": entry.full_name,
                "Score": round(c.score, 1),
                "Clears auto-assign bar": "yes" if clears_bar else "no",
                "Actually auto-assigned": "yes" if would_auto_assign else "",
            }
        )
    st.table(rows)


def _stamp_lines_for(sid: str | None, roster: Roster) -> list[str] | None:
    if sid is None:
        return None
    entry = roster.by_sid[sid]
    return [f"SID: {entry.sid}", f"PD: {entry.period_display}"]


def _bbox_to_canvas_rect(bbox: Bbox, dpi: int, *, interactive: bool = True) -> dict:
    """Page-point Bbox -> a fabric.js rect object at canvas (image) pixel
    scale, for `st_canvas`'s `initial_drawing` -- seeds the canvas with
    whatever geometry (automatic detection, or a previously-drawn region)
    already exists for this page, so the common case is nudging an
    existing box rather than drawing from nothing.

    `interactive` bakes `selectable`/`evented`/`hasControls`/`hasBorders`
    directly into the object's own JSON, rather than leaving them at
    fabric.js's class defaults and relying on the canvas component's own
    `configureCanvas` pass to set them correctly after `initial_drawing`
    loads. That pass runs synchronously at mount, before the async
    `loadFromJSON` callback that actually populates the canvas with these
    objects has necessarily completed -- so on a fresh load its
    `forEachObject` sweep can run over an empty canvas and never touch the
    objects this function built at all, leaving them at whatever fabric.js
    defaults `loadFromJSON` fills in instead. This was the root cause
    behind three symptoms that looked separate but shared one mechanism:
    an existing box being draggable while "Draw a new box" mode was
    supposed to have locked it (which could intercept a click meant to
    start a *second* new box), and resize controls not reliably appearing
    on a freshly-loaded box. Baking the flag into the object itself removes
    the dependency on that pass's timing entirely -- every caller here
    passes `interactive=True` only while in "Move / resize" mode and
    `False` while in "Draw a new box" mode (see _render_manual_editor)."""
    scale = dpi / 72.0
    left, top, right, bottom = bbox
    return {
        "type": "rect",
        "left": left * scale,
        "top": top * scale,
        "width": max(1.0, (right - left) * scale),
        "height": max(1.0, (bottom - top) * scale),
        "scaleX": 1,
        "scaleY": 1,
        "angle": 0,
        "fill": "rgba(255, 0, 0, 0.25)",
        "stroke": "red",
        "strokeWidth": 2,
        "selectable": interactive,
        "evented": interactive,
        "hasControls": interactive,
        "hasBorders": interactive,
        "lockRotation": True,
    }


def _canvas_rect_to_bbox(obj: dict, dpi: int) -> Bbox:
    """Inverse of `_bbox_to_canvas_rect`: a fabric.js rect object (after a
    reviewer has dragged/resized it, or drawn a new one) back to a
    page-point Bbox. `scaleX`/`scaleY` are fabric.js's own resize factors
    applied on top of the object's original width/height -- both must be
    folded in, not just width/height alone, or a corner-dragged resize
    would silently be ignored."""
    scale = dpi / 72.0
    left = float(obj.get("left", 0.0))
    top = float(obj.get("top", 0.0))
    width = float(obj.get("width", 0.0)) * float(obj.get("scaleX", 1.0))
    height = float(obj.get("height", 0.0)) * float(obj.get("scaleY", 1.0))
    return (left / scale, top / scale, (left + width) / scale, (top + height) / scale)


def _advisory_outline_image(image: Image.Image, words: list, dpi: int) -> Image.Image:
    """Non-destructive: outlines (never fills) each advisory word's own box
    on a copy of `image`, so a reviewer can see exactly what find_
    uncovered_group_words flagged without it blocking anything (see
    pipeline.py's `advisory_uncovered_words` -- 2026-08-14, this check no
    longer holds a packet back, see CLAUDE.md). Orange, not the redaction
    boxes' red, so the two are never visually confused."""
    if not words:
        return image
    preview = image.copy()
    draw = ImageDraw.Draw(preview)
    scale = dpi / 72.0
    for w in words:
        draw.rectangle(
            [w["x0"] * scale, w["top"] * scale, w["x1"] * scale, w["bottom"] * scale],
            outline=(255, 140, 0),
            width=3,
        )
    return preview


def _seed_manual_regions(
    args: argparse.Namespace, packet: Packet, flagged_regions: dict[str, list] | None = None
) -> dict[int, list[Bbox]]:
    """Initial regions for a freshly-opened editor session: the header
    page's own automatically-detected two rectangles (see redact_bboxes_
    for_band), so a reviewer starts from what detection already proposed
    and only has to nudge it -- never from a blank page. Other pages start
    with no regions, UNLESS this packet was queued for a consensus-ink
    anomaly (see pipeline.AnomalyHold/`flagged_regions`, persisted on the
    queue entry by `_queue_for_manual_redaction`) -- in that case the exact
    flagged bbox is pre-seeded on its own page too, so a reviewer opening
    the editor sees a box already sitting on the anomalous ink rather than
    having to hunt across the packet's pages for it. `flagged_regions` uses
    JSON string offset keys (see pipeline._serialize style), normalized to
    int here."""
    regions: dict[int, list[Bbox]] = {}
    if packet.header_page_index is not None:
        header_offset = packet.page_indices.index(packet.header_page_index)
        raw_image = _page_image(args.pdf_path, packet.header_page_index, DPI, st.session_state.orientation_overrides)
        fields = _header_fields(args.pdf_path, packet.header_page_index, st.session_state.orientation_overrides)
        band = detect_header_band(
            raw_image, dpi=DPI, anchors=fields.anchors, row_height=header_row_height(fields.anchors)
        )
        regions[header_offset] = list(redact_bboxes_for_band(band, fields.anchors.group_top))
    for offset_key, bboxes in (flagged_regions or {}).items():
        offset = int(offset_key)
        regions.setdefault(offset, [])
        regions[offset] = regions[offset] + [tuple(b) for b in bboxes]
    return regions


def _render_manual_editor(
    args: argparse.Namespace,
    roster: Roster,
    packet: Packet,
    tag: str,
    *,
    default_sid: str | None = None,
    flagged_regions: dict[str, list] | None = None,
) -> None:
    """The drag-corner redaction editor, reachable for ANY packet -- not
    just one the automated checks held back (see CLAUDE.md's "From
    detection-gates-workflow to human-reviews-everything" section: every
    new leak variant used to get its own automatic detector; this editor is
    the general answer instead, since a human already looks at every
    packet before anything ships). `_render_packet`'s own "Edit redaction"
    expander opens this with `flagged_regions=None` for an ordinary packet;
    `_render_manual_queue` opens it with the queue entry's own
    `flagged_regions` for a packet an automated check actually held.

    Original page on the left, live redacted result on the right -- both
    rendered at an identical, fixed pixel width computed per page (see
    `_editor_dpi_for_page`/config.MANUAL_EDITOR_TARGET_WIDTH_PX), and the
    preview pane's `st.image` call is given that same width explicitly, so
    neither pane is ever left to container-relative CSS sizing that could
    make the two drift apart (found 2026-08-15: the canvas renders at a
    literal pixel size regardless of its container, while a plain
    `st.image` with no explicit width silently shrinks to fit a narrower
    container -- two views of the same page at two different effective
    sizes is what read as "misaligned, shifts between renders"). A
    reviewer drags the corners of the seeded rectangles (see `_seed_
    manual_regions`, which seeds the header page's own automatically-
    detected boxes so the common case is nudging what detection already
    proposed, not drawing from nothing) or draws new ones on any page of
    the packet -- the page selector below acts as a tab strip, one page at
    a time, each with its own independent rectangles, so a stray name on
    page 3 is exactly as reachable as page 1's header.

    The redacted preview only regenerates on an explicit "Update preview"
    click (plus once automatically, the first time a page is opened) --
    see the comment above `preview_state_key` below for why: st_canvas
    debounces its own sync back to Streamlit at ~200ms *during* an active
    drag/resize, and recomputing the redaction geometry inline on every one
    of those reruns was slow enough to read as a drag that "sometimes
    doesn't take."

    find_uncovered_group_words' own finding is shown as an orange advisory
    outline on the header page (see `_advisory_outline_image`) -- it no
    longer blocks anything (2026-08-14, see CLAUDE.md), so drawing over it
    is optional, but the editor still points a reviewer's eyes at it.

    Applying always goes through `pipeline.release_from_manual_queue`
    (regardless of whether this packet was ever queued -- it's a no-op to
    clear a queue entry that doesn't exist), which re-runs the real
    redaction and the checks that still gate a write (consensus-ink
    coverage when `flagged_regions` is set, verify_no_leaked_names) against
    exactly this geometry. The decision itself is recorded via `_confirm`
    right alongside the release, whether or not the release actually
    succeeds -- this is what keeps "present in out/" iff "has a confirmed
    decision" true (see pipeline.py's module docstring) even for a packet
    that was never separately run through "Confirm decision" first; editing
    and applying a redaction IS the review decision for that packet. A
    reviewer resolves the student by typing their NAME (see
    `filter_roster_by_name`) -- there is no field anywhere in this editor
    that accepts a SID directly, since a mistyped digit would be a silently
    wrong assignment nothing downstream could catch."""
    regions_key = f"mq_regions_{tag}"
    if regions_key not in st.session_state:
        st.session_state[regions_key] = _seed_manual_regions(args, packet, flagged_regions)
    regions: dict[int, list[Bbox]] = st.session_state[regions_key]

    header_offset = packet.page_indices.index(packet.header_page_index) if packet.header_page_index is not None else None
    flagged_offsets = {int(k) for k in (flagged_regions or {})}
    page_options = list(range(packet.n_pages))
    default_page = min(flagged_offsets) if flagged_offsets else (header_offset if header_offset is not None else 0)
    page_offset = st.selectbox(
        "Editing page",
        options=page_options,
        format_func=lambda i: f"Page {i + 1} of {packet.n_pages}" + (" (flagged ink here)" if i in flagged_offsets else ""),
        index=default_page,
        key=f"mq_pageselect_{tag}",
    )
    if page_offset in flagged_offsets:
        st.warning(
            "This page has consensus-ink anomaly ink flagged (see the pre-drawn region below) -- "
            "template-agnostic handwriting detection found ink here that only a few packets sharing "
            "this worksheet page have, not printed content and not a common answer field. Confirm the "
            "seeded region actually covers it (resize if needed) before applying."
        )

    current_boxes = regions.get(page_offset, [])
    page_idx = packet.page_indices[page_offset]

    # A page-derived DPI, not the module-level DPI constant every other
    # preview in this file uses -- see config.MANUAL_EDITOR_TARGET_
    # WIDTH_PX's own docstring. Both panes below render at this exact
    # pixel width, with an explicit `width=` on the plain-image pane too,
    # so neither is ever left to container-relative CSS sizing that could
    # drift between the two or between reruns.
    page_w_pt, _page_h_pt = _page_size_pt(args.pdf_path, page_idx, st.session_state.orientation_overrides)
    editor_dpi = _editor_dpi_for_page(page_w_pt)

    render_start = time.perf_counter()
    raw_image = _page_image(args.pdf_path, page_idx, editor_dpi, st.session_state.orientation_overrides)
    render_elapsed_ms = (time.perf_counter() - render_start) * 1000
    pane_width = raw_image.width

    mode_label = st.radio(
        "Tool",
        ["Move / resize existing boxes", "Draw a new box"],
        key=f"mq_mode_{tag}_{page_offset}",
        horizontal=True,
        help="'Draw a new box' adds one rectangle per click-and-drag -- switch tools again and draw "
        "another to add a second, third, etc. 'Move / resize' drags a box's body to move it, drags a "
        "corner or edge handle to resize it, and supports the canvas's own multi-select (shift-click, "
        "or drag a rubber-band over several boxes) to move or resize more than one at once.",
    )
    if st.button("Clear boxes on this page", key=f"mq_clear_{tag}_{page_offset}"):
        regions[page_offset] = []
        current_boxes = []

    interactive = mode_label.startswith("Move")
    col_canvas, col_preview = st.columns(2)
    with col_canvas:
        st.caption(f"Original — page {page_offset + 1}")
        canvas_result = st_canvas(
            fill_color="rgba(255, 0, 0, 0.25)",
            stroke_width=2,
            stroke_color="red",
            background_image=raw_image,
            update_streamlit=True,
            height=raw_image.height,
            width=raw_image.width,
            drawing_mode="transform" if interactive else "rect",
            initial_drawing={
                "version": "4.4.0",
                "objects": [_bbox_to_canvas_rect(b, editor_dpi, interactive=interactive) for b in current_boxes],
            },
            # Stable across every rerun this function can cause on its own
            # (page/tool/delete/preview-button interactions never change
            # tag or page_offset) -- the component is never remounted by
            # anything this editor itself does, so in-progress canvas
            # state (fabric.js's own selection/undo history) survives.
            key=f"mq_canvas_{tag}_{page_offset}",
        )
        st.caption(
            f"Original pane rendered in {render_elapsed_ms:.0f} ms"
            + (" (cached)" if render_elapsed_ms < 50 else "")
            + f" · {pane_width}×{raw_image.height}px"
        )
    if canvas_result.json_data is not None:
        objs = [o for o in canvas_result.json_data.get("objects", []) if o.get("type") == "rect"]
        current_boxes = [_canvas_rect_to_bbox(o, editor_dpi) for o in objs]
        regions[page_offset] = current_boxes

    if current_boxes:
        # A reliable, Python-driven delete, independent of whatever the
        # canvas component's own delete gesture (double-click an object in
        # "Move / resize" mode) does or doesn't do in a given browser --
        # deleting one rectangle out of several is exactly the operation a
        # gesture-only affordance is easiest to get wrong.
        del_col1, del_col2 = st.columns([3, 1])
        del_index = del_col1.selectbox(
            "Rectangle to delete",
            options=list(range(len(current_boxes))),
            format_func=lambda i: f"Box {i + 1}: " + ", ".join(f"{v:.0f}" for v in current_boxes[i]),
            key=f"mq_delidx_{tag}_{page_offset}",
        )
        if del_col2.button("Delete", key=f"mq_delbtn_{tag}_{page_offset}"):
            del current_boxes[del_index]
            regions[page_offset] = current_boxes
            st.rerun()

    header_bbox_override: tuple[Bbox, Bbox] | None = None
    if page_offset == header_offset and len(current_boxes) >= 2:
        header_bbox_override = (current_boxes[0], current_boxes[1])
    elif page_offset == header_offset and len(current_boxes) == 1:
        header_bbox_override = (current_boxes[0], current_boxes[0])

    advisory_words: list = []
    if page_offset == header_offset and header_bbox_override is not None:
        header_fields = _header_fields(args.pdf_path, page_idx, st.session_state.orientation_overrides)
        header_words = _header_words(args.pdf_path, page_idx, st.session_state.orientation_overrides)
        advisory_words = find_uncovered_group_words(
            header_words, header_fields.anchors, header_bbox_override[0], header_bbox_override[1]
        )

    # The redacted preview is regenerated only on an explicit click, not on
    # every rerun this editor causes (a box drag/resize, a tool switch, the
    # delete control) -- found 2026-08-15: st_canvas syncs its state back to
    # Streamlit on a ~200ms debounce *during* an active drag/resize, each
    # sync triggering a full script rerun; recomputing the redaction preview
    # (detect_header_band's raster scan, find_uncovered_group_words) inline
    # on every one of those reruns is expensive enough that it could still
    # be running when the next debounced sync lands mid-gesture, which reads
    # to a reviewer as a drag that "sometimes doesn't take." Gating it here
    # means every mid-drag rerun only has to re-render this already-disk-
    # cached page image and rebuild `initial_drawing`, not redo the
    # redaction geometry, drawing, and advisory check too. A first view of a
    # page still gets an automatic preview (`is_first_view`) so a reviewer
    # isn't staring at nothing before drawing anything.
    preview_state_key = f"mq_preview_state_{tag}"
    if preview_state_key not in st.session_state:
        st.session_state[preview_state_key] = {}
    preview_cache = st.session_state[preview_state_key]
    boxes_signature = tuple(current_boxes)
    cached = preview_cache.get(page_offset)
    is_first_view = cached is None

    with col_preview:
        st.caption("Redacted result")
        update_clicked = st.button("🔄 Update preview", key=f"mq_updatepreview_{tag}_{page_offset}")
        if is_first_view or update_clicked:
            preview_start = time.perf_counter()
            stamp_lines = _stamp_lines_for(default_sid, roster) if default_sid in roster else None
            if page_offset == header_offset:
                if header_bbox_override is None:
                    preview_image, preview_caption = raw_image, "No regions drawn yet on the header page"
                else:
                    preview_image, _band = render_redaction_preview(
                        raw_image, dpi=editor_dpi, stamp_lines=stamp_lines, header_bbox_override=header_bbox_override
                    )
                    preview_caption = "Preview with your drawn regions"
            else:
                preview_image = render_region_preview(raw_image, dpi=editor_dpi, bboxes=current_boxes)
                preview_caption = "Preview with your drawn regions" if current_boxes else "No regions drawn on this page"
            preview_elapsed_ms = (time.perf_counter() - preview_start) * 1000
            preview_cache[page_offset] = {
                "boxes": boxes_signature,
                "image": preview_image,
                "caption": preview_caption,
                "elapsed_ms": preview_elapsed_ms,
            }
            cached = preview_cache[page_offset]
        st.image(cached["image"], caption=cached["caption"], width=pane_width)
        st.caption(
            f"Redacted pane rendered in {cached['elapsed_ms']:.0f} ms"
            + (" (cached)" if not (is_first_view or update_clicked) else "")
            + f" · {pane_width}×{raw_image.height}px"
        )
        if cached["boxes"] != boxes_signature:
            st.info(
                "This preview reflects an earlier set of regions -- click 'Update preview' to refresh it. "
                "Applying always uses your current regions regardless of what the preview shows."
            )

    if advisory_words:
        st.warning(
            f"Advisory (not blocking): find_uncovered_group_words flagged {len(advisory_words)} word(s) "
            "near the Group row your drawn boxes don't cover -- outlined in orange below. Real-data "
            "evidence says this is usually printed body text near the header border, not missed "
            "handwriting (see CLAUDE.md), so it no longer holds the packet -- but take a look before "
            "applying."
        )
        st.image(
            _advisory_outline_image(raw_image, advisory_words, editor_dpi),
            caption="Advisory: possible uncovered ink (orange outline, not blocking)",
            width=pane_width,
        )

    st.write("Resolve the student by name — never by typing a SID directly:")
    default_query = roster.by_sid[default_sid].full_name if default_sid in roster else ""
    name_query = st.text_input("Student name", value=default_query, key=f"mq_name_{tag}")
    matches = filter_roster_by_name(roster, name_query)
    resolved_sid: str | None = None
    if not name_query.strip():
        st.info("Type a student name to search the roster.")
    elif not matches:
        st.warning("No roster entry matches this name.")
    else:
        resolved_sid = st.selectbox(
            "Matching roster entries",
            options=[e.sid for e in matches],
            format_func=lambda s: _decision_label(s, roster),
            key=f"mq_sidselect_{tag}",
        )

    st.write("Worksheet details (pre-filled from detection, editable):")
    c1, c2 = st.columns(2)
    worksheet_type_value = c1.text_input("Worksheet type", value=packet.worksheet_type or "", key=f"mq_wtype_{tag}")
    period_value = roster.by_sid[resolved_sid].period_display if resolved_sid in roster else ""
    c2.text_input("Period (derived from resolved SID)", value=period_value, disabled=True, key=f"mq_period_{tag}")

    apply_disabled = resolved_sid is None or not worksheet_type_value.strip()
    if st.button("Apply manual redaction", type="primary", key=f"mq_apply_{tag}", disabled=apply_disabled):
        final_header_bbox_override: tuple[Bbox, Bbox] | None = None
        extra_page_regions: dict[int, list[Bbox]] = {}
        for offset, boxes in regions.items():
            if not boxes:
                continue
            if offset == header_offset:
                final_header_bbox_override = (boxes[0], boxes[1]) if len(boxes) >= 2 else (boxes[0], boxes[0])
            else:
                extra_page_regions[offset] = boxes

        packet_for_release = packet
        if worksheet_type_value.strip() != (packet.worksheet_type or ""):
            packet_for_release = dataclasses.replace(packet, worksheet_type=worksheet_type_value.strip())

        # Drawing and applying a redaction IS the review decision for this
        # packet -- record it the same way "Confirm decision" would,
        # whether or not the release below actually succeeds, so this
        # packet is never left silently un-decided just because a reviewer
        # used the editor instead of the ordinary radio+Confirm flow.
        _confirm(args.pdf_path, args.decisions_dir, tag, resolved_sid)
        result = release_from_manual_queue(
            args.pdf_path,
            packet_for_release,
            tag,
            resolved_sid,
            roster,
            None,
            out_dir=Path(args.out_dir),
            decisions_dir=Path(args.decisions_dir),
            header_bbox_override=final_header_bbox_override,
            extra_page_regions=extra_page_regions or None,
            flagged_regions_to_verify=flagged_regions,
            orientation_overrides=st.session_state.orientation_overrides,
            page_sequence=st.session_state.page_order,
        )
        if result.released:
            st.success(f"Released {tag} -> {result.out_path}")
            if result.advisory_uncovered_words:
                st.info(f"Shipped with {len(result.advisory_uncovered_words)} advisory uncovered-ink region(s) noted.")
            del st.session_state[regions_key]
            st.rerun()
        else:
            st.error(f"Still not safe to release with these regions: {result.reason}")


def _render_manual_queue(args: argparse.Namespace, roster: Roster, packet_by_tag: dict[str, Packet]) -> None:
    """The backstop for a genuine detection-confidence, uncovered-ink, or
    consensus-ink-anomaly miss (see CLAUDE.md's "the manual-redaction queue
    is a backstop" section) -- never a substitute for the automated checks
    catching it in the first place. Each queued entry shows the drafted
    (not-safe-to-ship) attempt exactly as it was held back, then opens
    `_render_manual_editor` for drawing a corrected region directly --
    applying always goes through `release_from_manual_queue`'s own re-check
    of every automated check (uncovered_group_words, the consensus-ink
    coverage re-check when this entry carries `flagged_regions`,
    verify_no_leaked_names); a wrong correction stays queued, nothing is
    written."""
    entries = [e for e in list_manual_queue(args.out_dir) if e["pdf_path"] == str(Path(args.pdf_path))]
    if not entries:
        st.info("Manual redaction queue is empty for this scan.")
        return

    for entry in entries:
        tag = entry["packet_tag"]
        sid = entry["sid"]
        packet = packet_by_tag.get(tag)
        label = f"{tag} — sid {sid} ({_decision_label(sid, roster) if sid in roster else 'not on roster'})"
        with st.expander(f"{label}: {entry['reason']}", expanded=False):
            if packet is None:
                st.warning("This queued packet no longer matches any packet in this scan (re-segmented differently?).")
                continue

            draft_path = manual_queue_draft_path(args.out_dir, args.pdf_path, tag)
            if draft_path.exists():
                with open_pdf(draft_path) as pdf:
                    draft_image = pdf.pages[0].to_image(resolution=DPI).original.convert("RGB")
                st.image(draft_image, caption="Drafted redaction attempt that was held back -- not safe to ship as is")

            _render_manual_editor(
                args, roster, packet, tag, default_sid=sid, flagged_regions=entry.get("flagged_regions")
            )


def _render_page_stack(args: argparse.Namespace, packet: Packet, tag: str) -> None:
    """Every page of this packet, stacked vertically in packet order, each
    labelled with its position within the packet ("Page 2 of 2") alongside
    the packet's own tag -- so a reviewer can see which page of which
    packet needs a rotation fix without cross-referencing anything else on
    screen. A page the orientation detector is unsure about (a confident
    but unconfirmed nonzero rotation guess, or a classification too weak
    to guess from at all -- see orientation.py's detect-and-ask design)
    gets a visible warning banner, so the reviewer's eye goes there first
    rather than having to scan every page looking for a problem.

    Rotating a page here always wins over the detector's own guess (see
    orientation.resolve_pages) and reaches segmentation, OCR, matching,
    and redaction identically, not just this preview -- once *applied*
    (see the Apply button below), every one of those goes through the same
    `st.session_state.orientation_overrides` dict via `open_pdf`'s own
    `orientation_overrides` parameter, and changing that dict naturally
    invalidates every in-memory cache entry that depended on the old
    orientation (see `_set_page_rotation`).

    **Left/Right/180 only ever move an uncommitted preview, never the real
    orientation.** The displayed image comes from `_page_image(...,
    orientation_overrides={})` -- the page's own as-scanned rendering,
    which never needs a resave (no page anywhere is ever automatically
    rotated a nonzero amount -- see orientation.py's detect-and-ask
    design) and is therefore always already disk-cached the moment this
    page has been viewed once -- rotated in memory via `_rotate_image_
    for_display` (plain PIL, no PDF or OCR work at all) to show what the
    candidate angle would look like. A reviewer can cycle through several
    candidate angles for free; only clicking Apply pays for anything
    real, and only for this one page, not the whole file (see
    `_set_page_rotation`'s own docstring)."""
    orientation_map = _orientation_map(args.pdf_path, st.session_state.orientation_overrides)
    st.subheader(f"Pages — packet {tag}")
    for offset, page_idx in enumerate(packet.page_indices):
        po = orientation_map.get(page_idx)
        page_label = f"Page {offset + 1} of {packet.n_pages}"
        has_override = page_idx in st.session_state.orientation_overrides
        needs_attention = po is not None and not has_override and (po.needs_confirmation or not po.resolved)
        committed_angle = st.session_state.orientation_overrides.get(
            page_idx, po.applied_angle if po is not None else 0
        )
        preview_angle = _preview_angle(page_idx, committed_angle)
        is_pending = preview_angle != committed_angle
        with st.container(border=True):
            title = f"**{tag} — {page_label}**"
            if has_override:
                title += "  🔄 rotated by reviewer"
            if is_pending:
                title += "  ✏️ preview (not yet applied)"
            st.markdown(title)
            if needs_attention and po.needs_confirmation:
                st.warning(
                    f"⚠️ {page_label}: orientation detected as rotated {po.detected_angle}° "
                    f"(confidence {po.score:.2f}) but not yet confirmed by a reviewer — use the "
                    "controls below to confirm this guess or correct it before this page can be "
                    "processed."
                )
            elif needs_attention:
                st.warning(
                    f"⚠️ {page_label}: orientation could not be confidently determined "
                    f"(score {po.score:.2f}) — rotate it by hand below if you can tell which way "
                    "it should go."
                )
            base_image = _page_image(args.pdf_path, page_idx, DPI, {})
            preview_image = _rotate_image_for_display(base_image, preview_angle)
            st.image(preview_image, width=360)

            rot_cols = st.columns(5)
            if rot_cols[0].button("⟲ Left 90°", key=f"rot_l_{tag}_{page_idx}"):
                _set_pending_rotation(page_idx, preview_angle - 90)
                st.rerun()
            if rot_cols[1].button("↻ Right 90°", key=f"rot_r_{tag}_{page_idx}"):
                _set_pending_rotation(page_idx, preview_angle + 90)
                st.rerun()
            if rot_cols[2].button("⟳ 180°", key=f"rot_180_{tag}_{page_idx}"):
                _set_pending_rotation(page_idx, preview_angle + 180)
                st.rerun()
            if rot_cols[3].button(
                "Reset to automatic",
                key=f"rot_reset_{tag}_{page_idx}",
                disabled=not has_override and not is_pending,
            ):
                _set_page_rotation(args.pdf_path, args.decisions_dir, page_idx, None)
                st.rerun()
            if rot_cols[4].button(
                "✅ Apply rotation",
                key=f"rot_apply_{tag}_{page_idx}",
                disabled=not is_pending,
                type="primary" if is_pending else "secondary",
            ):
                _set_page_rotation(args.pdf_path, args.decisions_dir, page_idx, preview_angle)
                st.rerun()

            if is_pending:
                st.caption(f"Previewing {preview_angle}° from as-scanned — click Apply to use this for real processing.")
            elif has_override:
                st.caption(f"Reviewer-confirmed rotation: {committed_angle}° from as-scanned.")
            elif po is not None and po.source == "auto" and po.applied_angle == 0:
                st.caption("Orientation: upright (automatic).")


def _render_page_composition_editor(
    args: argparse.Namespace,
    segmented: SegmentResult,
    packet: Packet,
    tag: str,
    tags: list[str],
    packet_by_tag: dict[str, Packet],
) -> None:
    """A reviewer's tool for the scanner's other page-order defect (see
    orientation.py's own detect-and-ask design for the rotation one):
    segment_pdf assumes physical scan order is document order (see its own
    module docstring), and a scanner that fed pages out of order breaks
    that assumption in a way no automatic check can safely repair on its
    own -- reordering, removing, or moving a page between packets is
    exactly the kind of structural correction that has to be a human
    decision, confirmed here, never guessed.

    Shows this packet's own pages (physical index + raw footer text, for
    context) with Up/Down (swap with the sequence-adjacent page within
    this same packet), Remove (the page becomes unassigned to any packet,
    not deleted from the file -- see `_sequence_remove`'s own docstring),
    and, on the packet's first/last page only, a move-to-adjacent-packet
    shortcut. Every control computes a new full page processing order and
    calls `_set_page_order`, which persists it and is picked up on rerun
    by every cache keyed on `page_order` (`_segment`, `_proposals`, ...) --
    the identical "mutate the override, rerun, let the cache keys do the
    invalidation" mechanism `_set_page_rotation` already uses for rotation.

    `find_reversed_continuation_header_pairs` (see segment.py) is checked
    first and, when it names a page in *this* packet, shown as a one-click
    proposal rather than requiring the general controls below -- but it's
    exactly that: a proposal a reviewer confirms by clicking, never applied
    on its own.

    Applying any edit changes which physical page is this packet's own
    first page in processing order, which can change `packet_tag` itself
    (see pipeline.packet_tag -- based on `page_indices[0]`) -- a decision
    already recorded under the *old* tag for an affected packet won't
    follow it automatically. This is surfaced as a caption, not silently
    hidden, since the packets this editor exists for are exactly the ones
    that were too broken to have a trustworthy decision recorded yet.
    """
    sequence = _current_page_sequence(segmented.page_count)

    suggestions = find_reversed_continuation_header_pairs(segmented)
    relevant = [
        s for s in suggestions if s.continuation_page_index in packet.page_indices or s.header_page_index in packet.page_indices
    ]
    for s in relevant:
        st.warning(
            f"Pages {s.continuation_page_index} and {s.header_page_index} look like a reversed "
            f"continuation/header pair -- both declare a {s.declared_total}-page packet, consistent "
            "with being one packet if simply read in the other order."
        )
        if st.button(
            f"Apply suggested fix: process page {s.header_page_index} immediately before page "
            f"{s.continuation_page_index}",
            key=f"pc_apply_reversal_{tag}_{s.continuation_page_index}",
        ):
            new_sequence = _sequence_insert_before(sequence, s.header_page_index, s.continuation_page_index)
            _set_page_order(args.pdf_path, args.decisions_dir, new_sequence, segmented.page_count)
            st.rerun()

    idx_in_tags = tags.index(tag)
    prev_packet = packet_by_tag[tags[idx_in_tags - 1]] if idx_in_tags > 0 else None
    next_packet = packet_by_tag[tags[idx_in_tags + 1]] if idx_in_tags + 1 < len(tags) else None

    st.caption(
        "Reordering, removing, or moving a page can change this packet's own identity (packet_tag is "
        "based on its first page in processing order) -- an existing decision for the old tag won't "
        "follow automatically; re-review affected packets after applying an edit here."
    )

    for i, page_idx in enumerate(packet.page_indices):
        cols = st.columns([2, 1, 1, 2, 3])
        cols[0].write(f"Physical page {page_idx}")
        if cols[1].button("↑", key=f"pc_up_{tag}_{page_idx}", disabled=i == 0, help="Move up within this packet"):
            new_sequence = _sequence_move_within(sequence, page_idx, -1)
            _set_page_order(args.pdf_path, args.decisions_dir, new_sequence, segmented.page_count)
            st.rerun()
        if cols[2].button(
            "↓", key=f"pc_down_{tag}_{page_idx}", disabled=i == len(packet.page_indices) - 1, help="Move down within this packet"
        ):
            new_sequence = _sequence_move_within(sequence, page_idx, 1)
            _set_page_order(args.pdf_path, args.decisions_dir, new_sequence, segmented.page_count)
            st.rerun()
        if cols[3].button("✕ Remove from packet", key=f"pc_remove_{tag}_{page_idx}"):
            new_sequence = _sequence_remove(sequence, page_idx)
            _set_page_order(args.pdf_path, args.decisions_dir, new_sequence, segmented.page_count)
            st.rerun()
        if i == 0 and prev_packet is not None:
            if cols[4].button("← Move to previous packet", key=f"pc_prev_{tag}_{page_idx}"):
                new_sequence = _sequence_insert_after(sequence, page_idx, prev_packet.page_indices[-1])
                _set_page_order(args.pdf_path, args.decisions_dir, new_sequence, segmented.page_count)
                st.rerun()
        elif i == len(packet.page_indices) - 1 and next_packet is not None:
            if cols[4].button("Move to next packet →", key=f"pc_next_{tag}_{page_idx}"):
                new_sequence = _sequence_insert_before(sequence, page_idx, next_packet.page_indices[0])
                _set_page_order(args.pdf_path, args.decisions_dir, new_sequence, segmented.page_count)
                st.rerun()

    removed = [p for p in range(segmented.page_count) if p not in sequence]
    if removed:
        st.write(f"Removed (currently unassigned to any packet): {removed}")
        pick = st.selectbox("Re-insert a removed page", options=removed, key=f"pc_reinsert_pick_{tag}")
        insert_col1, insert_col2 = st.columns(2)
        if insert_col1.button("Insert before this packet", key=f"pc_reinsert_before_{tag}"):
            new_sequence = _sequence_insert_before(sequence, pick, packet.page_indices[0])
            _set_page_order(args.pdf_path, args.decisions_dir, new_sequence, segmented.page_count)
            st.rerun()
        if insert_col2.button("Insert after this packet", key=f"pc_reinsert_after_{tag}"):
            new_sequence = _sequence_insert_after(sequence, pick, packet.page_indices[-1])
            _set_page_order(args.pdf_path, args.decisions_dir, new_sequence, segmented.page_count)
            st.rerun()

    if st.session_state.page_order is not None:
        if st.button("Reset entire file to natural physical order", key=f"pc_reset_all_{tag}"):
            _set_page_order(args.pdf_path, args.decisions_dir, list(range(segmented.page_count)), segmented.page_count)
            st.rerun()


def _render_packet(
    args: argparse.Namespace,
    packet: Packet,
    tag: str,
    roster: Roster,
    proposal,
    auto_assignments,
    tags: list[str],
    resolved_block: BlockMeaning | None = None,
    disagreeing_tags: frozenset[str] = frozenset(),
    round_labels: dict[str, str] | None = None,
    output_round_disagreeing: frozenset[str] = frozenset(),
    segmented: SegmentResult | None = None,
    packet_by_tag: dict[str, Packet] | None = None,
) -> None:
    if resolved_block is not None:
        st.caption(f"Approving into: {resolved_block.describe()}")

    _render_page_stack(args, packet, tag)

    unconfirmable_issues = [i for i in packet.issues if not is_composition_confirmable_issue(i)]
    if packet.issues:
        # Every blocked packet must tell a reviewer the specific action
        # that clears it, not just that something's wrong (see CLAUDE.md's
        # "unreadable footers should not block a human who can see the
        # page" section) -- these hints are derived from which *kind* of
        # issue segment.py actually recorded, not a generic "resolve out
        # of band" that leaves a reviewer guessing.
        action_hints = []
        if any("orientation" in i for i in packet.issues):
            action_hints.append("rotate the affected page above to confirm or correct its orientation")
        if any(is_composition_confirmable_issue(i) for i in packet.issues):
            action_hints.append(
                "open 'Fix page composition' below to reorder/remove/move pages, or, if the page "
                "count/order actually is correct, confirm it there to release this hold"
            )
        if any("worksheet type unreadable" in i for i in packet.issues):
            action_hints.append("set the worksheet type by hand in the manual redaction editor below")
        hint_text = "; ".join(action_hints) if action_hints else "resolve the issue(s) named below"
        st.warning(
            f"This packet has unresolved segmentation issues and cannot be assigned a SID until "
            f"resolved -- {hint_text}. You may still mark it as not-on-roster to reject it.\n\n"
            + "\n".join(f"- {i}" for i in packet.issues)
        )
        if not unconfirmable_issues:
            override_checked = st.checkbox(
                "I've looked at this packet's actual pages and confirm the page count/order is "
                "correct -- release this packet from the footer/sequence hold above despite the "
                "issue(s) listed. This never releases a leak finding or an unconfirmed rotation, "
                "only a footer/page-count confidence problem.",
                value=tag in st.session_state.composition_overrides,
                key=f"composition_override_{tag}",
            )
            if override_checked != (tag in st.session_state.composition_overrides):
                _set_composition_override(args.pdf_path, args.decisions_dir, tag, override_checked)
                st.toast(f"Composition override {'granted' if override_checked else 'revoked'} for {tag}")

    if segmented is not None and packet_by_tag is not None:
        # Reachable for any packet, not just a blocked one -- a page-order
        # defect can be spotted just by looking at the page stack above,
        # before segmentation even reports an issue for it (e.g. the
        # *header* page of a reversed pair looks like an ordinary packet on
        # its own). Collapsed by default unless this packet actually has an
        # unresolved segmentation issue or a reversal suggestion names one
        # of its pages, so it doesn't compete with the common "looks right,
        # confirm" path.
        suggestions_here = [
            s
            for s in find_reversed_continuation_header_pairs(segmented)
            if s.continuation_page_index in packet.page_indices or s.header_page_index in packet.page_indices
        ]
        with st.expander("📑 Fix page composition", expanded=bool(packet.issues) or bool(suggestions_here)):
            _render_page_composition_editor(args, segmented, packet, tag, tags, packet_by_tag)

    if tag not in st.session_state.decisions and proposal.is_held_match:
        st.info(
            f"🔒 This packet's best match is a **held name**: {proposal.top_held.full_name} "
            "-- consent-known, but this student's SID couldn't be trusted in the roster export "
            f"(see `data/teacher_codes/*_holds.csv`). It will be fully redacted but never written "
            "to `out/` and never deleted; recording a decision here (a real roster SID, or "
            "confirmed non-consent) overrides this hold."
        )

    selected_sid: str | None = st.session_state.decisions.get(tag)
    if packet.header_page_index is None:
        st.info("No header page for this packet (orphan continuation page).")
        candidate_options: list[tuple[str, str | None]] = []
        has_ocrd_name = False
    else:
        fields = _header_fields(args.pdf_path, packet.header_page_index, st.session_state.orientation_overrides)

        top5 = proposal.candidates[:5]
        candidate_options = [(_decision_label(c.sid, roster), c.sid) for c in top5]
        has_ocrd_name = bool(fields.name_text and fields.name_text.strip())
        all_options_preview = _decision_options(tag, roster, candidate_options, has_ocrd_name)
        default_sid_preview = _default_decision_sid(tag, auto_assignments, has_ocrd_name)
        default_index_preview = next(
            (i for i, (_, sid) in enumerate(all_options_preview) if sid == default_sid_preview),
            len(all_options_preview) - 1,
        )
        # Streamlit persists a widget's value in session_state by key across
        # reruns, updated *before* this rerun starts whenever the user just
        # interacted with it -- so reading it here (ahead of the radio being
        # instantiated further down) reflects whatever's currently selected,
        # not last run's stale default. This is what lets the preview render
        # through the exact same redact_bbox/stamp mechanism the real
        # decision would use, live, rather than a fixed "REDACTED" stand-in
        # that could read differently from what Confirm would actually
        # produce.
        selected_label = st.session_state.get(f"decision_{tag}", all_options_preview[default_index_preview][0])
        selected_sid = dict(all_options_preview).get(selected_label, default_sid_preview)

        render_start = time.perf_counter()
        raw_image = _page_image(args.pdf_path, packet.header_page_index, DPI, st.session_state.orientation_overrides)
        render_elapsed = time.perf_counter() - render_start
        st.caption(f"Packet rendered in {render_elapsed * 1000:.0f} ms" + (" (cached)" if render_elapsed < 0.05 else ""))
        stamp_lines = _stamp_lines_for(selected_sid, roster) if selected_sid is not _NO_SELECTION else None
        preview_image, band = render_redaction_preview(raw_image, dpi=DPI, anchors=fields.anchors, stamp_lines=stamp_lines)
        if not band.detected:
            st.warning("Header border not confidently detected on this page -- redaction geometry may be unreliable.")
            override_checked = st.checkbox(
                "I've reviewed the preview below and confirm this box fully covers the name "
                "(and all other identifying handwriting) on this page -- release this packet "
                "for writing despite the detection failure.",
                value=tag in st.session_state.detection_overrides,
                key=f"override_{tag}",
            )
            if override_checked != (tag in st.session_state.detection_overrides):
                _set_detection_override(args.pdf_path, args.decisions_dir, tag, override_checked)
                st.toast(
                    f"Detection-hold override {'granted' if override_checked else 'revoked'} for {tag}"
                )

        if not has_ocrd_name and tag not in st.session_state.decisions:
            st.warning(
                "⚠️ OCR read no name at all on this page -- the Decision below has nothing pre-selected. "
                "Look at the scan and either search the roster for the right student or explicitly mark "
                "this packet as not on the roster; nothing is confirmable until you choose."
            )

        col1, col2 = st.columns(2)
        col1.image(raw_image, caption="Original scan")
        band_note = "border detected" if band.detected else "fallback band used"
        preview_caption = f"Redaction preview, reflects current selection ({band_note})"
        if selected_sid is _NO_SELECTION:
            preview_caption += " -- no decision made yet"
        elif selected_sid is None:
            preview_caption += " -- no packet would be written for 'Not on roster'"
        col2.image(preview_image, caption=preview_caption)

        _render_field_table(fields, round_labels.get(tag) if round_labels else None)
        if tag in disagreeing_tags:
            st.warning(
                f"This packet's own date ({fields.date_text!r}) disagrees with the file's resolved "
                "collection round -- shown for awareness, not held or blocked (see the block "
                "resolution banner above; students get their own written date wrong often enough "
                "that a single packet's date is a flag, not a signal to act on)."
            )
        if tag in output_round_disagreeing:
            st.warning(
                f"This packet's own date ({fields.date_text!r}) disagrees with its **output round** "
                f"group's assigned label ({round_labels.get(tag) if round_labels else '?'}) -- shown for "
                "awareness only, never held or blocked (see the round grouping report above; a single "
                "packet's own date is a flag, not a signal to act on -- the group's majority label is "
                "what actually decides this packet's output path)."
            )
        _render_candidates(proposal, top5, roster, auto_assignments, tag)

    with st.expander("Search the full roster"):
        query = st.text_input("Filter by name", key=f"search_{tag}")
        matches = filter_roster_by_name(roster, query)
        if matches:
            chosen = st.selectbox(
                "Roster entry",
                options=[e.sid for e in matches],
                format_func=lambda sid: _decision_label(sid, roster),
                key=f"search_select_{tag}",
            )
            if st.button("Use this roster entry", key=f"search_use_{tag}", disabled=bool(packet.issues)):
                # Real bug fixed here (packet 70, 2026-08-15): this used to
                # only write the decision, leaving the Decision radio below
                # unaware of the choice -- its own stale default ("Not on
                # roster", since no matcher candidate cleared MIN_SCORE)
                # would then silently overwrite this exact write the next
                # time a reviewer clicked "Confirm decision", not realizing
                # the radio disagreed with what had just been saved. Setting
                # search_added_sid *before* recomputing the shared option
                # list means the radio's own widget state (set below) is
                # guaranteed to use a label that's actually present in that
                # list next render -- see _decision_options' own docstring.
                st.session_state[_search_added_key(tag)] = chosen
                _confirm(args.pdf_path, args.decisions_dir, tag, chosen)
                fresh_options = _decision_options(tag, roster, candidate_options, has_ocrd_name)
                label_for_chosen = next(label for label, sid in fresh_options if sid == chosen)
                st.session_state[f"decision_{tag}"] = label_for_chosen
                st.rerun()

    all_options = _decision_options(tag, roster, candidate_options, has_ocrd_name)
    labels = [label for label, _ in all_options]
    default_sid = _default_decision_sid(tag, auto_assignments, has_ocrd_name)
    default_index = next((i for i, (_, sid) in enumerate(all_options) if sid == default_sid), len(all_options) - 1)

    choice_label = st.radio("Decision", options=labels, index=default_index, key=f"decision_{tag}")
    choice_sid = dict(all_options)[choice_label]
    if choice_sid is _NO_SELECTION:
        st.caption("No decision selected yet -- Confirm is disabled until you choose one.")

    # A packet with unresolved issues still blocks Confirm-with-a-SID --
    # unless every one of those issues is composition-confirmable (see
    # is_composition_confirmable_issue) AND a human has actually ticked the
    # composition-override checkbox above: that's what "makes the packet
    # assignable" (see CLAUDE.md). Rejecting as not-on-roster is always
    # allowed regardless, same as before this override existed.
    composition_released = not unconfirmable_issues and tag in st.session_state.composition_overrides
    still_blocked = bool(packet.issues) and not composition_released
    disabled = (still_blocked and choice_sid is not None) or choice_sid is _NO_SELECTION
    next_index = tags.index(tag) + 1
    has_next = next_index < len(tags)

    def _confirm_and_advance() -> None:
        # Must run as an on_click callback, not inline after a plain
        # st.button() check: `packet_select`'s own selectbox widget has
        # already been instantiated earlier in this same script run (see
        # main()), and Streamlit forbids writing to a widget's session_state
        # key after that point in the same run -- on_click callbacks run
        # *before* the next rerun starts, which is exactly what the
        # existing Prev/Next buttons' `_go` callback already relies on.
        _confirm(args.pdf_path, args.decisions_dir, tag, choice_sid)
        if has_next:
            st.session_state.packet_select = tags[next_index]

    col_confirm, col_confirm_next = st.columns(2)
    if col_confirm.button("Confirm decision", type="primary", disabled=disabled, key=f"confirm_{tag}"):
        _confirm(args.pdf_path, args.decisions_dir, tag, choice_sid)
        st.rerun()
    col_confirm_next.button(
        "Confirm & Next >",
        disabled=disabled or not has_next,
        key=f"confirm_next_{tag}",
        on_click=_confirm_and_advance,
    )

    current = st.session_state.decisions.get(tag)
    if tag in st.session_state.decisions:
        st.caption(f"Recorded: {_decision_label(current, roster)}")
    else:
        st.caption("Recorded: pending (not yet reviewed)")

    # Reachable for ANY packet, held or not -- see CLAUDE.md's "From
    # detection-gates-workflow to human-reviews-everything" section. The
    # automatic geometry (or, for a currently-queued packet, the flagged
    # region an automated check held it for) is seeded as the starting
    # rectangles, so the common case -- automatic detection already got it
    # right -- is one glance at the preview and a single Apply click, not a
    # from-scratch drawing exercise. Placed after the ordinary Decision
    # radio/Confirm buttons rather than before them so this stays an
    # advanced/fallback path a reviewer opts into, not something that
    # visually competes with the one-click "looks right, confirm" flow that
    # covers the overwhelming majority of packets.
    queued_entry = next(
        (e for e in list_manual_queue(args.out_dir) if e["pdf_path"] == str(Path(args.pdf_path)) and e["packet_tag"] == tag),
        None,
    )
    expander_label = "✏️ Edit redaction (manual)"
    if queued_entry is not None:
        expander_label += " -- currently held: " + queued_entry["reason"]
    with st.expander(expander_label, expanded=queued_entry is not None):
        _render_manual_editor(
            args,
            roster,
            packet,
            tag,
            default_sid=selected_sid,
            flagged_regions=queued_entry.get("flagged_regions") if queued_entry is not None else None,
        )


def _resolve_class_period(args: argparse.Namespace) -> int | None:
    if args.class_period is not None:
        return args.class_period
    inferred = infer_period_from_filename(args.pdf_path)
    return int(inferred) if inferred is not None else None


def _render_block_gate(args: argparse.Namespace) -> tuple[BlockMeaning, frozenset[str]] | None:
    """Shows the block-resolution banner and, once resolved, a mandatory
    confirmation checkbox -- returns (chosen_block, disagreeing_packet_tags)
    only once a human has ticked it for this exact block; returns None
    otherwise, telling `main` to stop rendering anything else (sidebar,
    packet selector, packets) below this point. Only ever called when
    `_block_metadata` found a sidecar -- see main()'s own gate on that.

    This mirrors cli.py's own --confirm-block gate, just as an on-screen
    checkbox instead of a flag: no automated check can ever tell a packet
    correctly assigned to one block from the same packet wrongly assigned
    to another block with the same student names (see blocks.py's module
    docstring), so a human reading the report and explicitly confirming it
    is the only defense available, on either surface.
    """
    metadata = _block_metadata(args.roster_path)
    class_period = _resolve_class_period(args)
    if class_period is None:
        st.error(
            "Block metadata exists for this roster, but no --class-period was given and none "
            "could be inferred from the scan filename (expected something like 'PD1') -- restart "
            "with --class-period passed explicitly."
        )
        return None

    resolution = _block_resolution(args.pdf_path, class_period, args.roster_path, st.session_state.orientation_overrides, st.session_state.page_order)

    st.header("Block resolution")
    st.code(format_resolution_report(resolution), language=None)

    chosen_block: BlockMeaning | None = None
    if args.block is not None:
        block_code = normalize_block(args.block)
        if block_code not in metadata.blocks:
            st.error(
                f"--block {args.block!r} is not defined in this roster's block metadata "
                f"(known blocks: {sorted(metadata.blocks)})."
            )
            return None
        chosen_block = metadata.blocks[block_code]
        st.info(f"Explicit --block override in use: {chosen_block.describe()}")
    elif resolution.resolved:
        chosen_block = resolution.chosen_block

    if chosen_block is None:
        st.error(
            "Could not resolve a block from packet dates and no --block override was given -- "
            "restart with --block <NN> passed explicitly to proceed."
        )
        return None

    confirm_key = f"block_confirmed_{Path(args.pdf_path).stem}_{chosen_block.block}"
    confirmed = st.checkbox(
        f"I have read the resolution report above and confirm this review session should use "
        f"**{chosen_block.describe()}**.",
        key=confirm_key,
    )
    if not confirmed:
        st.info("Tick the box above to continue -- no packets are shown until the block is confirmed.")
        return None

    decisions = load_decisions(args.pdf_path, decisions_dir=Path(args.decisions_dir))
    mismatches = decisions_scope_mismatches(decisions, chosen_block.block)
    if mismatches:
        offending = ", ".join(f"{t} (sid {sid})" for t, sid in mismatches)
        st.error(
            f"Decisions already recorded for this scan are scoped to a different block than "
            f"{chosen_block.describe()}: {offending}. Either this session's block is wrong, or "
            "these decisions were recorded under a different block -- resolve the discrepancy "
            "before continuing."
        )
        return None

    save_resolved_block_record(args.pdf_path, chosen_block, decisions_dir=Path(args.decisions_dir))
    disagreeing = frozenset(disagreeing_packets(resolution))
    if disagreeing:
        st.warning(
            f"{len(disagreeing)} packet(s) have their own date disagreeing with the file's resolved "
            f"majority (flagged next to each affected packet's date, not blocked): {sorted(disagreeing)}"
        )
    return chosen_block, disagreeing


def main() -> None:
    st.set_page_config(page_title="MEL MPR+ADR review", layout="wide")
    args = _parse_args()

    if not Path(args.pdf_path).exists():
        st.error(f"PDF not found: {args.pdf_path}")
        return
    if not Path(args.roster_path).exists():
        st.error(f"Roster CSV not found: {args.roster_path}")
        return

    # Orientation overrides (a human's per-page rotation choice, see
    # orientation.py's detect-and-ask design) have to be in session_state
    # before the very first segment/round/consensus/propose pass below --
    # every one of those is keyed on this exact dict, so loading it late
    # would mean this run's first pass ignores a reviewer's already-
    # recorded rotation from a prior session.
    _init_state(args.pdf_path, args.decisions_dir)

    segmented = _segment(args.pdf_path, st.session_state.orientation_overrides, st.session_state.page_order)

    # Round grouping (see blocks.group_into_rounds) applies to every
    # teacher, unlike the _blocks.json-gated block-resolution feature
    # below -- shown as a plain informational banner, never a gate:
    # CLAUDE.md's round-segment design deliberately never holds or blocks a
    # packet on its own date disagreeing with its group (see the per-packet
    # warning in _render_packet), so there's nothing here to confirm.
    round_dates, round_groups = _round_data(args.pdf_path, st.session_state.orientation_overrides, st.session_state.page_order)
    round_labels = round_labels_by_tag(round_groups)
    output_round_disagreeing = round_disagreeing_tags(round_groups, round_dates)
    st.header("Round grouping")
    st.code(format_round_report(round_groups), language=None)

    # Consensus-ink anomaly check (see melredact/consensus.py): a
    # template-agnostic handwriting finder that only ever looks at
    # non-header pages, since the header page is already unconditionally
    # redacted regardless of any match. Shown here purely so a reviewer
    # sees what the check found before it gates "Run redaction pipeline"
    # below the same way held_back already does for the other checks.
    consensus_analysis = _consensus(args.pdf_path, st.session_state.orientation_overrides, st.session_state.page_order)
    with st.expander("Consensus-ink anomaly check", expanded=bool(consensus_analysis.holds)):
        st.code(format_consensus_report(consensus_analysis), language=None)

    # --round restricts this whole session to one collection session inside
    # a larger concatenated scan (see pipeline.filter_packets_by_round).
    # Round grouping itself is always computed over the whole file above --
    # grouping is inherently file-level (see blocks.py) -- but every packet
    # a reviewer can actually see, match against, or send through "Run
    # redaction pipeline" below only ever comes from `segmented` after this
    # point, so an excluded packet is never touched: not segmented for
    # matching, not shown, not redacted, not written, and never looked up
    # in the ledger.
    if args.round is not None:
        known_labels = sorted({g.label for g in round_groups})
        if args.round not in known_labels:
            st.error(f"--round {args.round!r} is not one of this file's round groups ({known_labels}).")
            return
        segmented = filter_packets_by_round(args.pdf_path, segmented, round_labels, args.round)
        if not segmented.packets:
            st.error(f"Round {args.round!r} has no packets to show.")
            return
        st.info(f"Restricting this session to round {args.round!r}: {len(segmented.packets)} packet(s).")

    resolved_block: BlockMeaning | None = None
    disagreeing_tags: frozenset[str] = frozenset()
    period_for_roster = args.period
    if _block_metadata(args.roster_path) is not None:
        gate = _render_block_gate(args)
        if gate is None:
            return
        resolved_block, disagreeing_tags = gate
        period_for_roster = resolved_block.block

    try:
        roster = _roster(args.roster_path, args.pdf_path, period_for_roster)
    except RosterError as exc:
        st.error(f"Roster error: {exc}")
        return
    session_tags = {packet_tag(args.pdf_path, p) for p in segmented.packets}
    proposals = [p for p in _proposals(args.pdf_path, args.roster_path, period_for_roster, st.session_state.orientation_overrides, st.session_state.page_order) if p.packet_tag in session_tags]
    auto_assignments = assign_all(proposals, round_labels=round_labels)
    proposals_by_tag = {p.packet_tag: p for p in proposals}

    _render_sidebar(args, segmented, roster, resolved_block, round_labels, consensus_analysis.holds)

    tags = [packet_tag(args.pdf_path, p) for p in segmented.packets]
    packet_by_tag = dict(zip(tags, segmented.packets))

    with st.sidebar.expander("All packets"):
        for t in tags:
            st.write(f"{_status_icon(t, packet_by_tag[t], st.session_state.decisions, proposals_by_tag.get(t))} {t}")

    if st.session_state.get("show_manual_queue"):
        st.header("Manual redaction queue")
        _render_manual_queue(args, roster, packet_by_tag)
        return

    # The selectbox's *value* is always the stable tag -- never a status
    # icon baked into the option text, which would invalidate the widget's
    # stored value the moment that packet's own icon changes (e.g. right
    # after Confirm flips it from pending to approved), since Streamlit
    # requires a selectbox's current value to remain a member of `options`
    # by equality. Status is shown separately as a caption instead.
    if "packet_select" not in st.session_state:
        st.session_state.packet_select = tags[0]

    def _go(delta: int) -> None:
        i = tags.index(st.session_state.packet_select)
        st.session_state.packet_select = tags[max(0, min(len(tags) - 1, i + delta))]

    col_prev, col_select, col_next = st.columns([1, 6, 1])
    col_prev.button("< Prev", on_click=_go, args=(-1,))
    col_next.button("Next >", on_click=_go, args=(1,))
    tag = col_select.selectbox("Packet", options=tags, key="packet_select")
    packet = packet_by_tag[tag]
    status = _status_icon(tag, packet, st.session_state.decisions, proposals_by_tag.get(tag))
    st.subheader(f"{status} Packet {tag} ({packet.n_pages} page{'s' if packet.n_pages != 1 else ''})")
    _render_packet(
        args,
        packet,
        tag,
        roster,
        proposals_by_tag[tag],
        auto_assignments,
        tags,
        resolved_block,
        disagreeing_tags,
        round_labels,
        output_round_disagreeing,
        segmented,
        packet_by_tag,
    )
    _prefetch_next_packet(args, tags, packet_by_tag, tag)


def _prefetch_next_packet(
    args: argparse.Namespace, tags: list[str], packet_by_tag: dict[str, Packet], current_tag: str
) -> None:
    """Renders the next packet's own pages into `_page_image`'s cache
    (disk-backed, see CACHE_DIR) before this script run ends, so a
    reviewer clicking Next lands on already-rendered images instead of
    paying the render cost live -- see CLAUDE.md's "Make the editor fast
    enough to use 46 times in a sitting" section. Streamlit reruns
    synchronously, so this doesn't run concurrently with the reviewer's own
    time on the current packet, but it does mean the *next* click's render
    cost is paid now, while the current packet is already on screen,
    rather than after the click -- the only prefetch shape available
    without introducing a separate worker thread/process. Best-effort:
    swallowed exceptions here must never break the packet actually on
    screen, and a page that fails to prefetch just renders live on demand
    when the reviewer actually gets to it."""
    i = tags.index(current_tag)
    if i + 1 >= len(tags):
        return
    next_packet = packet_by_tag[tags[i + 1]]
    try:
        for page_idx in next_packet.page_indices:
            _page_image(args.pdf_path, page_idx, DPI, st.session_state.orientation_overrides)
            # Also warm the as-scanned ({}) rendering the page-stack preview
            # reads from (see _render_page_stack) -- same page, a second,
            # cheap disk-cache entry, not a second expensive render whenever
            # this page has no override (the overwhelming case: both calls
            # resolve to the identical cache_file already).
            _page_image(args.pdf_path, page_idx, DPI, {})
    except Exception:
        pass


if __name__ == "__main__":
    main()
