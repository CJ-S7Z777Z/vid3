
import os
import psycopg2
import yt_dlp
import telegram
import asyncio
import ssl
import urllib.parse
import logging
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)
import aiohttp
import requests

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Главные администраторы (их ID прописаны в коде)
ADMIN_CHAT_IDS = [1276928573, 332786197, 1786980999, 228845914]  # Замените на реальные ID главных админов

# Состояния для ConversationHandler
WAITING_ADMIN_ID = 1
WAITING_REMOVE_ADMIN_ID = 2

# Переменные окружения для ограничений скачиваний
REGULAR_DAILY_LIMIT = int(os.getenv("REGULAR_DAILY_LIMIT"))  # Значение по умолчанию
ADMIN_DAILY_LIMIT = int(os.getenv("ADMIN_DAILY_LIMIT"))    # Значение по умолчанию

# Строка подключения к PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL")

# Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Путь к файлу cookies для Instagram
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"  # Убедитесь, что этот файл существует и содержит актуальные cookies

# Функция для получения подключения к базе данных
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# Создание базы данных
def setup_database():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS admins
        (
            chat_id BIGINT PRIMARY KEY
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS video_downloads
        (
            user_id BIGINT,
            date DATE,
            count INTEGER,
            PRIMARY KEY (user_id, date)
        )
        """
    )
    conn.commit()
    conn.close()

# Функции работы с администраторами
def is_admin(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM admins WHERE chat_id=%s", (chat_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def add_admin_to_db(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO admins (chat_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (chat_id,),
    )
    conn.commit()
    conn.close()

def remove_admin_from_db(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM admins WHERE chat_id=%s", (chat_id,))
    conn.commit()
    conn.close()

def get_admins():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM admins")
    admins = c.fetchall()
    conn.close()
    return [admin[0] for admin in admins]

# Функции для отслеживания скачиваний видео
def get_daily_download_count(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    today = datetime.utcnow().date()
    c.execute(
        "SELECT count FROM video_downloads WHERE user_id=%s AND date=%s",
        (user_id, today),
    )
    result = c.fetchone()
    conn.close()
    if result:
        return result[0]
    else:
        return 0

def increment_daily_download_count(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    today = datetime.utcnow().date()
    current_count = get_daily_download_count(user_id)
    if current_count == 0:
        c.execute(
            """
            INSERT INTO video_downloads (user_id, date, count)
            VALUES (%s, %s, %s)
            """,
            (user_id, today, 1),
        )
    else:
        c.execute(
            """
            UPDATE video_downloads
            SET count = count + 1
            WHERE user_id=%s AND date=%s
            """,
            (user_id, today),
        )
    conn.commit()
    conn.close()

def get_download_limit(chat_id):
    if chat_id in ADMIN_CHAT_IDS or is_admin(chat_id):
        return ADMIN_DAILY_LIMIT
    else:
        return REGULAR_DAILY_LIMIT

# Функция для повторных попыток отправки сообщения
async def send_message_with_retry(
    update, text, reply_markup=None, max_retries=3
):
    for attempt in range(max_retries):
        try:
            if reply_markup:
                return await update.message.reply_text(
                    text, reply_markup=reply_markup
                )
            else:
                return await update.message.reply_text(text)
        except (telegram.error.NetworkError, telegram.error.Timeout) as e:
            if attempt == max_retries - 1:
                raise e
            await asyncio.sleep(1)

# Список тарифов (можно адаптировать или удалить, если не требуется)
tariffs = [
    {"name": "Новичок", "cost": "900руб", "videos": 500},
    {"name": "Любитель", "cost": "4000руб", "videos": 1000},
    {"name": "Профи", "cost": "7500руб", "videos": 2000},
    {"name": "Бизнес", "cost": "14500руб", "videos": 3000},
]

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Проверяем, является ли пользователь администратором
    if chat_id in ADMIN_CHAT_IDS or is_admin(chat_id):
        # Отправляем главное меню администратора
        keyboard = [
            [KeyboardButton("Добавить администратора"), KeyboardButton("Удалить администратора")],
            [KeyboardButton("Администраторы")],
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await send_message_with_retry(
            update,
            "Привет! Вы можете отправить ссылку на видео, и я скачам его для вас.",
            reply_markup=reply_markup,
        )
    else:
        # Пользователь не администратор, предоставляем функцию скачивания видео
        await send_message_with_retry(
            update,
            "Отправьте ссылку на видео из TikTok, YouTube, VK или Instagram, и я скачам его для вас.",
        )
    return ConversationHandler.END

# Команда 'Добавить администратора'
async def add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ADMIN_CHAT_IDS:
        reply_markup = ReplyKeyboardMarkup(
            [[KeyboardButton("Отмена")]], resize_keyboard=True, one_time_keyboard=True
        )
        await send_message_with_retry(
            update,
            "Пожалуйста, отправьте ID пользователя, которого вы хотите добавить в качестве администратора:",
            reply_markup=reply_markup,
        )
        return WAITING_ADMIN_ID
    else:
        await send_message_with_retry(
            update, "Команда доступна только для главных администраторов."
        )
        return ConversationHandler.END

async def add_admin_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        new_admin_chat_id = int(text)
        if new_admin_chat_id not in ADMIN_CHAT_IDS and not is_admin(new_admin_chat_id):
            add_admin_to_db(new_admin_chat_id)
            await send_message_with_retry(
                update,
                f"✅ Пользователь с ID {new_admin_chat_id} добавлен в качестве администратора!",
            )
        else:
            await send_message_with_retry(
                update,
                f"❌ Пользователь с ID {new_admin_chat_id} уже является администратором.",
            )
    except ValueError:
        await send_message_with_retry(
            update, "Неверный формат ID пользователя. Введите число."
        )
        return WAITING_ADMIN_ID
    await start(update, context)
    return ConversationHandler.END

# Команда 'Удалить администратора'
async def remove_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ADMIN_CHAT_IDS:
        admins = get_admins()
        if not admins:
            await send_message_with_retry(update, "Нет добавленных администраторов.")
            return ConversationHandler.END

        message = "Добавленные администраторы:\n\n"
        for i, admin in enumerate(admins, 1):
            message += f"{i}. ID: {admin}\n"

        reply_markup = ReplyKeyboardMarkup(
            [[KeyboardButton("Отмена")]], resize_keyboard=True, one_time_keyboard=True
        )

        await send_message_with_retry(
            update,
            f"{message}\nПожалуйста, отправьте ID администратора, которого вы хотите удалить:",
            reply_markup=reply_markup,
        )
        return WAITING_REMOVE_ADMIN_ID
    else:
        await send_message_with_retry(
            update, "Команда доступна только для главных администраторов."
        )
        return ConversationHandler.END

async def remove_admin_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        admin_chat_id = int(text)
        if is_admin(admin_chat_id) or admin_chat_id in ADMIN_CHAT_IDS:
            remove_admin_from_db(admin_chat_id)
            await send_message_with_retry(
                update,
                f"✅ Пользователь с ID {admin_chat_id} удален из списка администраторов!",
            )
        else:
            await send_message_with_retry(
                update,
                f"❌ Пользователь с ID {admin_chat_id} не является администратором.",
            )
    except ValueError:
        await send_message_with_retry(
            update, "Неверный формат ID пользователя. Введите число."
        )
        return WAITING_REMOVE_ADMIN_ID
    await start(update, context)
    return ConversationHandler.END

# Команда 'Администраторы'
async def show_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ADMIN_CHAT_IDS:
        admins = get_admins()
        if not admins:
            await send_message_with_retry(update, "Нет добавленных администраторов.")
            return

        message = "Добавленные администраторы:\n\n"
        for i, admin in enumerate(admins, 1):
            message += f"{i}. ID: {admin}\n"

        await send_message_with_retry(update, message)
    else:
        await send_message_with_retry(
            update, "Команда доступна только для главных администраторов."
        )

# Функция для удаления видео после таймаута
async def delete_video_after_timeout(video_path, timeout):
    try:
        await asyncio.sleep(timeout)
        if os.path.exists(video_path):
            os.remove(video_path)
            # Удаляем папку пользователя, если она пуста
            user_video_dir = os.path.dirname(video_path)
            try:
                os.rmdir(user_video_dir)
            except OSError:
                pass  # Папка не пуста
    except asyncio.CancelledError:
        # Задача была отменена, ничего не делаем
        pass

# Обработка сообщений с ссылками на видео
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Проверяем лимит скачиваний
    download_limit = get_download_limit(chat_id)
    current_count = get_daily_download_count(user_id)
    if current_count >= download_limit:
        await send_message_with_retry(
            update,
            f"❌ Вы достигли ежедневного лимита скачиваний ({download_limit} видео). Попробуйте завтра.",
        )
        return

    # Проверяем, содержит ли сообщение ссылку на видео
    url = update.message.text.strip()
    if any(
        domain in url
        for domain in ["tiktok.com", "youtube.com", "youtu.be", "vk.com", "instagram.com"]
    ):
        # Отправляем сообщение "Идет загрузка..."
        loading_message = await send_message_with_retry(update, "Идет загрузка...")
        try:
            # Создаем директорию для пользователя
            user_video_dir = f"Video{user_id}"
            if not os.path.exists(user_video_dir):
                os.makedirs(user_video_dir)

            # Определяем опции для yt_dlp
            ydl_options = {
                "format": "best",
                "outtmpl": f"{user_video_dir}/downloaded_video.%(ext)s",
                "quiet": True,
                "socket_timeout": 600,
                "geo_bypass": True,
                "geo_bypass_country": "DE",
            }

            # Проверяем, является ли ссылка Instagram
            if "instagram.com" in url:
                ydl_options["cookiefile"] = INSTAGRAM_COOKIES_FILE  # Убедитесь, что файл существует

            with yt_dlp.YoutubeDL(ydl_options) as ydl:
                result = ydl.extract_info(url, download=True)
                video_file = ydl.prepare_filename(result)

            await asyncio.sleep(1)
            await loading_message.delete()

            # Отправляем видео пользователю
            if os.path.exists(video_file):
                with open(video_file, "rb") as video:
                    await context.bot.send_video(chat_id=chat_id, video=video)
            else:
                await send_message_with_retry(update, "Не удалось найти скачанное видео.")

            # Увеличиваем счетчик скачиваний
            increment_daily_download_count(user_id)

            # Устанавливаем таймер для удаления видео через 10 минут (600 секунд)
            DELETE_TIMEOUT = 600  # 10 минут
            asyncio.create_task(delete_video_after_timeout(video_file, DELETE_TIMEOUT))

        except Exception as e:
            await send_message_with_retry(
                update, f"Ошибка при скачивании видео: {str(e)}"
            )
    else:
        await send_message_with_retry(
            update,
            "Пожалуйста, отправьте ссылку на видео из TikTok, YouTube, VK или Instagram.",
        )

# Функция отмены текущего разговора
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)  # Вызов функции start
    return ConversationHandler.END  # Завершение текущего разговора

# Обработка текстовых сообщений (кнопок)
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    if text == "Добавить администратора":
        return await add_admin_start(update, context)
    elif text == "Удалить администратора":
        return await remove_admin_start(update, context)
    elif text == "Администраторы":
        await show_admins(update, context)
    elif text == "Отмена":
        await cancel(update, context)
    else:
        await handle_user_message(update, context)  # Обрабатываем как возможную ссылку на видео

def main():
    # Создаем базу данных при запуске
    setup_database()

    # Настройка бота
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # Добавление обработчиков
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^(Добавить администратора)$"), add_admin_start),
            MessageHandler(filters.Regex("^(Удалить администратора)$"), remove_admin_start),
            MessageHandler(filters.Regex("^(Администраторы)$"), show_admins),
        ],
        states={
            WAITING_ADMIN_ID: [
                MessageHandler(filters.Regex("^Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_id_received),
            ],
            WAITING_REMOVE_ADMIN_ID: [
                MessageHandler(filters.Regex("^Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, remove_admin_id_received),
            ],
        },
        fallbacks=[
            CommandHandler("start", cancel),
            MessageHandler(filters.Regex("^Отмена$"), cancel),
        ],
    )

    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(CommandHandler("start", start))

    # Запуск бота
    application.run_polling()

if __name__ == "__main__":
    main()
