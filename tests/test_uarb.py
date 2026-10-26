"""Offline download handling; only the opt-in test contacts the public website."""

import os
from pathlib import Path

import pytest

from src.models import DocumentType, MatterMetadata
from src.uarb import UARBClient, _save_download


class Download:
    def __init__(self, name="document.pdf", data=b"%PDF-1.7\ncontent", failure=None):
        self.suggested_filename, self.data, self.error = name, data, failure

    def save_as(self, path):
        Path(path).write_bytes(self.data)

    def failure(self):
        return self.error


def test_preserve_names_and_collisions(tmp_path):
    first = _save_download(Download("H-1.pdf"), tmp_path)
    second = _save_download(Download("H-1.pdf", b"%PDF-2.0\nsecond"), tmp_path)
    third = _save_download(Download("H-1.pdf"), tmp_path)
    assert [p.name for p in (first, second, third)] == ["H-1.pdf", "H-1 (1).pdf", "H-1 (2).pdf"]
    assert first.read_bytes() == b"%PDF-1.7\ncontent"
    assert second.read_bytes() == b"%PDF-2.0\nsecond"
    assert len(list(tmp_path.iterdir())) == 3


@pytest.mark.parametrize(
    "download",
    [
        Download(data=b""),
        Download(data=b"<html>error</html>"),
        Download(failure="cancelled"),
        Download("../escape.pdf"),
        Download("C:\\escape.pdf"),
        Download(""),
    ],
)
def test_invalid_download_is_not_published(tmp_path, download):
    with pytest.raises((ValueError, OSError)):
        _save_download(download, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_non_pdf_is_nonempty_but_not_pdf_validated(tmp_path):
    assert (
        _save_download(Download("recording.mp3", b"ID3audio"), tmp_path).read_bytes() == b"ID3audio"
    )


class LoopClient(UARBClient):
    def __init__(self, failures=(), count=15):
        super().__init__()
        self._metadata = MatterMetadata("M12205", counts={DocumentType.EXHIBITS: count})
        self.failures = set(failures)
        self.attempts = []
        self.selected = []

    def _select_category(self, category, count):
        self.selected.append(category)

    def _row(self, index, count):
        return self.Row(index)

    class Row:
        def __init__(self, index):
            self.index = index

        def inner_text(self):
            return f"document {self.index}"

    def _download_one(self, row, destination):
        self.attempts.append(row.index)
        attempt = self.attempts.count(row.index)
        if (row.index, attempt) in self.failures:
            raise OSError("download interrupted")
        return _save_download(Download(f"{row.index}.pdf"), destination)


def test_retry_once_then_continue_until_success_limit(tmp_path):
    client = LoopClient(failures={(1, 1), (1, 2), (2, 1)})
    result = client.download_documents("M12205", DocumentType.EXHIBITS, tmp_path, limit=2)
    assert client.attempts == [1, 1, 2, 2, 3]
    assert [p.name for p in result.files] == ["2.pdf", "3.pdf"]
    assert len(result.errors) == 1
    assert result.errors[0].document == "document 1"
    assert "interrupted" in result.errors[0].error
    assert client.selected == [DocumentType.EXHIBITS]


def test_default_limit_is_ten_successes(tmp_path):
    client = LoopClient(failures={(1, 1), (1, 2)})
    result = client.download_documents("M12205", DocumentType.EXHIBITS, tmp_path)
    assert len(result.files) == 10
    assert client.attempts[-1] == 11


def test_exhaustion_and_all_failures(tmp_path):
    client = LoopClient(failures={(1, 1), (1, 2), (2, 1), (2, 2)}, count=2)
    result = client.download_documents("M12205", DocumentType.EXHIBITS, tmp_path)
    assert not result.files
    assert len(result.errors) == 2
    assert client.attempts == [1, 1, 2, 2]


def test_empty_category_or_zero_limit_does_not_open_browser(tmp_path):
    client = LoopClient(count=0)
    assert client.download_documents("M12205", DocumentType.EXHIBITS, tmp_path).files == ()
    assert not client.selected
    with UARBClient() as client:
        assert not client.download_documents(
            "M12205", DocumentType.EXHIBITS, tmp_path, limit=0
        ).files
        assert client._page is None


@pytest.mark.parametrize("limit", [-1, 11, True, 1.5])
def test_invalid_limit_is_offline(tmp_path, limit):
    with UARBClient() as client, pytest.raises(ValueError):
        client.download_documents("M12205", DocumentType.EXHIBITS, tmp_path, limit=limit)


def test_invalid_request_is_offline(tmp_path):
    with UARBClient() as client:
        with pytest.raises(ValueError):
            client.get_matter("M123")
        with pytest.raises(ValueError):
            client.download_documents("M12205", "Exhibits", tmp_path)
        assert client._page is None


@pytest.mark.skipif(os.getenv("UARB_LIVE") != "1", reason="set UARB_LIVE=1 for public-site test")
def test_live_m12205_key_document(tmp_path):
    with UARBClient() as client:
        metadata = client.get_matter("M12205")
        assert metadata.title
        assert set(metadata.counts) == set(DocumentType)
        result = client.download_documents("M12205", DocumentType.KEY_DOCUMENTS, tmp_path, limit=1)
        assert not result.errors
        assert len(result.files) == 1
        assert result.files[0].read_bytes().startswith(b"%PDF-")
