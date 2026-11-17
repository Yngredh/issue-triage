"""Ferramentas do especialista sobre a Base de conhecimento (D11, D22; RF14, RF15, RNF10, RNF11).

`list_docs`, `search_docs` e `read_doc` ficam presas à pasta `CATALOG_SOURCE_DIR/<repositório>`
do caso e só enxergam os arquivos `.md` diretamente nela, em ordem alfabética. As respostas são
JSON no conteúdo de texto do resultado MCP; erros vão com `is_error` e um código fixo. Os
contadores de chamadas e de caracteres lidos são da sessão (`CatalogTools`, uma por chamada ao
especialista): a chamada que estouraria um limite não é executada e marca a sessão para ser
encerrada na hora. O retorno das ferramentas é dado, nunca instrução (RNF09).
"""

import asyncio
import json
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import structlog

from issue_triage.domain.specialist import LIST_DOCS, READ_DOC, SEARCH_DOCS

logger = structlog.get_logger()

LIST_LIMIT = 50
SEARCH_LIMIT = 10
SNIPPET_CHARS = 300
READ_CHUNK_CHARS = 4000

PATH_NOT_ALLOWED = "path_not_allowed"
FILE_NOT_FOUND = "file_not_found"
EMPTY_QUERY = "empty_query"
LIMIT_EXCEEDED = "limit_exceeded"

# Motivos de `limit_exceeded` guardados na sessão.
TOOL_CALLS = "tool_calls"
READ_CHARS = "read_chars"

_DOC_SUFFIX = ".md"


class ToolError(Exception):
    """Erro de ferramenta devolvido ao modelo como `{"error": code, "detail": detail}`."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def docs_dir(source_dir: str | Path, repository: str) -> Path:
    """Pasta da Base de conhecimento do repositório (`<owner>/<nome>` → `source_dir/<nome>`)."""
    return Path(source_dir) / repository.split("/", 1)[-1]


def visible_docs(folder: Path) -> list[str]:
    """Nomes dos `.md` diretamente na pasta (sem subpastas), em ordem alfabética."""
    if not folder.is_dir():
        return []
    return sorted(
        entry.name
        for entry in folder.iterdir()
        if entry.name.endswith(_DOC_SUFFIX) and entry.is_file()
    )


def resolve_doc(folder: Path, path: str) -> Path:
    """Caminho do arquivo `path` dentro da pasta, ou `ToolError`.

    Recusa com `path_not_allowed` `..`, caminho absoluto, subpasta, arquivo que não é `.md` ou
    fora da pasta; `file_not_found` para um `.md` válido que não existe.
    """
    if (
        not path
        or ".." in path
        or "/" in path
        or "\\" in path
        or ":" in path
        or Path(path).is_absolute()
        or not path.endswith(_DOC_SUFFIX)
    ):
        raise ToolError(PATH_NOT_ALLOWED, "path must be a .md file name inside the project folder")
    root = folder.resolve()
    candidate = (root / path).resolve()
    if candidate.parent != root:
        raise ToolError(PATH_NOT_ALLOWED, "path must be a .md file name inside the project folder")
    if not candidate.is_file():
        raise ToolError(FILE_NOT_FOUND, f"file {path} does not exist")
    return candidate


def read_text(file: Path) -> str:
    """Lê o arquivo em modo texto (UTF-8, quebras de linha normalizadas para LF).

    É a mesma leitura do validador (checagem 4), para que as evidências batam com o que as
    ferramentas mostraram.
    """
    return file.read_text(encoding="utf-8")


def read_doc_text(folder: Path, path: str) -> str:
    """Texto de um documento da pasta, com as mesmas regras de caminho de `read_doc`."""
    return read_text(resolve_doc(folder, path))


def fold(text: str) -> str:
    """Forma de comparação da busca: `casefold` e sem acentos (NFKD sem marcas combinantes)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def snippet(text: str, start: int, end: int) -> str:
    """Janela de até 300 caracteres do texto centrada no meio da linha `text[start:end]`.

    Linha com 300 caracteres ou mais é cortada nos 300 primeiros; a janela não passa do início
    nem do fim do arquivo.
    """
    if end - start >= SNIPPET_CHARS:
        return text[start : start + SNIPPET_CHARS]
    middle = start + (end - start) // 2
    left = middle - SNIPPET_CHARS // 2
    return text[max(left, 0) : min(left + SNIPPET_CHARS, len(text))]


@dataclass(frozen=True)
class ToolCall:
    """Registro de uma chamada, gravado em `proposals.tool_calls` (RNF11).

    `paths` são os arquivos que a chamada devolveu (o arquivo de um `read_doc` sem erro, os
    arquivos dos trechos de um `search_docs`), usados pela checagem 4 do validador (D25).
    """

    name: str
    arguments: dict[str, Any]
    duration_ms: float
    result_chars: int
    truncated: bool
    error: str | None
    paths: list[str]


@dataclass(frozen=True)
class _Output:
    payload: dict[str, Any]
    read_chars: int
    truncated: bool
    paths: tuple[str, ...] = ()


class CatalogTools:
    """Sessão das ferramentas de uma chamada ao especialista, com os contadores (D22).

    `SPECIALIST_MAX_READ_CHARS` soma o `content` de cada `read_doc` e os `snippet` de cada
    `search_docs`. A chamada de número `max_tool_calls + 1`, ou a que passaria do limite de
    leitura, não é executada: devolve `limit_exceeded`, entra em `calls` e marca `limit` e
    `limit_reached`, para o harness encerrar a sessão na hora.
    """

    def __init__(self, folder: Path, *, max_tool_calls: int, max_read_chars: int) -> None:
        self.folder = folder
        self.max_tool_calls = max_tool_calls
        self.max_read_chars = max_read_chars
        self.read_chars = 0
        self.limit: str | None = None
        self.limit_reached = asyncio.Event()
        self._calls: list[ToolCall] = []

    @property
    def calls(self) -> list[ToolCall]:
        return list(self._calls)

    def tool_calls(self) -> list[dict[str, Any]]:
        """As chamadas no formato gravado em `proposals.tool_calls`."""
        return [asdict(call) for call in self._calls]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Executa uma ferramenta e devolve o resultado MCP (`content` e `is_error`)."""
        started = time.perf_counter()
        error: ToolError | None = None
        output: _Output | None = None
        if self.limit is not None:
            error = self._limit_error(self.limit)
        elif len(self._calls) >= self.max_tool_calls:
            error = self._limit_error(TOOL_CALLS)
        else:
            try:
                output = self._execute(name, arguments)
            except ToolError as exc:
                error = exc
            if output is not None and self.read_chars + output.read_chars > self.max_read_chars:
                output = None
                error = self._limit_error(READ_CHARS)
        if output is not None:
            self.read_chars += output.read_chars
            text = json.dumps(output.payload, ensure_ascii=False)
        else:
            assert error is not None
            text = json.dumps({"error": error.code, "detail": error.detail}, ensure_ascii=False)

        record = ToolCall(
            name=name,
            arguments=dict(arguments),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            result_chars=len(text),
            truncated=output.truncated if output is not None else False,
            error=error.code if error is not None else None,
            paths=list(output.paths) if output is not None else [],
        )
        self._calls.append(record)
        logger.info(
            "triage.tool_call",
            tool=record.name,
            duration_ms=record.duration_ms,
            result_chars=record.result_chars,
            truncated=record.truncated,
            error=record.error,
        )
        if self.limit is not None:
            self.limit_reached.set()
        return {"content": [{"type": "text", "text": text}], "is_error": error is not None}

    def _limit_error(self, limit: str) -> ToolError:
        self.limit = limit
        if limit == TOOL_CALLS:
            detail = f"tool call limit of {self.max_tool_calls} reached"
        else:
            detail = f"read limit of {self.max_read_chars} characters would be exceeded"
        return ToolError(LIMIT_EXCEEDED, f"{limit}: {detail}")

    def _execute(self, name: str, arguments: dict[str, Any]) -> _Output:
        if name == LIST_DOCS:
            return self._list_docs()
        if name == SEARCH_DOCS:
            return self._search_docs(arguments.get("query", ""))
        if name == READ_DOC:
            return self._read_doc(arguments.get("path", ""), arguments.get("offset", 0))
        raise ValueError(f"unknown tool {name}")

    def _list_docs(self) -> _Output:
        names = visible_docs(self.folder)
        truncated = len(names) > LIST_LIMIT
        return _Output({"files": names[:LIST_LIMIT], "truncated": truncated}, 0, truncated)

    def _search_docs(self, query: str) -> _Output:
        terms = [fold(term) for term in query.split()]
        if not terms:
            raise ToolError(EMPTY_QUERY, "query must not be empty")
        matches: list[dict[str, Any]] = []
        truncated = False
        for name in visible_docs(self.folder):
            text = read_text(self.folder / name)
            start = 0
            for number, line in enumerate(text.split("\n"), start=1):
                end = start + len(line)
                folded = fold(line)
                if all(term in folded for term in terms):
                    if len(matches) == SEARCH_LIMIT:
                        truncated = True
                        break
                    matches.append(
                        {"path": name, "line": number, "snippet": snippet(text, start, end)}
                    )
                start = end + 1
            if truncated:
                break
        read = sum(len(match["snippet"]) for match in matches)
        paths = tuple(dict.fromkeys(match["path"] for match in matches))
        return _Output({"matches": matches, "truncated": truncated}, read, truncated, paths)

    def _read_doc(self, path: str, offset: int) -> _Output:
        text = read_doc_text(self.folder, path)
        content = text[offset : offset + READ_CHUNK_CHARS]
        end = offset + len(content)
        truncated = end < len(text)
        payload = {
            "path": path,
            "offset": offset,
            "content": content,
            "truncated": truncated,
            "next_offset": end if truncated else None,
        }
        return _Output(payload, len(content), truncated, (path,))
