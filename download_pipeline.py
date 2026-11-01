"""Последовательная доставка файлов yt-dlp с очисткой каждого запроса."""

import copy
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from network_safety import YoutubeDL
from secret_safety import redact
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadCancelled

from cookie_scope import site_for_url


class RequestAbortedError(DownloadCancelled):
    """Лимит запроса исчерпан: прекращаем альбом, сохраняя обязательную очистку."""

    def __init__(self, message):
        self.msg = message
        super().__init__(message)


def plain_error(error):
    return redact(error)


def is_drm_error(error):
    text = plain_error(error).lower()
    return any(marker in text for marker in (
        'drm protected', 'drm-protected', 'drm protection', '[drm]', 'защищён drm', 'защитой drm',
    ))


def drm_error_hint(source='источник'):
    return (f'🔒 {source} отдаёт этот трек только в формате с защитой DRM. '
            'Ссылка распознана, но yt-dlp не поддерживает скачивание такого потока. '
            'Выбор MP3 вместо исходного формата это ограничение не снимает. '
            'Можно отправить ссылку на эту запись из другого источника.')


def cleanup_folder(folder, download_root):
    """Удаляет только вложенную папку загрузок; повторяет при блокировке Windows."""
    target = Path(folder).resolve()
    root = Path(download_root).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError('Попытка удалить папку вне каталога загрузок.')
    for attempt in range(4):
        try:
            shutil.rmtree(target)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 3:
                raise
            time.sleep(0.2 * (attempt + 1))


def is_auth_error(error):
    text = plain_error(error).lower()
    return any(marker in text for marker in (
        'cookies', 'sign in', 'http error 403', 'http error 401',
        'login required', 'authentication required', 'captcha',
    ))


def is_tiktok_webpage_error(error):
    text = plain_error(error).lower()
    return any(marker in text for marker in (
        'unexpected response from webpage request',
        'unable to extract universal data for rehydration',
        'unable to extract challenge data',
    ))


def get_download_attempts(url, cookie_sources):
    sources = list(cookie_sources)
    if site_for_url(url) != 'tiktok':
        return sources
    attempts = list(sources)
    # Некоторые новые профили curl_cffi получают от TikTok пустую страницу.
    # Профили применяются только при повторе неудачного TikTok-запроса.
    for profile in ('chrome-146', 'chrome-131'):
        for name, cookie_options in sources:
            attempts.append((f'{name}, HTTP {profile}', {
                **cookie_options, 'impersonate': ImpersonateTarget.from_str(profile),
            }))
    return attempts


def count_entries(info):
    # None обозначает недоступный/неудачно скачанный элемент плейлиста.
    if info is None:
        return 1
    if info.get('_type') in ('playlist', 'multi_video'):
        return sum(count_entries(entry) for entry in info.get('entries') or [])
    return 1


@dataclass
class DeliveryReport:
    sent: int = 0
    failed: int = 0
    errors: list = field(default_factory=list)

    def add(self, other):
        self.sent += other.sent
        self.failed += other.failed
        self.errors.extend(other.errors)


class DownloadLogger:
    def __init__(self):
        self.errors = []

    def debug(self, message):
        pass

    def warning(self, message):
        print(plain_error(message))

    def error(self, message):
        message = plain_error(message)
        if is_drm_error(message):
            message = drm_error_hint('Источник')
        self.errors.append(message)
        print(message)


class DeliverFilePP(PostProcessor):
    """Вызывается после конвертации и переноса, отдельно для каждого трека."""

    def __init__(self, downloader, callback):
        super().__init__(downloader)
        self.callback = callback

    def run(self, information):
        self.callback(information)
        return [], information


def download_and_deliver(url, options, on_file, cookie_sources,
                         load_cookies, request_folder, download_root, on_retry=None):
    """yt-dlp обходит альбом по порядку; сбой трека не прерывает остальные.

    При повторе с cookies уже отправленные элементы не отправляются снова.
    Файлы всех попыток, включая .part и промежуточные форматы, удаляются.
    """
    completed = {}
    delivery_errors = []
    info = None
    last_errors = []
    attempts = get_download_attempts(url, cookie_sources)
    tiktok = site_for_url(url) == 'tiktok'

    for attempt_number, (source_name, cookie_options) in enumerate(attempts):
        attempt_folder = tempfile.mkdtemp(prefix='attempt-', dir=request_folder)
        logger = DownloadLogger()
        attempt_opts = copy.deepcopy(options)
        attempt_opts.update({
            'outtmpl': '%(playlist_index|0)06d-%(id)s.%(ext)s',
            'paths': {'home': attempt_folder, 'temp': attempt_folder},
            'logger': logger,
            'ignoreerrors': True,
            'overwrites': True,
            'color': 'never',
        })
        attempt_opts.update({key: value for key, value in cookie_options.items()
                             if not key.startswith('_')})

        def deliver(information):
            key = (information.get('playlist_id'), information.get('playlist_index'),
                   information.get('id'))
            filename = information['filepath']
            if not Path(filename).resolve().is_relative_to(Path(attempt_folder).resolve()):
                raise ValueError('Загрузчик вернул файл вне временной папки.')
            try:
                if key in completed:
                    return
                try:
                    on_file(filename, information)
                except RequestAbortedError:
                    raise
                except Exception as error:
                    completed[key] = False
                    title = information.get('track') or information.get('title') or 'Трек'
                    delivery_errors.append(f'{title}: {plain_error(error)}')
                    print(delivery_errors[-1])
                else:
                    completed[key] = True
            finally:
                # Контекст open() у отправителя уже закрыт, в том числе при ошибке Telegram.
                if os.path.isfile(filename):
                    os.remove(filename)

        try:
            with YoutubeDL(attempt_opts) as ydl:
                scoped_file = cookie_options.get('_scoped_cookie_file')
                if scoped_file:
                    ydl.cookiejar = load_cookies(scoped_file, cookie_options['_cookie_site'])
                ydl.add_post_processor(DeliverFilePP(ydl, deliver), when='after_move')
                info = ydl.extract_info(url, download=True)
        except RequestAbortedError:
            raise
        except Exception as error:
            info = None
            logger.error(error)
        finally:
            cleanup_folder(attempt_folder, download_root)

        last_errors = logger.errors
        has_next_source = attempt_number + 1 < len(attempts)
        retryable = any(is_auth_error(error) or (tiktok and is_tiktok_webpage_error(error))
                        for error in last_errors)
        if has_next_source and retryable:
            print(f'Повтор загрузки после неудачной попытки: {source_name}')
            if on_retry:
                try:
                    on_retry(attempt_number + 2, len(attempts))
                except Exception as error:
                    print(f'Не удалось обновить статус повтора: {plain_error(error)}')
            continue
        break

    sent = sum(completed.values())
    total = max(count_entries(info), len(completed))
    if info is None and last_errors and completed:
        total = len(completed) + 1
    failed = total - sent
    errors = delivery_errors + last_errors
    if failed and not errors:
        errors.append('Часть файлов недоступна или превышает лимит скачивания.')
    return DeliveryReport(sent, failed, errors)
