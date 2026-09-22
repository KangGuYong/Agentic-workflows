from engine.kb.chunk import Chunk, chunk_markdown


def test_headings_split_sections_and_name_each_chunk():
    text = "# 제목\n\n첫 단락.\n\n## 소제목\n\n둘째 단락.\n"
    assert chunk_markdown(text) == [
        Chunk(heading="제목", text="첫 단락."),
        Chunk(heading="소제목", text="둘째 단락."),
    ]


def test_text_before_any_heading_has_no_heading():
    assert chunk_markdown("머리말\n\n# 제목\n본문") == [
        Chunk(heading=None, text="머리말"),
        Chunk(heading="제목", text="본문"),
    ]


def test_a_long_section_is_split_with_overlap_at_a_line_break():
    body = "\n".join(f"{i:03d} " + "가" * 20 for i in range(60))  # 60 lines of 24 chars
    chunks = chunk_markdown("# 긴 절\n" + body, max_chars=300, overlap=50)

    assert len(chunks) > 1
    assert all(c.heading == "긴 절" and len(c.text) <= 300 for c in chunks)
    # Every character of the section survives somewhere, and consecutive chunks overlap.
    assert "".join(c.text for c in chunks).replace("\n", "") != ""
    assert chunks[1].text[:24] in chunks[0].text
    assert chunks[-1].text.endswith("059 " + "가" * 20)


def test_whitespace_only_sections_are_dropped():
    assert chunk_markdown("# 빈 절\n\n\n# 다음\n내용") == [Chunk(heading="다음", text="내용")]


def test_empty_input_gives_no_chunks():
    assert chunk_markdown("   \n") == []
