import itertools

from app.indexing.chunker import MAX_CHARS, OVERLAP_LINES, TARGET_CHARS, chunk_text


def functions(n: int) -> str:
    return "".join(f"def f{i}():\n    return {i}\n\n" for i in range(n))


def test_empty_and_whitespace_files_produce_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("\n\n   \n") == []


def test_a_small_file_is_one_chunk_with_line_numbers():
    (chunk,) = chunk_text("a = 1\nb = 2\nc = 3\n")
    assert (chunk.index, chunk.start_line, chunk.end_line) == (0, 1, 3)
    assert chunk.content == "a = 1\nb = 2\nc = 3\n"


def test_a_short_file_is_never_duplicated_by_the_overlap():
    for lines in range(1, 12):
        text = "".join(f"line {i}\n" for i in range(lines))
        (chunk,) = chunk_text(text)
        assert (chunk.start_line, chunk.end_line) == (1, lines)


def test_the_final_chunk_is_not_a_repeat_of_the_previous_tail():
    chunks = chunk_text(functions(300))
    assert chunks[-1].end_line == functions(300).count("\n")
    assert chunks[-1].start_line > chunks[-2].start_line
    assert chunks[-1].content.strip() != chunks[-2].content.strip()


def test_no_text_is_lost_and_chunks_are_bounded():
    text = functions(300)
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c.content) <= MAX_CHARS for c in chunks)
    # Every line of the file appears in at least one chunk.
    covered = set()
    for c in chunks:
        covered.update(range(c.start_line, c.end_line + 1))
    assert covered == set(range(1, text.count("\n") + 1))
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_consecutive_chunks_overlap():
    chunks = chunk_text(functions(300))
    for previous, current in itertools.pairwise(chunks):
        assert current.start_line <= previous.end_line  # overlap
        assert current.start_line > previous.start_line  # but always progress
        assert previous.end_line - current.start_line < OVERLAP_LINES + 2


def test_chunks_prefer_to_end_at_a_blank_line():
    chunks = chunk_text(functions(300))
    # Every chunk except the last ends right after a blank line (a function boundary).
    assert all(c.content.endswith("\n\n") for c in chunks[:-1])


def test_very_long_lines_are_split():
    text = "x = '" + "a" * (MAX_CHARS * 3) + "'\n"
    chunks = chunk_text(text)
    assert len(chunks) >= 3
    assert all(len(c.content) <= MAX_CHARS for c in chunks)
    assert all(c.start_line == 1 and c.end_line == 1 for c in chunks)


def test_chunking_terminates_on_pathological_input():
    assert chunk_text("\n" * 5000) == []
    assert len(chunk_text("a\n" * 5000)) > 1


def test_chunk_size_target_is_respected_on_average():
    sizes = [len(c.content) for c in chunk_text(functions(500))[:-1]]
    assert max(sizes) <= MAX_CHARS
    assert min(sizes) >= TARGET_CHARS // 2
