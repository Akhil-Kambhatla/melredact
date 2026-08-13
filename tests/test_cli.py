import pytest

from melredact.cli import main
from melredact.pipeline import decisions_path, packet_tag, save_decisions
from melredact.segment import segment_pdf
from tests.make_fixture import build_main_fixture


@pytest.fixture(scope="module")
def main_fixture(tmp_path_factory):
    return build_main_fixture(tmp_path_factory.mktemp("cli_fixture"))


# --- --out must be a directory, not a .pdf path (per-packet output is
# intentional; see RUNBOOK/CLAUDE.md) ---


def test_run_rejects_a_dot_pdf_out_path(main_fixture, tmp_path, capsys):
    out = tmp_path / "combined.pdf"
    rc = main(["run", "--pdf", str(main_fixture.pdf_path), "--roster", str(main_fixture.roster_path), "--out", str(out)])
    assert rc == 1
    assert not out.exists()
    err = capsys.readouterr().err
    assert "directory" in err
    assert str(out) in err


def test_run_accepts_a_plain_directory_name_for_out(main_fixture, tmp_path):
    segmented = segment_pdf(main_fixture.pdf_path)
    decisions_dir = tmp_path / "decisions"
    tag = packet_tag(main_fixture.pdf_path, segmented.packets[0])
    save_decisions(main_fixture.pdf_path, {tag: None}, decisions_dir=decisions_dir)
    assert decisions_path(main_fixture.pdf_path, decisions_dir).exists()

    out = tmp_path / "out"
    rc = main(
        [
            "run",
            "--pdf",
            str(main_fixture.pdf_path),
            "--roster",
            str(main_fixture.roster_path),
            "--out",
            str(out),
            "--decisions",
            str(decisions_dir),
        ]
    )
    assert rc == 0
    assert out.is_dir()


# --- verify: unscoped against the whole (possibly multi-period) roster ---


def _multi_period_roster(tmp_path, extra_rows):
    path = tmp_path / "multi_period_roster.csv"
    with path.open("w", newline="") as f:
        f.write("SID,Last Name,First Name\n")
        for sid, last, first in extra_rows:
            f.write(f"{sid},{last},{first}\n")
    return path


def test_verify_does_not_take_a_period_flag():
    with pytest.raises(SystemExit):
        main(["verify", "--roster", "x.csv", "--period", "2"])


def test_verify_does_not_take_a_pdf_flag():
    """Regression guard for the naming change: output is now named by SID
    under <teacher>/<period>/, not by the source scan's stem, so --pdf has
    nothing left to filter by and verify must not accept it."""
    with pytest.raises(SystemExit):
        main(["verify", "--pdf", "x.pdf", "--roster", "x.csv"])


def test_verify_fails_loudly_on_a_directory_with_no_matching_output(tmp_path, capsys):
    """The old flat-naming discovery silently checked nothing (and passed)
    when it found no matching files -- more dangerous than erroring, since
    a clean silent run looks identical to a genuinely clean one. Pointing
    verify at an out_dir with nothing in the new <teacher>/<period>/<SID>.pdf
    shape must fail loudly instead."""
    roster_path = tmp_path / "roster.csv"
    roster_path.write_text("SID,Last Name,First Name\n0204150201,Ames,Jordan\n")
    empty_out = tmp_path / "empty_out"
    empty_out.mkdir()

    rc = main(["verify", "--roster", str(roster_path), "--out", str(empty_out)])
    assert rc == 1
    err = capsys.readouterr().err
    assert str(empty_out) in err


def test_verify_recursive_glob_finds_files_under_the_topic_and_round_segments(main_fixture, tmp_path, capsys):
    """Output now lands at out/<teacher>/<period>/<worksheet_type>/<topic>/
    <round>/<SID>.pdf -- two levels deeper than verify's old fixed
    four-level glob. verify must walk recursively and still report a
    nonzero checked count, not silently find nothing at the old fixed
    depth."""
    segmented = segment_pdf(main_fixture.pdf_path)
    decisions_dir = tmp_path / "decisions"
    tag = packet_tag(main_fixture.pdf_path, segmented.packets[0])
    consented_sid = main_fixture.expected_final_sid["clean_match"]
    save_decisions(main_fixture.pdf_path, {tag: consented_sid}, decisions_dir=decisions_dir)

    out = tmp_path / "out"
    rc = main(
        [
            "run",
            "--pdf",
            str(main_fixture.pdf_path),
            "--roster",
            str(main_fixture.roster_path),
            "--out",
            str(out),
            "--decisions",
            str(decisions_dir),
        ]
    )
    assert rc == 0

    written = list(out.rglob("*.pdf"))
    assert written
    assert any(p.parent.parent.name == "NA" for p in written), "fixture filename carries no topic -- must fall to NA"
    from melredact.blocks import round_label

    assert any(
        p.parent.name == round_label("10/03/2025") for p in written
    ), "fixture packets share one date -- must resolve to one round group"

    rc = main(["verify", "--roster", str(main_fixture.roster_path), "--out", str(out)])
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "1 file(s) checked, 0 failed" in out_text


# --- --round / --no-delete / analyze (2026-08-13) ---


def test_run_rejects_an_unknown_round_label(main_fixture, tmp_path, capsys):
    out = tmp_path / "out"
    rc = main(
        [
            "run",
            "--pdf",
            str(main_fixture.pdf_path),
            "--roster",
            str(main_fixture.roster_path),
            "--out",
            str(out),
            "--round",
            "not-a-real-round",
        ]
    )
    assert rc == 1
    assert not out.exists()
    err = capsys.readouterr().err
    assert "not-a-real-round" in err


def test_run_round_flag_processes_the_matching_round_normally(main_fixture, tmp_path):
    """The fixture's packets share one date, so its own round label (see
    blocks.round_label) must still process normally when explicitly passed
    via --round -- the flag restricts, it doesn't break, a run that
    genuinely only has that one round."""
    from melredact.blocks import round_label

    segmented = segment_pdf(main_fixture.pdf_path)
    decisions_dir = tmp_path / "decisions"
    tag = packet_tag(main_fixture.pdf_path, segmented.packets[0])
    consented_sid = main_fixture.expected_final_sid["clean_match"]
    save_decisions(main_fixture.pdf_path, {tag: consented_sid}, decisions_dir=decisions_dir)

    out = tmp_path / "out"
    rc = main(
        [
            "run",
            "--pdf",
            str(main_fixture.pdf_path),
            "--roster",
            str(main_fixture.roster_path),
            "--out",
            str(out),
            "--decisions",
            str(decisions_dir),
            "--round",
            round_label("10/03/2025"),
        ]
    )
    assert rc == 0
    assert list(out.rglob("*.pdf"))


def test_run_no_delete_flag_leaves_prior_output_in_place(main_fixture, tmp_path, capsys):
    segmented = segment_pdf(main_fixture.pdf_path)
    decisions_dir = tmp_path / "decisions"
    tag = packet_tag(main_fixture.pdf_path, segmented.packets[0])
    consented_sid = main_fixture.expected_final_sid["clean_match"]
    save_decisions(main_fixture.pdf_path, {tag: consented_sid}, decisions_dir=decisions_dir)

    out = tmp_path / "out"
    rc = main(
        [
            "run",
            "--pdf",
            str(main_fixture.pdf_path),
            "--roster",
            str(main_fixture.roster_path),
            "--out",
            str(out),
            "--decisions",
            str(decisions_dir),
        ]
    )
    assert rc == 0
    written = list(out.rglob("*.pdf"))
    assert len(written) == 1
    out_path = written[0]

    # A reviewer flips the same tag to confirmed non-consent -- but this
    # run passes --no-delete, so the previously-written file must survive.
    save_decisions(main_fixture.pdf_path, {tag: None}, decisions_dir=decisions_dir)
    rc = main(
        [
            "run",
            "--pdf",
            str(main_fixture.pdf_path),
            "--roster",
            str(main_fixture.roster_path),
            "--out",
            str(out),
            "--decisions",
            str(decisions_dir),
            "--no-delete",
        ]
    )
    assert rc == 0
    assert out_path.exists()
    out_text = capsys.readouterr().out
    assert "deletion disabled" in out_text
    assert "0 deleted" in out_text


def test_cmd_analyze_reports_without_writing_or_deleting(main_fixture, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["analyze", "--pdf", str(main_fixture.pdf_path), "--roster", str(main_fixture.roster_path)])
    assert rc == 0
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "decisions").exists()
    out_text = capsys.readouterr().out
    assert "Round grouping report" in out_text
    assert "Redaction hold analysis" in out_text
    assert "nothing written" in out_text.lower() or "nothing is written" in out_text.lower()


def test_verify_succeeds_against_a_roster_spanning_multiple_periods(main_fixture, tmp_path, capsys):
    """Regression: verify used to call the *scoped* loader, which raises
    RosterError the moment the roster spans more than one period and no
    --period is given/inferable -- even though verify never accepted
    --period in the first place. verify must load every period, always."""
    from tests.make_fixture import ROSTER

    extra_period_rows = [(f"02041530{i:02d}", f"Extra{i}", f"Student{i}") for i in range(1, 3)]
    roster_path = _multi_period_roster(tmp_path, list(ROSTER) + [("", "", "")] + extra_period_rows)

    segmented = segment_pdf(main_fixture.pdf_path)
    decisions_dir = tmp_path / "decisions"
    packet = segmented.packets[0]  # clean_match
    tag = packet_tag(main_fixture.pdf_path, packet)
    consented_sid = main_fixture.expected_final_sid["clean_match"]
    save_decisions(main_fixture.pdf_path, {tag: consented_sid}, decisions_dir=decisions_dir)

    out = tmp_path / "out"
    rc = main(
        [
            "run",
            "--pdf",
            str(main_fixture.pdf_path),
            "--roster",
            str(main_fixture.roster_path),
            "--out",
            str(out),
            "--decisions",
            str(decisions_dir),
        ]
    )
    assert rc == 0

    rc = main(["verify", "--roster", str(roster_path), "--out", str(out)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "multiple periods" not in err
