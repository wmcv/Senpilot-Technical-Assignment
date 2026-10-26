"""Gmail OAuth, MIME handling, and a small persistent delivery ledger.

No credentials, message bodies, or raw API exceptions are logged. Read operations
retry transient failures. Sending is never blindly retried after an uncertain result.
"""

import base64
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from httplib2 import Http

from src.config import Config
from src.models import DocumentType, IncomingEmail, ParseResult

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
MAX_ATTACHMENT_BYTES = 18_000_000
MAX_MESSAGE_BYTES = 24_000_000  # Includes MIME/base64 overhead; below personal Gmail's limit.


class GmailError(RuntimeError):
    """Sanitized error safe to display without disclosing message/API contents."""


class AttachmentTooLarge(GmailError):
    pass


class AuthenticationRequired(GmailError):
    pass


class DeliveryUncertain(GmailError):
    pass


class AlreadyHandled(GmailError):
    pass


class MessageStore:
    """Persist IDs and validated partial requests, never raw email bodies.

    'sending' requires manual review; pending state changes with the send receipt.
    """

    def __init__(self, path: Path, mailbox: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS messages "
                "(id TEXT PRIMARY KEY, status TEXT NOT NULL, reply_id TEXT)"
            )
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('mailbox', ?)", (mailbox.casefold(),)
            )
            if db.execute("SELECT value FROM settings WHERE key='mailbox'").fetchone()[0] != (
                mailbox.casefold()
            ):
                raise GmailError("Processed-message store belongs to a different mailbox")
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('cutoff_ms', ?)",
                (str(int(time.time() * 1000)),),
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS conversations ("
                "thread_id TEXT, sender TEXT, request_json TEXT NOT NULL, turns INTEGER NOT NULL, "
                "status TEXT NOT NULL, PRIMARY KEY(thread_id, sender))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS conversation_updates ("
                "message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, sender TEXT NOT NULL, "
                "request_json TEXT, turns INTEGER NOT NULL, status TEXT NOT NULL)"
            )
            self.cutoff_ms = int(
                db.execute("SELECT value FROM settings WHERE key='cutoff_ms'").fetchone()[0]
            )

    @contextmanager
    def connect(self):
        # sqlite's connection context commits/rolls back but does not close.
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def status(self, message_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT status FROM messages WHERE id=?", (message_id,)).fetchone()
            return row[0] if row else None

    def claim(self, message_id: str) -> None:
        try:
            with self.connect() as db:
                db.execute("INSERT INTO messages (id, status) VALUES (?, 'sending')", (message_id,))
        except sqlite3.IntegrityError:
            raise AlreadyHandled("Message already handled or awaiting delivery review") from None

    def complete(self, message_id: str, reply_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE messages SET status='processed', reply_id=? WHERE id=?",
                (reply_id, message_id),
            )
            update = db.execute(
                "SELECT thread_id, sender, request_json, turns, status FROM conversation_updates "
                "WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if update:
                thread, sender, request, turns, status = update
                if request is None:
                    db.execute(
                        "DELETE FROM conversations WHERE thread_id=? AND sender=?", (thread, sender)
                    )
                else:
                    db.execute(
                        "INSERT INTO conversations VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(thread_id, sender) DO UPDATE SET "
                        "request_json=excluded.request_json, turns=excluded.turns, "
                        "status=excluded.status",
                        (thread, sender, request, turns, status),
                    )
                db.execute("DELETE FROM conversation_updates WHERE message_id=?", (message_id,))

    def release(self, message_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM messages WHERE id=? AND status='sending'", (message_id,))
            db.execute("DELETE FROM conversation_updates WHERE message_id=?", (message_id,))

    def pending(self, thread_id: str, sender: str) -> tuple[ParseResult, int, str] | None:
        sender = _address(sender).casefold()
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM conversation_updates u JOIN messages m ON m.id=u.message_id "
                "WHERE u.thread_id=? AND u.sender=? AND m.status='sending'",
                (thread_id, sender),
            ).fetchone():
                raise DeliveryUncertain(
                    "Conversation awaits delivery review; automatic replies held"
                )
            row = db.execute(
                "SELECT request_json, turns, status FROM conversations "
                "WHERE thread_id=? AND sender=?",
                (thread_id, sender),
            ).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        result = ParseResult(
            value["matter_id"],
            DocumentType(value["document_type"]) if value["document_type"] else None,
            tuple(value["missing_fields"]),
            tuple(value["ambiguous_fields"]),
            value["clarification"],
        )
        return result, row[1], row[2]

    def discard_pending(self, thread_id: str, sender: str) -> None:
        """Terminate request state without marking an incoming message delivered."""
        with self.connect() as db:
            db.execute(
                "DELETE FROM conversations WHERE thread_id=? AND sender=?",
                (thread_id, _address(sender).casefold()),
            )

    def stage_conversation(
        self,
        incoming: IncomingEmail,
        result: ParseResult | None,
        turns: int = 0,
        status: str = "clarifying",
    ) -> None:
        """Stage a transition before sending; complete() applies it atomically with the receipt."""
        request = (
            json.dumps(
                {
                    "matter_id": result.matter_id,
                    "document_type": result.document_type,
                    "missing_fields": result.missing_fields,
                    "ambiguous_fields": result.ambiguous_fields,
                    "clarification": result.clarification,
                }
            )
            if result
            else None
        )
        sender = _address(incoming.sender).casefold()
        with self.connect() as db:
            db.execute(
                "INSERT INTO conversation_updates VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(message_id) DO UPDATE SET request_json=excluded.request_json, "
                "turns=excluded.turns, status=excluded.status",
                (incoming.message_id, incoming.thread_id, sender, request, turns, status),
            )


def _read(request):
    for attempt in range(3):
        try:
            return request.execute(num_retries=0)
        except HttpError as error:
            if error.resp.status == 401:
                raise AuthenticationRequired(
                    "Gmail authorization required; run --gmail-auth"
                ) from None
            if error.resp.status not in {429, 500, 502, 503, 504} or attempt == 2:
                raise GmailError(f"Gmail read failed (HTTP {error.resp.status})") from None
        except RefreshError as error:
            if error.retryable:
                raise GmailError("Gmail token refresh temporarily unavailable") from None
            raise AuthenticationRequired(
                "Gmail authorization expired or was revoked; run --gmail-auth"
            ) from None
        except (OSError, TransportError):
            if attempt == 2:
                raise GmailError("Gmail read failed due to a network error") from None
        time.sleep(2**attempt)
    raise GmailError("Gmail read failed")


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        quoted = (
            tag == "blockquote"
            or "gmail_quote" in attributes.get("class", "").split()
            or attributes.get("id") == "divRplyFwdMsg"
        )
        if self.hidden or tag in {"script", "style"} or quoted:
            if tag not in {"br", "img", "hr", "input", "meta", "link"}:
                self.hidden.append(tag)
            return
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
                del self.hidden[index:]
            return
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _address(value: str) -> str:
    addresses = getaddresses([value])
    if len(addresses) != 1 or "@" not in addresses[0][1] or any(c in value for c in "\r\n"):
        raise GmailError("Message has no safe, unambiguous reply address")
    return addresses[0][1]


def parse_message(data: dict, agent_email: str) -> IncomingEmail | None:
    """Decode Gmail's RFC message; text/plain is preferred over HTML alternatives."""
    if set(data.get("labelIds", ())) & {"SENT", "DRAFT", "TRASH"}:
        return None
    try:
        message = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(data["raw"] + "===")
        )
        sender = str(message.get("From", ""))
        if _address(sender).casefold() == agent_email.casefold():
            return None
        if str(message.get("Auto-Submitted", "no")).lower() != "no":
            return None
        part = message.get_body(preferencelist=("plain", "html"))
        body = part.get_content() if part else ""
        if part and part.get_content_type() == "text/html":
            html = _HTMLText()
            html.feed(body)
            body = "".join(html.parts).strip()
        return IncomingEmail(
            data["id"],
            data["threadId"],
            sender,
            str(message.get("Subject", "")),
            body,
            str(message["Message-ID"]) if message["Message-ID"] else None,
            str(message["References"]) if message["References"] else None,
            str(message["Reply-To"]) if message["Reply-To"] else None,
        )
    except (KeyError, ValueError, LookupError, TypeError):
        raise GmailError("Cannot decode incoming Gmail message") from None


def build_reply(
    original: IncomingEmail,
    body: str,
    mailbox: str,
    attachment: Path | None = None,
) -> EmailMessage:
    """Create the exact MIME bytes sent; never alter or recompress the verified ZIP."""
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = mailbox
    message["To"] = _address(original.reply_to or original.sender)
    message["Subject"] = (
        original.subject
        if original.subject.lower().startswith("re:")
        else (f"Re: {original.subject}")
    )
    if not original.rfc_message_id:
        raise GmailError("Incoming message lacks Message-ID; cannot safely thread a reply")
    message["In-Reply-To"] = original.rfc_message_id
    message["References"] = " ".join(filter(None, (original.references, original.rfc_message_id)))
    digest = hashlib.sha256(f"{mailbox}:{original.message_id}".encode()).hexdigest()
    message["Message-ID"] = f"<senpilot-{digest}@{mailbox.split('@')[-1]}>"
    message["Auto-Submitted"] = "auto-replied"
    message.set_content(body)
    if attachment:
        # Read at most the limit + 1 bytes, even if the file changes after stat().
        if attachment.stat().st_size > MAX_ATTACHMENT_BYTES:
            raise AttachmentTooLarge("ZIP attachment exceeds the safe email size limit")
        with attachment.open("rb") as stream:
            data = stream.read(MAX_ATTACHMENT_BYTES + 1)
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentTooLarge("ZIP attachment is empty or exceeds the safe email size limit")
        message.add_attachment(
            data, maintype="application", subtype="zip", filename=attachment.name
        )
    if len(message.as_bytes()) > MAX_MESSAGE_BYTES:
        raise AttachmentTooLarge("Encoded message exceeds the safe email size limit")
    return message


class EmailClient:
    def __init__(
        self,
        config: Config | None = None,
        *,
        service=None,
        state_path: Path = Path("output/gmail_state.sqlite3"),
    ) -> None:
        self.config = config or Config.from_env()
        self._service = service
        self.state_path = state_path.resolve()
        self._store: MessageStore | None = None

    @property
    def store(self) -> MessageStore:
        if self._store is None:
            if not self.config.agent_email:
                raise AuthenticationRequired("AGENT_EMAIL is required for Gmail")
            self._store = MessageStore(self.state_path, self.config.agent_email)
        return self._store

    def authenticate(self, *, interactive: bool = False) -> str:
        self.config.validate_gmail()
        if self._service is None:
            token = self.config.gmail_token_path
            credentials = None
            if token.is_file():
                try:
                    credentials = Credentials.from_authorized_user_file(str(token))
                except (ValueError, OSError):
                    raise AuthenticationRequired(
                        "Stored Gmail token is invalid; reauthorize"
                    ) from None
                if not credentials.has_scopes(SCOPES):
                    credentials = None
                elif credentials.expired and credentials.refresh_token:
                    try:
                        credentials.refresh(Request())
                    except RefreshError as error:
                        if error.retryable:
                            raise GmailError(
                                "Gmail token refresh temporarily unavailable"
                            ) from None
                        credentials = None
                    except TransportError:
                        raise GmailError(
                            "Cannot refresh Gmail credentials: network unavailable"
                        ) from None
            if not credentials or not credentials.valid:
                if not interactive:
                    raise AuthenticationRequired("Run uv run python -m src.main --gmail-auth")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.config.oauth_credentials_path),
                    SCOPES,
                )
                credentials = flow.run_local_server(
                    port=0,
                    login_hint=self.config.agent_email,
                    timeout_seconds=180,
                    authorization_prompt_message="Authorize the Gmail account in your browser.",
                    success_message="Authorization received. You can close this window.",
                )
            self._service = build(
                "gmail",
                "v1",
                http=AuthorizedHttp(credentials, http=Http(timeout=30)),
                cache_discovery=False,
            )
            account = self._verify_mailbox()
            # Tokens are only persisted after verifying the actual mailbox.
            token.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", dir=token.parent, delete=False) as stream:
                staging = Path(stream.name)
                try:
                    os.chmod(staging, 0o600)
                    stream.write(credentials.to_json())
                    stream.flush()
                    os.replace(staging, token)
                finally:
                    staging.unlink(missing_ok=True)
            _ = self.store  # Establish the first-run cutoff before accepting requests.
            return account
        account = self._verify_mailbox()
        _ = self.store
        return account

    def _verify_mailbox(self) -> str:
        try:
            account = _read(self._service.users().getProfile(userId="me"))["emailAddress"]
            if account.casefold() != self.config.agent_email.casefold():
                raise AuthenticationRequired("Authenticated mailbox does not match AGENT_EMAIL")
            return account
        except Exception:
            self._service = None
            raise

    def _api(self):
        if self._service is None:
            self.authenticate()
        return self._service.users().messages()

    def receive(self) -> list[IncomingEmail]:
        api = self._api()
        store = self.store
        query = f"after:{store.cutoff_ms // 1000 - 1} -in:sent -in:drafts -in:trash"
        received = []
        page_token = None
        while True:
            page = _read(
                api.list(
                    userId="me",
                    q=query,
                    pageToken=page_token,
                    maxResults=100,
                    includeSpamTrash=True,
                )
            )
            for item in page.get("messages", []):
                if store.status(item["id"]) is not None:
                    continue
                data = _read(api.get(userId="me", id=item["id"], format="raw"))
                if int(data["internalDate"]) < store.cutoff_ms:
                    continue
                message = parse_message(data, self.config.agent_email)
                if message:
                    received.append((int(data["internalDate"]), message))
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return [message for _, message in sorted(received, key=lambda item: item[0])]

    def send_reply(
        self,
        original: IncomingEmail,
        body: str,
        attachment: Path | None = None,
    ) -> None:
        api = self._api()
        if self.store.status(original.message_id) is not None:
            raise AlreadyHandled("Message already handled or awaiting delivery review")
        message = build_reply(original, body, self.config.agent_email, attachment)
        payload = {
            "threadId": original.thread_id,
            "raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii"),
        }
        self.store.claim(original.message_id)
        try:
            # Never let the library retry a possibly accepted send request.
            response = api.send(userId="me", body=payload).execute(num_retries=0)
        except HttpError as error:
            if error.resp.status in {400, 401, 403, 404, 413, 429}:
                self.store.release(original.message_id)
                if error.resp.status == 413:
                    raise AttachmentTooLarge("Gmail rejected the attachment size") from None
                if error.resp.status == 401:
                    raise AuthenticationRequired(
                        "Gmail authorization required; run --gmail-auth"
                    ) from None
                raise GmailError(f"Gmail rejected reply (HTTP {error.resp.status})") from None
            raise DeliveryUncertain(
                "Reply delivery is uncertain; automatic resend suppressed"
            ) from None
        except Exception:
            raise DeliveryUncertain(
                "Reply delivery is uncertain; automatic resend suppressed"
            ) from None
        try:
            if not response.get("id"):
                raise ValueError("no send receipt")
            if response.get("threadId", original.thread_id) != original.thread_id:
                raise ValueError("reply thread did not match")
            self.store.complete(original.message_id, response["id"])
        except Exception:
            raise DeliveryUncertain(
                "Reply may be delivered but state could not be recorded; "
                "automatic resend suppressed"
            ) from None
