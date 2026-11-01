"""Метаданные Spotify и поиск соответствующего аудио без обхода DRM."""

import re
import unicodedata
import threading
from contextlib import contextmanager
from difflib import SequenceMatcher
from urllib.parse import urlsplit

import requests
from network_safety import PublicSession, PublicCurlSession
from secret_safety import redact


class MusicSourceError(Exception):
    pass


SPOTIFY_HOSTS = {'open.spotify.com', 'spotify.link', 'spoti.fi'}
_spotify_transport_lock = threading.Lock()
_spotify_transport_ready = False


def configure_spotify_transport():
    """Use the library's client-pool factory with our guarded curl subclass."""
    global _spotify_transport_ready
    with _spotify_transport_lock:
        if _spotify_transport_ready:
            return
        from spotapi.http.request import TLSClient
        from spotapi.public import client_pool

        class PublicTLSClient(TLSClient, PublicCurlSession):
            pass

        with client_pool.lock:
            for client in client_pool.queue:
                client.close()
            client_pool.queue.clear()
            client_pool.obj_factory = lambda: PublicTLSClient('chrome120', '', auto_retries=1)
        _spotify_transport_ready = True


class MusicSession(PublicSession):
    def request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', (10, 30))
        return super().request(method, url, **kwargs)


@contextmanager
def music_search_client():
    try:
        from ytmusicapi import YTMusic
    except ImportError as error:
        raise MusicSourceError('Установите зависимости через Install Requirments.bat.') from error
    with MusicSession() as session:
        yield YTMusic(requests_session=session)


def is_spotify_url(url):
    return (urlsplit(url).hostname or '').lower() in SPOTIFY_HOSTS


def spotify_reference(url):
    host = (urlsplit(url).hostname or '').lower()
    if host in {'spotify.link', 'spoti.fi'}:
        with MusicSession() as session:
            with session.get(url, timeout=20, allow_redirects=True) as response:
                response.raise_for_status()
                url = response.url
    parsed = urlsplit(url)
    if parsed.hostname != 'open.spotify.com':
        raise MusicSourceError('Короткая Spotify-ссылка не привела к open.spotify.com.')
    match = re.fullmatch(
        r'/(?:intl-[\w-]+/)?(?:embed/)?(track|album|playlist)/([A-Za-z0-9]+)/*', parsed.path)
    if not match:
        raise MusicSourceError('Нужна Spotify-ссылка на трек, альбом или публичный плейлист.')
    return match.groups()


def get_spotify_tracks(url):
    try:
        from SpotipyFree import Spotify
    except ImportError as error:
        raise MusicSourceError('Установите зависимости через Install Requirments.bat.') from error
    kind, identifier = spotify_reference(url)
    configure_spotify_transport()
    client = Spotify()
    try:
        if kind == 'track':
            tracks = [client.track(identifier)]
        elif kind == 'album':
            # album_tracks() загружает все страницы, а не только первую страницу альбома.
            tracks = client.album_tracks(identifier)['items']
        else:
            tracks = [item.get('track') for item in client.playlist_items(identifier)['items']]
    except Exception as error:
        raise MusicSourceError(
            'Не удалось получить список Spotify. Проверьте доступность ссылки и обновите '
            f'зависимости. Поддерживаются публичные ссылки. {redact(error)}') from None
    if not tracks:
        raise MusicSourceError('Spotify вернул пустой список треков.')
    return tracks


def artist_names(track):
    names = []
    for artist in track.get('artists') or []:
        if isinstance(artist, str):
            name = artist
        else:
            name = artist.get('name') or (artist.get('profile') or {}).get('name')
        if name:
            names.append(name)
    return names


def normalized(text):
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', text).casefold()))


def similarity(first, second):
    first, second = normalized(first), normalized(second)
    if not first or not second:
        return 0
    return max(SequenceMatcher(None, first, second).ratio(),
               SequenceMatcher(None, ' '.join(sorted(first.split())),
                               ' '.join(sorted(second.split()))).ratio())


def match_score(track, candidate):
    title = track.get('name') or ''
    names = artist_names(track)
    candidate_names = artist_names(candidate)
    if not names or not candidate_names or not candidate.get('videoId'):
        return 0
    candidate_title = candidate.get('title') or ''
    # Не подменяем студийную запись ремиксом/кавером/концертом с похожим названием.
    for version in ('live', 'remix', 'cover', 'karaoke', 'instrumental', 'slowed', 'sped up'):
        if re.search(r'\b' + version + r'\b', normalized(candidate_title)) and not re.search(
                r'\b' + version + r'\b', normalized(title)):
            return 0
    title_score = similarity(title, candidate_title)
    artist_score = max(similarity(names[0], name) for name in candidate_names)
    expected_duration = (track.get('duration_ms') or 0) / 1000
    actual_duration = candidate.get('duration_seconds')
    if not actual_duration and candidate.get('duration'):
        try:
            actual_duration = 0
            for part in candidate['duration'].split(':'):
                actual_duration = actual_duration * 60 + int(part)
        except (ValueError, AttributeError):
            return 0
    if not expected_duration or not actual_duration:
        return 0
    if abs(expected_duration - actual_duration) > max(7, expected_duration * 0.06):
        return 0
    if title_score < 0.72 or artist_score < 0.7:
        return 0
    return title_score * 0.65 + artist_score * 0.35


def find_music_audio(track, music_client=None, source_name='Источник'):
    if not track or track.get('is_local') or track.get('type', 'track') != 'track':
        raise MusicSourceError(f'Недоступный трек {source_name} пропущен.')
    names = artist_names(track)
    if not names or not track.get('name'):
        raise MusicSourceError(f'{source_name} не вернул название или исполнителя трека.')
    if music_client is None:
        with music_search_client() as client:
            return find_music_audio(track, client, source_name)
    query = f'{names[0]} {track["name"]}'
    candidates = music_client.search(query, filter='songs', limit=10)
    matches = [(match_score(track, candidate), candidate) for candidate in candidates]
    score, match = max(matches, key=lambda item: item[0], default=(0, None))
    if score < 0.8 or match is None:
        raise MusicSourceError(f'Не найдено точное соответствие на YouTube Music: {query}')
    video_id = match['videoId']
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise MusicSourceError('Поиск YouTube Music вернул некорректный идентификатор.')
    metadata = {'title': track['name'], 'track': track['name'], 'artist': ', '.join(names)}
    return f'https://www.youtube.com/watch?v={video_id}', metadata


def find_spotify_audio(track, music_client=None):
    return find_music_audio(track, music_client, 'Spotify')
