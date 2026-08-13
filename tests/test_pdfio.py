"""Regression tests for melredact.pdfio's pdfplumber-read-repair shim.

The real bug (data/PRT/010406_PD1_PRT.pdf, 2026-08-13) is a `pdfminer.six`
xref-parsing quirk on a file with bare `\r` line endings in its xref table
and trailer -- confirmed by direct inspection (see pdfio.py's module
docstring), but not practical to reproduce byte-for-byte in a small
synthetic fixture (a minimal pikepdf-written file with every `\n` swapped
for `\r` did not reproduce it; whatever real scanner/export tool wrote the
real file's xref table triggers a more specific parser path). These tests
instead exercise `open_pdf`'s actual, observable contract -- "pdfplumber
sees zero pages where pikepdf sees real ones -> fall back to a
pikepdf-resaved, disk-cached copy" -- by simulating that exact symptom via
monkeypatching, and separately confirm the shim is a no-op (no pikepdf
call, no cache write) for an ordinary, unaffected file.
"""

import pdfplumber
import pytest

from melredact import pdfio
from tests.make_fixture import build_main_fixture


@pytest.fixture(scope="module")
def main_fixture(tmp_path_factory):
    return build_main_fixture(tmp_path_factory.mktemp("pdfio_fixture"))


def test_open_pdf_matches_plain_pdfplumber_for_an_unaffected_file(main_fixture):
    with pdfio.open_pdf(main_fixture.pdf_path) as via_shim:
        n_shim = len(via_shim.pages)
    with pdfplumber.open(main_fixture.pdf_path) as direct:
        n_direct = len(direct.pages)
    assert n_shim == n_direct > 0


def test_open_pdf_does_not_touch_pikepdf_or_the_cache_for_an_unaffected_file(main_fixture, tmp_path, monkeypatch):
    cache_dir = tmp_path / "normalized_cache"
    monkeypatch.setattr(pdfio, "_NORMALIZED_DIR", cache_dir)
    with pdfio.open_pdf(main_fixture.pdf_path):
        pass
    assert not cache_dir.exists()


class _EmptyPagesStub:
    pages = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_open_pdf_repairs_a_file_pdfplumber_reads_as_zero_pages(main_fixture, tmp_path, monkeypatch):
    """Simulates the real symptom directly: pdfplumber.open(<affected path>)
    returns an object with zero pages, no exception -- exactly what was
    observed on the real file. open_pdf must detect this, fall back to a
    pikepdf-resaved copy under the (disk) cache, and return that copy's
    real pages instead."""
    cache_dir = tmp_path / "normalized_cache"
    monkeypatch.setattr(pdfio, "_NORMALIZED_DIR", cache_dir)

    real_open = pdfplumber.open
    affected_path = str(main_fixture.pdf_path)

    def fake_open(path, *args, **kwargs):
        if str(path) == affected_path:
            return _EmptyPagesStub()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(pdfio.pdfplumber, "open", fake_open)

    with pdfio.open_pdf(main_fixture.pdf_path) as pdf:
        assert len(pdf.pages) > 0  # recovered via the pikepdf-resaved copy, not the empty stub

    repaired = list(cache_dir.glob("*.pdf"))
    assert len(repaired) == 1


def test_repaired_copy_is_cached_by_content_hash_not_recomputed_every_call(main_fixture, tmp_path, monkeypatch):
    cache_dir = tmp_path / "normalized_cache"
    monkeypatch.setattr(pdfio, "_NORMALIZED_DIR", cache_dir)

    real_open = pdfplumber.open
    affected_path = str(main_fixture.pdf_path)

    def fake_open(path, *args, **kwargs):
        if str(path) == affected_path:
            return _EmptyPagesStub()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(pdfio.pdfplumber, "open", fake_open)

    with pdfio.open_pdf(main_fixture.pdf_path):
        pass
    first = {p: p.stat().st_mtime for p in cache_dir.glob("*.pdf")}

    with pdfio.open_pdf(main_fixture.pdf_path):
        pass
    second = {p: p.stat().st_mtime for p in cache_dir.glob("*.pdf")}

    assert first == second  # same file, untouched -- not resaved a second time
