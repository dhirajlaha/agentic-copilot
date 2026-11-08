"""Splits a file into overlapping chunks of roughly equal size.

Language-agnostic on purpose: it works for any text file. It prefers to cut at blank lines
(between functions, classes or paragraphs) and keeps a few lines of overlap so a statement
cut by a boundary still appears whole in one chunk. Line numbers are kept so a chunk can be
shown to the model and to developers as `path:start-end`.
"""

from dataclasses import dataclass

# Sized for the embedding model's 2,048-token input limit with a wide margin (code is
# roughly 3 characters per token).
TARGET_CHARS = 1800
MAX_CHARS = 2400
OVERLAP_LINES = 3


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    start_line: int  # 1-based, inclusive
    end_line: int
    content: str


def _segments(text: str) -> list[tuple[int, str]]:
    """(line number, text) pieces, with over-long lines cut so none exceeds MAX_CHARS."""
    pieces: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        for start in range(0, len(line), MAX_CHARS) or [0]:
            pieces.append((number, line[start : start + MAX_CHARS]))
    return pieces


def chunk_text(text: str) -> list[Chunk]:
    segments = _segments(text)
    chunks: list[Chunk] = []
    start = 0
    while start < len(segments):
        end, size = start, 0
        while end < len(segments):
            length = len(segments[end][1])
            if end > start and (size + length > MAX_CHARS or size >= TARGET_CHARS):
                break
            size += length
            end += 1

        if end < len(segments):
            # Cut at the last blank line in the second half of the chunk, if there is one.
            midpoint = start + (end - start) // 2
            for i in range(end - 1, midpoint, -1):
                if not segments[i][1].strip():
                    end = i + 1
                    break

        content = "".join(piece for _, piece in segments[start:end])
        if content.strip():
            chunks.append(
                Chunk(
                    index=len(chunks),
                    start_line=segments[start][0],
                    end_line=segments[end - 1][0],
                    content=content,
                )
            )
        if end >= len(segments):
            break  # the last chunk reached the end of the file: no overlap to carry
        # Step back a few segments for overlap, but always make progress.
        start = end - OVERLAP_LINES if end - OVERLAP_LINES > start else end
    return chunks
