import os
import gc
import time
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from instagrapi import Client
from dotenv import load_dotenv

# Load local environment variables if available
load_dotenv()

# Setup clean terminal logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ----------------- Configuration -----------------
USERNAME = os.getenv("IG_USERNAME", "pike.klaxon")
PASSWORD = os.getenv("IG_PASSWORD", "205259")
PORT = int(os.getenv("PORT", 10000))
SESSION_PATH = "session.json"

DM_REPLY_TEMPLATE = "Hey @{username}! I'm away right now, will reply shortly."
GC_REPLY_TEMPLATE = "Hey {mentions}! Got your message: '{last_msg}'."
# --------------------------------------------------


# ----------------- 1. Ultra-Light Ping Server -----
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        """Zero-byte response for cron monitoring tools."""
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        """Minimal 2-byte response to prevent 'Output too large' errors."""
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Mute access logs to prevent memory/log clutter
        return


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    logging.info(f"Keep-alive health server running on port {PORT}")
    server.serve_forever()


# ----------------- 2. Event Handler ----------------
def handle_incoming_message(cl, bot_user_id, thread_id, message):
    """Instantly processes incoming message and purges objects from memory."""
    try:
        sender_id = str(message.user_id)

        # Ignore messages sent by the bot account itself
        if sender_id == str(bot_user_id):
            return

        thread = cl.direct_thread(thread_id)
        is_group = thread.is_group

        # Direct Messages (DM)
        if not is_group:
            sender_user = thread.users[0] if thread.users else None
            sender_username = sender_user.username if sender_user else "there"
            reply_text = DM_REPLY_TEMPLATE.format(username=sender_username)
            cl.direct_send(reply_text, thread_ids=[thread_id])
            logging.info(f"[Instant DM] Replied to @{sender_username}")

        # Group Chats (GC)
        else:
            user_map = {str(u.pk): u.username for u in thread.users}
            username = user_map.get(sender_id, "member")
            last_text = (message.text or "[media]")[:40]

            reply_text = GC_REPLY_TEMPLATE.format(
                mentions=f"@{username}",
                last_msg=last_text
            )
            cl.direct_send(reply_text, thread_ids=[thread_id])
            logging.info(f"[Instant GC] Replied to @{username} in thread {thread_id}")

        # Clear local scope references immediately
        del thread
        del message

    except Exception as e:
        logging.error(f"Error handling incoming message: {e}")

    finally:
        # Force Python garbage collector to free RAM instantly
        gc.collect()


# ----------------- 3. Client Session & Engine -----
def login_client(username, password):
    cl = Client()
    cl.request_timeout = 15

    if os.path.exists(SESSION_PATH):
        try:
            cl.load_settings(SESSION_PATH)
            cl.login(username, password)
            logging.info("Successfully authenticated with cached session.json")
            return cl
        except Exception as err:
            logging.warning(f"Cached session rejected ({err}). Logging in fresh...")

    cl.login(username, password)
    cl.dump_settings(SESSION_PATH)
    logging.info("Saved fresh session.json")
    return cl


def main():
    # Start web keep-alive server daemon
    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()

    # Initial login
    cl = login_client(USERNAME, PASSWORD)
    bot_user_id = cl.user_id
    logging.info(f"Bot connected for @{USERNAME} (ID: {bot_user_id})")

    # Define message callback
    def on_direct_message(message):
        thread_id = getattr(message, "thread_id", None)
        if thread_id:
            handle_incoming_message(cl, bot_user_id, thread_id, message)

    # Infinite auto-reconnect loop to prevent crashes on network drops
    backoff_delay = 5
    while True:
        try:
            logging.info("Realtime push listener active. Waiting for incoming messages...")
            # Blocking realtime connection (uses ~0% CPU while idle)
            cl.start_realtime(on_direct_message)
        except Exception as e:
            logging.warning(f"Connection dropped: {e}. Reconnecting in {backoff_delay} seconds...")
            time.sleep(backoff_delay)
            # Try to re-establish session if dropped
            try:
                cl = login_client(USERNAME, PASSWORD)
                bot_user_id = cl.user_id
                backoff_delay = 5  # Reset backoff on success
            except Exception as login_err:
                logging.error(f"Re-login failed: {login_err}")
                backoff_delay = min(backoff_delay * 2, 60)  # Exponential backoff up to 1 min


if __name__ == "__main__":
    main()
