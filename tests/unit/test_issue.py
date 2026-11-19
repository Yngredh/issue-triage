import pytest

from issue_triage.domain.issue import is_blank


@pytest.mark.parametrize(
    "text",
    ["", "   ", "\n", "\r\n\r\n", "\t\t", " \t\r\n\v\f ", "  "],
    ids=["empty", "spaces", "newline", "crlf", "tabs", "mixed", "unicode-spaces"],
)
def test_blank_text_is_empty_description(text: str) -> None:
    assert is_blank(text)


@pytest.mark.parametrize("text", ["a", "  ok  ", "\n\tpasso 1\n"])
def test_text_with_content_is_not_blank(text: str) -> None:
    assert not is_blank(text)
