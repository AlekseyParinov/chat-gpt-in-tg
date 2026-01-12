import logging
import sqlite3
import time
import os
import requests
import threading
import uuid
import base64
import json
import asyncio
from aiohttp import web
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

# --- Глобальная переменная для Telegram бота ---
telegram_bot = None

# --- Умный выбор модели ---
def choose_model(text: str) -> str:
    """
    Выбирает модель в зависимости от сложности вопроса.
    GPT-4o для сложных задач, GPT-4o-mini для простых.
    """
    text_lower = text.lower()
    
    complex_keywords = [
        'код', 'code', 'python', 'javascript', 'программ', 'функци', 'алгоритм',
        'математик', 'math', 'формул', 'уравнен', 'интеграл', 'производн', 'вычисл',
        'анализ', 'исследова', 'сравн', 'объясни подробно', 'разбер',
        'напиши код', 'создай программ', 'реши задач', 'докажи',
        'sql', 'база данных', 'api', 'json', 'html', 'css',
        'физик', 'химия', 'биолог', 'научн',
        'перевод', 'translate', 'english', 'essay', 'сочинен',
        'стратег', 'план', 'бизнес', 'маркетинг'
    ]
    
    for keyword in complex_keywords:
        if keyword in text_lower:
            return "gpt-4o"
    
    if len(text) > 500:
        return "gpt-4o"
    
    return "gpt-4o-mini"

# --- Webhook handlers (aiohttp) ---
async def handle_health(request):
    return web.json_response({"status": "running", "bot": "active"})

async def handle_yookassa_webhook(request):
    try:
        data = await request.json()
        logging.info(f"Webhook received: {data.get('event') if data else 'no data'}")
        
        if data and data.get('event') == 'payment.succeeded':
            payment_obj = data.get('object', {})
            payment_id = payment_obj.get('id')
            metadata = payment_obj.get('metadata', {})
            user_id = metadata.get('user_id')
            months = int(metadata.get('months', 1))
            
            logging.info(f"Processing payment {payment_id} for user {user_id}, months: {months}")
            
            if user_id:
                days = months * 30
                
                webhook_conn = sqlite3.connect("user_contexts.db")
                webhook_cursor = webhook_conn.cursor()
                
                try:
                    webhook_cursor.execute(
                        "SELECT role, history, free_requests, subscription_end FROM contexts WHERE user_id=?", 
                        (user_id,)
                    )
                    row = webhook_cursor.fetchone()
                    
                    if row:
                        role, history, free_requests, current_sub_end = row
                    else:
                        default_role = "Ты ассистент, который отвечает коротко и логично."
                        role, history, free_requests, current_sub_end = default_role, "[]", 10, 0
                    
                    if current_sub_end > time.time():
                        subscription_end = current_sub_end + days * 24 * 3600
                    else:
                        subscription_end = time.time() + days * 24 * 3600
                    
                    webhook_cursor.execute(
                        "INSERT OR REPLACE INTO contexts VALUES (?,?,?,?,?)",
                        (user_id, role, history, free_requests, subscription_end)
                    )
                    
                    webhook_cursor.execute(
                        "UPDATE yookassa_payments SET status = ? WHERE payment_id = ?",
                        ("succeeded", payment_id)
                    )
                    webhook_conn.commit()
                    
                    logging.info(f"Webhook: Subscription activated for user {user_id} for {days} days")
                    
                    if telegram_bot:
                        try:
                            await telegram_bot.send_message(
                                chat_id=int(user_id),
                                text=f"✅ Оплата получена! Подписка активирована на {days} дней."
                            )
                            logging.info(f"Notification sent to user {user_id}")
                        except Exception as e:
                            logging.error(f"Failed to send notification to {user_id}: {e}")
                
                except Exception as db_error:
                    logging.error(f"Database error in webhook: {db_error}")
                finally:
                    webhook_conn.close()
        
        return web.json_response({"status": "ok"})
        
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return web.json_response({"status": "ok"})

def get_main_menu():
    keyboard = [
        ["/chat_start"],
        ["/profile"],
        ["/subscribe", "/help"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_payment_menu():
    keyboard = [
        [InlineKeyboardButton("💳 Банковская карта (ЮКасса)", callback_data="pay_yookassa")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_user_context(user_id):
    cursor.execute("SELECT role, history, free_requests, subscription_end FROM contexts WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if row:
        role, history, free_requests, subscription_end = row
        return role, eval(history), free_requests, subscription_end
    else:
        default_role = "Ты ассистент, который отвечает коротко и логично. Важно: никогда не используй LaTeX формулы (\\[ \\] или $ $). Пиши математические формулы простым текстом с Unicode символами: √ для корня, ² ³ для степеней, × для умножения, ÷ для деления, ≈ для приблизительно. Пример: v = √(50² + 15²) = √(2500 + 225) = √2725 ≈ 52.2 м/с"
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
        "👋 Привет! Я твой AI-помощник на базе GPT-4o.\n\n"
        "🧠 Что я умею:\n"
        "• Отвечать на любые вопросы\n"
        "• Решать задачи по фото — просто отправь фотографию\n"
        "• Переводить тексты и объяснять сложные темы\n\n"
        "📋 Команды:\n"
        "/profile — твой профиль и статус подписки\n"
        "/chat_start — начать диалог\n"
        "/subscribe — оформить подписку\n"
        "/help — помощь и примеры использования\n\n"
        "💬 Первые 10 сообщений бесплатно!\n\n"
        "Выбери действие в меню ниже или просто напиши мне:",
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
        "🤖 Я могу:\n"
        "• Отвечать на ваши вопросы\n"
        "• Анализировать фотографии — просто отправьте фото с подписью или без\n\n"
        "📸 Примеры использования фото:\n"
        "— Сфотографируйте задачу и попросите решить\n"
        "— Отправьте скриншот текста для перевода\n"
        "— Пришлите фото для описания\n\n"
        "Если у вас возникли вопросы, обратитесь к администратору: @adam0v_0",
        reply_markup=get_main_menu()
    )

async def subscribe_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("1 месяц — 30₽", callback_data="sub_1")],
        [InlineKeyboardButton("3 месяца — 80₽", callback_data="sub_3")],
        [InlineKeyboardButton("6 месяцев — 160₽", callback_data="sub_6")]
    ]
    await update.message.reply_text(
        "💳 Выберите срок подписки:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

SUBSCRIPTION_PLANS = {
    "sub_1": {"months": 1, "amount": "30.00", "label": "1 месяц"},
    "sub_3": {"months": 3, "amount": "80.00", "label": "3 месяца"},
    "sub_6": {"months": 6, "amount": "160.00", "label": "6 месяцев"}
}

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data in SUBSCRIPTION_PLANS:
        plan = SUBSCRIPTION_PLANS[query.data]
        await pay_yookassa(update, context, plan["amount"], plan["months"], plan["label"])
    elif query.data == "pay_yookassa":
        await pay_yookassa(update, context, "30.00", 1, "1 месяц")
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
        await update.message.reply_text("Использование: /activate_sub <user_id> [месяцев]\nПример: /activate_sub 123456789 3")
        return
    
    target_user_id = context.args[0]
    months = 1
    if len(context.args) > 1:
        try:
            months = int(context.args[1])
            if months < 1:
                months = 1
        except ValueError:
            months = 1
    
    days = months * 30
    role, history, free_requests, _ = get_user_context(target_user_id)
    subscription_end = time.time() + days * 24 * 3600
    save_user_context(target_user_id, role, history, free_requests, subscription_end)
    
    month_word = "месяц" if months == 1 else ("месяца" if months < 5 else "месяцев")
    await update.message.reply_text(f"✅ Подписка для {target_user_id} активирована на {months} {month_word}.")
    try:
        await context.bot.send_message(chat_id=target_user_id, text=f"🌟 Ваша подписка активирована на {months} {month_word}! Приятного использования.")
    except Exception:
        pass

async def deactivate_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if str(user.id) != ADMIN_ID and user.username != "adam0v_0":
        return
    
    if not context.args:
        await update.message.reply_text("Использование: /deactivate_sub <user_id>\nПример: /deactivate_sub 123456789")
        return
    
    target_user_id = context.args[0]
    role, history, free_requests, _ = get_user_context(target_user_id)
    save_user_context(target_user_id, role, history, free_requests, 0)
    
    await update.message.reply_text(f"❌ Подписка для {target_user_id} деактивирована.")
    try:
        await context.bot.send_message(
            chat_id=target_user_id, 
            text="⚠️ Ваша подписка признана недействительной по решению администратора.\n\nЕсли у вас есть вопросы, обратитесь к @adam0v_0."
        )
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

# --- YooKassa платежи ---
async def pay_yookassa(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: str = "30.00", months: int = 1, label: str = "1 месяц"):
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
        days = months * 30
        payment = Payment.create({
            "amount": {
                "value": amount,
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/smaart_chatbot"
            },
            "capture": True,
            "description": f"Подписка на бота ({label}) для пользователя {user_id}",
            "metadata": {
                "user_id": user_id,
                "months": months
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
            f"💳 Для оплаты подписки ({label}) перейдите по ссылке:\n\n{payment_url}\n\n"
            "✅ После оплаты подписка активируется автоматически!",
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
            months = int(payment.metadata.get("months", 1)) if payment.metadata else 1
            days = months * 30
            role, history, free_requests, current_sub_end = get_user_context(user_id)
            
            if current_sub_end > time.time():
                subscription_end = current_sub_end + days * 24 * 3600
            else:
                subscription_end = time.time() + days * 24 * 3600
            
            save_user_context(user_id, role, history, free_requests, subscription_end)
            
            cursor.execute(
                "UPDATE yookassa_payments SET status = ? WHERE payment_id = ?",
                ("succeeded", payment_id)
            )
            conn.commit()
            
            await update.message.reply_text(
                f"✅ Оплата подтверждена! Подписка активирована на {days} дней.",
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
        await update.message.reply_text("Первые 10 сообщений закончились. Используй /subscribe для оформления подписки.")
        return

    math_instruction = "ВАЖНО: Никогда не используй LaTeX (\\[, \\], $, $$, \\frac, \\sqrt и т.д.). Пиши формулы только простым текстом с Unicode: √ для корня, ² ³ для степеней, × для умножения, ÷ для деления, ≈ для приблизительно равно. Пример правильного ответа: v = √(50² + 15²) = √2725 ≈ 52.2 м/с"
    system_content = f"{role}\n\n{math_instruction}"
    messages = [{"role": "system", "content": system_content}] + history + [{"role": "user", "content": text}]
    
    selected_model = choose_model(text)
    logging.info(f"User {user_id}: using model {selected_model} for message")
    
    try:
        response = openai_client.chat.completions.create(
            model=selected_model,
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

# --- Обработка фото с GPT-4o Vision ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    role, history, free_requests, subscription_end = get_user_context(user_id)
    
    if not has_access(user_id):
        await update.message.reply_text("Первые 10 сообщений закончились. Используй /subscribe для оформления подписки.")
        return
    
    caption = update.message.caption or "Что изображено на этом фото? Опиши подробно и помоги с любым заданием, если оно есть."
    
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        base64_image = base64.b64encode(photo_bytes).decode('utf-8')
        
        await update.message.reply_text("🔍 Анализирую изображение...")
        
        math_instruction = "ВАЖНО: Никогда не используй LaTeX (\\[, \\], $, $$, \\frac, \\sqrt и т.д.). Пиши формулы только простым текстом с Unicode: √ для корня, ² ³ для степеней, × для умножения, ÷ для деления, ≈ для приблизительно равно. Пример правильного ответа: v = √(50² + 15²) = √2725 ≈ 52.2 м/с"
        system_content = f"{role}\n\n{math_instruction}"
        messages = [
            {"role": "system", "content": system_content},
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
        ]
        
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=2000
        )
        answer = response.choices[0].message.content
        
        if len(answer) > 4000:
            for i in range(0, len(answer), 4000):
                chunk = answer[i:i+4000]
                if chunk:
                    await update.message.reply_text(chunk)
        else:
            await update.message.reply_text(answer)
        
        history.append({"role": "user", "content": f"[Фото] {caption}"})
        history.append({"role": "assistant", "content": answer})
        history = history[-20:]
        if free_requests > 0:
            free_requests -= 1
        save_user_context(user_id, role, history, free_requests, subscription_end)
        
    except Exception as e:
        error_msg = str(e)
        logging.error(f"Photo processing error: {e}")
        if "insufficient_quota" in error_msg or "429" in error_msg:
            await update.message.reply_text(
                "🤖 Извините, сейчас я перегружен. Попробуйте позже или обратитесь к @adam0v_0.",
                reply_markup=get_main_menu()
            )
        else:
            await update.message.reply_text(
                "Произошла ошибка при обработке фото. Попробуйте еще раз.",
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

# --- Фоновая проверка платежей ---
async def check_pending_payments(bot):
    while True:
        try:
            await asyncio.sleep(30)
            
            if not YOOKASSA_AVAILABLE:
                continue
            
            check_conn = sqlite3.connect("user_contexts.db")
            check_cursor = check_conn.cursor()
            
            check_cursor.execute(
                "SELECT payment_id, user_id FROM yookassa_payments WHERE status = 'pending'"
            )
            pending = check_cursor.fetchall()
            
            for payment_id, user_id in pending:
                try:
                    payment = Payment.find_one(payment_id)
                    
                    if payment.status == "succeeded":
                        months = int(payment.metadata.get("months", 1)) if payment.metadata else 1
                        days = months * 30
                        
                        check_cursor.execute(
                            "SELECT role, history, free_requests, subscription_end FROM contexts WHERE user_id=?",
                            (user_id,)
                        )
                        row = check_cursor.fetchone()
                        
                        if row:
                            role, history, free_requests, current_sub_end = row
                        else:
                            role, history, free_requests, current_sub_end = "Ты ассистент.", "[]", 10, 0
                        
                        if current_sub_end > time.time():
                            subscription_end = current_sub_end + days * 24 * 3600
                        else:
                            subscription_end = time.time() + days * 24 * 3600
                        
                        check_cursor.execute(
                            "INSERT OR REPLACE INTO contexts VALUES (?,?,?,?,?)",
                            (user_id, role, history, free_requests, subscription_end)
                        )
                        check_cursor.execute(
                            "UPDATE yookassa_payments SET status = 'succeeded' WHERE payment_id = ?",
                            (payment_id,)
                        )
                        check_conn.commit()
                        
                        logging.info(f"Payment check: Subscription activated for {user_id} for {days} days")
                        
                        try:
                            await bot.send_message(
                                chat_id=int(user_id),
                                text=f"✅ Оплата получена! Подписка активирована на {days} дней."
                            )
                        except Exception as e:
                            logging.error(f"Failed to notify user {user_id}: {e}")
                    
                    elif payment.status == "canceled":
                        check_cursor.execute(
                            "UPDATE yookassa_payments SET status = 'canceled' WHERE payment_id = ?",
                            (payment_id,)
                        )
                        check_conn.commit()
                
                except Exception as e:
                    logging.error(f"Error checking payment {payment_id}: {e}")
            
            check_conn.close()
            
        except Exception as e:
            logging.error(f"Payment check loop error: {e}")
            await asyncio.sleep(60)

# --- Основная функция ---
async def run_bot():
    global telegram_bot
    
    tg_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("chat_start", chat_start))
    tg_app.add_handler(CommandHandler("image_start", image_start))
    tg_app.add_handler(CommandHandler("profile", profile_command))
    tg_app.add_handler(CommandHandler("help", help_command))
    tg_app.add_handler(CommandHandler("history", history_command))
    tg_app.add_handler(CommandHandler("subscribe", subscribe_menu))

    tg_app.add_handler(CommandHandler("admin_stats", admin_stats))
    tg_app.add_handler(CommandHandler("admin_broadcast", admin_broadcast))
    tg_app.add_handler(CommandHandler("activate_sub", activate_subscription))
    tg_app.add_handler(CommandHandler("deactivate_sub", deactivate_subscription))

    tg_app.add_handler(CallbackQueryHandler(button_handler))
    
    tg_app.add_handler(CommandHandler("subscribe_telegram", subscribe_telegram))
    tg_app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    tg_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    tg_app.add_handler(CommandHandler("check_payment", check_yookassa_payment))

    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    tg_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logging.error(f"Exception while handling an update: {context.error}")
        if isinstance(update, Update) and update.message:
            await update.message.reply_text(f"Произошла ошибка: {context.error}")

    tg_app.add_error_handler(error_handler)
    
    telegram_bot = tg_app.bot
    
    health_app = web.Application()
    health_app.router.add_get('/', handle_health)
    health_app.router.add_post('/yookassa-webhook', handle_yookassa_webhook)
    
    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 5000)
    await site.start()
    print("Health check server on port 5000")
    logging.info("Health check server on port 5000")
    
    async with tg_app:
        await tg_app.start()
        print("Telegram bot started...")
        logging.info("Telegram bot started")
        
        asyncio.create_task(check_pending_payments(tg_app.bot))
        print("Payment checker started (every 30 seconds)")
        logging.info("Payment checker started")
        
        await tg_app.updater.start_polling()
        
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    print("Starting Telegram bot...")
    asyncio.run(run_bot())
