"""SoundCloud metadata and strict matching to an alternative public YouTube Music recording."""
from urllib.parse import urlsplit
import math
import re

from cookie_scope import matches_domain
from download_pipeline import is_drm_error, drm_error_hint
from music_sources import (MusicSourceError, find_music_audio, music_search_client,
                           normalized, similarity, artist_names)

SOURCE_NOTE = ('SoundCloud вернул поток с защитой DRM, поэтому этот трек скачан с YouTube Music. '
               'Это аудио YouTube Music, не lossless-оригинал SoundCloud.')


def is_soundcloud_url(url):
    host = urlsplit(url).hostname or ''
    return matches_domain(host, 'soundcloud.com')


class MetadataLogger:
    def __init__(self):
        self.drm = False

    def debug(self, text):
        pass

    def error(self, text):
        self.drm = self.drm or is_drm_error(text)

    warning = error


def read_soundcloud_info(url, sources, load_cookies, runtime_options, ydl_factory):
    drm_seen = False
    protected_info = None
    for _, cookies in sources:
        logger = MetadataLogger()
        options = {'quiet': True, 'no_warnings': True, 'skip_download': True,
                   'format': 'bestaudio/best', 'extract_flat': 'in_playlist',
                   'ignore_no_formats_error': True, 'playlistend': 201, 'ignoreerrors': False,
                   'socket_timeout': 15, 'retries': 1, 'logger': logger, **runtime_options}
        try:
            with ydl_factory(options) as downloader:
                if cookies.get('_scoped_cookie_file'):
                    downloader.cookiejar = load_cookies(cookies['_scoped_cookie_file'], cookies['_cookie_site'])
                info = downloader.extract_info(url, download=False)
            if info:
                protected = logger.drm and not info.get('formats') and 'entries' not in info
                if protected:
                    protected_info = info
                    continue
                return info, False
        except Exception as error:
            drm_seen = drm_seen or logger.drm or is_drm_error(error)
    if protected_info is not None:
        return protected_info, True
    if drm_seen:
        raise MusicSourceError(drm_error_hint('SoundCloud'))
    raise MusicSourceError('Не удалось получить информацию SoundCloud. Попробуйте позже.')


def soundcloud_plan(info, original_url, drm=False):
    album = 'entries' in info
    entries = list(info.get('entries') or []) if album else [info]
    count = max(len(entries), info.get('playlist_count') or 0) if album else 1
    if not entries or count > 200:
        raise MusicSourceError('Нужен трек или альбом/плейлист не более 200 треков.')
    result = []
    for index, entry in enumerate(entries, 1):
        entry = entry or {}
        source_url = entry.get('webpage_url') or entry.get('url') or (original_url if not album else '')
        if not source_url and str(entry.get('id') or '').isdigit():
            source_url = 'https://api-v2.soundcloud.com/tracks/' + str(entry['id'])
        if not is_soundcloud_url(source_url):
            source_url = ''
        result.append({
            '_soundcloud': True, '_drm': bool(drm and not album), 'source_url': source_url,
            'id': entry.get('id'), 'title': entry.get('title') or entry.get('track'),
            'track': entry.get('track') or entry.get('title'),
            'artist': entry.get('artist'), 'artists': entry.get('artists'),
            'uploader': entry.get('uploader'), 'duration': entry.get('duration'),
            'album': entry.get('album') or (info.get('title') if album else None),
            'playlist_index': index, 'playlist_id': info.get('id') if album else None,
        })
    return result


def find_soundcloud_audio(info, music_client=None):
    names = info.get('artists') or ([info['artist']] if info.get('artist') else [])
    if isinstance(names, str):
        names = [names]
    names = [name.get('name') if isinstance(name, dict) else name for name in names]
    names = [str(name) for name in names if name]
    if not names and info.get('uploader'):
        names = [str(info['uploader'])]
    names = split_combined_artists(names, info.get('uploader'))
    try:
        duration = float(info.get('duration') or 0)
    except (ValueError, TypeError):
        duration = 0
    if not math.isfinite(duration) or duration <= 0 or not names or not (info.get('track') or info.get('title')):
        raise MusicSourceError('Недостаточно метаданных SoundCloud для точного поиска на YouTube Music.')
    original_title = info.get('track') or info['title']
    title, featured = comparable_title(original_title, names)
    if not normalized(title):
        raise MusicSourceError('SoundCloud не вернул название для точного сопоставления.')
    required_credits = []
    for credit in [*names[1:], *featured]:
        if normalized(credit) and normalized(credit) not in {normalized(v) for v in required_credits}:
            required_credits.append(credit)
    reference = {'type': 'track', 'name': title,
                 'artists': [{'name': name} for name in names], 'duration_ms': duration * 1000}
    if music_client is None:
        with music_search_client() as client:
            return find_soundcloud_audio(info, client)
    class VersionCheckedClient:
        def __init__(self, kind):
            self.kind = kind

        def search(self, *args, **kwargs):
            query = ' '.join([names[0], title, *required_credits])
            kwargs['filter'] = self.kind
            matches = []
            for candidate in music_client.search(query, **kwargs):
                raw_title = candidate.get('title') or ''
                if not same_version(original_title, raw_title):
                    continue
                candidate_names = split_combined_artists(artist_names(candidate), names[0])
                candidate_title, _ = comparable_title(raw_title, [*names, *candidate_names])
                if similarity(title, candidate_title) < 0.9:
                    continue
                if set(re.findall(r'\d+',title)) != set(re.findall(r'\d+',candidate_title)):
                    continue
                if not all(credit_present(credit, raw_title, candidate_names) for credit in required_credits):
                    continue
                matches.append({**candidate, 'title': candidate_title,
                                'artists': [{'name': name} for name in candidate_names]})
            return matches
    try:
        url, metadata = find_music_audio(reference, VersionCheckedClient('songs'), 'SoundCloud')
    except MusicSourceError as error:
        if not str(error).startswith('Не найдено точное соответствие'):
            raise
        url, metadata = find_music_audio(reference, VersionCheckedClient('videos'), 'SoundCloud')
    return url, {**metadata, 'title': original_title, 'track': original_title, '_source_note': SOURCE_NOTE,
                 'album': info.get('album'), 'playlist_index': info.get('playlist_index'),
                 'playlist_id': info.get('playlist_id')}


def split_combined_artists(names, primary_hint=None):
    result = []
    for name in names:
        parts = [part.strip() for part in re.split(r'[,，;；&]', name) if part.strip()]
        # Keep names such as "Tyler, the Creator" intact when the full name is the author.
        if (len(parts)>1 and primary_hint and normalized(name)!=normalized(primary_hint)
                and normalized(parts[0])==normalized(primary_hint)):
            result.extend(parts)
        else:
            result.append(name)
    return list(dict.fromkeys(result))


def comparable_title(title, names):
    text = re.sub(r'[\[(]\s*(?:official\s+)?(?:music\s+video|audio|video|lyrics?|lyric\s+video|visuali[sz]er)'
                  r'(?:\s+(?:hd|4k))?\s*[\])]', '', title, flags=re.I).strip()
    parts = re.split(r'\s+[-–—]\s+',text,maxsplit=1)
    if len(parts)==2 and normalized(parts[0]) in {normalized(v) for v in [*names,' '.join(names)]}:
        text=parts[1]
    match=re.search(r'\b(?:feat(?:uring)?|ft)\b\.?\s*',text,flags=re.I)
    credits=[]
    if match:
        credits=[credit.strip(' ()[] .') for credit in re.split(r'[,，;&]',text[match.end():])]
        text=text[:match.start()].rstrip(' ([.-–—')
    return text.strip(), [credit for credit in credits if credit]


def credit_present(credit, raw_title, candidate_names):
    description=normalized(' '.join([raw_title,*candidate_names]))
    words=set(normalized(credit).split())-{'feat','ft','featuring','and','with','the'}
    if words and words<=set(description.split()):
        return True
    if normalized(credit).replace(' ','') in description.replace(' ',''):
        return True
    return any(similarity(credit,name)>=0.85 for name in candidate_names)


def same_version(source_title, candidate_title):
    source, candidate = normalized(source_title), normalized(candidate_title)
    variants = (r'\bremix(?:ed)?\b', r'\blive\b', r'\bcover\b', r'\bkaraoke\b',
                r'\binstrumental\b', r'\bslowed\b', r'\b(?:sped|speed)\s*up\b',
                r'\bacoustic\b', r'\bedit\b', r'\bextended\b', r'\bremaster(?:ed)?\b',
                r'\bclean\b', r'\bexplicit\b', r'\bdemo\b', r'\bnightcore\b', r'\breverb\b')
    return all(bool(re.search(pattern, source)) == bool(re.search(pattern, candidate))
               for pattern in variants)
