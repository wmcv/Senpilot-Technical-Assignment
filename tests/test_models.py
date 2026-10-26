from pathlib import Path

import pytest

from src.config import Config
from src.models import (
    DocumentError,
    DocumentType,
    DownloadResult,
    IncomingEmail,
    MatterMetadata,
    ParsedRequest,
    ParseResult,
    ProcessingResult,
)


def test_valid_contracts():
    request = ParsedRequest("M12205", DocumentType.EXHIBITS)
    assert ParseResult(request.matter_id, request.document_type).require_request() == request
    assert DownloadResult((Path("exhibit.pdf"),)).files
    assert IncomingEmail("id", "thread", "sender@example.com", "", "").message_id == "id"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ParsedRequest("m12205", DocumentType.EXHIBITS),
        lambda: ParsedRequest("M123456", DocumentType.EXHIBITS),
        lambda: ParsedRequest("M12205", "Exhibits"),
        lambda: ParsedRequest("M12205", None),
        lambda: IncomingEmail("", "thread", "sender", "", ""),
        lambda: ParseResult(),
        lambda: ParseResult("M12205", DocumentType.EXHIBITS, missing_fields=("matter_id",)),
        lambda: MatterMetadata("M12205", counts={DocumentType.EXHIBITS: -1}),
        lambda: MatterMetadata("M12205", counts={DocumentType.EXHIBITS: True}),
        lambda: MatterMetadata("M12205", counts={"Exhibits": 1}),
        lambda: DownloadResult((Path("a"), Path("a"))),
        lambda: DownloadResult(("a",)),
        lambda: DocumentError("doc", ""),
        lambda: ProcessingResult(11, 20, MatterMetadata("M12205")),
        lambda: ProcessingResult(2, 1, MatterMetadata("M12205")),
        lambda: ProcessingResult(0, -1, MatterMetadata("M12205")),
    ],
)
def test_invalid_contracts(factory):
    with pytest.raises(ValueError):
        factory()


def test_configuration_is_lazy(monkeypatch, tmp_path):
    for key in ("AGENT_EMAIL", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"):
        monkeypatch.delenv(key, raising=False)
    config = Config.from_env(tmp_path / "absent.env")
    assert config.agent_email is None
    with pytest.raises(ValueError):
        config.validate_gmail()
    with pytest.raises(ValueError):
        config.validate_openrouter()


def test_cancelled_request_contract():
    result = ParseResult(
        "M12345", None, ("document_type",), clarification="Cancelled.", cancelled=True
    )
    with pytest.raises(ValueError, match="cancelled"):
        result.require_request()
    with pytest.raises(ValueError):
        ParseResult("M12345", DocumentType.EXHIBITS, cancelled=True)
