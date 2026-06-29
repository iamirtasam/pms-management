import re
import json
import discord
import asyncio
import os
import sys
import threading
from datetime import datetime, timedelta
from shared import entries, LOG_FILE
from firebase_sync import sync_attendance, get_pending_welcome_dms, mark_welcome_dm_sent, get_admin_discord_id_by_username, update_doctor_name_by_discord_id, get_linked_doctors, get_bot_admin_ids, ensure_bot_admins_seeded
import sheet_sync
import attendance_audit

# Pakistan Standard Time is UTC+5 (no DST). Daily audit runs at 1 AM PKT.
PKT_OFFSET = timedelta(hours=5)
AUDIT_HOUR = 1
import console_log

try:
    from google import genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    # will be logged after log() is defined

# ANSI color codes
class Colors:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    GRAY = '\033[90m'

def clear_screen():
    """Clear the terminal screen"""
    os.system('cls' if os.name == 'nt' else 'clear')

def print_banner():
    """Print ASCII banner"""
    banner = f"""{Colors.CYAN}{Colors.BOLD}
╔═══════════════════════════════════════════════════════════╗
║                                                           ║
║   ██████╗ ███╗   ███╗███████╗    ██████╗  ██████╗ ██████╗████████╗ █████╗ ██╗     ║
║   ██╔══██╗████╗ ████║██╔════╝    ██╔══██╗██╔═══██╗██╔══██╚══██╔══╝██╔══██╗██║     ║
║   ██████╔╝██╔████╔██║███████╗    ██████╔╝██║   ██║██████╔╝  ██║   ███████║██║     ║
║   ██╔═══╝ ██║╚██╔╝██║╚════██║    ██╔═══╝ ██║   ██║██╔══██╗  ██║   ██╔══██║██║     ║
║   ██║     ██║ ╚═╝ ██║███████║    ██║     ╚██████╔╝██║  ██║  ██║   ██║  ██║███████╗║
║   ╚═╝     ╚═╝     ╚═╝╚══════╝    ╚═╝      ╚═════╝ ╚═╝  ╚═╝  ╚═╝   ╚═╝  ╚═╝╚══════╝║
║                                                           ║
║              {Colors.YELLOW}Made by iamirtasam on github{Colors.CYAN}               ║
║                                                           ║
╚═══════════════════════════════════════════════════════════╝{Colors.RESET}
"""
    print(banner)

def log(message, level='INFO'):
    """Print formatted log message with timestamp and color, also write to log file"""
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
    elif level == 'INFO':
        color = Colors.CYAN
        icon = 'ℹ'
    else:
        color = Colors.WHITE
        icon = '•'
    
    print(f"{Colors.GRAY}[{now}]{Colors.RESET} {color}{icon} {message}{Colors.RESET}")
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{now}] {icon} [{level}] {message}\n")
    except Exception:
        pass
    # Mirror to Firestore for the web console (best-effort, never raises)
    try:
        console_log.push_log(now, level, message)
    except Exception:
        pass

if os.path.exists("config.json"):
    with open("config.json") as f:
        config = json.load(f)
else:
    # Strip whitespace AND surrounding quotes that Railway sometimes adds
    def _clean(val):
        return val.strip().strip('"').strip("'")
    config = {
        "token":      _clean(os.environ["DISCORD_TOKEN"]),
        "guild_id":   int(_clean(os.environ["GUILD_ID"])),
        "channel_id": int(_clean(os.environ["CHANNEL_ID"])),
        "web_port":   int(_clean(os.environ.get("WEB_PORT", "5000"))),
    }

log(f"Token loaded — length: {len(config['token'])} chars (first 4: {config['token'][:4]}...)", 'INFO')

GEMINI_API_KEY = config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")

if GEMINI_AVAILABLE and GEMINI_API_KEY:
    _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    log("Gemini 2.5 Flash ready as fallback parser", 'SUCCESS')
elif not GEMINI_AVAILABLE:
    log("google-genai not installed — Gemini fallback disabled", 'WARNING')
    _gemini_client = None
else:
    log("No GEMINI_API_KEY set — Gemini fallback disabled", 'WARNING')
    _gemini_client = None

# Bootstrap admins — used ONLY to seed bot_config/discord_admins the very first
# time it doesn't exist, and as the in-memory starting set before the first
# Firestore read. Once the doc exists, the master admin panel is the single
# source of truth (these become normal, removable entries).
SEED_ADMIN_ENTRIES = [
    {"name": "Admin 1", "discordId": "1007633493427228672"},
    {"name": "Admin 2", "discordId": "822044765502832701"},
    {"name": "Admin 3", "discordId": "579933818862043136"},
]
SEED_ADMIN_USER_IDS = {int(e["discordId"]) for e in SEED_ADMIN_ENTRIES}

# Live set used everywhere for permission checks. Refreshed every 60s by
# refresh_admin_ids_loop() from bot_config/discord_admins. Mutated in place
# (never rebound) so all references stay valid.
ADMIN_USER_IDS = set(SEED_ADMIN_USER_IDS)

# Track processed messages to avoid duplicates
processed_messages = set()

# Track when bot started - only process reactions added after this time
bot_start_time = None

client = discord.Client()

def parse_message(content):
    # Remove markdown formatting (asterisks) from the entire content
    content = content.replace('*', '').replace('_', '')
    
    def get(label):
        # Try both singular and plural forms
        patterns = [
            rf"{label}\s*:\s*(.+)",
            rf"{label}s\s*:\s*(.+)",  # plural
        ]
        for pattern in patterns:
            m = re.search(pattern, content, re.IGNORECASE)
            if m:
                # Strip any remaining whitespace and special characters
                return m.group(1).strip()
        return ""

    raw_name = get("Name")
    uid_match = re.search(r"<@(\d+)>", raw_name)
    if not uid_match:
        return None

    return {
        "user_id": uid_match.group(1),
        "name": raw_name,
        "on_duty": get("On Duty"),
        "off_duty": get("Off Duty"),
        "total_hours": get("Total Hour"),  # Will match both "Total Hour" and "Total Hours"
        "breaks": get("Break"),  # Will match both "Break" and "Breaks"
        "date": get("Date"),
    }

def format_portal_name(display_name):
    """
    Convert a Discord nickname into the portal name format.

    Rules:
      - Split on '|' and keep only the first two segments (badge + name),
        dropping any trailing suffix like 'M', 'TRN', 'F', etc.
      - Badge code (segment 1) is kept exactly as-is (e.g. 'AEMT-26').
      - Name part (segment 2) is converted to Title Case ('KANWAR' -> 'Kanwar').
      - Re-joined with ' | ' (space-pipe-space).

    Examples:
      'AEMT-26 | KANWAR | M'   -> 'AEMT-26 | Kanwar'
      'SRP-11 | ZAKARIYA | TRN'-> 'SRP-11 | Zakariya'
      'PR-20 | ABU BAKAR'      -> 'PR-20 | Abu Bakar'

    Returns the formatted string, or None if the nickname has no '|'
    (i.e. doesn't look like a badge|name format and shouldn't be synced).
    """
    if not display_name:
        return None
    parts = [p.strip() for p in display_name.split('|')]
    if len(parts) < 2:
        return None
    badge = parts[0]
    name  = parts[1]
    if not badge or not name:
        return None
    return f"{badge} | {name.title()}"

def parse_hours_minutes(total_hours_str):
    """Parse '5 hours 7 minutes' or '1 hour 30 minutes' into (hours, minutes)"""
    hours = 0
    minutes = 0
    
    # Match hours
    hour_match = re.search(r'(\d+)\s*hours?', total_hours_str, re.IGNORECASE)
    if hour_match:
        hours = int(hour_match.group(1))
    
    # Match minutes
    min_match = re.search(r'(\d+)\s*minutes?', total_hours_str, re.IGNORECASE)
    if min_match:
        minutes = int(min_match.group(1))
    
    # If no match, try to extract from format like "5h 20m" or "5 h 20 m"
    if hours == 0 and minutes == 0:
        compact_match = re.search(r'(\d+)\s*h(?:ours?)?\s*(\d+)?\s*m(?:inutes?)?', total_hours_str, re.IGNORECASE)
        if compact_match:
            hours = int(compact_match.group(1))
            if compact_match.group(2):
                minutes = int(compact_match.group(2))
    
    return hours, minutes

def parse_date(date_str):
    """Convert MM/DD/YYYY or DD/MM/YYYY to YYYY-MM-DD, then subtract 1 day"""
    try:
        # Try MM/DD/YYYY format first (US format)
        dt = datetime.strptime(date_str, "%m/%d/%Y")
    except:
        try:
            # Try DD/MM/YYYY format (European format)
            dt = datetime.strptime(date_str, "%d/%m/%Y")
        except:
            return None
    
    # Subtract 1 day from the parsed date
    from datetime import timedelta
    dt = dt - timedelta(days=1)
    
    return dt.strftime("%Y-%m-%d")

async def parse_hours_with_gemini(message_content):
    """
    Use Gemini to extract total hours and minutes from a malformed attendance message.
    Tries gemini-2.5-flash up to 3 times (retry on 503/UNAVAILABLE), then falls back
    to gemini-2.0-flash. Returns (hours, minutes) or (0, 0) if all attempts fail.
    """
    if not _gemini_client:
        return 0, 0

    prompt = (
        "You are parsing an EMS attendance log message. "
        "Extract ONLY the total working hours and minutes from the 'Total Hours' field. "
        "The field may be misspelled (e.g. 'hurs', 'mutes', 'hrs', 'mns', 'min', etc.). "
        "Respond with ONLY two integers on one line separated by a space: HOURS MINUTES "
        "(e.g. '5 30' means 5 hours 30 minutes). "
        "Do not include any other text, explanation, or punctuation.\n\n"
        f"Attendance message:\n{message_content}"
    )

    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash"]
    for model_name in models_to_try:
        for attempt in range(3):
            try:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda m=model_name: _gemini_client.models.generate_content(
                        model=m, contents=prompt
                    )
                )
                text = response.text.strip()
                parts = text.split()
                if len(parts) >= 2:
                    hours   = int(parts[0])
                    minutes = int(parts[1])
                    log(f"Gemini ({model_name}) parsed: {hours}h {minutes}m from malformed input", 'SUCCESS')
                    return hours, minutes
                elif len(parts) == 1:
                    hours = int(parts[0])
                    log(f"Gemini ({model_name}) parsed: {hours}h 0m (only hours found)", 'SUCCESS')
                    return hours, 0
            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    if attempt < 2:
                        log(f"Gemini ({model_name}) attempt {attempt + 1} got 503 — retrying in 3s...", 'WARNING')
                        await asyncio.sleep(3)
                        continue
                    log(f"Gemini ({model_name}) all retries exhausted — trying next model", 'WARNING')
                else:
                    log(f"Gemini ({model_name}) error: {e}", 'ERROR')
                break

    return 0, 0

async def sync_attendance_to_firebase(discord_user_id, date_key, hours, minutes):
    """Sync attendance to Firebase using REST API"""
    try:
        # Run sync in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, sync_attendance, discord_user_id, date_key, hours, minutes)
        return success
    except Exception as e:
        print(f"❌ Failed to sync attendance: {e}")
        return False

@client.event
async def on_ready():
    global bot_start_time
    bot_start_time = datetime.utcnow()  # Record when bot started
    
    clear_screen()
    print_banner()
    
    log(f"Logged in as {client.user}", 'SUCCESS')
    log(f"User ID: {client.user.id}", 'INFO')
    log(f"Bot started at {bot_start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}", 'INFO')
    
    channel = client.get_channel(config["channel_id"])
    if channel:
        # Load message history but mark all existing messages as already processed
        async for message in channel.history(limit=200):
            entry = parse_message(message.content)
            if entry:
                uid = entry["user_id"]
                if entry not in entries.get(uid, []):
                    entries.setdefault(uid, []).append(entry)
            
            # Mark all existing messages as processed to ignore old reactions
            processed_messages.add(message.id)
        
        log(f"Loaded {len(entries)} users from history", 'SUCCESS')
        log(f"Marked {len(processed_messages)} existing messages as processed", 'INFO')
        log(f"Watching for ✅ reactions from admins (IDs: {ADMIN_USER_IDS})", 'INFO')
    
    # One-time: create the panel-managed admin list from the bootstrap set if it
    # doesn't exist yet, then load it. After this, the panel is authoritative.
    await asyncio.get_event_loop().run_in_executor(None, ensure_bot_admins_seeded, SEED_ADMIN_ENTRIES)
    await refresh_admin_ids(initial=True)

    # Start background loops
    client.loop.create_task(check_reactions_loop())
    client.loop.create_task(check_welcome_dms_loop())
    client.loop.create_task(refresh_admin_ids_loop())
    client.loop.create_task(attendance_audit_loop())

def _msg_link(message):
    return f"https://discord.com/channels/{config['guild_id']}/{config['channel_id']}/{message.id}"

async def run_attendance_audit():
    """
    Scan the attendance channel and return (target_date_str, [report_line, ...])
    for yesterday's (PKT) faulty attendances. Each line is:
        <@authorId> <reason> <message-link>
    A message belongs to "yesterday" if its Date field == yesterday, OR (when its
    date is missing/invalid) it was posted during yesterday PKT — so broken-date
    attendances are still caught. Already ✅-ticked messages are skipped.
    """
    channel = client.get_channel(config["channel_id"])
    if not channel:
        log("Audit: attendance channel not found", 'ERROR')
        return ("", [])

    now_pkt    = datetime.utcnow() + PKT_OFFSET
    target     = (now_pkt - timedelta(days=1)).date()
    target_str = target.strftime('%m/%d/%Y')
    cutoff_utc = datetime.utcnow() - timedelta(days=2)  # perf bound for history scan

    log(f"Running attendance audit for {target_str}", 'INFO')

    wrong = []
    async for message in channel.history(limit=300):
        created = message.created_at.replace(tzinfo=None)  # discord gives UTC
        if created < cutoff_utc:
            break  # history is newest-first; nothing older matters
        content = message.content or ''
        if not attendance_audit.looks_like_attendance(content):
            continue
        if any(str(r.emoji) == '✅' for r in message.reactions):
            continue  # already handled/approved

        # Membership is by the day the message was POSTED (PKT), not the written
        # date — so the audit covers exactly that calendar day (12 AM–11:59 PM)
        # and never the next day's posts. The written date is then validated
        # against this posting day.
        posted_pkt = (created + PKT_OFFSET).date()
        if posted_pkt != target:
            continue

        posted_tuple = (posted_pkt.month, posted_pkt.day, posted_pkt.year)
        reason = attendance_audit.validate_attendance(content, message.author.id, posted_tuple)
        if reason:
            wrong.append(f"<@{message.author.id}> {reason} {_msg_link(message)}")

    log(f"Audit complete: {len(wrong)} faulty attendance(s) for {target_str}", 'SUCCESS')
    return (target_str, wrong)

def build_audit_report(target_str, items):
    """Format the audit into Discord messages (chunked under the 2000-char limit)."""
    header = f"# Todays Wrong attendances [{target_str}]\n\n"
    if not items:
        return [header + "✅ No faulty attendances found."]
    blocks, cur = [], ""
    for line in items:
        if len(cur) + len(line) + 2 > 1600:
            blocks.append(cur.rstrip())
            cur = ""
        cur += line + "\n\n"
    if cur.strip():
        blocks.append(cur.rstrip())
    return [(header if i == 0 else "") + "```\n" + b + "\n```" for i, b in enumerate(blocks)]

async def dm_all_bot_admins(messages):
    """DM the given message(s) to every bot-control admin."""
    guild = client.get_guild(config["guild_id"])
    sent = 0
    for aid in list(ADMIN_USER_IDS):
        try:
            member = (guild.get_member(aid) if guild else None) or await client.fetch_user(aid)
            dm = await member.create_dm()
            for m in messages:
                await dm.send(m)
            sent += 1
        except Exception as e:
            log(f"Audit: failed to DM admin {aid}: {e}", 'WARNING')
    log(f"Audit report sent to {sent} bot admin(s)", 'SUCCESS')

async def attendance_audit_loop():
    """Run the attendance audit once a day at AUDIT_HOUR (PKT) and DM all admins."""
    await client.wait_until_ready()
    log(f"Attendance audit loop started (daily at {AUDIT_HOUR}:00 AM PKT)", 'INFO')
    while not client.is_closed():
        now_pkt = datetime.utcnow() + PKT_OFFSET
        nxt = now_pkt.replace(hour=AUDIT_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now_pkt:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now_pkt).total_seconds())
        try:
            target_str, wrong = await run_attendance_audit()
            await dm_all_bot_admins(build_audit_report(target_str, wrong))
        except Exception as e:
            log(f"Attendance audit failed: {e}", 'ERROR')

async def refresh_admin_ids(initial=False):
    """
    Reload the authoritative bot-admin list from the master admin panel
    (bot_config/discord_admins). The panel is the single source of truth:
    whatever it contains IS the admin set. A None result means the list could
    not be read (Firebase down or doc not created yet) — in that case we keep
    the current set so a transient hiccup never wipes admins.
    """
    try:
        loop    = asyncio.get_event_loop()
        managed = await loop.run_in_executor(None, get_bot_admin_ids)
        if managed is None:
            if initial:
                log(f"Bot admins: panel list unavailable, using current set: {sorted(ADMIN_USER_IDS)}", 'WARNING')
            return
        if managed != ADMIN_USER_IDS:
            added   = managed - ADMIN_USER_IDS
            removed = ADMIN_USER_IDS - managed
            ADMIN_USER_IDS.clear()
            ADMIN_USER_IDS.update(managed)
            if added:
                log(f"Bot admins added: {sorted(added)}", 'SUCCESS')
            if removed:
                log(f"Bot admins removed: {sorted(removed)}", 'WARNING')
        if initial:
            log(f"Bot admins loaded ({len(ADMIN_USER_IDS)} total): {sorted(ADMIN_USER_IDS)}", 'INFO')
    except Exception as e:
        log(f"Error refreshing bot admins: {e}", 'ERROR')

async def refresh_admin_ids_loop():
    """Every 60 seconds, refresh the managed bot-admin list from Firestore."""
    await client.wait_until_ready()
    log("Bot-admin refresh loop started (checking every 60 seconds)", 'INFO')
    while not client.is_closed():
        await asyncio.sleep(60)
        await refresh_admin_ids()

async def check_reactions_loop():
    """Poll for new ✅ reactions every 5 seconds"""
    await client.wait_until_ready()
    channel = client.get_channel(config["channel_id"])
    
    if not channel:
        log(f"Could not find channel {config['channel_id']}", 'ERROR')
        return
    
    log("Reaction polling started (checking every 5 seconds)", 'INFO')
    
    while not client.is_closed():
        try:
            # Fetch recent messages (last 50)
            async for message in channel.history(limit=50):
                # Skip if already processed
                if message.id in processed_messages:
                    continue
                
                # Check if message has ✅ reaction from admin
                has_admin_checkmark = False
                reacting_admin = None
                for reaction in message.reactions:
                    if str(reaction.emoji) == '✅':
                        # Check if admin reacted
                        users = [user async for user in reaction.users()]
                        for user in users:
                            if user.id in ADMIN_USER_IDS:
                                has_admin_checkmark = True
                                reacting_admin = user
                                break
                        if has_admin_checkmark:
                            break
                
                if not has_admin_checkmark:
                    continue
                
                # Mark as processed
                processed_messages.add(message.id)
                
                log(f"Detected ✅ reaction on message {message.id}", 'INFO')
                
                # Parse the attendance message
                entry = parse_message(message.content)
                if not entry:
                    log("Could not parse attendance message", 'WARNING')
                    continue
                
                discord_user_id = entry["user_id"]
                total_hours_str = entry["total_hours"]
                date_str = entry["date"]
                
                if not total_hours_str or not date_str:
                    log("Missing hours or date in message", 'WARNING')
                    continue
                
                # Parse hours and minutes
                hours, minutes = parse_hours_minutes(total_hours_str)

                # Fallback: if regex parsing returned (0, 0), try Gemini
                if hours == 0 and minutes == 0:
                    log(f"Regex parse returned 0h 0m for '{total_hours_str}' — trying Gemini fallback...", 'WARNING')
                    hours, minutes = await parse_hours_with_gemini(message.content)
                    if hours == 0 and minutes == 0:
                        log("Gemini also returned 0h 0m — skipping this attendance record", 'WARNING')
                        if reacting_admin:
                            try:
                                dm = await reacting_admin.create_dm()
                                await dm.send(
                                    f"⚠️ Could not auto-parse attendance hours for user <@{discord_user_id}> on {date_str}.\n"
                                    "Please add their attendance manually in the admin panel."
                                )
                            except Exception as dm_err:
                                log(f"Failed to send DM to admin: {dm_err}", 'WARNING')
                        continue

                # Parse date to YYYY-MM-DD format
                date_key = parse_date(date_str)
                if not date_key:
                    log(f"Could not parse date: {date_str}", 'WARNING')
                    continue

                log(f"Attendance approved - User: {discord_user_id}, Date: {date_str} → {date_key}, Duration: {hours}h {minutes}m", 'SUCCESS')

                # Sync to Firebase
                await sync_attendance_to_firebase(discord_user_id, date_key, hours, minutes)

            # Clean up old processed messages (keep last 100)
            if len(processed_messages) > 100:
                processed_messages.clear()

        except Exception as e:
            log(f"Error in polling loop: {e}", 'ERROR')

        # Wait 5 seconds before next check
        await asyncio.sleep(5)

PORTAL_URL = "https://legendary-bavarois-b61429.netlify.app/login"

async def check_welcome_dms_loop():
    """Every 30 seconds, send welcome DMs to newly created doctors. One attempt only."""
    await client.wait_until_ready()
    log("Welcome DM loop started (checking every 30 seconds)", 'INFO')
    while not client.is_closed():
        try:
            loop = asyncio.get_event_loop()
            pending = await loop.run_in_executor(None, get_pending_welcome_dms)
            for doctor in pending:
                discord_id = doctor['discordId']
                doc_id     = doctor['doc_id']
                username   = doctor['username']
                password   = doctor['plainPassword']
                name       = doctor['name']
                created_by = doctor['createdBy']  # admin username who created the doctor
                log(f"Sending welcome DM to {name} (Discord ID: {discord_id})", 'INFO')
                dm_sent = False
                fail_reason = ''
                try:
                    # Prefer Member from shared guild (more reliable for selfbots)
                    guild  = client.get_guild(config['guild_id'])
                    member = guild.get_member(int(discord_id)) if guild else None
                    if member is None:
                        log(f"Member not in guild cache, falling back to fetch_user", 'WARNING')
                        member = await client.fetch_user(int(discord_id))
                    dm  = await member.create_dm()
                    msg = (
                        f"# PMS Portal\n\n"
                        f"**Site:** {PORTAL_URL}\n\n"
                        f"**Username:** {username}\n"
                        f"**Password:** ||{password}||\n\n"
                        f"Welcome to the EMS Portal, {name}! Use the link above to sign in."
                    )
                    await dm.send(msg)
                    dm_sent = True
                    log(f"Welcome DM sent to {name} ({discord_id})", 'SUCCESS')
                except discord.NotFound:
                    fail_reason = f"User ID `{discord_id}` not found on Discord."
                    log(f"User {discord_id} not found on Discord", 'WARNING')
                except discord.Forbidden as e:
                    fail_reason = f"Cannot DM `{discord_id}`: {e.text} (code {e.code})"
                    log(f"Forbidden when DMing {discord_id}: {e.text!r} (code {e.code})", 'WARNING')
                except Exception as e:
                    fail_reason = f"Unexpected error DMing `{discord_id}`: {type(e).__name__}: {e}"
                    log(f"Failed to DM {discord_id}: {type(e).__name__}: {e}", 'ERROR')

                # Mark in Firestore — one attempt only, no retry
                await loop.run_in_executor(None, mark_welcome_dm_sent, doc_id, dm_sent)

                # If failed, notify only the admin who created this doctor
                if not dm_sent:
                    log(f"DM failed for {name} — looking up creator admin '{created_by}'", 'WARNING')
                    creator_did = await loop.run_in_executor(
                        None, get_admin_discord_id_by_username, created_by
                    )
                    if creator_did:
                        try:
                            guild       = client.get_guild(config['guild_id'])
                            admin_member = guild.get_member(int(creator_did)) if guild else None
                            if admin_member is None:
                                admin_member = await client.fetch_user(int(creator_did))
                            admin_dm   = await admin_member.create_dm()
                            cred_block = (
                                f"```\n"
                                f"# PMS Portal\n\n"
                                f"**Site:** {PORTAL_URL}\n\n"
                                f"**Username:** {username}\n"
                                f"**Password:** ||{password}||\n\n"
                                f"Welcome to the EMS Portal, {name}! Use the link above to sign in."
                                f"\n```"
                            )
                            await admin_dm.send(
                                f"\u26a0\ufe0f **Failed to send welcome DM to `{name}`**\n"
                                f"Discord ID: `{discord_id}`\n"
                                f"Reason: {fail_reason}\n\n"
                                f"Please DM them manually with their portal credentials.\n\n"
                                + cred_block
                            )
                            log(f"Notified creator admin {creator_did} about failed DM", 'SUCCESS')
                        except Exception as ae:
                            log(f"Could not notify creator admin {creator_did}: {ae}", 'WARNING')
                    else:
                        log(f"Creator admin '{created_by}' has no Discord ID set — cannot notify", 'WARNING')
        except Exception as e:
            log(f"Error in welcome DM loop: {e}", 'ERROR')
        await asyncio.sleep(30)

async def sync_member_name(member, source='live'):
    """Sync a single guild member's nickname to their linked portal doctor."""
    display = member.nick or member.name
    portal_name = format_portal_name(display)
    if not portal_name:
        return None  # nickname not in 'badge | name' format — ignore
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, update_doctor_name_by_discord_id, str(member.id), portal_name
    )
    if result.get('status') == 'updated':
        log(f"[{source}] Portal name synced for {member.id}: "
            f"'{result['old']}' -> '{result['new']}'", 'SUCCESS')
    elif result.get('status') == 'notfound':
        log(f"[{source}] Nickname changed for {member.id} ({portal_name}) "
            f"but no linked doctor found", 'INFO')
    return result

@client.event
async def on_member_update(before, after):
    """
    Fire when a guild member's nickname changes. If it changed and they are
    a linked doctor, update their portal name automatically.
    """
    if after.guild.id != config["guild_id"]:
        return
    before_nick = before.nick or before.name
    after_nick  = after.nick or after.name
    if before_nick == after_nick:
        return  # nickname didn't change (some other field did)
    log(f"Nickname change detected for {after.id}: '{before_nick}' -> '{after_nick}'", 'INFO')
    await sync_member_name(after, source='live')

@client.event
async def on_message(message):
    # ── DM command handler ────────────────────────────────────────
    if message.guild is None and message.author.id in ADMIN_USER_IDS:
        content = message.content.strip()
        if content.startswith('pms!'):
            await handle_command(message, content)
            return

    # ── Attendance channel listener ───────────────────────────────
    if message.guild and message.guild.id == config["guild_id"] and message.channel.id == config["channel_id"]:
        entry = parse_message(message.content)
        if entry:
            uid = entry["user_id"]
            entries.setdefault(uid, []).append(entry)
            log(f"New attendance message logged for user {uid}", 'INFO')

async def handle_command(message, content):
    """Handle pms! prefix commands sent via DM from admin"""
    cmd = content[4:].strip().lower()  # strip 'pms!' prefix

    # ── pms!sheet or pms!sheet --current ─────────────────────────
    if cmd == 'sheet' or cmd == 'sheet --current':
        use_current = cmd == 'sheet --current'
        mode = 'current week' if use_current else 'previous completed week'
        log(f"pms!sheet triggered via DM (mode: {mode})", 'INFO')
        await message.channel.send(f"⏳ Generating sheet for **{mode}**... please wait.")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, sheet_sync.run, use_current)
            await message.channel.send(f"✅ Sheet generated successfully for **{mode}**.\nhttps://docs.google.com/spreadsheets/d/{sheet_sync.SHEET_ID}")
            log("Sheet generation completed via DM command", 'SUCCESS')
        except Exception as e:
            await message.channel.send(f"❌ Sheet generation failed: `{e}`")
            log(f"Sheet generation failed: {e}", 'ERROR')

    # ── pms!logs ──────────────────────────────────────────────────
    elif cmd == 'logs':
        log("pms!logs triggered via DM", 'INFO')
        try:
            if not os.path.exists(LOG_FILE):
                await message.channel.send("📭 No log file found yet.")
                return
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            last_lines = lines[-100:] if len(lines) >= 100 else lines
            text = ''.join(last_lines).strip()
            if not text:
                await message.channel.send("📭 Log file is empty.")
                return
            # Discord message limit is 2000 chars — chunk if needed
            chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
            for chunk in chunks:
                await message.channel.send(f"```\n{chunk}\n```")
        except Exception as e:
            await message.channel.send(f"❌ Failed to read logs: `{e}`")

    # ── pms!restart ───────────────────────────────────────────────
    elif cmd == 'restart':
        log("pms!restart triggered via DM — restarting...", 'WARNING')
        await message.channel.send("🔄 Restarting selfbot...")
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ── pms!check ─────────────────────────────────────────────────
    elif cmd == 'check':
        log("pms!check triggered via DM", 'INFO')
        await message.channel.send("⏳ Auditing yesterday's attendances...")
        try:
            target_str, wrong = await run_attendance_audit()
            for m in build_audit_report(target_str, wrong):
                await message.channel.send(m)
            log(f"pms!check completed ({len(wrong)} faulty)", 'SUCCESS')
        except Exception as e:
            await message.channel.send(f"❌ Audit failed: `{e}`")
            log(f"pms!check failed: {e}", 'ERROR')

    # ── pms!syncnames ─────────────────────────────────────────────
    elif cmd == 'syncnames':
        log("pms!syncnames triggered via DM", 'INFO')
        await message.channel.send("⏳ Re-syncing portal names from current Discord nicknames... please wait.")
        try:
            loop   = asyncio.get_event_loop()
            linked = await loop.run_in_executor(None, get_linked_doctors)
            guild  = client.get_guild(config['guild_id'])
            if not guild:
                await message.channel.send("❌ Could not access the guild.")
                return

            updated, unchanged, skipped, notfound = [], 0, 0, 0
            for doc in linked:
                did = doc['discordId']
                try:
                    member = guild.get_member(int(did))
                    if member is None:
                        member = await guild.fetch_member(int(did))
                except Exception:
                    member = None
                if member is None:
                    notfound += 1
                    continue
                display     = member.nick or member.name
                portal_name = format_portal_name(display)
                if not portal_name:
                    skipped += 1
                    continue
                result = await loop.run_in_executor(
                    None, update_doctor_name_by_discord_id, did, portal_name
                )
                status = result.get('status')
                if status == 'updated':
                    updated.append(f"`{result['old']}` → `{result['new']}`")
                elif status == 'unchanged':
                    unchanged += 1
                else:
                    skipped += 1

            summary = (
                f"✅ **Name sync complete**\n"
                f"Updated: **{len(updated)}**  |  Unchanged: **{unchanged}**  |  "
                f"Skipped: **{skipped}**  |  Member not in server: **{notfound}**"
            )
            if updated:
                detail = "\n".join(updated)
                # keep within Discord's 2000 char limit
                if len(detail) > 1700:
                    detail = detail[:1700] + "\n…(truncated)"
                summary += "\n\n" + detail
            await message.channel.send(summary)
            log(f"syncnames done: {len(updated)} updated, {unchanged} unchanged, "
                f"{skipped} skipped, {notfound} not in server", 'SUCCESS')
        except Exception as e:
            await message.channel.send(f"❌ Name sync failed: `{e}`")
            log(f"syncnames failed: {e}", 'ERROR')

    # ── pms!help ──────────────────────────────────────────────────
    elif cmd == 'help':
        help_text = (
            "**PMS Selfbot Commands**\n"
            "`pms!sheet` — generate sheet for last completed week\n"
            "`pms!sheet --current` — generate sheet for current ongoing week\n"
            "`pms!logs` — get last 100 lines from the log file\n"
            "`pms!check` — audit yesterday's attendances and list the faulty ones\n"
            "`pms!syncnames` — re-sync all portal names from current Discord nicknames\n"
            "`pms!restart` — restart the selfbot process\n"
            "`pms!help` — show this message"
        )
        await message.channel.send(help_text)

    else:
        await message.channel.send(f"❓ Unknown command `pms!{cmd}`. Send `pms!help` for a list of commands.")

def run():
    client.run(config["token"])
