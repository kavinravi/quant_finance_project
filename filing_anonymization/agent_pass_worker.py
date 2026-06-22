"""
Stage 2 worker: intermediate/<TICKER>/ → processed/<TICKER>/

Usage: python3 agent_pass_worker.py <TICKER>

Applies:
  1. Safety-net re-application of entities.yaml substitutions
  2. Corporate-suffix pattern → [COMPANY]
  3. Title-prefixed person names (Mr./Ms./Dr.) → [EXECUTIVE]

Protected entities (regulatory bodies, exchanges, accounting standards)
are never replaced.
"""

import re, sys, yaml, json
from pathlib import Path

TICKER = sys.argv[1].upper()
BASE    = Path(__file__).parent
IN_DIR  = BASE / "data" / "intermediate" / TICKER
OUT_DIR = BASE / "data" / "processed"    / TICKER

# ── Load config ──────────────────────────────────────────────────────────────
with open(BASE / "config" / "entities.yaml") as f:
    CONFIG = yaml.safe_load(f)

ticker_cfg    = CONFIG["tickers"][TICKER]
SYNTHETIC_NAME = ticker_cfg["company_names"]["synthetic"]

# Collect all synthetic company names across all tickers (never replace these)
SYNTHETIC_NAMES = {c["company_names"]["synthetic"] for c in CONFIG["tickers"].values()}

# Collect all synthetic executive names (never replace these)
SYNTHETIC_EXECS = set()
for c in CONFIG["tickers"].values():
    for e in (c.get("executives") or []):
        s = e.get("synthetic", "")
        if s:
            SYNTHETIC_EXECS.add(s)

# ── Safety-net substitution patterns ─────────────────────────────────────────
def _to_list(val):
    if val is None: return []
    if isinstance(val, list): return [str(v) for v in val if v is not None]
    return [str(val)]

def _add(pairs, real_val, synthetic, wb=None):
    if not synthetic: return
    for real in _to_list(real_val):
        if not real: continue
        needs_wb = wb if wb is not None else not any(c in real for c in " ,()")
        pat = (r"\b" + re.escape(real) + r"\b") if needs_wb else re.escape(real)
        pairs.append((len(real), pat, synthetic))

raw = []
cn = ticker_cfg.get("company_names") or {}
_add(raw, cn.get("real"), cn.get("synthetic"))
ts = ticker_cfg.get("ticker_symbol") or {}
_add(raw, ts.get("real"), ts.get("synthetic"), wb=True)
for ar in (ts.get("also_replace") or []):
    _add(raw, ar.get("real"), ar.get("synthetic"), wb=True)
legal = ticker_cfg.get("legal_ids") or {}
for k in ["ein", "commission_file", "cik"]:
    item = legal.get(k) or {}
    _add(raw, item.get("real"), item.get("synthetic"))
for addr in (ticker_cfg.get("addresses") or []):
    _add(raw, addr.get("real"), addr.get("synthetic"), wb=False)
for ph in (ticker_cfg.get("phone_numbers") or []):
    _add(raw, ph.get("real"), ph.get("synthetic"), wb=False)
for d in (ticker_cfg.get("domains") or []):
    _add(raw, d.get("real"), d.get("synthetic"))
for e in (ticker_cfg.get("executives") or []):
    _add(raw, e.get("real"), e.get("synthetic"))
for cat in ["products", "brands", "store_banners", "private_labels"]:
    for item in (ticker_cfg.get(cat) or []):
        _add(raw, item.get("real"), item.get("synthetic"))
for s in (ticker_cfg.get("subsidiaries") or []):
    _add(raw, s.get("real"), s.get("synthetic"))
raw.sort(key=lambda x: x[0], reverse=True)
SAFETY_SUBS = [(pat, repl) for _, pat, repl in raw]

# ── Protected entity lists ────────────────────────────────────────────────────
PROTECTED_EXACT = frozenset([
    "SEC", "FASB", "PCAOB", "NYSE", "NASDAQ", "Nasdaq", "IRS", "FTC",
    "DOJ", "GAAP", "IFRS", "EU", "NATO", "DoD", "Fed", "FINRA", "CFTC",
    "OECD", "IMF", "WTO", "WHO", "UN", "FBI", "CIA", "NSA", "FDA", "EPA",
])

PROTECTED_SUBSTRINGS = [
    "Securities and Exchange Commission",
    "Financial Accounting Standards Board",
    "Public Company Accounting Oversight Board",
    "New York Stock Exchange",
    "Nasdaq Stock Market",
    "Internal Revenue Service",
    "Federal Reserve",
    "Federal Trade Commission",
    "Department of Justice",
    "Department of Defense",
    "European Union",
    "Generally Accepted Accounting Principles",
    "International Financial Reporting Standards",
    "U.S. GAAP",
    "United States of America",
    "United States Government",
    "U.S. Government",
    "London Stock Exchange",
    "Tokyo Stock Exchange",
    "Hong Kong Stock Exchange",
    "Chicago Mercantile Exchange",
    "Financial Industry Regulatory Authority",
]

def is_protected(name: str) -> bool:
    name = name.strip()
    if name in PROTECTED_EXACT:
        return True
    if name in SYNTHETIC_NAMES:
        return True
    for p in PROTECTED_SUBSTRINGS:
        if p.lower() in name.lower():
            return True
    return False

# ── Regex patterns ────────────────────────────────────────────────────────────
# Match "Acme Technologies Inc." / "First National Corp" / etc.
CORP_PAT = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9&\'\-\.]+\s+){0,5}[A-Z][A-Za-z0-9&\'\-\.]+\s+"
    r"(?:Inc\.?|Corp\.?|LLC|Ltd\.?|L\.L\.C\.?|L\.P\.?|LLP|LP|PLC|N\.V\.|S\.A\.|AG|GmbH|"
    r"Corporation|Incorporated|Company|Technologies|Technology|Holdings|Group|Enterprises|"
    r"Partners|Associates|Industries|Solutions|Systems|Services|Networks|Analytics|"
    r"Capital|Ventures|Investments|Management|Financial|Pharmaceuticals|Therapeutics|"
    r"Semiconductors|Communications|Energy|Media|Entertainment|Logistics|Distribution)\b"
)

# Match "Mr. John Smith" / "Dr. Jane Doe" etc.
PERSON_PAT = re.compile(
    r"\b(?:Mr\.|Ms\.|Mrs\.|Dr\.|Prof\.)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b"
)

def replace_org(m):
    matched = m.group()
    if is_protected(matched):
        return matched
    return "[COMPANY]"

def replace_person(m):
    matched = m.group()
    for synth_exec in SYNTHETIC_EXECS:
        if synth_exec in matched:
            return matched
    return "[EXECUTIVE]"

# ── Process files ─────────────────────────────────────────────────────────────
OUT_DIR.mkdir(parents=True, exist_ok=True)
files = sorted(IN_DIR.glob("*.txt"))
company_count = 0
person_count  = 0

for fpath in files:
    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    # 1. Safety-net entity substitutions
    for pat, repl in SAFETY_SUBS:
        text = re.sub(pat, repl, text)

    # 2. Third-party company names → [COMPANY]
    before = text.count("[COMPANY]")
    text = CORP_PAT.sub(replace_org, text)
    company_count += text.count("[COMPANY]") - before

    # 3. Titled person names → [EXECUTIVE]
    before = text.count("[EXECUTIVE]")
    text = PERSON_PAT.sub(replace_person, text)
    person_count += text.count("[EXECUTIVE]") - before

    with open(OUT_DIR / fpath.name, "w", encoding="utf-8") as f:
        f.write(text)

# ── Output JSON for Workflow collection ──────────────────────────────────────
result = {
    "ticker": TICKER,
    "files_processed": len(files),
    "company_replacements": company_count,
    "executive_replacements": person_count,
    "output_dir": str(OUT_DIR),
}
print(json.dumps(result))
