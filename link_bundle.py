"""Подготовка одного файла или ZIP альбома для режима ссылок."""

import os
import re
import zipfile
import unicodedata
from pathlib import Path

from download_pipeline import RequestAbortedError


def safe_name(name):
    name = unicodedata.normalize('NFC', str(name))
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', name).strip(' .')[:100]
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name):
        name = '_' + name
    return name or 'Без названия'


class LinkBundle:
    def __init__(self, request_folder, max_bytes, max_files=200):
        self.folder = Path(request_folder) / 'bundle'
        self.folder.mkdir()
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.files = []
        self.total_bytes = 0
        self.title = None
        self.transferred_bytes = 0
        self._progress = {}

    def progress_hook(self, data):
        if data.get('status') not in ('downloading', 'finished'):
            return
        filename = data.get('filename') or data.get('tmpfilename')
        if not filename:
            return
        downloaded = data.get('downloaded_bytes')
        if downloaded is None:
            return
        previous = self._progress.get(filename, 0)
        # Учитываем повторную загрузку одного имени после сброса прогресса.
        self.transferred_bytes += downloaded - previous if downloaded >= previous else downloaded
        self._progress[filename] = downloaded
        if self.transferred_bytes > self.max_bytes:
            raise RequestAbortedError('Достигнут общий лимит скачивания для одной ссылки.')

    def add(self, filename, info):
        size = Path(filename).stat().st_size
        if len(self.files) >= self.max_files or self.total_bytes + size > self.max_bytes:
            raise RequestAbortedError('Альбом превышает лимит размера или количества треков.')
        index = len(self.files) + 1
        title = info.get('track') or info.get('title') or f'Файл {index}'
        artist = info.get('artist') or ''
        display_name = f'{artist} — {title}' if artist else title
        destination = self.folder / f'{index:03d} - {safe_name(display_name)}{Path(filename).suffix}'
        os.replace(filename, destination)
        self.files.append(destination)
        self.total_bytes += size
        self.title = self.title or info.get('playlist') or info.get('album') or title

    def finalize(self, source_notes=None, force_archive=False):
        if not self.files:
            raise ValueError('Не удалось подготовить ни одного файла для ссылки.')
        if len(self.files) == 1 and not force_archive and not source_notes:
            path = self.files[0]
            return path, path.name.split(' - ', 1)[-1]
        archive = self.folder.parent / 'album.zip'
        # MP3/MP4 уже сжаты, ZIP_STORED сохраняет качество и не расходует CPU на повторное сжатие.
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as output:
            for path in self.files:
                output.write(path, arcname=path.name)
            if source_notes:
                output.writestr('ИСТОЧНИКИ.txt', source_notes.encode('utf-8'))
        if archive.stat().st_size > self.max_bytes:
            raise RequestAbortedError('ZIP альбома превышает лимит режима ссылок.')
        return archive, f'{safe_name(self.title or "Альбом")}.zip'
