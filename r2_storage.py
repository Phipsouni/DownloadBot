"""Закрытые файлы Cloudflare R2, временные ссылки, квота и очистка."""

import hashlib
import json
import mimetypes
import re
import secrets
import sqlite3
import time
import threading
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit


class StorageError(Exception):
    pass


class StorageNotConfigured(StorageError):
    pass


class StorageQuotaError(StorageError):
    pass


@dataclass(frozen=True)
class R2Settings:
    endpoint_url: str
    bucket: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    ttl_seconds: int = 21600
    max_file_bytes: int = 2_000_000_000
    max_storage_bytes: int = 8_000_000_000
    max_objects: int = 200

    def __post_init__(self):
        parsed = urlsplit(self.endpoint_url)
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port
                or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or not re.fullmatch(r'[a-f0-9]{32}\.r2\.cloudflarestorage\.com', parsed.hostname or '')):
            raise StorageError('Некорректный HTTPS endpoint Cloudflare R2.')
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', self.bucket):
            raise StorageError('Некорректное имя bucket R2.')
        if not self.access_key_id or not self.secret_access_key:
            raise StorageNotConfigured('В настройках R2 отсутствуют ключи доступа.')
        if not 60 <= self.ttl_seconds <= 604800:
            raise StorageError('Срок ссылки должен составлять от минуты до 7 дней.')
        if not 1 <= self.max_file_bytes <= self.max_storage_bytes:
            raise StorageError('Лимит файла должен помещаться в квоту хранилища.')
        if not 1 <= self.max_objects <= 10000:
            raise StorageError('Некорректный лимит количества файлов.')

    @classmethod
    def from_file(cls, filename):
        try:
            data = json.loads(Path(filename).read_text(encoding='utf-8-sig'))
        except FileNotFoundError as error:
            raise StorageNotConfigured('R2 ещё не подключён. Нужен файл r2_config.json.') from error
        except (OSError, ValueError) as error:
            raise StorageError('Не удалось прочитать r2_config.json.') from error
        if not isinstance(data, dict):
            raise StorageError('r2_config.json должен содержать объект настроек.')
        if not data.get('enabled', False):
            raise StorageNotConfigured('Режим ссылок R2 пока выключен в r2_config.json.')
        try:
            fields = cls.__dataclass_fields__
            return cls(**{key: value for key, value in data.items() if key in fields})
        except (TypeError, ValueError) as error:
            raise StorageError('Проверьте параметры r2_config.json.') from error


@dataclass(frozen=True)
class StoredFile:
    key: str
    url: str = field(repr=False)
    expires_at: float
    size: int
    filename: str


def attachment_header(filename):
    filename = Path(filename).name.replace('\r', '').replace('\n', '')[:180]
    suffix = Path(filename).suffix
    return f'attachment; filename="download{suffix}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


def create_client(settings):
    try:
        import boto3
        from botocore.config import Config
    except ImportError as error:
        raise StorageError('Обновите зависимости через Install Requirments.bat.') from error
    return boto3.client(
        's3', endpoint_url=settings.endpoint_url, region_name='auto',
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        config=Config(signature_version='s3v4', connect_timeout=10, read_timeout=90,
                      retries={'max_attempts': 3, 'mode': 'standard'},
                      s3={'addressing_style': 'path'},
                      request_checksum_calculation='when_required',
                      response_checksum_validation='when_required'))


class R2Storage:
    def __init__(self, settings, state_file, client=None, clock=time.time):
        self.settings = settings
        self.client = client if client is not None else create_client(settings)
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.owner = hashlib.sha256(
            f'{settings.endpoint_url}/{settings.bucket}'.encode()).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS r2_objects (
                owner TEXT NOT NULL, object_key TEXT NOT NULL, size INTEGER NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(owner, object_key))''')
            connection.execute('BEGIN IMMEDIATE')
            try:
                columns = {row[1] for row in connection.execute('PRAGMA table_info(r2_objects)')}
                if 'user_id' not in columns:
                    connection.execute('ALTER TABLE r2_objects ADD COLUMN user_id TEXT')
                connection.execute('COMMIT')
            except BaseException:
                connection.execute('ROLLBACK')
                raise

    def _connect(self):
        return sqlite3.connect(self.state_file, timeout=30, isolation_level=None)

    def _remote_objects(self):
        objects = {}
        token = None
        while True:
            params = {'Bucket': self.settings.bucket, 'MaxKeys': 1000}
            if token:
                params['ContinuationToken'] = token
            page = self.client.list_objects_v2(**params)
            for item in page.get('Contents', []):
                objects[item['Key']] = int(item['Size'])
            if not page.get('IsTruncated'):
                return objects
            token = page.get('NextContinuationToken')
            if not token:
                raise StorageError('R2 не вернул следующую страницу списка файлов.')

    def _reserve(self, key, size, user_id=None, user_limit_bytes=None):
        remote = self._remote_objects()
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                rows = dict(connection.execute(
                    'SELECT object_key, size FROM r2_objects WHERE owner=?', (self.owner,)))
                untracked = {name: value for name, value in remote.items() if name not in rows}
                used = sum(rows.values()) + sum(untracked.values())
                if used + size > self.settings.max_storage_bytes:
                    raise StorageQuotaError('Временное хранилище заполнено. Дождитесь очистки файлов.')
                if len(rows) + len(untracked) >= self.settings.max_objects:
                    raise StorageQuotaError('Достигнут лимит количества файлов в хранилище.')
                if user_limit_bytes is not None:
                    if user_id is None:
                        raise StorageError('Не указан владелец облачной загрузки.')
                    personal_used = connection.execute(
                        'SELECT COALESCE(SUM(size), 0) FROM r2_objects WHERE owner=? AND user_id=?',
                        (self.owner, str(user_id))).fetchone()[0]
                    if personal_used + size > user_limit_bytes:
                        raise StorageQuotaError(
                            'В облачном хранилище для вас доступно 2 ГБ одновременно. '
                            f'Сейчас занято {personal_used / 1_000_000:.1f} МБ, '
                            f'для этого файла нужно {size / 1_000_000:.1f} МБ. '
                            'Дождитесь удаления предыдущих файлов и повторите запрос.')
                now = self.clock()
                connection.execute('''INSERT INTO r2_objects
                    (owner, object_key, size, created_at, expires_at, status, user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (self.owner, key, size, now, now + 86400, 'reserved',
                     str(user_id) if user_id is not None else None))
                connection.execute('COMMIT')
            except BaseException:
                connection.execute('ROLLBACK')
                raise

    def _forget(self, key):
        with closing(self._connect()) as connection:
            connection.execute('DELETE FROM r2_objects WHERE owner=? AND object_key=?',
                               (self.owner, key))

    def _remove(self, key, incomplete=False):
        if not key.startswith('dl06/'):
            raise StorageError('Отказ удаления объекта вне каталога бота.')
        if incomplete:
            token_key = token_upload = None
            while True:
                params = {'Bucket': self.settings.bucket, 'Prefix': key}
                if token_key:
                    params.update(KeyMarker=token_key, UploadIdMarker=token_upload)
                page = self.client.list_multipart_uploads(**params)
                for upload in page.get('Uploads', []):
                    if upload['Key'] == key:
                        self.client.abort_multipart_upload(
                            Bucket=self.settings.bucket, Key=key, UploadId=upload['UploadId'])
                if not page.get('IsTruncated'):
                    break
                token_key, token_upload = page['NextKeyMarker'], page['NextUploadIdMarker']
        self.client.delete_object(Bucket=self.settings.bucket, Key=key)
        self._forget(key)

    def cleanup_expired(self):
        with closing(self._connect()) as connection:
            rows = list(connection.execute(
                'SELECT object_key, status FROM r2_objects WHERE owner=? AND expires_at<=?',
                (self.owner, self.clock())))
        removed = 0
        for key, status in rows:
            try:
                self._remove(key, incomplete=status == 'reserved')
                removed += 1
            except Exception:
                # При сетевой ошибке запись и занятая квота сохраняются для следующего повтора.
                print('R2: не удалось удалить временный объект, очистка будет повторена.')
        return removed

    def upload(self, filename, download_name=None, progress_callback=None,
               user_id=None, user_limit_bytes=None):
        path = Path(filename)
        size = path.stat().st_size
        if size <= 0 or size > self.settings.max_file_bytes:
            raise StorageQuotaError('Размер файла превышает лимит режима ссылок или файл пуст.')
        name = Path(download_name or path.name).name
        key = f'dl06/{secrets.token_hex(24)}{path.suffix.lower()}'
        reserved = False
        try:
            self.cleanup_expired()
            self._reserve(key, size, user_id, user_limit_bytes)
            reserved = True
            from boto3.s3.transfer import TransferConfig
            transferred = 0
            progress_lock = threading.Lock()

            def report_progress(delta):
                nonlocal transferred
                with progress_lock:
                    transferred = max(0, min(size, transferred + delta))
                    if progress_callback is not None:
                        try:
                            progress_callback(transferred, size)
                        except Exception:
                            print('Не удалось обновить прогресс облачной загрузки.')

            self.client.upload_file(
                str(path), self.settings.bucket, key,
                ExtraArgs={'ContentType': mimetypes.guess_type(name)[0] or 'application/octet-stream',
                           'ContentDisposition': attachment_header(name)},
                Config=TransferConfig(multipart_threshold=32 * 1024**2,
                                      multipart_chunksize=32 * 1024**2, max_concurrency=2),
                Callback=report_progress if progress_callback is not None else None)
            expires_at = self.clock() + self.settings.ttl_seconds
            with closing(self._connect()) as connection:
                connection.execute(
                    'UPDATE r2_objects SET status=?, expires_at=? WHERE owner=? AND object_key=?',
                    ('ready', expires_at, self.owner, key))
            url = self.client.generate_presigned_url(
                'get_object', Params={'Bucket': self.settings.bucket, 'Key': key,
                                     'ResponseContentDisposition': attachment_header(name)},
                ExpiresIn=self.settings.ttl_seconds)
            if (urlsplit(url).scheme != 'https'
                    or urlsplit(url).hostname != urlsplit(self.settings.endpoint_url).hostname):
                raise StorageError('R2 вернул неожиданный адрес скачивания.')
            return StoredFile(key, url, expires_at, size, name)
        except Exception as error:
            if reserved:
                try:
                    self._remove(key, incomplete=True)
                except Exception:
                    print('R2: очистка прерванной загрузки будет повторена позже.')
            if isinstance(error, StorageError):
                raise
            # Не публикуем исходный SDK exception: он может содержать подписанные URL.
            raise StorageError('Не удалось загрузить файл в R2. Проверьте подключение и права ключа.') from None

    def check_connection(self):
        try:
            self.client.head_bucket(Bucket=self.settings.bucket)
            objects = self._remote_objects()
        except Exception:
            raise StorageError('R2 недоступен: проверьте endpoint, bucket и права ключа.') from None
        return {'objects': len(objects), 'bytes': sum(objects.values())}

    def status_snapshot(self):
        """Read actual objects and reconcile link lifetimes with the persistent ledger."""
        try:
            self.client.head_bucket(Bucket=self.settings.bucket)
            remote = self._remote_objects()
            with closing(self._connect()) as connection:
                rows = list(connection.execute(
                    'SELECT object_key, expires_at, status FROM r2_objects WHERE owner=?',
                    (self.owner,)))
            now = self.clock()
            active = expired = 0
            known = set()
            upcoming = {}
            for key, expires, status in rows:
                if key not in remote:
                    continue
                known.add(key)
                if status == 'ready' and expires > now:
                    active += 1
                if expires <= now:
                    expired += 1
                else:
                    minute = int(expires // 60)
                    group = upcoming.setdefault(minute, {'at': expires, 'count': 0, 'bytes': 0})
                    group['at'] = min(group['at'], expires)
                    group['count'] += 1
                    group['bytes'] += remote[key]
            return {'objects': len(remote), 'bytes': sum(remote.values()),
                    'limit': self.settings.max_storage_bytes, 'active_links': active,
                    'expired': expired, 'untracked': len(remote.keys() - known),
                    'upcoming': [upcoming[key] for key in sorted(upcoming)[:5]]}
        except Exception:
            raise StorageError('Не удалось получить состояние R2: проверьте сеть и права ключа.') from None

    def revoke(self, stored_file):
        """Удаление файла, ссылка на который не была доставлена пользователю."""
        try:
            self._remove(stored_file.key)
        except Exception:
            raise StorageError('Не удалось удалить объект R2; очистка повторится по сроку хранения.') from None
