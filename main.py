import telebot
import requests
import base64
import os
import re

TOKEN = os.getenv('TOKEN')                       # токен бота от @BotFather
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')  # канал, подписка обязательна, формат: @название
FOOTER_TAG = os.getenv('FOOTER_TAG', '')          # подпись под сообщениями
FILE_PREFIX = os.getenv('FILE_PREFIX', 'keys')    # префикс имени файла с ключами

if not TOKEN:
    raise ValueError("Переменная окружения TOKEN не задана")
if not CHANNEL_USERNAME:
    raise ValueError("Переменная окружения CHANNEL_USERNAME не задана")

bot = telebot.TeleBot(TOKEN)

# Счётчик для нумерации файлов (@LimeVPNFREE_keys1.txt, @LimeVPNFREE_keys2.txt, ...)
file_counter = 0

def next_file_name() -> str:
    global file_counter
    file_counter += 1
    return f"{FILE_PREFIX}{file_counter}.txt"

def with_footer(text: str) -> str:
    return f"{text}\n\n{FOOTER_TAG}"

def is_subscribed(user_id: int) -> bool:
    """Проверяет подписку пользователя на обязательный канал."""
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ('member', 'administrator', 'creator')
    except Exception as e:
        print(f"[ОШИБКА проверки подписки]: {e}")
        # Если не удалось проверить (бот не админ канала и т.п.) — по умолчанию не пускаем
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
    bot.send_message(
        chat_id,
        with_footer(f"Для использования бота необходимо подписаться на канал {CHANNEL_USERNAME}."),
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def handle_check_sub(call):
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Подписка подтверждена ✅")
        bot.send_message(call.message.chat.id, with_footer("Отлично! Теперь можешь пользоваться ботом."))
    else:
        bot.answer_callback_query(call.id, "Подписка не найдена. Подпишись и попробуй снова.", show_alert=True)

# Регулярка для всех популярных VPN-схем ссылок
KEY_PATTERN = re.compile(
    r'(?:vless|vmess|trojan|ss|ssr|hysteria2?|hy2|tuic)://[^\s<>"]+',
    re.IGNORECASE
)

def decrypt_happ_link(encrypted_link: str) -> str:
    """Дешифрует happ ссылку через API."""
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

def fetch_and_decode_configs(url: str) -> str:
    """Скачивает конфигурации по прямой ссылке и декодирует из Base64."""
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        content = response.text.strip()

        # Попытка декодирования Base64 (стандарт для подписок)
        try:
            padded_content = content + '=' * (-len(content) % 4)
            decoded_bytes = base64.b64decode(padded_content)
            return decoded_bytes.decode('utf-8')
        except Exception:
            return content

    except requests.exceptions.RequestException as e:
        return f"Сетевая ошибка при скачивании подписки: {e}"
    except Exception as e:
        return f"Внутренняя ошибка обработки: {e}"

def extract_keys(text: str) -> list:
    """Достаёт все vless/vmess/trojan/ss/hysteria2/tuic ключи из текста."""
    return KEY_PATTERN.findall(text)

def send_keys(chat_id: int, keys: list):
    """Отправляет найденные ключи: текстом, если помещается, иначе файлом."""
    joined = "\n".join(keys)

    # Если ключей много и текст не влезает в лимит Telegram (4096 символов) — шлём файлом
    if len(joined) > 3500:
        file_name = next_file_name()
        try:
            with open(file_name, 'w', encoding='utf-8') as file:
                file.write(joined)
            with open(file_name, 'rb') as file:
                bot.send_document(
                    chat_id,
                    file,
                    visible_file_name=file_name,
                    caption=with_footer(f"Найдено ключей: {len(keys)}. Файл во вложении.")
                )
        finally:
            if os.path.exists(file_name):
                os.remove(file_name)
    else:
        bot.send_message(chat_id, with_footer(f"Найдено ключей: {len(keys)}\n\n{joined}"))

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    welcome_text = (
        "Здравствуйте.\n\n"
        "• Отправьте ссылку **happ://crypt...**, чтобы расшифровать её в URL.\n"
        "• Отправьте обычную ссылку (**http://...** или **https://...**), чтобы скачать подписку и достать из неё VPN-ключи (vless/vmess/trojan/ss/hysteria2/tuic)."
    )
    bot.reply_to(message, with_footer(welcome_text), parse_mode="Markdown")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    try:
        handle_text_inner(message)
    except Exception as e:
        print(f"[ОШИБКА в handle_text]: {e}")
        try:
            bot.reply_to(message, with_footer(f"Произошла ошибка при обработке: {e}"))
        except Exception:
            pass

def handle_text_inner(message):
    if not is_subscribed(message.from_user.id):
        send_subscribe_prompt(message.chat.id)
        return

    text = message.text.strip()

    # Сценарий 1: Пользователь прислал зашифрованную ссылку happ://crypt
    if text.startswith("happ://crypt"):
        bot.reply_to(message, with_footer("Обрабатываю happ-ссылку..."))
        decrypted_url = decrypt_happ_link(text)

        if decrypted_url:
            bot.reply_to(message, with_footer(f"Ссылка успешно расшифрована:\n\n{decrypted_url}"))
        else:
            bot.reply_to(message, with_footer("Не удалось расшифровать ссылку. Проверьте правильность введенных данных."))

    # Сценарий 2: Пользователь прислал обычную ссылку (http/https)
    elif text.startswith("http://") or text.startswith("https://"):
        bot.reply_to(message, with_footer("Загружаю подписку по ссылке и извлекаю конфигурации..."))

        configs_text = fetch_and_decode_configs(text)

        if configs_text.startswith("Сетевая ошибка") or configs_text.startswith("Внутренняя ошибка"):
            bot.reply_to(message, with_footer(configs_text))
            return

        keys = extract_keys(configs_text)

        if keys:
            send_keys(message.chat.id, keys)
        else:
            # Ключи не найдены — как и раньше, отдаём файлом сырой контент
            file_name = next_file_name()
            try:
                with open(file_name, 'w', encoding='utf-8') as file:
                    file.write(configs_text)

                with open(file_name, 'rb') as file:
                    bot.send_document(
                        message.chat.id,
                        file,
                        visible_file_name=file_name,
                        caption=with_footer("VPN-ключи по известным схемам не найдены. Файл с сырым содержимым подписки во вложении.")
                    )
            except Exception as e:
                bot.reply_to(message, with_footer(f"Ошибка при создании файла: {e}"))
            finally:
                if os.path.exists(file_name):
                    os.remove(file_name)

    else:
        bot.reply_to(message, with_footer("Неверный формат. Отправьте либо ссылку **happ://crypt...**, либо обычную ссылку на подписку (**http://...**)."), parse_mode="Markdown")

if __name__ == '__main__':
    import traceback
    try:
        bot.remove_webhook()
        bot.polling(none_stop=True)
    except Exception:
        traceback.print_exc()
    finally:
        input("\nНажмите Enter, чтобы закрыть окно...")
