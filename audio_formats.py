"""Convert Ogg audio to a music-player-compatible format without changing genuine FLAC."""
import json
import subprocess
from pathlib import Path
from network_safety import LOCAL_MEDIA_ARGS


COMPATIBILITY_EXTENSIONS = {'.ogg', '.oga', '.opus', '.webm', '.aac'}


class AudioFormatError(Exception):
    pass


def probe_audio_stream(filename, ffprobe):
    try:
        names = ('title','artist','album','album_artist','albumartist','track','tracknumber',
                 'disc','discnumber','date','genre','comment','composer','copyright')
        tags = ','.join([*names, *(name.upper() for name in names)])
        result = subprocess.run([
            str(ffprobe), '-v', 'error', *LOCAL_MEDIA_ARGS, '-select_streams', 'a:0',
            '-show_entries', f'stream=codec_name,channels,sample_rate:stream_tags={tags}:format_tags={tags}',
            '-of', 'json', str(filename),
        ], capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError()
        body = json.loads(result.stdout)
        stream = body['streams'][0]
        stream['_metadata'] = {key.lower(): value for tags in
                               (body.get('format',{}).get('tags',{}),stream.get('tags',{}))
                               for key,value in tags.items()}
        if not stream.get('codec_name'):
            raise ValueError()
        return stream
    except Exception:
        raise AudioFormatError('Не удалось определить аудиоформат для музыкального плеера.') from None


def prepare_compatible_audio(filename, ffmpeg, request_folder):
    source = Path(filename).resolve()
    if source.suffix.lower() not in COMPATIBILITY_EXTENSIONS:
        return str(source)
    root = Path(request_folder).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise AudioFormatError('Аудиофайл находится вне временной папки запроса.')
    ffprobe = Path(ffmpeg).with_name('ffprobe.exe')
    stream = probe_audio_stream(source, ffprobe)
    codec = stream['codec_name']
    extension = '.flac' if codec == 'flac' else '.m4a'
    target = source.with_suffix(extension)
    if target.exists():
        raise AudioFormatError('Не удалось подготовить уникальное имя аудиофайла.')
    copy_audio = codec in ('aac', 'flac')
    channels = int(stream.get('channels') or 2)
    audio_options = (['-c:a', 'copy'] if copy_audio else
                     ['-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', f'{max(320, channels * 128)}k'])
    if codec == 'aac' and source.suffix.lower() == '.aac':
        audio_options += ['-bsf:a', 'aac_adtstoasc']
    if extension == '.m4a':
        audio_options += ['-movflags', '+faststart']
    metadata_options = []
    aliases = {'albumartist':'album_artist','tracknumber':'track','discnumber':'disc'}
    for name,value in stream['_metadata'].items():
        metadata_options += ['-metadata', f'{aliases.get(name,name)}={value}']
    try:
        result = subprocess.run([
            str(ffmpeg), '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
            *LOCAL_MEDIA_ARGS, '-i', str(source), '-map', '0:a:0', '-vn', '-map_metadata', '0',
            *audio_options, *metadata_options, str(target),
        ], capture_output=True, timeout=3600)
        if result.returncode or not target.is_file() or not target.stat().st_size:
            raise ValueError()
        output = probe_audio_stream(target, ffprobe)
        expected_codec = codec if copy_audio else 'aac'
        if output['codec_name'] != expected_codec:
            raise ValueError()
    except Exception:
        if target.is_file():
            target.unlink()
        raise AudioFormatError('Не удалось подготовить M4A для музыкального плеера. Попробуйте вариант MP3.') from None
    source.unlink()
    return str(target)
