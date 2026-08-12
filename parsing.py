"""Shared parsing logic: turns raw timetable rows (from either Excel or a
live Google Sheet CSV export) into the clean schedule.json structure."""

import re
from datetime import datetime

KNOWN_CODES = ['SAPM', 'TASS', 'AFSA', 'CSNEG', 'EBFM', 'TFEM', 'TEFM', 'CRA', 'FMA', 'FIS', 'SDM',
               'PRA', 'MOB', 'LSCM', 'FD-II', 'FD', 'DT', 'RM', 'IB', 'SM', 'TA', 'PM', 'DM', 'MR', 'CB']

# Some subjects appear as a full written-out phrase in the sheet instead of a
# short code (e.g. "Financial Modelling(Post Mid Term)" rather than an
# abbreviation). These need to be matched by phrase, case-insensitively,
# and mapped to a short canonical code for consistency with everything else.
PHRASE_CODES = {
    'Financial Modelling': 'FM',
}

SESSION_TIMES = {
    'S1': '8:30 - 10:00', 'S2': '10:15 - 11:45', 'S3': '12:00 - 1:30',
    'S4': '2:30 - 4:00', 'S5': '4:15 - 5:45', 'S6': '6:00 - 7:30'
}

ALIAS = {'TEFM': 'TFEM'}
# NOTE: TA and EBFM (Prof P Lal) were previously aliased to RM, assuming they
# were the same course under a post-mid-term name. Confirmed wrong — RM is
# specifically Prof Jayatu Sen's course. TA/EBFM are a different, unrelated
# subject and are intentionally left un-aliased (and not in SUBJECTS below),
# so they won't show up in anyone's schedule.


def clean_date(val):
    """Accepts a datetime, or a string like '22nd Jun 2026' / '22-Jun-26'."""
    if isinstance(val, datetime):
        return val.date().isoformat()
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    s = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', s)
    s = s.replace('July', 'Jul').replace('Sept', 'Sep')
    for fmt in ('%d %b %Y', '%d %B %Y', '%d-%b-%y', '%d-%b-%Y', '%Y-%m-%d', '%m/%d/%Y'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def split_entries(text):
    if not text:
        return []
    text = str(text).replace('\n', ' ')
    parts = re.split(r',(?![^(]*\))', text)
    return [p.strip() for p in parts if p.strip()]


def parse_entry(text, source):
    # Find the code that appears EARLIEST in the actual text — not the first
    # one in KNOWN_CODES order. (Bug found: a cell like "SM(...) RM Quiz-2..."
    # was mislabeled as RM just because RM was earlier in the list, when SM
    # is clearly the actual class and RM is just a quiz mentioned in passing.)
    code = None
    earliest_pos = None
    for c in KNOWN_CODES:
        m = re.search(r'\b' + re.escape(c) + r'\b', text)
        if m and (earliest_pos is None or m.start() < earliest_pos):
            code = c
            earliest_pos = m.start()
    for phrase, short_code in PHRASE_CODES.items():
        m = re.search(re.escape(phrase), text, re.I)
        if m and (earliest_pos is None or m.start() < earliest_pos):
            code = short_code
            earliest_pos = m.start()
    batch = None
    m = re.search(r'Batch\s*-?\s*([IVX]+|\d+)', text, re.I)
    if m:
        b = m.group(1).upper()
        batch = {'1': 'I', '2': 'II', '3': 'III'}.get(b, b)
    prof = None
    m = re.search(r'Prof\.?\s*([A-Za-z.\s]+?)(?:\(|$)', text)
    if m:
        prof = m.group(1).strip().rstrip(',')
    venue = None
    parens = re.findall(r'\(([^)]+)\)', text)
    for p in parens:
        if re.search(r'floor|audi|orion|classroom|itc|block|room|lab', p, re.I):
            venue = p.strip()
            break
    if not venue:
        m = re.search(r'Venue:\s*([A-Za-z0-9\-\s.]+)', text, re.I)
        if m:
            venue = m.group(1).strip()

    canonical = ALIAS.get(code, code) if code else None

    return {
        'raw': text,
        'code': code,
        'canonical': canonical,
        'batch': batch,
        'prof': prof,
        'venue': venue,
        'source': source,
    }


def score(e):
    return (1 if e['venue'] else 0) + (1 if e['prof'] else 0) + (1 if e['batch'] else 0)


def build_schedule(row_sources):
    """row_sources: list of (rows, source_label) where rows is a list of
    {'date': iso date str, 'day': str, 'sessions': {sname: raw_cell_text}}"""
    schedule = {}

    for rows, source in row_sources:
        for row in rows:
            d = row['date']
            if not d:
                continue
            if d not in schedule:
                schedule[d] = {'day': row['day'], 'sessions': {s: [] for s in SESSION_TIMES}}
            for sname, celltext in row['sessions'].items():
                for entry_text in split_entries(celltext):
                    parsed = parse_entry(entry_text, source)
                    schedule[d]['sessions'][sname].append(parsed)

    for d, block in schedule.items():
        for sname, entries in block['sessions'].items():
            best = {}
            events = []
            for e in entries:
                if e['code'] is None:
                    events.append(e['raw'])
                    continue
                key = (e['canonical'], e['batch'])
                if key not in best or score(e) > score(best[key]):
                    best[key] = e
            block['sessions'][sname] = {
                'time': SESSION_TIMES[sname],
                'classes': list(best.values()),
                'events': sorted(set(events)),
            }

    return schedule
