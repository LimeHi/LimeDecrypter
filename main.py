import telebot
import requests
import base64
import os
import re
import sqlite3
import string
import random
import threading
import time
import json
import urllib.parse
import uuid
import gzip
import io
import urllib3

from flask import Flask, Response

# Отключаем ошибки SSL для "кривых" серверов панелей
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== КОНФИГУРАЦИЯ ====================

TOKEN = os.getenv('TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')
FOOTER_TAG = os.getenv('FOOTER_TAG', '')
FILE_PREFIX = os.getenv('FILE_PREFIX', 'keys')
BASE_URL = os.getenv('BASE_URL')
PORT = int(os.getenv('PORT', '3000'))
DATA_DIR = os.getenv('DATA_DIR', '/app/data')

if not TOKEN or not CHANNEL_USERNAME or not BASE_URL:
    raise ValueError("TOKEN, CHANNEL_USERNAME и BASE_URL должны быть заданы!")

BASE_URL = BASE_URL.rstrip('/')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'links.db')

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=10)

# ==================== БАЗА ДАННЫХ ====================

db_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS links (
            code TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            owner_id INTEGER,
            name TEXT DEFAULT NULL,
            hwid TEXT DEFAULT NULL,
            source_url TEXT DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Миграция базы данных (если таблица старая)
    try: conn.execute('ALTER TABLE links ADD COLUMN name TEXT DEFAULT NULL')
    except: pass
    try: conn.execute('ALTER TABLE links ADD COLUMN hwid TEXT DEFAULT NULL')
    except: pass
    try: 
        conn.execute('ALTER TABLE links ADD COLUMN source_url TEXT DEFAULT NULL')
        conn.execute("UPDATE links SET source_url = content, content = '' WHERE type = 'url' AND source_url IS NULL")
        conn.commit()
    except: pass
    return conn

def generate_code(length: int = 7) -> str:
    alphabet = string.ascii_letters + string.digits
    with db_lock:
        conn = get_db()
        try:
            while True:
                code = ''.join(random.choices(alphabet, k=length))
                if not conn.execute('SELECT 1 FROM links WHERE code = ?', (code,)).fetchone():
                    return code
        finally:
            conn.close()

def save_link(link_type: str, content: str, owner_id: int, name: str = None, hwid: str = None, source_url: str = None) -> str:
    code = generate_code()
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                'INSERT INTO links (code, type, content, owner_id, name, hwid, source_url) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (code, link_type, content, owner_id, name, hwid, source_url)
            )
            conn.commit()
        finally:
            conn.close()
    return code

def get_link_full(code: str):
    with db_lock:
        conn = get_db()
        try:
            return conn.execute('SELECT type, content, owner_id, name, hwid, source_url FROM links WHERE code = ?', (code,)).fetchone()
        finally:
            conn.close()

def get_links_by_owner(owner_id: int, limit: int = 20):
    with db_lock:
        conn = get_db()
        try:
            return conn.execute('SELECT code, type, content, created_at, name, source_url FROM links WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?', (owner_id, limit)).fetchall()
        finally:
            conn.close()

def update_link_content(code: str, new_content: str):
    with db_lock:
        conn = get_db()
        try:
            conn.execute('UPDATE links SET content = ? WHERE code = ?', (new_content, code))
            conn.commit()
        finally:
            conn.close()

def update_link_hwid(code: str, owner_id: int, hwid: str) -> bool:
    with db_lock:
        conn = get_db()
        try:
            cur = conn.execute('UPDATE links SET hwid = ? WHERE code = ? AND owner_id = ?', (hwid, code, owner_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

def delete_link(code: str, owner_id: int) -> bool:
    with db_lock:
        conn = get_db()
        try:
            cur = conn.execute('DELETE FROM links WHERE code = ? AND owner_id = ?', (code, owner_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

def build_short_url(code: str) -> str:
    return f"{BASE_URL}/{code}"

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def with_footer(text: str) -> str:
    return f"{text}\n\n{FOOTER_TAG}" if FOOTER_TAG else text

sub_cache = {}
def is_subscribed(user_id: int) -> bool:
    now = time.time()
    if user_id in sub_cache and now - sub_cache[user_id]['time'] < 300:
        return sub_cache[user_id]['status']
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        status = member.status in ('member', 'administrator', 'creator')
        sub_cache[user_id] = {'status': status, 'time': now}
        return status
    except Exception:
        sub_cache[user_id] = {'status': False, 'time': now - 270} 
        return False

def send_subscribe_prompt(chat_id: int):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
    markup.add(telebot.types.InlineKeyboardButton("Я подписался ✅", callback_data="check_sub"))
    bot.send_message(chat_id, with_footer(f"Для использования бота необходимо подписаться на канал {CHANNEL_USERNAME}."), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def handle_check_sub(call):
    if call.from_user.id in sub_cache:
        del sub_cache[call.from_user.id]
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Подписка подтверждена ✅")
        bot.send_message(call.message.chat.id, with_footer("Отлично! Теперь можешь пользоваться ботом."))
    else:
        bot.answer_callback_query(call.id, "Подписка не найдена.", show_alert=True)

# ==================== ЭКСТРАКЦИЯ КЛЮЧЕЙ ====================

KEY_PATTERN = re.compile(r'(?:vless|vmess|trojan|ss|ssr|hysteria2?|hy2|tuic)://[^\s<>"]+', re.IGNORECASE)

def try_decode_base64(text: str) -> str:
    try:
        padded = text + '=' * (-len(text) % 4)
        return base64.b64decode(padded).decode('utf-8')
    except Exception: return text

def is_html(ct: str, body: str) -> bool:
    return "text/html" in ct or body.lstrip().startswith(("<html", "<!DOCTYPE", "<!doctype"))

def extract_keys(text: str) -> list:
    keys = KEY_PATTERN.findall(text)
    if keys: return keys
    
    # Попытка разобрать как XRAY JSON
    try:
        data = json.loads(text)
        configs = data if isinstance(data, list) else [data]
        links = []
        for cfg in configs:
            if not isinstance(cfg, dict): continue
            for ob in cfg.get('outbounds', []):
                proto = ob.get('protocol')
                if proto == 'vless':
                    # Упрощенная генерация vless из json (основные поля)
                    addr = ob.get('settings', {}).get('vnext', [{}])[0].get('address', '')
                    port = ob.get('settings', {}).get('vnext', [{}])[0].get('port', '')
                    uid = ob.get('settings', {}).get('vnext', [{}])[0].get('users', [{}])[0].get('id', '')
                    if addr and port and uid:
                        links.append(f"vless://{uid}@{addr}:{port}?type=tcp&security=none")
        return links
    except: pass
    return []

# ==================== HAPP ИМИТАЦИЯ ====================

def fetch_keys_from_url(url: str, hwid: str, timeout: int = 5) -> list:
    """Делает запрос с нужным HWID и возвращает список ключей"""
    actual_hwid = hwid if hwid else str(random.randint(100000000000000000, 999999999999999999))
    headers = {
        "User-Agent": f"Happ/3.26.3/Android/{actual_hwid}",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive"
    }

    session = requests.Session()
    session.headers.update(headers)
    
    def fetch(u):
        r = session.get(u, timeout=timeout, stream=True, verify=False)
        r.raise_for_status()
        if r.headers.get('Content-Encoding') == 'gzip':
            try: return gzip.GzipFile(fileobj=io.BytesIO(r.content)).read().decode('utf-8')
            except: pass
        return r.text

    try:
        body = fetch(url)
    except Exception as e:
        print(f"[HTTP] Ошибка скачивания {url}: {e}")
        return []

    # Если это HTML, возможно это панель, где подписка в /sub/
    if is_html("", body):
        match = re.search(r'(https?://[^\s"\'<>]+\.(?:txt|json|xml|conf|cfg|sub))', body, re.IGNORECASE)
        if match:
            try: body = fetch(match.group(1))
            except: pass

    decoded = try_decode_base64(body)
    return extract_keys(decoded)

# ==================== HTTP-СЕРВЕР (Динамический прокси без таймаутов) ====================

app = Flask(__name__)

def generate_fake_node(message: str) -> str:
    safe_msg = urllib.parse.quote(message)
    return f"vless://00000000-0000-0000-0000-000000000000@127.0.0.1:443?type=tcp&security=none#{safe_msg}"

@app.route('/')
def health_check(): return 'OK'

@app.route('/<code>')
def resolve_link(code):
    print(f"\n[GET] Клиент запросил подписку: /{code}", flush=True)
    row = get_link_full(code)
    
    if not row:
        encoded = base64.b64encode(generate_fake_node("ОШИБКА: Подписка удалена из бота").encode('utf-8')).decode('utf-8')
        return Response(encoded, mimetype='text/plain')

    link_type, content, owner_id, name, hwid, source_url = row

    if link_type == 'url':
        # Пытаемся быстро (за 4 секунды) скачать новые ключи от оригинального сервера
        new_keys = []
        try:
            new_keys = fetch_keys_from_url(source_url, hwid, timeout=4)
        except Exception:
            pass
        
        if new_keys:
            # Если успешно - сохраняем в базу и отдаём
            content = "\n".join(new_keys)
            update_link_content(code, content)
            print(f"[SUCCESS] Конфиги обновлены и отданы клиенту.", flush=True)
        else:
            # Если сервер недоступен - отдаём ПОСЛЕДНЮЮ СОХРАНЁННУЮ КОПИЮ из базы! (Клиент никогда не отвалится)
            print(f"[FALLBACK] Оригинал недоступен. Отдаю сохраненную копию из БД.", flush=True)
        
        # Если база вообще пустая (при первой генерации не получилось)
        if not content:
            content = generate_fake_node("ОШИБКА: Исходный сервер не работает")
            
        encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        return Response(encoded, mimetype='text/plain')
    else:
        # Для /addkeys
        encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        return Response(encoded, mimetype='text/plain')

# ==================== КОМАНДЫ БОТА ====================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    welcome_text = (
        "Здравствуйте.\n\n"
        "📌 <b>Бот умеет:</b>\n"
        "• /shorten — создать подписку. Бот будет скачивать конфиги, притворяясь Happ-клиентом. HWID можно настроить.\n"
        "• /addkeys — сохранить ключи (без автообновления).\n"
        "• /profile — управление подписками и <b>настройка HWID</b>."
    )
    bot.reply_to(message, welcome_text, parse_mode="HTML")

# --- /shorten ---
user_data_lock = threading.Lock()
user_data = {}

@bot.message_handler(commands=['shorten'])
def cmd_shorten(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    msg = bot.reply_to(message, "Пришлите ссылку на подписку, конфигурации которой я должен забирать:")
    bot.register_next_step_handler(msg, process_url)

def process_url(message):
    url = (message.text or "").strip()
    if not url.startswith("http"):
        bot.reply_to(message, "Это не ссылка. Начните заново через /shorten.")
        return
    with user_data_lock: user_data[message.from_user.id] = {'url': url}
    msg = bot.reply_to(message, "Введите название для этой подписки:")
    bot.register_next_step_handler(msg, process_name)

def process_name(message):
    user_id = message.from_user.id
    with user_data_lock:
        if user_id not in user_data: return
        user_data[user_id]['name'] = (message.text or "").strip() or "Без названия"
    msg = bot.reply_to(message, "Введите HWID для эмуляции клиента Happ (или отправьте 0 для создания случайного):")
    bot.register_next_step_handler(msg, process_hwid)

def process_hwid(message):
    user_id = message.from_user.id
    with user_data_lock:
        data = user_data.get(user_id)
        if not data: return
        hwid_input = (message.text or "").strip()
        data['hwid'] = str(random.randint(100000000000000000, 999999999999999999)) if hwid_input == '0' else hwid_input

    # Сразу пытаемся скачать ключи, чтобы сохранить их в базу
    bot.reply_to(message, "⏳ Создаю подписку и забираю первые конфиги...")
    keys = fetch_keys_from_url(data['url'], data['hwid'], timeout=10)
    content = "\n".join(keys) if keys else ""
    
    code = save_link('url', content, user_id, data['name'], data['hwid'], data['url'])
    short_url = build_short_url(code)
    
    status = f"✅ Извлечено {len(keys)} ключей." if keys else "⚠️ Сервер недоступен, но ссылка создана. Будет пытаться обновиться позже."
    text = f"<b>Подписка готова!</b>\n\n📌 Название: {data['name']}\n🆔 HWID: {data['hwid']}\n🔗 {short_url}\n\n{status}"
    bot.send_message(message.chat.id, text, parse_mode="HTML")

# --- /addkeys ---
@bot.message_handler(commands=['addkeys'])
def cmd_addkeys(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    msg = bot.reply_to(message, "Пришлите ключи для сохранения:")
    bot.register_next_step_handler(msg, process_addkeys)

def process_addkeys(message):
    keys = extract_keys((message.text or "").strip())
    if not keys:
        bot.reply_to(message, "Ключи не найдены.")
        return
    code = save_link('keys', "\n".join(keys), message.from_user.id, name="Статичные ключи")
    bot.reply_to(message, f"✅ Сохранено ключей: {len(keys)}\n\n🔗 {build_short_url(code)}")

# --- ПРОФИЛЬ И НАСТРОЙКИ ---
def build_profile_menu(rows: list) -> telebot.types.InlineKeyboardMarkup:
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for row in rows:
        code, link_type, content, created_at, name, source_url = row
        icon = "🔄" if link_type == 'url' else "🔑"
        markup.add(telebot.types.InlineKeyboardButton(text=f"{icon} {name or code}", callback_data=f"view:{code}"))
    return markup

@bot.message_handler(commands=['profile'])
def cmd_profile(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    rows = get_links_by_owner(message.from_user.id)
    if not rows:
        bot.reply_to(message, "У вас нет подписок.")
        return
    bot.reply_to(message, "Ваши подписки:", reply_markup=build_profile_menu(rows))

@bot.callback_query_handler(func=lambda call: call.data.startswith("view:"))
def handle_view_link(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)
    if not row or row[2] != call.from_user.id:
        bot.answer_callback_query(call.id, "Не найдено.", show_alert=True)
        return

    link_type, content, owner_id, name, hwid, source_url = row
    
    lines = content.split('\n') if content else []
    keys_count = len([x for x in lines if x.strip()])
    
    text = f"<b>{name or 'Подписка'}</b>\n"
    text += f"🔗 {build_short_url(code)}\n\n"
    if link_type == 'url':
        text += f"🌐 <b>Источник:</b> {source_url}\n"
        text += f"🆔 <b>HWID для обхода:</b> {hwid}\n"
        text += f"📦 <b>Сохранено ключей:</b> {keys_count}"
    else:
        text += f"📦 <b>Сохранено ключей:</b> {keys_count}"

    markup = telebot.types.InlineKeyboardMarkup()
    if link_type == 'url':
        markup.row(
            telebot.types.InlineKeyboardButton("⚙️ Изменить HWID", callback_data=f"hwid:{code}"),
            telebot.types.InlineKeyboardButton("🔄 Обновить сейчас", callback_data=f"force_update:{code}")
        )
    markup.row(
        telebot.types.InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{code}"),
        telebot.types.InlineKeyboardButton("‹ Назад", callback_data="profile_back")
    )

    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
    except: pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("force_update:"))
def handle_force_update(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)
    if not row or row[2] != call.from_user.id: return
    
    bot.answer_callback_query(call.id, "Связываюсь с сервером...")
    link_type, content, owner_id, name, hwid, source_url = row
    
    keys = fetch_keys_from_url(source_url, hwid, timeout=10)
    if keys:
        update_link_content(code, "\n".join(keys))
        bot.send_message(call.message.chat.id, f"✅ Подписка <b>{name}</b> успешно обновлена. Извлечено ключей: {len(keys)}", parse_mode="HTML")
    else:
        bot.send_message(call.message.chat.id, f"⚠️ Не удалось обновить <b>{name}</b>. Исходный сервер не ответил.", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("hwid:"))
def handle_edit_hwid(call):
    code = call.data.split(":", 1)[1]
    msg = bot.send_message(call.message.chat.id, "Введите новый HWID:")
    bot.register_next_step_handler(msg, process_edit_hwid, code, call.from_user.id)

def process_edit_hwid(message, code, owner_id):
    hwid = message.text.strip()
    if update_link_hwid(code, owner_id, hwid):
        bot.reply_to(message, "✅ HWID успешно изменен. Нажмите 'Обновить сейчас' в профиле, чтобы применить.")
    else:
        bot.reply_to(message, "Ошибка.")

@bot.callback_query_handler(func=lambda call: call.data == "profile_back")
def handle_profile_back(call):
    rows = get_links_by_owner(call.from_user.id)
    bot.answer_callback_query(call.id)
    if not rows:
        bot.edit_message_text("Пусто.", call.message.chat.id, call.message.message_id)
        return
    bot.edit_message_text("Ваши подписки:", call.message.chat.id, call.message.message_id, reply_markup=build_profile_menu(rows))

@bot.callback_query_handler(func=lambda call: call.data.startswith("del:"))
def handle_delete_link(call):
    code = call.data.split(":", 1)[1]
    if delete_link(code, call.from_user.id):
        bot.answer_callback_query(call.id, "Удалено ✅")
        handle_profile_back(call)

# ==================== ЗАПУСК ====================

def run_bot_polling():
    print("[START] Запуск polling Telegram бота...", flush=True)
    bot.infinity_polling(timeout=60, logger_level=None)

if __name__ == '__main__':
    print("[START] Запуск...", flush=True)
    conn = get_db()
    conn.close()
    
    try: bot.delete_webhook(drop_pending_updates=True, timeout=5)
    except: pass

    threading.Thread(target=run_bot_polling, daemon=True).start()
    
    print(f"[START] HTTP сервер запущен на порту {PORT}", flush=True)
    app.run(host='0.0.0.0', port=PORT, threaded=True, use_reloader=False)
