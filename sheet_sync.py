#!/usr/bin/env python3
"""
PMS Weekly Attendance -- Google Sheets Generator
Run from the selfbot/ directory:
    python sheet_sync.py              <- previous completed week (default)
    python sheet_sync.py --current    <- current ongoing week
"""

import sys
import os
import re
from datetime import datetime, timedelta, timezone

missing = []
try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    missing.append("gspread google-auth")
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    missing.append("firebase-admin")
if missing:
    print("Missing packages. Run:  pip install " + " ".join(missing))
    sys.exit(1)

SHEET_ID             = "1DxY_DtP2ExXVgKxO7yBHSTvd5KcqFAzk0MchkhbdGTA"
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "serviceAccountKey.json")

# The tab that every weekly sheet is copied from. Matched case-insensitively.
# Must be a real tab name, never a positional index: new tabs are inserted at
# index 0, so all_sheets[0] is last week's report, not the blank template.
TEMPLATE_TAB = "Template"

# Ranks/roles that are tracked manually and must never be auto-filled.
EXEMPT_BADGES = {'C-01', 'C-02', 'C-03', 'DC-02', 'AS-03'}

# Sheet layout (1-based rows, A1 columns).
HEADER_ROW    = 5    # "Weekly Report <from> - <to>"
COL_HEADER_ROW = 6   # "Call Sign | Name | InGame Name | Hours | ..."
FIRST_DATA_ROW = 7   # first row that can hold a badge
COL_BADGE      = 'A'
COL_NAME       = 'B'
COL_INGAME     = 'C'
COL_HOURS      = 'D'  # merged D:F
COL_START_DATE = 'P'  # merged P:Q
COL_END_DATE   = 'R'  # merged R:S

# A badge code is a letter-run optionally followed by -<alnum>, e.g. AEMT-26,
# PMS-C, PMS, 806. Section headers ("Advance EMT", "Chief Of EMS") contain a
# space and are excluded by \S+ anchoring, but single-word headers ("EMT",
# "Cadet", "Paramedic") would still match -- so they are listed explicitly
# below and skipped. Matching is case-insensitive throughout.
BADGE_RE = re.compile(r'^[A-Za-z]+(-[A-Za-z0-9]+)?$|^\d+$')

# Single-word section headers that BADGE_RE would otherwise treat as badges.
# Without this the generator overwrites the "EMT"/"Cadet" divider rows.
SECTION_HEADERS = {
    'emt', 'cadet', 'paramedic', 'supervisor', 'medic', 'trainee',
    'probationary', 'senior', 'junior', 'chief', 'rank', 'staff',
    'intern', 'volunteer', 'reserve', 'command', 'director',
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Deadline for every Firestore round-trip. Without it a stale gRPC channel
# hangs forever and permanently occupies one of the bot's blocking-pool
# workers, even though the caller has already given up.
FS_TIMEOUT = 30.0


class SheetSyncError(RuntimeError):
    """
    Raised instead of sys.exit() when run() fails.

    run() is called as a library function by the selfbot (pms!sheet). A
    sys.exit() there raises SystemExit, which is a BaseException and so slips
    straight past `except Exception` in the command handler — the user would
    never be told the sheet failed.
    """

class C:
    RESET='\033[0m'; BOLD='\033[1m'; GREEN='\033[92m'; RED='\033[91m'
    YELLOW='\033[93m'; CYAN='\033[96m'; GRAY='\033[90m'

def log(msg, level='INFO'):
    now = datetime.now().strftime('%H:%M:%S')
    icons = {'SUCCESS':(C.GREEN,'v'),'ERROR':(C.RED,'x'),'WARNING':(C.YELLOW,'!'),'INFO':(C.CYAN,'i')}
    color, icon = icons.get(level, (C.RESET,'-'))
    print("{g}[{t}]{r} {c}{i} {m}{r}".format(g=C.GRAY,t=now,r=C.RESET,c=color,i=icon,m=msg))
    try:
        import console_log
        console_log.push_log(now, level, msg)
    except Exception:
        pass

def get_week_range(use_current=False):
    now  = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5)
    day  = now.weekday()
    diff = (day - 4) % 7
    this_friday = (now - timedelta(days=diff)).replace(hour=0,minute=0,second=0,microsecond=0)
    friday   = this_friday if use_current else this_friday - timedelta(days=7)
    thursday = friday + timedelta(days=6)
    return friday, thursday

def date_key(dt):    return dt.strftime('%Y-%m-%d')
def fmt_cell_date(dt): return dt.strftime('%d/%m/%Y')
def fmt_tab_name(f,t):
    return "{} to {}".format(f.strftime('%d/ %B/%Y'), t.strftime('%d/ %B/%Y'))
def fmt_header_text(f,t):
    return "Weekly Report {} - {}".format(f.strftime('%d/%B/%Y'), t.strftime('%d/%B/%Y'))
def fmt_hours(total_minutes):
    h = total_minutes // 60
    m = total_minutes % 60
    if h == 0 and m == 0:
        return '0 hours'
    return '{} hours {} mins'.format(h, m)

def norm_badge(s):
    """Canonical badge key: case- and space-insensitive. 'Aemt-26' == 'AEMT-26'."""
    return (s or '').strip().upper()

EXEMPT_BADGES_N = {b.strip().upper() for b in EXEMPT_BADGES}

def is_badge(cell):
    """True if a column-A cell is a badge code rather than a section header."""
    s = (cell or '').strip()
    if not s or not BADGE_RE.match(s):
        return False
    return s.lower() not in SECTION_HEADERS

def _get_service_account_cred():
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        return SERVICE_ACCOUNT_FILE, None  # path-based
    sa_json = os.environ.get("SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise RuntimeError("No serviceAccountKey.json and no SERVICE_ACCOUNT_JSON env var found")
    import json as _json
    return None, _json.loads(sa_json)  # dict-based

def init_firebase():
    if not firebase_admin._apps:
        path, info = _get_service_account_cred()
        cred = credentials.Certificate(path if path else info)
        firebase_admin.initialize_app(cred)
    return firestore.client()

def get_doctors(db):
    """
    Active doctors grouped by normalised badge code.

    Returns {BADGE: [{'name','doc_id','full'}, ...]} -- a LIST per badge,
    because the template has several interchangeable slots for the same code
    (PMS-C x9, PMS x12) and more than one doctor legitimately shares it.
    Disabled doctors are excluded entirely.
    """
    result  = {}
    skipped = 0
    for doc in db.collection('doctors').stream(timeout=FS_TIMEOUT):
        d = doc.to_dict()
        if d.get('disabled'):
            skipped += 1
            continue
        full_name = (d.get('name') or '').strip()
        if '|' in full_name:
            badge, name = [p.strip() for p in full_name.split('|', 1)]
        else:
            badge = name = full_name
        if not badge:
            continue
        result.setdefault(norm_badge(badge), []).append(
            {'name': name, 'doc_id': doc.id, 'full': full_name}
        )
    # Stable ordering so slot assignment doesn't shuffle between runs.
    for v in result.values():
        v.sort(key=lambda x: x['name'].lower())
    log("Skipped {} disabled doctor(s)".format(skipped), 'INFO')
    return result

def get_weekly_hours(db, friday, thursday):
    week_dates = {date_key(friday + timedelta(days=i)) for i in range(7)}
    totals = {}
    for doc in db.collection('attendance').stream(timeout=FS_TIMEOUT):
        d = doc.to_dict()
        if d.get('dateKey','') not in week_dates: continue
        if d.get('status') not in ('present','late'): continue
        doctor_id = d.get('doctorId','')
        if not doctor_id: continue
        h = int(d.get('hours',0) or 0)
        m = int(d.get('minutes',0) or 0)
        totals[doctor_id] = totals.get(doctor_id,0) + h*60 + m
    return totals

def init_sheets():
    path, info = _get_service_account_cred()
    if path:
        creds = Credentials.from_service_account_file(path, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)

def find_template(spreadsheet):
    """The blank template tab, by name. Never positional."""
    for ws in spreadsheet.worksheets():
        if ws.title.strip().lower() == TEMPLATE_TAB.lower():
            return ws
    raise RuntimeError(
        "No tab named '{}' found. The generator copies that tab for every "
        "weekly report -- create it (or rename the blank one) and re-run."
        .format(TEMPLATE_TAB)
    )

def run(use_current=False):
    print("\n" + C.BOLD + C.CYAN + "=== PMS Weekly Attendance Sheet Generator ===" + C.RESET)
    print(C.GRAY + "    Mode: " + ("current week" if use_current else "previous completed week") + C.RESET + "\n")

    friday, thursday = get_week_range(use_current)
    log("Target week : {} -> {}".format(fmt_cell_date(friday), fmt_cell_date(thursday)), 'INFO')
    log("Tab name    : " + fmt_tab_name(friday, thursday), 'INFO')
    print()

    log("Connecting to Firebase...", 'INFO')
    try:
        db = init_firebase()
    except Exception as e:
        log("Firebase init failed: {}".format(e), 'ERROR'); raise SheetSyncError("Firebase init failed: {}".format(e))

    try:
        doctors = get_doctors(db)
        total_active = sum(len(v) for v in doctors.values())
        log("Loaded {} active doctors across {} badge codes".format(total_active, len(doctors)), 'SUCCESS')
    except Exception as e:
        log("Failed to load doctors: {}".format(e), 'ERROR'); raise SheetSyncError("Failed to load doctors: {}".format(e))

    try:
        weekly_hours = get_weekly_hours(db, friday, thursday)
        log("Loaded attendance for {} doctors this week".format(len(weekly_hours)), 'SUCCESS')
    except Exception as e:
        log("Failed to load attendance: {}".format(e), 'ERROR'); raise SheetSyncError("Failed to load attendance: {}".format(e))

    print()
    log("Connecting to Google Sheets...", 'INFO')
    try:
        gc          = init_sheets()
        spreadsheet = gc.open_by_key(SHEET_ID)
    except gspread.exceptions.APIError as e:
        log("Sheets API error: {}".format(e), 'ERROR')
        log("Make sure the service account has Editor access.", 'WARNING')
        raise SheetSyncError("Sheets API error: {}".format(e))
    except Exception as e:
        log("Failed to connect to Sheets: {}".format(e), 'ERROR')
        log("Check: 1) Google Sheets API enabled  2) Service account has Editor access", 'WARNING')
        raise SheetSyncError("Failed to connect to Sheets: {}".format(e))

    tab_name = fmt_tab_name(friday, thursday)

    try:
        template = find_template(spreadsheet)
    except RuntimeError as e:
        log(str(e), 'ERROR'); raise SheetSyncError(str(e))

    existing = {ws.title: ws for ws in spreadsheet.worksheets()}
    if tab_name in existing:
        # Rebuild from the template so a re-run never inherits stale rows.
        log("Sheet '{}' already exists -- deleting and rebuilding from template.".format(tab_name), 'WARNING')
        try:
            spreadsheet.del_worksheet(existing[tab_name])
        except Exception as e:
            log("Could not delete existing tab: {}".format(e), 'ERROR'); raise SheetSyncError("Could not delete existing tab: {}".format(e))

    log("Duplicating template: '{}'".format(template.title), 'INFO')
    try:
        new_sheet = spreadsheet.duplicate_sheet(
            source_sheet_id=template.id, insert_sheet_index=0, new_sheet_name=tab_name)
        log("Created sheet: '{}'".format(tab_name), 'SUCCESS')
    except Exception as e:
        log("Failed to duplicate template: {}".format(e), 'ERROR'); raise SheetSyncError("Failed to duplicate template: {}".format(e))

    print()
    log("Filling rows...", 'INFO')
    all_values = new_sheet.get_all_values()
    updates    = []
    start_str  = fmt_cell_date(friday)
    end_str    = fmt_cell_date(thursday)

    # Week header (row 5) and per-badge date columns.
    updates.append({'range': '{}{}'.format(COL_BADGE, HEADER_ROW),
                    'values': [[fmt_header_text(friday, thursday)]]})

    # Slot cursor per badge: consecutive rows with the same code take the
    # next unused doctor, so PMS-C x9 fills nine different cadets.
    cursor    = {}
    filled    = 0
    vacant    = 0
    exempt    = 0

    for row_idx, row in enumerate(all_values):
        gsheet_row = row_idx + 1
        if gsheet_row < FIRST_DATA_ROW:
            continue
        cell_a = row[0].strip() if row else ''
        if not is_badge(cell_a):
            continue

        # Every badge row gets the week's start and end dates.
        updates.append({'range': '{}{}'.format(COL_START_DATE, gsheet_row), 'values': [[start_str]]})
        updates.append({'range': '{}{}'.format(COL_END_DATE,   gsheet_row), 'values': [[end_str]]})

        badge = norm_badge(cell_a)
        if badge in EXEMPT_BADGES_N:
            # Managed by hand. Consume the slot so the doctor holding this
            # code isn't mistaken for unplaced and appended at the bottom.
            cursor[badge] = cursor.get(badge, 0) + 1
            exempt += 1
            continue

        pool = doctors.get(badge, [])
        i    = cursor.get(badge, 0)
        if i >= len(pool):
            # No (more) active doctors for this code -- leave the template's
            # placeholder untouched so a vacant slot stays visibly vacant.
            vacant += 1
            continue
        cursor[badge] = i + 1

        doctor     = pool[i]
        total_mins = weekly_hours.get(doctor['doc_id'], 0)
        hours_str  = fmt_hours(total_mins)
        updates.append({'range': '{}{}'.format(COL_NAME,  gsheet_row), 'values': [[doctor['name']]]})
        updates.append({'range': '{}{}'.format(COL_HOURS, gsheet_row), 'values': [[hours_str]]})
        log("  r{:<3} {:<10} -> {:<22} {}".format(gsheet_row, cell_a, doctor['name'], hours_str), 'SUCCESS')
        filled += 1

    # Any active doctor who never got a slot (badge absent from the template,
    # or more doctors than slots) is appended so nobody silently disappears.
    leftovers = []
    for badge, pool in sorted(doctors.items()):
        used = cursor.get(badge, 0)
        for doctor in pool[used:]:
            leftovers.append((badge, doctor))

    appended = 0
    if leftovers:
        print()
        log("{} active doctor(s) had no template slot -- appending...".format(len(leftovers)), 'WARNING')
        next_row = len(all_values) + 1
        for badge, doctor in leftovers:
            total_mins = weekly_hours.get(doctor['doc_id'], 0)
            hours_str  = fmt_hours(total_mins)
            updates.append({'range': '{}{}'.format(COL_BADGE,      next_row), 'values': [[badge]]})
            updates.append({'range': '{}{}'.format(COL_NAME,       next_row), 'values': [[doctor['name']]]})
            updates.append({'range': '{}{}'.format(COL_HOURS,      next_row), 'values': [[hours_str]]})
            updates.append({'range': '{}{}'.format(COL_START_DATE, next_row), 'values': [[start_str]]})
            updates.append({'range': '{}{}'.format(COL_END_DATE,   next_row), 'values': [[end_str]]})
            log("  {:<10} -> {:<22} {} [APPENDED]".format(badge, doctor['name'], hours_str), 'WARNING')
            next_row += 1
            appended += 1

    print()
    log("Writing {} cell updates in one batch...".format(len(updates)), 'INFO')
    try:
        new_sheet.batch_update(updates, value_input_option='USER_ENTERED')
    except Exception as e:
        log("Batch write failed: {}".format(e), 'ERROR'); raise SheetSyncError("Batch write failed: {}".format(e))

    print()
    log("Done!  {} filled  |  {} appended  |  {} vacant slots  |  {} exempt".format(
        filled, appended, vacant, exempt), 'SUCCESS')
    log("Open: https://docs.google.com/spreadsheets/d/{}".format(SHEET_ID), 'INFO')
    print()

if __name__ == '__main__':
    try:
        run(use_current='--current' in sys.argv)
    except SheetSyncError as e:
        log(str(e), 'ERROR')
        sys.exit(1)
