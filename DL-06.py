import telebot
from telebot import types
from network_safety import YoutubeDL, source_url, UnsafeURLError, LOCAL_MEDIA_ARGS
from secret_safety import load_local_secrets, install_diagnostic_redaction
from access_safety import RateGate, telegram_user_id
from functools import wraps
from cookie_scope import (
    cookie_file_for_url, load_scoped_cookie_file, site_for_url,
)
from download_pipeline import (
    DeliveryReport, RequestAbortedError, cleanup_folder, download_and_deliver,
    is_tiktok_webpage_error, plain_error, is_drm_error, drm_error_hint,
)
from music_sources import (
    MusicSourceError, find_spotify_audio, get_spotify_tracks, is_spotify_url,
    music_search_client, spotify_reference,
)
import os
from link_bundle import LinkBundle
from r2_storage import R2Settings, R2Storage, StorageError, StorageQuotaError
from traffic_stats import TrafficStats, DownloadTraffic
from cloudflare_analytics import monthly_operations, AnalyticsError
from admin_reports import storage_report, traffic_report
from stage_progress import StageProgress, download_status, upload_status
import re
import threading
import time
import json
import subprocess
import tempfile
import shutil
import html
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from media_policy import (REQUEST_LIMIT, USER_STORAGE_LIMIT, RequestBudget,
                          is_music_link, is_short_video, readable_filename)
from request_choices import ChoiceStore, ChoiceError
from cover_art import prepare_cover, tag_audio
from audio_formats import COMPATIBILITY_EXTENSIONS, prepare_compatible_audio
from soundcloud_fallback import (is_soundcloud_url, read_soundcloud_info, soundcloud_plan,
                                 find_soundcloud_audio, SOURCE_NOTE)

# --- ИМПОРТ КОНФИГУРАЦИИ ---
import config

# --- ⚙️ КОНФИГУРАЦИЯ ⚙️ --
TOKEN = config.TOKEN
try:
    ADMIN_ID = int(telegram_user_id(config.ADMIN_ID))
except (ValueError, TypeError, AttributeError):
    raise RuntimeError('Укажите корректный положительный ADMIN_ID в config.py.') from None
FFMPEG_PATH = config.FFMPEG_PATH
# --------------------------------------------------------

bot = telebot.TeleBot(TOKEN)

# --- ГЛОБАЛЬНЫЕ НАСТРОЙКИ ---
# У облачного Telegram Bot API предел отправки около 50 МБ.
# Небольшой запас исключает ошибку 413 на пограничных файлах.
MAX_FILE_SIZE_MB = 49
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1_000_000
BASE_FOLDER = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_FOLDER = os.path.join(BASE_FOLDER, 'downloads')
WHITELIST_FILE = os.path.join(BASE_FOLDER, 'whitelist.txt')
R2_CONFIG_FILE = os.path.join(BASE_FOLDER, 'r2_config.json')
R2_STATE_FILE = os.path.join(BASE_FOLDER, '_state', 'r2.sqlite3')
TRAFFIC_STATE_FILE = os.path.join(BASE_FOLDER, '_state', 'traffic.sqlite3')
ANALYTICS_CONFIG_FILE = os.path.join(BASE_FOLDER, 'cloudflare_analytics.json')
ALLOWED_USER_IDS = set()
# Режим скачивания для каждого пользователя: 'audio' | 'video' (отправлять ссылки подряд)
USER_DOWNLOAD_MODES = {}
LINK_DOWNLOAD_LOCK = threading.Lock()
CHOICES = ChoiceStore()
METADATA_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix='metadata')
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='download')
ACTIVE_DOWNLOAD_USERS = set()
ACTIVE_DOWNLOAD_LOCK = threading.Lock()
WHITELIST_LOCK = threading.Lock()
METADATA_SLOTS = threading.BoundedSemaphore(8)
LINK_RATE = RateGate(3, 15)
DENIED_USER_RATE = RateGate(1, 900)
DENIED_GLOBAL_RATE = RateGate(30, 3600, max_keys=1)

if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)


def get_ydlp_cookie_sources(url):
    """Cookies разрешены только для выбранного сервиса."""
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
        print(f"Ошибка ffmpeg/ffprobe: {plain_error(result.stderr[-1000:])}")
        raise CompressionError("Не удалось обработать файл с помощью ffmpeg.")
    return result.stdout


def get_media_duration(filename):
    ffprobe_path = os.path.join(os.path.dirname(FFMPEG_PATH), 'ffprobe.exe')
    output = run_media_command([
        ffprobe_path, '-v', 'error', *LOCAL_MEDIA_ARGS, '-show_entries', 'format=duration',
        '-of', 'json', filename,
    ])
    try:
        duration = float(json.loads(output)['format']['duration'])
    except (KeyError, ValueError, TypeError, OverflowError) as error:
        raise CompressionError("Не удалось определить длительность файла.") from error
    if duration <= 0:
        raise CompressionError("Не удалось определить длительность файла.")
    return duration


def compress_media(filename, audio_only, max_output_bytes=MAX_FILE_SIZE_BYTES,
                   temporary_parent=None):
    """Возвращает путь к сжатому файлу и временную папку для удаления после отправки."""
    duration = get_media_duration(filename)
    temporary_folder = tempfile.mkdtemp(
        prefix='dl06-compress-', dir=temporary_parent or DOWNLOAD_FOLDER)
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
                    *LOCAL_MEDIA_ARGS, '-i', filename, '-map', '0:a:0', '-vn', '-c:a', 'libmp3lame',
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
                    *LOCAL_MEDIA_ARGS, '-i', filename, '-map', '0:v:0', '-an', '-sn', '-dn',
                    *video_options, '-pass', '1', '-f', 'null', 'NUL',
                ])
                run_media_command([
                    FFMPEG_PATH, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    *LOCAL_MEDIA_ARGS, '-i', filename, '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn',
                    *video_options, '-pass', '2', '-c:a', 'aac', '-b:a', str(audio_bitrate),
                    '-movflags', '+faststart', output_filename,
                ])

            if os.path.getsize(output_filename) <= max_output_bytes:
                return output_filename, temporary_folder

        raise CompressionError("После сжатия файл всё ещё превышает лимит Telegram.")
    except Exception:
        cleanup_folder(temporary_folder, DOWNLOAD_FOLDER)
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
    # A concurrent add or interrupted write must not truncate the access list.
    with WHITELIST_LOCK:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=os.path.dirname(WHITELIST_FILE),
                                             prefix='.whitelist-', suffix='.tmp', delete=False) as file:
                temporary = file.name
                for user_id in sorted(ALLOWED_USER_IDS.copy()):
                    file.write(f'{user_id}\n')
            os.replace(temporary, WHITELIST_FILE)
        finally:
            if temporary and os.path.isfile(temporary):
                os.remove(temporary)


# --- ФУНКЦИЯ ПРОВЕРКИ ДОСТУПА И ДЕКОРАТОР ---

def is_user_admin(message):
    return str(getattr(getattr(message, 'from_user', None), 'id', None)) == str(ADMIN_ID)


def is_user_allowed(message):
    return str(getattr(getattr(message, 'from_user', None), 'id', None)) in ALLOWED_USER_IDS


def admin_only(func):
    @wraps(func)
    def wrapper(message):
        callback = hasattr(message, 'data')
        target = getattr(message, 'message', None) if callback else message
        private = getattr(getattr(target, 'chat', None), 'type', None) == 'private'
        if not is_user_admin(message) or not private:
            text = '🚫 Эта команда доступна только администратору в личном чате с ботом.'
            if callback:
                bot.answer_callback_query(message.id, text, show_alert=True)
            else:
                bot.reply_to(message, text)
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

    # Информационные разделы видны только администратору.
    if str(user_id) == str(ADMIN_ID):
        markup.row(types.KeyboardButton('Инфо'))

    return markup


def get_info_reply_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, selective=True)
    markup.row(types.KeyboardButton('Пользователи'), types.KeyboardButton('Статистика'))
    markup.row(types.KeyboardButton('Хранилище R2'))
    markup.row(types.KeyboardButton('⬅️ Главное меню'))
    return markup


def get_users_reply_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, selective=True)
    markup.row(types.KeyboardButton('Добавить пользователя'))
    markup.row(types.KeyboardButton('⬅️ Инфо'))
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


# --- СКАЧИВАНИЕ И ПОСЛЕДОВАТЕЛЬНАЯ ОТПРАВКА ---

def get_link_storage():
    return R2Storage(R2Settings.from_file(R2_CONFIG_FILE), R2_STATE_FILE)


def get_traffic_store():
    return TrafficStats(TRAFFIC_STATE_FILE)


def record_traffic(field, size):
    try:
        get_traffic_store().add(field, size)
    except Exception:
        # An accounting failure must never repeat or interrupt media delivery.
        print('Не удалось сохранить счётчик трафика; проверьте базу статистики и место на диске.')


def validate_downloaded_media(filename, info):
    if str(info.get('extractor_key', '')).lower().startswith('yandexmusic'):
        expected_duration = info.get('duration') or 0
        if expected_duration:
            actual_duration = get_media_duration(filename)
            if actual_duration < expected_duration - max(3, expected_duration * 0.05):
                raise MusicSourceError(
                    'Яндекс вернул короткое превью вместо полного трека. '
                    'Обновите site_cookies/yandex_music.txt и проверьте подписку.')


def send_downloaded_file(filename, info, chat_id, audio_only, set_status,
                         request_folder):
    title = str(info.get('track') or info.get('title') or 'Без названия')
    artist = str(info.get('artist') or info.get('uploader') or 'Неизвестный исполнитель')
    caption = f'{artist} — {title}'[:500]
    if info.get('_source_note'):
        caption += '\n\n♻️ ' + str(info['_source_note'])[:400]
    validate_downloaded_media(filename, info)
    if os.path.getsize(filename) > MAX_FILE_SIZE_BYTES:
        raise ValueError('Файл не помещается в Telegram и должен быть отправлен ссылкой.')
    set_status(f'📤 Отправляю: {artist} — {title}' if audio_only
               else f'📤 Отправляю видео: {title}')
    document = False
    for attempt in range(4):
        try:
            with ExitStack() as stack:
                content = stack.enter_context(open(filename, 'rb'))
                if document:
                    bot.send_document(chat_id, content, timeout=120,
                                      caption=(caption + '\nTelegram не принял формат '
                                               'как музыкальный трек. Исходный файл сохранён без перекодирования.')[:1024])
                elif audio_only:
                    extra = {}
                    cover = info.get('_telegram_thumbnail')
                    if cover and os.path.isfile(cover):
                        extra['thumbnail'] = stack.enter_context(open(cover, 'rb'))
                    bot.send_audio(chat_id, content, caption=caption,
                                   performer=artist[:128], title=title[:128], timeout=120, **extra)
                else:
                    bot.send_video(chat_id, content, supports_streaming=True, timeout=120)
            break
        except telebot.apihelper.ApiTelegramException as error:
            description = str(error.result_json.get('description') or '').lower()
            if (audio_only and not document and error.error_code == 400
                    and any(part in description for part in
                            ('audio', 'unsupported', 'wrong file type', 'content type'))):
                document = True
                continue
            retry_after = (error.result_json.get('parameters') or {}).get('retry_after')
            if error.error_code != 429 or not retry_after or attempt == 3:
                raise
            set_status(f'⏳ Telegram просит подождать {retry_after} сек. перед отправкой.')
            time.sleep(retry_after + 1)
    else:
        raise ValueError('Не удалось отправить файл в Telegram после повторных попыток.')
    record_traffic('telegram_bytes', os.path.getsize(filename))
    print(f'Отправлено: {title}')


def get_music_cookie_sources(url):
    sources = get_ydlp_cookie_sources(url)
    if site_for_url(url) != 'yandex_music':
        return sources
    try:
        from yt_dlp_plugins.extractor import yandex_music_v2  # noqa: F401
    except ImportError as error:
        raise MusicSourceError('Для Яндекс.Музыки запустите Install Requirments.bat.') from error
    sources = [(name, options) for name, options in sources if options]
    if not sources:
        raise MusicSourceError(
            'Для полных треков Яндекс.Музыки нужен файл site_cookies/yandex_music.txt '
            'с cookies аккаунта с действующей подпиской (формат Netscape).')
    jar = load_scoped_cookie_file(sources[0][1]['_scoped_cookie_file'], 'yandex_music')
    if not any(cookie.name == 'Session_id' and not cookie.is_expired() for cookie in jar):
        raise MusicSourceError(
            'В site_cookies/yandex_music.txt нет действующего Session_id. '
            'Экспортируйте свежие cookies Яндекс.Музыки.')
    return sources


def download_content(url, chat_id, download_type, delivery_mode='auto', audio_format='mp3',
                     user_id=None, prepared_tracks=None):
    with LINK_DOWNLOAD_LOCK:
        return process_download_content(url, chat_id, download_type, delivery_mode,
                                        audio_format, user_id, prepared_tracks)


def process_download_content(url, chat_id, download_type, delivery_mode='auto', audio_format='mp3',
                             user_id=None, prepared_tracks=None):
    link_mode = delivery_mode in ('link', 'zip')
    user_id = str(user_id if user_id is not None else chat_id)
    spotify = is_spotify_url(url)
    yandex_music = site_for_url(url) == 'yandex_music'
    audio_only = download_type == 'audio' or spotify or yandex_music
    file_type = 'аудио' if audio_only else 'видео'
    try:
        status_message = bot.send_message(
            chat_id, f'⏳ Начинаю загрузку {file_type}. '
            + ('Подготовлю файл или ZIP альбома для скачивания по ссылке.' if link_mode
               else 'Треки альбома будут отправляться по очереди.'))
    except Exception as error:
        print(f'Не удалось отправить статус: {plain_error(error)}')
        return

    def edit_status(text):
        # Сбой редактирования прогресса не должен срывать загрузку или отправку.
        try:
            bot.edit_message_text(plain_error(text)[:3900], chat_id, status_message.message_id, timeout=10)
        except Exception as error:
            if getattr(error, 'error_code', None) == 429:
                raise
            print(f'Не удалось обновить статус: {plain_error(error)}')

    progress = StageProgress(edit_status)
    set_status = progress.set
    bundle = None
    budget = RequestBudget()
    track_position = None
    source_traffic = DownloadTraffic(lambda size: record_traffic('source_bytes', size))

    def download_hook(data):
        source_traffic.observe(data)
        budget.observe(data)
        if data['status'] == 'downloading':
            info = data.get('info_dict') or {}
            phase = ('download', data.get('filename'), info.get('format_id'))
            set_status(download_status(data, track_position), phase=phase)
        elif data['status'] == 'finished':
            set_status('⚙️ Скачивание части файла завершено. Подготавливаю файл…', phase='prepare')

    def postprocessor_hook(data):
        name = str(data.get('postprocessor', '')).lower()
        if data.get('status') not in ('started', 'processing') or 'deliverfile' in name:
            return
        if 'merger' in name:
            text = '⚙️ Объединяю видео и звук…'
        elif 'extractaudio' in name:
            text = ('⚙️ Сохраняю исходный аудиоформат…' if audio_format == 'original'
                    else '⚙️ Подготавливаю MP3…')
        elif 'metadata' in name:
            text = '⚙️ Записываю название и исполнителя…'
        else:
            text = '⚙️ Обрабатываю файл…'
        set_status(text, phase=('postprocess', name))

    options = {
        'format': ('bestaudio/best' if audio_only else
                   'bestvideo[ext=mp4]+bestaudio[ext=m4a]/'
                   'bestvideo[height>=720]+bestaudio/'
                   'bestvideo[height>=480]+bestaudio/best'),
        'restrictfilenames': False,
        'noplaylist': not audio_only or (site_for_url(url) == 'youtube'
                                        and not urlsplit(url).path.startswith('/playlist')),
        'max_filesize': REQUEST_LIMIT,
        'playlistend': 201,
        'quiet': True,
        'no_warnings': False,
        'ffmpeg_location': FFMPEG_PATH,
        'retries': 5,
        'fragment_retries': 10,
        'socket_timeout': 30,
        'progress_hooks': [download_hook],
        'postprocessor_hooks': [postprocessor_hook],
        'merge_output_format': 'mp4',
    }
    options.update(get_ydlp_js_runtime_options())
    if audio_only:
        options['writethumbnail'] = True
        options['postprocessors'] = [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'best' if audio_format == 'original' else 'mp3',
             **({} if audio_format == 'original' else {'preferredquality': '0'})},
            {'key': 'FFmpegMetadata'},
        ]

    report = DeliveryReport()
    request_folder = None
    storage = None
    link_delivered = False
    sent_telegram = 0
    sent_links = 0
    replacement_names = []

    def cloud_storage():
        nonlocal storage
        if storage is None:
            storage = get_link_storage()
        return storage

    def send_cloud(filename, download_name, *, album=False, source_note=None):
        nonlocal sent_links
        cloud = cloud_storage()
        size = os.path.getsize(filename)
        set_status(upload_status(0, size, download_name), phase='upload')
        stored = cloud.upload(
            filename, download_name, user_id=user_id,
            user_limit_bytes=None if user_id == str(ADMIN_ID) else USER_STORAGE_LIMIT,
            progress_callback=lambda done, total: set_status(
                upload_status(done, total, download_name), phase='upload'))
        record_traffic('r2_bytes', stored.size)
        kind = 'трек' if audio_only else 'видео'
        button = '⬇️ Скачать альбом' if album else f'⬇️ Скачать {kind}'
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(button, url=stored.url))
        introduction = ('✅ Альбом готов для скачивания.' if album else
                        f'⚠️ {"Трек" if audio_only else "Видео"} превышает размер для отправки в Telegram.')
        text = (f'{introduction}\n<b>{html.escape(stored.filename)}</b>\n'
                f'Размер: {stored.size / 1_000_000:.1f} МБ\n'
                f'Ссылка действует {cloud.settings.ttl_seconds / 3600:g} ч.')
        if source_note:
            text += '\n\n♻️ ' + html.escape(source_note)
        try:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML',
                             disable_web_page_preview=True)
            sent_links += 1
        except Exception:
            cloud.revoke(stored)
            raise

    try:
        progress.start()
        required_free = REQUEST_LIMIT * 2 + 512_000_000
        if shutil.disk_usage(DOWNLOAD_FOLDER).free < required_free:
            raise StorageError('Недостаточно свободного места для подготовки файла.')
        if link_mode:
            set_status('☁️ Проверяю доступность облачного хранилища…', phase='cloud-check')
            storage = get_link_storage()
            usage = storage.check_connection()
            if (usage['bytes'] >= storage.settings.max_storage_bytes
                    or usage['objects'] >= storage.settings.max_objects):
                raise StorageQuotaError('Временное хранилище заполнено. Дождитесь очистки файлов.')
            options['max_filesize'] = storage.settings.max_file_bytes
            budget.max_bytes = storage.settings.max_file_bytes
        request_folder = tempfile.mkdtemp(prefix='dl06-request-', dir=DOWNLOAD_FOLDER)
        set_status('🔎 Получаю сведения о файле и доступных форматах…', phase='metadata')
        if link_mode:
            bundle = LinkBundle(request_folder, storage.settings.max_file_bytes)

        def deliver(filename, info):
            nonlocal sent_telegram
            validate_downloaded_media(filename, info)
            if audio_only and os.path.splitext(filename)[1].lower() in COMPATIBILITY_EXTENSIONS:
                info = {**info, '_source_extension': os.path.splitext(filename)[1]}
                set_status('⚙️ Подготавливаю M4A для музыкального плеера…', phase='audio-compatibility')
                original_cover = prepare_cover(filename, info, request_folder, FFMPEG_PATH)
                if original_cover:
                    info = {**info, 'thumbnails': [*(info.get('thumbnails') or []),
                                                  {'filepath':str(original_cover)}]}
                filename = prepare_compatible_audio(filename, FFMPEG_PATH, request_folder)
            destination = os.path.join(os.path.dirname(filename), readable_filename(filename, info))
            if os.path.normcase(destination) != os.path.normcase(filename):
                os.replace(filename, destination)
                filename = destination
            if audio_only:
                tag_audio(filename, info)
            cover = prepare_cover(filename, info, request_folder, FFMPEG_PATH) if audio_only else None
            if cover:
                info = {**info, '_telegram_thumbnail': str(cover)}
            budget.complete(os.path.getsize(filename))
            if link_mode:
                bundle.add(filename, info)
            else:
                send_filename = filename
                compression_folder = None
                try:
                    if (not audio_only and os.path.getsize(filename) > MAX_FILE_SIZE_BYTES
                            and is_short_video(url, info)):
                        set_status('⚙️ Сжимаю короткое видео для отправки в Telegram…', phase='compress')
                        try:
                            send_filename, compression_folder = compress_media(
                                filename, False, max_output_bytes=MAX_FILE_SIZE_BYTES,
                                temporary_parent=request_folder)
                            readable_compressed = os.path.join(compression_folder, os.path.basename(filename))
                            if os.path.normcase(send_filename) != os.path.normcase(readable_compressed):
                                os.replace(send_filename, readable_compressed)
                                send_filename = readable_compressed
                        except CompressionError:
                            send_filename = filename
                    if os.path.getsize(send_filename) <= MAX_FILE_SIZE_BYTES:
                        send_downloaded_file(send_filename, info, chat_id, audio_only,
                                             set_status, request_folder)
                        sent_telegram += 1
                    else:
                        send_cloud(filename, os.path.basename(filename), source_note=info.get('_source_note'))
                finally:
                    if compression_folder:
                        cleanup_folder(compression_folder, DOWNLOAD_FOLDER)
                    if os.path.isfile(filename):
                        os.remove(filename)

        def soundcloud_metadata(source_url):
            return read_soundcloud_info(
                source_url, get_music_cookie_sources(source_url), load_scoped_cookie_file,
                get_ydlp_js_runtime_options(), YoutubeDL)

        def run_soundcloud(entries):
            nonlocal track_position
            result = DeliveryReport()
            music_client = None
            with ExitStack() as clients:
                for index, entry in enumerate(entries, 1):
                    track_position = f'Трек {index} из {len(entries)}'
                    source_url = entry.get('source_url') or ''
                    name = entry.get('title') or f'Трек {index}'
                    if not source_url:
                        result.failed += 1
                        result.errors.append(f'{name}: SoundCloud не вернул ссылку на трек.')
                        continue
                    context = {key: entry[key] for key in ('album','playlist_index','playlist_id')
                               if entry.get(key) is not None}
                    try:
                        native = None
                        if not entry.get('_drm'):
                            def deliver_native(filename, information):
                                deliver(filename, {**information, **context})
                            native = download_and_deliver(
                                source_url, options, deliver_native, get_music_cookie_sources(source_url),
                                load_scoped_cookie_file, request_folder, DOWNLOAD_FOLDER)
                            if native.sent or not any(is_drm_error(error) for error in native.errors):
                                result.add(native)
                                continue
                        set_status(f'♻️ SoundCloud: защита DRM. Ищу на YouTube Music — {name}', phase='sc-search')
                        details = entry
                        if not details.get('duration') or not (details.get('artists') or details.get('artist')):
                            details, _ = soundcloud_metadata(source_url)
                        details = {**details, **context}
                        if music_client is None:
                            music_client = clients.enter_context(music_search_client())
                        audio_url, metadata = find_soundcloud_audio(details, music_client)
                        def deliver_replacement(filename, information):
                            deliver(filename, {**information, **metadata})
                            replacement_names.append(f'{metadata["artist"]} — {metadata["track"]}')
                        replacement = download_and_deliver(
                            audio_url, options, deliver_replacement, get_ydlp_cookie_sources(audio_url),
                            load_scoped_cookie_file, request_folder, DOWNLOAD_FOLDER)
                        result.add(replacement)
                    except RequestAbortedError:
                        raise
                    except Exception as error:
                        result.failed += 1
                        result.errors.append(f'{name}: {plain_error(error)}')
            return result

        if spotify:
            set_status('🔎 Получаю список Spotify. Аудио подбираю на YouTube Music '
                       'по названию, исполнителю и длительности.')
            tracks = prepared_tracks if prepared_tracks is not None else get_spotify_tracks(url)
            with music_search_client() as music_client:
                for index, track in enumerate(tracks, 1):
                    track_position = f'Трек {index} из {len(tracks)}'
                    name = (track or {}).get('name') or 'Недоступный трек'
                    set_status(f'🔎 Spotify: {index}/{len(tracks)} — {name}')
                    try:
                        audio_url, metadata = find_spotify_audio(track, music_client)

                        def deliver_spotify(filename, info):
                            deliver(filename, {**info, **metadata})

                        report.add(download_and_deliver(
                            audio_url, options, deliver_spotify, get_ydlp_cookie_sources(audio_url),
                            load_scoped_cookie_file, request_folder, DOWNLOAD_FOLDER))
                    except RequestAbortedError:
                        raise
                    except Exception as error:
                        report.failed += 1
                        report.errors.append(f'{name}: {plain_error(error)}')
        elif (is_soundcloud_url(url) and prepared_tracks
              and prepared_tracks[0].get('_soundcloud')):
            report = run_soundcloud(prepared_tracks)
        else:
            report = download_and_deliver(
                url, options, deliver, get_music_cookie_sources(url),
                load_scoped_cookie_file, request_folder, DOWNLOAD_FOLDER,
                on_retry=lambda attempt, total: set_status(
                    f'♻️ Повторная попытка скачивания: {attempt}/{total}...'))
            if (audio_only and is_soundcloud_url(url) and not report.sent
                    and any(is_drm_error(error) for error in report.errors)):
                details, protected = soundcloud_metadata(url)
                report = run_soundcloud(soundcloud_plan(details, url, protected))
        if link_mode and report.sent:
            set_status('📦 Подготавливаю файл для скачивания. Собираю альбом в ZIP, если треков несколько…',
                       phase='bundle')
            notes = None
            source_note = None
            if replacement_names:
                notes = SOURCE_NOTE + '\n\nТреки:\n' + '\n'.join(replacement_names) + '\n'
                source_note = (f'Из-за DRM у SoundCloud {len(replacement_names)} трек(ов) скачано с YouTube Music:\n'
                               + '\n'.join(name[:140] for name in replacement_names[:10]))
                if len(replacement_names) > 10:
                    source_note += f'\nИ ещё {len(replacement_names)-10}. Полный список — ИСТОЧНИКИ.txt в архиве.'
                source_note += '\nЭто аудио YouTube Music, не lossless SoundCloud.'
            upload_file, download_name = bundle.finalize(source_notes=notes, force_archive=delivery_mode=='zip')
            send_cloud(upload_file, download_name, album=True, source_note=source_note)
            link_delivered = True
    except Exception as error:
        if link_mode and not link_delivered:
            report.sent = 0
        report.failed += 1
        report.errors.append(plain_error(error))
        print(f'Ошибка загрузки: {plain_error(error)}')
    finally:
        try:
            source_traffic.flush()
            if request_folder:
                try:
                    set_status('🧹 Удаляю временные файлы с компьютера…', phase='cleanup')
                    cleanup_folder(request_folder, DOWNLOAD_FOLDER)
                except OSError as error:
                    report.errors.append(f'Не удалось очистить временную папку: {plain_error(error)}')
                    print(report.errors[-1])
        finally:
            progress.close()

    # The background publisher is stopped before the final status is edited.
    def final_status(text):
        try:
            edit_status(text)
        except Exception:
            print('Не удалось обновить итоговый статус запроса.')

    if report.failed or report.errors:
        details = '\n'.join(plain_error(error)[:600] for error in report.errors[:3])
        if site_for_url(url) == 'tiktok' and any(
                is_tiktok_webpage_error(error) for error in report.errors):
            details = ('TikTok не отдал данные ролика после повторных попыток. '
                       'Проверьте, что ссылка открывается в браузере, и повторите позже.')
        if any('Sign in to confirm your age' in error for error in report.errors):
            details += '\nОбновите site_cookies/youtube.txt.'
        label = 'Файлов по ссылке' if link_mode else 'Отправлено'
        final_status(f'⚠️ {label}: {report.sent}. Не удалось: {report.failed}.\n\n{details}')
    else:
        final_status((f'✅ Готово! Файлов по ссылке: {report.sent}.' if link_mode
                   else f'✅ Готово! Отправлено файлов: {report.sent}.\n'
                        f'В Telegram: {sent_telegram}. По ссылкам: {sent_links}.')
                     + (f'\nИз-за DRM скачано с YouTube Music: {len(replacement_names)}.'
                        if replacement_names else ''))


# --- ОБРАБОТЧИКИ КОМАНД ---
@bot.message_handler(func=lambda message: message.text in ('Инфо', '⬅️ Инфо'))
@admin_only
def show_info(message):
    if message.chat.type != 'private':
        bot.reply_to(message, 'Инфо доступно администратору в личном чате с ботом.')
        return
    bot.reply_to(message, 'ℹ️ Инфо\nВыбери раздел:', reply_markup=get_info_reply_keyboard())


@bot.message_handler(func=lambda message: message.text == '⬅️ Главное меню')
@admin_only
def back_to_main_menu(message):
    if message.chat.type != 'private':
        bot.reply_to(message, 'Инфо доступно администратору в личном чате с ботом.')
        return
    bot.reply_to(message, 'Главное меню ⬇️',
                 reply_markup=get_main_reply_keyboard(message.from_user.id))


@bot.message_handler(commands=['storage'])
@bot.message_handler(func=lambda message: message.text == 'Хранилище R2')
@admin_only
def show_storage(message):
    if message.chat.type != 'private':
        bot.reply_to(message, 'Статистика доступна администратору в личном чате с ботом.')
        return
    snapshot = error = operations = analytics_error = None
    storage = None
    try:
        storage = get_link_storage()
        snapshot = storage.status_snapshot()
    except StorageError as failure:
        error = str(failure)
    except Exception:
        error = 'Не удалось прочитать состояние хранилища.'
    if storage is not None:
        try:
            operations = monthly_operations(ANALYTICS_CONFIG_FILE, storage.settings)
        except AnalyticsError as failure:
            analytics_error = str(failure)
        except Exception:
            analytics_error = 'Не удалось получить аналитику Cloudflare.'
    bot.reply_to(message, storage_report(snapshot, error, operations, analytics_error),
                 reply_markup=get_info_reply_keyboard())


@bot.message_handler(commands=['stats'])
@bot.message_handler(func=lambda message: message.text == 'Статистика')
@admin_only
def show_stats(message):
    if message.chat.type != 'private':
        bot.reply_to(message, 'Статистика доступна администратору в личном чате с ботом.')
        return
    try:
        text = traffic_report(get_traffic_store().snapshot())
    except Exception:
        text = '📊 Статистика недоступна: не удалось прочитать базу счётчиков.'
    bot.reply_to(message, text, reply_markup=get_info_reply_keyboard())



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
        "**Audio 🎵** — исходный формат или MP3\n"
        "**Video 🎥** — MP4 (YouTube, Reels, TikTok)\n\n"
        "Нажми кнопку режима — после этого можно отправлять ссылки подряд. "
        "Для музыки предложу формат, для альбома — треки в Telegram или ZIP по ссылке.\n"
        "Поддерживаю SoundCloud, Spotify (подбор на YouTube Music), Яндекс.Музыку.\n"
        "Файлы до 49 МБ отправляю в Telegram, крупные — ссылкой для скачивания. "
        "Музыку не сжимаю ради размера. TikTok, Shorts и Reels при необходимости "
        "пробую сжать для Telegram."
    )
    bot.reply_to(message, welcome_text, reply_markup=reply_markup, parse_mode='Markdown')


# --- ОБРАБОТЧИК ДЛЯ КНОПКИ "ПОЛЬЗОВАТЕЛИ" ---
@bot.message_handler(func=lambda message: message.text == "Пользователи")
@admin_only
def show_whitelist(message):
    if message.chat.type != 'private':
        bot.reply_to(message, 'Пользователи доступны администратору в личном чате с ботом.')
        return
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

    bot.reply_to(message, response, parse_mode='Markdown', reply_markup=get_users_reply_keyboard())


# --- ОБРАБОТЧИК КНОПКИ "ДОБАВИТЬ ПОЛЬЗОВАТЕЛЯ" ---
@bot.message_handler(func=lambda message: message.text == "Добавить пользователя")
@admin_only
def add_user_trigger(message):
    if message.chat.type != 'private':
        bot.reply_to(message, 'Добавление пользователей доступно администратору в личном чате с ботом.')
        return
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
    text_input = (message.text or '').strip()
    user_id_to_add = None

    if text_input.startswith('@'):
        try:
            chat_info = bot.get_chat(text_input)
            if chat_info.type != 'private':
                raise ValueError('Нужен пользователь, а не группа или канал.')
            user_id_to_add = telegram_user_id(chat_info.id)
        except Exception:
            # Восстанавливаем главное меню после ошибки
            bot.reply_to(message, "❌ Не удалось найти пользователя по этому Username или он неактивен.",
                         reply_markup=get_main_reply_keyboard(message.from_user.id))
            return
    else:
        try:
            user_id_to_add = telegram_user_id(text_input)
        except ValueError:
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

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('add_id:'))
@admin_only
def callback_add_id(call):
    try:
        _, value = call.data.split(':')
        user_id_to_add = telegram_user_id(value)
    except ValueError:
        bot.answer_callback_query(call.id, 'Некорректный ID пользователя.', show_alert=True)
        return
    bot.answer_callback_query(call.id)

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
        print(f"Не удалось отправить сообщение добавленному пользователю: {plain_error(e)}")


def notify_admin_unauthorized(message):
    user = message.from_user
    if user is None or not DENIED_USER_RATE.allow(user.id):
        return
    # One unknown user cannot generate repeated notifications; different IDs also
    # share a global budget. HTML escaping prevents forged text/markup in a name.
    bot.reply_to(message, '🚫 Доступ к боту ограничен. Обратитесь к администратору.')
    if not DENIED_GLOBAL_RATE.allow('all'):
        return
    user_info = (
        '🚨 НОВЫЙ ПОЛЬЗОВАТЕЛЬ ПЫТАЕТСЯ ИСПОЛЬЗОВАТЬ БОТА\n\n'
        f'ID: <code>{user.id}</code>\n'
        f"Username: @{html.escape(user.username or 'отсутствует')}\n"
        f"Имя: {html.escape(user.first_name or 'Нет')}"
    )

    markup = types.InlineKeyboardMarkup()
    btn_add = types.InlineKeyboardButton("✅ Добавить в Whitelist", callback_data=f'add_id:{user.id}')
    markup.add(btn_add)

    bot.send_message(ADMIN_ID, user_info, parse_mode='HTML', reply_markup=markup)


# --- ОБРАБОТЧИКИ РЕЖИМА И ССЫЛОК ---

def set_user_mode_and_pin(chat_id, user_id, mode):
    """Переключает режим пользователя, отправляет сообщение и закрепляет его."""
    USER_DOWNLOAD_MODES[str(user_id)] = mode
    mode_name = "Аудио (исходный формат или MP3)" if mode == 'audio' else "Видео (MP4)"
    text = f"✅ Режим скачивания: **{mode_name}**. Отправляйте ссылки подряд — каждая будет обработана."
    msg = bot.send_message(chat_id, text, parse_mode='Markdown',
                           reply_markup=get_main_reply_keyboard(user_id))
    try:
        bot.pin_chat_message(chat_id, msg.message_id)
    except Exception as e:
        print(f"Не удалось закрепить сообщение: {plain_error(e)}")


@bot.message_handler(func=lambda message: message.text == "Audio 🎵" and is_user_allowed(message))
def set_audio_mode(message):
    set_user_mode_and_pin(message.chat.id, message.from_user.id, 'audio')


@bot.message_handler(func=lambda message: message.text == "Video 🎥" and is_user_allowed(message))
def set_video_mode(message):
    set_user_mode_and_pin(message.chat.id, message.from_user.id, 'video')


@bot.message_handler(func=lambda message: message.text in ('В Telegram 📎', 'Ссылка 🔗')
                     and is_user_allowed(message))
def set_delivery_mode(message):
    bot.send_message(message.chat.id,
                     '✅ Доставка теперь автоматическая: небольшие файлы — в Telegram, '
                     'крупные — ссылкой. Для альбома можно выбрать ZIP по ссылке.',
                     reply_markup=get_main_reply_keyboard(message.from_user.id))


def format_choice_keyboard(key, delivery=False):
    markup = types.InlineKeyboardMarkup()
    buttons = ([('Треками в Telegram', 'auto'), ('ZIP по ссылке', 'zip')] if delivery else
               [('Исходный формат', 'original'), ('MP3', 'mp3')])
    for label, action in buttons:
        markup.add(types.InlineKeyboardButton(label, callback_data=f'choose:{key}:{action}'))
    markup.add(types.InlineKeyboardButton('Отмена', callback_data=f'choose:{key}:cancel'))
    return markup


def probe_audio(url):
    if is_spotify_url(url):
        kind, identifier = spotify_reference(url)
        tracks = get_spotify_tracks(f'https://open.spotify.com/{kind}/{identifier}')
        if not tracks or len(tracks) > 200:
            raise ChoiceError('Нужен трек или альбом/плейлист не более 200 треков.')
        album = kind in ('album', 'playlist')
        first = tracks[0] or {}
        title = ((first.get('album') or {}).get('name') if album else first.get('name'))
        return album, str(title or 'Музыка Spotify'), len(tracks), tracks
    if is_soundcloud_url(url):
        try:
            info, protected = read_soundcloud_info(
                url, get_music_cookie_sources(url), load_scoped_cookie_file,
                get_ydlp_js_runtime_options(), YoutubeDL)
            tracks = soundcloud_plan(info, url, protected)
        except MusicSourceError as error:
            raise ChoiceError(str(error)) from None
        return ('entries' in info, str(info.get('title') or info.get('track') or 'Музыка SoundCloud'),
                len(tracks), tracks)
    class QuietMetadata:
        def __init__(self):
            self.drm = False
        def debug(self, text):
            pass
        def error(self, text):
            self.drm = self.drm or is_drm_error(text)
        warning = error
    drm_seen = False
    for _, cookies in get_music_cookie_sources(url):
        logger = QuietMetadata()
        options = {'quiet': True, 'no_warnings': True, 'skip_download': True,
                   'format': 'bestaudio/best',
                   'extract_flat': 'in_playlist', 'playlistend': 201, 'ignoreerrors': False,
                   'socket_timeout': 15, 'retries': 1, 'logger': logger,
                   'noplaylist': site_for_url(url) == 'youtube' and not urlsplit(url).path.startswith('/playlist')}
        options.update(get_ydlp_js_runtime_options())
        try:
            with YoutubeDL(options) as downloader:
                if cookies.get('_scoped_cookie_file'):
                    downloader.cookiejar = load_scoped_cookie_file(
                        cookies['_scoped_cookie_file'], cookies['_cookie_site'])
                info = downloader.extract_info(url, download=False)
            if not info:
                continue
            album = 'entries' in info
            entries = list(info.get('entries') or []) if album else []
            count = max(len(entries), info.get('playlist_count') or 0) if album else 1
            if count > 200:
                raise ChoiceError('В одном запросе можно скачать не более 200 треков.')
            if album and not count:
                raise ChoiceError('В альбоме не удалось найти доступные треки.')
            return album, str(info.get('title') or info.get('track') or 'Музыка'), count, None
        except ChoiceError:
            raise
        except Exception as error:
            drm_seen = drm_seen or logger.drm or is_drm_error(error)
            continue
    if drm_seen:
        host = (urlsplit(url).hostname or '').lower()
        source = 'SoundCloud' if host == 'soundcloud.com' or host.endswith('.soundcloud.com') else 'Источник'
        raise ChoiceError(drm_error_hint(source))
    raise ChoiceError('Не удалось получить информацию о музыке. Проверьте ссылку и попробуйте ещё раз.')


def prepare_audio_choice(item, message, status_message=None):
    ticker = None
    try:
        if status_message is not None:
            ticker = StageProgress(lambda text: bot.edit_message_text(
                text, item.chat_id, status_message.message_id, timeout=10))
            ticker.set('🔎 Определяю трек или альбом и доступные форматы…')
            ticker.start()
        try:
            album, title, count, tracks = probe_audio(item.url)
        finally:
            if ticker is not None:
                ticker.close()
        prepared = CHOICES.prepare(item.key, album=album, title=title, count=count, tracks=tracks)
        if prepared is None:
            return
        label = f'🎵 Альбом/плейлист: {title}\nТреков: {count}' if album else f'🎵 Трек: {title}'
        text = (label + '\n\nВыбери формат скачивания:\n'
                'OGG/Opus автоматически преобразую в M4A (AAC) для музыкального плеера. '
                'Настоящий FLAC сохраню в FLAC.')
        if is_soundcloud_url(item.url):
            text += ('\nSoundCloud вернул защищённый DRM-поток. Попробую найти ту же запись на YouTube Music. '
                     if tracks and any(track.get('_drm') for track in tracks) else
                     '\nЕсли SoundCloud вернёт DRM, попробую найти ту же запись на YouTube Music. ')
            text += 'Замена источника будет отмечена; lossless YouTube Music не предоставляет.'
        if status_message is not None:
            bot.edit_message_text(text, item.chat_id, status_message.message_id,
                                  reply_markup=format_choice_keyboard(item.key))
            CHOICES.bind(item.key, status_message.message_id)
        else:
            response = bot.reply_to(message, text, reply_markup=format_choice_keyboard(item.key))
            CHOICES.bind(item.key, response.message_id)
    except Exception as error:
        CHOICES.discard(item.key)
        text = plain_error(error) if isinstance(error, (ChoiceError, MusicSourceError)) else 'Не удалось подготовить выбор формата.'
        bot.reply_to(message, text[:1000])


def enqueue_download(url, chat_id, download_type, *, user_id, audio_format='original',
                     delivery_mode='auto', tracks=None):
    user_id = str(user_id)
    with ACTIVE_DOWNLOAD_LOCK:
        if user_id in ACTIVE_DOWNLOAD_USERS:
            bot.send_message(chat_id, '⏳ Ваш предыдущий запрос ещё выполняется или находится в очереди.')
            return False
        if len(ACTIVE_DOWNLOAD_USERS) >= 20:
            bot.send_message(chat_id, '⏳ Очередь загрузок заполнена. Попробуйте позже.')
            return False
        ACTIVE_DOWNLOAD_USERS.add(user_id)
    def run():
        try:
            download_content(url, chat_id, download_type, delivery_mode,
                             audio_format, user_id, tracks)
        except Exception:
            bot.send_message(chat_id, 'Не удалось завершить запрос. Попробуйте ещё раз.')
        finally:
            with ACTIVE_DOWNLOAD_LOCK:
                ACTIVE_DOWNLOAD_USERS.discard(user_id)
    try:
        bot.send_message(chat_id, '⏳ Запрос принят. Подготовка начнётся по очереди.')
        DOWNLOAD_EXECUTOR.submit(run)
    except Exception:
        with ACTIVE_DOWNLOAD_LOCK:
            ACTIVE_DOWNLOAD_USERS.discard(user_id)
        raise
    return True


@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('choose:'))
def choose_download(call):
    if not is_user_allowed(call):
        bot.answer_callback_query(call.id, 'Нет доступа к боту.', show_alert=True)
        return
    if getattr(call, 'message', None) is None:
        bot.answer_callback_query(call.id, 'Отправьте ссылку заново.', show_alert=True)
        return
    try:
        _, key, action = call.data.split(':')
        item, step = CHOICES.choose(key, call.from_user.id, call.message.chat.id,
                                   call.message.message_id, action)
    except (ValueError, ChoiceError) as error:
        text = str(error) if isinstance(error, ChoiceError) else 'Некорректная кнопка.'
        bot.answer_callback_query(call.id, text, show_alert=True)
        return
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    if step == 'cancel':
        text, keyboard = 'Запрос отменён.', None
    elif step == 'delivery':
        text = (f'🎵 {item.title}\nФормат: {"исходный" if item.audio_format == "original" else "MP3"}.\n\n'
                'Как отправить альбом?\nПри отправке треками крупные файлы придут отдельными ссылками.')
        keyboard = format_choice_keyboard(key, delivery=True)
    else:
        text, keyboard = '✅ Формат выбран. Запрос передан на подготовку.', None
    try:
        bot.edit_message_text(text[:3900], call.message.chat.id, call.message.message_id, reply_markup=keyboard)
    except Exception:
        if step == 'delivery':
            CHOICES.discard(key)
            bot.send_message(item.chat_id, 'Не удалось показать выбор доставки. Отправьте ссылку заново.')
            return
    if step in ('auto', 'zip'):
        enqueue_download(item.url, item.chat_id, 'audio', user_id=item.user_id,
                         audio_format=item.audio_format, delivery_mode=step, tracks=item.tracks)


def is_link_message(message):
    """Проверяет, что сообщение похоже на ссылку и пользователь в режиме скачивания."""
    if not message.text or not message.text.strip():
        return False
    if not is_user_allowed(message):
        return False
    return bool(re.match(r'https?://[^\s]+', message.text.strip()))


@bot.message_handler(func=is_link_message)
def handle_link_in_mode(message):
    """Обрабатывает ссылку, когда у пользователя выбран режим (аудио или видео)."""
    url = message.text.strip()
    user_id = str(message.from_user.id)
    if not is_user_allowed(message):
        return
    try:
        source_url(url)
    except UnsafeURLError as error:
        bot.reply_to(message, str(error))
        return
    if not LINK_RATE.allow(user_id):
        bot.reply_to(message, '⏳ Слишком много ссылок. Подождите 15 секунд.')
        return
    download_type = 'audio' if is_music_link(url) else USER_DOWNLOAD_MODES.get(user_id, 'video')
    if download_type == 'audio':
        if not METADATA_SLOTS.acquire(blocking=False):
            bot.reply_to(message, '⏳ Очередь подготовки заполнена. Попробуйте позже.')
            return
        try:
            item = CHOICES.create(user_id, message.chat.id, url)
        except ChoiceError as error:
            METADATA_SLOTS.release()
            bot.reply_to(message, str(error))
            return
        def prepare():
            try:
                prepare_audio_choice(item, message, status)
            finally:
                METADATA_SLOTS.release()
        try:
            status = bot.reply_to(message, '🔎 Определяю трек или альбом и доступные форматы…')
            METADATA_EXECUTOR.submit(prepare)
        except Exception:
            CHOICES.discard(item.key)
            METADATA_SLOTS.release()
            raise
    else:
        enqueue_download(url, message.chat.id, 'video', user_id=user_id)


@bot.message_handler(func=lambda message: True)
def handle_other_messages(message):
    if not is_user_allowed(message):
        notify_admin_unauthorized(message)
        return

    bot.reply_to(message,
                 "Неизвестная команда. Пожалуйста, используйте кнопки меню для выбора действия ⬇️",
                 reply_markup=get_main_reply_keyboard(message.from_user.id))


# --- ЗАПУСК БОТА ---
def cleanup_storage_worker():
    while True:
        try:
            if os.path.isfile(R2_CONFIG_FILE):
                get_link_storage().cleanup_expired()
        except Exception:
            print('R2: очистка временно недоступна; повтор через 5 минут.')
        time.sleep(300)


if __name__ == '__main__':
    load_local_secrets(BASE_FOLDER, config)
    install_diagnostic_redaction()
    load_whitelist()
    try:
        get_traffic_store()
    except Exception:
        print('Не удалось открыть базу статистики трафика.')
    threading.Thread(target=cleanup_storage_worker, daemon=True).start()

    if not ADMIN_ID:
        print("!!! КРИТИЧЕСКАЯ ОШИБКА: ADMIN_ID не установлен в config.py. Бот не будет работать корректно. !!!")

    print(f'Бот запущен. Разрешённых пользователей: {len(ALLOWED_USER_IDS)}.')
    bot.infinity_polling()
