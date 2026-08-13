import json

import pytest

from melredact.blocks import (
    BlockMeaning,
    BlockMetadata,
    PacketDate,
    decisions_scope_mismatches,
    disagreeing_packets,
    load_block_metadata,
    load_resolved_block_record,
    parse_month,
    resolve_block,
)
from melredact.cli import main
from melredact.pipeline import packet_tag, save_decisions
from melredact.roster import infer_period_from_filename
from melredact.segment import segment_pdf
from tests.make_fixture import ROSTER, PacketSpec, _build_packets_pdf, build_main_fixture

TEACHER_CODE = "010406"


# --- parse_month ---


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3/31/2026", 3),
        ("03/31/26", 3),
        ("3-31-2026", 3),
        ("12/1/2025", 12),
        ("March 31, 2026", 3),
        ("Mar 31 2026", 3),
        ("December 1 2025", 12),
        ("Dec. 1, 2025", 12),
    ],
)
def test_parse_month_good_inputs(text, expected):
    assert parse_month(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "   ",
        "asdkfjh",
        "13/31/2026",  # month out of range
        "3/32/2026",  # day out of range
        "3//2026",  # incomplete numeric date
        "0/5/2026",  # month zero
    ],
)
def test_parse_month_garbage_returns_none(text):
    assert parse_month(text) is None


# --- resolve_block: majority resolution ---


def _metadata(month_by_block: dict[str, int], class_period: int = 1) -> BlockMetadata:
    return BlockMetadata(
        teacher_code=TEACHER_CODE,
        blocks={b: BlockMeaning(block=b, class_period=class_period, month=m) for b, m in month_by_block.items()},
    )


def test_resolve_block_majority_picks_the_right_block():
    dates = [PacketDate(f"t{i}", "", 3) for i in range(4)] + [PacketDate("t5", "", 2)]
    res = resolve_block(dates, class_period=1, metadata=_metadata({"01": 2, "02": 3}))
    assert res.resolved
    assert res.chosen_block.block == "02"
    assert res.confidence == pytest.approx(4 / 5)


def test_resolve_block_abstains_below_min_dated_packets():
    dates = [PacketDate("t1", "", 3), PacketDate("t2", "", 3)]
    res = resolve_block(dates, class_period=1, metadata=_metadata({"01": 2, "02": 3}))
    assert not res.resolved
    assert res.chosen_block is None
    assert "only 2" in res.reason


def test_resolve_block_abstains_below_majority_fraction():
    dates = [PacketDate("t1", "", 3), PacketDate("t2", "", 3), PacketDate("t3", "", 2), PacketDate("t4", "", 2)]
    res = resolve_block(dates, class_period=1, metadata=_metadata({"01": 2, "02": 3}))
    assert not res.resolved
    assert res.chosen_block is None
    assert "60%" in res.reason


def test_resolve_block_abstains_when_no_block_defined_for_the_majority_month():
    dates = [PacketDate(f"t{i}", "", 5) for i in range(4)]  # May -- not in metadata
    res = resolve_block(dates, class_period=1, metadata=_metadata({"01": 2, "02": 3}))
    assert not res.resolved
    assert "no block is defined" in res.reason


def test_disagreeing_packets_flags_minority_dates_without_blocking():
    dates = [PacketDate("a", "", 3), PacketDate("b", "", 3), PacketDate("c", "", 3), PacketDate("d", "", 2)]
    res = resolve_block(dates, class_period=1, metadata=_metadata({"01": 2, "02": 3}))
    assert res.resolved
    assert disagreeing_packets(res) == ["d"]


def test_decisions_scope_mismatches():
    decisions = {"t1": "0104060101", "t2": "0104060201", "t3": None}
    assert decisions_scope_mismatches(decisions, "02") == [("t1", "0104060101")]
    assert decisions_scope_mismatches(decisions, "01") == [("t2", "0104060201")]


# --- End-to-end: synthetic teacher with the real motivating scenario -- two
# blocks (01=Feb, 02=March) for the same class period, sharing all names ---


def _dual_block_roster_and_metadata(tmp_path, month_by_block=None, class_period=1):
    month_by_block = month_by_block or {"01": 2, "02": 3}
    rows = {
        block: [(f"{TEACHER_CODE}{block}{i:02d}", last, first) for i, (_, last, first) in enumerate(ROSTER, 1)]
        for block in month_by_block
    }
    roster_path = tmp_path / f"{TEACHER_CODE}.csv"
    with roster_path.open("w", newline="") as f:
        f.write("SID,Last Name,First Name\n")
        for i, (block, block_rows) in enumerate(rows.items()):
            if i > 0:
                f.write(",,\n")
            for sid, last, first in block_rows:
                f.write(f"{sid},{last},{first}\n")
    blocks_path = roster_path.with_name(f"{roster_path.stem}_blocks.json")
    blocks_path.write_text(
        json.dumps(
            {
                "teacher_code": TEACHER_CODE,
                "blocks": {b: {"class_period": class_period, "month": m} for b, m in month_by_block.items()},
            }
        )
    )
    return roster_path, rows


def _march_packets_pdf(tmp_path, filename="010406_PD1_PRT.pdf"):
    packets = [
        PacketSpec(f"p{i}", f"{first} {last}", "Hannel", "none", 2, None, date_text=f"3/{i}/2026")
        for i, (_, last, first) in enumerate(ROSTER[:4], 1)
    ]
    pdf_path = tmp_path / filename
    _build_packets_pdf(packets, [], [], pdf_path, tmp_path / "_unused_roster.csv")
    return pdf_path


def test_pd1_filename_with_march_dates_resolves_to_block_02_not_01(tmp_path):
    from melredact.blocks import collect_packet_dates

    roster_path, _rows = _dual_block_roster_and_metadata(tmp_path)
    pdf_path = _march_packets_pdf(tmp_path)

    metadata = load_block_metadata(roster_path)
    assert metadata is not None
    class_period = int(infer_period_from_filename(pdf_path))
    assert class_period == 1  # filename says PD1

    dates = collect_packet_dates(pdf_path)
    res = resolve_block(dates, class_period, metadata)
    assert res.resolved
    assert res.chosen_block.block == "02"  # dates say March, not block 01 (February)


def test_run_without_confirm_block_exits_nonzero_and_writes_nothing(tmp_path, capsys):
    roster_path, _rows = _dual_block_roster_and_metadata(tmp_path)
    pdf_path = _march_packets_pdf(tmp_path)
    out = tmp_path / "out"

    rc = main(
        [
            "run",
            "--pdf",
            str(pdf_path),
            "--roster",
            str(roster_path),
            "--out",
            str(out),
            "--decisions",
            str(tmp_path / "decisions"),
        ]
    )
    assert rc == 1
    assert not out.exists()
    err = capsys.readouterr().err
    assert "--confirm-block" in err


def test_run_with_wrong_confirm_block_refuses(tmp_path, capsys):
    roster_path, _rows = _dual_block_roster_and_metadata(tmp_path)
    pdf_path = _march_packets_pdf(tmp_path)
    out = tmp_path / "out"

    rc = main(
        [
            "run",
            "--pdf",
            str(pdf_path),
            "--roster",
            str(roster_path),
            "--out",
            str(out),
            "--decisions",
            str(tmp_path / "decisions"),
            "--confirm-block",
            "01",
        ]
    )
    assert rc == 1
    assert not out.exists()


def test_decisions_from_a_different_block_aborts_the_run(tmp_path, capsys):
    roster_path, rows = _dual_block_roster_and_metadata(tmp_path)
    pdf_path = _march_packets_pdf(tmp_path)

    decisions_dir = tmp_path / "decisions"
    segmented = segment_pdf(pdf_path)
    tag0 = packet_tag(pdf_path, segmented.packets[0])
    # A decision naming a block-01 SID, but this file's dates resolve to
    # block 02 -- must abort, not silently process under the wrong scope.
    save_decisions(pdf_path, {tag0: rows["01"][0][0]}, decisions_dir=decisions_dir)

    out = tmp_path / "out"
    rc = main(
        [
            "run",
            "--pdf",
            str(pdf_path),
            "--roster",
            str(roster_path),
            "--out",
            str(out),
            "--decisions",
            str(decisions_dir),
            "--confirm-block",
            "02",
        ]
    )
    assert rc == 1
    assert not out.exists() or not any(out.rglob("*.pdf"))
    err = capsys.readouterr().err
    assert tag0 in err
    assert rows["01"][0][0] in err


def test_confirmed_run_scopes_roster_to_resolved_block_and_records_it(tmp_path):
    roster_path, _rows = _dual_block_roster_and_metadata(tmp_path)
    pdf_path = _march_packets_pdf(tmp_path)
    decisions_dir = tmp_path / "decisions"
    out = tmp_path / "out"

    rc = main(
        [
            "run",
            "--pdf",
            str(pdf_path),
            "--roster",
            str(roster_path),
            "--out",
            str(out),
            "--decisions",
            str(decisions_dir),
            "--confirm-block",
            "02",
        ]
    )
    assert rc == 0
    record = load_resolved_block_record(pdf_path, decisions_dir)
    assert record == {"block": "02", "class_period": 1, "month": 3}


# --- No sidecar: existing single-block teachers must be completely untouched ---


def test_no_block_metadata_sidecar_behaves_exactly_as_before(tmp_path):
    fx = build_main_fixture(tmp_path)
    assert load_block_metadata(fx.roster_path) is None

    out = tmp_path / "out"
    decisions_dir = tmp_path / "decisions"
    rc = main(
        [
            "run",
            "--pdf",
            str(fx.pdf_path),
            "--roster",
            str(fx.roster_path),
            "--out",
            str(out),
            "--decisions",
            str(decisions_dir),
        ]
    )
    # No --confirm-block, --class-period, or --block passed at all -- if the
    # gate applied here, this would exit 1 as in the tests above. It must
    # not: this teacher has no _blocks.json sidecar, so none of this
    # feature's behavior should engage.
    assert rc == 0
    assert out.is_dir()
