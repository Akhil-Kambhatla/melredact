# data/

This directory holds real, identifiable inputs and is gitignored except for
this file. Nothing else in here should ever be committed.

Expected contents (local only), organized by subfolder:

- `MPR/` — scanned MPR worksheet PDFs downloaded from Box.
- `PRT/` — scanned PRT worksheet PDFs downloaded from Box.
- `teacher_codes/` — roster CSVs exported from the Google Sheet (`SID`,
  `Last Name`, `First Name`). A roster CSV may have an optional sidecar,
  `<teacher_code>_holds.csv` (`Last Name`, `First Name`, no SID column) --
  known-consented students whose SID couldn't be trusted in the export
  (see CLAUDE.md's "Held names" section). `scripts/prepare_roster.py`
  builds this sidecar from a corrupted export; both files are real,
  identifiable data and gitignored the same as everything else here.
- `samples/` — small sample/smoke-test PDFs (a handful of pages cut from a
  real scan), used for a faster local run than the full file.

## Do not sync `.cache/`

`review_app.py` and the pipeline cache rendered page images (both raw scans
and redacted previews) under `.cache/melredact/` at the repo root, not inside
this directory. That cache contains identifiable scanned worksheets. It is
gitignored, but git is not the only way data leaks: do not point Dropbox,
iCloud, a shared drive, or any other sync tool at the repo root or at that
cache directory. Treat it the same as `data/` itself.

`melredact/consensus.py` (the template-agnostic handwriting detector --
see CLAUDE.md) adds its own cache under `.cache/melredact/consensus/`,
keyed the same way the OCR cache above is (source file content hash, page,
dpi), plus the group's reference page and block size. Each cached file is
a per-page ink-density grid derived directly from a real rasterized scan --
not the raster image itself, but still identifiable in the same sense the
OCR word cache is, since it's positional handwriting-ink data. Already
covered by the blanket `.cache/` gitignore rule; called out here explicitly
so it isn't mistaken for a smaller, less-sensitive cache than it is.
