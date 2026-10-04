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
import urllib.parse
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import telebot
from telebot import types

# ----------------- CONFIGURATION -----------------
BOT_TOKEN = "8737334045:AAEpU1UwBKlcKocRvUoPNfLg5c3xlHI0Gjc"
ADMIN_ID = 5624448603
BOT_USERNAME = "@paymentapisajidbot"

PORT = int(os.environ.get("PORT", 5000))
DB_PATH = "payment_hub.db"
IMAP_SERVER = "imap.gmail.com"

# Master Admin Bot Base URL (Without trailing slash)
ADMIN_API_URL = "https://botverse-admin-bot.onrender.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

user_states = {}

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
                plan_name TEXT,
                expires_at TIMESTAMP
            );
        """)

        # Default Pricing & Admin Settings
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('upi_id', 'not_set@fam')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_upi', 'admin@fam')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('email_user', '')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('email_pass', '')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('price_1m', '49')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('price_3m', '129')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('price_6m', '229')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('price_12m', '399')")
    logging.info("Database initialized successfully.")

init_db()

# ----------------- CONFIG & SUBSCRIPTION HELPERS -----------------
def get_setting(key, default=""):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))

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

# ----------------- KEYBOARD MENUS -----------------
def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_sub_plans = types.KeyboardButton("💎 Upgrade / Subscribe")
    btn_sub_status = types.KeyboardButton("📅 Subscription Status")
    btn_upi = types.KeyboardButton("💳 Set UPI")
    btn_email = types.KeyboardButton("📧 Set Email")
    btn_connect = types.KeyboardButton("🔗 Connect Bot")
    btn_list = types.KeyboardButton("🤖 Bot List")
    btn_tx = types.KeyboardButton("📊 Transactions")
    btn_tutorial = types.KeyboardButton("📖 Tutorial")

    markup.add(btn_sub_plans, btn_sub_status)
    markup.add(btn_upi, btn_email)
    markup.add(btn_connect, btn_list)
    markup.add(btn_tx, btn_tutorial)
    return markup

def get_plans_inline_keyboard():
    p1 = get_setting("price_1m", "49")
    p3 = get_setting("price_3m", "129")
    p6 = get_setting("price_6m", "229")
    p12 = get_setting("price_12m", "399")

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(f"⭐ 1 Month Plan - ₹{p1}", callback_data="buy_1m"),
        types.InlineKeyboardButton(f"🔥 3 Months Plan - ₹{p3}", callback_data="buy_3m"),
        types.InlineKeyboardButton(f"💎 6 Months Plan - ₹{p6}", callback_data="buy_6m"),
        types.InlineKeyboardButton(f"👑 12 Months Plan - ₹{p12}", callback_data="buy_12m")
    )
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
                                        logging.info(f"Payment alert: ₹{amount} | UTR: {utr}")
                                    except sqlite3.IntegrityError:
                                        pass
            mail.logout()
        except Exception as e:
            logging.error(f"IMAP Sync issue: {e}")
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
            return jsonify({"status": "error", "message": "Subscription expired!"}), 403

        bot_id = bot_row["bot_id"]
        bot_name = bot_row["bot_name"]

        tx_row = conn.execute("SELECT utr FROM transactions WHERE utr = ?", (utr,)).fetchone()
        if tx_row:
            return jsonify({"status": "already_used", "message": "This UTR has already been claimed."}), 400

        ledger_row = conn.execute("SELECT amount, claimed FROM email_ledger WHERE utr = ?", (utr,)).fetchone()
        if not ledger_row:
            return jsonify({"status": "not_found", "message": "Transaction not detected yet."}), 404

        received_amount = ledger_row["amount"]
        if ledger_row["claimed"] == 1:
            return jsonify({"status": "already_used", "message": "This UTR has already been claimed."}), 400

        if received_amount < expected_amount:
            return jsonify({"status": "insufficient_amount", "message": f"Received ₹{received_amount}, expected ₹{expected_amount}"}), 400

        conn.execute("UPDATE email_ledger SET claimed = 1 WHERE utr = ?", (utr,))
        conn.execute("INSERT INTO transactions (utr, bot_id, user_id, amount) VALUES (?, ?, ?, ?)",
                     (utr, bot_id, user_id, received_amount))

    try:
        bot.send_message(
            ADMIN_ID,
            f"<b>✅ Client Bot Payment Received!</b>\n\n"
            f"<b>Bot:</b> {bot_name}\n"
            f"<b>Amount:</b> ₹{received_amount:.2f}\n"
            f"<b>UTR:</b> <code>{utr}</code>",
            reply_markup=get_main_keyboard()
        )
    except Exception:
        pass

    return jsonify({"status": "success", "amount": received_amount, "utr": utr}), 200

# ----------------- MASTER ADMIN CONNECT (/api) -----------------
@bot.message_handler(commands=["api"])
def cmd_api_register(message):
    if message.from_user.id != ADMIN_ID:
        return

    code = f"PAY_{secrets.token_hex(3).upper()}"
    default_config = {
        "admin_upi": get_setting("admin_upi", "admin@fam"),
        "price_1m": int(get_setting("price_1m", 49)),
        "price_3m": int(get_setting("price_3m", 129)),
        "price_6m": int(get_setting("price_6m", 229)),
        "price_12m": int(get_setting("price_12m", 399))
    }

    payload = {
        "auth_code": code,
        "bot_name": "UPI Payment Hub",
        "bot_username": BOT_USERNAME,
        "settings": default_config
    }

    try:
        res = requests.post(f"{ADMIN_API_URL}/api/register", json=payload, timeout=8)
        if res.status_code == 200:
            bot.reply_to(
                message,
                f"✅ <b>Payment Engine Connected to Master Hub!</b>\n\n"
                f"🔑 <b>Authentication Code:</b> <code>{code}</code>\n\n"
                f"Send this command to your Master Admin Bot:\n"
                f"<code>/add {code}</code>",
                parse_mode="HTML"
            )
        else:
            bot.reply_to(message, f"❌ Master Hub Server Error: HTTP {res.status_code}")
    except Exception as e:
        bot.reply_to(message, f"❌ Connection Error: {e}")

# ----------------- START COMMAND -----------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id
    user_states.pop(message.chat.id, None)
    fetch_master_settings()

    subscribed, exp_date = is_subscribed(user_id)
    status_badge = f"<b>Active</b> (Until: <code>{exp_date}</code>)" if subscribed else "<b>Free Account</b>"

    welcome_msg = (
        f"<b>👋 Welcome to BotVerse Payment Hub!</b>\n\n"
        f"Manage automated UPI payments & real-time UTR verifications for all your Telegram child bots.\n\n"
        f"• <b>Account Status:</b> {status_badge}\n\n"
        f"You can explore all options, configure your UPI, and check tutorials from the keyboard below:"
    )
    bot.send_message(message.chat.id, welcome_msg, reply_markup=get_main_keyboard())

# ----------------- MENU BUTTON HANDLER -----------------
@bot.message_handler(func=lambda m: m.text in [
    "💎 Upgrade / Subscribe", "📅 Subscription Status",
    "💳 Set UPI", "📧 Set Email", "🔗 Connect Bot", "🤖 Bot List", "📊 Transactions", "📖 Tutorial"
])
def handle_menu_buttons(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    fetch_master_settings()

    # ১. সাবস্ক্রিপশন প্ল্যান দেখা
    if message.text == "💎 Upgrade / Subscribe":
        bot.send_message(
            chat_id,
            "<b>💎 Select Your Subscription Tier:</b>\n\n"
            "An active subscription is required to connect and automate your bots. Choose a plan:",
            reply_markup=get_plans_inline_keyboard()
        )
        return

    # ২. সাবস্ক্রিপশন স্ট্যাটাস চেক
    elif message.text == "📅 Subscription Status":
        subscribed, exp_date = is_subscribed(user_id)
        if subscribed:
            bot.send_message(
                chat_id,
                f"<b>✅ Subscription Status: Active</b>\n\n"
                f"• <b>Tier:</b> Premium Access\n"
                f"• <b>Expires On:</b> <code>{exp_date}</code>\n\n"
                "Your integrated bots and webhooks are running smoothly.",
                reply_markup=get_main_keyboard()
            )
        else:
            bot.send_message(
                chat_id,
                "<b>ℹ️ Subscription Status: Free / Inactive</b>\n\n"
                "You can configure your settings anytime. To connect and run active bots, tap <b>💎 Upgrade / Subscribe</b>.",
                reply_markup=get_main_keyboard()
            )
        return

    # ৩. শুধুমাত্র 🔗 Connect Bot চাপলে সাবস্ক্রিপশন চেক হবে
    elif message.text == "🔗 Connect Bot":
        subscribed, _ = is_subscribed(user_id)
        if not subscribed:
            msg = (
                "🔒 <b>Subscription Required to Connect Bots!</b>\n\n"
                "You can configure your UPI and alerts for free, but activating a bot requires a subscription.\n\n"
                "Tap below to choose a plan and unlock instant connection:"
            )
            bot.send_message(chat_id, msg, reply_markup=get_plans_inline_keyboard())
            return

        user_states[chat_id] = {"step": "AWAITING_BOT_TOKEN"}
        bot.send_message(
            chat_id,
            "🤖 <b>Connect Telegram Bot:</b>\n\n"
            "Send your child bot's Token from @BotFather:",
            reply_markup=get_main_keyboard()
        )
        return

    # ৪. বাকি সব ফিচার ওপেন থাকবে (ইউজার ট্রাস্ট বাড়ানোর জন্য)
    elif message.text == "💳 Set UPI":
        user_states[chat_id] = {"step": "AWAITING_UPI"}
        curr = get_setting("upi_id", "Not Configured")
        bot.send_message(
            chat_id,
            f"ℹ️ <b>Current Receiving UPI:</b> <code>{curr}</code>\n\n"
            "Send the UPI ID where customer payments should go (e.g. <code>username@fam</code>):",
            reply_markup=get_main_keyboard()
        )

    elif message.text == "📧 Set Email":
        user_states[chat_id] = {"step": "AWAITING_GMAIL"}
        curr_e = get_setting("email_user", "Not Configured")
        bot.send_message(
            chat_id,
            f"📧 <b>Alert Email:</b> <code>{curr_e}</code>\n\n"
            "Send your Gmail address that receives bank/FamPay payment alerts:",
            reply_markup=get_main_keyboard()
        )

    elif message.text == "🤖 Bot List":
        user_states.pop(chat_id, None)
        with get_db() as conn:
            if user_id == ADMIN_ID:
                bots = conn.execute("SELECT bot_id, bot_name FROM connected_bots").fetchall()
            else:
                bots = conn.execute("SELECT bot_id, bot_name FROM connected_bots WHERE owner_id = ?", (user_id,)).fetchall()

        if not bots:
            bot.send_message(chat_id, "🚫 You haven't connected any bots yet! Tap <b>🔗 Connect Bot</b> to get started.", reply_markup=get_main_keyboard())
            return

        markup = types.InlineKeyboardMarkup(row_width=1)
        for b in bots:
            markup.add(types.InlineKeyboardButton(f"🤖 {b['bot_name']}", callback_data=f"view_{b['bot_id']}"))

        bot.send_message(chat_id, "📋 <b>Your Connected Bots:</b>\n\nSelect a bot to manage:", reply_markup=markup)

    elif message.text == "📊 Transactions":
        user_states.pop(chat_id, None)
        with get_db() as conn:
            stats = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM transactions").fetchone()
            latest = conn.execute("SELECT utr, bot_id, amount, timestamp FROM transactions ORDER BY timestamp DESC LIMIT 5").fetchall()

        msg = (
            f"<b>📊 Lifetime Transaction Report</b>\n\n"
            f"• <b>Total Count:</b> <code>{stats[0]}</code>\n"
            f"• <b>Total Volume:</b> <code>₹{stats[1]:.2f}</code>\n\n"
            f"<b>🕒 Recent Transactions:</b>\n"
        )
        if latest:
            for tx in latest:
                msg += f"▫️ ₹{tx['amount']:.2f} | UTR: <code>{tx['utr']}</code> | Bot: <code>{tx['bot_id']}</code>\n"
        else:
            msg += "<i>No transaction history recorded yet.</i>"

        bot.send_message(chat_id, msg, reply_markup=get_main_keyboard())

    elif message.text == "📖 Tutorial":
        user_states.pop(chat_id, None)
        tutorial_text = (
            "<b>📖 How to Use BotVerse Payment Hub:</b>\n\n"
            "1. Tap <b>💳 Set UPI</b>: Add your personal UPI to collect money.\n"
            "2. Tap <b>📧 Set Email</b>: Link Gmail & 16-digit App Password for auto-UTR detection.\n"
            "3. Tap <b>🔗 Connect Bot</b>: Add your bot token to start automated payments!\n\n"
            "💡 Everything is completely verified in real time without any delay."
        )
        bot.send_message(chat_id, tutorial_text, reply_markup=get_main_keyboard())

# ----------------- SUBSCRIPTION PLAN CALLBACKS & DYNAMIC QR -----------------
@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def handle_plan_selection(call):
    plan_code = call.data.replace("buy_", "")
    fetch_master_settings()

    plans = {
        "1m": {"days": 30, "price": float(get_setting("price_1m", 49)), "title": "1 Month"},
        "3m": {"days": 90, "price": float(get_setting("price_3m", 129)), "title": "3 Months"},
        "6m": {"days": 180, "price": float(get_setting("price_6m", 229)), "title": "6 Months"},
        "12m": {"days": 365, "price": float(get_setting("price_12m", 399)), "title": "12 Months"}
    }

    selected = plans.get(plan_code)
    if not selected:
        bot.answer_callback_query(call.id, "Invalid tier.")
        return

    admin_upi = get_setting("admin_upi", "admin@fam")
    amount = selected["price"]
    days = selected["days"]
    title = selected["title"]

    user_states[call.message.chat.id] = {
        "step": "AWAITING_SUB_UTR",
        "expected_amount": amount,
        "days": days,
        "title": title
    }

    upi_payload = f"upi://pay?pa={admin_upi}&pn=PaymentHub&am={amount:.2f}&cu=INR&tn=Subscription_{title}"
    encoded_payload = urllib.parse.quote(upi_payload)
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={encoded_payload}"

    caption = (
        f"<b>💳 Checkout: {title} Subscription</b>\n\n"
        f"• <b>Amount:</b> ₹{amount:.2f}\n"
        f"• <b>Validity:</b> {days} Days\n"
        f"• <b>UPI ID:</b> <code>{admin_upi}</code>\n\n"
        f"👉 Scan the QR Code or send directly to the UPI ID.\n"
        f"Once payment is done, send the <b>12-digit UTR number</b> here for instant verification:"
    )

    try:
        bot.send_photo(call.message.chat.id, qr_url, caption=caption, parse_mode="HTML")
    except Exception:
        bot.send_message(call.message.chat.id, caption, parse_mode="HTML")

    bot.answer_callback_query(call.id)

# ----------------- TEXT INPUT HANDLER & AUTO VERIFY -----------------
@bot.message_handler(func=lambda m: m.chat.id in user_states)
def handle_text_inputs(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    state_data = user_states.get(chat_id, {})
    step = state_data.get("step")
    text = message.text.strip()

    # Subscription UTR Verification
    if step == "AWAITING_SUB_UTR":
        utr_match = re.search(r"\b\d{12}\b", text)
        if not utr_match:
            bot.send_message(chat_id, "⚠️ Please enter a valid 12-digit numeric UTR:")
            return

        utr = utr_match.group(0)
        expected_amount = state_data["expected_amount"]
        days = state_data["days"]
        title = state_data["title"]

        wait_msg = bot.send_message(chat_id, "🔍 Verifying payment with email ledger records...")

        with get_db() as conn:
            tx_row = conn.execute("SELECT utr FROM transactions WHERE utr = ?", (utr,)).fetchone()
            if tx_row:
                bot.edit_message_text("❌ This UTR has already been claimed.", chat_id, wait_msg.message_id)
                return

            ledger_row = conn.execute("SELECT amount, claimed FROM email_ledger WHERE utr = ?", (utr,)).fetchone()
            if not ledger_row:
                bot.edit_message_text(
                    "⏳ <b>Transaction Not Detected Yet</b>\n\n"
                    "Please wait 30–60 seconds for the email alert to sync, then submit the UTR again.",
                    chat_id,
                    wait_msg.message_id
                )
                return

            received_amount = ledger_row["amount"]
            if ledger_row["claimed"] == 1:
                bot.edit_message_text("❌ This UTR has already been claimed.", chat_id, wait_msg.message_id)
                return

            if received_amount < expected_amount:
                bot.edit_message_text(
                    f"⚠️ <b>Partial Amount Detected</b>\n\n"
                    f"Received ₹{received_amount:.2f}, expected ₹{expected_amount:.2f}.",
                    chat_id,
                    wait_msg.message_id
                )
                return

            conn.execute("UPDATE email_ledger SET claimed = 1 WHERE utr = ?", (utr,))
            conn.execute("INSERT INTO transactions (utr, bot_id, user_id, amount) VALUES (?, 'SUBSCRIPTION', ?, ?)",
                         (utr, user_id, received_amount))

            current_sub, exp_str = is_subscribed(user_id)
            if current_sub and exp_str != "Lifetime (Super Admin)":
                try:
                    current_exp = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
                    new_exp = current_exp + timedelta(days=days)
                except Exception:
                    new_exp = datetime.now() + timedelta(days=days)
            else:
                new_exp = datetime.now() + timedelta(days=days)

            final_exp = new_exp.strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT OR REPLACE INTO user_subscriptions (user_id, plan_name, expires_at) VALUES (?, ?, ?)",
                (user_id, title, final_exp)
            )

        user_states.pop(chat_id, None)

        bot.edit_message_text(
            f"🎉 <b>Payment Verified & Subscription Activated!</b>\n\n"
            f"• <b>Plan:</b> {title}\n"
            f"• <b>Valid Until:</b> <code>{final_exp}</code>\n"
            f"• <b>UTR:</b> <code>{utr}</code>\n\n"
            "You can now connect your Telegram bot and start automating payments!",
            chat_id,
            wait_msg.message_id
        )

        try:
            bot.send_message(
                ADMIN_ID,
                f"<b>💰 New Hub Subscription!</b>\n\n"
                f"• <b>User ID:</b> <code>{user_id}</code>\n"
                f"• <b>Plan:</b> {title}\n"
                f"• <b>Amount:</b> ₹{received_amount:.2f}\n"
                f"• <b>UTR:</b> <code>{utr}</code>"
            )
        except Exception:
            pass
        return

    # UPI Setup
    if step == "AWAITING_UPI":
        user_states.pop(chat_id, None)
        set_setting("upi_id", text)
        bot.send_message(chat_id, f"✅ <b>UPI ID Successfully Saved:</b> <code>{text}</code>", reply_markup=get_main_keyboard())

    # Gmail Setup
    elif step == "AWAITING_GMAIL":
        if "@" not in text or "." not in text:
            bot.send_message(chat_id, "❌ Please enter a valid email address:")
            return
        user_states[chat_id] = {"step": "AWAITING_APP_PASS", "email": text}
        bot.send_message(
            chat_id,
            f"🔑 <b>Google App Password:</b>\n\n"
            f"Email: <code>{text}</code>\n\n"
            "Send your 16-character Google App Password:"
        )

    elif step == "AWAITING_APP_PASS":
        app_pass = text.replace(" ", "")
        saved_email = state_data.get("email")
        user_states.pop(chat_id, None)

        wait_msg = bot.send_message(chat_id, "⏳ Testing Google IMAP connection...")
        try:
            test_mail = imaplib.IMAP4_SSL(IMAP_SERVER)
            test_mail.login(saved_email, app_pass)
            test_mail.logout()

            set_setting("email_user", saved_email)
            set_setting("email_pass", app_pass)

            bot.edit_message_text(
                f"<b>🎉 Email Connected Successfully!</b>\n\n"
                f"<b>Email:</b> <code>{saved_email}</code>",
                chat_id,
                wait_msg.message_id
            )
        except Exception as e:
            bot.edit_message_text(f"❌ <b>Authentication Failed:</b> <code>{e}</code>", chat_id, wait_msg.message_id)

    # Bot Connection (Subscribed Users Only)
    elif step == "AWAITING_BOT_TOKEN":
        user_states.pop(chat_id, None)
        bot_token = text

        wait_msg = bot.send_message(chat_id, "⏳ Validating bot token with Telegram...")
        try:
            res = requests.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=8).json()
            if not res.get("ok"):
                bot.edit_message_text("❌ Invalid Bot Token provided.", chat_id, wait_msg.message_id)
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
                f"• <b>Name:</b> {client_name}\n"
                f"• <b>Username:</b> @{client_username}\n\n"
                f"<b>Your API Key:</b>\n<code>{api_key}</code>\n\n"
                "<i>Use this API key in your bot code for automated payments.</i>",
                chat_id,
                wait_msg.message_id
            )
        except Exception as e:
            bot.edit_message_text(f"⚠️ Error verifying bot token: {e}", chat_id, wait_msg.message_id)

# ----------------- CALLBACK QUERY HANDLER -----------------
@bot.callback_query_handler(func=lambda call: call.data.startswith("view_"))
def handle_view_bot(call):
    bot_id = call.data.replace("view_", "", 1)
    with get_db() as conn:
        bot_info = conn.execute("SELECT bot_name, created_at, api_key FROM connected_bots WHERE bot_id = ?", (bot_id,)).fetchone()
        if not bot_info:
            bot.answer_callback_query(call.id, "Bot not found.")
            return

        stats = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM transactions WHERE bot_id = ?", (bot_id,)).fetchone()

    text = (
        f"<b>🤖 Bot Details: {bot_info['bot_name']}</b>\n\n"
        f"• <b>ID:</b> <code>{bot_id}</code>\n"
        f"• <b>API Key:</b> <code>{bot_info['api_key']}</code>\n"
        f"• <b>Connected:</b> {bot_info['created_at']}\n\n"
        f"<b>📊 Stats:</b> {stats[0]} transactions | ₹{stats[1]:.2f}"
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
    bot.answer_callback_query(call.id, "Bot deleted.")
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
        bot.edit_message_text("🚫 No bots connected yet.", call.message.chat.id, call.message.message_id)
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for b in bots:
        markup.add(types.InlineKeyboardButton(f"🤖 {b['bot_name']}", callback_data=f"view_{b['bot_id']}"))

    bot.edit_message_text("📋 <b>Integrated Bots:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup)

# ----------------- BACKGROUND RUNNERS -----------------
def start_bot_polling():
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception:
        pass

    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=True)
        except Exception as e:
            logging.error(f"Polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=imap_worker, daemon=True).start()
    threading.Thread(target=start_bot_polling, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
