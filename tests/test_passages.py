from __future__ import annotations

from opensac.broker.capabilities.passages import segment_passages


def test_chunking_is_stable_exact_and_handles_long_single_lines() -> None:
    text = "heading\n\n" + "a" * 4_500 + " needle"

    first = segment_passages(text, chunk_chars=2_000, overlap_chars=200)
    second = segment_passages(text, chunk_chars=2_000, overlap_chars=200)

    assert first == second
    assert len(first) == 3
    for passage, start, end, coordinates in first:
        assert passage == text[start:end]
        assert 0 < len(passage) <= 2_000
        assert coordinates.start_line >= 1
        assert coordinates.end_line >= coordinates.start_line
    assert first[-1][0].endswith("needle")
    assert first[-1][3].end_line == 3
    assert first[-1][3].end_character == len(text) - len("heading\n\n")

    indented = segment_passages(
        "\n  alpha\nbeta",
        chunk_chars=2_000,
        overlap_chars=200,
    )[0]
    assert indented[3].model_dump() == {
        "start_line": 2,
        "start_character": 2,
        "end_line": 3,
        "end_character": 4,
    }

    aligned = segment_passages(
        "a" * 1_100 + "\n\n" + "b" * 1_000,
        chunk_chars=2_000,
        overlap_chars=200,
    )
    assert aligned[0][2] == 1_100
