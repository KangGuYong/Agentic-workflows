"""Markdown → chunks (knowledge-base design §4.2). Pure, so it is tested without a database."""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHARS = 1000
OVERLAP = 150
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")


@dataclass(frozen=True)
class Chunk:
    heading: str | None
    text: str


def chunk_markdown(markdown: str, *, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading: str | None = None
    lines: list[str] = []
    fenced = False

    def flush() -> None:
        body = "\n".join(lines).strip()
        lines.clear()
        for piece in _split(body, max_chars, overlap):
            chunks.append(Chunk(heading, piece))

    for line in markdown.splitlines():
        # A '# comment' inside a code fence is not a heading; fences toggle, they do not nest.
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line)
        if match:
            flush()
            heading = match.group(1)
        else:
            lines.append(line)
    flush()
    return chunks


def _split(body: str, max_chars: int, overlap: int) -> list[str]:
    """Windows of at most `max_chars`, cut at the last line break in the window when there is one past
    its midpoint, each starting `overlap` characters before the previous one ended."""
    pieces: list[str] = []
    start = 0
    while start < len(body):
        end = min(start + max_chars, len(body))
        if end < len(body):
            cut = body.rfind("\n", start + max_chars // 2, end)
            if cut != -1:
                end = cut
        piece = body[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(body):
            break
        start = max(end - overlap, start + 1)  # always advances, even when overlap >= the window
    return pieces
