"""Safe text extraction for uploaded files.

The upload endpoint accepts a small whitelist of formats. We never execute
the content — only extract plain text:

  * .txt, .md            → read as utf-8 (with replacement on undecodable bytes)
  * .html, .htm          → strip tags via stdlib HTMLParser (no JS, no DOM)
  * .jsonl               → join the `text` field of each row (capture-shaped)
  * .json                → if it has a `text` key, return that; else stringify

We intentionally avoid `lxml` or `beautifulsoup` for the html path — the
stdlib parser is slower but has zero CVE surface and ships with Python.

The function returns ``(title_hint, body_text)``. The title hint is the
first non-empty heading or the first sentence, used when the user did not
supply an explicit title.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Iterable


# Cap upload bytes to keep DoS / accidental huge files contained.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024     # 5 MB
ALLOWED_SUFFIXES = (".txt", ".md", ".markdown", ".html", ".htm", ".jsonl", ".json")


class _HTMLToText(HTMLParser):
    """Drop tags, scripts, styles. Output plain text + first heading."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf = StringIO()
        self._skip = 0
        self._first_heading: str | None = None
        self._cap_next_heading_chars = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style", "noscript", "iframe", "object", "embed"):
            self._skip += 1
        if tag in ("h1", "h2", "h3") and self._first_heading is None:
            self._cap_next_heading_chars = 200

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "iframe", "object", "embed"):
            self._skip = max(0, self._skip - 1)
        if tag in ("h1", "h2", "h3") and self._cap_next_heading_chars > 0:
            heading = self._buf.getvalue().rsplit("\n", 1)[-1].strip()
            if heading:
                self._first_heading = heading
            self._cap_next_heading_chars = 0
        if tag in ("p", "div", "br", "li", "tr"):
            self._buf.write("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._buf.write(data)

    @property
    def text(self) -> str:
        # Collapse runs of blank lines.
        out: list[str] = []
        blank = 0
        for line in self._buf.getvalue().splitlines():
            stripped = line.strip()
            if not stripped:
                blank += 1
                if blank > 1:
                    continue
            else:
                blank = 0
            out.append(stripped)
        return "\n".join(out).strip()

    @property
    def first_heading(self) -> str | None:
        return self._first_heading


def _strip_html(body: str) -> tuple[str | None, str]:
    parser = _HTMLToText()
    parser.feed(body)
    parser.close()
    return parser.first_heading, parser.text


def _first_sentence(text: str, max_chars: int = 200) -> str | None:
    text = text.strip()
    if not text:
        return None
    # Naive sentence cut on .!?
    for i, ch in enumerate(text):
        if ch in ".!?" and i > 20:
            return text[: i + 1][:max_chars].strip()
    return text[:max_chars].strip()


def extract_text(filename: str, body: bytes) -> tuple[str | None, str]:
    """Extract `(title_hint, body_text)` from an uploaded file payload.

    Raises ``ValueError`` on unsupported suffixes, oversized payloads, and
    JSONL rows that don't carry a text field.
    """
    if not body:
        raise ValueError("upload body is empty")
    if len(body) > MAX_UPLOAD_BYTES:
        raise ValueError(f"upload too large: {len(body)} > {MAX_UPLOAD_BYTES} bytes")

    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(
            f"unsupported file type: {suffix!r}. allowed: {ALLOWED_SUFFIXES}"
        )

    text = body.decode("utf-8", errors="replace")

    if suffix in (".html", ".htm"):
        heading, plain = _strip_html(text)
        if not plain.strip():
            raise ValueError("html upload produced no extractable text")
        return heading or _first_sentence(plain), plain

    if suffix == ".jsonl":
        rows = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"jsonl line {lineno}: invalid json: {exc}") from exc
            if not isinstance(obj, dict):
                continue
            body_text = obj.get("text") or obj.get("body") or ""
            if body_text:
                rows.append(str(body_text))
        if not rows:
            raise ValueError("jsonl contained no rows with a 'text' field")
        merged = "\n\n".join(rows)
        return _first_sentence(merged), merged

    if suffix == ".json":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"json: {exc}") from exc
        if isinstance(obj, dict):
            t = obj.get("text") or obj.get("body") or ""
            title = obj.get("title")
            if t:
                return title, str(t)
            raise ValueError("json upload has no 'text' field")
        raise ValueError("json upload must be an object with 'text' field")

    # txt / md / markdown — return as-is, first heading or sentence as title hint
    title_hint: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            title_hint = line.lstrip("# ").strip()
            break
    if title_hint is None:
        title_hint = _first_sentence(text)
    return title_hint, text


__all__ = ["extract_text", "ALLOWED_SUFFIXES", "MAX_UPLOAD_BYTES"]
