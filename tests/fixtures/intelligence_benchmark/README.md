# Frozen acquisition benchmark fixtures

`dataset.json` contains 100 synthetic, owned contract cases: macro/policy,
earnings/guidance, industry/supply chain, negative/risk, and time/entity/repost
traps each have 20 cases. Five cases per stratum are calibration cases; the other
15 are marked blind for exercising the separation in reports. All fixture data
is visible in the repository, so that label does not constitute a real blind
evaluation. The `.invalid` domains and synthetic event identities contain no real
financial claims.

`capture-ideal.json` and `capture-degraded.json` are deterministic fixture inputs,
not search-provider output. The degraded input has missing retrievals, wrong
subjects, future publication times, absent original bodies and common-origin
reposts. Both captures identify their mode as `fixture_replay`; the evaluator
will not issue a production or provider-quality conclusion from them.

Run without a network connection:

```bash
python scripts/intelligence-benchmark.py \
  --dataset tests/fixtures/intelligence_benchmark/dataset.json \
  --capture tests/fixtures/intelligence_benchmark/capture-ideal.json \
  --capture tests/fixtures/intelligence_benchmark/capture-degraded.json \
  --out /operator/new/benchmark-report.json
```

For a financial benchmark, supply an independently adjudicated `reviewed_gold`
dataset with primary reference URLs, subject/event identities, a cutoff per case,
and reviewer/method/time provenance. Freeze the dataset before unblinding provider
captures; keep calibration and blind queries separate. `prospective`,
`frozen_replay` and `historical_reconstruction` captures retain different modes.
Use the bounded acquisition service for actual I/O; this evaluator has no network
code and never reads credentials. Unknown billing remains unverified.
