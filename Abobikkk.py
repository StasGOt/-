import logging
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
import telegram
import sqlite3
import os
from ftplib import FTP, error_perm
from dotenv import load_dotenv
import random
import bcrypt
import secrets
import psutil

from profanity_words import PROFANITY_LIST

def normalize_text_for_profanity(text: str) -> str:
    """
    Нормалізує текст для перевірки на нецензурну лексику:
    переводить у нижній регістр, видаляє цифри, знаки пунктуації та зайві пробіли.
    """
    import re  # Імпортуємо re тут, щоб не додавати його в основний список імпортів, якщо він потрібен тільки тут
    
    # Прибираємо цифри та більшість знаків пунктуації, замінюючи їх на пробіли
    # Залишаємо тільки літери та пробіли
    normalized_text = re.sub(r"[^а-яА-ЯіІїЇєЄґҐa-zA-Z\s]", " ", text).lower()
    
    # Замінюємо множинні пробіли на один
    normalized_text = re.sub(r"\s+", " ", normalized_text).strip()
    
    return normalized_text


def contains_profanity(text: str) -> bool:
    """
    Перевіряє, чи містить текст нецензурну лексику зі списку PROFANITY_LIST.
    """
    normalized_input = normalize_text_for_profanity(text)
    input_words = set(normalized_input.split())
    
    # Перевірка на точні входження слів
    if any(word in input_words for word in PROFANITY_LIST):
        return True
        
    # Додаткова перевірка на часткові входження (якщо заборонене слово є підрядком)
    # Ця перевірка може давати хибні спрацювання на невинні слова (наприклад, "коса" містить "ос", якщо у списку є "ос").
    # Якщо це стане проблемою, цей блок можна прибрати.
    for forbidden_word in PROFANITY_LIST:
        if forbidden_word in normalized_input:
            return True
            
    return False


# --- ІНТЕГРАЦІЯ: Імпорт SQLManaging ---
from SQManager import SQLManaging
# ---------------------------------------

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

try:
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:
    logger.critical(
        "Не вдалося завантажити часовий пояс 'Europe/Kyiv'. Переконайтеся, що пакет 'tzdata' встановлений. Бот не може продовжити."
    )
    exit("Критична помилка: Часовий пояс 'Europe/Kyiv' не знайдено.")

# --- Конфігурація ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN не знайдено в .env файлі!")
    exit("BOT_TOKEN відсутній")

ADMIN_USER_IDS = {
    int(admin_id)
    for admin_id in os.getenv("ADMIN_USER_IDS", "").split(",")
    if admin_id.strip().isdigit()
}

DONATION_CARD_NUMBER = os.getenv("DONATION_CARD_NUMBER", "Не вказано")
REPORT_CHANNEL_ID_STR = os.getenv("REPORT_CHANNEL_ID")
REPORT_CHANNEL_ID = (
    int(REPORT_CHANNEL_ID_STR)
    if REPORT_CHANNEL_ID_STR and REPORT_CHANNEL_ID_STR.lstrip("-").isdigit()
    else None
)

# URL веб-додатку для відкриття в Telegram WebApp
# При відсутності змінної середовища використовується локальний dev-сервер
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:5000")

def _build_webapp_url_for_user(user_id: int) -> str:
    """Формує персоналізоване посилання для Telegram WebApp з параметрами користувача."""
    try:
        role = get_user_role_from_db(user_id) or "guest"
    except Exception:
        role = "guest"
    try:
        group = get_user_group_from_db(user_id) or ""
    except Exception:
        group = ""
    # Проста передача параметрів через query-string
    from urllib.parse import urlencode
    qs = urlencode({
        "uid": str(user_id),
        "role": role,
        "group": group,
    })
    sep = "&" if ("?" in WEBAPP_URL) else "?"
    return f"{WEBAPP_URL}{sep}{qs}"

# Канал для репортів від викладачів (розробники)
TEACHER_REPORT_CHANNEL_ID = -1002521518792

# --- Налаштування каналу для пропозицій ---
SUGGESTION_CHANNEL_ID_STR = os.getenv("SUGGESTION_CHANNEL_ID")
SUGGESTION_CHANNEL_ID = (
    int(SUGGESTION_CHANNEL_ID_STR)
    if SUGGESTION_CHANNEL_ID_STR and SUGGESTION_CHANNEL_ID_STR.lstrip("-").isdigit()
    else None
)

FEEDBACK_CHANNEL_ID_STR = os.getenv("FEEDBACK_CHANNEL_ID")
FEEDBACK_CHANNEL_ID = (
    int(FEEDBACK_CHANNEL_ID_STR)
    if FEEDBACK_CHANNEL_ID_STR and FEEDBACK_CHANNEL_ID_STR.lstrip("-").isdigit()
    else None
)


# --- Налаштування розіграшу ---
RAFFLE_ACTIVE = os.getenv("RAFFLE_ACTIVE", "true").lower() == "true"
RAFFLE_END_DATE_STR = os.getenv("RAFFLE_END_DATE", "2025-06-10 17:00:00")
RAFFLE_CHANNEL_USERNAME = os.getenv("RAFFLE_CHANNEL_USERNAME", "chgek")
RAFFLE_PRIZE = os.getenv("RAFFLE_PRIZE", "піци СИРНА САЛЯМІ 38 СМ")
# -----------------------------

try:
    RAFFLE_END_DATE = datetime.strptime(RAFFLE_END_DATE_STR, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=KYIV_TZ
    )
except ValueError:
    logger.error(
        f"Некоректний формат RAFFLE_END_DATE ('{RAFFLE_END_DATE_STR}'). Використовується значення за замовчуванням 2025-06-10 17:00:00."
    )
    RAFFLE_END_DATE = datetime(2025, 6, 10, 17, 0, 0, tzinfo=KYIV_TZ)

# ростік то назарій добавив для організації воно всьо працює як шось непонятно пиши

json_folder_prefix = "static/json/"

db_folder_prefix = "static/dbs/"


# --- ІНТЕГРАЦІЯ: Налаштування для бази даних розкладу ---

SCHEDULE_DB_NAME = db_folder_prefix + os.getenv("SCHEDULE_DB_NAME", "schedule.db")

SCHEDULE_JSON_SOURCE_FILE = json_folder_prefix + os.getenv(
    "SCHEDULE_JSON_SOURCE", "static/json/schedule_all_groups.json"
)

sql_manager: SQLManaging | None = None

schedule_cache = None

# ----------------------------------------------------

# --- Конфігурація для спеціальностей ---

SPECIALTIES_JSON_FILE = json_folder_prefix + os.getenv(
    "SPECIALTIES_JSON_SOURCE", "static/json/specialties_data.json"
)

specialties_cache = None

# ---------------------------------------

# --- НОВЕ: Конфігурація для документів для вступу ---

ADMISSION_DOCS_JSON_FILE = json_folder_prefix + os.getenv(
    "ADMISSION_DOCS_JSON_SOURCE", "static/json/admission_docs.json"
)

admission_docs_cache = None  # Змінна для кешування даних про документи

# ----------------------------------------------------


BASE_DATE_STR = os.getenv("BASE_DATE", "2024-09-02")

try:

    _naive_base_date = datetime.strptime(BASE_DATE_STR, "%Y-%m-%d")

    BASE_DATE = datetime(
        _naive_base_date.year, _naive_base_date.month, _naive_base_date.day, 0, 0, 0, tzinfo=KYIV_TZ
    )

except ValueError:

    logger.error(
        f"Некоректний формат BASE_DATE ('{BASE_DATE_STR}'). Використовується значення 2025-06-02."
    )

    BASE_DATE = datetime(2025, 6, 2, 0, 0, 0, tzinfo=KYIV_TZ)


maintenance_mode_active = False

maintenance_message = "Бот на технічному обслуговуванні. Будь ласка, спробуйте пізніше."

maintenance_end_time = None

MAINTENANCE_JOB_NAME = "disable_maintenance_job"

FTP_SYNC_JOB_NAME = "ftp_sync_db_job"

# Змінна для зберігання ID повідомлень про технічне обслуговування

# Використовуємо словник для зберігання message_id для кожного чату

# Це потрібно, якщо бот може надсилати повідомлення про ТО в різні чати (наприклад, групові чати користувачів, а не тільки приватні)

maintenance_messages_ids = {}  # {chat_id: message_id}


MAX_ALBUM_PHOTOS = 10


# Це ЄДИНИЙ і ПРАВИЛЬНИЙ БЛОК ВИЗНАЧЕННЯ КОНСТАНТ, який має бути у файлі.

SELECTING_ROLE, SELECTING_COURSE, GUEST_MENU, STAFF_MENU = range(4)

# ... решта констант


SELECTING_MAINTENANCE_ACTION, SELECTING_DURATION, TYPING_DURATION, TYPING_MESSAGE = range(5, 9)

SELECTING_GROUP = range(9, 10)[0]

ANNOUNCE_SELECT_TARGET, ANNOUNCE_SELECT_GROUP_FOR_ANNOUNCE,
ANNOUNCE_TYPING_MESSAGE_FOR_ANNOUNCE, ANNOUNCE_CHOOSING_MEDIA_TYPE,
ANNOUNCE_WAITING_FOR_PHOTOS, ANNOUNCE_TYPING_CAPTION_FOR_MEDIA = range(10, 16)


RAFFLE_MENU = 16

RAFFLE_JOIN_CONFIRMATION, RAFFLE_REFERRAL_CODE_ENTRY = range(17, 19)

TYPING_REPORT = range(19, 20)[0]

TYPING_SUGGESTION = TYPING_REPORT + 1

TYPING_FEEDBACK = TYPING_SUGGESTION + 1  # НОВА КОНСТАНТА ДЛЯ СТАНУ ВІДГУКУ


# Нові константи для входу викладача та адмін-панелі

(
    TYPING_ONE_TIME_PASSWORD,
    ADMIN_TEACHER_MENU,
    ADMIN_TEACHER_ADD_NAME,
    ADMIN_TEACHER_ADD_GROUP,
    ADMIN_TEACHER_SELECT_FOR_OTP,
    ADMIN_TEACHER_SET_OTP_DURATION,
    ADMIN_TEACHER_EDIT_SELECT,
    ADMIN_TEACHER_EDIT_MENU,
    ADMIN_TEACHER_EDIT_NAME,
    ADMIN_TEACHER_EDIT_GROUP,
    ADMIN_TEACHER_DELETE_CONFIRM,
) = range(
    TYPING_FEEDBACK + 1, TYPING_FEEDBACK + 12
)  # ОНОВЛЕНО: кількість має збігатися з числом констант

# Кінець єдиного блоку констант


# SELECTING_ADMISSION_FORM = TYPING_TEACHER_SET_OTP_DURATION + 1 # Закоментували старий рядок

SELECTING_ADMISSION_FORM = TYPING_FEEDBACK + 12  # НОВА МІНІМАЛЬНА ЗМІНА: Обчислюємо значення тут

VIEWING_ADMISSION_DOCS = SELECTING_ADMISSION_FORM + 1

# Кінець єдиного блоку констант

# Кінець єдиного блоку констант


DATABASE_NAME = "static/dbs/bot_users.db"

ENABLE_FTP_SYNC = os.getenv("ENABLE_FTP_SYNC", "false").lower() == "true"

FTP_HOST = os.getenv("FTP_HOST")

FTP_PORT_STR = os.getenv("FTP_PORT", "21")

FTP_USER = os.getenv("FTP_USER")

FTP_PASSWORD = os.getenv("FTP_PASSWORD")

FTP_REMOTE_DB_PATH = os.getenv("FTP_REMOTE_DB_PATH")


# Constants for progress updates

PROGRESS_UPDATE_INTERVAL = 50  # Update every 50 messages sent


# --- ІНТЕГРАЦІЯ: Функція ініціалізації БД розкладу ---


def initialize_schedule_database():
    global sql_manager, schedule_cache
    logger.info(f"БД Розкладу: Ініціалізація SQLManager з БД '{SCHEDULE_DB_NAME}'...")
    
    try:
        sql_manager = SQLManaging(db=SCHEDULE_DB_NAME, json_schedule_file=SCHEDULE_JSON_SOURCE_FILE)
        static_groups = sql_manager.get_static().get("Groups", {})
        sql_manager.cr.execute(f'SELECT COUNT(*) FROM "{sql_manager.table}"')
        schedule_entries_count = sql_manager.cr.fetchone()[0]
        
        if not static_groups or schedule_entries_count == 0:
            logger.info(
                f"БД Розкладу: База даних порожня або не містить груп/записів. Спроба завантажити дані з '{SCHEDULE_JSON_SOURCE_FILE}'..."
            )
            
            if not os.path.exists(SCHEDULE_JSON_SOURCE_FILE):
                logger.error(
                    f"БД Розкладу: Файл '{SCHEDULE_JSON_SOURCE_FILE}' не знайдено! Розклад не буде завантажено."
                )
            else:
                try:
                    sql_manager.encode_json()
                    logger.info(f"БД Розкладу: Дані успішно завантажені з '{SCHEDULE_JSON_SOURCE_FILE}'.")
                    sql_manager.get_static(force_reload=True)
                except Exception as e:
                    logger.error(f"БД Розкладу: Помилка під час encode_json: {e}", exc_info=True)
        else:
            logger.info(
                f"БД Розкладу: SQLManager ініціалізовано. Знайдено {len(static_groups)} груп та {schedule_entries_count} записів розкладу."
            )
        
        schedule_cache = None
        get_cached_schedule()
        
    except Exception as e:
        logger.critical(
            f"БД Розкладу: Критична помилка ініціалізації SQLManager: {e}", exc_info=True
        )


def initialize_database():
    # Ensure the directory exists before trying to connect to the database
    db_dir = os.path.dirname(DATABASE_NAME)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        logger.info(f"Створено директорію для БД: {db_dir}")
    
    # Try FTP download if enabled, but don't fail if it doesn't work
    ftp_success = False
    if ENABLE_FTP_SYNC:
        try:
            ftp_success = download_db_from_ftp()
            if ftp_success:
                logger.info(f"БД Користувачів ('{DATABASE_NAME}'): Завантажено з FTP.")
            else:
                logger.warning(
                    f"БД Користувачів ('{DATABASE_NAME}'): FTP завантаження не вдалося, використовується локальна."
                )
        except Exception as e:
            logger.warning(
                f"БД Користувачів ('{DATABASE_NAME}'): FTP завантаження завершилося помилкою: {e}, використовується локальна."
            )
            ftp_success = False
    
    if not ftp_success:
        logger.info(f"БД Користувачів ('{DATABASE_NAME}'): Використовується локальна.")

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute(
                """

                CREATE TABLE IF NOT EXISTS users (

                    user_id INTEGER PRIMARY KEY,

                    username TEXT,

                    first_name TEXT,

                    last_name TEXT,

                    group_name TEXT DEFAULT NULL,

                    referrer_id INTEGER DEFAULT NULL,

                    is_raffle_participant BOOLEAN DEFAULT FALSE,

                    referred_count INTEGER DEFAULT 0,

                    raffle_participation_date TIMESTAMP DEFAULT NULL,

                    joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP

                )

            """
            )

        existing_columns = [col[1] for col in cursor.execute("PRAGMA table_info(users)").fetchall()]

        if "referrer_id" not in existing_columns:

            cursor.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT NULL")

        if "is_raffle_participant" not in existing_columns:

            cursor.execute(
                "ALTER TABLE users ADD COLUMN is_raffle_participant BOOLEAN DEFAULT FALSE"
            )

        if "referred_count" not in existing_columns:

            cursor.execute("ALTER TABLE users ADD COLUMN referred_count INTEGER DEFAULT 0")

        if "raffle_participation_date" not in existing_columns:

            cursor.execute(
                "ALTER TABLE users ADD COLUMN raffle_participation_date TIMESTAMP DEFAULT NULL"
            )

        if "joined_date" not in existing_columns:

            # Додаємо стовпець без DEFAULT CURRENT_TIMESTAMP, щоб уникнути помилки

            cursor.execute("ALTER TABLE users ADD COLUMN joined_date TIMESTAMP")

            # Оновлюємо існуючі записи, щоб встановитиjoined_date для тих, у кого він NULL

            # Це гарантує, що старі користувачі також отримають joined_date

            cursor.execute(
                "UPDATE users SET joined_date = CURRENT_TIMESTAMP WHERE joined_date IS NULL"
            )

            logger.info(
                "БД Користувачів: Додано стовпець 'joined_date' та оновлено існуючі записи."
            )

        if "role" not in existing_columns:

            cursor.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'ASK_ROLE'")  #

            logger.info("БД Користувачів: Додано стовпець 'role'.")  #

        # ---- ДОДАЙТЕ ЦЕЙ БЛОК ДЛЯ СТВОРЕННЯ ТАБЛИЦІ ВИКЛАДАЧІВ ----

        cursor.execute(
            """

            CREATE TABLE IF NOT EXISTS teachers (

                teacher_id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_id INTEGER UNIQUE,

                full_name TEXT UNIQUE NOT NULL,

                curated_group_name TEXT,

                one_time_password_hash TEXT,

                password_expires_at TIMESTAMP,

                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE

            )

        """
        )

        logger.info("БД Викладачів: Таблиця 'teachers' готова.")

        # ---------------------------

        cursor.execute(
            "CREATE TABLE IF NOT EXISTS command_stats (command TEXT PRIMARY KEY, count INTEGER DEFAULT 0)"
        )

        cursor.execute(
            "CREATE TABLE IF NOT EXISTS dead_letter_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, message_text TEXT NOT NULL, error_message TEXT, failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'new')"
        )

        conn.commit()

        logger.info(f"БД Користувачів: '{DATABASE_NAME}' готова.")

    except sqlite3.Error as e:

        logger.critical(f"БД Користувачів: Критична помилка SQLite під час ініціалізації: {e}")

        raise

    except Exception as e:

        logger.critical(f"БД Користувачів: Критична помилка під час ініціалізації: {e}")

        raise


def add_or_update_user_in_db(
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    group_name: str | None = "ASK_LATER",
    referrer_id: int | None = None,
):

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))

            existing_user = cursor.fetchone()

            if existing_user:

                cursor.execute(
                    """

                    UPDATE users SET

                        username = ?,

                        first_name = ?,

                        last_name = ?

                    WHERE user_id = ?

                """,
                    (username, first_name, last_name, user_id),
                )

            else:

                cursor.execute(
                    """

                    INSERT INTO users (user_id, username, first_name, last_name, group_name, referrer_id)

                    VALUES (?, ?, ?, ?, ?, ?)

                """,
                    (user_id, username, first_name, last_name, group_name, referrer_id),
                )

            conn.commit()

            if not existing_user and referrer_id:

                increment_referred_count(referrer_id)

                logger.info(
                    f"БД Користувачів: Користувач {user_id} доданий за рефералом {referrer_id}. Лічильник рефералів для {referrer_id} збільшено."
                )

            else:

                logger.info(
                    f"БД Користувачів: Користувач {user_id} збережений/оновлений. Група: {get_user_group_from_db(user_id)}"
                )

    except sqlite3.Error as e:

        logger.error(f"БД Користувачів: Помилка збереження користувача {user_id}: {e}")


def get_user_data_from_db(user_id: int) -> dict | None:
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            return dict(result) if result else None
    except sqlite3.Error as e:
        logger.error(f"БД Користувачів: Помилка отримання даних користувача {user_id}: {e}")
        return None


def set_user_group_in_db(user_id: int, group_name: str | None) -> bool:
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET group_name = ? WHERE user_id = ?", 
                (group_name, user_id)
            )
            conn.commit()
        
        logger.info(f"БД Користувачів: Для {user_id} встановлено групу: {group_name}")
        return True
    except sqlite3.Error as e:
        logger.error(f"БД Користувачів: Помилка встановлення групи для {user_id}: {e}")
        return False


def set_user_role_in_db(user_id: int, role: str) -> bool:
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
            conn.commit()
        logger.info(f"БД Користувачів: Для {user_id} встановлено роль: {role}")
        return True
    except sqlite3.Error as e:
        logger.error(f"БД Користувачів: Помилка встановлення ролі для {user_id}: {e}")
        return False


def get_user_role_from_db(user_id: int) -> str | None:
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            return result[0] if result and result[0] else None
    except sqlite3.Error as e:
        logger.error(f"БД Користувачів: Помилка отримання ролі для {user_id}: {e}")
        return None


def get_user_group_from_db(user_id: int) -> str | None:
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT group_name FROM users WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            return result[0] if result and result[0] else None
    except sqlite3.Error as e:
        logger.error(f"БД Користувачів: Помилка отримання групи для {user_id}: {e}")
        return None


def get_all_user_ids_from_db(group_name: str | None = None) -> set[int]:
    try:
        with sqlite3.connect(DATABASE_NAME) as conn:
            cursor = conn.cursor()
            if group_name == "__ALL_USERS_WITH_GROUP__":
                cursor.execute("SELECT user_id FROM users WHERE group_name IS NOT NULL")
            elif group_name:
                cursor.execute("SELECT user_id FROM users WHERE group_name = ?", (group_name,))
            else:
                cursor.execute("SELECT user_id FROM users")
            return {row[0] for row in cursor.fetchall()}
    except sqlite3.Error as e:
        logger.error(f"БД Користувачів: Помилка отримання ID (група: {group_name}): {e}")
        return set()


# --- НОВІ ФУНКЦІЇ ДЛЯ РОБОТИ З ТАБЛИЦЕЮ ВИКЛАДАЧІВ ---


def add_or_update_teacher_in_db(full_name: str, curated_group_name: str | None) -> bool:
    """Додає нового викладача (без user_id) або оновлює дані існуючого за ПІБ."""

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute(
                """

                INSERT INTO teachers (full_name, curated_group_name)

                VALUES (?, ?)

                ON CONFLICT(full_name) DO UPDATE SET

                    curated_group_name = excluded.curated_group_name

            """,
                (full_name, curated_group_name),
            )

            conn.commit()

        logger.info(f"БД Викладачів: Додано/Оновлено викладача {full_name}.")

        return True

    except sqlite3.Error as e:

        logger.error(f"БД Викладачів: Помилка при додаванні/оновленні викладача {full_name}: {e}")

        return False


def update_teacher_name_in_db(teacher_id: int, new_full_name: str) -> bool:
    """Оновлює ПІБ викладача за його teacher_id."""

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute(
                """

                UPDATE teachers SET full_name = ?

                WHERE teacher_id = ?

            """,
                (new_full_name, teacher_id),
            )

            conn.commit()

        logger.info(
            f"БД Викладачів: Оновлено ім'я викладача (teacher_id={teacher_id}) на '{new_full_name}'."
        )

        return True

    except sqlite3.Error as e:

        logger.error(
            f"БД Викладачів: Помилка оновлення імені викладача (teacher_id={teacher_id}): {e}"
        )

        return False


def update_teacher_curated_group_in_db(teacher_id: int, curated_group_name: str | None) -> bool:
    """Оновлює кураторську групу викладача. Якщо None – очищає поле."""

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute(
                """

                UPDATE teachers SET curated_group_name = ?

                WHERE teacher_id = ?

            """,
                (curated_group_name, teacher_id),
            )

            conn.commit()

        logger.info(
            f"БД Викладачів: Оновлено кураторську групу (teacher_id={teacher_id}) на '{curated_group_name}'."
        )

        return True

    except sqlite3.Error as e:

        logger.error(
            f"БД Викладачів: Помилка оновлення кураторської групи (teacher_id={teacher_id}): {e}"
        )

        return False


def delete_teacher_in_db(teacher_id: int) -> bool:
    """Видаляє викладача за його teacher_id."""

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute("DELETE FROM teachers WHERE teacher_id = ?", (teacher_id,))

            conn.commit()

        logger.info(f"БД Викладачів: Видалено викладача teacher_id={teacher_id}.")

        return True

    except sqlite3.Error as e:

        logger.error(f"БД Викладачів: Помилка видалення викладача teacher_id={teacher_id}: {e}")

        return False


def get_teacher_data_from_db(user_id: int) -> dict | None:
    """Отримує дані викладача за його user_id."""

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            conn.row_factory = sqlite3.Row

            cursor = conn.cursor()

            cursor.execute("SELECT * FROM teachers WHERE user_id = ?", (user_id,))

            result = cursor.fetchone()

            return dict(result) if result else None

    except sqlite3.Error as e:

        logger.error(f"БД Викладачів: Помилка отримання даних для викладача {user_id}: {e}")

        return None


def set_teacher_otp_by_id(teacher_id: int, otp_lifetime_minutes: int) -> str | None:
    """Генерує, хешує та зберігає OTP для викладача за його ID в таблиці teachers."""

    try:

        otp = secrets.token_hex(4)

        hashed_otp = bcrypt.hashpw(otp.encode("utf-8"), bcrypt.gensalt())

        expires_at = datetime.now(KYIV_TZ) + timedelta(minutes=otp_lifetime_minutes)

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute(
                """

                UPDATE teachers SET one_time_password_hash = ?, password_expires_at = ?

                WHERE teacher_id = ?

            """,
                (hashed_otp, expires_at.isoformat(), teacher_id),
            )

            conn.commit()

        logger.info(
            f"OTP: Згенеровано новий пароль для викладача (teacher_id={teacher_id}), дійсний до {expires_at}."
        )

        return otp

    except sqlite3.Error as e:

        logger.error(
            f"OTP: Помилка при встановленні пароля для викладача (teacher_id={teacher_id}): {e}"
        )

        return None


def verify_otp_and_claim_profile(entered_otp: str, claimer_user_id: int) -> tuple[bool, str]:
    """Перевіряє OTP. Якщо він вірний, 'прив'язує' профіль викладача до Telegram ID користувача."""

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            conn.row_factory = sqlite3.Row

            cursor = conn.cursor()

            cursor.execute("SELECT * FROM teachers WHERE one_time_password_hash IS NOT NULL")

            potential_teachers = cursor.fetchall()

            for teacher_row in potential_teachers:

                hashed_otp = teacher_row["one_time_password_hash"]

                if bcrypt.checkpw(entered_otp.encode("utf-8"), hashed_otp):

                    expires_at = datetime.fromisoformat(teacher_row["password_expires_at"])

                    if datetime.now(KYIV_TZ) > expires_at:

                        cursor.execute(
                            "UPDATE teachers SET one_time_password_hash = NULL, password_expires_at = NULL WHERE teacher_id = ?",
                            (teacher_row["teacher_id"],),
                        )

                        conn.commit()

                        return (False, "Термін дії вашого пароля минув.")

                    previous_user_id = teacher_row["user_id"]

                    # Дозволяємо перевипуск та перев'язку навіть якщо профіль вже активований

                    cursor.execute(
                        "UPDATE teachers SET user_id = ?, one_time_password_hash = NULL, password_expires_at = NULL WHERE teacher_id = ?",
                        (claimer_user_id, teacher_row["teacher_id"]),
                    )

                    conn.commit()

                    if previous_user_id and previous_user_id != claimer_user_id:

                        logger.info(
                            f"OTP: Профіль викладача {teacher_row['full_name']} перев'язано з user_id {previous_user_id} на {claimer_user_id}."
                        )

                    else:

                        logger.info(
                            f"OTP: Профіль викладача {teacher_row['full_name']} успішно прив'язано до user_id {claimer_user_id}."
                        )

                    return (True, "Вхід успішний!")

            return (False, "Невірний пароль.")

    except Exception as e:

        logger.error(f"OTP: Критична помилка під час верифікації OTP: {e}")

        return (False, "Сталася системна помилка. Спробуйте пізніше.")


def update_command_stats(command_name: str) -> None:

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute(
                """

                INSERT INTO command_stats (command, count) VALUES (?, 1)

                ON CONFLICT(command) DO UPDATE SET count = count + 1

            """,
                (command_name,),
            )

            conn.commit()

    except sqlite3.Error as e:

        logger.error(f"Статистика: Помилка оновлення лічильника для '{command_name}': {e}")


def add_to_dlq(user_id: int, message_text: str, error_message: str) -> None:

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute(
                """

                INSERT INTO dead_letter_queue (user_id, message_text, error_message, status)

                VALUES (?, ?, ?, 'new')

            """,
                (user_id, message_text, error_message),
            )

            conn.commit()

        logger.info(f"DLQ: Повідомлення для {user_id} додано до DLQ. Помилка: {error_message}")

    except sqlite3.Error as e:

        logger.error(f"DLQ: Помилка запису в DLQ для {user_id}: {e}")


def clear_dlq(status: str = "new", older_than_days: int = 30) -> int:
    """

    Очищає записи з Dead Letter Queue.

    Видаляє записи зі вказаним статусом, старші ніж older_than_days.

    Повертає кількість видалених записів.

    """

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            # Визначаємо дату, до якої записи вважаються "старими"

            threshold_date = datetime.now(KYIV_TZ) - timedelta(days=older_than_days)

            threshold_date_str = threshold_date.isoformat()

            # Видаляємо записи, які відповідають статусу і старші за поріг

            cursor.execute(
                "DELETE FROM dead_letter_queue WHERE status = ? AND failed_at < ?",
                (status, threshold_date_str),
            )

            deleted_count = cursor.rowcount

            conn.commit()

            logger.info(
                f"DLQ: Видалено {deleted_count} записів зі статусом '{status}' старших за {older_than_days} днів."
            )

            return deleted_count

    except sqlite3.Error as e:

        logger.error(f"DLQ: Помилка під час очищення DLQ: {e}")

        return -1


def get_cached_schedule():

    global schedule_cache, sql_manager

    if sql_manager is None:

        logger.error("SQLManager не ініціалізовано. Неможливо завантажити розклад.")

        return {"розклади_груп": {}, "дзвінки": []}

    if schedule_cache is None:

        logger.info("Кеш розкладу порожній. Завантаження з бази даних через SQLManager...")

        try:

            schedule_cache = sql_manager.get_info()

            if (
                not schedule_cache
                or "розклади_груп" not in schedule_cache
                or "дзвінки" not in schedule_cache
            ):

                logger.error("SQLManager.get_info() повернув некоректні або неповні дані.")

                schedule_cache = {"розклади_груп": {}, "дзвінки": []}

            else:

                logger.info("Розклад успішно завантажено з БД та кешовано.")

        except Exception as e:

            logger.error(f"Помилка завантаження розкладу з БД через SQLManager: {e}", exc_info=True)

            schedule_cache = {"розклади_груп": {}, "дзвінки": []}

    return schedule_cache


def clear_schedule_cache_data():

    global schedule_cache

    schedule_cache = None

    logger.info("Кеш розкладу очищено. Наступний запит оновить його з БД.")


def load_specialties_data():

    global specialties_cache

    if specialties_cache is None:

        logger.info(
            f"Кеш спеціальностей порожній. Завантаження з файлу '{SPECIALTIES_JSON_FILE}'..."
        )

        if not os.path.exists(SPECIALTIES_JSON_FILE):

            logger.error(
                f"Файл спеціальностей '{SPECIALTIES_JSON_FILE}' не знайдено! Функціонал спеціальностей не працюватиме."
            )

            specialties_cache = {"specialties": {}}

            return specialties_cache

        try:

            with open(SPECIALTIES_JSON_FILE, "r", encoding="utf-8") as f:

                specialties_cache = json.load(f)

            logger.info(f"Спеціальності успішно завантажено з '{SPECIALTIES_JSON_FILE}'.")

        except json.JSONDecodeError as e:

            logger.error(f"Помилка декодування JSON у файлі '{SPECIALTIES_JSON_FILE}': {e}")

            specialties_cache = {"specialties": {}}

        except Exception as e:

            logger.error(f"Невідома помилка при завантаженні спеціальностей: {e}")

            specialties_cache = {"specialties": {}}

    return specialties_cache.get("specialties", {})


def load_admission_docs_data():

    global admission_docs_cache

    if admission_docs_cache is None:

        logger.info(
            f"Кеш документів для вступу порожній. Завантаження з файлу '{ADMISSION_DOCS_JSON_FILE}'..."
        )

        if not os.path.exists(ADMISSION_DOCS_JSON_FILE):

            logger.error(
                f"Файл документів для вступу '{ADMISSION_DOCS_JSON_FILE}' не знайдено! Функціонал документів не працюватиме."
            )

            admission_docs_cache = {}

            return admission_docs_cache

        try:

            with open(ADMISSION_DOCS_JSON_FILE, "r", encoding="utf-8") as f:

                admission_docs_cache = json.load(f)

            logger.info(f"Документи для вступу успішно завантажено з '{ADMISSION_DOCS_JSON_FILE}'.")

        except json.JSONDecodeError as e:

            logger.error(f"Помилка декодування JSON у файлі '{ADMISSION_DOCS_JSON_FILE}': {e}")

            admission_docs_cache = {}

        except Exception as e:

            logger.error(f"Невідома помилка при завантаженні документів для вступу: {e}")

            admission_docs_cache = {}

    return admission_docs_cache


def get_admission_docs_by_form(form_type: str) -> dict | None:

    all_docs = load_admission_docs_data()

    return all_docs.get(
        f"{form_type}_form"
    )  # Використовуємо f-рядок для "day_form" або "extramural_form"


def get_all_specialties() -> dict:

    return load_specialties_data()


def get_specialty_by_id(specialty_id: str) -> dict | None:

    all_specialties = get_all_specialties()

    return all_specialties.get(specialty_id)


def get_all_group_names_from_cache() -> list[str]:

    global sql_manager

    if sql_manager is None:

        logger.warning("SQLManager не ініціалізовано при спробі отримати назви груп.")

        cache = get_cached_schedule()

        return sorted(list(cache.get("розклади_груп", {}).keys()))

    try:

        groups_static_data = sql_manager.get_static().get("Groups", {})

        group_names = [details["Name"] for details in groups_static_data.values()]

        return sorted(group_names)

    except Exception as e:

        logger.error(f"Помилка отримання назв груп з SQLManager: {e}", exc_info=True)

        cache_fallback = schedule_cache if schedule_cache else {"розклади_груп": {}}

        return sorted(list(cache_fallback.get("розклади_груп", {}).keys()))


def get_schedule_data_for_group(group_name: str) -> dict | None:

    cache = get_cached_schedule()

    return cache.get("розклади_груп", {}).get(group_name)


def get_current_week_type_for_schedule(current_date: datetime) -> str:

    # Якщо current_date не має timezone info, додаємо його

    if current_date.tzinfo is None or current_date.tzinfo.utcoffset(current_date) is None:

        current_date = current_date.replace(tzinfo=KYIV_TZ)

    delta_days = (current_date - BASE_DATE).days

    weeks_passed = delta_days // 7

    return "чисельник" if weeks_passed % 2 == 0 else "знаменник"


# --- Функції для реферальної системи ---


def increment_referred_count(user_id: int) -> None:

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute(
                "UPDATE users SET referred_count = referred_count + 1 WHERE user_id = ?", (user_id,)
            )

            conn.commit()

        logger.info(f"Лічильник рефералів для користувача {user_id} збільшено.")

    except sqlite3.Error as e:

        logger.error(f"Помилка збільшення лічильника рефералів для {user_id}: {e}")


def get_referred_count(user_id: int) -> int:

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute("SELECT referred_count FROM users WHERE user_id = ?", (user_id,))

            result = cursor.fetchone()

            return result[0] if result else 0

    except sqlite3.Error as e:

        logger.error(f"Помилка отримання лічильника рефералів для {user_id}: {e}")

        return 0


def set_raffle_participant_status(user_id: int, status: bool) -> bool:

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            current_time = datetime.now(KYIV_TZ).isoformat() if status else None

            cursor.execute(
                "UPDATE users SET is_raffle_participant = ?, raffle_participation_date = ? WHERE user_id = ?",
                (status, current_time, user_id),
            )

            conn.commit()

        logger.info(f"Статус учасника розіграшу для {user_id} встановлено на {status}.")

        return True

    except sqlite3.Error as e:

        logger.error(f"Помилка встановлення статусу учасника розіграшу для {user_id}: {e}")

        return False


def get_raffle_participant_status(user_id: int) -> bool:

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute("SELECT is_raffle_participant FROM users WHERE user_id = ?", (user_id,))

            result = cursor.fetchone()

            return bool(result[0]) if result else False

    except sqlite3.Error as e:

        logger.error(f"Помилка отримання статусу учасника розіграшу для {user_id}: {e}")

        return False


def user_exists(user_id: int) -> bool:

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))

            return cursor.fetchone() is not None

    except sqlite3.Error as e:

        logger.error(f"Помилка перевірки існування користувача {user_id}: {e}")

        return False


# --- Клавіатури ---

# Нові клавіатури для вибору ролі та курсу


def get_role_selection_keyboard() -> InlineKeyboardMarkup:

    keyboard = [
        [InlineKeyboardButton("🎓 Я студент", callback_data="select_role_student")],
        [InlineKeyboardButton("👨‍🏫 Я викладач", callback_data="select_role_teacher")],
        [InlineKeyboardButton("🚶‍♂️ Я гість", callback_data="select_role_guest")],
        [InlineKeyboardButton("👷‍♂️ Я працівник", callback_data="select_role_staff")],
    ]

    return InlineKeyboardMarkup(keyboard)


def get_student_course_selection_keyboard() -> InlineKeyboardMarkup:

    keyboard = [
        [InlineKeyboardButton("Курс 1", callback_data="select_course_1")],
        [InlineKeyboardButton("Курс 2", callback_data="select_course_2")],
        [InlineKeyboardButton("Курс 3", callback_data="select_course_3")],
        [InlineKeyboardButton("Курс 4", callback_data="select_course_4")],
        [InlineKeyboardButton("⬅️ Назад до вибору ролі", callback_data="back_to_role_selection")],
    ]

    return InlineKeyboardMarkup(keyboard)


def get_back_to_role_selection_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад до вибору ролі", callback_data="back_to_role_selection")]]
    )


def get_floor_by_auditorium(auditorium: str) -> str:
    """Визначає поверх за номером аудиторії"""

    if not auditorium or auditorium.lower() in ["с/з", "спортзал", "спорт", ""]:

        return ""

    # Видаляємо всі нецифрові символи для перевірки

    import re

    clean_aud = re.sub(r"[^\d]", "", str(auditorium))

    if not clean_aud:

        return ""

    aud_num = int(clean_aud)

    # 1 поверх: ауд. 1, 2, 3, 4, 5, 6, 9, 10

    if aud_num in [1, 2, 3, 4, 5, 6, 9, 10]:

        return "1 Поверх"

    # 2 поверх: ауд. 16, 17, 18, 19, 21

    elif aud_num in [16, 17, 18, 19, 21]:

        return "2 Поверх"

    # 3 поверх: ауд. 26, 28, 29, 30, 31, 32, 33, 34, 37, 38, 39, 42, 41

    elif aud_num in [26, 28, 29, 30, 31, 32, 33, 34, 37, 38, 39, 42, 41]:

        return "3 Поверх"

    # 4 поверх: ауд. 43, 44, 45, 46, 47, 48, 49, 50, 52, 63, 53, 54, 55, 56

    elif aud_num in [43, 44, 45, 46, 47, 48, 49, 50, 52, 63, 53, 54, 55, 56]:

        return "4 Поверх"

    # Якщо аудиторія не знайдена в списку, повертаємо порожній рядок

    return ""


def get_textbooks_menu_keyboard() -> InlineKeyboardMarkup:
    """Створює клавіатуру з навчальними книжками"""

    keyboard = [
        [
            InlineKeyboardButton(
                "📖 Українська мова 10 клас Авраменко",
                url="https://pidruchnyk.com.ua/1168-ukrainska-mova-10-klas-avramenko.html",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 Українська мова 11 клас Авраменко",
                url="https://pidruchnyk.com.ua/1239-ukrainska-mova-11-klas-avramenko.html",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 Українська література 10 клас Авраменко",
                url="https://pidruchnyk.com.ua/392-ukrayinska-lteratura-avramenko-paharenko-10-klas.html",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 Українська література 11 клас Авраменко",
                url="https://pidruchnyk.com.ua/1237-ukrliteratura-avramenko-11klas.html",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 Зарубіжна література 10 клас Ніколенко",
                url="https://pidruchnyk.com.ua/1146-zarubizhna-literatura-10-klas-nikolenko.html",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 Зарубіжна література 11 клас Ніколенко",
                url="https://pidruchnyk.com.ua/1256-zarubizhna-literatura-11-klas-nikolenko.html",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 Англійська мова 10 клас Карпюк",
                url="https://pidruchnyk.com.ua/425-anglyska-mova-karpyuk-10-klas.html",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 Англійська мова 11 клас Карпюк",
                url="https://pidruchnyk.com.ua/454-anglyska-mova-karpyuk-11-klas.html",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 Граматика англійської мови Верба",
                url="https://ifccyc1.pnu.edu.ua/wp-content/uploads/sites/106/2019/12/%D0%93%D1%80%D0%B0%D0%BC%D0%B0%D1%82%D0%B8%D0%BA%D0%B0-%D0%B0%D0%BD%D0%B3%D0%BB.pdf",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 Logistics Victoria Evans",
                url="https://www.expresspublishing.co.uk/files/Logistics.pdf",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 Information technology Victoria Evans",
                url="https://www.expresspublishing.co.uk/files/Informechnew.pdf",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 Hotels and Catering Virginia Evans",
                url="https://language-teachings.com/wp-content/uploads/2021/04/Virginia-Evans.-Hotels-Catering.pdf",
            )
        ],
        [
            InlineKeyboardButton(
                "📖 English for logistics Marion Grussendorf",
                url="https://mzientek.v.prz.edu.pl/fcp/qGBUKOQtTKlQhbx08SlkTVARGUWRuHQwFDBoIVURNWHxSFVZpCFghUHcKVigEQUw/704/englishlogisticsbook-1.pdf",
            )
        ],
        [InlineKeyboardButton("⬅️ Назад до головного меню", callback_data="back_to_main_menu")],
    ]

    return InlineKeyboardMarkup(keyboard)


def get_guest_menu_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "ℹ️ Інформація про коледж", callback_data="about_college_from_guest"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Назад до вибору, щоб увійти", callback_data="back_to_role_selection"
                )
            ],
        ]
    )


def get_about_college_menu_keyboard(user_role: str | None = None) -> InlineKeyboardMarkup:

    keyboard_buttons = [
        [InlineKeyboardButton("📚 Спеціальності", callback_data="about_college_specialties")],
        [InlineKeyboardButton("🌟 Чому ми?", callback_data="about_college_why_us")],
        [
            InlineKeyboardButton(
                "📄 Документи для вступу", callback_data="about_college_admission_docs"
            )
        ],
        [InlineKeyboardButton("📞 Зв'язки з нами", callback_data="about_college_contacts")],
        [InlineKeyboardButton("🌐 Соцмережі", callback_data="about_college_social_media")],
        [
            InlineKeyboardButton(
                "🗺️ Віртуальна екскурсія",
                url="https://view.genially.com/66578acef2390b0015a681f8/interactive-image-virtualna-ekskursiya-mistom",
            )
        ],
    ]

    if user_role == "guest":

        keyboard_buttons.append(
            [
                InlineKeyboardButton(
                    "⬅️ Назад до вибору, щоб увійти", callback_data="back_to_role_selection"
                )
            ]
        )

    else:

        keyboard_buttons.append(
            [InlineKeyboardButton("⬅️ Назад до головного меню", callback_data="back_to_main_menu")]
        )

    return InlineKeyboardMarkup(keyboard_buttons)


def get_specialties_list_keyboard() -> InlineKeyboardMarkup:

    all_specialties = get_all_specialties()

    specialty_keys = sorted(list(all_specialties.keys()))

    emoji_map = {"F2": "💻", "G19": "🏗️", "G16": "🛢️", "D5": "📈", "D7": "🛒", "D2": "💰"}

    if not specialty_keys:

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Немає доступних спеціальностей", callback_data="no_specialties"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Назад до меню коледжу", callback_data="back_to_about_college_menu"
                    )
                ],
            ]
        )

    keyboard = []

    for specialty_id in specialty_keys:

        specialty_info = all_specialties.get(specialty_id)

        if specialty_info:

            emoji = emoji_map.get(specialty_id, "")

            button_text = f"{emoji} {specialty_info.get('name', specialty_id)}"

            keyboard.append(
                [
                    InlineKeyboardButton(
                        button_text.strip(), callback_data=f"show_specialty_details_{specialty_id}"
                    )
                ]
            )

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ Назад до меню коледжу", callback_data="back_to_about_college_menu"
            )
        ]
    )

    return InlineKeyboardMarkup(keyboard)


def get_back_to_specialties_list_keyboard(user_role: str | None = None) -> InlineKeyboardMarkup:

    # Оновлюємо callback_data для "Назад до меню коледжу" в залежності від ролі

    back_to_college_callback = (
        "about_college_from_guest" if user_role == "guest" else "back_to_about_college_menu"
    )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Назад до списку спеціальностей", callback_data="about_college_specialties"
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Назад до меню коледжу", callback_data=back_to_college_callback
                )
            ],
        ]
    )


def get_back_to_about_college_menu_keyboard(
    user_role: str | None = None,
) -> InlineKeyboardMarkup:  # ДОДАНО user_role

    callback_data = "about_college_from_guest" if user_role == "guest" else "about_college"

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад до меню коледжу", callback_data=callback_data)]]
    )


def get_admission_form_selection_keyboard(user_role: str | None = None) -> InlineKeyboardMarkup:

    # Оновлюємо callback_data для "Назад до меню коледжу" в залежності від ролі

    back_to_college_callback = (
        "about_college_from_guest" if user_role == "guest" else "back_to_about_college_menu"
    )

    keyboard = [
        [InlineKeyboardButton("📚 Денна форма навчання", callback_data="show_admission_docs_day")],
        [
            InlineKeyboardButton(
                "📖 Заочна форма навчання", callback_data="show_admission_docs_extramural"
            )
        ],
        # --- НОВЕ: Додаємо кнопку "Абітурієнту" тут, над кнопкою "Назад" ---
        [InlineKeyboardButton("🙋 Абітурієнту", url="https://dvnzchgek.edu.ua/abituriyentu")],
        # ------------------------------------------------------------------
        [InlineKeyboardButton("⬅️ Назад до меню коледжу", callback_data=back_to_college_callback)],
    ]

    return InlineKeyboardMarkup(keyboard)


def get_back_to_admission_form_selection_keyboard(
    user_role: str | None = None,
) -> InlineKeyboardMarkup:

    # Оновлюємо callback_data для "Назад до меню коледжу" в залежності від ролі

    back_to_college_callback = (
        "about_college_from_guest" if user_role == "guest" else "back_to_about_college_menu"
    )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Назад до вибору форми", callback_data="about_college_admission_docs"
                )
            ],  # Повертає до вибору денна/заочна
            [
                InlineKeyboardButton(
                    "🏠 Назад до меню коледжу", callback_data=back_to_college_callback
                )
            ],
        ]
    )


# -------------------------------------


# --- Обробники для меню "Про коледж" ---


async def about_college_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query

    if query and query.from_user:

        user_id = query.from_user.id

    else:

        user_id = update.effective_user.id if update.effective_user else None

    user_role = get_user_role_from_db(user_id) if user_id is not None else None  # Отримуємо роль

    if query:

        await query.answer()

    text = "ℹ️ *Про коледж: Що вас цікавить?*"

    reply_markup = get_about_college_menu_keyboard(user_role=user_role)  # Передаємо роль

    if query and query.message:

        try:

            await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")  # type: ignore

        except telegram.error.BadRequest as e:

            if "Message is not modified" in str(e):

                logger.debug(
                    f"Повідомлення не модифіковано (кнопка back_to_about_college_menu): {e}"
                )

            else:

                logger.error(
                    f"Помилка при редагуванні повідомлення в about_college_handler: {e}",
                    exc_info=True,
                )

    elif update.message:

        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")  # type: ignore

    else:

        logger.warning("about_college_handler викликано без відповідного об'єкта Update.")


async def show_specialties_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query

    user_id = None

    if query and query.from_user:

        user_id = query.from_user.id

    elif update.effective_user:

        user_id = update.effective_user.id

    if user_id is None:

        logger.warning("show_specialties_list_handler: не вдалося отримати user_id")

        return

    user_role = get_user_role_from_db(user_id)  # Отримуємо роль

    text = "📚 *Оберіть спеціальність:*"

    reply_markup = get_specialties_list_keyboard()

    # Модифікуємо кнопку "Назад до меню коледжу", якщо користувач гість

    if user_role == "guest":

        modified_keyboard_buttons = []

        for row in reply_markup.inline_keyboard:

            new_row = []

            for button in row:

                if button.callback_data == "back_to_about_college_menu":

                    new_row.append(
                        InlineKeyboardButton(
                            "⬅️ Назад до меню коледжу", callback_data="about_college_from_guest"
                        )
                    )

                else:

                    new_row.append(button)

            modified_keyboard_buttons.append(new_row)

        reply_markup = InlineKeyboardMarkup(modified_keyboard_buttons)

    if query:

        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")  # type: ignore

    elif update.message:

        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")  # type: ignore


async def show_specialty_details_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning(
            "show_specialty_details_handler викликано без відповідного query або from_user"
        )

        return

    user_id = query.from_user.id  # Отримуємо user_id з query

    user_role = get_user_role_from_db(user_id)  # Отримуємо роль

    if not query.data:

        logger.warning("show_specialty_details_handler викликано без query.data")

        return

    specialty_id = query.data.replace("show_specialty_details_", "")

    specialty_info = get_specialty_by_id(specialty_id)

    if not specialty_info:

        await query.edit_message_text(
            "❌ Інформацію про спеціальність не знайдено.",
            reply_markup=get_back_to_specialties_list_keyboard(user_role=user_role),
        )  # Передаємо роль

        return

    text = f"📚 *{specialty_info.get('name', 'Невідома спеціальність').upper()}*\n\n"

    for detail_block in specialty_info.get("details", []):

        text += f"*{detail_block.get('title', '')}:*\n"

        for point in detail_block.get("points", []):

            text += f"  • {point}\n"

        text += "\n"

    if specialty_info.get("duration"):

        text += f"*Термін навчання:* {specialty_info.get('duration')}\n\n"

    reply_markup = get_back_to_specialties_list_keyboard(user_role=user_role)  # Передаємо роль

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")  # type: ignore


def get_why_us_text() -> str:

    return (
        "🌟 *Чому саме наш коледж?*\n\n"
        "• *ПРОФЕСІЙНИЙ ПЕДАГОГІЧНИЙ КОЛЕКТИВ:*\n"
        "  наш висококваліфікований та досвідчений педагогічний колектив гарантує якісну освіту та індивідуальний підхід до кожного студента.\n\n"
        "• *СУЧАСНА МАТЕРІАЛЬНО-ТЕХНІЧНА БАЗА:*\n"
        "  завдяки сучасній матеріально-технічній базі наші студенти мають можливість вчитися та досліджувати в новітніх умовах, що сприяє їхньому академічному розвитку.\n\n"
        "• *ПРАКТИКА:*\n"
        "  наша програма включає практичний підхід до навчання, що дозволяє студентам отримати реальний досвід у своїй галузі та готує їх до успішного впровадження у професійну діяльність.\n\n"
        "• *ВЛАСНІ УКРИТТЯ:*\n"
        "  коледж забезпечує безпекові умови для студентів, маючи власні укриття, де кожен може знаходитися під час повітряних тривог, гарантуючи їхню захищеність та спокій у таких ситуаціях.\n\n"
        "• *ВЛАСНА ЇДАЛЬНЯ:*\n"
        "  наша власна комфортна їдальня гарантує смачне та збалансоване харчування для студентів.\n\n"
        "• *ГУРТОЖИТОК:*\n"
        "  надаючи комфортні умови проживання, наш гуртожиток з сучасним ремонтом забезпечує студентам затишок та безпеку. Гуртожиток знаходиться поруч з коледжем та має 175 місць для проживання."
    )


async def about_college_why_us_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query

    user_id = None

    if query and query.from_user:

        user_id = query.from_user.id

    elif update.effective_user:

        user_id = update.effective_user.id

    if user_id is None:

        logger.warning("about_college_why_us_handler: не вдалося отримати user_id")

        return

    user_role = get_user_role_from_db(user_id)  # Отримуємо роль

    if query:

        await query.answer()

    text = get_why_us_text()

    reply_markup = get_back_to_about_college_menu_keyboard(user_role=user_role)  # Передаємо роль

    if query and query.message:

        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")  # type: ignore

    else:

        logger.warning("about_college_why_us_handler викликано без відповідного об'єкта Update.")


def get_contacts_info_text() -> str:

    return (
        "📞 *Зв'язки з нами:*\n\n"
        "🏢 *Адреса:*\n"
        "  м. Шептицький\n"
        "  вул. Василя Стуса, 17\n\n"
        "🗓️ *Графік приймальної комісії:*\n"
        "  Пн-Пт: 09:00 - 17:00\n"
        "  Сб: 09:00 - 14:00\n"
        "  Нд: вихідний\n\n"
        "☎️ *Телефони:*\n"
        "  Приймальна: `+38 (032) 493 11 47`\n"
        "  Гуртожиток: `+38 (032) 493 15 03`\n\n"
        "📧 *Email:*\n"
        "  `cher.collage@gmail.com`"
    )


async def about_college_contacts_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    query = update.callback_query

    user_id = None

    if query and query.from_user:

        user_id = query.from_user.id

    elif update.effective_user:

        user_id = update.effective_user.id

    if user_id is None:

        logger.warning("about_college_contacts_handler: не вдалося отримати user_id")

        return

    user_role = get_user_role_from_db(user_id)  # Отримуємо роль

    if query:

        await query.answer()

    text = get_contacts_info_text()

    reply_markup = get_back_to_about_college_menu_keyboard(user_role=user_role)  # Передаємо роль

    if query and query.message:

        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")  # type: ignore

    else:

        logger.warning("about_college_contacts_handler викликано без відповідного об'єкта Update.")


### НОВЕ: Функція для отримання тексту "Соцмережі"


def get_social_media_text() -> str:

    return (
        "🌐 *Наші соцмережі:*\n\n"
        "🔗 [Наш сайт](https://dvnzchgek.edu.ua/)\n"
        "📘 [Facebook](https://www.facebook.com/koledzh.org/)\n"
        "📸 [Instagram](https://www.instagram.com/gefk_ua/)\n"
        "✈️ [Telegram](https://t.me/chgek)\n"
    )


### НОВЕ: Обробник для кнопки "Соцмережі"


async def about_college_social_media_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    query = update.callback_query

    user_id = None

    if query and query.from_user:

        user_id = query.from_user.id

    elif update.effective_user:

        user_id = update.effective_user.id

    if user_id is None:

        logger.warning("about_college_social_media_handler: не вдалося отримати user_id")

        return

    user_role = get_user_role_from_db(user_id)  # Отримуємо роль

    if query:

        await query.answer()

    text = get_social_media_text()

    reply_markup = get_back_to_about_college_menu_keyboard(user_role=user_role)  # Передаємо роль

    if query and query.message:

        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=False)  # type: ignore

    else:

        logger.warning(
            "about_college_social_media_handler викликано без відповідного об'єкта Update."
        )


async def show_textbooks_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник для показу меню навчальних книжок"""

    query = update.callback_query

    if not query:

        return

    await query.answer()

    text = """📚 *Навчальні книжки*



Оберіть підручник, який вас цікавить. При натисканні на назву книжки вона відкриється в браузері.



*Доступні підручники:*"""

    reply_markup = get_textbooks_menu_keyboard()

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


# --- НОВІ ОБРОБНИКИ ДЛЯ ДОКУМЕНТІВ ---


async def about_college_admission_docs_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning(
            "about_college_admission_docs_handler: не вдалося отримати query або from_user"
        )

        return SELECTING_ADMISSION_FORM

    user_id = query.from_user.id

    user_role = get_user_role_from_db(user_id)

    if query:

        await query.answer()

    text = "📄 *Документи для вступу: Оберіть форму навчання:*"

    reply_markup = get_admission_form_selection_keyboard(user_role=user_role)

    if query and query.message:

        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")  # type: ignore

    elif update.message:  # Це на випадок, якщо викликаємо не через callback

        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")  # type: ignore

    return SELECTING_ADMISSION_FORM  # Переходимо в новий стан розмови


# Функція для виведення списку документів (поки заглушка)


async def show_admission_docs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning("show_admission_docs_handler: не вдалося отримати query або from_user")

        return VIEWING_ADMISSION_DOCS

    user_id = query.from_user.id

    user_role = get_user_role_from_db(user_id)

    if query:

        await query.answer()

    if not query.data:

        logger.warning("show_admission_docs_handler: не вдалося отримати query.data")

        return VIEWING_ADMISSION_DOCS

    form_type = query.data.replace("show_admission_docs_", "")  # 'day' або 'extramural'

    docs_data = get_admission_docs_by_form(form_type)

    if not docs_data:

        text = "❌ Інформацію про документи для цієї форми навчання не знайдено."

    else:

        text = f"📄 *{docs_data.get('title', 'Документи для вступу')}*\n\n"

        # Блок "admission_dates" був видалений з JSON і тому не буде тут відображатися.

        # Якщо знову знадобиться, його треба буде додати назад в JSON і тоді в цей код.

        text += "*ПЕРЕЛІК ДОКУМЕНТІВ:*\n"

        if "required_documents" in docs_data and docs_data["required_documents"]:

            for doc in docs_data["required_documents"]:

                text += f"• {doc}\n"

        else:

            text += "_Список документів відсутній._\n"

        # Додаємо додаткові примітки, якщо вони є

        if docs_data.get("additional_notes"):

            text += f"\n_{docs_data['additional_notes']}_"

    reply_markup = get_back_to_admission_form_selection_keyboard(user_role=user_role)

    if query and query.message:

        try:

            await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)  # type: ignore

        except telegram.error.BadRequest as e:

            if "Message is not modified" in str(e):

                logger.debug(f"Повідомлення документів не модифіковано: {e}")

            else:

                logger.error(
                    f"Помилка при редагуванні повідомлення show_admission_docs_handler: {e}",
                    exc_info=True,
                )

                # Якщо не вдалося відредагувати, спробуємо надіслати нове

                await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)  # type: ignore

    elif update.message:

        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True)  # type: ignore

    return VIEWING_ADMISSION_DOCS


# --- Клавіатури ---

# Оновлений код


def get_main_menu_keyboard(user_id: int, user_group: str | None) -> InlineKeyboardMarkup:

    group_text = f" ({user_group})" if user_group else " (Група не обрана)"

    keyboard_buttons = [
        [
            InlineKeyboardButton(
                f"📅 Показати розклад{group_text}", callback_data="show_schedule_menu"
            )
        ],
        [
            InlineKeyboardButton(
                "🌐 Відкрити веб‑додаток",
                web_app=telegram.WebAppInfo(url=_build_webapp_url_for_user(user_id)),
            )
        ],
        [InlineKeyboardButton("🔄 Змінити/Вказати групу", callback_data="change_set_group_prompt")],
        [InlineKeyboardButton("📚 Навчальні книжки", callback_data="show_textbooks_menu")],
        [InlineKeyboardButton("ℹ️ Про коледж", callback_data="about_college")],
        [InlineKeyboardButton("💰 Підтримати бота", callback_data="show_donation_info")],
        [
            InlineKeyboardButton("📝 Анонімний відгук", callback_data="send_feedback_prompt")
        ],  # НОВА КНОПКА
        [
            InlineKeyboardButton(
                "💡 Як покращити коледж?", callback_data="suggest_improvement_prompt"
            )
        ],
        [
            InlineKeyboardButton(
                "💡 Пропозиція / 🐞 Повідомити про проблему",
                callback_data="report_bug_button_prompt",
            )
        ],
    ]

    if RAFFLE_ACTIVE and datetime.now(KYIV_TZ) < RAFFLE_END_DATE:

        # Вставка кнопки розіграшу перед "Як покращити коледж?" або "Повідомити про проблему"

        # Щоб вона була на логічному місці. Тепер 'Відгуки' на 4й позиції, тому 5

        insert_index = 5  # ЗМІНЕНО: тепер після "Відгуки"

        keyboard_buttons.insert(
            insert_index,
            [
                InlineKeyboardButton(
                    f"🎁 Розіграш {RAFFLE_PRIZE.upper()} 🎁", callback_data="show_raffle_info"
                )
            ],
        )

    if user_id in ADMIN_USER_IDS:

        keyboard_buttons.append(
            [InlineKeyboardButton("🛠️ Панель адміністратора", callback_data="show_admin_panel")]
        )

    return InlineKeyboardMarkup(keyboard_buttons)


# --- ДОДАЙТЕ ЦЮ НОВУ УНІВЕРСАЛЬНУ ФУНКЦІЮ ---


def get_correct_main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """

    Перевіряє роль користувача і повертає відповідну клавіатуру головного меню.

    """

    user_role = get_user_role_from_db(user_id)

    if user_role == "teacher":

        # Для викладача повертаємо меню викладача

        return get_teacher_menu_keyboard(user_id)

    else:

        # Для всіх інших (студент, гість, тощо) повертаємо стандартне меню

        user_group = get_user_group_from_db(user_id)

        return get_main_menu_keyboard(user_id, user_group)


# --- НОВІ ФУНКЦІЇ ДЛЯ МЕНЮ ВИКЛАДАЧА ---


def get_teacher_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:

    teacher_data = get_teacher_data_from_db(user_id)

    keyboard = [[InlineKeyboardButton("📅 Мій розклад", callback_data="teacher_my_schedule")]]

    # Додаємо кнопку для куратора, якщо є кураторська група

    if teacher_data and teacher_data.get("curated_group_name"):

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"🎓 Розклад моєї групи ({teacher_data['curated_group_name']})",
                    callback_data="teacher_curated_group_schedule",
                )
            ]
        )

    elif teacher_data and teacher_data.get("curated_group_name"):

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"🎓 Мій клас: {teacher_data['curated_group_name']} (в розробці)",
                    callback_data="teacher_curated_group",
                )
            ]
        )

    # Додаємо кнопку для перегляду розкладу будь-якої групи

    keyboard.append(
        [
            InlineKeyboardButton(
                "🔍 Розклад будь-якої групи", callback_data="teacher_any_group_schedule"
            )
        ]
    )

    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    "🌐 Відкрити веб‑додаток",
                    web_app=telegram.WebAppInfo(url=_build_webapp_url_for_user(user_id)),
                )
            ],
            [InlineKeyboardButton("📚 Навчальні книжки", callback_data="show_textbooks_menu")],
            [InlineKeyboardButton("ℹ️ Про коледж", callback_data="about_college")],
            [InlineKeyboardButton("💰 Підтримати бота", callback_data="show_donation_info")],
            [
                InlineKeyboardButton(
                    "💡 Пропозиція / 🐞 Повідомити про проблему",
                    callback_data="report_bug_button_prompt",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 Змінити роль / Вийти", callback_data="back_to_role_selection"
                )
            ],
        ]
    )

    if user_id in ADMIN_USER_IDS:

        keyboard.append(
            [InlineKeyboardButton("🛠️ Панель адміністратора", callback_data="show_admin_panel")]
        )

    return InlineKeyboardMarkup(keyboard)


async def show_teacher_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    user = update.effective_user

    if not user:

        logger.warning("show_teacher_menu_handler викликано без effective_user")

        return

    teacher_data = get_teacher_data_from_db(user.id)

    teacher_name = teacher_data.get("full_name", user.full_name) if teacher_data else user.full_name

    text = f"Вітаю, *{teacher_name}*!\nВи увійшли як викладач. Чим можу допомогти?"

    reply_markup = get_teacher_menu_keyboard(user.id)

    if update.callback_query:

        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )

    elif update.message:

        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def teacher_my_schedule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query

    if not query:

        logger.warning("teacher_my_schedule_handler викликано без callback_query")

        return

    await query.answer("Цей функціонал наразі в розробці.", show_alert=True)


async def teacher_curated_group_schedule_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning("teacher_curated_group_schedule_handler викликано без query або from_user")

        return

    user = query.from_user

    teacher_data = get_teacher_data_from_db(user.id)

    if not teacher_data or not teacher_data.get("curated_group_name"):

        await query.answer("У вас не вказано кураторську групу.", show_alert=True)

        return

    group_name = teacher_data["curated_group_name"]

    group_schedule_data = get_schedule_data_for_group(group_name)

    if not group_schedule_data:

        await query.edit_message_text(
            f"На жаль, розклад для групи *{group_name}* не знайдено.",
            reply_markup=get_teacher_menu_keyboard(user.id),
            parse_mode="Markdown",
        )

        return

    # Зберігаємо групу в контексті для подальшого використання

    if context.user_data is None:

        context.user_data = {}

    context.user_data["curated_group_name"] = group_name

    text = f"📅 Меню розкладу для групи: *{group_name}*.\nОберіть пункт:"

    reply_markup = get_schedule_menu_keyboard(group_name)

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def teacher_any_group_schedule_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning("teacher_any_group_schedule_handler викликано без query або from_user")

        return

    # Позначаємо, що викладач переглядає розклад будь-якої групи

    if context.user_data is None:

        context.user_data = {}

    context.user_data["teacher_viewing_any_group"] = True

    # Крок 1: показуємо вибір курсу

    text = "🎓 Оберіть курс, а потім групу:"

    reply_markup = get_teacher_course_selection_keyboard()

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


def get_teacher_group_selection_keyboard(all_groups):

    keyboard = []

    row = []

    for group_name in all_groups:

        row.append(
            InlineKeyboardButton(group_name, callback_data=f"teacher_view_group_{group_name}")
        )

        if len(row) == 2:  # 2 групи в рядку для кращого відображення

            keyboard.append(row)

            row = []

    if row:

        keyboard.append(row)

    keyboard.append(
        [InlineKeyboardButton("⬅️ Назад до меню викладача", callback_data="back_to_main_menu")]
    )

    return InlineKeyboardMarkup(keyboard)


# --- ФУНКЦІЇ ДЛЯ РОЗКЛАДУ ВИКЛАДАЧІВ ---


def get_teacher_name_from_callback_data(callback_data: str, prefix: str) -> str:
    """Отримує оригінальне ім'я викладача з callback_data, замінюючи підкреслення на пробіли."""

    if not callback_data.startswith(prefix):

        return ""

    # Видаляємо префікс

    name_part = callback_data[len(prefix) :]

    # Замінюємо підкреслення на пробіли

    return name_part.replace("_", " ")


def normalize_teacher_name_for_matching(full_name: str) -> str:
    """Нормалізує повне ім'я викладача для зіставлення зі скороченим."""

    if not full_name:

        return ""

    # Розбиваємо на частини

    parts = full_name.strip().split()

    if len(parts) < 2:

        return full_name.strip()

    # Беремо прізвище та ініціали

    surname = parts[0]

    initials = ""

    for part in parts[1:]:

        if part and len(part) > 0:

            initials += part[0].upper() + "."

    return f"{surname} {initials}".strip()


def find_teacher_lessons_in_schedule(teacher_full_name: str) -> list:
    """Знаходить всі пари викладача в розкладі всіх груп."""

    schedule_data = get_cached_schedule()

    if not schedule_data or "розклади_груп" not in schedule_data:

        return []

    # Нормалізуємо ім'я викладача

    normalized_teacher_name = normalize_teacher_name_for_matching(teacher_full_name)

    teacher_lessons = []

    # Проходимо по всіх групах

    for group_name, group_data in schedule_data["розклади_груп"].items():

        if "тиждень" not in group_data:

            continue

        # Проходимо по всіх днях тижня

        for day_name, day_lessons in group_data["тиждень"].items():

            if not isinstance(day_lessons, list):

                continue

            # Проходимо по всіх парах дня

            for lesson in day_lessons:

                if not isinstance(lesson, dict) or "викладач" not in lesson:

                    continue

                lesson_teacher = lesson.get("викладач", "").strip()

                # Перевіряємо збіг імен та що це не "Немає пари"

                if (
                    lesson_teacher == normalized_teacher_name
                    or lesson_teacher == teacher_full_name
                    or lesson_teacher in teacher_full_name
                    or teacher_full_name in lesson_teacher
                ):

                    # Перевіряємо назву предмету

                    subject_name = lesson.get("назва", "").strip()

                    if subject_name and subject_name.lower() not in [
                        "виховна година",
                        "виховна",
                        "",
                    ]:

                        # Додаємо інформацію про групу до уроку

                        lesson_with_group = lesson.copy()

                        lesson_with_group["група"] = group_name

                        lesson_with_group["день"] = day_name

                        teacher_lessons.append(lesson_with_group)

    return teacher_lessons


def get_teacher_schedule_for_day(
    teacher_full_name: str, day_name: str, week_type: str = "завжди"
) -> str:
    """Генерує розклад викладача на конкретний день та тип тижня.



    Показує ВСІ пари по фіксованих слотах часу у правильному порядку.

    Якщо в слоті немає пари, виводить "Немає пари". Без зайвих порожніх рядків.

    """

    lessons = find_teacher_lessons_in_schedule(teacher_full_name)

    # Фільтруємо по дню та типу тижня

    day_lessons = []

    for lesson in lessons:

        if lesson.get("день") == day_name:

            lesson_week_type = lesson.get("тип_тижня", "завжди")

            if lesson_week_type == "завжди" or lesson_week_type == week_type:

                day_lessons.append(lesson)

    response_header = f"👨‍🏫 Розклад *{teacher_full_name}* на *{day_name}* ({week_type}):\n\n"

    # Якщо взагалі немає жодного запису на цей день і тип тижня

    if not day_lessons:

        return (
            response_header
            + "• 08:00-09:20 - Немає пари\n"
            + "• 09:30-10:50 - Немає пари\n"
            + "• 11:40-13:00 - Немає пари\n"
            + "• 13:10-14:30 - Немає пари"
        )

    # Групуємо пари за часом

    lessons_by_time: dict[str, list] = {}

    for lesson in day_lessons:

        time = lesson.get("час", "")

        if time not in lessons_by_time:

            lessons_by_time[time] = []

        lessons_by_time[time].append(lesson)

    # Фіксований порядок слотів часу

    canonical_times = [
        "08:00-09:20",
        "09:30-10:50",
        "11:40-13:00",
        "13:10-14:30",
    ]

    lines: list[str] = []

    for time in canonical_times:

        time_lessons = lessons_by_time.get(time, [])

        # Відкидаємо псевдо-записи типу "Немає пари"

        real_lessons = [
            lesson
            for lesson in time_lessons
            if lesson.get("назва", "").strip().lower() not in ["немає пари", "немає", "відсутня"]
        ]

        if real_lessons:

            # Може бути кілька груп у той самий час – виводимо кожну окремо

            for lesson in real_lessons:

                subject = lesson.get("назва", "")

                group = lesson.get("група", "")

                room = lesson.get("аудиторія", "")

                lines.append(f"• {time} - {subject}")

                # Формуємо інформацію про аудиторію з поверхом

                room_info = ""

                if room:

                    floor = get_floor_by_auditorium(room)

                    if floor:

                        room_info = f"🏢 Ауд. {room} ({floor})"

                    else:

                        room_info = f"🏢 Ауд. {room}"

                else:

                    room_info = "🏢 Ауд. -"

                lines.append(f"  📚 Група: {group} | {room_info}")

        else:

            lines.append(f"• {time} - Немає пари")

    return (response_header + "\n".join(lines)).strip()


def get_full_teacher_schedule(teacher_full_name: str) -> str:
    """Генерує повний розклад викладача на тиждень."""

    lessons = find_teacher_lessons_in_schedule(teacher_full_name)

    if not lessons:

        return f"👨‍🏫 У *{teacher_full_name}* немає пар в розкладі."

    # Групуємо по днях та типах тижня

    schedule_by_day = {}

    for lesson in lessons:

        day = lesson.get("день", "")

        week_type = lesson.get("тип_тижня", "завжди")

        if day not in schedule_by_day:

            schedule_by_day[day] = {"завжди": [], "чисельник": [], "знаменник": []}

        if week_type in schedule_by_day[day]:

            schedule_by_day[day][week_type].append(lesson)

    # Сортуємо пари по часу в кожному дні

    for day_data in schedule_by_day.values():

        for week_type_lessons in day_data.values():

            week_type_lessons.sort(key=lambda x: x.get("час", ""))

    # Формуємо відповідь

    response = f"👨‍🏫 Повний розклад *{teacher_full_name}* на тиждень:\n\n"

    days_order = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]

    for day in days_order:

        if day not in schedule_by_day:

            continue

        day_data = schedule_by_day[day]

        has_lessons = any(day_data[wt] for wt in ["завжди", "чисельник", "знаменник"])

        if not has_lessons:

            continue

        response += f"*{day.capitalize()}*:\n"

        # Показуємо пари для кожного типу тижня

        for week_type in ["завжди", "чисельник", "знаменник"]:

            week_lessons = day_data.get(week_type, [])

            if week_lessons:

                week_type_display = (
                    "Чисельник"
                    if week_type == "чисельник"
                    else "Знаменник" if week_type == "знаменник" else "Завжди"
                )

                response += f"  {week_type_display}:\n"

                # Групуємо пари за часом

                lessons_by_time = {}

                for lesson in week_lessons:

                    time = lesson.get("час", "")

                    if time not in lessons_by_time:

                        lessons_by_time[time] = []

                    lessons_by_time[time].append(lesson)

                # Сортуємо по часу

                sorted_times = sorted(lessons_by_time.keys())

                for time in sorted_times:

                    time_lessons = lessons_by_time[time]

                    # Перевіряємо, чи є реальні пари (не "Немає пари")

                    real_lessons = [
                        lesson
                        for lesson in time_lessons
                        if lesson.get("назва", "").strip().lower()
                        not in ["немає пари", "немає", "відсутня"]
                    ]

                    if real_lessons:

                        # Показуємо реальні пари

                        for lesson in real_lessons:

                            subject = lesson.get("назва", "")

                            group = lesson.get("група", "")

                            room = lesson.get("аудиторія", "")

                            response += f"    • {time} - {subject} | {group} | Ауд. {room}\n"

                    else:

                        # Якщо тільки "Немає пари", показуємо один раз

                        response += f"    • {time} - Немає пари\n"

        response += "\n"

    return response.strip()


def get_teacher_schedule_by_week_type(teacher_full_name: str, week_type: str) -> str:
    """Генерує розклад викладача на конкретний тип тижня."""

    lessons = find_teacher_lessons_in_schedule(teacher_full_name)

    # Фільтруємо по типу тижня

    filtered_lessons = []

    for lesson in lessons:

        lesson_week_type = lesson.get("тип_тижня", "завжди")

        if lesson_week_type == "завжди" or lesson_week_type == week_type:

            filtered_lessons.append(lesson)

    if not filtered_lessons:

        week_type_display = (
            "Чисельник"
            if week_type == "чисельник"
            else "Знаменник" if week_type == "знаменник" else week_type
        )

        return f"👨‍🏫 У *{teacher_full_name}* немає пар на *{week_type_display}*."

    # Групуємо по днях

    schedule_by_day = {}

    for lesson in filtered_lessons:

        day = lesson.get("день", "")

        if day not in schedule_by_day:

            schedule_by_day[day] = []

        schedule_by_day[day].append(lesson)

    # Сортуємо пари по часу в кожному дні

    for day_lessons in schedule_by_day.values():

        day_lessons.sort(key=lambda x: x.get("час", ""))

    # Формуємо відповідь

    week_type_display = (
        "Чисельник"
        if week_type == "чисельник"
        else "Знаменник" if week_type == "знаменник" else week_type
    )

    response = f"👨‍🏫 Розклад *{teacher_full_name}* на *{week_type_display}*:\n\n"

    days_order = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]

    for day in days_order:

        if day not in schedule_by_day:

            continue

        day_lessons = schedule_by_day[day]

        if not day_lessons:

            continue

        response += f"*{day.capitalize()}*:\n"

        # Групуємо пари за часом

        lessons_by_time = {}

        for lesson in day_lessons:

            time = lesson.get("час", "")

            if time not in lessons_by_time:

                lessons_by_time[time] = []

            lessons_by_time[time].append(lesson)

        # Сортуємо по часу

        sorted_times = sorted(lessons_by_time.keys())

        for time in sorted_times:

            time_lessons = lessons_by_time[time]

            # Перевіряємо, чи є реальні пари (не "Немає пари")

            real_lessons = [
                lesson
                for lesson in time_lessons
                if lesson.get("назва", "").strip().lower()
                not in ["немає пари", "немає", "відсутня"]
            ]

            if real_lessons:

                # Показуємо реальні пари

                for lesson in real_lessons:

                    subject = lesson.get("назва", "")

                    group = lesson.get("група", "")

                    room = lesson.get("аудиторія", "")

                    response += f"  • {time} - {subject} | {group} | Ауд. {room}\n"

            else:

                # Якщо тільки "Немає пари", показуємо один раз

                response += f"  • {time} - Немає пари\n"

        response += "\n"

    return response.strip()


# --- КЛАВІАТУРИ ДЛЯ РОЗКЛАДУ ВИКЛАДАЧА ---


def get_teacher_schedule_menu_keyboard(teacher_id: int) -> InlineKeyboardMarkup:
    """Створює клавіатуру меню розкладу викладача."""

    keyboard = [
        [InlineKeyboardButton("📅 Розклад на сьогодні", callback_data=f"t_today_{teacher_id}")],
        [
            InlineKeyboardButton(
                "📋 Повний розклад на тиждень", callback_data=f"t_full_{teacher_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🔍 Розклад на конкретний день", callback_data=f"t_day_schedule_{teacher_id}"
            )
        ],
        [InlineKeyboardButton("⬅️ Назад до меню викладача", callback_data="back_to_main_menu")],
    ]

    return InlineKeyboardMarkup(keyboard)


# ДОДАНО: вибір курсу для викладача (перед вибором групи)


def get_teacher_course_selection_keyboard() -> InlineKeyboardMarkup:

    keyboard = [
        [InlineKeyboardButton("Курс 1", callback_data="teacher_select_course_1")],
        [InlineKeyboardButton("Курс 2", callback_data="teacher_select_course_2")],
        [InlineKeyboardButton("Курс 3", callback_data="teacher_select_course_3")],
        [InlineKeyboardButton("Курс 4", callback_data="teacher_select_course_4")],
        [InlineKeyboardButton("⬅️ Назад до меню викладача", callback_data="back_to_main_menu")],
    ]

    return InlineKeyboardMarkup(keyboard)


# ДОДАНО: вибір груп для викладача з фільтрацією за курсом


def get_teacher_group_selection_keyboard_by_course(selected_course: int) -> InlineKeyboardMarkup:

    all_groups = get_all_group_names_from_cache()

    if not all_groups:

        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Немає доступних груп", callback_data="no_groups_available")],
                [
                    InlineKeyboardButton(
                        "⬅️ Назад до вибору курсу", callback_data="teacher_any_group_schedule"
                    )
                ],
            ]
        )

    course_year_map = {1: "25", 2: "24", 3: "23", 4: "22"}

    target_year = course_year_map.get(selected_course, "25")

    filtered_groups = [
        group
        for group in all_groups
        if group.endswith(f"-{target_year}") or f"-{target_year} (" in group
    ]

    if not filtered_groups:

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Немає груп для {selected_course} курсу",
                        callback_data="no_groups_available",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Назад до вибору курсу", callback_data="teacher_any_group_schedule"
                    )
                ],
            ]
        )

    keyboard = []

    row = []

    for group_name in filtered_groups:

        row.append(
            InlineKeyboardButton(group_name, callback_data=f"teacher_view_group_{group_name}")
        )

        if len(row) == 3:

            keyboard.append(row)

            row = []

    if row:

        keyboard.append(row)

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ Назад до вибору курсу", callback_data="teacher_any_group_schedule"
            )
        ]
    )

    return InlineKeyboardMarkup(keyboard)


def get_teacher_day_selection_keyboard(teacher_id: int) -> InlineKeyboardMarkup:
    """Створює клавіатуру вибору дня тижня для розкладу викладача."""

    days = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця"]

    keyboard = []

    for day in days:

        # Використовуємо коротші назви днів

        day_short = {
            "понеділок": "mon",
            "вівторок": "tue",
            "середа": "wed",
            "четвер": "thu",
            "п'ятниця": "fri",
        }.get(day, day)

        keyboard.append(
            [
                InlineKeyboardButton(
                    day.capitalize(), callback_data=f"t_day_{day_short}_{teacher_id}"
                )
            ]
        )

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"t_menu_{teacher_id}")])

    return InlineKeyboardMarkup(keyboard)


def get_teacher_week_type_selection_keyboard(
    day_short: str, teacher_id: int
) -> InlineKeyboardMarkup:
    """Створює клавіатуру вибору типу тижня для розкладу викладача."""

    keyboard = [
        [InlineKeyboardButton("Чисельник", callback_data=f"t_week_num_{day_short}_{teacher_id}")],
        [InlineKeyboardButton("Знаменник", callback_data=f"t_week_den_{day_short}_{teacher_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"t_day_schedule_{teacher_id}")],
    ]

    return InlineKeyboardMarkup(keyboard)


# --- ОБРОБНИКИ ДЛЯ РОЗКЛАДУ ВИКЛАДАЧА ---


async def teacher_my_schedule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує меню розкладу викладача."""

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning("teacher_my_schedule_handler викликано без query або from_user")

        return

    # Отримуємо teacher_id з callback_data або з user.id

    callback_data = query.data

    if callback_data and callback_data.startswith("t_menu_"):

        teacher_id = int(callback_data.replace("t_menu_", ""))

    else:

        teacher_id = query.from_user.id

    teacher_data = get_teacher_data_from_db(teacher_id)

    if not teacher_data:

        await query.answer("Ви не зареєстровані як викладач.", show_alert=True)

        return

    teacher_name = teacher_data.get("full_name", "")

    if not teacher_name:

        await query.answer("У вас не вказано повне ім'я.", show_alert=True)

        return

    text = f"👨‍🏫 Меню розкладу для *{teacher_name}*\n\nОберіть тип розкладу:"

    reply_markup = get_teacher_schedule_menu_keyboard(teacher_id)

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


# ДОДАНО: обробник вибору курсу викладачем для перегляду будь-якої групи


async def teacher_select_course_for_any_group_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    query = update.callback_query

    if not query or not query.data.startswith("teacher_select_course_"):

        return

    await query.answer()

    try:

        course_number = int(query.data.replace("teacher_select_course_", ""))

    except ValueError:

        await query.answer("Невірний курс.", show_alert=True)

        return

    text = f"🔍 Оберіть групу для перегляду (курс {course_number}):"

    reply_markup = get_teacher_group_selection_keyboard_by_course(course_number)

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def show_teacher_today_schedule_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Показує розклад викладача на сьогодні."""

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning("show_teacher_today_schedule_handler викликано без query або from_user")

        return

    # Отримуємо teacher_id з callback_data

    callback_data = query.data

    if not callback_data or not callback_data.startswith("t_today_"):

        logger.warning(f"Неправильний callback_data: {callback_data}")

        return

    teacher_id = int(callback_data.replace("t_today_", ""))

    teacher_data = get_teacher_data_from_db(teacher_id)

    if not teacher_data:

        await query.answer("Викладач не знайдений.", show_alert=True)

        return

    teacher_name = teacher_data.get("full_name", "")

    if not teacher_name:

        await query.answer("У викладача не вказано повне ім'я.", show_alert=True)

        return

    # Визначаємо поточний день та тип тижня

    current_date = datetime.now()

    current_weekday = current_date.weekday()  # 0 = понеділок, 6 = неділя

    days = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]

    current_day = days[current_weekday]

    current_week_type = get_current_week_type_for_schedule(current_date)

    # Генеруємо розклад на сьогодні

    schedule_text = get_teacher_schedule_for_day(teacher_name, current_day, current_week_type)

    reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад до меню розкладу", callback_data=f"t_menu_{teacher_id}")]]
    )

    await query.edit_message_text(schedule_text, reply_markup=reply_markup, parse_mode="Markdown")


async def show_teacher_full_schedule_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Показує повний розклад викладача на тиждень."""

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning("show_teacher_full_schedule_handler викликано без query або from_user")

        return

    # Отримуємо teacher_id з callback_data

    callback_data = query.data

    if not callback_data or not callback_data.startswith("t_full_"):

        logger.warning(f"Неправильний callback_data: {callback_data}")

        return

    teacher_id = int(callback_data.replace("t_full_", ""))

    teacher_data = get_teacher_data_from_db(teacher_id)

    if not teacher_data:

        await query.answer("Викладач не знайдений.", show_alert=True)

        return

    teacher_name = teacher_data.get("full_name", "")

    if not teacher_name:

        await query.answer("У викладача не вказано повне ім'я.", show_alert=True)

        return

    # Генеруємо повний розклад

    schedule_text = get_full_teacher_schedule(teacher_name)

    reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад до меню розкладу", callback_data=f"t_menu_{teacher_id}")]]
    )

    await query.edit_message_text(schedule_text, reply_markup=reply_markup, parse_mode="Markdown")


async def show_teacher_day_schedule_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Показує меню вибору дня тижня для розкладу викладача."""

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning("show_teacher_day_schedule_handler викликано без query або from_user")

        return

    # Отримуємо teacher_id з callback_data

    callback_data = query.data

    if not callback_data or not callback_data.startswith("t_day_schedule_"):

        logger.warning(f"Неправильний callback_data: {callback_data}")

        return

    teacher_id = int(callback_data.replace("t_day_schedule_", ""))

    teacher_data = get_teacher_data_from_db(teacher_id)

    if not teacher_data:

        await query.answer("Викладач не знайдений.", show_alert=True)

        return

    teacher_name = teacher_data.get("full_name", "")

    if not teacher_name:

        await query.answer("У викладача не вказано повне ім'я.", show_alert=True)

        return

    text = f"👨‍🏫 Оберіть день тижня для розкладу *{teacher_name}*:"

    reply_markup = get_teacher_day_selection_keyboard(teacher_id)

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def show_teacher_specific_day_schedule_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Показує меню вибору типу тижня для конкретного дня."""

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning(
            "show_teacher_specific_day_schedule_handler викликано без query або from_user"
        )

        return

    user = query.from_user

    teacher_data = get_teacher_data_from_db(user.id)

    if not teacher_data:

        await query.answer("Ви не зареєстровані як викладач.", show_alert=True)

        return

    teacher_name = teacher_data.get("full_name", "")

    if not teacher_name:

        await query.answer("У вас не вказано повне ім'я.", show_alert=True)

        return

    # Отримуємо дані з callback_data

    callback_data = query.data

    if not callback_data or not callback_data.startswith("t_day_"):

        logger.warning(f"Неправильний callback_data: {callback_data}")

        return

    # Формат: t_day_{day_short}_{teacher_id}

    parts = callback_data.split("_")

    if len(parts) < 4:

        logger.warning(f"Неправильний формат callback_data: {callback_data}")

        return

    day_short = parts[2]  # Коротка назва дня

    teacher_id = int(parts[3])  # ID викладача

    # Переводимо коротку назву в повну

    day_names = {
        "mon": "понеділок",
        "tue": "вівторок",
        "wed": "середа",
        "thu": "четвер",
        "fri": "п'ятниця",
        "sat": "субота",
        "sun": "неділя",
    }

    day_name = day_names.get(day_short, day_short)

    text = f"👨‍🏫 Оберіть тип тижня для *{day_name}* (*{teacher_name}*):"

    reply_markup = get_teacher_week_type_selection_keyboard(day_short, teacher_id)

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def show_teacher_final_day_schedule_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Показує фінальний розклад викладача на конкретний день та тип тижня."""

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning("show_teacher_final_day_schedule_handler викликано без query або from_user")

        return

    user = query.from_user

    teacher_data = get_teacher_data_from_db(user.id)

    if not teacher_data:

        await query.answer("Ви не зареєстровані як викладач.", show_alert=True)

        return

    teacher_name = teacher_data.get("full_name", "")

    if not teacher_name:

        await query.answer("У вас не вказано повне ім'я.", show_alert=True)

        return

    # Отримуємо дані з callback_data

    callback_data = query.data

    if not callback_data or not callback_data.startswith("t_week_"):

        logger.warning(f"Неправильний callback_data: {callback_data}")

        return

    # Формат: t_week_{num/den}_{day_short}_{teacher_id}

    parts = callback_data.split("_")

    if len(parts) < 5:

        logger.warning(f"Неправильний формат callback_data: {callback_data}")

        return

    week_type_short = parts[2]  # num або den

    day_short = parts[3]  # Коротка назва дня

    teacher_id = int(parts[4])  # ID викладача

    # Переводимо короткі назви в повні

    week_type = "чисельник" if week_type_short == "num" else "знаменник"

    day_names = {
        "mon": "понеділок",
        "tue": "вівторок",
        "wed": "середа",
        "thu": "четвер",
        "fri": "п'ятниця",
        "sat": "субота",
        "sun": "неділя",
    }

    day_name = day_names.get(day_short, day_short)

    # Генеруємо розклад

    schedule_text = get_teacher_schedule_for_day(teacher_name, day_name, week_type)

    reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад до меню розкладу", callback_data=f"t_menu_{teacher_id}")]]
    )

    await query.edit_message_text(schedule_text, reply_markup=reply_markup, parse_mode="Markdown")


async def teacher_view_group_schedule_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    query = update.callback_query

    if not query or not query.from_user:

        logger.warning("teacher_view_group_schedule_handler викликано без query або from_user")

        return

    # Отримуємо назву групи з callback_data

    callback_data = query.data

    if not callback_data or not callback_data.startswith("teacher_view_group_"):

        logger.warning(f"Неправильний callback_data: {callback_data}")

        return

    group_name = callback_data.replace("teacher_view_group_", "")

    group_schedule_data = get_schedule_data_for_group(group_name)

    if not group_schedule_data:

        await query.answer(f"На жаль, розклад для групи {group_name} не знайдено.", show_alert=True)

        return

    # Зберігаємо групу в контексті для подальшого використання

    if context.user_data is None:

        context.user_data = {}

    context.user_data["teacher_viewing_group"] = group_name

    context.user_data["teacher_viewing_any_group"] = True

    text = f"📅 Меню розкладу для групи: *{group_name}*.\nОберіть пункт:"

    reply_markup = get_schedule_menu_keyboard(group_name)

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


def get_schedule_menu_keyboard(user_group: str | None) -> InlineKeyboardMarkup:

    if not user_group:

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Будь ласка, спочатку оберіть групу",
                        callback_data="change_set_group_prompt",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Назад до головного меню", callback_data="back_to_main_menu"
                    )
                ],
            ]
        )

    buttons = [
        [
            InlineKeyboardButton("Сьогодні", callback_data="get_schedule_today"),
            InlineKeyboardButton("Завтра", callback_data="get_schedule_tomorrow"),
        ],
        [
            InlineKeyboardButton(
                "Обрати день (поточний тиждень)", callback_data="show_day_schedule_menu"
            )
        ],
        [
            InlineKeyboardButton(
                "Обрати день + тип тижня", callback_data="select_specific_day_and_type"
            )
        ],
        [InlineKeyboardButton("Розклад дзвінків", callback_data="get_call_schedule")],
        [InlineKeyboardButton("Повний розклад (по групі)", callback_data="get_full_schedule_all")],
        [InlineKeyboardButton("⬅️ Назад до головного меню", callback_data="back_to_main_menu")],
    ]

    return InlineKeyboardMarkup(buttons)


def get_day_schedule_menu_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Пн", callback_data="get_schedule_day_понеділок"),
                InlineKeyboardButton("Вт", callback_data="get_schedule_day_вівторок"),
                InlineKeyboardButton("Ср", callback_data="get_schedule_day_середа"),
            ],
            [
                InlineKeyboardButton("Чт", callback_data="get_schedule_day_четвер"),
                InlineKeyboardButton("Пт", callback_data="get_schedule_day_п'ятниця"),
            ],
            [InlineKeyboardButton("⬅️ Назад до меню розкладу", callback_data="show_schedule_menu")],
        ]
    )


def get_specific_day_selection_keyboard() -> InlineKeyboardMarkup:

    keyboard = [
        [
            InlineKeyboardButton("Пн", callback_data="chose_day_понеділок"),
            InlineKeyboardButton("Вт", callback_data="chose_day_вівторок"),
            InlineKeyboardButton("Ср", callback_data="chose_day_середа"),
        ],
        [
            InlineKeyboardButton("Чт", callback_data="chose_day_четвер"),
            InlineKeyboardButton("Пт", callback_data="chose_day_п'ятниця"),
        ],
        [InlineKeyboardButton("⬅️ Назад до меню розкладу", callback_data="show_schedule_menu")],
    ]

    return InlineKeyboardMarkup(keyboard)


def get_week_type_selection_keyboard(day_name: str) -> InlineKeyboardMarkup:

    keyboard = [
        [
            InlineKeyboardButton(
                "Чисельник", callback_data=f"show_day_explicit_{day_name}_чисельник"
            )
        ],
        [
            InlineKeyboardButton(
                "Знаменник", callback_data=f"show_day_explicit_{day_name}_знаменник"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Назад до вибору дня", callback_data="select_specific_day_and_type"
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def get_raffle_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:

    buttons = []

    user_is_participant = get_raffle_participant_status(user_id)

    if user_is_participant:

        buttons.append(
            [
                InlineKeyboardButton(
                    "✅ Ви вже берете участь!", callback_data="raffle_already_joined"
                )
            ]
        )

    else:

        buttons.append(
            [
                InlineKeyboardButton(
                    "🎉 Взяти участь у розіграші", callback_data="raffle_join_prompt"
                )
            ]
        )

    buttons.append(
        [InlineKeyboardButton("⬅️ Назад до головного меню", callback_data="back_to_main_menu")]
    )

    return InlineKeyboardMarkup(buttons)


def get_raffle_join_confirmation_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Так, підтверджую", callback_data="raffle_confirm_join")],
            [InlineKeyboardButton("Скасувати", callback_data="back_to_raffle_menu")],
        ]
    )


def get_raffle_referral_success_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎉 Продовжити", callback_data="raffle_continue_after_referral")],
            [
                InlineKeyboardButton(
                    "⬅️ Назад до меню розіграшу", callback_data="back_to_raffle_menu"
                )
            ],
        ]
    )


def get_back_to_raffle_menu_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад до меню розіграшу", callback_data="back_to_raffle_menu")]]
    )


def get_admin_panel_keyboard() -> InlineKeyboardMarkup:

    keyboard = [
        [InlineKeyboardButton("📢 Оголошення", callback_data="admin_announce_start")],
        [InlineKeyboardButton("🖥️ Статус сервера", callback_data="admin_server_status")],
        [InlineKeyboardButton("⚙️ Режим обслуговування", callback_data="admin_maintenance_menu")],
        [InlineKeyboardButton("📬 Переглянути DLQ", callback_data="view_dlq_callback")],
        [InlineKeyboardButton("🗑️ Очистити DLQ", callback_data="admin_clear_dlq")],
        [InlineKeyboardButton("📊 Статистика бота", callback_data="admin_show_stats")],
        # [InlineKeyboardButton("📢 Оголошення", callback_data='admin_announce_start')],  <-- Я ВИДАЛИВ ДУБЛІКАТ
        [InlineKeyboardButton("👨‍🏫 Керування викладачами", callback_data="admin_manage_teachers")],
        [
            InlineKeyboardButton(
                "🔄 Очистити кеш розкладу (пам'ять)", callback_data="admin_clear_schedule_cache"
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Перезавантажити розклад з JSON", callback_data="admin_reload_schedule_json"
            )
        ],
        [
            InlineKeyboardButton(
                "📥 Завантажити локальну БД (користувачі)", callback_data="admin_download_local_db"
            )
        ],
        # [InlineKeyboardButton("🏆 Обрати переможця розіграшу", callback_data='admin_pick_raffle_winner')] <-- І КНОПКУ РОЗІГРАШУ
    ]

    if ENABLE_FTP_SYNC:

        keyboard.append(
            [
                InlineKeyboardButton(
                    "💾 Завантажити БД користувачів на FTP", callback_data="admin_upload_db_to_ftp"
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("⬅️ Назад до головного меню", callback_data="back_to_main_menu")]
    )

    return InlineKeyboardMarkup(keyboard)


# ЗАМІНІТЬ СТАРУ ФУНКЦІЮ get_group_selection_keyboard НА ЦЮ


def get_group_selection_keyboard(
    page: int = 0, page_size: int = 9, registration_flow: bool = False, selected_course: int = None
) -> InlineKeyboardMarkup:

    all_groups = get_all_group_names_from_cache()

    if not all_groups:

        # Кнопка "Назад" тут також має бути динамічною

        back_button = (
            InlineKeyboardButton(
                "⬅️ Назад до вибору курсу", callback_data="back_to_course_selection"
            )
            if registration_flow
            else InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main_menu")
        )

        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Немає доступних груп", callback_data="no_groups_available")],
                [back_button],
            ]
        )

    # Фільтруємо групи за курсом, якщо він вказаний

    if selected_course is not None:

        # Визначаємо рік за курсом: 1 курс = 25, 2 курс = 24, 3 курс = 23, 4 курс = 22

        course_year_map = {1: "25", 2: "24", 3: "23", 4: "22"}

        target_year = course_year_map.get(selected_course, "25")

        # Фільтруємо групи, що закінчуються на рік або містять рік з підгрупами

        filtered_groups = [
            group
            for group in all_groups
            if group.endswith(f"-{target_year}") or f"-{target_year} (" in group
        ]

        all_groups = filtered_groups

    if not all_groups:

        back_button = (
            InlineKeyboardButton(
                "⬅️ Назад до вибору курсу", callback_data="back_to_course_selection"
            )
            if registration_flow
            else InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main_menu")
        )

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Немає груп для {selected_course} курсу",
                        callback_data="no_groups_available",
                    )
                ],
                [back_button],
            ]
        )

    display_page_size = 50

    groups_on_page = all_groups[0:display_page_size]

    keyboard = []

    row = []

    for group_name in groups_on_page:

        row.append(InlineKeyboardButton(group_name, callback_data=f"set_group_{group_name}"))

        if len(row) == 3:

            keyboard.append(row)

            row = []

    if row:

        keyboard.append(row)

    # Ось ключова зміна: ми обираємо кнопку "Назад" залежно від контексту

    # ... всередині get_group_selection_keyboard()

    if registration_flow:

        # Під час реєстрації кнопка повертає до вибору курсу

        keyboard.append(
            [
                InlineKeyboardButton(
                    "⬅️ Назад до вибору курсу", callback_data="back_to_course_selection"
                )
            ]
        )

    else:

        # Додаємо кнопку для повернення на самий початок

        keyboard.append(
            [
                InlineKeyboardButton(
                    "↩️ Почати реєстрацію з початку", callback_data="back_to_role_selection"
                )
            ]
        )

        # При зміні групи з головного меню, кнопка повертає в головне меню

        keyboard.append([InlineKeyboardButton("⬅️ Скасувати", callback_data="back_to_main_menu")])

    return InlineKeyboardMarkup(keyboard)


def get_back_to_schedule_menu_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад до меню розкладу", callback_data="show_schedule_menu")]]
    )


def get_back_to_teacher_menu_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад до меню викладача", callback_data="back_to_main_menu")]]
    )


def get_back_to_main_menu_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад до головного меню", callback_data="back_to_main_menu")]]
    )


def get_cancel_profanity_flow_keyboard(flow_type: str) -> InlineKeyboardMarkup:
    """

    Повертає клавіатуру для скасування поточного введення після виявлення нецензурної лексики,

    та повертає до відповідного головного меню.

    'flow_type' має бути 'report', 'suggestion', або 'feedback'.

    """

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Скасувати", callback_data=f"cancel_{flow_type}_flow")]]
    )


def get_back_to_admin_panel_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад до адмін-панелі", callback_data="show_admin_panel")]]
    )


def get_maintenance_status_text() -> str:

    if maintenance_mode_active:

        status = "🟢 *УВІМКНЕНО*"

        msg = maintenance_message

        end_time_str = (
            maintenance_end_time.strftime("%Y-%m-%d %H:%M:%S %Z")
            if maintenance_end_time
            else "не встановлено"
        )

        time_left_str = ""

        if maintenance_end_time and maintenance_end_time > datetime.now(KYIV_TZ):

            remaining_delta = maintenance_end_time - datetime.now(KYIV_TZ)

            total_seconds = int(remaining_delta.total_seconds())

            days, rem = divmod(total_seconds, 86400)

            hours, rem = divmod(rem, 3600)

            minutes, seconds = divmod(rem, 60)

            if days > 0:
                time_left_str = f"{days}д {hours}г {minutes}хв"

            elif hours > 0:
                time_left_str = f"{hours}г {minutes}хв"

            elif minutes > 0:
                time_left_str = f"{minutes}хв {seconds}с"

            else:
                time_left_str = f"{seconds}с"

            time_left_str = f" (залишилось: {time_left_str})"

        return (
            f"Статус режиму обслуговування: {status}\n"
            f'Повідомлення для користувачів: "{msg}"\n'
            f"Завершується: {end_time_str}{time_left_str}"
        )

    else:

        return "Статус режиму обслуговування: 🔴 *ВИМКНЕНО*"


def get_server_status_text() -> str:

    try:

        cpu_usage = psutil.cpu_percent(interval=0.1)

        memory_info = psutil.virtual_memory()

        memory_usage = memory_info.percent

        disk_info = psutil.disk_usage("/")

        disk_usage = disk_info.percent

        p = psutil.Process(os.getpid())

        process_uptime_seconds = time.time() - p.create_time()

        process_uptime_str = str(timedelta(seconds=int(process_uptime_seconds)))

        text = (
            f"🖥️ *Статус сервера:*\n\n"
            f"CPU Навантаження: {cpu_usage}%\n"
            f"Використання RAM: {memory_usage}% (Всього: {memory_info.total // (1024**3)} GB, Використано: {memory_info.used // (1024**2)} MB)\n"
            f"Використання Диску (/): {disk_usage}% (Всього: {disk_info.total // (1024**3)} GB, Використано: {disk_info.used // (1024**3)} GB)\n"
            f"Час роботи бота: {process_uptime_str}"
        )

        return text

    except Exception as e:

        logger.error(f"Помилка отримання статусу сервера: {e}")

        return "Не вдалося отримати статус сервера."


def escape_markdown(text: str) -> str:
    """Escapes common MarkdownV2 special characters."""

    if not text:

        return ""

    escape_chars = r"\_*[]()~`>#+-=|{}.!"

    return "".join(["\\" + char if char in escape_chars else char for char in text])


# --- Обробники Команд та Кнопок ---


async def maintenance_menu_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query

    if query:
        await query.answer()

    text = get_maintenance_status_text() + "\n\nОберіть дію:"

    keyboard_buttons = []

    if maintenance_mode_active:

        keyboard_buttons.append(
            [InlineKeyboardButton("🔴 Вимкнути зараз", callback_data="maint_disable_now")]
        )

    else:

        keyboard_buttons.append(
            [InlineKeyboardButton("🟢 Увімкнути режим", callback_data="maint_start_setup")]
        )

    keyboard_buttons.append(
        [InlineKeyboardButton("⬅️ Назад до адмін-панелі", callback_data="show_admin_panel")]
    )

    reply_markup = InlineKeyboardMarkup(keyboard_buttons)

    if query:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")

    elif update.message:
        await update.message.reply_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")


async def maintenance_start_setup_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query

    await query.answer()

    context.user_data["maintenance_setter_id"] = query.from_user.id

    keyboard = [
        [
            InlineKeyboardButton("10 хв", callback_data="maint_set_duration_10"),
            InlineKeyboardButton("30 хв", callback_data="maint_set_duration_30"),
            InlineKeyboardButton("60 хв", callback_data="maint_set_duration_60"),
        ],
        [InlineKeyboardButton("Ввести вручну (хв)", callback_data="maint_manual_duration_prompt")],
        [InlineKeyboardButton(" скасувати", callback_data="maint_cancel_setup")],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "⚙️ Налаштування режиму обслуговування:\n\nОберіть тривалість:", reply_markup=reply_markup
    )

    return SELECTING_DURATION


async def maintenance_set_duration_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query

    await query.answer()

    if context.user_data.get("maintenance_setter_id") != query.from_user.id:

        await query.edit_message_text(
            "Помилка: інший адміністратор вже налаштовує.",
            reply_markup=get_back_to_admin_panel_keyboard(),
        )

        return ConversationHandler.END

    duration_minutes = int(query.data.split("_")[-1])

    context.user_data["maintenance_duration"] = duration_minutes

    await query.edit_message_text(
        f"Тривалість: {duration_minutes} хв.\nНадішліть повідомлення для користувачів:"
    )

    return TYPING_MESSAGE


async def maintenance_manual_duration_prompt_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query

    await query.answer()

    if context.user_data.get("maintenance_setter_id") != query.from_user.id:

        await query.edit_message_text(
            "Помилка доступу.", reply_markup=get_back_to_admin_panel_keyboard()
        )

        return ConversationHandler.END

    await query.edit_message_text("Введіть тривалість в хвилинах (число):")

    return TYPING_DURATION


async def maintenance_typed_duration_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    if context.user_data.get("maintenance_setter_id") != update.effective_user.id:

        await update.message.reply_text(
            "Помилка доступу.", reply_markup=get_back_to_admin_panel_keyboard()
        )

        return ConversationHandler.END

    try:

        duration_minutes = int(update.message.text)

        if duration_minutes <= 0:

            await update.message.reply_text("Тривалість має бути позитивною. Введіть ще раз:")

            return TYPING_DURATION

        context.user_data["maintenance_duration"] = duration_minutes

        await update.message.reply_text(
            f"Тривалість: {duration_minutes} хв.\nНадішліть повідомлення для користувачів:"
        )

        return TYPING_MESSAGE

    except ValueError:

        await update.message.reply_text("Введіть числове значення для тривалості:")

        return TYPING_DURATION


async def maintenance_typed_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    if context.user_data.get("maintenance_setter_id") != update.effective_user.id:

        await update.message.reply_text(
            "Помилка доступу.", reply_markup=get_back_to_admin_panel_keyboard()
        )

        context.user_data.clear()

        return ConversationHandler.END

    user_message = update.message.text

    duration_minutes = context.user_data.get("maintenance_duration")

    if not user_message or not duration_minutes:

        await update.message.reply_text(
            "Помилка: не вдалося отримати дані.", reply_markup=get_back_to_admin_panel_keyboard()
        )

        context.user_data.clear()

        return ConversationHandler.END

    global maintenance_mode_active, maintenance_message, maintenance_end_time, maintenance_messages_ids

    maintenance_mode_active = True

    maintenance_message = user_message

    maintenance_end_time = datetime.now(KYIV_TZ) + timedelta(minutes=duration_minutes)

    current_jobs = context.job_queue.get_jobs_by_name(MAINTENANCE_JOB_NAME)

    for job in current_jobs:
        job.schedule_removal()

    context.job_queue.run_once(
        disable_maintenance_job_callback, maintenance_end_time, name=MAINTENANCE_JOB_NAME
    )

    activation_msg = (
        f"✅ Режим обслуговування УВІМКНЕНО!\n"
        f'Тривалість: {duration_minutes} хв.\nПовідомлення: "{maintenance_message}"\n'
        f"Завершиться: {maintenance_end_time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    # Зберігаємо ID повідомлення про активацію для подальшого видалення

    sent_message = await update.message.reply_text(
        activation_msg, reply_markup=get_admin_panel_keyboard()
    )

    maintenance_messages_ids[sent_message.chat_id] = sent_message.message_id

    logger.info(
        f"Адмін {update.effective_user.id} увімкнув режим обслуговування на {duration_minutes} хв. Збережено maintenance_message_id: {sent_message.message_id}"
    )

    context.user_data.clear()

    return ConversationHandler.END


async def disable_maintenance_job_callback(context: ContextTypes.DEFAULT_TYPE) -> None:

    global maintenance_mode_active, maintenance_end_time, maintenance_messages_ids

    if maintenance_mode_active:

        maintenance_mode_active = False

        maintenance_end_time = None

        logger.info(
            f"Режим обслуговування автоматично вимкнено (за розкладом о {datetime.now(KYIV_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')})."
        )

        # Видалення старих повідомлень про ТО

        for chat_id, message_id in list(maintenance_messages_ids.items()):

            try:

                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)

                logger.info(
                    f"Автоматично видалено старе повідомлення про ТО (chat_id: {chat_id}, message_id: {message_id})."
                )

            except telegram.error.BadRequest as e:

                if "Message to delete not found" in str(e):

                    logger.warning(
                        f"Повідомлення про ТО вже видалено або не існує (chat_id: {chat_id}, message_id: {message_id})."
                    )

                else:

                    logger.error(
                        f"Помилка при автоматичному видаленні повідомлення про ТО (chat_id: {chat_id}, message_id: {message_id}): {e}"
                    )

            except Exception as e:

                logger.error(
                    f"Неочікувана помилка при автоматичному видаленні повідомлення про ТО (chat_id: {chat_id}, message_id: {message_id}): {e}"
                )

            finally:

                del maintenance_messages_ids[chat_id]

        for admin_id in ADMIN_USER_IDS:

            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text="ℹ️ Режим технічного обслуговування автоматично завершено.",
                )

            except Exception as e:
                logger.error(f"Не вдалося сповістити адміна {admin_id}: {e}")


async def maintenance_disable_now_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    global maintenance_mode_active, maintenance_end_time, maintenance_messages_ids

    user_id = update.effective_user.id

    if user_id not in ADMIN_USER_IDS:

        if update.message:
            await update.message.reply_text("Доступ заборонено.")

        elif update.callback_query:
            await update.callback_query.answer("Доступ заборонено.", show_alert=True)

        return

    if update.callback_query:
        await update.callback_query.answer()

    maintenance_mode_active = False

    maintenance_end_time = None

    current_jobs = context.job_queue.get_jobs_by_name(MAINTENANCE_JOB_NAME)

    for job in current_jobs:
        job.schedule_removal()

    text = "🔴 Режим обслуговування вимкнено вручну."

    reply_markup = get_admin_panel_keyboard()

    # Видалення старих повідомлень про ТО при ручному вимкненні

    for chat_id, message_id in list(maintenance_messages_ids.items()):

        try:

            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)

            logger.info(
                f"Вручну видалено старе повідомлення про ТО (chat_id: {chat_id}, message_id: {message_id})."
            )

        except telegram.error.BadRequest as e:

            if "Message to delete not found" in str(e):

                logger.warning(
                    f"Повідомлення про ТО вже видалено або не існує (chat_id: {chat_id}, message_id: {message_id})."
                )

            else:

                logger.error(
                    f"Помилка при ручному видаленні повідомлення про ТО (chat_id: {chat_id}, message_id: {message_id}): {e}"
                )

        except Exception as e:

            logger.error(
                f"Неочікувана помилка при ручному видаленні повідомлення про ТО (chat_id: {chat_id}, message_id: {message_id}): {e}"
            )

        finally:

            del maintenance_messages_ids[chat_id]

    if update.callback_query:

        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

    elif update.message:

        await update.message.reply_text(text, reply_markup=reply_markup)

    logger.info(f"Адмін {user_id} вимкнув режим обслуговування вручну.")


async def maintenance_cancel_setup_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query

    await query.answer()

    context.user_data.clear()

    await query.edit_message_text(
        "Налаштування режиму обслуговування скасовано.", reply_markup=get_admin_panel_keyboard()
    )

    return ConversationHandler.END


async def check_maintenance_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:

    global maintenance_messages_ids

    if maintenance_mode_active and update.effective_user.id not in ADMIN_USER_IDS:

        time_left_str = ""

        if maintenance_end_time and maintenance_end_time > datetime.now(KYIV_TZ):

            remaining_delta = maintenance_end_time - datetime.now(KYIV_TZ)

            total_seconds = int(remaining_delta.total_seconds())

            days, rem = divmod(total_seconds, 86400)

            hours, rem = divmod(rem, 3600)

            minutes, _ = divmod(rem, 60)

            if days > 0:
                time_left_str = f" орієнтовно {days}д {hours}г"

            elif hours > 0:
                time_left_str = f" орієнтовно {hours}г {minutes}хв"

            elif minutes > 0:
                time_left_str = f" орієнтовно {minutes}хв"

            else:
                time_left_str = " менше хвилини"

            time_left_str = f" Залишилось: {time_left_str}."

        full_maintenance_msg = f"⚙️ {maintenance_message}{time_left_str}"

        # Видалення старого повідомлення про ТО (якщо воно існує) та надсилання нового

        chat_id_to_send = update.effective_chat.id

        if chat_id_to_send in maintenance_messages_ids:

            try:

                await context.bot.delete_message(
                    chat_id=chat_id_to_send, message_id=maintenance_messages_ids[chat_id_to_send]
                )

                logger.debug(
                    f"Видалено попереднє повідомлення про ТО для {chat_id_to_send} перед надсиланням нового."
                )

            except telegram.error.BadRequest as e:

                if "Message to delete not found" in str(e):

                    logger.warning(
                        f"Повідомлення про ТО для {chat_id_to_send} вже видалено або не існує. ({e})"
                    )

                else:

                    logger.error(
                        f"Помилка при видаленні попереднього повідомлення про ТО для {chat_id_to_send}: {e}"
                    )

            finally:

                del maintenance_messages_ids[chat_id_to_send]

        if update.callback_query:

            try:

                if not context.user_data.get(f"answered_query_{update.callback_query.id}"):

                    # Якщо це перший раз, коли бот відповідає на цей запит, надсилаємо сповіщення

                    await update.callback_query.answer(full_maintenance_msg, show_alert=True)

                    context.user_data[f"answered_query_{update.callback_query.id}"] = True

                else:

                    # В іншому випадку, якщо вже було попередження, просто відповідаємо текстом

                    sent_msg = await update.callback_query.message.reply_text(full_maintenance_msg)

                    maintenance_messages_ids[chat_id_to_send] = sent_msg.message_id

            except Exception as e:

                logger.error(f"Помилка при відповіді на callback з повідомленням про ТО: {e}")

                # Якщо callback.answer не спрацював, надішлемо звичайне повідомлення

                sent_msg = await update.callback_query.message.reply_text(full_maintenance_msg)

                maintenance_messages_ids[chat_id_to_send] = sent_msg.message_id

        elif update.message:

            sent_msg = await update.message.reply_text(full_maintenance_msg)

            maintenance_messages_ids[chat_id_to_send] = sent_msg.message_id

        return True

    return False


# ЗАМІНІТЬ СТАРУ ФУНКЦІЮ select_role_callback_handler


async def select_role_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    role = query.data.replace("select_role_", "")

    logger.info(
        f"select_role_callback_handler: User {user_id} selected role '{role}' (callback_data: {query.data})"
    )

    logger.info(
        f"select_role_callback_handler: Function called from {update.effective_chat.type} chat"
    )

    if role == "student":

        logger.info(
            f"Setting user {user_id} role to 'student' and transitioning to SELECTING_COURSE"
        )

        set_user_role_in_db(user_id, role)

        await query.edit_message_text(
            "🎓 Ви обрали 'Студент'. Будь ласка, оберіть ваш курс:",
            reply_markup=get_student_course_selection_keyboard(),
        )

        logger.info(
            f"User {user_id} role set to 'student', message updated, returning SELECTING_COURSE"
        )

        return SELECTING_COURSE

    elif role == "guest":

        logger.info(f"Setting user {user_id} role to 'guest' and transitioning to GUEST_MENU")

        set_user_role_in_db(user_id, role)

        await query.edit_message_text(
            "🚶‍♂️ Ви обрали 'Гість'. Доступні опції:", reply_markup=get_guest_menu_keyboard()
        )

        logger.info(f"User {user_id} role set to 'guest', message updated, returning GUEST_MENU")

        return GUEST_MENU

    elif role == "staff":

        logger.info(f"Setting user {user_id} role to 'staff' and staying in SELECTING_ROLE")

        set_user_role_in_db(user_id, role)

        await query.edit_message_text(
            "👷‍♂️ Ви обрали 'Працівник'. Цей функціонал *в розробці*.",
            reply_markup=get_back_to_role_selection_keyboard(),
            parse_mode="Markdown",
        )

        logger.info(
            f"User {user_id} role set to 'staff', message updated, returning SELECTING_ROLE"
        )

        return SELECTING_ROLE

    else:

        logger.warning(f"Unknown role '{role}' selected by user {user_id}")

    return ConversationHandler.END


async def select_teacher_role_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Окремий обробник для кнопки 'Я викладач'"""

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    logger.info(f"select_teacher_role_callback_handler: User {user_id} selected teacher role")

    await query.edit_message_text(
        "👨‍🏫 Будь ласка, введіть ваш одноразовий пароль (код запрошення), отриманий від адміністратора.",
        reply_markup=get_back_to_role_selection_keyboard(),
    )

    return TYPING_ONE_TIME_PASSWORD


# ЗАМІНІТЬ СТАРУ ФУНКЦІЮ select_student_course_handler НА ЦЮ


async def select_student_course_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    course = query.data.replace("select_course_", "")

    logger.info(f"select_student_course_handler: User {user_id} selected course '{course}'")

    try:

        course_number = int(course)

        if course_number in [1, 2, 3, 4]:

            logger.info(
                f"User {user_id} selected valid course {course_number}, proceeding to group selection"
            )

            # Користувач обрав курс. Тепер ми маємо запитати його групу.

            # Ми редагуємо поточне повідомлення, щоб показати клавіатуру вибору групи

            # і переводимо розмову до стану SELECTING_GROUP.

            text = f"Чудово! Тепер, будь ласка, оберіть вашу групу зі списку для {course_number} курсу:"

            # Викликаємо оновлену функцію клавіатури, вказавши, що це процес реєстрації та курс

            reply_markup = get_group_selection_keyboard(
                registration_flow=True, selected_course=course_number
            )

            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

            # Зберігаємо вибраний курс в контексті для подальшого використання

            context.user_data["selected_course"] = course_number

            # Повертаємо наступний стан у нашій розмові. Це найважливіша зміна!

            return SELECTING_GROUP

    except ValueError:

        logger.warning(f"User {user_id} selected invalid course '{course}'")

    # Якщо щось пішло не так, повертаємося до вибору курсу

    logger.info(f"User {user_id} returning to course selection")

    await query.edit_message_text(
        "Будь ласка, оберіть курс:",
        reply_markup=get_student_course_selection_keyboard(),
        parse_mode="Markdown",
    )

    return SELECTING_COURSE


# ЗАМІНІТЬ СТАРУ ФУНКЦІЮ handle_teacher_otp_entry або handle_teacher_initials


async def handle_teacher_otp_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    # 1. Надсилаємо початкове повідомлення і зберігаємо його

    processing_message = await update.message.reply_text(
        "Перевіряю ваш код... Зачекайте, будь ласка ⏳"
    )

    user = update.effective_user

    entered_otp = update.message.text.strip()

    # 2. Виконуємо повільну перевірку пароля

    is_successful, message = verify_otp_and_claim_profile(entered_otp, user.id)

    # 3. Редагуємо початкове повідомлення з кінцевим результатом

    if is_successful:

        # Якщо успіх, готуємо і показуємо меню викладача

        if not user_exists(user.id):

            add_or_update_user_in_db(user.id, user.username, user.first_name, user.last_name)

        set_user_role_in_db(user.id, "teacher")

        teacher_data = get_teacher_data_from_db(user.id)

        teacher_name = (
            teacher_data.get("full_name", user.full_name) if teacher_data else user.full_name
        )

        success_text = f"Вітаю, *{teacher_name}*!\nВи увійшли як викладач. Чим можу допомогти?"

        reply_markup = get_teacher_menu_keyboard(user.id)

        await processing_message.edit_text(
            text=success_text, reply_markup=reply_markup, parse_mode="Markdown"
        )

        return ConversationHandler.END

    else:

        # Якщо помилка, показуємо її

        await processing_message.edit_text(
            text=f"❌ {message}\nСпробуйте ще раз або зверніться до адміністратора.",
            reply_markup=get_back_to_role_selection_keyboard(),
        )

        return TYPING_ONE_TIME_PASSWORD


async def handle_guest_info_about_college(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query

    user_id = None

    if query and query.from_user:

        user_id = query.from_user.id

    elif update.effective_user:

        user_id = update.effective_user.id

    if user_id is None:

        logger.warning("handle_guest_info_about_college: не вдалося отримати user_id")

        return GUEST_MENU

    user_role = get_user_role_from_db(user_id)  # Отримуємо роль

    if query:

        await query.answer()

    text = "ℹ️ *Про коледж: Що вас цікавить?*"

    reply_markup = get_about_college_menu_keyboard(user_role=user_role)  # Передаємо роль

    if query and query.message:

        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")  # type: ignore

    elif update.message:

        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")  # type: ignore

    return GUEST_MENU


# Правильна версія


async def back_to_role_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    if query:

        await query.answer()  # Відповідаємо на запит негайно

    user = update.effective_user  # Краще використовувати effective_user

    text = f"Привіт, {user.mention_html()}! Я бот 'ЧГЕФК'.\n" "Будь ласка, оберіть, хто ви:"

    reply_markup = get_role_selection_keyboard()

    # Скидаємо роль користувача в базі даних

    set_user_role_in_db(user.id, "ASK_ROLE")

    # Редагуємо повідомлення, якщо можемо, інакше надсилаємо нове

    if query and query.message:

        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")

    else:

        await context.bot.send_message(
            chat_id=user.id, text=text, reply_markup=reply_markup, parse_mode="HTML"
        )

    # Завершуємо будь-яку активну розмову. Це ключова зміна.

    return ConversationHandler.END


# ДОДАЙТЕ ЦЮ НОВУ ФУНКЦІЮ


async def back_to_course_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробник для повернення користувача з етапу вибору групи до вибору курсу."""

    query = update.callback_query

    await query.answer()

    # Показуємо меню вибору курсу знову

    text = "🎓 Будь ласка, оберіть ваш курс:"

    reply_markup = get_student_course_selection_keyboard()

    await query.edit_message_text(text, reply_markup=reply_markup)

    # Повертаємо розмову до стану вибору курсу

    return SELECTING_COURSE


async def handle_staff_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    await query.answer()

    await query.edit_message_text(
        "👷‍♂️ Ви обрали 'Працівник'. Цей функціонал *в розробці*.",
        reply_markup=get_back_to_role_selection_keyboard(),
        parse_mode="Markdown",
    )

    return SELECTING_ROLE


# ЗНАЙДІТЬ ЦЮ ФУНКЦІЮ І ПЕРЕКОНАЙТЕСЬ, ЩО ВОНА ВИГЛЯДАЄ ТАК


async def prompt_set_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    message_to_edit_or_reply = None

    if query:

        await query.answer()

        message_to_edit_or_reply = query.message

    elif update.message:

        message_to_edit_or_reply = update.message

    if not message_to_edit_or_reply:

        logger.warning("prompt_set_group_handler викликаний без query або message.")

        return ConversationHandler.END

    user_id = update.effective_user.id

    current_group = get_user_group_from_db(user_id)

    action_text = "змінити" if current_group else "вказати"

    message_text = f"Ваша поточна група: *{current_group or 'не обрана'}*.\nБудь ласка, {action_text} вашу групу:"

    # Показуємо вибір курсу спочатку, щоб не було всього списку одразу

    reply_markup = get_student_course_selection_keyboard()

    if query:

        await message_to_edit_or_reply.edit_text(
            message_text, reply_markup=reply_markup, parse_mode="Markdown"
        )

    else:

        await message_to_edit_or_reply.reply_text(
            message_text, reply_markup=reply_markup, parse_mode="Markdown"
        )

    return SELECTING_GROUP


async def select_group_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    await query.answer()

    page = int(query.data.split("_")[-1])

    user_id = update.effective_user.id

    current_group = get_user_group_from_db(user_id)

    action_text = "змінити" if current_group else "вказати"

    message_text = f"Ваша поточна група: *{current_group or 'не встановлена'}*.\nБудь ласка, {action_text} вашу групу (Сторінка {page + 1}):"

    # Після пагінації більше не використовується, але залишимо для сумісності

    reply_markup = get_student_course_selection_keyboard()

    await query.message.edit_text(
        text=message_text, reply_markup=reply_markup, parse_mode="Markdown"
    )

    return SELECTING_GROUP


async def set_group_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    await query.answer()

    group_name = query.data.replace("set_group_", "")

    user = query.from_user

    if set_user_group_in_db(user.id, group_name):

        # Ось тут ми замінюємо текст і клавіатуру на головне меню

        await query.message.edit_text(
            f"✅ Дякую! Вашу групу встановлено: *{group_name}*.",
            reply_markup=get_main_menu_keyboard(user.id, group_name),
            parse_mode="Markdown",
        )

        # Завершуємо ConversationHandler.

        # Якщо користувач потім натисне іншу кнопку, button_callback_handler її обробить.

        return ConversationHandler.END  # <-- ЗАЛИШИМО ЦЕ ТАК

    else:

        await query.message.edit_text(
            "❌ Сталася помилка при збереженні групи.",
            reply_markup=get_main_menu_keyboard(user.id, get_user_group_from_db(user.id)),
        )

    return ConversationHandler.END


async def cancel_group_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    user = query.from_user

    text = f"Привіт, {user.mention_html()}! Я бот 'ЧГЕФК'.\n" "Будь ласка, оберіть, хто ви:"

    reply_markup = get_role_selection_keyboard()

    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")

    set_user_role_in_db(user.id, "ASK_ROLE")  # Скидаємо роль до ASK_ROLE

    return SELECTING_ROLE  # Дуже важливо: повертаємо стан SELECTING_ROLE

    # Повертаємо користувача до вибору ролі

    user = query.from_user

    text = f"Привіт, {user.mention_html()}! Я бот 'ЧГЕФК'.\n" "Будь ласка, оберіть, хто ви:"

    reply_markup = get_role_selection_keyboard()

    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")

    set_user_role_in_db(user.id, "ASK_ROLE")  # Скидаємо роль до ASK_ROLE

    return SELECTING_ROLE  # Повертаємо стан SELECTING_ROLE, щоб ConversationHandler знав, що ми повертаємося до цього етапу.


async def start_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    if await check_maintenance_and_reply(update, context):

        return ConversationHandler.END

    user = update.effective_user

    logger.info(
        f"start_command_handler: Користувач {user.id} ({user.full_name}) розпочав роботу (/start)."
    )

    is_new_user = not user_exists(user.id)

    referrer_id = None

    if context.args and context.args[0].isdigit():

        potential_referrer_id = int(context.args[0])

        if potential_referrer_id != user.id:

            if user_exists(potential_referrer_id):

                referrer_id = potential_referrer_id

                logger.info(f"Користувач {user.id} прийшов за рефералом від {referrer_id}.")

            else:

                logger.info(
                    f"Отримано недійсний реферальний ID {context.args[0]}: реферер {potential_referrer_id} не знайдено в БД."
                )

        else:

            logger.info(f"Користувач {user.id} спробував реферити сам себе.")

    # Отримуємо поточну роль користувача

    current_user_role = get_user_role_from_db(user.id)

    user_group = get_user_group_from_db(user.id)

    logger.info(
        f"Start: User {user.id}, is_new_user: {is_new_user}, role: {current_user_role}, group: {user_group}."
    )  # ДОДАНО ЦЕЙ РЯДОК

    is_new_user = not user_exists(user.id)

    referrer_id = None

    if context.args and context.args[0].isdigit():

        potential_referrer_id = int(context.args[0])

        if potential_referrer_id != user.id:

            if user_exists(potential_referrer_id):

                referrer_id = potential_referrer_id

                logger.info(f"Користувач {user.id} прийшов за рефералом від {referrer_id}.")

            else:

                logger.info(
                    f"Отримано недійсний реферальний ID {context.args[0]}: реферер {potential_referrer_id} не знайдено в БД."
                )

        else:

            logger.info(f"Користувач {user.id} спробував реферити сам себе.")

    # Отримуємо поточну роль користувача

    current_user_role = get_user_role_from_db(user.id)

    user_group = get_user_group_from_db(user.id)

    # Додаємо або оновлюємо користувача в БД.

    # Якщо це новий користувач або його роль ASK_ROLE, то встановлюємо ASK_ROLE.

    # В іншому випадку, залишаємо існуючу роль і групу.

    if is_new_user or current_user_role is None:

        add_or_update_user_in_db(
            user.id,
            user.username,
            user.first_name,
            user.last_name,
            group_name="ASK_LATER",
            referrer_id=referrer_id,
        )

        set_user_role_in_db(user.id, "ASK_ROLE")

        current_user_role = "ASK_ROLE"  # Оновлюємо змінну для подальшої логіки

        user_group = "ASK_LATER"  # Оновлюємо змінну для подальшої логіки

    # Якщо користувач вже має роль, відмінну від 'ASK_ROLE', і (якщо студент) має групу,

    # то одразу показуємо головне меню.

    if current_user_role == "teacher" and get_teacher_data_from_db(user.id):

        await show_teacher_menu_handler(update, context)

        return ConversationHandler.END

    if current_user_role != "ASK_ROLE" and (
        current_user_role != "student" or (user_group is not None and user_group != "ASK_LATER")
    ):

        text = f"Привіт, {user.full_name}! Твоя роль: *{current_user_role.capitalize()}*.\n"

        if current_user_role == "student":

            text += f"Твоя група: *{user_group or 'не обрана'}*.\n"

        text += "Чим можу допомогти?"

        reply_markup = get_main_menu_keyboard(user.id, user_group)

        if update.message:

            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

        elif update.callback_query:

            # Якщо це колбек, ми повинні відредагувати існуюче повідомлення

            await update.callback_query.edit_message_text(
                text, reply_markup=reply_markup, parse_mode="Markdown"
            )

        return ConversationHandler.END  # Завершуємо ConversationHandler

    # Якщо користувач новий або роль "ASK_ROLE", просимо обрати роль

    if current_user_role == "ASK_ROLE":

        logger.info(
            f"User {user.id} has ASK_ROLE, showing role selection menu and returning SELECTING_ROLE"
        )

        text = f"Привіт, {user.mention_html()}! Я бот 'ЧГЕФК'.\n" "Будь ласка, оберіть, хто ви:"

        reply_markup = get_role_selection_keyboard()

        if update.message:

            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")

        elif update.callback_query:

            await update.callback_query.message.edit_text(
                text, reply_markup=reply_markup, parse_mode="HTML"
            )

        logger.info(f"User {user.id} role selection menu shown, returning SELECTING_ROLE")

        return SELECTING_ROLE

    # Якщо користувач студент і група не встановлена, просимо обрати групу

    if current_user_role == "student" and (user_group is None or user_group == "ASK_LATER"):

        text = (
            f"Привіт, {user.mention_html()}! Я бот 'ЧГЕФК'.\n"
            "Твоя група ще не встановлена.\n"
            "Будь ласка, обери свою групу, щоб я міг показувати тобі актуальний розклад:"
        )

        reply_markup = get_group_selection_keyboard(selected_course=None)

        if update.message:

            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")

        elif update.callback_query:

            await update.callback_query.message.edit_text(
                text, reply_markup=reply_markup, parse_mode="HTML"
            )

        return SELECTING_GROUP

    return ConversationHandler.END


async def show_main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if await check_maintenance_and_reply(update, context):
        return

    query = update.callback_query

    user = query.from_user

    user_group = get_user_group_from_db(user.id)

    text = f"Головне меню. Ваша група: *{user_group or 'не обрана'}*.\nЧим можу допомогти?"

    reply_markup = get_main_menu_keyboard(user.id, user_group)

    try:

        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    except Exception as e:

        logger.debug(
            f"Не вдалося відредагувати повідомлення на головне меню (можливо, не змінилось): {e}"
        )


# --- ДОДАЙТЕ ЦЮ НОВУ ФУНКЦІЮ ---


async def back_to_main_menu_universal_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """

    Універсальний обробник для кнопки "Назад до головного меню".

    Перевіряє роль користувача і показує відповідне меню.

    """

    query = update.callback_query

    if query:

        await query.answer()

    user_id = update.effective_user.id

    user_role = get_user_role_from_db(user_id)

    logger.info(
        f"Universal back to menu: User {user_id} with role '{user_role}' is returning to main menu."
    )

    if user_role == "teacher":

        # Якщо це викладач, показуємо меню викладача

        await show_teacher_menu_handler(update, context)

    elif user_role == "student":

        # Якщо це студент, показуємо меню студента (стара логіка)

        await show_main_menu_handler(update, context)

    else:

        # Для всіх інших (гості, ті, хто ще не обрав роль) повертаємо на екран вибору ролі

        await back_to_role_selection_handler(update, context)


async def schedule_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if await check_maintenance_and_reply(update, context):
        return

    user_id = update.effective_user.id

    # Перевіряємо, чи це куратор, який дивиться розклад своєї групи

    curated_group = context.user_data.get("curated_group_name")

    if curated_group:

        user_group = curated_group

        back_keyboard = get_back_to_teacher_menu_keyboard()

    else:

        user_group = get_user_group_from_db(user_id)

        back_keyboard = get_back_to_main_menu_keyboard()

    text = f"📅 Меню розкладу для групи: *{user_group or 'НЕ ОБРАНА'}*.\nОберіть пункт:"

    reply_markup = get_schedule_menu_keyboard(user_group)

    if update.callback_query:

        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )

    elif update.message:

        update_command_stats(update.message.text.split()[0])

        if not user_group:

            await update.message.reply_text(
                "Будь ласка, спочатку встановіть вашу групу.",
                reply_markup=get_main_menu_keyboard(user_id, None),
            )

            return

        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def day_schedule_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if await check_maintenance_and_reply(update, context):
        return

    query = update.callback_query

    user_id = query.from_user.id

    # Перевіряємо, чи це куратор, який дивиться розклад своєї групи

    curated_group = context.user_data.get("curated_group_name")

    if curated_group:

        user_group = curated_group

    else:

        user_group = get_user_group_from_db(user_id)

    if not user_group:

        await query.answer("Спочатку оберіть групу!", show_alert=True)

        await query.edit_message_text(
            "Будь ласка, оберіть групу:",
            reply_markup=get_group_selection_keyboard(selected_course=None),
        )

        return

    text = "🗓️ Обери день тижня (для поточного типу тижня):"

    reply_markup = get_day_schedule_menu_keyboard()

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def select_specific_day_menu_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    query = update.callback_query

    user_id = query.from_user.id

    # Виправлена логіка: якщо викладач дивиться іншу групу, показуємо саме її

    teacher_viewing_group = context.user_data.get("teacher_viewing_group")

    curated_group = context.user_data.get("curated_group_name")

    if teacher_viewing_group:

        user_group = teacher_viewing_group

    elif curated_group:

        user_group = curated_group

    else:

        user_group = get_user_group_from_db(user_id)

    if not user_group:

        await query.edit_message_text(
            "Будь ласка, спочатку оберіть вашу групу.",
            reply_markup=get_main_menu_keyboard(user_id, None),
        )

        return

    text = "🗓️ Оберіть день, для якого бажаєте вказати тип тижня:"

    reply_markup = get_specific_day_selection_keyboard()

    await query.edit_message_text(text, reply_markup=reply_markup)


async def select_week_type_for_day_menu_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    query = update.callback_query

    day_name = query.data.replace("chose_day_", "")

    text = f"🗓️ Оберіть тип тижня для: *{day_name.capitalize()}*"

    reply_markup = get_week_type_selection_keyboard(day_name)

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def show_schedule_for_day_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command_or_day_data: str
) -> None:

    if await check_maintenance_and_reply(update, context):
        return

    query = update.callback_query

    if not query:

        logger.warning("show_schedule_for_day_handler викликано без callback_query.")

        if update.message:
            await update.message.reply_text("Сталася помилка. Спробуйте через меню.")

        return

    user_id = query.from_user.id

    # Виправлена логіка: якщо викладач дивиться іншу групу, показуємо саме її

    teacher_viewing_group = context.user_data.get("teacher_viewing_group")

    curated_group = context.user_data.get("curated_group_name")

    if teacher_viewing_group:

        user_group = teacher_viewing_group

    elif curated_group:

        user_group = curated_group

    else:

        user_group = get_user_group_from_db(user_id)

    if not user_group:

        await query.answer("Будь ласка, спочатку оберіть вашу групу.", show_alert=True)

        current_text = "Для перегляду розкладу, будь ласка, спочатку встановіть вашу групу:"

        reply_mk = get_group_selection_keyboard(selected_course=None)

        try:
            await query.edit_message_text(current_text, reply_markup=reply_mk)

        except Exception:
            await query.message.reply_text(current_text, reply_markup=reply_mk)

        return

    group_schedule_data = get_schedule_data_for_group(user_group)

    if not group_schedule_data:

        # Використовуємо відповідну клавіатуру повернення залежно від контексту

        if curated_group:

            back_keyboard = get_back_to_teacher_menu_keyboard()

        else:

            back_keyboard = get_back_to_schedule_menu_keyboard()

        await query.edit_message_text(
            f"На жаль, розклад для групи *{user_group}* не знайдено.",
            reply_markup=back_keyboard,
            parse_mode="Markdown",
        )

        return

    current_day_kyiv = datetime.now(KYIV_TZ)

    target_date_for_week_type_calc = current_day_kyiv

    day_to_display_key = ""

    week_type_to_use = ""

    days_indices_to_text = {
        0: "понеділок",
        1: "вівторок",
        2: "середа",
        3: "четвер",
        4: "п'ятниця",
        5: "субота",
        6: "неділя",
    }

    if command_or_day_data == "get_schedule_today":

        target_date_for_week_type_calc = current_day_kyiv

        day_of_week_index = target_date_for_week_type_calc.weekday()

        day_to_display_key = days_indices_to_text.get(day_of_week_index)

        if day_of_week_index >= 5:

            response_text = (
                f"Сьогодні *{day_to_display_key.capitalize()}*, пар немає. Відпочивай! 🥳"
            )

            await query.edit_message_text(
                response_text,
                reply_markup=get_back_to_schedule_menu_keyboard(),
                parse_mode="Markdown",
            )

            return

        week_type_to_use = get_current_week_type_for_schedule(target_date_for_week_type_calc)

    elif command_or_day_data == "get_schedule_tomorrow":

        target_date_for_week_type_calc = current_day_kyiv + timedelta(days=1)

        day_of_week_index = target_date_for_week_type_calc.weekday()

        day_to_display_key = days_indices_to_text.get(day_of_week_index)

        if day_of_week_index >= 5:

            day_name = day_to_display_key.capitalize() if day_to_display_key else "Вихідний"

            response_text = f"Завтра *{day_name}*, пар немає. Плануй відпочинок! 🏖️"

            await query.edit_message_text(
                response_text,
                reply_markup=get_back_to_schedule_menu_keyboard(),
                parse_mode="Markdown",
            )

            return

        week_type_to_use = get_current_week_type_for_schedule(target_date_for_week_type_calc)

    elif command_or_day_data.startswith("show_day_explicit_"):

        parts = command_or_day_data.replace("show_day_explicit_", "").rsplit("_", 1)

        if len(parts) == 2:

            day_to_display_key = parts[0]

            week_type_to_use = parts[1]

            if week_type_to_use not in ["чисельник", "знаменник"]:
                week_type_to_use = ""

        else:
            day_to_display_key = ""
            week_type_to_use = ""

    elif command_or_day_data.startswith("get_schedule_day_"):

        day_to_display_key = command_or_day_data.replace("get_schedule_day_", "")

        week_type_to_use = get_current_week_type_for_schedule(current_day_kyiv)

    else:

        await query.edit_message_text(
            "Невідома команда для розкладу.", reply_markup=get_back_to_schedule_menu_keyboard()
        )

        return

    if not day_to_display_key or not week_type_to_use:

        await query.edit_message_text(
            "Не вдалося визначити день або тип тижня.",
            reply_markup=get_back_to_schedule_menu_keyboard(),
        )

        return

    response_text = get_schedule_for_day(group_schedule_data, day_to_display_key, week_type_to_use)

    # Використовуємо відповідну клавіатуру повернення залежно від контексту

    if curated_group or context.user_data.get("teacher_viewing_any_group"):

        back_button_markup = get_back_to_teacher_menu_keyboard()

        if command_or_day_data.startswith("show_day_explicit_"):

            day_name_for_back_button = day_to_display_key

            back_button_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Назад до вибору типу тижня",
                            callback_data=f"chose_day_{day_name_for_back_button}",
                        )
                    ],
                    [InlineKeyboardButton("🏠 Меню викладача", callback_data="back_to_main_menu")],
                ]
            )

    else:

        back_button_markup = get_back_to_schedule_menu_keyboard()

        if command_or_day_data.startswith("show_day_explicit_"):

            day_name_for_back_button = day_to_display_key

            back_button_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Назад до вибору типу тижня",
                            callback_data=f"chose_day_{day_name_for_back_button}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🏠 Головне меню розкладу", callback_data="show_schedule_menu"
                        )
                    ],
                ]
            )

    await query.edit_message_text(
        response_text, reply_markup=back_button_markup, parse_mode="Markdown"
    )


def get_schedule_for_day(
    group_schedule_data: dict | None, day_name_key: str, current_week_type: str
) -> str:

    if not group_schedule_data:

        return "_Розклад для вашої групи не знайдено._"

    week_data_key = "тиждень"

    schedule_for_this_type_of_week = group_schedule_data.get(week_data_key, {})

    week_type_name_display = "Чисельник" if current_week_type == "чисельник" else "Знаменник"

    day_name_display = day_name_key.capitalize()

    header = f"🗓️ Розклад на *{day_name_display}* ({week_type_name_display}):\n\n"

    if day_name_key in schedule_for_this_type_of_week:

        lessons_for_day = schedule_for_this_type_of_week[day_name_key]

        if not lessons_for_day:

            return header + "_Пар немає. Відпочивай!_"

        filtered_lessons = []

        for lesson in lessons_for_day:

            lesson_type = lesson.get("тип_тижня", "завжди").lower()

            if lesson_type == "завжди" or lesson_type == current_week_type:

                filtered_lessons.append(lesson)

        if not filtered_lessons:

            return header + "_Пар немає для цього типу тижня. Відпочивай!_"

        details = []

        for lesson in filtered_lessons:

            time_val = lesson.get("час", "??:??")

            name = lesson.get("назва", "Невідомо")

            if name.lower() == "немає пари":
                continue

            # Формуємо інформацію про аудиторію з поверхом

            aud_info = ""

            if lesson.get("аудиторія"):

                auditorium = lesson.get("аудиторія", "-")

                floor = get_floor_by_auditorium(auditorium)

                if floor:

                    aud_info = f" (Ауд. {auditorium}) ({floor})"

                else:

                    aud_info = f" (Ауд. {auditorium})"

            teacher = f" ({lesson.get('викладач', '-')})" if lesson.get("викладач") else ""

            details.append(f" • *{time_val}* - {name}{aud_info}{teacher}")

        if not details:

            return header + "_Пар немає для цього типу тижня. Відпочивай!_"

        return header + "\n".join(details)

    return header + f"_Немає даних для {day_name_display} на {week_type_name_display}._"


def get_call_schedule_formatted(group_schedule_data: dict | None) -> str:

    cache = get_cached_schedule()

    calls_data = cache.get("дзвінки")

    if not calls_data:

        return "🔔 Розклад дзвінків не знайдено."

    response = "🔔 Розклад дзвінків:\n\n"

    for call in calls_data:

        response += f"• {call.get('пара', '?')} пара: *{call.get('початок', '??:??')}* - *{call.get('кінець', '??:??')}*\n"

    return response


def get_full_schedule_formatted(group_schedule_data: dict | None, group_name: str) -> str:

    if not group_schedule_data:

        return f"📋 Повний розклад для групи *{group_name}* не знайдено."

    response = f"📋 Повний розклад для групи *{group_name}*:\n\n"

    days_order = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця"]

    week_data_key = "тиждень"

    week_data = group_schedule_data.get(week_data_key, {})

    response += "*Чисельник*:\n"

    current_week_type_filter = "чисельник"

    week_has_lessons_numerator = False

    for day in days_order:

        if day in week_data and week_data[day]:

            lessons_today = []

            for lesson in week_data[day]:

                lesson_type = lesson.get("тип_тижня", "завжди").lower()

                if (
                    lesson_type == "завжди" or lesson_type == current_week_type_filter
                ) and lesson.get("назва", "").lower() != "немає пари":

                    lessons_today.append(lesson)

            if lessons_today:

                week_has_lessons_numerator = True

                response += f"  *{day.capitalize()}*:\n"

                for lesson_val in lessons_today:

                    time_val = lesson_val.get("час", "??:??")

                    # Формуємо інформацію про аудиторію з поверхом

                    aud_info = ""

                    if lesson_val.get("аудиторія"):

                        auditorium = lesson_val.get("аудиторія", "-")

                        floor = get_floor_by_auditorium(auditorium)

                        if floor:

                            aud_info = f" (Ауд. {auditorium}) ({floor})"

                        else:

                            aud_info = f" (Ауд. {auditorium})"

                    teacher = (
                        f" ({lesson_val.get('викладач', '-')})"
                        if lesson_val.get("викладач")
                        else ""
                    )

                    response += f"    • {time_val} - {lesson_val.get('назва', 'Невідомо')}{aud_info}{teacher}\n"

    if not week_has_lessons_numerator:
        response += "  _На цьому тижні (чисельник) пар немає._\n"

    response += "\n*Знаменник*:\n"

    current_week_type_filter = "знаменник"

    week_has_lessons_denominator = False

    for day in days_order:

        if day in week_data and week_data[day]:

            lessons_today = []

            for lesson_val in week_data[day]:

                lesson_type = lesson_val.get("тип_тижня", "завжди").lower()

                if (
                    lesson_type == "завжди" or lesson_type == current_week_type_filter
                ) and lesson_val.get("назва", "").lower() != "немає пари":

                    lessons_today.append(lesson_val)

            if lessons_today:

                week_has_lessons_denominator = True

                response += f"  *{day.capitalize()}*:\n"

                for lesson_val_inner in lessons_today:

                    time_val = lesson_val_inner.get("час", "??:??")

                    # Формуємо інформацію про аудиторію з поверхом

                    aud_info = ""

                    if lesson_val_inner.get("аудиторія"):

                        auditorium = lesson_val_inner.get("аудиторія", "-")

                        floor = get_floor_by_auditorium(auditorium)

                        if floor:

                            aud_info = f" (Ауд. {auditorium}) ({floor})"

                        else:

                            aud_info = f" (Ауд. {auditorium})"

                    teacher = (
                        f" ({lesson_val_inner.get('викладач', '-')})"
                        if lesson_val_inner.get("викладач")
                        else ""
                    )

                    response += f"    • {time_val} - {lesson_val_inner.get('назва', 'Невідомо')}{aud_info}{teacher}\n"

    if not week_has_lessons_denominator:
        response += "  _На цьому тижні (знаменник) пар немає._\n"

    response += "\n" + get_call_schedule_formatted(group_schedule_data)

    return response.strip()


async def call_schedule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if await check_maintenance_and_reply(update, context):
        return

    user_id = update.effective_user.id

    # Перевіряємо, чи це викладач, який дивиться розклад

    curated_group = context.user_data.get("curated_group_name")

    teacher_viewing_group = context.user_data.get("teacher_viewing_group")

    if curated_group:

        user_group = curated_group

    elif teacher_viewing_group:

        user_group = teacher_viewing_group

    else:

        user_group = get_user_group_from_db(user_id)

    msg_target = update.callback_query.message if update.callback_query else update.message

    if not user_group:

        reply_m = (
            get_main_menu_keyboard(user_id, None)
            if update.message
            else get_group_selection_keyboard(selected_course=None)
        )

        await msg_target.reply_text(
            "Будь ласка, спочатку встановіть вашу групу (хоча розклад дзвінків зазвичай глобальний).",
            reply_markup=reply_m,
        )

        if update.callback_query:
            await update.callback_query.answer()

        return

    group_schedule_data = get_schedule_data_for_group(user_group)

    response_text = get_call_schedule_formatted(group_schedule_data)

    # Використовуємо відповідну клавіатуру повернення залежно від контексту

    if curated_group or teacher_viewing_group:

        reply_markup = get_back_to_teacher_menu_keyboard()

    else:

        reply_markup = get_back_to_schedule_menu_keyboard()

    if update.callback_query:

        await update.callback_query.edit_message_text(
            response_text, reply_markup=reply_markup, parse_mode="Markdown"
        )

    elif update.message:

        update_command_stats("/call_schedule")

        await update.message.reply_text(
            response_text, reply_markup=reply_markup, parse_mode="Markdown"
        )


async def full_schedule_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if await check_maintenance_and_reply(update, context):
        return

    user_id = update.effective_user.id

    # Перевіряємо, чи це викладач, який дивиться розклад

    curated_group = context.user_data.get("curated_group_name")

    teacher_viewing_group = context.user_data.get("teacher_viewing_group")

    if curated_group:

        user_group = curated_group

    elif teacher_viewing_group:

        user_group = teacher_viewing_group

    else:

        user_group = get_user_group_from_db(user_id)

    msg_target = update.callback_query.message if update.callback_query else update.message

    if not user_group:

        reply_m = (
            get_main_menu_keyboard(user_id, None)
            if update.message
            else get_group_selection_keyboard(selected_course=None)
        )

        await msg_target.reply_text(
            "Будь ласка, спочатку встановіть вашу групу.", reply_markup=reply_m
        )

        if update.callback_query:
            await update.callback_query.answer()

        return

    group_schedule_data = get_schedule_data_for_group(user_group)

    response_text = get_full_schedule_formatted(group_schedule_data, user_group)

    # Використовуємо відповідну клавіатуру повернення залежно від контексту

    if curated_group or teacher_viewing_group:

        reply_markup = get_back_to_teacher_menu_keyboard()

    else:

        reply_markup = get_back_to_schedule_menu_keyboard()

    if update.callback_query:

        await update.callback_query.edit_message_text(
            response_text, reply_markup=reply_markup, parse_mode="Markdown"
        )

    elif update.message:

        update_command_stats("/full_schedule")

        await update.message.reply_text(
            response_text, reply_markup=reply_markup, parse_mode="Markdown"
        )


async def donation_info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if await check_maintenance_and_reply(update, context):
        return

    message = (
        f"Дякую за бажання підтримати бота! 💖\n\n"
        f"Можеш кинути копійку на карту:\n`{DONATION_CARD_NUMBER}`\n\nБудь-яка допомога цінується!"
    )

    reply_markup = get_back_to_main_menu_keyboard()

    if update.callback_query:

        await update.callback_query.edit_message_text(
            message, reply_markup=reply_markup, parse_mode="Markdown"
        )

    elif update.message:

        update_command_stats("/donate")

        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")


async def send_report_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    if query:

        await query.answer()

        context.user_data["message_to_edit_id"] = query.message.message_id

        context.user_data["chat_id_for_edit"] = query.message.chat_id

    elif update.message:

        context.user_data["message_to_edit_id"] = update.message.message_id

        context.user_data["chat_id_for_edit"] = update.message.chat_id

    text = "Будь ласка, опишіть проблему, з якою ви зіткнулися. Адміністратори отримають ваше повідомлення."

    keyboard = [[InlineKeyboardButton("Скасувати", callback_data="cancel_report_flow")]]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if query and query.message:

        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    elif update.message:

        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    return TYPING_REPORT


# --- НОВІ ОБРОБНИКИ ДЛЯ ВІДГУКІВ ---


async def send_feedback_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    if query:

        await query.answer()

        context.user_data["message_to_delete_id"] = query.message.message_id

        context.user_data["chat_id_for_delete"] = query.message.chat_id

    elif update.message:

        context.user_data["message_to_delete_id"] = update.message.message_id

        context.user_data["chat_id_for_delete"] = update.message.chat_id

    text = "Будь ласка, залиште ваш відгук. Він буде відправлений *анонімно*."

    keyboard = [[InlineKeyboardButton("Скасувати", callback_data="cancel_feedback_flow")]]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if query and query.message:

        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    elif update.message:

        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    return TYPING_FEEDBACK


async def receive_feedback_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    feedback_text = update.message.text

    user = update.effective_user

    user_group = get_user_group_from_db(user.id)

    if contains_profanity(feedback_text):

        warn_text = "⚠️ Будь ласка, використовуйте цензурну лексику. Ваше повідомлення не було відправлено. Спробуйте ще раз."

        # ЗМІНА ТУТ: Передаємо 'feedback' як flow_type

        await update.message.reply_text(
            warn_text, reply_markup=get_cancel_profanity_flow_keyboard("feedback")
        )

        return TYPING_FEEDBACK

    if not FEEDBACK_CHANNEL_ID:

        await update.message.reply_text("Система відгуків не налаштована.")

        context.user_data.clear()

        return ConversationHandler.END

    try:

        # Для анонімного відгуку user_group не використовується у самому повідомленні для каналу,

        # але потрібен для get_correct_main_menu_keyboard у confirm_text або у випадку помилки.

        safe_feedback = escape_markdown(feedback_text)

        feedback_message_text_for_channel = f"📝 **Новий анонімний відгук:**\n\n" f"{safe_feedback}"

        try:

            await context.bot.send_message(
                chat_id=FEEDBACK_CHANNEL_ID,
                text=feedback_message_text_for_channel,
                parse_mode="Markdown",
            )

        except telegram.error.BadRequest as e:

            if "can't parse entities" in str(e).lower():

                # Якщо Markdown не парситься, відправляємо без нього

                fallback_text = f"📝 Новий анонімний відгук:\n\n{feedback_text}"

                await context.bot.send_message(chat_id=FEEDBACK_CHANNEL_ID, text=fallback_text)

            else:

                raise e

        # 1. Спроба видалити попереднє повідомлення з запитом на відгук

        if (
            "message_to_delete_id" in context.user_data
            and "chat_id_for_delete" in context.user_data
        ):

            try:

                await context.bot.delete_message(
                    chat_id=context.user_data["chat_id_for_delete"],
                    message_id=context.user_data["message_to_delete_id"],
                )

                logger.debug(
                    f"Видалено повідомлення {context.user_data['message_to_delete_id']} в чаті {context.user_data['chat_id_for_delete']}."
                )

            except telegram.error.BadRequest as e:

                logger.warning(
                    f"Не вдалося видалити повідомлення {context.user_data.get('message_to_delete_id')}: {e}"
                )

            except Exception as e:

                logger.error(
                    f"Неочікувана помилка при видаленні повідомлення {context.user_data.get('message_to_delete_id')}: {e}"
                )

        # 2. Надсилаємо нове повідомлення-підтвердження користувачу

        confirm_text = "✅ Дякую! Ваш відгук успішно відправлено."

        await update.message.reply_text(
            confirm_text, reply_markup=get_correct_main_menu_keyboard(user.id)
        )

        logger.info(
            f"Відгук: Анонімний відгук від {user.id} до каналу {FEEDBACK_CHANNEL_ID}. Текст: {feedback_text[:50]}..."
        )

    except Exception as e:

        logger.error(
            f"Відгук: Помилка відправлення від {user.id} до {FEEDBACK_CHANNEL_ID}: {e}",
            exc_info=True,
        )

        user_group_for_menu = get_user_group_from_db(user.id)  # Отримуємо групу для меню

        await update.message.reply_text(
            "❌ Виникла помилка під час відправлення відгуку. Спробуйте пізніше.",
            reply_markup=get_main_menu_keyboard(user.id, user_group_for_menu),
        )

    context.user_data.clear()

    return ConversationHandler.END


async def cancel_feedback_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    if query:

        await query.answer()

        user = query.from_user

    else:

        user = update.effective_user

    user_id = user.id

    text = "Надсилання відгуку скасовано."

    # ЗМІНИ ЦЕЙ БЛОК:

    if "message_to_delete_id" in context.user_data and "chat_id_for_delete" in context.user_data:

        try:

            await context.bot.delete_message(
                chat_id=context.user_data["chat_id_for_delete"],
                message_id=context.user_data["message_to_delete_id"],
            )

            logger.debug(
                f"Видалено повідомлення {context.user_data['message_to_delete_id']} в чаті {context.user_data['chat_id_for_delete']} при скасуванні відгуку."
            )

        except telegram.error.BadRequest as e:

            if "Message to delete not found" in str(e):

                logger.warning(
                    f"Повідомлення відгуку для видалення вже відсутнє або видалено (ID: {context.user_data.get('message_to_delete_id')})."
                )

            else:

                logger.error(
                    f"Помилка при видаленні повідомлення відгуку (ID: {context.user_data.get('message_to_delete_id')}): {e}"
                )

            await (query.message if query else update.message).reply_text(
                text, reply_markup=get_correct_main_menu_keyboard(user_id)
            )

            context.user_data.clear()

            return ConversationHandler.END

        except Exception as e:

            logger.error(
                f"Неочікувана помилка при видаленні повідомлення відгуку (ID: {context.user_data.get('message_to_delete_id')}): {e}"
            )

            await (query.message if query else update.message).reply_text(
                text, reply_markup=get_correct_main_menu_keyboard(user_id)
            )

            context.user_data.clear()

            return ConversationHandler.END

    # Якщо повідомлення було успішно видалене або не існувало,

    # надсилаємо повідомлення про скасування і головне меню.

    if query and query.message:

        try:

            await query.message.edit_text(
                text, reply_markup=get_correct_main_menu_keyboard(user_id)
            )

        except telegram.error.BadRequest as e:

            if "Message is not modified" not in str(e):

                await query.message.reply_text(
                    text, reply_markup=get_correct_main_menu_keyboard(user_id)
                )

        except Exception:

            await query.message.reply_text(
                text, reply_markup=get_correct_main_menu_keyboard(user_id)
            )

    else:

        await update.message.reply_text(text, reply_markup=get_correct_main_menu_keyboard(user_id))

    context.user_data.clear()

    return ConversationHandler.END


async def report_bug_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    user = update.effective_user

    report_text = " ".join(context.args)

    update_command_stats("/report")

    if not report_text:

        await update.message.reply_text("Будь ласка, опиши проблему після команди `/report`.")

        return

    # Визначаємо роль користувача

    user_role = get_user_role_from_db(user.id)

    # Викладачі відправляють репорти тільки в канал розробників

    if user_role == "teacher":

        target_channel = TEACHER_REPORT_CHANNEL_ID

        if not target_channel:

            await update.message.reply_text("Система репортів для викладачів не налаштована.")

            return

    else:

        # Студенти та інші відправляють в загальний канал репортів

        target_channel = REPORT_CHANNEL_ID

        if not target_channel:

            await update.message.reply_text("Система репортів не налаштована.")

            return

    user_group = get_user_group_from_db(user.id)

    try:

        safe_report = escape_markdown(report_text)

        # Різні формати для викладачів та студентів

        if user_role == "teacher":

            report_message_text = (
                f"👨‍🏫 **Репорт від викладача:**\nID: `{user.id}`\nUsername: @{user.username or 'N/A'}\n"
                f"Ім'я: {escape_markdown(user.full_name)}\n\nОпис:\n{safe_report}"
            )

        else:

            report_message_text = (
                f"🐞 **Новий репорт:**\nID: `{user.id}`\nUsername: @{user.username or 'N/A'}\n"
                f"Ім'я: {escape_markdown(user.full_name)}\nГрупа: {escape_markdown(user_group or 'Не вказана')}\n\nОпис:\n{safe_report}"
            )

        try:

            await context.bot.send_message(
                chat_id=target_channel, text=report_message_text, parse_mode="Markdown"
            )

        except telegram.error.BadRequest as e:

            if "can't parse entities" in str(e).lower():

                # Якщо Markdown не парситься, відправляємо без нього

                if user_role == "teacher":

                    fallback_text = (
                        f"👨‍🏫 Репорт від викладача:\nID: {user.id}\nUsername: @{user.username or 'N/A'}\n"
                        f"Ім'я: {user.full_name}\n\nОпис:\n{report_text}"
                    )

                else:

                    fallback_text = (
                        f"🐞 Новий репорт:\nID: {user.id}\nUsername: @{user.username or 'N/A'}\n"
                        f"Ім'я: {user.full_name}\nГрупа: {user_group or 'Не вказана'}\n\nОпис:\n{report_text}"
                    )

                await context.bot.send_message(chat_id=target_channel, text=fallback_text)

            else:

                raise e

        channel_name = "канал розробників" if user_role == "teacher" else "загальний канал репортів"

        logger.info(
            f"Репорт: Відправлено від {user.id} (Роль: {user_role}) в {channel_name}. Текст: {report_text[:50]}..."
        )

        await update.message.reply_text("Дякую! Репорт відправлено.")

    except Exception as e:

        logger.error(f"Репорт: Помилка відправлення від {user.id}: {e}")

        await update.message.reply_text("Помилка відправлення репорту.")


async def send_suggestion_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    if query:

        await query.answer()

        context.user_data["message_to_delete_id"] = query.message.message_id

        context.user_data["chat_id_for_delete"] = query.message.chat_id

    elif update.message:

        context.user_data["message_to_delete_id"] = update.message.message_id

        context.user_data["chat_id_for_delete"] = update.message.chat_id

    text = "Будь ласка, детально опишіть вашу ідею або пропозицію. Ваше повідомлення буде передано адміністрації."

    keyboard = [[InlineKeyboardButton("Скасувати", callback_data="cancel_suggestion_flow")]]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if query and query.message:

        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    elif update.message:

        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    return TYPING_SUGGESTION


async def receive_suggestion_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    suggestion_text = update.message.text

    user = update.effective_user

    update_command_stats("suggestion_from_button")

    user_group = get_user_group_from_db(user.id)

    if contains_profanity(suggestion_text):

        warn_text = "⚠️ Будь ласка, використовуйте цензурну лексику. Ваше повідомлення не було відправлено. Спробуйте ще раз."

        # ЗМІНА ТУТ: Передаємо 'suggestion' як flow_type

        await update.message.reply_text(
            warn_text, reply_markup=get_cancel_profanity_flow_keyboard("suggestion")
        )

        return TYPING_SUGGESTION

    if not SUGGESTION_CHANNEL_ID:

        await update.message.reply_text("Система пропозицій не налаштована.")

        context.user_data.clear()

        return ConversationHandler.END

    try:

        # Тут user_data_for_log вже має доступ до user_group

        user_data_for_log = (
            f"ID: `{user.id}` | Username: @{user.username or 'N/A'}\n"
            f"Ім'я: {user.full_name} | Група: {user_group or 'Не вказана'}"
        )  # Цей рядок тепер коректний

        safe_suggestion = escape_markdown(suggestion_text)

        suggestion_message_text_for_channel = (
            f"💡 **Нова пропозиція:**\n"
            f"Інформація про відправника:\n{user_data_for_log}\n\n"
            f"Опис:\n{safe_suggestion}"
        )

        try:

            await context.bot.send_message(
                chat_id=SUGGESTION_CHANNEL_ID,
                text=suggestion_message_text_for_channel,
                parse_mode="Markdown",
            )

        except telegram.error.BadRequest as e:

            if "can't parse entities" in str(e).lower():

                # Якщо Markdown не парситься, відправляємо без нього

                fallback_text = (
                    f"💡 Нова пропозиція:\n"
                    f"Інформація про відправника:\n{user_data_for_log}\n\n"
                    f"Опис:\n{suggestion_text}"
                )

                await context.bot.send_message(chat_id=SUGGESTION_CHANNEL_ID, text=fallback_text)

            else:

                raise e

        # 1. Спроба видалити попереднє повідомлення з запитом на пропозицію

        if (
            "message_to_delete_id" in context.user_data
            and "chat_id_for_delete" in context.user_data
        ):

            try:

                await context.bot.delete_message(
                    chat_id=context.user_data["chat_id_for_delete"],
                    message_id=context.user_data["message_to_delete_id"],
                )

                logger.debug(
                    f"Видалено повідомлення {context.user_data['message_to_delete_id']} в чаті {context.user_data['chat_id_for_delete']}."
                )

            except telegram.error.BadRequest as e:

                logger.warning(
                    f"Не вдалося видалити повідомлення {context.user_data.get('message_to_delete_id')}: {e}"
                )

            except Exception as e:

                logger.error(
                    f"Неочікувана помилка при видаленні повідомлення {context.user_data.get('message_to_delete_id')}: {e}"
                )

        # 2. Надсилаємо нове повідомлення-підтвердження користувачу

        confirm_text = "✅ Дякую! Вашу пропозицію успішно відправлено адміністрації."

        # ЗАМІНІТЬ НА ЦЕ

        await update.message.reply_text(
            confirm_text, reply_markup=get_correct_main_menu_keyboard(user.id)
        )

        logger.info(
            f"Пропозиція: Відправлено від {user.id} (Група: {user_group or 'N/A'}) до каналу {SUGGESTION_CHANNEL_ID}. Текст: {suggestion_text[:50]}..."
        )

    except Exception as e:

        logger.error(
            f"Пропозиція: Помилка відправлення від {user.id} до {SUGGESTION_CHANNEL_ID}: {e}",
            exc_info=True,
        )

        await update.message.reply_text(
            "❌ Виникла помилка під час відправлення пропозиції. Спробуйте пізніше.",
            reply_markup=get_main_menu_keyboard(user.id, user_group),
        )

    context.user_data.clear()

    return ConversationHandler.END


async def cancel_suggestion_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    if query:

        await query.answer()

        user = query.from_user

    else:

        user = update.effective_user

    user_id = user.id

    text = "Надсилання пропозиції скасовано."

    # ЗМІНИ ЦЕЙ БЛОК:

    if "message_to_delete_id" in context.user_data and "chat_id_for_delete" in context.user_data:

        try:

            await context.bot.delete_message(
                chat_id=context.user_data["chat_id_for_delete"],
                message_id=context.user_data["message_to_delete_id"],
            )

            logger.debug(
                f"Видалено повідомлення {context.user_data['message_to_delete_id']} в чаті {context.user_data['chat_id_for_delete']} при скасуванні пропозиції."
            )

        except telegram.error.BadRequest as e:

            # Якщо повідомлення вже видалене або не знайдено (наприклад, після перезапуску бота або якщо користувач сам видалив)

            if "Message to delete not found" in str(e):

                logger.warning(
                    f"Повідомлення пропозиції для видалення вже відсутнє або видалено (ID: {context.user_data.get('message_to_delete_id')})."
                )

            else:

                logger.error(
                    f"Помилка при видаленні повідомлення пропозиції (ID: {context.user_data.get('message_to_delete_id')}): {e}"
                )

            # Продовжуємо надсилати нове повідомлення, навіть якщо старе не видалилося

            await (query.message if query else update.message).reply_text(
                text, reply_markup=get_correct_main_menu_keyboard(user_id)
            )

            context.user_data.clear()

            return ConversationHandler.END

        except Exception as e:

            logger.error(
                f"Неочікувана помилка при видаленні повідомлення пропозиції (ID: {context.user_data.get('message_to_delete_id')}): {e}"
            )

            await (query.message if query else update.message).reply_text(
                text, reply_markup=get_correct_main_menu_keyboard(user_id)
            )

            context.user_data.clear()

            return ConversationHandler.END

    # Якщо повідомлення було успішно видалене або не існувало,

    # надсилаємо повідомлення про скасування і головне меню.

    # Якщо це callback_query, редагуємо його повідомлення, щоб не надсилати нове,

    # якщо воно вже було відредаговане або ми не хочемо створювати нове

    if query and query.message:

        try:

            await query.message.edit_text(
                text, reply_markup=get_correct_main_menu_keyboard(user_id)
            )

        except telegram.error.BadRequest as e:

            if "Message is not modified" not in str(
                e
            ):  # Якщо повідомлення справді змінилося, але не відредагувалося

                await query.message.reply_text(
                    text, reply_markup=get_correct_main_menu_keyboard(user_id)
                )

        except Exception:  # Якщо щось пішло не так при редагуванні, відправте нове

            await query.message.reply_text(
                text, reply_markup=get_correct_main_menu_keyboard(user_id)
            )

    else:  # Якщо це не callback_query (наприклад, команда /cancel), просто відправте нове

        await update.message.reply_text(text, reply_markup=get_correct_main_menu_keyboard(user_id))

    context.user_data.clear()

    return ConversationHandler.END


async def receive_report_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    report_text = update.message.text

    user = update.effective_user

    update_command_stats("report_from_button")

    user_group = get_user_group_from_db(user.id)

    if contains_profanity(report_text):

        warn_text = "⚠️ Будь ласка, використовуйте цензурну лексику. Ваше повідомлення не було відправлено. Спробуйте ще раз."

        # ЗМІНА ТУТ: Передаємо 'report' як flow_type

        await update.message.reply_text(
            warn_text, reply_markup=get_cancel_profanity_flow_keyboard("report")
        )

        return TYPING_REPORT

    # Визначаємо роль користувача

    user_role = get_user_role_from_db(user.id)

    # Викладачі відправляють репорти тільки в канал розробників

    if user_role == "teacher":

        target_channel = TEACHER_REPORT_CHANNEL_ID

        if not target_channel:

            await update.message.reply_text("Система репортів для викладачів не налаштована.")

            context.user_data.clear()

            return ConversationHandler.END

    else:

        # Студенти та інші відправляють в загальний канал репортів

        target_channel = REPORT_CHANNEL_ID

        if not target_channel:

            await update.message.reply_text("Система репортів не налаштована.")

            context.user_data.clear()

            return ConversationHandler.END

    try:

        safe_report = escape_markdown(report_text)

        safe_name = escape_markdown(user.full_name)

        safe_group = escape_markdown(user_group or "Не вказана")

        # Різні формати для викладачів та студентів

        if user_role == "teacher":

            report_message_text = (
                f"👨‍🏫 **Репорт від викладача:**\n"
                f"ID: `{user.id}`\nUsername: @{user.username or 'N/A'}\n"
                f"Ім'я: {safe_name}\n\nОпис:\n{safe_report}"
            )

        else:

            report_message_text = (
                f"🐞 **Новий репорт:**\n"
                f"ID: `{user.id}`\nUsername: @{user.username or 'N/A'}\n"
                f"Ім'я: {safe_name}\nГрупа: {safe_group}\n\nОпис:\n{safe_report}"
            )

        try:

            await context.bot.send_message(
                chat_id=target_channel, text=report_message_text, parse_mode="Markdown"
            )

        except telegram.error.BadRequest as e:

            if "can't parse entities" in str(e).lower():

                # Якщо Markdown не парситься, відправляємо без нього

                if user_role == "teacher":

                    fallback_text = (
                        f"👨‍🏫 Репорт від викладача:\n"
                        f"ID: {user.id}\nUsername: @{user.username or 'N/A'}\n"
                        f"Ім'я: {user.full_name}\n\nОпис:\n{report_text}"
                    )

                else:

                    fallback_text = (
                        f"🐞 Новий репорт:\n"
                        f"ID: {user.id}\nUsername: @{user.username or 'N/A'}\n"
                        f"Ім'я: {user.full_name}\nГрупа: {user_group or 'Не вказана'}\n\nОпис:\n{report_text}"
                    )

                await context.bot.send_message(chat_id=target_channel, text=fallback_text)

            else:

                raise e

        # Редагуємо попереднє повідомлення або надсилаємо нове підтвердження

        if "message_to_edit_id" in context.user_data and "chat_id_for_edit" in context.user_data:

            try:

                await context.bot.delete_message(
                    chat_id=context.user_data["chat_id_for_edit"],
                    message_id=context.user_data["message_to_edit_id"],
                )

                logger.debug(
                    f"Видалено повідомлення {context.user_data['message_to_edit_id']} в чаті {context.user_data['chat_id_for_edit']}."
                )

            except telegram.error.BadRequest as e:

                logger.warning(
                    f"Не вдалося видалити повідомлення {context.user_data.get('message_to_edit_id')}: {e}"
                )

            except Exception as e:

                logger.error(
                    f"Неочікувана помилка при видаленні повідомлення {context.user_data.get('message_to_edit_id')}: {e}"
                )

        # 2. Надсилаємо нове повідомлення-підтвердження

        confirm_text = "✅ Дякую! Ваш репорт відправлено адміністраторам."

        # Використовуємо update.message.reply_text, щоб підтвердження було відповіддю на останнє повідомлення користувача

        # ЗАМІНІТЬ НА ЦЕ

        await update.message.reply_text(
            confirm_text, reply_markup=get_correct_main_menu_keyboard(user.id)
        )

        channel_name = "канал розробників" if user_role == "teacher" else "загальний канал репортів"

        logger.info(
            f"Репорт: Відправлено від {user.id} (Роль: {user_role}, Група: {user_group or 'N/A'}) в {channel_name}. Текст: {report_text[:50]}..."
        )

    except Exception as e:

        logger.error(f"Репорт: Помилка відправлення від {user.id}: {e}", exc_info=True)

        # У випадку помилки, також надсилаємо нове повідомлення

        await update.message.reply_text(
            "❌ Виникла помилка під час відправлення репорту. Спробуйте пізніше.",
            reply_markup=get_main_menu_keyboard(user.id, user_group),
        )

    context.user_data.clear()

    return ConversationHandler.END


async def cancel_report_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    if query:

        await query.answer()

        user = query.from_user

    else:

        user = update.effective_user

    user_id = user.id

    text = "Надсилання репорту скасовано."

    # ЗМІНИ ЦЕЙ БЛОК:

    if "message_to_edit_id" in context.user_data and "chat_id_for_edit" in context.user_data:

        try:

            await context.bot.delete_message(
                chat_id=context.user_data["chat_id_for_edit"],
                message_id=context.user_data["message_to_edit_id"],
            )

            logger.debug(
                f"Видалено повідомлення {context.user_data['message_to_edit_id']} в чаті {context.user_data['chat_id_for_edit']} при скасуванні репорту."
            )

        except telegram.error.BadRequest as e:

            if "Message to delete not found" in str(e):

                logger.warning(
                    f"Повідомлення репорту для видалення вже відсутнє або видалено (ID: {context.user_data.get('message_to_edit_id')})."
                )

            else:

                logger.error(
                    f"Помилка при видаленні повідомлення репорту (ID: {context.user_data.get('message_to_edit_id')}): {e}"
                )

            await (query.message if query else update.message).reply_text(
                text, reply_markup=get_correct_main_menu_keyboard(user_id)
            )

            context.user_data.clear()

            return ConversationHandler.END

        except Exception as e:

            logger.error(
                f"Неочікувана помилка при видаленні повідомлення репорту (ID: {context.user_data.get('message_to_edit_id')}): {e}"
            )

            await (query.message if query else update.message).reply_text(
                text, reply_markup=get_correct_main_menu_keyboard(user_id)
            )

            context.user_data.clear()

            return ConversationHandler.END

    # Якщо повідомлення було успішно видалене або не існувало,

    # надсилаємо повідомлення про скасування і головне меню.

    if query and query.message:

        try:

            await query.message.edit_text(
                text, reply_markup=get_correct_main_menu_keyboard(user_id)
            )

        except telegram.error.BadRequest as e:

            if "Message is not modified" not in str(e):

                await query.message.reply_text(
                    text, reply_markup=get_correct_main_menu_keyboard(user_id)
                )

        except Exception:

            await query.message.reply_text(
                text, reply_markup=get_correct_main_menu_keyboard(user.id)
            )

    else:

        await update.message.reply_text(text, reply_markup=get_correct_main_menu_keyboard(user_id))

    context.user_data.clear()

    return ConversationHandler.END


async def admin_panel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    user_id = update.effective_user.id

    if user_id not in ADMIN_USER_IDS:

        text = "Доступ заборонено."

        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)

        elif update.message:
            await update.message.reply_text(text)

        return

    text = "🛠️ Панель адміністратора:"

    reply_markup = get_admin_panel_keyboard()

    if update.callback_query:

        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="HTML"
        )

    elif update.message:

        update_command_stats(update.message.text.split()[0])

        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def admin_announce_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    if query:
        await query.answer()

    keyboard = [
        [InlineKeyboardButton("Всім користувачам", callback_data="announce_target_all")],
        [InlineKeyboardButton("Конкретній групі", callback_data="announce_target_group")],
        [InlineKeyboardButton(" Скасувати", callback_data="announce_cancel")],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    text = "📢 Оголошення: Оберіть цільову аудиторію:"

    if query:
        await query.edit_message_text(text, reply_markup=reply_markup)

    return ANNOUNCE_SELECT_TARGET


async def announce_select_target_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query

    await query.answer()

    target = query.data

    if target == "announce_target_all":

        context.user_data["announce_target_group"] = None

        text = "Оберіть тип оголошення для *ВСІХ* користувачів:"

    elif target == "announce_target_group":

        all_groups = get_all_group_names_from_cache()

        if not all_groups:

            await query.edit_message_text(
                "Немає груп для вибору.", reply_markup=get_back_to_admin_panel_keyboard()
            )

            context.user_data.clear()

            return ConversationHandler.END

        group_buttons = [
            [InlineKeyboardButton(g, callback_data=f"announce_select_group_for_type_{g}")]
            for g in all_groups
        ]

        group_buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="announce_cancel")])

        reply_markup = InlineKeyboardMarkup(group_buttons)

        await query.edit_message_text("Оберіть групу для оголошення:", reply_markup=reply_markup)

        return ANNOUNCE_SELECT_GROUP_FOR_ANNOUNCE

    keyboard = [
        [InlineKeyboardButton("Тільки текст", callback_data="announce_type_text")],
        [InlineKeyboardButton("Фото / Альбом з текстом", callback_data="announce_type_media")],
        [InlineKeyboardButton(" Скасувати", callback_data="announce_cancel")],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    return ANNOUNCE_CHOOSING_MEDIA_TYPE


async def announce_select_group_for_announce_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query

    await query.answer()

    group_name = query.data.replace("announce_select_group_for_type_", "")

    context.user_data["announce_target_group"] = group_name

    text = f"Оберіть тип оголошення для групи *{group_name}*:"

    keyboard = [
        [InlineKeyboardButton("Тільки текст", callback_data="announce_type_text")],
        [InlineKeyboardButton("Фото / Альбом з текстом", callback_data="announce_type_media")],
        [InlineKeyboardButton(" Скасувати", callback_data="announce_cancel")],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    return ANNOUNCE_CHOOSING_MEDIA_TYPE


async def announce_choose_media_type_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query

    await query.answer()

    media_type = query.data.replace("announce_type_", "")

    context.user_data["announce_media_type"] = media_type

    if media_type == "text":

        target_description = (
            f"групи *{context.user_data['announce_target_group']}*"
            if context.user_data.get("announce_target_group")
            else "*ВСІХ* користувачів"
        )

        await query.edit_message_text(
            f"Введіть текст оголошення для {target_description}:", parse_mode="Markdown"
        )

        return ANNOUNCE_TYPING_MESSAGE_FOR_ANNOUNCE

    elif media_type == "media":

        await query.edit_message_text(
            f"Надішліть фото для оголошення.\nВи можете відправити до {MAX_ALBUM_PHOTOS} фотографій як альбом.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Скасувати", callback_data="announce_cancel_media")]]
            ),
        )

        context.user_data["media_group_photos"] = []

        return ANNOUNCE_WAITING_FOR_PHOTOS


async def announce_waiting_for_photos_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    if update.message.photo:

        photo_file_id = update.message.photo[-1].file_id

        context.user_data["media_group_photos"].append(photo_file_id)

        current_photo_count = len(context.user_data["media_group_photos"])

        if current_photo_count >= MAX_ALBUM_PHOTOS:

            await update.message.reply_text(
                f"Ви завантажили максимальну кількість фото ({MAX_ALBUM_PHOTOS}).\nТепер введіть підпис для фото:",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Скасувати", callback_data="announce_cancel_media")]]
                ),
            )

            return ANNOUNCE_TYPING_CAPTION_FOR_MEDIA

        else:

            await update.message.reply_text(
                f"Фото додано до альбому ({current_photo_count}/{MAX_ALBUM_PHOTOS}).\n"
                "Відправте ще фото або введіть підпис для оголошення, якщо це всі фото:",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Скасувати", callback_data="announce_cancel_media")]]
                ),
            )

            return ANNOUNCE_WAITING_FOR_PHOTOS

    elif update.message.text:

        if not context.user_data["media_group_photos"]:

            await update.message.reply_text(
                "Будь ласка, спочатку надішліть хоча б одне фото, або скасуйте.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Скасувати", callback_data="announce_cancel_media")]]
                ),
            )

            return ANNOUNCE_WAITING_FOR_PHOTOS

        context.user_data["announcement_caption"] = update.message.text

        return await finalize_media_announcement_send(update, context)

    else:

        await update.message.reply_text(
            "Будь ласка, надішліть фото або введіть текст для підпису.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Скасувати", callback_data="announce_cancel_media")]]
            ),
        )

        return ANNOUNCE_WAITING_FOR_PHOTOS


async def announce_typing_caption_for_media_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    if update.message.text:

        context.user_data["announcement_caption"] = update.message.text

        return await finalize_media_announcement_send(update, context)

    else:

        await update.message.reply_text(
            "Будь ласка, введіть текст підпису.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Скасувати", callback_data="announce_cancel_media")]]
            ),
        )

        return ANNOUNCE_TYPING_CAPTION_FOR_MEDIA


async def finalize_media_announcement_send(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    announcement_caption = context.user_data.get("announcement_caption", "")

    media_file_ids = context.user_data.get("media_group_photos", [])

    target_group = context.user_data.get("announce_target_group")

    target_description = f"групи {target_group}" if target_group else "всіх користувачів"

    if not media_file_ids:

        await update.message.reply_text(
            "Немає фото для відправки оголошення.", reply_markup=get_admin_panel_keyboard()
        )

        context.user_data.clear()

        return ConversationHandler.END

    user_ids_to_send = list(
        get_all_user_ids_from_db(group_name=target_group if target_group else None)
    )

    total_users = len(user_ids_to_send)

    if not user_ids_to_send:

        await update.message.reply_text(
            f"Немає користувачів для розсилки ({target_description}).",
            reply_markup=get_admin_panel_keyboard(),
        )

        context.user_data.clear()

        return ConversationHandler.END

    progress_message = await update.message.reply_text(
        f"Розпочинаю розсилку для {total_users} ({target_description})... 0/{total_users} надіслано."
    )

    sent_count, failed_count, dlq_added_count = 0, 0, 0

    media_objects = []

    for i, file_id in enumerate(media_file_ids):

        if i == 0 and announcement_caption:

            media_objects.append(
                InputMediaPhoto(
                    media=file_id,
                    caption=f"📢 ОГОЛОШЕННЯ 📢\n\n{announcement_caption}",
                    parse_mode="Markdown",
                )
            )

        else:

            media_objects.append(InputMediaPhoto(media=file_id))

    for idx, user_id in enumerate(user_ids_to_send):

        try:

            if len(media_objects) > 1:

                await context.bot.send_media_group(chat_id=user_id, media=media_objects)

            else:

                caption_to_send = (
                    media_objects[0].caption if hasattr(media_objects[0], "caption") else None
                )

                parse_mode_to_send = (
                    media_objects[0].parse_mode if hasattr(media_objects[0], "parse_mode") else None
                )

                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=media_objects[0].media,
                    caption=caption_to_send,
                    parse_mode=parse_mode_to_send,
                )

            sent_count += 1

            if (idx + 1) % PROGRESS_UPDATE_INTERVAL == 0 or (idx + 1) == total_users:

                try:

                    await progress_message.edit_text(
                        f"Розсилка для {total_users} ({target_description})... {sent_count}/{total_users} надіслано. Невдачі: {failed_count}."
                    )

                except telegram.error.BadRequest as e:

                    if "Message is not modified" in str(e):

                        logger.debug(f"Progress message not modified: {e}")

                    else:

                        logger.warning(f"Failed to edit progress message: {e}")

            time.sleep(0.1)

        except Exception as e:

            logger.error(f"Розсилка медіа: Не вдалося {user_id}: {e}")

            failed_count += 1

            add_to_dlq(user_id, f"[Оголошення з фото] {announcement_caption}", str(e))

            dlq_added_count += 1

            if (idx + 1) % PROGRESS_UPDATE_INTERVAL == 0 or (idx + 1) == total_users:

                try:

                    await progress_message.edit_text(
                        f"Розсилка для {total_users} ({target_description})... {sent_count}/{total_users} надіслано. Невдачі: {failed_count}. Додано в DLQ: {dlq_added_count}."
                    )

                except telegram.error.BadRequest as e:

                    if "Message is not modified" in str(e):

                        logger.debug(f"Progress message not modified on failure: {e}")

                    else:

                        logger.warning(f"Failed to edit progress message on failure: {e}")

    summary_text = (
        f"Розсилку медіа-оголошення завершено.\nЦіль: {target_description}\n✅ Відправлено: {sent_count}\n"
        f"❌ Невдало: {failed_count}\n📬 Додано в DLQ: {dlq_added_count}"
    )

    await update.message.reply_text(summary_text, reply_markup=get_admin_panel_keyboard())

    logger.info(f"Адмін {update.effective_user.id} розіслав медіа-оголошення. {summary_text}")

    context.user_data.clear()

    return ConversationHandler.END


async def announce_cancel_media_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    await query.answer()

    context.user_data.clear()

    await query.edit_message_text(
        "Створення медіа-оголошення скасовано.", reply_markup=get_admin_panel_keyboard()
    )

    return ConversationHandler.END


async def announce_typed_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    announcement_text = update.message.text

    target_group = context.user_data.get("announce_target_group")

    target_description = f"групи {target_group}" if target_group else "всіх користувачів"

    user_ids_to_send = list(
        get_all_user_ids_from_db(group_name=target_group if target_group else None)
    )

    total_users = len(user_ids_to_send)

    if not user_ids_to_send:

        await update.message.reply_text(
            f"Немає користувачів для розсилки ({target_description}).",
            reply_markup=get_admin_panel_keyboard(),
        )

        context.user_data.clear()

        return ConversationHandler.END

    progress_message = await update.message.reply_text(
        f"Розпочинаю розсилку для {total_users} ({target_description})... 0/{total_users} надіслано."
    )

    sent_count, failed_count, dlq_added_count = 0, 0, 0

    full_message_to_send = f"📢 ОГОЛОШЕННЯ 📢\n\n{announcement_text}"

    for idx, user_id in enumerate(user_ids_to_send):

        try:

            await context.bot.send_message(chat_id=user_id, text=full_message_to_send)

            sent_count += 1

            if (idx + 1) % PROGRESS_UPDATE_INTERVAL == 0 or (idx + 1) == total_users:

                try:

                    await progress_message.edit_text(
                        f"Розсилка для {total_users} ({target_description})... {sent_count}/{total_users} надіслано. Невдачі: {failed_count}."
                    )

                except telegram.error.BadRequest as e:

                    if "Message is not modified" in str(e):

                        logger.debug(f"Progress message not modified: {e}")

                    else:

                        logger.warning(f"Failed to edit progress message: {e}")

            time.sleep(0.1)

        except Exception as e:

            logger.error(f"Розсилка: Не вдалося {user_id}: {e}")

            failed_count += 1

            add_to_dlq(user_id, announcement_text, str(e))

            dlq_added_count += 1

            if (idx + 1) % PROGRESS_UPDATE_INTERVAL == 0 or (idx + 1) == total_users:

                try:

                    await progress_message.edit_text(
                        f"Розсилка для {total_users} ({target_description})... {sent_count}/{total_users} надіслано. Невдачі: {failed_count}. Додано в DLQ: {dlq_added_count}."
                    )

                except telegram.error.BadRequest as e:

                    if "Message is not modified" in str(e):

                        logger.debug(f"Progress message not modified on failure: {e}")

                    else:

                        logger.warning(f"Failed to edit progress message on failure: {e}")

    summary_text = (
        f"Розсилку завершено.\nЦіль: {target_description}\n✅ Відправлено: {sent_count}\n"
        f"❌ Невдало: {failed_count}\n📬 Додано в DLQ: {dlq_added_count}"
    )

    await update.message.reply_text(summary_text, reply_markup=get_admin_panel_keyboard())

    logger.info(f"Адмін {update.effective_user.id} розіслав '{announcement_text}'. {summary_text}")

    context.user_data.clear()

    return ConversationHandler.END


async def announce_cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    await query.answer()

    context.user_data.clear()

    await query.edit_message_text(
        "Створення оголошення скасовано.", reply_markup=get_admin_panel_keyboard()
    )

    return ConversationHandler.END


async def announce_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if update.effective_user.id not in ADMIN_USER_IDS:

        await update.message.reply_text("Доступ заборонено.")

        return

    update_command_stats("/announce")

    if not context.args:

        await update.message.reply_text(
            "Використовуйте кнопку 'Оголошення' або /announce ТЕКСТ.",
            reply_markup=get_admin_panel_keyboard(),
        )

        return

    announcement_text = " ".join(context.args)

    all_user_ids = list(get_all_user_ids_from_db())

    total_users = len(all_user_ids)

    if not all_user_ids:

        await update.message.reply_text("Немає користувачів для розсилки.")

        return

    progress_message = await update.message.reply_text(
        f"Розпочинаю розсилку (всім) для {total_users} користувачів... 0/{total_users} надіслано."
    )

    sent_count, failed_count, dlq_added_count = 0, 0, 0

    full_message_to_send = f"📢 ОГОЛОШЕННЯ 📢\n\n{announcement_text}"

    for idx, user_id in enumerate(all_user_ids):

        try:

            await context.bot.send_message(chat_id=user_id, text=full_message_to_send)

            sent_count += 1

            if (idx + 1) % PROGRESS_UPDATE_INTERVAL == 0 or (idx + 1) == total_users:

                try:

                    await progress_message.edit_text(
                        f"Розсилка (всім) для {total_users} користувачів... {sent_count}/{total_users} надіслано. Невдачі: {failed_count}."
                    )

                except telegram.error.BadRequest as e:

                    if "Message is not modified" in str(e):

                        logger.debug(f"Progress message not modified: {e}")

                    else:

                        logger.warning(f"Failed to edit progress message: {e}")

            time.sleep(0.1)

        except Exception as e:

            logger.error(f"Розсилка (команда): Не вдалося {user_id}: {e}")

            failed_count += 1

            add_to_dlq(user_id, announcement_text, str(e))

            dlq_added_count += 1

            if (idx + 1) % PROGRESS_UPDATE_INTERVAL == 0 or (idx + 1) == total_users:

                try:

                    await progress_message.edit_text(
                        f"Розсилка (всім) для {total_users} користувачів... {sent_count}/{total_users} надіслано. Невдачі: {failed_count}. Додано в DLQ: {dlq_added_count}."
                    )

                except telegram.error.BadRequest as e:

                    if "Message is not modified" in str(e):

                        logger.debug(f"Progress message not modified on failure: {e}")

                    else:

                        logger.warning(f"Failed to edit progress message on failure: {e}")

    summary_text = (
        f"Розсилку (команда) завершено.\n✅ Відправлено: {sent_count}\n"
        f"❌ Невдало: {failed_count}\n📬 Додано в DLQ: {dlq_added_count}"
    )

    await update.message.reply_text(summary_text)

    logger.info(f"Адмін {update.effective_user.id} розіслав '{announcement_text}'. {summary_text}")


async def view_dlq_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    user_id_effective = update.effective_user.id

    if user_id_effective not in ADMIN_USER_IDS:

        if update.callback_query:
            await update.callback_query.answer("Доступ заборонено.", show_alert=True)

        elif update.message:
            await update.message.reply_text("Доступ заборонено.")

        return

    response_text = "📄 Останні записи в Dead Letter Queue (нові):\n\n"

    has_records = False

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            conn.row_factory = sqlite3.Row

            cursor = conn.cursor()

            cursor.execute(
                "SELECT id, user_id, SUBSTR(message_text, 1, 25) || '...' AS short_msg, error_message, failed_at, status FROM dead_letter_queue WHERE status = 'new' ORDER BY failed_at DESC LIMIT 10"
            )

            for row in cursor.fetchall():

                has_records = True

                failed_at_str = "N/A"

                try:

                    failed_at_dt_naive = datetime.fromisoformat(row["failed_at"].split(".")[0])

                    failed_at_dt_utc = failed_at_dt_naive.replace(tzinfo=ZoneInfo("UTC"))

                    failed_at_kyiv = failed_at_dt_utc.astimezone(KYIV_TZ)

                    failed_at_str = failed_at_kyiv.strftime("%Y-%m-%d %H:%M %Z")

                except Exception as e_parse:

                    logger.warning(
                        f"DLQ: Не вдалося розпарсити/конвертувати failed_at ('{row['failed_at']}'): {e_parse}"
                    )

                    failed_at_str = str(row["failed_at"])

                response_text += (
                    f"`ID: {row['id']}` | `User: {row['user_id']}` | `{failed_at_str}`\n"
                    f"Msg: `{row['short_msg']}`\nError: `{row['error_message']}` (`{row['status']}`)\n---\n"
                )

        if not has_records:
            response_text = "DLQ порожня або немає нових записів. 👍"

    except sqlite3.Error as e:

        logger.error(f"DLQ: Помилка читання: {e}")

        response_text = "Помилка отримання даних з DLQ."

    reply_markup = get_back_to_admin_panel_keyboard()

    if update.callback_query:

        await update.callback_query.edit_message_text(
            response_text, reply_markup=reply_markup, parse_mode="Markdown"
        )

    elif update.message:

        update_command_stats("/view_dlq")

        await update.message.reply_text(
            response_text, reply_markup=reply_markup, parse_mode="Markdown"
        )


async def admin_clear_dlq_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query

    user_id = query.from_user.id

    if user_id not in ADMIN_USER_IDS:

        await query.answer("Доступ заборонено.", show_alert=True)

        return

    await query.answer("Очищення DLQ...", show_alert=False)

    # Видаляємо старі "нові" записи

    deleted_new = clear_dlq(status="new", older_than_days=30)

    # Видаляємо всі "оброблені" записи

    deleted_processed = clear_dlq(status="processed", older_than_days=0)

    if deleted_new >= 0 and deleted_processed >= 0:

        response_text = (
            f"✅ DLQ очищено!\n"
            f"Видалено нових записів (старше 30 днів): {deleted_new}\n"
            f"Видалено оброблених записів: {deleted_processed}"
        )

    else:

        response_text = "❌ Помилка під час очищення DLQ. Дивіться логи."

    await query.edit_message_text(
        response_text, reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown"
    )

    logger.info(
        f"Адмін {user_id} ініціював очищення DLQ. Результат: {response_text.replace('*', '')}"
    )


async def show_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    user_id_effective = update.effective_user.id

    if user_id_effective not in ADMIN_USER_IDS:

        if update.callback_query:
            await update.callback_query.answer("Доступ заборонено.", show_alert=True)

        elif update.message:
            await update.message.reply_text("Доступ заборонено.")

        return

    response_text = "*📊 Статистика використання бота:*\n\n"

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            cursor = conn.cursor()

            # Total unique users

            cursor.execute("SELECT COUNT(DISTINCT user_id) FROM users")

            total_users = cursor.fetchone()[0]

            response_text += f"👥 *Всього унікальних користувачів:* {total_users}\n"

            # Users with selected group

            cursor.execute(
                "SELECT COUNT(DISTINCT user_id) FROM users WHERE group_name IS NOT NULL AND group_name != 'ASK_LATER'"
            )

            users_with_group = cursor.fetchone()[0]

            response_text += f"👤 *Користувачів з обраною групою:* {users_with_group}\n\n"

            # New users over periods (assuming joined_date is reliable for new users)

            today_kyiv = datetime.now(KYIV_TZ)

            intervals = {"7 днів": 7, "30 днів": 30, "90 днів": 90}

            response_text += "*📈 Нові користувачі за періодами (за датою приєднання):*\n"

            for label, days in intervals.items():

                past_date = today_kyiv - timedelta(days=days)

                cursor.execute(
                    "SELECT COUNT(DISTINCT user_id) FROM users WHERE joined_date >= ?",
                    (past_date.isoformat(),),
                )

                new_users_count = cursor.fetchone()[0]

                response_text += f"  • Останні {label}: {new_users_count}\n"

            # Total bot usage (sum of command counts)

            cursor.execute("SELECT SUM(count) FROM command_stats")

            total_commands_used = cursor.fetchone()[0] or 0

            response_text += f"\n*🤖 Загальна активність бота (всього команд/кнопок використано):* {total_commands_used}\n\n"

            # User distribution by group

            response_text += "*📊 Розподіл користувачів за групами:*\n"

            cursor.execute(
                "SELECT group_name, COUNT(user_id) FROM users WHERE group_name IS NOT NULL AND group_name != 'ASK_LATER' GROUP BY group_name ORDER BY COUNT(user_id) DESC"
            )

            group_distribution = cursor.fetchall()

            if group_distribution:

                for group, count in group_distribution:

                    response_text += f"  • {group}: {count}\n"

            else:

                response_text += "  _Немає користувачів з обраними групами._\n"

            # Top 10 commands/buttons

            response_text += "\n*🏆 Топ-10 команд/кнопок:*\n"

            cursor.execute("SELECT command, count FROM command_stats ORDER BY count DESC LIMIT 10")

            top_commands = cursor.fetchall()

            if top_commands:

                for i, (cmd, count) in enumerate(top_commands):

                    response_text += f"{i+1}. `{cmd}`: {count} разів\n"

            else:

                response_text += "_Статистика команд ще не зібрана._\n"

    except sqlite3.Error as e:

        logger.error(f"Статистика: Помилка БД: {e}")

        response_text = "Помилка завантаження статистики."

    reply_markup = get_back_to_admin_panel_keyboard()

    if update.callback_query:

        await update.callback_query.edit_message_text(
            response_text, reply_markup=reply_markup, parse_mode="Markdown"
        )

    elif update.message:

        update_command_stats("/stats")

        await update.message.reply_text(
            response_text, reply_markup=reply_markup, parse_mode="Markdown"
        )


async def admin_clear_cache_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query

    if query.from_user.id not in ADMIN_USER_IDS:

        await query.answer("Недостатньо прав!", show_alert=True)

        return

    clear_schedule_cache_data()

    get_cached_schedule()

    await query.answer(
        "Кеш розкладу в пам'яті очищено. Наступний запит оновить його з БД.", show_alert=True
    )

    logger.info(f"Адмін {query.from_user.id} очистив кеш розкладу в пам'яті.")


async def server_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    user_id_effective = update.effective_user.id

    if user_id_effective not in ADMIN_USER_IDS:

        if update.callback_query:
            await update.callback_query.answer("Доступ заборонено.", show_alert=True)

        elif update.message:
            await update.message.reply_text("Доступ заборонено.")

        return

    status_text = get_server_status_text()

    reply_markup = get_back_to_admin_panel_keyboard()

    if update.callback_query:

        await update.callback_query.edit_message_text(
            status_text, reply_markup=reply_markup, parse_mode="Markdown"
        )

    elif update.message:

        update_command_stats("/server_status")

        await update.message.reply_text(
            status_text, reply_markup=reply_markup, parse_mode="Markdown"
        )


async def admin_upload_db_to_ftp_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    query = update.callback_query

    user_id = query.from_user.id

    if user_id not in ADMIN_USER_IDS:

        await query.answer("Доступ заборонено.", show_alert=True)

        return

    if not ENABLE_FTP_SYNC:

        await query.answer("FTP синхронізація вимкнена в налаштуваннях.", show_alert=True)

        return

    await context.bot.send_message(chat_id=user_id, text="Розпочинаю завантаження БД на FTP...")

    success = upload_db_to_ftp()

    if success:

        await context.bot.send_message(
            chat_id=user_id, text="✅ База даних успішно завантажена на FTP."
        )

        logger.info(f"Адмін {user_id} вручну завантажив БД на FTP.")

    else:

        await context.bot.send_message(
            chat_id=user_id, text="❌ Помилка під час завантаження БД на FTP. Дивіться логи."
        )

        logger.error(f"Адмін {user_id}: невдала спроба вручну завантажити БД на FTP.")


async def admin_download_local_db_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    user_id_to_send_to = -1

    query = update.callback_query

    if query:

        user_id_to_send_to = query.from_user.id

        try:

            await query.answer()

        except Exception as e:

            logger.debug(f"Error answering callback for admin_download_local_db: {e}")

    elif update.message:

        user_id_to_send_to = update.message.from_user.id

    else:

        logger.error("admin_download_local_db_handler called without query or message.")

        return

    if user_id_to_send_to not in ADMIN_USER_IDS:

        await context.bot.send_message(chat_id=user_id_to_send_to, text="Доступ заборонено.")

        return

    db_path = DATABASE_NAME

    if not os.path.exists(db_path):

        logger.error(
            f"Файл бази даних '{db_path}' не знайдено для завантаження адміном {user_id_to_send_to}."
        )

        await context.bot.send_message(
            chat_id=user_id_to_send_to, text=f"❌ Файл бази даних '{db_path}' не знайдено."
        )

        return

    try:

        await context.bot.send_message(
            chat_id=user_id_to_send_to, text=f"🔄 Надсилаю файл бази даних ({db_path})..."
        )

        with open(db_path, "rb") as db_file:

            await context.bot.send_document(
                chat_id=user_id_to_send_to,
                document=db_file,
                filename=DATABASE_NAME,
                caption=f"Локальна база даних станом на {datetime.now(KYIV_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}",
            )

        logger.info(f"Адмін {user_id_to_send_to} завантажив локальну БД: {db_path}")

    except Exception as e:

        logger.error(f"Помилка надсилання файлу БД адміну {user_id_to_send_to}: {e}")

        await context.bot.send_message(
            chat_id=user_id_to_send_to,
            text=f"❌ Не вдалося надіслати файл бази даних. Помилка: {e}",
        )


async def admin_reload_schedule_from_json_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:

    user_id_to_send_to = -1

    query = update.callback_query

    message_obj = update.message

    if query:

        user_id_to_send_to = query.from_user.id

        if query.message:
            await query.message.chat.send_action("typing")

        await query.answer("Розпочинаю перезавантаження розкладу...", show_alert=False)

    elif message_obj:

        user_id_to_send_to = message_obj.from_user.id

        await message_obj.chat.send_action("typing")

    else:

        return

    if user_id_to_send_to not in ADMIN_USER_IDS:

        if query:
            await query.answer("Доступ заборонено.", show_alert=True)

        elif message_obj:
            await message_obj.reply_text("Доступ заборонено.")

        return

    if sql_manager is None:

        if query:
            await query.answer("Помилка: SQLManager не ініціалізовано.", show_alert=True)

        elif message_obj:
            await message_obj.reply_text("Помилка: SQLManager не ініціалізовано.")

        logger.warning(
            f"Адмін {user_id_to_send_to} спробував перезавантажити розклад, але SQLManager не ініціалізований."
        )

        return

    logger.info(
        f"Адмін {user_id_to_send_to} ініціював перезавантаження розкладу з файлу: {SCHEDULE_JSON_SOURCE_FILE}."
    )

    try:

        clear_schedule_cache_data()

        sql_manager.encode_json()

        sql_manager.get_static(force_reload=True)

        get_cached_schedule()

        await context.bot.send_message(
            chat_id=user_id_to_send_to,
            text=f"✅ Розклад успішно перезавантажено з файлу {SCHEDULE_JSON_SOURCE_FILE}.",
        )

        logger.info(f"Розклад успішно перезавантажено адміном {user_id_to_send_to}.")

    except FileNotFoundError:

        await context.bot.send_message(
            chat_id=user_id_to_send_to, text=f"❌ Файл {SCHEDULE_JSON_SOURCE_FILE} не знайдено."
        )

        logger.error(f"Перезавантаження розкладу: Файл {SCHEDULE_JSON_SOURCE_FILE} не знайдено.")

    except Exception as e:

        await context.bot.send_message(
            chat_id=user_id_to_send_to, text=f"❌ Помилка перезавантаження розкладу: {e}"
        )

        logger.error(
            f"Помилка перезавантаження розкладу адміном {user_id_to_send_to}: {e}", exc_info=True
        )


async def ftp_sync_db_job_callback(context: ContextTypes.DEFAULT_TYPE):

    logger.info("FTP: Запускається планове завантаження БД на FTP...")

    if upload_db_to_ftp():

        logger.info("FTP: Планове завантаження БД успішно завершено.")

    else:

        logger.warning("FTP: Планове завантаження БД завершилося з помилкою.")


def upload_db_to_ftp():

    if not (ENABLE_FTP_SYNC and FTP_HOST and FTP_USER and FTP_PASSWORD and FTP_REMOTE_DB_PATH):

        if ENABLE_FTP_SYNC:
            logger.warning("FTP: Синхронізація увімкнена, але не всі FTP дані налаштовані.")

        return False

    try:

        ftp_port = int(FTP_PORT_STR)

        with FTP() as ftp:

            logger.info(f"FTP: Підключення до {FTP_HOST}:{ftp_port}...")

            ftp.connect(FTP_HOST, ftp_port, timeout=30)

            logger.info(f"FTP: Логін користувачем {FTP_USER}...")

            ftp.login(FTP_USER, FTP_PASSWORD)

            logger.info(f"FTP: Успішно підключено та залогінено.")

            remote_dir = os.path.dirname(FTP_REMOTE_DB_PATH)

            remote_filename = os.path.basename(FTP_REMOTE_DB_PATH)

            if remote_dir and remote_dir != "/":

                path_parts = remote_dir.strip("/").split("/")

                current_path_on_ftp = ""

                for part in path_parts:

                    if not part:
                        continue

                    current_path_on_ftp += "/" + part.lstrip("/")

                    try:

                        ftp.cwd(current_path_on_ftp)

                        logger.debug(f"FTP: Змінено директорію на '{current_path_on_ftp}'")

                    except error_perm:

                        logger.info(
                            f"FTP: Директорія '{current_path_on_ftp}' не знайдена, спроба створити."
                        )

                        try:

                            ftp.mkd(current_path_on_ftp)

                            logger.info(f"FTP: Створено директорію '{current_path_on_ftp}'")

                            ftp.cwd(current_path_on_ftp)

                        except error_perm as e_mkd_inner:

                            logger.error(
                                f"FTP: Не вдалося створити або перейти в директорію '{current_path_on_ftp}': {e_mkd_inner}"
                            )

                            return False

            elif remote_dir == "/":

                ftp.cwd("/")

                logger.debug("FTP: Встановлено кореневу директорію.")

            with open(DATABASE_NAME, "rb") as f:

                current_ftp_dir = ftp.pwd()

                logger.info(
                    f"FTP: Розпочато завантаження '{DATABASE_NAME}' як '{remote_filename}' в '{current_ftp_dir}'."
                )

                ftp.storbinary(f"STOR {remote_filename}", f)

            logger.info(
                f"FTP: База даних '{DATABASE_NAME}' успішно завантажена на '{FTP_REMOTE_DB_PATH}'."
            )

            return True

    except error_perm as e_perm:

        logger.critical(f"FTP: Помилка прав доступу або файлових операцій: {e_perm}")

        return False

    except Exception as e:

        logger.critical(f"FTP: Критична помилка під час завантаження БД на FTP: {e}")

        return False


def download_db_from_ftp():

    if not (ENABLE_FTP_SYNC and FTP_HOST and FTP_USER and FTP_PASSWORD and FTP_REMOTE_DB_PATH):

        if ENABLE_FTP_SYNC:
            logger.warning(
                "FTP: Синхронізація (завантаження) увімкнена, але не всі FTP дані налаштовані."
            )

        return False

    try:

        ftp_port = int(FTP_PORT_STR)

        with FTP() as ftp:

            logger.info(f"FTP: Підключення до {FTP_HOST}:{ftp_port} для завантаження БД...")

            ftp.connect(FTP_HOST, ftp_port, timeout=30)

            logger.info(f"FTP: Логін користувачем {FTP_USER}...")

            ftp.login(FTP_USER, FTP_PASSWORD)

            logger.info(f"FTP: Успішно підключено та залогінено для завантаження БД.")

            remote_dir = os.path.dirname(FTP_REMOTE_DB_PATH)

            remote_filename = os.path.basename(FTP_REMOTE_DB_PATH)

            target_ftp_dir = remote_dir

            if not target_ftp_dir or target_ftp_dir == ".":

                target_ftp_dir = ftp.pwd()

            if remote_dir and remote_dir != "/":

                try:

                    ftp.cwd(remote_dir)

                    target_ftp_dir = remote_dir

                    logger.debug(f"FTP: Змінено директорію на '{remote_dir}' для завантаження.")

                except error_perm:

                    logger.warning(
                        f"FTP: Директорія '{remote_dir}' не знайдена на FTP при спробі завантаження БД."
                    )

                    return False

            elif remote_dir == "/":

                ftp.cwd("/")

                target_ftp_dir = "/"

                logger.debug("FTP: Встановлено кореневу директорію для завантаження БД.")

            file_list = ftp.nlst()

            if remote_filename not in file_list:

                logger.warning(
                    f"FTP: Файл '{remote_filename}' не знайдено в '{target_ftp_dir}'. Список файлів: {file_list}"
                )

                return False

            # Ensure the directory exists before creating the file

            db_dir = os.path.dirname(DATABASE_NAME)

            if db_dir and not os.path.exists(db_dir):

                os.makedirs(db_dir, exist_ok=True)

                logger.info(f"FTP: Створено директорію для БД: {db_dir}")

            with open(DATABASE_NAME, "wb") as f:

                logger.info(
                    f"FTP: Розпочато завантаження '{remote_filename}' з '{target_ftp_dir}' до '{DATABASE_NAME}'."
                )

                ftp.retrbinary(f"RETR {remote_filename}", f.write)

            logger.info(
                f"FTP: База даних '{DATABASE_NAME}' успішно завантажена з '{FTP_REMOTE_DB_PATH}'."
            )

            return True

    except error_perm as e_perm:

        logger.warning(
            f"FTP: Помилка прав або файл/директорія не знайдено під час завантаження БД: {e_perm}."
        )

        return False

    except OSError as e:

        logger.warning(f"FTP: Помилка операційної системи під час завантаження БД: {e}")

        return False

    except Exception as e:

        logger.critical(f"FTP: Критична помилка під час завантаження БД з FTP: {e}")

        return False


# --- Нові обробники для розіграшу ---


async def show_raffle_info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    if await check_maintenance_and_reply(update, context):

        return ConversationHandler.END

    query = update.callback_query

    user_id = query.from_user.id

    current_time = datetime.now(KYIV_TZ)

    if not RAFFLE_ACTIVE or current_time >= RAFFLE_END_DATE:

        text = "❌ На жаль, розіграш неактивний або вже завершився."

        reply_markup = get_back_to_main_menu_keyboard()

        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

        return ConversationHandler.END

    time_left: timedelta = RAFFLE_END_DATE - current_time

    days = time_left.days

    hours, remainder = divmod(time_left.seconds, 3600)

    minutes, seconds = divmod(remainder, 60)

    time_left_str = ""

    if days > 0:

        time_left_str += f"{days} дн. "

    if hours > 0:

        time_left_str += f"{hours} год. "

    time_left_str += f"{minutes} хв."

    user_is_participant = get_raffle_participant_status(user_id)

    referred_count = get_referred_count(user_id)

    status_text = ""

    if user_is_participant:

        status_text = "✅ Ви вже берете участь у розіграші!"

    else:

        status_text = "Ви ще не берете участь у розіграші."

    text = (
        f"🎁 *Розіграш {RAFFLE_PRIZE.upper()}* 🎁\n\n"
        f"Умови:\n"
        f"1. Бути підписаним на Telegram канал: @{RAFFLE_CHANNEL_USERNAME}\n"
        f"2. Запросити 1 друга в цей бот за вашим реферальним посиланням.\n\n"
        f"Ваше реферальне посилання: `https://t.me/{context.bot.username}?start={user_id}`\n"
        f"Запрошених друзів: *{referred_count}*\n\n"
        f"Закінчення розіграшу: *{RAFFLE_END_DATE.strftime('%d.%m.%Y о %H:%M')}* (за Київським часом)\n"
        f"Залишилось: *{time_left_str}*\n\n"
        f"{status_text}"
    )

    reply_markup = get_raffle_menu_keyboard(user_id)

    await query.edit_message_text(
        text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True
    )

    return RAFFLE_MENU


async def raffle_join_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    user_id = query.from_user.id

    if get_raffle_participant_status(user_id):

        await query.answer("Ви вже берете участь у розіграші!", show_alert=True)

        return RAFFLE_MENU

    try:

        chat_member = await context.bot.get_chat_member(
            chat_id=f"@{RAFFLE_CHANNEL_USERNAME}", user_id=user_id
        )

        if chat_member.status not in ["member", "administrator", "creator"]:

            logger.info(
                f"Користувач {user_id} не підписаний на канал @{RAFFLE_CHANNEL_USERNAME}. Статус: {chat_member.status}"
            )

            await query.edit_message_text(
                f"Для участі у розіграші ви маєте бути підписані на канал @{RAFFLE_CHANNEL_USERNAME}.",
                reply_markup=get_back_to_raffle_menu_keyboard(),
            )

            return RAFFLE_MENU

        else:

            logger.info(
                f"Користувач {user_id} підписаний на канал @{RAFFLE_CHANNEL_USERNAME}. Статус: {chat_member.status}"
            )

    except Exception as e:

        logger.error(
            f"Помилка перевірки підписки на канал для {user_id} (@{RAFFLE_CHANNEL_USERNAME}): {e}"
        )

        await query.edit_message_text(
            "На жаль, не вдалося перевірити вашу підписку на канал. "
            "Переконайтеся, що бот доданий як адміністратор до каналу і має право "
            "'Переглядати учасників'. Спробуйте пізніше або зверніться до адміністратора.",
            reply_markup=get_back_to_raffle_menu_keyboard(),
        )

        return RAFFLE_MENU

    referred_count = get_referred_count(user_id)

    if referred_count < 1:

        text = (
            f"Для участі у розіграші вам необхідно запросити 1 друга. \n"
            f"Ваше реферальне посилання: `https://t.me/{context.bot.username}?start={user_id}`\n"
            f"Запрошених друзів: *{referred_count}*\n\n"
            f"Ви можете поділитися цим посиланням з другом, який ще не користується ботом. "
            f"Коли він перейде за посиланням і натисне /start, ваш лічильник збільшиться."
        )

        await query.edit_message_text(
            text,
            reply_markup=get_back_to_raffle_menu_keyboard(),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

        return RAFFLE_MENU

    text = (
        f"Ви виконали всі умови для участі в розіграші *{RAFFLE_PRIZE.upper()}*!\n\n"
        f"Підтверджуєте свою участь?"
    )

    reply_markup = get_raffle_join_confirmation_keyboard()

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    return RAFFLE_JOIN_CONFIRMATION


async def raffle_confirm_join_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    user_id = query.from_user.id

    if get_raffle_participant_status(user_id):

        await query.answer("Ви вже берете участь у розіграші!", show_alert=True)

        return RAFFLE_MENU

    if set_raffle_participant_status(user_id, True):

        await query.edit_message_text(
            f"🎉 Вітаємо! Ви успішно приєдналися до розіграшу *{RAFFLE_PRIZE.upper()}*!\n"
            f"Переможець буде оголошений *{RAFFLE_END_DATE.strftime('%d.%m.%Y о %H:%M')}*.",
            reply_markup=get_back_to_raffle_menu_keyboard(),
            parse_mode="Markdown",
        )

        logger.info(f"Користувач {user_id} приєднався до розіграшу.")

    else:

        await query.edit_message_text(
            "На жаль, сталася помилка під час реєстрації в розіграші. Спробуйте пізніше.",
            reply_markup=get_back_to_raffle_menu_keyboard(),
        )

    return RAFFLE_MENU


async def back_to_raffle_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query

    await query.answer()

    return await show_raffle_info_handler(update, context)


async def raffle_already_joined_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query

    await query.answer("Ви вже берете участь у розіграші!", show_alert=True)

    user_id = query.from_user.id

    current_time = datetime.now(KYIV_TZ)

    time_left: timedelta = RAFFLE_END_DATE - current_time

    days = time_left.days

    hours, remainder = divmod(time_left.seconds, 3600)

    minutes, seconds = divmod(remainder, 60)

    time_left_str = ""

    if days > 0:

        time_left_str += f"{days} дн. "

    if hours > 0:

        time_left_str += f"{hours} год. "

    time_left_str += f"{minutes} хв."

    referred_count = get_referred_count(user_id)

    text = (
        f"🎁 *Розіграш {RAFFLE_PRIZE.upper()}* 🎁\n\n"
        f"Умови:\n"
        f"1. Бути підписаним на Telegram канал: @{RAFFLE_CHANNEL_USERNAME}\n"
        f"2. Запросити 1 друга в цей бот за вашим реферальним посиланням.\n\n"
        f"Ваше реферальне посилання: `https://t.me/{context.bot.username}?start={user_id}`\n"
        f"Запрошених друзів: *{referred_count}*\n\n"
        f"Закінчення розіграшу: *{RAFFLE_END_DATE.strftime('%d.%m.%Y о %H:%M')}* (за Київським часом)\n"
        f"Залишилось: *{time_left_str}*\n\n"
        f"✅ Ви вже берете участь у розіграші!"
    )

    reply_markup = get_raffle_menu_keyboard(user_id)

    await query.edit_message_text(
        text, reply_markup=reply_markup, parse_mode="Markdown", disable_web_page_preview=True
    )


async def admin_pick_raffle_winner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    user_id_effective = update.effective_user.id

    if user_id_effective not in ADMIN_USER_IDS:

        if update.callback_query:
            await update.callback_query.answer("Доступ заборонено.", show_alert=True)

        elif update.message:
            await update.message.reply_text("Доступ заборонено.")

        return

    message_target = update.callback_query.message if update.callback_query else update.message

    current_time = datetime.now(KYIV_TZ)

    if current_time < RAFFLE_END_DATE:

        await message_target.reply_text(
            f"❌ Розіграш ще не закінчився! Залишилось: "
            f"{(RAFFLE_END_DATE - current_time).days} дн. "
            f"{(RAFFLE_END_DATE - current_time).seconds // 3600} год. "
            f"{((RAFFLE_END_DATE - current_time).seconds % 3600) // 60} хв.\n"
            "Переможця можна обрати лише після його завершення.",
            reply_markup=get_admin_panel_keyboard(),
        )

        return

    await message_target.reply_text("Шукаю учасників розіграшу, які відповідають умовам...")

    eligible_participants = []

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            conn.row_factory = sqlite3.Row

            cursor = conn.cursor()

            cursor.execute(
                "SELECT user_id, username, first_name, referred_count FROM users WHERE is_raffle_participant = TRUE AND referred_count >= 1"
            )

            for row in cursor.fetchall():

                user_id = row["user_id"]

                try:

                    chat_member = await context.bot.get_chat_member(
                        chat_id=f"@{RAFFLE_CHANNEL_USERNAME}", user_id=user_id
                    )

                    if chat_member.status in ["member", "administrator", "creator"]:

                        eligible_participants.append(dict(row))

                    else:

                        logger.info(
                            f"Користувач {user_id} не відповідає умовам (не підписаний на канал) при виборі переможця."
                        )

                except Exception as e:

                    logger.warning(
                        f"Не вдалося перевірити підписку для {user_id} при виборі переможця: {e}"
                    )

    except sqlite3.Error as e:

        logger.error(f"Помилка БД при отриманні учасників розіграшу: {e}")

        await message_target.reply_text(
            "❌ Помилка під час отримання списку учасників розіграшу з бази даних.",
            reply_markup=get_admin_panel_keyboard(),
        )

        return

    if not eligible_participants:

        await message_target.reply_text(
            "😔 Немає учасників, які відповідають всім умовам розіграшу (підписка на канал та запрошення друга).",
            reply_markup=get_admin_panel_keyboard(),
        )

        return

    winner = random.choice(eligible_participants)

    winner_username = f"@{winner['username']}" if winner["username"] else "не вказано"

    winner_full_name = winner["first_name"] if winner["first_name"] else "Користувач"

    winner_message_for_admin = (
        f"🎉 *Переможець розіграшу '{RAFFLE_PRIZE.upper()}' обраний!* 🎉\n\n"
        f"Користувач: {winner_full_name} (`{winner['user_id']}`)\n"
        f"Username: {winner_username}\n"
        f"Запрошених друзів: {winner['referred_count']}\n\n"
        f"Будь ласка, зв'яжіться з переможцем для передачі призу."
    )

    await message_target.reply_text(
        winner_message_for_admin, reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown"
    )

    logger.info(f"Обрано переможця розіграшу: {winner['user_id']} ({winner_full_name}).")

    try:

        await context.bot.send_message(
            chat_id=winner["user_id"],
            text=(
                f"🎉 Вітаємо! Ви стали переможцем у розіграші *{RAFFLE_PRIZE.upper()}*!\n\n"
                "З вами зв'яжеться адміністратор для уточнення деталей отримання призу."
            ),
            parse_mode="Markdown",
        )

        logger.info(f"Переможцю {winner['user_id']} надіслано повідомлення про виграш.")

    except Exception as e:

        logger.error(f"Не вдалося повідомити переможця {winner['user_id']} про виграш: {e}")

        await message_target.reply_text(
            f"⚠️ Не вдалося надіслати повідомлення переможцю {winner_full_name} ({winner['user_id']}). "
            "Можливо, він заблокував бота або має приватні налаштування.",
            parse_mode="Markdown",
        )


async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query

    data = query.data

    user_id = query.from_user.id

    answered_key = f"answered_query_{query.id}"

    if not context.user_data.get(answered_key):

        try:

            await query.answer()

            context.user_data[answered_key] = True

        except Exception as e:

            logger.debug(
                f"Не вдалося відповісти на callback '{data}' (можливо, вже відповіли): {e}"
            )

    admin_callbacks = [
        "show_admin_panel",
        "admin_maintenance_menu",
        "admin_announce_start",
        "admin_server_status",
        "view_dlq_callback",
        "admin_show_stats",
        "admin_clear_schedule_cache",
        "admin_upload_db_to_ftp",
        "admin_download_local_db",
        "admin_reload_schedule_json",
        "admin_pick_raffle_winner",
        "admin_clear_dlq",
    ]

    is_admin_action_button = data in admin_callbacks and user_id in ADMIN_USER_IDS

    is_maint_disable_action = data == "maint_disable_now" and user_id in ADMIN_USER_IDS

    if not (is_admin_action_button or is_maint_disable_action):

        if await check_maintenance_and_reply(update, context):

            if context.user_data.get(answered_key):
                del context.user_data[answered_key]

            return

    update_command_stats(f"button_{data}")

    logger.info(
        f"Кнопка: Користувач {user_id} натиснув: {data} (Is admin: {user_id in ADMIN_USER_IDS})"
    )

    if data == "show_schedule_menu":
        await schedule_menu_handler(update, context)

    elif data == "get_schedule_today":
        await show_schedule_for_day_handler(update, context, "get_schedule_today")

    elif data == "get_schedule_tomorrow":
        await show_schedule_for_day_handler(update, context, "get_schedule_tomorrow")

    elif data == "get_call_schedule":
        await call_schedule_handler(update, context)

    elif data == "get_full_schedule_all":
        await full_schedule_handler(update, context)

    elif data == "show_day_schedule_menu":
        await day_schedule_menu_handler(update, context)

    elif data.startswith("get_schedule_day_"):

        await show_schedule_for_day_handler(update, context, data)

    elif data == "select_specific_day_and_type":

        await select_specific_day_menu_handler(update, context)

    elif data.startswith("chose_day_"):

        await select_week_type_for_day_menu_handler(update, context)

    elif data.startswith("show_day_explicit_"):

        await show_schedule_for_day_handler(update, context, data)

    elif data == "show_donation_info":
        await donation_info_handler(update, context)

    elif data == "send_feedback_prompt":
        await send_feedback_prompt_handler(update, context)  # НОВИЙ РЯДОК

    elif data == "suggest_improvement_prompt":
        await send_suggestion_prompt_handler(update, context)

    elif data == "report_bug_button_prompt":
        await send_report_prompt_handler(update, context)

    elif data == "back_to_main_menu":
        await back_to_main_menu_universal_handler(update, context)

    elif data == "about_college":
        await about_college_handler(update, context)

    elif data == "about_college_specialties":
        await show_specialties_list_handler(update, context)

    elif data.startswith("specialties_page_"):
        await show_specialties_list_handler(update, context)

    elif data.startswith("show_specialty_details_"):
        await show_specialty_details_handler(update, context)

    elif data == "about_college_why_us":
        await about_college_why_us_handler(update, context)

    elif data == "about_college_contacts":
        await about_college_contacts_handler(update, context)

    elif data == "about_college_social_media":
        await about_college_social_media_handler(update, context)

    elif data == "about_college_admission_docs":
        await about_college_admission_docs_handler(update, context)

    elif data.startswith("show_admission_docs_"):
        await show_admission_docs_handler(update, context)

    elif data == "back_to_about_college_menu":
        await about_college_handler(update, context)

    elif data == "show_textbooks_menu":
        await show_textbooks_menu_handler(update, context)

    elif data == "show_raffle_info":
        await show_raffle_info_handler(update, context)

    elif data == "raffle_join_prompt":
        await raffle_join_prompt_handler(update, context)

    elif data == "raffle_confirm_join":
        await raffle_confirm_join_handler(update, context)

    elif data == "raffle_already_joined":
        await raffle_already_joined_handler(update, context)

    elif data == "back_to_raffle_menu":
        await back_to_raffle_menu_handler(update, context)

    # ---- ДОДАЙТЕ ЦІ РЯДКИ ДЛЯ КНОПОК ВИКЛАДАЧА ----

    elif data == "teacher_my_schedule":
        await teacher_my_schedule_handler(update, context)

    # elif data == 'teacher_curated_group': await teacher_curated_group_handler(update, context)  # Функція не реалізована

    elif data == "teacher_curated_group_schedule":
        await teacher_curated_group_schedule_handler(update, context)

    elif data == "teacher_any_group_schedule":
        await teacher_any_group_schedule_handler(update, context)

    elif data.startswith("teacher_select_course_"):
        await teacher_select_course_for_any_group_handler(update, context)

    elif data.startswith("teacher_view_group_"):
        await teacher_view_group_schedule_handler(update, context)

    # ---- ОБРОБНИКИ ДЛЯ РОЗКЛАДУ ВИКЛАДАЧА ----

    elif data.startswith("t_today_"):
        await show_teacher_today_schedule_handler(update, context)

    elif data.startswith("t_full_"):
        await show_teacher_full_schedule_handler(update, context)

    elif data.startswith("t_day_schedule_"):
        await show_teacher_day_schedule_handler(update, context)

    elif data.startswith("t_day_"):
        await show_teacher_specific_day_schedule_handler(update, context)

    elif data.startswith("t_week_"):
        await show_teacher_final_day_schedule_handler(update, context)

    elif data.startswith("t_menu_"):
        await teacher_my_schedule_handler(update, context)

    elif data == "show_admin_panel" and user_id in ADMIN_USER_IDS:
        await admin_panel_handler(update, context)

    elif data == "view_dlq_callback" and user_id in ADMIN_USER_IDS:
        await view_dlq_handler(update, context)

    elif data == "admin_clear_dlq" and user_id in ADMIN_USER_IDS:
        await admin_clear_dlq_handler(update, context)

    elif data == "admin_show_stats" and user_id in ADMIN_USER_IDS:
        await show_stats_handler(update, context)

    elif data == "admin_clear_schedule_cache" and user_id in ADMIN_USER_IDS:
        await admin_clear_cache_handler(update, context)

    elif data == "admin_reload_schedule_json" and user_id in ADMIN_USER_IDS:
        await admin_reload_schedule_from_json_handler(update, context)

    elif data == "admin_server_status" and user_id in ADMIN_USER_IDS:
        await server_status_handler(update, context)

    elif data == "admin_maintenance_menu" and user_id in ADMIN_USER_IDS:
        await maintenance_menu_page(update, context)

    elif data == "maint_disable_now" and user_id in ADMIN_USER_IDS:
        await maintenance_disable_now_callback(update, context)

    elif data == "admin_upload_db_to_ftp" and user_id in ADMIN_USER_IDS:
        await admin_upload_db_to_ftp_handler(update, context)

    elif data == "admin_download_local_db" and user_id in ADMIN_USER_IDS:
        await admin_download_local_db_handler(update, context)

    elif data == "admin_pick_raffle_winner" and user_id in ADMIN_USER_IDS:
        await admin_pick_raffle_winner(update, context)

    elif data == "about_college_from_guest":
        await handle_guest_info_about_college(update, context)

    elif data == "back_to_role_selection":
        await back_to_role_selection_handler(update, context)

    # ДОДАЄМО ОБРОБКУ КНОПОК ВИБОРУ РОЛІ

    elif data == "select_role_student":

        logger.info(f"button_callback_handler: Processing select_role_student for user {user_id}")

        await select_role_callback_handler(update, context)

    elif data == "select_role_guest":

        logger.info(f"button_callback_handler: Processing select_role_guest for user {user_id}")

        await select_role_callback_handler(update, context)

    elif data == "select_role_staff":

        logger.info(f"button_callback_handler: Processing select_role_staff for user {user_id}")

        await select_role_callback_handler(update, context)

    # ДОДАЄМО ОБРОБКУ КНОПОК ВИБОРУ КУРСУ

    elif data.startswith("select_course_"):

        logger.info(f"button_callback_handler: Processing {data} for user {user_id}")

        await select_student_course_handler(update, context)

    # ДОДАЄМО ОБРОБКУ КНОПОК ВИБОРУ ГРУПИ

    elif data.startswith("set_group_"):

        logger.info(f"button_callback_handler: Processing {data} for user {user_id}")

        await set_group_callback_handler(update, context)

    # ДОДАЄМО ОБРОБКУ КНОПОК "НАЗАД"

    elif data == "back_to_course_selection":

        logger.info(
            f"button_callback_handler: Processing back_to_course_selection for user {user_id}"
        )

        await back_to_course_handler(update, context)

    if context.user_data.get(answered_key):

        del context.user_data[answered_key]


# --- CONVERSATION HANDLER ДЛЯ КЕРУВАННЯ ВИКЛАДАЧАМИ ---


def get_manage_teachers_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Додати викладача", callback_data="teacher_admin_add")],
            [
                InlineKeyboardButton(
                    "🔑 Згенерувати пароль (для одного)", callback_data="teacher_admin_gen_otp"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔑 Згенерувати коди для всіх (24 год)",
                    callback_data="teacher_admin_gen_otp_all",
                )
            ],  # <--- НОВА КНОПКА
            [InlineKeyboardButton("📋 Список викладачів", callback_data="teacher_admin_view_list")],
            [InlineKeyboardButton("✏️ Редагувати/Видалити", callback_data="teacher_admin_edit")],
            [InlineKeyboardButton("⬅️ Назад до адмін-панелі", callback_data="show_admin_panel")],
        ]
    )


async def admin_manage_teachers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query
    await query.answer()

    text = "👨‍🏫 *Керування викладачами*\n\nОберіть дію:"

    await query.edit_message_text(
        text, reply_markup=get_manage_teachers_keyboard(), parse_mode="Markdown"
    )

    return ADMIN_TEACHER_MENU


async def admin_teacher_add_prompt_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Введіть повне ім'я викладача (ПІБ):", reply_markup=get_back_to_admin_panel_keyboard()
    )

    return ADMIN_TEACHER_ADD_NAME


async def admin_teacher_add_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    context.user_data["teacher_add_name"] = update.message.text.strip()

    text = f"Ім'я: {context.user_data['teacher_add_name']}.\nТепер введіть назву групи, яку він курує (або `-`, якщо не куратор)."

    await update.message.reply_text(text)

    return ADMIN_TEACHER_ADD_GROUP


async def admin_teacher_add_receive_group(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    curated_group = update.message.text.strip()

    full_name = context.user_data["teacher_add_name"]

    text = (
        f"✅ Успіх! Викладача *{full_name}* було додано/оновлено."
        if add_or_update_teacher_in_db(full_name, curated_group if curated_group != "-" else None)
        else "❌ Помилка. Можливо, викладач з таким ПІБ вже існує."
    )

    await update.message.reply_text(
        text, reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown"
    )

    context.user_data.clear()

    return ConversationHandler.END


async def admin_teacher_gen_otp_select_teacher(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query
    await query.answer()

    keyboard_buttons = []

    with sqlite3.connect(DATABASE_NAME) as conn:

        all_teachers = (
            conn.cursor()
            .execute("SELECT teacher_id, full_name FROM teachers ORDER BY full_name")
            .fetchall()
        )

    if not all_teachers:

        await query.answer("Немає викладачів у базі.", show_alert=True)

        return ADMIN_TEACHER_MENU

    for teacher_id, full_name in all_teachers:

        keyboard_buttons.append(
            [InlineKeyboardButton(full_name, callback_data=f"otp_for_{teacher_id}")]
        )

    keyboard_buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_teacher_menu")])

    await query.edit_message_text(
        "Оберіть викладача для генерації пароля:",
        reply_markup=InlineKeyboardMarkup(keyboard_buttons),
    )

    return ADMIN_TEACHER_SELECT_FOR_OTP


async def admin_teacher_select_otp_duration(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query

    context.user_data["otp_teacher_id"] = int(query.data.split("_")[-1])

    keyboard = [
        [
            InlineKeyboardButton("15 хвилин", callback_data="otp_dur_15"),
            InlineKeyboardButton("1 година", callback_data="otp_dur_60"),
        ],
        [InlineKeyboardButton("24 години", callback_data="otp_dur_1440")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="teacher_admin_gen_otp")],
    ]

    await query.edit_message_text(
        "Оберіть термін дії пароля:", reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return ADMIN_TEACHER_SET_OTP_DURATION


async def admin_teacher_generate_and_show_otp(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query

    duration_minutes = int(query.data.split("_")[-1])

    teacher_id = context.user_data["otp_teacher_id"]

    with sqlite3.connect(DATABASE_NAME) as conn:

        teacher_name = (
            conn.cursor()
            .execute("SELECT full_name FROM teachers WHERE teacher_id = ?", (teacher_id,))
            .fetchone()[0]
        )

    otp = set_teacher_otp_by_id(teacher_id, duration_minutes)

    if otp:

        text = f"✅ Пароль для *{teacher_name}*:\n\n`{otp}`\n\n⚠️ *ВАЖЛИВО:* Передайте цей пароль викладачу. Він дійсний *{duration_minutes} хвилин*."

    else:
        text = "❌ Не вдалося згенерувати пароль."

    await query.edit_message_text(
        text, reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown"
    )

    context.user_data.clear()

    return ConversationHandler.END


async def admin_teacher_view_list_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query
    await query.answer()

    text = "📋 *Список зареєстрованих викладачів:*\n\n"

    with sqlite3.connect(DATABASE_NAME) as conn:

        teachers = (
            conn.cursor()
            .execute(
                "SELECT full_name, user_id, curated_group_name FROM teachers ORDER BY full_name"
            )
            .fetchall()
        )

    if not teachers:
        text += "_Немає зареєстрованих викладачів._"

    else:

        for full_name, user_id, group in teachers:

            status = "🔴 (не активовано)" if user_id is None else "🟢 (активовано)"

            group_info = f", куратор: {group}" if group else ""

            text += f"• *{full_name}*{group_info} - `{status}`\n"

    await query.edit_message_text(
        text, reply_markup=get_manage_teachers_keyboard(), parse_mode="Markdown"
    )

    return ADMIN_TEACHER_MENU


async def admin_teacher_edit_select_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query
    await query.answer()

    keyboard_buttons = []

    with sqlite3.connect(DATABASE_NAME) as conn:

        teachers = (
            conn.cursor()
            .execute("SELECT teacher_id, full_name FROM teachers ORDER BY full_name")
            .fetchall()
        )

    if not teachers:

        await query.answer("Немає викладачів для редагування.", show_alert=True)

        return ADMIN_TEACHER_MENU

    for teacher_id, full_name in teachers:

        keyboard_buttons.append(
            [InlineKeyboardButton(full_name, callback_data=f"edit_teacher_{teacher_id}")]
        )

    keyboard_buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_teacher_menu")])

    await query.edit_message_text(
        "Оберіть викладача для редагування:", reply_markup=InlineKeyboardMarkup(keyboard_buttons)
    )

    return ADMIN_TEACHER_EDIT_SELECT


def get_teacher_edit_menu_keyboard(teacher_id: int) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Змінити ім'я", callback_data=f"edit_name_{teacher_id}")],
            [
                InlineKeyboardButton(
                    "🏷 Змінити кураторську групу", callback_data=f"edit_group_{teacher_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 Видалити викладача", callback_data=f"delete_teacher_{teacher_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔑 Згенерувати пароль", callback_data=f"edit_genotp_{teacher_id}"
                )
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="teacher_admin_edit")],
        ]
    )


async def admin_teacher_edit_menu_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query
    await query.answer()

    teacher_id = int(query.data.split("_")[-1])

    context.user_data["edit_teacher_id"] = teacher_id

    with sqlite3.connect(DATABASE_NAME) as conn:

        row = (
            conn.cursor()
            .execute(
                "SELECT full_name, curated_group_name, user_id FROM teachers WHERE teacher_id = ?",
                (teacher_id,),
            )
            .fetchone()
        )

    if not row:

        await query.answer("Викладача не знайдено.", show_alert=True)

        return ADMIN_TEACHER_MENU

    full_name, curated_group_name, user_id = row

    status = "🟢 активовано" if user_id is not None else "🔴 не активовано"

    text = f"👨‍🏫 *{full_name}*\nСтатус: {status}\nКураторська група: {curated_group_name or '-'}\n\nОберіть дію:"

    await query.edit_message_text(
        text, reply_markup=get_teacher_edit_menu_keyboard(teacher_id), parse_mode="Markdown"
    )

    return ADMIN_TEACHER_EDIT_MENU


async def admin_teacher_edit_name_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Введіть нове повне ім'я викладача:", reply_markup=get_back_to_admin_panel_keyboard()
    )

    return ADMIN_TEACHER_EDIT_NAME


async def admin_teacher_edit_name_receive(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    teacher_id = context.user_data.get("edit_teacher_id")

    new_name = update.message.text.strip()

    ok = update_teacher_name_in_db(teacher_id, new_name)

    text = "✅ Ім'я оновлено." if ok else "❌ Не вдалося оновити ім'я."

    await update.message.reply_text(text, reply_markup=get_manage_teachers_keyboard())

    context.user_data.pop("edit_teacher_id", None)

    return ADMIN_TEACHER_MENU


async def admin_teacher_edit_group_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Введіть назву кураторської групи (або '-' щоб прибрати):",
        reply_markup=get_back_to_admin_panel_keyboard(),
    )

    return ADMIN_TEACHER_EDIT_GROUP


async def admin_teacher_edit_group_receive(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    teacher_id = context.user_data.get("edit_teacher_id")

    group_text = update.message.text.strip()

    curated = None if group_text == "-" else group_text

    ok = update_teacher_curated_group_in_db(teacher_id, curated)

    text = "✅ Кураторську групу оновлено." if ok else "❌ Не вдалося оновити групу."

    await update.message.reply_text(text, reply_markup=get_manage_teachers_keyboard())

    context.user_data.pop("edit_teacher_id", None)

    return ADMIN_TEACHER_MENU


async def admin_teacher_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query
    await query.answer()

    teacher_id = int(query.data.split("_")[-1])

    context.user_data["edit_teacher_id"] = teacher_id

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Так, видалити", callback_data="delete_teacher_yes")],
            [InlineKeyboardButton("⬅️ Скасувати", callback_data=f"edit_teacher_{teacher_id}")],
        ]
    )

    await query.edit_message_text("Підтвердити видалення викладача?", reply_markup=keyboard)

    return ADMIN_TEACHER_DELETE_CONFIRM


async def admin_teacher_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    query = update.callback_query
    await query.answer()

    teacher_id = context.user_data.get("edit_teacher_id")

    ok = delete_teacher_in_db(teacher_id)

    text = "✅ Викладача видалено." if ok else "❌ Не вдалося видалити викладача."

    await query.edit_message_text(text, reply_markup=get_manage_teachers_keyboard())

    context.user_data.pop("edit_teacher_id", None)

    return ADMIN_TEACHER_MENU


async def admin_teacher_edit_genotp_duration(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:

    query = update.callback_query
    await query.answer()

    teacher_id = int(query.data.split("_")[-1])

    context.user_data["otp_teacher_id"] = teacher_id

    keyboard = [
        [
            InlineKeyboardButton("15 хвилин", callback_data="otp_dur_15"),
            InlineKeyboardButton("1 година", callback_data="otp_dur_60"),
        ],
        [InlineKeyboardButton("24 години", callback_data="otp_dur_1440")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"edit_teacher_{teacher_id}")],
    ]

    await query.edit_message_text(
        "Оберіть термін дії пароля:", reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return ADMIN_TEACHER_SET_OTP_DURATION


async def admin_teacher_gen_otp_for_all_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Генерує 24-годинні коди для ВСІХ неактивованих викладачів."""

    query = update.callback_query

    await query.answer()

    await query.edit_message_text(
        "⏳ Генерую коди для всіх неактивованих викладачів... Це може зайняти деякий час."
    )

    generated_codes = []

    try:

        with sqlite3.connect(DATABASE_NAME) as conn:

            # Вибираємо тільки тих викладачів, у яких немає прив'язаного user_id

            teachers_to_process = (
                conn.cursor()
                .execute(
                    "SELECT teacher_id, full_name FROM teachers WHERE user_id IS NULL ORDER BY full_name"
                )
                .fetchall()
            )

        if not teachers_to_process:

            await query.edit_message_text(
                "✅ Немає неактивованих викладачів для генерації кодів.",
                reply_markup=get_manage_teachers_keyboard(),
            )

            return ADMIN_TEACHER_MENU

        # Генеруємо коди для кожного

        for teacher_id, full_name in teachers_to_process:

            # 24 години = 1440 хвилин

            otp = set_teacher_otp_by_id(teacher_id, 1440)

            if otp:

                generated_codes.append((full_name, otp))

        # Форматуємо результат для адміна

        if generated_codes:

            response_text = "✅ Згенеровано 24-годинні коди для неактивованих викладачів:\n\n"

            for name, code in generated_codes:

                response_text += f"• *{name}*: `{code}`\n"

            response_text += "\n⚠️ *ВАЖЛИВО:* Передайте ці коди відповідним викладачам."

        else:

            response_text = "❌ Не вдалося згенерувати коди. Перевірте логи."

        await query.edit_message_text(
            response_text, reply_markup=get_manage_teachers_keyboard(), parse_mode="Markdown"
        )

    except Exception as e:

        logger.error(f"Помилка при масовій генерації кодів: {e}", exc_info=True)

        await query.edit_message_text(
            f"❌ Сталася помилка під час генерації: {e}",
            reply_markup=get_manage_teachers_keyboard(),
        )

    return ADMIN_TEACHER_MENU


manage_teachers_conv_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(admin_manage_teachers_handler, pattern="^admin_manage_teachers$")
    ],
    states={
        ADMIN_TEACHER_MENU: [
            CallbackQueryHandler(admin_teacher_add_prompt_name, pattern="^teacher_admin_add$"),
            CallbackQueryHandler(
                admin_teacher_gen_otp_select_teacher, pattern="^teacher_admin_gen_otp$"
            ),
            CallbackQueryHandler(
                admin_teacher_gen_otp_for_all_handler, pattern="^teacher_admin_gen_otp_all$"
            ),
            CallbackQueryHandler(
                admin_teacher_view_list_handler, pattern="^teacher_admin_view_list$"
            ),
            CallbackQueryHandler(admin_teacher_edit_select_handler, pattern="^teacher_admin_edit$"),
        ],
        ADMIN_TEACHER_ADD_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_teacher_add_receive_name)
        ],
        ADMIN_TEACHER_ADD_GROUP: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_teacher_add_receive_group)
        ],
        ADMIN_TEACHER_SELECT_FOR_OTP: [
            CallbackQueryHandler(admin_teacher_select_otp_duration, pattern=r"^otp_for_\d+$"),
            CallbackQueryHandler(admin_manage_teachers_handler, pattern="^back_to_teacher_menu$"),
        ],
        ADMIN_TEACHER_SET_OTP_DURATION: [
            CallbackQueryHandler(admin_teacher_generate_and_show_otp, pattern=r"^otp_dur_\d+$"),
            CallbackQueryHandler(
                admin_teacher_gen_otp_select_teacher, pattern="^teacher_admin_gen_otp$"
            ),
            CallbackQueryHandler(admin_teacher_edit_menu_handler, pattern=r"^edit_teacher_\d+$"),
        ],
        ADMIN_TEACHER_EDIT_SELECT: [
            CallbackQueryHandler(admin_teacher_edit_menu_handler, pattern=r"^edit_teacher_\d+$"),
            CallbackQueryHandler(admin_manage_teachers_handler, pattern="^back_to_teacher_menu$"),
        ],
        ADMIN_TEACHER_EDIT_MENU: [
            CallbackQueryHandler(admin_teacher_edit_name_prompt, pattern=r"^edit_name_\d+$"),
            CallbackQueryHandler(admin_teacher_edit_group_prompt, pattern=r"^edit_group_\d+$"),
            CallbackQueryHandler(admin_teacher_delete_confirm, pattern=r"^delete_teacher_\d+$"),
            CallbackQueryHandler(admin_teacher_edit_genotp_duration, pattern=r"^edit_genotp_\d+$"),
            CallbackQueryHandler(admin_teacher_edit_select_handler, pattern="^teacher_admin_edit$"),
        ],
        ADMIN_TEACHER_EDIT_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_teacher_edit_name_receive)
        ],
        ADMIN_TEACHER_EDIT_GROUP: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_teacher_edit_group_receive)
        ],
        ADMIN_TEACHER_DELETE_CONFIRM: [
            CallbackQueryHandler(admin_teacher_delete_execute, pattern="^delete_teacher_yes$"),
            CallbackQueryHandler(admin_teacher_edit_menu_handler, pattern=r"^edit_teacher_\d+$"),
        ],
    },
    fallbacks=[
        CallbackQueryHandler(admin_panel_handler, pattern="^show_admin_panel$"),
        CommandHandler("cancel", admin_panel_handler),
    ],
    per_user=True,
    allow_reentry=True,
)


# НОВИЙ ОБРОБНИК СПЕЦІАЛЬНО ДЛЯ ВХОДУ ВИКЛАДАЧА

teacher_login_conv_handler = ConversationHandler(
    entry_points=[
        # Цей обробник активується ТІЛЬКИ коли користувач натискає кнопку "Я викладач"
        # Використовуємо дуже специфічний паттерн, щоб не перехоплювати інші кнопки
        CallbackQueryHandler(select_teacher_role_callback_handler, pattern="^select_role_teacher$")
    ],
    states={
        TYPING_ONE_TIME_PASSWORD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_teacher_otp_entry)
        ],
    },
    fallbacks=[
        # Дозволяє повернутися до вибору ролі
        CallbackQueryHandler(back_to_role_selection_handler, pattern="^back_to_role_selection$"),
        CommandHandler("cancel", start_command_handler),
    ],
    per_user=True,
    allow_reentry=True,
)


def main() -> None:

    initialize_database()

    initialize_schedule_database()

    logger.info("Завантаження початкового кешу розкладу...")

    initial_cache = get_cached_schedule()

    if initial_cache and initial_cache.get("розклади_груп"):

        logger.info(
            f"Початковий кеш розкладу завантажено, знайдено {len(initial_cache['розклади_груп'])} груп."
        )

    else:

        logger.warning("Початковий кеш розкладу порожній або не вдалося завантажити дані груп.")

    application = Application.builder().token(BOT_TOKEN).build()

    # --- ВИЗНАЧЕННЯ CONVERSATIONHANDLER'ІВ ---

    # Переконайтесь, що всі ці блоки йдуть ПЕРЕД application.add_handler(...)

    # role_selection_conv_handler (цей ти додав першим, це правильно)

    # ЗАМІНІТЬ ВАШ role_selection_conv_handler НА ЦЕЙ

    # ЗАМІНІТЬ ВАШ role_selection_conv_handler НА ЦЕЙ

    role_selection_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_command_handler)],
        states={
            SELECTING_ROLE: [
                # Обробляємо кнопки вибору ролі (студент, гість, працівник)
                CallbackQueryHandler(
                    select_role_callback_handler, pattern="^select_role_(student|guest|staff)$"
                ),
                CallbackQueryHandler(
                    back_to_role_selection_handler, pattern="^back_to_role_selection$"
                ),
            ],
            SELECTING_COURSE: [
                CallbackQueryHandler(select_student_course_handler, pattern="^select_course_"),
                CallbackQueryHandler(
                    back_to_role_selection_handler, pattern="^back_to_role_selection$"
                ),
            ],
            SELECTING_GROUP: [
                CallbackQueryHandler(set_group_callback_handler, pattern="^set_group_"),
                CallbackQueryHandler(back_to_course_handler, pattern="^back_to_course_selection$"),
                CallbackQueryHandler(show_main_menu_handler, pattern="^back_to_main_menu$"),
            ],
            # БЛОК TYPING_ONE_TIME_PASSWORD ПОВНІСТЮ ВИДАЛЕНО ЗВІДСИ
            GUEST_MENU: [
                CallbackQueryHandler(
                    handle_guest_info_about_college, pattern="^about_college_from_guest$"
                ),
                CallbackQueryHandler(
                    show_specialties_list_handler, pattern="^about_college_specialties$"
                ),
                CallbackQueryHandler(
                    show_specialty_details_handler, pattern="^show_specialty_details_"
                ),
                CallbackQueryHandler(
                    about_college_why_us_handler, pattern="^about_college_why_us$"
                ),
                CallbackQueryHandler(
                    about_college_contacts_handler, pattern="^about_college_contacts$"
                ),
                CallbackQueryHandler(
                    about_college_social_media_handler, pattern="^about_college_social_media$"
                ),
                CallbackQueryHandler(
                    back_to_role_selection_handler, pattern="^back_to_role_selection$"
                ),
                CallbackQueryHandler(about_college_handler, pattern="^back_to_about_college_menu$"),
            ],
            STAFF_MENU: [
                CallbackQueryHandler(handle_staff_entry, pattern="^select_role_staff$"),
                CallbackQueryHandler(
                    back_to_role_selection_handler, pattern="^back_to_role_selection$"
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", start_command_handler),
            CallbackQueryHandler(
                back_to_role_selection_handler, pattern="^back_to_role_selection$"
            ),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # НОВИЙ ОБРОБНИК ДЛЯ ЗМІНИ ГРУПИ З ГОЛОВНОГО МЕНЮ

    # НОВИЙ ОБРОБНИК ДЛЯ ЗМІНИ ГРУПИ З ГОЛОВНОГО МЕНЮ

    change_group_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(prompt_set_group_handler, pattern="^change_set_group_prompt$")
        ],
        states={
            SELECTING_GROUP: [
                CallbackQueryHandler(set_group_callback_handler, pattern="^set_group_"),
                CallbackQueryHandler(
                    back_to_role_selection_handler, pattern="^back_to_role_selection$"
                ),
                # Цей рядок можна залишити, він потрібен для кнопки "Скасувати"
                CallbackQueryHandler(
                    back_to_main_menu_universal_handler, pattern="^back_to_main_menu$"
                ),
            ],
        },
        fallbacks=[
            # Замінюємо на універсальний обробник для узгодженості
            CallbackQueryHandler(
                back_to_main_menu_universal_handler, pattern="^back_to_main_menu$"
            ),
            CommandHandler("cancel", back_to_main_menu_universal_handler),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # maintenance_conv_handler (МАЄ БУТИ ВИЗНАЧЕНИЙ ТУТ)

    maintenance_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(maintenance_start_setup_handler, pattern="^maint_start_setup$")
        ],
        states={
            SELECTING_DURATION: [
                CallbackQueryHandler(
                    maintenance_set_duration_callback, pattern="^maint_set_duration_"
                ),
                CallbackQueryHandler(
                    maintenance_manual_duration_prompt_callback,
                    pattern="^maint_manual_duration_prompt$",
                ),
            ],
            TYPING_DURATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, maintenance_typed_duration_handler)
            ],
            TYPING_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, maintenance_typed_message_handler)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(maintenance_cancel_setup_callback, pattern="^maint_cancel_setup$"),
            CommandHandler("cancel_maintenance", maintenance_cancel_setup_callback),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # announce_conv_handler (МАЄ БУТИ ВИЗНАЧЕНИЙ ТУТ)

    announce_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_announce_start_handler, pattern="^admin_announce_start$")
        ],
        states={
            ANNOUNCE_SELECT_TARGET: [
                CallbackQueryHandler(announce_select_target_callback, pattern="^announce_target_"),
            ],
            ANNOUNCE_SELECT_GROUP_FOR_ANNOUNCE: [
                CallbackQueryHandler(
                    announce_select_group_for_announce_callback,
                    pattern="^announce_select_group_for_type_",
                )
            ],
            ANNOUNCE_CHOOSING_MEDIA_TYPE: [
                CallbackQueryHandler(
                    announce_choose_media_type_callback, pattern="^announce_type_"
                ),
            ],
            ANNOUNCE_WAITING_FOR_PHOTOS: [
                MessageHandler(filters.PHOTO, announce_waiting_for_photos_handler),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, announce_waiting_for_photos_handler
                ),
                CallbackQueryHandler(
                    announce_cancel_media_callback, pattern="^announce_cancel_media$"
                ),
            ],
            ANNOUNCE_TYPING_CAPTION_FOR_MEDIA: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, announce_typing_caption_for_media_handler
                ),
                CallbackQueryHandler(
                    announce_cancel_media_callback, pattern="^announce_cancel_media$"
                ),
            ],
            ANNOUNCE_TYPING_MESSAGE_FOR_ANNOUNCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, announce_typed_message_handler)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(announce_cancel_handler, pattern="^announce_cancel$"),
            CallbackQueryHandler(announce_cancel_media_callback, pattern="^announce_cancel_media$"),
            CommandHandler("cancel_announce", announce_cancel_handler),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # raffle_conv_handler (МАЄ БУТИ ВИЗНАЧЕНИЙ ТУТ)

    raffle_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(show_raffle_info_handler, pattern="^show_raffle_info$")],
        states={
            RAFFLE_MENU: [
                CallbackQueryHandler(raffle_join_prompt_handler, pattern="^raffle_join_prompt$"),
                CallbackQueryHandler(
                    raffle_already_joined_handler, pattern="^raffle_already_joined$"
                ),
                CallbackQueryHandler(back_to_raffle_menu_handler, pattern="^back_to_raffle_menu$"),
            ],
            RAFFLE_JOIN_CONFIRMATION: [
                CallbackQueryHandler(raffle_confirm_join_handler, pattern="^raffle_confirm_join$"),
                CallbackQueryHandler(back_to_raffle_menu_handler, pattern="^back_to_raffle_menu$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(show_main_menu_handler, pattern="^back_to_main_menu$")],
        per_user=True,
        allow_reentry=True,
    )

    # report_conv_handler (МАЄ БУТИ ВИЗНАЧЕНИЙ ТУТ)

    report_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(send_report_prompt_handler, pattern="^report_bug_button_prompt$"),
            CommandHandler("report_button", send_report_prompt_handler),
        ],
        states={
            TYPING_REPORT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_report_message_handler)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_report_flow_handler, pattern="^cancel_report_flow$"),
            CommandHandler("cancel", cancel_report_flow_handler),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # suggestion_conv_handler (МАЄ БУТИ ВИЗНАЧЕНИЙ ТУТ)

    suggestion_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                send_suggestion_prompt_handler, pattern="^suggest_improvement_prompt$"
            ),
            CommandHandler("suggest", send_suggestion_prompt_handler),
        ],
        states={
            TYPING_SUGGESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_suggestion_message_handler)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(
                cancel_suggestion_flow_handler, pattern="^cancel_suggestion_flow$"
            ),
            CommandHandler("cancel", cancel_suggestion_flow_handler),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # feedback_conv_handler (НОВИЙ)

    feedback_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(send_feedback_prompt_handler, pattern="^send_feedback_prompt$"),
            CommandHandler(
                "feedback", send_feedback_prompt_handler
            ),  # Дозволяємо також команду /feedback
        ],
        states={
            TYPING_FEEDBACK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_feedback_message_handler)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_feedback_flow_handler, pattern="^cancel_feedback_flow$"),
            CommandHandler("cancel", cancel_feedback_flow_handler),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # --- ДОДАВАННЯ CONVERSATIONHANDLER'ІВ ДО APPLICATION ---

    if ENABLE_FTP_SYNC:

        application.job_queue.run_repeating(
            ftp_sync_db_job_callback,
            interval=timedelta(minutes=10),
            first=timedelta(seconds=10),
            name=FTP_SYNC_JOB_NAME,
        )

        logger.info(
            f"Заплановано FTP синхронізацію БД користувачів ('{DATABASE_NAME}') кожні 10 хвилин."
        )

    else:

        logger.info(f"FTP синхронізація БД користувачів ('{DATABASE_NAME}') вимкнена.")

    # ВАЖЛИВА ЗМІНА: Тепер ми знову будемо обробляти всі ролі в одному місці,

    # але логін викладача буде винесено в окрему розмову.

    # Обробники вже додані в конфігурацію ConversationHandler

    # ВАЖЛИВО: role_selection_conv_handler додається ПЕРШИМ, щоб він обробляв всі ролі та /start

    application.add_handler(role_selection_conv_handler)

    # teacher_login_conv_handler додається другим, але він має специфічний паттерн тільки для викладачів

    application.add_handler(teacher_login_conv_handler)

    application.add_handler(change_group_conv_handler)

    application.add_handler(maintenance_conv_handler)

    application.add_handler(announce_conv_handler)

    application.add_handler(raffle_conv_handler)

    application.add_handler(report_conv_handler)

    application.add_handler(suggestion_conv_handler)

    application.add_handler(feedback_conv_handler)  # НОВИЙ РЯДОК

    application.add_handler(manage_teachers_conv_handler)

    # Цей хендлер кнопок має бути після всіх ConversationHandlers, щоб вони мали пріоритет

    application.add_handler(CallbackQueryHandler(button_callback_handler))

    # --- ІНШІ КОМАНДНІ ХЕНДЛЕРИ ---

    # Ці хендлери залишаються, але вони будуть спрацьовувати, тільки якщо ConversationHandler не перехопив команду.

    # Наприклад, /start оброблятиме role_selection_conv_handler

    application.add_handler(CommandHandler(["schedule", "schedule_buttons"], schedule_menu_handler))

    application.add_handler(CommandHandler("call_schedule", call_schedule_handler))

    application.add_handler(CommandHandler("full_schedule", full_schedule_handler))

    application.add_handler(CommandHandler("donate", donation_info_handler))

    application.add_handler(CommandHandler("report", report_bug_command_handler))

    application.add_handler(
        CallbackQueryHandler(send_suggestion_prompt_handler, pattern="^suggest_improvement_prompt$")
    )  # Цей колбек, можливо, дублює вже доданий в suggestion_conv_handler entry_points

    application.add_handler(CommandHandler("feedback", send_feedback_prompt_handler))  # НОВИЙ РЯДОК

    admin_filter = filters.User(ADMIN_USER_IDS) if ADMIN_USER_IDS else filters.User(user_id=-1)

    application.add_handler(
        CommandHandler(["admin", "admin_panel"], admin_panel_handler, filters=admin_filter)
    )

    application.add_handler(
        CommandHandler("announce", announce_command_handler, filters=admin_filter)
    )

    application.add_handler(CommandHandler("view_dlq", view_dlq_handler, filters=admin_filter))

    application.add_handler(
        CommandHandler("clear_dlq", admin_clear_dlq_handler, filters=admin_filter)
    )

    application.add_handler(CommandHandler("stats", show_stats_handler, filters=admin_filter))

    application.add_handler(
        CommandHandler("server_status", server_status_handler, filters=admin_filter)
    )

    application.add_handler(
        CommandHandler(
            "force_disable_maintenance", maintenance_disable_now_callback, filters=admin_filter
        )
    )

    application.add_handler(
        CommandHandler("force_upload_db", admin_upload_db_to_ftp_handler, filters=admin_filter)
    )

    application.add_handler(
        CommandHandler("download_db", admin_download_local_db_handler, filters=admin_filter)
    )

    application.add_handler(
        CommandHandler(
            "reload_schedule", admin_reload_schedule_from_json_handler, filters=admin_filter
        )
    )

    application.add_handler(
        CommandHandler("pick_winner", admin_pick_raffle_winner, filters=admin_filter)
    )

    logger.info("Бот запущено. Натисни Ctrl+C для зупинки.")

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":

    main()
