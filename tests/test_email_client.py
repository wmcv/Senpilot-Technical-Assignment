"""Mocked Gmail/OAuth and delivery-state checks. No external services are used."""

import base64
import sqlite3
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import Mock

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from src.agent import Agent, run_polling
from src.config import Config
from src.email_client import (
    AlreadyHandled,
    AttachmentTooLarge,
    AuthenticationRequired,
    DeliveryUncertain,
    EmailClient,
    GmailError,
    MessageStore,
    _read,
    build_reply,
    parse_message,
)
from src.models import DocumentType, IncomingEmail, MatterMetadata
from src.packager import Packager


@pytest.fixture
def config(tmp_path):
    credentials = tmp_path / "oauth.json"
    credentials.write_text("{}")
    return Config(
        agent_email="agent@example.com",
        google_oauth_path=credentials,
        gmail_token_path=tmp_path / "token.json",
    )


@pytest.fixture
def service():
    service = Mock()
    service.users().getProfile().execute.return_value = {"emailAddress": "agent@example.com"}
    service.users().messages().list().execute.return_value = {}
    service.users().messages().send().execute.return_value = {"id": "reply-id"}
    return service


@pytest.fixture
def client(config, service, tmp_path):
    return EmailClient(config, service=service, state_path=tmp_path / "state.sqlite3")


def incoming():
    return IncomingEmail(
        "message-1",
        "thread-1",
        "Some Sender <sender@example.net>",
        "M12205 Exhibits",
        "Please send exhibits",
        "<original@example.net>",
        "<ancestor@example.net>",
    )


def gmail_message(
    *, sender="Sender <sender@example.net>", html=False, labels=(), date=9999999999999
):
    message = EmailMessage()
    message["From"], message["Subject"] = sender, "M12205 Key Documents"
    message["Message-ID"] = "<original@example.net>"
    message["Reply-To"] = "reply@example.net"
    message.set_content(
        "<p>Other <b>Documents</b></p><script>ignore</script>"
        if html
        else "Please send Key Documents for M12205",
        subtype="html" if html else "plain",
    )
    return {
        "id": "message-1",
        "threadId": "thread-1",
        "labelIds": list(labels),
        "internalDate": str(date),
        "raw": base64.urlsafe_b64encode(message.as_bytes()).decode(),
    }


def test_parse_plain_and_headers():
    parsed = parse_message(gmail_message(), "agent@example.com")
    assert parsed.message_id == "message-1"
    assert parsed.thread_id == "thread-1"
    assert parsed.rfc_message_id == "<original@example.net>"
    assert parsed.reply_to == "reply@example.net"
    assert "Key Documents for M12205" in parsed.body


def test_parse_html_only():
    parsed = parse_message(gmail_message(html=True), "agent@example.com")
    assert "Other Documents" in parsed.body
    assert "ignore" not in parsed.body
    assert "<b>" not in parsed.body


def test_multipart_prefers_plain_text():
    data = gmail_message()
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(data["raw"]))
    message.add_alternative("<p>HTML alternative</p>", subtype="html")
    message.add_attachment(b"ignored", maintype="text", subtype="plain", filename="attachment.txt")
    data["raw"] = base64.urlsafe_b64encode(message.as_bytes()).decode()
    assert "Key Documents for M12205" in parse_message(data, "agent@example.com").body
    assert "ignored" not in parse_message(data, "agent@example.com").body


@pytest.mark.parametrize(
    "labels,sender",
    [
        (("SENT",), "sender@example.net"),
        (("DRAFT",), "sender@example.net"),
        ((), "Agent <AGENT@example.com>"),
    ],
)
def test_skip_sent_drafts_and_self(labels, sender):
    assert parse_message(gmail_message(sender=sender, labels=labels), "agent@example.com") is None


def test_skip_automatic_reply():
    data = gmail_message()
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(data["raw"]))
    message["Auto-Submitted"] = "auto-replied"
    data["raw"] = base64.urlsafe_b64encode(message.as_bytes()).decode()
    assert parse_message(data, "agent@example.com") is None


def test_mime_threading_and_exact_zip_bytes(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-content")
    archive = Packager().package((source,), tmp_path / "files.zip")
    original = incoming()
    message = build_reply(original, "Summary", "agent@example.com", archive)
    decoded = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
    assert decoded["To"] == "sender@example.net"
    assert decoded["In-Reply-To"] == original.rfc_message_id
    assert decoded["References"] == "<ancestor@example.net> <original@example.net>"
    assert decoded["Subject"] == "Re: M12205 Exhibits"
    attachments = list(decoded.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "application/zip"
    assert attachments[0].get_payload(decode=True) == archive.read_bytes()
    assert (
        message["Message-ID"] == build_reply(original, "Summary", "agent@example.com")["Message-ID"]
    )


def test_reply_to_used_and_missing_thread_header_fails():
    original = IncomingEmail(
        "id",
        "thread",
        "sender@example.net",
        "Re: Request",
        "",
        "<original@example.net>",
        reply_to="reply@example.org",
    )
    message = build_reply(original, "body", "agent@example.com")
    assert message["To"] == "reply@example.org"
    assert message["Subject"] == "Re: Request"
    with pytest.raises(GmailError, match="Message-ID"):
        build_reply(
            IncomingEmail("id", "thread", "sender@example.net", "", ""), "", "agent@example.com"
        )


def test_attachment_size_before_send(client, tmp_path, monkeypatch):
    monkeypatch.setattr("src.email_client.MAX_ATTACHMENT_BYTES", 3)
    attachment = tmp_path / "big.zip"
    attachment.write_bytes(b"too big")
    with pytest.raises(AttachmentTooLarge):
        client.send_reply(incoming(), "Summary", attachment)
    assert client.store.status("message-1") is None
    # Mock setup created one call; send itself must not be invoked.
    assert client._service.users().messages().send.call_count == 1


def test_encoded_message_size_limit(monkeypatch):
    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", 3)
    with pytest.raises(AttachmentTooLarge):
        build_reply(incoming(), "Summary", "agent@example.com")


def test_success_marks_processed_and_suppresses_restart_duplicate(client, config, service):
    client.send_reply(incoming(), "Summary")
    assert client.store.status("message-1") == "processed"
    payload = service.users().messages().send.call_args.kwargs["body"]
    assert payload["threadId"] == "thread-1"
    restarted = EmailClient(config, service=service, state_path=client.state_path)
    with pytest.raises(AlreadyHandled):
        restarted.send_reply(incoming(), "Summary")


@pytest.mark.parametrize("failure", ["network", "state"])
def test_uncertain_send_is_never_automatically_resent(
    client, config, service, monkeypatch, failure
):
    if failure == "network":
        service.users().messages().send().execute.side_effect = OSError("private transport details")
    else:
        monkeypatch.setattr(
            client.store, "complete", Mock(side_effect=sqlite3.OperationalError("disk"))
        )
    with pytest.raises(DeliveryUncertain) as error:
        client.send_reply(incoming(), "Summary")
    assert "private" not in str(error.value)
    restarted = EmailClient(config, service=service, state_path=client.state_path)
    assert restarted.store.status("message-1") == "sending"
    with pytest.raises(AlreadyHandled):
        restarted.send_reply(incoming(), "Summary")


def test_definite_send_rejection_is_not_processed(client, service):
    service.users().messages().send().execute.side_effect = HttpError(
        httplib2.Response({"status": "403"}),
        b"private api details",
    )
    with pytest.raises(GmailError, match="HTTP 403") as error:
        client.send_reply(incoming(), "Summary")
    assert "private" not in str(error.value)
    assert client.store.status("message-1") is None


def test_receive_cutoff_pagination_and_duplicate_suppression(client, service):
    api = service.users().messages()
    cutoff = client.store.cutoff_ms
    old = {**gmail_message(date=cutoff - 1), "id": "old"}
    current = gmail_message(date=cutoff + 1)
    api.list().execute.side_effect = [
        {"messages": [{"id": "old"}], "nextPageToken": "page-2"},
        {"messages": [{"id": "message-1"}]},
    ]
    api.get().execute.side_effect = [old, current]
    assert len(client.receive()) == 1
    assert api.list.call_args.kwargs["pageToken"] == "page-2"
    page_calls = [call for call in api.list.call_args_list if "pageToken" in call.kwargs]
    assert len(page_calls) == 2
    assert all(call.kwargs["includeSpamTrash"] is True for call in page_calls)
    client.store.claim("message-1")
    api.list().execute.side_effect = None
    api.list().execute.return_value = {"messages": [{"id": "message-1"}]}
    assert client.receive() == []


@pytest.mark.parametrize(
    "labels,eligible",
    [
        (("INBOX",), True),
        (("SPAM",), True),
        ((), True),  # Archived incoming mail remains eligible too.
        (("TRASH",), False),
        (("SPAM", "TRASH"), False),
        (("INBOX", "TRASH"), False),
    ],
)
def test_receive_inbox_spam_and_trash_filters(client, service, labels, eligible):
    api = service.users().messages()
    cutoff = client.store.cutoff_ms
    api.list().execute.return_value = {"messages": [{"id": "message-1"}]}
    api.get().execute.return_value = gmail_message(labels=labels, date=cutoff + 1)
    messages = client.receive()
    assert [message.message_id for message in messages] == (["message-1"] if eligible else [])
    arguments = api.list.call_args.kwargs
    assert arguments["includeSpamTrash"] is True
    assert arguments["q"] == f"after:{cutoff // 1000 - 1} -in:sent -in:drafts -in:trash"
    assert "labelIds" not in arguments
    assert client.store.status("message-1") is None


def test_cutoff_survives_restart_and_store_is_private(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = MessageStore(path, "agent@example.com")
    assert MessageStore(path, "agent@example.com").cutoff_ms == first.cutoff_ms
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(GmailError, match="different mailbox"):
        MessageStore(path, "different@example.com")


def test_bounded_read_retries_and_sanitized_errors(monkeypatch):
    request = Mock()
    request.execute.side_effect = [OSError("private"), OSError("private"), {"ok": True}]
    pauses = Mock()
    monkeypatch.setattr("src.email_client.time.sleep", pauses)
    assert _read(request) == {"ok": True}
    assert request.execute.call_count == 3
    assert [call.args[0] for call in pauses.call_args_list] == [1, 2]
    request.execute.side_effect = OSError("private")
    with pytest.raises(GmailError) as error:
        _read(request)
    assert "private" not in str(error.value)


def test_auth_requires_manual_authorization(config, tmp_path):
    email = EmailClient(config, state_path=tmp_path / "state.sqlite3")
    with pytest.raises(AuthenticationRequired, match="gmail-auth"):
        email.authenticate()


def test_interactive_oauth_and_token_permissions(config, service, tmp_path, monkeypatch):
    credentials = Mock(valid=True)
    credentials.to_json.return_value = '{"mock_token": true}'
    flow = Mock()
    flow.run_local_server.return_value = credentials
    factory = Mock(return_value=flow)
    monkeypatch.setattr("src.email_client.InstalledAppFlow.from_client_secrets_file", factory)
    monkeypatch.setattr("src.email_client.build", Mock(return_value=service))
    email = EmailClient(config, state_path=tmp_path / "state.sqlite3")
    assert email.authenticate(interactive=True) == "agent@example.com"
    assert flow.run_local_server.call_args.kwargs["login_hint"] == "agent@example.com"
    assert config.gmail_token_path.stat().st_mode & 0o777 == 0o600
    assert email.store.cutoff_ms


def test_oauth_wrong_mailbox_never_saves_token(config, service, tmp_path, monkeypatch):
    service.users().getProfile().execute.return_value = {"emailAddress": "wrong@example.com"}
    flow = Mock()
    flow.run_local_server.return_value = Mock(valid=True)
    monkeypatch.setattr(
        "src.email_client.InstalledAppFlow.from_client_secrets_file", Mock(return_value=flow)
    )
    monkeypatch.setattr("src.email_client.build", Mock(return_value=service))
    email = EmailClient(config, state_path=tmp_path / "state.sqlite3")
    with pytest.raises(AuthenticationRequired, match="does not match"):
        email.authenticate(interactive=True)
    assert not config.gmail_token_path.exists()


def test_token_reuse_refresh_and_revocation(config, service, tmp_path, monkeypatch):
    config.gmail_token_path.write_text("{}")
    credentials = Mock(valid=True, expired=True, refresh_token="mock")
    credentials.has_scopes.return_value = True
    credentials.to_json.return_value = "{}"
    monkeypatch.setattr(
        "src.email_client.Credentials.from_authorized_user_file", Mock(return_value=credentials)
    )
    monkeypatch.setattr("src.email_client.build", Mock(return_value=service))
    email = EmailClient(config, state_path=tmp_path / "state.sqlite3")
    email.authenticate()
    credentials.refresh.assert_called_once()
    credentials.refresh.side_effect = RefreshError("revoked")
    with pytest.raises(AuthenticationRequired):
        EmailClient(config, state_path=email.state_path).authenticate()


class EmptyMatter:
    def get_matter(self, matter_id):
        return MatterMetadata(matter_id, title="Test matter", counts={DocumentType.EXHIBITS: 0})


def test_worker_processes_once_and_cleans_request_files(client, service, tmp_path):
    api = service.users().messages()
    api.list().execute.return_value = {"messages": [{"id": "message-1"}]}
    data = gmail_message(date=client.store.cutoff_ms + 1)

    # Fixture subject/body requests Key Documents; use an empty category for that type.
    class Matter:
        def get_matter(self, matter_id):
            return MatterMetadata(matter_id, counts={DocumentType.KEY_DOCUMENTS: 0})

    api.get().execute.return_value = data
    work = tmp_path / "requests"
    agent = Agent(Matter(), Packager(), client)
    assert run_polling(agent, work, once=True) == 1
    assert client.store.status("message-1") == "processed"
    assert run_polling(agent, work, once=True) == 0
    assert list(work.iterdir()) == []
    payload = api.send.call_args.kwargs["body"]
    reply = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(payload["raw"]))
    assert "No files are available" in reply.get_body().get_content()
    assert not list(reply.iter_attachments())


@pytest.mark.parametrize(
    "body, expected", [("M12205", "document type"), ("Exhibits", "matter number")]
)
def test_incomplete_request_gets_specific_clarification(client, service, tmp_path, body, expected):
    original = IncomingEmail(
        "message-1", "thread-1", "sender@example.net", "", body, "<original@example.net>"
    )
    Agent(EmptyMatter(), Packager(), client).process(original, tmp_path)
    payload = service.users().messages().send.call_args.kwargs["body"]
    reply = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(payload["raw"]))
    assert expected in reply.get_body().get_content()
    assert client.store.status("message-1") == "processed"


def test_not_found_sends_safe_failure_response(client, service, tmp_path):
    matter = Mock()
    matter.get_matter.side_effect = LookupError("private details")
    Agent(matter, Packager(), client).process(incoming(), tmp_path)
    raw = service.users().messages().send.call_args.kwargs["body"]["raw"]
    reply = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
    assert "could not find matter M12205" in reply.get_body().get_content()
    assert "private" not in reply.get_body().get_content()


def test_send_failure_does_not_send_second_generic_error(client, service, tmp_path):
    api = service.users().messages()
    api.send.reset_mock()
    api.send().execute.side_effect = OSError("lost response")
    api.send.reset_mock()
    with pytest.raises(DeliveryUncertain):
        Agent(EmptyMatter(), Packager(), client).process(incoming(), tmp_path)
    assert api.send.call_count == 1


def test_worker_lock_refuses_overlap(client, tmp_path):
    import fcntl

    lock = client.state_path.with_suffix(".lock")
    with lock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(GmailError, match="Another Gmail worker"):
            run_polling(Agent(EmptyMatter(), Packager(), client), tmp_path / "work", once=True)


@pytest.mark.parametrize("oversized", [False, True])
def test_complete_mocked_workflow_preserves_zip_and_failures(
    client, service, tmp_path, monkeypatch, oversized
):
    from io import BytesIO
    from zipfile import ZipFile

    from src.models import DocumentError, DownloadResult

    class PartialMatter:
        def get_matter(self, matter_id):
            return MatterMetadata(
                matter_id,
                title="Fixture title",
                status="Closed",
                counts={DocumentType.KEY_DOCUMENTS: 5},
            )

        def download_documents(self, matter_id, document_type, destination, *, limit=10):
            good = destination / "original.pdf"
            good.write_bytes(b"%PDF-1.7\nverified fixture")
            return DownloadResult((good,), (DocumentError("bad.pdf", "unavailable"),))

    class VerifyingPackager(Packager):
        def package(self, files, destination):
            path = super().package(files, destination)
            self.verified = path.read_bytes()
            return path

    if oversized:
        monkeypatch.setattr("src.email_client.MAX_ATTACHMENT_BYTES", 3)
    packager = VerifyingPackager()
    api = service.users().messages()
    api.list().execute.return_value = {"messages": [{"id": "message-1"}]}
    api.get().execute.return_value = gmail_message(date=client.store.cutoff_ms + 1)
    work = tmp_path / "work"
    assert run_polling(Agent(PartialMatter(), packager, client), work, once=True) == 1
    reply = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(
            api.send.call_args.kwargs["body"]["raw"],
        )
    )
    body = reply.get_body().get_content()
    assert "I retrieved 1 of 5 Key Documents" in body
    assert "I couldn't retrieve 1 requested document" in body
    attachments = list(reply.iter_attachments())
    if oversized:
        assert not attachments
        assert "exceeded the email attachment limit" in body
        assert "attached as a ZIP" not in body
    else:
        assert attachments[0].get_payload(decode=True) == packager.verified
        with ZipFile(BytesIO(attachments[0].get_payload(decode=True))) as archive:
            assert archive.namelist() == ["original.pdf"]
            assert archive.testzip() is None
    assert list(work.iterdir()) == []
    assert client.store.status("message-1") == "processed"


def test_unexpected_retrieval_error_is_sanitized(client, service, tmp_path):
    matter = Mock()
    matter.get_matter.side_effect = RuntimeError("private details")
    Agent(matter, Packager(), client).process(incoming(), tmp_path)
    reply = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(
            service.users().messages().send.call_args.kwargs["body"]["raw"],
        )
    )
    assert "could not complete retrieval" in reply.get_body().get_content()
    assert "private" not in reply.get_body().get_content()
    assert not list(reply.iter_attachments())


def test_email_client_uses_configured_state_path_and_explicit_override(config, service, tmp_path):
    from dataclasses import replace

    config = replace(config, gmail_state_path=tmp_path / "persistent.sqlite3")
    assert EmailClient(config, service=service).state_path == config.gmail_state_path
    override = tmp_path / "override.sqlite3"
    assert EmailClient(config, service=service, state_path=override).state_path == override


def test_container_cannot_start_interactive_oauth(config, tmp_path, monkeypatch):
    from dataclasses import replace

    config = replace(config, gmail_allow_interactive_auth=False)
    flow = Mock()
    monkeypatch.setattr("src.email_client.InstalledAppFlow.from_client_secrets_file", flow)
    email = EmailClient(config, state_path=tmp_path / "state.sqlite3")
    with pytest.raises(AuthenticationRequired, match="Interactive authorization is disabled"):
        email.authenticate(interactive=True)
    flow.assert_not_called()
    assert not config.gmail_token_path.exists()


def test_noninteractive_container_can_reuse_valid_token(config, service, tmp_path, monkeypatch):
    from dataclasses import replace

    config = replace(config, gmail_allow_interactive_auth=False)
    config.gmail_token_path.write_text("{}")
    credentials = Mock(valid=True, expired=False)
    credentials.has_scopes.return_value = True
    credentials.to_json.return_value = "{}"
    monkeypatch.setattr(
        "src.email_client.Credentials.from_authorized_user_file", Mock(return_value=credentials)
    )
    monkeypatch.setattr("src.email_client.build", Mock(return_value=service))
    flow = Mock()
    monkeypatch.setattr("src.email_client.InstalledAppFlow.from_client_secrets_file", flow)
    email = EmailClient(config, state_path=tmp_path / "state.sqlite3")
    assert email.authenticate() == "agent@example.com"
    flow.assert_not_called()


def test_interrupted_send_retains_duplicate_protection(client, config, service):
    service.users().messages().send().execute.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        client.send_reply(incoming(), "Summary")
    assert client.store.status("message-1") == "sending"
    restarted = EmailClient(config, service=service, state_path=client.state_path)
    with pytest.raises(AlreadyHandled):
        restarted.send_reply(incoming(), "Summary")


def prepare_small_parts(client, tmp_path, monkeypatch, count=3):
    """Incompressible seeded bytes force MIME-driven splitting with tiny limits."""
    import random

    from src.agent import format_summary
    from src.models import ProcessingResult

    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", 7500)
    monkeypatch.setattr("src.email_client.MAX_ATTACHMENT_BYTES", 10000)
    rng = random.Random(7301)
    files = []
    for index in range(count):
        path = tmp_path / f"file-{index}.pdf"
        path.write_bytes(b"%PDF-1.7\n" + rng.randbytes(3000))
        files.append(path)
    result = ProcessingResult(
        count, 13, MatterMetadata("M12205", counts={DocumentType.EXHIBITS: 13})
    )
    summary = format_summary(result, DocumentType.EXHIBITS, delivery_pending=True)
    parts = client.prepare_delivery(
        incoming(),
        summary,
        tuple(files),
        Packager(),
        tmp_path / "zip",
        matter_id="M12205",
        document_type=DocumentType.EXHIBITS,
    )
    assert len(parts) == count
    return parts, files


def decode_reply(raw):
    return BytesParser(policy=policy.default).parsebytes(raw)


def test_multipart_mime_size_threading_receipts_and_counts(client, service, tmp_path, monkeypatch):
    import json
    from io import BytesIO
    from zipfile import ZipFile

    parts, files = prepare_small_parts(client, tmp_path, monkeypatch)
    assert all(len(part.raw) <= 7500 for part in parts)
    messages = [decode_reply(part.raw) for part in parts]
    assert len({message["Message-ID"] for message in messages}) == 3
    for index, (message, source) in enumerate(zip(messages, files, strict=True), 1):
        assert message["Subject"] == "Re: M12205 Exhibits"
        assert message["In-Reply-To"] == incoming().rfc_message_id
        assert message["References"] == "<ancestor@example.net> <original@example.net>"
        body = message.get_body().get_content()
        if index == 1:
            assert "I retrieved 3 of 13 Exhibits" in body
            assert "all 3 retrieved documents across 3 separate replies" in body
            assert "I've attached the first document" in body
        else:
            assert "database" not in body.lower()
            assert "retrieved" not in body.lower()
            assert "13 Exhibits" not in body
            assert len(body.splitlines()) <= 8
            assert "Exhibits for matter M12205" in body
            assert ("final set" in body) == (index == 3)
            assert ("previous email" in body) == (index == 2)
        for technical in ("MIME", "encoded", "receipts", "prepared", "Part "):
            assert technical not in body
        attachments = list(message.iter_attachments())
        assert len(attachments) == 1
        with ZipFile(BytesIO(attachments[0].get_payload(decode=True))) as archive:
            assert archive.namelist() == [source.name]
            assert archive.read(source.name) == source.read_bytes()
    service.users().messages().send().execute.side_effect = [
        {"id": f"receipt-{index}", "threadId": "thread-1"} for index in range(3)
    ]
    client.send_delivery(incoming(), parts)
    assert client.store.status("message-1") == "processed"
    rows = client.store.parts("message-1")
    assert [row[3] for row in rows] == ["accepted"] * 3
    assert [row[4] for row in rows] == [f"receipt-{index}" for index in range(3)]
    assert [json.loads(row[2]) for row in rows] == [[source.name] for source in files]
    assert all(row[1] is None for row in rows)  # Release accepted MIME blobs.
    assert client.store.resumable() == []


def test_exact_encoded_message_boundary(client, tmp_path, monkeypatch):
    source = tmp_path / "test.pdf"
    source.write_bytes(b"%PDF-test")
    archive = Packager().package((source,), tmp_path / "test.zip")
    body = "Summary with Unicode: é. " * 20
    size = len(build_reply(incoming(), body, "agent@example.com", archive).as_bytes())
    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", size)
    assert len(build_reply(incoming(), body, "agent@example.com", archive).as_bytes()) == size
    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", size - 1)
    with pytest.raises(AttachmentTooLarge, match="Encoded message"):
        build_reply(incoming(), body, "agent@example.com", archive)
    assert client.store.status("message-1") is None


def test_unfit_and_invalid_pdfs_reported_without_blocking_other_files(
    client, tmp_path, monkeypatch
):
    import random

    rng = random.Random(5)
    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", 7500)
    files = []
    for name, content in (
        ("huge.pdf", b"%PDF-" + rng.randbytes(10000)),
        ("error.pdf", b"not a PDF"),
        ("empty.pdf", b""),
        ("small.pdf", b"%PDF-" + rng.randbytes(500)),
    ):
        source = tmp_path / name
        source.write_bytes(content)
        files.append(source)
    parts = client.prepare_delivery(
        incoming(), "Downloaded 4 files.", tuple(files), Packager(), tmp_path / "batches"
    )
    assert len(parts) == 1
    assert parts[0].filenames == ("small.pdf",)
    message = decode_reply(parts[0].raw)
    assert len(list(message.iter_attachments())) == 1
    body = message.get_body().get_content()
    assert "1 of the 4 retrieved documents" in body
    assert (
        "3 documents could not be included, including 1 that exceeded the email attachment limit"
        in (body)
    )
    assert all(path.name not in body for path in files)
    assert {(error.document, error.error) for error in parts[0].errors} == {
        ("huge.pdf", "exceeds safe encoded message limit"),
        ("error.pdf", "invalid PDF signature"),
        ("empty.pdf", "download source is empty"),
    }
    client.send_delivery(incoming(), parts)
    with client.store.connect() as db:
        diagnostics = db.execute("SELECT document, error FROM delivery_errors").fetchall()
    assert set(diagnostics) == {(error.document, error.error) for error in parts[0].errors}


@pytest.mark.parametrize(
    "failure", ["timeout", "server", "receipt", "thread", "sqlite", "interrupt"]
)
def test_uncertain_part_holds_remaining_delivery_and_conversation(
    client, config, service, tmp_path, monkeypatch, failure
):
    parts, _ = prepare_small_parts(client, tmp_path, monkeypatch)
    client.store.stage_delivery(incoming(), parts)
    api = service.users().messages()
    if failure == "sqlite":
        original_complete = client.store.complete_part

        def complete(message_id, part, reply_id):
            if part == 2:
                raise sqlite3.OperationalError("private disk details")
            original_complete(message_id, part, reply_id)

        monkeypatch.setattr(client.store, "complete_part", complete)
        second = {"id": "second"}
    else:
        second = {
            "timeout": OSError("private transport details"),
            "server": HttpError(httplib2.Response({"status": "500"}), b"private"),
            "receipt": {},
            "thread": {"id": "second", "threadId": "wrong-thread"},
            "interrupt": KeyboardInterrupt(),
        }[failure]
    api.send().execute.side_effect = [{"id": "first"}, second, {"id": "must-not-send"}]
    api.send.reset_mock()
    expected = KeyboardInterrupt if failure == "interrupt" else DeliveryUncertain
    with pytest.raises(expected):
        client.resume_delivery(incoming())
    assert api.send.call_count == 2
    assert client.store.status("message-1") == "sending"
    assert [row[3] for row in client.store.parts("message-1")] == ["accepted", "sending", "pending"]
    restarted = EmailClient(config, service=service, state_path=client.state_path)
    assert restarted.store.resumable() == []
    with pytest.raises(AlreadyHandled):
        restarted.resume_delivery(incoming())
    with pytest.raises(DeliveryUncertain):
        restarted.store.pending("thread-1", incoming().sender)
    assert restarted.store.pending("thread-1", "another@example.net") is None
    assert api.send.call_count == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 429])
def test_definite_part_rejection_preserves_receipts_and_resumes_exact_bytes(
    client, config, service, tmp_path, monkeypatch, status
):
    parts, files = prepare_small_parts(client, tmp_path, monkeypatch)
    api = service.users().messages()
    api.send().execute.side_effect = [
        {"id": "first"},
        HttpError(httplib2.Response({"status": str(status)}), b"private"),
    ]
    with pytest.raises(GmailError):
        client.send_delivery(incoming(), parts)
    rows = client.store.parts("message-1")
    assert [row[3] for row in rows] == ["accepted", "pending", "pending"]
    assert client.store.status("message-1") == "delivering"
    with pytest.raises(GmailError, match="unresolved"):
        client.store.complete("message-1", "bogus")
    for source in files:
        source.unlink()
    for archive in (tmp_path / "zip").glob("*.zip"):
        archive.unlink()
    restarted = EmailClient(config, service=service, state_path=client.state_path)
    saved = restarted.store.resumable()[0]
    assert saved.body == ""
    api.send().execute.side_effect = [{"id": "second"}, {"id": "third"}]
    api.send.reset_mock()
    restarted.resume_delivery(saved)
    assert api.send.call_count == 2
    sent = [call.kwargs["body"] for call in api.send.call_args_list]
    assert [base64.urlsafe_b64decode(payload["raw"]) for payload in sent] == [
        parts[1].raw,
        parts[2].raw,
    ]
    assert all(payload["threadId"] == "thread-1" for payload in sent)
    assert restarted.store.status("message-1") == "processed"
    assert restarted.store.parts("message-1")[0][4] == "first"
    assert restarted.store.pending("thread-1", incoming().sender) is None


@pytest.mark.parametrize("after_last", [False, True])
def test_crash_between_parts_or_before_final_commit_resumes_without_resend(
    client, config, service, tmp_path, monkeypatch, after_last
):
    parts, _ = prepare_small_parts(client, tmp_path, monkeypatch)
    client.store.stage_delivery(incoming(), parts)
    accepted = 3 if after_last else 1
    for index in range(1, accepted + 1):
        client.store.claim_part("message-1", index)
        client.store.complete_part("message-1", index, f"receipt-{index}")
    restarted = EmailClient(config, service=service, state_path=client.state_path)
    api = service.users().messages()
    api.send.reset_mock()
    agent = Agent(Mock(), Packager(), restarted, store=restarted.store)
    work = tmp_path / "workspace"
    assert run_polling(agent, work, once=True) == 1
    assert api.send.call_count == 3 - accepted
    agent.uarb.get_matter.assert_not_called()
    assert list(work.iterdir()) == []
    assert restarted.store.status("message-1") == "processed"
    with pytest.raises(AlreadyHandled):
        agent.process(incoming(), work)


def test_delivery_finalization_failure_keeps_receipts_without_resending(
    client, config, service, tmp_path, monkeypatch
):
    parts, _ = prepare_small_parts(client, tmp_path, monkeypatch)
    with client.store.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_finish BEFORE DELETE ON conversation_updates "
            "BEGIN SELECT RAISE(ABORT, 'test disk failure'); END"
        )
    with pytest.raises(GmailError, match="receipts saved"):
        client.send_delivery(incoming(), parts)
    assert client.store.status("message-1") == "delivering"
    assert all(row[3] == "accepted" for row in client.store.parts("message-1"))
    with client.store.connect() as db:
        db.execute("DROP TRIGGER fail_finish")
    restarted = EmailClient(config, service=service, state_path=client.state_path)
    api = service.users().messages()
    api.send.reset_mock()
    restarted.resume_delivery(incoming())
    api.send.assert_not_called()
    assert restarted.store.status("message-1") == "processed"


def test_resume_rejects_other_sender_or_thread(client, tmp_path, monkeypatch):
    from dataclasses import replace

    parts, _ = prepare_small_parts(client, tmp_path, monkeypatch)
    client.store.stage_delivery(incoming(), parts)
    for original in (
        replace(incoming(), sender="other@example.net"),
        replace(incoming(), thread_id="other-thread"),
    ):
        with pytest.raises(GmailError, match="routing"):
            client.resume_delivery(original)
    assert all(row[3] == "pending" for row in client.store.parts("message-1"))


def test_all_ten_files_can_be_delivered_with_partial_download_errors(
    client, service, tmp_path, monkeypatch
):
    import random
    from dataclasses import replace

    from src.models import DocumentError, DownloadResult

    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", 7500)

    class Matter:
        def get_matter(self, matter_id):
            return MatterMetadata(matter_id, counts={DocumentType.EXHIBITS: 13})

        def download_documents(self, matter_id, document_type, destination, *, limit=10):
            assert limit == 10
            rng = random.Random(29)
            files = []
            for index in range(limit):
                source = destination / f"doc-{index}.pdf"
                source.write_bytes(b"%PDF-" + rng.randbytes(3000))
                files.append(source)
            return DownloadResult(
                tuple(files),
                (
                    DocumentError("failed-1.pdf", "unavailable"),
                    DocumentError("failed-2.pdf", "unavailable"),
                ),
            )

    agent = Agent(Matter(), Packager(), client, store=client.store)
    result = agent.process(replace(incoming(), body="M12205 Exhibits"), tmp_path / "work")
    assert result.downloaded_count == 10
    assert len(result.errors) == 2
    rows = client.store.parts("message-1")
    import json

    delivered = [name for row in rows for name in json.loads(row[2])]
    assert delivered == [f"doc-{index}.pdf" for index in range(10)]
    assert all(row[3] == "accepted" for row in rows)
    message = decode_reply(
        base64.urlsafe_b64decode(service.users().messages().send.call_args.kwargs["body"]["raw"])
    )
    body = message.get_body().get_content()
    assert "Attached is the final set of Exhibits for matter M12205" in body
    assert "retrieved" not in body
    payloads = [
        call.kwargs["body"]
        for call in service.users().messages().send.call_args_list
        if "body" in call.kwargs
    ]
    first_body = decode_reply(base64.urlsafe_b64decode(payloads[0]["raw"])).get_body().get_content()
    assert "I retrieved 10 of 13 Exhibits" in first_body
    assert "I couldn't retrieve 2 requested documents" in first_body
    assert "10 separate replies" in first_body
    assert "failed-1.pdf" not in first_body
    with client.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM delivery_errors").fetchone()[0] == 2
    assert client.store.status("message-1") == "processed"
    assert not list((tmp_path / "work").iterdir())


def test_multiple_documents_are_grouped_when_the_encoded_message_fits(
    client, tmp_path, monkeypatch
):
    parts, _ = prepare_small_parts(client, tmp_path, monkeypatch, count=2)
    assert len(parts) == 2
    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", 15000)
    combined = client.prepare_delivery(
        incoming(),
        "Downloaded two files.",
        tuple(tmp_path / f"file-{index}.pdf" for index in range(2)),
        Packager(),
        tmp_path / "combined",
    )
    assert len(combined) == 1
    assert combined[0].filenames == ("file-0.pdf", "file-1.pdf")
    body = decode_reply(combined[0].raw).get_body().get_content()
    assert "I've attached all 2 retrieved documents" in body
    assert "separate replies" not in body
    assert "attachment limit" not in body
    assert "Part " not in body


def prepare_multi_zip_delivery(client, tmp_path, monkeypatch, *, count=2, message_limit=15000):
    """Force small individual ZIPs, independent of the larger whole-message limit."""
    import random

    from src.agent import format_summary
    from src.models import ProcessingResult

    monkeypatch.setattr("src.email_client.MAX_ATTACHMENT_BYTES", 3500)
    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", message_limit)
    rng = random.Random(61)
    sources = []
    for index in range(count):
        source = tmp_path / f"document-{index}.pdf"
        source.write_bytes(b"%PDF-1.7\n" + rng.randbytes(3000))
        sources.append(source)
    result = ProcessingResult(
        count,
        13,
        MatterMetadata("M12205", title="Water project", counts={DocumentType.EXHIBITS: 13}),
    )
    prepared = client.prepare_delivery(
        incoming(),
        format_summary(result, DocumentType.EXHIBITS, delivery_pending=True),
        tuple(sources),
        Packager(),
        tmp_path / "archives",
        matter_id="M12205",
        document_type=DocumentType.EXHIBITS,
    )
    return prepared, sources


def test_multiple_verified_zips_share_one_email_and_one_receipt(
    client, service, tmp_path, monkeypatch
):
    import json
    from io import BytesIO
    from zipfile import ZipFile

    parts, sources = prepare_multi_zip_delivery(client, tmp_path, monkeypatch)
    assert len(parts) == 1
    reply = decode_reply(parts[0].raw)
    attachments = list(reply.iter_attachments())
    assert len(attachments) == 2
    contents = {}
    for attachment in attachments:
        data = attachment.get_payload(decode=True)
        assert len(data) <= 3500
        with ZipFile(BytesIO(data)) as archive:
            assert archive.testzip() is None
            contents.update({name: archive.read(name) for name in archive.namelist()})
    assert contents == {source.name: source.read_bytes() for source in sources}
    body = reply.get_body().get_content()
    assert "I retrieved 2 of 13 Exhibits" in body
    assert "I've attached all 2 retrieved documents" in body
    for unwanted in ("separate replies", "attachment limit", "MIME", "receipts", "Part "):
        assert unwanted not in body
    service.users().messages().send.reset_mock()
    client.send_delivery(incoming(), parts)
    assert service.users().messages().send.call_count == 1
    row = client.store.parts("message-1")[0]
    assert json.loads(row[2]) == [source.name for source in sources]
    assert row[3:] == ("accepted", "reply-id")


def test_multiple_zips_split_by_complete_mime_size_not_combined_raw_size(
    client, tmp_path, monkeypatch
):
    parts, _ = prepare_multi_zip_delivery(client, tmp_path, monkeypatch, message_limit=7500)
    assert len(parts) == 2
    messages = [decode_reply(part.raw) for part in parts]
    raw_zip_bytes = sum(
        len(attachment.get_payload(decode=True))
        for message in messages
        for attachment in message.iter_attachments()
    )
    assert raw_zip_bytes < 7500  # Raw files fit; their complete encoded envelope does not.
    assert all(len(part.raw) <= 7500 for part in parts)
    first, final = [message.get_body().get_content() for message in messages]
    assert "across 2 separate replies" in first
    assert "Attached is the final set of Exhibits for matter M12205" in final
    assert "Water project" not in final
    assert "database" not in final


def test_multi_attachment_mime_exact_size_boundary(tmp_path, monkeypatch):
    paths = tuple(tmp_path / f"{index}.zip" for index in range(2))
    for path in paths:
        path.write_bytes(b"zip fixture")
    reply = build_reply(incoming(), "Two attachments", "agent@example.com", paths)
    exact = len(reply.as_bytes())
    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", exact)
    assert len(
        build_reply(incoming(), "Two attachments", "agent@example.com", paths).as_bytes()
    ) == (exact)
    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", exact - 1)
    with pytest.raises(AttachmentTooLarge):
        build_reply(incoming(), "Two attachments", "agent@example.com", paths)


@pytest.mark.parametrize("uncertain", [False, True])
def test_multi_attachment_restart_keeps_messages_and_completed_receipts(
    client, config, service, tmp_path, monkeypatch, uncertain
):
    import shutil

    parts, sources = prepare_multi_zip_delivery(
        client, tmp_path, monkeypatch, count=4, message_limit=10000
    )
    assert len(parts) == 2
    assert [len(list(decode_reply(part.raw).iter_attachments())) for part in parts] == [2, 2]
    api = service.users().messages()
    error = (
        OSError("private transport error")
        if uncertain
        else HttpError(httplib2.Response({"status": "403"}), b"private provider response")
    )
    api.send().execute.side_effect = [{"id": "first-receipt"}, error]
    with pytest.raises(DeliveryUncertain if uncertain else GmailError):
        client.send_delivery(incoming(), parts)
    for source in sources:
        source.unlink()
    shutil.rmtree(tmp_path / "archives")
    restarted = EmailClient(config, service=service, state_path=client.state_path)
    api.send.reset_mock()
    if uncertain:
        assert restarted.store.resumable() == []
        with pytest.raises(AlreadyHandled):
            restarted.resume_delivery(incoming())
        api.send.assert_not_called()
    else:
        api.send().execute.side_effect = [{"id": "final-receipt"}]
        api.send.reset_mock()
        restarted.resume_delivery(restarted.store.resumable()[0])
        assert api.send.call_count == 1
        payload = api.send.call_args.kwargs["body"]
        assert payload["threadId"] == incoming().thread_id
        assert base64.urlsafe_b64decode(payload["raw"]) == parts[1].raw
        assert len(list(decode_reply(parts[1].raw).iter_attachments())) == 2
        assert restarted.store.status("message-1") == "processed"
    assert restarted.store.parts("message-1")[0][4] == "first-receipt"


def test_all_four_documents_too_large_produces_one_plain_language_reply(
    client, tmp_path, monkeypatch
):
    import random

    rng = random.Random(4)
    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", 7500)
    files = []
    for index in range(4):
        path = tmp_path / f"private-{index}.pdf"
        path.write_bytes(b"%PDF-" + rng.randbytes(10000))
        files.append(path)
    parts = client.prepare_delivery(
        incoming(),
        "Hi,\n\nI retrieved 4 of 13 Exhibits.",
        tuple(files),
        Packager(),
        tmp_path / "batches",
        matter_id="M12205",
        document_type=DocumentType.EXHIBITS,
    )
    assert len(parts) == 1
    reply = decode_reply(parts[0].raw)
    assert not list(reply.iter_attachments())
    body = reply.get_body().get_content()
    assert "None of the retrieved documents could be included" in body
    assert "4 documents exceeded the email attachment limit and couldn't be included" in body
    assert "separate replies" not in body
    assert "I've attached" not in body
    assert all(source.name not in body for source in files)
    assert len(parts[0].errors) == 4


def test_zero_retrieval_errors_remain_internal_without_exposing_filenames(
    client, service, tmp_path
):
    from dataclasses import replace

    from src.models import DocumentError, DownloadResult

    class FailedMatter:
        def get_matter(self, matter_id):
            return MatterMetadata(matter_id, counts={DocumentType.EXHIBITS: 3})

        def download_documents(self, *args, **kwargs):
            return DownloadResult(errors=(DocumentError("private.pdf", "technical diagnostic"),))

    result = Agent(FailedMatter(), Packager(), client, store=client.store).process(
        replace(incoming(), body="M12205 Exhibits"), tmp_path
    )
    assert result.downloaded_count == 0
    reply = decode_reply(
        base64.urlsafe_b64decode(service.users().messages().send.call_args.kwargs["body"]["raw"])
    )
    assert "couldn't retrieve 1 requested document" in reply.get_body().get_content()
    assert "private.pdf" not in reply.get_body().get_content()
    assert "technical diagnostic" not in reply.get_body().get_content()
    assert not list(reply.iter_attachments())
    with client.store.connect() as db:
        assert db.execute("SELECT document, error FROM delivery_errors").fetchall() == [
            ("private.pdf", "technical diagnostic")
        ]


def test_largest_first_zip_grouping_avoids_an_unnecessary_third_email(
    client, tmp_path, monkeypatch
):
    import random

    from src.packager import ZipBatch

    class IndividualZips(Packager):
        def batches(self, files, directory, fits):
            directory.mkdir()
            return tuple(
                ZipBatch(self.package((source,), directory / f"{index}.zip"), (source.name,))
                for index, source in enumerate(files)
            ), ()

    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", 8500)
    rng = random.Random(99)
    files = []
    # Input-order grouping would put both small ZIPs together and need three replies.
    for index, size in enumerate((2000, 2000, 3000, 3000)):
        source = tmp_path / f"{index}.pdf"
        source.write_bytes(b"%PDF-" + rng.randbytes(size))
        files.append(source)
    parts = client.prepare_delivery(
        incoming(),
        "Hi,\n\nDocuments for M12205.",
        tuple(files),
        IndividualZips(),
        tmp_path / "zips",
        matter_id="M12205",
        document_type=DocumentType.EXHIBITS,
    )
    assert len(parts) == 2
    assert [len(list(decode_reply(part.raw).iter_attachments())) for part in parts] == [2, 2]
    assert all(len(part.raw) <= 8500 for part in parts)
    assert sorted(name for part in parts for name in part.filenames) == [
        path.name for path in files
    ]


def test_summary_can_precede_a_large_attachment_without_discarding_the_document(
    client, tmp_path, monkeypatch
):
    import random

    monkeypatch.setattr("src.email_client.MAX_MESSAGE_BYTES", 7500)
    source = tmp_path / "large.pdf"
    source.write_bytes(b"%PDF-" + random.Random(1).randbytes(4000))
    parts = client.prepare_delivery(
        incoming(),
        "Hi,\n\n" + "Retrieved matter metadata. " * 100,
        (source,),
        Packager(),
        tmp_path / "zips",
        matter_id="M12205",
        document_type=DocumentType.EXHIBITS,
    )
    assert len(parts) == 2
    first, last = [decode_reply(part.raw) for part in parts]
    assert not list(first.iter_attachments())
    assert "The attachments will follow in a separate reply" in first.get_body().get_content()
    assert "I've attached" not in first.get_body().get_content()
    assert len(list(last.iter_attachments())) == 1
    assert "final set of Exhibits for matter M12205" in last.get_body().get_content()
    assert not parts[0].errors
    assert parts[1].filenames == ("large.pdf",)
