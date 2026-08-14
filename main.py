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

from flask import Flask, abort, Response, request

# ==================== КОНФИГУРАЦИЯ ====================

TOKEN = os.getenv('TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')
FOOTER_TAG = os.getenv('FOOTER_TAG', '')
FILE_PREFIX = os.getenv('FILE_PREFIX', 'keys')
BASE_URL = os.getenv('BASE_URL')
PORT = int(os.getenv('PORT', '3000'))
DATA_DIR = os.getenv('DATA_DIR', '/app/data')

# Секретный ключ для HWID bypass (установи в переменных окружения)
BYPASS_SECRET = os.getenv('BYPASS_SECRET', 'default_secret_change_me')

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

# ==================== HWID BYPASS СИСТЕМА ====================

def generate_bypass_payload(code: str, hwid: str = None) -> str:
    """
    Генерирует payload для обхода HWID.
    Формат: base64(code:timestamp:signature)
    Signature = HMAC-SHA256(code + timestamp + hwid, secret_key)
    """
    timestamp = str(int(time.time()))
    hwid_part = hwid or "universal"
    
    # Создаём подпись
    message = f"{code}:{timestamp}:{hwid_part}"
    signature = hmac.new(
        BYPASS_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()[:16]  # Берём первые 16 символов
    
    # Собираем payload
    payload_data = f"{code}:{timestamp}:{signature}"
    payload = base64.urlsafe_b64encode(payload_data.encode()).decode().rstrip('=')
    
    return payload

def verify_bypass_payload(payload: str, max_age: int = 3600) -> tuple:
    """
    Проверяет payload и возвращает (code, valid).
    max_age - максимальный возраст токена в секундах (по умолчанию 1 час)
    """
    try:
        # Добавляем padding если нужно
        padding = '=' * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode()
        
        parts = decoded.split(':')
        if len(parts) != 3:
            return None, False
        
        code, timestamp, signature = parts
        
        # Проверяем возраст токена
        token_age = int(time.time()) - int(timestamp)
        if token_age > max_age:
            return None, False
        
        # Проверяем подпись (пробуем с universal HWID)
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
    """
    Создаёт URL с HWID bypass по аналогии с wepogp.gay
    Формат: /bypass-hwid-lock-{code}?payload={payload}
    """
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
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                'INSERT INTO links (code, type, content, owner_id, hwid_bypass) VALUES (?, ?, ?, ?, ?)',
                (code, link_type, content, owner_id, 1 if hwid_bypass else 0)
            )
            conn.commit()
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
    """
    Обработка HWID bypass ссылок.
    Формат: /bypass-hwid-lock-ABC123?payload=xyz
    """
    payload = request.args.get('payload')
    
    if not payload:
        abort(400, "Missing payload parameter")
    
    # Проверяем payload
    verified_code, valid = verify_bypass_payload(payload)
    
    if not valid or verified_code != code:
        abort(403, "Invalid or expired bypass token")
    
    # Получаем ссылку
    row = get_link(code)
    if not row:
        abort(404)
    
    link_type, content, hwid_bypass = row
    
    # Проверяем что у ссылки включен HWID bypass
    if not hwid_bypass:
        abort(403, "HWID bypass not enabled for this link")
    
    # Возвращаем контент с заголовком обхода
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

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БОТА ====================

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

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    welcome_text = (
        "Здравствуйте.\n\n"
        "• Отправьте ссылку **happ://crypt...**, чтобы расшифровать её в URL.\n"
        "• Отправьте обычную ссылку (**http://...** или **https://...**), чтобы скачать подписку и достать из неё VPN-ключи.\n"
        "• Команда /addkeys — чтобы сохранить свои ключи и получить короткую ссылку на них.\n"
        "• Команда /shorten — чтобы сократить любую ссылку (оригинал будет скрыт).\n"
        "• Команда /profile — посмотреть, изменить или удалить свои сохранённые ссылки.\n\n"
        "🔓 **HWID Bypass**: При создании ссылок через /addkeys или /shorten можно включить обход HWID-блокировки."
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
        with_footer(
            "Пришлите ключи, которые нужно сохранить (можно несколько, каждый с новой строки "
            "или через пробел), одним сообщением."
        )
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
            safe_reply_to(message, with_footer(
                "Не нашёл ни одного ключа в сообщении. Попробуйте ещё раз через /addkeys."
            ))
            return

        content = "\n".join(keys)
        
        # Спрашиваем про HWID bypass
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(
            telebot.types.InlineKeyboardButton("Да 🔓", callback_data=f"hwid_yes:{content}"),
            telebot.types.InlineKeyboardButton("Нет", callback_data=f"hwid_no:{content}")
        )
        
        safe_reply_to(
            message,
            with_footer(f"Найдено ключей: {len(keys)}\n\nВключить HWID bypass для этой ссылки?"),
            reply_markup=markup
        )
        
    except Exception as e:
        print(f"[ОШИБКА в process_addkeys]: {e}")
        try:
            safe_reply_to(message, with_footer(f"Произошла ошибка при сохранении: {e}"))
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("hwid_"))
def handle_hwid_choice(call):
    try:
        choice, content = call.data.split(":", 1)
        hwid_bypass = choice == "hwid_yes"
        
        code = save_link('keys', content, call.from_user.id, hwid_bypass)
        short_url = build_short_url(code)
        
        response_text = f"Сохранено!\n\nОбычная ссылка:\n{short_url}"
        
        if hwid_bypass:
            bypass_url = build_bypass_url(code)
            response_text += f"\n\n🔓 HWID Bypass ссылка:\n{bypass_url}\n\n⚡️ Bypass ссылка обходит блокировки по железу (работает 1 час с момента создания)"
        
        bot.answer_callback_query(call.id)
        safe_send_message(call.message.chat.id, with_footer(response_text))
        
    except Exception as e:
        print(f"[ОШИБКА в handle_hwid_choice]: {e}")
        bot.answer_callback_query(call.id, "Ошибка сохранения", show_alert=True)

@bot.message_handler(commands=['shorten'])
def cmd_shorten(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        message.text = parts[1].strip()
        process_shorten(message)
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
            safe_reply_to(message, with_footer(
                "Это не похоже на ссылку. Попробуйте ещё раз через /shorten."
            ))
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

# ==================== ПРОФИЛЬ ====================

def build_profile_menu(rows: list) -> telebot.types.InlineKeyboardMarkup:
    markup = telebot.types.InlineKeyboardMarkup()
    for code, link_type, content, created_at, hwid_bypass in rows:
        icon = "🔗" if link_type == 'url' else "🔑"
        if hwid_bypass:
            icon += "🔓"
        markup.add(
            telebot.types.InlineKeyboardButton(
                text=f"{icon}  /{code}",
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
        safe_reply_to(message, with_footer(
            "У вас пока нет сохранённых ссылок. Создайте их через /shorten или /addkeys."
        ))
        return

    markup = build_profile_menu(rows)
    safe_reply_to(
        message,
        with_footer(f"Ваши ссылки ({len(rows)}). 🔓 = HWID bypass включен.\n\nНажмите на любую, чтобы посмотреть детали:"),
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("view:"))
def handle_view_link(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)

    if not row or row[2] != call.from_user.id:
        bot.answer_callback_query(call.id, "Ссылка не найдена или не ваша.", show_alert=True)
        return

    link_type, content, _, hwid_bypass = row
    short_url = build_short_url(code)
    type_label = "🔗 Ссылка" if link_type == 'url' else "🔑 Ключи"
    preview = content if len(content) <= 200 else content[:197] + "..."

    text = f"{type_label}\n{short_url}\n\n{preview}"
    
    if hwid_bypass:
        bypass_url = build_bypass_url(code)
        text += f"\n\n🔓 HWID Bypass:\n{bypass_url}"

    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{code}"),
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
            with_footer(f"Ваши ссылки ({len(rows)}). 🔓 = HWID bypass включен:"),
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
        bot.answer_callback_query(call.id, "Не удалось удалить.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit:"))
def handle_edit_link(call):
    code = call.data.split(":", 1)[1]
    row = get_link_full(code)

    if not row or row[2] != call.from_user.id:
        bot.answer_callback_query(call.id, "Ссылка не найдена или не ваша.", show_alert=True)
        return

    link_type = row[0]
    bot.answer_callback_query(call.id)

    if link_type == 'url':
        prompt = f"Пришлите новую ссылку для {build_short_url(code)}:"
    else:
        prompt = f"Пришлите новые ключи для {build_short_url(code)}:"

    msg = safe_send_message(call.message.chat.id, with_footer(prompt))
    bot.register_next_step_handler(msg, process_edit_link, code, link_type, call.from_user.id)

def process_edit_link(message, code, link_type, owner_id):
    try:
        if message.from_user.id != owner_id:
            return

        new_text = (message.text or "").strip()

        if link_type == 'url':
            if not (new_text.startswith("http://") or new_text.startswith("https://")):
                safe_reply_to(message, with_footer("Это не похоже на ссылку. Изменения отменены."))
                return
            new_content = new_text
        else:
            keys = extract_keys(new_text)
            if not keys:
                safe_reply_to(message, with_footer("Не нашёл ключей. Изменения отменены."))
                return
            new_content = "\n".join(keys)

        success = update_link_content(code, owner_id, new_content)

        if success:
            safe_reply_to(message, with_footer(f"Обновлено!\n{build_short_url(code)}"))
        else:
            safe_reply_to(message, with_footer("Не удалось обновить."))
    except Exception as e:
        print(f"[ОШИБКА в process_edit_link]: {e}")
        try:
            safe_reply_to(message, with_footer(f"Ошибка: {e}"))
        except Exception:
            pass

# ==================== ОСНОВНОЙ ОБРАБОТЧИК ====================

@bot.message_handler(content_types=['text'])
def handle_text(message):
    try:
        handle_text_inner(message)
    except Exception as e:
        print(f"[ОШИБКА в handle_text]: {e}")
        try:
            safe_reply_to(message, with_footer(f"Ошибка: {e}"))
        except Exception:
            pass

def handle_text_inner(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    text = message.text.strip()

    if text.startswith("happ://crypt"):
        safe_reply_to(message, with_footer("Обрабатываю happ-ссылку..."))
        decrypted_url = decrypt_happ_link(text)
        if decrypted_url:
            safe_reply_to(message, with_footer(f"Расшифровано:\n\n{decrypted_url}"))
        else:
            safe_reply_to(message, with_footer("Не удалось расшифровать."))

    elif text.startswith("http://") or text.startswith("https://"):
        safe_reply_to(message, with_footer("Загружаю подписку..."))
        configs_text = fetch_and_decode_configs(text)

        if configs_text.startswith("Сетевая ошибка") or configs_text.startswith("Внутренняя ошибка"):
            safe_reply_to(message, with_footer(configs_text))
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
                        caption=with_footer("Ключи не найдены. Файл с содержимым во вложении.")
                    )
            except Exception as e:
                safe_reply_to(message, with_footer(f"Ошибка: {e}"))
            finally:
                if os.path.exists(file_name):
                    os.remove(file_name)

    else:
        safe_reply_to(message, with_footer(
            "Неверный формат. Используйте /addkeys или /shorten."
        ), parse_mode="Markdown")

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
