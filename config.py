"""Локальные настройки. Секреты берутся из .env или переменных среды."""

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
if not TOKEN:
    raise RuntimeError('Укажите TELEGRAM_BOT_TOKEN в файле .env.')

try:
    ADMIN_ID = int(os.getenv('TELEGRAM_ADMIN_ID', '0').strip())
except ValueError as error:
    raise RuntimeError('TELEGRAM_ADMIN_ID должен быть числовым ID.') from error
if ADMIN_ID <= 0:
    raise RuntimeError('Укажите TELEGRAM_ADMIN_ID в файле .env.')

ffmpeg_setting = os.getenv('FFMPEG_PATH', '').strip()
FFMPEG_PATH = shutil.which(ffmpeg_setting or 'ffmpeg')
if not FFMPEG_PATH:
    raise RuntimeError('Установите FFmpeg и добавьте его в PATH или укажите FFMPEG_PATH в .env.')
if not Path(FFMPEG_PATH).with_name('ffprobe.exe').is_file():
    raise RuntimeError('Рядом с ffmpeg.exe должен находиться ffprobe.exe.')

YTDLP_NODE_PATH = os.getenv('YTDLP_NODE_PATH', '').strip() or None

YTDLP_SITE_COOKIE_FILES = {
    'youtube': str(BASE_DIR / 'site_cookies' / 'youtube.txt'),
    'instagram': str(BASE_DIR / 'site_cookies' / 'instagram.txt'),
    'tiktok': str(BASE_DIR / 'site_cookies' / 'tiktok.txt'),
}
