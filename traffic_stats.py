"""Persistent media byte counters; no URLs, credentials or user identifiers."""
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class TrafficStats:
    FIELDS = {'source_bytes', 'telegram_bytes', 'r2_bytes'}

    def __init__(self, filename, clock=time.time):
        self.filename = Path(filename)
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS traffic_totals (
                id INTEGER PRIMARY KEY CHECK(id=1), started_at REAL NOT NULL,
                source_bytes INTEGER NOT NULL DEFAULT 0,
                telegram_bytes INTEGER NOT NULL DEFAULT 0,
                r2_bytes INTEGER NOT NULL DEFAULT 0)''')
            db.execute('INSERT OR IGNORE INTO traffic_totals (id, started_at) VALUES (1, ?)',
                       (clock(),))

    def _connect(self):
        return sqlite3.connect(self.filename, timeout=30, isolation_level=None)

    def add(self, field, size):
        if field not in self.FIELDS or not isinstance(size, int) or size < 0:
            raise ValueError('Invalid traffic counter')
        if size:
            with closing(self._connect()) as db:
                db.execute(f'UPDATE traffic_totals SET {field}={field}+? WHERE id=1', (size,))

    def snapshot(self):
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute('SELECT * FROM traffic_totals WHERE id=1').fetchone())


class DownloadTraffic:
    """Count yt-dlp media bytes, including partial attempts, without counting final hooks twice."""
    def __init__(self, record, clock=time.monotonic):
        self.record = record
        self.clock = clock
        self.last_flush = clock()
        self.progress = {}
        self.pending = 0

    def observe(self, data):
        if data.get('status') not in ('downloading', 'finished'):
            return
        name = data.get('filename') or data.get('tmpfilename')
        size = data.get('downloaded_bytes')
        if not name or not isinstance(size, (int, float)) or size < 0:
            return
        size = int(size)
        previous = self.progress.get(name, 0)
        self.pending += size - previous if size >= previous else size
        self.progress[name] = size
        if data['status'] == 'finished' or self.clock() - self.last_flush >= 2:
            self.flush()

    def flush(self):
        if self.pending:
            self.record(self.pending)
            self.pending = 0
        self.last_flush = self.clock()
