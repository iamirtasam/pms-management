import json
import os
import logging
from flask import Flask, send_from_directory, abort

# Suppress Flask logs
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

if os.path.exists("config.json"):
    with open("config.json") as f:
        config = json.load(f)
else:
    config = {
        "web_port": int(os.environ.get("WEB_PORT", 5000)),
    }

# Get the parent directory (root of the project)
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

app = Flask(__name__)

@app.route("/")
def index():
    try:
        return send_from_directory(ROOT_DIR, "index.html")
    except Exception as e:
        return f"Error loading index.html: {e}", 500

@app.route("/<path:filename>")
def serve_file(filename):
    try:
        return send_from_directory(ROOT_DIR, filename)
    except FileNotFoundError:
        abort(404)
    except Exception as e:
        return f"Error loading {filename}: {e}", 500

def run():
    app.run(host='0.0.0.0', port=config["web_port"], debug=False, use_reloader=False)
