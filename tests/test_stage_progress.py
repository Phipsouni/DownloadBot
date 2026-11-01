import threading
import unittest
from unittest.mock import Mock

from stage_progress import StageProgress, download_status, upload_status


class StageProgressTests(unittest.TestCase):
    def test_heartbeat_updates_unchanged_phase_and_stops_before_final_message(self):
        updates = []
        ready = threading.Event()
        def publish(text):
            updates.append(text)
            if len(updates) >= 2:
                ready.set()
        progress = StageProgress(publish, interval=0.02)
        progress.set('⚙️ Объединяю видео и звук…')
        progress.start()
        try:
            self.assertTrue(ready.wait(2))
        finally:
            progress.close()
        self.assertFalse(progress._thread.is_alive())
        self.assertTrue(all('Объединяю' in text and 'сек.' in text for text in updates))
        count = len(updates)
        progress.set('Позднее обновление')
        self.assertEqual(len(updates), count)

    def test_timer_does_not_restart_on_every_percentage_update(self):
        now = [0]
        progress = StageProgress(Mock(), clock=lambda: now[0])
        progress.set('10%', phase='upload')
        now[0] = 5
        progress.set('50%', phase='upload')
        self.assertIn('5 сек.', progress.render())
        progress.set('Готовлю ссылку', phase='link')
        self.assertIn('0 сек.', progress.render())

    def test_transient_publish_failure_does_not_kill_status_worker(self):
        ready = threading.Event()
        calls = []
        def publish(text):
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError('network')
            ready.set()
        progress = StageProgress(publish, interval=0.02)
        progress.start()
        try:
            self.assertTrue(ready.wait(2))
        finally:
            progress.close()
        self.assertGreaterEqual(len(calls), 2)

    def test_unknown_total_does_not_invent_zero_percent_and_upload_uses_cloud_name(self):
        text = download_status({'downloaded_bytes': 1234, 'info_dict': {'title': 'Test'}})
        self.assertIn('размер уточняется', text)
        self.assertNotIn('0.0%', text)
        text = download_status({'downloaded_bytes': 50, 'total_bytes': 100,
                                'info_dict': {'title': 'Track', 'playlist_index': 2, 'n_entries': 4}})
        self.assertIn('50.0%', text)
        self.assertIn('Файл 2 из 4', text)
        text = upload_status(50, 100, 'album.zip')
        self.assertIn('облачное хранилище: 50.0%', text)
        self.assertNotIn('R2', text)
