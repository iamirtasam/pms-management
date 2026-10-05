import threading
import web
import selfbot
import socket
from datetime import datetime

# ANSI color codes
class Colors:
    RESET = '\033[0m'
    GREEN = '\033[92m'
    CYAN = '\033[96m'
    GRAY = '\033[90m'

def log(message, color=Colors.GREEN):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"{Colors.GRAY}[{now}]{Colors.RESET} {color}✓ {message}{Colors.RESET}")

def get_local_ip():
    """Get the local IP address of this machine"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "Unable to detect"

log("Starting web server...", Colors.GREEN)
threading.Thread(target=web.run, daemon=True).start()

local_ip = get_local_ip()
port = web.config['web_port']

log(f"Web server running on port {port}", Colors.GREEN)
log(f"Local access: http://localhost:{port}", Colors.CYAN)
log(f"Network access: http://{local_ip}:{port}", Colors.CYAN)

selfbot.run()
