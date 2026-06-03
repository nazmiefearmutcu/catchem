from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from catchem.awareness_reader import (
    discover_awareness_jsonl_root,
    iter_captures,
    iter_finalized_files,
    parse_capture_line,
    replay_directory,
)


@pytest.mark.regression
def test_iter_finalized_files_nonexistent_root() -> None:
    assert iter_finalized_files(Path("nonexistent_directory_12345")) == []


@pytest.mark.regression
def test_iter_finalized_files_sorting(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    root.mkdir()
    p1 = root / "a.jsonl"
    p2 = root / "b.jsonl"
    p3 = root / "c.jsonl"

    # Write dummy files
    p1.write_text("{}", encoding="utf-8")
    p2.write_text("{}", encoding="utf-8")
    p3.write_text("{}", encoding="utf-8")

    # Set distinct modification times (b is oldest, a is middle, c is newest)
    os.utime(p2, (1000, 1000))
    os.utime(p1, (2000, 2000))
    os.utime(p3, (3000, 3000))

    files = iter_finalized_files(root)
    assert files == [p2, p1, p3]


@pytest.mark.regression
def test_iter_finalized_files_sorting_tie(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    root.mkdir()
    p1 = root / "z.jsonl"
    p2 = root / "a.jsonl"

    p1.write_text("{}", encoding="utf-8")
    p2.write_text("{}", encoding="utf-8")

    # Set identical modification times to test alphabetical tie-breaker sorting
    os.utime(p1, (1000, 1000))
    os.utime(p2, (1000, 1000))

    files = iter_finalized_files(root)
    assert files == [p2, p1]  # 'a.jsonl' should come before 'z.jsonl'


@pytest.mark.regression
def test_parse_capture_line_empty() -> None:
    assert parse_capture_line("   ") is None
    assert parse_capture_line("") is None


@pytest.mark.regression
def test_parse_capture_line_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        "catchem.awareness_reader.logger.warning",
        lambda event, *args, **kwargs: warnings.append(event),
    )
    res = parse_capture_line("not-json-at-all")
    assert res is None
    assert "jsonl_decode_error" in warnings


@pytest.mark.regression
def test_parse_capture_line_non_dict() -> None:
    assert parse_capture_line("12345") is None
    assert parse_capture_line('"string"') is None
    assert parse_capture_line("[]") is None


@pytest.mark.regression
def test_parse_capture_line_pydantic_error(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        "catchem.awareness_reader.logger.warning",
        lambda event, *args, **kwargs: warnings.append(event),
    )
    res = parse_capture_line('{"not_a_valid_capture": true}')
    assert res is None
    assert "capture_validate_failed" in warnings


@pytest.mark.regression
def test_parse_capture_line_valid(synth_capture: Any) -> None:
    cap = synth_capture(capture_id="valid-1")
    line = cap.model_dump_json()
    res = parse_capture_line(line)
    assert res is not None
    assert res.capture_id == "valid-1"


@pytest.mark.regression
def test_iter_captures_nonexistent() -> None:
    gen = iter_captures(Path("nonexistent_file_98765"))
    assert list(gen) == []


@pytest.mark.regression
def test_iter_captures_offset_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth_capture: Any
) -> None:
    p = tmp_path / "test.jsonl"
    cap = synth_capture(capture_id="cap-1")
    p.write_text(cap.model_dump_json() + "\n", encoding="utf-8")

    warnings: list[str] = []
    monkeypatch.setattr(
        "catchem.awareness_reader.logger.warning",
        lambda event, *args, **kwargs: warnings.append(event),
    )

    # File has 1 line, start_offset is 5 (which is > 1)
    results = list(iter_captures(p, start_offset=5))
    assert len(results) == 1
    assert results[0][0] == 1  # Line offset yielded is 1
    assert results[0][1].capture_id == "cap-1"
    assert "jsonl_offset_reset" in warnings


@pytest.mark.regression
def test_iter_captures_offset_no_reset(tmp_path: Path, synth_capture: Any) -> None:
    p = tmp_path / "test_no_reset.jsonl"
    cap1 = synth_capture(capture_id="cap-1")
    cap2 = synth_capture(capture_id="cap-2")
    content = cap1.model_dump_json() + "\n" + cap2.model_dump_json() + "\n"
    p.write_text(content, encoding="utf-8")

    # File has 2 lines, start_offset is 1 (which is <= 2). No reset.
    results = list(iter_captures(p, start_offset=1))
    assert len(results) == 1
    assert results[0][0] == 2  # Yields second line (line offset 2)
    assert results[0][1].capture_id == "cap-2"


@pytest.mark.regression
def test_iter_captures_skips_invalid(tmp_path: Path, synth_capture: Any) -> None:
    p = tmp_path / "test_invalid.jsonl"
    cap1 = synth_capture(capture_id="cap-1")
    content = (
        cap1.model_dump_json() + "\n"
        + "invalid_json\n"
        + "\n"
        + cap1.model_dump_json() + "\n"
    )
    p.write_text(content, encoding="utf-8")
    results = list(iter_captures(p))
    assert len(results) == 2
    # Verify line numbers yielded: 1 and 4
    assert results[0][0] == 1
    assert results[1][0] == 4
    assert results[0][1].capture_id == "cap-1"
    assert results[1][1].capture_id == "cap-1"


@pytest.mark.regression
def test_replay_directory_limit(tmp_path: Path, synth_capture: Any) -> None:
    root = tmp_path / "jsonl"
    root.mkdir()
    p1 = root / "file1.jsonl"
    cap1 = synth_capture(capture_id="c1")
    cap2 = synth_capture(capture_id="c2")
    p1.write_text(cap1.model_dump_json() + "\n" + cap2.model_dump_json() + "\n", encoding="utf-8")

    # Case 1: no limit
    res_no_limit = list(replay_directory(root))
    assert len(res_no_limit) == 2
    assert [c.capture_id for c in res_no_limit] == ["c1", "c2"]

    # Case 2: limit=1
    res_limit_1 = list(replay_directory(root, limit=1))
    assert len(res_limit_1) == 1
    assert res_limit_1[0].capture_id == "c1"

    # Case 3: limit=2
    res_limit_2 = list(replay_directory(root, limit=2))
    assert len(res_limit_2) == 2

    # Case 4: limit=3 (more than exists)
    res_limit_3 = list(replay_directory(root, limit=3))
    assert len(res_limit_3) == 2


@pytest.mark.regression
def test_discover_awareness_jsonl_root(tmp_path: Path) -> None:
    # Case 1: jsonl folder exists under the path
    root_with_jsonl = tmp_path / "root1"
    root_with_jsonl.mkdir()
    jsonl_dir = root_with_jsonl / "jsonl"
    jsonl_dir.mkdir()

    res1 = discover_awareness_jsonl_root(root_with_jsonl)
    assert res1 == jsonl_dir

    # Case 2: jsonl folder does not exist under the path
    root_without_jsonl = tmp_path / "root2"
    root_without_jsonl.mkdir()

    res2 = discover_awareness_jsonl_root(root_without_jsonl)
    assert res2 == root_without_jsonl
