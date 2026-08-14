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
    raise ValueError("TOKEN не задан")
if not CHANNEL_USERNAME:
    raise ValueError("CHANNEL_USERNAME не задан")
if not BASE_URL:
    raise ValueError("BASE_URL не задан")

BASE_URL = BASE_URL.rstrip('/')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'links.db')

bot = telebot.TeleBot(TOKEN)
telebot.apihelper.READ_TIMEOUT = 60
telebot.apihelper.CONNECT_TIMEOUT = 60

# ---------- Функции отправки с ретраями ----------

def safe_send_message(chat_id, text, retries=3, delay=2, **kwargs):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            last_error = e
            print(f"[Таймаут send_message, попытка {attempt}/{retries}]: {e}")
            time.sleep(delay)
        except Exception as e:
            last_error = e
            print(f"[Ошибка send_message]: {e}")
            break
    if last_error:
        raise last_error

def safe_send_document(chat_id, document, retries=3, delay=2, **kwargs):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return bot.send_document(chat_id, document, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            last_error = e
            print(f"[Таймаут send_document, попытка {attempt}/{retries}]: {e}")
            time.sleep(delay)
            if hasattr(document, 'seek'):
                try:
                    document.seek(0)
                except Exception:
                    pass
        except Exception as e:
            last_error = e
            print(f"[Ошибка send_document]: {e}")
            break
    if last_error:
        raise last_error

def safe_reply_to(message, text, retries=3, delay=2, **kwargs):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return bot.reply_to(message, text, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            last_error = e
            print(f"[Таймаут reply_to, попытка {attempt}/{retries}]: {e}")
            time.sleep(delay)
        except Exception as e:
            last_error = e
            print(f"[Ошибка reply_to]: {e}")
            break
    if last_error:
        raise last_error

# ---------- БАЗА ДАННЫХ С МИГРАЦИЕЙ ----------

db_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS links (
            code TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            owner_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor = conn.execute("PRAGMA table_info(links)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'hwid' not in columns:
        print("[MIGRATION] Добавляем колонку hwid...")
        conn.execute('ALTER TABLE links ADD COLUMN hwid TEXT')
        conn.commit()
        print("[MIGRATION] Колонка hwid добавлена.")
    return conn

def generate_code(length=7):
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

def save_link(link_type, content, owner_id, hwid=None):
    code = generate_code()
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                'INSERT INTO links (code, type, content, owner_id, hwid) VALUES (?, ?, ?, ?, ?)',
                (code, link_type, content, owner_id, hwid)
            )
            conn.commit()
        finally:
            conn.close()
    return code

def get_link_full(code):
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute('SELECT type, content, owner_id, hwid FROM links WHERE code = ?', (code,)).fetchone()
            return row
        finally:
            conn.close()

def get_links_by_owner(owner_id, limit=20):
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                'SELECT code, type, content, created_at FROM links WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?',
                (owner_id, limit)
            ).fetchall()
            return rows
        finally:
            conn.close()

def delete_link(code, owner_id):
    with db_lock:
        conn = get_db()
        try:
            cur = conn.execute('DELETE FROM links WHERE code = ? AND owner_id = ?', (code, owner_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

def update_link_content(code, owner_id, new_content):
    with db_lock:
        conn = get_db()
        try:
            cur = conn.execute(
                'UPDATE links SET content = ? WHERE code = ? AND owner_id = ?',
                (new_content, code, owner_id)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

def build_short_url(code):
    return f"{BASE_URL}/{code}"

# ---------- HTTP-СЕРВЕР (ИСПРАВЛЕННАЯ ЛОГИКА HWID) ----------

app = Flask(__name__)

@app.route('/')
def health_check():
    return 'OK'

@app.route('/<code>')
def resolve_link(code):
    row = get_link_full(code)
    if not row:
        abort(404)
    link_type, content, owner_id, hwid = row
    print(f"[HWID CHECK] code={code}, hwid_in_db={hwid}")

    if hwid:
        request_hwid = request.args.get('hwid') or request.args.get('payload')
        print(f"[HWID CHECK] request_hwid={request_hwid}")
        if not request_hwid or request_hwid != hwid:
            print(f"[HWID CHECK] ДОСТУП ЗАПРЕЩЁН (HWID не совпадает)")
            abort(403)
        else:
            print(f"[HWID CHECK] HWID совпадает, доступ разрешён")

    if link_type == 'url':
        try:
            resp = requests.get(content, timeout=15)
            resp.raise_for_status()
            return Response(resp.content, mimetype=resp.headers.get('Content-Type', 'text/plain'))
        except Exception as e:
            print(f"[Ошибка проксирования {code}]: {e}")
            abort(502)
    else:
        return Response(content, mimetype='text/plain')

def run_http_server():
    app.run(host='0.0.0.0', port=PORT)

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БОТА ----------

file_counter = 0

def next_file_name():
    global file_counter
    file_counter += 1
    return f"{FILE_PREFIX}{file_counter}.txt"

def with_footer(text):
    if FOOTER_TAG:
        return f"{text}\n\n{FOOTER_TAG}"
    return text

def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ('member', 'administrator', 'creator')
    except Exception as e:
        print(f"[Ошибка проверки подписки]: {e}")
        return False

def send_subscribe_prompt(chat_id):
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
        safe_send_message(call.message.chat.id, with_footer("Отлично! Теперь можешь пользоваться ботом."))
    else:
        bot.answer_callback_query(call.id, "Подписка не найдена.", show_alert=True)

# ---------- ОСТАЛЬНЫЕ ВАШИ ФУНКЦИИ (ПАРСИНГ, КОНВЕРТЕРЫ И Т.Д.) ----------
# ВСТАВЬТЕ СЮДА ВЕСЬ ВАШ КОД, КОТОРЫЙ БЫЛ РАНЕЕ:
# KEY_PATTERN, decrypt_happ_link, try_decode_base64, is_html,
# _vless_outbound_to_uri, _hysteria_outbound_to_uri, _trojan_outbound_to_uri,
# _NON_PROXY_PROTOCOLS, _OUTBOUND_CONVERTERS, convert_xray_json_to_links,
# fetch_and_decode_configs, extract_keys, send_keys.
# Я их не копирую, чтобы не раздувать ответ, но они ОБЯЗАТЕЛЬНО должны быть.

# ---------- ОБРАБОТЧИКИ КОМАНД ----------

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    welcome_text = (
        "Здравствуйте.\n\n"
        "• Отправьте ссылку happ://crypt...\n"
        "• Отправьте обычную ссылку (http://... или https://...)\n"
        "• /addkeys — сохранить ключи\n"
        "• /shorten — сократить ссылку\n"
        "• /hwid — создать ссылку с защитой\n"
        "• /hwid_auto — создать ссылку с автогенерацией HWID\n"
        "• /profile — управление ссылками"
    )
    safe_reply_to(message, with_footer(welcome_text))

# ----- HWID -----
@bot.message_handler(commands=['hwid'])
def cmd_hwid(message):
    try:
        if not is_subscribed(message.from_user.id):
            send_subscribe_prompt(message.chat.id)
            return
        parts = message.text.strip().split(maxsplit=2)
        if len(parts) < 3:
            safe_reply_to(message, with_footer("Использование: /hwid <HWID> <содержимое>"))
            return
        hwid = parts[1].strip()
        content = parts[2].strip()
        if not hwid:
            safe_reply_to(message, with_footer("HWID не может быть пустым."))
            return
        code = save_link('hwid', content, message.from_user.id, hwid)
        short_url = build_short_url(code)
        safe_reply_to(message, with_footer(f"✅ HWID-ссылка создана!\nHWID: {hwid}\nСсылка: {short_url}?hwid={hwid}"))
    except Exception as e:
        print(f"[ERROR cmd_hwid] {e}")
        safe_reply_to(message, with_footer(f"Ошибка: {e}"))

@bot.message_handler(commands=['hwid_auto'])
def cmd_hwid_auto(message):
    try:
        if not is_subscribed(message.from_user.id):
            send_subscribe_prompt(message.chat.id)
            return
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) < 2:
            safe_reply_to(message, with_footer("Использование: /hwid_auto <содержимое>"))
            return
        content = parts[1].strip()
        hwid = f"tg_{message.from_user.id}_{random.randint(1000, 9999)}"
        code = save_link('hwid', content, message.from_user.id, hwid)
        short_url = build_short_url(code)
        safe_reply_to(message, with_footer(f"✅ HWID-ссылка создана!\nHWID: {hwid}\nСсылка: {short_url}?hwid={hwid}"))
    except Exception as e:
        print(f"[ERROR cmd_hwid_auto] {e}")
        safe_reply_to(message, with_footer(f"Ошибка: {e}"))

# ----- Остальные команды (addkeys, shorten, profile) -----
# ВАШИ СУЩЕСТВУЮЩИЕ ОБРАБОТЧИКИ (без parse_mode, либо с parse_mode="HTML")
# Я их не дублирую, оставляю как у вас, только проверьте, что нет Markdown.
# Они у вас уже были, и они работают.

# ---------- ЗАПУСК ----------

if __name__ == '__main__':
    import traceback
    try:
        http_thread = threading.Thread(target=run_http_server, daemon=True)
        http_thread.start()
        bot.remove_webhook()
        bot.polling(none_stop=True)
    except Exception:
        traceback.print_exc()
    finally:
        input("\nНажмите Enter, чтобы закрыть окно...")
