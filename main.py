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
# ০. গ্লোবাল ভেরিয়েবল, লক ও লগিং
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
session_lock = threading.Lock()

# প্রি-কম্পাইল্ড রেগুলার এক্সপ্রেশন (Performance Fix)
_COOKIE_RE = re.compile(r'SESSION=([^\s;]+)', re.I)
_TS_RE = re.compile(r'TS0108b707=([^\s;]+)', re.I)
_CSRF_RE = re.compile(r'name="_csrf"\s+content="([^"]+)"')
_DATA_ID_RE = re.compile(r'href=".*?\?data=([A-Za-z0-9_\-]+)"')
_PHONE_RE = re.compile(r'^(?:\+8801|01|8801)[3-9]\d{8}$')

VALID_CMDS = frozenset(['apps', 'corr', 'repr']) # O(1) Lookup
DEFAULT_PERMS = {"apps": True, "corr": True, "repr": True, "search": True, "ubrn_update": True, "server_pdf": True, "print": True}

active_downloads = set() 
download_lock = threading.Lock()

# ==========================================
# ১. কনফিগারেশন ও ডেটাবেস
# ==========================================
API_TOKEN = os.environ.get('BOT_TOKEN')
MONGO_URI = os.environ.get('MONGO_URI')
ADMIN_ID_STR = os.environ.get('ADMIN_ID')

if not all([API_TOKEN, MONGO_URI, ADMIN_ID_STR]):
    logging.critical("❌ Critical Environment Variables missing! BOT_TOKEN, MONGO_URI, ADMIN_ID সেট করুন।")
    sys.exit(1)

ADMIN_ID = int(ADMIN_ID_STR)
# Race Condition Fix: Disable telebot's internal threading
bot = telebot.TeleBot(API_TOKEN, threaded=False)

try:
    mongo_client = MongoClient(MONGO_URI)
    mongo_client.admin.command('ping') # DB Ping Check
    db = mongo_client['bdris_bot_db']
    sessions_collection = db['users_sessions']
    access_collection = db['users_access']
    settings_collection = db['bot_settings']
    
    if not settings_collection.find_one({"_id": "config"}):
        settings_collection.insert_one({"_id": "config", "payment_active": True})
        
    logging.info("✅ MongoDB Connected Successfully!")
except Exception as e:
    logging.critical(f"❌ MongoDB Connection Failed: {e}")
    sys.exit(1)

# ==========================================
# ২. সেফ র‍্যাপারস (Safe Wrappers)
# ==========================================
def safe_send(chat_id, text, **kwargs):
    try: return bot.send_message(chat_id, text, **kwargs)
    except Exception as e: logging.error(f"Send Error: {e}"); return None

def safe_edit(text, chat_id, message_id, **kwargs):
    try: return bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception as e: logging.error(f"Edit Error: {e}"); return None

def safe_delete(chat_id, message_id):
    try: return bot.delete_message(chat_id, message_id)
    except Exception as e: logging.error(f"Delete Error: {e}"); return False

# ==========================================
# ৩. ইউজার এক্সেস ও ব্যালেন্স
# ==========================================
def is_payment_active():
    config = settings_collection.find_one({"_id": "config"})
    return config.get("payment_active", True) if config else True

def get_service_cost(user_id, service="default"):
    if user_id == ADMIN_ID or not is_payment_active(): return 0
    return {"pdf": 25, "pay": 25, "server_pdf_login": 25, "server_pdf_no_login": 50}.get(service, 25)

def check_user_access(user_id, user_name):
    if user_id == ADMIN_ID: return True
    user_record = access_collection.find_one({"chat_id": user_id})
    if not user_record:
        access_collection.insert_one({
            "chat_id": user_id, "name": str(user_name), "status": "allowed", 
            "permissions": DEFAULT_PERMS, "balance": 0
        })
        safe_name = str(user_name).replace('*', '').replace('_', '').replace('`', '')
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

def get_balance(user_id):
    if user_id == ADMIN_ID: return 999999
    record = access_collection.find_one({"chat_id": user_id})
    return record.get("balance", 0) if record else 0

def update_balance(user_id, amount):
    if user_id == ADMIN_ID: return
    access_collection.update_one({"chat_id": user_id}, {"$inc": {"balance": amount}})

# ==========================================
# ৪. ডায়নামিক কিবোর্ড
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
# ৫. সেশন ম্যানেজমেন্ট ও অ্যান্টি-স্প্যাম
# ==========================================
user_sessions = {}

def get_default_session_dict():
    return {
        "req_session": requests.Session(), "csrf": "",
        "ch_session": requests.Session(), "ch_csrf": "", "ch_otp": "",
        "mode": "SECRETARY", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "is_alive": False, "current_page": "https://bdris.gov.bd/admin/",
        "app_start": 0, "app_length": 5, "sharok_no": 1, "temp_data": {}, "id_cache": {},
        "last_action_time": time.time(), "last_warning_time": 0, "current_search_val": ""
    }

def save_session_to_db(user_id, u_sess):
    try:
        data = {
            "chat_id": user_id, "sec_cookies": u_sess["req_session"].cookies.get_dict(),
            "ch_cookies": u_sess["ch_session"].cookies.get_dict(),
            "mode": u_sess["mode"], "ch_otp": u_sess.get("ch_otp", ""), "is_alive": u_sess["is_alive"]
        }
        sessions_collection.update_one({"chat_id": user_id}, {"$set": data}, upsert=True)
    except Exception as e: logging.error(f"❌ Session DB Save Error: {e}")

def get_session(user_id):
    if user_id in user_sessions: return user_sessions[user_id]
        
    u_sess = get_default_session_dict()
    try:
        db_data = sessions_collection.find_one({"chat_id": user_id})
        if db_data:
            u_sess["req_session"].cookies.update(db_data.get("sec_cookies", {}))
            u_sess["ch_session"].cookies.update(db_data.get("ch_cookies", {}))
            u_sess["mode"] = db_data.get("mode", "SECRETARY")
            u_sess["ch_otp"] = db_data.get("ch_otp", "")
            u_sess["is_alive"] = db_data.get("is_alive", False)
    except Exception as e: logging.error(f"❌ Session DB Load Error: {e}")

    with session_lock:
        if user_id not in user_sessions: user_sessions[user_id] = u_sess
    return user_sessions[user_id]

def is_rate_limited(user_id):
    u_sess = get_session(user_id)
    current_time = time.time()
    with session_lock:
        if current_time - u_sess.get("last_action_time", current_time) < 2:
            if current_time - u_sess.get("last_warning_time", 0) > 5:
                safe_send(user_id, "⚠️ *একটু ধীরে!* ২ সেকেন্ড অপেক্ষা করুন।", parse_mode="Markdown")
                u_sess["last_warning_time"] = current_time
            return True
        u_sess["last_action_time"] = current_time
    return False

def is_cancel(m):
    if not m.text: return False
    if any(cmd in m.text for cmd in ["/start", "Back to Menu", "Dashboard", "🏠 Dashboard"]):
        bot.clear_step_handler_by_chat_id(m.chat.id)
        safe_send(m.chat.id, "🏠 ইনপুট বাতিল করে মেনুতে ফিরে আসা হলো।", reply_markup=generate_main_menu(m.chat.id, m.from_user.id))
        return True
    return False

# ==========================================
# ৬. কোর রিকোয়েস্ট ও বেসিক ফাংশন
# ==========================================
def _set_session_cookies(sess, sid, tsid):
    sess.cookies.clear()
    sess.cookies.set("SESSION", sid, domain='bdris.gov.bd')
    sess.cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')

def extract_sid_tsid(text):
    s = _COOKIE_RE.search(text)
    tsid = _TS_RE.search(text)
    return (s.group(1), tsid.group(1)) if s and tsid else (None, None)

def get_active_session(u_sess):
    return (u_sess["ch_session"], u_sess["ch_csrf"]) if u_sess["mode"] == "CHAIRMAN" else (u_sess["req_session"], u_sess["csrf"])

def call_api(user_id, url, method="GET", data=None, extra_headers=None, retries=2):
    u_sess = get_session(user_id)
    sess, csrf = get_active_session(u_sess)
    headers = {'x-csrf-token': csrf, 'x-requested-with': 'XMLHttpRequest', 'user-agent': u_sess["ua"], 'referer': u_sess["current_page"]}
    if extra_headers: headers.update(extra_headers)
        
    for attempt in range(retries):
        try:
            if method == "POST": return sess.post(url, headers=headers, data=data, timeout=30)
            return sess.get(url, headers=headers, timeout=30)
        except Exception as e:
            logging.warning(f"⚠️ API Call Attempt {attempt+1} Error [{method} {url}]: {e}")
            if attempt < retries - 1: time.sleep(1)
    return None

def navigate_to(user_id, url):
    u_sess = get_session(user_id)
    sess, _ = get_active_session(u_sess)
    try:
        res = sess.get(url, headers={'User-Agent': u_sess["ua"], 'Referer': u_sess["current_page"]}, timeout=25)
        csrf = _CSRF_RE.search(res.text)
        if csrf:
            with session_lock:
                if u_sess["mode"] == "CHAIRMAN": u_sess["ch_csrf"] = csrf.group(1)
                else: u_sess["csrf"] = csrf.group(1)
        u_sess["current_page"] = url
        return True, res.text
    except Exception as e:
        logging.error(f"❌ Navigate Error [{url}]: {e}")
        return False, None

def keep_sessions_alive_and_cleanup():
    while True:
        time.sleep(300)
        current_time = time.time()
        with session_lock:
            expired_users = [uid for uid, s in user_sessions.items() 
                             if not s["is_alive"] and (current_time - s.get("last_action_time", current_time)) > 3600]
            for uid in expired_users: del user_sessions[uid]
            active_users = [(uid, u_sess["ua"], u_sess["req_session"], u_sess["ch_session"]) 
                            for uid, u_sess in user_sessions.items() if u_sess["is_alive"]]
            
        for uid, ua, req_sess, ch_sess in active_users:
            try:
                res_sec = req_sess.get("https://bdris.gov.bd/admin/", headers={'User-Agent': ua}, timeout=20)
                c1 = _CSRF_RE.search(res_sec.text)
                res_ch = ch_sess.get("https://bdris.gov.bd/admin/", headers={'User-Agent': ua}, timeout=20)
                c2 = _CSRF_RE.search(res_ch.text)
                
                with session_lock:
                    if uid in user_sessions:
                        if c1: user_sessions[uid]["csrf"] = c1.group(1)
                        if c2: user_sessions[uid]["ch_csrf"] = c2.group(1)
            except Exception as e: logging.warning(f"⚠️ Keep-alive failed for {uid}: {e}")

# ==========================================
# ৭. লগইন ফ্লো ও রিচার্জ ফ্লো
# ==========================================
def admin_login_logic(m):
    if not m.text: return
    sid, tsid = extract_sid_tsid(m.text.strip())
    uid = m.from_user.id
    u_sess = get_session(uid)
    if sid and tsid:
        _set_session_cookies(u_sess["req_session"], sid, tsid)
        u_sess["is_alive"] = True
        save_session_to_db(uid, u_sess)
        safe_send(m.chat.id, "✅ এডমিন সেশন সেট হয়েছে!", reply_markup=generate_main_menu(m.chat.id, uid))
    else: safe_send(m.chat.id, "❌ কুকি ভুল ফরম্যাটে আছে।")

def role_step_1(m):
    if is_cancel(m): return
    if not m.text: return
    uid = m.from_user.id
    u_sess = get_session(uid)
    sid, tsid = extract_sid_tsid(m.text.strip())
    if not sid or not tsid:
        msg = safe_send(m.chat.id, "❌ নিবন্ধক কুকি পাওয়া যায়নি! সঠিক ফরম্যাটে দিন:")
        if msg: bot.register_next_step_handler(msg, role_step_1)
        return

    _set_session_cookies(u_sess["ch_session"], sid, tsid)
    msg = safe_send(m.chat.id, "✅ নিবন্ধকের সেশন গৃহীত! এখন নিবন্ধকের OTP দিন:")
    if msg: bot.register_next_step_handler(msg, role_step_2)

def role_step_2(m):
    if is_cancel(m): return
    if not m.text or not m.text.strip().isdigit():
        msg = safe_send(m.chat.id, "❌ OTP শুধুমাত্র সংখ্যা হতে হবে! আবার দিন:")
        if msg: bot.register_next_step_handler(msg, role_step_2)
        return
    with session_lock: get_session(m.from_user.id)["ch_otp"] = m.text.strip()
    msg = safe_send(m.chat.id, "✅ OTP সংরক্ষিত! এখন অথোরাইজড ইউজারের সেশন দিন:")
    if msg: bot.register_next_step_handler(msg, role_step_3)

def role_step_3(m):
    if is_cancel(m): return
    if not m.text: return
    uid = m.from_user.id
    u_sess = get_session(uid)
    sid, tsid = extract_sid_tsid(m.text.strip())
    if sid and tsid:
        _set_session_cookies(u_sess["req_session"], sid, tsid)
        u_sess["is_alive"] = True
        save_session_to_db(uid, u_sess)
        safe_send(m.chat.id, "🎉 লগইন সফল হয়েছে!", reply_markup=generate_main_menu(m.chat.id, uid))
    else: 
        msg = safe_send(m.chat.id, "❌ অথোরাইজড ইউজার কুকি পাওয়া যায়নি! আবার দিন:")
        if msg: bot.register_next_step_handler(msg, role_step_3)

def process_recharge(m):
    if is_cancel(m): return
    if not m.text: return
    trxid = m.text.strip()
    uid = m.from_user.id
    
    if not (5 <= len(trxid) <= 50):
        msg = safe_send(m.chat.id, "❌ TrxID সঠিক নয়! সঠিক TrxID দিন:")
        if msg: bot.register_next_step_handler(msg, process_recharge)
        return
        
    safe_send(m.chat.id, "✅ আপনার রিকোয়েস্ট অ্যাডমিনের কাছে পাঠানো হয়েছে।", reply_markup=generate_main_menu(m.chat.id, uid))
    safe_name = str(m.from_user.first_name).replace('*', '').replace('_', '').replace('`', '')
    msg_text = f"💰 *নতুন রিচার্জ রিকোয়েস্ট!*\n👤 User: {safe_name} (`{uid}`)\n📝 TrxID: `{trxid}`"
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✅ Approve", callback_data=f"apprvbal:{uid}"),
        telebot.types.InlineKeyboardButton("❌ Reject", callback_data=f"rejbal:{uid}")
    )
    safe_send(ADMIN_ID, msg_text, reply_markup=markup, parse_mode="Markdown")

def admin_add_balance_step(m, target_id):
    if is_cancel(m): return
    if not m.text: return
    try:
        amount = int(m.text.strip())
        if amount <= 0: raise ValueError
        update_balance(target_id, amount)
        safe_send(m.chat.id, f"✅ User {target_id} এর অ্যাকাউন্টে {amount}৳ যোগ করা হয়েছে!")
        safe_send(target_id, f"🎉 *আপনার অ্যাকাউন্টে {amount}৳ রিচার্জ সফল হয়েছে!*\nবর্তমান ব্যালেন্স: {get_balance(target_id)}৳", parse_mode="Markdown")
    except ValueError:
        safe_send(m.chat.id, "❌ ভুল ইনপুট। শুধু ধনাত্মক সংখ্যা দিন।")

# ==========================================
# ৮. অ্যাপ লিস্ট লজিক
# ==========================================
def handle_category_init(m, cmd):
    if cmd not in VALID_CMDS: return safe_send(m.chat.id, "❌ অজানা কমান্ড।")
    u_sess = get_session(m.from_user.id)
    with session_lock:
        u_sess["app_start"] = 0
        u_sess["current_search_val"] = ""
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔍 Search ID", "📋 All List", "🏠 Back to Menu")
    msg = safe_send(m.chat.id, f"📂 {cmd.upper()} সেকশন:", reply_markup=markup)
    if msg: bot.register_next_step_handler(msg, category_gate, cmd)

def category_gate(m, cmd):
    if is_cancel(m): return
    if not m.text: return
    if "Search ID" in m.text:
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
        msg = safe_send(m.chat.id, "🆔 আইডি দিন:", reply_markup=markup)
        if msg: bot.register_next_step_handler(msg, search_loop, cmd)
    elif "All List" in m.text: 
        with session_lock: get_session(m.from_user.id)["current_search_val"] = ""
        fetch_list_ui(m.chat.id, m.from_user.id, cmd)
    else:
        msg = safe_send(m.chat.id, "⚠️ সঠিক অপশন বেছে নিন:")
        if msg: bot.register_next_step_handler(msg, category_gate, cmd)

def search_loop(m, cmd):
    if is_cancel(m): return
    if not m.text: return
    u_sess = get_session(m.from_user.id)
    with session_lock: u_sess["current_search_val"] = m.text.strip()
    fetch_list_ui(m.chat.id, m.from_user.id, cmd)
    msg = safe_send(m.chat.id, "🔍 আরও আইডি দিন (বা মেনুতে ফিরুন):")
    if msg: bot.register_next_step_handler(msg, search_loop, cmd)

def fetch_list_ui(chat_id, user_id, cmd, message_id=None):
    u_sess = get_session(user_id)
    perms = get_user_permissions(user_id)
    search_val = u_sess.get("current_search_val", "")
    
    config = {
        'apps': ("/admin/br/applications/search", "/api/br/applications/search"),
        'corr': ("/admin/br/correction-applications/search", "/api/br/correction-applications/search"),
        'repr': ("/admin/br/reprint/view/applications/search", "/api/br/reprint/applications/search")
    }
    
    success, html = navigate_to(user_id, f"https://bdris.gov.bd{config[cmd][0]}")
    if not success or not html: return safe_send(chat_id, "❌ পেজ লোড ব্যর্থ। সেশন মেয়াদোত্তীর্ণ হতে পারে।")
        
    id_match = _DATA_ID_RE.search(html)
    data_id = id_match.group(1) if id_match else None
    if not data_id: return safe_send(chat_id, "❌ ডাটা আইডি মেলেনি। সেশন চেক করুন।")

    url = (f"https://bdris.gov.bd{config[cmd][1]}"
           f"?data={data_id}&status=ALL&draw=1"
           f"&start={u_sess['app_start']}&length={u_sess['app_length']}"
           f"&search[value]={quote(search_val)}&search[regex]=false"
           f"&order[0][column]=1&order[0][dir]=desc")
    res = call_api(user_id, url)
    
    if not res or res.status_code != 200: return safe_send(chat_id, "❌ ডেটা লোড ব্যর্থ।")
    try: resp_json = res.json()
    except Exception: return safe_send(chat_id, "❌ সার্ভার রেসপন্স পার্স করা যায়নি।")
        
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
        
        short_id = str(abs(hash(enc_id)))[-8:]
        with session_lock:
            u_sess["id_cache"][short_id] = enc_id
            while len(u_sess["id_cache"]) > 300:
                u_sess["id_cache"].pop(next(iter(u_sess["id_cache"])))
            
        app_id = item.get('id') or item.get('applicationId') or 'N/A'
        person_name = item.get('personNameBn') or 'নাম অজানা'
        msg_text += f"🆔 `{app_id}` | {person_name}\n🚩 Status: `{status}`\n"
        
        btns = []
        if u_sess["mode"] == "CHAIRMAN" and "RECEIVED" in status:
            btns.append(telebot.types.InlineKeyboardButton("✅ Register", callback_data=f"reg:{short_id}")) if cmd == 'apps' else btns.append(telebot.types.InlineKeyboardButton("📝 Corr Register", callback_data=f"coreg:{short_id}"))
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
        total = resp_json.get('recordsTotal', 0)
        if u_sess["app_start"] > 0: nav.append(telebot.types.InlineKeyboardButton("⬅️ Prev", callback_data=f"prev:{cmd}"))
        if u_sess["app_start"] + u_sess["app_length"] < total: nav.append(telebot.types.InlineKeyboardButton("Next ➡️", callback_data=f"next:{cmd}"))
        if nav: markup.row(*nav)
        
    if len(msg_text) > 4000: msg_text = msg_text[:4000] + "\n\n⚠️ বাকি তথ্য কাটা গেছে।"
    
    if message_id: safe_edit(msg_text, chat_id, message_id, reply_markup=markup, parse_mode='Markdown')
    else: safe_send(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')

# ==========================================
# ৯. Search, UBRN Update & PDF Print
# ==========================================
def process_search_by_name(m):
    if is_cancel(m): return
    if not m.text: return
    uid = m.from_user.id
    payload = f"personNameBn={quote(m.text.strip())}&personNameEn=&nameLang=BENGALI"
    extra_h = {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}
    
    navigate_to(uid, "https://bdris.gov.bd/admin/br/advanced-search-by-name")
    res = call_api(uid, "https://bdris.gov.bd/api/br/advanced-search-by-name", method="POST", data=payload, extra_headers=extra_h)
    
    if res and res.status_code == 200:
        try:
            result_text = json.dumps(res.json(), indent=2, ensure_ascii=False)
            if len(result_text) > 3500: result_text = result_text[:3500] + "\n... (বাকি অংশ কাটা হয়েছে)"
            safe_send(m.chat.id, f"📊 *Search Result:*\n```json\n{result_text}\n```", parse_mode='Markdown')
        except Exception: safe_send(m.chat.id, "❌ ডেটা প্রসেস করতে সমস্যা হয়েছে।")
    else: safe_send(m.chat.id, "❌ কোনো তথ্য পাওয়া যায়নি বা সার্ভার এরর।")

def process_search_by_ubrn(m):
    if is_cancel(m): return
    if not m.text: return
    ubrn = m.text.strip()
    if not ubrn.isdigit() or len(ubrn) != 17:
        msg = safe_send(m.chat.id, "❌ UBRN অবশ্যই ১৭ ডিজিটের সংখ্যা হতে হবে! আবার দিন:")
        if msg: bot.register_next_step_handler(msg, process_search_by_ubrn)
        return
        
    uid = m.from_user.id
    res = call_api(uid, f"https://bdris.gov.bd/api/br/info/ubrn/{ubrn}")
    if res and res.status_code == 200:
        try:
            result_text = json.dumps(res.json(), indent=2, ensure_ascii=False)
            if len(result_text) > 3500: result_text = result_text[:3500] + "\n... (বাকি অংশ কাটা হয়েছে)"
            safe_send(m.chat.id, f"📊 *UBRN Result:*\n```json\n{result_text}\n```", parse_mode='Markdown')
        except Exception: safe_send(m.chat.id, "❌ ডেটা পার্স করা যায়নি।")
    else: safe_send(m.chat.id, "❌ তথ্য পাওয়া যায়নি।")

def start_ubrn_flow(m):
    u_sess = get_session(m.from_user.id)
    with session_lock: u_sess["temp_data"]["ubrn"] = {}
    navigate_to(m.from_user.id, "https://bdris.gov.bd/admin/br/parents-ubrn-update")
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
    msg = safe_send(m.chat.id, "১. ব্যক্তির ১৭ ডিজিট UBRN দিন:", reply_markup=markup)
    if msg: bot.register_next_step_handler(msg, ubrn_p_step)

def ubrn_p_step(m):
    if is_cancel(m): return
    if not m.text: return
    ubrn = m.text.strip()
    if not ubrn.isdigit() or len(ubrn) != 17:
        msg = safe_send(m.chat.id, "❌ UBRN অবশ্যই ১৭ ডিজিট হতে হবে! আবার দিন:")
        if msg: bot.register_next_step_handler(msg, ubrn_p_step)
        return
    with session_lock: get_session(m.from_user.id)["temp_data"]["ubrn"]["p"] = ubrn
    msg = safe_send(m.chat.id, "২. পিতার UBRN দিন (না থাকলে 0 দিন):")
    if msg: bot.register_next_step_handler(msg, ubrn_f_step)

def ubrn_f_step(m):
    if is_cancel(m): return
    if not m.text: return
    val = "" if m.text.strip() == '0' else m.text.strip()
    with session_lock: get_session(m.from_user.id)["temp_data"]["ubrn"]["f"] = val
    msg = safe_send(m.chat.id, "৩. মাতার UBRN দিন (না থাকলে 0 দিন):")
    if msg: bot.register_next_step_handler(msg, ubrn_m_step)

def ubrn_m_step(m):
    if is_cancel(m): return
    if not m.text: return
    val = "" if m.text.strip() == '0' else m.text.strip()
    with session_lock: get_session(m.from_user.id)["temp_data"]["ubrn"]["m"] = val
    msg = safe_send(m.chat.id, "৪. মোবাইল নম্বর দিন (01XXXXXXXXX):")
    if msg: bot.register_next_step_handler(msg, ubrn_ph_step)

def ubrn_ph_step(m):
    if is_cancel(m): return
    if not m.text: return
    uid = m.from_user.id
    u_sess = get_session(uid)
    raw_phone = m.text.strip()
    
    if not _PHONE_RE.match(raw_phone):
        msg = safe_send(m.chat.id, "❌ মোবাইল নম্বর সঠিক নয়! সঠিক নম্বর দিন:")
        if msg: bot.register_next_step_handler(msg, ubrn_ph_step)
        return
        
    phone = raw_phone if raw_phone.startswith('+') else "+88" + raw_phone.lstrip('+88')
    
    with session_lock:
        u_sess["temp_data"]["ubrn"]["ph"] = phone
        d = u_sess["temp_data"]["ubrn"].copy() # Lock-free safe copy
        
    res = call_api(
        uid, 
        f"https://bdris.gov.bd/admin/br/parents-ubrn-update/send-otp?personBrn={d['p']}&fatherBrn={d['f']}&motherBrn={d['m']}&phone={quote(phone)}&email=", 
        method="POST"
    )
    if res and res.status_code == 200:
        msg = safe_send(m.chat.id, "✅ OTP পাঠানো হয়েছে! OTP দিন:")
        if msg: bot.register_next_step_handler(msg, ubrn_final)
    else: safe_send(m.chat.id, "❌ OTP পাঠাতে ব্যর্থ। নম্বর ও UBRN চেক করুন।")

def ubrn_final(m):
    if is_cancel(m): return
    if not m.text: return
    otp = m.text.strip()
    if not otp.isdigit():
        msg = safe_send(m.chat.id, "❌ OTP শুধু সংখ্যা হবে! আবার দিন:")
        if msg: bot.register_next_step_handler(msg, ubrn_final)
        return
        
    uid = m.from_user.id
    u_sess = get_session(uid)
    with session_lock: d = u_sess["temp_data"]["ubrn"].copy()
    _, active_csrf = get_active_session(u_sess)
    
    payload = {
        '_csrf': active_csrf,
        'personBrn': d['p'], 'fatherBrn': d['f'], 'motherBrn': d['m'],
        'phone': d['ph'], 'email': '', 'otp': otp
    }
    res = call_api(uid, "https://bdris.gov.bd/admin/br/parents-ubrn-update", method="POST", data=payload)
    if res and res.status_code == 200: safe_send(m.chat.id, "✅ UBRN আপডেট সফল!", reply_markup=generate_main_menu(m.chat.id, uid))
    else: safe_send(m.chat.id, "❌ আপডেট ব্যর্থ! OTP বা তথ্য চেক করুন।")

def download_server_by_ubrn(m):
    if is_cancel(m): return
    if not m.text: return
    chat_id = m.chat.id
    user_id = m.from_user.id
    ubrn = m.text.strip()
    
    if not ubrn.isdigit() or len(ubrn) != 17:
        msg = safe_send(chat_id, "❌ UBRN অবশ্যই ১৭ ডিজিটের সংখ্যা হতে হবে! আবার দিন:")
        if msg: bot.register_next_step_handler(msg, download_server_by_ubrn)
        return
        
    u_sess = get_session(user_id)
    cost = get_service_cost(user_id, "server_pdf_login" if u_sess["is_alive"] else "server_pdf_no_login")

    if cost > 0 and get_balance(user_id) < cost:
        safe_send(chat_id, f"❌ *পর্যাপ্ত ব্যালেন্স নেই!*\nএই সার্ভিসের জন্য {cost}৳ প্রয়োজন।\nআপনার ব্যালেন্স: {get_balance(user_id)}৳", parse_mode="Markdown")
        return

    working_uid = user_id if u_sess["is_alive"] else ADMIN_ID
    if not u_sess["is_alive"] and not get_session(ADMIN_ID)["is_alive"]:
        return safe_send(chat_id, "❌ বর্তমানে সিস্টেম সাময়িকভাবে ডাউন আছে। পরে চেষ্টা করুন।")

    task_id = f"pdf_{user_id}_{ubrn}"
    with download_lock:
        if task_id in active_downloads: return safe_send(chat_id, "⚠️ আপনার একটি ডাউনলোড রিকোয়েস্ট আগে থেকেই প্রসেসিং-এ আছে।")
        active_downloads.add(task_id)

    if cost > 0: update_balance(user_id, -cost)
    wait_msg = safe_send(chat_id, f"⏳ সার্ভারে খোঁজা হচ্ছে...{f' (Cost: {cost}৳)' if cost > 0 else ''}")
    
    def fetch_and_send():
        try:
            res = call_api(working_uid, f"https://bdris.gov.bd/api/br/info/ubrn/{ubrn}")
            if wait_msg: safe_delete(chat_id, wait_msg.message_id)

            if res and res.status_code == 200:
                try: enc_id = res.json().get('encryptedId')
                except Exception: enc_id = None
                if enc_id: 
                    download_server_pdf(chat_id, user_id, working_uid, enc_id, f"PDF_{ubrn}", cost)
                    return
                else: safe_send(chat_id, "❌ Encrypted ID পাওয়া যায়নি। UBRN চেক করুন।")
            else: safe_send(chat_id, "❌ UBRN পাওয়া যায়নি বা সার্ভার সমস্যা।")
            
            if cost > 0:
                update_balance(user_id, cost)
                safe_send(chat_id, f"⚠️ ডাউনলোড ব্যর্থ হওয়ায় {cost}৳ রিফান্ড করা হয়েছে।")
        finally:
            with download_lock: active_downloads.discard(task_id)

    Thread(target=fetch_and_send, daemon=True).start()

def download_server_pdf(chat_id, user_id, session_user_id, enc_id, filename, cost):
    sess, _ = get_active_session(get_session(session_user_id))
    u_sess = get_session(session_user_id)
    try:
        safe_send(chat_id, "📥 পিডিএফ জেনারেট হচ্ছে, অপেক্ষা করুন...")
        sess.get(f"https://bdris.gov.bd/admin/new-certificate/check?data={enc_id}", headers={'User-Agent': u_sess["ua"]}, timeout=60)
        res = sess.get(f"https://bdris.gov.bd/admin/new-certificate/print?data={enc_id}", headers={'User-Agent': u_sess["ua"]}, timeout=180)
        
        if 'application/pdf' in res.headers.get('Content-Type', ''):
            try: bot.send_document(chat_id, io.BytesIO(res.content), visible_file_name=f"{filename}.pdf")
            except Exception: pass
            if cost > 0: safe_send(chat_id, f"✅ পিডিএফ ডাউনলোড হয়েছে!\n💰 {cost}৳ কাটা হয়েছে। বর্তমান ব্যালেন্স: {get_balance(user_id)}৳")
            else: safe_send(chat_id, "✅ পিডিএফ সফলভাবে ডাউনলোড হয়েছে!")
        else: 
            safe_send(chat_id, "⚠️ পিডিএফ পাওয়া যায়নি। সার্ভার সমস্যা বা সেশন মেয়াদ উত্তীর্ণ।")
            if cost > 0:
                update_balance(user_id, cost)
                safe_send(chat_id, f"⚠️ ডাউনলোড ব্যর্থ হওয়ায় {cost}৳ রিফান্ড করা হয়েছে।")
    except Exception as e:
        logging.error(f"❌ PDF Download Error: {e}")
        safe_send(chat_id, "❌ পিডিএফ ডাউনলোডে সমস্যা হয়েছে। পরে চেষ্টা করুন।")
        if cost > 0:
            update_balance(user_id, cost)
            safe_send(chat_id, f"⚠️ ডাউনলোড ক্র্যাশ করায় {cost}৳ রিফান্ড করা হয়েছে।")

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
                    t_sess["is_alive"] = True
                else: return safe_send(m.chat.id, "❌ ভুল ফরম্যাট।")
            elif field == "CH":
                s, t = extract_sid_tsid(val)
                if s and t:
                    _set_session_cookies(t_sess["ch_session"], s, t)
                    t_sess["is_alive"] = True
                else: return safe_send(m.chat.id, "❌ ভুল ফরম্যাট।")
            elif field == "OTP": t_sess["ch_otp"] = val
            
        save_session_to_db(target_uid, t_sess)
        safe_send(m.chat.id, f"✅ User {target_uid} এর {field} আপডেট হয়েছে!")
    except Exception as e:
        logging.error(f"❌ Admin Edit Error: {e}")
        safe_send(m.chat.id, "❌ আপডেট ব্যর্থ হয়েছে।")

def refresh_admin_panel(chat_id, target_user_id, message_id=None):
    record = access_collection.find_one({"chat_id": target_user_id}) or {}
    p = record.get("permissions", DEFAULT_PERMS)
    t_sess = get_session(target_user_id)
    bal = record.get("balance", 0)
    
    msg = f"👤 *User:* `{target_user_id}`\n💰 Balance: {bal}৳\n🔑 CH OTP: `{t_sess.get('ch_otp', 'N/A')}`\n\nপারমিশন কন্ট্রোল:"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✏️ SEC", callback_data=f"edsec:{target_user_id}"),
        telebot.types.InlineKeyboardButton("✏️ CH", callback_data=f"edch:{target_user_id}"),
        telebot.types.InlineKeyboardButton("✏️ OTP", callback_data=f"edotp:{target_user_id}")
    )
    cmd_labels = [("apps", "Apps"), ("corr", "Corr"), ("repr", "Repr"), ("search", "Search"), ("ubrn_update", "UBRN Update"), ("server_pdf", "Srv PDF"), ("print", "Inline Print")]
    for k, n in cmd_labels:
        st = p.get(k, True)
        label = f"{'❌ Disable' if st else '✅ Enable'} {n}"
        markup.row(telebot.types.InlineKeyboardButton(label, callback_data=f"tgl:{target_user_id}:{k}:{'off' if st else 'on'}"))
    
    status = record.get("status", "allowed")
    if status == "allowed": markup.row(telebot.types.InlineKeyboardButton("🚫 Block User", callback_data=f"block:{target_user_id}"))
    else: markup.row(telebot.types.InlineKeyboardButton("✅ Unblock User", callback_data=f"unblock:{target_user_id}"))
    
    if message_id: safe_edit(msg, chat_id, message_id, reply_markup=markup, parse_mode='Markdown')
    else: safe_send(chat_id, msg, reply_markup=markup, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    
    if is_rate_limited(user_id): return bot.answer_callback_query(call.id, "⚠️ একটু ধীরে ক্লিক করুন!", show_alert=True)
    if not check_user_access(user_id, call.from_user.first_name): return bot.answer_callback_query(call.id, "🚫 অ্যাক্সেস নেই!", show_alert=True)
        
    u_sess = get_session(user_id)
    perms = get_user_permissions(user_id)
    
    parts = call.data.split(':')
    action = parts[0]
    sid = parts[1] if len(parts) > 1 else ""
    
    enc_id = None
    if action in ["pay", "recv", "reg", "coreg", "print"]:
        enc_id = u_sess["id_cache"].get(sid)
        if not enc_id: return bot.answer_callback_query(call.id, "❌ ক্যাশ এক্সপায়ার হয়েছে! অনুগ্রহ করে লিস্টটি রিফ্রেশ করুন।", show_alert=True)

    if action in ["next", "prev"]:
        cmd = sid
        with session_lock:
            if action == "next": u_sess["app_start"] += u_sess["app_length"]
            else: u_sess["app_start"] = max(0, u_sess["app_start"] - u_sess["app_length"])
        bot.answer_callback_query(call.id)
        fetch_list_ui(chat_id, user_id, cmd, call.message.message_id)

    elif action == "reqrecharge":
        msg = safe_send(chat_id, "💼 *রিচার্জের নিয়ম:*\n১. বিকাশ/নগদ নম্বরে Send Money করুন\n২. TrxID মেসেজে পাঠান:", parse_mode="Markdown")
        if msg: bot.register_next_step_handler(msg, process_recharge)
        bot.answer_callback_query(call.id)

    elif action == "apprvbal" and user_id == ADMIN_ID:
        target_id = int(sid)
        msg = safe_send(chat_id, f"User {target_id} এর জন্য কত টাকা অ্যাড করবেন? (শুধুমাত্র সংখ্যা)")
        if msg: bot.register_next_step_handler(msg, admin_add_balance_step, target_id)
        
        safe_original_text = call.message.text or ""
        safe_edit(f"{safe_original_text}\n\n✅ Processing...", chat_id, call.message.message_id)
        bot.answer_callback_query(call.id)

    elif action == "rejbal" and user_id == ADMIN_ID:
        target_id = int(sid)
        safe_send(target_id, "❌ আপনার রিচার্জ রিকোয়েস্ট বাতিল করা হয়েছে।")
        safe_original_text = call.message.text or ""
        safe_edit(f"{safe_original_text}\n\n❌ Rejected", chat_id, call.message.message_id)
        bot.answer_callback_query(call.id)

    elif action == "admuser" and user_id == ADMIN_ID:
        refresh_admin_panel(chat_id, int(sid), call.message.message_id)
        bot.answer_callback_query(call.id)

    elif action == "tgl" and user_id == ADMIN_ID and len(parts) == 4:
        target_id = int(parts[1])
        perm_key = parts[2]
        state_val = parts[3]
        
        if perm_key not in DEFAULT_PERMS or state_val not in ["on", "off"]: 
            return bot.answer_callback_query(call.id, "❌ Invalid permission key.")
            
        access_collection.update_one({"chat_id": target_id}, {"$set": {f"permissions.{perm_key}": state_val == "on"}})
        bot.answer_callback_query(call.id, f"✅ {perm_key} আপডেট হয়েছে!")
        refresh_admin_panel(chat_id, target_id, call.message.message_id)

    elif action == "edsec" and user_id == ADMIN_ID:
        msg = safe_send(chat_id, f"User {sid} এর নতুন SEC সেশন দিন:")
        if msg: bot.register_next_step_handler(msg, admin_edit_field, int(sid), "SEC")
        bot.answer_callback_query(call.id)

    elif action == "edch" and user_id == ADMIN_ID:
        msg = safe_send(chat_id, f"User {sid} এর নতুন CH সেশন দিন:")
        if msg: bot.register_next_step_handler(msg, admin_edit_field, int(sid), "CH")
        bot.answer_callback_query(call.id)

    elif action == "edotp" and user_id == ADMIN_ID:
        msg = safe_send(chat_id, f"User {sid} এর নতুন OTP দিন:")
        if msg: bot.register_next_step_handler(msg, admin_edit_field, int(sid), "OTP")
        bot.answer_callback_query(call.id)

    elif action == "block" and user_id == ADMIN_ID:
        access_collection.update_one({"chat_id": int(sid)}, {"$set": {"status": "blocked"}})
        bot.answer_callback_query(call.id, f"✅ User {sid} ব্লক করা হয়েছে।", show_alert=True)
        safe_send(int(sid), "🚫 আপনাকে ব্লক করা হয়েছে।")

    elif action == "unblock" and user_id == ADMIN_ID:
        access_collection.update_one({"chat_id": int(sid)}, {"$set": {"status": "allowed"}})
        bot.answer_callback_query(call.id, f"✅ User {sid} আনব্লক করা হয়েছে।", show_alert=True)
        safe_send(int(sid), "✅ আপনার অ্যাক্সেস পুনরায় চালু হয়েছে।")

    elif action == "pay":
        cost = get_service_cost(user_id, "pay")
        task_id = f"pay_{user_id}_{enc_id}"
        
        with download_lock:
            if task_id in active_downloads: return bot.answer_callback_query(call.id, "⚠️ রিকোয়েস্ট প্রসেসিং-এ আছে...", show_alert=True)
            if cost > 0 and get_balance(user_id) < cost: return bot.answer_callback_query(call.id, f"❌ পর্যাপ্ত ব্যালেন্স ({cost}৳) নেই!", show_alert=True)
            active_downloads.add(task_id)
            if cost > 0: update_balance(user_id, -cost)

        bot.answer_callback_query(call.id, "⏳ পেমেন্ট প্রসেস হচ্ছে...")
        
        def process_payment():
            try:
                _, active_csrf = get_active_session(u_sess)
                payload = {'data': enc_id, 'paymentType': 'PAYMENT_BY_DISCOUNT', 'discountAmount': '50', 'discountSharokNo': str(u_sess["sharok_no"]), 'discountSharokDate': datetime.now().strftime("%d/%m/%Y"), '_csrf': active_csrf}
                res = call_api(user_id, "https://bdris.gov.bd/api/payment/receive", method="POST", data=payload)
                
                if res and res.status_code == 200:
                    with session_lock: u_sess["sharok_no"] += 1
                    safe_send(chat_id, f"✅ পেমেন্ট সফল! {cost}৳ কাটা হয়েছে। বর্তমান ব্যালেন্স: {get_balance(user_id)}৳" if cost > 0 else "✅ পেমেন্ট সফল!")
                else:
                    if cost > 0: update_balance(user_id, cost)
                    safe_send(chat_id, "❌ পেমেন্ট ব্যর্থ! রিফান্ড করা হয়েছে।")
            finally:
                with download_lock: active_downloads.discard(task_id)

        Thread(target=process_payment, daemon=True).start()

    elif action == "recv":
        _, active_csrf = get_active_session(u_sess)
        res = call_api(user_id, "https://bdris.gov.bd/api/application/receive", method="POST", data={'data': enc_id, '_csrf': active_csrf})
        if res and res.status_code == 200: bot.answer_callback_query(call.id, "✅ রিসিভ সফল!", show_alert=True)
        else: bot.answer_callback_query(call.id, "❌ রিসিভ ব্যর্থ!", show_alert=True)

    elif action in ["reg", "coreg"] and u_sess["mode"] == "CHAIRMAN":
        bot.answer_callback_query(call.id, "⏳ রেজিস্ট্রেশন হচ্ছে...")
        path = "correction-application" if action == "coreg" else "application"
        try:
            ch_sess = get_active_session(u_sess)[0]
            html = ch_sess.get(f"https://bdris.gov.bd/admin/br/{path}/register?data={enc_id}", headers={'User-Agent': u_sess["ua"]}, timeout=30).text
            v = re.search(r'<option\s+value="(\d{17})"[^>]*>([^<]+)</option>', html)
            if v:
                _, active_csrf = get_active_session(u_sess)
                payload = {"birthPlaceAndDobVerifierName": v.group(2).strip(), "birthPlaceAndDobVerifierBrn": v.group(1), "birthPlaceAndDobVerificationDate": datetime.now().strftime("%d/%m/%Y"), "otp": u_sess["ch_otp"], "data": enc_id, "_csrf": active_csrf}
                res = call_api(user_id, f"https://bdris.gov.bd/api/br/{path}/register", method="POST", data=payload)
                if res and res.status_code == 200: safe_send(chat_id, "✅ রেজিস্ট্রেশন সফল!")
                else: safe_send(chat_id, f"❌ রেজিস্ট্রেশন ব্যর্থ। (Status: {res.status_code if res else 'N/A'})")
            else: safe_send(chat_id, "❌ ভেরিফায়ার পাওয়া যায়নি। পেজ সোর্স চেক করুন।")
        except Exception as e:
            safe_send(chat_id, "❌ রেজিস্ট্রেশনে ক্র্যাশ হয়েছে।")

    elif action == "print":
        if perms.get("print") or user_id == ADMIN_ID:
            cost = get_service_cost(user_id, "pdf")
            task_id = f"print_{user_id}_{enc_id}"
            
            with download_lock:
                if task_id in active_downloads: return bot.answer_callback_query(call.id, "⚠️ ডাউনলোড প্রসেসিং-এ আছে...", show_alert=True)
                if cost > 0 and get_balance(user_id) < cost: return bot.answer_callback_query(call.id, f"❌ ব্যালেন্স কম! প্রয়োজন: {cost}৳", show_alert=True)
                active_downloads.add(task_id)
                if cost > 0: update_balance(user_id, -cost)
                
            bot.answer_callback_query(call.id, "⏳ ডাউনলোড শুরু হচ্ছে...")
            working_uid = user_id if u_sess["is_alive"] else ADMIN_ID
            
            def print_pdf_thread():
                try: download_server_pdf(chat_id, user_id, working_uid, enc_id, f"Cert_{sid}", cost)
                finally:
                    with download_lock: active_downloads.discard(task_id)
                    
            Thread(target=print_pdf_thread, daemon=True).start()
        else: bot.answer_callback_query(call.id, "🚫 প্রিন্টের অনুমতি নেই!", show_alert=True)
    else: bot.answer_callback_query(call.id)

# ==========================================
# ১১. মেইন রাউটার
# ==========================================
@bot.message_handler(func=lambda m: True)
def router(m):
    cid = m.chat.id
    uid = m.from_user.id
    if not m.text: return safe_send(cid, "⚠️ শুধুমাত্র টেক্সট মেসেজ সাপোর্টেড।")
    
    t = m.text
    if is_rate_limited(uid): return
    if not check_user_access(uid, m.from_user.first_name): return safe_send(cid, "🚫 আপনার অ্যাক্সেস নেই।")
        
    u_sess = get_session(uid)
    perms = get_user_permissions(uid)

    if uid == ADMIN_ID:
        if t == "/payment_on":
            settings_collection.update_one({"_id": "config"}, {"$set": {"payment_active": True}})
            return safe_send(cid, "✅ *পেমেন্ট সিস্টেম চালু।*", parse_mode="Markdown", reply_markup=generate_main_menu(cid, uid))
        elif t == "/payment_off":
            settings_collection.update_one({"_id": "config"}, {"$set": {"payment_active": False}})
            return safe_send(cid, "❌ *পেমেন্ট সিস্টেম বন্ধ।* সব সার্ভিস ফ্রি।", parse_mode="Markdown", reply_markup=generate_main_menu(cid, uid))
        elif t == "🔑 Admin Login":
            msg = safe_send(cid, "🔑 এডমিন সেশন (SESSION ও TS) দিন:")
            if msg: bot.register_next_step_handler(msg, admin_login_logic)
            return
        elif t == "🛠️ Check Cookies":
            sec_c = u_sess["req_session"].cookies.get_dict()
            ch_c = u_sess["ch_session"].cookies.get_dict()
            return safe_send(cid, f"*SEC Cookies:* `{sec_c}`\n*CH Cookies:* `{ch_c}`\n*OTP:* `{u_sess['ch_otp']}`", parse_mode="Markdown")
        elif t == "👥 Manage Users":
            users = list(access_collection.find({}))
            if not users: return safe_send(cid, "📭 কোনো ইউজার নেই।")
            markup = telebot.types.InlineKeyboardMarkup()
            for u in users:
                status_icon = "✅" if u.get('status') == 'allowed' else "🚫"
                markup.row(telebot.types.InlineKeyboardButton(f"{status_icon} {u.get('name', 'N/A')} | {u.get('balance', 0)}৳", callback_data=f"admuser:{u.get('chat_id')}"))
            return safe_send(cid, "👥 ইউজার প্যানেল:", reply_markup=markup)

    if "/start" in t or "Back to Menu" in t:
        return safe_send(cid, "🚀 BDRIS Master Bot Active!", reply_markup=generate_main_menu(cid, uid))

    elif t == "🏠 Dashboard":
        if u_sess["is_alive"]:
            success, _ = navigate_to(uid, "https://bdris.gov.bd/admin/")
            if success: bot.reply_to(m, "🏠 ড্যাশবোর্ড রিফ্রেশ হয়েছে।", reply_markup=generate_main_menu(cid, uid))
            else: bot.reply_to(m, "❌ ড্যাশবোর্ড লোড ব্যর্থ।")
        else: safe_send(cid, "⚠️ আগে লগইন করুন।", reply_markup=generate_main_menu(cid, uid))
        return

    elif t == "💰 My Profile & Recharge":
        if not is_payment_active(): return safe_send(cid, "ℹ️ বর্তমানে সব সার্ভিস ফ্রি। রিচার্জের প্রয়োজন নেই।")
        safe_name = str(m.from_user.first_name).replace('*', '').replace('_', '').replace('`', '')
        msg = f"👤 *Profile:* {safe_name}\n🆔 ID: `{uid}`\n\n💰 *Current Balance: {get_balance(uid)}৳*"
        markup = telebot.types.InlineKeyboardMarkup().add(telebot.types.InlineKeyboardButton("➕ Add Balance", callback_data="reqrecharge"))
        return safe_send(cid, msg, reply_markup=markup, parse_mode="Markdown")

    elif t == "🔑 User Login":
        msg = safe_send(cid, "✅ নিবন্ধকের সেশন দিন (SESSION= ও TS0108b707= সহ):")
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
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)

if __name__ == "__main__":
    logging.info("🚀 BDRIS Bot Starting...")
    Thread(target=keep_sessions_alive_and_cleanup, daemon=True).start()
    Thread(target=run_flask, daemon=True).start()
    logging.info("✅ All threads started. Bot polling...")
    
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            logging.error(f"❌ Polling Crashed: {e}. Restarting in 5 seconds...")
            time.sleep(5)
