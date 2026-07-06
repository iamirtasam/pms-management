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
BADGE_RE             = re.compile(r'^[A-Za-z]+(-[A-Za-z0-9]+)?$')
EXEMPT_BADGES        = {'C-01', 'C-02', 'C-03', 'DC-02', 'AS-03'}
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

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
    result = {}
    for doc in db.collection('doctors').stream():
        d         = doc.to_dict()
        full_name = (d.get('name') or '').strip()
        if '|' in full_name:
            parts = full_name.split('|', 1)
            badge = parts[0].strip()
            name  = parts[1].strip()
        else:
            badge = full_name
            name  = full_name
        if badge:
            result[badge] = {'name': name, 'doc_id': doc.id}
    return result

def get_weekly_hours(db, friday, thursday):
    week_dates = {date_key(friday + timedelta(days=i)) for i in range(7)}
    totals = {}
    for doc in db.collection('attendance').stream():
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
        log("Firebase init failed: {}".format(e), 'ERROR'); sys.exit(1)

    try:
        doctors = get_doctors(db)
        log("Loaded {} doctors from Firebase".format(len(doctors)), 'SUCCESS')
    except Exception as e:
        log("Failed to load doctors: {}".format(e), 'ERROR'); sys.exit(1)

    try:
        weekly_hours = get_weekly_hours(db, friday, thursday)
        log("Loaded attendance for {} doctors this week".format(len(weekly_hours)), 'SUCCESS')
    except Exception as e:
        log("Failed to load attendance: {}".format(e), 'ERROR'); sys.exit(1)

    print()
    log("Connecting to Google Sheets...", 'INFO')
    try:
        gc          = init_sheets()
        spreadsheet = gc.open_by_key(SHEET_ID)
    except gspread.exceptions.APIError as e:
        log("Sheets API error: {}".format(e), 'ERROR')
        log("Make sure the service account has Editor access.", 'WARNING')
        sys.exit(1)
    except Exception as e:
        log("Failed to connect to Sheets: {}".format(e), 'ERROR')
        log("Check: 1) Google Sheets API enabled  2) Service account has Editor access", 'WARNING')
        sys.exit(1)

    tab_name   = fmt_tab_name(friday, thursday)
    all_sheets = spreadsheet.worksheets()

    if tab_name in [ws.title for ws in all_sheets]:
        log("Sheet '{}' already exists -- updating.".format(tab_name), 'WARNING')
        new_sheet = spreadsheet.worksheet(tab_name)
    else:
        source = all_sheets[0]
        log("Duplicating: '{}'".format(source.title), 'INFO')
        try:
            new_sheet = spreadsheet.duplicate_sheet(
                source_sheet_id=source.id, insert_sheet_index=0, new_sheet_name=tab_name)
            log("Created sheet: '{}'".format(tab_name), 'SUCCESS')
        except Exception as e:
            log("Failed to duplicate sheet: {}".format(e), 'ERROR'); sys.exit(1)

    print()
    log("Processing existing rows...", 'INFO')
    all_values     = new_sheet.get_all_values()
    updates        = []
    matched_badges = set()
    written_badges = set()  # tracks which badges already had data written (first row only)
    found = 0
    not_in_firebase = 0

    # Row 5: week header
    updates.append({'range': 'A5', 'values': [[fmt_header_text(friday, thursday)]]})

    for row_idx, row in enumerate(all_values):
        if row_idx < 6: continue
        cell_a     = row[0].strip() if row else ''
        gsheet_row = row_idx + 1
        if not cell_a or not BADGE_RE.match(cell_a): continue

        badge_code = cell_a
        matched_badges.add(badge_code)

        updates.append({'range': 'R{}'.format(gsheet_row), 'values': [[fmt_cell_date(friday)]]})
        updates.append({'range': 'S{}'.format(gsheet_row), 'values': [[fmt_cell_date(thursday)]]})

        if badge_code in EXEMPT_BADGES:
            log("  {:<10} -> exempt (skipped)".format(badge_code), 'INFO')
            continue

        # Duplicate row — clear it and skip writing data
        if badge_code in written_badges:
            updates.append({'range': 'B{}'.format(gsheet_row), 'values': [['']]})
            updates.append({'range': 'D{}'.format(gsheet_row), 'values': [['']]})
            continue

        written_badges.add(badge_code)
        doctor = doctors.get(badge_code)
        if doctor:
            total_mins = weekly_hours.get(doctor['doc_id'], 0)
            hours_str  = fmt_hours(total_mins)
            updates.append({'range': 'B{}'.format(gsheet_row), 'values': [[doctor['name']]]})
            updates.append({'range': 'D{}'.format(gsheet_row), 'values': [[hours_str]]})
            log("  {:<10} -> {:<20} {}".format(badge_code, doctor['name'], hours_str), 'SUCCESS')
            found += 1
        else:
            updates.append({'range': 'B{}'.format(gsheet_row), 'values': [['']]})
            updates.append({'range': 'D{}'.format(gsheet_row), 'values': [['0 hours']]})
            log("  {:<10} -> not in Firebase (cleared)".format(badge_code), 'WARNING')
            not_in_firebase += 1

    # Append Firebase doctors not found in the sheet
    unmatched = {b: i for b, i in doctors.items() if b not in matched_badges}
    appended  = 0
    if unmatched:
        print()
        log("{} Firebase doctors not in sheet -- appending...".format(len(unmatched)), 'INFO')
        next_row = len(all_values) + 1
        for badge, info in sorted(unmatched.items()):
            total_mins = weekly_hours.get(info['doc_id'], 0)
            hours_str  = fmt_hours(total_mins)
            updates.append({'range': 'A{}'.format(next_row), 'values': [[badge]]})
            updates.append({'range': 'B{}'.format(next_row), 'values': [[info['name']]]})
            updates.append({'range': 'D{}'.format(next_row), 'values': [[hours_str]]})
            updates.append({'range': 'R{}'.format(next_row), 'values': [[fmt_cell_date(friday)]]})
            updates.append({'range': 'S{}'.format(next_row), 'values': [[fmt_cell_date(thursday)]]})
            log("  {:<10} -> {:<20} {} [APPENDED]".format(badge, info['name'], hours_str), 'SUCCESS')
            next_row += 1
            appended += 1

    print()
    log("Writing {} cell updates in one batch...".format(len(updates)), 'INFO')
    try:
        new_sheet.batch_update(updates, value_input_option='USER_ENTERED')
    except Exception as e:
        log("Batch write failed: {}".format(e), 'ERROR'); sys.exit(1)

    print()
    log("Done!  {} updated  |  {} appended  |  {} not in Firebase".format(
        found, appended, not_in_firebase), 'SUCCESS')
    log("Open: https://docs.google.com/spreadsheets/d/{}".format(SHEET_ID), 'INFO')
    print()

if __name__ == '__main__':
    run(use_current='--current' in sys.argv)
