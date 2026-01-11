import logging
import sqlite3
import time
import os
import requests
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, LabeledPrice, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    filters, PreCheckoutQueryHandler, CallbackQueryHandler
)
from openai import OpenAI
from io import BytesIO

# YooKassa imports
try:
    from yookassa import Configuration, Payment
    YOOKASSA_AVAILABLE = True
except ImportError:
    YOOKASSA_AVAILABLE = False

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- Переменные окружения ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
PAYMENT_PROVIDER_TOKEN = os.environ.get("PAYMENT_PROVIDER_TOKEN")
QIWI_API_KEY = os.environ.get("QIWI_API_KEY")  # токен API для Qiwi
QIWI_PHONE = os.environ.get("QIWI_PHONE")     # номер кошелька

CARD_MIR_NUMBER = os.environ.get("CARD_MIR_NUMBER")  # карта Мир
CARD_MIR_AMOUNT = int(os.environ.get("CARD_MIR_AMOUNT", 30))  # сумма перевода в рублях

# YooKassa settings
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY")

# Configure YooKassa
if YOOKASSA_AVAILABLE and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

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

# Таблица для хранения платежей YooKassa
cursor.execute("""
CREATE TABLE IF NOT EXISTS yookassa_payments (
    payment_id TEXT PRIMARY KEY,
    user_id TEXT,
    amount REAL,
    status TEXT,
    created_at REAL
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
        ["/chat_start"],
        ["/profile"],
        ["/subscribe", "/help"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_payment_menu():
    keyboard = [
        [InlineKeyboardButton("💳 Банковская карта (ЮКасса)", callback_data="pay_yookassa")],
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
    _, _, free_requests, subscription_end = get_user_context(user_id)
    return free_requests > 0 or subscription_end > time.time()

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я твой продвинутый AI-помощник.\n"
        "Я могу отвечать на твои вопросы.\n\n"
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
        "Я могу отвечать на ваши вопросы.\n\n"
        "Если у вас возникли вопросы или проблемы, пожалуйста, обратитесь к администратору: @adam0v_0",
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
    
    if query.data == "pay_yookassa":
        await pay_yookassa(update, context)
    elif query.data == "pay_qiwi":
        await pay_qiwi(update, context)
    elif query.data == "pay_telegram":
        await subscribe_telegram(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    text = update.message.text
    
    # Если сообщение начинается с /, это команда, она обработается CommandHandler
    if text.startswith('/'):
        return

    # Если мы дошли сюда, значит это обычное сообщение для ИИ
    role, history, free_requests, subscription_end = get_user_context(user_id)
    
    if not has_access(user_id):
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
            # Отправляем сообщение пользователю
            await context.bot.send_message(chat_id=user[0], text=msg)
            count += 1
        except Exception as e:
            logging.error(f"Error sending message to {user[0]}: {e}")
            continue
            
    await update.message.reply_text(f"✅ Рассылка завершена. Отправлено {count} пользователям.")

async def activate_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if str(user.id) != ADMIN_ID and user.username != "adam0v_0":
        return
    
    if not context.args:
        await update.message.reply_text("Использование: /activate_sub <user_id>")
        return
        
    target_user_id = context.args[0]
    role, history, free_requests, _ = get_user_context(target_user_id)
    subscription_end = time.time() + 30*24*3600
    save_user_context(target_user_id, role, history, free_requests, subscription_end)
    
    await update.message.reply_text(f"✅ Подписка для {target_user_id} активирована на 30 дней.")
    try:
        await context.bot.send_message(chat_id=target_user_id, text="🌟 Ваша подписка активирована на 30 дней! Приятного использования.")
    except Exception:
        pass

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
# --- YooKassa платежи ---
async def pay_yookassa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_target = update.message or update.callback_query.message
    user_id = str((update.message or update.callback_query).from_user.id)
    
    if not YOOKASSA_AVAILABLE or not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        await msg_target.reply_text(
            "Оплата через ЮКассу временно недоступна. Пожалуйста, используйте другой способ оплаты или обратитесь к @adam0v_0.",
            reply_markup=get_main_menu()
        )
        return
    
    try:
        idempotence_key = str(uuid.uuid4())
        payment = Payment.create({
            "amount": {
                "value": "30.00",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/your_bot"
            },
            "capture": True,
            "description": f"Подписка на бота (30 дней) для пользователя {user_id}",
            "metadata": {
                "user_id": user_id
            }
        }, idempotence_key)
        
        # Сохраняем платеж в базу
        cursor.execute(
            "INSERT OR REPLACE INTO yookassa_payments VALUES (?, ?, ?, ?, ?)",
            (payment.id, user_id, 30.0, payment.status, time.time())
        )
        conn.commit()
        
        payment_url = payment.confirmation.confirmation_url
        
        await msg_target.reply_text(
            f"💳 Для оплаты подписки (30₽) перейдите по ссылке:\n\n{payment_url}\n\n"
            "После оплаты используйте команду /check_payment для активации подписки.",
            reply_markup=get_main_menu()
        )
    except Exception as e:
        logging.error(f"YooKassa payment error: {e}")
        await msg_target.reply_text(
            "Произошла ошибка при создании платежа. Попробуйте позже или обратитесь к @adam0v_0.",
            reply_markup=get_main_menu()
        )

async def check_yookassa_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    
    if not YOOKASSA_AVAILABLE or not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        await update.message.reply_text("Проверка платежей ЮКассы недоступна.", reply_markup=get_main_menu())
        return
    
    # Получаем последний платеж пользователя
    cursor.execute(
        "SELECT payment_id FROM yookassa_payments WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    )
    row = cursor.fetchone()
    
    if not row:
        await update.message.reply_text(
            "У вас нет ожидающих платежей. Используйте /subscribe для оплаты.",
            reply_markup=get_main_menu()
        )
        return
    
    payment_id = row[0]
    
    try:
        payment = Payment.find_one(payment_id)
        
        if payment.status == "succeeded":
            # Платеж успешен - активируем подписку
            role, history, free_requests, _ = get_user_context(user_id)
            subscription_end = time.time() + 30*24*3600
            save_user_context(user_id, role, history, free_requests, subscription_end)
            
            # Обновляем статус в базе
            cursor.execute(
                "UPDATE yookassa_payments SET status = ? WHERE payment_id = ?",
                ("succeeded", payment_id)
            )
            conn.commit()
            
            await update.message.reply_text(
                "✅ Оплата подтверждена! Подписка активирована на 30 дней.",
                reply_markup=get_main_menu()
            )
        elif payment.status == "pending":
            await update.message.reply_text(
                "⏳ Платеж еще обрабатывается. Пожалуйста, подождите и попробуйте снова через несколько минут.",
                reply_markup=get_main_menu()
            )
        elif payment.status == "canceled":
            await update.message.reply_text(
                "❌ Платеж был отменен. Используйте /subscribe для новой попытки оплаты.",
                reply_markup=get_main_menu()
            )
        else:
            await update.message.reply_text(
                f"Статус платежа: {payment.status}. Если возникли проблемы, обратитесь к @adam0v_0.",
                reply_markup=get_main_menu()
            )
    except Exception as e:
        logging.error(f"YooKassa check error: {e}")
        await update.message.reply_text(
            "Произошла ошибка при проверке платежа. Попробуйте позже.",
            reply_markup=get_main_menu()
        )

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
            model="gpt-4o",
            messages=messages,
            temperature=0.7
        )
        answer = response.choices[0].message.content
        
        # Разбиваем длинные сообщения, если они превышают лимит Telegram (4096 символов)
        if len(answer) > 4000:
            for i in range(0, len(answer), 4000):
                chunk = answer[i:i+4000]
                if chunk:
                    await update.message.reply_text(chunk)
        else:
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
    _, _, free_requests, subscription_end = get_user_context(user_id)
    if not has_access(user_id):
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
    app.add_handler(CommandHandler("activate_sub", activate_subscription))

    # Обработчики оплаты
    app.add_handler(CallbackQueryHandler(button_handler))
    
    app.add_handler(CommandHandler("subscribe_telegram", subscribe_telegram))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    app.add_handler(CommandHandler("pay_qiwi", pay_qiwi))
    app.add_handler(CommandHandler("check_qiwi", check_qiwi))

    app.add_handler(CommandHandler("check_payment", check_yookassa_payment))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

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
