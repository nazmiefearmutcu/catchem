from __future__ import annotations

import pytest
from catchem.text_extract import extract_text, MAX_UPLOAD_BYTES


def test_extract_text_empty_and_large() -> None:
    # Empty body error
    with pytest.raises(ValueError, match="upload body is empty"):
        extract_text("test.txt", b"")

    # Too large body error
    large_payload = b"x" * (MAX_UPLOAD_BYTES + 1)
    with pytest.raises(ValueError, match="upload too large"):
        extract_text("test.txt", large_payload)

    # Invalid extension error
    with pytest.raises(ValueError, match="unsupported file type"):
        extract_text("test.png", b"some bytes")


def test_extract_text_html_edges() -> None:
    # Heading with no text -> first heading stays None, falls back to first sentence
    html_no_heading_text = b"<html><body><h1>  </h1><p>First sentence of paragraph. Next sentence.</p></body></html>"
    title, body = extract_text("test.html", html_no_heading_text)
    assert title == "First sentence of paragraph."
    assert body == "First sentence of paragraph. Next sentence."

    # Script/style tag skipping and multiple blank lines collapsing
    html_script_style = (
        b"<html><body>"
        b"<script>const a = 1;</script>"
        b"<style>body { color: red; }</style>"
        b"<p>Line one</p>"
        b"<br><br><br>"
        b"<p>Line two</p>"
        b"</body></html>"
    )
    title, body = extract_text("test.html", html_script_style)
    # script/style contents must not bleed in
    assert "const a" not in body
    assert "color: red" not in body
    # multiple line breaks collapsed to single line breaks
    assert body == "Line one\nLine two"

    # HTML with no extractable text
    html_empty = b"<html><body><script>const a = 1;</script></body></html>"
    with pytest.raises(ValueError, match="html upload produced no extractable text"):
        extract_text("test.html", html_empty)

    # Valid heading in HTML
    html_with_heading = b"<html><body><h1>My Valid Title</h1><p>Paragraph content.</p></body></html>"
    title, body = extract_text("test.html", html_with_heading)
    assert title == "My Valid Title"
    assert body == "My Valid TitleParagraph content."


    # Multiple blank lines collapse check (triggering blank > 1)
    html_multiple_breaks = b"<html><body>Line one<p></p><p></p><p></p>Line two</body></html>"
    title, body = extract_text("test.html", html_multiple_breaks)
    assert body == "Line one\n\nLine two"



def test_first_sentence_edges() -> None:
    # Sentence with boundary under index 20
    text_short_period = b"Hi. This is a very long text that has no other period until the end."
    title, _ = extract_text("test.txt", text_short_period)
    # Since first period is at index 2 (under index 20 limit), it does not cut there.
    # It cuts at the next period or default max_chars limit.
    assert title == "Hi. This is a very long text that has no other period until the end."

    # Text with no punctuation at all
    text_no_punctuation = b"This is a long sentence with absolutely no punctuation whatsoever to test the fallback behaviour"
    title, _ = extract_text("test.txt", text_no_punctuation)
    assert title == "This is a long sentence with absolutely no punctuation whatsoever to test the fallback behaviour"

    # Whitespace-only text triggering empty first sentence returning None (line 103)
    title, body = extract_text("test.txt", b"    ")
    assert title is None
    assert body == "    "


def test_extract_text_markdown_and_txt() -> None:
    # Markdown with heading
    md = b"# Markdown Heading\nThis is content."
    title, body = extract_text("test.md", md)
    assert title == "Markdown Heading"
    assert body == "# Markdown Heading\nThis is content."


def test_extract_text_jsonl_edges() -> None:
    # Empty lines and non-dict objects ignored, valid rows parsed
    jsonl_mixed = (
        b'{"text": "First line"}\n'
        b'\n'  # Empty line
        b'123\n'  # Non-dict JSON object
        b'{"body": "Second line"}\n'
    )
    title, body = extract_text("test.jsonl", jsonl_mixed)
    assert title == "First line\n\nSecond line"
    assert body == "First line\n\nSecond line"

    # JSONL decode error
    jsonl_corrupt = b'{"text": "Valid"}\n{invalid json}\n'
    with pytest.raises(ValueError, match="jsonl line 2: invalid json:"):
        extract_text("test.jsonl", jsonl_corrupt)

    # JSONL with no text/body fields at all
    jsonl_no_text = b'{"title": "No text here"}\n{"foo": "bar"}\n'
    with pytest.raises(ValueError, match="jsonl contained no rows with a 'text' field"):
        extract_text("test.jsonl", jsonl_no_text)


def test_extract_text_json_edges() -> None:
    # JSON decode error
    with pytest.raises(ValueError, match="json:"):
        extract_text("test.json", b"{invalid json}")

    # JSON not an object
    with pytest.raises(ValueError, match="json upload must be an object"):
        extract_text("test.json", b"[1, 2, 3]")

    # JSON dict missing text/body field
    with pytest.raises(ValueError, match="json upload has no 'text' field"):
        extract_text("test.json", b'{"title": "Just title"}')

    # JSON dict with text but no title (missing/none title)
    title, body = extract_text("test.json", b'{"text": "Content without title"}')
    assert title is None
    assert body == "Content without title"

