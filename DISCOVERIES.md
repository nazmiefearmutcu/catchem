# DISCOVERIES — catchem

Append-only log produced by the `/ralph-loop` discovery harness at `~/Desktop/.claude/`.

Each line: `[type-id] file:line — title. explanation. evidence. next-action.`

## Findings

- **[bug-hunt]** `pyproject.toml:68` — Duplicate force-include in wheel configuration. The force-include directive tried to add files inside `src/catchem/static` a second time, causing the python `build` tool to fail with `ValueError: A second file is being added to the wheel archive at the same path`. Fixed by removing the duplicate configuration block.
- **[bug-hunt]** `src/catchem/zero_shot_classifier.py:80-110` — Set-based unigram/bigram deduplication instead of Counter. The zero-shot classifier used set comprehension for title and body words/bigrams, meaning repeated keyword mentions did not increase the label's relevance score, failing the intention of the unigram repetition test (which only passed by accident). Fixed by implementing collections.Counter frequency-based scoring.
- **[test-gap]** `tests/test_deepseek_stream.py:1-195` — Missing coverage for DeepSeek client stream and sync review. Created a dedicated unit test suite mapping all stream chunks, HTTP non-200 codes, timeout exceptions, invalid JSON formats, custom clients, and float clamping, raising `deepseek.py` coverage from 55% to 99%.
- **[test-gap]** `tests/test_symbol_mapper.py:112-167` — Missing coverage for SymbolMapper config loading. Added tests covering SymbolMapper `_merge_yaml` config parser, corrupt/non-dict YAML/JSON manifests, and the scanner's 12-file limit, raising `symbol_mapper.py` coverage from 75% to 96%.
