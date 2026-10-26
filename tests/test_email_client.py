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
    assert "Successfully downloaded 1 of 5 Key Documents" in body
    assert "Failed document attempts (after retry): 1" in body
    attachments = list(reply.iter_attachments())
    if oversized:
        assert not attachments
        assert "exceeds the safe email attachment size limit" in body
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
