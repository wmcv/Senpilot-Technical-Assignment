"""ZIP verification, failure cleanup, and safe archive member names."""

from zipfile import ZipFile

import pytest

from src.packager import Packager


def pdf(directory, name, content=b"%PDF-1.7\ncontent"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return path


def test_multiple_pdfs_preserve_names_and_contents(tmp_path):
    files = (pdf(tmp_path, "97289.pdf"), pdf(tmp_path, "H-2.pdf", b"%PDF-1.7\nsecond"))
    pdf(tmp_path, "unrelated.pdf")
    destination = tmp_path / "result.zip"
    assert Packager().package(files, destination) == destination
    with ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == [source.name for source in files]
        for source in files:
            assert archive.read(source.name) == source.read_bytes()


def test_duplicate_names_reserve_other_original_names(tmp_path):
    files = (
        pdf(tmp_path / "a", "file.pdf", b"%PDF-first"),
        pdf(tmp_path / "b", "file.pdf", b"%PDF-second"),
        pdf(tmp_path / "c", "file (1).pdf", b"%PDF-third"),
    )
    result = Packager().package(files, tmp_path / "result.zip")
    with ZipFile(result) as archive:
        assert archive.namelist() == ["file.pdf", "file (2).pdf", "file (1).pdf"]
        assert [archive.read(name) for name in archive.namelist()] == [
            p.read_bytes() for p in files
        ]


@pytest.mark.parametrize("kind", ["missing", "empty", "directory", "partial"])
def test_invalid_sources_do_not_publish_archive(tmp_path, kind):
    valid = pdf(tmp_path, "valid.pdf")
    source = tmp_path / "invalid.pdf"
    if kind == "empty":
        source.touch()
    elif kind == "directory":
        source.mkdir()
    elif kind == "partial":
        source = pdf(tmp_path, "invalid.crdownload")
    with pytest.raises((ValueError, FileNotFoundError)):
        Packager().package((valid, source), tmp_path / "result.zip")
    assert not (tmp_path / "result.zip").exists()
    assert not list(tmp_path.glob(".zip-*"))


def test_empty_input_is_explicit_without_empty_zip(tmp_path):
    with pytest.raises(ValueError, match="zero files"):
        Packager().package((), tmp_path / "result.zip")
    assert not list(tmp_path.iterdir())


def test_existing_archive_is_not_overwritten(tmp_path):
    source = pdf(tmp_path, "source.pdf")
    destination = tmp_path / "result.zip"
    destination.write_bytes(b"existing archive")
    with pytest.raises(FileExistsError):
        Packager().package((source,), destination)
    assert destination.read_bytes() == b"existing archive"
    assert not list(tmp_path.glob(".zip-*"))


def test_integrity_failure_prevents_publication(tmp_path, monkeypatch):
    source = pdf(tmp_path, "source.pdf")
    monkeypatch.setattr(ZipFile, "testzip", lambda self: "source.pdf")
    with pytest.raises(ValueError, match="integrity"):
        Packager().package((source,), tmp_path / "result.zip")
    assert not (tmp_path / "result.zip").exists()
    assert not list(tmp_path.glob(".zip-*"))


def test_changed_source_is_detected(tmp_path, monkeypatch):
    source = pdf(tmp_path, "source.pdf")
    original = ZipFile.write

    def changed_write(self, filename, *args, **kwargs):
        source.write_bytes(b"changed during packaging")
        return original(self, filename, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "write", changed_write)
    with pytest.raises(ValueError, match="content verification"):
        Packager().package((source,), tmp_path / "result.zip")
    assert not (tmp_path / "result.zip").exists()
    assert not list(tmp_path.glob(".zip-*"))
