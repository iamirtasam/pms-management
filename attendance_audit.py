"""
Pure attendance-validation logic for the PMS selfbot.

Given the raw text of an attendance message (and the Discord author id), decide
whether it is faulty and, if so, which single reason to report. Kept free of any
Discord objects so it can be unit-tested in isolation. The selfbot handles the
channel scanning, scheduling, and DM reporting.

Expected attendance format (spacing is lenient — alignment spaces are fine):

    Name             :  <@579933818862043136>
    On Duty          : 4:19 PM /9:30 PM
    Off Duty         :  6:00 PM/12:00 AM
    Total Hours      : 4 hours 11 minutes
    Breaks           :
    Date             : 06/28/2026
"""
import re

# ── Report reason strings (exact wording requested) ────────────────
REASON_TOTAL_WRONG   = "Your **Total Hours** are **Wrong**"
REASON_TOTAL_MISSING = "Your **Total Hours** are **Missing**"
REASON_FORMAT        = "Your Attendance **Format** is **Wrong**. Please fix **whatever is wrong in format (spaces or anything else)**"
REASON_DATE          = "Your **Date Format** is **Wrong**. Please Write date as **Month/Date/Year**"
REASON_DATE_MISMATCH = "Your **Date** is **Wrong**. Please write the **correct date** as **Month/Date/Year**"
REASON_TAG           = "You **Tagged** the **Wrong Person** in your Attendance"
REASON_OFFDUTY       = "Please **Close** your Attendance, Your **Off Duty** time is missing"
REASON_AMPM          = "Your **AM/PM** is missing"

# Canonical fields in their expected order. Each label tolerates singular/plural
# and flexible internal spacing.
_LABELS = [
    ('name',        r'Name'),
    ('on_duty',     r'On\s*Duty'),
    ('off_duty',    r'Off\s*Duty'),
    ('total_hours', r'Total\s*Hours?'),
    ('breaks',      r'Breaks?'),
    ('date',        r'Date'),
]
_ALL_KEYS = {k for k, _ in _LABELS}


def extract_fields(content):
    """Return ({key: value}, {key: start_index}) for every labelled field found."""
    fields, positions = {}, {}
    for key, pat in _LABELS:
        m = re.search(r'^[ \t]*' + pat + r'[ \t]*:[ \t]*(.*)$', content,
                      re.IGNORECASE | re.MULTILINE)
        if m:
            fields[key]    = m.group(1).strip()
            positions[key] = m.start()
    return fields, positions


def looks_like_attendance(content):
    """Cheap gate so we don't validate random chatter in the channel."""
    if re.search(r'<@!?\d+>', content or ''):
        return True
    low = (content or '').lower()
    return ('on duty' in low) or ('total hour' in low)


def _positions_in_order(positions):
    order = [k for k, _ in _LABELS if k in positions]
    pos   = [positions[k] for k in order]
    return pos == sorted(pos)


def _parse_clock(s):
    """'4:19 PM' -> minutes since midnight, or None."""
    m = re.search(r'(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]?\.?', s)
    if not m:
        return None
    h    = int(m.group(1)) % 12
    mins = int(m.group(2))
    if m.group(3).lower() == 'p':
        h += 12
    return h * 60 + mins


def _sessions_total(on_str, off_str):
    """Sum paired On/Off sessions in minutes (handles midnight rollover)."""
    ons  = [x for x in on_str.split('/')  if x.strip()]
    offs = [x for x in off_str.split('/') if x.strip()]
    if not ons or len(ons) != len(offs):
        return None
    total = 0
    for o, f in zip(ons, offs):
        a, b = _parse_clock(o), _parse_clock(f)
        if a is None or b is None:
            return None
        if b <= a:          # crossed midnight (e.g. 9:30 PM -> 12:00 AM)
            b += 1440
        total += b - a
    return total


def _ampm_missing(time_field):
    """True if any clock time in the field lacks an AM/PM marker (e.g. '4:19')."""
    for seg in time_field.split('/'):
        seg = seg.strip()
        if re.search(r'\d{1,2}:\d{2}', seg) and not re.search(r'\d{1,2}:\d{2}\s*[AaPp]\.?\s*[Mm]?\.?', seg):
            return True
    return False


def _parse_total_minutes(s):
    """'4 hours 11 minutes' -> 251. Returns None if no number present."""
    if not s.strip():
        return None
    hh = re.search(r'(\d+)\s*h', s, re.IGNORECASE)
    mm = re.search(r'(\d+)\s*m', s, re.IGNORECASE)
    if not hh and not mm:
        n = re.search(r'(\d+)', s)
        return int(n.group(1)) * 60 if n else None
    return (int(hh.group(1)) if hh else 0) * 60 + (int(mm.group(1)) if mm else 0)


def classify_date(date_val):
    """Return ('ok', (mm,dd,yyyy)) | ('invalid', None) | ('missing', None)."""
    s = (date_val or '').strip()
    if not s:
        return ('missing', None)
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', s)
    if not m:
        return ('invalid', None)
    mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mm <= 12) or not (1 <= dd <= 31):
        return ('invalid', None)   # e.g. 28/06/2026 (Day/Month order)
    return ('ok', (mm, dd, yy))


def message_date_status(content):
    """classify_date() for the Date field of a whole message."""
    fields, _ = extract_fields(content)
    return classify_date(fields.get('date', ''))




def validate_attendance(content, author_id, posted_date=None):
    """
    Return a single reason string if the attendance is faulty, else None.
    Checks run in priority order so the most actionable issue is reported first.

    posted_date: optional (month, day, year) tuple of the day the message was
    posted (Pakistan time). When given, a valid-format date that doesn't equal
    the posting day is flagged as a wrong date (REASON_DATE_MISMATCH).
    """
    fields, positions = extract_fields(content)

    name_val = fields.get('name', '')
    mt = re.search(r'<@!?(\d+)>', name_val) or re.search(r'<@!?(\d+)>', content)
    tagged = mt.group(1) if mt else None

    # 1. Wrong tag — author isn't the person they tagged.
    if tagged is not None and str(author_id) != str(tagged):
        return REASON_TAG

    # 2. Structural format — missing tag, missing field, or fields out of order.
    if tagged is None or set(positions.keys()) != _ALL_KEYS or not _positions_in_order(positions):
        return REASON_FORMAT

    # 3. Off Duty missing (didn't close attendance).
    if not fields.get('off_duty', '').strip():
        return REASON_OFFDUTY

    # 4. Total Hours field empty.
    total_field = fields.get('total_hours', '').strip()
    if not total_field:
        return REASON_TOTAL_MISSING

    # 5. On/Off Duty times missing an AM/PM marker.
    if _ampm_missing(fields.get('on_duty', '')) or _ampm_missing(fields.get('off_duty', '')):
        return REASON_AMPM

    # 6. Date — must be valid Month/Day/Year AND match the day it was posted.
    status, tup = classify_date(fields.get('date', ''))
    if status != 'ok':
        return REASON_DATE                       # malformed / missing format
    if posted_date is not None and tup != tuple(posted_date):
        return REASON_DATE_MISMATCH              # valid format but wrong day

    # 6. Total Hours don't match the On/Off times (±1 min tolerance).
    calc   = _sessions_total(fields.get('on_duty', ''), fields.get('off_duty', ''))
    stated = _parse_total_minutes(total_field)
    if calc is not None and stated is not None and abs(calc - stated) > 1:
        return REASON_TOTAL_WRONG

    return None
