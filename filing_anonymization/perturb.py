"""
Financial perturbation — Stage 4: processed/<TICKER>/ -> final/<TICKER>/

Usage: python3 perturb.py <TICKER>          # or  python3 perturb.py ALL

Layer A — multiply every PROSE DOLLAR AMOUNT by the company's non-round `scale`.
  The `$` anchor isolates money from dates/CIK/EIN/ZIP/section numbers. A single
  factor keeps figures mutually consistent (statements still articulate) and preserves
  ratios — which is why Layer B exists.

Layer B — break the ratio/growth fingerprints Layer A leaves intact:
  B-1 counts  : numbers anchored by a financial keyword (shares, employees, stores,
                vehicles, subscribers, units, ...) scaled by `count_scale` (distinct
                from `scale`, so per-share/EPS ratios no longer recover).
  B-2 percent : every N% scaled by `pct_scale` (growth rates, market shares, margins);
                0% and 100% are left intact to avoid nonsense.

NOT yet handled (see perturbation_design.md):
  - XBRL bare-integer numeric blocks (decision pending: scale-above-threshold vs strip)
  - per-figure curated headline jitter (the keyword/percent mechanism covers most of it)
"""

import re, sys, yaml
from pathlib import Path

BASE = Path(__file__).parent
PROC = BASE / "data" / "processed"
FINAL = BASE / "data" / "final"

with open(BASE / "config" / "perturbation.yaml") as f:
    PCFG = yaml.safe_load(f)["companies"]

# Layer A: $[ (][number][ unit]  — prefix preserved, numeric string, optional magnitude.
DOLLAR_RE = re.compile(
    r"(\$\s?\(?\s?)(\d[\d,]*(?:\.\d+)?)(\s?(?:thousand|million|billion|trillion))?",
    re.IGNORECASE,
)

# Layer B-1: counts anchored by a financial keyword (so we never touch dates/IDs/sections).
COUNT_KEYWORDS = (
    r"shares|outstanding|issued|authorized|employees|associates|subscribers|members|"
    r"memberships|stores|supermarkets|vehicles|units|customers|restaurants|franchises|"
    r"dealers|locations|fuel\s+centers|distribution\s+centers|manufacturing\s+plants|"
    r"retail\s+locations"
)
COUNT_RE = re.compile(
    rf"(\d[\d,]*(?:\.\d+)?)(\s+(?:thousand|million|billion))?(\s+(?:{COUNT_KEYWORDS}))",
    re.IGNORECASE,
)

# Layer B-2: percentages (growth rates, margins, market share). 0% / 100% skipped.
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)(\s?%)")

def _fmt(newval, numstr):
    """Format a scaled number preserving the original decimal precision & grouping."""
    if "." in numstr:
        dec = len(numstr.split(".")[1])
        return f"{newval:,.{dec}f}"
    if "," in numstr or abs(newval) >= 1000:
        return f"{round(newval):,}"
    return str(round(newval))

def make_scaler(f, stats):                      # Layer A — $ amounts
    def repl(m):
        prefix, numstr, unit = m.group(1), m.group(2), m.group(3) or ""
        try:
            val = float(numstr.replace(",", ""))
        except ValueError:
            return m.group(0)
        out = _fmt(val * f, numstr)
        stats["dollars"] += 1
        if len(stats["samples"]) < 5:
            stats["samples"].append((m.group(0).strip(), (prefix + out + unit).strip()))
        return prefix + out + unit
    return repl

def make_count_scaler(f, stats):                # Layer B-1 — keyword-anchored counts
    def repl(m):
        numstr, mag, kw = m.group(1), m.group(2) or "", m.group(3)
        try:
            val = float(numstr.replace(",", ""))
        except ValueError:
            return m.group(0)
        stats["counts"] += 1
        return _fmt(val * f, numstr) + mag + kw
    return repl

def make_pct_scaler(f, stats):                  # Layer B-2 — percentages
    def repl(m):
        numstr, suffix = m.group(1), m.group(2)
        try:
            val = float(numstr)
        except ValueError:
            return m.group(0)
        if val == 0 or val == 100:              # leave 0% / 100% (ownership etc.) intact
            return m.group(0)
        dec = len(numstr.split(".")[1]) if "." in numstr else 0
        stats["pcts"] += 1
        return f"{val * f:.{dec}f}" + suffix
    return repl

def perturb_ticker(ticker):
    cfg = PCFG.get(ticker)
    if not cfg:
        print(f"  {ticker}: no perturbation config — skipping"); return
    f   = float(cfg["scale"])
    cf  = float(cfg.get("count_scale", f))
    pf  = float(cfg.get("pct_scale", 1.0))
    in_dir, out_dir = PROC / ticker, FINAL / ticker
    if out_dir.exists():
        import shutil; shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    files = sorted(in_dir.glob("*.txt"))
    stats = {"dollars": 0, "counts": 0, "pcts": 0, "samples": []}
    dollar_s, count_s, pct_s = make_scaler(f, stats), make_count_scaler(cf, stats), make_pct_scaler(pf, stats)
    for fp in files:
        text = fp.read_text(encoding="utf-8", errors="replace")
        text = DOLLAR_RE.sub(dollar_s, text)    # Layer A
        text = COUNT_RE.sub(count_s, text)      # Layer B-1
        text = PCT_RE.sub(pct_s, text)          # Layer B-2
        (out_dir / fp.name).write_text(text, encoding="utf-8")
    print(f"  {ticker}: scale={f} count={cf} pct={pf}  files={len(files)}  "
          f"$={stats['dollars']:,} counts={stats['counts']:,} pct={stats['pcts']:,}")
    for before, after in stats["samples"][:4]:
        print(f"      {before:>22}  ->  {after}")

if __name__ == "__main__":
    arg = (sys.argv[1] if len(sys.argv) > 1 else "ALL").upper()
    tickers = list(PCFG.keys()) if arg == "ALL" else [arg]
    print(f"Perturbation (Layer A: $ amounts | Layer B: counts + percentages)")
    for t in tickers:
        perturb_ticker(t)
