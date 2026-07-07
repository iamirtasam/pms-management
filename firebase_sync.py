"""
Firebase integration for attendance syncing using Firebase Admin SDK
"""
import json
import os
from datetime import datetime

# ANSI color codes
class Colors:
    RESET = '\033[0m'
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    GRAY = '\033[90m'

def log(message, level='INFO'):
    """Print formatted log message with timestamp and color"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if level == 'SUCCESS':
        color = Colors.GREEN
        icon = '✓'
    elif level == 'ERROR':
        color = Colors.RED
        icon = '✗'
    elif level == 'WARNING':
        color = Colors.YELLOW
        icon = '⚠'
    else:
        color = Colors.RESET
        icon = '•'
    
    print(f"{Colors.GRAY}[{now}]{Colors.RESET} {color}{icon} {message}{Colors.RESET}")
    # Mirror to the web console buffer (best-effort, never raises)
    try:
        import console_log
        console_log.push_log(now, level, message)
    except Exception:
        pass

# We'll use the Firebase REST API but need to go through the web SDK
# Since we can't use Admin SDK without service account, we'll use a workaround
# by directly calling the same Firestore methods the web app uses

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    log("firebase-admin not installed. Install with: pip install firebase-admin", 'WARNING')

# Load Firebase project ID — env var takes priority (Railway), file is local fallback
def load_firebase_config():
    """Get Firebase project ID from env var or firebase-config.js (local dev)."""
    # 1. Try environment variable (set this in Railway)
    project_id = os.environ.get("FIREBASE_PROJECT_ID", "").strip().strip('"').strip("'")
    if project_id:
        log(f"Firebase project ID loaded from env: {project_id}", 'INFO')
        return {'projectId': project_id}

    # 2. Fall back to parsing the JS file (local development)
    config_path = os.path.join(os.path.dirname(__file__), "..", "firebase-config.js")
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()
        project_match = content.split('projectId:')[1].split('"')[1]
        log(f"Firebase project ID loaded from file: {project_match}", 'INFO')
        return {'projectId': project_match}
    except Exception as e:
        log(f"Error loading Firebase config (set FIREBASE_PROJECT_ID env var on Railway): {e}", 'ERROR')
        return None


FIREBASE_CONFIG = load_firebase_config()
_db = None

def get_firestore_db():
    """Initialize Firebase Admin SDK (requires service account key)"""
    global _db
    if _db:
        return _db
    
    if not FIREBASE_AVAILABLE:
        return None
    
    try:
        if not firebase_admin._apps:
            key_path = os.path.join(os.path.dirname(__file__), "serviceAccountKey.json")
            if os.path.exists(key_path):
                cred = credentials.Certificate(key_path)
            else:
                import json as _json
                sa_json = os.environ.get("SERVICE_ACCOUNT_JSON")
                if not sa_json:
                    log("No serviceAccountKey.json and no SERVICE_ACCOUNT_JSON env var found", 'ERROR')
                    return None
                cred = credentials.Certificate(_json.loads(sa_json))
            firebase_admin.initialize_app(cred)
        
        _db = firestore.client()
        return _db
    except Exception as e:
        log(f"Could not initialize Firebase Admin: {e}", 'ERROR')
        return None

def get_doctor_by_discord_id(discord_id):
    """Find doctor document by Discord ID using Admin SDK"""
    db = get_firestore_db()
    if not db:
        log("Firebase not available - cannot query doctors", 'ERROR')
        return None
    
    try:
        # Query doctors collection for matching discordId
        docs = db.collection('doctors').where('discordId', '==', discord_id).limit(1).stream()
        
        for doc in docs:
            return doc.id
        
        return None
    except Exception as e:
        log(f"Error querying doctors: {e}", 'ERROR')
        return None

def add_attendance(doctor_id, date_key, hours, minutes):
    """Add attendance record to Firebase using Admin SDK"""
    db = get_firestore_db()
    if not db:
        log("Firebase not available - cannot add attendance", 'ERROR')
        return False
    
    try:
        # Create attendance document with composite key: doctorId_dateKey
        doc_id = f"{doctor_id}_{date_key}"
        
        # Prepare document data (matching admin.html format)
        data = {
            'doctorId': doctor_id,
            'dateKey': date_key,
            'status': 'present',
            'hours': hours,
            'minutes': minutes,
            'markedAt': datetime.utcnow().isoformat() + 'Z'
        }
        
        # Write to Firestore
        db.collection('attendance').document(doc_id).set(data)
        
        log(f"Synced to Firebase: {doc_id} ({hours}h {minutes}m)", 'SUCCESS')
        return True
            
    except Exception as e:
        log(f"Error syncing attendance: {e}", 'ERROR')
        return False

def delete_attendance(discord_user_id, date_key):
    """
    Delete the attendance record for this Discord user on date_key.
    Used when a ticked attendance message is edited to a different date —
    the record at the OLD date must be removed so no stale duplicate remains.
    """
    db = get_firestore_db()
    if not db:
        log("Firebase not available - cannot delete attendance", 'ERROR')
        return False
    doctor_id = get_doctor_by_discord_id(discord_user_id)
    if not doctor_id:
        log(f"No doctor found with Discord ID: {discord_user_id}", 'WARNING')
        return False
    try:
        db.collection('attendance').document(f"{doctor_id}_{date_key}").delete()
        log(f"Deleted attendance {doctor_id}_{date_key} (message edit moved it)", 'SUCCESS')
        return True
    except Exception as e:
        log(f"Error deleting attendance: {e}", 'ERROR')
        return False

def sync_attendance(discord_user_id, date_key, hours, minutes):
    """Main sync function"""
    log(f"Syncing attendance for Discord ID {discord_user_id}", 'INFO')
    
    # Find doctor by Discord ID
    doctor_id = get_doctor_by_discord_id(discord_user_id)
    
    if not doctor_id:
        log(f"No doctor found with Discord ID: {discord_user_id}", 'WARNING')
        return False
    
    log(f"Found doctor: {doctor_id}", 'SUCCESS')
    
    # Add attendance
    success = add_attendance(doctor_id, date_key, hours, minutes)
    
    return success

def get_pending_welcome_dms():
    """
    Query Firestore for doctors with welcomeDmSent == False.
    Returns a list of dicts: {doc_id, discordId, username, plainPassword, name}
    """
    db = get_firestore_db()
    if not db:
        log("Firebase not available - cannot query pending DMs", 'ERROR')
        return []
    try:
        docs = db.collection('doctors').where('welcomeDmSent', '==', False).stream()
        pending = []
        for doc in docs:
            d = doc.to_dict()
            discord_id = d.get('discordId', '').strip()
            if not discord_id:
                continue  # no Discord ID — skip silently
            pending.append({
                'doc_id':        doc.id,
                'discordId':     discord_id,
                'username':      d.get('username', ''),
                'plainPassword': d.get('plainPassword', d.get('password', '')),
                'name':          d.get('name', ''),
                'createdBy':     d.get('createdBy', ''),  # admin username who created the doctor
            })
        return pending
    except Exception as e:
        log(f"Error querying pending DMs: {e}", 'ERROR')
        return []

def mark_welcome_dm_sent(doc_id, success=True):
    """Mark a doctor's welcomeDmSent field as True (or 'failed') in Firestore."""
    db = get_firestore_db()
    if not db:
        return
    try:
        db.collection('doctors').document(doc_id).update({
            'welcomeDmSent': True if success else 'failed'
        })
    except Exception as e:
        log(f"Error marking welcomeDmSent for {doc_id}: {e}", 'ERROR')

def update_doctor_name_by_discord_id(discord_id, new_name):
    """
    Find the doctor linked to this Discord ID and update their 'name' field.
    Only writes if the name actually changed. Returns a dict:
      {'status': 'updated', 'old': ..., 'new': ...}
      {'status': 'unchanged', 'name': ...}
      {'status': 'notfound'}
      {'status': 'error', 'reason': ...}
    """
    db = get_firestore_db()
    if not db:
        return {'status': 'error', 'reason': 'firebase unavailable'}
    try:
        docs = list(
            db.collection('doctors').where('discordId', '==', discord_id).limit(1).stream()
        )
        if not docs:
            return {'status': 'notfound'}
        doc     = docs[0]
        current = (doc.to_dict().get('name') or '').strip()
        if current == new_name:
            return {'status': 'unchanged', 'name': current}
        db.collection('doctors').document(doc.id).update({'name': new_name})
        log(f"Doctor name updated: '{current}' -> '{new_name}' (discordId {discord_id})", 'SUCCESS')
        return {'status': 'updated', 'old': current, 'new': new_name}
    except Exception as e:
        log(f"Error updating doctor name for {discord_id}: {e}", 'ERROR')
        return {'status': 'error', 'reason': str(e)}

def get_bot_admin_ids():
    """
    Return the set of int Discord user IDs allowed to control the bot, read from
    bot_config/discord_admins ({ 'admins': [{'name':..., 'discordId':...}, ...] }).

    Returns:
      - a set (possibly EMPTY) when the doc was read successfully — this is the
        authoritative list, including a deliberately empty one.
      - None when the list could not be determined (Firebase unavailable, read
        error, or the doc doesn't exist yet). Callers should treat None as
        "keep whatever you currently have" so a transient hiccup never wipes
        the admin list.
    """
    db = get_firestore_db()
    if not db:
        return None
    try:
        snap = db.collection('bot_config').document('discord_admins').get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        ids = set()
        for entry in data.get('admins', []):
            did = str(entry.get('discordId', '')).strip()
            if did.isdigit():
                ids.add(int(did))
        return ids
    except Exception as e:
        log(f"Error fetching bot admin IDs: {e}", 'ERROR')
        return None

def ensure_bot_admins_seeded(seed_entries):
    """
    One-time migration: if bot_config/discord_admins does not exist yet, create
    it from seed_entries ([{'name':..., 'discordId':...}, ...]) so the current
    hardcoded admins appear in the panel and become fully manageable. If the doc
    already exists (even empty), it is left untouched — the panel is authoritative.
    """
    db = get_firestore_db()
    if not db:
        return
    try:
        ref  = db.collection('bot_config').document('discord_admins')
        snap = ref.get()
        if snap.exists:
            return
        ref.set({
            'admins':    list(seed_entries),
            'updatedAt': datetime.utcnow().isoformat() + 'Z',
        })
        log(f"Seeded bot_config/discord_admins with {len(seed_entries)} admin(s)", 'SUCCESS')
    except Exception as e:
        log(f"Error seeding bot admins: {e}", 'ERROR')

def get_linked_doctors():
    """
    Return a list of all doctors that have a non-empty discordId:
      [{'doc_id': ..., 'discordId': ..., 'name': ...}, ...]
    """
    db = get_firestore_db()
    if not db:
        log("Firebase not available - cannot list linked doctors", 'ERROR')
        return []
    try:
        out = []
        for doc in db.collection('doctors').stream():
            d   = doc.to_dict()
            did = (d.get('discordId') or '').strip()
            if did:
                out.append({
                    'doc_id':    doc.id,
                    'discordId': did,
                    'name':      (d.get('name') or '').strip(),
                })
        return out
    except Exception as e:
        log(f"Error listing linked doctors: {e}", 'ERROR')
        return []

def get_admin_discord_id_by_username(username):
    """Return the Discord ID of the sub-admin with the given username, or None."""
    if not username:
        return None
    db = get_firestore_db()
    if not db:
        return None
    try:
        docs = db.collection('admins').where('username', '==', username).limit(1).stream()
        for doc in docs:
            did = doc.to_dict().get('discordId', '').strip()
            return did if did else None
        return None
    except Exception as e:
        log(f"Error fetching admin Discord ID for '{username}': {e}", 'ERROR')
        return None


