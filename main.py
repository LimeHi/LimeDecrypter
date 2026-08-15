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
import gzip
import io

from flask import Flask, abort, Response

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

telebot.apihelper.READ_TIMEOUT = 20
telebot.apihelper.CONNECT_TIMEOUT = 15

# ==================== БЕЗОПАСНАЯ ОТПРАВКА ====================

def safe_send_message(chat_id, text, retries=3, delay=2, **kwargs):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            last_error = e
            time.sleep(delay)
        except Exception as e:
            last_error = e
            break
    if last_error:
        print(f"[Ошибка send_message]: {last_error}")

def safe_send_document(chat_id, document, retries=3, delay=2, **kwargs):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return bot.send_document(chat_id, document, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            last_error = e
            time.sleep(delay)
            if hasattr(document, 'seek'):
                try:
                    document.seek(0)
                except Exception:
                    pass
        except Exception as e:
            last_error = e
            break
    if last_error:
        print(f"[Ошибка send_document]: {last_error}")

def safe_reply_to(message, text, retries=3, delay=2, **kwargs):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return bot.reply_to(message, text, **kwargs)
        except requests.exceptions.ReadTimeout as e:
            last_error = e
            time.sleep(delay)
        except Exception as e:
            last_error = e
            break
    if last_error:
        print(f"[Ошибка reply_to]: {last_error}")

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
        conn.execute('ALTER TABLE links ADD COLUMN name TEXT DEFAULT NULL')
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

# ==================== ЭМУЛЯЦИЯ HAPP ====================

def fetch_and_decode_configs(url: str, hwid: str = None) -> str:
    """Эмулирует Happ клиент с нужным HWID и скачивает подписку"""
    actual_hwid = hwid if hwid else str(random.randint(100000000000000000, 999999999999999999))
    
    HAPP_HEADERS = {
        "User-Agent": f"Happ/3.26.3/Android/{actual_hwid}",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }

    def fetch(u, headers=None):
        r = requests.get(u, headers=headers or HAPP_HEADERS, timeout=10)
        r.raise_for_status()
        
        if r.headers.get('Content-Encoding') == 'gzip':
            try:
                gz = gzip.GzipFile(fileobj=io.BytesIO(r.content))
                return r, gz.read().decode('utf-8')
            except:
                pass
        return r, r.text

    print(f"[HTTP] Скачивание конфигов: {url} (HWID: {actual_hwid})")

    try:
        resp, body = fetch(url)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        print(f"[HTTP-ОШИБКА] Таймаут или недоступно: {e}")
        return "СЕТЕВАЯ_ОШИБКА"
    except Exception as e:
        print(f"[HTTP-ОШИБКА] Отклонено: {e}")
        body = ""

    if 'resp' in locals():
        ct = resp.headers.get("Content-Type", "")
        if not is_html(ct, body) and body:
            return try_decode_base64(body)
            
        if is_html(ct, body):
            match = re.search(r'(https?://[^\s"\'<>]+\.(?:txt|json|xml|conf|cfg|sub))', body, re.IGNORECASE)
            if match:
                try:
                    resp2, body2 = fetch(match.group(1))
                    if not is_html(resp2.headers.get('Content-Type', ''), body2):
                        return try_decode_base64(body2)
                except Exception:
                    pass

    base = url.rstrip("/")
    candidates = [
        f"{base}/sub",
        f"{base}?format=clash",
        f"{base}?app=happ"
    ]
    
    for alt_url in candidates:
        try:
            r, body_alt = fetch(alt_url)
            ct_alt = r.headers.get("Content-Type", "")
            if not is_html(ct_alt, body_alt) and body_alt:
                return try_decode_base64(body_alt)
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
    print(f"\n[GET] Запрошена подписка /{code}")
    row = get_link_full(code)
    
    if not row:
        return Response("Error 404: Subscription not found.", status=404, mimetype='text/plain')

    link_type, content, owner_id, name, hwid = row

    if link_type == 'url':
        if code in subscription_cache:
            cached_time, cached_data = subscription_cache[code]
            if time.time() - cached_time < CACHE_TTL:
                return Response(cached_data, mimetype='text/plain')

        try:
            configs_text = fetch_and_decode_configs(content, hwid)
            if configs_text == "СЕТЕВАЯ_ОШИБКА":
                return Response("Error 502: Original server is unreachable.", status=502, mimetype='text/plain')

            keys = extract_keys(configs_text)
            if keys:
                joined_keys = "\n".join(keys)
                encoded_sub = base64.b64encode(joined_keys.encode('utf-8')).decode('utf-8')
                subscription_cache[code] = (time.time(), encoded_sub)
                return Response(encoded_sub, mimetype='text/plain')
            else:
                return Response("Error 502: No VPN keys found.", status=502, mimetype='text/plain')
        except Exception as e:
            print(f"[ОШИБКА проксирования {code}]: {e}")
            return Response("Error 500: Internal server error.", status=500, mimetype='text/plain')
    else:
        # Статические ключи
        encoded_sub = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        return Response(encoded_sub, mimetype='text/plain')

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
        print(f"[ОШИБКА проверки подписки]: {e}")
        sub_cache[user_id] = {'status': False, 'time': now - 270} 
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
    if call.from_user.id in sub_cache:
        del sub_cache[call.from_user.id]

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

# --- Конвертеры XRAY ---
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

    if not (address and port and uid): return None
    network = stream.get('network', 'tcp')
    security = stream.get('security', 'none')

    params = {'encryption': encryption or 'none', 'security': security, 'type': network}
    if flow: params['flow'] = flow

    tls = stream.get('tlsSettings') or stream.get('realitySettings') or {}
    if tls.get('serverName'): params['sni'] = tls['serverName']
    if tls.get('fingerprint'): params['fp'] = tls['fingerprint']
    if tls.get('alpn'): params['alpn'] = ','.join(tls['alpn'])

    if network == 'ws':
        ws = stream.get('wsSettings', {}) or {}
        if ws.get('path'): params['path'] = ws['path']
        host = (ws.get('headers') or {}).get('Host') or ws.get('host')
        if host: params['host'] = host
    elif network == 'grpc':
        grpc = stream.get('grpcSettings', {}) or {}
        if grpc.get('serviceName'): params['serviceName'] = grpc['serviceName']

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

    if not (address and port and auth): return None
    scheme = 'hysteria2' if version == 2 else 'hysteria'
    params = {}
    if tls.get('serverName'): params['sni'] = tls['serverName']
    if tls.get('alpn'): params['alpn'] = ','.join(tls['alpn'])

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

    if not (address and port and password): return None
    security = stream.get('security', 'tls')
    params = {'security': security}
    tls = stream.get('tlsSettings', {}) or {}
    if tls.get('serverName'): params['sni'] = tls['serverName']

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
    try: data = json.loads(text)
    except Exception: return []

    configs = data if isinstance(data, list) else [data]
    links = []

    for cfg in configs:
        if not isinstance(cfg, dict): continue
        outbounds = cfg.get('outbounds')
        if not isinstance(outbounds, list): continue
        remarks = cfg.get('remarks', '')
        
        for ob in outbounds:
            if not isinstance(ob, dict): continue
            proto = ob.get('protocol')
            if not proto or proto in _NON_PROXY_PROTOCOLS: continue
            converter = _OUTBOUND_CONVERTERS.get(proto)
            if not converter: continue
            try: uri = converter(ob, remarks)
            except Exception: uri = None
            if uri: links.append(uri)
    return links

def extract_keys(text: str) -> list:
    keys = KEY_PATTERN.findall(text)
    if keys: return keys
    return convert_xray_json_to_links(text)

def send_keys(chat_id: int, keys: list):
    joined = "\n".join(keys)
    file_name = next_file_name()
    try:
        with open(file_name, 'w', encoding='utf-8') as file:
            file.write(joined)
        with open(file_name, 'rb') as file:
            safe_send_document(
                chat_id, file, visible_file_name=file_name,
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
        "• Отправьте ссылку <b>happ://crypt...</b>, чтобы расшифровать её в URL.\n"
        "• Отправьте обычную ссылку (<b>http/https</b>), чтобы проверить её и достать ключи.\n"
        "• Команда /addkeys — сохранить ключи (без проксирования).\n"
        "• Команда /shorten — создать динамическую прокси-подписку (с возможностью задать HWID).\n"
        "• Команда /profile — управлять сохранёнными ссылками."
    )
    safe_reply_to(message, with_footer(welcome_text), parse_mode="HTML")

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

    msg = safe_reply_to(message, with_footer("Пришлите ключи, которые нужно сохранить (можно несколько, каждый с новой строки)."))
    bot.register_next_step_handler(msg, process_addkeys)

def process_addkeys(message):
    try:
        if not is_subscribed(message.from_user.id):
            send_subscribe_prompt(message.chat.id)
            return

        text = (message.text or "").strip()
        keys = extract_keys(text)

        if not keys:
            safe_reply_to(message, with_footer("Не нашёл ни одного ключа в сообщении. Попробуйте ещё раз через /addkeys."))
            return

        content = "\n".join(keys)
        code = save_link('keys', content, message.from_user.id)
        short_url = build_short_url(code)

        safe_reply_to(message, with_footer(f"✅ Сохранено ключей: {len(keys)}\n\nВаша короткая ссылка:\n{short_url}"))
    except Exception as e:
        safe_reply_to(message, with_footer(f"Произошла ошибка при сохранении: {e}"))

# --- Сохранение стейта для генерации подписки ---
user_data_lock = threading.Lock()
user_data = {}

@bot.message_handler(commands=['shorten'])
def cmd_shorten(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        message.text = parts[1].strip()
        process_shorten_url(message)
        return

    msg = safe_reply_to(message, with_footer("Пришлите ссылку (http:// или https://), которую нужно сделать прокси-подпиской."))
    bot.register_next_step_handler(msg, process_shorten_url)

def process_shorten_url(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return
    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        safe_reply_to(message, with_footer("Это не похоже на ссылку. Начните заново через /shorten."))
        return
    with user_data_lock:
        user_data[message.from_user.id] = {'url': url}
    msg = safe_reply_to(message, with_footer("Введите название для этой подписки (будет видно в профиле):"))
    bot.register_next_step_handler(msg, process_shorten_name)

def process_shorten_name(message):
    user_id = message.from_user.id
    with user_data_lock:
        if user_id not in user_data: return
        user_data[user_id]['name'] = (message.text or "").strip() or "Без названия"
    msg = safe_reply_to(message, with_footer("Введите HWID для эмуляции клиента Happ (отправьте 0, если хотите сгенерировать случайный стандартный):"))
    bot.register_next_step_handler(msg, process_shorten_hwid)

def process_shorten_hwid(message):
    user_id = message.from_user.id
    with user_data_lock:
        data = user_data.get(user_id)
        if not data: return
        hwid_input = (message.text or "").strip()
        data['hwid'] = str(random.randint(100000000000000000, 999999999999999999)) if hwid_input == '0' else hwid_input

    code = save_link('url', data['url'], user_id, data['name'], data['hwid'])
    short_url = build_short_url(code)
    
    response_text = f"✅ <b>Динамическая прокси-подписка создана!</b>\n\n📌 Название: {data['name']}\n🆔 HWID: {data['hwid']}\n🔗 Ссылка: {short_url}\n\n<i>Эта ссылка будет автоматически перехватывать и обновлять ключи из оригинала.</i>"
    safe_reply_to(message, with_footer(response_text), parse_mode="HTML")

# ==================== ПРОФИЛЬ ====================

def build_profile_menu(rows: list) -> telebot.types.InlineKeyboardMarkup:
    markup = telebot.types.InlineKeyboardMarkup()
    for code, link_type, content, created_at, name, hwid in rows:
        icon = "🔄" if link_type == 'url' else "🔑"
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
        safe_reply_to(message, with_footer("У вас пока нет сохранённых ссылок. Создайте их через /shorten или /addkeys."))
        return

    markup = build_profile_menu(rows)
    safe_reply_to(message, with_footer(f"Ваши подписки ({len(rows)}). Нажмите на любую для деталей:"), reply_markup=markup)

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
    
    text = f"<b>{type_label}</b>\n\n📌 Название: {name or '—'}{hwid_text}\n🔗 {short_url}\n\n📄 <b>Содержимое/Оригинал:</b>\n{preview}"

    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✏️ Изменить оригинал", callback_data=f"edit:{code}"),
        telebot.types.InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{code}")
    )
    markup.add(telebot.types.InlineKeyboardButton("‹ Назад к списку", callback_data="profile_back"))

    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(with_footer(text), call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    except Exception:
        safe_send_message(call.message.chat.id, with_footer(text), reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "profile_back")
def handle_profile_back(call):
    rows = get_links_by_owner(call.from_user.id)
    bot.answer_callback_query(call.id)

    if not rows:
        try:
            bot.edit_message_text(with_footer("У вас больше нет сохранённых ссылок."), call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    markup = build_profile_menu(rows)
    try:
        bot.edit_message_text(with_footer(f"Ваши ссылки ({len(rows)}):"), call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("del:"))
def handle_delete_link(call):
    code = call.data.split(":", 1)[1]
    if delete_link(code, call.from_user.id):
        bot.answer_callback_query(call.id, "Удалено ✅")
        handle_profile_back(call)
    else:
        bot.answer_callback_query(call.id, "Ошибка удаления.", show_alert=True)

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
        prompt = f"Пришлите новую ссылку (вместо старой) для {build_short_url(code)}:"
    else:
        prompt = f"Пришлите новые ключи (вместо старых) для {build_short_url(code)}:"

    msg = safe_send_message(call.message.chat.id, with_footer(prompt))
    bot.register_next_step_handler(msg, process_edit_link, code, link_type, call.from_user.id)

def process_edit_link(message, code, link_type, owner_id):
    if message.from_user.id != owner_id: return
    new_text = (message.text or "").strip()

    if link_type == 'url':
        if not (new_text.startswith("http://") or new_text.startswith("https://")):
            safe_reply_to(message, with_footer("Это не похоже на ссылку. Изменения отменены."))
            return
        new_content = new_text
    else:
        keys = extract_keys(new_text)
        if not keys:
            safe_reply_to(message, with_footer("Не нашёл ни одного ключа. Изменения отменены."))
            return
        new_content = "\n".join(keys)

    if update_link_content(code, owner_id, new_content):
        safe_reply_to(message, with_footer(f"Обновлено!\n{build_short_url(code)}"))
    else:
        safe_reply_to(message, with_footer("Не удалось обновить — ссылка больше не существует."))

# ==================== ОСНОВНОЙ ОБРАБОТЧИК ТЕКСТА ====================

@bot.message_handler(content_types=['text'])
def handle_text(message):
    try:
        handle_text_inner(message)
    except Exception as e:
        print(f"[ОШИБКА в handle_text]: {e}")
        try: safe_reply_to(message, with_footer(f"Произошла ошибка при обработке: {e}"))
        except: pass

def handle_text_inner(message):
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
        safe_reply_to(message, with_footer("Проверяю подписку и извлекаю ключи..."))
        configs_text = fetch_and_decode_configs(text)

        if configs_text == "СЕТЕВАЯ_ОШИБКА":
            safe_reply_to(message, with_footer("Сетевая ошибка при скачивании подписки (Недоступно/Таймаут)."))
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
                        message.chat.id, file, visible_file_name=file_name,
                        caption=with_footer("VPN-ключи не найдены. Файл с сырым содержимым прикреплен.")
                    )
            except Exception as e:
                safe_reply_to(message, with_footer(f"Ошибка при создании файла: {e}"))
            finally:
                if os.path.exists(file_name): os.remove(file_name)
    else:
        safe_reply_to(message, with_footer(
            "Неверный формат. Отправьте ссылку **happ://crypt...**, обычную ссылку на подписку, "
            "или используйте /addkeys или /shorten."
        ), parse_mode="Markdown")

# ==================== ЗАПУСК ====================

def run_bot_polling():
    print("[START] Запуск polling Telegram бота...")
    bot.infinity_polling(timeout=60, logger_level=None)

if __name__ == '__main__':
    import traceback
    try:
        print("[START] Инициализация базы данных...")
        conn = get_db()
        conn.close()

        # Удаляем старый вебхук без залипаний
        try:
            bot.delete_webhook(drop_pending_updates=True, timeout=5)
        except Exception as e:
            print(f"[START] Вебхук очищен с ошибкой (игнорируем): {e}")

        # Запускаем бота в фоне
        bot_thread = threading.Thread(target=run_bot_polling, daemon=True)
        bot_thread.start()

        # Запускаем Flask в главном потоке
        print(f"[START] Запуск HTTP-сервера на порту {PORT}...")
        app.run(host='0.0.0.0', port=PORT, threaded=True, use_reloader=False)
    except Exception:
        traceback.print_exc()
        input("\nНажмите Enter, чтобы закрыть окно...")
