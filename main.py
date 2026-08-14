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

from flask import Flask, abort, Response, request

# ==================== КОНФИГУРАЦИЯ ====================

TOKEN = os.getenv('TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')
FOOTER_TAG = os.getenv('FOOTER_TAG', '')
FILE_PREFIX = os.getenv('FILE_PREFIX', 'keys')
BASE_URL = os.getenv('BASE_URL')
PORT = int(os.getenv('PORT', '3000'))
DATA_DIR = os.getenv('DATA_DIR', '/app/data')
BYPASS_SECRET = os.getenv('BYPASS_SECRET', 'default_secret_change_me')

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

telebot.apihelper.READ_TIMEOUT = 60
telebot.apihelper.CONNECT_TIMEOUT = 60

# ==================== ВРЕМЕННОЕ ХРАНИЛИЩЕ ====================

pending_saves = {}
pending_saves_lock = threading.Lock()

def create_pending_save(content: str) -> str:
    session_id = str(uuid.uuid4())[:8]
    with pending_saves_lock:
        pending_saves[session_id] = content
        print(f"[DEBUG] Created session {session_id}, content length: {len(content)}")
    return session_id

def get_pending_save(session_id: str) -> str:
    with pending_saves_lock:
        content = pending_saves.pop(session_id, None)
        print(f"[DEBUG] Retrieved session {session_id}, found: {content is not None}")
        return content

def cleanup_pending_saves():
    while True:
        time.sleep(300)
        with pending_saves_lock:
            if len(pending_saves) > 1000:
                keys = list(pending_saves.keys())[:500]
                for k in keys:
                    pending_saves.pop(k, None)
                print(f"[DEBUG] Cleaned {len(keys)} old sessions")

cleanup_thread = threading.Thread(target=cleanup_pending_saves, daemon=True)
cleanup_thread.start()

def safe_send_message(chat_id, text, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            print(f"[Таймаут send_message, попытка {attempt}/{retries}]: {e}")
            time.sleep(delay)
        except Exception as e:
            print(f"[Ошибка send_message]: {e}")
            raise

def safe_send_document(chat_id, document, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return bot.send_document(chat_id, document, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            print(f"[Таймаут send_document, попытка {attempt}/{retries}]: {e}")
            time.sleep(delay)
            if hasattr(document, 'seek'):
                try:
                    document.seek(0)
                except Exception:
                    pass
        except Exception as e:
            print(f"[Ошибка send_document]: {e}")
            raise

def safe_reply_to(message, text, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return bot.reply_to(message, text, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            print(f"[Таймаут reply_to, попытка {attempt}/{retries}]: {e}")
            time.sleep(delay)
        except Exception as e:
            print(f"[Ошибка reply_to]: {e}")
            raise

# ==================== HWID BYPASS ====================

def generate_bypass_payload(code: str, hwid: str = None) -> str:
    timestamp = str(int(time.time()))
    hwid_part = hwid or "universal"
    
    message = f"{code}:{timestamp}:{hwid_part}"
    signature = hmac.new(
        BYPASS_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    
    payload_data = f"{code}:{timestamp}:{signature}"
    payload = base64.urlsafe_b64encode(payload_data.encode()).decode().rstrip('=')
    
    return payload

def verify_bypass_payload(payload: str, max_age: int = 3600) -> tuple:
    try:
        padding = '=' * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode()
        
        parts = decoded.split(':')
        if len(parts) != 3:
            return None, False
        
        code, timestamp, signature = parts
        
        token_age = int(time.time()) - int(timestamp)
        if token_age > max_age:
            return None, False
        
        message = f"{code}:{timestamp}:universal"
        expected_sig = hmac.new(
            BYPASS_SECRET.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()[:16]
        
        if hmac.compare_digest(signature, expected_sig):
            return code, True
        
        return None, False
        
    except Exception as e:
        print(f"[ОШИБКА verify_bypass_payload]: {e}")
        return None, False

def build_bypass_url(code: str) -> str:
    payload = generate_bypass_payload(code)
    return f"{BASE_URL}/bypass-hwid-lock-{code}?payload={payload}"

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
            hwid_bypass BOOLEAN DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
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

def save_link(link_type: str, content: str, owner_id: int, hwid_bypass: bool = False) -> str:
    code = generate_code()
    print(f"[DEBUG] Saving link: type={link_type}, owner={owner_id}, bypass={hwid_bypass}, content_len={len(content)}")
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                'INSERT INTO links (code, type, content, owner_id, hwid_bypass) VALUES (?, ?, ?, ?, ?)',
                (code, link_type, content, owner_id, 1 if hwid_bypass else 0)
            )
            conn.commit()
            print(f"[DEBUG] Link saved successfully: {code}")
        except Exception as e:
            print(f"[ERROR] Database save failed: {e}")
            raise
        finally:
            conn.close()
    return code

def get_link(code: str):
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute('SELECT type, content, hwid_bypass FROM links WHERE code = ?', (code,)).fetchone()
            return row
        finally:
            conn.close()

def get_link_full(code: str):
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute('SELECT type, content, owner_id, hwid_bypass FROM links WHERE code = ?', (code,)).fetchone()
            return row
        finally:
            conn.close()

def get_links_by_owner(owner_id: int, limit: int = 20):
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                'SELECT code, type, content, created_at, hwid_bypass FROM links WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?',
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

def update_link_content(code: str, owner_id: int, new_content: str) -> bool:
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

def build_short_url(code: str) -> str:
    return f"{BASE_URL}/{code}"

# ==================== HTTP-СЕРВЕР ====================

app = Flask(__name__)

@app.route('/')
def health_check():
    return 'OK'

@app.route('/<code>')
def resolve_link(code):
    row = get_link(code)
    if not row:
        abort(404)

    link_type, content, hwid_bypass = row

    if link_type == 'url':
        try:
            resp = requests.get(content, timeout=15)
            resp.raise_for_status()
            content_type = resp.headers.get('Content-Type', 'text/plain; charset=utf-8')
            return Response(resp.content, mimetype=content_type)
        except Exception as e:
            print(f"[ОШИБКА проксирования {code}]: {e}")
            abort(502)
    else:
        return Response(content, mimetype='text/plain')

@app.route('/bypass-hwid-lock-<code>')
def bypass_hwid_link(code):
    payload = request.args.get('payload')
    
    if not payload:
        abort(400, "Missing payload parameter")
    
    verified_code, valid = verify_bypass_payload(payload)
    
    if not valid or verified_code != code:
        abort(403, "Invalid or expired bypass token")
    
    row = get_link(code)
    if not row:
        abort(404)
    
    link_type, content, hwid_bypass = row
    
    if not hwid_bypass:
        abort(403, "HWID bypass not enabled for this link")
    
    headers = {
        'X-HWID-Bypass': 'enabled',
        'X-Bypass-Token': payload
    }
    
    if link_type == 'url':
        try:
            resp = requests.get(content, timeout=15)
            resp.raise_for_status()
            content_type = resp.headers.get('Content-Type', 'text/plain; charset=utf-8')
            return Response(resp.content, mimetype=content_type, headers=headers)
        except Exception as e:
            print(f"[ОШИБКА проксирования bypass {code}]: {e}")
            abort(502)
    else:
        return Response(content, mimetype='text/plain', headers=headers)

def run_http_server():
    app.run(host='0.0.0.0', port=PORT)

# ==================== ВСПОМОГАТЕЛЬНЫЕ ====================

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
    safe_send_message(
        chat_id,
        with_footer(f"Для использования бота необходимо подписаться на канал {CHANNEL_USERNAME}."),
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def handle_check_sub(call):
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Подписка подтверждена ✅")
        safe_send_message(call.message.chat.id, with_footer("Отлично! Теперь можешь пользоваться ботом."))
    else:
        bot.answer_callback_query(call.id, "Подписка не найдена. Подпишись и попробуй снова.", show_alert=True)

KEY_PATTERN = re.compile(
    r'(?:vless|vmess|trojan|ss|ssr|hysteria2?|hy2|tuic)://[^\s<>"]+',
    re.IGNORECASE
)

def extract_keys(text: str) -> list:
    """Простая версия — только regex, без JSON парсинга"""
    return KEY_PATTERN.findall(text)

# ==================== КОМАНДЫ ====================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    welcome_text = (
        "Здравствуйте.\n\n"
        "• /addkeys — сохранить свои ключи и получить короткую ссылку.\n"
        "• /shorten — сократить любую ссылку.\n"
        "• /profile — посмотреть свои сохранённые ссылки.\n\n"
        "🔓 **HWID Bypass**: При создании ссылок можно включить обход блокировки по железу."
    )
    safe_reply_to(message, with_footer(welcome_text), parse_mode="Markdown")

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

    msg = bot.reply_to(
        message,
        with_footer("Пришлите ключи, которые нужно сохранить.")
    )
    bot.register_next_step_handler(msg, process_addkeys)

def process_addkeys(message):
    try:
        print(f"[DEBUG] process_addkeys called, user: {message.from_user.id}")
        
        if not is_subscribed(message.from_user.id):
            send_subscribe_prompt(message.chat.id)
            return

        text = (message.text or "").strip()
        print(f"[DEBUG] Raw text length: {len(text)}")
        
        keys = extract_keys(text)
        print(f"[DEBUG] Extracted keys: {len(keys)}")

        if not keys:
            safe_reply_to(message, with_footer("Не нашёл ни одного ключа. Попробуйте ещё раз через /addkeys."))
            return

        content = "\n".join(keys)
        print(f"[DEBUG] Content length: {len(content)}")
        
        # Сохраняем во временное хранилище
        session_id = create_pending_save(content)
        print(f"[DEBUG] Created session: {session_id}")
        
        # Создаём кнопки с коротким session_id
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(
            telebot.types.InlineKeyboardButton("Да 🔓", callback_data=f"hwid_yes:{session_id}"),
            telebot.types.InlineKeyboardButton("Нет", callback_data=f"hwid_no:{session_id}")
        )
        
        print(f"[DEBUG] Sending HWID choice menu")
        safe_reply_to(
            message,
            with_footer(f"Найдено ключей: {len(keys)}\n\nВключить HWID bypass для этой ссылки?"),
            reply_markup=markup
        )
        print(f"[DEBUG] Menu sent successfully")
        
    except Exception as e:
        print(f"[ERROR] Exception in process_addkeys: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        try:
            safe_reply_to(message, with_footer(f"Произошла ошибка: {e}"))
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("hwid_"))
def handle_hwid_choice(call):
    try:
        print(f"[DEBUG] hwid callback triggered, data: {call.data}")
        
        choice, session_id = call.data.split(":", 1)
        hwid_bypass = choice == "hwid_yes"
        
        print(f"[DEBUG] Choice: {choice}, Session: {session_id}, Bypass: {hwid_bypass}")
        
        # Получаем контент из временного хранилища
        content = get_pending_save(session_id)
        
        if not content:
            print(f"[ERROR] Session {session_id} not found in pending_saves")
            bot.answer_callback_query(call.id, "Сессия истекла. Попробуйте ещё раз через /addkeys", show_alert=True)
            return
        
        print(f"[DEBUG] Retrieved content length: {len(content)}")
        
        # Сохраняем в базу
        code = save_link('keys', content, call.from_user.id, hwid_bypass)
        print(f"[DEBUG] Saved with code: {code}")
        
        short_url = build_short_url(code)
        
        response_text = f"✅ Сохранено!\n\nОбычная ссылка:\n{short_url}"
        
        if hwid_bypass:
            bypass_url = build_bypass_url(code)
            response_text += f"\n\n🔓 HWID Bypass ссылка:\n{bypass_url}\n\n⚡️ Bypass ссылка обходит блокировки по железу (работает 1 час)"
        
        bot.answer_callback_query(call.id, "Сохранено ✅")
        safe_send_message(call.message.chat.id, with_footer(response_text))
        print(f"[DEBUG] Response sent successfully")
        
    except Exception as e:
        print(f"[ERROR] Exception in handle_hwid_choice: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        try:
            bot.answer_callback_query(call.id, f"Ошибка сохранения: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass

@bot.message_handler(commands=['shorten'])
def cmd_shorten(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    msg = bot.reply_to(
        message,
        with_footer("Пришлите ссылку (http:// или https://), которую нужно сократить.")
    )
    bot.register_next_step_handler(msg, process_shorten)

def process_shorten(message):
    try:
        if not is_subscribed(message.from_user.id):
            send_subscribe_prompt(message.chat.id)
            return

        text = (message.text or "").strip()

        if not (text.startswith("http://") or text.startswith("https://")):
            safe_reply_to(message, with_footer("Это не похоже на ссылку. Попробуйте ещё раз через /shorten."))
            return

        code = save_link('url', text, message.from_user.id, hwid_bypass=False)
        short_url = build_short_url(code)

        safe_reply_to(message, with_footer(f"Готово! Короткая ссылка:\n{short_url}"))
    except Exception as e:
        print(f"[ОШИБКА в process_shorten]: {e}")
        try:
            safe_reply_to(message, with_footer(f"Произошла ошибка: {e}"))
        except Exception:
            pass

@bot.message_handler(commands=['profile'])
def cmd_profile(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    rows = get_links_by_owner(message.from_user.id)

    if not rows:
        safe_reply_to(message, with_footer("У вас пока нет сохранённых ссылок."))
        return

    text = f"Ваши ссылки ({len(rows)}):\n\n"
    for code, link_type, content, created_at, hwid_bypass in rows:
        icon = "🔗" if link_type == 'url' else "🔑"
        if hwid_bypass:
            icon += "🔓"
        short_url = build_short_url(code)
        text += f"{icon} {short_url}\n"

    safe_reply_to(message, with_footer(text))

# ==================== ЗАПУСК ====================

if __name__ == '__main__':
    import traceback
    try:
        print("[START] Запуск бота...")
        
        http_thread = threading.Thread(target=run_http_server, daemon=True)
        http_thread.start()
        print("[START] HTTP сервер запущен")

        bot.remove_webhook()
        print("[START] Webhook удалён")
        
        print("[START] Запуск polling...")
        bot.polling(none_stop=True)
    except Exception:
        traceback.print_exc()
    finally:
        input("\nНажмите Enter, чтобы закрыть окно...")
