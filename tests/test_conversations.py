"""Persistent Gmail clarification using fake Gmail receipts and offline matter data."""

import base64
import json
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import Mock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from src.agent import Agent, run_polling
from src.config import Config
from src.email_client import (
    AlreadyHandled,
    DeliveryUncertain,
    EmailClient,
    GmailError,
    parse_message,
)
from src.models import DocumentType, DownloadResult, IncomingEmail, MatterMetadata
from src.packager import Packager
from src.parser import parse_request


def incoming(
    message_id,
    body,
    *,
    thread="thread-1",
    sender="Alice <alice@example.net>",
    subject="Re: Request",
):
    return IncomingEmail(message_id, thread, sender, subject, body, f"<{message_id}@example.net>")


class Matter:
    def __init__(self, with_files=False):
        self.visited = []
        self.downloaded = []
        self.with_files = with_files

    def get_matter(self, matter_id):
        self.visited.append(matter_id)
        return MatterMetadata(
            matter_id,
            title="Fixture matter",
            status="Closed",
            counts={category: int(self.with_files) for category in DocumentType},
        )

    def download_documents(self, matter_id, document_type, destination, *, limit=10):
        self.downloaded.append((matter_id, document_type))
        path = destination / "document.pdf"
        path.write_bytes(b"%PDF-fixture")
        return DownloadResult((path,))


@pytest.fixture
def setup(tmp_path):
    service = Mock()
    replies = []
    api = service.users().messages()

    def send(*, userId, body):
        request = Mock()

        def execute(**kwargs):
            reply = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(body["raw"])
            )
            replies.append(reply)
            return {"id": f"sent-{len(replies)}", "threadId": body["threadId"]}

        request.execute.side_effect = execute
        return request

    api.send.side_effect = send
    client = EmailClient(
        Config(agent_email="agent@example.com"),
        service=service,
        state_path=tmp_path / "state.sqlite3",
    )
    matter = Matter()
    agent = Agent(matter, Packager(), client, store=client.store)
    return agent, client, matter, replies, service


def test_multiple_turns_survive_worker_restart_and_clear_on_success(setup, tmp_path):
    agent, email, matter, replies, service = setup
    first = incoming("one", "Please send the documents for M12345.")
    agent.process(first, tmp_path / "first")
    pending = email.store.pending("thread-1", first.sender)
    assert pending[0].matter_id == "M12345"
    assert pending[0].document_type is None
    assert pending[1:] == (1, "clarifying")
    assert "document type" in replies[-1].get_content()
    restarted_email = EmailClient(email.config, service=service, state_path=email.state_path)
    restarted = Agent(matter, Packager(), restarted_email, store=restarted_email.store)
    restarted.process(incoming("two", "I'm unsure."), tmp_path / "second")
    assert restarted_email.store.pending("thread-1", first.sender)[1] == 2
    restarted.process(incoming("three", "Key Documents."), tmp_path / "third")
    assert matter.visited == ["M12345"]
    assert "I downloaded 0 of 0 Key Documents" in replies[-1].get_content()
    assert email.store.pending("thread-1", first.sender) is None
    assert email.store.status("three") == "processed"


def test_explicit_category_correction_ignores_history_and_stale_subject(setup, tmp_path):
    agent, email, matter, replies, _ = setup
    agent.process(incoming("one", "Transcripts, please."), tmp_path / "one")
    agent.process(
        incoming(
            "two",
            "Actually, make that Exhibits.\n> M54321 Transcripts",
            subject="Re: Transcripts, please",
        ),
        tmp_path / "two",
    )
    assert (
        email.store.pending("thread-1", "alice@example.net")[0].document_type
        == DocumentType.EXHIBITS
    )
    agent.process(
        incoming("three", "M12345", subject="Re: Transcripts, please"), tmp_path / "three"
    )
    assert matter.visited == ["M12345"]
    assert "I downloaded 0 of 0 Exhibits" in replies[-1].get_content()


def test_explicit_matter_correction(setup, tmp_path):
    agent, email, matter, replies, _ = setup
    agent.process(incoming("one", "M12345"), tmp_path / "one")
    agent.process(incoming("two", "Correction: M54321"), tmp_path / "two")
    assert email.store.pending("thread-1", "alice@example.net")[0].matter_id == "M54321"
    agent.process(incoming("three", "Recordings please"), tmp_path / "three")
    assert matter.visited == ["M54321"]
    assert "I downloaded 0 of 0 Recordings" in replies[-1].get_content()


def test_unmarked_conflicting_correction_still_requires_clarification(setup, tmp_path):
    agent, email, matter, _, _ = setup
    agent.process(incoming("one", "M12345"), tmp_path / "one")
    agent.process(incoming("two", "M54321"), tmp_path / "two")
    result = email.store.pending("thread-1", "alice@example.net")[0]
    assert result.matter_id is None
    assert result.ambiguous_fields == ("matter_id",)
    assert not matter.visited
    agent.process(incoming("three", "M12345 Exhibits"), tmp_path / "three")
    assert matter.visited == ["M12345"]


@pytest.mark.parametrize(
    "thread,sender",
    [
        ("different-thread", "alice@example.net"),
        ("thread-1", "bob@example.net"),
    ],
)
def test_thread_and_sender_isolation(setup, tmp_path, thread, sender):
    agent, email, matter, _, _ = setup
    agent.process(incoming("one", "M12345"), tmp_path / "one")
    agent.process(incoming("two", "Key Documents", thread=thread, sender=sender), tmp_path / "two")
    assert not matter.visited
    assert email.store.pending("thread-1", "alice@example.net")[0].matter_id == "M12345"
    other = email.store.pending(thread, sender)[0]
    assert other.matter_id is None
    assert other.document_type == DocumentType.KEY_DOCUMENTS


def test_sender_address_normalization_does_not_mix_reply_to(setup, tmp_path):
    agent, email, matter, _, _ = setup
    agent.process(incoming("one", "M12345"), tmp_path / "one")
    reply = incoming("two", "Exhibits", sender="Different Display Name <ALICE@example.net>")
    agent.process(reply, tmp_path / "two")
    assert matter.visited == ["M12345"]
    assert email.store.pending("thread-1", "alice@example.net") is None


def test_completed_thread_never_reuses_stale_subject(setup, tmp_path):
    agent, email, matter, replies, _ = setup
    agent.process(incoming("one", "M12345 Exhibits"), tmp_path / "one")
    assert email.store.pending("thread-1", "alice@example.net") is None
    agent.process(incoming("two", "Thanks!", subject="Re: M12345 Exhibits"), tmp_path / "two")
    assert matter.visited == ["M12345"]
    assert set(email.store.pending("thread-1", "alice@example.net")[0].missing_fields) == {
        "matter_id",
        "document_type",
    }
    assert "matter number" in replies[-1].get_content()
    # A complete new request in the same thread is allowed, using only its new text.
    agent.process(
        incoming("three", "M54321 Recordings", subject="Re: M12345 Exhibits"), tmp_path / "three"
    )
    assert matter.visited == ["M12345", "M54321"]


def test_duplicate_clarification_does_not_send_or_call_model_twice(setup, tmp_path):
    agent, email, _, replies, _ = setup
    fallback = Mock()
    fallback.parse.side_effect = lambda text, previous: parse_request(text, previous)
    agent.fallback = fallback
    message = incoming("one", "M12345")
    agent.process(message, tmp_path / "one")
    with pytest.raises(AlreadyHandled):
        agent.process(message, tmp_path / "two")
    assert len(replies) == 1
    assert fallback.parse.call_count == 1
    assert email.store.pending("thread-1", message.sender)[1] == 1


def test_clarification_limit_stops_merging_and_model_calls(setup, tmp_path):
    agent, email, matter, replies, _ = setup
    fallback = Mock()
    fallback.parse.side_effect = lambda text, previous: parse_request(text, previous)
    agent.fallback = fallback
    for number, text in enumerate(("M12345", "Unsure", "Still unsure")):
        agent.process(incoming(str(number), text), tmp_path / str(number))
    assert email.store.pending("thread-1", "alice@example.net")[1:] == (3, "limited")
    assert "M12205 Key Documents" in replies[-1].get_content()
    calls = fallback.parse.call_count
    agent.process(incoming("four", "Exhibits"), tmp_path / "four")
    assert not matter.visited
    assert fallback.parse.call_count == calls
    agent.process(incoming("five", "M54321 Exhibits"), tmp_path / "five")
    assert matter.visited == ["M54321"]
    assert email.store.pending("thread-1", "alice@example.net") is None


def test_definite_send_failure_never_advances_pending_state(setup, tmp_path):
    agent, email, _, _, service = setup
    agent.process(incoming("one", "M12345"), tmp_path / "one")
    before = email.store.pending("thread-1", "alice@example.net")
    service.users().messages().send.side_effect = HttpError(
        httplib2.Response({"status": "403"}), b"denied"
    )
    with pytest.raises(GmailError):
        agent.process(incoming("two", "Key Documents"), tmp_path / "two")
    assert email.store.status("two") is None
    assert email.store.pending("thread-1", "alice@example.net") == before


def test_sent_but_state_failure_rolls_back_and_holds_thread(setup, tmp_path):
    agent, email, matter, replies, service = setup
    agent.process(incoming("one", "M12345"), tmp_path / "one")
    with email.store.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_cleanup BEFORE DELETE ON conversations "
            "BEGIN SELECT RAISE(ABORT, 'test transaction failure'); END"
        )
    with pytest.raises(DeliveryUncertain):
        agent.process(incoming("two", "Exhibits"), tmp_path / "two")
    assert len(replies) == 2  # Gmail accepted the reply; SQLite refused the same transaction.
    assert email.store.status("two") == "sending"
    with email.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM conversation_updates").fetchone()[0] == 1
    restarted = EmailClient(email.config, service=service, state_path=email.state_path)
    new_agent = Agent(matter, Packager(), restarted, store=restarted.store)
    with pytest.raises(DeliveryUncertain):
        new_agent.process(incoming("three", "M54321 recordings"), tmp_path / "three")
    assert len(replies) == 2


def test_pending_json_contains_only_validated_fields_not_email_history(setup, tmp_path):
    agent, email, _, _, _ = setup
    agent.process(
        incoming("one", "Please send M12345 documents. Private unrelated text here."), tmp_path
    )
    with email.store.connect() as db:
        saved = db.execute("SELECT request_json FROM conversations").fetchone()[0]
    assert "Private unrelated" not in saved
    assert json.loads(saved)["matter_id"] == "M12345"


def test_html_quote_history_is_not_used_for_corrections():
    message = EmailMessage()
    message["From"] = "alice@example.net"
    message["Subject"] = "Re: request"
    message["Message-ID"] = "<test@example.net>"
    message.set_content(
        "<p>Actually, make that Exhibits.</p>"
        '<div class="gmail_quote"><blockquote><p>M54321 Transcripts</p>'
        "</blockquote></div>",
        subtype="html",
    )
    parsed = parse_message(
        {
            "id": "one",
            "threadId": "thread-1",
            "labelIds": ["SPAM"],
            "raw": base64.urlsafe_b64encode(message.as_bytes()).decode(),
        },
        "agent@example.com",
    )
    assert "Actually, make that Exhibits" in parsed.body
    assert "M54321" not in parsed.body
    assert "Transcripts" not in parsed.body


def test_production_worker_resumes_and_cleans_zip(setup, tmp_path):
    agent, email, matter, replies, service = setup
    matter.with_files = True
    first = incoming("one", "M12345")
    second = incoming("two", "Key Documents")
    email.receive = Mock(side_effect=[[first], [second], [second]])
    work = tmp_path / "work"
    assert run_polling(agent, work, once=True) == 1
    assert email.store.pending(first.thread_id, first.sender)
    assert run_polling(agent, work, once=True) == 1
    assert matter.downloaded == [("M12345", DocumentType.KEY_DOCUMENTS)]
    attachment = list(replies[-1].iter_attachments())[0]
    assert attachment.get_content_type() == "application/zip"
    assert email.store.pending(first.thread_id, first.sender) is None
    assert list(work.iterdir()) == []
    assert run_polling(agent, work, once=True) == 0
    assert len(replies) == 2


def test_acknowledging_retrieval_failure_does_not_retry_stale_request(setup, tmp_path):
    agent, email, matter, replies, _ = setup
    matter.get_matter = Mock(side_effect=RuntimeError("unavailable"))
    agent.process(incoming("one", "M12345 Exhibits"), tmp_path / "one")
    assert email.store.pending("thread-1", "alice@example.net")[2] == "retry"
    agent.process(incoming("two", "Thanks"), tmp_path / "two")
    assert matter.get_matter.call_count == 1
    assert "matter number" in replies[-1].get_content()


def test_explicit_retry_can_resume_failed_retrieval(setup, tmp_path):
    agent, email, matter, replies, _ = setup
    original = matter.get_matter
    matter.get_matter = Mock(side_effect=RuntimeError("unavailable"))
    agent.process(incoming("one", "M12345 Exhibits"), tmp_path / "one")
    matter.get_matter = original
    agent.process(incoming("two", "Please try again"), tmp_path / "two")
    assert matter.visited == ["M12345"]
    assert email.store.pending("thread-1", "alice@example.net") is None


def test_not_found_followup_can_supply_corrected_matter(setup, tmp_path):
    agent, email, matter, replies, _ = setup
    original = matter.get_matter
    matter.get_matter = Mock(side_effect=LookupError("not found"))
    agent.process(incoming("one", "M12345 Exhibits"), tmp_path / "one")
    partial = email.store.pending("thread-1", "alice@example.net")[0]
    assert partial.matter_id is None
    assert partial.document_type == DocumentType.EXHIBITS
    matter.get_matter = original
    agent.process(incoming("two", "Actually M54321"), tmp_path / "two")
    assert matter.visited == ["M54321"]


def test_reply_references_prevent_reusing_subject_even_without_re_prefix(setup, tmp_path):
    from dataclasses import replace

    agent, email, matter, replies, _ = setup
    agent.process(incoming("one", "M12345 Exhibits"), tmp_path / "one")
    thanks = replace(
        incoming("two", "Thanks", subject="M12345 Exhibits"), references="<one@example.net>"
    )
    agent.process(thanks, tmp_path / "two")
    assert matter.visited == ["M12345"]
    assert "matter number" in replies[-1].get_content()


def test_correction_after_greeting_is_recognized(setup, tmp_path):
    agent, email, matter, replies, _ = setup
    agent.process(incoming("one", "Transcripts please"), tmp_path / "one")
    agent.process(incoming("two", "Hi,\nActually, make that Exhibits"), tmp_path / "two")
    assert (
        email.store.pending("thread-1", "alice@example.net")[0].document_type
        == DocumentType.EXHIBITS
    )


def test_model_recovery_completes_pending_conversation(setup, tmp_path):
    agent, email, matter, replies, _ = setup
    from src.models import ParseResult

    fallback = Mock()
    fallback.parse.side_effect = [
        parse_request("M12345"),
        ParseResult("M12345", DocumentType.TRANSCRIPTS),
    ]
    agent.fallback = fallback
    agent.process(incoming("one", "Please send the documents for M12345"), tmp_path / "one")
    agent.process(
        incoming("two", "The verbatim account of the hearing please.\n> M54321 exhibits"),
        tmp_path / "two",
    )
    assert matter.visited == ["M12345"]
    assert "I downloaded 0 of 0 Transcripts" in replies[-1].get_content()
    assert "M54321" not in fallback.parse.call_args.args[0]
    assert fallback.parse.call_args.args[1].matter_id == "M12345"
    assert email.store.pending("thread-1", "alice@example.net") is None


def test_pending_cancellation_terminates_without_retrieval_and_isolates_sender(setup, tmp_path):
    agent, email, matter, replies, _ = setup
    agent.process(incoming("one", "M12345"), tmp_path / "one")
    agent.process(incoming("bob", "M54321", sender="bob@example.net"), tmp_path / "bob")
    result = agent.process(incoming("cancel", "Cancel my request"), tmp_path / "cancel")
    assert result.cancelled
    assert not matter.visited
    assert email.store.pending("thread-1", "alice@example.net") is None
    assert email.store.pending("thread-1", "bob@example.net")[0].matter_id == "M54321"
    assert "cancelled" in replies[-1].get_content()
    agent.process(incoming("next", "Key Documents"), tmp_path / "next")
    assert not matter.visited
    assert email.store.pending("thread-1", "alice@example.net")[0].matter_id is None


def test_cancellation_remains_effective_when_acknowledgement_is_rejected(setup, tmp_path):
    agent, email, matter, _, service = setup
    agent.process(incoming("one", "M12345"), tmp_path / "one")
    service.users().messages().send.side_effect = HttpError(
        httplib2.Response({"status": "403"}), b"denied"
    )
    with pytest.raises(GmailError):
        agent.process(incoming("cancel", "Please cancel"), tmp_path / "cancel")
    assert email.store.pending("thread-1", "alice@example.net") is None
    assert email.store.status("cancel") is None
    assert not matter.visited
