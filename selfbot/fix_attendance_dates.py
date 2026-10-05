"""
One-time cleanup: shift bot-created attendance records forward by 1 day.

Background: the selfbot's parse_date() subtracted 1 day from every written
date since the initial deployment (May 2026), so every attendance record the
bot synced is stored one day EARLIER than the real Pakistani date. That shift
has now been removed from selfbot.py, and the portal's dateKey() display bug
(the reason the shift looked necessary) is fixed in firebase-config.js.
This script repairs the already-stored records.

How records are classified:
  - MANUAL (skipped): records whose (doctorId, dateKey) matches an
    ATTENDANCE_MARK entry in the `logs` collection — those were entered
    through the admin panel with the correct date.
  - BOT (shifted +1 day): everything else with status 'present'
    (the bot only ever writes status 'present').
  - Records with other statuses (absent/late/off) are never shifted — the
    bot never wrote those.

Safety:
  - DRY RUN by default: prints exactly what it would change and writes a
    full JSON backup of the attendance collection. Nothing is modified.
  - Run with --apply to actually perform the changes.
  - Records are processed newest-date-first per doctor so consecutive-day
    records never collide with each other while shifting.
  - If the target date already holds a manual record, the bot record is
    NOT moved; it is listed at the end for you to resolve by hand.

Usage:
    python fix_attendance_dates.py            # dry run + backup
    python fix_attendance_dates.py --apply    # do it for real
"""
import json
import sys
from datetime import datetime, timedelta

from firebase_sync import get_firestore_db


def shift_date(date_key, days=1):
    dt = datetime.strptime(date_key, "%Y-%m-%d")
    return (dt + timedelta(days=days)).strftime("%Y-%m-%d")


def main():
    apply_changes = "--apply" in sys.argv

    db = get_firestore_db()
    if not db:
        print("Could not connect to Firebase — aborting.")
        sys.exit(1)

    # ── Load everything up front ─────────────────────────────────────
    print("Loading attendance records...")
    records = []
    for doc in db.collection("attendance").stream():
        d = doc.to_dict()
        d["_doc_id"] = doc.id
        records.append(d)
    print(f"  {len(records)} attendance records loaded")

    print("Loading manual-mark logs (ATTENDANCE_MARK)...")
    manual_keys = set()
    for doc in db.collection("logs").where("action", "==", "ATTENDANCE_MARK").stream():
        d = doc.to_dict()
        target = d.get("targetId", "")
        date = d.get("date", "")
        if target and date:
            manual_keys.add((target, date))
    print(f"  {len(manual_keys)} manual attendance marks found in logs")

    # ── Backup before touching anything ──────────────────────────────
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = f"attendance_backup_{stamp}.json"
    with open(backup_file, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)
    print(f"Backup written: {backup_file}\n")

    # ── Classify ─────────────────────────────────────────────────────
    to_shift, skipped_manual, skipped_status, bad = [], [], [], []

    for r in records:
        doctor_id = r.get("doctorId", "")
        date_key = r.get("dateKey", "")
        if not doctor_id or not date_key:
            bad.append(r)
            continue
        if (doctor_id, date_key) in manual_keys:
            skipped_manual.append(r)
            continue
        if r.get("status") != "present":
            skipped_status.append(r)
            continue
        to_shift.append(r)

    # Newest-first per doctor so a record never lands on the slot of an
    # unprocessed older sibling (July 4 -> July 5 runs before July 3 -> July 4).
    to_shift.sort(key=lambda r: (r["doctorId"], r["dateKey"]), reverse=True)

    # Manual records stay where they are — a shift may not overwrite them.
    manual_positions = {
        (r["doctorId"], r["dateKey"])
        for r in skipped_manual + skipped_status
    }

    # ── Plan / execute ───────────────────────────────────────────────
    # After the newest-first sort, a shifting record can only collide with a
    # manual / non-'present' record: every other 'present' record is itself
    # in to_shift and has already vacated its slot by the time we reach it.
    moved, collisions = 0, []

    print(f"{'APPLYING' if apply_changes else 'DRY RUN — would apply'} "
          f"{len(to_shift)} shift(s):\n")

    for r in to_shift:
        doctor_id, old_date = r["doctorId"], r["dateKey"]
        new_date = shift_date(old_date, +1)

        if (doctor_id, new_date) in manual_positions:
            collisions.append((r, new_date))
            print(f"  CONFLICT  {doctor_id}  {old_date} -> {new_date}  "
                  f"(a manual record already exists on the target date — left untouched)")
            continue

        print(f"  shift     {doctor_id}  {old_date} -> {new_date}  "
              f"({r.get('hours', 0)}h {r.get('minutes', 0)}m)")

        if apply_changes:
            new_doc_id = f"{doctor_id}_{new_date}"
            data = {k: v for k, v in r.items() if not k.startswith("_")}
            data["dateKey"] = new_date
            db.collection("attendance").document(new_doc_id).set(data)
            if r["_doc_id"] != new_doc_id:
                db.collection("attendance").document(r["_doc_id"]).delete()

        moved += 1

    # ── Summary ──────────────────────────────────────────────────────
    print(f"""
Summary
  shifted (+1 day) : {moved}
  manual (skipped) : {len(skipped_manual)}
  non-'present'    : {len(skipped_status)} (bot never wrote these — untouched)
  conflicts        : {len(collisions)} (resolve manually in the admin panel)
  bad records      : {len(bad)} (missing doctorId/dateKey)
""")
    if collisions:
        print("Conflicting records (bot record kept at its OLD date):")
        for r, new_date in collisions:
            print(f"  doctor {r['doctorId']}: {r['dateKey']} wanted -> {new_date}")

    if not apply_changes:
        print("This was a DRY RUN. Review the list above, then run:")
        print("    python fix_attendance_dates.py --apply")


if __name__ == "__main__":
    main()
