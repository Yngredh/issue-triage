"""Ferramentas do especialista numa pasta temporária (Etapa 7; D11, D22; RF14, RF15, RNF11)."""

import json
from pathlib import Path
from typing import Any

import pytest
import structlog

from issue_triage.adapters.catalog_tools import (
    CatalogTools,
    docs_dir,
    read_doc_text,
    snippet,
)


def _tools(folder: Path, *, max_tool_calls: int = 8, max_read_chars: int = 20000) -> CatalogTools:
    return CatalogTools(folder, max_tool_calls=max_tool_calls, max_read_chars=max_read_chars)


def _write(folder: Path, name: str, text: str) -> None:
    (folder / name).write_bytes(text.encode("utf-8"))


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert [item["type"] for item in result["content"]] == ["text"]
    return json.loads(result["content"][0]["text"])


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    assert result["is_error"] is False
    return _payload(result)


def _error(result: dict[str, Any]) -> str:
    assert result["is_error"] is True
    payload = _payload(result)
    assert set(payload) == {"error", "detail"}
    return payload["error"]


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    project = tmp_path / "mechanical-hub-orders"
    project.mkdir()
    return project


def test_docs_dir_is_the_repository_name_inside_the_source_dir() -> None:
    assert docs_dir("catalog/source", "Yaane-h/mechanical-hub-orders") == Path(
        "catalog/source/mechanical-hub-orders"
    )


def test_list_docs_shows_only_md_files_in_the_folder_in_alphabetical_order(folder: Path) -> None:
    _write(folder, "b.md", "B")
    _write(folder, "a.md", "A")
    _write(folder, "notes.txt", "fora")
    (folder / "sub").mkdir()
    _write(folder / "sub", "c.md", "subpasta")

    assert _ok(_tools(folder).call("list_docs", {})) == {
        "files": ["a.md", "b.md"],
        "truncated": False,
    }


def test_list_docs_is_limited_to_50_files(folder: Path) -> None:
    for index in range(51):
        _write(folder, f"doc{index:02d}.md", "x")

    payload = _ok(_tools(folder).call("list_docs", {}))

    assert payload["files"] == [f"doc{index:02d}.md" for index in range(50)]
    assert payload["truncated"] is True


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "..",
        "sub/c.md",
        "sub\\c.md",
        "/etc/passwd.md",
        "C:\\docs\\a.md",
        "C:a.md",
        "a.txt",
        "a",
        "",
    ],
)
def test_path_traversal_and_non_md_are_refused(folder: Path, path: str) -> None:
    _write(folder, "a.md", "A")
    _write(folder, "a.txt", "A")
    _write(folder.parent, "outside.md", "fora da pasta")
    (folder / "sub").mkdir()
    _write(folder / "sub", "c.md", "subpasta")
    tools = _tools(folder)

    assert _error(tools.call("read_doc", {"path": path})) == "path_not_allowed"


def test_absolute_path_inside_the_folder_is_refused(folder: Path) -> None:
    _write(folder, "a.md", "A")

    result = _tools(folder).call("read_doc", {"path": str(folder / "a.md")})

    assert _error(result) == "path_not_allowed"


def test_missing_md_file_is_file_not_found(folder: Path) -> None:
    assert _error(_tools(folder).call("read_doc", {"path": "missing.md"})) == "file_not_found"


def test_read_doc_cuts_at_4000_characters_and_continues_from_next_offset(folder: Path) -> None:
    text = "".join(chr(ord("a") + index % 26) for index in range(4500))
    _write(folder, "long.md", text)
    tools = _tools(folder)

    first = _ok(tools.call("read_doc", {"path": "long.md"}))
    second = _ok(tools.call("read_doc", {"path": "long.md", "offset": first["next_offset"]}))

    assert first == {
        "path": "long.md",
        "offset": 0,
        "content": text[:4000],
        "truncated": True,
        "next_offset": 4000,
    }
    assert second == {
        "path": "long.md",
        "offset": 4000,
        "content": text[4000:],
        "truncated": False,
        "next_offset": None,
    }
    assert tools.read_chars == 4500


def test_read_doc_reads_in_text_mode_with_lf(folder: Path) -> None:
    _write(folder, "crlf.md", "linha 1\r\nlinha 2\r\nação\r\n")

    payload = _ok(_tools(folder).call("read_doc", {"path": "crlf.md"}))

    assert payload["content"] == "linha 1\nlinha 2\nação\n"
    assert read_doc_text(folder, "crlf.md") == payload["content"]


def test_search_ignores_accents_and_case_and_needs_every_term(folder: Path) -> None:
    _write(
        folder,
        "b.md",
        "Introdução\nA ORDEM de serviço muda de status.\nSó a ordem.\nordem, Servico e status\n",
    )
    _write(folder, "a.md", "Nada aqui.\nOrdem de SERVIÇO aberta.\n")

    payload = _ok(_tools(folder).call("search_docs", {"query": "  ordem   servico "}))

    assert [(match["path"], match["line"]) for match in payload["matches"]] == [
        ("a.md", 2),
        ("b.md", 2),
        ("b.md", 4),
    ]
    assert payload["truncated"] is False
    assert all(set(match) == {"path", "line", "snippet"} for match in payload["matches"])


def test_search_returns_at_most_10_matches(folder: Path) -> None:
    _write(folder, "a.md", "\n".join(f"estoque {index}" for index in range(8)))
    _write(folder, "b.md", "\n".join(f"estoque {index}" for index in range(8)))

    payload = _ok(_tools(folder).call("search_docs", {"query": "ESTOQUE"}))

    assert len(payload["matches"]) == 10
    assert [m["path"] for m in payload["matches"]] == ["a.md"] * 8 + ["b.md"] * 2
    assert payload["truncated"] is True


@pytest.mark.parametrize("query", ["", "   ", "\t\n"])
def test_empty_query_is_refused(folder: Path, query: str) -> None:
    _write(folder, "a.md", "texto")
    tools = _tools(folder)

    assert _error(tools.call("search_docs", {"query": query})) == "empty_query"
    assert tools.limit is None


def test_snippet_is_centered_on_the_middle_of_the_line(folder: Path) -> None:
    before = "x" * 500 + "\n"
    line = "o pagamento falhou"
    text = before + line + "\n" + "y" * 500
    _write(folder, "a.md", text)

    payload = _ok(_tools(folder).call("search_docs", {"query": "pagamento"}))

    start = len(before)
    middle = start + len(line) // 2
    assert payload["matches"][0]["snippet"] == text[middle - 150 : middle + 150]
    assert len(payload["matches"][0]["snippet"]) == 300


def test_snippet_does_not_pass_the_start_nor_the_end_of_the_file() -> None:
    text = "linha curta\n" + "z" * 1000
    assert snippet(text, 0, 11) == text[: 5 + 150]
    tail = "a" * 1000 + "\nfim"
    assert snippet(tail, 1001, 1004) == tail[1002 - 150 :]


def test_snippet_of_a_long_line_is_its_first_300_characters() -> None:
    text = "antes\n" + "L" * 100 + "M" * 400 + "\ndepois"
    assert snippet(text, 6, 506) == "L" * 100 + "M" * 200


def test_tool_call_limit_refuses_the_extra_call_and_marks_the_session(folder: Path) -> None:
    _write(folder, "a.md", "A")
    tools = _tools(folder, max_tool_calls=2)

    _ok(tools.call("list_docs", {}))
    assert _error(tools.call("read_doc", {"path": "../x.md"})) == "path_not_allowed"
    assert tools.limit is None  # erro de caminho conta como chamada, mas não encerra
    assert not tools.limit_reached.is_set()
    extra = tools.call("read_doc", {"path": "a.md"})

    assert _error(extra) == "limit_exceeded"
    assert "tool_calls" in _payload(extra)["detail"]
    assert tools.limit == "tool_calls"
    assert tools.limit_reached.is_set()
    assert tools.read_chars == 0  # a chamada recusada não foi executada
    assert [(call.name, call.error) for call in tools.calls] == [
        ("list_docs", None),
        ("read_doc", "path_not_allowed"),
        ("read_doc", "limit_exceeded"),
    ]


def test_read_limit_refuses_the_call_that_would_pass_it(folder: Path) -> None:
    _write(folder, "a.md", "a" * 3000)
    _write(folder, "b.md", "b" * 3000)
    tools = _tools(folder, max_read_chars=5000)

    _ok(tools.call("list_docs", {}))  # nomes não contam
    _ok(tools.call("read_doc", {"path": "a.md"}))
    refused = tools.call("read_doc", {"path": "b.md"})

    assert _error(refused) == "limit_exceeded"
    assert "read_chars" in _payload(refused)["detail"]
    assert tools.limit == "read_chars"
    assert tools.limit_reached.is_set()
    assert tools.read_chars == 3000
    # Depois do estouro, a sessão está encerrada: nada mais é executado.
    assert _error(tools.call("list_docs", {})) == "limit_exceeded"


def test_search_snippets_count_toward_the_read_limit(folder: Path) -> None:
    _write(folder, "a.md", "\n".join(["pagamento"] * 3))
    tools = _tools(folder, max_read_chars=100)

    _ok(tools.call("search_docs", {"query": "pagamento"}))
    first = tools.read_chars

    assert 50 < first <= 100
    assert _error(tools.call("search_docs", {"query": "pagamento"})) == "limit_exceeded"
    assert tools.read_chars == first


def test_every_call_is_recorded_for_tool_calls(folder: Path) -> None:
    _write(folder, "a.md", "a" * 4100)
    tools = _tools(folder)

    with structlog.testing.capture_logs() as logs:
        result = tools.call("read_doc", {"path": "a.md"})
        tools.call("read_doc", {"path": "nope.md"})

    first, second = tools.tool_calls()
    assert set(first) == {"name", "arguments", "duration_ms", "result_chars", "truncated", "error"}
    assert first["name"] == "read_doc"
    assert first["arguments"] == {"path": "a.md"}
    assert first["duration_ms"] >= 0
    assert first["result_chars"] == len(result["content"][0]["text"])
    assert (first["truncated"], first["error"]) == (True, None)
    assert (second["truncated"], second["error"]) == (False, "file_not_found")
    tool_logs = [entry for entry in logs if entry["event"] == "triage.tool_call"]
    assert [entry["tool"] for entry in tool_logs] == ["read_doc", "read_doc"]
    assert "a" * 100 not in repr(logs)
