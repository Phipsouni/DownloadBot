"""A single paced status publisher per request, including phases without byte progress."""
import threading
import time

from admin_reports import volume


class StageProgress:
    def __init__(self, publish, interval=2.5, clock=time.monotonic):
        self.publish = publish
        self.interval = interval
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._text = '🔎 Получаю сведения о файле…'
        self._phase = 'metadata'
        self._since = clock()

    def set(self, text, phase=None):
        with self._lock:
            phase = phase or text
            if phase != self._phase:
                self._phase = phase
                self._since = self.clock()
            self._text = text

    def render(self):
        with self._lock:
            elapsed = max(0, int(self.clock() - self._since))
            return f'{self._text}\n⏱ Этот этап: {elapsed} сек.'

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        delay = self.interval
        while not self._stop.wait(delay):
            delay = self.interval
            try:
                self.publish(self.render())
            except Exception as error:
                retry_after = ((getattr(error, 'result_json', None) or {})
                               .get('parameters', {}).get('retry_after'))
                if isinstance(retry_after, (int, float)) and retry_after > 0:
                    delay = max(delay, retry_after + 1)
                print('Не удалось обновить прогресс; обновление будет повторено.')

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()


def download_status(data, position=None):
    info = data.get('info_dict') or {}
    title = str(info.get('title') or 'Файл')[:200]
    index = info.get('playlist_index')
    count = info.get('n_entries') or info.get('playlist_count')
    position = f'Файл {index} из {count}' if index and count else position
    downloaded = data.get('downloaded_bytes') or 0
    total = data.get('total_bytes') or data.get('total_bytes_estimate')
    estimated = not data.get('total_bytes') and bool(total)
    if total and total > 0:
        percent = min(100, max(0, downloaded / total * 100))
        detail = f'{"≈" if estimated else ""}{percent:.1f}% · {volume(int(downloaded))} / {volume(int(total))}'
    else:
        detail = f'{volume(int(downloaded))} · размер уточняется'
    speed = data.get('speed')
    if speed and speed > 0:
        detail += f' · {volume(int(speed))}/с'
    eta = data.get('eta')
    if isinstance(eta, (int, float)) and eta >= 0:
        detail += f' · осталось ~{int(eta)} сек.'
    track = f'\n{position}' if position else ''
    return f'⬇️ Скачивание: {detail}\n{title}{track}'


def upload_status(downloaded, total, name):
    # The final acknowledgement may still be pending after the last data chunk.
    percent = min(99.9, max(0, downloaded / total * 100)) if total else 0
    return (f'☁️ Загрузка в облачное хранилище: {percent:.1f}%\n'
            f'{str(name)[:200]}\n{volume(int(downloaded))} / {volume(int(total))}')
