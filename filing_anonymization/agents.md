# Filing Anonymization — Agent Guide

## Project Goal

Produce a synthetic financial dataset in which no AI system can identify the real company behind the data. Every filing must be stripped of all identifying information while preserving volume and realistic document structure.

## Repo Layout

```
filing_anonymization/
├── data/
│   ├── preprocessed/     # input: filings stripped of logos/images/tables (by ticker)
│   ├── intermediate/     # after regex pass: identifiers replaced with synthetic tokens
│   └── processed/        # final output: fully anonymized, ready for use
├── config/
│   └── entities.yaml     # per-ticker substitution maps (real → synthetic)
├── 01_regex_pass.ipynb   # Stage 1: preprocessed → intermediate
├── agents.md             # this file
└── agent_layer.md        # Stage 2 instructions: intermediate → processed
```

Eight tickers are present: AMD, APP, GM, KR, MCK, NFLX, PLTR, SNDK. Each has subdirectories for filing types (10-K, 10-Q, 8-K, etc.).

## Pipeline Overview

### Stage 1 — Regex Pass (notebook)
`01_regex_pass.ipynb` reads from `data/preprocessed/`, applies deterministic regex substitutions defined in `config/entities.yaml`, and writes output to `data/intermediate/`, preserving the same ticker/filing-type directory structure.

What the regex pass replaces: company names, CEO/executive names, product names, locations, phone numbers, addresses, ticker symbols, and any other hard identifiers listed in `entities.yaml`.

### Stage 2 — Agent Pass (you)
Once the regex pass is complete and `data/intermediate/` is populated, read `agent_layer.md` for instructions on how to run the LLM-based second pass that moves files from `data/intermediate/` to `data/processed/`.

## Key Constraints

- **Do not fabricate price-moving events** (mergers, acquisitions, earnings surprises). Only surface-level identifier replacement is in scope.
- **Preserve volume** — do not summarize or compress documents. A 250-page 10-K should remain ~250 pages.
- **Hard identifiers must go**: logos (already stripped), footnote citations to real entities, product names, location names, executive names.
- **Perfect consistency is not required** — a synthetic CEO name that varies across one news article is acceptable. The only hard requirement is that the company remains unidentifiable.
- **Do not synthesize structured/price data** — this pipeline is for unstructured filings only.
