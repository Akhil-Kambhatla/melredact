import csv

from melredact.roster import load_full_roster
from scripts.prepare_roster import prepare_roster


def _write_raw_csv(path, header, rows):
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def _read_raw_csv(path):
    with path.open(newline="") as f:
        return list(csv.reader(f))


def test_prepare_roster_drops_unlabeled_trailing_columns_and_strips_punctuation(tmp_path):
    path = tmp_path / "999999.csv"
    _write_raw_csv(
        path,
        ["SID", "Last Name", "First Name", "", "", ""],
        [
            ["9999990101", "Ames", "Jordan", "", "Period 1", "March"],
            ["9999990102", "Vantar?", "Lee", "", "", ""],
        ],
    )

    prepare_roster(path)

    rows = _read_raw_csv(path)
    assert rows[0] == ["SID", "Last Name", "First Name"]
    assert rows[1] == ["9999990101", "Ames", "Jordan"]
    assert rows[2] == ["9999990102", "Vantar", "Lee"]


def test_prepare_roster_moves_duplicate_sid_rows_to_holds_and_leaves_blank_rows_untouched(tmp_path):
    """Reproduces the real 010406.csv corruption shape (fictional SIDs/
    names): a duplicated SID pair inside a block, an untouched sibling row,
    and a blank period-block delimiter row that must survive unchanged."""
    path = tmp_path / "999999.csv"
    _write_raw_csv(
        path,
        ["SID", "Last Name", "First Name"],
        [
            ["9999990401", "Ames", "Jordan"],
            ["9999990402", "Chandra", "Priya"],
            ["9999990403", "Dupe", "One"],
            ["9999990404", "Untouched", "Solo"],
            ["9999990403", "Dupe", "Two"],
            ["", "", ""],
            ["9999990501", "NextBlock", "Student"],
        ],
    )

    prepare_roster(path)

    rows = _read_raw_csv(path)
    assert rows[0] == ["SID", "Last Name", "First Name"]
    main_data = rows[1:]

    sids = [r[0] for r in main_data if any(r)]
    assert sids == ["9999990401", "9999990402", "9999990404", "9999990501"]
    assert ["", "", ""] in main_data, "blank period-block delimiter row must survive untouched"

    holds_rows = _read_raw_csv(tmp_path / "999999_holds.csv")
    assert holds_rows[0] == ["Last Name", "First Name"]
    assert ["Dupe", "One"] in holds_rows[1:]
    assert ["Dupe", "Two"] in holds_rows[1:]
    assert len(holds_rows) - 1 == 2

    # The cleaned file now loads cleanly, and the moved students show up
    # as held names, not as roster entries under an untrustworthy SID.
    roster = load_full_roster(path)
    assert "9999990403" not in roster
    assert len(roster.held_names) == 2


def test_prepare_roster_never_renumbers_or_drops_a_sid_that_is_not_itself_duplicated(tmp_path):
    """Only SIDs that actually collide get moved -- a SID that merely sits
    inside a corrupted run but isn't itself duplicated is left in place;
    picking that one out is a human judgment call (see CLAUDE.md/the task
    that moved Osman, Jad by hand), not something a duplicate-count scan
    should ever guess at."""
    path = tmp_path / "999999.csv"
    _write_raw_csv(
        path,
        ["SID", "Last Name", "First Name"],
        [
            ["9999990601", "A", "One"],
            ["9999990602", "B", "Two"],
            ["9999990602", "C", "Three"],
            ["9999990603", "D", "Four"],
        ],
    )

    prepare_roster(path)

    rows = _read_raw_csv(path)
    sids = [r[0] for r in rows[1:] if any(r)]
    assert sids == ["9999990601", "9999990603"]


def test_prepare_roster_is_idempotent_on_a_re_run(tmp_path):
    """Running the script a second time over an already-cleaned file must
    not lose the holds it already wrote, or re-add duplicates."""
    path = tmp_path / "999999.csv"
    _write_raw_csv(
        path,
        ["SID", "Last Name", "First Name"],
        [
            ["9999990701", "A", "One"],
            ["9999990702", "Dupe", "X"],
            ["9999990702", "Dupe", "Y"],
        ],
    )

    prepare_roster(path)
    first_holds = _read_raw_csv(tmp_path / "999999_holds.csv")

    prepare_roster(path)
    second_holds = _read_raw_csv(tmp_path / "999999_holds.csv")

    assert first_holds == second_holds
