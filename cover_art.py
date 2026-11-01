"""Best-effort cover art, without transcoding the audio or fetching URLs through FFmpeg."""
import base64
import subprocess
import tempfile
from pathlib import Path
from network_safety import LOCAL_MEDIA_ARGS


def tag_audio(filename, info):
    try:
        from mutagen import File
        audio = File(filename, easy=True)
        if audio is None:
            return
        if audio.tags is None:
            audio.add_tags()
        for source, tag in [('track', 'title'), ('artist', 'artist'), ('album', 'album'),
                            ('track_number', 'tracknumber')]:
            value = info.get(source)
            if value and isinstance(value, (str, int)):
                audio[tag] = [str(value)]
        audio.save()
    except Exception:
        pass


def prepare_cover(filename, info, request_folder, ffmpeg):
    root = Path(request_folder).resolve()
    source = Path(filename).resolve()
    thumbnail_source = False
    for thumbnail in reversed(info.get('thumbnails') or []):
        candidate = thumbnail.get('filepath')
        if candidate:
            candidate = Path(candidate).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                source = candidate
                thumbnail_source = True
                break
    if (not source.is_relative_to(root) or not source.is_file()
            or source.stat().st_size < (1 if thumbnail_source else 1024)):
        return None
    folder = Path(tempfile.mkdtemp(prefix='cover-', dir=root))
    target = folder / 'cover.jpg'
    try:
        result = subprocess.run([
            ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            *LOCAL_MEDIA_ARGS, '-i', str(source), '-map', '0:v:0', '-frames:v', '1',
            '-vf', 'scale=320:320:force_original_aspect_ratio=decrease', '-q:v', '5', str(target),
        ], capture_output=True, timeout=30)
        if result.returncode or not target.is_file() or target.stat().st_size > 200_000:
            return None
        _embed(filename, target.read_bytes())
        return target
    except Exception:
        return None


def _embed(filename, jpeg):
    try:
        from mutagen.flac import FLAC, Picture
        suffix = Path(filename).suffix.lower()
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, 'image/jpeg', jpeg
        if suffix == '.flac':
            audio = FLAC(filename)
            audio.clear_pictures()
            audio.add_picture(picture)
        elif suffix == '.mp3':
            from mutagen.mp3 import MP3
            from mutagen.id3 import APIC
            audio = MP3(filename)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall('APIC')
            audio.tags.add(APIC(mime='image/jpeg', type=3, data=jpeg))
        elif suffix == '.m4a':
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(filename)
            if audio.tags is None:
                audio.add_tags()
            audio['covr'] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        elif suffix == '.opus':
            from mutagen.oggopus import OggOpus
            audio = OggOpus(filename)
            audio['metadata_block_picture'] = [base64.b64encode(picture.write()).decode('ascii')]
        else:
            return
        audio.save()
    except Exception:
        # Missing artwork support must never turn a usable track into a failed download.
        pass
