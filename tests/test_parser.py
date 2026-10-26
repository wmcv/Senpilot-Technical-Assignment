import pytest

from src.models import DocumentType
from src.parser import parse_request


@pytest.mark.parametrize("matter", ["M12205", "m00000", "M99999"])
def test_valid_matter(matter):
    assert parse_request(f"{matter} exhibits").matter_id == matter.upper()


@pytest.mark.parametrize("matter", ["", "M1234", "M123456", "AM12205", "M12205x", "M１２２０５"])
def test_invalid_or_missing_matter(matter):
    result = parse_request(f"{matter} exhibits")
    assert result.matter_id is None
    assert result.missing_fields == ("matter_id",)
    assert result.clarification


@pytest.mark.parametrize("category", list(DocumentType))
def test_all_categories(category):
    assert parse_request(f"M12205 {category.value}").document_type == category


@pytest.mark.parametrize(
    "text, expected",
    [
        ("  OTHER \n DOCUMENTS, please! m12205", DocumentType.OTHER_DOCUMENTS),
        ("Send TRANSCRIPTS for M12205", DocumentType.TRANSCRIPTS),
        ("M12205: key-docs", DocumentType.KEY_DOCUMENTS),
        ("exhibit for matter M12205", DocumentType.EXHIBITS),
        ("M12205 audio", DocumentType.RECORDINGS),
        ("M12205 Othre Documents", DocumentType.OTHER_DOCUMENTS),
        ("M12205 transcrips", DocumentType.TRANSCRIPTS),
    ],
)
def test_variations(text, expected):
    assert parse_request(text).require_request().document_type == expected


@pytest.mark.parametrize(
    "text, field",
    [
        ("M12205 exhibits or recordings", "document_type"),
        ("M12205 other documents or key documents", "document_type"),
        ("M12205 Othre Documents or exhibits", "document_type"),
        ("M12205 or M54321 exhibits", "matter_id"),
    ],
)
def test_ambiguity(text, field):
    result = parse_request(text)
    assert field in result.ambiguous_fields
    assert getattr(result, field) is None
    with pytest.raises(ValueError):
        result.require_request()


def test_no_guess_and_empty_input():
    assert parse_request("M12205 documents").document_type is None
    assert set(parse_request("").missing_fields) == {"matter_id", "document_type"}


@pytest.mark.parametrize(
    "initial, followup",
    [
        ("M12205", "Other Documents"),
        ("Other Documents", "M12205"),
        ("M12205 exhibits or other documents", "Other Documents"),
    ],
)
def test_followup(initial, followup):
    result = parse_request(followup, parse_request(initial))
    assert result.require_request().matter_id == "M12205"
    assert result.document_type == DocumentType.OTHER_DOCUMENTS


def test_followup_conflicts_do_not_overwrite():
    result = parse_request("M54321", parse_request("M12205"))
    assert result.ambiguous_fields == ("matter_id",)


def test_repeated_values_are_not_ambiguous():
    assert not parse_request("M12205 m12205 exhibit exhibits").missing_fields
