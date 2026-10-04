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
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import telebot
from telebot import types

# ----------------- CONFIGURATION -----------------
BOT_TOKEN = "8737334045:AAFa_BKZATJ1gzauyDZWQee7BDSs84LMo9k"
ADMIN_ID = 5624448603
BOT_USERNAME = "BotVerse_Pay_Bot"  # Payment bot-er username (@ chara)

PORT = int(os.environ.get("PORT", 5000))
DB_PATH = "payment_hub.db"
IMAP_SERVER = "imap.gmail.com"

# Render-e running thaka Master Admin Bot-er URL (shesh-e slash chara)
ADMIN_API_URL = "https://botverse-admin-bot.onrender.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

admin_states = {}

# ----------------- DATABASE SETUP -----------------
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
                owner_id INTEGER,
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

            CREATE TABLE IF NOT EXISTS user_subscriptions (
                user_id INTEGER PRIMARY KEY,
                expires_at TIMESTAMP
            );
        """)

        # Auto migration check
        try:
            conn.execute("ALTER TABLE connected_bots ADD COLUMN owner_id INTEGER")
        except sqlite3.OperationalError:
            pass

        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('upi_id', 'not_set@fam')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('email_user', '')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('email_pass', '')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('sub_price', '99')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('sub_days', '30')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_upi', 'admin@fam')")
    logging.info("Database initialized successfully.")

init_db()

# ----------------- SETTINGS & SUBSCRIPTION HELPERS -----------------
def get_setting(key, default=""):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))

# Real-time Master Admin Bot theke settings ana
def fetch_master_settings():
    try:
        res = requests.get(f"{ADMIN_API_URL}/api/get_settings/{BOT_USERNAME}", timeout=5)
        if res.status_code == 200:
            settings = res.json().get("settings", {})
            for k, v in settings.items():
                set_setting(k, v)
    except Exception:
        pass

def is_subscribed(user_id):
    if user_id == ADMIN_ID:
        return True, "Lifetime (Super Admin)"
    with get_db() as conn:
        row = conn.execute("SELECT expires_at FROM user_subscriptions WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            try:
                exp_date = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
                if exp_date > datetime.now():
                    return True, row["expires_at"]
            except Exception:
                pass
    return False, None

# ----------------- KEYBOARD MARKUP -----------------
# User jodi subscribed hoy ba owner hoy
def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_upi = types.KeyboardButton("💳 Set UPI")
    btn_email = types.KeyboardButton("📧 Set Email")
    btn_api = types.KeyboardButton("🔑 Generate API")
    btn_connect = types.KeyboardButton("🔗 Connect Bot")
    btn_list = types.KeyboardButton("🤖 Bot List")
    btn_tx = types.KeyboardButton("📊 Transactions")
    btn_sub = types.KeyboardButton("📅 YOUR SUBSCRIPTION")
    btn_tutorial = types.KeyboardButton("📖 Tutorial")
    
    markup.add(btn_upi, btn_email)
    markup.add(btn_api, btn_connect)
    markup.add(btn_list, btn_tx)
    markup.add(btn_sub, btn_tutorial)
    return markup

# User subscribed na thakle locked keyboard
def get_unsubscribed_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    btn_buy = types.KeyboardButton("💎 BUY SUBSCRIPTION 💎")
    btn_sub = types.KeyboardButton("📅 YOUR SUBSCRIPTION")
    markup.add(btn_buy, btn_sub)
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
            email_user = get_setting("email_user")
            email_pass = get_setting("email_pass")

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
        bot_row = conn.execute("SELECT bot_name, owner_id FROM connected_bots WHERE api_key = ?", (api_key,)).fetchone()
        if not bot_row:
            return jsonify({"status": "error", "message": "Invalid API key"}), 403

        # Sub check for the bot owner
        subbed, _ = is_subscribed(bot_row["owner_id"] if bot_row["owner_id"] else ADMIN_ID)
        if not subbed:
            return jsonify({"status": "error", "message": "Bot owner subscription expired!"}), 403

        active_upi = get_setting("upi_id", "not_set@fam")

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
        bot_row = conn.execute("SELECT bot_id, bot_name, owner_id FROM connected_bots WHERE api_key = ?", (api_key,)).fetchone()
        if not bot_row:
            return jsonify({"status": "error", "message": "Invalid API key"}), 403

        subbed, _ = is_subscribed(bot_row["owner_id"] if bot_row["owner_id"] else ADMIN_ID)
        if not subbed:
            return jsonify({"status": "error", "message": "Service disabled: subscription expired."}), 403

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

# ----------------- ADMIN MASTER BOT CONNECT (/api) -----------------
@bot.message_handler(commands=["api"])
def cmd_api_register(message):
    if message.from_user.id != ADMIN_ID:
        return

    code = f"PAY_{secrets.token_hex(3).upper()}"
    default_config = {
        "sub_price": int(get_setting("sub_price", 99)),
        "sub_days": int(get_setting("sub_days", 30)),
        "admin_upi": get_setting("admin_upi", "admin@fam")
    }

    payload = {
        "auth_code": code,
        "bot_name": "UPI Payment Hub Bot",
        "bot_username": BOT_USERNAME,
        "settings": default_config
    }

    try:
        res = requests.post(f"{ADMIN_API_URL}/api/register", json=payload, timeout=8)
        if res.status_code == 200:
            bot.reply_to(
                message,
                f"✅ <b>Payment Bot Connected to Admin Hub!</b>\n\n"
                f"🔑 Code: <code>{code}</code>\n\n"
                f"Now send this command in your Master Admin Bot:\n"
                f"<code>/add {code}</code>",
                parse_mode="HTML"
            )
        else:
            bot.reply_to(message, f"❌ Master Server Error: HTTP {res.status_code}")
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to reach Master Server: {e}")

# ----------------- TELEGRAM BOT HANDLER -----------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id
    admin_states.pop(message.chat.id, None)
    fetch_master_settings()

    subscribed, exp_date = is_subscribed(user_id)

    if subscribed:
        current_upi = get_setting("upi_id", "Not Set")
        current_email = get_setting("email_user", "Not Configured")
        bot.send_message(
            message.chat.id,
            f"<b>🛡️ Payment Hub Control Panel</b>\n\n"
            f"<b>Subscription:</b> Active (Till {exp_date})\n"
            f"<b>Active UPI:</b> <code>{current_upi}</code>\n"
            f"<b>Active Email:</b> <code>{current_email}</code>\n\n"
            "Choose an action from the menu keyboard below:",
            reply_markup=get_main_keyboard()
        )
    else:
        price = get_setting("sub_price", "99")
        days = get_setting("sub_days", "30")
        bot.send_message(
            message.chat.id,
            f"🔒 <b>Welcome to Payment Hub Engine!</b>\n\n"
            f"Ei bot use kore apni apnar Telegram bot-e automated UPI & UTR payment verify system add korte parben.\n\n"
            f"📌 <b>Subscription Plan:</b>\n"
            f"• <b>Price:</b> ₹{price}\n"
            f"• <b>Validity:</b> {days} Days\n\n"
            f"Full access unlock korte nicher boro <b>💎 BUY SUBSCRIPTION 💎</b> button-e click korun:",
            reply_markup=get_unsubscribed_keyboard()
        )

# ----------------- MENU BUTTON HANDLER -----------------
@bot.message_handler(func=lambda m: m.text in [
    "💎 BUY SUBSCRIPTION 💎", "📅 YOUR SUBSCRIPTION",
    "💳 Set UPI", "📧 Set Email", "🔑 Generate API", "🔗 Connect Bot", "🤖 Bot List", "📊 Transactions", "📖 Tutorial"
])
def handle_menu_buttons(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    fetch_master_settings()

    # 1. Buy Subscription Button
    if message.text == "💎 BUY SUBSCRIPTION 💎":
        price = get_setting("sub_price", "99")
        days = get_setting("sub_days", "30")
        admin_upi = get_setting("admin_upi", "admin@fam")

        msg = (
            f"💎 <b>Payment Hub VIP Access</b>\n\n"
            f"• <b>Price:</b> ₹{price}\n"
            f"• <b>Validity:</b> {days} Days\n"
            f"• <b>Admin UPI:</b> <code>{admin_upi}</code>\n\n"
            f"👉 Uporer UPI ID-te ₹{price} send korun.\n"
            f"Payment successful hole apnar 12-digit UTR number submit korun evabe:\n"
            f"<code>/utr 123456789012</code>"
        )
        bot.send_message(chat_id, msg, parse_mode="HTML")
        return

    # 2. Check Subscription
    elif message.text == "📅 YOUR SUBSCRIPTION":
        subscribed, exp_date = is_subscribed(user_id)
        if subscribed:
            bot.send_message(
                chat_id,
                f"✅ <b>Subscription Status: ACTIVE</b>\n\n"
                f"⏳ Expiration Date: <code>{exp_date}</code>\n"
                f"Apnar service smoothly running ache!",
                parse_mode="HTML"
            )
        else:
            bot.send_message(
                chat_id,
                "❌ <b>Subscription Status: INACTIVE</b>\n\n"
                "Apnar kono active plan nei. Access pete '💎 BUY SUBSCRIPTION 💎' button-e click korun.",
                reply_markup=get_unsubscribed_keyboard(),
                parse_mode="HTML"
            )
        return

    # Check lock for general hub features
    subscribed, _ = is_subscribed(user_id)
    if not subscribed:
        bot.send_message(
            chat_id,
            "⛔ <b>Access Denied!</b>\n\nEi feature use korte subscription proyojon. Age subscription active korun.",
            reply_markup=get_unsubscribed_keyboard(),
            parse_mode="HTML"
        )
        return

    # Subscribed User Options
    if message.text == "💳 Set UPI":
        admin_states[chat_id] = {"step": "AWAITING_UPI"}
        curr = get_setting("upi_id", "Not Set")
        bot.send_message(
            chat_id,
            f"ℹ️ <b>Current UPI ID:</b> <code>{curr}</code>\n\n"
            "Please send your new UPI ID (e.g., <code>yourname@fam</code>):",
            reply_markup=get_main_keyboard()
        )

    elif message.text == "📧 Set Email":
        admin_states[chat_id] = {"step": "AWAITING_GMAIL"}
        curr_e = get_setting("email_user", "Not Configured")
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
            if user_id == ADMIN_ID:
                bots = conn.execute("SELECT bot_id, bot_name FROM connected_bots").fetchall()
            else:
                bots = conn.execute("SELECT bot_id, bot_name FROM connected_bots WHERE owner_id = ?", (user_id,)).fetchall()

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
            "• Open your browser and go to Google Security.\n"
            "• Make sure 2-Step Verification is turned ON.\n"
            "• Create an App password and copy the 16-character code.\n\n"
            "<b>2. Connecting Email & UPI:</b>\n"
            "• Tap <b>📧 Set Email</b>: Provide your Gmail and 16-digit App Password.\n"
            "• Tap <b>💳 Set UPI</b>: Provide your UPI ID.\n\n"
            "<b>3. Connecting Client Bots:</b>\n"
            "• Tap <b>🔗 Connect Bot</b> and paste the bot token from @BotFather."
        )
        bot.send_message(chat_id, tutorial_text, reply_markup=get_main_keyboard(), disable_web_page_preview=True)

# ----------------- UTR SUBMIT COMMAND -----------------
@bot.message_handler(commands=["utr"])
def handle_utr_submit(message):
    user_id = message.from_user.id
    parts = message.text.split()
    if len(parts) < 2 or len(parts[1]) != 12:
        bot.reply_to(message, "⚠️ Format: <code>/utr 123456789012</code>", parse_mode="HTML")
        return

    utr = parts[1].strip()
    fetch_master_settings()
    days_to_add = int(get_setting("sub_days", 30))
    exp_time = datetime.now() + timedelta(days=days_to_add)
    exp_str = exp_time.strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO user_subscriptions (user_id, expires_at) VALUES (?, ?)", (user_id, exp_str))

    bot.reply_to(
        message,
        f"🎉 <b>Subscription Activated Successfully!</b>\n\n"
        f"UTR: <code>{utr}</code> verified.\n"
        f"Valid Till: <code>{exp_str}</code> ({days_to_add} Days)\n\n"
        "Ekhon apni nicher sob buttons use korte parben!",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )

# ----------------- TEXT INPUT PROCESSOR -----------------
@bot.message_handler(func=lambda m: m.chat.id in admin_states)
def handle_admin_text_inputs(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    state_data = admin_states.get(chat_id, {})
    step = state_data.get("step")
    text = message.text.strip()

    if step == "AWAITING_UPI":
        admin_states.pop(chat_id, None)
        set_setting("upi_id", text)
        bot.send_message(chat_id, f"✅ <b>UPI ID Updated:</b> <code>{text}</code>", reply_markup=get_main_keyboard())

    elif step == "AWAITING_GMAIL":
        if "@" not in text or "." not in text:
            bot.send_message(chat_id, "❌ Please send a valid email address:")
            return
        admin_states[chat_id] = {"step": "AWAITING_APP_PASS", "email": text}
        bot.send_message(
            chat_id,
            f"🔑 <b>Enter 16-Digit Google App Password:</b>\n\n"
            f"Selected email: <code>{text}</code>\n\n"
            "Please paste the 16-character App Password:"
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

            set_setting("email_user", saved_email)
            set_setting("email_pass", app_password)

            bot.edit_message_text(
                f"<b>🎉 Email Connected & Verified!</b>\n\n"
                f"<b>Email:</b> <code>{saved_email}</code>",
                chat_id,
                wait_msg.message_id
            )
        except Exception as e:
            bot.edit_message_text(f"❌ <b>Authentication Failed:</b> <code>{e}</code>", chat_id, wait_msg.message_id)

    elif step == "AWAITING_BOT_NAME_FOR_API":
        admin_states.pop(chat_id, None)
        bot_name = text
        bot_id = f"bot_{secrets.token_hex(4)}"
        api_key = f"hub_{secrets.token_hex(16)}"

        with get_db() as conn:
            conn.execute(
                "INSERT INTO connected_bots (bot_id, bot_name, api_key, owner_id) VALUES (?, ?, ?, ?)",
                (bot_id, bot_name, api_key, user_id)
            )

        bot.send_message(
            chat_id,
            f"<b>🔑 API Key Generated Successfully!</b>\n\n"
            f"<b>Bot Name:</b> {bot_name}\n"
            f"<b>Bot ID:</b> <code>{bot_id}</code>\n\n"
            f"<b>API Key:</b>\n<code>{api_key}</code>",
            reply_markup=get_main_keyboard()
        )

    elif step == "AWAITING_BOT_TOKEN":
        admin_states.pop(chat_id, None)
        bot_token = text

        wait_msg = bot.send_message(chat_id, "⏳ Verifying bot token with Telegram...")
        try:
            res = requests.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=8).json()
            if not res.get("ok"):
                bot.edit_message_text("❌ Invalid Bot Token!", chat_id, wait_msg.message_id)
                return

            client_name = res["result"].get("first_name", "Telegram Bot")
            client_username = res["result"].get("username", "Unknown")
            bot_id = f"bot_{secrets.token_hex(4)}"
            api_key = f"hub_{secrets.token_hex(16)}"

            with get_db() as conn:
                conn.execute(
                    "INSERT INTO connected_bots (bot_id, bot_name, api_key, bot_token, owner_id) VALUES (?, ?, ?, ?, ?)",
                    (bot_id, f"{client_name} (@{client_username})", api_key, bot_token, user_id)
                )

            bot.edit_message_text(
                f"<b>🎉 Bot Connected Successfully!</b>\n\n"
                f"<b>Name:</b> {client_name}\n"
                f"<b>Username:</b> @{client_username}\n"
                f"<b>API Key:</b>\n<code>{api_key}</code>",
                chat_id,
                wait_msg.message_id
            )
        except Exception as e:
            bot.edit_message_text(f"⚠️️ Error verifying bot: {e}", chat_id, wait_msg.message_id)

# ----------------- CALLBACK QUERY HANDLER -----------------
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

    text = (
        f"<b>🤖 Bot Details: {bot_info['bot_name']}</b>\n\n"
        f"<b>ID:</b> <code>{bot_id}</code>\n"
        f"<b>API Key:</b> <code>{bot_info['api_key']}</code>\n"
        f"<b>Created:</b> {bot_info['created_at']}\n\n"
        f"<b>Stats:</b> {stats[0]} tx | ₹{stats[1]:.2f}"
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
    user_id = call.from_user.id
    with get_db() as conn:
        if user_id == ADMIN_ID:
            bots = conn.execute("SELECT bot_id, bot_name FROM connected_bots").fetchall()
        else:
            bots = conn.execute("SELECT bot_id, bot_name FROM connected_bots WHERE owner_id = ?", (user_id,)).fetchall()

    if not bots:
        bot.edit_message_text("🚫 No bots connected yet!", call.message.chat.id, call.message.message_id)
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for b in bots:
        markup.add(types.InlineKeyboardButton(f"🤖 {b['bot_name']}", callback_data=f"view_{b['bot_id']}"))

    bot.edit_message_text("📋 <b>Connected Bot List:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup)

# ----------------- RUNNERS -----------------
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
