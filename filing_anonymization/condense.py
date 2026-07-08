#!/usr/bin/env python3
"""Stage 4: condense anonymized filings (data/final) into LLM-sized excerpts (data/condensed).

Why: commercial-API context/TPM limits (~500k tokens) can't hold raw filings —
the preprocessed text keeps the full EDGAR submission (XBRL fact dumps, linkbase
text, uuencoded blobs, raw HTML tables), so a "10-K" file measures 0.7M-11M
tokens while the actual narrative document is only ~110k. This script:
  1. finds the clean narrative window (SEC header -> junk onset),
  2. detects form type (earliest match wins, so an S-8 that *mentions* the 10-K
     isn't misclassified),
  3. for 10-K/10-Q, slices high-value sections via anchor CLUSTERS (page-header
     repeats within 5k chars collapse into one cluster; last cluster before the
     next landmark = the real section start, TOC/cross-refs lose),
  4. applies per-section char caps (marked with [...TRUNCATED]),
  5. names output chronologically: YYYY-MM-DD_<FORM>.txt using the period-end /
     report date parsed from the document header (source filenames are unreliable),
  6. drops non-core forms (S-8/S-3/S-4/SD/25/11-K/certs) — noise for
     sentiment/forecasting.

Reads only data/final (already anonymized + perturbed); only deletes text, so
all confidentiality properties carry over. Wipes its output dir first (idempotent).

Usage: python3 condense.py [--profile standard|compact] [--since 20190101]
"""
import argparse, collections, json, os, re, shutil, sys

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, 'data', 'final')
TICKERS = ['AMD', 'APP', 'GM', 'KR', 'MCK', 'NFLX', 'PLTR', 'SNDK']

# ---------------- profiles: per-section char caps ----------------
# chars/token for this text runs ~3.6-4.0; caps are chars.
PROFILES = {
    # target: one stock x 7 years <= ~400k tokens (fits a 500k-token call)
    'standard': {
        '10-K': {'cover': 8_000, 'business': 22_000, 'risk': 12_000,
                 'mdna': 48_000, 'financials': 26_000},
        '10-Q': {'cover': 3_000, 'statements': 6_500, 'mdna': 9_000},
        '8-K': {'earnings': 12_000, 'other': 4_500},
        'amendment': 8_000,
    },
    # targets: whole-universe 10-K-only subset (*_10-K.txt) <= ~400k tokens (500k
    # call); whole universe incl. 10-Q/8-K <= ~900k (1M-context models)
    'compact': {
        '10-K': {'cover': 2_500, 'business': 7_000, 'risk': 3_500,
                 'mdna': 13_000, 'financials': 8_000},
        '10-Q': {'cover': 1_000, 'statements': 2_500, 'mdna': 3_000},
        '8-K': {'earnings': 3_500, 'other': 1_200},
        'amendment': 2_500,
    },
}

SEC_HDR = re.compile(r'UNITED STATES\s+SECURITIES AND EXCHANGE COMMISSION', re.I)
# earliest match wins; more specific forms listed but position decides
FORM_PATS = [
    ('10-K/A', r'Form\s+10-K/A'), ('10-Q/A', r'Form\s+10-Q/A'), ('8-K/A', r'Form\s+8-K/A'),
    ('10-K', r'Form\s+10-K(?!/)'), ('10-Q', r'Form\s+10-Q(?!/)'), ('8-K', r'Form\s+8-K(?!/)'),
    ('S-8', r'Form\s+S-8'), ('S-3', r'Form\s+S-3'), ('S-4', r'Form\s+S-4'),
    ('S-1', r'Form\s+S-1'), ('11-K', r'Form\s+11-K'), ('25', r'Form\s+25\b'),
    ('SD', r'Form\s+SD\b'), ('DEF14A', r'Schedule\s+14A'),
    ('10-K', r'ANNUAL REPORT PURSUANT TO'), ('10-Q', r'QUARTERLY REPORT PURSUANT TO'),
    ('8-K', r'CURRENT REPORT\s+Pursuant'),
]
DROP_FORMS = {'S-8', 'S-3', 'S-4', 'S-1', '11-K', '25', 'SD', 'DEF14A', 'UNKNOWN'}

# junk-onset markers (end of useful text)
RE_UUENC = re.compile(r'begin 6\d\d \S')
RE_XBRLREF = re.compile(r'http://www\.xbrl\.org/2003')
RE_HTML = re.compile(r'(?:<(?:td|tr|span|div)[ >].*?){12}', re.S)

MONTHS = {m: i + 1 for i, m in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July',
     'August', 'September', 'October', 'November', 'December'])}
RE_DATE = r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s*,\s*(\d{4})'


def iso(m):
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"


def detect_form(window):
    best = ('UNKNOWN', len(window))
    for name, pat in FORM_PATS:
        m = re.search(pat, window, re.I)
        if m and m.start() < best[1]:
            best = (name, m.start())
    return best[0]


def junk_onset(text, start):
    """First position after start where non-narrative junk begins."""
    cands = [len(text)]
    for rx in (RE_UUENC, RE_XBRLREF):
        m = rx.search(text, start)
        if m:
            cands.append(m.start())
    m = RE_HTML.search(text, start)
    if m:
        cands.append(m.start())
    return min(cands)


def clusters(pattern, text, lo, hi, gap=5_000):
    """Match positions in [lo,hi), collapsing runs within `gap` chars to their first
    position (page-header repeats -> one cluster)."""
    pos = [m.start() for m in re.finditer(pattern, text) if lo <= m.start() < hi]
    out = []
    for p in pos:
        if not out or p - out[-1][-1] > gap:
            out.append([p])
        else:
            out[-1].append(p)
    return [c[0] for c in out]


def cap(seg, n):
    if len(seg) <= n:
        return seg
    return seg[:n].rsplit(' ', 1)[0] + '\n[...TRUNCATED]\n'


def parse_period(form, window, fallback):
    if form.startswith('10-K'):
        m = re.search(r'fiscal year ended[:\s]+' + RE_DATE, window, re.I)
        if m:
            return iso(m)
    if form.startswith('10-Q'):
        m = re.search(r'(?:quarterly|quarter|period) ended[:\s]+' + RE_DATE, window, re.I)
        if m:
            return iso(m)
    if form.startswith('8-K'):
        m = re.search(RE_DATE + r'\s*[^a-zA-Z]{0,10}Date of Report', window)
        if m:
            return iso(m)
        m = re.search(r'Date of Report[^:]{0,60}[:)\s]\s*' + RE_DATE, window)
        if m:
            return iso(m)
    return fallback  # filename date YYYY-MM-DD


ANCH = {  # all (?i): filers vary between Title Case and ALL CAPS headers
    'risk': r'(?i)Item\s*1A\s*[.:—-]?\s*Risk\s*Factors',
    'i5': r'(?i)Item\s*5\s*[.:—-]?\s*Market',
    'mdna': r'(?i)Management[’\']s\s+Discussion\s+and\s+Analysis',
    'i7a': r'(?i)Quantitative\s+and\s+Qualitative\s+Disclosures',
    'i8': r'(?i)(Item\s*8\s*[.:—-]?\s*Financial\s*Statements'
          r'|Report of Independent Registered Public Accounting Firm)',
    'i9a': r'(?i)Item\s*9A\s*[.:—-]?\s*Controls',
    'stmts': r'(?i)(condensed\s+)?(consolidated|combined)\s+(balance\s+sheets?|statements?\s+of\s+\w+(\s+\w+)?)',
}


def body_anchor(key, text, lo, hi, before=None):
    """Last anchor cluster in [lo, min(hi,before)) -- TOC & cross-refs lose to the
    real section header, which is the final mention before the next landmark."""
    hi = min(hi, before) if before else hi
    cs = clusters(ANCH[key], text, lo, hi)
    return cs[-1] if cs else None


RE_RISKLANG = re.compile(r'(?i)adversely affect|could harm|may not be able|no assurance'
                         r'|material adverse|could result in')


def risk_anchor(text, lo, hi):
    """Risk Factors needs content scoring: cross-references ("see Item 1A...") and
    page-header repeats are indistinguishable from the real header by pattern alone,
    so pick the candidate whose following text is saturated with risk language."""
    cs = clusters(ANCH['risk'], text, lo, hi)
    if not cs:
        return None
    return max(cs, key=lambda p: len(RE_RISKLANG.findall(text[p:p + 12_000])))


def stmts_anchor(text, lo, hi):
    """The primary financial statements block. Statement-title mentions also occur in
    MD&A/notes cross-refs, audit opinions, and index lists (where page numbers/dates
    fake digit density); filer layouts vary (NFLX puts statements after Part IV, AMD
    puts audit reports AFTER the statements). The one signature unique to the real
    block: >=3 statement titles clustered within ~20k chars, EACH followed by a
    number-dense table (income stmt, balance sheet, cash flows are consecutive)."""
    # prose mentions continue the sentence ("...Balance Sheets, inclusive of...",
    # "...Statements of Operations. As of..."); real titles are followed by column
    # headers (dates, "Years Ended", "(In millions"). A prev-char test can't work:
    # synthetic company names ending lowercase ("... Group") precede real titles.
    RE_PROSE_CONT = re.compile(r'\s*[,.;:]|\s+(and|or|for|in|as|is|are|was|were'
                               r'|includ\w*|reflect\w*|until|from)\b', re.I)
    ms = [m.start() for m in re.finditer(ANCH['stmts'], text)
          if lo <= m.start() < hi and not RE_PROSE_CONT.match(text[m.end():m.end() + 12])]

    def density(p, w=3_000):
        seg = text[p:p + w]
        return sum(c.isdigit() for c in seg) / max(len(seg), 1)

    dense = [p for p in ms if density(p) >= 0.08]
    for i, p in enumerate(dense):  # first group of >=3 dense titles within 12k chars
        if i + 2 < len(dense) and dense[i + 2] - p <= 12_000:  # runs of dense NOTE
            return p                                           # tables sit further apart
    # fallbacks: first isolated dense title (gap to next title >= a table's worth),
    # else the densest mention anywhere in range
    for i, p in enumerate(ms):
        nxt = ms[i + 1] if i + 1 < len(ms) else hi
        if nxt - p >= 1_500 and density(p, 4_000) >= 0.06:
            return p
    return max(ms, key=density) if ms else None


def condense_10k(text, nstart, nend, caps):
    toc_end = nstart + 8_000  # cover + TOC region; anchors before this are TOC entries
    i9a = body_anchor('i9a', text, toc_end, nend) or nend
    i8 = body_anchor('i8', text, toc_end, nend, before=i9a)
    i7a = body_anchor('i7a', text, toc_end, i8 or nend)
    mdna = body_anchor('mdna', text, toc_end, i7a or i8 or nend)
    risk = risk_anchor(text, toc_end, mdna or i8 or nend)
    i5 = body_anchor('i5', text, toc_end, mdna or i8 or nend)

    found = {'mdna': mdna, 'i8': i8, 'risk': risk}
    parts = []
    # cover + start of business (business follows the cover/TOC directly)
    biz_end = min(x for x in [risk, mdna, i8, nend] if x)
    parts.append(('COVER & BUSINESS',
                  cap(text[nstart:min(biz_end, nstart + caps['cover'] + caps['business'])],
                      caps['cover'] + caps['business'])))
    if risk:
        rend = min(x for x in [i5, mdna, i8, nend] if x and x > risk)
        parts.append(('RISK FACTORS', cap(text[risk:rend], caps['risk'])))
    if mdna:
        mend = min(x for x in [i7a, i8, nend] if x and x > mdna)
        parts.append(("MANAGEMENT'S DISCUSSION & ANALYSIS", cap(text[mdna:mend], caps['mdna'])))
    # search the whole body: some filers put audit reports AFTER the statements, so
    # anchoring the search at i8 can overshoot; the title-group signature is unique
    stm = stmts_anchor(text, toc_end, nend)
    found['stmts'] = stm
    if stm:
        parts.append(('FINANCIAL STATEMENTS', cap(text[stm:nend], caps['financials'])))
    elif i8:  # fallback: from Item 8 header
        parts.append(('FINANCIAL STATEMENTS', cap(text[i8:i9a], caps['financials'])))
    return parts, found


def condense_10q(text, nstart, nend, caps):
    toc_end = nstart + 1_500  # 10-Q covers are short; statements can start ~3k in
    mdna = body_anchor('mdna', text, toc_end, nend)
    st_cs = clusters(ANCH['stmts'], text, toc_end, mdna or nend)
    stmts = st_cs[0] if st_cs else None
    found = {'mdna': mdna, 'stmts': stmts}
    parts = [('COVER', cap(text[nstart:min(stmts or mdna or nend, nstart + caps['cover'])],
                           caps['cover']))]
    if stmts:
        parts.append(('CONDENSED FINANCIAL STATEMENTS',
                      cap(text[stmts:mdna or nend], caps['statements'])))
    if mdna:
        parts.append(("MANAGEMENT'S DISCUSSION & ANALYSIS",
                      cap(text[mdna:nend], caps['mdna'])))
    return parts, found


def condense_8k(text, nstart, nend, caps):
    seg = text[nstart:nend]
    earnings = bool(re.search(r'Item\s*(2\.02|7\.01)', seg[:6_000]))
    n = caps['earnings'] if earnings else caps['other']
    return [('8-K' + (' (EARNINGS/REG-FD)' if earnings else ''), cap(seg, n))], {}


def process_file(path, caps_all):
    text = open(path, errors='ignore').read()
    m = SEC_HDR.search(text)
    nstart = m.start() if m else 0
    window = text[nstart:nstart + 5_000]
    form = detect_form(window)
    if form in DROP_FORMS:
        return form, None, None, None
    nend = junk_onset(text, nstart)

    base = os.path.basename(path)
    fallback = f"{base[:4]}-{base[4:6]}-{base[6:8]}"
    period = parse_period(form, window, fallback)

    if form in ('10-K/A', '10-Q/A', '8-K/A'):
        parts, found = [(form, cap(text[nstart:nend], caps_all['amendment']))], {}
    elif form == '10-K':
        parts, found = condense_10k(text, nstart, nend, caps_all['10-K'])
    elif form == '10-Q':
        parts, found = condense_10q(text, nstart, nend, caps_all['10-Q'])
    else:
        parts, found = condense_8k(text, nstart, nend, caps_all['8-K'])

    secs = ', '.join(name for name, _ in parts)
    banner = f"[EXCERPTED FILING | FORM {form} | PERIOD/REPORT DATE {period} | SECTIONS: {secs}]"
    body = '\n\n'.join(f"===== {name} =====\n{seg.strip()}" for name, seg in parts)
    return form, period, banner + '\n\n' + body, found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', default='standard', choices=PROFILES)
    ap.add_argument('--since', default='20190101')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    caps_all = PROFILES[args.profile]
    out_root = args.out or os.path.join(BASE, 'data', 'condensed'
                                        + ('' if args.profile == 'standard' else '_' + args.profile))
    if os.path.isdir(out_root):
        shutil.rmtree(out_root)

    report = {'dropped': collections.Counter(), 'missing_anchors': [], 'files': []}
    for tk in TICKERS:
        os.makedirs(os.path.join(out_root, tk), exist_ok=True)
        names = collections.Counter()
        for base in sorted(os.listdir(os.path.join(SRC, tk))):
            # cheap pre-filter: filenames derive from XBRL period dates, which can
            # lag the true period by up to ~15 months -- scan a wide margin, then
            # scope precisely on the PARSED period date below
            if base[:8] < str(int(args.since[:4]) - 2) + args.since[4:]:
                continue
            form, period, out, found = process_file(os.path.join(SRC, tk, base), caps_all)
            if out is None:
                report['dropped'][form] += 1
                continue
            if period.replace('-', '') < args.since:
                continue
            if found:
                miss = [k for k, v in found.items() if v is None]
                if miss:
                    report['missing_anchors'].append((f"{tk}/{base}", form, miss))
            name = f"{period}_{form.replace('/', '')}"
            names[name] += 1
            if names[name] > 1:
                name += f"_{names[name]}"
            dest = os.path.join(out_root, tk, name + '.txt')
            with open(dest, 'w') as fh:
                fh.write(out)
            report['files'].append((tk, name, form, len(out)))

    # ---- summary ----
    per_tk = collections.defaultdict(lambda: collections.defaultdict(int))
    for tk, name, form, n in report['files']:
        per_tk[tk][form] += 1
        per_tk[tk]['_chars'] += n
    print(f"profile={args.profile}  out={out_root}")
    print(f"dropped (non-core forms): {dict(report['dropped'])}")
    print(f"{'ticker':<7}{'files':>6}{'chars':>13}{'~tokens(/3.8)':>15}")
    tot_c = 0
    for tk in TICKERS:
        d = per_tk[tk]
        nf = sum(v for k, v in d.items() if k != '_chars')
        tot_c += d['_chars']
        print(f"{tk:<7}{nf:>6}{d['_chars']:>13,}{d['_chars'] // 4:>15,}"
              f"   {{{', '.join(f'{k}:{v}' for k, v in sorted(d.items()) if k != '_chars')}}}")
    print(f"TOTAL chars={tot_c:,}  ~tokens={tot_c // 4:,}")
    if report['missing_anchors']:
        print(f"\nfiles with missing section anchors ({len(report['missing_anchors'])}):")
        for f, form, miss in report['missing_anchors'][:25]:
            print(f"  {f} ({form}): missing {miss}")
    json.dump(report['missing_anchors'],
              open(os.path.join(out_root, '_missing_anchors.json'), 'w'), indent=1)


if __name__ == '__main__':
    sys.exit(main())
