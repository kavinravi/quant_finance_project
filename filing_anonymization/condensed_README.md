# LLM-Ready Condensed SEC Filings (8 anonymized companies, 7 years)

Raw SEC filings are far too large for commercial-LLM API calls (a single raw
10-K file here measures 0.7M–11M tokens once XBRL data, exhibits, and markup
are counted; even the clean narrative alone is ~110k tokens). This dataset cuts
every filing down to the sections that matter for sentiment analysis,
forecasting, and comparative fundamental analysis, at sizes that fit real API
limits (~500k tokens/call for OpenAI, 200k–1M for Anthropic/Google).

## Structure

```
standard/<1..8>/YYYY-MM-DD_FORM.txt   # per-stock deep-analysis tier
compact/<1..8>/YYYY-MM-DD_FORM.txt    # whole-universe comparative tier
```

- Directories `1`–`8` are the eight companies (identities intentionally hidden).
- Filenames sort chronologically; the date is the fiscal period end (10-K/10-Q)
  or the report date (8-K). FORM ∈ {10-K, 10-Q, 8-K, and /A amendments}.
- Every file begins with a banner line stating form, period, and which sections
  it contains. Cuts inside a section are marked `[...TRUNCATED]`.

## What was kept (both tiers, different caps)

| Form | Sections kept | Rationale |
|------|---------------|-----------|
| 10-K | Cover page + Item 1 Business; Item 1A Risk Factors (head); full MD&A (Item 7); primary financial statements (income statement, balance sheet, cash flows + first notes) | Business model, management tone/outlook, risk language, and the numbers — everything sentiment/forecasting needs beyond raw financials |
| 10-Q | Cover; condensed statements; MD&A | Quarterly results + management commentary |
| 8-K  | Header items + press-release exhibit (earnings 8-Ks get a larger cap than administrative ones) | Earnings releases are the highest-signal sentiment documents; admin 8-Ks (officer changes, votes) kept small |

Dropped: XBRL fact dumps, exhibit blobs, HTML markup, S-8/S-3 registrations and
similar non-analytical forms.

## Token budgets (o200k_base, i.e. GPT-4o/4.1/5 tokenizer)

| Bundle | standard | compact |
|---|---|---|
| One 10-K | ~24k (max 27k) | ~8k (max 9k) |
| One 10-Q | ~4.5k | ~1.6k |
| One 8-K | ~1.2k median | ~0.4k median |
| One stock, one year (all forms) | ~54k (max 85k) | ~18k (max 28k) |
| One stock, all 7 years | 243k–431k | 16k–147k |
| All 8 stocks, 10-Ks only | 987k | **336k** |
| All 8 stocks, everything | 2.50M | **846k** |

## Suggested call patterns

- **Deep single-company analysis** (sentiment time series, forecasting one
  name): send one stock's entire `standard/` folder in one call — every stock
  fits within 500k tokens with room for the prompt and response.
- **Cross-company comparison in one call**: send all of `compact/*/​*_10-K.txt`
  (~336k tokens) to any model, or the entire `compact/` tree (~846k) to a
  1M-context model (Gemini, GPT-4.1, Claude Sonnet 1M).
- **Quarterly event studies**: per stock-year bundles are ~54k (standard) —
  cheap enough to loop over all 56 stock-years.
- Throughput note: per-minute token limits usually bind before context does;
  batching by stock-year keeps individual calls small and retryable.
