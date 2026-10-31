import os
import gc
import time
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from instagrapi import Client
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ----------------- Configuration -----------------
USERNAME = os.getenv("IG_USERNAME", "pike.klaxon")
PASSWORD = os.getenv("IG_PASSWORD", "205259")
# Render hamesha PORT dynamic deta hai, fallback 10000
PORT = int(os.environ.get("PORT", 10000))
SESSION_PATH = "session.json"

DM_REPLY_TEMPLATE = "Hey @{username}! I'm away right now, will reply shortly."
GC_REPLY_TEMPLATE = "Hey {mentions}! Got your message: '{last_msg}'."
# --------------------------------------------------


# --- 1. Bulletproof Web Server (Main Thread) ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Mute logs to keep stdout clean
        return

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --- 2. Instagram Bot Worker (Background Thread) ---
def run_instagram_bot():
    logging.info("Starting Instagram bot worker in background...")
    # Thoda wait taaki port bind confirm ho jaye
    time.sleep(2)

    cl = Client()
    cl.request_timeout = 20

    # Login flow
    logged_in = False
    if os.path.exists(SESSION_PATH):
        try:
            cl.load_settings(SESSION_PATH)
            cl.login(USERNAME, PASSWORD)
            logged_in = True
            logging.info("Logged in using existing session.json")
        except Exception as e:
            logging.warning(f"Session login failed: {e}. Trying fresh login...")

    if not logged_in:
        try:
            logging.info(f"Logging in fresh for @{USERNAME}...")
            cl.login(USERNAME, PASSWORD)
            cl.dump_settings(SESSION_PATH)
            logging.info("Fresh login successful. Session saved.")
        except Exception as e:
            logging.error(f"INSTAGRAM LOGIN FAILED: {e}")
            logging.error("If it asks for Challenge/2FA, approve it on your phone app!")
            return

    bot_user_id = str(cl.user_id)
    logging.info(f"Bot successfully hooked for @{USERNAME} (ID: {bot_user_id})")

    def on_direct_message(message):
        try:
            sender_id = str(getattr(message, "user_id", ""))
            if sender_id == bot_user_id:
                return

            thread_id = getattr(message, "thread_id", None)
            if not thread_id:
                return

            thread = cl.direct_thread(thread_id)
            if not thread.is_group:
                user = thread.users[0] if thread.users else None
                uname = user.username if user else "there"
                cl.direct_send(DM_REPLY_TEMPLATE.format(username=uname), thread_ids=[thread_id])
                logging.info(f"[Instant DM] -> Replied to @{uname}")
            else:
                user_map = {str(u.pk): u.username for u in thread.users}
                uname = user_map.get(sender_id, "member")
                text = (message.text or "[media]")[:35]
                cl.direct_send(GC_REPLY_TEMPLATE.format(mentions=f"@{uname}", last_msg=text), thread_ids=[thread_id])
                logging.info(f"[Instant GC] -> Replied to @{uname}")

            del thread
            del message
        except Exception as err:
            logging.error(f"Message handling error: {err}")
        finally:
            gc.collect()

    while True:
        try:
            logging.info("MQTT Push listener active. Waiting for messages...")
            cl.start_realtime(on_direct_message)
        except Exception as err:
            logging.warning(f"MQTT disconnected: {err}. Reconnecting in 10s...")
            time.sleep(10)


def main():
    # 1. Instagram bot ko background daemon thread me daalo
    bot_thread = threading.Thread(target=run_instagram_bot, daemon=True)
    bot_thread.start()

    # 2. Main thread par Web Server bind karo (Render ko 0.1 sec me signal mil jayega)
    try:
        server = ThreadedHTTPServer(("0.0.0.0", PORT), HealthHandler)
        logging.info(f"Health server bound to port {PORT} on MAIN thread. Render will NOT kill this.")
        server.serve_forever()
    except Exception as e:
        logging.error(f"Failed to start health server: {e}")


if __name__ == "__main__":
    main()
