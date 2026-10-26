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


def test_configurable_gmail_paths_preserve_local_defaults(monkeypatch, tmp_path):
    variables = (
        "GOOGLE_OAUTH_PATH",
        "GMAIL_TOKEN_PATH",
        "GMAIL_STATE_PATH",
        "GMAIL_ALLOW_INTERACTIVE_AUTH",
    )
    for name in variables:
        monkeypatch.delenv(name, raising=False)
    defaults = Config.from_env(tmp_path / "absent.env")
    assert defaults.google_oauth_path == Path("credentials/google_oauth.json")
    assert defaults.gmail_token_path == Path("credentials/gmail_token.json")
    assert defaults.gmail_state_path == Path("output/gmail_state.sqlite3")
    assert defaults.gmail_allow_interactive_auth
    monkeypatch.setenv("GOOGLE_OAUTH_PATH", str(tmp_path / "google.json"))
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.setenv("GMAIL_STATE_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("GMAIL_ALLOW_INTERACTIVE_AUTH", "false")
    configured = Config.from_env(tmp_path / "absent.env")
    assert configured.google_oauth_path == tmp_path / "google.json"
    assert configured.gmail_token_path == tmp_path / "token.json"
    assert configured.gmail_state_path == tmp_path / "state.sqlite3"
    assert not configured.gmail_allow_interactive_auth


def test_explicit_oauth_path_does_not_fall_back_to_another_file(tmp_path):
    (tmp_path / "client_secret_other.json").write_text("{}")
    config = Config(google_oauth_path=tmp_path / "missing.json")
    assert config.oauth_credentials_path == tmp_path / "missing.json"
