# Agent Layer — Stage 2: Intermediate → Processed

## Your Role

The regex pass in Stage 1 handled deterministic replacements. Your job is to catch everything it missed: contextual references, implied identifiers, paraphrased entity names, and anything a regex could not pattern-match reliably.

## What You're Looking For

Scan each document for any remaining content that could identify the real company, including but not limited to:

- Company name variants (abbreviations, informal references, legacy names, parent/subsidiary names)
- Executive names not caught by regex (e.g., first-name-only references, nicknames)
- Product or brand names that are company-specific
- Geographic references tied uniquely to this company (headquarters city used as a proxy, regional office names)
- Ticker symbols or CUSIP/ISIN numbers
- Competitor references that triangulate identity (e.g., "our main competitor in streaming" in context)
- Any URLs, domain names, or email addresses containing the real company name
- Analyst firm names tied to company-specific coverage if contextually identifying

Do **not** remove:
- Generic industry terms
- Macroeconomic references
- Regulatory body names (SEC, FASB, etc.)
- References to named competitors that appear generically across the industry

## How to Execute

Deploy **Haiku or Sonnet subagents** (not Opus) — one subagent per file. These passes are high-volume and the marginal quality gain from a larger model is not worth the cost.

Each subagent receives:
1. The full text of one file from `data/intermediate/<TICKER>/<filing_type>/`
2. The synthetic entity map for that ticker from `config/entities.yaml` (so it knows what replacements have already been made and what the synthetic names are)
3. The instruction to return the full document text with all remaining identifiers replaced — using the same synthetic tokens already established, not new ones

## Output

Write the cleaned file to `data/processed/`, mirroring the exact same directory structure as `data/intermediate/`:

```
data/processed/<TICKER>/<filing_type>/<filename>
```

## Consistency Rule

Use the synthetic names already defined in `config/entities.yaml`. If you encounter an identifier not covered by the map, invent a plausible synthetic replacement, note it, and apply it consistently within that document. Do not add it to `entities.yaml` automatically — flag it for human review.

## Done Condition

Stage 2 is complete when every file in `data/intermediate/` has a corresponding file in `data/processed/` and no document in `data/processed/` contains a string that matches any real entity listed in `entities.yaml`.
