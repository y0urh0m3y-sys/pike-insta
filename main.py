import os
import gc
import time
import random
import logging
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from instagrapi import Client
from dotenv import load_dotenv

# Load local .env if present
load_dotenv()

# Setup logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ----------------- Configuration -----------------
USERNAME = os.getenv("IG_USERNAME", "pike.klaxon")
PASSWORD = os.getenv("IG_PASSWORD", "205259")
PORT = int(os.getenv("PORT", 10000))
DB_PATH = "bot_state.db"
SESSION_PATH = "session.json"

DM_REPLY_TEMPLATE = "Hey @{username}! Thanks for reaching out. I'm away right now, will reply shortly."
GC_REPLY_TEMPLATE = "Hey {mentions}! Got your message: '{last_msg}'."

POLL_INTERVAL_MIN = 30
POLL_INTERVAL_MAX = 55
# --------------------------------------------------


# ----------------- 1. Lightweight Ping Server -----
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is healthy and polling.")

    def log_message(self, format, *args):
        # Silence access logs to prevent stdout flooding
        return


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    logging.info(f"Health server running on port {PORT}")
    server.serve_forever()


# ----------------- 2. SQLite State & Memory Pruning
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                msg_id TEXT PRIMARY KEY,
                timestamp REAL
            )
        """)
        conn.commit()


def is_processed(msg_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM processed_messages WHERE msg_id = ?", (str(msg_id),))
        return cur.fetchone() is not None


def mark_processed(msg_ids):
    if not msg_ids:
        return
    now = time.time()
    records = [(str(mid), now) for mid in msg_ids]
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany("INSERT OR IGNORE INTO processed_messages VALUES (?, ?)", records)
        conn.commit()


def prune_old_db_records(max_age_seconds=172800):
    """Deletes records older than 48 hours to prevent unbounded disk growth."""
    cutoff = time.time() - max_age_seconds
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM processed_messages WHERE timestamp < ?", (cutoff,))
        conn.commit()


# ----------------- 3. Instagram Engine ------------
def login_client(username, password, session_file=SESSION_PATH):
    cl = Client()
    cl.request_timeout = 15

    if os.path.exists(session_file):
        try:
            cl.load_settings(session_file)
            cl.login(username, password)
            logging.info("Logged in using existing session.json")
            return cl
        except Exception as err:
            logging.warning(f"Saved session rejected ({err}). Logging in fresh...")

    cl.login(username, password)
    cl.dump_settings(session_file)
    logging.info("New session saved to session.json")
    return cl


def process_threads(cl, bot_user_id):
    try:
        # Fetching a small batch keeps memory footprint around 60MB
        threads = cl.direct_threads(amount=6)
    except Exception as e:
        logging.error(f"Error fetching threads: {e}")
        return

    for thread in threads:
        thread_id = thread.id
        is_group = thread.is_group
        messages = thread.messages

        if not messages:
            continue

        latest_msg = messages[0]
        msg_id = str(latest_msg.id)
        sender_id = str(latest_msg.user_id)

        # Skip if already replied or if the bot itself sent the message
        if is_processed(msg_id) or sender_id == str(bot_user_id):
            continue

        # ---------------- Direct Message ----------------
        if not is_group:
            sender_user = thread.users[0] if thread.users else None
            sender_username = sender_user.username if sender_user else "there"
            reply_text = DM_REPLY_TEMPLATE.format(username=sender_username)

            try:
                cl.direct_send(reply_text, thread_ids=[thread_id])
                logging.info(f"[DM] Replied to @{sender_username}")
                mark_processed([msg_id])
            except Exception as e:
                logging.error(f"Failed to reply to DM {thread_id}: {e}")

        # ---------------- Group Chat ----------------
        else:
            recent_unanswered = []
            for msg in messages[:4]:
                if is_processed(msg.id):
                    break
                if str(msg.user_id) != str(bot_user_id):
                    recent_unanswered.append(msg)

            if not recent_unanswered:
                continue

            user_map = {str(u.pk): u.username for u in thread.users}
            unique_senders = list({str(m.user_id) for m in recent_unanswered})

            mentions = [
                f"@{user_map[uid]}"
                for uid in unique_senders
                if uid in user_map
            ]

            if not mentions:
                continue

            mentions_str = " ".join(mentions)
            last_text = (latest_msg.text or "[media]")[:40]

            reply_text = GC_REPLY_TEMPLATE.format(
                mentions=mentions_str,
                last_msg=last_text
            )

            try:
                cl.direct_send(reply_text, thread_ids=[thread_id])
                logging.info(f"[GC] Mentioned {mentions_str} in thread {thread_id}")
                mark_processed([m.id for m in recent_unanswered])
            except Exception as e:
                logging.error(f"Failed to reply to GC {thread_id}: {e}")


def main():
    init_db()

    # Spin up health server thread
    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()

    cl = login_client(USERNAME, PASSWORD)
    bot_user_id = cl.user_id
    logging.info(f"Bot active for @{USERNAME} (ID: {bot_user_id})")

    # Initial sync to ignore existing unread messages prior to boot
    try:
        initial_threads = cl.direct_threads(amount=6)
        initial_ids = [t.messages[0].id for t in initial_threads if t.messages]
        mark_processed(initial_ids)
        del initial_threads
    except Exception as e:
        logging.warning(f"Initial sync warning: {e}")

    loop_counter = 0

    while True:
        try:
            process_threads(cl, bot_user_id)
        except Exception as e:
            logging.error(f"Unexpected loop exception: {e}")

        # Prune old records every 100 cycles (~1 hour)
        loop_counter += 1
        if loop_counter >= 100:
            prune_old_db_records()
            loop_counter = 0

        # Run garbage collector to clear request heap
        gc.collect()

        sleep_time = random.uniform(POLL_INTERVAL_MIN, POLL_INTERVAL_MAX)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
