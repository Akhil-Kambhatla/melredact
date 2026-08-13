"""Human review UI for MEL MPR+ADR packet redaction.

    streamlit run review_app.py -- <scan.pdf> <roster.csv> [--out-dir DIR] [--decisions-dir DIR]

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
import sys
from pathlib import Path

import streamlit as st
from PIL import Image

from melredact.blocks import (
    BlockMeaning,
    BlockResolution,
    collect_packet_dates,
    decisions_scope_mismatches,
    disagreeing_packets,
    format_resolution_report,
    load_block_metadata,
    normalize_block,
    resolve_block,
    save_resolved_block_record,
)
from melredact.config import CACHE_DIR, HEADER_BAND_FALLBACK, MIN_MARGIN, MIN_SCORE, RENDER_DPI_PREVIEW
from melredact.match import assign_all
from melredact.pdfio import open_pdf
from melredact.pipeline import (
    list_manual_queue,
    load_decisions,
    load_detection_overrides,
    manual_queue_draft_path,
    packet_tag,
    propose_all,
    release_from_manual_queue,
    run_dispositions,
    save_decisions,
    save_detection_overrides,
)
from melredact.redact import HeaderBand, render_redaction_preview
from melredact.roster import Roster, RosterError, infer_period_from_filename, load_roster
from melredact.segment import Packet, SegmentResult, extract_header_fields, segment_pdf

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
    return parser.parse_args(sys.argv[1:])


@st.cache_data(show_spinner="Segmenting PDF into packets...")
def _segment(pdf_path: str) -> SegmentResult:
    return segment_pdf(pdf_path)


def _block_metadata(roster_path: str):
    # Not cache_data: cheap (one small JSON read), and load_block_metadata
    # returns None for the overwhelmingly common case -- no reason to pay
    # cache bookkeeping for that.
    return load_block_metadata(roster_path)


@st.cache_data(show_spinner="Reading packet dates for block resolution...")
def _block_resolution(pdf_path: str, class_period: int, roster_path: str) -> BlockResolution:
    # roster_path is only here to key the cache correctly if the sidecar
    # ever changes between reruns -- resolve_block itself only reads
    # `metadata`, recomputed fresh by the caller (cheap, see _block_metadata).
    metadata = load_block_metadata(roster_path)
    dates = collect_packet_dates(pdf_path)
    return resolve_block(dates, class_period, metadata)


@st.cache_data(show_spinner="Loading roster...")
def _roster(roster_path: str, pdf_path: str, period: str | None) -> Roster:
    return load_roster(roster_path, period=period, infer_period_from=pdf_path)


@st.cache_data(show_spinner="Scoring name candidates against the roster...")
def _proposals(pdf_path: str, roster_path: str, period: str | None):
    return propose_all(pdf_path, _segment(pdf_path), _roster(roster_path, pdf_path, period))


@st.cache_data(show_spinner=False)
def _header_fields(pdf_path: str, page_index: int):
    """Cached wrapper around extract_header_fields, keyed by (pdf_path,
    page_index) rather than a pdfplumber Page object (not hashable in a
    way st.cache_data can use) -- opens the pdf fresh, same pattern as
    _page_image below. Without this, every Streamlit rerun (a button
    click, a radio change, Prev/Next) re-ran OCR-based header field
    extraction for whichever packet was on screen; with melredact.ocr's
    disk cache in place that call is now cheap even on a cold cache, but
    there's no reason to repeat even the in-memory anchor-location work
    on every rerun within one session."""
    with open_pdf(pdf_path) as pdf:
        return extract_header_fields(pdf.pages[page_index])


@st.cache_data(show_spinner=False)
def _page_image(pdf_path: str, page_index: int, dpi: int) -> Image.Image:
    cache_file = Path(CACHE_DIR) / Path(pdf_path).stem / f"page_{page_index:04d}_{dpi}.png"
    if cache_file.exists():
        return Image.open(cache_file).convert("RGB")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open_pdf(pdf_path) as pdf:
        image = pdf.pages[page_index].to_image(resolution=dpi).original.convert("RGB")
    image.save(cache_file)
    return image


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


def _init_state(pdf_path: str, decisions_dir: str) -> None:
    if "decisions" not in st.session_state:
        st.session_state.decisions = load_decisions(pdf_path, decisions_dir=Path(decisions_dir))
    if "detection_overrides" not in st.session_state:
        st.session_state.detection_overrides = load_detection_overrides(pdf_path, decisions_dir=Path(decisions_dir))


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


def _render_sidebar(
    args: argparse.Namespace, segmented: SegmentResult, roster: Roster, resolved_block: BlockMeaning | None = None
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

    st.sidebar.divider()
    if st.sidebar.button("Run redaction pipeline", type="primary", disabled=n_pending == len(segmented.packets) == 0):
        with st.spinner("Redacting approved packets..."):
            fresh_decisions = load_decisions(args.pdf_path, decisions_dir=Path(args.decisions_dir))
            fresh_overrides = load_detection_overrides(args.pdf_path, decisions_dir=Path(args.decisions_dir))
            results = run_dispositions(
                args.pdf_path,
                segmented,
                fresh_decisions,
                roster,
                out_dir=Path(args.out_dir),
                detection_overrides=fresh_overrides,
            )
            written = [r for r in results if r.out_path is not None]
            deleted = sum(1 for r in results if r.deleted_path is not None)
            pending = sum(1 for r in results if r.pending)
            held_back = [r for r in results if r.held_back]
            consent_held = [r for r in results if r.consent_hold]
            overridden = [r for r in written if r.reason]
            collided = [r for r in written if r.collision_note]
            st.sidebar.success(
                f"{len(written)} written ({len(collided)} collision(s) avoided), {deleted} deleted, "
                f"{len(held_back)} held back for review, {len(consent_held)} consent-held (no SID), "
                f"{pending} still pending review"
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

    st.sidebar.divider()
    queue_entries = [e for e in list_manual_queue(args.out_dir) if e["pdf_path"] == str(Path(args.pdf_path))]
    st.sidebar.write(f"🛠️ Manual-redaction queue: {len(queue_entries)}")
    st.sidebar.checkbox("Show manual redaction queue", key="show_manual_queue", disabled=not queue_entries)


def _render_field_table(fields) -> None:
    st.table(
        {
            "Field": ["Name (used for matching)", "Teacher", "Group members (context only)", "Date", "Period"],
            "OCR'd text": [fields.name_text, fields.teacher_text, fields.group_text, fields.date_text, fields.period_text],
        }
    )


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


def _render_manual_queue(args: argparse.Namespace, roster: Roster, packet_by_tag: dict[str, Packet]) -> None:
    """The backstop for a genuine detection-confidence or coverage-check
    miss (see CLAUDE.md's "the manual-redaction queue is a backstop"
    section) -- never a substitute for the automated checks catching it in
    the first place. Each queued entry shows the drafted (not-safe-to-ship)
    attempt exactly as it was held back, lets a human propose a corrected
    band, previews that correction through the same render_redaction_
    preview mechanism the ordinary decision preview uses, and only writes
    to out/ if release_from_manual_queue's own re-check of both automated
    checks (uncovered_group_words, verify_no_leaked_names) still passes
    with that geometry -- a wrong correction stays queued, nothing is
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

            st.write("Propose a corrected header band (points, page-top-down):")
            c1, c2, c3, c4 = st.columns(4)
            left = c1.number_input("left", value=float(HEADER_BAND_FALLBACK["left"]), key=f"mq_left_{tag}")
            top = c2.number_input("top", value=float(HEADER_BAND_FALLBACK["top"]), key=f"mq_top_{tag}")
            right = c3.number_input("right", value=float(HEADER_BAND_FALLBACK["right"]), key=f"mq_right_{tag}")
            bottom = c4.number_input(
                "bottom", value=float(HEADER_BAND_FALLBACK["bottom"] + 20), key=f"mq_bottom_{tag}"
            )
            candidate_band = HeaderBand(left=left, top=top, right=right, bottom=bottom, detected=True)

            if packet.header_page_index is not None:
                raw_image = _page_image(args.pdf_path, packet.header_page_index, DPI)
                stamp_lines = _stamp_lines_for(sid, roster) if sid in roster else None
                preview_image, _ = render_redaction_preview(
                    raw_image, dpi=DPI, stamp_lines=stamp_lines, band_override=candidate_band
                )
                st.image(preview_image, caption="Preview with your proposed corrected band")

            if st.button("Release to out/", type="primary", key=f"mq_release_{tag}"):
                result = release_from_manual_queue(
                    args.pdf_path, packet, tag, sid, roster, candidate_band, out_dir=Path(args.out_dir)
                )
                if result.released:
                    st.success(f"Released {tag} -> {result.out_path}")
                    st.rerun()
                else:
                    st.error(f"Still not safe to release with this band: {result.reason}")


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
) -> None:
    if resolved_block is not None:
        st.caption(f"Approving into: {resolved_block.describe()}")

    if packet.issues:
        st.warning(
            "This packet has unresolved segmentation issues and cannot be assigned a SID "
            "until a human resolves them out of band (e.g. a missing/misfiled page). "
            "You may still mark it as not-on-roster to reject it.\n\n" + "\n".join(f"- {i}" for i in packet.issues)
        )

    if tag not in st.session_state.decisions and proposal.is_held_match:
        st.info(
            f"🔒 This packet's best match is a **held name**: {proposal.top_held.full_name} "
            "-- consent-known, but this student's SID couldn't be trusted in the roster export "
            f"(see `data/teacher_codes/*_holds.csv`). It will be fully redacted but never written "
            "to `out/` and never deleted; recording a decision here (a real roster SID, or "
            "confirmed non-consent) overrides this hold."
        )

    if packet.header_page_index is None:
        st.info("No header page for this packet (orphan continuation page).")
        candidate_options: list[tuple[str, str | None]] = []
    else:
        fields = _header_fields(args.pdf_path, packet.header_page_index)

        top5 = proposal.candidates[:5]
        candidate_options = [(_decision_label(c.sid, roster), c.sid) for c in top5]
        all_options_preview = candidate_options + [("Not on roster (no consent)", None)]
        default_sid_preview = auto_assignments.get(tag)
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

        raw_image = _page_image(args.pdf_path, packet.header_page_index, DPI)
        stamp_lines = _stamp_lines_for(selected_sid, roster)
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

        col1, col2 = st.columns(2)
        col1.image(raw_image, caption="Original scan")
        band_note = "border detected" if band.detected else "fallback band used"
        preview_caption = f"Redaction preview, reflects current selection ({band_note})"
        if selected_sid is None:
            preview_caption += " -- no packet would be written for 'Not on roster'"
        col2.image(preview_image, caption=preview_caption)

        _render_field_table(fields)
        if tag in disagreeing_tags:
            st.warning(
                f"This packet's own date ({fields.date_text!r}) disagrees with the file's resolved "
                "collection round -- shown for awareness, not held or blocked (see the block "
                "resolution banner above; students get their own written date wrong often enough "
                "that a single packet's date is a flag, not a signal to act on)."
            )
        _render_candidates(proposal, top5, roster, auto_assignments, tag)

    with st.expander("Search the full roster"):
        query = st.text_input("Filter by name", key=f"search_{tag}")
        matches = [e for e in roster if not query or query.lower() in e.full_name.lower()]
        if matches:
            chosen = st.selectbox(
                "Roster entry",
                options=[e.sid for e in matches],
                format_func=lambda sid: _decision_label(sid, roster),
                key=f"search_select_{tag}",
            )
            if st.button("Use this roster entry", key=f"search_use_{tag}", disabled=bool(packet.issues)):
                _confirm(args.pdf_path, args.decisions_dir, tag, chosen)
                st.rerun()

    all_options = candidate_options + [("Not on roster (no consent)", None)]
    labels = [label for label, _ in all_options]
    default_sid = auto_assignments.get(tag)
    default_index = next((i for i, (_, sid) in enumerate(all_options) if sid == default_sid), len(all_options) - 1)

    choice_label = st.radio("Decision", options=labels, index=default_index, key=f"decision_{tag}")
    choice_sid = dict(all_options)[choice_label]

    disabled = bool(packet.issues) and choice_sid is not None
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

    resolution = _block_resolution(args.pdf_path, class_period, args.roster_path)

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

    segmented = _segment(args.pdf_path)

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
    proposals = _proposals(args.pdf_path, args.roster_path, period_for_roster)
    auto_assignments = assign_all(proposals)
    proposals_by_tag = {p.packet_tag: p for p in proposals}

    _init_state(args.pdf_path, args.decisions_dir)
    _render_sidebar(args, segmented, roster, resolved_block)

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
    _render_packet(args, packet, tag, roster, proposals_by_tag[tag], auto_assignments, tags, resolved_block, disagreeing_tags)


if __name__ == "__main__":
    main()
