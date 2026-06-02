# DISCOVERIES — catchem

Append-only log produced by the `/ralph-loop` discovery harness at `~/Desktop/.claude/`.

Each line: `[type-id] file:line — title. explanation. evidence. next-action.`

## Findings

- **[bug-hunt]** `pyproject.toml:68` — Duplicate force-include in wheel configuration. The force-include directive tried to add files inside `src/catchem/static` a second time, causing the python `build` tool to fail with `ValueError: A second file is being added to the wheel archive at the same path`. Fixed by removing the duplicate configuration block.
- **[bug-hunt]** `src/catchem/zero_shot_classifier.py:80-110` — Set-based unigram/bigram deduplication instead of Counter. The zero-shot classifier used set comprehension for title and body words/bigrams, meaning repeated keyword mentions did not increase the label's relevance score, failing the intention of the unigram repetition test (which only passed by accident). Fixed by implementing collections.Counter frequency-based scoring.
- **[test-gap]** `tests/test_deepseek_stream.py:1-195` — Missing coverage for DeepSeek client stream and sync review. Created a dedicated unit test suite mapping all stream chunks, HTTP non-200 codes, timeout exceptions, invalid JSON formats, custom clients, and float clamping, raising `deepseek.py` coverage from 55% to 99%.
- **[test-gap]** `tests/test_symbol_mapper.py:112-167` — Missing coverage for SymbolMapper config loading. Added tests covering SymbolMapper `_merge_yaml` config parser, corrupt/non-dict YAML/JSON manifests, and the scanner's 12-file limit, raising `symbol_mapper.py` coverage from 75% to 96%.
- **[test-gap]** `tests/test_newsimpact_guard.py:145-224` — Missing coverage for standalone guard adapter verification. Added unit tests covering Standalone `assert_protected_artifacts_unmodified` baseline checker (happy/missing/modified paths), corrupt JSON index error, missing candidates lists, and custom allowed modes, raising adapter coverage from 75% to 98.9%.
- **[test-gap]** `tests/test_news_sources_x_twitter.py:169-186` — Missing coverage for link rewriter error paths. Added tests for invalid url parsing exceptions and parse_feed execution exceptions inside the X source pack, raising its coverage to 100%.
- **[test-gap]** `tests/test_news_sources_watchlist_dynamic.py:119-144` — Missing coverage for dynamic watchlist fallback. Added tests covering settings loading failures, empty/blank items, and duplicate case-insensitive tickers normalization, raising its coverage to 100%.
- **[test-gap]** `tests/test_sentiment.py:167-238` — Missing coverage for SentimentModel pipeline. Added unit tests with mocked optional transformers module, raising `sentiment.py` coverage from 62.50% to 98.86%.
- **[test-gap]** `tests/test_static_dashboard_packaged_install.py:137-197` — Missing coverage for static directory lookup and fallback error paths. Added tests for invalid override directories, escaped traversals, cache validation, and static bytes retrieval, raising `static_assets.py` coverage from 70.79% to 100.00%.
- **[test-gap]** `tests/test_taxonomy_unit.py:1-93` — Completely untested taxonomy loading & property methods. Created dedicated unit test suite covering default load pathways, missing file errors, and malformed content structures, raising `taxonomy.py` coverage to 100.00%.
- **[test-gap]** `tests/test_zero_shot_taxonomy.py:142-208` — Missing coverage for ZeroShotModel classification and bigram overlaps. Added tests covering BART-MNLI pipeline mock classifications, top_above filtering, and bigram overlap deduplication, raising `zero_shot_classifier.py` coverage from 79.23% to 99.23%.
- **[test-gap]** `tests/test_text_extract_unit.py:1-105` — Untested branches in safe text extraction. Created unit tests covering HTML heading collapse, multiple blank lines collapse, missing title/JSON formatting edge cases, and empty text/invalid payloads, raising `text_extract.py` coverage from 85.25% to 100.00%.



