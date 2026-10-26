"""Contract-level orchestration checks with explicit test doubles."""

from email import policy
from email.parser import BytesParser
from io import BytesIO

import pytest

from src.agent import Agent
from src.config import Config
from src.email_client import EmailClient
from src.models import DocumentError, DocumentType, DownloadResult, IncomingEmail, MatterMetadata
from src.packager import Packager
from src.uarb import UARBClient


class CapturingEmail(EmailClient):
    def __init__(self):
        self.replies = []
        self.config = Config(agent_email="agent@example.com")

    def send_reply(self, original, body, attachment=None):
        self.replies.append((original.thread_id, body, attachment))

    def send_delivery(self, original, parts):
        for part in parts:
            reply = BytesParser(policy=policy.default).parsebytes(part.raw)
            attachments = list(reply.iter_attachments())
            archive = BytesIO(attachments[0].get_payload(decode=True)) if attachments else None
            self.replies.append((original.thread_id, reply.get_body().get_content(), archive))


class FixtureUARB(UARBClient):
    def __init__(self, count=20):
        self.count = count
        self.limit = None

    def get_matter(self, matter_id):
        return MatterMetadata(matter_id, counts={DocumentType.EXHIBITS: self.count})

    def download_documents(self, matter_id, document_type, destination, *, limit=10):
        self.limit = limit
        path = destination / "a.pdf"
        path.write_bytes(b"%PDF-fixture")
        return DownloadResult((path,), (DocumentError("b.pdf", "failed"),))


class CapturingPackager(Packager):
    def __init__(self):
        self.files = ()

    def package(self, files, destination):
        self.files = files
        return super().package(files, destination)


def message(body):
    return IncomingEmail("id", "thread", "sender@example.com", "", body, "<id@example.com>")


def test_clarification_does_not_retrieve(tmp_path):
    email = CapturingEmail()
    agent = Agent(UARBClient(), Packager(), email)
    result = agent.process(message("M12205"), tmp_path)
    assert result.missing_fields == ("document_type",)
    assert email.replies[0][0] == "thread"
    assert email.replies[0][2] is None


def test_partial_download_reports_errors_and_caps_limit(tmp_path):
    email, uarb, packager = CapturingEmail(), FixtureUARB(), CapturingPackager()
    result = Agent(uarb, packager, email).process(message("M12205 exhibits"), tmp_path)
    assert uarb.limit == 10
    assert result.downloaded_count == 1
    assert result.available_count == 20
    assert len(packager.files) == 1
    assert packager.files[0].name == "a.pdf"
    assert packager.files[0].parent.parent == tmp_path
    assert not packager.files[0].parent.exists()
    assert "I couldn't retrieve 1 requested document" in email.replies[0][1]
    from zipfile import ZipFile

    with ZipFile(email.replies[0][2]) as archive:
        assert archive.namelist() == ["a.pdf"]


def test_zero_documents_does_not_package(tmp_path):
    email = CapturingEmail()
    result = Agent(FixtureUARB(0), Packager(), email).process(
        message("M12205 exhibits"),
        tmp_path,
    )
    assert result.downloaded_count == 0
    assert email.replies[0][2] is None


def test_summary_is_deterministic_and_uses_metadata():
    from datetime import date

    from src.agent import format_summary
    from src.models import ProcessingResult

    metadata = MatterMetadata(
        "M12205",
        title="A real matter title",
        category="Water",
        matter_type="Capital",
        status="Closed",
        outcome="Directions given",
        filing_dates={"Received": date(2025, 4, 7), "Decision": date(2025, 10, 23)},
        counts=dict(zip(DocumentType, (13, 6, 43, 0, 0), strict=True)),
    )
    result = ProcessingResult(
        10,
        43,
        metadata,
        (
            DocumentError("first.pdf", "unavailable"),
            DocumentError("second.pdf", "failed"),
        ),
    )
    body = format_summary(result, DocumentType.OTHER_DOCUMENTS, archive_attached=True)
    assert body.startswith("Hi,\n\nMatter M12205 concerns A real matter title.")
    for value in (
        "Capital in the Water category",
        "Its status is Closed",
        "Directions given",
        "decision was issued on October 23, 2025",
        "received on April 7, 2025",
        "no Transcripts",
        "no Recordings",
    ):
        assert value in body
    assert "I retrieved 10 of 43 Other Documents." in body
    assert "I couldn't retrieve 2 requested documents." in body
    assert "attached as a ZIP" in body
    assert body == format_summary(result, DocumentType.OTHER_DOCUMENTS, archive_attached=True)


@pytest.mark.parametrize(
    "available, downloaded, errors, expected",
    [
        (0, 0, (), "No files are available"),
        (6, 0, (DocumentError("a.pdf", "failed"),), "No files were downloaded"),
        (6, 0, (), "No files were downloaded"),
        (6, 1, (DocumentError("a.pdf", "failed"),), "I retrieved 1 of 6"),
    ],
)
def test_summary_zero_and_partial(available, downloaded, errors, expected):
    from src.agent import format_summary
    from src.models import ProcessingResult

    metadata = MatterMetadata("M12205", counts={DocumentType.KEY_DOCUMENTS: available})
    body = format_summary(
        ProcessingResult(downloaded, available, metadata, errors), DocumentType.KEY_DOCUMENTS
    )
    assert expected in body
    assert "No ZIP is attached" in body
    assert "is listed in the UARB database" in body
    assert "Counts for the remaining categories are unavailable" in body
    assert ("couldn't retrieve" in body) == bool(errors)


def test_agent_isolates_downloads_and_packages_only_successes(tmp_path):
    from zipfile import ZipFile

    class DownloadingClient(FixtureUARB):
        def download_documents(self, matter_id, document_type, destination, *, limit=10):
            self.destination = destination
            good = destination / "good.pdf"
            good.write_bytes(b"%PDF-content")
            (destination / "unfinished.crdownload").write_bytes(b"partial")
            return DownloadResult((good,), (DocumentError("bad.pdf", "unavailable"),))

    (tmp_path / "old.pdf").write_bytes(b"old request")
    email, client = CapturingEmail(), DownloadingClient()
    Agent(client, Packager(), email).process(message("M12205 exhibits"), tmp_path)
    attachment = email.replies[0][2]
    with ZipFile(attachment) as archive:
        assert archive.namelist() == ["good.pdf"]
        assert archive.read("good.pdf") == b"%PDF-content"
    assert not client.destination.exists()
    assert "I couldn't retrieve 1 requested document" in email.replies[0][1]
    assert "I've attached the retrieved document" in email.replies[0][1]


@pytest.mark.parametrize("available, failed", [(6, False), (0, False), (6, True)])
def test_zip_cli_packages_successes_or_reports_no_zip(
    tmp_path, monkeypatch, capsys, available, failed
):
    import sys
    from zipfile import ZipFile

    from src.main import main

    class CLIClient(FixtureUARB):
        def __init__(self, **kwargs):
            super().__init__(count=available)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def download_documents(self, matter_id, document_type, destination, *, limit=10):
            assert destination.parent == tmp_path
            assert destination != tmp_path
            if available == 0:
                return DownloadResult()
            source = destination / "first.pdf"
            source.write_bytes(b"%PDF-1.7\ncontent")
            (destination / "unfinished.crdownload").write_bytes(b"partial")
            errors = (DocumentError("second.pdf", "failed"),) if failed else ()
            return DownloadResult((source,), errors)

    monkeypatch.setattr("src.main.UARBClient", CLIClient)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "senpilot",
            "--matter",
            "M12205",
            "--type",
            "Exhibits",
            "--limit",
            "2",
            "--zip",
            "--output",
            str(tmp_path),
        ],
    )
    if failed:
        with pytest.raises(SystemExit) as exit_info:
            main()
        assert exit_info.value.code == 1
    else:
        main()
    output = capsys.readouterr().out
    archives = list(tmp_path.glob("*.zip"))
    assert not [path for path in tmp_path.iterdir() if path.is_dir()]
    if available:
        assert len(archives) == 1
        with ZipFile(archives[0]) as archive:
            assert archive.namelist() == ["first.pdf"]
        assert "I retrieved 1 of 6 Exhibits" in output
        assert f"ZIP: {archives[0]}" in output
    else:
        assert not archives
        assert "No files are available" in output
        assert "ZIP: none" in output
    assert ("I couldn't retrieve 1 requested document" in output) == failed


def test_sigterm_unwinds_cleanup_and_restores_previous_handler(tmp_path):
    import signal
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from src.main import _graceful_termination

    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt), _graceful_termination():
        with TemporaryDirectory(dir=tmp_path) as workspace:
            path = Path(workspace)
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
    assert not path.exists()
    assert signal.getsignal(signal.SIGTERM) == previous


def test_uv_forwards_sigterm_to_python_cleanup():
    import selectors
    import shutil
    import subprocess

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv executable not available")
    script = """
import signal
from src.main import _graceful_termination
try:
    with _graceful_termination():
        try:
            print('ready', flush=True)
            signal.pause()
        finally:
            print('cleanup', flush=True)
except KeyboardInterrupt:
    pass
"""
    child = subprocess.Popen(
        [uv, "run", "--no-sync", "python", "-u", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=10), "Signal-test child did not start"
        assert child.stdout.readline().strip() == "ready"
        child.terminate()
        output, _ = child.communicate(timeout=10)
        assert "cleanup" in output
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def test_summary_does_not_confuse_final_filing_with_decision_or_invent_values():
    from datetime import date

    from src.agent import format_summary
    from src.models import ProcessingResult

    metadata = MatterMetadata(
        "M54321",
        title="Project valued at $42,000",
        filing_dates={"Final Filing Date": date(2025, 1, 8)},
        counts={DocumentType.EXHIBITS: 2},
    )
    body = format_summary(ProcessingResult(1, 2, metadata), DocumentType.EXHIBITS)
    assert "concerns Project valued at $42,000." in body
    assert "final filing date is January 8, 2025" in body
    assert "decision" not in body.lower()
    assert "status" not in body.lower()
    assert "Counts for the remaining categories are unavailable" in body
    assert "No ZIP is attached" in body
    assert "attached as" not in body
