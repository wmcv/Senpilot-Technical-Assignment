"""Shared data contracts; no integration dependencies."""

import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Literal


class DocumentType(StrEnum):
    EXHIBITS = "Exhibits"
    KEY_DOCUMENTS = "Key Documents"
    OTHER_DOCUMENTS = "Other Documents"
    TRANSCRIPTS = "Transcripts"
    RECORDINGS = "Recordings"


RequestField = Literal["matter_id", "document_type"]


def validate_matter_id(value: str) -> None:
    if not re.fullmatch(r"M\d{5}", value, flags=re.ASCII):
        raise ValueError("matter_id must be uppercase M followed by five ASCII digits")


def validate_document_type(value: DocumentType | None) -> None:
    if value is not None and not isinstance(value, DocumentType):
        raise ValueError("document_type must be a DocumentType")


@dataclass(frozen=True)
class IncomingEmail:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    rfc_message_id: str | None = None
    references: str | None = None
    reply_to: str | None = None

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (self.message_id, self.thread_id, self.sender)):
            raise ValueError("message ID, thread ID, and sender must be nonempty")


@dataclass(frozen=True)
class ParsedRequest:
    matter_id: str
    document_type: DocumentType

    def __post_init__(self) -> None:
        validate_matter_id(self.matter_id)
        validate_document_type(self.document_type)
        if self.document_type is None:
            raise ValueError("document_type is required")


@dataclass(frozen=True)
class ParseResult:
    matter_id: str | None = None
    document_type: DocumentType | None = None
    missing_fields: tuple[RequestField, ...] = ()
    ambiguous_fields: tuple[RequestField, ...] = ()
    clarification: str | None = None
    cancelled: bool = False

    def __post_init__(self) -> None:
        if self.matter_id is not None:
            validate_matter_id(self.matter_id)
        validate_document_type(self.document_type)
        if type(self.cancelled) is not bool or (self.cancelled and self.document_type is not None):
            raise ValueError("cancelled requests cannot contain a selected document type")
        expected = {name for name in ("matter_id", "document_type") if getattr(self, name) is None}
        if set(self.missing_fields) != expected or len(set(self.missing_fields)) != len(
            self.missing_fields
        ):
            raise ValueError("missing_fields must exactly describe unresolved fields")
        if not set(self.ambiguous_fields) <= expected:
            raise ValueError("ambiguous fields must be unresolved")
        if expected and not self.clarification:
            raise ValueError("unresolved requests require clarification")

    def require_request(self) -> ParsedRequest:
        if self.cancelled:
            raise ValueError("request was cancelled")
        if self.matter_id is None or self.document_type is None:
            raise ValueError("request still requires clarification")
        return ParsedRequest(self.matter_id, self.document_type)


@dataclass(frozen=True)
class MatterMetadata:
    matter_id: str
    title: str | None = None
    status: str | None = None
    category: str | None = None
    matter_type: str | None = None
    filing_dates: dict[str, date] = field(default_factory=dict)
    outcome: str | None = None
    counts: dict[DocumentType, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_matter_id(self.matter_id)
        for category, count in self.counts.items():
            validate_document_type(category)
            if category is None or type(count) is not int or count < 0:
                raise ValueError("counts must be nonnegative integers keyed by DocumentType")
        if any(
            not key.strip() or not isinstance(value, date)
            for key, value in self.filing_dates.items()
        ):
            raise ValueError("filing dates require descriptive labels and date values")


@dataclass(frozen=True)
class DocumentError:
    document: str
    error: str

    def __post_init__(self) -> None:
        if not self.document.strip() or not self.error.strip():
            raise ValueError("document errors require an identifier and explanation")


@dataclass(frozen=True)
class DownloadResult:
    files: tuple[Path, ...] = ()
    errors: tuple[DocumentError, ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(path, Path) for path in self.files):
            raise ValueError("downloaded files must be Paths")
        if len(set(self.files)) != len(self.files):
            raise ValueError("downloaded paths must be unique")


@dataclass(frozen=True)
class ProcessingResult:
    downloaded_count: int
    available_count: int
    metadata: MatterMetadata
    errors: tuple[DocumentError, ...] = ()

    def __post_init__(self) -> None:
        if any(type(n) is not int for n in (self.downloaded_count, self.available_count)):
            raise ValueError("counts must be integers")
        if not 0 <= self.downloaded_count <= min(10, self.available_count):
            raise ValueError(
                "downloaded count must be between zero and available count, capped at ten"
            )
