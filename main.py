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
import hashlib
from threading import Thread
from datetime import datetime
from urllib.parse import quote
from collections import OrderedDict
from flask import Flask
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# ==========================================
# ০. গ্লোবাল ভেরিয়েবল ও থ্রেড লক
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
session_lock = threading.Lock()
download_lock = threading.Lock()
active_downloads = set()

# প্রি-কম্পাইল্ড রেজেক্স (Super Flexible for Cookies)
_COOKIE_RE = re.compile(r'SESSION\s*[:=]?\s*([A-Za-z0-9_-]+)', re.I)
_TS_RE = re.compile(r'TS01[A-Za-z0-9]*\s*[:=]?\s*([A-Za-z0-9_-]+)', re.I)
_CSRF_RE = re.compile(r'name="_csrf"\s+content="([^"]+)"')
_DATA_ID_RE = re.compile(r'href=".*?\?data=([A-Za-z0-9_\-]+)"')
_PHONE_RE = re.compile(r'^(\+?880|0)1[3-9]\d{8}$')

VALID_CMDS = frozenset(['apps', 'corr', 'repr'])
DEFAULT_PERMS = {"apps": True, "corr": True, "repr": True, "search": True, "ubrn_update": True, "server_pdf": True, "print": True}
SERVICE_COSTS = {"pdf": 25, "pay": 25, "server_pdf_login": 25, "server_pdf_no_login": 50}

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
    recharge_logs = db['recharge_logs']
    
    if not settings_collection.find_one({"_id": "config"}):
        settings_collection.insert_one({"_id": "config", "payment_active": True})
    logging.info("✅ MongoDB Connected Successfully!")
except Exception as e:
    logging.critical(f"❌ DB Connection Failed: {e}")
    sys.exit(1)

# ==========================================
# ২. সেফ র‍্যাপারস ও ইউজার লজিক
# ==========================================
def sanitize_name(name_str):
    """ Removes Markdown breaking characters """
    return re.sub(r'[*_`\[\]()]', '', str(name_str))

def safe_send(chat_id, text, **kwargs):
    try: return bot.send_message(chat_id, text, **kwargs)
    except Exception as e: logging.error(f"Send Error: {e}"); return None

def safe_edit(chat_id, message_id, text, **kwargs):
    try: return bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception: return None

def safe_delete(chat_id, message_id):
    try: bot.delete_message(chat_id, message_id)
    except Exception: pass

def is_payment_active():
    config = settings_collection.find_one({"_id": "config"})
    return config.get("payment_active", True) if config else True

def get_service_cost(user_id, service="default"):
    if user_id == ADMIN_ID or not is_payment_active(): return 0
    return SERVICE_COSTS.get(service, 25)

def get_balance(user_id):
    if user_id == ADMIN_ID: return 999999
    record = access_collection.find_one({"chat_id": user_id})
    return int(record.get("balance", 0)) if record else 0

def update_balance(user_id, amount):
    if user_id == ADMIN_ID: return
    access_collection.update_one({"chat_id": user_id}, {"$inc": {"balance": amount}})

def deduct_balance(user_id, amount):
    """ Atomic balance deduction preventing negative balance """
    if user_id == ADMIN_ID or amount <= 0: return True
    res = access_collection.update_one(
        {"chat_id": user_id, "balance": {"$gte": amount}}, 
        {"$inc": {"balance": -amount}}
    )
    return res.modified_count > 0

def check_user_access(user_id, user_name):
    if user_id == ADMIN_ID: return True
    user_record = access_collection.find_one({"chat_id": user_id})
    if not user_record:
        access_collection.insert_one({
            "chat_id": user_id, "name": str(user_name)[:100], "status": "allowed", 
            "permissions": DEFAULT_PERMS.copy(), "balance": 0
        })
        safe_name = sanitize_name(user_name)
        safe_send(ADMIN_ID, f"🔔 *নতুন ইউজার!*\n👤 {safe_name}\n🆔 `{user_id}`", parse_mode="Markdown")
        return True
    return user_record.get("status") == "allowed"

def get_user_permissions(user_id):
    if user_id == ADMIN_ID: return {k: True for k in DEFAULT_PERMS}
    record = access_collection.find_one({"chat_id": user_id})
    if record and "permissions" in record:
        p = DEFAULT_PERMS.copy()
        p.update(record["permissions"])
        return p
    return DEFAULT_PERMS.copy()

# ==========================================
# ৩. সেশন ম্যানেজমেন্ট (Persistence & True LRU Cache)
# ==========================================
user_sessions = {}

def get_default_session_dict():
    return {
        "req_session": requests.Session(), "csrf": "", "ch_session": requests.Session(), "ch_csrf": "", "ch_otp": "",
        "mode": "SECRETARY", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "is_alive": False, "sec_alive": False, "ch_alive": False,
        "current_page": "https://bdris.gov.bd/admin/",
        "app_start": 0, "app_length": 5, "sharok_no": 1, "temp_data": {}, 
        "id_cache": OrderedDict(),
        "last_action_time": time.time(), "last_warning_time": 0.0, "current_search_val": ""
    }

def get_session(user_id):
    with session_lock:
        if user_id in user_sessions: return user_sessions[user_id]
    
    u_sess = get_default_session_dict()
    try:
        db_data = sessions_collection.find_one({"chat_id": user_id})
        if db_data:
            u_sess["req_session"].cookies.update(db_data.get("sec_cookies", {}))
            u_sess["ch_session"].cookies.update(db_data.get("ch_cookies", {}))
            u_sess.update({
                "mode": db_data.get("mode", "SECRETARY"), "ch_otp": db_data.get("ch_otp", ""),
                "is_alive": db_data.get("is_alive", False), "sec_alive": db_data.get("sec_alive", False),
                "ch_alive": db_data.get("ch_alive", False), "sharok_no": db_data.get("sharok_no", 1),
                "app_length": db_data.get("app_length", 5)
            })
    except Exception as e: logging.error(f"DB Load Error: {e}")

    with session_lock:
        if user_id not in user_sessions: user_sessions[user_id] = u_sess
        return user_sessions[user_id]

def save_session_to_db(user_id, u_sess):
    try:
        data = {
            "chat_id": user_id, "sec_cookies": u_sess["req_session"].cookies.get_dict(),
            "ch_cookies": u_sess["ch_session"].cookies.get_dict(), "mode": u_sess["mode"],
            "ch_otp": u_sess.get("ch_otp", ""), "is_alive": u_sess["is_alive"],
            "sec_alive": u_sess.get("sec_alive", False), "ch_alive": u_sess.get("ch_alive", False),
            "sharok_no": u_sess.get("sharok_no", 1), "app_length": u_sess.get("app_length", 5)
        }
        sessions_collection.update_one({"chat_id": user_id}, {"$set": data}, upsert=True)
    except Exception as e: logging.error(f"DB Save Error: {e}")

def keep_sessions_alive_and_cleanup():
    while True:
        time.sleep(300)
        now = time.time()

        with session_lock:
            expired_users = [
                uid for uid, s in user_sessions.items()
                if not s.get("sec_alive", False) and not s.get("ch_alive", False)
                and (now - s.get("last_action_time", now)) > 3600
            ]
            for uid in expired_users: del user_sessions[uid]
            active_users = [
                (uid, u_sess["ua"], u_sess["req_session"], u_sess["ch_session"],
                 u_sess.get("sec_alive", False), u_sess.get("ch_alive", False))
                for uid, u_sess in user_sessions.items()
                if u_sess.get("sec_alive", False) or u_sess.get("ch_alive", False)
            ]

        for uid in expired_users:
            try: sessions_collection.update_one(
                {"chat_id": uid},
                {"$set": {"is_alive": False, "sec_alive": False, "ch_alive": False}}
            )
            except Exception as e: logging.warning(f"DB cleanup error [{uid}]: {e}")

        for uid, ua, req_sess, ch_sess, sec_alive, ch_alive in active_users:
            new_sec_alive = False
            new_ch_alive = False
            new_csrf = None
            new_ch_csrf = None

            if sec_alive:
                try:
                    res = req_sess.get("https://bdris.gov.bd/admin/", headers={'User-Agent': ua}, timeout=15)
                    if 'login' not in res.url.lower():
                        new_sec_alive = True
                        c = _CSRF_RE.search(res.text)
                        if c: new_csrf = c.group(1)
                    else:
                        logging.info(f"SEC session expired [{uid}]")
                except Exception as e:
                    logging.warning(f"SEC keepalive error [{uid}]: {e}")
                    new_sec_alive = True

            if ch_alive:
                try:
                    res = ch_sess.get("https://bdris.gov.bd/admin/", headers={'User-Agent': ua}, timeout=15)
                    if 'login' not in res.url.lower():
                        new_ch_alive = True
                        c = _CSRF_RE.search(res.text)
                        if c: new_ch_csrf = c.group(1)
                    else:
                        logging.info(f"CH session expired [{uid}]")
                except Exception as e:
                    logging.warning(f"CH keepalive error [{uid}]: {e}")
                    new_ch_alive = True

            with session_lock:
                if uid not in user_sessions: continue
                if new_csrf: user_sessions[uid]["csrf"] = new_csrf
                if new_ch_csrf: user_sessions[uid]["ch_csrf"] = new_ch_csrf
                user_sessions[uid]["sec_alive"] = new_sec_alive
                user_sessions[uid]["ch_alive"] = new_ch_alive
                user_sessions[uid]["is_alive"] = new_sec_alive or new_ch_alive

            try:
                sessions_collection.update_one({"chat_id": uid}, {"$set": {
                    "sec_alive": new_sec_alive,
                    "ch_alive": new_ch_alive,
                    "is_alive": new_sec_alive or new_ch_alive
                }})
            except Exception as e: logging.warning(f"DB update error [{uid}]: {e}")

def is_rate_limited(user_id):
    u_sess = get_session(user_id)
    now = time.time()
    trigger_warning = False
    is_limited = False
    
    with session_lock:
        if now - u_sess.get("last_action_time", now) < 1.5:
            is_limited = True
            if now - u_sess.get("last_warning_time", 0) > 5:
                u_sess["last_warning_time"] = now
                trigger_warning = True
        else:
            u_sess["last_action_time"] = now
            
    if trigger_warning:
        safe_send(user_id, "⚠️ *একটু ধীরে!* ২ সেকেন্ড অপেক্ষা করুন।", parse_mode="Markdown")
        
    return is_limited

def is_cancel(m):
    if not m or not m.text: return False
    if any(m.text.startswith(kw) for kw in ("/start", "Back to Menu", "Dashboard", "🏠 Dashboard")):
        bot.clear_step_handler_by_chat_id(m.chat.id)
        safe_send(m.chat.id, "🏠 মেনুতে ফিরে আসা হলো।", reply_markup=generate_main_menu(m.chat.id, m.from_user.id))
        return True
    return False

# ==========================================
# ৪. কিবোর্ড ও UI লজিক
# ==========================================
def generate_main_menu(chat_id, user_id=None):
    if not user_id: user_id = chat_id
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    u_sess = get_session(user_id)
    perms = get_user_permissions(user_id)

    markup.row("🔑 User Login")
    if not u_sess["is_alive"] and (perms.get("server_pdf") or user_id == ADMIN_ID):
        markup.row("🖨️ Server PDF Print")
        if is_payment_active(): markup.row("💰 My Profile & Recharge")

    if u_sess["is_alive"]:
        if is_payment_active(): markup.row("💰 My Profile & Recharge")
        markup.row("👤 নিবন্ধক সেকশন", "🧑‍💼 অথোরাইজড ইউজার")
        row_core = []
        if perms.get("apps") or user_id == ADMIN_ID: row_core.append("📋 Applications")
        if perms.get("corr") or user_id == ADMIN_ID: row_core.append("📝 Correction")
        if perms.get("repr") or user_id == ADMIN_ID: row_core.append("🔄 Reprint")
        if row_core: markup.row(*row_core)
        row_search = ["🏠 Dashboard"]
        if perms.get("search") or user_id == ADMIN_ID: row_search.extend(["🌐 Search By Name", "🔢 Search By UBRN"])
        markup.row(*row_search)
        row_tools = []
        if perms.get("ubrn_update") or user_id == ADMIN_ID: row_tools.append("👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ")
        if perms.get("server_pdf") or user_id == ADMIN_ID: row_tools.append("🖨️ Server PDF Print")
        if row_tools: markup.row(*row_tools)

    if user_id == ADMIN_ID: markup.row("🔑 Admin Login", "🛠️ Check Cookies", "👥 Manage Users")
    return markup

# ==========================================
# ৫. কোর API রিকোয়েস্ট ফাংশন
# ==========================================
def extract_sid_tsid(text):
    """ Smart and flexible extraction for Session and TS cookies """
    text = text.strip()
    
    # Step 1: Try flexible Regex match first
    s = _COOKIE_RE.search(text)
    t = _TS_RE.search(text)
    if s and t:
        return s.group(1), t.group(1)
        
    # Step 2: Ultimate Fallback (if user just pasted two long strings blindly)
    tokens = [tok.strip() for tok in re.split(r'[\s;,"\'\n\r]+', text) if len(tok.strip()) >= 15]
    if len(tokens) >= 2:
        # 'TS' token is usually longer and often starts with '01'
        if tokens[1].startswith('01') or len(tokens[1]) > len(tokens[0]):
            return tokens[0], tokens[1]
        elif tokens[0].startswith('01'):
            return tokens[1], tokens[0]
        else:
            return tokens[0], tokens[1]
            
    return None, None

def get_active_session(u_sess):
    sec_ok = u_sess.get("sec_alive", False)
    ch_ok = u_sess.get("ch_alive", False)
    if u_sess["mode"] == "CHAIRMAN":
        if ch_ok: return (u_sess["ch_session"], u_sess["ch_csrf"])
        return (u_sess["req_session"], u_sess["csrf"])
    else:
        if sec_ok: return (u_sess["req_session"], u_sess["csrf"])
        return (u_sess["ch_session"], u_sess["ch_csrf"])

def _set_session_cookies(sess, sid, tsid):
    sess.cookies.clear()
    sess.cookies.set("SESSION", sid, domain='bdris.gov.bd')
    sess.cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')

def call_api(user_id, url, method="GET", data=None, extra_headers=None, retries=2, force_sec=False):
    u_sess = get_session(user_id)
    if force_sec:
        sess, csrf = u_sess["req_session"], u_sess["csrf"]
    else:
        sess, csrf = get_active_session(u_sess)
        
    headers = {'x-csrf-token': csrf, 'x-requested-with': 'XMLHttpRequest', 'user-agent': u_sess["ua"], 'referer': u_sess["current_page"]}
    if extra_headers: headers.update(extra_headers)
        
    for attempt in range(retries):
        try:
            if method == "POST": return sess.post(url, headers=headers, data=data, timeout=30)
            return sess.get(url, headers=headers, timeout=30)
        except:
            if attempt < retries - 1: time.sleep(1)
    return None

def navigate_to(user_id, url):
    u_sess = get_session(user_id)
    sess, _ = get_active_session(u_sess)
    try:
        res = sess.get(url, headers={'User-Agent': u_sess["ua"], 'Referer': u_sess["current_page"]}, timeout=25)
        match = _CSRF_RE.search(res.text)
        if match:
            with session_lock:
                if u_sess["mode"] == "CHAIRMAN": u_sess["ch_csrf"] = match.group(1)
                else: u_sess["csrf"] = match.group(1)
        u_sess["current_page"] = url
        return True, res.text
    except Exception as e:
        logging.error(f"❌ Navigate Error [{url}]: {e}")
        return False, None

# ==========================================
# ৬. লগইন ফ্লো ও রিচার্জ
# ==========================================
def admin_login_logic(m):
    if is_cancel(m): return
    sid, tsid = extract_sid_tsid(m.text or "")
    uid = m.from_user.id
    if sid and tsid:
        u_sess = get_session(uid)
        with session_lock:
            _set_session_cookies(u_sess["req_session"], sid, tsid)
            u_sess["sec_alive"] = True
            u_sess["is_alive"] = True
        save_session_to_db(uid, u_sess)
        safe_send(m.chat.id, "✅ এডমিন সেশন সেট হয়েছে!", reply_markup=generate_main_menu(m.chat.id, uid))
    else:
        msg = safe_send(m.chat.id, "❌ ভুল ফরম্যাট! SESSION= ও TS01...= সহ বা শুধু ভ্যালুগুলো দিন:")
        if msg: bot.register_next_step_handler(msg, admin_login_logic)

def role_step_1(m):
    if is_cancel(m): return
    uid = m.from_user.id
    sid, tsid = extract_sid_tsid(m.text or "")
    if not sid or not tsid:
        msg = safe_send(m.chat.id, "❌ সঠিক কুকি ফরম্যাট পাওয়া যায়নি। আবার দিন:")
        if msg: bot.register_next_step_handler(msg, role_step_1)
        return
    u_sess = get_session(uid)
    with session_lock:
        _set_session_cookies(u_sess["ch_session"], sid, tsid)
        u_sess["ch_alive"] = True
    msg = safe_send(m.chat.id, "✅ নিবন্ধক সেশন গৃহীত। এখন নিবন্ধকের OTP দিন:")
    if msg: bot.register_next_step_handler(msg, role_step_2)

def role_step_2(m):
    if is_cancel(m): return
    otp = m.text.strip() if m.text else ""
    if not otp.isdigit():
        msg = safe_send(m.chat.id, "❌ OTP শুধু সংখ্যা হবে। আবার দিন:")
        if msg: bot.register_next_step_handler(msg, role_step_2)
        return
    with session_lock:
        get_session(m.from_user.id)["ch_otp"] = otp
    msg = safe_send(m.chat.id, "✅ OTP সংরক্ষিত। এখন অথোরাইজড ইউজার সেশন দিন:")
    if msg: bot.register_next_step_handler(msg, role_step_3)

def role_step_3(m):
    if is_cancel(m): return
    sid, tsid = extract_sid_tsid(m.text or "")
    uid = m.from_user.id
    if sid and tsid:
        u_sess = get_session(uid)
        with session_lock:
            _set_session_cookies(u_sess["req_session"], sid, tsid)
            u_sess["sec_alive"] = True
            u_sess["is_alive"] = True
        save_session_to_db(uid, u_sess)
        safe_send(m.chat.id, "🎉 লগইন সফল!", reply_markup=generate_main_menu(m.chat.id, uid))
    else:
        msg = safe_send(m.chat.id, "❌ ভুল ইউজার কুকি। আবার দিন:")
        if msg: bot.register_next_step_handler(msg, role_step_3)

def process_recharge(m):
    if is_cancel(m): return
    trxid = m.text.strip() if m.text else ""
    if not (5 <= len(trxid) <= 50):
        msg = safe_send(m.chat.id, "❌ অবৈধ TrxID। আবার দিন:")
        if msg: bot.register_next_step_handler(msg, process_recharge)
        return
    uid = m.from_user.id
    
    try:
        recharge_logs.insert_one({"_id": trxid, "user_id": uid, "status": "pending", "date": datetime.now()})
    except DuplicateKeyError:
        return safe_send(m.chat.id, "❌ এই TrxID ইতিমধ্যে ব্যবহৃত বা পেন্ডিং আছে।", reply_markup=generate_main_menu(m.chat.id, uid))
    
    safe_send(m.chat.id, "✅ আপনার রিচার্জ রিকোয়েস্ট পাঠানো হয়েছে।", reply_markup=generate_main_menu(m.chat.id, uid))
    
    safe_name = sanitize_name(m.from_user.first_name)
    msg_text = f"💰 *নতুন রিচার্জ রিকোয়েস্ট!*\n👤 User: {safe_name} (`{uid}`)\n📝 TrxID: `{trxid}`"
    markup = telebot.types.InlineKeyboardMarkup().row(
        telebot.types.InlineKeyboardButton("✅ Approve", callback_data=f"apprvbal:{uid}:{trxid}"),
        telebot.types.InlineKeyboardButton("❌ Reject", callback_data=f"rejbal:{uid}:{trxid}")
    )
    safe_send(ADMIN_ID, msg_text, reply_markup=markup, parse_mode="Markdown")

def admin_add_balance_step(m, target_id, trxid, admin_msg_id):
    if is_cancel(m): return
    try:
        amount = int(m.text.strip() if m.text else "")
        if not (0 < amount <= 50000): raise ValueError
        update_balance(target_id, amount)
        recharge_logs.update_one({"_id": trxid}, {"$set": {"status": "approved"}})
        
        safe_send(m.chat.id, f"✅ User {target_id} এর অ্যাকাউন্টে {amount}৳ যোগ হয়েছে।")
        safe_send(target_id, f"🎉 *রিচার্জ সফল!*\nযোগ হয়েছে: {amount}৳\nব্যালেন্স: {get_balance(target_id)}৳", parse_mode="Markdown")
        safe_delete(m.chat.id, admin_msg_id)
    except Exception:
        msg = safe_send(m.chat.id, "❌ ভুল ইনপুট। ১ থেকে ৫০০০০ এর মধ্যে সংখ্যা দিন।")
        if msg: bot.register_next_step_handler(msg, lambda m_: admin_add_balance_step(m_, target_id, trxid, admin_msg_id))

# ==========================================
# ৭. সার্ভার পিডিএফ ও ডাউনলোড (Centralized Refund)
# ==========================================
def download_server_by_ubrn(m):
    if is_cancel(m): return
    uid, cid = m.from_user.id, m.chat.id
    ubrn = m.text.strip() if m.text else ""
    if not (ubrn.isdigit() and len(ubrn) == 17):
        msg = safe_send(cid, "❌ সঠিক ১৭ ডিজিট UBRN দিন:")
        if msg: bot.register_next_step_handler(msg, download_server_by_ubrn)
        return

    u_sess = get_session(uid)
    working_uid = uid if u_sess["is_alive"] else ADMIN_ID
    if working_uid == ADMIN_ID and not get_session(ADMIN_ID)["is_alive"]:
        return safe_send(cid, "❌ সিস্টেম অফলাইন। অ্যাডমিন সেশন নেই।")

    cost = get_service_cost(uid, "server_pdf_login" if working_uid == uid else "server_pdf_no_login")
    task_id = f"dl_{uid}_{ubrn}"
    
    with download_lock:
        if task_id in active_downloads: 
            return safe_send(cid, "⚠️ অলরেডি প্রসেসিং হচ্ছে...")
        active_downloads.add(task_id)
    
    if cost > 0:
        if not deduct_balance(uid, cost):
            with download_lock: active_downloads.discard(task_id)
            return safe_send(cid, f"❌ পর্যাপ্ত ব্যালেন্স ({cost}৳) নেই।")

    wait = safe_send(cid, f"⏳ সার্ভারে খোঁজা হচ্ছে... (চার্জ: {cost}৳)")
    
    def fetch_and_send():
        try:
            res = call_api(working_uid, f"https://bdris.gov.bd/api/br/info/ubrn/{ubrn}")
            if wait: safe_delete(cid, wait.message_id)
            
            if res and res.status_code == 200 and 'encryptedId' in res.json():
                download_server_pdf(cid, working_uid, res.json()['encryptedId'], f"PDF_{ubrn}")
                safe_send(cid, f"✅ ডাউনলোড সফল! বর্তমান ব্যালেন্স: {get_balance(uid)}৳")
            else:
                raise ValueError("ID Not Found")
        except Exception as e:
            logging.error(f"Download Fetch Error: {e}")
            if cost > 0: update_balance(uid, cost) 
            safe_send(cid, "❌ সার্ভার এরর বা ডেটা পাওয়া যায়নি। টাকা রিফান্ড করা হয়েছে।")
        finally:
            with download_lock: active_downloads.discard(task_id)
            
    Thread(target=fetch_and_send, daemon=True).start()

def download_server_pdf(chat_id, session_uid, enc_id, filename):
    u = get_session(session_uid)
    sess, _ = get_active_session(u)
    safe_send(chat_id, "📥 পিডিএফ জেনারেট হচ্ছে...")
    sess.get(f"https://bdris.gov.bd/admin/new-certificate/check?data={enc_id}", timeout=30)
    res = sess.get(f"https://bdris.gov.bd/admin/new-certificate/print?data={enc_id}", timeout=60)
    
    if 'application/pdf' in res.headers.get('Content-Type', ''):
        try:
            bot.send_document(chat_id, io.BytesIO(res.content), visible_file_name=f"{filename}.pdf")
        except Exception as e:
            logging.error(f"Telegram Document Send Failed: {e}")
            raise RuntimeError("Telegram API Failed")
    else:
        raise ValueError("Invalid Content-Type from Server")

# ==========================================
# ৮. অ্যাপ লিস্ট লজিক ও পেজিনেশন (Data Caching)
# ==========================================
def handle_category_init(m, cmd):
    if cmd not in VALID_CMDS: return safe_send(m.chat.id, "❌ অজানা কমান্ড।")
    with session_lock:
        u_sess = get_session(m.from_user.id)
        u_sess["app_start"] = 0
        u_sess["current_search_val"] = ""
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔍 Search ID", "📋 All List", "🏠 Back to Menu")
    msg = safe_send(m.chat.id, f"📂 {cmd.upper()} সেকশন:", reply_markup=markup)
    if msg: bot.register_next_step_handler(msg, category_gate, cmd)

def category_gate(m, cmd):
    if is_cancel(m): return
    if m.text and "Search ID" in m.text:
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
        msg = safe_send(m.chat.id, "🆔 আইডি দিন:", reply_markup=markup)
        if msg: bot.register_next_step_handler(msg, search_loop, cmd)
    elif m.text and "All List" in m.text: 
        with session_lock: get_session(m.from_user.id)["current_search_val"] = ""
        fetch_list_ui(m.chat.id, m.from_user.id, cmd)
    else:
        msg = safe_send(m.chat.id, "⚠️ সঠিক অপশন বেছে নিন:")
        if msg: bot.register_next_step_handler(msg, category_gate, cmd)

def search_loop(m, cmd):
    if is_cancel(m): return
    if m.text:
        with session_lock: get_session(m.from_user.id)["current_search_val"] = m.text.strip()
        fetch_list_ui(m.chat.id, m.from_user.id, cmd)
    msg = safe_send(m.chat.id, "🔍 আরও আইডি দিন (বা মেনুতে ফিরুন):")
    if msg: bot.register_next_step_handler(msg, search_loop, cmd)

def fetch_list_ui(chat_id, user_id, cmd, message_id=None):
    if cmd not in VALID_CMDS: return safe_send(chat_id, "❌ অজানা কমান্ড।")
    u_sess = get_session(user_id)
    perms = get_user_permissions(user_id)
    search_val = u_sess.get("current_search_val", "")
    
    config = {'apps': ("/admin/br/applications/search", "/api/br/applications/search"),
              'corr': ("/admin/br/correction-applications/search", "/api/br/correction-applications/search"),
              'repr': ("/admin/br/reprint/view/applications/search", "/api/br/reprint/applications/search")}
    
    data_id_key = f"{cmd}_{u_sess['mode']}_data_id"
    data_id = u_sess["temp_data"].get(data_id_key)
    
    if not data_id:
        success, html = navigate_to(user_id, f"https://bdris.gov.bd{config[cmd][0]}")
        if not success or not html: return safe_send(chat_id, "❌ পেজ লোড ব্যর্থ।")
        id_match = _DATA_ID_RE.search(html)
        data_id = id_match.group(1) if id_match else None
        if not data_id: return safe_send(chat_id, "❌ ডাটা আইডি মেলেনি।")
        with session_lock: u_sess["temp_data"][data_id_key] = data_id

    url = (f"https://bdris.gov.bd{config[cmd][1]}?data={data_id}&status=ALL&draw=1"
           f"&start={u_sess['app_start']}&length={u_sess['app_length']}&search[value]={quote(search_val)}&search[regex]=false&order[0][column]=1&order[0][dir]=desc")
    res = call_api(user_id, url)
    
    if not res or res.status_code != 200: 
        with session_lock: u_sess["temp_data"].pop(data_id_key, None)
        return safe_send(chat_id, "❌ ডেটা লোড ব্যর্থ।")
        
    try: resp_json = res.json()
    except Exception: return safe_send(chat_id, "❌ রেসপন্স পার্স এরর।")
        
    items = resp_json.get('data', [])
    if not items:
        if message_id: safe_delete(chat_id, message_id)
        return safe_send(chat_id, "📭 কোনো ডেটা পাওয়া যায়নি।")
        
    markup = telebot.types.InlineKeyboardMarkup()
    mode_text = "নিবন্ধক সেকশন" if u_sess['mode'] == "CHAIRMAN" else "অথোরাইজড ইউজার"
    msg_text = f"📋 *{cmd.upper()} List* ({mode_text}):\n\n"
    pay_cost = get_service_cost(user_id, "pay")
    pay_btn_text = f"💳 Pay ({pay_cost}৳)" if pay_cost > 0 else "💳 Pay"
    
    for item in items:
        enc_id = item.get('encryptedId')
        status = str(item.get('status', '')).upper()
        if not enc_id: continue
        
        short_id = hashlib.md5(enc_id.encode()).hexdigest()[:8]
        
        with session_lock:
            u_sess["id_cache"][short_id] = enc_id
            u_sess["id_cache"].move_to_end(short_id)
            while len(u_sess["id_cache"]) > 300:
                u_sess["id_cache"].popitem(last=False)
            
        app_id = item.get('id') or item.get('applicationId') or 'N/A'
        person_name = sanitize_name(item.get('personNameBn') or 'নাম অজানা')
        msg_text += f"🆔 `{app_id}` | {person_name}\n🚩 Status: `{status}`\n"
        
        btns = []
        if u_sess["mode"] == "CHAIRMAN" and "RECEIVED" in status:
            btns.append(telebot.types.InlineKeyboardButton("✅ Register", callback_data=f"reg:{short_id}")) if cmd == 'apps' else btns.append(telebot.types.InlineKeyboardButton("📝 Corr", callback_data=f"coreg:{short_id}"))
        elif u_sess["mode"] == "SECRETARY" and any(w in status for w in ["APPLIED", "PENDING", "PAYMENT", "UNPAID"]):
            btns.extend([
                telebot.types.InlineKeyboardButton(pay_btn_text, callback_data=f"pay:{short_id}"),
                telebot.types.InlineKeyboardButton("📥 Receive", callback_data=f"recv:{short_id}")
            ])
        if btns: markup.row(*btns)
        if perms.get("print") or user_id == ADMIN_ID:
            markup.row(telebot.types.InlineKeyboardButton("🖨️ Print PDF", callback_data=f"print:{short_id}"))
        msg_text += "━━━━━━━━━━━━━━\n"
        
    if not search_val:
        nav = []
        total = int(resp_json.get('recordsTotal', 0))
        if u_sess["app_start"] > 0: nav.append(telebot.types.InlineKeyboardButton("⬅️ Prev", callback_data=f"prev:{cmd}"))
        if u_sess["app_start"] + u_sess["app_length"] < total: nav.append(telebot.types.InlineKeyboardButton("Next ➡️", callback_data=f"next:{cmd}"))
        
        lengths = []
        for ln in [5, 10, 20]:
            label = f"{'✅' if u_sess['app_length'] == ln else ''}{ln}/p"
            lengths.append(telebot.types.InlineKeyboardButton(label, callback_data=f"setlength:{cmd}:{ln}"))
            
        if nav: markup.row(*nav)
        if lengths: markup.row(*lengths)
        
    if len(msg_text) > 4000: msg_text = msg_text[:4000] + "\n\n⚠️ বাকি তথ্য কাটা গেছে।"
    
    if message_id: safe_edit(chat_id, message_id, msg_text, reply_markup=markup, parse_mode='Markdown')
    else: safe_send(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')

# ==========================================
# ৯. Search & UBRN Update
# ==========================================
def process_search_by_name(m):
    if is_cancel(m): return
    if not m.text: return
    
    uid = m.from_user.id
    raw_name = m.text.strip()
    safe_name = sanitize_name(raw_name)
    
    payload = f"personNameBn={quote(raw_name)}&personNameEn=&nameLang=BENGALI"
    extra_h = {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}
    
    navigate_to(uid, "https://bdris.gov.bd/admin/br/advanced-search-by-name")
    res = call_api(uid, "https://bdris.gov.bd/api/br/advanced-search-by-name", 
                   method="POST", data=payload, extra_headers=extra_h)
    
    if res and res.status_code == 200:
        try:
            data = res.json()
            items = data if isinstance(data, list) else data.get('data', [data])
            
            if not items:
                safe_send(m.chat.id, f"📭 *'{safe_name}'* নামে কোনো তথ্য পাওয়া যায়নি।", parse_mode='Markdown')
            else:
                msg_text = f"📊 *Search: {safe_name}* — {len(items)} টি ফলাফল\n\n"
                for i, item in enumerate(items[:10], 1):
                    ubrn = item.get('ubrn') or item.get('birthRegistrationNo', 'N/A')
                    name = sanitize_name(item.get('personNameBn') or item.get('name', 'অজানা'))
                    dob = item.get('dateOfBirth', '')
                    msg_text += f"{i}. 👤 {name}\n   🔢 UBRN: `{ubrn}`\n   📅 DOB: {dob}\n\n"
                
                if len(items) > 10:
                    msg_text += f"_...আরও {len(items)-10} টি ফলাফল আছে_\n"
                    
                sent = safe_send(m.chat.id, msg_text[:4000], parse_mode='Markdown')
                if not sent: safe_send(m.chat.id, "❌ ডেটা অনেক বড়, পাঠানো সম্ভব হয়নি।")
                
        except Exception as e:
            logging.error(f"Search Result Error [{uid}]: {e}")
            safe_send(m.chat.id, "❌ ডেটা প্রসেস করতে সমস্যা হয়েছে।")
    else:
        safe_send(m.chat.id, "❌ কোনো তথ্য পাওয়া যায়নি বা সার্ভার এরর।")
    
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
    msg = safe_send(m.chat.id, "🔍 আরও নাম দিন:", reply_markup=markup)
    if msg: bot.register_next_step_handler(msg, process_search_by_name)

def process_search_by_ubrn(m):
    if is_cancel(m): return
    if not m.text: return
    uid = m.from_user.id
    ubrn = m.text.strip()
    if not ubrn.isdigit() or len(ubrn) != 17:
        msg = safe_send(m.chat.id, "❌ UBRN অবশ্যই ১৭ ডিজিট হতে হবে। আবার দিন:")
        if msg: bot.register_next_step_handler(msg, process_search_by_ubrn)
        return

    res = call_api(uid, f"https://bdris.gov.bd/api/br/info/ubrn/{ubrn}")
    if res and res.status_code == 200:
        try:
            data = res.json()
            if not data or 'encryptedId' not in data:
                safe_send(m.chat.id, f"📭 `{ubrn}` এর কোনো তথ্য পাওয়া যায়নি।", parse_mode='Markdown')
            else:
                name = sanitize_name(data.get('personNameBn', 'অজানা'))
                dob = data.get('dateOfBirth', 'N/A')
                fname = sanitize_name(data.get('fatherNameBn', 'N/A'))
                mname = sanitize_name(data.get('motherNameBn', 'N/A'))
                status = data.get('status', 'N/A')
                
                msg_text = f"📊 *UBRN Result:*\n\n👤 *নাম:* {name}\n🔢 *UBRN:* `{ubrn}`\n📅 *DOB:* {dob}\n👨 *পিতা:* {fname}\n👩 *মাতা:* {mname}\n🚩 *স্ট্যাটাস:* `{status}`"
                safe_send(m.chat.id, msg_text, parse_mode='Markdown')
        except Exception as e: 
            logging.error(f"UBRN Parse Error: {e}")
            safe_send(m.chat.id, "❌ ডেটা পার্স করা যায়নি।")
    else: 
        safe_send(m.chat.id, "❌ তথ্য পাওয়া যায়নি।")
    
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
    msg = safe_send(m.chat.id, "🔍 আরও UBRN দিন:", reply_markup=markup)
    if msg: bot.register_next_step_handler(msg, process_search_by_ubrn)

def start_ubrn_flow(m):
    u_sess = get_session(m.from_user.id)
    with session_lock: u_sess["temp_data"]["ubrn"] = {}
    navigate_to(m.from_user.id, "https://bdris.gov.bd/admin/br/parents-ubrn-update")
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
    msg = safe_send(m.chat.id, "১. ব্যক্তির ১৭ ডিজিট UBRN দিন:", reply_markup=markup)
    if msg: bot.register_next_step_handler(msg, ubrn_p_step)

def _validate_ubrn_input(text):
    t = text.strip()
    if t == '0': return ''
    if not t.isdigit() or len(t) != 17: return None
    return t

def ubrn_p_step(m):
    if is_cancel(m): return
    if not m.text: return
    val = _validate_ubrn_input(m.text)
    if val is None:
        msg = safe_send(m.chat.id, "❌ সঠিক ১৭ ডিজিট UBRN দিন:")
        if msg: bot.register_next_step_handler(msg, ubrn_p_step)
        return
    with session_lock: get_session(m.from_user.id)["temp_data"]["ubrn"]["p"] = val
    msg = safe_send(m.chat.id, "২. পিতার UBRN দিন (না থাকলে 0 দিন):")
    if msg: bot.register_next_step_handler(msg, ubrn_f_step)

def ubrn_f_step(m):
    if is_cancel(m): return
    if not m.text: return
    val = _validate_ubrn_input(m.text)
    if val is None:
        msg = safe_send(m.chat.id, "❌ সঠিক ১৭ ডিজিট UBRN বা 0 দিন:")
        if msg: bot.register_next_step_handler(msg, ubrn_f_step)
        return
    with session_lock: get_session(m.from_user.id)["temp_data"]["ubrn"]["f"] = val
    msg = safe_send(m.chat.id, "৩. মাতার UBRN দিন (না থাকলে 0 দিন):")
    if msg: bot.register_next_step_handler(msg, ubrn_m_step)

def ubrn_m_step(m):
    if is_cancel(m): return
    if not m.text: return
    val = _validate_ubrn_input(m.text)
    if val is None:
        msg = safe_send(m.chat.id, "❌ সঠিক ১৭ ডিজিট UBRN বা 0 দিন:")
        if msg: bot.register_next_step_handler(msg, ubrn_m_step)
        return
    with session_lock: get_session(m.from_user.id)["temp_data"]["ubrn"]["m"] = val
    msg = safe_send(m.chat.id, "৪. মোবাইল নম্বর দিন (01XXXXXXXXX):")
    if msg: bot.register_next_step_handler(msg, ubrn_ph_step)

def ubrn_ph_step(m):
    if is_cancel(m): return
    if not m.text: return
    raw_phone = m.text.strip()
    if not _PHONE_RE.match(raw_phone):
        msg = safe_send(m.chat.id, "❌ অবৈধ মোবাইল নম্বর। 01XXXXXXXXX ফরম্যাটে দিন:")
        if msg: bot.register_next_step_handler(msg, ubrn_ph_step)
        return

    if raw_phone.startswith('+880'): phone = raw_phone
    elif raw_phone.startswith('880'): phone = '+' + raw_phone
    elif raw_phone.startswith('0'): phone = '+88' + raw_phone
    else: phone = raw_phone
    
    uid = m.from_user.id
    u_sess = get_session(uid)
    with session_lock:
        u_sess["temp_data"]["ubrn"]["ph"] = phone
        d = dict(u_sess["temp_data"]["ubrn"])

    res = call_api(uid, f"https://bdris.gov.bd/admin/br/parents-ubrn-update/send-otp?personBrn={d.get('p','')}&fatherBrn={d.get('f','')}&motherBrn={d.get('m','')}&phone={quote(phone)}&email=", method="POST", force_sec=True)
    if res and res.status_code == 200:
        msg = safe_send(m.chat.id, "✅ OTP পাঠানো হয়েছে! OTP দিন:")
        if msg: bot.register_next_step_handler(msg, ubrn_final)
    else: safe_send(m.chat.id, "❌ OTP পাঠাতে ব্যর্থ।")

def ubrn_final(m):
    if is_cancel(m): return
    if not m.text: return
    uid = m.from_user.id
    otp = m.text.strip()
    if not otp.isdigit():
        msg = safe_send(m.chat.id, "❌ OTP শুধুমাত্র সংখ্যা হওয়া উচিত। আবার দিন:")
        if msg: bot.register_next_step_handler(msg, ubrn_final)
        return

    u_sess = get_session(uid)
    with session_lock: 
        d = dict(u_sess["temp_data"].get("ubrn", {}))
        csrf = u_sess["csrf"]

    payload = {'_csrf': csrf, 'personBrn': d.get('p',''), 'fatherBrn': d.get('f',''), 'motherBrn': d.get('m',''), 'phone': d.get('ph',''), 'email': '', 'otp': otp}
    res = call_api(uid, "https://bdris.gov.bd/admin/br/parents-ubrn-update", method="POST", data=payload, force_sec=True)
    if res and res.status_code == 200: safe_send(m.chat.id, "✅ UBRN আপডেট সফল!", reply_markup=generate_main_menu(m.chat.id, uid))
    else: safe_send(m.chat.id, "❌ আপডেট ব্যর্থ! OTP চেক করুন।")

# ==========================================
# ১০. অ্যাডমিন কন্ট্রোল ও কলব্যাক হ্যান্ডলার
# ==========================================
def admin_edit_field(m, target_uid, field):
    if is_cancel(m): return
    if not m.text: return
    val = m.text.strip()
    try:
        t_sess = get_session(target_uid)
        with session_lock:
            if field == "SEC":
                s, t = extract_sid_tsid(val)
                if s and t: 
                    _set_session_cookies(t_sess["req_session"], s, t)
                    t_sess["sec_alive"] = True
                    t_sess["is_alive"] = True
                else: return safe_send(m.chat.id, "❌ ভুল ফরম্যাট।")
            elif field == "CH":
                s, t = extract_sid_tsid(val)
                if s and t: 
                    _set_session_cookies(t_sess["ch_session"], s, t)
                    t_sess["ch_alive"] = True
                    t_sess["is_alive"] = t_sess.get("sec_alive", False) or t_sess["ch_alive"]
                else: return safe_send(m.chat.id, "❌ ভুল ফরম্যাট।")
            elif field == "OTP":
                if not val.isdigit(): return safe_send(m.chat.id, "❌ OTP শুধুমাত্র সংখ্যা।")
                t_sess["ch_otp"] = val

        save_session_to_db(target_uid, t_sess)
        safe_send(m.chat.id, f"✅ User {target_uid} এর {field} আপডেট হয়েছে!")
    except Exception as e:
        logging.error(f"Admin Edit Error: {e}")
        safe_send(m.chat.id, "❌ আপডেট ব্যর্থ।")

def refresh_admin_panel(chat_id, target_user_id, message_id=None):
    rec = access_collection.find_one({"chat_id": target_user_id}) or {}
    p = rec.get("permissions", DEFAULT_PERMS)
    t_sess = get_session(target_user_id)
    msg = f"👤 User: `{target_user_id}`\n💰 Balance: {rec.get('balance', 0)}৳\n🔑 CH OTP: `{t_sess.get('ch_otp', 'N/A')}`\n\nপারমিশন:"
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✏️ SEC", callback_data=f"edsec:{target_user_id}"),
        telebot.types.InlineKeyboardButton("✏️ CH", callback_data=f"edch:{target_user_id}"),
        telebot.types.InlineKeyboardButton("✏️ OTP", callback_data=f"edotp:{target_user_id}")
    )
    perm_labels = [("apps", "Apps"), ("corr", "Corr"), ("repr", "Repr"), ("search", "Search"), ("ubrn_update", "UBRN Update"), ("server_pdf", "Srv PDF"), ("print", "Inline Print")]
    for k, n in perm_labels:
        st = p.get(k, True)
        markup.row(telebot.types.InlineKeyboardButton(f"{'❌ Disable' if st else '✅ Enable'} {n}", callback_data=f"tgl:{target_user_id}:{k}:{'off' if st else 'on'}"))
    
    if rec.get("status", "allowed") == "allowed": markup.row(telebot.types.InlineKeyboardButton("🚫 Block User", callback_data=f"block:{target_user_id}"))
    else: markup.row(telebot.types.InlineKeyboardButton("✅ Unblock User", callback_data=f"unblock:{target_user_id}"))

    if message_id: safe_edit(chat_id, message_id, msg, reply_markup=markup)
    else: safe_send(chat_id, msg, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    uid, cid = call.from_user.id, call.message.chat.id
    if is_rate_limited(uid): return bot.answer_callback_query(call.id, "⚠️ একটু ধীরে!", show_alert=True)
    if not check_user_access(uid, call.from_user.first_name): return bot.answer_callback_query(call.id, "🚫 অ্যাক্সেস নেই!", show_alert=True)

    u_sess = get_session(uid)
    parts = call.data.split(':')
    action = parts[0]
    sid = parts[1] if len(parts) > 1 else ""

    with session_lock:
        enc_id = u_sess["id_cache"].get(sid)

    if action in ["pay", "recv", "reg", "coreg", "print"] and not enc_id:
        return bot.answer_callback_query(call.id, "❌ ক্যাশ এক্সপায়ার হয়েছে! লিস্ট রিফ্রেশ করুন।", show_alert=True)

    if action in ["next", "prev"]:
        cmd = sid
        if cmd not in VALID_CMDS: return bot.answer_callback_query(call.id, "❌ অজানা কমান্ড।", show_alert=True)
        with session_lock:
            if action == "next": u_sess["app_start"] += u_sess["app_length"]
            else: u_sess["app_start"] = max(0, u_sess["app_start"] - u_sess["app_length"])
        bot.answer_callback_query(call.id)
        fetch_list_ui(cid, uid, cmd, call.message.message_id)

    elif action == "setlength" and len(parts) == 3:
        cmd = parts[1]
        try: length = int(parts[2])
        except: return bot.answer_callback_query(call.id, "❌ অবৈধ মান।", show_alert=True)
        with session_lock: u_sess["app_length"] = length; u_sess["app_start"] = 0
        save_session_to_db(uid, u_sess)
        bot.answer_callback_query(call.id, f"✅ {length}টি করে দেখানো হবে।")
        fetch_list_ui(cid, uid, cmd, call.message.message_id)

    elif action == "reqrecharge":
        msg = safe_send(cid, "💼 *রিচার্জের নিয়ম:*\n১. বিকাশ/নগদ নম্বরে Send Money করুন\n২. TrxID মেসেজে পাঠান:", parse_mode="Markdown")
        if msg: bot.register_next_step_handler(msg, process_recharge)
        bot.answer_callback_query(call.id)

    elif action == "apprvbal" and uid == ADMIN_ID:
        if len(parts) < 3: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        try: target = int(parts[1])
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        msg = safe_send(cid, f"User {target} এর জন্য টাকার পরিমাণ দিন:")
        if msg: bot.register_next_step_handler(msg, lambda m: admin_add_balance_step(m, target, parts[2], call.message.message_id))
        safe_edit(cid, call.message.message_id, call.message.text + "\n\n✅ Processing...")
        bot.answer_callback_query(call.id)

    elif action == "rejbal" and uid == ADMIN_ID:
        if len(parts) < 3: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        try: target = int(parts[1])
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        recharge_logs.update_one({"_id": parts[2]}, {"$set": {"status": "rejected"}})
        safe_send(target, "❌ আপনার রিচার্জ রিকোয়েস্ট অ্যাডমিন বাতিল করেছেন।")
        safe_edit(cid, call.message.message_id, call.message.text + "\n\n❌ Rejected")
        bot.answer_callback_query(call.id, "বাতিল করা হয়েছে।")

    elif action == "admuser" and uid == ADMIN_ID:
        try: target = int(parts[1])
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        refresh_admin_panel(cid, target, call.message.message_id)
        bot.answer_callback_query(call.id)

    elif action == "tgl" and uid == ADMIN_ID and len(parts) == 4:
        target_id, perm_key, state_val = int(parts[1]), parts[2], parts[3]
        if perm_key not in DEFAULT_PERMS or state_val not in ("on", "off"): return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        access_collection.update_one({"chat_id": target_id}, {"$set": {f"permissions.{perm_key}": state_val == "on"}})
        bot.answer_callback_query(call.id, f"✅ {perm_key} আপডেট হয়েছে!")
        refresh_admin_panel(cid, target_id, call.message.message_id)

    elif action == "edsec" and uid == ADMIN_ID:
        msg = safe_send(cid, f"User {sid} এর নতুন SEC সেশন দিন:")
        if msg: bot.register_next_step_handler(msg, lambda m: admin_edit_field(m, int(sid), "SEC"))
        bot.answer_callback_query(call.id)

    elif action == "edch" and uid == ADMIN_ID:
        msg = safe_send(cid, f"User {sid} এর নতুন CH সেশন দিন:")
        if msg: bot.register_next_step_handler(msg, lambda m: admin_edit_field(m, int(sid), "CH"))
        bot.answer_callback_query(call.id)

    elif action == "edotp" and uid == ADMIN_ID:
        msg = safe_send(cid, f"User {sid} এর নতুন OTP দিন:")
        if msg: bot.register_next_step_handler(msg, lambda m: admin_edit_field(m, int(sid), "OTP"))
        bot.answer_callback_query(call.id)

    elif action == "block" and uid == ADMIN_ID:
        access_collection.update_one({"chat_id": int(sid)}, {"$set": {"status": "blocked"}})
        bot.answer_callback_query(call.id, "✅ ব্লক করা হয়েছে।", show_alert=True)
        safe_send(int(sid), "🚫 আপনাকে ব্লক করা হয়েছে।")

    elif action == "unblock" and uid == ADMIN_ID:
        access_collection.update_one({"chat_id": int(sid)}, {"$set": {"status": "allowed"}})
        bot.answer_callback_query(call.id, "✅ আনব্লক করা হয়েছে।", show_alert=True)
        safe_send(int(sid), "✅ আপনার অ্যাক্সেস পুনরায় চালু হয়েছে।")

    elif action == "pay":
        cost = get_service_cost(uid, "pay")
        task_id = f"pay_{uid}_{enc_id}"
        
        with download_lock:
            if task_id in active_downloads: return bot.answer_callback_query(call.id, "⚠️ রিকোয়েস্ট প্রসেসিং-এ আছে...", show_alert=True)
            active_downloads.add(task_id)
            
        if cost > 0:
            if not deduct_balance(uid, cost):
                with download_lock: active_downloads.discard(task_id)
                return bot.answer_callback_query(call.id, f"❌ ব্যালেন্স নেই ({cost}৳)।", show_alert=True)
                
        bot.answer_callback_query(call.id, "⏳ পেমেন্ট প্রসেস হচ্ছে...")
        
        def process_payment():
            try:
                _, active_csrf = get_active_session(u_sess)
                with session_lock: curr_sharok = u_sess["sharok_no"]
                data = {'data': enc_id, 'paymentType': 'PAYMENT_BY_DISCOUNT', 'discountAmount': '50', 'discountSharokNo': str(curr_sharok), 'discountSharokDate': datetime.now().strftime("%d/%m/%Y"), '_csrf': active_csrf}
                res = call_api(uid, "https://bdris.gov.bd/api/payment/receive", method="POST", data=data)
                
                if res and res.status_code == 200:
                    with session_lock: u_sess["sharok_no"] += 1
                    save_session_to_db(uid, u_sess)
                    safe_send(cid, f"✅ পেমেন্ট সফল! {cost}৳ কাটা হয়েছে।" if cost > 0 else "✅ পেমেন্ট সফল!")
                else:
                    if cost > 0: update_balance(uid, cost) # Refund
                    safe_send(cid, "❌ পেমেন্ট ব্যর্থ! রিফান্ড করা হয়েছে।")
            finally:
                with download_lock: active_downloads.discard(task_id)
        Thread(target=process_payment, daemon=True).start()

    elif action == "recv":
        bot.answer_callback_query(call.id, "⏳ রিসিভ প্রসেস হচ্ছে...")
        def process_recv():
            _, active_csrf = get_active_session(u_sess)
            res = call_api(uid, "https://bdris.gov.bd/api/application/receive", method="POST", data={'data': enc_id, '_csrf': active_csrf})
            if res and res.status_code == 200: safe_send(cid, "✅ রিসিভ সফল!")
            else: safe_send(cid, "❌ রিসিভ ব্যর্থ!")
        Thread(target=process_recv, daemon=True).start()

    elif action in ["reg", "coreg"] and u_sess["mode"] == "CHAIRMAN":
        bot.answer_callback_query(call.id, "⏳ রেজিস্ট্রেশন হচ্ছে...")
        path = "correction-application" if action == "coreg" else "application"
        
        def process_registration():
            try:
                ch_sess = get_active_session(u_sess)[0]
                html = ch_sess.get(f"https://bdris.gov.bd/admin/br/{path}/register?data={enc_id}", headers={'User-Agent': u_sess["ua"]}, timeout=30).text
                v = re.search(r'<option\s+value="(\d{17})"[^>]*>([^<]+)</option>', html)
                if v:
                    _, active_csrf = get_active_session(u_sess)
                    payload = {"birthPlaceAndDobVerifierName": v.group(2).strip(), "birthPlaceAndDobVerifierBrn": v.group(1), "birthPlaceAndDobVerificationDate": datetime.now().strftime("%d/%m/%Y"), "otp": u_sess["ch_otp"], "data": enc_id, "_csrf": active_csrf}
                    res = call_api(uid, f"https://bdris.gov.bd/api/br/{path}/register", method="POST", data=payload)
                    if res and res.status_code == 200: safe_send(cid, "✅ রেজিস্ট্রেশন সফল!")
                    else: safe_send(cid, "❌ রেজিস্ট্রেশন ব্যর্থ।")
                else: safe_send(cid, "❌ ভেরিফায়ার পাওয়া যায়নি।")
            except Exception as e:
                logging.error(f"Registration Error: {e}")
                safe_send(cid, "❌ রেজিস্ট্রেশনে এরর।")
                
        Thread(target=process_registration, daemon=True).start()

    elif action == "print":
        perms = get_user_permissions(uid)
        if perms.get("print") or uid == ADMIN_ID:
            cost = get_service_cost(uid, "pdf")
            task_id = f"print_{uid}_{enc_id}"
            
            with download_lock:
                if task_id in active_downloads: return bot.answer_callback_query(call.id, "⚠️ প্রসেসিং-এ আছে...", show_alert=True)
                active_downloads.add(task_id)
                
            if cost > 0:
                if not deduct_balance(uid, cost):
                    with download_lock: active_downloads.discard(task_id)
                    return bot.answer_callback_query(call.id, f"❌ ব্যালেন্স নেই ({cost}৳)", show_alert=True)
                
            bot.answer_callback_query(call.id, "⏳ ডাউনলোড শুরু হচ্ছে...")
            working_uid = uid if u_sess["is_alive"] else ADMIN_ID
            
            def print_pdf_thread():
                try: 
                    download_server_pdf(cid, working_uid, enc_id, f"Cert_{sid}")
                    safe_send(cid, f"✅ ডাউনলোড সফল! ব্যালেন্স: {get_balance(uid)}৳" if cost > 0 else "✅ ডাউনলোড সফল!")
                except Exception as e:
                    if cost > 0: update_balance(uid, cost) # Centralized refund
                    safe_send(cid, "❌ এরর বা সার্ভার ডাউন। রিফান্ড করা হয়েছে।")
                finally:
                    with download_lock: active_downloads.discard(task_id)
            Thread(target=print_pdf_thread, daemon=True).start()
        else: bot.answer_callback_query(call.id, "🚫 অনুমতি নেই!", show_alert=True)
    else: bot.answer_callback_query(call.id)

# ==========================================
# ১১. মেইন রাউটার
# ==========================================
@bot.message_handler(func=lambda m: True)
def router(m):
    cid, uid, t = m.chat.id, m.from_user.id, m.text or ""
    if not t: return safe_send(cid, "⚠️ শুধুমাত্র টেক্সট মেসেজ সাপোর্টেড।")
    if is_rate_limited(uid): return
    if not check_user_access(uid, m.from_user.first_name): return safe_send(cid, "🚫 অ্যাক্সেস নেই।")
        
    u_sess = get_session(uid)
    perms = get_user_permissions(uid)

    if uid == ADMIN_ID:
        if t == "/payment_on":
            settings_collection.update_one({"_id": "config"}, {"$set": {"payment_active": True}})
            return safe_send(cid, "✅ পেমেন্ট চালু।", reply_markup=generate_main_menu(cid, uid))
        elif t == "/payment_off":
            settings_collection.update_one({"_id": "config"}, {"$set": {"payment_active": False}})
            return safe_send(cid, "❌ পেমেন্ট বন্ধ।", reply_markup=generate_main_menu(cid, uid))
        elif t == "🔑 Admin Login":
            msg = safe_send(cid, "🔑 এডমিন সেশন দিন:")
            if msg: bot.register_next_step_handler(msg, admin_login_logic)
            return
        elif t == "🛠️ Check Cookies":
            return safe_send(cid, f"SEC: `{u_sess['req_session'].cookies.get_dict()}`\nCH: `{u_sess['ch_session'].cookies.get_dict()}`\nOTP: `{u_sess['ch_otp']}`", parse_mode="Markdown")
        elif t == "👥 Manage Users":
            try: users = list(access_collection.find({}, {"chat_id": 1, "name": 1, "status": 1, "balance": 1}))
            except: return safe_send(cid, "❌ DB এরর।")
            if not users: return safe_send(cid, "📭 কোনো ইউজার নেই।")
            markup = telebot.types.InlineKeyboardMarkup()
            for u in users:
                markup.row(telebot.types.InlineKeyboardButton(f"{'✅' if u.get('status')=='allowed' else '🚫'} {u.get('name','')} | {u.get('balance',0)}৳", callback_data=f"admuser:{u.get('chat_id')}"))
            return safe_send(cid, "👥 ইউজার প্যানেল:", reply_markup=markup)

    if t.startswith("/start") or t == "Back to Menu":
        return safe_send(cid, "🚀 BDRIS Master Bot Active!", reply_markup=generate_main_menu(cid, uid))

    elif t == "🏠 Dashboard":
        if u_sess["is_alive"]:
            success, _ = navigate_to(uid, "https://bdris.gov.bd/admin/")
            safe_send(cid, "🏠 ড্যাশবোর্ড রিফ্রেশ হয়েছে।", reply_markup=generate_main_menu(cid, uid))
        else: safe_send(cid, "⚠️ আগে লগইন করুন।", reply_markup=generate_main_menu(cid, uid))
        return

    elif t == "💰 My Profile & Recharge":
        if not is_payment_active(): return safe_send(cid, "ℹ️ সার্ভিস ফ্রি।")
        safe_name = sanitize_name(m.from_user.first_name)
        msg = f"👤 Profile: {safe_name}\n🆔 ID: `{uid}`\n\n💰 Balance: {get_balance(uid)}৳"
        markup = telebot.types.InlineKeyboardMarkup().add(telebot.types.InlineKeyboardButton("➕ Add Balance", callback_data="reqrecharge"))
        return safe_send(cid, msg, reply_markup=markup, parse_mode="Markdown")

    elif t == "🔑 User Login":
        msg = safe_send(cid, "✅ নিবন্ধকের সেশন দিন:")
        if msg: bot.register_next_step_handler(msg, role_step_1)
        return

    elif t == "🖨️ Server PDF Print" and not u_sess["is_alive"] and (perms.get("server_pdf") or uid == ADMIN_ID):
        msg = safe_send(cid, "🖨️ ১৭ ডিজিট UBRN দিন:")
        if msg: bot.register_next_step_handler(msg, download_server_by_ubrn)
        return

    elif u_sess["is_alive"]:
        if t == "👤 নিবন্ধক সেকশন":
            with session_lock: u_sess["mode"] = "CHAIRMAN"
            save_session_to_db(uid, u_sess)
            safe_send(cid, "✅ নিবন্ধক সেকশন চালু।", reply_markup=generate_main_menu(cid, uid))
        elif t == "🧑‍💼 অথোরাইজড ইউজার":
            with session_lock: u_sess["mode"] = "SECRETARY"
            save_session_to_db(uid, u_sess)
            safe_send(cid, "✅ অথোরাইজড ইউজার সেকশন চালু।", reply_markup=generate_main_menu(cid, uid))
        elif t == "📋 Applications" and (perms.get("apps") or uid == ADMIN_ID): handle_category_init(m, 'apps')
        elif t == "📝 Correction" and (perms.get("corr") or uid == ADMIN_ID): handle_category_init(m, 'corr')
        elif t == "🔄 Reprint" and (perms.get("repr") or uid == ADMIN_ID): handle_category_init(m, 'repr')
        elif t == "🌐 Search By Name" and (perms.get("search") or uid == ADMIN_ID):
            msg = safe_send(cid, "🔍 নাম দিন (বাংলায়):")
            if msg: bot.register_next_step_handler(msg, process_search_by_name)
        elif t == "🔢 Search By UBRN" and (perms.get("search") or uid == ADMIN_ID):
            msg = safe_send(cid, "🔢 UBRN দিন (১৭ ডিজিট):")
            if msg: bot.register_next_step_handler(msg, process_search_by_ubrn)
        elif t == "👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ" and (perms.get("ubrn_update") or uid == ADMIN_ID):
            start_ubrn_flow(m)
        elif t == "🖨️ Server PDF Print" and (perms.get("server_pdf") or uid == ADMIN_ID):
            msg = safe_send(cid, "🖨️ ১৭ ডিজিট UBRN দিন:")
            if msg: bot.register_next_step_handler(msg, download_server_by_ubrn)
        else: safe_send(cid, "⚠️ অজানা কমান্ড।", reply_markup=generate_main_menu(cid, uid))
        return

    safe_send(cid, "⚠️ আগে লগইন করুন।", reply_markup=generate_main_menu(cid, uid))

# ==========================================
# ১২. Flask ও Main
# ==========================================
def run_flask():
    app = Flask(__name__)
    @app.route('/')
    def home(): return "✅ BDRIS Bot is Live and Running!"
    
    try:
        app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)), use_reloader=False)
    except Exception as e:
        logging.error(f"Flask Error: {e}")

if __name__ == "__main__":
    logging.info("🚀 BDRIS Bot Starting...")
    Thread(target=keep_sessions_alive_and_cleanup, daemon=True).start()
    Thread(target=run_flask, daemon=True).start()
    logging.info("✅ Polling...")
    while True:
        try: bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e: logging.error(f"❌ Crash: {e}"); time.sleep(5)
