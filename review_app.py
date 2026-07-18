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

import pdfplumber
import streamlit as st
from PIL import Image

from melredact.config import CACHE_DIR, MIN_MARGIN, MIN_SCORE, RENDER_DPI_PREVIEW
from melredact.match import assign_all
from melredact.pipeline import load_decisions, packet_tag, propose_all, run_dispositions, save_decisions
from melredact.redact import render_redaction_preview
from melredact.roster import Roster, RosterError, load_roster
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
        "inferred from the scan filename (e.g. 'PD2') if omitted",
    )
    return parser.parse_args(sys.argv[1:])


@st.cache_data(show_spinner="Segmenting PDF into packets...")
def _segment(pdf_path: str) -> SegmentResult:
    return segment_pdf(pdf_path)


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
    with pdfplumber.open(pdf_path) as pdf:
        return extract_header_fields(pdf.pages[page_index])


@st.cache_data(show_spinner=False)
def _page_image(pdf_path: str, page_index: int, dpi: int) -> Image.Image:
    cache_file = Path(CACHE_DIR) / Path(pdf_path).stem / f"page_{page_index:04d}_{dpi}.png"
    if cache_file.exists():
        return Image.open(cache_file).convert("RGB")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with pdfplumber.open(pdf_path) as pdf:
        image = pdf.pages[page_index].to_image(resolution=dpi).original.convert("RGB")
    image.save(cache_file)
    return image


def _status_icon(tag: str, packet: Packet, decisions: dict[str, str | None]) -> str:
    if packet.issues:
        return "⚠️"
    if tag not in decisions:
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


def _confirm(pdf_path: str, decisions_dir: str, tag: str, sid: str | None) -> None:
    st.session_state.decisions[tag] = sid
    save_decisions(pdf_path, st.session_state.decisions, decisions_dir=Path(decisions_dir))
    st.toast(f"Saved decision for {tag}")


def _render_sidebar(args: argparse.Namespace, segmented: SegmentResult, roster: Roster) -> None:
    decisions = st.session_state.decisions
    n_pending = sum(1 for p in segmented.packets if packet_tag(args.pdf_path, p) not in decisions)
    n_consented = sum(1 for sid in decisions.values() if sid is not None)
    n_rejected = sum(1 for sid in decisions.values() if sid is None)

    st.sidebar.header("MEL MPR+ADR review")
    st.sidebar.text(f"Scan: {Path(args.pdf_path).name}")
    period_note = f", period {roster.entries[0].period_display}" if roster.entries else ""
    st.sidebar.text(f"Roster: {len(roster)} students{period_note}")
    st.sidebar.metric("Packets", len(segmented.packets))
    st.sidebar.write(f"⏳ Pending: {n_pending}  ✅ Approved: {n_consented}  🚫 Rejected: {n_rejected}")

    st.sidebar.divider()
    if st.sidebar.button("Run redaction pipeline", type="primary", disabled=n_pending == len(segmented.packets) == 0):
        with st.spinner("Redacting approved packets..."):
            fresh_decisions = load_decisions(args.pdf_path, decisions_dir=Path(args.decisions_dir))
            try:
                results = run_dispositions(
                    args.pdf_path, segmented, fresh_decisions, roster, out_dir=Path(args.out_dir)
                )
            except RuntimeError as exc:
                st.sidebar.error(f"Verify pass failed, output deleted: {exc}")
            else:
                written = sum(1 for r in results if r.out_path is not None)
                deleted = sum(1 for r in results if r.deleted_path is not None)
                pending = sum(1 for r in results if r.pending)
                st.sidebar.success(f"{written} written, {deleted} deleted, {pending} still pending review")


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


def _render_packet(args: argparse.Namespace, packet: Packet, tag: str, roster: Roster, proposal, auto_assignments) -> None:
    if packet.issues:
        st.warning(
            "This packet has unresolved segmentation issues and cannot be assigned a SID "
            "until a human resolves them out of band (e.g. a missing/misfiled page). "
            "You may still mark it as not-on-roster to reject it.\n\n" + "\n".join(f"- {i}" for i in packet.issues)
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
        preview_image, band = render_redaction_preview(
            raw_image, dpi=DPI, group_top=fields.anchors.group_top, stamp_lines=stamp_lines
        )

        col1, col2 = st.columns(2)
        col1.image(raw_image, caption="Original scan")
        band_note = "border detected" if band.detected else "fallback band used"
        preview_caption = f"Redaction preview, reflects current selection ({band_note})"
        if selected_sid is None:
            preview_caption += " -- no packet would be written for 'Not on roster'"
        col2.image(preview_image, caption=preview_caption)

        _render_field_table(fields)
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
    if st.button("Confirm decision", type="primary", disabled=disabled, key=f"confirm_{tag}"):
        _confirm(args.pdf_path, args.decisions_dir, tag, choice_sid)
        st.rerun()

    current = st.session_state.decisions.get(tag)
    if tag in st.session_state.decisions:
        st.caption(f"Recorded: {_decision_label(current, roster)}")
    else:
        st.caption("Recorded: pending (not yet reviewed)")


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
    try:
        roster = _roster(args.roster_path, args.pdf_path, args.period)
    except RosterError as exc:
        st.error(f"Roster error: {exc}")
        return
    proposals = _proposals(args.pdf_path, args.roster_path, args.period)
    auto_assignments = assign_all(proposals)
    proposals_by_tag = {p.packet_tag: p for p in proposals}

    _init_state(args.pdf_path, args.decisions_dir)
    _render_sidebar(args, segmented, roster)

    tags = [packet_tag(args.pdf_path, p) for p in segmented.packets]
    packet_by_tag = dict(zip(tags, segmented.packets))

    with st.sidebar.expander("All packets"):
        for t in tags:
            st.write(f"{_status_icon(t, packet_by_tag[t], st.session_state.decisions)} {t}")

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
    status = _status_icon(tag, packet, st.session_state.decisions)
    st.subheader(f"{status} Packet {tag} ({packet.n_pages} page{'s' if packet.n_pages != 1 else ''})")
    _render_packet(args, packet, tag, roster, proposals_by_tag[tag], auto_assignments)


if __name__ == "__main__":
    main()
