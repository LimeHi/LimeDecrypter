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
import hashlib
import hmac
import uuid
import gzip
import io
import urllib3

from flask import Flask, abort, Response, request

# Отключаем предупреждения об опасных SSL, так как многие VPN-панели их используют
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== КОНФИГУРАЦИЯ ====================

TOKEN = os.getenv('TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')
FOOTER_TAG = os.getenv('FOOTER_TAG', '')
FILE_PREFIX = os.getenv('FILE_PREFIX', 'keys')
BASE_URL = os.getenv('BASE_URL')
PORT = int(os.getenv('PORT', '3000'))
DATA_DIR = os.getenv('DATA_DIR', '/app/data')
BYPASS_SECRET = os.getenv('BYPASS_SECRET', 'default_secret_change_me')

if not TOKEN or not CHANNEL_USERNAME or not BASE_URL:
    raise ValueError("TOKEN, CHANNEL_USERNAME и BASE_URL должны быть заданы!")

BASE_URL = BASE_URL.rstrip('/')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'links.db')

bot = telebot.TeleBot(TOKEN)
telebot.apihelper.READ_TIMEOUT = 20
telebot.apihelper.CONNECT_TIMEOUT = 15

# ==================== ВРЕМЕННОЕ ХРАНИЛИЩЕ ====================

pending_saves = {}
pending_saves_lock = threading.Lock()

def create_pending_save(content: str) -> str:
    session_id = str(uuid.uuid4())[:8]
    with pending_saves_lock:
        pending_saves[session_id] = content
    return session_id

def get_pending_save(session_id: str) -> str:
    with pending_saves_lock:
        return pending_saves.pop(session_id, None)

def cleanup_pending_saves():
    while True:
        time.sleep(300)
        with pending_saves_lock:
            if len(pending_saves) > 1000:
                keys = list(pending_saves.keys())[:500]
                for k in keys:
                    pending_saves.pop(k, None)

threading.Thread(target=cleanup_pending_saves, daemon=True).start()

# ==================== БЕЗОПАСНАЯ ОТПРАВКА ====================

def safe_send_message(chat_id, text, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try: return bot.send_message(chat_id, text, **kwargs)
        except Exception: time.sleep(delay)

def safe_send_document(chat_id, document, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try: return bot.send_document(chat_id, document, **kwargs)
        except Exception:
            time.sleep(delay)
            if hasattr(document, 'seek'):
                try: document.seek(0)
                except: pass

def safe_reply_to(message, text, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try: return bot.reply_to(message, text, **kwargs)
        except Exception: time.sleep(delay)

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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    try: conn.execute('ALTER TABLE links ADD COLUMN name TEXT DEFAULT NULL')
    except: pass
    try: conn.execute('ALTER TABLE links ADD COLUMN hwid TEXT DEFAULT NULL')
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

def save_link(link_type: str, content: str, owner_id: int, name: str = None, hwid: str = None) -> str:
    code = generate_code()
    with db_lock:
        conn = get_db()
        try:
            conn.execute('INSERT INTO links (code, type, content, owner_id, name, hwid) VALUES (?, ?, ?, ?, ?, ?)',
                         (code, link_type, content, owner_id, name, hwid))
            conn.commit()
        finally:
            conn.close()
    return code

def get_link(code: str):
    with db_lock:
        conn = get_db()
        try: return conn.execute('SELECT type, content, name, hwid FROM links WHERE code = ?', (code,)).fetchone()
        finally: conn.close()

def get_link_full(code: str):
    with db_lock:
        conn = get_db()
        try: return conn.execute('SELECT type, content, owner_id, name, hwid FROM links WHERE code = ?', (code,)).fetchone()
        finally: conn.close()

def get_links_by_owner(owner_id: int, limit: int = 20):
    with db_lock:
        conn = get_db()
        try: return conn.execute('SELECT code, type, content, created_at, name FROM links WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?', (owner_id, limit)).fetchall()
        finally: conn.close()

def delete_link(code: str, owner_id: int) -> bool:
    with db_lock:
        conn = get_db()
        try:
            cur = conn.execute('DELETE FROM links WHERE code = ? AND owner_id = ?', (code, owner_id))
            conn.commit()
            return cur.rowcount > 0
        finally: conn.close()

def update_link_name(code: str, owner_id: int, name: str) -> bool:
    with db_lock:
        conn = get_db()
        try:
            cur = conn.execute('UPDATE links SET name = ? WHERE code = ? AND owner_id = ?', (name, code, owner_id))
            conn.commit()
            return cur.rowcount > 0
        finally: conn.close()

def build_short_url(code: str) -> str:
    return f"{BASE_URL}/{code}"

# ==================== ВСПОМОГАТЕЛЬНЫЕ ====================

file_counter = 0

def next_file_name() -> str:
    global file_counter
    file_counter += 1
    return f"{FILE_PREFIX}{file_counter}.txt"

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
    except Exception as e:
        print(f"[ОШИБКА проверки подписки]: {e}", flush=True)
        sub_cache[user_id] = {'status': False, 'time': now - 270} 
        return False

def send_subscribe_prompt(chat_id: int):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
    markup.add(telebot.types.InlineKeyboardButton("Я подписался ✅", callback_data="check_sub"))
    safe_send_message(chat_id, with_footer(f"Для использования бота необходимо подписаться на канал {CHANNEL_USERNAME}."), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def handle_check_sub(call):
    if call.from_user.id in sub_cache:
        del sub_cache[call.from_user.id]
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Подписка подтверждена ✅")
        safe_send_message(call.message.chat.id, with_footer("Отлично! Теперь можешь пользоваться ботом."))
    else:
        bot.answer_callback_query(call.id, "Подписка не найдена.", show_alert=True)

KEY_PATTERN = re.compile(r'(?:vless|vmess|trojan|ss|ssr|hysteria2?|hy2|tuic)://[^\s<>"]+', re.IGNORECASE)

def decrypt_happ_link(encrypted_link: str) -> str:
    try:
        response = requests.post("https://api.ioo.ir/v1/happ/decrypt", json={"link": encrypted_link}, headers={"Content-Type": "application/json"}, timeout=15, verify=False)
        data = response.json()
        if data.get("ok"): return data.get("result", "")
        return ""
    except Exception: return ""

def try_decode_base64(text: str) -> str:
    try:
        padded = text + '=' * (-len(text) % 4)
        return base64.b64decode(padded).decode('utf-8')
    except Exception: return text

def is_html(content_type: str, body: str) -> bool:
    return "text/html" in content_type or body.lstrip().startswith(("<html", "<!DOCTYPE", "<!doctype"))

def extract_keys(text: str) -> list:
    keys = KEY_PATTERN.findall(text)
    if keys: return keys
    return []

# ==================== ЭМУЛЯЦИЯ HAPP ====================

def fetch_and_decode_configs(url: str, hwid: str = None) -> str:
    actual_hwid = hwid if hwid else str(random.randint(100000000000000000, 999999999999999999))
    
    HAPP_HEADERS = {
        "User-Agent": f"Happ/3.26.3/Android/{actual_hwid}",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }

    def fetch_with_headers(u, headers=None):
        session = requests.Session()
        session.headers.update(headers or HAPP_HEADERS)
        # ВАЖНО: verify=False отключает проверку SSL. Решает 90% мгновенных падений.
        resp = session.get(u, timeout=10, stream=True, verify=False)
        if resp.status_code >= 400: resp.raise_for_status()
        
        if resp.headers.get('Content-Encoding') == 'gzip':
            try:
                gz = gzip.GzipFile(fileobj=io.BytesIO(resp.content))
                return resp, gz.read().decode('utf-8')
            except: pass
        return resp, resp.text

    print(f"[HTTP] Скачивание конфигов: {url} (HWID: {actual_hwid})", flush=True)

    try:
        resp, body = fetch_with_headers(url)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        print(f"[HTTP-ОШИБКА] Целевой сервер недоступен: {e}", flush=True)
        return "СЕТЕВАЯ_ОШИБКА"
    except Exception as e:
        print(f"[HTTP-ОШИБКА] Запрос отклонен: {e}", flush=True)
        body = ""

    if 'resp' in locals():
        ct = resp.headers.get('Content-Type', '')
        if not is_html(ct, body) and body: return try_decode_base64(body)
        if is_html(ct, body):
            match = re.search(r'(https?://[^\s"\'<>]+\.(?:txt|json|xml|conf|cfg|sub))', body, re.IGNORECASE)
            if match:
                try:
                    resp2, body2 = fetch_with_headers(match.group(1))
                    if not is_html(resp2.headers.get('Content-Type', ''), body2): return try_decode_base64(body2)
                except: pass

    base = url.rstrip("/")
    candidates = [f"{base}/sub", f"{base}?format=clash"]
    
    for alt_url in candidates:
        try:
            resp, body = fetch_with_headers(alt_url)
            ct = resp.headers.get('Content-Type', '')
            if not is_html(ct, body) and body: return try_decode_base64(body)
        except: continue

    return body if body else "ПУСТО"

# ==================== HTTP-СЕРВЕР (Динамический прокси) ====================

app = Flask(__name__)
subscription_cache = {}
CACHE_TTL = 300 

def generate_error_node(message: str) -> str:
    """Создает фейковый VPN-узел для отображения ошибки внутри VPN клиента"""
    safe_msg = urllib.parse.quote(message)
    fake_sub = f"vless://00000000-0000-0000-0000-000000000000@127.0.0.1:443?type=tcp&security=none#{safe_msg}"
    return base64.b64encode(fake_sub.encode('utf-8')).decode('utf-8')

@app.route('/')
def health_check(): return 'OK'

@app.route('/<code>')
def resolve_link(code):
    print(f"\n[GET] Запрошена ссылка: /{code}", flush=True)
    row = get_link(code)
    
    if not row:
        print(f"[404] Код {code} НЕ НАЙДЕН в базе данных.", flush=True)
        # Отдаем клиенту фейковый ключ с ошибкой, чтобы он не ушел в "Тайм-аут"
        return Response(generate_error_node("ОШИБКА: Подписка удалена или не существует"), mimetype='text/plain')

    link_type, content, name, hwid = row

    if link_type == 'url':
        if code in subscription_cache:
            cached_time, cached_data = subscription_cache[code]
            if time.time() - cached_time < CACHE_TTL:
                print(f"[200] Отдаем {code} из КЭША.", flush=True)
                return Response(cached_data, mimetype='text/plain')

        try:
            print(f"[INFO] Скачиваем оригинал для {code}...", flush=True)
            configs_text = fetch_and_decode_configs(content, hwid)
            
            if configs_text == "СЕТЕВАЯ_ОШИБКА":
                print(f"[502] Оригинальный сервер недоступен.", flush=True)
                return Response(generate_error_node("ОШИБКА: Исходный сервер заблокирован или недоступен"), mimetype='text/plain')

            keys = extract_keys(configs_text)
            
            if keys:
                print(f"[200] Успешно извлечено ключей: {len(keys)}", flush=True)
                joined_keys = "\n".join(keys)
                encoded_sub = base64.b64encode(joined_keys.encode('utf-8')).decode('utf-8')
                subscription_cache[code] = (time.time(), encoded_sub)
                return Response(encoded_sub, mimetype='text/plain')
            else:
                print(f"[502] Сервер ответил, но VPN-ключей не найдено.", flush=True)
                return Response(generate_error_node("ОШИБКА: В исходной ссылке нет рабочих VPN ключей"), mimetype='text/plain')
        except Exception as e:
            print(f"[500] ОШИБКА проксирования {code}: {e}", flush=True)
            return Response(generate_error_node("ОШИБКА: Внутренняя ошибка прокси-бота"), mimetype='text/plain')
    else:
        print(f"[200] Отдаем статические ключи для {code}", flush=True)
        encoded_sub = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        return Response(encoded_sub, mimetype='text/plain')

# ==================== КОМАНДЫ БОТА ====================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    welcome_text = (
        "Здравствуйте.\n\n"
        "📌 <b>Бот умеет:</b>\n"
        "• Автоматически расшифровывать ссылки <b>happ://crypt</b>.\n"
        "• Извлекать VPN-ключи по <b>http/https</b> ссылкам (работает как настоящий клиент Happ).\n"
        "• /shorten — создать динамическую прокси-подписку (HWID генерируется скрытно).\n"
        "• /addkeys — сохранить свои ключи.\n"
        "• /profile — управлять сохранёнными подписками."
    )
    safe_reply_to(message, with_footer(welcome_text), parse_mode="HTML")

# ==================== ПРОФИЛЬ ====================

def build_profile_menu(rows: list) -> telebot.types.InlineKeyboardMarkup:
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for row in rows:
        code, link_type, content, created_at, name = row
        icon = "🔄" if link_type == 'url' else "🔑"
        display_name = name if name else code
        markup.add(telebot.types.InlineKeyboardButton(text=f"{icon} {display_name}", callback_data=f"view:{code}"))
    return markup

@bot.message_handler(commands=['profile'])
def cmd_profile(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    rows = get_links_by_owner(message.from_user.id)
    if not rows:
        safe_reply_to(message, with_footer("У вас пока нет сохранённых ссылок."))
        return
    markup = build_profile_menu(rows)
    safe_reply_to(message, with_footer(f"Ваши ссылки ({len(rows)}). Нажмите на любую для просмотра деталей:"), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view:"))
def handle_view_link(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)
    if not row or row[2] != call.from_user.id:
        bot.answer_callback_query(call.id, "Ссылка не найдена или не ваша.", show_alert=True)
        return

    link_type, content, owner_id, name, hwid = row
    short_url = build_short_url(code)
    type_label = "🔄 Динамическая подписка" if link_type == 'url' else "🔑 Статичные ключи"
    preview = content if len(content) <= 200 else content[:197] + "..."
    hwid_text = f"\n🆔 Скрытый HWID: {hwid}" if link_type == 'url' and hwid else ""

    text = f"<b>{type_label}</b>\n\n📌 Название: {name or '—'}{hwid_text}\n🔗 {short_url}\n\n📄 <b>Источник:</b>\n{preview}"

    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✏️ Название", callback_data=f"edit_name:{code}"),
        telebot.types.InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{code}")
    )
    markup.add(telebot.types.InlineKeyboardButton("‹ Назад к списку", callback_data="profile_back"))

    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(with_footer(text), call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    except: safe_send_message(call.message.chat.id, with_footer(text), reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "profile_back")
def handle_profile_back(call):
    rows = get_links_by_owner(call.from_user.id)
    bot.answer_callback_query(call.id)
    if not rows:
        try: bot.edit_message_text(with_footer("У вас больше нет сохранённых ссылок."), call.message.chat.id, call.message.message_id)
        except: pass
        return
    markup = build_profile_menu(rows)
    try: bot.edit_message_text(with_footer(f"Ваши ссылки ({len(rows)}):"), call.message.chat.id, call.message.message_id, reply_markup=markup)
    except: pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("del:"))
def handle_delete_link(call):
    code = call.data.split(":", 1)[1]
    if delete_link(code, call.from_user.id):
        bot.answer_callback_query(call.id, "Удалено ✅")
        handle_profile_back(call)
    else: bot.answer_callback_query(call.id, "Ошибка удаления.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_name:"))
def handle_edit_name(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)
    if not row or row[2] != call.from_user.id:
        bot.answer_callback_query(call.id, "Ссылка не найдена.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Введите новое название:")
    msg = safe_send_message(call.message.chat.id, with_footer(f"Для {build_short_url(code)} введите новое название:"))
    bot.register_next_step_handler(msg, process_edit_name, code, call.from_user.id)

def process_edit_name(message, code, owner_id):
    if message.from_user.id != owner_id: return
    name = (message.text or "").strip()
    if not name:
        safe_reply_to(message, with_footer("Название не может быть пустым."))
        return
    if update_link_name(code, owner_id, name): safe_reply_to(message, with_footer("Название обновлено!"))
    else: safe_reply_to(message, with_footer("Ошибка обновления."))

# ==================== /shorten (БЕЗ ВОПРОСА О HWID) ====================

user_data = {}
user_data_lock = threading.Lock()

@bot.message_handler(commands=['shorten'])
def cmd_shorten(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        url = parts[1].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            safe_reply_to(message, with_footer("Это не похоже на ссылку. Используйте /shorten <URL>."))
            return
        with user_data_lock: user_data[message.from_user.id] = {'url': url}
        msg = safe_reply_to(message, with_footer("Введите название (будет отображаться в профиле):"))
        bot.register_next_step_handler(msg, process_name)
        return
    msg = safe_reply_to(message, with_footer("Пришлите ссылку (http:// или https://), которую нужно сократить."))
    bot.register_next_step_handler(msg, process_url)

def process_url(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        safe_reply_to(message, with_footer("Это не похоже на ссылку. Попробуйте ещё раз."))
        return
    with user_data_lock: user_data[message.from_user.id] = {'url': url}
    msg = safe_reply_to(message, with_footer("Введите название (будет отображаться в профиле):"))
    bot.register_next_step_handler(msg, process_name)

def process_name(message):
    user_id = message.from_user.id
    with user_data_lock:
        data = user_data.get(user_id)
        if not data:
            safe_reply_to(message, with_footer("Сессия истекла. Начните заново через /shorten."))
            return
        data['name'] = (message.text or "").strip() or "Без названия"

    # СКРЫТЫЙ СБОР HWID (Пользователь об этом не знает)
    auto_hwid = str(random.randint(100000000000000000, 999999999999999999))
    code = save_link('url', data['url'], user_id, data['name'], auto_hwid)
    
    response_text = f"✅ <b>Динамическая подписка создана!</b>\n\n📌 Название: {data['name']}\n🔗 {build_short_url(code)}"
    safe_reply_to(message, with_footer(response_text), parse_mode="HTML")

# ==================== /addkeys ====================

@bot.message_handler(commands=['addkeys'])
def cmd_addkeys(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        message.text = parts[1].strip()
        process_addkeys(message)
        return
    msg = safe_reply_to(message, with_footer("Пришлите ключи, которые нужно сохранить."))
    bot.register_next_step_handler(msg, process_addkeys)

def process_addkeys(message):
    try:
        if not is_subscribed(message.from_user.id):
            send_subscribe_prompt(message.chat.id)
            return
        keys = extract_keys((message.text or "").strip())
        if not keys:
            safe_reply_to(message, with_footer("Не нашёл ни одного ключа."))
            return
        code = save_link('keys', "\n".join(keys), message.from_user.id, name=None)
        safe_reply_to(message, with_footer(f"✅ Ключи сохранены!\n\nСсылка:\n{build_short_url(code)}"))
    except Exception as e:
        safe_reply_to(message, with_footer(f"Ошибка: {e}"))

# ==================== ОБРАБОТЧИК ТЕКСТА ====================

def send_keys(chat_id: int, keys: list):
    file_name = next_file_name()
    try:
        with open(file_name, 'w', encoding='utf-8') as f: f.write("\n".join(keys))
        with open(file_name, 'rb') as f:
            safe_send_document(chat_id, f, visible_file_name=file_name, caption=with_footer(f"Найдено ключей: {len(keys)}."))
    finally:
        if os.path.exists(file_name): os.remove(file_name)

@bot.message_handler(content_types=['text'])
def handle_text(message):
    try:
        if not is_subscribed(message.from_user.id):
            send_subscribe_prompt(message.chat.id)
            return
        text = message.text.strip()
        if text.startswith("happ://crypt"):
            decrypted = decrypt_happ_link(text)
            safe_reply_to(message, with_footer(f"Расшифровано:\n\n{decrypted}" if decrypted else "Не удалось расшифровать."))
        elif text.startswith("http://") or text.startswith("https://"):
            safe_reply_to(message, with_footer("Загружаю подписку как клиент Happ..."))
            configs = fetch_and_decode_configs(text)
            if configs == "СЕТЕВАЯ_ОШИБКА":
                safe_reply_to(message, with_footer("Ошибка сети: источник заблокирован или недоступен."))
                return
            keys = extract_keys(configs)
            if keys: send_keys(message.chat.id, keys)
            else:
                file_name = next_file_name()
                try:
                    with open(file_name, 'w', encoding='utf-8') as f: f.write(configs)
                    with open(file_name, 'rb') as f: safe_send_document(message.chat.id, f, visible_file_name=file_name, caption=with_footer("Ключей нет. Сырые данные:"))
                finally:
                    if os.path.exists(file_name): os.remove(file_name)
        else: safe_reply_to(message, with_footer("Используйте /shorten или /addkeys."))
    except Exception as e:
        print(f"[ERROR] handle_text: {e}", flush=True)

# ==================== ЗАПУСК ====================

def run_bot_polling():
    print("[START] Запуск polling Telegram бота...", flush=True)
    bot.infinity_polling(timeout=60, logger_level=None)

if __name__ == '__main__':
    import traceback
    try:
        print("[START] Запуск...", flush=True)
        conn = get_db()
        conn.close()
        
        try: bot.delete_webhook(drop_pending_updates=True, timeout=5)
        except: pass

        threading.Thread(target=run_bot_polling, daemon=True).start()
        
        print(f"[START] HTTP сервер запущен на порту {PORT}", flush=True)
        app.run(host='0.0.0.0', port=PORT, threaded=True, use_reloader=False)
    except Exception:
        traceback.print_exc()
