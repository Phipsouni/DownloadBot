"""Delivery decisions based on final file size, without cloud calls for small media."""
from pathlib import Path
from urllib.parse import urlsplit

from cookie_scope import matches_domain, site_for_url
from download_pipeline import RequestAbortedError
from link_bundle import safe_name

REQUEST_LIMIT = 2_000_000_000
USER_STORAGE_LIMIT = 2_000_000_000


def is_music_link(url):
    host = (urlsplit(url).hostname or '').lower()
    return (site_for_url(url) == 'yandex_music'
            or any(matches_domain(host, domain) for domain in
                   ('soundcloud.com', 'on.soundcloud.com', 'open.spotify.com',
                    'spotify.link', 'spoti.fi', 'music.youtube.com')))


def is_short_video(url, info):
    urls = [url, info.get('webpage_url') or '', info.get('original_url') or '']
    for candidate in urls:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or '').lower()
        if matches_domain(host, 'tiktok.com'):
            return True
        if matches_domain(host, 'youtube.com') and parsed.path.startswith('/shorts/'):
            return True
        if matches_domain(host, 'instagram.com') and parsed.path.startswith(('/reel/', '/reels/')):
            return True
    return (str(info.get('extractor_key', '')).lower().startswith('tiktok')
            or info.get('media_type') in ('short', 'shorts', 'reel'))


def readable_filename(filename, info):
    extension = Path(filename).suffix.lower()
    title = str(info.get('track') or info.get('title') or 'Без названия')
    for suffix in (extension, info.get('_source_extension')):
        if suffix and title.lower().endswith(suffix.lower()):
            title = title[:-len(suffix)]
            break
    artist = info.get('artist')
    return safe_name(f'{artist} — {title}' if artist else title) + extension


class RequestBudget:
    def __init__(self, max_bytes=REQUEST_LIMIT, max_files=200):
        self.max_bytes = max_bytes
        self.max_files = max_files
        self._progress = {}
        self.transferred = 0
        self.completed = 0
        self.files = 0

    def observe(self, data):
        if data.get('status') not in ('downloading', 'finished'):
            return
        name = data.get('filename') or data.get('tmpfilename')
        size = data.get('downloaded_bytes')
        if not name or size is None:
            return
        previous = self._progress.get(name, 0)
        self.transferred += size - previous if size >= previous else size
        self._progress[name] = size
        if self.transferred > self.max_bytes:
            raise RequestAbortedError('Достигнут общий лимит скачивания 2 ГБ на запрос.')

    def complete(self, size):
        if self.completed + size > self.max_bytes or self.files >= self.max_files:
            raise RequestAbortedError('Запрос превышает лимит 2 ГБ или 200 файлов.')
        self.completed += size
        self.files += 1
