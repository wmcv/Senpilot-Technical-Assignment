"""Orchestration expressed entirely in terms of public integration contracts."""

import fcntl
import os
import re
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from src.email_client import (
    AlreadyHandled,
    AuthenticationRequired,
    DeliveryUncertain,
    EmailClient,
    GmailError,
    MessageStore,
    PreparedReply,
)
from src.models import DocumentType, IncomingEmail, ParsedRequest, ParseResult, ProcessingResult
from src.packager import Packager
from src.parser import RequestFallback, interpret_request, parse_request
from src.uarb import UARBClient


def format_summary(
    result: ProcessingResult,
    document_type: DocumentType | None = None,
    *,
    archive_attached: bool = False,
    delivery_pending: bool = False,
) -> str:
    """Pure email body; errors count documents exhausted after the downloader's retry.

    A failed retry is not counted twice. Recovered transient errors are not exposed
    by DownloadResult and are therefore not claimed here.
    """
    if archive_attached and result.downloaded_count == 0:
        raise ValueError("cannot attach an archive with no downloaded files")
    metadata = result.metadata
    category = document_type.value if document_type else "documents (category unspecified)"
    description = (
        f"Matter {metadata.matter_id} concerns {metadata.title.rstrip('.')}."
        if metadata.title
        else f"Matter {metadata.matter_id} is listed in the UARB database."
    )
    if metadata.matter_type and metadata.category:
        description += (
            f" It falls under {metadata.matter_type} in the {metadata.category} category."
        )
    elif metadata.category:
        description += f" It is in the {metadata.category} category."
    elif metadata.matter_type:
        description += f" Its type is {metadata.matter_type}."
    if metadata.status:
        description += f" Its status is {metadata.status}."
    months = (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    )
    for label, value in sorted(metadata.filing_dates.items()):
        rendered = f"{months[value.month - 1]} {value.day}, {value.year}"
        normalized = label.casefold().strip()
        if normalized in {"received", "date received"}:
            description += f" The matter was received on {rendered}."
        elif normalized in {"decision", "decision date"}:
            description += f" Its decision was issued on {rendered}."
        else:
            description += f" The recorded {label.lower()} is {rendered}."
    if metadata.outcome:
        description += f" The recorded outcome is {metadata.outcome.rstrip('.')}."
    counts = []
    for item in DocumentType:
        count = metadata.counts.get(item)
        amount = "an unknown number of" if count is None else ("no" if count == 0 else str(count))
        counts.append(f"{amount} {item.value}")
    count_sentence = ", ".join(counts[:-1]) + ", and " + counts[-1]
    lines = [
        "Hi,",
        "",
        description,
        "",
        f"The database lists {count_sentence}.",
        "",
        f"I downloaded {result.downloaded_count} of {result.available_count} {category}.",
    ]
    if result.available_count == 0:
        lines.append("No files are available in the requested category. No ZIP is attached.")
    elif result.downloaded_count == 0:
        lines.append("No files were downloaded. No ZIP is attached.")
    elif archive_attached:
        lines.append("This reply contains the downloaded files attached as a ZIP file.")
    elif not delivery_pending:
        lines.append("No ZIP is attached.")
    if result.errors:
        lines.append(f"Failed document attempts (after retry): {len(result.errors)}.")
        lines.extend(f"Error [{error.document}]: {error.error}" for error in result.errors)
    lines.extend(["", "Best,", "Senpilot Document Agent"])
    return "\n".join(lines)


class Agent:
    def __init__(
        self,
        uarb: UARBClient,
        packager: Packager,
        email: EmailClient,
        *,
        store: MessageStore | None = None,
        fallback: RequestFallback | None = None,
    ) -> None:
        self.uarb = uarb
        self.packager = packager
        self.email = email
        self.store = store
        self.fallback = fallback

    def process(
        self,
        incoming: IncomingEmail,
        workspace: Path,
        previous: ParseResult | None = None,
    ) -> ParseResult | ProcessingResult | None:
        pending = None
        if self.store is not None:
            if self.store.status(incoming.message_id) == "delivering":
                self.email.resume_delivery(incoming)
                return None
            if self.store.status(incoming.message_id) is not None:
                raise AlreadyHandled("Message already handled or awaiting delivery review")
            pending = self.store.pending(incoming.thread_id, incoming.sender)
        turns = pending[1] if pending else 0
        limited = pending is not None and pending[2] == "limited"
        if previous is None and pending and not limited:
            previous = pending[0]
        # Reply subjects contain stale fields from older requests. Only the new
        # body participates in a reply or a pending clarification conversation.
        text = (
            incoming.body
            if pending
            or (self.store is not None and incoming.references)
            or re.match(r"\s*re\s*:", incoming.subject, re.IGNORECASE)
            else (f"{incoming.subject}\n{incoming.body}")
        )
        if (
            pending
            and pending[2] == "retry"
            and not re.search(
                r"\b(?:retry|try\s+again)\b",
                text,
                re.IGNORECASE,
            )
        ):
            # An acknowledgement of an error is not authorization to rerun it.
            fresh = parse_request(text)
            if fresh.matter_id is None and fresh.document_type is None:
                previous = None
        parsed = interpret_request(
            text,
            previous if not limited else None,
            self.fallback if not limited else None,
            allow_corrections=bool(pending),
        )
        if parsed.cancelled:
            if self.store is not None:
                # Cancellation takes effect even if its acknowledgement cannot be sent.
                self.store.discard_pending(incoming.thread_id, incoming.sender)
            self._send(
                incoming,
                "Your request has been cancelled. No documents were retrieved or attached.",
            )
            return parsed
        if parsed.missing_fields:
            turns = min(turns + 1, 3)
            if turns >= 3:
                body = (
                    "I still need a clear request. Please send both the matter number and "
                    "one category, for example: M12205 Key Documents. Supported categories: "
                    "Exhibits, Key Documents, Other Documents, Transcripts, Recordings."
                )
                # Stop carrying partial details or spending model calls after the limit.
                self._send(incoming, body, pending=parse_request(""), turns=turns, status="limited")
            else:
                self._send(
                    incoming,
                    parsed.clarification or "Please clarify your request.",
                    pending=parsed,
                    turns=turns,
                )
            return parsed
        request = parsed.require_request()
        try:
            result, delivery = self._retrieve(request, workspace, incoming)
        except LookupError:
            self._send(
                incoming,
                f"I could not find matter {request.matter_id} in the UARB database. "
                "Please check the matter number. No ZIP is attached.",
                pending=parse_request(request.document_type.value),
                turns=turns,
            )
            return None
        except Exception:
            self._send(
                incoming,
                f"I could not complete retrieval for {request.matter_id}. "
                "The database or document packaging may be temporarily unavailable. "
                "Please try again later. No ZIP is attached.",
                pending=parsed,
                turns=turns,
                status="retry",
            )
            return None
        if delivery:
            self.email.send_delivery(incoming, delivery)
        else:
            self._send(incoming, format_summary(result, request.document_type))
        return result

    def _send(
        self,
        incoming: IncomingEmail,
        body: str,
        attachment: Path | None = None,
        *,
        pending: ParseResult | None = None,
        turns: int = 0,
        status: str = "clarifying",
    ) -> None:
        if self.store is not None:
            self.store.stage_conversation(incoming, pending, turns, status)
        self.email.send_reply(incoming, body, attachment)

    def _retrieve(
        self, request: ParsedRequest, workspace: Path, incoming: IncomingEmail
    ) -> tuple[ProcessingResult, tuple[PreparedReply, ...] | None]:
        metadata = self.uarb.get_matter(request.matter_id)
        if metadata.matter_id != request.matter_id:
            raise ValueError("retrieved metadata does not match requested matter")
        if request.document_type not in metadata.counts:
            raise ValueError("requested category count is unavailable")
        available = metadata.counts[request.document_type]
        workspace.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=f"{request.matter_id}-", dir=workspace) as temporary:
            downloads = (
                self.uarb.download_documents(
                    request.matter_id,
                    request.document_type,
                    Path(temporary),
                    limit=min(10, available),
                )
                if available
                else None
            )
            result = ProcessingResult(
                downloaded_count=len(downloads.files) if downloads else 0,
                available_count=available,
                metadata=metadata,
                errors=downloads.errors if downloads else (),
            )
            delivery = (
                self.email.prepare_delivery(
                    incoming,
                    format_summary(result, request.document_type, delivery_pending=True),
                    downloads.files,
                    self.packager,
                    Path(temporary) / "batches",
                )
                if downloads and downloads.files
                else None
            )
        return result, delivery


def run_polling(agent: Agent, workspace: Path, interval: int = 15, *, once: bool = False) -> int:
    """Serial worker with a cross-process lock; only successful sends are processed.

    SQLite's pre-send claim survives a crash and suppresses uncertain sends on restart.
    A second worker using the same store is refused even during browser retrieval.
    """
    if interval <= 0:
        raise ValueError("poll interval must be positive")
    workspace.mkdir(parents=True, exist_ok=True)
    lock_path = agent.email.state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GmailError("Another Gmail worker is already using this state store") from None
        completed = 0
        while True:
            try:
                messages = agent.email.store.resumable() + agent.email.receive()
                for message in messages:
                    status = agent.email.store.status(message.message_id)
                    if status not in {None, "delivering"}:
                        continue
                    with TemporaryDirectory(prefix="request-", dir=workspace) as temporary:
                        try:
                            if status == "delivering":
                                agent.email.resume_delivery(message)
                            else:
                                agent.process(message, Path(temporary))
                            completed += 1
                        except AlreadyHandled:
                            continue
                        except DeliveryUncertain:
                            if once:
                                raise
                            print(
                                "Delivery uncertain; automatic resend suppressed. "
                                "Review the delivery ledger.",
                                file=sys.stderr,
                            )
                        except AuthenticationRequired:
                            raise
                        except GmailError:
                            if once:
                                raise
                            print(
                                "Gmail processing failed; pending messages will be retried.",
                                file=sys.stderr,
                            )
            except AuthenticationRequired:
                raise
            except GmailError:
                if once:
                    raise
                print("Gmail polling failed; retrying after the polling interval.", file=sys.stderr)
            if once:
                return completed
            time.sleep(interval)
