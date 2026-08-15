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

from flask import Flask, abort, Response, request

# ==================== КОНФИГУРАЦИЯ ====================

TOKEN = os.getenv('TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')
FOOTER_TAG = os.getenv('FOOTER_TAG', '')
FILE_PREFIX = os.getenv('FILE_PREFIX', 'keys')
BASE_URL = os.getenv('BASE_URL')
PORT = int(os.getenv('PORT', '3000'))
DATA_DIR = os.getenv('DATA_DIR', '/app/data')

if not TOKEN:
    raise ValueError("TOKEN не задана")
if not CHANNEL_USERNAME:
    raise ValueError("CHANNEL_USERNAME не задана")
if not BASE_URL:
    raise ValueError("BASE_URL не задана")

BASE_URL = BASE_URL.rstrip('/')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'links.db')

bot = telebot.TeleBot(TOKEN)

# Оптимизация таймаутов Telegram API
telebot.apihelper.READ_TIMEOUT = 20
telebot.apihelper.CONNECT_TIMEOUT = 15

# ==================== БЕЗОПАСНАЯ ОТПРАВКА ====================

def safe_send_message(chat_id, text, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except requests.exceptions.ReadTimeout:
            time.sleep(delay)
        except Exception as e:
            print(f"[Ошибка send_message]: {e}")
            if attempt == retries:
                raise

def safe_send_document(chat_id, document, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return bot.send_document(chat_id, document, **kwargs)
        except requests.exceptions.ReadTimeout:
            time.sleep(delay)
            if hasattr(document, 'seek'):
                try:
                    document.seek(0)
                except Exception:
                    pass
        except Exception as e:
            print(f"[Ошибка send_document]: {e}")
            if attempt == retries:
                raise

def safe_reply_to(message, text, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return bot.reply_to(message, text, **kwargs)
        except requests.exceptions.ReadTimeout:
            time.sleep(delay)
        except Exception as e:
            print(f"[Ошибка reply_to]: {e}")
            if attempt == retries:
                raise

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
    try:
        conn.execute('ALTER TABLE links ADD COLUMN hwid TEXT DEFAULT NULL')
    except sqlite3.OperationalError:
        pass
    return conn

def generate_code(length: int = 7) -> str:
    alphabet = string.ascii_letters + string.digits
    with db_lock:
        conn = get_db()
        try:
            while True:
                code = ''.join(random.choices(alphabet, k=length))
                exists = conn.execute('SELECT 1 FROM links WHERE code = ?', (code,)).fetchone()
                if not exists:
                    return code
        finally:
            conn.close()

def save_link(link_type: str, content: str, owner_id: int, name: str = None, hwid: str = None) -> str:
    code = generate_code()
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                'INSERT INTO links (code, type, content, owner_id, name, hwid) VALUES (?, ?, ?, ?, ?, ?)',
                (code, link_type, content, owner_id, name, hwid)
            )
            conn.commit()
        finally:
            conn.close()
    return code

def get_link_full(code: str):
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute('SELECT type, content, owner_id, name, hwid FROM links WHERE code = ?', (code,)).fetchone()
            return row
        finally:
            conn.close()

def get_links_by_owner(owner_id: int, limit: int = 20):
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                'SELECT code, type, content, created_at, name, hwid FROM links WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?',
                (owner_id, limit)
            ).fetchall()
            return rows
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

def update_link_name(code: str, owner_id: int, name: str) -> bool:
    with db_lock:
        conn = get_db()
        try:
            cur = conn.execute('UPDATE links SET name = ? WHERE code = ? AND owner_id = ?', (name, code, owner_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

def build_short_url(code: str) -> str:
    return f"{BASE_URL}/{code}"

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

file_counter = 0

def next_file_name() -> str:
    global file_counter
    file_counter += 1
    return f"{FILE_PREFIX}{file_counter}.txt"

def with_footer(text: str) -> str:
    if FOOTER_TAG:
        return f"{text}\n\n{FOOTER_TAG}"
    return text

def is_subscribed(user_id: int) -> bool:
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ('member', 'administrator', 'creator')
    except Exception as e:
        print(f"[ОШИБКА проверки подписки]: {e}")
        return False

def send_subscribe_prompt(chat_id: int):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        text="Подписаться на канал",
        url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
    ))
    markup.add(telebot.types.InlineKeyboardButton(
        text="Я подписался ✅",
        callback_data="check_sub"
    ))
    safe_send_message(chat_id, with_footer(f"Для использования бота необходимо подписаться на канал {CHANNEL_USERNAME}."), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def handle_check_sub(call):
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Подписка подтверждена ✅")
        safe_send_message(call.message.chat.id, with_footer("Отлично! Теперь вы можете пользоваться ботом."))
    else:
        bot.answer_callback_query(call.id, "Подписка не найдена.", show_alert=True)

KEY_PATTERN = re.compile(
    r'(?:vless|vmess|trojan|ss|ssr|hysteria2?|hy2|tuic)://[^\s<>"]+',
    re.IGNORECASE
)

def try_decode_base64(text: str) -> str:
    try:
        padded = text + '=' * (-len(text) % 4)
        return base64.b64decode(padded).decode('utf-8')
    except Exception:
        return text

def is_html(content_type: str, body: str) -> bool:
    return "text/html" in content_type or body.lstrip().startswith(("<html", "<!DOCTYPE", "<!doctype"))

def extract_keys(text: str) -> list:
    # Здесь можно добавить конвертер JSON, если нужен, 
    # но регулярки достаточно для чистого Base64/URI.
    keys = KEY_PATTERN.findall(text)
    return keys

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
        if headers is None: headers = HAPP_HEADERS.copy()
        session = requests.Session()
        session.headers.update(headers)
        # Таймаут снижен до 5 секунд, чтобы клиент не зависал в бесконечном ожидании
        resp = session.get(u, timeout=5, stream=True)
        resp.raise_for_status()
        
        if resp.headers.get('Content-Encoding') == 'gzip':
            try:
                gz = gzip.GzipFile(fileobj=io.BytesIO(resp.content))
                content = gz.read().decode('utf-8')
            except:
                content = resp.text
        else:
            content = resp.text
        return resp, content

    print(f"[HTTP] Пытаемся скачать конфиги по URL: {url} (HWID: {actual_hwid})")

    # FAIL-FAST: Если первый же запрос падает по таймауту или недоступности сети — прерываем всё.
    try:
        resp, body = fetch_with_headers(url)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        print(f"[HTTP-ОШИБКА] Целевой сервер недоступен (тайм-аут или нет сети): {e}")
        return "СЕТЕВАЯ_ОШИБКА"
    except Exception as e:
        print(f"[HTTP-ОШИБКА] Первый запрос не удался: {e}")
        body = ""

    if 'resp' in locals():
        ct = resp.headers.get('Content-Type', '')
        if not is_html(ct, body) and body:
            return try_decode_base64(body)
            
        if is_html(ct, body):
            match = re.search(r'(https?://[^\s"\'<>]+\.(?:txt|json|xml|conf|cfg|sub))', body, re.IGNORECASE)
            if match:
                try:
                    resp2, body2 = fetch_with_headers(match.group(1))
                    if not is_html(resp2.headers.get('Content-Type', ''), body2):
                        return try_decode_base64(body2)
                except Exception:
                    pass

    # Альтернативные пути (выполняются только если сервер отвечает, но отдает не то)
    base = url.rstrip("/")
    candidates = [
        f"{base}/sub",
        f"{base}?format=clash"
    ]
    
    for alt_url in candidates:
        try:
            resp, body = fetch_with_headers(alt_url)
            ct = resp.headers.get('Content-Type', '')
            if not is_html(ct, body) and body:
                return try_decode_base64(body)
        except Exception:
            continue

    return body if body else "ПУСТО"

# ==================== HTTP-СЕРВЕР (Динамический прокси) ====================

app = Flask(__name__)
subscription_cache = {}
CACHE_TTL = 300 

@app.route('/')
def health_check():
    return 'OK'

@app.route('/<code>')
def resolve_link(code):
    print(f"\n[GET] Клиент запросил ссылку: /{code}")
    row = get_link_full(code)
    
    if not row:
        print(f"[404] Код {code} НЕ НАЙДЕН в базе данных. (Возможно, база стерлась при перезапуске).")
        return Response("Error 404: Subscription not found. Please create a new link in the bot.", status=404, mimetype='text/plain')

    link_type, content, owner_id, name, hwid = row

    if link_type == 'url':
        if code in subscription_cache:
            cached_time, cached_data = subscription_cache[code]
            if time.time() - cached_time < CACHE_TTL:
                print(f"[200] Отдаем {code} из КЭША.")
                return Response(cached_data, mimetype='text/plain')

        try:
            print(f"[INFO] Скачиваем оригинал для {code}...")
            configs_text = fetch_and_decode_configs(content, hwid)
            
            if configs_text == "СЕТЕВАЯ_ОШИБКА":
                print(f"[502] Оригинальный сервер недоступен.")
                return Response("Error 502: Original server is down or unreachable (Timeout).", status=502, mimetype='text/plain')

            keys = extract_keys(configs_text)
            
            if keys:
                print(f"[200] Успешно извлечено ключей: {len(keys)}")
                joined_keys = "\n".join(keys)
                encoded_sub = base64.b64encode(joined_keys.encode('utf-8')).decode('utf-8')
                
                subscription_cache[code] = (time.time(), encoded_sub)
                return Response(encoded_sub, mimetype='text/plain')
            else:
                print(f"[502] Сервер ответил, но VPN-ключей не найдено.")
                return Response("Error 502: No valid VPN keys found in the original subscription.", status=502, mimetype='text/plain')
        except Exception as e:
            print(f"[500] ОШИБКА проксирования {code}: {e}")
            return Response("Error 500: Internal server error.", status=500, mimetype='text/plain')
    else:
        print(f"[200] Отдаем статические ключи для {code}")
        encoded_sub = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        return Response(encoded_sub, mimetype='text/plain')

# ==================== КОМАНДЫ БОТА ====================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    print(f"[LOG] Получена команда /start от {message.from_user.id}")
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    welcome_text = (
        "Здравствуйте.\n\n"
        "📌 <b>Бот работает:</b>\n"
        "• /shorten — создать динамическую прокси-подписку (с возможностью задать HWID).\n"
        "• /addkeys — сохранить свои ключи.\n"
        "• /profile — управлять сохранёнными подписками."
    )
    safe_reply_to(message, with_footer(welcome_text), parse_mode="HTML")

user_data_lock = threading.Lock()
user_data = {}

@bot.message_handler(commands=['shorten'])
def cmd_shorten(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    msg = safe_reply_to(message, with_footer("Пришлите ссылку (http/https), которую нужно сделать прокси-подпиской."))
    bot.register_next_step_handler(msg, process_url)

def process_url(message):
    url = (message.text or "").strip()
    if not url.startswith("http"):
        safe_reply_to(message, with_footer("Это не похоже на ссылку."))
        return
    with user_data_lock:
        user_data[message.from_user.id] = {'url': url}
    msg = safe_reply_to(message, with_footer("Введите название для этой подписки:"))
    bot.register_next_step_handler(msg, process_name)

def process_name(message):
    user_id = message.from_user.id
    with user_data_lock:
        if user_id not in user_data: return
        user_data[user_id]['name'] = (message.text or "").strip() or "Без названия"
    msg = safe_reply_to(message, with_footer("Введите HWID (или 0 для случайного):"))
    bot.register_next_step_handler(msg, process_hwid)

def process_hwid(message):
    user_id = message.from_user.id
    with user_data_lock:
        data = user_data.get(user_id)
        if not data: return
        hwid_input = (message.text or "").strip()
        data['hwid'] = str(random.randint(100000000000000000, 999999999999999999)) if hwid_input == '0' else hwid_input

    code = save_link('url', data['url'], user_id, data['name'], data['hwid'])
    short_url = build_short_url(code)
    
    print(f"[DB] Создана новая ссылка: {code} для URL {data['url']}")
    
    response_text = f"✅ <b>Создано!</b>\n\n📌 Название: {data['name']}\n🔗 Ссылка: {short_url}\n\n<i>Скопируйте эту ссылку и добавьте её в VPN-клиент.</i>"
    safe_reply_to(message, with_footer(response_text), parse_mode="HTML")

@bot.message_handler(commands=['profile'])
def cmd_profile(message):
    rows = get_links_by_owner(message.from_user.id)
    if not rows:
        safe_reply_to(message, with_footer("У вас пока нет сохранённых ссылок/подписок."))
        return
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for row in rows:
        markup.add(telebot.types.InlineKeyboardButton(text=f"🔄 {row[4] or row[0]}", callback_data=f"view:{row[0]}"))
    safe_reply_to(message, with_footer(f"Ваши подписки:"), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view:"))
def handle_view_link(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)
    if not row: return
    
    text = f"<b>Подписка</b>\n\n📌 Название: {row[3]}\n🔗 {build_short_url(code)}"
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML")

# ==================== ЗАПУСК ====================

def run_bot_polling():
    print("[START] Запуск polling Telegram бота...")
    bot.infinity_polling(timeout=60, logger_level=None)

if __name__ == '__main__':
    import traceback
    try:
        print("[START] Инициализация...")
        conn = get_db()
        conn.close()
        print("[START] База данных готова")
        
        try:
            bot.delete_webhook(drop_pending_updates=True, timeout=5)
        except Exception as e:
            print(f"[START] Ошибка вебхука (игнорируется): {e}")

        bot_thread = threading.Thread(target=run_bot_polling, daemon=True)
        bot_thread.start()
        
        print(f"[START] HTTP сервер запускается на порту {PORT}")
        app.run(host='0.0.0.0', port=PORT, threaded=True, use_reloader=False)

    except Exception:
        traceback.print_exc()
        input("\nНажмите Enter, чтобы закрыть окно...")
