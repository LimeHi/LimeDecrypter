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

from flask import Flask, abort, Response, request

# ==================== КОНФИГУРАЦИЯ ====================

TOKEN = os.getenv('TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')
FOOTER_TAG = os.getenv('FOOTER_TAG', '')
FILE_PREFIX = os.getenv('FILE_PREFIX', 'keys')
BASE_URL = os.getenv('BASE_URL')
PORT = int(os.getenv('PORT', '3000'))
DATA_DIR = os.getenv('DATA_DIR', '/app/data')

# HWID (device id), который бот подставляет в User-Agent при обращении
# к исходному серверу подписки, имитируя реальный клиент Happ.
# Это НЕ HWID для привязки клиента — используется только для скачивания
# конфигурации с апстрим-сервера подписки.
DEFAULT_DEVICE_ID = os.getenv('DEFAULT_DEVICE_ID', '178394521473618780')
HAPP_VERSION = os.getenv('HAPP_VERSION', '3.26.3')

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

# ==================== ВРЕМЕННОЕ ХРАНИЛИЩЕ (для команд) ====================

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

cleanup_thread = threading.Thread(target=cleanup_pending_saves, daemon=True)
cleanup_thread.start()

def safe_send_message(chat_id, text, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            time.sleep(delay)
        except Exception as e:
            print(f"[Ошибка send_message]: {e}")
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
            raise

def safe_reply_to(message, text, retries=3, delay=2, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return bot.reply_to(message, text, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            time.sleep(delay)
        except Exception as e:
            print(f"[Ошибка reply_to]: {e}")
            raise

# ==================== БАЗА ДАННЫХ ====================
# ВАЖНО: колонка device_id используется ТОЛЬКО для ссылок типа 'url'
# (то есть для подписок, которые бот сам ходит и забирает у апстрима).
# Для type='keys' (просто вставленные vless/vmess/trojan/... ключи)
# device_id никогда не используется и не запрашивается.

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
            device_id TEXT DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Добавляем device_id, если база создана старой версией скрипта
    try:
        conn.execute('SELECT device_id FROM links LIMIT 1')
    except Exception:
        try:
            conn.execute('ALTER TABLE links ADD COLUMN device_id TEXT DEFAULT NULL')
        except Exception:
            pass
    # На всякий случай подчищаем старые HWID-колонки клиента (если остались от прошлых версий)
    for legacy_col in ('hwid', 'hwid_bypass'):
        try:
            conn.execute(f'SELECT {legacy_col} FROM links LIMIT 1')
            conn.execute(f'ALTER TABLE links DROP COLUMN {legacy_col}')
        except Exception:
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

def generate_device_id() -> str:
    """Генерирует псевдослучайный числовой device id в формате, похожем на настоящий Happ."""
    return ''.join(random.choices(string.digits, k=18))

def save_link(link_type: str, content: str, owner_id: int, name: str = None, device_id: str = None) -> str:
    code = generate_code()
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                'INSERT INTO links (code, type, content, owner_id, name, device_id) VALUES (?, ?, ?, ?, ?, ?)',
                (code, link_type, content, owner_id, name, device_id)
            )
            conn.commit()
        finally:
            conn.close()
    return code

def get_link(code: str):
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute('SELECT type, content, name, device_id FROM links WHERE code = ?', (code,)).fetchone()
            return row
        finally:
            conn.close()

def get_link_full(code: str):
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute('SELECT type, content, owner_id, name, device_id FROM links WHERE code = ?', (code,)).fetchone()
            return row
        finally:
            conn.close()

def get_links_by_owner(owner_id: int, limit: int = 20):
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                'SELECT code, type, content, created_at, name FROM links WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?',
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

def update_link_device_id(code: str, owner_id: int, device_id: str) -> bool:
    """Меняет HWID (device id), который используется при скачивании подписки у апстрима.
    Работает только для ссылок типа 'url' — вызывающий код должен это проверить сам."""
    with db_lock:
        conn = get_db()
        try:
            cur = conn.execute(
                'UPDATE links SET device_id = ? WHERE code = ? AND owner_id = ? AND type = ?',
                (device_id, code, owner_id, 'url')
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

def build_short_url(code: str) -> str:
    return f"{BASE_URL}/{code}"

# ==================== HTTP-СЕРВЕР ====================
# Ключевое изменение: ссылка типа 'url' — это ЖИВАЯ подписка.
# Каждый раз, когда её открывает клиент (Happ/v2rayNG/...), наш сервер
# сам, прямо сейчас, ходит на исходный апстрим-сервер, представляясь
# клиентом Happ (с указанным HWID), скачивает актуальный конфиг и
# отдаёт его дальше. Ничего не кешируется — подписка всегда "живая"
# и обновляется вместе с апстримом.

app = Flask(__name__)

@app.route('/')
def health_check():
    return 'OK'

@app.route('/<code>')
def resolve_link(code):
    row = get_link(code)
    if not row:
        abort(404)

    link_type, content, name, device_id = row

    if link_type == 'url':
        # content — это исходный (реальный) URL подписки апстрима.
        # Живём заново каждый раз: имитируем Happ-клиент с нужным HWID.
        try:
            configs_text = fetch_and_decode_configs(content, device_id=device_id)
            keys = extract_keys(configs_text)
            if keys:
                body = "\n".join(keys)
                return Response(body, mimetype='text/plain; charset=utf-8')
            # Если извлечь ключи не получилось — отдаём то, что получили,
            # как есть (может пригодиться клиенту, если это уже готовый sub-формат).
            return Response(configs_text, mimetype='text/plain; charset=utf-8')
        except Exception as e:
            print(f"[ОШИБКА проксирования {code}]: {e}")
            abort(502)
    else:
        # type == 'keys' — статически сохранённые ключи, HWID тут не участвует.
        return Response(content, mimetype='text/plain; charset=utf-8')

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
    safe_send_message(chat_id, with_footer(f"Для использования бота необходимо подписаться на канал {CHANNEL_USERNAME}."), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def handle_check_sub(call):
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Подписка подтверждена ✅")
        safe_send_message(call.message.chat.id, with_footer("Отлично! Теперь можешь пользоваться ботом."))
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

# ==================== КОНВЕРТЕР XRAY/V2RAY JSON -> URI ====================

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

# ==================== ГЛАВНАЯ ФУНКЦИЯ — ЭМУЛЯЦИЯ Happ ====================

def _happ_headers(device_id: str = None) -> dict:
    dev = device_id or DEFAULT_DEVICE_ID
    return {
        "User-Agent": f"Happ/{HAPP_VERSION}/Android/{dev}",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }

def fetch_and_decode_configs(url: str, device_id: str = None) -> str:
    """
    Эмулирует запрос от Happ/<version>/Android/<device_id>.
    device_id (HWID) можно задать под конкретную подписку — это влияет
    ТОЛЬКО на то, каким устройством бот представляется апстриму при
    скачивании конфигурации, и никак не связано с ключами, которые
    сохраняются через /addkeys.
    Пробует разные комбинации заголовков и параметров, чтобы получить
    реальные конфигурации.
    """

    def fetch_with_headers(u, headers=None):
        if headers is None:
            headers = _happ_headers(device_id)
        session = requests.Session()
        session.headers.update(headers)
        resp = session.get(u, timeout=15, stream=True)
        resp.raise_for_status()
        if resp.headers.get('Content-Encoding') == 'gzip':
            try:
                gz = gzip.GzipFile(fileobj=io.BytesIO(resp.content))
                content = gz.read().decode('utf-8')
            except Exception:
                content = resp.text
        else:
            content = resp.text
        return resp, content

    # Шаг 1: обычный запрос с базовыми заголовками Happ
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
    except Exception as e:
        print(f"[Шаг 1] ошибка: {e}")

    # Шаг 2: меняем Accept на application/json
    try:
        headers_json = _happ_headers(device_id)
        headers_json["Accept"] = "application/json"
        resp, body = fetch_with_headers(url, headers_json)
        ct = resp.headers.get('Content-Type', '')
        if not is_html(ct, body) and body:
            return try_decode_base64(body)
    except Exception as e:
        print(f"[Шаг 2] ошибка: {e}")

    # Шаг 3: добавляем параметры ?format=clash, ?app=happ, /sub/
    base = url.rstrip("/")
    token = base.split("/")[-1] if "/sub/" not in base else ""
    origin = "/".join(base.split("/")[:-1]) if "/sub/" not in base else base

    candidates = []
    if token:
        candidates.append(f"{origin}/sub/{token}")
    candidates.append(f"{base}/sub")
    if "?" in base:
        candidates.append(f"{base}&format=clash")
        candidates.append(f"{base}&app=happ")
    else:
        candidates.append(f"{base}?format=clash")
        candidates.append(f"{base}?app=happ")

    for alt_url in candidates:
        try:
            resp, body = fetch_with_headers(alt_url)
            ct = resp.headers.get('Content-Type', '')
            if not is_html(ct, body) and body:
                return try_decode_base64(body)
        except Exception as e:
            print(f"[Альтернатива {alt_url}] ошибка: {e}")

    return body if 'body' in locals() else "Не удалось получить подписку"

def extract_keys(text: str) -> list:
    keys = KEY_PATTERN.findall(text)
    if keys:
        return keys
    return convert_xray_json_to_links(text)

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

# ==================== КОМАНДЫ ====================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    welcome_text = (
        "Здравствуйте.\n\n"
        "📌 Бот умеет:\n"
        "• Автоматически расшифровывать ссылки **happ://crypt**.\n"
        "• Автоматически загружать подписки по **http://** или **https://** ссылкам и извлекать VPN-ключи (vless, vmess, trojan, ss, hysteria и др.) – работает как настоящий клиент Happ.\n"
        "• /shorten — создать свою короткую живую подписку (бот сам будет ходить к апстриму под видом Happ и отдавать актуальные конфиги; можно задать свой HWID).\n"
        "• /addkeys — сохранить свои ключи и получить короткую ссылку (обычный статичный список, без HWID).\n"
        "• /profile — посмотреть все свои сохранённые ссылки, управлять ими (удалить, изменить название, для подписок — сменить HWID)."
    )
    safe_reply_to(message, with_footer(welcome_text))

# ==================== ПРОФИЛЬ ====================

def build_profile_menu(rows: list) -> telebot.types.InlineKeyboardMarkup:
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for row in rows:
        code, link_type, content, created_at, name = row
        icon = "🔗" if link_type == 'url' else "🔑"
        display_name = name if name else code
        markup.add(
            telebot.types.InlineKeyboardButton(
                text=f"{icon} {display_name}",
                callback_data=f"view:{code}"
            )
        )
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
    safe_reply_to(
        message,
        with_footer(f"Ваши ссылки ({len(rows)}). Нажмите на любую для просмотра деталей:"),
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("view:"))
def handle_view_link(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)

    if not row or row[2] != call.from_user.id:
        bot.answer_callback_query(call.id, "Ссылка не найдена или не ваша.", show_alert=True)
        return

    link_type, content, owner_id, name, device_id = row
    short_url = build_short_url(code)
    type_label = "🔗 Подписка (живая)" if link_type == 'url' else "🔑 Ключи (статично)"
    preview = content if len(content) <= 200 else content[:197] + "..."

    text = f"{type_label}\n\n📌 Название: {name or '—'}\n🔗 {short_url}\n\n📄 Исходник:\n{preview}"
    if link_type == 'url':
        text += f"\n\n🆔 HWID для скачивания: {device_id or DEFAULT_DEVICE_ID + ' (по умолчанию)'}"

    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✏️ Название", callback_data=f"edit_name:{code}"),
        telebot.types.InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{code}")
    )
    if link_type == 'url':
        markup.add(
            telebot.types.InlineKeyboardButton("🆔 Сменить HWID", callback_data=f"edit_device:{code}")
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
            reply_markup=markup
        )
    except Exception:
        safe_send_message(call.message.chat.id, with_footer(text), reply_markup=markup)

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
            with_footer(f"Ваши ссылки ({len(rows)}). Нажмите на любую для просмотра деталей:"),
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
        rows = get_links_by_owner(call.from_user.id)
        if not rows:
            try:
                bot.edit_message_text(
                    with_footer("Ссылка удалена. Сохранённых ссылок больше нет."),
                    call.message.chat.id,
                    call.message.message_id
                )
            except Exception:
                pass
        else:
            markup = build_profile_menu(rows)
            try:
                bot.edit_message_text(
                    with_footer(f"Ссылка удалена. Ваши ссылки ({len(rows)}):"),
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=markup
                )
            except Exception:
                pass
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_device:"))
def handle_edit_device(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)
    if not row or row[2] != call.from_user.id or row[0] != 'url':
        bot.answer_callback_query(call.id, "Недоступно для этой ссылки.", show_alert=True)
        return

    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        telebot.types.InlineKeyboardButton("🎲 Случайный HWID", callback_data=f"device_random:{code}"),
        telebot.types.InlineKeyboardButton("↩️ Сбросить на стандартный", callback_data=f"device_default:{code}"),
        telebot.types.InlineKeyboardButton("✏️ Ввести свой", callback_data=f"device_custom:{code}"),
    )
    bot.answer_callback_query(call.id)
    safe_send_message(
        call.message.chat.id,
        with_footer(
            "HWID (device id) используется только при скачивании конфигурации "
            "с апстрим-сервера подписки — под этим HWID бот представляется как Happ-клиент.\n"
            "Выберите вариант:"
        ),
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("device_random:"))
def handle_device_random(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(call.data.split(":", 1)[1])
    if not row or row[2] != call.from_user.id or row[0] != 'url':
        bot.answer_callback_query(call.id, "Недоступно для этой ссылки.", show_alert=True)
        return
    new_device_id = generate_device_id()
    update_link_device_id(code, call.from_user.id, new_device_id)
    bot.answer_callback_query(call.id, "HWID сгенерирован ✅")
    safe_send_message(call.message.chat.id, with_footer(f"Новый HWID для скачивания подписки:\n{new_device_id}"))

@bot.callback_query_handler(func=lambda call: call.data.startswith("device_default:"))
def handle_device_default(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)
    if not row or row[2] != call.from_user.id or row[0] != 'url':
        bot.answer_callback_query(call.id, "Недоступно для этой ссылки.", show_alert=True)
        return
    update_link_device_id(code, call.from_user.id, None)
    bot.answer_callback_query(call.id, "Сброшено на стандартный HWID ✅")
    safe_send_message(call.message.chat.id, with_footer("HWID сброшен на стандартный."))

@bot.callback_query_handler(func=lambda call: call.data.startswith("device_custom:"))
def handle_device_custom(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)
    if not row or row[2] != call.from_user.id or row[0] != 'url':
        bot.answer_callback_query(call.id, "Недоступно для этой ссылки.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    msg = safe_send_message(call.message.chat.id, with_footer("Введите свой HWID (только цифры/буквы, без пробелов):"))
    bot.register_next_step_handler(msg, process_device_custom, code, call.from_user.id)

def process_device_custom(message, code, owner_id):
    if message.from_user.id != owner_id:
        return
    value = (message.text or "").strip()
    if not value or not re.match(r'^[A-Za-z0-9\-_]{4,64}$', value):
        safe_reply_to(message, with_footer("Некорректный HWID. Разрешены буквы, цифры, - и _, от 4 до 64 символов."))
        return
    success = update_link_device_id(code, owner_id, value)
    if success:
        safe_reply_to(message, with_footer("HWID обновлён!"))
    else:
        safe_reply_to(message, with_footer("Ошибка обновления HWID."))

# ==================== /shorten (создание живой подписки) ====================

user_data = {}
user_data_lock = threading.Lock()

def _device_choice_markup() -> telebot.types.InlineKeyboardMarkup:
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        telebot.types.InlineKeyboardButton("↩️ Стандартный HWID", callback_data="new_device:default"),
        telebot.types.InlineKeyboardButton("🎲 Случайный HWID", callback_data="new_device:random"),
        telebot.types.InlineKeyboardButton("✏️ Ввести свой", callback_data="new_device:custom"),
    )
    return markup

@bot.message_handler(commands=['shorten'])
def cmd_shorten(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        url = parts[1].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            safe_reply_to(message, with_footer("Это не похоже на ссылку. Используйте /shorten <URL> или отправьте ссылку отдельно."))
            return
        with user_data_lock:
            user_data[message.from_user.id] = {'url': url}
        msg = safe_reply_to(message, with_footer("Введите название (будет отображаться в профиле):"))
        bot.register_next_step_handler(msg, process_name)
        return

    msg = safe_reply_to(message, with_footer("Пришлите ссылку исходной подписки (http:// или https://), которую нужно сократить."))
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

    safe_reply_to(
        message,
        with_footer(
            "Теперь выберите HWID, под которым бот будет ходить к серверу подписки "
            "(имитируя клиент Happ). Это не HWID для клиента, а только для скачивания конфига."
        ),
        reply_markup=_device_choice_markup()
    )

def _finalize_shorten(chat_id: int, user_id: int, device_id: str = None):
    with user_data_lock:
        data = user_data.pop(user_id, None)
    if not data:
        safe_send_message(chat_id, with_footer("Сессия истекла. Начните заново через /shorten."))
        return
    url = data['url']
    name = data['name']
    code = save_link('url', url, user_id, name=name, device_id=device_id)
    short_url = build_short_url(code)
    hwid_line = f"\n🆔 HWID: {device_id}" if device_id else "\n🆔 HWID: стандартный"
    response_text = f"✅ Подписка создана!\n\n📌 Название: {name}\n🔗 {short_url}{hwid_line}\n\nСсылка живая — при каждом обновлении в клиенте бот заново заберёт актуальный конфиг с исходного сервера."
    safe_send_message(chat_id, with_footer(response_text))

@bot.callback_query_handler(func=lambda call: call.data == "new_device:default")
def handle_new_device_default(call):
    bot.answer_callback_query(call.id)
    _finalize_shorten(call.message.chat.id, call.from_user.id, device_id=None)

@bot.callback_query_handler(func=lambda call: call.data == "new_device:random")
def handle_new_device_random(call):
    bot.answer_callback_query(call.id)
    _finalize_shorten(call.message.chat.id, call.from_user.id, device_id=generate_device_id())

@bot.callback_query_handler(func=lambda call: call.data == "new_device:custom")
def handle_new_device_custom(call):
    bot.answer_callback_query(call.id)
    msg = safe_send_message(call.message.chat.id, with_footer("Введите свой HWID (буквы/цифры/-/_, от 4 до 64 символов):"))
    bot.register_next_step_handler(msg, process_new_device_custom)

def process_new_device_custom(message):
    value = (message.text or "").strip()
    if not value or not re.match(r'^[A-Za-z0-9\-_]{4,64}$', value):
        safe_reply_to(message, with_footer("Некорректный HWID. Попробуйте ещё раз."))
        with user_data_lock:
            has_pending = message.from_user.id in user_data
        if has_pending:
            msg = safe_reply_to(message, with_footer("Введите свой HWID (буквы/цифры/-/_, от 4 до 64 символов):"))
            bot.register_next_step_handler(msg, process_new_device_custom)
        return
    _finalize_shorten(message.chat.id, message.from_user.id, device_id=value)

# ==================== /addkeys (без HWID — статичные ключи) ====================

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

    msg = safe_reply_to(
        message,
        with_footer("Пришлите ключи, которые нужно сохранить (можно несколько, каждый с новой строки).")
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
        # device_id намеренно не передаётся — ключи статичны и HWID к ним не относится.
        code = save_link('keys', content, message.from_user.id, name=None)
        short_url = build_short_url(code)
        safe_reply_to(message, with_footer(f"✅ Ключи сохранены!\n\nСсылка:\n{short_url}"))
    except Exception as e:
        print(f"[ERROR] process_addkeys: {e}")
        import traceback
        traceback.print_exc()
        try:
            safe_reply_to(message, with_footer(f"Ошибка: {e}"))
        except Exception:
            pass

# ==================== ОБРАБОТЧИК ТЕКСТА ====================

@bot.message_handler(content_types=['text'])
def handle_text(message):
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
            safe_reply_to(message, with_footer("Загружаю подписку как клиент Happ..."))
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
                            caption=with_footer("Ключи не найдены. Вот что вернул сервер (сырые данные).")
                        )
                except Exception as e:
                    safe_reply_to(message, with_footer(f"Ошибка при создании файла: {e}"))
                finally:
                    if os.path.exists(file_name):
                        os.remove(file_name)

        else:
            safe_reply_to(message, with_footer("Неверный формат. Используйте /shorten, /addkeys или отправьте ссылку на подписку."))
    except Exception as e:
        print(f"[ERROR] handle_text: {e}")
        import traceback
        traceback.print_exc()
        try:
            safe_reply_to(message, with_footer(f"Ошибка: {e}"))
        except Exception:
            pass

# ==================== ЗАПУСК ====================

if __name__ == '__main__':
    import traceback
    try:
        print("[START] Запуск бота...")
        conn = get_db()
        conn.close()
        print("[START] База данных инициализирована")
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
