"""One-off cleanup for a roster CSV exported straight from the source
Google Sheet, before it can be loaded by melredact.roster.load_roster.

Three real problems this fixes, none of which load_roster should ever
paper over silently:

- The export can carry trailing columns from a colour legend in the
  source spreadsheet that have no header at all -- these are dropped.
- A name cell can carry a literal trailing punctuation mark (e.g. a
  spreadsheet formula artifact like "Matheuir?") -- stripped.
- A block of the sheet can have corrupted SID numbering: two different
  students sharing the same SID. There is no way to know which of the two
  (if either) actually owns that SID, so this never guesses or renumbers
  -- every row sharing a duplicated SID is moved out of the main CSV
  entirely, into <teacher_code>_holds.csv (see roster.py's Roster.
  held_names), dropping the untrustworthy SID column on the way. A human
  can still separately add a name to the holds file for a row this script
  wouldn't catch on its own (an SID that isn't itself duplicated but sits
  inside the same corrupted run) -- that's a judgment call, not something
  a duplicate-SID scan can find.

Blank period-block delimiter rows (see roster.py's module docstring) are
left exactly where they are -- they're never touched by any step here.

    python scripts/prepare_roster.py data/teacher_codes/010406.csv
"""

from __future__ import annotations

import csv
import string
import sys
from collections import Counter
from pathlib import Path

REQUIRED_COLUMNS = ["SID", "Last Name", "First Name"]
HOLDS_COLUMNS = ["Last Name", "First Name"]


def _strip_trailing_punct(value: str) -> str:
    return value.rstrip(string.punctuation)


def _is_blank_row(row: list[str]) -> bool:
    return not any((cell or "").strip() for cell in row[:3])


def _read_existing_holds(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        return [((row.get("Last Name") or "").strip(), (row.get("First Name") or "").strip()) for row in reader]


def prepare_roster(path: Path) -> None:
    teacher_code = path.stem
    holds_file = path.with_name(f"{teacher_code}_holds.csv")

    with path.open(newline="") as f:
        rows = list(csv.reader(f))

    if not rows:
        print(f"{path}: empty file, nothing to do")
        return

    header, data_rows = rows[0], rows[1:]

    keep_idx = list(range(min(len(header), len(REQUIRED_COLUMNS))))
    for i in range(len(REQUIRED_COLUMNS), len(header)):
        if header[i].strip():
            keep_idx.append(i)
        else:
            print(f"dropping trailing unlabeled column at index {i} (blank header)")

    def project(row: list[str]) -> list[str]:
        return [row[i] if i < len(row) else "" for i in keep_idx]

    header = project(header)
    if header[:3] != REQUIRED_COLUMNS:
        print(f"warning: expected header {REQUIRED_COLUMNS}, got {header[:3]!r} -- proceeding positionally")

    projected_rows = [project(r) for r in data_rows]

    cleaned_rows: list[list[str]] = []
    for row_num, row in enumerate(projected_rows, start=2):
        if _is_blank_row(row):
            cleaned_rows.append(row)
            continue
        sid, last, first = row[0].strip(), row[1], row[2]
        new_last = _strip_trailing_punct(last.strip())
        new_first = _strip_trailing_punct(first.strip())
        if new_last != last.strip():
            print(f"row {row_num}: stripped trailing punctuation in Last Name: {last.strip()!r} -> {new_last!r}")
        if new_first != first.strip():
            print(f"row {row_num}: stripped trailing punctuation in First Name: {first.strip()!r} -> {new_first!r}")
        cleaned_rows.append([sid, new_last, new_first])

    sid_counts = Counter(row[0] for row in cleaned_rows if not _is_blank_row(row))
    duplicated_sids = {sid for sid, count in sid_counts.items() if count > 1}

    main_rows: list[list[str]] = []
    new_holds: list[tuple[str, str]] = []
    for row in cleaned_rows:
        if not _is_blank_row(row) and row[0] in duplicated_sids:
            print(f"moving duplicate-SID row to holds: SID {row[0]}, {row[1]}, {row[2]}")
            new_holds.append((row[1], row[2]))
        else:
            main_rows.append(row)

    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(main_rows)
    print(f"wrote {path}: {len(main_rows)} row(s) (including any blank delimiter rows)")

    # Dedupe against what's already on disk (e.g. a name a human appended
    # by hand earlier) but never drop an existing entry -- this script only
    # ever adds to the holds file, never rewrites or removes from it.
    existing_holds = _read_existing_holds(holds_file)
    added_holds = [pair for pair in new_holds if pair not in existing_holds]
    all_holds = existing_holds + added_holds

    with holds_file.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HOLDS_COLUMNS)
        writer.writerows(all_holds)
    print(f"wrote {holds_file}: {len(all_holds)} held name(s) total ({len(added_holds)} added this run)")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/prepare_roster.py <roster.csv>", file=sys.stderr)
        return 1
    path = Path(argv[1])
    if not path.exists():
        print(f"roster CSV not found: {path}", file=sys.stderr)
        return 1
    prepare_roster(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
