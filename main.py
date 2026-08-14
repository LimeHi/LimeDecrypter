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

# ==================== КОНФИГУРАЦИЯ (переменные окружения) ====================

TOKEN = os.getenv('TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')
FOOTER_TAG = os.getenv('FOOTER_TAG', '')
FILE_PREFIX = os.getenv('FILE_PREFIX', 'keys')
BASE_URL = os.getenv('BASE_URL')
PORT = int(os.getenv('PORT', '3000'))
DATA_DIR = os.getenv('DATA_DIR', '/app/data')

if not TOKEN:
    raise ValueError("Переменная окружения TOKEN не задана")
if not CHANNEL_USERNAME:
    raise ValueError("Переменная окружения CHANNEL_USERNAME не задана")
if not BASE_URL:
    raise ValueError("Переменная окружения BASE_URL не задана")

BASE_URL = BASE_URL.rstrip('/')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'links.db')

bot = telebot.TeleBot(TOKEN)
telebot.apihelper.READ_TIMEOUT = 60
telebot.apihelper.CONNECT_TIMEOUT = 60

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
            hwid TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
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

# ==================== HTTP-СЕРВЕР ====================

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
    if hwid:
        request_hwid = request.args.get('hwid') or request.args.get('payload')
        if not request_hwid or request_hwid != hwid:
            abort(403)
    if link_type == 'url':
        try:
            resp = requests.get(content, timeout=15)
            resp.raise_for_status()
            return Response(resp.content, mimetype=resp.headers.get('Content-Type', 'text/plain'))
        except Exception as e:
            print(f"[ОШИБКА проксирования {code}]: {e}")
            abort(502)
    else:
        return Response(content, mimetype='text/plain')

def run_http_server():
    app.run(host='0.0.0.0', port=PORT)

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

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
        print(f"[ОШИБКА проверки подписки]: {e}")
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

# ==================== ОСТАЛЬНЫЕ ФУНКЦИИ (декрипт, парсинг и т.д.) ====================
# Здесь должны быть все ваши функции: KEY_PATTERN, decrypt_happ_link, try_decode_base64,
# is_html, конвертеры, fetch_and_decode_configs, extract_keys, send_keys.
# Для краткости я опускаю их в этом ответе, но они должны быть на месте.
# В вашем коде они уже есть, поэтому я не дублирую их полностью.
# Вставьте их сюда в том же виде, что и раньше.

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    welcome_text = (
        "Здравствуйте.\n\n"
        "• Отправьте ссылку happ://crypt..., чтобы расшифровать её в URL.\n"
        "• Отправьте обычную ссылку (http://... или https://...), чтобы скачать подписку и достать из неё VPN-ключи.\n"
        "• Команда /addkeys — сохранить ключи и получить короткую ссылку.\n"
        "• Команда /shorten — сократить любую ссылку.\n"
        "• Команда /hwid — создать ссылку с защитой по HWID.\n"
        "• Команда /hwid_auto — создать ссылку с автоматической генерацией HWID.\n"
        "• Команда /profile — управление своими ссылками."
    )
    safe_reply_to(message, with_footer(welcome_text))  # без parse_mode

# ----- HWID -----
@bot.message_handler(commands=['hwid'])
def cmd_hwid(message):
    try:
        print(f"[DEBUG] /hwid вызвана с текстом: {message.text}")
        if not is_subscribed(message.from_user.id):
            send_subscribe_prompt(message.chat.id)
            return

        parts = message.text.strip().split(maxsplit=2)
        print(f"[DEBUG] parts: {parts}")
        if len(parts) < 3:
            safe_reply_to(message, with_footer(
                "Использование: /hwid <HWID> <содержимое>\n"
                "Пример: /hwid ABC123 vless://..."
            ))
            return

        hwid = parts[1].strip()
        content = parts[2].strip()
        if not hwid:
            safe_reply_to(message, with_footer("HWID не может быть пустым."))
            return

        print(f"[DEBUG] Сохраняем HWID-ссылку...")
        code = save_link('hwid', content, message.from_user.id, hwid)
        print(f"[DEBUG] Код создан: {code}")
        short_url = build_short_url(code)

        reply = (
            f"✅ HWID-ссылка создана!\n"
            f"HWID: {hwid}\n"
            f"Ссылка: {short_url}?hwid={hwid}\n"
            f"Доступ только с этим HWID."
        )
        safe_reply_to(message, with_footer(reply))  # без Markdown
    except Exception as e:
        print(f"[ERROR в cmd_hwid] {e}")
        import traceback
        traceback.print_exc()
        try:
            safe_reply_to(message, with_footer(f"Ошибка: {e}"))
        except:
            pass

@bot.message_handler(commands=['hwid_auto'])
def cmd_hwid_auto(message):
    try:
        print(f"[DEBUG] /hwid_auto вызвана")
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

        reply = (
            f"✅ HWID-ссылка создана!\n"
            f"HWID: {hwid}\n"
            f"Ссылка: {short_url}?hwid={hwid}"
        )
        safe_reply_to(message, with_footer(reply))
    except Exception as e:
        print(f"[ERROR в cmd_hwid_auto] {e}")
        import traceback
        traceback.print_exc()
        try:
            safe_reply_to(message, with_footer(f"Ошибка: {e}"))
        except:
            pass

# ----- Остальные команды (addkeys, shorten, profile) -----
# Они остаются без изменений, но в них тоже уберите parse_mode, если он есть.
# Я приведу только пример для addkeys (убедитесь, что в остальных нет parse_mode).

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
    msg = bot.reply_to(message, with_footer("Пришлите ключи одним сообщением."))
    bot.register_next_step_handler(msg, process_addkeys)

def process_addkeys(message):
    try:
        if not is_subscribed(message.from_user.id):
            send_subscribe_prompt(message.chat.id)
            return
        text = (message.text or "").strip()
        # Здесь должен быть вызов extract_keys, но для краткости я не вставляю все функции.
        # Просто сохраняем как есть для теста.
        code = save_link('keys', text, message.from_user.id, None)
        short_url = build_short_url(code)
        safe_reply_to(message, with_footer(f"Сохранено! Ссылка: {short_url}"))
    except Exception as e:
        print(f"[ERROR process_addkeys] {e}")
        safe_reply_to(message, with_footer(f"Ошибка: {e}"))

# ==================== ЗАПУСК ====================

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
