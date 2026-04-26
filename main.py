import os
import sys
import telebot
import requests
import json
import io
import time
import re
import logging
import threading
from threading import Thread
from datetime import datetime
from urllib.parse import quote
from flask import Flask
from pymongo import MongoClient

# ==========================================
# ০. গ্লোবাল ভেরিয়েবল ও থ্রেড লক
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
session_lock = threading.Lock()
download_lock = threading.Lock()
active_downloads = set()

# Rate limiting store
_rate_limit_store = {}
_rate_limit_lock = threading.Lock()

# প্রি-কম্পাইল্ড রেজেক্স
_COOKIE_RE = re.compile(r'SESSION=([^\s;]+)', re.I)
_TS_RE = re.compile(r'TS01[a-zA-Z0-9]+=([^\s;]+)', re.I)
_CSRF_RE = re.compile(r'name="_csrf"\s+content="([^"]+)"')
_DATA_ID_RE = re.compile(r'href=".*?\?data=([A-Za-z0-9_\-]+)"')
_PHONE_RE = re.compile(r'^(\+?880|0)1[3-9]\d{8}$')

VALID_CMDS = frozenset(['apps', 'corr', 'repr'])
DEFAULT_PERMS = {
    "apps": True, "corr": True, "repr": True,
    "search": True, "ubrn_update": True, "server_pdf": True, "print": True
}
SERVICE_COSTS = {
    "pdf": 25, "pay": 25,
    "server_pdf_login": 25, "server_pdf_no_login": 50
}

# ==========================================
# ১. কনফিগারেশন ও ডাটাবেস
# ==========================================
API_TOKEN = os.environ.get('BOT_TOKEN', '').strip()
MONGO_URI = os.environ.get('MONGO_URI', '').strip()
ADMIN_ID_STR = os.environ.get('ADMIN_ID', '').strip()

if not all([API_TOKEN, MONGO_URI, ADMIN_ID_STR]):
    logging.critical("❌ Critical Environment Variables missing!")
    sys.exit(1)

ADMIN_ID = int(ADMIN_ID_STR)
bot = telebot.TeleBot(API_TOKEN, threaded=True, num_threads=4)

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    db = mongo_client['bdris_bot_db']
    sessions_collection = db['users_sessions']
    access_collection = db['users_access']
    settings_collection = db['bot_settings']
    if not settings_collection.find_one({"_id": "config"}):
        settings_collection.insert_one({"_id": "config", "payment_active": True})
    logging.info("✅ MongoDB Connected Successfully!")
except Exception as e:
    logging.critical(f"❌ DB Connection Failed: {e}")
    sys.exit(1)

# ==========================================
# ২. সেফ র‍্যাপারস ও ইউজার লজিক
# ==========================================
def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logging.error(f"Send Error: {e}")
        return None

def safe_edit(chat_id, message_id, text, **kwargs):
    try:
        return bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception:
        return None

def safe_delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass

def is_payment_active():
    config = settings_collection.find_one({"_id": "config"})
    return config.get("payment_active", True) if config else True

def get_service_cost(user_id, service="default"):
    if user_id == ADMIN_ID or not is_payment_active():
        return 0
    return SERVICE_COSTS.get(service, 25)

def get_balance(user_id):
    if user_id == ADMIN_ID:
        return 999999
    record = access_collection.find_one({"chat_id": user_id})
    return int(record.get("balance", 0)) if record else 0

def update_balance(user_id, amount):
    if user_id == ADMIN_ID:
        return
    access_collection.update_one({"chat_id": user_id}, {"$inc": {"balance": amount}})

def check_user_access(user_id, user_name):
    if user_id == ADMIN_ID:
        return True
    user_record = access_collection.find_one({"chat_id": user_id})
    if not user_record:
        access_collection.insert_one({
            "chat_id": user_id,
            "name": str(user_name)[:100],
            "status": "allowed",
            "permissions": DEFAULT_PERMS.copy(),
            "balance": 0,
            "recharge_trxids": []
        })
        safe_send(ADMIN_ID, f"🔔 *নতুন ইউজার!*\n👤 {user_name}\n🆔 `{user_id}`", parse_mode="Markdown")
        return True
    return user_record.get("status") == "allowed"

# ==========================================
# ২.১ রেট লিমিটিং (FIX: ফাংশন ছিল না)
# ==========================================
def is_rate_limited(user_id, max_calls=5, window=10):
    """প্রতি `window` সেকেন্ডে `max_calls` এর বেশি হলে ব্লক করে।"""
    if user_id == ADMIN_ID:
        return False
    now = time.time()
    with _rate_limit_lock:
        history = _rate_limit_store.get(user_id, [])
        history = [t for t in history if now - t < window]
        if len(history) >= max_calls:
            return True
        history.append(now)
        _rate_limit_store[user_id] = history
    return False

# ==========================================
# ৩. সেশন ম্যানেজমেন্ট
# ==========================================
user_sessions = {}

def get_session(user_id):
    with session_lock:
        if user_id in user_sessions:
            return user_sessions[user_id]

    u_sess = {
        "req_session": requests.Session(),
        "csrf": "",
        "ch_session": requests.Session(),
        "ch_csrf": "",
        "ch_otp": "",
        "mode": "SECRETARY",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "is_alive": False,
        "current_page": "https://bdris.gov.bd/admin/",
        "app_start": 0,
        "app_length": 5,
        "sharok_no": 1,
        "temp_data": {},
        "id_cache": {},
        "last_action_time": time.time(),
        "last_warning_time": 0.0,
        "current_search_val": ""
    }
    db_data = sessions_collection.find_one({"chat_id": user_id})
    if db_data:
        u_sess["req_session"].cookies.update(db_data.get("sec_cookies", {}))
        u_sess["ch_session"].cookies.update(db_data.get("ch_cookies", {}))
        u_sess["mode"] = db_data.get("mode", "SECRETARY")
        u_sess["ch_otp"] = db_data.get("ch_otp", "")
        u_sess["is_alive"] = db_data.get("is_alive", False)
        u_sess["sharok_no"] = db_data.get("sharok_no", 1)
        u_sess["app_length"] = db_data.get("app_length", 5)

    with session_lock:
        user_sessions[user_id] = u_sess
    return u_sess

def save_session_to_db(user_id, u_sess):
    try:
        data = {
            "chat_id": user_id,
            "sec_cookies": u_sess["req_session"].cookies.get_dict(),
            "ch_cookies": u_sess["ch_session"].cookies.get_dict(),
            "mode": u_sess["mode"],
            "ch_otp": u_sess.get("ch_otp", ""),
            "is_alive": u_sess["is_alive"],
            "sharok_no": u_sess.get("sharok_no", 1),
            "app_length": u_sess.get("app_length", 5)
        }
        sessions_collection.update_one({"chat_id": user_id}, {"$set": data}, upsert=True)
    except Exception as e:
        logging.error(f"DB Save Error: {e}")

def keep_sessions_alive_and_cleanup():
    while True:
        time.sleep(300)
        now = time.time()
        with session_lock:
            uids = list(user_sessions.keys())

        for uid in uids:
            u = get_session(uid)
            if u["is_alive"]:
                try:
                    r1 = u["req_session"].get(
                        "https://bdris.gov.bd/admin/",
                        headers={'User-Agent': u["ua"]},
                        timeout=15
                    )
                    c1 = _CSRF_RE.search(r1.text)
                    if c1:
                        with session_lock:
                            u["csrf"] = c1.group(1)
                        save_session_to_db(uid, u)  # FIX: CSRF আপডেটের পর সেভ করা
                except Exception as e:
                    logging.warning(f"Keep-alive failed for {uid}: {e}")
            elif now - u["last_action_time"] > 3600:
                sessions_collection.update_one({"chat_id": uid}, {"$set": {"is_alive": False}})
                with session_lock:
                    user_sessions.pop(uid, None)

# ==========================================
# ৪. কিবোর্ড ও ক্যানসেল লজিক
# ==========================================
def generate_main_menu(chat_id, user_id=None):
    if not user_id:
        user_id = chat_id
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    u_sess = get_session(user_id)
    perms = get_user_permissions(user_id)

    markup.row("🔑 User Login")
    if not u_sess["is_alive"] and (perms.get("server_pdf") or user_id == ADMIN_ID):
        markup.row("🖨️ Server PDF Print")
        if is_payment_active():
            markup.row("💰 My Profile & Recharge")

    if u_sess["is_alive"]:
        if is_payment_active():
            markup.row("💰 My Profile & Recharge")
        markup.row("👤 নিবন্ধক সেকশন", "🧑‍💼 অথোরাইজড ইউজার")
        row_core = []
        if perms.get("apps") or user_id == ADMIN_ID:
            row_core.append("📋 Applications")
        if perms.get("corr") or user_id == ADMIN_ID:
            row_core.append("📝 Correction")
        if perms.get("repr") or user_id == ADMIN_ID:
            row_core.append("🔄 Reprint")
        if row_core:
            markup.row(*row_core)
        row_search = ["🏠 Dashboard"]
        if perms.get("search") or user_id == ADMIN_ID:
            row_search.extend(["🌐 Search By Name", "🔢 Search By UBRN"])
        markup.row(*row_search)
        row_tools = []
        if perms.get("ubrn_update") or user_id == ADMIN_ID:
            row_tools.append("👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ")
        if perms.get("server_pdf") or user_id == ADMIN_ID:
            row_tools.append("🖨️ Server PDF Print")
        if row_tools:
            markup.row(*row_tools)
    if user_id == ADMIN_ID:
        markup.row("🔑 Admin Login", "🛠️ Check Cookies", "👥 Manage Users")
    return markup

def get_user_permissions(user_id):
    if user_id == ADMIN_ID:
        return {k: True for k in DEFAULT_PERMS}
    record = access_collection.find_one({"chat_id": user_id})
    if record and "permissions" in record:
        p = DEFAULT_PERMS.copy()
        p.update(record["permissions"])
        return p
    return DEFAULT_PERMS.copy()

def is_cancel(m):
    if not m or not m.text:
        return False
    if any(kw in m.text for kw in ("/start", "Back to Menu", "Dashboard", "🏠 Dashboard")):
        bot.clear_step_handler_by_chat_id(m.chat.id)
        safe_send(
            m.chat.id, "🏠 মেনুতে ফিরে আসা হলো।",
            reply_markup=generate_main_menu(m.chat.id, m.from_user.id)
        )
        return True
    return False

# ==========================================
# ৫. বিডিআরআইএস একশনস
# ==========================================
def _set_session_cookies(sess, sid, tsid):
    sess.cookies.clear()
    sess.cookies.set("SESSION", sid, domain='bdris.gov.bd')
    sess.cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')

def extract_sid_tsid(text):
    s = _COOKIE_RE.search(text)
    tsid = _TS_RE.search(text)
    return (s.group(1), tsid.group(1)) if s and tsid else (None, None)

def admin_login_logic(m):
    if is_cancel(m):
        return
    sid, tsid = extract_sid_tsid(m.text or "")
    uid = m.from_user.id
    u_sess = get_session(uid)
    if sid and tsid:
        _set_session_cookies(u_sess["req_session"], sid, tsid)
        u_sess["is_alive"] = True
        save_session_to_db(uid, u_sess)
        safe_send(m.chat.id, "✅ এডমিন সেশন সেট হয়েছে!", reply_markup=generate_main_menu(m.chat.id, uid))
    else:
        msg = safe_send(m.chat.id, "❌ ভুল ফরম্যাট! SESSION= ও TS01...= সহ আবার দিন:")
        if msg:
            bot.register_next_step_handler(msg, admin_login_logic)

def role_step_1(m):
    if is_cancel(m):
        return
    uid = m.from_user.id
    sid, tsid = extract_sid_tsid(m.text or "")
    if not sid or not tsid:
        msg = safe_send(m.chat.id, "❌ সঠিক কুকি ফরম্যাট পাওয়া যায়নি। আবার দিন:")
        if msg:
            bot.register_next_step_handler(msg, role_step_1)
        return
    u_sess = get_session(uid)
    _set_session_cookies(u_sess["ch_session"], sid, tsid)
    msg = safe_send(m.chat.id, "✅ নিবন্ধক সেশন গৃহীত। এখন নিবন্ধকের OTP দিন:")
    if msg:
        bot.register_next_step_handler(msg, role_step_2)

def role_step_2(m):
    if is_cancel(m):
        return
    otp = m.text.strip() if m.text else ""
    if not otp.isdigit():
        msg = safe_send(m.chat.id, "❌ OTP শুধু সংখ্যা হবে। আবার দিন:")
        if msg:
            bot.register_next_step_handler(msg, role_step_2)
        return
    get_session(m.from_user.id)["ch_otp"] = otp
    msg = safe_send(m.chat.id, "✅ OTP সংরক্ষিত। এখন অথোরাইজড ইউজার সেশন দিন:")
    if msg:
        bot.register_next_step_handler(msg, role_step_3)

def role_step_3(m):
    if is_cancel(m):
        return
    sid, tsid = extract_sid_tsid(m.text or "")
    uid = m.from_user.id
    if sid and tsid:
        u_sess = get_session(uid)
        _set_session_cookies(u_sess["req_session"], sid, tsid)
        u_sess["is_alive"] = True
        save_session_to_db(uid, u_sess)
        safe_send(m.chat.id, "🎉 লগইন সফল!", reply_markup=generate_main_menu(m.chat.id, uid))
    else:
        msg = safe_send(m.chat.id, "❌ ভুল ইউজার কুকি। আবার দিন:")
        if msg:
            bot.register_next_step_handler(msg, role_step_3)

# ==========================================
# ৬. রিচার্জ ও ব্যালেন্স
# ==========================================
def process_recharge(m):
    if is_cancel(m):
        return
    trxid = m.text.strip() if m.text else ""
    if not (5 <= len(trxid) <= 50):
        msg = safe_send(m.chat.id, "❌ অবৈধ TrxID। আবার দিন:")
        if msg:
            bot.register_next_step_handler(msg, process_recharge)
        return
    uid = m.from_user.id
    if access_collection.find_one({"recharge_trxids": trxid}):
        return safe_send(
            m.chat.id, "❌ এই TrxID ইতিমধ্যে ব্যবহৃত হয়েছে।",
            reply_markup=generate_main_menu(m.chat.id, uid)
        )

    access_collection.update_one({"chat_id": uid}, {"$addToSet": {"recharge_trxids": trxid}})
    safe_send(m.chat.id, "✅ আপনার রিচার্জ রিকোয়েস্ট পাঠানো হয়েছে।", reply_markup=generate_main_menu(m.chat.id, uid))

    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✅ Approve", callback_data=f"apprvbal:{uid}"),
        telebot.types.InlineKeyboardButton("❌ Reject", callback_data=f"rejbal:{uid}")
    )
    safe_send(
        ADMIN_ID,
        f"💰 *নতুন রিচার্জ!*\nUser: `{uid}`\nTrxID: `{trxid}`",
        reply_markup=markup,
        parse_mode="Markdown"
    )

def admin_add_balance_step(m, target_id, admin_msg_id):
    if is_cancel(m):
        return
    try:
        amount = int(m.text.strip())
        if amount <= 0:
            raise ValueError
        update_balance(target_id, amount)
        safe_send(m.chat.id, f"✅ User {target_id} এর অ্যাকাউন্টে {amount}৳ যোগ হয়েছে।")
        safe_send(
            target_id,
            f"🎉 *রিচার্জ সফল!*\nযোগ হয়েছে: {amount}৳\nব্যালেন্স: {get_balance(target_id)}৳",
            parse_mode="Markdown"
        )
        safe_delete(m.chat.id, admin_msg_id)
    except Exception:
        safe_send(m.chat.id, "❌ ভুল ইনপুট। শুধু পজিটিভ সংখ্যা দিন।")

# ==========================================
# ৭. সার্ভার পিডিএফ ও ডাউনলোড
# ==========================================
def download_server_by_ubrn(m):
    if is_cancel(m):
        return
    uid, cid = m.from_user.id, m.chat.id
    ubrn = m.text.strip() if m.text else ""
    if not (ubrn.isdigit() and len(ubrn) == 17):
        msg = safe_send(cid, "❌ সঠিক ১৭ ডিজিট UBRN দিন:")
        if msg:
            bot.register_next_step_handler(msg, download_server_by_ubrn)
        return

    u_sess = get_session(uid)
    working_uid = uid if u_sess["is_alive"] else ADMIN_ID
    if working_uid == ADMIN_ID and not get_session(ADMIN_ID)["is_alive"]:
        return safe_send(cid, "❌ সিস্টেম অফলাইন। অ্যাডমিন সেশন নেই।")

    cost = get_service_cost(uid, "server_pdf_login" if working_uid == uid else "server_pdf_no_login")
    if get_balance(uid) < cost:
        return safe_send(cid, f"❌ পর্যাপ্ত ব্যালেন্স ({cost}৳) নেই।")

    task_id = f"dl_{uid}_{ubrn}"
    with download_lock:
        if task_id in active_downloads:
            return safe_send(cid, "⚠️ অলরেডি প্রসেসিং হচ্ছে...")
        active_downloads.add(task_id)

    update_balance(uid, -cost)
    wait = safe_send(cid, f"⏳ সার্ভারে খোঁজা হচ্ছে... (চার্জ: {cost}৳)")

    def run():
        try:
            work_sess = get_session(working_uid)["req_session"]
            res = work_sess.get(
                f"https://bdris.gov.bd/api/br/info/ubrn/{ubrn}",
                timeout=30
            )
            if wait:
                safe_delete(cid, wait.message_id)

            if res.status_code == 200 and 'encryptedId' in res.json():
                download_server_pdf(cid, uid, working_uid, res.json()['encryptedId'], f"PDF_{ubrn}", cost)
            else:
                update_balance(uid, cost)
                safe_send(cid, "❌ তথ্য পাওয়া যায়নি। ব্যালেন্স রিফান্ড করা হয়েছে।")
        except Exception as e:
            logging.error(f"UBRN download error: {e}")
            update_balance(uid, cost)
            safe_send(cid, "❌ সার্ভার এরর। রিফান্ড করা হয়েছে।")
        finally:
            with download_lock:
                active_downloads.discard(task_id)

    Thread(target=run, daemon=True).start()

def download_server_pdf(chat_id, user_id, session_uid, enc_id, filename, cost):
    u = get_session(session_uid)
    sess = u["ch_session"] if u["mode"] == "CHAIRMAN" else u["req_session"]
    try:
        safe_send(chat_id, "📥 পিডিএফ জেনারেট হচ্ছে...")
        sess.get(f"https://bdris.gov.bd/admin/new-certificate/check?data={enc_id}", timeout=30)
        res = sess.get(f"https://bdris.gov.bd/admin/new-certificate/print?data={enc_id}", timeout=60)
        if 'application/pdf' in res.headers.get('Content-Type', ''):
            bot.send_document(chat_id, io.BytesIO(res.content), visible_file_name=f"{filename}.pdf")
            safe_send(chat_id, f"✅ ডাউনলোড সফল! বর্তমান ব্যালেন্স: {get_balance(user_id)}৳")
        else:
            update_balance(user_id, cost)
            safe_send(chat_id, "❌ পিডিএফ পাওয়া যায়নি। রিফান্ড করা হয়েছে।")
    except Exception as e:
        logging.error(f"PDF download error: {e}")
        update_balance(user_id, cost)
        safe_send(chat_id, "❌ প্রসেসিং এরর। রিফান্ড করা হয়েছে।")

# ==========================================
# ৮. অ্যাপ্লিকেশন লিস্ট UI (FIX: ফাংশন ছিল না)
# ==========================================
CATEGORY_URLS = {
    'apps': "https://bdris.gov.bd/admin/birth-registration/list",
    'corr': "https://bdris.gov.bd/admin/birth-registration/correction-list",
    'repr': "https://bdris.gov.bd/admin/birth-registration/reprint-list",
}
CATEGORY_LABELS = {
    'apps': "📋 Applications",
    'corr': "📝 Correction",
    'repr': "🔄 Reprint",
}

def handle_category_init(m, cat):
    """যেকোনো ক্যাটাগরির লিস্ট শুরু করে।"""
    uid, cid = m.from_user.id, m.chat.id
    u = get_session(uid)
    with session_lock:
        u["app_start"] = 0
    fetch_list_ui(cid, uid, cat)

def fetch_list_ui(chat_id, user_id, cat, edit_msg_id=None):
    """সার্ভার থেকে লিস্ট এনে ইনলাইন কিবোর্ড সহ দেখায়।"""
    u = get_session(user_id)
    if not u["is_alive"]:
        return safe_send(chat_id, "❌ আগে লগইন করুন।")

    url = CATEGORY_URLS.get(cat)
    if not url:
        return safe_send(chat_id, "❌ অজানা ক্যাটাগরি।")

    wait = None
    if not edit_msg_id:
        wait = safe_send(chat_id, "⏳ লোড হচ্ছে...")

    def run():
        try:
            params = {
                "start": u["app_start"],
                "length": u["app_length"],
                "draw": 1
            }
            res = u["req_session"].get(
                url,
                params=params,
                headers={"User-Agent": u["ua"], "X-Requested-With": "XMLHttpRequest"},
                timeout=30
            )
            if wait:
                safe_delete(chat_id, wait.message_id)

            if res.status_code != 200:
                return safe_send(chat_id, "❌ সার্ভার রেসপন্স করেনি।")

            try:
                data = res.json()
            except Exception:
                return safe_send(chat_id, "❌ ডেটা পার্স করা যায়নি।")

            records = data.get("data", [])
            total = data.get("recordsTotal", 0)

            if not records:
                return safe_send(chat_id, "📭 কোনো রেকর্ড পাওয়া যায়নি।")

            text_lines = [f"*{CATEGORY_LABELS.get(cat, cat)}* — মোট: {total}\n"]
            markup = telebot.types.InlineKeyboardMarkup()

            for i, rec in enumerate(records):
                name = rec.get("name") or rec.get("applicantName") or "N/A"
                ubrn = rec.get("ubrn") or rec.get("registrationNo") or "N/A"
                enc_id = rec.get("encryptedId") or ""

                short_id = f"{i}"
                with session_lock:
                    u["id_cache"][short_id] = enc_id

                text_lines.append(f"{u['app_start'] + i + 1}. {name} | {ubrn}")

                row_btns = []
                if enc_id:
                    row_btns.append(
                        telebot.types.InlineKeyboardButton(
                            f"💳 Pay #{u['app_start'] + i + 1}",
                            callback_data=f"pay:{short_id}"
                        )
                    )
                if row_btns:
                    markup.row(*row_btns)

            # Pagination
            nav_row = []
            if u["app_start"] > 0:
                nav_row.append(telebot.types.InlineKeyboardButton("⬅️ Prev", callback_data=f"prev:{cat}"))
            if u["app_start"] + u["app_length"] < total:
                nav_row.append(telebot.types.InlineKeyboardButton("Next ➡️", callback_data=f"next:{cat}"))
            if nav_row:
                markup.row(*nav_row)

            # Page size selector
            markup.row(
                telebot.types.InlineKeyboardButton("5/page", callback_data=f"setlength:{cat}:5"),
                telebot.types.InlineKeyboardButton("10/page", callback_data=f"setlength:{cat}:10"),
                telebot.types.InlineKeyboardButton("20/page", callback_data=f"setlength:{cat}:20"),
            )

            full_text = "\n".join(text_lines)
            if edit_msg_id:
                safe_edit(chat_id, edit_msg_id, full_text, reply_markup=markup, parse_mode="Markdown")
            else:
                safe_send(chat_id, full_text, reply_markup=markup, parse_mode="Markdown")

        except Exception as e:
            logging.error(f"fetch_list_ui error: {e}")
            if wait:
                safe_delete(chat_id, wait.message_id)
            safe_send(chat_id, "❌ লিস্ট লোড করতে সমস্যা হয়েছে।")

    Thread(target=run, daemon=True).start()

# ==========================================
# ৮.১ অ্যাডমিন — ইউজার ম্যানেজমেন্ট (FIX: callback ছিল না)
# ==========================================
def send_user_detail_panel(chat_id, target_id):
    record = access_collection.find_one({"chat_id": target_id})
    if not record:
        return safe_send(chat_id, "❌ ইউজার খুঁজে পাওয়া যায়নি।")

    name = record.get("name", "N/A")
    balance = record.get("balance", 0)
    status = record.get("status", "N/A")
    perms = record.get("permissions", {})

    text = (
        f"👤 *ইউজার ডিটেইল*\n"
        f"নাম: {name}\n"
        f"ID: `{target_id}`\n"
        f"ব্যালেন্স: {balance}৳\n"
        f"স্ট্যাটাস: {status}\n"
        f"পারমিশন: {', '.join(k for k, v in perms.items() if v)}"
    )
    markup = telebot.types.InlineKeyboardMarkup()
    toggle_status = "block" if status == "allowed" else "unblock"
    toggle_label = "🚫 Block" if status == "allowed" else "✅ Unblock"
    markup.row(
        telebot.types.InlineKeyboardButton(toggle_label, callback_data=f"usrtoggle:{target_id}:{toggle_status}"),
        telebot.types.InlineKeyboardButton("➕ Add Balance", callback_data=f"addbal:{target_id}")
    )
    safe_send(chat_id, text, reply_markup=markup, parse_mode="Markdown")

# ==========================================
# ৯. কলব্যাক হ্যান্ডলার
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    uid, cid = call.from_user.id, call.message.chat.id
    u = get_session(uid)
    parts = call.data.split(':')
    action = parts[0]

    if is_rate_limited(uid):
        bot.answer_callback_query(call.id, "⚠️ বেশি দ্রুত ক্লিক করছেন। একটু অপেক্ষা করুন।")
        return

    # ---- Admin: Approve recharge ----
    if action == "apprvbal" and uid == ADMIN_ID:
        target = int(parts[1])
        msg = safe_send(cid, f"ইউজার `{target}` এর জন্য অ্যামাউন্ট দিন:", parse_mode="Markdown")
        if msg:
            bot.register_next_step_handler(
                msg, lambda m: admin_add_balance_step(m, target, call.message.message_id)
            )
        bot.answer_callback_query(call.id)

    # ---- Admin: Reject recharge ----
    elif action == "rejbal" and uid == ADMIN_ID:
        target = int(parts[1])
        safe_send(target, "❌ আপনার রিচার্জ রিকোয়েস্ট অ্যাডমিন বাতিল করেছেন।")
        safe_delete(cid, call.message.message_id)
        bot.answer_callback_query(call.id, "বাতিল করা হয়েছে।")

    # ---- Admin: Add balance directly ----
    elif action == "addbal" and uid == ADMIN_ID:
        target = int(parts[1])
        msg = safe_send(cid, f"ইউজার `{target}` এর জন্য অ্যামাউন্ট দিন:", parse_mode="Markdown")
        if msg:
            bot.register_next_step_handler(
                msg, lambda m: admin_add_balance_step(m, target, call.message.message_id)
            )
        bot.answer_callback_query(call.id)

    # ---- Admin: Block/Unblock user ----
    elif action == "usrtoggle" and uid == ADMIN_ID:
        target = int(parts[1])
        new_status = "allowed" if parts[2] == "unblock" else "blocked"
        access_collection.update_one({"chat_id": target}, {"$set": {"status": new_status}})
        bot.answer_callback_query(call.id, f"স্ট্যাটাস '{new_status}' করা হয়েছে।")
        safe_delete(cid, call.message.message_id)
        send_user_detail_panel(cid, target)

    # ---- Admin: User detail panel ----
    elif action == "admuser" and uid == ADMIN_ID:
        target = int(parts[1])
        bot.answer_callback_query(call.id)
        send_user_detail_panel(cid, target)

    # ---- Page size ----
    elif action == "setlength":
        with session_lock:
            u["app_length"] = int(parts[2])
            u["app_start"] = 0
        save_session_to_db(uid, u)
        bot.answer_callback_query(call.id, f"প্রতি পেজে {parts[2]}টি সেট হয়েছে।")
        fetch_list_ui(cid, uid, parts[1], call.message.message_id)

    # ---- Pagination ----
    elif action in ["next", "prev"]:
        with session_lock:
            if action == "next":
                u["app_start"] += u["app_length"]
            else:
                u["app_start"] = max(0, u["app_start"] - u["app_length"])
        fetch_list_ui(cid, uid, parts[1], call.message.message_id)
        bot.answer_callback_query(call.id)

    # ---- Pay ----
    elif action == "pay":
        short_id = parts[1]
        enc_id = u["id_cache"].get(short_id)
        if not enc_id:
            return bot.answer_callback_query(call.id, "❌ ক্যাশ এক্সপায়ার হয়েছে!", show_alert=True)

        cost = get_service_cost(uid, "pay")
        if get_balance(uid) < cost:
            return bot.answer_callback_query(call.id, f"❌ ব্যালেন্স নেই ({cost}৳)।", show_alert=True)

        update_balance(uid, -cost)
        data = {
            'data': enc_id,
            'paymentType': 'PAYMENT_BY_DISCOUNT',
            'discountAmount': '50',
            'discountSharokNo': str(u["sharok_no"]),
            'discountSharokDate': datetime.now().strftime("%d/%m/%Y"),
            '_csrf': u["csrf"]
        }
        try:
            res = u["req_session"].post(
                "https://bdris.gov.bd/api/payment/receive",
                data=data,
                timeout=30
            )
            if res.status_code == 200:
                with session_lock:
                    u["sharok_no"] += 1
                save_session_to_db(uid, u)
                bot.answer_callback_query(call.id, "✅ পেমেন্ট সফল!")
                safe_send(cid, f"✅ পেমেন্ট সফল হয়েছে। চার্জ: {cost}৳।")
            else:
                update_balance(uid, cost)
                bot.answer_callback_query(call.id, "❌ সার্ভার রিজেক্ট করেছে। রিফান্ড করা হয়েছে।", show_alert=True)
        except Exception as e:
            logging.error(f"Payment error: {e}")
            update_balance(uid, cost)
            bot.answer_callback_query(call.id, "❌ নেটওয়ার্ক এরর। রিফান্ড করা হয়েছে।", show_alert=True)

    else:
        bot.answer_callback_query(call.id)

# ==========================================
# ১০. /start হ্যান্ডলার (FIX: ছিল না)
# ==========================================
@bot.message_handler(commands=['start'])
def start_handler(m):
    uid, cid = m.from_user.id, m.chat.id
    if not check_user_access(uid, m.from_user.first_name):
        return safe_send(cid, "🚫 অ্যাক্সেস ব্লক করা হয়েছে।")
    safe_send(
        cid,
        "🚀 *বিডিআরআইএস মাস্টার বট*\nস্বাগতম! মেনু থেকে অপশন বেছে নিন।",
        reply_markup=generate_main_menu(cid, uid),
        parse_mode="Markdown"
    )

# ==========================================
# ১১. মেইন রাউটার
# ==========================================
@bot.message_handler(func=lambda m: True)
def router(m):
    cid, uid = m.chat.id, m.from_user.id
    t = m.text or ""

    if is_rate_limited(uid):
        return safe_send(cid, "⚠️ একটু ধীরে। কিছুক্ষণ পরে আবার চেষ্টা করুন।")
    if not check_user_access(uid, m.from_user.first_name):
        return safe_send(cid, "🚫 অ্যাক্সেস ব্লক।")

    u_sess = get_session(uid)
    with session_lock:
        u_sess["last_action_time"] = time.time()
    perms = get_user_permissions(uid)

    # ---- Admin-only commands ----
    if uid == ADMIN_ID:
        if t == "🔑 Admin Login":
            msg = safe_send(cid, "এডমিন কুকি দিন (SESSION= ও TS...=):")
            if msg:
                bot.register_next_step_handler(msg, admin_login_logic)
            return
        elif t == "🛠️ Check Cookies":
            cookies = u_sess['req_session'].cookies.get_dict()
            return safe_send(
                cid,
                f"SEC: `{cookies}`\nOTP: `{u_sess['ch_otp']}`",
                parse_mode="Markdown"
            )
        elif t == "👥 Manage Users":
            users = list(access_collection.find({}))
            if not users:
                return safe_send(cid, "কোনো ইউজার নেই।")
            markup = telebot.types.InlineKeyboardMarkup()
            for user in users:
                label = f"{user.get('name', 'N/A')} | {user.get('balance', 0)}৳ | {user.get('status', 'N/A')}"
                markup.row(
                    telebot.types.InlineKeyboardButton(
                        label[:60],
                        callback_data=f"admuser:{user.get('chat_id')}"
                    )
                )
            return safe_send(cid, "👥 *ইউজার প্যানেল:*", reply_markup=markup, parse_mode="Markdown")

    # ---- User Login ----
    if t == "🔑 User Login":
        msg = safe_send(cid, "নিবন্ধক সেশন দিন (SESSION= ও TS...=):")
        if msg:
            bot.register_next_step_handler(msg, role_step_1)

    # ---- Server PDF ----
    elif t == "🖨️ Server PDF Print":
        if not (perms.get("server_pdf") or uid == ADMIN_ID):
            return safe_send(cid, "🚫 এই ফিচারে আপনার অ্যাক্সেস নেই।")
        msg = safe_send(cid, "১৭ ডিজিট UBRN দিন:")
        if msg:
            bot.register_next_step_handler(msg, download_server_by_ubrn)

    # ---- Profile & Recharge ----
    elif t == "💰 My Profile & Recharge":
        bal = get_balance(uid)
        safe_send(cid, f"💰 *আপনার প্রোফাইল*\nID: `{uid}`\nব্যালেন্স: {bal}৳\n\nরিচার্জ করতে TrxID দিন:", parse_mode="Markdown")
        msg = safe_send(cid, "TrxID ইনপুট করুন:")
        if msg:
            bot.register_next_step_handler(msg, process_recharge)

    # ---- Dashboard ----
    elif t in ("🏠 Dashboard", "/start"):
        safe_send(
            cid, "🏠 ড্যাশবোর্ড — মেনু থেকে অপশন বেছে নিন।",
            reply_markup=generate_main_menu(cid, uid)
        )

    # ---- Category lists (requires login) ----
    elif t == "📋 Applications" and u_sess["is_alive"] and (perms.get("apps") or uid == ADMIN_ID):
        handle_category_init(m, 'apps')
    elif t == "📝 Correction" and u_sess["is_alive"] and (perms.get("corr") or uid == ADMIN_ID):
        handle_category_init(m, 'corr')
    elif t == "🔄 Reprint" and u_sess["is_alive"] and (perms.get("repr") or uid == ADMIN_ID):
        handle_category_init(m, 'repr')

    # ---- Not logged in guard ----
    elif t in ("📋 Applications", "📝 Correction", "🔄 Reprint"):
        safe_send(cid, "❌ এই ফিচার ব্যবহার করতে আগে লগইন করুন।")

    # ---- Fallback ----
    else:
        safe_send(
            cid, "🚀 বিডিআরআইএস মাস্টার বট। মেনু থেকে অপশন বেছে নিন।",
            reply_markup=generate_main_menu(cid, uid)
        )

# ==========================================
# ১২. এক্সেকিউশন
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "BDRIS BOT ACTIVE"

if __name__ == "__main__":
    Thread(target=keep_sessions_alive_and_cleanup, daemon=True).start()
    Thread(
        target=lambda: app.run(host='0.0.0.0', port=10000, use_reloader=False),
        daemon=True
    ).start()
    logging.info("🚀 Bot is Polling...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            logging.error(f"Restarting... {e}")
            time.sleep(5)
