# Agent Layer — Stage 2: Intermediate → Processed

## Your Role

The regex pass in Stage 1 handled deterministic entity replacements.
Your job is to catch everything it missed: SEC filing metadata, XBRL identifiers,
contextual references, paraphrased entity names, and anything a regex alone could not
reliably pattern-match.

**The threat model:** an LLM given these documents must not be able to identify the real company.
This includes attacking via SEC EDGAR lookups (accession numbers, CIK), XBRL namespace strings,
executive compensation table fingerprinting, social media handles, and contextual triangulation.

---

## Primary tool: `agent_pass_worker.py` (deterministic, run it for every ticker)

```
python3 agent_pass_worker.py <TICKER>      # intermediate/<TICKER>/ -> processed/<TICKER>/
```

The worker is the workhorse — it is deterministic, idempotent (it wipes its output dir
first, so re-runs never accumulate duplicate files), and reads all maps from
`config/entities.yaml` + the `additional_terms:` block. It applies:

| What | How |
|------|-----|
| Company names (distinctive) | **case-insensitive** substring + space-stripped form (catches `GENERAL MOTORS`, `mckesson123.com`, `KrogerSpecialtyMember`) |
| Short company abbreviations (AMD, GM, KR) | case-sensitive whole-word |
| Ticker XBRL forms | `gm:` `gm_` `gm-YYYYMMDD` `GmFinancial` (camel) `GMIMember` (compound) `gmail`-style prefix — all → synth ticker |
| Bare ticker token | `\bAMD\b`, `"tix":"sndk"` → synth ticker (uppercase-only for `app`, which is a real word) |
| CIK (10-digit padded) | substring (catches `ck0002023554` XBRL namespaces) |
| CIK short / EIN / commission-file | word-boundary |
| EDGAR Archives/search URLs | `sec.gov/Archives/...`, `efts.sec.gov/...` → `[SEC-FILING-URL]` |
| Accession numbers | `0001628280-24-005593` → `[FILING-ID]` |
| Executives | case-insensitive (catches ALL-CAPS signatures `MARY T. BARRA`) + space-stripped (`AdamForoughiMember`) |
| Subsidiaries / products / brands / banners | **proper-noun matching** (see corruption rule below) |
| Third-party companies (corp-suffix pattern) | → `[COMPANY]` (synthetic names are protected) |
| Title-prefixed person names | `Dr. Jane Smith` → `[EXECUTIVE]` |
| Output file naming | accession → `YYYYMMDD.txt` (collisions get `_02`, `_03`) |

### Critical correctness rule (why matching mode matters)

Different identifier classes need different matching, learned the hard way:

- **Case-insensitive substring** — ONLY for distinctive, non-dictionary strings (company
  names, person names, domains, distinctive subsidiaries like `Xilinx`). Required because
  these appear ALL-CAPS in headers and lowercase in XBRL.
- **Case-sensitive whole-word** — for proper nouns that are also dictionary words or word
  prefixes (`Cruise`, `Adjust`, `OnStar`, products like `Bolt`/`Colorado`). A
  case-insensitive substring here corrupts prose: `ATI`→ breaks "incorpor**ati**on",
  `Adjust`→ breaks "**Adjust**ed", `Cruise`→ breaks "cruising". Whole-word + case-sensitive
  catches the company form while preserving the common word and its inflections.
- **Whole-word case-insensitive** — for short tokens (≤4 chars: `ATI`, `MAX`, `GMC`).
- `additional_terms` entries default to case-insensitive substring; set `match: word` for
  short/ambiguous acronyms (`GME` would otherwise break "au**gme**nt").

After any change, run the verification sweep (below) and the corruption check (grep that
common words like `incorporation`, `adjusted`, `segment` still survive).

---

## Verification sweep — the real done condition

This is mandatory and replaces eyeballing. Per ticker, grep `data/processed/<TICKER>/` for:
the real company name (ci), `ticker_` / `ticker:` / `ticker-YYYYMMDD` (ci), the
ticker-as-compound-prefix, real CIK/EIN/commission-file (**word-boundary** — short numbers
match inside larger numbers/binary as false positives), executive full names, subsidiary
proper-noun forms, and every `additional_terms` real value. Iterate worker ↔ entities.yaml
until the count is **0** for all eight tickers. Then confirm the corruption check passes.

> A clean run was achieved with 0 real residuals across all 2,094 files. Apparent CIK hits
> from substring greps (e.g. `2488` inside binary, `1065280` inside `106528000`) are sweep
> false positives — verify with `\bNNN\b` word-boundary before "fixing" them.

---

## Manual spot-check (secondary, after the sweep is green)

The worker + sweep handle the bulk deterministically. Still read 2–3 of the largest output
files per ticker and skim for contextual leaks a regex can't catch (see categories below).
Fix anything found in `entities.yaml`/`additional_terms` and re-run the worker — do NOT hand-edit
individual output files (those edits are lost on the next re-run and don't generalize).

### 1. SEC / EDGAR Metadata Fingerprints
These are the strongest giveaway — directly searchable in EDGAR.

- [ ] No accession numbers remain (`\d{10}-\d{2}-\d{6}`)
- [ ] No CIK numbers remain in any form (numeric CIK value, in URLs, in XBRL `EntityCentralIndexKey`)
- [ ] No EDGAR URLs remain (`sec.gov/Archives/...`, `efts.sec.gov/...`)
- [ ] No XBRL namespace strings containing real ticker (`ticker-YYYYMMDD` pattern)
- [ ] No XBRL element prefixes containing real ticker (`ticker:ElementName`)
- [ ] No inline references like `"This report was filed as amd-20240213.htm"`

### 2. Company Identity Leaks
- [ ] Real company name does not appear in any form (full legal, abbreviation, ALL CAPS, possessive `'s`)
- [ ] Real ticker symbol does not appear as a standalone token
- [ ] Company-specific domain names, email domains, and URLs are replaced
- [ ] Social media handles (Twitter/X `@handle`, LinkedIn, etc.) are replaced
- [ ] Subsidiary and legacy company names are replaced (e.g., pre-acquisition names)

### 3. Executive / Personnel Leaks
- [ ] Named executives are replaced — check for first-name-only references ("Lisa approved..." → "[EXECUTIVE] approved...")
- [ ] Nicknames or informal references ("Mary's tenure", "Reed's vision") are replaced
- [ ] Compensation tables: executive names replaced, but **do not alter dollar amounts**
- [ ] Board member names replaced with `[BOARD MEMBER]`
- [ ] Auditor firm names that are company-specific (e.g., a Big 4 firm exclusively covering this client)

### 4. Product / Brand Fingerprints
- [ ] Company-specific product names replaced with synthetic equivalents from entities.yaml
- [ ] Version numbers attached to replaced products updated (e.g., "Ryzen 7 5800X" → "Apex 7 5800X")
- [ ] Taglines, slogans, or campaign names replaced with `[TAGLINE]`

### 5. Geographic Fingerprints
- [ ] HQ address replaced (including zip code, suite number)
- [ ] Campus/building names replaced ("Building A at [Real Address]")
- [ ] City name used as a proxy identifier (e.g., "our Detroit operations" when no other auto OEM HQ is there)
- [ ] Historical addresses (company relocated) also replaced

### 6. Third-Party Entity Leaks
These require judgment — automated patterns only catch formal names with corporate suffixes.

- [ ] Named customers or suppliers that triangulate identity (e.g., "our largest cloud customer" plus context)
- [ ] Named competitors — replace with `[COMPANY]` when they appear as direct comparisons
- [ ] Named law firms, auditors, underwriters, or banks if they are company-specific identifiers
- [ ] Named government agencies or programs that are exclusively tied to this company

### 7. Document Structure Fingerprints (hardest to catch)
- [ ] Form type header line — if the original filing type + exact date combination narrows identity, note it
- [ ] Exact filing dates are preserved (by design — they are part of the research data) but check that
  the date alone does not identify the company (e.g., only one company filed an 8-K on that exact day
  for a major event the market remembers)
- [ ] Internal cross-references like "as described in our 10-K filed [DATE]" — dates fine, but ensure
  the cross-reference doesn't name the company
- [ ] Exhibit numbers and exhibit titles that contain the company name

---

## Placeholder Reference

| Placeholder | Use for |
|-------------|---------|
| `[COMPANY]` | Any third-party company or organization name |
| `[EXECUTIVE]` | Any named individual (executive, board member, analyst) |
| `[BOARD MEMBER]` | Board directors specifically, if distinction matters |
| `[FILING-ID]` | SEC accession numbers |
| `[SEC-FILING-URL]` | EDGAR URLs |
| `[TAGLINE]` | Company slogans, campaign names |

---

## Output Rules

- **File naming:** output files are named `YYYYMMDD.txt` (filing date). Same-date collisions get `YYYYMMDD_02.txt`, `YYYYMMDD_03.txt`, etc.
- **No content removal:** do not delete sentences, paragraphs, or sections. Volume is intentional.
- **No narrative changes:** do not rephrase, summarize, or alter sentence structure.
- **No financial figure changes:** dollar amounts, percentages, share counts, and financial ratios must remain exactly as-is.
- **Synthetic names are authoritative:** always use the name from `config/entities.yaml`, not a new invention.
- **New identifiers found:** if you find an identifier not covered by entities.yaml, replace it with the appropriate placeholder, and include it in your notes so entities.yaml can be updated.

---

## Done Condition

Stage 2 is complete when:
1. Every file in `data/intermediate/<TICKER>/` has a corresponding date-named file in `data/processed/<TICKER>/` (file counts match).
2. The **verification sweep** returns 0 real residuals for all eight tickers (real name, ticker XBRL forms, word-boundary CIK/EIN, exec names, subsidiaries, additional_terms).
3. The **corruption check** passes — common words and inflections survive (`incorporation`, `adjusted`, `segment`, `cruise control`, `application`).
4. Spot-check: feeding the largest output files to an LLM and asking "what company is this?" yields no identification.
