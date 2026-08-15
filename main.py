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

# Снижаем базовые таймауты Telegram API, чтобы бот не зависал при проблемах с сетью
telebot.apihelper.READ_TIMEOUT = 20
telebot.apihelper.CONNECT_TIMEOUT = 15

# ==================== БЕЗОПАСНАЯ ОТПРАВКА ====================

def safe_send_message(chat_id, text, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            time.sleep(delay)
        except Exception as e:
            print(f"[Ошибка send_message]: {e}")
            if attempt == retries:
                raise

def safe_send_document(chat_id, document, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return bot.send_document(chat_id, document, **kwargs)
        except requests.exceptions.ReadTimeout as e:
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
        except requests.exceptions.ReadTimeout as e:
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

def decrypt_happ_link(encrypted_link: str) -> str:
    endpoint = "https://api.ioo.ir/v1/happ/decrypt"
    headers = {"Content-Type": "application/json"}
    payload = {"link": encrypted_link}
    try:
        response = requests.post(endpoint, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("ok"):
            return data.get("result", "")
        return ""
    except Exception as e:
        print(f"Ошибка API дешифровки: {e}")
        return ""

def try_decode_base64(text: str) -> str:
    try:
        padded = text + '=' * (-len(text) % 4)
        return base64.b64decode(padded).decode('utf-8')
    except Exception:
        return text

def is_html(content_type: str, body: str) -> bool:
    return "text/html" in content_type or body.lstrip().startswith(("<html", "<!DOCTYPE", "<!doctype"))

# ==================== КОНВЕРТЕР JSON -> URI ====================

def _vless_outbound_to_uri(outbound: dict, remarks: str) -> str | None:
    settings = outbound.get('settings', {}) or {}
    stream = outbound.get('streamSettings', {}) or {}

    vnext = settings.get('vnext')
    if vnext:
        node = vnext[0]
        address = node.get('address')
        port = node.get('port')
        user = (node.get('users') or [{}])[0]
        uid = user.get('id')
        encryption = user.get('encryption', settings.get('encryption', 'none'))
        flow = user.get('flow', '')
    else:
        address = settings.get('address')
        port = settings.get('port')
        uid = settings.get('id')
        encryption = settings.get('encryption', 'none')
        flow = settings.get('flow', '')

    if not (address and port and uid):
        return None

    network = stream.get('network', 'tcp')
    security = stream.get('security', 'none')

    params = {'encryption': encryption or 'none', 'security': security, 'type': network}
    if flow:
        params['flow'] = flow

    tls = stream.get('tlsSettings') or stream.get('realitySettings') or {}
    if tls.get('serverName'):
        params['sni'] = tls['serverName']
    if tls.get('fingerprint'):
        params['fp'] = tls['fingerprint']
    if tls.get('alpn'):
        params['alpn'] = ','.join(tls['alpn'])

    if network == 'ws':
        ws = stream.get('wsSettings', {}) or {}
        if ws.get('path'):
            params['path'] = ws['path']
        host = (ws.get('headers') or {}).get('Host') or ws.get('host')
        if host:
            params['host'] = host
    elif network == 'grpc':
        grpc = stream.get('grpcSettings', {}) or {}
        if grpc.get('serviceName'):
            params['serviceName'] = grpc['serviceName']

    query = urllib.parse.urlencode(params, safe=',')
    name = urllib.parse.quote(remarks or '')
    return f"vless://{uid}@{address}:{port}?{query}#{name}"

def _hysteria_outbound_to_uri(outbound: dict, remarks: str) -> str | None:
    settings = outbound.get('settings', {}) or {}
    stream = outbound.get('streamSettings', {}) or {}
    hy = stream.get('hysteriaSettings', {}) or {}
    tls = stream.get('tlsSettings', {}) or {}

    address = settings.get('address')
    port = settings.get('port')
    auth = hy.get('auth')
    version = hy.get('version') or settings.get('version') or 2

    if not (address and port and auth):
        return None

    scheme = 'hysteria2' if version == 2 else 'hysteria'
    params = {}
    if tls.get('serverName'):
        params['sni'] = tls['serverName']
    if tls.get('alpn'):
        params['alpn'] = ','.join(tls['alpn'])

    query = urllib.parse.urlencode(params, safe=',')
    name = urllib.parse.quote(remarks or '')
    return f"{scheme}://{auth}@{address}:{port}?{query}#{name}"

def _trojan_outbound_to_uri(outbound: dict, remarks: str) -> str | None:
    settings = outbound.get('settings', {}) or {}
    stream = outbound.get('streamSettings', {}) or {}

    servers = settings.get('servers')
    if servers:
        node = servers[0]
        address = node.get('address')
        port = node.get('port')
        password = node.get('password')
    else:
        address = settings.get('address')
        port = settings.get('port')
        password = settings.get('password')

    if not (address and port and password):
        return None

    security = stream.get('security', 'tls')
    params = {'security': security}
    tls = stream.get('tlsSettings', {}) or {}
    if tls.get('serverName'):
        params['sni'] = tls['serverName']

    query = urllib.parse.urlencode(params, safe=',')
    name = urllib.parse.quote(remarks or '')
    return f"trojan://{password}@{address}:{port}?{query}#{name}"

_NON_PROXY_PROTOCOLS = {'freedom', 'blackhole', 'dns'}

_OUTBOUND_CONVERTERS = {
    'vless': _vless_outbound_to_uri,
    'hysteria': _hysteria_outbound_to_uri,
    'hysteria2': _hysteria_outbound_to_uri,
    'trojan': _trojan_outbound_to_uri,
}

def convert_xray_json_to_links(text: str) -> list:
    try:
        data = json.loads(text)
    except Exception:
        return []

    configs = data if isinstance(data, list) else [data]
    links = []

    for cfg in configs:
        if not isinstance(cfg, dict):
            continue
        outbounds = cfg.get('outbounds')
        if not isinstance(outbounds, list):
            continue

        remarks = cfg.get('remarks', '')
        for ob in outbounds:
            if not isinstance(ob, dict):
                continue
            proto = ob.get('protocol')
            if not proto or proto in _NON_PROXY_PROTOCOLS:
                continue
            converter = _OUTBOUND_CONVERTERS.get(proto)
            if not converter:
                continue
            try:
                uri = converter(ob, remarks)
            except Exception as e:
                print(f"[Ошибка конвертации outbound {proto}]: {e}")
                uri = None
            if uri:
                links.append(uri)
    return links

def extract_keys(text: str) -> list:
    keys = KEY_PATTERN.findall(text)
    if keys:
        return keys
    return convert_xray_json_to_links(text)

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
        if headers is None:
            headers = HAPP_HEADERS.copy()
        session = requests.Session()
        session.headers.update(headers)
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

    try:
        resp, body = fetch_with_headers(url)
        ct = resp.headers.get('Content-Type', '')
        if not is_html(ct, body) and body:
            return try_decode_base64(body)
            
        if is_html(ct, body):
            match = re.search(r'(https?://[^\s"\'<>]+\.(?:txt|json|xml|conf|cfg|sub))', body, re.IGNORECASE)
            if match:
                real_url = match.group(1)
                resp2, body2 = fetch_with_headers(real_url)
                ct2 = resp2.headers.get('Content-Type', '')
                if not is_html(ct2, body2) and body2:
                    return try_decode_base64(body2)
    except Exception:
        pass

    try:
        headers_json = HAPP_HEADERS.copy()
        headers_json["Accept"] = "application/json"
        resp, body = fetch_with_headers(url, headers_json)
        ct = resp.headers.get('Content-Type', '')
        if not is_html(ct, body) and body:
            return try_decode_base64(body)
    except Exception:
        pass

    base = url.rstrip("/")
    token = base.split("/")[-1] if "/sub/" not in base else ""
    origin = "/".join(base.split("/")[:-1]) if "/sub/" not in base else base

    candidates = []
    if token:
        candidates.append(f"{origin}/sub/{token}")
    candidates.append(f"{base}/sub")
    candidates.append(f"{base}?format=clash")
    
    for alt_url in candidates:
        try:
            resp, body = fetch_with_headers(alt_url)
            ct = resp.headers.get('Content-Type', '')
            if not is_html(ct, body) and body:
                return try_decode_base64(body)
        except Exception:
            continue

    return body if 'body' in locals() else "Не удалось получить подписку"

# ==================== HTTP-СЕРВЕР (Динамический прокси) ====================

app = Flask(__name__)

subscription_cache = {}
CACHE_TTL = 300 

@app.route('/')
def health_check():
    return 'OK'

@app.route('/<code>')
def resolve_link(code):
    row = get_link_full(code)
    if not row:
        abort(404)

    link_type, content, owner_id, name, hwid = row

    if link_type == 'url':
        if code in subscription_cache:
            cached_time, cached_data = subscription_cache[code]
            if time.time() - cached_time < CACHE_TTL:
                return Response(cached_data, mimetype='text/plain')

        try:
            configs_text = fetch_and_decode_configs(content, hwid)
            keys = extract_keys(configs_text)
            
            if keys:
                joined_keys = "\n".join(keys)
                encoded_sub = base64.b64encode(joined_keys.encode('utf-8')).decode('utf-8')
                
                subscription_cache[code] = (time.time(), encoded_sub)
                return Response(encoded_sub, mimetype='text/plain')
            else:
                return Response("Ошибка: ключи не найдены или сервер источника не ответил", status=404)
        except Exception as e:
            print(f"[ОШИБКА проксирования {code}]: {e}")
            abort(502)
    else:
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
        "📌 <b>Бот умеет:</b>\n"
        "• Автоматически расшифровывать ссылки <b>happ://crypt</b>.\n"
        "• Извлекать VPN-ключи по <b>http/https</b> ссылкам (работает как настоящий клиент Happ).\n"
        "• /shorten — создать динамическую прокси-подписку (с возможностью задать HWID для обхода).\n"
        "• /addkeys — сохранить свои ключи и получить короткую ссылку-подписку.\n"
        "• /profile — управлять сохранёнными подписками."
    )
    safe_reply_to(message, with_footer(welcome_text), parse_mode="HTML")

@bot.message_handler(commands=['shorten'])
def cmd_shorten(message):
    print(f"[LOG] Получена команда /shorten от {message.from_user.id}")
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        url = parts[1].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            safe_reply_to(message, with_footer("Это не похоже на ссылку. Используйте /shorten <URL>."))
            return
        with user_data_lock:
            user_data[message.from_user.id] = {'url': url}
        msg = safe_reply_to(message, with_footer("Введите название для этой подписки:"))
        bot.register_next_step_handler(msg, process_name)
        return

    msg = safe_reply_to(message, with_footer("Пришлите ссылку (http/https), которую нужно сделать прокси-подпиской."))
    bot.register_next_step_handler(msg, process_url)

def process_url(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        safe_reply_to(message, with_footer("Это не похоже на ссылку. Попробуйте ещё раз через /shorten."))
        return
    with user_data_lock:
        user_data[message.from_user.id] = {'url': url}
    msg = safe_reply_to(message, with_footer("Введите название для этой подписки:"))
    bot.register_next_step_handler(msg, process_name)

def process_name(message):
    user_id = message.from_user.id
    with user_data_lock:
        data = user_data.get(user_id)
        if not data:
            safe_reply_to(message, with_footer("Сессия истекла."))
            return
        data['name'] = (message.text or "").strip() or "Без названия"
    msg = safe_reply_to(message, with_footer("Введите HWID для эмуляции Happ-клиента (отправьте 0, если хотите сгенерировать случайный стандартный HWID):"))
    bot.register_next_step_handler(msg, process_hwid)

def process_hwid(message):
    user_id = message.from_user.id
    with user_data_lock:
        data = user_data.get(user_id)
        if not data:
            safe_reply_to(message, with_footer("Сессия истекла."))
            return
            
        hwid_input = (message.text or "").strip()
        if hwid_input == '0' or not hwid_input:
            data['hwid'] = str(random.randint(100000000000000000, 999999999999999999))
        else:
            data['hwid'] = hwid_input

    url = data['url']
    name = data['name']
    hwid = data['hwid']

    code = save_link('url', url, user_id, name, hwid)
    short_url = build_short_url(code)
    response_text = f"✅ <b>Динамическая прокси-подписка создана!</b>\n\n📌 Название: {name}\n🆔 HWID: {hwid}\n🔗 {short_url}\n\nТеперь эта ссылка будет автоматически перехватывать и обновлять ключи из оригинала при каждом обновлении в клиенте."
    safe_reply_to(message, with_footer(response_text), parse_mode="HTML")

@bot.message_handler(commands=['addkeys'])
def cmd_addkeys(message):
    print(f"[LOG] Получена команда /addkeys от {message.from_user.id}")
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        message.text = parts[1].strip()
        process_addkeys(message)
        return

    msg = safe_reply_to(
        message,
        with_footer("Пришлите ключи, которые нужно сохранить (можно несколько, каждый с новой строки). HWID не требуется.")
    )
    bot.register_next_step_handler(msg, process_addkeys)

def process_addkeys(message):
    try:
        if not is_subscribed(message.from_user.id):
            send_subscribe_prompt(message.chat.id)
            return

        text = (message.text or "").strip()
        keys = extract_keys(text)

        if not keys:
            safe_reply_to(message, with_footer("Не нашёл ни одного ключа. Попробуйте ещё раз через /addkeys."))
            return

        content = "\n".join(keys)
        code = save_link('keys', content, message.from_user.id, name=None)
        short_url = build_short_url(code)
        safe_reply_to(message, with_footer(f"✅ Ключи сохранены в статичную подписку!\n\nСсылка:\n{short_url}"))
    except Exception as e:
        safe_reply_to(message, with_footer(f"Ошибка: {e}"))

@bot.message_handler(commands=['profile'])
def cmd_profile(message):
    print(f"[LOG] Получена команда /profile от {message.from_user.id}")
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    rows = get_links_by_owner(message.from_user.id)

    if not rows:
        safe_reply_to(message, with_footer("У вас пока нет сохранённых ссылок/подписок."))
        return

    markup = build_profile_menu(rows)
    safe_reply_to(
        message,
        with_footer(f"Ваши подписки ({len(rows)}). Нажмите для просмотра деталей:"),
        reply_markup=markup
    )

def build_profile_menu(rows: list) -> telebot.types.InlineKeyboardMarkup:
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for row in rows:
        code, link_type, content, created_at, name, hwid = row
        icon = "🔄" if link_type == 'url' else "🔑"
        display_name = name if name else code
        markup.add(
            telebot.types.InlineKeyboardButton(
                text=f"{icon} {display_name}",
                callback_data=f"view:{code}"
            )
        )
    return markup

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

    hwid_text = f"\n🆔 HWID: {hwid}" if link_type == 'url' and hwid else ""
    text = f"<b>{type_label}</b>\n\n📌 Название: {name or '—'}{hwid_text}\n🔗 {short_url}\n\n📄 <b>Источник / Содержимое:</b>\n{preview}"

    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✏️ Изменить название", callback_data=f"edit_name:{code}"),
        telebot.types.InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{code}")
    )
    markup.add(
        telebot.types.InlineKeyboardButton("‹ Назад к списку", callback_data="profile_back")
    )

    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            with_footer(text),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception:
        safe_send_message(call.message.chat.id, with_footer(text), reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "profile_back")
def handle_profile_back(call):
    rows = get_links_by_owner(call.from_user.id)
    bot.answer_callback_query(call.id)
    if not rows:
        try:
            bot.edit_message_text(
                with_footer("У вас больше нет сохранённых ссылок."),
                call.message.chat.id,
                call.message.message_id
            )
        except Exception:
            pass
        return
    markup = build_profile_menu(rows)
    try:
        bot.edit_message_text(
            with_footer(f"Ваши подписки ({len(rows)}):"),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("del:"))
def handle_delete_link(call):
    code = call.data.split(":", 1)[1]
    success = delete_link(code, call.from_user.id)
    if success:
        bot.answer_callback_query(call.id, "Удалено ✅")
        handle_profile_back(call)
    else:
        bot.answer_callback_query(call.id, "Не удалось удалить (ссылка не найдена или не ваша).", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_name:"))
def handle_edit_name(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)
    if not row or row[2] != call.from_user.id:
        bot.answer_callback_query(call.id, "Ссылка не найдена или не ваша.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Введите новое название:")
    msg = safe_send_message(call.message.chat.id, with_footer(f"Для {build_short_url(code)} введите новое название:"))
    bot.register_next_step_handler(msg, process_edit_name, code, call.from_user.id)

def process_edit_name(message, code, owner_id):
    if message.from_user.id != owner_id:
        return
    name = (message.text or "").strip()
    if not name:
        safe_reply_to(message, with_footer("Название не может быть пустым."))
        return
    success = update_link_name(code, owner_id, name)
    if success:
        safe_reply_to(message, with_footer("Название обновлено!"))
    else:
        safe_reply_to(message, with_footer("Ошибка обновления."))

# ==================== ОБРАБОТЧИК ТЕКСТА (Диагностика) ====================

def send_keys(chat_id: int, keys: list):
    joined = "\n".join(keys)
    file_name = next_file_name()
    try:
        with open(file_name, 'w', encoding='utf-8') as file:
            file.write(joined)
        with open(file_name, 'rb') as file:
            safe_send_document(
                chat_id,
                file,
                visible_file_name=file_name,
                caption=with_footer(f"Найдено ключей: {len(keys)}. Файл во вложении.")
            )
    finally:
        if os.path.exists(file_name):
            os.remove(file_name)

@bot.message_handler(content_types=['text'])
def handle_text(message):
    print(f"[LOG] Получен текст от {message.from_user.id}: {message.text[:20]}...")
    try:
        if not is_subscribed(message.from_user.id):
            send_subscribe_prompt(message.chat.id)
            return

        text = message.text.strip()

        if text.startswith("happ://crypt"):
            safe_reply_to(message, with_footer("Обрабатываю happ-ссылку..."))
            decrypted_url = decrypt_happ_link(text)
            if decrypted_url:
                safe_reply_to(message, with_footer(f"Ссылка успешно расшифрована:\n\n{decrypted_url}"))
            else:
                safe_reply_to(message, with_footer("Не удалось расшифровать ссылку."))

        elif text.startswith("http://") or text.startswith("https://"):
            safe_reply_to(message, with_footer("Загружаю подписку как клиент Happ (для проверки/извлечения)..."))
            configs_text = fetch_and_decode_configs(text)

            if configs_text.startswith("Сетевая ошибка") or configs_text.startswith("Внутренняя ошибка") or not configs_text:
                safe_reply_to(message, with_footer(configs_text or "Пустой ответ от сервера."))
                return

            keys = extract_keys(configs_text)

            if keys:
                send_keys(message.chat.id, keys)
            else:
                file_name = next_file_name()
                try:
                    with open(file_name, 'w', encoding='utf-8') as file:
                        file.write(configs_text)
                    with open(file_name, 'rb') as file:
                        safe_send_document(
                            message.chat.id,
                            file,
                            visible_file_name=file_name,
                            caption=with_footer("Ключи не найдены. Вот сырые данные сервера.")
                        )
                except Exception as e:
                    safe_reply_to(message, with_footer(f"Ошибка: {e}"))
                finally:
                    if os.path.exists(file_name):
                        os.remove(file_name)

        else:
            safe_reply_to(message, with_footer("Неверный формат. Используйте /shorten (создать подписку), /addkeys или отправьте ссылку на проверку."))
    except Exception as e:
        safe_reply_to(message, with_footer(f"Ошибка: {e}"))

# ==================== ЗАПУСК ====================

user_data_lock = threading.Lock()
user_data = {}

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
        
        # Удаление вебхука с минимальным таймаутом, чтобы не зависало
        try:
            bot.delete_webhook(drop_pending_updates=True, timeout=5)
            print("[START] Webhook очищен")
        except Exception as e:
            print(f"[START] Ошибка очистки вебхука (игнорируется): {e}")

        # Запускаем бота в ФОНОВОМ потоке
        bot_thread = threading.Thread(target=run_bot_polling, daemon=True)
        bot_thread.start()
        
        # Запускаем Flask сервер в ГЛАВНОМ потоке
        print(f"[START] HTTP сервер запускается на порту {PORT}")
        app.run(host='0.0.0.0', port=PORT, threaded=True, use_reloader=False)

    except Exception:
        traceback.print_exc()
        input("\nНажмите Enter, чтобы закрыть окно...")
