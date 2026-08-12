"""Merge both timetable Excel files into a single clean schedule.json"""
import openpyxl, re, json
from datetime import datetime

KNOWN_CODES = ['SAPM','TASS','AFSA','CSNEG','EBFM','TFEM','TEFM','FMA','FIS','SDM',
               'PRA','MOB','LSCM','FD','DT','RM','IB','SM','TA','PM','DM','MR','CB']

SESSION_TIMES = {
    'S1': '8:30 - 10:00', 'S2': '10:15 - 11:45', 'S3': '12:00 - 1:30',
    'S4': '2:30 - 4:00', 'S5': '4:15 - 5:45', 'S6': '6:00 - 7:30'
}

def clean_date(val):
    if isinstance(val, datetime):
        return val.date().isoformat()
    if val is None:
        return None
    s = str(val).strip()
    s = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', s)
    s = s.replace('July', 'Jul').replace('Sept', 'Sep')
    for fmt in ('%d %b %Y', '%d %B %Y'):
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
    code = None
    for c in KNOWN_CODES:
        if re.search(r'\b' + re.escape(c) + r'\b', text):
            code = c
            break
    batch = None
    m = re.search(r'Batch\s*-?\s*([IVX]+|\d+)', text, re.I)
    if m:
        b = m.group(1).upper()
        batch = {'1': 'I', '2': 'II', '3': 'III'}.get(b, b)
    prof = None
    m = re.search(r'Prof\.?\s*([A-Za-z\.\s]+?)(?:\(|$)', text)
    if m:
        prof = m.group(1).strip().rstrip(',')
    venue = None
    parens = re.findall(r'\(([^)]+)\)', text)
    for p in parens:
        if re.search(r'floor|audi|orion|classroom|itc|block|room', p, re.I):
            venue = p.strip()
            break

    # normalize alias codes to a canonical subject key
    alias = {'TA': 'RM', 'EBFM': 'RM', 'TEFM': 'TFEM', 'SDM': 'SDM'}
    canonical = alias.get(code, code) if code else None

    return {
        'raw': text,
        'code': code,
        'canonical': canonical,
        'batch': batch,
        'prof': prof,
        'venue': venue,
        'source': source
    }

def extract_rows(path, max_row):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = []
    for r in range(5, max_row + 1, 4):
        date_val = ws.cell(r, 1).value
        day_val = ws.cell(r, 2).value
        if date_val is None and day_val is None:
            continue
        d = clean_date(date_val)
        if not d:
            continue
        sessions = {}
        for sname, c in [('S1', 3), ('S2', 4), ('S3', 5), ('S4', 7), ('S5', 8), ('S6', 9)]:
            sessions[sname] = ws.cell(r, c).value
        rows.append({'date': d, 'day': day_val, 'sessions': sessions})
    return rows

rows1 = extract_rows('/mnt/user-data/uploads/PGDMBFS_2025-2027_TERM_IVSchedule.xlsx', 376)
rows2 = extract_rows('/mnt/user-data/uploads/Term_IV_Class_Schedule_2025-27.xlsx', 424)

schedule = {}

def ingest(rows, source):
    for row in rows:
        d = row['date']
        if d not in schedule:
            schedule[d] = {'day': row['day'], 'sessions': {s: [] for s in SESSION_TIMES}}
        for sname, celltext in row['sessions'].items():
            for entry_text in split_entries(celltext):
                parsed = parse_entry(entry_text, source)
                schedule[d]['sessions'][sname].append(parsed)

ingest(rows2, 'combined')  # base: full-term, has venues/batches
ingest(rows1, 'bfs')       # overlay: confirms which subjects are hers, adds SM etc not in file2

# Dedup per date/session: keep entries, but merge duplicates of same canonical+batch,
# preferring the one with more info (venue/prof present)
def score(e):
    return (1 if e['venue'] else 0) + (1 if e['prof'] else 0) + (1 if e['batch'] else 0)

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
            'events': sorted(set(events))
        }

with open('schedule.json', 'w') as f:
    json.dump(schedule, f, indent=2)

print("Total dates:", len(schedule))
# print a sample
import itertools
for d in sorted(schedule)[:3]:
    print(d, schedule[d])
