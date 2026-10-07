# S&P 500 Event-Driven Scanner

This module implements a research-oriented event-driven stock selection flow:

1. Collect latest S&P 500 news.
2. Score each event by relevance, sentiment, impact, and confidence.
3. Rank short-term launch candidates with event momentum, price trend, and volume confirmation.
4. Validate historical events against future returns and calibrate a quantitative model.

The output is for research and screening, not direct trading advice.

## Realtime Scan

```powershell
python agent/scripts/event_driven_sp500/sp500_event_scanner.py `
  --tickers AAPL,NVDA,MSFT `
  --output-dir .tmp/event_scan `
  --top 10 `
  --workers 8
```

Outputs:

- `.tmp/event_scan/data/events.csv`
- `.tmp/event_scan/artifacts/signals.csv`
- `.tmp/event_scan/artifacts/event_driven_report.json`
- `.tmp/event_scan/artifacts/event_driven_report.md`

## Historical Collection

GDELT is used as the no-key historical news source. It may rate-limit requests,
so use small batches or cached runs for large universes.

Recommended historical workflow:

- Start with 5-20 liquid tickers and 1-3 months of news.
- Keep `workers=1` and `sleep>=1.0` for GDELT to reduce 429 rate limits.
- Accumulate several validated runs instead of forcing one very large pull.
- Import your own vendor/news CSV into `/event-driven/validate` when available.

```powershell
python agent/scripts/event_driven_sp500/gdelt_historical_collector.py `
  --tickers AAPL,NVDA,MSFT,TSLA,AMZN `
  --start-date 2026-03-01 `
  --end-date 2026-04-30 `
  --output-dir .tmp/event_history `
  --max-records-per-ticker 20 `
  --workers 1 `
  --sleep 1.0
```

Output:

- `.tmp/event_history/data/historical_events.csv`

## Quantitative Validation

```powershell
python agent/scripts/event_driven_sp500/event_impact_validator.py `
  --events-csv .tmp/event_history/data/historical_events.csv `
  --output-dir .tmp/event_history/artifacts `
  --benchmark SPY `
  --horizons 1,3,5,10
```

Outputs:

- `event_impact_study.csv`: event-level forward return and abnormal return table.
- `event_impact_summary.json`: IC, beta, hit rate, top-bottom spread, t-stat, p-value, and 95% confidence interval.
- `event_impact_report.md`: human-readable event-study report.

Interpretation notes:

- `mean_abnormal_return`: event-window return minus benchmark return.
- `Spearman IC`: rank correlation between event predictor and future abnormal return.
- `Beta`: linear slope from event predictor to abnormal return.
- `t-stat / p-value`: approximate significance for mean abnormal return, useful as a noise check.
- `95% CI`: approximate confidence interval for mean abnormal return.

The calibrated model is event-type aware:

```text
expected_abnormal_return_h = beta_{event_type,h} * score * impact_score * confidence
```

When an event type has too few historical samples, the scanner shrinks its
event-type beta back toward the global beta:

```text
effective_beta = w * event_type_beta + (1 - w) * global_beta
w = n / (n + 20)
```

## Calibrated Realtime Prediction

After validation, feed the calibration summary into the realtime scanner:

```powershell
python agent/scripts/event_driven_sp500/sp500_event_scanner.py `
  --tickers AAPL,NVDA,MSFT `
  --output-dir .tmp/event_scan_calibrated `
  --calibration-json .tmp/event_history/artifacts/event_impact_summary.json
```

`signals.csv` will include:

- `expected_abret`: calibrated expected abnormal return for the primary horizon.
- `calibration_ic`: historical Spearman IC for that calibration.
- `calibration_shrinkage`: how much weight was given to event-type beta versus global beta.
- `calibrated_signal`: positive / negative / neutral calibrated direction.

## API Endpoints

- `GET /event-radar-tool`
- `POST /event-driven/sp500/scan`
- `GET /event-driven/calibrations`
- `POST /event-driven/historical-collect`
- `POST /event-driven/historical-batch`
- `POST /event-driven/validate`

Open `/event-radar-tool` in the browser for a standalone research UI. It does
not require rebuilding the React SPA and exposes both workflows:

- realtime S&P 500 event scan and launch-signal ranking;
- historical GDELT collection plus event-impact validation/calibration.

Useful realtime API payload:

```json
{
  "limit": 50,
  "top": 15,
  "news_per_ticker": 3,
  "workers": 12,
  "auto_calibration": true,
  "min_calibration_events": 20
}
```

Use `GET /event-driven/calibrations?usable_only=true&min_events=20` to inspect
which historical validation runs are eligible. You can still pin a specific
model with `calibration_run_id` when you want fully reproducible research.

Full S&P 500 lightweight scan payload:

```json
{
  "limit": 0,
  "top": 20,
  "news_per_ticker": 1,
  "workers": 16,
  "include_prices": false,
  "auto_calibration": true,
  "min_calibration_events": 20
}
```

Use `include_prices=true` for the final research report. The scanner only
fetches prices for symbols with collected events and uses batched yfinance
downloads before falling back to per-symbol history calls.

Optional LLM semantic scoring:

```json
{
  "limit": 50,
  "top": 20,
  "news_per_ticker": 2,
  "use_llm_scoring": true,
  "auto_calibration": true
}
```

When `use_llm_scoring=true`, the scanner calls the configured
OpenAI-compatible provider (DeepSeek/OpenRouter/DeepSeek API depending on
`agent/.env`) after the relevance filter, then blends the LLM judgment with
the rule score. This keeps the run auditable while allowing DeepSeek to
interpret ambiguous news headlines.

Useful historical API payload:

```json
{
  "tickers": ["AAPL", "NVDA", "MSFT", "TSLA", "AMZN"],
  "start_date": "2026-03-01",
  "end_date": "2026-04-30",
  "max_records_per_ticker": 20,
  "workers": 1,
  "sleep_seconds": 1.0,
  "validate": true
}
```

Useful resumable batch payload:

```json
{
  "tickers": ["AAPL", "NVDA", "MSFT", "TSLA"],
  "start_date": "2026-03-01",
  "end_date": "2026-04-30",
  "batch_size": 2,
  "max_batches": 1,
  "resume_run_id": "event_history_batch_YYYYMMDD_HHMMSS_xxxxxx",
  "validate": true
}
```

The batch endpoint writes:

- `data/historical_events.csv`: merged historical events from successful batches.
- `artifacts/historical_batch_state.json`: per-batch success / failure / empty state.
- `artifacts/event_impact_summary.json`: validation output when `validate=true`.
