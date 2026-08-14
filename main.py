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

# ==================== HWID BYPASS (без ограничения по времени) ====================

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

def verify_bypass_payload(payload: str) -> tuple:
    try:
        padding = '=' * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode()
        parts = decoded.split(':')
        if len(parts) != 3:
            return None, False
        code, timestamp, signature = parts
        # Проверяем подпись без учёта времени (вечный токен)
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

def generate_random_hwid(length: int = 8) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Миграция для колонок, которых может не быть
    for col in ('hwid_bypass', 'hwid', 'name', 'description'):
        try:
            conn.execute(f'SELECT {col} FROM links LIMIT 1')
        except sqlite3.OperationalError:
            print(f"[MIGRATION] Добавляем колонку {col}")
            conn.execute(f'ALTER TABLE links ADD COLUMN {col} TEXT DEFAULT NULL' if col != 'hwid_bypass' else 
                         f'ALTER TABLE links ADD COLUMN {col} BOOLEAN DEFAULT 0')
            conn.commit()
            print(f"[MIGRATION] Колонка {col} добавлена")
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

def save_link(link_type: str, content: str, owner_id: int, 
              hwid_bypass: bool = False, hwid: str = None,
              name: str = None, description: str = None) -> str:
    code = generate_code()
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                '''INSERT INTO links 
                   (code, type, content, owner_id, hwid_bypass, hwid, name, description)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (code, link_type, content, owner_id, 1 if hwid_bypass else 0, hwid, name, description)
            )
            conn.commit()
        finally:
            conn.close()
    return code

def get_link(code: str):
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                'SELECT type, content, hwid_bypass, hwid, name, description FROM links WHERE code = ?',
                (code,)
            ).fetchone()
            return row
        finally:
            conn.close()

def get_link_full(code: str):
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                'SELECT type, content, owner_id, hwid_bypass, hwid, name, description FROM links WHERE code = ?',
                (code,)
            ).fetchone()
            return row
        finally:
            conn.close()

def get_links_by_owner(owner_id: int, limit: int = 20):
    with db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                '''SELECT code, type, content, created_at, hwid_bypass, hwid, name, description 
                   FROM links WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?''',
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

    link_type, content, hwid_bypass, hwid, name, description = row

    if hwid:
        request_hwid = request.args.get('hwid') or request.args.get('payload')
        if not request_hwid or request_hwid != hwid:
            abort(403, "HWID required or mismatch")

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

@app.route('/bypass-hwid-lock-<code>')
def bypass_hwid_link(code):
    payload = request.args.get('payload')
    if not payload:
        abort(400, "Missing payload parameter")
    verified_code, valid = verify_bypass_payload(payload)
    if not valid or verified_code != code:
        abort(403, "Invalid bypass token")
    row = get_link(code)
    if not row:
        abort(404)
    link_type, content, hwid_bypass, hwid, name, description = row
    if not hwid_bypass:
        abort(403, "HWID bypass not enabled for this link")
    headers = {'X-HWID-Bypass': 'enabled', 'X-Bypass-Token': payload}
    if link_type == 'url':
        try:
            resp = requests.get(content, timeout=15)
            resp.raise_for_status()
            return Response(resp.content, mimetype=resp.headers.get('Content-Type', 'text/plain'), headers=headers)
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

def fetch_and_decode_configs(url: str) -> str:
    VPN_HEADERS = {
        "User-Agent": "clash.meta",
        "Accept": "text/plain, application/json, */*",
    }

    def fetch(u, headers=None):
        r = requests.get(u, headers=headers or {}, timeout=15)
        r.raise_for_status()
        return r

    try:
        resp = fetch(url)
        ct = resp.headers.get("Content-Type", "")
        body = resp.text.strip()

        if not is_html(ct, body):
            return try_decode_base64(body)

        resp2 = fetch(url, VPN_HEADERS)
        ct2 = resp2.headers.get("Content-Type", "")
        body2 = resp2.text.strip()

        if not is_html(ct2, body2):
            return try_decode_base64(body2)

        base = url.rstrip("/")
        token = base.split("/")[-1]
        origin = "/".join(base.split("/")[:-1])

        sub_candidates = [
            f"{origin}/sub/{token}",
            f"{base}/sub",
            f"{base}?format=clash",
            f"{base}?app=happ",
        ]

        for alt_url in sub_candidates:
            try:
                r = fetch(alt_url, VPN_HEADERS)
                ct_alt = r.headers.get("Content-Type", "")
                body_alt = r.text.strip()
                if not is_html(ct_alt, body_alt) and body_alt:
                    return try_decode_base64(body_alt)
            except Exception:
                continue

        return body2

    except requests.exceptions.RequestException as e:
        return f"Сетевая ошибка при скачивании подписки: {e}"
    except Exception as e:
        return f"Внутренняя ошибка обработки: {e}"

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
        "• /shorten — сократить ссылку на подписку (можно добавить название VPN и описание, а также включить HWID-защиту).\n"
        "• /addkeys — сохранить VPN-ключи и получить короткую ссылку.\n"
        "• /profile — посмотреть свои сохранённые ссылки."
    )
    safe_reply_to(message, with_footer(welcome_text))

# ==================== /shorten (с запросом названия, описания и HWID) ====================

# Словарь для хранения временных данных пользователя (url, name, description)
user_data = {}
user_data_lock = threading.Lock()

@bot.message_handler(commands=['shorten'])
def cmd_shorten(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        # Если сразу передан URL, сохраняем его и переходим к запросу имени
        url = parts[1].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            safe_reply_to(message, with_footer("Это не похоже на ссылку. Используйте /shorten <URL> или отправьте ссылку отдельно."))
            return
        # Сохраняем URL в словарь
        with user_data_lock:
            user_data[message.from_user.id] = {'url': url}
        # Спрашиваем название
        msg = safe_reply_to(message, with_footer("Введите название VPN (будет отображаться в профиле):"))
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
        safe_reply_to(message, with_footer("Это не похоже на ссылку. Попробуйте ещё раз через /shorten."))
        return
    with user_data_lock:
        user_data[message.from_user.id] = {'url': url}
    msg = safe_reply_to(message, with_footer("Введите название VPN (будет отображаться в профиле):"))
    bot.register_next_step_handler(msg, process_name)

def process_name(message):
    user_id = message.from_user.id
    with user_data_lock:
        data = user_data.get(user_id)
        if not data:
            safe_reply_to(message, with_footer("Сессия истекла. Начните заново через /shorten."))
            return
        data['name'] = (message.text or "").strip() or "Без названия"
    msg = safe_reply_to(message, with_footer("Введите описание (или отправьте '-' чтобы пропустить):"))
    bot.register_next_step_handler(msg, process_description)

def process_description(message):
    user_id = message.from_user.id
    with user_data_lock:
        data = user_data.get(user_id)
        if not data:
            safe_reply_to(message, with_footer("Сессия истекла. Начните заново через /shorten."))
            return
        desc = (message.text or "").strip()
        data['description'] = desc if desc != '-' else ''
    # Теперь спрашиваем про HWID-защиту
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("Да 🔓", callback_data=f"shorten_hwid_yes:{user_id}"),
        telebot.types.InlineKeyboardButton("Нет", callback_data=f"shorten_hwid_no:{user_id}")
    )
    safe_reply_to(
        message,
        with_footer("Включить HWID-защиту для этой ссылки?\n(При 'Да' будет создана вечная bypass-ссылка)"),
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("shorten_hwid_"))
def handle_shorten_hwid_choice(call):
    try:
        choice, user_id_str = call.data.split(":", 1)
        user_id = int(user_id_str)
        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "Это не ваша сессия.", show_alert=True)
            return
        hwid_bypass = choice == "shorten_hwid_yes"
        with user_data_lock:
            data = user_data.pop(user_id, None)
        if not data:
            bot.answer_callback_query(call.id, "Сессия истекла. Попробуйте заново через /shorten.", show_alert=True)
            return
        url = data['url']
        name = data.get('name', '')
        description = data.get('description', '')
        if hwid_bypass:
            hwid = generate_random_hwid()
        else:
            hwid = None
        code = save_link('url', url, user_id, hwid_bypass, hwid, name, description)
        short_url = build_short_url(code)
        response_text = f"✅ Ссылка сокращена!\n\n📌 Название: {name}\n📝 Описание: {description}\n🔗 Обычная ссылка:\n{short_url}"
        if hwid:
            response_text += f"\n\n⚠️ Для доступа по обычной ссылке нужен HWID: `{hwid}`\nДобавьте параметр: `?hwid={hwid}`"
            bypass_url = build_bypass_url(code)
            response_text += f"\n\n🔓 **HWID Bypass ссылка** (действует без ограничения по времени):\n{bypass_url}"
        bot.answer_callback_query(call.id, "Готово ✅")
        safe_send_message(call.message.chat.id, with_footer(response_text))
    except Exception as e:
        print(f"[ERROR] handle_shorten_hwid_choice: {e}")
        import traceback
        traceback.print_exc()
        try:
            bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass

# ==================== /addkeys (без HWID) ====================

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
        # Сохраняем без HWID и без bypass
        code = save_link('keys', content, message.from_user.id, hwid_bypass=False, hwid=None, name=None, description=None)
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

# ==================== /profile ====================

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
    for row in rows:
        code, link_type, content, created_at, hwid_bypass, hwid, name, description = row
        icon = "🔗" if link_type == 'url' else "🔑"
        if hwid:
            icon += "🔒"
        if hwid_bypass:
            icon += "🔓"
        short_url = build_short_url(code)
        text += f"{icon} {short_url}"
        if name:
            text += f" – {name}"
        if hwid:
            text += f" (HWID: {hwid})"
        text += "\n"

    safe_reply_to(message, with_footer(text))

# ==================== ОБРАБОТЧИК ТЕКСТА (для happ:// и обычных ссылок) ====================

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
            # Если пользователь просто отправил ссылку без команды, предлагаем сократить через /shorten
            safe_reply_to(message, with_footer("Используйте /shorten для сокращения ссылки с возможностью добавить название и защиту."))

        else:
            safe_reply_to(message, with_footer("Неверный формат. Используйте /shorten или /addkeys."))
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
