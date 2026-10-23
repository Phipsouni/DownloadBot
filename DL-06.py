import telebot
from telebot import types
from yt_dlp import YoutubeDL
from cookie_scope import (
    cookie_file_for_url, load_scoped_cookie_file, site_for_url,
)
import os
import re
import threading
import time
import json
import shutil
import subprocess
import tempfile

# --- ИМПОРТ КОНФИГУРАЦИИ ---
import config

# --- ⚙️ КОНФИГУРАЦИЯ ⚙️ --
TOKEN = config.TOKEN
ADMIN_ID = config.ADMIN_ID
FFMPEG_PATH = config.FFMPEG_PATH
# --------------------------------------------------------

bot = telebot.TeleBot(TOKEN)

# --- ГЛОБАЛЬНЫЕ НАСТРОЙКИ ---
# У облачного Telegram Bot API предел отправки около 50 МБ.
# Небольшой запас исключает ошибку 413 на пограничных файлах.
MAX_FILE_SIZE_MB = 49
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1_000_000
MAX_DOWNLOAD_SIZE_MB = 75
MAX_DOWNLOAD_SIZE_BYTES = MAX_DOWNLOAD_SIZE_MB * 1_000_000
DOWNLOAD_FOLDER = 'downloads'
WHITELIST_FILE = 'whitelist.txt'
ALLOWED_USER_IDS = set()
# Режим скачивания для каждого пользователя: 'audio' | 'video' (отправлять ссылки подряд)
USER_DOWNLOAD_MODES = {}

if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)


def get_ydlp_cookie_sources(url):
    """Cookies разрешены только для выбранного видеосервиса."""
    sources = [("без cookies", {})]
    site = site_for_url(url)
    if not site:
        return sources

    cookies_file = cookie_file_for_url(
        url, getattr(config, 'YTDLP_SITE_COOKIE_FILES', {}))
    if cookies_file:
        sources.append((f"файл cookies: {cookies_file}",
                        {'_scoped_cookie_file': cookies_file, '_cookie_site': site}))

    return sources


def get_ydlp_js_runtime_options():
    node_path = getattr(config, 'YTDLP_NODE_PATH', None)
    if node_path and os.path.exists(node_path):
        return {'js_runtimes': {'node': {'path': node_path}}}

    return {}


def is_cookie_or_auth_error(error):
    error_text = str(error).lower()
    return any(text in error_text for text in (
        "cookies",
        "sign in to confirm",
        "http error 403",
        "http error 401",
        "login required",
        "authentication required",
    ))


def format_cookie_attempt_errors(errors):
    if not errors:
        return ""

    last_errors = "\n".join(f"- {error}" for error in errors[-3:])
    return f"\n\nПопытки cookies:\n{last_errors}"


def get_owner_error_hint(error, cookie_errors):
    error_text = str(error)
    hint = ""

    if "Sign in to confirm your age" in error_text:
        hint = (
            "\n\nДля владельца: YouTube требует авторизацию. "
            "Обновите файл site_cookies/youtube.txt."
        )

    return f"{hint}{format_cookie_attempt_errors(cookie_errors)}"


class CompressionError(Exception):
    pass


def run_media_command(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired as error:
        raise CompressionError("Превышено время обработки файла.") from error
    except OSError as error:
        raise CompressionError("Не удалось запустить ffmpeg или ffprobe.") from error
    if result.returncode:
        print(f"Ошибка ffmpeg/ffprobe: {result.stderr[-1000:]}")
        raise CompressionError("Не удалось обработать файл с помощью ffmpeg.")
    return result.stdout


def get_media_duration(filename):
    ffprobe_path = os.path.join(os.path.dirname(FFMPEG_PATH), 'ffprobe.exe')
    output = run_media_command([
        ffprobe_path, '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'json', filename,
    ])
    try:
        duration = float(json.loads(output)['format']['duration'])
    except (KeyError, ValueError, TypeError, OverflowError) as error:
        raise CompressionError("Не удалось определить длительность файла.") from error
    if duration <= 0:
        raise CompressionError("Не удалось определить длительность файла.")
    return duration


def compress_media(filename, audio_only, max_output_bytes=MAX_FILE_SIZE_BYTES):
    """Возвращает путь к сжатому файлу и временную папку для удаления после отправки."""
    duration = get_media_duration(filename)
    temporary_folder = tempfile.mkdtemp(prefix='dl06-compress-', dir=DOWNLOAD_FOLDER)
    extension = 'mp3' if audio_only else 'mp4'
    base_name = os.path.splitext(os.path.basename(filename))[0]
    output_filename = os.path.join(temporary_folder, f'{base_name}.compressed.{extension}')

    try:
        for attempt in range(2):
            # Оставляем запас для контейнера, метаданных и погрешности битрейта.
            target_bytes = max_output_bytes * (0.94 if attempt == 0 else 0.80)
            total_bitrate = int(target_bytes * 8 / duration * 0.96)

            if audio_only:
                audio_bitrate = min(192_000, total_bitrate)
                if audio_bitrate < 48_000:
                    raise CompressionError("Для этой длительности качество звука будет слишком низким.")
                run_media_command([
                    FFMPEG_PATH, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-i', filename, '-map', '0:a:0', '-vn', '-c:a', 'libmp3lame',
                    '-b:a', str(audio_bitrate), output_filename,
                ])
            else:
                audio_bitrate = 96_000
                video_bitrate = total_bitrate - audio_bitrate
                if video_bitrate < 250_000:
                    raise CompressionError("Для этой длительности качество видео будет слишком низким.")
                passlog = os.path.join(temporary_folder, 'ffmpeg-pass')
                video_options = [
                    '-c:v', 'libx264', '-preset', 'medium', '-b:v', str(video_bitrate),
                    '-pix_fmt', 'yuv420p', '-passlogfile', passlog,
                ]
                run_media_command([
                    FFMPEG_PATH, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-i', filename, '-map', '0:v:0', '-an', '-sn', '-dn',
                    *video_options, '-pass', '1', '-f', 'null', 'NUL',
                ])
                run_media_command([
                    FFMPEG_PATH, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-i', filename, '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn',
                    *video_options, '-pass', '2', '-c:a', 'aac', '-b:a', str(audio_bitrate),
                    '-movflags', '+faststart', output_filename,
                ])

            if os.path.getsize(output_filename) <= max_output_bytes:
                return output_filename, temporary_folder

        raise CompressionError("После сжатия файл всё ещё превышает лимит Telegram.")
    except Exception:
        shutil.rmtree(temporary_folder, ignore_errors=True)
        raise


# --- ФУНКЦИИ УПРАВЛЕНИЯ WHITELIST ---

def load_whitelist():
    try:
        with open(WHITELIST_FILE, 'r') as f:
            for line in f:
                if line.strip():
                    ALLOWED_USER_IDS.add(line.strip())
        ALLOWED_USER_IDS.add(str(ADMIN_ID))
    except FileNotFoundError:
        ALLOWED_USER_IDS.add(str(ADMIN_ID))
        save_whitelist()


def save_whitelist():
    with open(WHITELIST_FILE, 'w') as f:
        for user_id in ALLOWED_USER_IDS:
            f.write(f"{user_id}\n")


# --- ФУНКЦИЯ ПРОВЕРКИ ДОСТУПА И ДЕКОРАТОР ---

def is_user_admin(message):
    return str(message.from_user.id) == str(ADMIN_ID)


def is_user_allowed(message):
    return str(message.from_user.id) in ALLOWED_USER_IDS


def admin_only(func):
    def wrapper(message):
        if not is_user_admin(message):
            bot.reply_to(message, "🚫 Эта команда доступна только администратору.")
            return
        return func(message)

    return wrapper


# --- ХЕЛПЕРЫ ДЛЯ КЛАВИАТУР ---

def get_main_reply_keyboard(user_id):
    """Создает главную клавиатуру с выбором типа и админ-панелью."""
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, selective=True)

    # Кнопки для всех (в один ряд)
    btn_audio = types.KeyboardButton("Audio 🎵")
    btn_video = types.KeyboardButton("Video 🎥")
    markup.row(btn_audio, btn_video)

    # Кнопки для админа (каждая в своем ряду для ясности)
    if str(user_id) == str(ADMIN_ID):
        btn_add_user = types.KeyboardButton("Добавить пользователя")
        btn_show_users = types.KeyboardButton("Пользователи")
        markup.row(btn_add_user)
        markup.row(btn_show_users)

    return markup


def get_cancel_keyboard():
    """Создает клавиатуру с кнопкой 'Отмена'."""
    cancel_markup = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True, selective=True)
    cancel_btn = types.KeyboardButton("Отмена")
    cancel_markup.add(cancel_btn)
    return cancel_markup


def handle_cancel(message):
    """Обрабатывает отмену и возвращает главное меню."""
    if message.text == "Отмена":
        bot.send_message(message.chat.id, "Действие отменено. Возврат в главное меню.",
                         reply_markup=get_main_reply_keyboard(message.from_user.id))
        return True
    return False


# --- ЕДИНАЯ ФУНКЦИЯ ДЛЯ СКАЧИВАНИЯ КОНТЕНТА ---
def download_content(url, chat_id, download_type):
    final_filename_to_delete = None
    compression_folder_to_delete = None
    title = 'Неизвестное название'  # Инициализация для обработки ошибок

    is_audio_only = (download_type == 'audio')
    file_type = 'аудио' if is_audio_only else 'видео'

    # 1. ОТПРАВКА ПЕРВОГО СООБЩЕНИЯ (БУДЕТ РЕДАКТИРОВАТЬСЯ)
    try:
        status_message = bot.send_message(chat_id,
                                          f"⏳ Начинаю скачивание **{file_type}**... "
                                          f"(до {MAX_DOWNLOAD_SIZE_MB} МБ; при необходимости сожму до {MAX_FILE_SIZE_MB} МБ)",
                                          parse_mode='Markdown')
        status_message_id = status_message.message_id
    except Exception as e:
        print(f"Ошибка при отправке статусного сообщения: {e}")
        return

    # --- 2. PROGRESS HOOK ДЛЯ ОБНОВЛЕНИЯ СТАТУСА ---
    update_control = {'last_time': 0, 'status_sent': None}  # Добавлен статус

    def download_hook(d):
        nonlocal file_type  # Используем file_type из внешней функции

        current_time = time.time()

        # Обновляем сообщение не чаще, чем раз в 2 секунды
        if d['status'] == 'downloading':
            if current_time - update_control['last_time'] < 2:
                return

            if '_percent_str' in d and '_eta_str' in d:
                progress = d['_percent_str'].strip()
                speed = d.get('_speed_str', 'N/A').strip()
                eta = d['_eta_str'].strip()

                new_text = f"⬇️ Загрузка **{file_type}**: **{progress}**\nСкорость: {speed}, ETA: {eta}"

                try:
                    bot.edit_message_text(new_text, chat_id, status_message_id, parse_mode='Markdown')
                    update_control['last_time'] = current_time
                    update_control['status_sent'] = 'downloading'
                except Exception:
                    pass

        elif d['status'] == 'postprocessing':
            # Отправляем сообщение о пост-обработке только один раз, или если до этого был статус 'downloading'
            if update_control['status_sent'] != 'postprocessing':
                try:
                    bot.edit_message_text(f"⚙️ Обработка **{file_type}**... (Конвертация в MP3)",
                                          chat_id, status_message_id, parse_mode='Markdown')
                    update_control['status_sent'] = 'postprocessing'
                except Exception:
                    pass

        elif d['status'] == 'finished':
            # Обновляем, что загрузка завершена, перед тем как начнется postprocessing или отправка
            if update_control['status_sent'] != 'finished':
                try:
                    bot.edit_message_text(f"✅ Скачивание **{file_type}** завершено. Ожидание обработки...",
                                          chat_id, status_message_id, parse_mode='Markdown')
                    update_control['status_sent'] = 'finished'
                except Exception:
                    pass

    #
    # === НАСТРОЙКИ YD-DLP (с фильтром по высоте) ===
    #
    ydl_opts = {
        # 1. Сначала ищем mp4+m4a (для YouTube)
        # 2. Затем ищем ЛУЧШЕЕ видео (с высотой >= 720p) + ЛУЧШЕЕ аудио (ЭТО ИСПРАВИТ REELS)
        # 3. Затем резервный вариант с 480p
        # 4. Затем лучший одиночный файл
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720]+bestaudio/bestvideo[height>=480]+bestaudio/best',

        'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(id)s.%(ext)s'),
        'restrictfilenames': True,
        'noplaylist': True,
        'max_filesize': MAX_DOWNLOAD_SIZE_BYTES,
        'quiet': True,
        'no_warnings': True,
        'ffmpeg_location': FFMPEG_PATH,  # Используем явно указанный путь
        'retries': 5,
        'fragment_retries': 10,
        'progress_hooks': [download_hook],

        # Гарантируем, что на выходе будет MP4 при слиянии
        'merge_output_format': 'mp4',
    }
    ydl_opts.update(get_ydlp_js_runtime_options())

    if is_audio_only:
        # (ИЗМЕНЕНО: ВОЗВРАТ К MP3)
        # Логика для аудио (MP3)
        ydl_opts['format'] = 'bestaudio/best'

        # Возвращаем postprocessors для конвертации в MP3
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            # '0' - это наилучшее качество VBR (лучше, чем 320 CBR)
            'preferredquality': '0',
        },
            {'key': 'FFmpegMetadata'},
        ]
        ydl_opts['outtmpl'] = os.path.join(DOWNLOAD_FOLDER, '%(id)s.%(ext)s')

    #
    # === 🚀 ИСПРАВЛЕНИЕ: ПРИНУДИТЕЛЬНОЕ ПЕРЕКОДИРОВАНИЕ ДЛЯ МОБИЛЬНЫХ 🚀 ===
    #
    else:
        # Для видео: форсируем ПЕРЕКОДИРОВАНИЕ в H.264/AAC для
        # 100% совместимости с мобильными клиентами Telegram.

        ydl_opts['merge_output_format'] = 'mp4'

        ydl_opts['postprocessor_args'] = [
            # 1. Видеокодек: H.264 (вместо 'copy')
            '-c:v', 'libx264',

            # 2. Аудиокодек: AAC (вместо 'copy')
            '-c:a', 'aac',

            # 3. Пресет скорости: 'veryfast' - хороший баланс скорости и качества.
            '-preset', 'veryfast',

            # 4. Совместимый формат пикселей
            '-pix_fmt', 'yuv420p',

            # 5. Флаг для перемещения метаданных в начало файла (ОСТАВЛЯЕМ)
            '-movflags', '+faststart'
        ]
    #
    # === 🚀 КОНЕЦ ИСПРАВЛЕНИЯ 🚀 ===
    #

    cookie_attempt_errors = []

    try:
        # 3. СКАЧИВАНИЕ
        cookie_sources = get_ydlp_cookie_sources(url)
        for attempt_number, (cookie_source_name, cookie_opts) in enumerate(cookie_sources):
            attempt_opts = ydl_opts.copy()
            attempt_opts.update({key: value for key, value in cookie_opts.items()
                                 if not key.startswith('_')})

            try:
                if cookie_opts:
                    print(f"Пробую cookies: {cookie_source_name}")

                with YoutubeDL(attempt_opts) as ydl:
                    scoped_file = cookie_opts.get('_scoped_cookie_file')
                    if scoped_file:
                        ydl.cookiejar = load_scoped_cookie_file(
                            scoped_file, cookie_opts['_cookie_site'])
                    info_dict = ydl.extract_info(url, download=True)
                break
            except Exception as e:
                has_more_cookie_sources = attempt_number < len(cookie_sources) - 1
                if has_more_cookie_sources and (cookie_opts or is_cookie_or_auth_error(e)):
                    cookie_attempt_errors.append(f"{cookie_source_name}: {e}")
                    print(f"Источник cookies не сработал ({cookie_source_name}): {e}")
                    continue
                raise
        else:
            raise Exception("Не удалось скачать контент ни с одним источником cookies.")

        if not info_dict:
            raise Exception("Не удалось получить информацию о контенте.")

        # Обновляем название файла для корректного отчета об ошибках
        title = info_dict.get('title') or info_dict.get('fulltitle') or 'Неизвестное название'

        if info_dict.get('_type') == 'playlist':
            bot.send_message(chat_id,
                             "❌ Пожалуйста, отправьте ссылку на **отдельный трек/клип**, а не на плейлист.")
            bot.edit_message_text("❌ Загрузка отменена (отправлена ссылка на плейлист).", chat_id,
                                  status_message_id)
            return

        video_id = info_dict.get('id')

        # (ИЗМЕНЕНО: ВОЗВРАТ К MP3)
        # Возвращаем старую, простую логику поиска файла.
        # Мы точно знаем, что аудио будет '.mp3', а видео '.mp4'.
        ext = 'mp3' if is_audio_only else 'mp4'
        final_filename = os.path.join(DOWNLOAD_FOLDER, f'{video_id}.{ext}')

        if not os.path.exists(final_filename):
            found = False
            for f in os.listdir(DOWNLOAD_FOLDER):
                if f.startswith(video_id) and (f.endswith('.mp4') or f.endswith('.mp3')):
                    final_filename = os.path.join(DOWNLOAD_FOLDER, f)
                    found = True
                    break
            if not found:
                raise FileNotFoundError(f"Файл {video_id} не найден в папке после загрузки/конвертации.")
        # (КОНЕЦ ИЗМЕНЕНИЯ ПОИСКА)

        final_filename_to_delete = final_filename

        # 5. ПРОВЕРКА РАЗМЕРА И ОТПРАВКА
        file_size = os.path.getsize(final_filename)

        if file_size > MAX_DOWNLOAD_SIZE_BYTES:
            bot.edit_message_text(
                f"❌ **{file_type.capitalize()}** не отправлено: файл получился "
                f"{file_size / 1_000_000:.1f} МБ, а лимит загрузки — {MAX_DOWNLOAD_SIZE_MB} МБ.",
                chat_id, status_message_id, parse_mode='Markdown')
            return

        send_filename = final_filename
        if file_size > MAX_FILE_SIZE_BYTES:
            bot.edit_message_text(
                f"⚙️ Файл весит {file_size / 1_000_000:.1f} МБ. "
                f"Пробую сжать до {MAX_FILE_SIZE_MB} МБ...",
                chat_id, status_message_id)
            try:
                send_filename, compression_folder_to_delete = compress_media(
                    final_filename, is_audio_only)
            except CompressionError as error:
                bot.edit_message_text(f"❌ Не удалось уместить файл в лимит Telegram: {error}",
                                      chat_id, status_message_id)
                return

        bot.edit_message_text(f"📤 **{file_type.capitalize()}** скачано, идет отправка в Telegram...",
                              chat_id, status_message_id, parse_mode='Markdown')

        with open(send_filename, 'rb') as content_file:
            if is_audio_only:
                artist = info_dict.get('artist') or info_dict.get('uploader') or 'Неизвестный Исполнитель'
                title = info_dict.get('title') or info_dict.get('fulltitle') or 'Неизвестный Трек'

                bot.send_audio(
                    chat_id,
                    content_file,
                    caption=f"Трек: {artist} - {title}",
                    performer=artist,
                    title=title,
                    timeout=120  # <--- ДОБАВЛЕНО: Увеличиваем тайм-аут до 120 секунд
                )
            else:
                # Удаляем подпись для видео, чтобы исключить конфликт
                bot.send_video(
                    chat_id,
                    content_file,
                    # caption=title,
                    supports_streaming=True,
                    timeout=120  # <--- ДОБАВЛЕНО: Увеличиваем тайм-аут до 120 секунд
                )

        # 6. РЕДАКТИРОВАНИЕ СТАТУСА: УСПЕХ
        bot.edit_message_text(f"✅ **{file_type.capitalize()}** успешно отправлено!",
                              chat_id, status_message_id, parse_mode='Markdown')
        print(f"Успешная загрузка: {title} ({file_type})")

    except Exception as e:
        owner_hint = get_owner_error_hint(e, cookie_attempt_errors)

        if 'Error code: 413' in str(e):
            error_message = (
                f"❌ Не удалось отправить {file_type}: Telegram отклонил файл как слишком большой. "
                "Лимит обычного Bot API — 50 МБ."
            )
        else:
            error_message = (
                f"❌ Не удалось скачать или конвертировать **{file_type}**.\n"
                f"Название: *{title}*\n\n"
                f"Причина ошибки (для владельца): {e}{owner_hint}"
            )
        try:
            bot.edit_message_text(error_message, chat_id, status_message_id)
        except Exception:
            bot.send_message(chat_id, error_message)
        print(f"Ошибка при обработке {url}: {e}")

    finally:
        # 7. ОБЯЗАТЕЛЬНОЕ УДАЛЕНИЕ ВРЕМЕННЫХ ФАЙЛОВ
        if compression_folder_to_delete:
            shutil.rmtree(compression_folder_to_delete, ignore_errors=True)
        if final_filename_to_delete and os.path.exists(final_filename_to_delete):
            os.remove(final_filename_to_delete)
            print(f"Удален временный файл: {final_filename_to_delete}")


# --- ОБРАБОТЧИКИ КОМАНД ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_user_allowed(message):
        notify_admin_unauthorized(message)
        return

    # Показываем главное меню всем разрешенным пользователям
    reply_markup = get_main_reply_keyboard(message.from_user.id)

    welcome_text = (
        "👋 Привет! Я бот DL-06.\n\n"
        "Выбери режим скачивания:\n"
        "**Audio 🎵** — MP3 в лучшем качестве\n"
        "**Video 🎥** — MP4 (YouTube, Reels, TikTok)\n\n"
        "Нажми кнопку режима — после этого можно отправлять ссылки подряд. "
        "Скачиваю до 75 МБ и при необходимости сжимаю для отправки."
    )
    bot.reply_to(message, welcome_text, reply_markup=reply_markup, parse_mode='Markdown')


# --- ОБРАБОТЧИК ДЛЯ КНОПКИ "ПОЛЬЗОВАТЕЛИ" ---
@bot.message_handler(func=lambda message: is_user_admin(message) and message.text == "Пользователи")
@admin_only
def show_whitelist(message):
    ids = sorted(list(ALLOWED_USER_IDS))

    if not ids:
        response = "📝 Список разрешенных пользователей пуст."
    else:
        response = "📝 **Разрешенные ID пользователей:**\n"
        for user_id in ids:
            # Не показываем ADMIN_ID в общем списке, так как он всегда там, если список не пуст
            if str(user_id) != str(ADMIN_ID):
                response += f"`{user_id}`\n"

        # === ИСПРАВЛЕНИЯ ЗДЕСЬ ===

        # 1. Убран дефис из "Раз-решенные"
        if response == "📝 **Разрешенные ID пользователей:**\n":
            response = "📝 В списке разрешенных пользователей только Ваш ID."
        else:
            # 2. Добавлен '\' перед '*', чтобы Telegram не искал парный символ
            response += "\n\\*Ваш ID (Администратор) всегда включен в список."

    bot.reply_to(message, response, parse_mode='Markdown')


# --- ОБРАБОТЧИК КНОПКИ "ДОБАВИТЬ ПОЛЬЗОВАТЕЛЯ" ---
@bot.message_handler(func=lambda message: is_user_admin(message) and message.text == "Добавить пользователя")
@admin_only
def add_user_trigger(message):
    # 1. Используем новую стандартизированную клавиатуру "Отмена"
    cancel_markup = get_cancel_keyboard()

    # 2. Отправка запроса
    msg = bot.send_message(message.chat.id,
                           "Оправьте **@username** или **ID-пользователя** для добавления в разрешенный список. "
                           "(Нажмите 'Отмена' для выхода)",
                           parse_mode='Markdown',
                           reply_markup=cancel_markup)

    # 3. Регистрация следующего шага
    bot.register_next_step_handler(msg, process_add_user_step)


@admin_only
def process_add_user_step(message):
    # --- 1. Обработка нажатия "Отмена" (используем новый обработчик) ---
    if handle_cancel(message):
        return

    # --- 2. Основная логика добавления ---
    text_input = message.text.strip()
    user_id_to_add = None

    if text_input.startswith('@'):
        try:
            chat_info = bot.get_chat(text_input)
            user_id_to_add = str(chat_info.id)
        except Exception:
            # Восстанавливаем главное меню после ошибки
            bot.reply_to(message, "❌ Не удалось найти пользователя по этому Username или он неактивен.",
                         reply_markup=get_main_reply_keyboard(message.from_user.id))
            return
    else:
        if text_input.isdigit():
            user_id_to_add = text_input
        else:
            # Восстанавливаем главное меню после ошибки
            bot.reply_to(message, "❌ Неверный формат. Пожалуйста, отправьте только ID (цифры) или @username.",
                         reply_markup=get_main_reply_keyboard(message.from_user.id))
            return

    if user_id_to_add in ALLOWED_USER_IDS:
        # Восстанавливаем главное меню
        bot.reply_to(message, f"✅ Пользователь `{user_id_to_add}` уже находится в Whitelist.",
                     reply_markup=get_main_reply_keyboard(message.from_user.id))
        return

    ALLOWED_USER_IDS.add(user_id_to_add)
    save_whitelist()

    # Успех: восстанавливаем главное меню
    bot.reply_to(message, f"✅ Пользователь с ID `{user_id_to_add}` успешно добавлен в Whitelist!",
                 reply_markup=get_main_reply_keyboard(message.from_user.id))

    try:
        bot.send_message(user_id_to_add, "🎉 Доступ к боту предоставлен! Можешь начинать скачивание.")
    except Exception:
        bot.send_message(ADMIN_ID,
                         f"⚠️ Не удалось отправить сообщение добавленному пользователю `{user_id_to_add}`. Возможно, он заблокировал бота.")


# --- Обработчик для inline-кнопки (для уведомления о неавторизованном доступе) ---

@bot.callback_query_handler(func=lambda call: call.data.startswith('add_id:'))
@admin_only
def callback_add_id(call):
    user_id_to_add = call.data.split(':')[1]

    if user_id_to_add in ALLOWED_USER_IDS:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"✅ Пользователь `{user_id_to_add}` уже был в списке.",
            parse_mode='Markdown'
        )
        return

    ALLOWED_USER_IDS.add(user_id_to_add)
    save_whitelist()
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Пользователь с ID `{user_id_to_add}` успешно добавлен в Whitelist!",
        parse_mode='Markdown'
    )
    try:
        bot.send_message(user_id_to_add, "🎉 Доступ к боту предоставлен! Можешь начинать скачивание.")
    except Exception as e:
        print(f"Не удалось отправить сообщение добавленному пользователю {user_id_to_add}: {e}")


def notify_admin_unauthorized(message):
    user = message.from_user
    user_info = (
        f"🚨 **НОВЫЙ ПОЛЬЗОВАТЕЛЬ ПЫТАЕТСЯ ИСПОЛЬЗОВАТЬ БОТА** 🚨\n\n"
        f"ID: `{user.id}`\n"
        f"Username: @{user.username or 'отсутствует'}\n"
        f"Имя: {user.first_name or 'Нет'}"
    )

    markup = types.InlineKeyboardMarkup()
    btn_add = types.InlineKeyboardButton("✅ Добавить в Whitelist", callback_data=f'add_id:{user.id}')
    markup.add(btn_add)

    bot.send_message(ADMIN_ID, user_info, parse_mode='Markdown', reply_markup=markup)
    bot.reply_to(message, "🚫 Доступ к боту ограничен. Обратитесь к администратору.")


# --- ОБРАБОТЧИКИ РЕЖИМА И ССЫЛОК ---

def set_user_mode_and_pin(chat_id, user_id, mode):
    """Переключает режим пользователя, отправляет сообщение и закрепляет его."""
    USER_DOWNLOAD_MODES[str(user_id)] = mode
    mode_name = "Аудио (MP3)" if mode == 'audio' else "Видео (MP4)"
    text = f"✅ Режим скачивания: **{mode_name}**. Отправляйте ссылки подряд — каждая будет обработана."
    msg = bot.send_message(chat_id, text, parse_mode='Markdown',
                           reply_markup=get_main_reply_keyboard(user_id))
    try:
        bot.pin_chat_message(chat_id, msg.message_id)
    except Exception as e:
        print(f"Не удалось закрепить сообщение в чате {chat_id}: {e}")


@bot.message_handler(func=lambda message: message.text == "Audio 🎵" and is_user_allowed(message))
def set_audio_mode(message):
    set_user_mode_and_pin(message.chat.id, message.from_user.id, 'audio')


@bot.message_handler(func=lambda message: message.text == "Video 🎥" and is_user_allowed(message))
def set_video_mode(message):
    set_user_mode_and_pin(message.chat.id, message.from_user.id, 'video')


def is_link_message(message):
    """Проверяет, что сообщение похоже на ссылку и пользователь в режиме скачивания."""
    if not message.text or not message.text.strip():
        return False
    if not is_user_allowed(message):
        return False
    uid = str(message.from_user.id)
    if uid not in USER_DOWNLOAD_MODES or USER_DOWNLOAD_MODES[uid] not in ('audio', 'video'):
        return False
    return bool(re.match(r'https?://[^\s]+', message.text.strip()))


@bot.message_handler(func=is_link_message)
def handle_link_in_mode(message):
    """Обрабатывает ссылку, когда у пользователя выбран режим (аудио или видео)."""
    url = message.text.strip()
    user_id = str(message.from_user.id)
    download_type = USER_DOWNLOAD_MODES[user_id]
    bot.reply_to(message, "Ссылка принята, начинаю загрузку...",
                 reply_markup=get_main_reply_keyboard(message.from_user.id))
    threading.Thread(target=download_content, args=(url, message.chat.id, download_type)).start()


@bot.message_handler(func=lambda message: True)
def handle_other_messages(message):
    if not is_user_allowed(message):
        notify_admin_unauthorized(message)
        return

    bot.reply_to(message,
                 "Неизвестная команда. Пожалуйста, используйте кнопки меню для выбора действия ⬇️",
                 reply_markup=get_main_reply_keyboard(message.from_user.id))


# --- ЗАПУСК БОТА ---
if __name__ == '__main__':
    load_whitelist()

    if not ADMIN_ID:
        print("!!! КРИТИЧЕСКАЯ ОШИБКА: ADMIN_ID не установлен в config.py. Бот не будет работать корректно. !!!")

    print(f"Бот запущен. Admin ID: {ADMIN_ID}. Разрешенные ID: {', '.join(ALLOWED_USER_IDS)}")
    bot.infinity_polling()
