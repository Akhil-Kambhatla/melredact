import pytest

from melredact.roster import RosterError, filter_by_period, infer_period_from_filename, load_full_roster, load_roster
from tests.make_fixture import ROSTER, build_main_fixture


def _write_csv(path, rows):
    """rows may contain (sid, last, first) triples, or the single string
    "" to mean a blank period-block delimiter row."""
    with path.open("w", newline="") as f:
        f.write("SID,Last Name,First Name\n")
        for row in rows:
            if row == "":
                f.write(",,\n")
            else:
                sid, last, first = row
                f.write(f"{sid},{last},{first}\n")


def test_loads_valid_roster(tmp_path):
    path = tmp_path / "roster.csv"
    _write_csv(path, [("0204150201", "Ames", "Jordan"), ("0204150202", "Chandra", "Priya")])
    roster = load_roster(path)
    assert len(roster) == 2
    assert "0204150201" in roster
    entry = roster.by_sid["0204150201"]
    assert entry.full_name == "Jordan Ames"
    assert entry.teacher_code == "020415"
    assert entry.period_display == "02"
    assert entry.student_index == "01"


@pytest.mark.parametrize(
    "bad_sid",
    ["020415020", "02041502011", "020415020a", ""],
)
def test_rejects_malformed_sid(tmp_path, bad_sid):
    path = tmp_path / "roster.csv"
    _write_csv(path, [(bad_sid, "Ames", "Jordan")])
    with pytest.raises(RosterError):
        load_roster(path)


def test_strips_incidental_whitespace_rather_than_rejecting(tmp_path):
    """A trailing space from a spreadsheet export isn't malformed data."""
    path = tmp_path / "roster.csv"
    _write_csv(path, [(" 0204150201 ", "Ames", "Jordan")])
    roster = load_roster(path)
    assert "0204150201" in roster


def test_rejects_duplicate_sid(tmp_path):
    path = tmp_path / "roster.csv"
    _write_csv(path, [("0204150201", "Ames", "Jordan"), ("0204150201", "Chandra", "Priya")])
    with pytest.raises(RosterError):
        load_roster(path)


def test_rejects_missing_required_column(tmp_path):
    path = tmp_path / "roster.csv"
    with path.open("w", newline="") as f:
        f.write("SID,Last Name\n0204150201,Ames\n")
    with pytest.raises(RosterError):
        load_roster(path)


def test_membership_and_lookup_are_sid_based(tmp_path):
    path = tmp_path / "roster.csv"
    _write_csv(path, [("0204150201", "Ames", "Jordan")])
    roster = load_roster(path)
    assert "0204150201" in roster
    assert "0204150299" not in roster
    assert [e.sid for e in roster] == ["0204150201"]


def test_loads_the_fixture_roster(tmp_path):
    fixture = build_main_fixture(tmp_path / "fixture")
    roster = load_roster(fixture.roster_path)
    assert len(roster) == len(ROSTER)
    for sid, last, first in ROSTER:
        assert roster.by_sid[sid].last_name == last
        assert roster.by_sid[sid].first_name == first


# --- blank rows are period-block delimiters, not errors ---


def test_blank_row_is_a_delimiter_not_an_error(tmp_path):
    """A blank row doesn't raise, even when (as here) it happens to split
    rows that are all still the same period -- parsing tolerates it as
    structure regardless of what's on either side."""
    path = tmp_path / "roster.csv"
    _write_csv(
        path,
        [
            ("0204150201", "Shaik", "Nuzhat"),
            ("0204150202", "Pfleger", "Carter"),
            "",
            ("0204150203", "Salla", "Gonik"),
        ],
    )
    roster = load_roster(path)
    assert len(roster) == 3
    assert {e.sid for e in roster} == {"0204150201", "0204150202", "0204150203"}


def test_partially_blank_row_still_raises(tmp_path):
    """Only all-three-blank is a valid delimiter; a row with some fields
    filled is neither a valid delimiter nor a valid entry."""
    path = tmp_path / "roster.csv"
    with path.open("w", newline="") as f:
        f.write("SID,Last Name,First Name\n0204150201,Shaik,Nuzhat\n,,Nuzhat\n")
    with pytest.raises(RosterError, match="partially blank"):
        load_roster(path)


# --- block position vs. SID period cross-check ---


def test_block_and_sid_period_agreement_is_required(tmp_path):
    """A student whose SID encodes a different period than the block
    they're physically listed in (via blank-row position) is a data
    problem worth surfacing loudly, not something to silently resolve
    one way or the other."""
    path = tmp_path / "roster.csv"
    _write_csv(
        path,
        [
            ("0204150201", "Shaik", "Nuzhat"),
            ("0204150301", "Misfiled", "Student"),  # period 03 SID inside the period-02 block
            "",
            ("0204150302", "Riker", "Paige"),
        ],
    )
    with pytest.raises(RosterError, match="disagree"):
        load_roster(path)


# --- period scoping ---


def _multi_period_csv(tmp_path):
    path = tmp_path / "roster.csv"
    _write_csv(
        path,
        [
            ("0204150201", "Shaik", "Nuzhat"),
            ("0204150202", "Pfleger", "Carter"),
            "",
            ("0204150301", "Riker", "Paige"),
            ("0204150302", "Wylie", "Eden"),
        ],
    )
    return path


def test_multi_period_roster_requires_a_period_when_none_inferrable(tmp_path):
    path = _multi_period_csv(tmp_path)
    with pytest.raises(RosterError, match="multiple periods"):
        load_roster(path)


def test_explicit_period_narrows_to_that_block(tmp_path):
    path = _multi_period_csv(tmp_path)
    roster = load_roster(path, period="2")
    assert {e.sid for e in roster} == {"0204150201", "0204150202"}


def test_period_inferred_from_scan_filename(tmp_path):
    path = _multi_period_csv(tmp_path)
    roster = load_roster(path, infer_period_from="data/Hannel MPR PD3.pdf")
    assert {e.sid for e in roster} == {"0204150301", "0204150302"}


def test_explicit_period_wins_over_inference(tmp_path):
    path = _multi_period_csv(tmp_path)
    roster = load_roster(path, period="3", infer_period_from="data/Hannel MPR PD2.pdf")
    assert {e.sid for e in roster} == {"0204150301", "0204150302"}


def test_single_period_roster_never_needs_scoping(tmp_path):
    """No blank rows at all -- period/infer_period_from are accepted but
    irrelevant, since there's nothing to narrow."""
    path = tmp_path / "roster.csv"
    _write_csv(path, [("0204150201", "Shaik", "Nuzhat"), ("0204150202", "Pfleger", "Carter")])
    roster = load_roster(path)
    assert len(roster) == 2


def test_filter_by_period_raises_for_unknown_period(tmp_path):
    path = _multi_period_csv(tmp_path)
    roster = load_roster(path, period="2")
    with pytest.raises(RosterError, match="no roster entries"):
        filter_by_period(roster, "9")


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("Hannel MPR PD2.pdf", "02"),
        ("data/Hannel MPR PD12.pdf", "12"),
        ("pd3_scan.pdf", "03"),
        ("no period here.pdf", None),
    ],
)
def test_infer_period_from_filename(filename, expected):
    assert infer_period_from_filename(filename) == expected


# --- load_full_roster: the unscoped loader verify uses ---


def test_load_full_roster_never_narrows_a_multi_period_roster(tmp_path):
    """The exact case that used to break `verify`: load_roster would raise
    here because no period is given/inferable, but load_full_roster must
    return every period's entries regardless -- that's the whole point."""
    path = _multi_period_csv(tmp_path)
    roster = load_full_roster(path)
    assert {e.sid for e in roster} == {"0204150201", "0204150202", "0204150301", "0204150302"}


def test_load_full_roster_takes_no_period_argument():
    import inspect

    params = inspect.signature(load_full_roster).parameters
    assert list(params) == ["path"]


def test_load_full_roster_still_validates_malformed_data(tmp_path):
    """Unscoped means "don't narrow by period," not "don't validate" --
    a genuinely malformed roster must still fail loudly."""
    path = tmp_path / "roster.csv"
    _write_csv(path, [("0204150201", "Ames", "Jordan"), ("0204150201", "Chandra", "Priya")])
    with pytest.raises(RosterError):
        load_full_roster(path)


def test_load_full_roster_matches_load_roster_on_a_single_period_roster(tmp_path):
    path = tmp_path / "roster.csv"
    _write_csv(path, [("0204150201", "Shaik", "Nuzhat"), ("0204150202", "Pfleger", "Carter")])
    assert {e.sid for e in load_full_roster(path)} == {e.sid for e in load_roster(path)}
