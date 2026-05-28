// Plain-English glossary for domain-specific jargon that surfaces in
// the UI. Hover/focus on a `<JargonTooltip term="…" />` reveals these
// strings via the native `title` attribute (no popper library).
//
// Keep entries short (≤ 2 sentences). When the term names a contract
// from another project (e.g. merged_news / newsimpact), call that out
// explicitly so analysts know why Catchem mirrors the spelling.
export const JARGON: Record<string, string> = {
  "DLQ": "Dead-letter queue. Records that failed pipeline processing get parked here for re-tries. Non-zero usually means malformed JSONL upstream.",
  "fusion_verdict_class": "External governance classification owned by merged_news (newsimpact). Catchem mirrors the upstream class name verbatim — FUSION_* prefix is intentional.",
  "FUSION_REGRESSIVE": "Fusion verdict indicating the news-impact model regressed on validation. Quarantines the record from publish/promote paths.",
  "release_gate_passed": "Governance flag — true means the candidate model passed all three checks (sha256 pin, validation, drift). Catchem keeps it FALSE by default; merged_news is always quarantined here.",
  "quarantine_state": "Why a record is held back from publish. Common values: QUARANTINED_REGRESSIVE_MULTIMODAL, QUARANTINED_DRIFT, RELEASED.",
  "governance sha256": "Cryptographic pin on the upstream NewsImpact governance index file. Changes if merged_news ships a new model — Catchem refuses to load unless the sha matches expected.",
  "NewsImpact": "Sibling project that produces multi-modal regression-tested news-impact scores. Catchem treats it as read-only/quarantined; never overrides relevance.",
  "taxonomy": "The asset-class + reason-code allow-list used to label records. Lives in configs/taxonomy.yaml.",
  "diagnostic_allowed": "Whether the runtime is permitted to flip into diagnostic mode (writes diagnostic stamps, runs research adapters). production_safe means NO.",
  "production_safe": "Default runtime mode. Diagnostic adapter is hard-refused even if the env flag is set. All records have diagnostic_* fields forced to false/null at the API surface.",
  "use_ml_stubs": "When true, ML pipeline uses deterministic CPU stubs instead of real Hugging Face models. Stubs produce 100% on the synthetic golden set; for real evaluation use --with-ml.",
  "finance relevance score": "Calibrated 0-1 score from the relevance scorer. Empirical max ≈ 0.80. Overview + Feed color-band ≥0.70 (top decile) and ≥0.40 (solid middle).",
  "asset class": "High-level financial category: indices, equities, crypto, rates, fx, commodities, credit, macro. A record can carry multiple.",
  "reason code": "Why the record matters: earnings, inflation, central_bank, regulation, m_and_a, cyber_outage, geopolitics, fraud_governance, etc.",
  "z-score": "Quantitative anomaly metric: how many standard deviations a value is from the mean. z≥2 is unusual; z≥3 is rare. Catchem's anomaly engine flags high-z bursts.",
  "spillover": "Cross-asset Granger-style relationship — when news on one asset class predicts news on another with a lag.",
  "Pearson r": "Linear correlation between two series in [-1, +1]. +1 = perfect co-move; 0 = independent; -1 = perfect anti-move. Catchem uses it on per-bucket mention counts to find symbols whose narratives co-fire (or surge in opposition).",
  "regime shift": "Detected change in the underlying news-flow distribution (KL divergence over time buckets). Real events tend to cluster around shifts.",
  "cluster coherence": "How tightly grouped a cluster of related news items is (weighted Jaccard similarity over symbols + reasons + sources).",
  "KL divergence": "Kullback–Leibler divergence — measures how different one probability distribution is from another. Catchem uses it bucket-over-bucket on topic mix; spikes mark regime shifts.",
  "lift": "Association strength vs an independence baseline. lift=1 means random co-occurrence; lift=2× means the pair appears twice as often as chance would predict.",
  "sentiment momentum": "Difference between fast and slow sentiment EMA per ticker. Positive = sentiment trending up; sign flips mark inflections.",
  "novelty": "How dissimilar a record's title is to recent corpus. 1.0 = nothing like it recently; 0.0 = near-duplicate of something already seen.",
  "weighted Jaccard": "Set-similarity metric — overlap divided by union, weighted by component counts. Used inside cluster coherence.",
  "composite source score": "0-1 leaderboard rank combining relevant_rate, signal density, asset+reason diversity, and symbol uniqueness for each domain.",
  "signal density": "Fraction of a source's records that carry at least one asset class AND one reason code (not just generic noise).",
  "lead rate": "Of events a source participated in, the fraction where it published FIRST. >50% = leader; <50% = follower.",
};

/**
 * Worked formulas + examples for numeric quant signals. Consumed by
 * `<SignalExplainer term="…" />` to power the "?" popovers next to
 * metric badges on `/scan`. Keep examples concrete + numeric.
 */
export const SIGNAL_FORMULAS: Record<string, { formula?: string; example?: string }> = {
  "z-score": {
    formula: "(value - mean) / std_dev",
    example:
      "If avg mention rate is 5/min with std=2, a 12/min burst has z = (12-5)/2 = 3.5 — rare.",
  },
  "KL divergence": {
    formula: "Σ p(x) · log(p(x) / q(x))",
    example:
      "Compares current 5-min bucket's topic distribution to baseline. Higher KL = bigger regime shift.",
  },
  "weighted Jaccard": {
    formula: "Σ min(a_i, b_i) / Σ max(a_i, b_i)",
    example:
      "Two clusters sharing 80% symbols + 60% sources score ≈ 0.7 — strongly related.",
  },
  "spillover": {
    formula: "Granger-style F-stat over lagged news volume",
    example:
      "If equity-news spikes consistently precede crypto-news by 5 min, spillover[equity→crypto] is high.",
  },
  "Pearson r": {
    formula: "Σ(xᵢ - x̄)(yᵢ - ȳ) / √(Σ(xᵢ - x̄)² · Σ(yᵢ - ȳ)²)",
    example:
      "AAPL and MSFT both surge in tech-earnings buckets and stay quiet otherwise → r ≈ +0.85. Strong narrative co-movement.",
  },
  "cluster coherence": {
    formula: "mean pairwise weighted Jaccard across members",
    example:
      "A cluster with 5 records all sharing 3+ symbols + 2+ reasons has coherence ≈ 0.85.",
  },
  "sentiment momentum": {
    formula: "EMA(net_sentiment, fast) − EMA(net_sentiment, slow)",
    example:
      "Fast EMA above slow EMA = sentiment improving. Sign change = ⚡ flip.",
  },
  "novelty": {
    formula: "1 − max cosine(title, recent_titles)",
    example:
      "A title sharing few keywords with the last 100 records scores ≈ 0.8.",
  },
  "lift": {
    formula: "P(A ∧ B) / (P(A) · P(B))",
    example:
      "If 'crypto' appears in 10% of records and 'regulation' in 5%, but 2% carry both — lift = 0.02/(0.10×0.05) = 4×.",
  },
  "composite source score": {
    formula: "0.35·rel_rate + 0.25·density + 0.20·diversity + 0.20·uniqueness",
    example:
      "A wire with 80% relevant + 70% signal density + 60% asset diversity + 50% uniqueness scores ≈ 0.66.",
  },
  "signal density": {
    formula: "records_with_asset_AND_reason / total_records",
    example:
      "Of 200 records from one domain, if 130 carry both an asset class and a reason code, signal density = 0.65.",
  },
  "lead rate": {
    formula: "events_led_first / events_participated",
    example:
      "A wire that led 18 of 30 multi-source events scores 0.60 — clear leader status.",
  },
  "regime shift": {
    formula: "KL ≥ adaptive_threshold (mean + 2·stdev of recent KL)",
    example:
      "If recent KL mean=0.05, stdev=0.04 → threshold≈0.13. A 0.21 spike fires a shift.",
  },
};
