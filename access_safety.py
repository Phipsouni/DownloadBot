"""Small, bounded, thread-safe gates for work queues and notification spam."""
import threading
import time
from collections import OrderedDict, deque


class RateGate:
    def __init__(self, capacity, period, *, max_keys=1000, clock=time.monotonic):
        self.capacity, self.period = capacity, period
        self.max_keys, self.clock = max_keys, clock
        self.events = OrderedDict()
        self.lock = threading.Lock()

    def allow(self, key):
        now = self.clock()
        with self.lock:
            events = self.events.pop(str(key), deque())
            while events and events[0] <= now - self.period:
                events.popleft()
            allowed = len(events) < self.capacity
            if allowed:
                events.append(now)
            self.events[str(key)] = events
            while len(self.events) > self.max_keys:
                self.events.popitem(last=False)
            return allowed


def telegram_user_id(value):
    text = str(value)
    if not text.isascii() or not text.isdecimal() or not 0 < int(text) < 2 ** 52:
        raise ValueError('Нужен положительный числовой ID пользователя Telegram.')
    return str(int(text))
