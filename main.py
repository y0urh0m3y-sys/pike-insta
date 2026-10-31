import os
import gc
import time
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from instagrapi import Client
from dotenv import load_dotenv

# Load local environment variables
load_dotenv()

# Setup terminal logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ----------------- Configuration -----------------
USERNAME = os.getenv("IG_USERNAME", "pike.klaxon")
PASSWORD = os.getenv("IG_PASSWORD", "205259")
PORT = int(os.environ.get("PORT", 10000))
SESSION_PATH = "session.json"

DM_REPLY_TEMPLATE = "Hey @{username}! I'm away right now, will reply shortly."
GC_REPLY_TEMPLATE = "Hey {mentions}! Got your message: '{last_msg}'."
# --------------------------------------------------


# --- 1. Render Health-Check Server (Main Thread) ---
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
        return

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --- 2. Instagram Bot Worker (Background Thread) ---
def run_instagram_bot():
    logging.info("Starting Instagram bot worker in background...")
    time.sleep(2)

    cl = Client()
    cl.request_timeout = 25

    # Bypass "version out of date" error with latest Android headers
    cl.set_user_agent(
        "Instagram 320.0.0.35.108 Android (34/14; 420dpi; 1080x2400; Google/google; Pixel 8; shiba; shiba; en_US; 578023194)"
    )
    cl.set_country("US")
    cl.set_locale("en_US")
    cl.set_timezone_offset(-5 * 3600)

    # Login flow
    logged_in = False
    if os.path.exists(SESSION_PATH):
        try:
            cl.load_settings(SESSION_PATH)
            cl.login(USERNAME, PASSWORD)
            logged_in = True
            logging.info("Logged in using existing session.json")
        except Exception as e:
            logging.warning(f"Cached session expired ({e}). Attempting fresh login...")

    if not logged_in:
        try:
            logging.info(f"Logging in fresh for @{USERNAME} with updated Android headers...")
            cl.login(USERNAME, PASSWORD)
            cl.dump_settings(SESSION_PATH)
            logging.info("Fresh login successful. Session saved.")
        except Exception as e:
            logging.error(f"INSTAGRAM LOGIN FAILED: {e}")
            logging.error("If challenge/2FA prompted, approve 'This was me' on your phone app!")
            return

    bot_user_id = str(cl.user_id)
    logging.info(f"Bot successfully hooked for @{USERNAME} (ID: {bot_user_id})")

    # Realtime message event handler
    def handle_message(message):
        try:
            sender_id = str(getattr(message, "user_id", ""))
            if sender_id == bot_user_id or not sender_id:
                return

            thread_id = getattr(message, "thread_id", None)
            if not thread_id:
                return

            logging.info(f"New incoming message in thread {thread_id} from {sender_id}")

            thread = cl.direct_thread(thread_id)
            if not thread.is_group:
                user = thread.users[0] if thread.users else None
                uname = user.username if user else "there"
                cl.direct_send(DM_REPLY_TEMPLATE.format(username=uname), thread_ids=[thread_id])
                logging.info(f"[Instant DM] -> Replied to @{uname}")
            else:
                user_map = {str(u.pk): u.username for u in thread.users}
                uname = user_map.get(sender_id, "member")
                text = (getattr(message, "text", "") or "[media]")[:35]
                cl.direct_send(GC_REPLY_TEMPLATE.format(mentions=f"@{uname}", last_msg=text), thread_ids=[thread_id])
                logging.info(f"[Instant GC] -> Replied to @{uname}")

            del thread
        except Exception as err:
            logging.error(f"Message handling error: {err}")
        finally:
            gc.collect()

    # Reconnect loop (zero crash on socket drop)
    while True:
        try:
            logging.info("MQTT Push listener active. Waiting for messages in real time...")
            cl.start_realtime(handle_message)
        except Exception as err:
            logging.warning(f"MQTT disconnected: {err}. Reconnecting in 10s...")
            time.sleep(10)


def main():
    # 1. Start Instagram worker thread
    bot_thread = threading.Thread(target=run_instagram_bot, daemon=True)
    bot_thread.start()

    # 2. Start HTTP Health server on Main thread
    try:
        server = ThreadedHTTPServer(("0.0.0.0", PORT), HealthHandler)
        logging.info(f"Health check server LIVE on port {PORT} (Main Thread)")
        server.serve_forever()
    except Exception as e:
        logging.error(f"Failed to start health server: {e}")


if __name__ == "__main__":
    main()
