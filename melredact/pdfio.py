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


def open_pdf(path: str | Path) -> pdfplumber.PDF:
    """Drop-in replacement for `pdfplumber.open(path)` everywhere this
    codebase opens a real, caller-supplied PDF -- see the module
    docstring for what this repairs and why. Returns a normal
    `pdfplumber.PDF` (a context manager, used the same way
    `pdfplumber.open(...)` already was at every call site this replaces),
    reading from a disk-cached, pikepdf-normalized copy only when the
    original actually needs it.
    """
    path = Path(path)
    if _needs_repair(path):
        path = _repaired_copy(path)
    return pdfplumber.open(path)
