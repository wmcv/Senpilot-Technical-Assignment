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
from dataclasses import asdict, dataclass
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
from src.models import DocumentError, DocumentType, IncomingEmail, ParseResult
from src.packager import Packager, ZipBatch

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


@dataclass(frozen=True)
class PreparedReply:
    raw: bytes
    filenames: tuple[str, ...] = ()
    errors: tuple[DocumentError, ...] = ()


class MessageStore:
    """Persist IDs, validated requests, and unfinished outgoing MIME parts.

    Incoming bodies are never stored. 'sending' requires manual review;
    conversation state changes only after all send receipts are saved.
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
            db.execute(
                "CREATE TABLE IF NOT EXISTS deliveries ("
                "message_id TEXT PRIMARY KEY, original_json TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS delivery_parts ("
                "message_id TEXT, part INTEGER, raw BLOB, filenames TEXT NOT NULL, "
                "status TEXT NOT NULL, reply_id TEXT, PRIMARY KEY(message_id, part))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS delivery_errors ("
                "message_id TEXT, document TEXT, error TEXT, "
                "PRIMARY KEY(message_id, document, error))"
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

    def stage_delivery(self, original: IncomingEmail, parts: tuple[PreparedReply, ...]) -> None:
        if not parts:
            raise ValueError("delivery requires at least one reply")
        # Only reply-routing headers are retained, never the incoming body.
        routing = asdict(original) | {"body": ""}
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO messages (id, status) VALUES (?, 'delivering')",
                    (original.message_id,),
                )
                db.execute(
                    "INSERT INTO deliveries VALUES (?, ?)",
                    (original.message_id, json.dumps(routing)),
                )
                db.execute(
                    "INSERT OR REPLACE INTO conversation_updates VALUES (?, ?, ?, NULL, 0, ?)",
                    (
                        original.message_id,
                        original.thread_id,
                        _address(original.sender).casefold(),
                        "delivering",
                    ),
                )
                db.executemany(
                    "INSERT INTO delivery_parts VALUES (?, ?, ?, ?, 'pending', NULL)",
                    [
                        (original.message_id, index, part.raw, json.dumps(part.filenames))
                        for index, part in enumerate(parts, 1)
                    ],
                )
                db.executemany(
                    "INSERT OR IGNORE INTO delivery_errors VALUES (?, ?, ?)",
                    [
                        (original.message_id, error.document, error.error)
                        for part in parts
                        for error in part.errors
                    ],
                )
        except sqlite3.IntegrityError:
            raise AlreadyHandled("Delivery already staged") from None

    def resumable(self) -> list[IncomingEmail]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT d.original_json FROM deliveries d JOIN messages m ON m.id=d.message_id "
                "WHERE m.status='delivering' ORDER BY d.rowid"
            ).fetchall()
        return [IncomingEmail(**json.loads(row[0])) for row in rows]

    def record_errors(self, message_id: str, errors: tuple[DocumentError, ...]) -> None:
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO delivery_errors VALUES (?, ?, ?)",
                [(message_id, error.document, error.error) for error in errors],
            )

    def parts(self, message_id: str, *, include_payload: bool = True) -> list[tuple]:
        payload = "raw" if include_payload else "NULL"
        with self.connect() as db:
            return db.execute(
                f"SELECT part, {payload}, filenames, status, reply_id FROM delivery_parts "
                "WHERE message_id=? ORDER BY part",
                (message_id,),
            ).fetchall()

    def part_payload(self, message_id: str, part: int) -> bytes | None:
        with self.connect() as db:
            return db.execute(
                "SELECT raw FROM delivery_parts WHERE message_id=? AND part=?", (message_id, part)
            ).fetchone()[0]

    def claim_part(self, message_id: str, part: int) -> None:
        with self.connect() as db:
            claimed = db.execute(
                "UPDATE messages SET status='sending' WHERE id=? AND status='delivering'",
                (message_id,),
            ).rowcount
            changed = db.execute(
                "UPDATE delivery_parts SET status='sending' "
                "WHERE message_id=? AND part=? AND status='pending'",
                (message_id, part),
            ).rowcount
            if not claimed or not changed:
                raise AlreadyHandled("Delivery part already handled or awaiting review")

    def release_part(self, message_id: str, part: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE delivery_parts SET status='pending' "
                "WHERE message_id=? AND part=? AND status='sending'",
                (message_id, part),
            )
            db.execute("UPDATE messages SET status='delivering' WHERE id=?", (message_id,))

    def complete_part(self, message_id: str, part: int, reply_id: str) -> None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE delivery_parts SET status='accepted', reply_id=?, raw=NULL "
                "WHERE message_id=? AND part=? AND status='sending'",
                (reply_id, message_id, part),
            ).rowcount
            if not changed:
                raise GmailError("Delivery part was not claimed")
            db.execute("UPDATE messages SET status='delivering' WHERE id=?", (message_id,))

    def complete(self, message_id: str, reply_id: str) -> None:
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM delivery_parts WHERE message_id=? AND status!='accepted'",
                (message_id,),
            ).fetchone():
                raise GmailError("Delivery still has unresolved parts")
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
            db.execute("DELETE FROM deliveries WHERE message_id=?", (message_id,))

    def release(self, message_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM messages WHERE id=? AND status='sending'", (message_id,))
            db.execute("DELETE FROM conversation_updates WHERE message_id=?", (message_id,))

    def pending(self, thread_id: str, sender: str) -> tuple[ParseResult, int, str] | None:
        sender = _address(sender).casefold()
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM conversation_updates u JOIN messages m ON m.id=u.message_id "
                "WHERE u.thread_id=? AND u.sender=? AND m.status IN ('sending', 'delivering')",
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
    attachment: Path | tuple[Path, ...] | None = None,
    *,
    part: tuple[int, int] | None = None,
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
    # Part labels belong in the body: changing the subject can split Gmail threads.
    if not original.rfc_message_id:
        raise GmailError("Incoming message lacks Message-ID; cannot safely thread a reply")
    message["In-Reply-To"] = original.rfc_message_id
    message["References"] = " ".join(filter(None, (original.references, original.rfc_message_id)))
    identity = f"{mailbox}:{original.message_id}" + (f":part:{part[0]}" if part else "")
    digest = hashlib.sha256(identity.encode()).hexdigest()
    message["Message-ID"] = f"<senpilot-{digest}@{mailbox.split('@')[-1]}>"
    message["Auto-Submitted"] = "auto-replied"
    message.set_content(body)
    attachments = (attachment,) if isinstance(attachment, Path) else (attachment or ())
    for attachment in attachments:
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
        state_path: Path | None = None,
    ) -> None:
        self.config = config or Config.from_env()
        self._service = service
        self.state_path = (
            state_path if state_path is not None else self.config.gmail_state_path
        ).resolve()
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
                if not self.config.gmail_allow_interactive_auth:
                    raise AuthenticationRequired(
                        "Gmail authorization required. Authorize locally with --gmail-auth, "
                        "then securely provision the Gmail token at GMAIL_TOKEN_PATH. "
                        "Interactive authorization is disabled."
                    )
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

    def prepare_delivery(
        self,
        original: IncomingEmail,
        summary: str,
        files: tuple[Path, ...],
        packager: Packager,
        directory: Path,
        *,
        matter_id: str | None = None,
        document_type: DocumentType | None = None,
        errors: tuple[DocumentError, ...] = (),
        available_count: int | None = None,
    ) -> tuple[PreparedReply, ...]:
        """Prepare complete messages; prefer one, then first-fit decreasing ZIP groups.

        Each fit check serializes the entire MIME message. Bodies for follow-ups
        are deliberately short; only the first reply carries the matter summary.
        """
        if len(files) > 10:
            raise ValueError("at most ten downloaded files may be delivered")
        mailbox = self.config.agent_email
        category = document_type.value if document_type else "documents"
        context = f"{category} for matter {matter_id}" if matter_id else category
        signoff = "\n\nBest,\nSenpilot Document Agent"
        failures: tuple[DocumentError, ...] = ()

        words = (
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
        )

        def body(index, total, members, deliverable, zip_count=0):
            unit = "document" if members == 1 else "documents"
            if index > 1:
                sentence = (
                    f"Attached is the final set of {context} ({members} {unit})."
                    if index == total
                    else "Following up on my previous email, I've attached the next set of "
                    f"{context} ({members} {unit})."
                )
                return f"Hi,\n\n{sentence}{signoff}"
            text = summary.removesuffix(signoff)
            label = category.removesuffix("s") if available_count == 1 else category
            sentence = f"I retrieved {len(files)}"
            sentence += (
                f" of the {available_count} {label}" if available_count is not None else f" {label}"
            )
            if members:
                archives = (
                    "in a ZIP file" if zip_count == 1 else (f"across {words[zip_count]} ZIP files")
                )
                sentence += f" and attached {words[members]} {archives}."
            elif total == 1:
                sentence += ", but none could be attached."
            else:
                sentence += "."
            oversized = sum(
                error.error == "exceeds safe encoded message limit" for error in failures
            )
            if oversized and oversized == len(failures):
                if total == 1 and members and oversized == len(files) - members:
                    subject = f"The remaining {words[oversized]}"
                else:
                    noun = "document" if oversized == 1 else "documents"
                    subject = f"{words[oversized].capitalize()} {noun}"
                sentence += f" {subject} exceeded the email attachment limit."
            elif failures:
                noun = "document" if len(failures) == 1 else "documents"
                sentence += f" {words[len(failures)].capitalize()} {noun} could not be included"
                if oversized:
                    sentence += (
                        f", including {words[oversized]} that exceeded the email attachment limit"
                    )
                sentence += "."
            previous_sentence = f"I retrieved {len(files)} of {available_count} {category}."
            if available_count is not None and previous_sentence in text.splitlines():
                text = text.replace(previous_sentence, sentence, 1)
            else:
                text += "\n\n" + sentence
            if total > 1:
                included = (
                    f"all {deliverable} retrieved documents"
                    if deliverable == len(files)
                    else f"{deliverable} of the {len(files)} retrieved documents"
                )
                if members:
                    text += f"\n\nI'll send {included} across {total} separate replies."
                else:
                    following = (
                        "a separate reply" if total == 2 else f"{total - 1} separate replies"
                    )
                    text += (
                        f"\n\nI can send {included}. The attachments will follow in {following}."
                    )
            return text + signoff

        def message(group, index, total, deliverable):
            return build_reply(
                original,
                body(
                    index,
                    total,
                    sum(len(batch.filenames) for batch in group),
                    deliverable,
                    len(group),
                ),
                mailbox,
                tuple(batch.archive for batch in group),
                part=(index, total),
            )

        def fits_archive(archive):
            # A file that cannot accompany the full summary may still fit in a
            # short follow-up. The first reply can carry just the summary then.
            try:
                build_reply(original, body(2, 3, 10, 10), mailbox, archive, part=(2, 3))
            except AttachmentTooLarge:
                return False
            return True

        batches, failures = packager.batches(files, directory, fits_archive)
        deliverable = sum(len(batch.filenames) for batch in batches)

        def fits(group, index, total):
            # Base64 alone is a cheap lower bound; accepted groups still pass
            # the full MIME check, including text, headers and attachment framing.
            if sum(4 * ((batch.archive.stat().st_size + 2) // 3) for batch in group) > (
                MAX_MESSAGE_BYTES
            ):
                return False
            try:
                message(group, index, total, deliverable)
            except AttachmentTooLarge:
                return False
            return True

        if fits(batches, 1, 1):
            groups = [list(batches)]
        else:
            groups: list[list[ZipBatch]] = [[]]
            # At most ten ZIPs plus a summary-only first email. This two-digit
            # count conservatively sizes the first email until grouping is known.
            for batch in sorted(
                batches, key=lambda item: item.archive.stat().st_size, reverse=True
            ):
                for index, group in enumerate(groups, 1):
                    if fits([*group, batch], index, 11):
                        group.append(batch)
                        break
                else:
                    groups.append([batch])
            # Shorter final bodies or the final email count may permit a merge.
            while True:
                merged = False
                for first in range(len(groups)):
                    for second in range(first + 1, len(groups)):
                        if fits(groups[first] + groups[second], first + 1, len(groups) - 1):
                            groups[first].extend(groups.pop(second))
                            merged = True
                            break
                    if merged:
                        break
                if not merged:
                    break
        prepared = []
        for index, group in enumerate(groups, 1):
            raw = message(group, index, len(groups), deliverable).as_bytes()
            names = tuple(name for batch in group for name in batch.filenames)
            prepared.append(PreparedReply(raw, names, (*errors, *failures) if index == 1 else ()))
        return tuple(prepared)

    def send_delivery(self, original: IncomingEmail, parts: tuple[PreparedReply, ...]) -> None:
        self.store.stage_delivery(original, parts)
        self.resume_delivery(original)

    def resume_delivery(self, original: IncomingEmail) -> None:
        """Use persisted bytes only; never redownload or regenerate accepted parts."""
        if self.store.status(original.message_id) != "delivering":
            raise AlreadyHandled("Delivery already handled or awaiting review")
        saved = next(
            (item for item in self.store.resumable() if item.message_id == original.message_id),
            None,
        )
        if saved is None or (
            saved.thread_id != original.thread_id
            or _address(saved.sender).casefold() != _address(original.sender).casefold()
        ):
            raise GmailError("Delivery routing does not match the saved request")
        api = self._api()
        rows = self.store.parts(original.message_id, include_payload=False)
        for part, _, _, status, _ in rows:
            if status == "accepted":
                continue
            raw = self.store.part_payload(original.message_id, part)
            if status != "pending" or raw is None:
                raise DeliveryUncertain("Delivery part requires review; sending held")
            self.store.claim_part(original.message_id, part)
            try:
                response = api.send(
                    userId="me",
                    body={
                        "threadId": saved.thread_id,
                        "raw": base64.urlsafe_b64encode(raw).decode("ascii"),
                    },
                ).execute(num_retries=0)
            except HttpError as error:
                if error.resp.status in {400, 401, 403, 404, 413, 429}:
                    self.store.release_part(original.message_id, part)
                    if error.resp.status == 401:
                        raise AuthenticationRequired("Gmail authorization required") from None
                    raise GmailError(
                        f"Gmail rejected ZIP part (HTTP {error.resp.status})"
                    ) from None
                raise DeliveryUncertain("ZIP part delivery uncertain; sending held") from None
            except Exception:
                raise DeliveryUncertain("ZIP part delivery uncertain; sending held") from None
            try:
                if not response.get("id") or response.get("threadId", saved.thread_id) != (
                    saved.thread_id
                ):
                    raise ValueError("invalid send receipt")
                self.store.complete_part(original.message_id, part, response["id"])
            except Exception:
                raise DeliveryUncertain(
                    "ZIP part receipt could not be saved; sending held"
                ) from None
        # If this final transaction fails, every accepted part stays recorded and
        # the next run retries only completion, without sending another message.
        try:
            receipts = self.store.parts(original.message_id, include_payload=False)
            self.store.complete(original.message_id, receipts[-1][4])
        except sqlite3.Error:
            raise GmailError(
                "Delivery receipts saved; completion pending. Accepted parts will not be resent."
            ) from None

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
