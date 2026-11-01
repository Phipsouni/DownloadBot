"""Short-lived, owner-bound inline selections; callbacks contain no source URLs."""
import secrets
import threading
import time
from dataclasses import dataclass, replace


class ChoiceError(Exception):
    pass


@dataclass
class PendingChoice:
    key: str
    user_id: str
    chat_id: int
    url: str
    created_at: float
    message_id: int | None = None
    album: bool = False
    title: str = ''
    count: int | None = None
    tracks: list | None = None
    audio_format: str | None = None


class ChoiceStore:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.items = {}

    def create(self, user_id, chat_id, url):
        with self.lock:
            now = self.clock()
            self.items = {key: item for key, item in self.items.items() if now-item.created_at < 900}
            if len(self.items) >= 50 or sum(item.user_id == str(user_id) for item in self.items.values()) >= 2:
                raise ChoiceError('Сначала завершите выбор для предыдущей ссылки.')
            item = PendingChoice(secrets.token_hex(10), str(user_id), chat_id, url, now)
            self.items[item.key] = item
            return replace(item)

    def prepare(self, key, *, album, title, count, tracks=None):
        with self.lock:
            item = self.items.get(key)
            if item is None:
                return None
            item.album, item.title, item.count, item.tracks = album, title, count, tracks
            return replace(item)

    def bind(self, key, message_id):
        with self.lock:
            if key in self.items:
                self.items[key].message_id = message_id

    def discard(self, key):
        with self.lock:
            self.items.pop(key, None)

    def choose(self, key, user_id, chat_id, message_id, action):
        with self.lock:
            item = self.items.get(key)
            if item is None or self.clock()-item.created_at >= 900:
                self.items.pop(key, None)
                raise ChoiceError('Выбор устарел. Отправьте ссылку заново.')
            if (item.user_id != str(user_id) or item.chat_id != chat_id
                    or item.message_id != message_id):
                raise ChoiceError('Этот выбор доступен только отправителю ссылки.')
            if action == 'cancel':
                self.items.pop(key)
                return replace(item), 'cancel'
            if action in ('original', 'mp3') and item.audio_format is None:
                item.audio_format = action
                if item.album:
                    return replace(item), 'delivery'
                self.items.pop(key)
                return replace(item), 'auto'
            if action in ('auto', 'zip') and item.album and item.audio_format is not None:
                self.items.pop(key)
                return replace(item), action
            raise ChoiceError('Этот выбор уже обработан.')
