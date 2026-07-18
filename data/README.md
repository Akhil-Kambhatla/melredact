# data/

This directory holds real, identifiable inputs and is gitignored except for
this file. Nothing else in here should ever be committed.

Expected contents (local only):

- Scanned PDFs downloaded from Box, one per teacher/period/assessment.
- Roster CSVs exported from the Google Sheet (`SID`, `Last Name`, `First Name`).

## Do not sync `.cache/`

`review_app.py` and the pipeline cache rendered page images (both raw scans
and redacted previews) under `.cache/melredact/` at the repo root, not inside
this directory. That cache contains identifiable scanned worksheets. It is
gitignored, but git is not the only way data leaks: do not point Dropbox,
iCloud, a shared drive, or any other sync tool at the repo root or at that
cache directory. Treat it the same as `data/` itself.
