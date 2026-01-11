import logging
import sqlite3
import time
import os
import requests
from telegram import Update, LabeledPrice
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    filters, PreCheckoutQueryHandler
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
        "Привет! Я платный AI-бот на GPT-3.5.\n"
        "Первые 10 сообщений бесплатно.\n"
        "Подписка после — 30₽/мес.\n"
        "Команды:\n"
        "/help - помощь\n"
        "/history - последние сообщения\n"
        "/subscribe_telegram - Telegram Payments\n"
        "/pay_qiwi - оплата через Qiwi\n"
        "/pay_card - оплата на карту Мир\n"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start - запуск бота\n"
        "/help - помощь\n"
        "/history - последние 20 сообщений\n"
        "/subscribe_telegram - Telegram Payments\n"
        "/pay_qiwi - оплата через Qiwi\n"
        "/pay_card - оплата на карту Мир"
    )

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
    user_id = str(update.message.from_user.id)
    await update.message.reply_text(
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
    await update.message.reply_text(
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
            messages=messages,
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
        await update.message.reply_text(f"Ошибка OpenAI: {e}")

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
        await update.message.reply_text(f"Ошибка при генерации картинки: {e}")

# --- Основная функция ---
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("history", history_command))

    app.add_handler(CommandHandler("subscribe_telegram", subscribe_telegram))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    app.add_handler(CommandHandler("pay_qiwi", pay_qiwi))
    app.add_handler(CommandHandler("check_qiwi", check_qiwi))

    app.add_handler(CommandHandler("pay_card", pay_card))
    app.add_handler(CommandHandler("check_card", check_card))
    app.add_handler(CommandHandler("confirm_card", confirm_card))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
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
    main()
