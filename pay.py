import os
import re
import io
import time
import email
import secrets
import sqlite3
import imaplib
import logging
import threading
import requests
from flask import Flask, request, jsonify
import telebot
from telebot import types

# ----------------- CONFIGURATION -----------------
BOT_TOKEN = "8737334045:AAFa_BKZATJ1gzauyDZWQee7BDSs84LMo9k"
ADMIN_ID = 5624448603

PORT = int(os.environ.get("PORT", 5000))
DB_PATH = "payment_hub.db"
IMAP_SERVER = "imap.gmail.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# User state tracker for interactive button inputs
admin_states = {}

# ----------------- DATABASE SETUP & MIGRATION -----------------
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS connected_bots (
                bot_id TEXT PRIMARY KEY,
                bot_name TEXT,
                api_key TEXT UNIQUE,
                bot_token TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS transactions (
                utr TEXT PRIMARY KEY,
                bot_id TEXT,
                user_id INTEGER,
                amount REAL,
                status TEXT DEFAULT 'SUCCESS',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS email_ledger (
                utr TEXT PRIMARY KEY,
                amount REAL,
                raw_text TEXT,
                claimed INTEGER DEFAULT 0,
                received_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Auto-migration for bot_token column
        try:
            conn.execute("ALTER TABLE connected_bots ADD COLUMN bot_token TEXT")
        except sqlite3.OperationalError:
            pass

        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('upi_id', 'not_set@fam')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('email_user', '')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('email_pass', '')")
    logging.info("Database initialized successfully.")

init_db()

# ----------------- KEYBOARD MARKUP -----------------
def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_upi = types.KeyboardButton("💳 Set UPI")
    btn_email = types.KeyboardButton("📧 Set Email")
    btn_api = types.KeyboardButton("🔑 Generate API")
    btn_connect = types.KeyboardButton("🔗 Connect Bot")
    btn_list = types.KeyboardButton("🤖 Bot List")
    btn_tx = types.KeyboardButton("📊 Transactions")
    btn_tutorial = types.KeyboardButton("📖 Tutorial")
    
    markup.add(btn_upi, btn_email)
    markup.add(btn_api, btn_connect)
    markup.add(btn_list, btn_tx)
    markup.add(btn_tutorial)
    return markup

# ----------------- DYNAMIC EMAIL IMAP WORKER -----------------
def extract_payment_details(text):
    amount_match = re.search(r"(?:Rs\.?|INR|₹)\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    utr_match = re.search(r"\b\d{12}\b", text)
    amount = None
    if amount_match:
        try:
            amount = float(amount_match.group(1).replace(",", ""))
        except ValueError:
            amount = None
    utr = utr_match.group(0) if utr_match else None
    return amount, utr

def imap_worker():
    while True:
        try:
            with get_db() as conn:
                user_row = conn.execute("SELECT value FROM settings WHERE key = 'email_user'").fetchone()
                pass_row = conn.execute("SELECT value FROM settings WHERE key = 'email_pass'").fetchone()
                email_user = user_row["value"] if user_row else ""
                email_pass = pass_row["value"] if pass_row else ""

            if not email_user or not email_pass:
                time.sleep(10)
                continue

            mail = imaplib.IMAP4_SSL(IMAP_SERVER)
            mail.login(email_user, email_pass)
            mail.select("inbox")

            status, messages = mail.search(None, '(UNSEEN)')
            if status == "OK" and messages[0]:
                for num in messages[0].split():
                    res, msg_data = mail.fetch(num, "(RFC822)")
                    for part in msg_data:
                        if isinstance(part, tuple):
                            msg = email.message_from_bytes(part[1])
                            body = ""
                            if msg.is_multipart():
                                for p in msg.walk():
                                    if p.get_content_type() == "text/plain":
                                        body = p.get_payload(decode=True).decode(errors="ignore")
                                        break
                            else:
                                body = msg.get_payload(decode=True).decode(errors="ignore")

                            amount, utr = extract_payment_details(body)
                            if amount and utr:
                                with get_db() as conn:
                                    try:
                                        conn.execute(
                                            "INSERT INTO email_ledger (utr, amount, raw_text, claimed) VALUES (?, ?, ?, 0)",
                                            (utr, amount, body[:150])
                                        )
                                        logging.info(f"Payment detected: ₹{amount} | UTR: {utr}")
                                    except sqlite3.IntegrityError:
                                        pass
            mail.logout()
        except Exception as e:
            logging.error(f"IMAP sync issue: {e}")
        time.sleep(10)

# ----------------- FLASK REST API ROUTES -----------------
@app.route("/")
def index():
    return jsonify({"status": "running", "service": "Payment Hub Engine"}), 200

@app.route("/api/get-upi", methods=["GET"])
def get_active_upi():
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return jsonify({"status": "error", "message": "Missing Authorization header"}), 401

    api_key = auth_header.replace("Bearer ", "").strip()
    with get_db() as conn:
        bot_row = conn.execute("SELECT bot_name FROM connected_bots WHERE api_key = ?", (api_key,)).fetchone()
        if not bot_row:
            return jsonify({"status": "error", "message": "Invalid API key"}), 403

        upi_row = conn.execute("SELECT value FROM settings WHERE key = 'upi_id'").fetchone()
        active_upi = upi_row["value"] if upi_row else "not_set@fam"

    return jsonify({"status": "success", "upi_id": active_upi}), 200

@app.route("/api/verify-utr", methods=["POST"])
def verify_utr():
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return jsonify({"status": "error", "message": "Missing Authorization header"}), 401

    api_key = auth_header.replace("Bearer ", "").strip()
    data = request.json or {}
    utr = str(data.get("utr", "")).strip()
    expected_amount = float(data.get("amount", 0))
    user_id = data.get("user_id")

    with get_db() as conn:
        bot_row = conn.execute("SELECT bot_id, bot_name FROM connected_bots WHERE api_key = ?", (api_key,)).fetchone()
        if not bot_row:
            return jsonify({"status": "error", "message": "Invalid API key"}), 403

        bot_id = bot_row["bot_id"]
        bot_name = bot_row["bot_name"]

        tx_row = conn.execute("SELECT utr FROM transactions WHERE utr = ?", (utr,)).fetchone()
        if tx_row:
            return jsonify({"status": "already_used", "message": "This UTR has already been claimed."}), 400

        ledger_row = conn.execute("SELECT amount, claimed FROM email_ledger WHERE utr = ?", (utr,)).fetchone()
        if not ledger_row:
            return jsonify({"status": "not_found", "message": "Transaction not found in ledger yet. Please try again in 30 seconds."}), 404

        received_amount = ledger_row["amount"]
        if ledger_row["claimed"] == 1:
            return jsonify({"status": "already_used", "message": "This UTR has already been claimed."}), 400

        if received_amount < expected_amount:
            return jsonify({
                "status": "insufficient_amount",
                "message": f"Paid ₹{received_amount}, expected ₹{expected_amount}"
            }), 400

        conn.execute("UPDATE email_ledger SET claimed = 1 WHERE utr = ?", (utr,))
        conn.execute("INSERT INTO transactions (utr, bot_id, user_id, amount) VALUES (?, ?, ?, ?)",
                     (utr, bot_id, user_id, received_amount))

    try:
        bot.send_message(
            ADMIN_ID,
            f"<b>✅ Payment Received & Verified!</b>\n\n"
            f"<b>Source Bot:</b> {bot_name} (<code>{bot_id}</code>)\n"
            f"<b>User ID:</b> <code>{user_id}</code>\n"
            f"<b>Amount:</b> ₹{received_amount:.2f}\n"
            f"<b>UTR:</b> <code>{utr}</code>",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logging.error(f"Telegram notification error: {e}")

    return jsonify({"status": "success", "amount": received_amount, "utr": utr}), 200

# ----------------- ADMIN TELEGRAM BOT -----------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Access denied.")
        return

    admin_states.pop(message.chat.id, None)

    with get_db() as conn:
        upi_row = conn.execute("SELECT value FROM settings WHERE key = 'upi_id'").fetchone()
        email_row = conn.execute("SELECT value FROM settings WHERE key = 'email_user'").fetchone()
        current_upi = upi_row["value"] if upi_row else "Not Set"
        current_email = email_row["value"] if (email_row and email_row["value"]) else "Not Configured"

    bot.send_message(
        message.chat.id,
        f"<b>🛡️ Payment Hub Control Panel</b>\n\n"
        f"<b>Active UPI:</b> <code>{current_upi}</code>\n"
        f"<b>Active Email:</b> <code>{current_email}</code>\n\n"
        "Choose an action from the menu keyboard below:",
        reply_markup=get_main_keyboard()
    )

# ----------------- MENU BUTTON HANDLER -----------------
@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and m.text in [
    "💳 Set UPI", "📧 Set Email", "🔑 Generate API", "🔗 Connect Bot", "🤖 Bot List", "📊 Transactions", "📖 Tutorial"
])
def handle_menu_buttons(message):
    chat_id = message.chat.id

    if message.text == "💳 Set UPI":
        admin_states[chat_id] = {"step": "AWAITING_UPI"}
        with get_db() as conn:
            upi_row = conn.execute("SELECT value FROM settings WHERE key = 'upi_id'").fetchone()
            curr = upi_row["value"] if upi_row else "Not Set"
        bot.send_message(
            chat_id,
            f"ℹ️ <b>Current UPI ID:</b> <code>{curr}</code>\n\n"
            "Please send your new UPI ID (e.g., <code>yourname@fam</code>):",
            reply_markup=get_main_keyboard()
        )

    elif message.text == "📧 Set Email":
        admin_states[chat_id] = {"step": "AWAITING_GMAIL"}
        with get_db() as conn:
            e_row = conn.execute("SELECT value FROM settings WHERE key = 'email_user'").fetchone()
            curr_e = e_row["value"] if (e_row and e_row["value"]) else "Not Configured"
        bot.send_message(
            chat_id,
            f"📧 <b>Current Alert Email:</b> <code>{curr_e}</code>\n\n"
            "Send the <b>Gmail address</b> that receives FamPay transaction alerts:",
            reply_markup=get_main_keyboard()
        )

    elif message.text == "🔑 Generate API":
        admin_states[chat_id] = {"step": "AWAITING_BOT_NAME_FOR_API"}
        bot.send_message(
            chat_id,
            "📝 <b>Enter Bot Name:</b>\n"
            "Send the name of the bot you want to generate an API key for:",
            reply_markup=get_main_keyboard()
        )

    elif message.text == "🔗 Connect Bot":
        admin_states[chat_id] = {"step": "AWAITING_BOT_TOKEN"}
        bot.send_message(
            chat_id,
            "🤖 <b>Enter Bot Token:</b>\n\n"
            "Send the Telegram Bot Token obtained from @BotFather:",
            reply_markup=get_main_keyboard()
        )

    elif message.text == "🤖 Bot List":
        admin_states.pop(chat_id, None)
        with get_db() as conn:
            bots = conn.execute("SELECT bot_id, bot_name FROM connected_bots").fetchall()

        if not bots:
            bot.send_message(chat_id, "🚫 No bots connected yet!", reply_markup=get_main_keyboard())
            return

        markup = types.InlineKeyboardMarkup(row_width=1)
        for b in bots:
            markup.add(types.InlineKeyboardButton(f"🤖 {b['bot_name']}", callback_data=f"view_{b['bot_id']}"))

        bot.send_message(chat_id, "📋 <b>Connected Bot List:</b>\n\nSelect a bot to view details or remove it:", reply_markup=markup)

    elif message.text == "📊 Transactions":
        admin_states.pop(chat_id, None)
        with get_db() as conn:
            stats = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM transactions").fetchone()
            latest = conn.execute("SELECT utr, bot_id, amount, timestamp FROM transactions ORDER BY timestamp DESC LIMIT 5").fetchall()

        msg = (
            f"<b>📊 Lifetime Transaction Report</b>\n\n"
            f"• <b>Total Count:</b> <code>{stats[0]}</code>\n"
            f"• <b>Total Volume:</b> <code>₹{stats[1]:.2f}</code>\n\n"
            f"<b>🕒 Last 5 Transactions:</b>\n"
        )
        if latest:
            for tx in latest:
                msg += f"▫️ ₹{tx['amount']:.2f} | UTR: <code>{tx['utr']}</code> | Bot: <code>{tx['bot_id']}</code>\n"
        else:
            msg += "<i>No transaction records found.</i>"

        bot.send_message(chat_id, msg, reply_markup=get_main_keyboard())

    elif message.text == "📖 Tutorial":
        admin_states.pop(chat_id, None)
        tutorial_text = (
            "<b>📖 Complete Setup Tutorial (A-Z Guide)</b>\n\n"
            "<b>1. Generating Google 16-Digit App Password:</b>\n"
            "• Open your browser and go to: <a href='https://myaccount.google.com/security'>Google Security</a>.\n"
            "• Make sure <b>2-Step Verification</b> is turned <b>ON</b>.\n"
            "• Search for <b>App passwords</b> (or visit: <a href='https://myaccount.google.com/apppasswords'>Direct Link</a>).\n"
            "• Enter an app name (e.g. <code>Payment Bot</code>) and click <b>Create</b>.\n"
            "• Copy the 16-character code (e.g. <code>abcd efgh ijkl mnop</code>).\n\n"
            "<b>2. Connecting Email & UPI:</b>\n"
            "• Tap <b>📧 Set Email</b>: Send your FamPay registered Gmail, then send the 16-digit App Password.\n"
            "• Tap <b>💳 Set UPI</b>: Send your FamPay UPI ID (e.g. <code>username@fam</code>).\n\n"
            "<b>3. Connecting Client Bots:</b>\n"
            "• Tap <b>🔗 Connect Bot</b> and send the client bot's token from @BotFather.\n"
            "• The bot will verify it and issue an <code>API Key</code>.\n"
            "• Paste that <code>API Key</code> into your client bot python code.\n\n"
            "<b>4. How Verification Works:</b>\n"
            "• Client bot generates a dynamic QR code using your active UPI ID.\n"
            "• When a user pays, FamPay sends an email alert to your Gmail.\n"
            "• This bot's background worker parses the UTR and amount from the email.\n"
            "• As soon as the user enters the 12-digit UTR, payment is instantly verified and credited!"
        )
        bot.send_message(chat_id, tutorial_text, reply_markup=get_main_keyboard(), disable_web_page_preview=True)

# ----------------- ADMIN INPUT TEXT ROUTER -----------------
@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and m.chat.id in admin_states)
def handle_admin_text_inputs(message):
    chat_id = message.chat.id
    state_data = admin_states.get(chat_id, {})
    step = state_data.get("step")
    text = message.text.strip()

    if step == "AWAITING_UPI":
        admin_states.pop(chat_id, None)
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('upi_id', ?)", (text,))
        bot.send_message(chat_id, f"✅ <b>UPI ID Updated:</b> <code>{text}</code>", reply_markup=get_main_keyboard())

    elif step == "AWAITING_GMAIL":
        if "@" not in text or "." not in text:
            bot.send_message(chat_id, "❌ Please send a valid email address (e.g., <code>user@gmail.com</code>):")
            return
        admin_states[chat_id] = {"step": "AWAITING_APP_PASS", "email": text}
        bot.send_message(
            chat_id,
            f"🔑 <b>Enter 16-Digit Google App Password:</b>\n\n"
            f"Selected email: <code>{text}</code>\n\n"
            "Please paste the 16-character App Password generated from Google Security:"
        )

    elif step == "AWAITING_APP_PASS":
        app_password = text.replace(" ", "")
        saved_email = state_data.get("email")
        admin_states.pop(chat_id, None)

        wait_msg = bot.send_message(chat_id, "⏳ Testing Gmail IMAP authentication...")
        try:
            test_mail = imaplib.IMAP4_SSL(IMAP_SERVER)
            test_mail.login(saved_email, app_password)
            test_mail.logout()

            with get_db() as conn:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('email_user', ?)", (saved_email,))
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('email_pass', ?)", (app_password,))

            bot.edit_message_text(
                f"<b>🎉 Email Connected & Verified!</b>\n\n"
                f"<b>Email:</b> <code>{saved_email}</code>\n\n"
                "The background engine is now listening for incoming FamPay transaction alerts.",
                chat_id,
                wait_msg.message_id
            )
        except Exception as e:
            bot.edit_message_text(
                f"❌ <b>Authentication Failed:</b> <code>{e}</code>\n\n"
                "Incorrect email or App Password. Tap '📧 Set Email' to try again.",
                chat_id,
                wait_msg.message_id
            )

    elif step == "AWAITING_BOT_NAME_FOR_API":
        admin_states.pop(chat_id, None)
        bot_name = text
        bot_id = f"bot_{secrets.token_hex(4)}"
        api_key = f"hub_{secrets.token_hex(16)}"

        with get_db() as conn:
            conn.execute(
                "INSERT INTO connected_bots (bot_id, bot_name, api_key) VALUES (?, ?, ?)",
                (bot_id, bot_name, api_key)
            )

        bot.send_message(
            chat_id,
            f"<b>🔑 API Key Generated Successfully!</b>\n\n"
            f"<b>Bot Name:</b> {bot_name}\n"
            f"<b>Bot ID:</b> <code>{bot_id}</code>\n\n"
            f"<b>API Key:</b>\n<code>{api_key}</code>\n\n"
            "<i>Use this key as HUB_API_KEY inside your client bot script.</i>",
            reply_markup=get_main_keyboard()
        )

    elif step == "AWAITING_BOT_TOKEN":
        admin_states.pop(chat_id, None)
        bot_token = text

        wait_msg = bot.send_message(chat_id, "⏳ Verifying bot token with Telegram...")
        try:
            res = requests.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=8).json()
            if not res.get("ok"):
                bot.edit_message_text("❌ Invalid Bot Token! Please provide a valid token from @BotFather.", chat_id, wait_msg.message_id)
                return

            client_name = res["result"].get("first_name", "Telegram Bot")
            client_username = res["result"].get("username", "Unknown")
            bot_id = f"bot_{secrets.token_hex(4)}"
            api_key = f"hub_{secrets.token_hex(16)}"

            with get_db() as conn:
                conn.execute(
                    "INSERT INTO connected_bots (bot_id, bot_name, api_key, bot_token) VALUES (?, ?, ?, ?)",
                    (bot_id, f"{client_name} (@{client_username})", api_key, bot_token)
                )

            bot.edit_message_text(
                f"<b>🎉 Bot Successfully Connected!</b>\n\n"
                f"<b>Name:</b> {client_name}\n"
                f"<b>Username:</b> @{client_username}\n"
                f"<b>Assigned Bot ID:</b> <code>{bot_id}</code>\n\n"
                f"<b>Generated API Key:</b>\n<code>{api_key}</code>",
                chat_id,
                wait_msg.message_id
            )
        except Exception as e:
            bot.edit_message_text(f"⚠️ Error verifying bot: {e}", chat_id, wait_msg.message_id)

# ----------------- INLINE CALLBACKS FOR BOT LIST & DELETE -----------------
@bot.callback_query_handler(func=lambda call: call.data.startswith("view_"))
def handle_view_bot(call):
    bot_id = call.data.replace("view_", "", 1)
    with get_db() as conn:
        bot_info = conn.execute("SELECT bot_name, created_at, api_key FROM connected_bots WHERE bot_id = ?", (bot_id,)).fetchone()
        if not bot_info:
            bot.answer_callback_query(call.id, "Bot not found.")
            return

        stats = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM transactions WHERE bot_id = ?", (bot_id,)
        ).fetchone()

    total_tx = stats[0]
    total_rev = stats[1]

    text = (
        f"<b>🤖 Bot Details: {bot_info['bot_name']}</b>\n\n"
        f"<b>ID:</b> <code>{bot_id}</code>\n"
        f"<b>API Key:</b> <code>{bot_info['api_key']}</code>\n"
        f"<b>Connected On:</b> {bot_info['created_at']}\n\n"
        f"<b>📊 Lifetime Stats:</b>\n"
        f"• Transactions: <code>{total_tx}</code>\n"
        f"• Total Volume: <code>₹{total_rev:.2f}</code>"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗑 Delete Bot", callback_data=f"del_{bot_id}"))
    markup.add(types.InlineKeyboardButton("⬅️ Back to List", callback_data="back_list"))

    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("del_"))
def handle_del_bot(call):
    bot_id = call.data.replace("del_", "", 1)
    with get_db() as conn:
        conn.execute("DELETE FROM connected_bots WHERE bot_id = ?", (bot_id,))
    bot.answer_callback_query(call.id, "Bot deleted successfully!")
    handle_back_list(call)

@bot.callback_query_handler(func=lambda call: call.data == "back_list")
def handle_back_list(call):
    with get_db() as conn:
        bots = conn.execute("SELECT bot_id, bot_name FROM connected_bots").fetchall()

    if not bots:
        bot.edit_message_text("🚫 No bots connected yet!", call.message.chat.id, call.message.message_id)
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for b in bots:
        markup.add(types.InlineKeyboardButton(f"🤖 {b['bot_name']}", callback_data=f"view_{b['bot_id']}"))

    bot.edit_message_text("📋 <b>Connected Bot List:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup)

# ----------------- BACKGROUND RUNNERS -----------------
def start_bot_polling():
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20)
        except Exception as e:
            logging.error(f"Polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=imap_worker, daemon=True).start()
    threading.Thread(target=start_bot_polling, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
