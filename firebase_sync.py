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

# Load Firebase config from parent directory
def load_firebase_config():
    """Extract Firebase config from firebase-config.js"""
    config_path = os.path.join(os.path.dirname(__file__), "..", "firebase-config.js")
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # Extract projectId
        project_match = content.split('projectId:')[1].split('"')[1]
        
        return {
            'projectId': project_match
        }
    except Exception as e:
        log(f"Error loading Firebase config: {e}", 'ERROR')
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

