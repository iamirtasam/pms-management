"""
Mirrors the selfbot's console output into a single Firestore document
(bot_console/live) so the web admin panel can show a realtime console.

Design:
  - Logs are appended to an in-memory ring buffer (last MAX_LINES lines).
  - A daemon thread flushes the buffer to ONE Firestore document every
    FLUSH_INTERVAL seconds, but only when something changed.
  - A single capped document is used (not one doc per line) to keep
    Firestore reads/writes cheap and bounded — no cleanup job required.

The browser reads bot_console/live via onSnapshot for true realtime updates.
"""
import threading
from collections import deque
from datetime import datetime

MAX_LINES      = 300   # how many recent lines to keep / show
FLUSH_INTERVAL = 2.0   # seconds between Firestore writes
MAX_MSG_LEN    = 600   # trim very long single lines

_buffer    = deque(maxlen=MAX_LINES)
_lock      = threading.Lock()
_dirty     = False
_started   = False
_start_lock = threading.Lock()


def push_log(timestamp, level, message):
    """Append one log entry to the buffer. Safe to call from any thread."""
    global _dirty
    msg = str(message)
    if len(msg) > MAX_MSG_LEN:
        msg = msg[:MAX_MSG_LEN] + "…"
    with _lock:
        _buffer.append({'t': timestamp, 'l': level, 'm': msg})
        _dirty = True
    _ensure_started()


def _ensure_started():
    """Lazily start the background flush thread on first use."""
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        t = threading.Thread(target=_flush_loop, daemon=True, name='console-flush')
        t.start()
        _started = True


def _flush_loop():
    import time
    # Lazy import to avoid any circular import at module load.
    from firebase_sync import get_firestore_db
    while True:
        time.sleep(FLUSH_INTERVAL)
        global _dirty
        with _lock:
            if not _dirty:
                continue
            lines = list(_buffer)
            _dirty = False
        try:
            db = get_firestore_db()
            if not db:
                continue
            db.collection('bot_console').document('live').set({
                'lines':     lines,
                'updatedAt': datetime.utcnow().isoformat() + 'Z',
            })
        except Exception:
            # Never let console mirroring break the bot. Mark dirty so the
            # next tick retries.
            with _lock:
                _dirty = True
