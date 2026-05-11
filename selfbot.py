import re
import json
import discord
import asyncio
import os
import sys
import threading
from datetime import datetime, timedelta
from shared import entries, LOG_FILE
from firebase_sync import sync_attendance
import sheet_sync

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

if os.path.exists("config.json"):
    with open("config.json") as f:
        config = json.load(f)
else:
    config = {
        "token":      os.environ["DISCORD_TOKEN"],
        "guild_id":   int(os.environ["GUILD_ID"]),
        "channel_id": int(os.environ["CHANNEL_ID"]),
        "web_port":   int(os.environ.get("WEB_PORT", 5000)),
    }

# Discord user IDs that can trigger attendance approval and DM commands
ADMIN_USER_IDS = {1233480562455609385, 1007633493427228672}

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
    
    # Start the polling loop
    client.loop.create_task(check_reactions_loop())

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
                for reaction in message.reactions:
                    if str(reaction.emoji) == '✅':
                        # Check if admin reacted
                        users = [user async for user in reaction.users()]
                        if any(user.id in ADMIN_USER_IDS for user in users):
                            has_admin_checkmark = True
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

    # ── pms!help ──────────────────────────────────────────────────
    elif cmd == 'help':
        help_text = (
            "**PMS Selfbot Commands**\n"
            "`pms!sheet` — generate sheet for last completed week\n"
            "`pms!sheet --current` — generate sheet for current ongoing week\n"
            "`pms!logs` — get last 100 lines from the log file\n"
            "`pms!restart` — restart the selfbot process\n"
            "`pms!help` — show this message"
        )
        await message.channel.send(help_text)

    else:
        await message.channel.send(f"❓ Unknown command `pms!{cmd}`. Send `pms!help` for a list of commands.")

def run():
    client.run(config["token"])
