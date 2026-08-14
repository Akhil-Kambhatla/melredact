"""Read-repair shim for a real PDF-reading incompatibility between this
codebase's two PDF libraries.

Found 2026-08-13 processing a real scan, `data/PRT/010406_PD1_PRT.pdf`:
`pdfplumber.open(path).pages` came back `[]` -- zero pages, no exception
raised -- for a file `pikepdf` (a different, qpdf-based library) opens
without complaint, reporting 92 pages under a completely ordinary page
tree (`/Root` -> `/Pages` -> 92 `/Kids`, each a normal `/Type /Page`
dict). Traced to the file's xref table and trailer using bare `\r`
(old-Mac-style) line endings instead of `\n`/`\r\n`: both are valid PDF
EOL conventions, but `pdfminer.six` (what `pdfplumber` wraps) mis-tokenizes
the `\r`-delimited xref subsection, shifting every parsed object number by
one -- confirmed directly: its own xref offsets dict came back keyed
2..280 for a 280-object file (should be 0..279), so `Root 278 0 R` in the
trailer resolves to whatever object 278's *shifted* slot actually points
at, not the real catalog, and `doc.catalog` (and therefore
`PDFPage.create_pages`) comes back empty.

This is a library compatibility gap, not a data-integrity problem --
qpdf's own structural read of the file succeeds cleanly, so the PDF
itself is well-formed, just written with a valid-but-unusual EOL choice
one of our two readers doesn't handle. That's a different class of
problem than everything else this codebase treats as "fail loudly,
abstain, never guess" (see CLAUDE.md's Working preferences): there is
nothing ambiguous about this file's actual content to guess about, only
a parser bug to route around. `open_pdf` does that losslessly and
deterministically: `pikepdf` (which already reads the file correctly)
re-saves it, which normalizes the xref/trailer into a form `pdfminer.six`
handles, with no change to any page's content, image data, or metadata.
The repaired copy is disk-cached by the *source* file's own content hash
(same pattern `melredact.ocr` already uses, for the same reason: pay the
one-time resave cost once per distinct input file, not once per call),
under `CACHE_DIR/normalized/` -- gitignored the same as the rest of
`CACHE_DIR`, since a repaired copy of a real scan is exactly as
identifiable as the scan itself.

Every module that opens a caller-supplied *source* PDF (a scan on disk,
never this codebase's own already-pikepdf-written output in `out/` or a
manual-queue draft, which never has this problem in the first place)
calls `open_pdf` instead of `pdfplumber.open` directly, so the fix
applies uniformly rather than needing to be remembered at each call site.
`open_pdf` is cheap to call even on a file that was never affected: the
affected-or-not check is one `pdfplumber.open` pass (the same cost the
plain call already paid), and an unaffected file never touches `pikepdf`
or the cache at all.

**Chained with page-orientation normalization, 2026-08-14 (see
`melredact/orientation.py`).** `open_pdf` resolves the xref/trailer repair
first (this module's own job, unchanged), then hands that resolved path to
`orientation.normalize_pdf` before ever calling `pdfplumber.open` on it --
so every caller of `open_pdf` sees a file where every confidently-
classifiable page is already upright, with no rotation-awareness of its
own needed anywhere downstream. `resolved_source_path` is the public split
of the repair step alone, used by `orientation.orientation_for` so a
caller with only the *original* source path in scope (e.g.
`segment.segment_pdf`, which never opens the file directly) can look up
the identical, already-cached per-page orientation result `open_pdf`
itself produced when it opened the same file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pdfplumber
import pikepdf

from melredact.config import CACHE_DIR

_NORMALIZED_DIR = Path(CACHE_DIR) / "normalized"


def _content_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _needs_repair(path: Path) -> bool:
    with pdfplumber.open(path) as pdf:
        return len(pdf.pages) == 0


def _repaired_copy(path: Path) -> Path:
    digest = _content_hash(path)
    out_path = _NORMALIZED_DIR / f"{digest}.pdf"
    if not out_path.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with pikepdf.open(path) as pdf:
            pdf.save(out_path)
    return out_path


def resolved_source_path(path: str | Path) -> Path:
    """The concrete file `open_pdf` would actually read from -- the
    xref/trailer-repaired copy when `path` needs it, else `path` itself
    unchanged. Public so `orientation.orientation_for` can look up the
    same, already-cached per-page orientation result `open_pdf` produced
    for this exact file, without a caller that only has the *original*
    path (segment.segment_pdf) needing to re-derive the repair step."""
    path = Path(path)
    if _needs_repair(path):
        return _repaired_copy(path)
    return path


def open_pdf(path: str | Path, *, orientation_overrides: dict[int, int] | None = None) -> pdfplumber.PDF:
    """Drop-in replacement for `pdfplumber.open(path)` everywhere this
    codebase opens a real, caller-supplied PDF -- see the module
    docstring for what this repairs and why. Returns a normal
    `pdfplumber.PDF` (a context manager, used the same way
    `pdfplumber.open(...)` already was at every call site this replaces),
    reading from a disk-cached, pikepdf-normalized copy only when the
    original actually needs it -- and, as of 2026-08-14, also chained
    through `orientation.normalize_pdf` so every page is upright before a
    single downstream module ever sees it (see module docstring).

    `orientation_overrides` (page_index -> 0/90/180/270) is a human's
    explicit per-page rotation choice -- see `orientation.py`'s
    detect-and-ask design. Left as None (the default, and what every call
    site not explicitly reviewer-facing still passes), a page whose
    rotation the classifier can't confidently apply on its own stays
    exactly as found rather than being guessed -- this parameter is the
    only way a human's confirmation or correction actually reaches the
    physical file every downstream module reads.
    """
    from melredact.orientation import normalize_pdf

    resolved = resolved_source_path(path)
    result = normalize_pdf(resolved, overrides=orientation_overrides)
    return pdfplumber.open(result.normalized_path)
