# Financial Perturbation — Design

## Why
The blind-attack test identified all 8 companies at 99% even after string anonymization.
A major vector ("Bucket 3") was **exact financial figures** — e.g. AMD "+56% to $2.8B",
GM "1,589,788,282 shares / $46.1B market cap", matched by an LLM from memory or web search.
Anonymizing names cannot touch this; perturbing the numbers can. The professor authorized:
"reduce/increase overall financials (e.g. −20%) so numbers aren't identical but the logic
still makes sense" and "change easy-to-find headline numbers by ±5%."

This perturbation is **one of three layers** and does NOT replace the others:
1. Lexical anonymization (names/tickers/IDs — the regex + agent pass).
2. Semantic generalization (segments, named events, fiscal-year tells — still TODO).
3. **Quantitative perturbation (this document).**

## Principles

### 1. Per-company, time-series-consistent
One transform **per company**, applied to that company's **entire filing history**, not
per-filing. Otherwise the FY2023 figure quoted as "prior year" in the FY2024 10-K won't
match the actual 2023 filing — breaking both internal coherence and any time-series analysis
in the downstream 310 course. The discovery loop runs on 8 filings; the transform is defined
once per company and ultimately applied to all of that company's filings.

### 2. Two layers
- **Layer A — uniform scale.** Multiply every monetary amount by a single per-company factor
  `f`. This kills absolute-value matching while **preserving internal articulation** (balance
  sheet still balances, segments still sum to total, EPS still ties — everything scales
  together) and preserving ratios/growth rates (keeps the data self-consistent and realistic).
- **Layer B — headline jitter.** For the handful of easily-Googleable headline figures and
  their famous growth rates, apply an additional independent ±3–7%. This breaks the
  **ratio/growth fingerprint** at exactly the points a model retrieves (Layer A alone leaves
  "+56% YoY" intact because both years scale equally). Accept the small articulation drift this
  creates on those headlines — within the professor's "logic still makes sense" tolerance.

### 3. Non-round factor, distinct per company
Avoid exactly −20% / +20%: it's trivially reversible (×1.25) and "round" looks engineered.
Use `f ∈ [0.78, 0.88] ∪ [1.12, 1.22]`, 2–3 significant figures, a different value per company
(e.g. AMD 0.84, GM 1.17, …). Factors live in `config/perturbation.yaml`.

### 4. Scope = monetary quantities ONLY
Never alter: dates, years, fiscal-period strings, CIK / EIN / commission-file / accession
(already synthetic), ZIP codes, section / item / rule numbers, page numbers, plain counts
("3 reportable segments", employee headcount is borderline — treat as headline), and bare
percentages (they're ratios; Layer A leaves them, Layer B jitters only selected headline
growth rates).

**Share counts & per-share:** scaling `$` by `f` and shares by `f` leaves EPS unchanged
(NI·f / shares·f). Decision: scale share counts by `f` too (keeps market-cap = price×shares
coherent), then put the headline share count + EPS in the Layer-B jitter set so they vary.

## What counts as a monetary amount (extraction)

The filings are table-stripped (FinBERT prep), so numbers live in two surfaces:

1. **Prose dollar figures** — `$ 2.8 billion`, `$1,234`, `$1,234.5 million`.
   Regex-anchored on `$`, which safely avoids dates/IDs/ZIPs. Scale the numeric part by `f`.
   **This is v1 (implemented now).** Same-`f` scaling keeps all prose figures mutually consistent.

2. **XBRL numeric data blocks** — long runs of bare integers
   (`333000 97287000 35536000 106528000 …`). These are the exact reported values and the
   strongest machine-matchable fingerprint, but bare integers are hard to separate from
   counts/dates/IDs. **Decision required (flag to team/professor):**
   - (a) Scale integers above a magnitude threshold inside recognizable XBRL value contexts.
   - (b) Strip/coarsen the XBRL numeric blocks entirely — they are non-narrative machine data,
     low value for an LLM-reading exercise, high as a fingerprint.
   Recommendation: (a) if 310 wants the structured values preserved; otherwise (b) for safety.
   v1 does NOT touch these yet.

## Pipeline placement
New stage **after** anonymization (operating on `data/processed/`), driven by
`config/perturbation.yaml`. Numbers are identical across pre/intermediate/processed, so it
runs on the already-anonymized text; the `$`-anchor guarantees it won't scale the synthetic
CIK/EIN/phone/ZIP/dates.

## Testing & validation
- Re-run the blind-attack loop (fresh strong attackers, **including one with web access**) on
  perturbed+anonymized filings. Success = no confident ID, and the attacker's "cited
  financials" no longer match any real filing.
- Diff check: confirm only `$`-amounts changed (no dates/CIK/EIN/ZIP/section numbers touched).
- Articulation spot-check: a few segment/balance sums still tie within jitter tolerance.
- Cross-filing: the same line item across years scaled by the same `f`.

## Honest limitations
- Uniform scaling preserves ratios; only Layer-B jitter breaks them, and only for curated
  headlines. A model reasoning about **industry + approximate scale** ("auto OEM, ~$130B
  revenue") may still shortlist correctly even with exact numbers changed. Defeating
  scale-reasoning needs larger/structural perturbation, which costs realism.
- Does not address lexical (Bucket 1) or semantic (Bucket 2) leaks — separate layers.
- XBRL bare-number handling is deferred pending the decision above.
