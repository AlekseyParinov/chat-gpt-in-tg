import logging
import sqlite3
import time
import os
import requests
import threading
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, LabeledPrice, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    filters, PreCheckoutQueryHandler, CallbackQueryHandler
)
from openai import OpenAI
from io import BytesIO

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- Переменные окружения ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
PAYMENT_PROVIDER_TOKEN = os.environ.get("PAYMENT_PROVIDER_TOKEN")
QIWI_API_KEY = os.environ.get("QIWI_API_KEY")  # токен API для Qiwi
QIWI_PHONE = os.environ.get("QIWI_PHONE")     # номер кошелька

CARD_MIR_NUMBER = os.environ.get("CARD_MIR_NUMBER")  # карта Мир
CARD_MIR_AMOUNT = int(os.environ.get("CARD_MIR_AMOUNT", 30))  # сумма перевода в рублях

ADMIN_ID = os.environ.get("ADMIN_ID") # ID администратора
ADMIN_USERNAME = "@adam0v_0" # Username администратора

# Initialize OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# --- База ---
conn = sqlite3.connect("user_contexts.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS contexts (
    user_id TEXT PRIMARY KEY,
    role TEXT,
    history TEXT,
    free_requests INTEGER,
    subscription_end REAL
)
""")
conn.commit()

# --- Хелперы ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return

def run_health_check_server():
    server_address = ('0.0.0.0', 5000)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    print("Health check server started on port 5000")
    httpd.serve_forever()

def get_main_menu():
    keyboard = [
        ["/chat_start", "/image_start"],
        ["/profile", "/history"],
        ["/subscribe", "/help"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_payment_menu():
    keyboard = [
        [InlineKeyboardButton("💳 Карта Мир", callback_data="pay_card")],
        [InlineKeyboardButton("🥝 Qiwi", callback_data="pay_qiwi")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_user_context(user_id):
    cursor.execute("SELECT role, history, free_requests, subscription_end FROM contexts WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if row:
        role, history, free_requests, subscription_end = row
        return role, eval(history), free_requests, subscription_end
    else:
        default_role = "Ты ассистент, который отвечает коротко и логично."
        cursor.execute(
            "INSERT OR REPLACE INTO contexts VALUES (?,?,?,?,?)",
            (user_id, default_role, str([]), 10, 0)
        )
        conn.commit()
        return default_role, [], 10, 0

def save_user_context(user_id, role, history, free_requests, subscription_end):
    cursor.execute(
        "INSERT OR REPLACE INTO contexts VALUES (?,?,?,?,?)",
        (user_id, role, str(history), free_requests, subscription_end)
    )
    conn.commit()

def has_access(user_id):
    # Бесплатный доступ для избранных пользователей
    try:
        user_info = cursor.execute("SELECT username FROM contexts WHERE user_id=?", (user_id,)).fetchone()
        # В этой версии username может не быть в таблице, так что проверим по ID или добавим логику имен
        # Но проще всего добавить список разрешенных username и проверять в вызывающей функции
        pass 
    except:
        pass

    _, _, free_requests, subscription_end = get_user_context(user_id)
    return free_requests > 0 or subscription_end > time.time()

# Список VIP-пользователей
VIP_USERNAMES = ["@adam0v_0", "@zeiszee", "@Leksei_yy", "adam0v_0", "zeiszee", "Leksei_yy"]

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я твой продвинутый AI-помощник.\n"
        "Я могу отвечать на твои вопросы и создавать уникальные изображения.\n\n"
        "Выберите нужное действие в меню ниже:",
        reply_markup=get_main_menu()
    )

async def chat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Просто напишите мне любое сообщение, и я отвечу!", reply_markup=get_main_menu())

async def image_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Используйте команду /image <ваш запрос>, чтобы создать картинку.", reply_markup=get_main_menu())

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    role, history, free_requests, subscription_end = get_user_context(user_id)
    status = "Активна" if subscription_end > time.time() else "Неактивна"
    sub_text = time.strftime('%d.%m.%Y %H:%M', time.localtime(subscription_end)) if subscription_end > 0 else "Нет"
    await update.effective_message.reply_text(
        f"👤 Профиль\n\n"
        f"Ваш ID: {user_id}\n"
        f"Остаток бесплатных запросов: {free_requests}\n"
        f"Подписка: {status}\n"
        f"Дата окончания: {sub_text}",
        reply_markup=get_main_menu()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я могу отвечать на вопросы и генерировать картинки.\n"
        "Воспользуйтесь кнопками меню для навигации.",
        reply_markup=get_main_menu()
    )

async def subscribe_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выберите удобный способ оплаты подписки (30₽/мес):",
        reply_markup=get_payment_menu()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "pay_card":
        await pay_card(update, context)
    elif query.data == "pay_qiwi":
        await pay_qiwi(update, context)
    elif query.data == "pay_telegram":
        await subscribe_telegram(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    username = update.message.from_user.username
    text = update.message.text
    
    # Если сообщение начинается с /, это команда, она обработается CommandHandler
    if text.startswith('/'):
        return

    # Если мы дошли сюда, значит это обычное сообщение для ИИ
    role, history, free_requests, subscription_end = get_user_context(user_id)
    
    # Добавляем инструкцию по форматированию в системную роль
    clean_role = role + " ВАЖНО: Не используй LaTeX-разметку (символы \(, \), \[, \], $, {}). Пиши математические формулы обычным текстом, используя простые символы (^ для степени, * для умножения, / для деления)."

    # Проверка на VIP доступ
    is_vip = username in VIP_USERNAMES if username else False

    if not has_access(user_id) and not is_vip:
        await update.message.reply_text("Первые 10 сообщений закончились. Используй оплату для доступа.", reply_markup=get_main_menu())
        return

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if str(user.id) != ADMIN_ID and user.username != "adam0v_0":
        return
    
    cursor.execute("SELECT COUNT(*) FROM contexts")
    total_users = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM contexts WHERE subscription_end > ?", (time.time(),))
    active_subs = cursor.fetchone()[0]
    
    await update.message.reply_text(
        f"📊 Статистика бота\n\n"
        f"Всего пользователей: {total_users}\n"
        f"Активных подписок: {active_subs}"
    )

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if str(user.id) != ADMIN_ID and user.username != "adam0v_0":
        return
    
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Введите текст рассылки после команды.")
        return
    
    cursor.execute("SELECT user_id FROM contexts")
    users = cursor.fetchall()
    
    count = 0
    for user in users:
        try:
            await context.bot.send_message(chat_id=user[0], text=msg)
            count += 1
        except Exception:
            continue
            
    await update.message.reply_text(f"✅ Рассылка завершена. Отправлено {count} пользователям.")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    _, history, _, _ = get_user_context(user_id)
    if not history:
        await update.message.reply_text("История пустая.")
    else:
        text = "\n\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in history[-20:]])
        await update.message.reply_text(text)

# --- Telegram Payments ---
async def subscribe_telegram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = LabeledPrice(label="Подписка на 30₽/мес", amount=3000)  # сумма в копейках
    await context.bot.send_invoice(
        chat_id=update.message.chat_id,
        title="Подписка на бота",
        description="Доступ ко всем функциям бота на 30 дней",
        payload="subscribe_payload",
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="RUB",
        prices=[price]
    )

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    role, history, free_requests, _ = get_user_context(user_id)
    subscription_end = time.time() + 30*24*3600
    save_user_context(user_id, role, history, free_requests, subscription_end)
    await update.message.reply_text("Оплата через Telegram успешна! Подписка активирована на 30 дней.")

# --- Qiwi ---
async def pay_qiwi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str((update.message or update.callback_query).from_user.id)
    msg_target = update.message or update.callback_query.message
    await msg_target.reply_text(
        f"Переведите 30₽ на Qiwi кошелек: {QIWI_PHONE}\n"
        f"ВАЖНО: В комментарии к платежу ОБЯЗАТЕЛЬНО укажите ваш ID: {user_id}\n\n"
        "После перевода используйте команду /check_qiwi для автоматической активации."
    )

async def check_qiwi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if not QIWI_API_KEY or not QIWI_PHONE:
        await update.message.reply_text("Ошибка: Настройки Qiwi не заданы администратором.")
        return

    try:
        headers = {"Authorization": f"Bearer {QIWI_API_KEY}", "Accept": "application/json"}
        url = f"https://edge.qiwi.com/payment-history/v2/persons/{QIWI_PHONE}/payments?rows=20"
        resp = requests.get(url, headers=headers)
        
        if resp.status_code != 200:
            await update.message.reply_text(f"Ошибка API Qiwi: {resp.status_code}")
            return
            
        data = resp.json()
        found = False
        for item in data.get("data", []):
            # Проверяем сумму, валюту (643 - рубль) и комментарий
            amount = item.get("sum", {}).get("amount")
            comment = item.get("comment")
            status = item.get("status")
            
            if amount == 30 and comment == user_id and status == "SUCCESS":
                found = True
                break
                
        if found:
            role, history, free_requests, _ = get_user_context(user_id)
            subscription_end = time.time() + 30*24*3600
            save_user_context(user_id, role, history, free_requests, subscription_end)
            await update.message.reply_text("Оплата через Qiwi подтверждена! Подписка активирована на 30 дней.")
        else:
            await update.message.reply_text(
                "Платеж не найден. Убедитесь, что:\n"
                "1. Вы перевели ровно 30₽.\n"
                f"2. Вы указали в комментарии ID: {user_id}\n"
                "3. Платеж уже прошел (статус 'Успешно')."
            )
    except Exception as e:
        logging.error(f"Qiwi check error: {e}")
        await update.message.reply_text("Произошла ошибка при проверке Qiwi. Попробуйте позже.")

# --- Карта Мир ---
async def pay_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_target = update.message or update.callback_query.message
    await msg_target.reply_text(
        f"Переведите {CARD_MIR_AMOUNT}₽ на карту: {CARD_MIR_NUMBER}\n"
        "После перевода используйте команду /check_card для активации подписки."
    )

async def check_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    await update.message.reply_text(
        f"Для активации подписки после перевода {CARD_MIR_AMOUNT}₽ на карту {CARD_MIR_NUMBER}, "
        "пожалуйста, пришлите скриншот чека об оплате. Администратор проверит его и активирует доступ."
    )

async def confirm_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Эта команда теперь предназначена только для администратора (нужно добавить проверку ID)
    # Для примера оставим как есть, но предупредим пользователя
    await update.message.reply_text("Команда доступна только администратору для ручной активации.")

# --- Генерация текста GPT-3.5 ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    role, history, free_requests, subscription_end = get_user_context(user_id)
    text = update.message.text

    if not has_access(user_id):
        await update.message.reply_text("Первые 10 сообщений закончились. Используй /subscribe_telegram, /pay_qiwi или /pay_card.")
        return

    messages = [{"role": "system", "content": role}] + history + [{"role": "user", "content": text}]
    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": clean_role}] + history + [{"role": "user", "content": text}],
            max_tokens=300,
            temperature=0.7
        )
        answer = response.choices[0].message.content
        await update.message.reply_text(answer)

        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": answer})
        history = history[-20:]
        if free_requests > 0:
            free_requests -= 1
        save_user_context(user_id, role, history, free_requests, subscription_end)
    except Exception as e:
        error_msg = str(e)
        if "insufficient_quota" in error_msg or "429" in error_msg:
            await update.message.reply_text(
                "🤖 Извините, сейчас я перегружен или у меня закончились ресурсы для обработки запросов. "
                "Пожалуйста, попробуйте позже или обратитесь к администратору @adam0v_0.",
                reply_markup=get_main_menu()
            )
        else:
            await update.message.reply_text(
                "Произошла ошибка при обработке сообщения. Попробуйте еще раз позже.",
                reply_markup=get_main_menu()
            )

# --- Генерация картинок ---
async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    username = update.message.from_user.username
    _, _, free_requests, subscription_end = get_user_context(user_id)
    
    is_vip = username in VIP_USERNAMES if username else False
    if not has_access(user_id) and not is_vip:
        await update.message.reply_text("Первые 10 сообщений закончились. Используй оплату для доступа.")
        return

    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Напиши текст после команды /image")
        return

    try:
        response = openai_client.images.generate(prompt=prompt, n=1, size="512x512")
        image_url = response.data[0].url
        image_data = requests.get(image_url).content
        await update.message.reply_photo(photo=BytesIO(image_data))
        role, history, free_requests, subscription_end = get_user_context(user_id)
        if free_requests > 0:
            free_requests -= 1
        save_user_context(user_id, role, history, free_requests, subscription_end)
    except Exception as e:
        error_msg = str(e)
        if "insufficient_quota" in error_msg or "429" in error_msg:
            await update.message.reply_text(
                "🤖 Извините, сейчас у меня закончились ресурсы для генерации изображений. "
                "Пожалуйста, попробуйте позже или обратитесь к администратору @adam0v_0.",
                reply_markup=get_main_menu()
            )
        else:
            await update.message.reply_text(
                "Произошла ошибка при генерации картинки. Попробуйте еще раз позже.",
                reply_markup=get_main_menu()
            )

# --- Генерация картинок ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    username = update.message.from_user.username
    role, history, free_requests, subscription_end = get_user_context(user_id)
    
    is_vip = username in VIP_USERNAMES if username else False
    if not has_access(user_id) and not is_vip:
        await update.message.reply_text("Первые 10 сообщений закончились. Используй оплату для доступа.", reply_markup=get_main_menu())
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    
    # Кодируем в base64 для OpenAI Vision
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    
    caption = (update.message.caption or "Реши это задание на фото.") + " ВАЖНО: Не используй LaTeX-разметку. Пиши формулы обычным понятным текстом."
    
    try:
        await update.message.reply_text("⏳ Анализирую фото, подождите...", reply_markup=get_main_menu())
        
        response = openai_client.chat.completions.create(
            model="gpt-4o",  # Используем модель с поддержкой Vision
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": caption},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=1000
        )
        
        answer = response.choices[0].message.content
        await update.message.reply_text(answer, reply_markup=get_main_menu())
        
        # Сохраняем в историю (текстовое описание)
        history.append({"role": "user", "content": f"[Фото]: {caption}"})
        history.append({"role": "assistant", "content": answer})
        history = history[-20:]
        if free_requests > 0:
            free_requests -= 1
        save_user_context(user_id, role, history, free_requests, subscription_end)
        
    except Exception as e:
        error_msg = str(e)
        if "insufficient_quota" in error_msg or "429" in error_msg:
            await update.message.reply_text(
                "🤖 Извините, сейчас у меня закончились ресурсы. Пожалуйста, попробуйте позже.",
                reply_markup=get_main_menu()
            )
        else:
            await update.message.reply_text(f"Произошла ошибка: {e}", reply_markup=get_main_menu())

# --- Основная функция ---
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chat_start", chat_start))
    app.add_handler(CommandHandler("image_start", image_start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("subscribe", subscribe_menu))

    app.add_handler(CommandHandler("admin_stats", admin_stats))
    app.add_handler(CommandHandler("admin_broadcast", admin_broadcast))

    # Обработчики оплаты
    app.add_handler(CallbackQueryHandler(button_handler))
    
    app.add_handler(CommandHandler("subscribe_telegram", subscribe_telegram))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    app.add_handler(CommandHandler("pay_qiwi", pay_qiwi))
    app.add_handler(CommandHandler("check_qiwi", check_qiwi))

    app.add_handler(CommandHandler("pay_card", pay_card))
    app.add_handler(CommandHandler("check_card", check_card))
    app.add_handler(CommandHandler("confirm_card", confirm_card))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("image", generate_image))

    # Error handler
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logging.error(f"Exception while handling an update: {context.error}")
        if isinstance(update, Update) and update.message:
            await update.message.reply_text(f"Произошла ошибка: {context.error}")

    app.add_error_handler(error_handler)

    print("Платный бот с Telegram Payments, Qiwi и картой Мир запущен...")
    app.run_polling()

if __name__ == "__main__":
    # Start health check server in a separate thread
    health_check_thread = threading.Thread(target=run_health_check_server, daemon=True)
    health_check_thread.start()
    
    main()
