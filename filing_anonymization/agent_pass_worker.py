"""
Stage 2 worker: intermediate/<TICKER>/ -> processed/<TICKER>/

Usage: python3 agent_pass_worker.py <TICKER>

v2 — case-insensitive + XBRL-aware. Fixes the leaks the case-sensitive v1 missed:
  - ALL-CAPS / lowercase company-name variants (GENERAL MOTORS, mckesson123.com)
  - XBRL element prefixes with the real ticker:   gm:  gm_  gm-YYYYMMDD  GmFinancial
  - Real name embedded in XBRL element names:      KrogerSpecialty, McKessonEurope
  - Real CIK / EIN / commission-file numbers (word-boundary)
  - additional_terms discovered during the agent pass (acronyms, brands, board, etc.)

Replacement passes (per file, in order):
  1. EDGAR Archives/search URLs        -> [SEC-FILING-URL]
  2. SEC accession numbers             -> [FILING-ID]
  3. Ticker XBRL forms (ci)            -> synthetic ticker
  4. Legal IDs (word-boundary)         -> synthetic
  5. Case-insensitive substring map    -> synthetic   (names, execs, addresses, domains, subs, additional)
  6. Word-boundary case-sensitive map  -> synthetic   (short abbreviations / ambiguous acronyms)
  7. Case-sensitive substring map      -> synthetic   (products/brands — avoid common-word false positives)
  8. Third-party orgs                  -> [COMPANY]
  9. Titled person names               -> [EXECUTIVE]

Output files are renamed to YYYYMMDD.txt (filing date). The output dir is wiped
first so re-runs are idempotent (no accumulating _NN duplicates).

Products/brands stay case-sensitive on purpose: tokens like MAX, Bolt, Ultra,
Colorado would wreck normal text as case-insensitive substrings.
"""

import re, sys, yaml, json, shutil
from pathlib import Path
from datetime import datetime

TICKER  = sys.argv[1].upper()
BASE    = Path(__file__).parent
IN_DIR  = BASE / "data" / "intermediate" / TICKER
OUT_DIR = BASE / "data" / "processed"    / TICKER

with open(BASE / "config" / "entities.yaml") as f:
    CFG = yaml.safe_load(f)

ticker_cfg     = CFG["tickers"][TICKER]
ADDITIONAL     = list((CFG.get("additional_terms") or {}).get(TICKER, []))
SYNTHETIC_NAME = ticker_cfg["company_names"]["synthetic"]

# Merge supplements: lexical-sweep (bulk discovered entities) + semantic map
# (targeted segment names / event labels / fiscal-year phrases). Same schema.
for _supp_name in ("entities_lexical_sweep.yaml", "semantic_map.yaml"):
    _supp_path = BASE / "config" / _supp_name
    if _supp_path.exists():
        with open(_supp_path) as f:
            _supp = yaml.safe_load(f) or {}
        ADDITIONAL += (_supp.get("additional_terms") or {}).get(TICKER, [])

# Synthetic names/execs across all tickers — never replace or [COMPANY]-ify these
SYNTHETIC_NAMES = {c["company_names"]["synthetic"] for c in CFG["tickers"].values()}
SYNTHETIC_EXECS = set()
for c in CFG["tickers"].values():
    for e in (c.get("executives") or []):
        if e.get("synthetic"):
            SYNTHETIC_EXECS.add(e["synthetic"])

def _flat(v):
    if v is None: return []
    if isinstance(v, list):
        out = []
        for x in v: out += _flat(x)
        return out
    return [str(v)]

def _distinctive(s):
    """True if safe to match case-insensitively as a substring."""
    return (" " in s) or (len(s) >= 6 and bool(re.search("[a-z]", s)))

def _nospace(s):
    # XBRL element names strip spaces/punctuation but KEEP periods (e.g.
    # "U.S.PharmaceuticalandSpecialtySolutionsMember"), so keep periods here.
    for ch in (" ", ",", "-", "&", "'", "’", "/"):
        s = s.replace(ch, "")
    return s

# ── Accumulate replacement maps ───────────────────────────────────────────────
ci_map     = {}   # lower(real)  -> synthetic   (case-insensitive substring)
wb_ci_map  = {}   # lower(real)  -> synthetic   (word-boundary, case-insensitive: numbers)
wb_cs_map  = {}   # real         -> synthetic   (word-boundary, case-sensitive)
cs_map     = {}   # real         -> synthetic   (case-sensitive substring: products)
proper_camel_rules = []  # (compiled, repl) — dict-word proper noun as XBRL camelCase prefix
camel_ci_rules     = []  # (compiled, repl) — camelCase brand, ci anchored to token start

def add_ci(real, synth):
    if real and synth: ci_map[real.lower()] = synth

def add_ci_multi(real, synth):
    """Add a string and (if multi-word) its space-stripped form."""
    add_ci(real, synth)
    if " " in real:
        add_ci(_nospace(real), _nospace(synth))

def add_proper(real, synth):
    """Proper-noun term (subsidiary / product / brand / banner). Designed so it
    can never corrupt a common word or its inflections:
      - multi-word phrase  -> case-sensitive substring (+ ALL-CAPS variant)
      - single token >=5   -> case-sensitive WHOLE-WORD (+ ALL-CAPS variant).
        Whole-word (not substring) protects 'Adjusted'/'Cruiser' etc. Case-
        sensitive protects the lowercase common word ('cruise control').
      - single token <=4    -> whole-word, case-insensitive (ATI, MAX, GMC)."""
    if not (real and synth): return
    if " " in real:
        cs_map[real] = synth
        if real.upper() != real: cs_map[real.upper()] = synth
        add_ci(_nospace(real), _nospace(synth))   # XBRL compound form (no spaces)
    elif re.search(r"[a-z][A-Z]", real):
        # camelCase brand (CoverMyMeds, AmeriCredit, OnStar): distinctive, never
        # a common word — but NOT safe as a bare ci substring: "onstar" ci eats
        # the middle of us-gaap "...OfSatisfactionStartDateAxis" (…cti-onStar-
        # tDate…), splicing the synthetic into a standard element name
        # (corruption + a reverse-engineerable leak). So: ci substring anchored
        # to a token start — non-alnum before (onstar.com, onstarbygm.ca,
        # ONSTAR) or a camelCase transition (OfCovermymedsDetails, GmOnStar
        # Member). Mid-word lowercase runs (satisfacti·onstar·tdate) never
        # start a token, so the us-gaap element survives.
        # NB: no re.IGNORECASE — it would make the boundary lookarounds
        # case-insensitive too ((?=[A-Z]) -> any letter), re-breaking the
        # us-gaap element. Case-insensitivity is built into the brand chars.
        _brand_ci = "".join(
            f"[{c.upper()}{c.lower()}]" if c.isalpha() else re.escape(c)
            for c in real)
        camel_ci_rules.append((re.compile(
            rf"(?:(?<![A-Za-z0-9])|(?<=[a-z0-9])(?=[A-Z])){_brand_ci}"), synth))
    elif len(real) >= 5:
        wb_cs_map[real] = synth
        if real.upper() != real: wb_cs_map[real.upper()] = synth
        # dict-word proper noun in an XBRL camelCase compound (CruiseMember,
        # UltiumCellHoldings): "Cruise" followed by an uppercase letter -> no-space
        # synthetic. Case-sensitive + lookahead so prose ("cruise control") is safe.
        # No \b: catches the token mid-compound too (MRDGCruiseHoldings,
        # ...AndUltiumCell). Case-sensitive + uppercase-lookahead keeps prose safe
        # ("cruise control" / "Cruise " never match; only camelCase XBRL does).
        proper_camel_rules.append(
            (re.compile(rf"{re.escape(real)}(?=[A-Z])"), _nospace(synth)))
    else:
        wb_ci_map[real.lower()] = synth

# Company names — distinctive (non-dictionary) so ci substring is safe and is
# REQUIRED (they appear lowercase in XBRL, e.g. mckesson123.com). Short
# abbreviations (AMD, GM, KR) go to case-sensitive whole-word.
cn = ticker_cfg.get("company_names") or {}
syn_name = cn.get("synthetic")
for nm in _flat(cn.get("real")):
    if _distinctive(nm):
        add_ci_multi(nm, syn_name)
    else:
        wb_cs_map[nm] = syn_name          # short abbreviations (AMD, GM, KR ...)

# Legal IDs.  10-digit zero-padded CIK -> substring (catches "ck0002023554"
# XBRL namespaces where a leading word-boundary fails). Shorter numeric forms
# stay word-boundary so they don't match inside larger numbers.
legal = ticker_cfg.get("legal_ids") or {}
for k in ["ein", "commission_file", "cik"]:
    node = legal.get(k)
    if isinstance(node, dict):
        synth = node.get("synthetic")
        for val in _flat(node.get("real")):
            if not (val and synth): continue
            if re.fullmatch(r"\d{10}", val):
                ci_map[val.lower()] = synth        # substring (numeric, case-agnostic)
            else:
                wb_ci_map[val.lower()] = synth      # word-boundary

# Addresses / domains — case-insensitive substring
for addr in (ticker_cfg.get("addresses") or []):
    for val in _flat(addr.get("real")): add_ci(val, addr.get("synthetic"))
for dom in (ticker_cfg.get("domains") or []):
    for val in _flat(dom.get("real")): add_ci(val, dom.get("synthetic"))

# Phones — build a WHITESPACE-TOLERANT regex from the 10 digits so all formats match,
# including the preprocessing artifact "( 408 ) 540-3700" (spaces inside parens).
phone_rules = []  # (compiled, synthetic)
for ph in (ticker_cfg.get("phone_numbers") or []):
    synth = ph.get("synthetic")
    if not synth: continue
    for val in _flat(ph.get("real")):
        digits = re.sub(r"\D", "", val)
        if len(digits) == 10:
            d = digits
            pat = rf"\(?\s*{d[0:3]}\s*\)?\s*[-.\s]?\s*{d[3:6]}\s*[-.\s]?\s*{d[6:10]}"
            phone_rules.append((re.compile(pat), synth))

# Executives — ci (+ no-space form): person names are non-dictionary, and appear
# ALL-CAPS in signature blocks (MARY T. BARRA) so case-insensitive is needed.
# Also add the First+Last no-space form (drops middle initials) to catch XBRL compound
# member names like "AlexanderKarpMember".
def _first_last_nospace(name):
    parts = [p for p in re.split(r"\s+", name.strip()) if p and not re.fullmatch(r"[A-Za-z]\.?", p)]
    return (parts[0] + parts[-1]) if len(parts) >= 2 else None

for e in (ticker_cfg.get("executives") or []):
    synth = e.get("synthetic")
    for val in _flat(e.get("real")):
        add_ci_multi(val, synth)
        rfl, sfl = _first_last_nospace(val), _first_last_nospace(synth or "")
        if rfl and sfl: add_ci(rfl, sfl)

# Subsidiaries — proper-noun (case-sensitive). Many are dictionary words
# (Cruise, Adjust), so ci substring would corrupt prose.
for s in (ticker_cfg.get("subsidiaries") or []):
    for val in _flat(s.get("real")): add_proper(val, s.get("synthetic"))

# additional_terms — ci substring by default (all curated as distinctive,
# non-dictionary strings); word-boundary CS when match: word (short/ambiguous).
for item in ADDITIONAL:
    synth = item.get("synthetic")
    mode  = item.get("match", "ci")
    for val in _flat(item.get("real")):
        if not (val and synth): continue
        if mode == "word":
            wb_cs_map[val] = synth
        else:
            add_ci_multi(val, synth)

# Products / brands / banners / labels — proper-noun (case-sensitive substring;
# short tokens like MAX/GMC become whole-word to avoid MAXIMUM-style collisions)
for cat in ["products", "brands", "store_banners", "private_labels"]:
    for item in (ticker_cfg.get(cat) or []):
        for val in _flat(item.get("real")):
            add_proper(val, item.get("synthetic"))

# Cross-ticker company names -> [COMPANY]. The other 7 dataset companies show up
# in THIS ticker's filings (proxy peer-group tables, exec bios, customer lists)
# and CORP_PAT misses suffix-less forms ("Advanced Micro Devices," / "Netflix").
# Generic [COMPANY], NOT the other ticker's synthetic name — that would link the
# mention to the other company's numbered dir in the deliverable. Distinctive
# variants only: short forms (AMD, GM, KR) are ambiguous cross-ticker ("GM" =
# general merchandise in Kroger tables). Own-ticker mappings win on collision.
for _other, _ocfg in CFG["tickers"].items():
    if _other == TICKER: continue
    for nm in _flat((_ocfg.get("company_names") or {}).get("real")):
        if _distinctive(nm) and nm.lower() not in ci_map:
            ci_map[nm.lower()] = "[COMPANY]"
            ns = _nospace(nm)
            if " " in nm and ns.lower() not in ci_map:
                ci_map[ns.lower()] = "[COMPANY]"
    # Cross-ticker executives -> [EXECUTIVE]: people cross company lines (AMD's
    # CFO sits on SNDK's board, bio names him untitled so PERSON_PAT misses it).
    # Full names only (space required) — single tokens are too collision-prone.
    for _e in (_ocfg.get("executives") or []):
        for nm in _flat(_e.get("real")):
            if " " in nm and nm.lower() not in ci_map:
                ci_map[nm.lower()] = "[EXECUTIVE]"
                ns = _nospace(nm)
                if ns.lower() not in ci_map:
                    ci_map[ns.lower()] = "[EXECUTIVE]"

# ── Compile combined alternation patterns (longest-first) ─────────────────────
def compile_alt(keys, flags=0, boundary=False):
    keys = sorted({k for k in keys if k}, key=len, reverse=True)
    if not keys: return None
    body = "|".join(re.escape(k) for k in keys)
    pat  = (r"\b(?:" + body + r")\b") if boundary else r"(?:" + body + r")"
    return re.compile(pat, flags)

RE_CI    = compile_alt(ci_map.keys(),    re.IGNORECASE)
RE_WB_CI = compile_alt(wb_ci_map.keys(), re.IGNORECASE, boundary=True)
RE_WB_CS = compile_alt(wb_cs_map.keys(), 0,             boundary=True)
RE_CS    = compile_alt(cs_map.keys(),    0)

def sub_ci(m):    return ci_map[m.group(0).lower()]
def sub_wb_ci(m): return wb_ci_map[m.group(0).lower()]
def sub_wb_cs(m): return wb_cs_map[m.group(0)]
def sub_cs(m):    return cs_map[m.group(0)]

# ── Ticker XBRL forms (real ticker + also_replace aliases) ────────────────────
ts = ticker_cfg.get("ticker_symbol") or {}
ticker_pairs = [(str(ts.get("real", TICKER)), str(ts.get("synthetic", "SYN")))]
for ar in (ts.get("also_replace") or []):
    if ar.get("real"): ticker_pairs.append((str(ar["real"]), str(ar.get("synthetic", "SYN"))))

# Tickers that are also common English words — only replace the UPPERCASE form
# (case-sensitive) so we don't mangle prose like "the app store".
COMMON_WORD_TICKERS = {"app"}

ticker_rules = []   # (compiled, repl) — repl may be str or callable
for real_t, synth_t in ticker_pairs:
    rt, st = real_t.lower(), synth_t.lower()
    # ticker-YYYYMMDD  -> synth-YYYYMMDD
    ticker_rules.append((re.compile(rf"\b{re.escape(rt)}-(\d{{8}})", re.I),
                         lambda m, st=st: f"{st}-{m.group(1)}"))
    # ticker_  /  ticker:
    ticker_rules.append((re.compile(rf"\b{re.escape(rt)}_", re.I), f"{st}_"))
    ticker_rules.append((re.compile(rf"\b{re.escape(rt)}:", re.I), f"{st}:"))
    # Ticker as a compound prefix in XBRL element names / doc-name artifacts:
    #   AMD64, amdform10, GMIMember, GmeMember, GMDAT, WDCSubsidiary, ...
    # For tickers that are also word onsets (kr->krill, app->application) only the
    # UPPERCASE form is safe; for distinctive tickers match case-insensitively.
    su, sl = synth_t.upper(), synth_t.lower()
    # Prefix rule: uppercase-only for tickers whose lowercase form starts real
    # words (kr->krill, app->application); case-insensitive otherwise.
    if rt in COMMON_WORD_TICKERS or rt == "kr":
        ticker_rules.append((re.compile(rf"\b{re.escape(real_t.upper())}(?=[A-Za-z0-9])"), su))
    else:
        ticker_rules.append((
            re.compile(rf"\b{re.escape(rt)}(?=[A-Za-z0-9])", re.I),
            lambda m, su=su, sl=sl: su if m.group(0).isupper() else sl))
    # Bare whole-word ticker: uppercase-only for "app" (real word), ci otherwise.
    if rt in COMMON_WORD_TICKERS:
        ticker_rules.append((re.compile(rf"\b{re.escape(real_t.upper())}\b"), su))
    else:
        ticker_rules.append((re.compile(rf"\b{re.escape(rt)}\b", re.I), su))

# ── Generic regex passes ──────────────────────────────────────────────────────
EDGAR_URL_PAT = re.compile(r"https?://(?:[\w\-]+\.)?(?:sec\.gov/Archives|efts\.sec\.gov)[^\s\"'<>\]]*", re.I)
ACCESSION_PAT = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")

CORP_PAT = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9&\'\-\.]+\s+){0,5}[A-Z][A-Za-z0-9&\'\-\.]+\s+"
    r"(?:Inc\.?|Corp\.?|LLC|Ltd\.?|L\.L\.C\.?|L\.P\.?|LLP|LP|PLC|N\.V\.|S\.A\.|AG|GmbH|"
    r"Corporation|Incorporated|Company|Technologies|Technology|Holdings|Group|Enterprises|"
    r"Partners|Associates|Industries|Solutions|Systems|Services|Networks|Analytics|"
    r"Capital|Ventures|Investments|Management|Financial|Pharmaceuticals|Therapeutics|"
    r"Semiconductors|Communications|Energy|Media|Entertainment|Logistics|Distribution)\b"
)
PERSON_PAT = re.compile(r"\b(?:Mr\.|Ms\.|Mrs\.|Dr\.|Prof\.)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b")

PROTECTED_EXACT = frozenset([
    "SEC","FASB","PCAOB","NYSE","NASDAQ","Nasdaq","IRS","FTC","DOJ","GAAP","IFRS",
    "EU","NATO","DoD","Fed","FINRA","CFTC","OECD","IMF","WTO","WHO","UN","FBI","CIA",
    "NSA","FDA","EPA",
])
PROTECTED_SUBSTRINGS = [
    "Securities and Exchange Commission","Financial Accounting Standards Board",
    "Public Company Accounting Oversight Board","New York Stock Exchange","Nasdaq Stock Market",
    "Internal Revenue Service","Federal Reserve","Federal Trade Commission","Department of Justice",
    "Department of Defense","European Union","Generally Accepted Accounting Principles",
    "International Financial Reporting Standards","U.S. GAAP","United States Government",
    "U.S. Government","London Stock Exchange","Tokyo Stock Exchange","Chicago Mercantile Exchange",
    "Financial Industry Regulatory Authority",
]
def is_protected(name):
    s = name.strip()
    if s in PROTECTED_EXACT or s in SYNTHETIC_NAMES: return True
    return any(p.lower() in s.lower() for p in PROTECTED_SUBSTRINGS)

def replace_org(m):
    return m.group() if is_protected(m.group()) else "[COMPANY]"
def replace_person(m):
    matched = m.group()
    return matched if any(se in matched for se in SYNTHETIC_EXECS) else "[EXECUTIVE]"

# ── Date extraction / output naming ───────────────────────────────────────────
def extract_date(text, filename):
    m = re.search(r"\d{10}-\d{2}-\d{6}\s+(\d{4}-\d{2}-\d{2})", text[:3000])
    if m: return m.group(1).replace("-", "")
    m = re.search(r"period[_\s]of[_\s]report[^\d]*(\d{4}-\d{2}-\d{2})", text[:5000], re.I)
    if m: return m.group(1).replace("-", "")
    m = re.search(r"Date of Report[^\n]{0,120}?([A-Z][a-z]+ \d{1,2},?\s+\d{4})", text[:5000], re.I)
    if m:
        for fmt in ["%B %d, %Y", "%B %d,  %Y", "%B %d %Y"]:
            try: return datetime.strptime(m.group(1).strip().rstrip(","), fmt).strftime("%Y%m%d")
            except: pass
    m = re.search(r"(\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))", text[:3000])
    if m: return m.group(1).replace("-", "")
    fn = re.match(r"\d{10}-(\d{2})-", Path(filename).stem)
    if fn:
        yy = int(fn.group(1)); yr = yy + (2000 if yy < 50 else 1900)
        return f"{yr}0101"
    return "unknown"

def make_output_name(date_str, out_dir):
    base = f"{date_str}.txt"
    if not (out_dir / base).exists(): return base
    n = 2
    while (out_dir / f"{date_str}_{n:02d}.txt").exists(): n += 1
    return f"{date_str}_{n:02d}.txt"

# ── Process ───────────────────────────────────────────────────────────────────
if OUT_DIR.exists():
    shutil.rmtree(OUT_DIR)          # idempotent: no accumulating duplicates
OUT_DIR.mkdir(parents=True)

files = sorted(IN_DIR.glob("*.txt"))
counts = {"company": 0, "executive": 0, "accession": 0, "ticker_xbrl": 0}

for fpath in files:
    text = fpath.read_text(encoding="utf-8", errors="replace")

    text = EDGAR_URL_PAT.sub("[SEC-FILING-URL]", text)

    before = text.count("[FILING-ID]")
    text = ACCESSION_PAT.sub("[FILING-ID]", text)
    counts["accession"] += text.count("[FILING-ID]") - before

    # Entity names FIRST, then ticker XBRL forms. Order matters: the ticker
    # compound-prefix rule (\bmck(?=alnum)) would otherwise eat the company name
    # "Mckesson" -> "aphdesson" before the name rule "McKesson" -> synthetic runs.
    if RE_WB_CI: text = RE_WB_CI.sub(sub_wb_ci, text)
    if RE_CI:    text = RE_CI.sub(sub_ci, text)
    for pat, synth in camel_ci_rules:      # camelCase brands, token-start ci
        text = pat.sub(lambda m, s=synth: s, text)

    for pat, synth in phone_rules:
        text = pat.sub(lambda m, s=synth: s, text)

    for pat, repl in ticker_rules:
        text, n = pat.subn(repl, text)
        counts["ticker_xbrl"] += n

    if RE_WB_CS: text = RE_WB_CS.sub(sub_wb_cs, text)
    if RE_CS:    text = RE_CS.sub(sub_cs, text)
    for pat, repl in proper_camel_rules:   # XBRL camelCase compounds of dict-word proper nouns
        text = pat.sub(repl, text)

    before = text.count("[COMPANY]")
    text = CORP_PAT.sub(replace_org, text)
    counts["company"] += text.count("[COMPANY]") - before

    before = text.count("[EXECUTIVE]")
    text = PERSON_PAT.sub(replace_person, text)
    counts["executive"] += text.count("[EXECUTIVE]") - before

    date_str = extract_date(text, fpath.name)
    (OUT_DIR / make_output_name(date_str, OUT_DIR)).write_text(text, encoding="utf-8")

print(json.dumps({
    "ticker": TICKER,
    "files_processed": len(files),
    "company_replacements":   counts["company"],
    "executive_replacements": counts["executive"],
    "accession_replacements": counts["accession"],
    "ticker_xbrl_cleanups":   counts["ticker_xbrl"],
    "ci_patterns":  len(ci_map),
    "cs_patterns":  len(cs_map),
    "output_dir": str(OUT_DIR),
}))
