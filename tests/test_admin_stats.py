import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import telebot

from test_downloads import ROOT, FakeYDL
from test_links import LocalStorage
from test_r2_storage import ENDPOINT, FakeS3
import cloudflare_analytics as analytics
import download_pipeline as pipeline
from admin_reports import storage_report
from r2_storage import R2Settings, R2Storage, StorageError
from traffic_stats import TrafficStats, DownloadTraffic


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / 'traffic.sqlite3'

    def test_atomic_counters_survive_concurrent_writes_and_restart(self):
        store = TrafficStats(self.file, clock=lambda: 100)
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(lambda _: store.add('source_bytes', 17), range(50)))
        store.add('r2_bytes', 70)
        restarted = TrafficStats(self.file, clock=lambda: 200)
        self.assertEqual(restarted.snapshot()['source_bytes'], 850)
        self.assertEqual(restarted.snapshot()['r2_bytes'], 70)
        self.assertEqual(restarted.snapshot()['started_at'], 100)

    def test_download_progress_handles_formats_retries_partial_and_duplicate_final(self):
        recorded = []
        tracker = DownloadTraffic(recorded.append)
        for name, status, size in [('video', 'downloading', 30), ('video', 'finished', 100),
                                   ('video', 'finished', 100), ('audio', 'finished', 20),
                                   ('retry', 'downloading', 15), ('retry', 'downloading', 5),
                                   ('retry', 'downloading', 10)]:
            tracker.observe({'filename': name, 'status': status, 'downloaded_bytes': size})
        tracker.flush()
        tracker.flush()
        self.assertEqual(sum(recorded), 145)

    def test_progress_is_checkpointed_before_request_finishes(self):
        now = [0]
        recorded = []
        tracker = DownloadTraffic(recorded.append, clock=lambda: now[0])
        tracker.observe({'status': 'downloading', 'filename': 'f', 'downloaded_bytes': 10})
        now[0] = 3
        tracker.observe({'status': 'downloading', 'filename': 'f', 'downloaded_bytes': 20})
        self.assertEqual(recorded, [20])


class StorageSnapshotTests(unittest.TestCase):
    def test_links_are_reconciled_with_remote_files_expiry_and_account(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = R2Settings(ENDPOINT, 'dl06-downloads', 'access', 'secret')
            client = FakeS3()
            store = R2Storage(settings, Path(directory) / 'state.sqlite3', client, clock=lambda: 1000)
            client.objects = {'dl06/active': 10, 'dl06/expired': 20, 'foreign': 30}
            with closing(sqlite3.connect(store.state_file)) as db:
                for key, expiry in [('active', 1300), ('expired', 999), ('gone', 1400)]:
                    db.execute('''INSERT INTO r2_objects
                               (owner, object_key, size, created_at, expires_at, status)
                               VALUES (?, ?, ?, ?, ?, ?)''',
                               (store.owner, 'dl06/' + key, 10, 900, expiry, 'ready'))
                db.commit()
            snapshot = store.status_snapshot()
            self.assertEqual((snapshot['objects'], snapshot['bytes']), (3, 60))
            self.assertEqual(snapshot['active_links'], 1)
            self.assertEqual(snapshot['expired'], 1)
            self.assertEqual(snapshot['untracked'], 1)
            self.assertEqual(snapshot['upcoming'], [{'at': 1300, 'count': 1, 'bytes': 10}])

    def test_storage_failure_is_not_reported_as_empty_or_active(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeS3()
            client.head_bucket = Mock(side_effect=RuntimeError('secret-url'))
            store = R2Storage(R2Settings(ENDPOINT, 'dl06-downloads', 'a', 's'),
                              Path(directory) / 'state.sqlite3', client)
            with self.assertRaises(StorageError) as failure:
                store.status_snapshot()
            text = storage_report(error=str(failure.exception))
            self.assertIn('🔴 Не активен', text)
            self.assertNotIn('secret-url', text)
            self.assertNotIn('Файлов: 0', text)


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / 'analytics.json'
        self.file.write_text(json.dumps({'account_id': 'a' * 32, 'api_token': 'test-bearer-secret'}))
        self.settings = R2Settings(ENDPOINT, 'dl06-downloads', 's3-access', 's3-secret')
        analytics._cache.clear()

    def response(self, rows):
        return Mock(json=lambda: {'data': {'viewer': {'accounts': [
            {'r2OperationsAdaptiveGroups': rows}]}}})

    def test_queries_moscow_calendar_month_and_bucket_and_caches_results(self):
        rows = [{'dimensions': {'actionType': action}, 'sum': {'requests': n}}
                for action, n in [('PutObject', 2), ('GetObject', 3), ('DeleteObject', 4), ('NewOperation', 5)]]
        post = Mock(return_value=self.response(rows))
        now = datetime(2026, 11, 1, 0, 30, tzinfo=analytics.MOSCOW)
        result = analytics.monthly_operations(self.file, self.settings, now=now, post=post)
        self.assertEqual([result[k] for k in ('total', 'a', 'b', 'free', 'other')], [14, 2, 3, 4, 5])
        self.assertEqual(post.call_args.kwargs['json']['variables']['start'], '2026-10-31T21:00:00+00:00')
        self.assertEqual(post.call_args.kwargs['json']['variables']['bucket'], 'dl06-downloads')
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer test-bearer-secret')
        analytics.monthly_operations(self.file, self.settings, now=now, post=post)
        self.assertEqual(post.call_count, 1)

    def test_upstream_errors_and_wrong_account_never_become_zero_or_leak_secrets(self):
        post = Mock(return_value=Mock(json=lambda: {'errors': [{'message': 'test-bearer-secret'}]}))
        with self.assertRaises(analytics.AnalyticsError) as failure:
            analytics.monthly_operations(self.file, self.settings, post=post)
        self.assertNotIn('test-bearer-secret', str(failure.exception))
        wrong = R2Settings('https://' + 'b' * 32 + '.r2.cloudflarestorage.com', 'dl06-downloads', 'a', 's')
        post.reset_mock()
        with self.assertRaises(analytics.AnalyticsError):
            analytics.monthly_operations(self.file, wrong, post=post)
        post.assert_not_called()


class AdminCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = telebot.TeleBot('123:test', threaded=False)
        config = SimpleNamespace(TOKEN='123:test', ADMIN_ID=1, FFMPEG_PATH='ffmpeg')
        spec = importlib.util.spec_from_file_location('dl06_admin_test', ROOT / 'DL-06.py')
        cls.app = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'config': config}), patch('telebot.TeleBot', return_value=cls.bot):
            spec.loader.exec_module(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.app.TRAFFIC_STATE_FILE = str(self.folder / 'traffic.sqlite3')
        self.app.DOWNLOAD_FOLDER = str(self.folder / 'downloads')
        Path(self.app.DOWNLOAD_FOLDER).mkdir()
        self.app.ALLOWED_USER_IDS = {'1', '2'}
        for name in ('reply_to', 'send_message', 'edit_message_text', 'send_audio', 'send_video'):
            patcher = patch.object(self.bot, name, return_value=SimpleNamespace(message_id=1))
            patcher.start()
            self.addCleanup(patcher.stop)

    def dispatch(self, command, user=1, chat_type='private'):
        message = telebot.types.Message.de_json({
            'message_id': 10, 'date': 1, 'text': command,
            'from': {'id': user, 'is_bot': False, 'first_name': 'Test'},
            'chat': {'id': user if chat_type == 'private' else -10, 'type': chat_type},
            'entities': ([{'type': 'bot_command', 'offset': 0, 'length': len(command)}]
                         if command.startswith('/') else [])})
        self.bot.process_new_messages([message])
        return self.bot.reply_to.call_args.args[1]

    def test_both_commands_deny_allowed_and_unlisted_nonadmins_before_any_data_access(self):
        with patch.object(self.app, 'get_link_storage') as storage, \
                patch.object(self.app, 'get_traffic_store') as traffic:
            for user in (2, 999):
                for command in ('/storage', '/stats', 'Инфо', '⬅️ Инфо', 'Пользователи',
                                'Статистика', 'Хранилище R2', 'Добавить пользователя'):
                    self.assertIn('только администратору', self.dispatch(command, user=user))
            storage.assert_not_called()
            traffic.assert_not_called()

    def test_admin_group_commands_do_not_disclose_reports(self):
        with patch.object(self.app, 'get_link_storage') as storage, \
                patch.object(self.app, 'get_traffic_store') as traffic:
            for command in ('/storage', '/stats', 'Инфо', 'Пользователи', 'Статистика',
                            'Хранилище R2', 'Добавить пользователя'):
                self.assertIn('личном чате', self.dispatch(command, chat_type='supergroup'))
            storage.assert_not_called()
            traffic.assert_not_called()

    def test_admin_storage_shows_real_snapshot_and_analytics(self):
        storage = Mock(settings=SimpleNamespace())
        storage.status_snapshot.return_value = {'bytes': 123, 'limit': 8000000000, 'objects': 2,
                                                'active_links': 1, 'expired': 0, 'untracked': 0,
                                                'upcoming': [{'at': 2000, 'count': 1, 'bytes': 23}]}
        operations = {'total': 12, 'a': 3, 'b': 9, 'free': 0, 'other': 0,
                      'month': '10.2026', 'as_of': 1000}
        with patch.object(self.app, 'get_link_storage', return_value=storage), \
                patch.object(self.app, 'monthly_operations', return_value=operations):
            text = self.dispatch('/storage')
            self.assertEqual(self.dispatch('Хранилище R2'), text)
        self.assertIn('🟢 Активен', text)
        self.assertIn('Файлов: 2', text)
        self.assertIn('Активных ссылок: 1', text)
        self.assertIn('Класс A: 3', text)
        self.assertIn('/ 1 000 000 бесплатно в месяц', text)
        self.assertIn('/ 10 000 000 бесплатно в месяц', text)
        self.assertIn('Занято: 123 Б / 10 ГБ', text)
        self.assertIn('Рабочий лимит бота: 8.00 ГБ', text)

    def test_info_navigation_and_admin_only_keyboard(self):
        def buttons():
            markup = json.loads(self.bot.reply_to.call_args.kwargs['reply_markup'].to_json())
            return [button['text'] for row in markup['keyboard'] for button in row]
        text = self.dispatch('/start')
        self.assertNotIn('Администратору:', text)
        self.assertIn('Инфо', buttons())
        self.assertNotIn('Добавить пользователя', buttons())
        self.assertNotIn('Пользователи', buttons())
        self.dispatch('Инфо')
        self.assertEqual(buttons(), ['Пользователи', 'Статистика', 'Хранилище R2', '⬅️ Главное меню'])
        self.dispatch('Пользователи')
        self.assertEqual(buttons(), ['Добавить пользователя', '⬅️ Инфо'])
        self.dispatch('⬅️ Инфо')
        self.assertIn('Хранилище R2', buttons())
        self.dispatch('⬅️ Главное меню')
        self.assertIn('Инфо', buttons())
        self.dispatch('/start', user=2)
        self.assertNotIn('Инфо', buttons())

    def test_admin_storage_offline_and_stats_db_errors_are_explicit(self):
        with patch.object(self.app, 'get_link_storage', side_effect=StorageError('Нет подключения')):
            self.assertIn('🔴 Не активен', self.dispatch('/storage'))
        with patch.object(self.app, 'get_traffic_store', side_effect=RuntimeError('secret')):
            text = self.dispatch('/stats')
        self.assertIn('недоступна', text)
        self.assertNotIn('secret', text)

    def test_stats_survive_reopening_and_telegram_failed_send_is_not_counted(self):
        store = self.app.get_traffic_store()
        store.add('source_bytes', 200)
        filename = self.folder / 'audio.mp3'
        filename.write_bytes(b'audio')
        self.bot.send_audio.side_effect = RuntimeError('send failed')
        with self.assertRaises(RuntimeError):
            self.app.send_downloaded_file(str(filename), {'title': 'Track'}, 1, True, Mock(), self.folder)
        self.assertEqual(self.app.get_traffic_store().snapshot()['telegram_bytes'], 0)
        self.bot.send_audio.side_effect = None
        self.app.send_downloaded_file(str(filename), {'title': 'Track'}, 1, True, Mock(), self.folder)
        self.assertEqual(self.app.get_traffic_store().snapshot()['telegram_bytes'], 5)
        text = self.dispatch('/stats')
        self.assertEqual(self.dispatch('Статистика'), text)
        self.assertIn('на ПК: 200 Б', text)
        self.assertIn('Telegram: 5 Б', text)

    def test_r2_uploaded_bytes_count_even_if_telegram_link_fails(self):
        storage = LocalStorage()
        def send_message(chat_id, text, **kwargs):
            if kwargs.get('reply_markup'):
                raise RuntimeError('link delivery failed')
            return SimpleNamespace(message_id=1)
        self.bot.send_message.side_effect = send_message
        def scenario(ydl):
            hook = ydl.params['progress_hooks'][0]
            hook({'status': 'finished', 'filename': str(Path(ydl.params['paths']['home']) / '1.mp3'),
                  'downloaded_bytes': 5})
            return ydl.item(1)
        FakeYDL.scenario = scenario
        with patch.object(pipeline, 'YoutubeDL', FakeYDL), \
                patch.object(self.app, 'get_link_storage', return_value=storage):
            self.app.download_content('https://soundcloud.com/artist/song', 1, 'audio', 'link')
        totals = self.app.get_traffic_store().snapshot()
        self.assertEqual([totals[k] for k in ('source_bytes', 'r2_bytes', 'telegram_bytes')], [5, 5, 0])
        storage.revoke.assert_called_once()
        self.assertEqual(list(Path(self.app.DOWNLOAD_FOLDER).iterdir()), [])

    def test_partial_download_is_counted_and_flushed_on_error(self):
        def scenario(ydl):
            ydl.params['progress_hooks'][0]({'status': 'downloading', 'filename': 'partial',
                                           'downloaded_bytes': 12})
            raise RuntimeError('network failed')
        FakeYDL.scenario = scenario
        with patch.object(pipeline, 'YoutubeDL', FakeYDL):
            self.app.download_content('https://soundcloud.com/artist/song', 1, 'audio')
        totals = self.app.get_traffic_store().snapshot()
        self.assertEqual(totals['source_bytes'], 12)
        self.assertEqual(totals['telegram_bytes'], 0)

    def test_failed_counter_write_does_not_repeat_successful_telegram_delivery(self):
        filename = self.folder / 'audio.mp3'
        filename.write_bytes(b'audio')
        with patch.object(self.app, 'get_traffic_store', side_effect=RuntimeError('disk full')):
            self.app.send_downloaded_file(str(filename), {'title': 'Track'}, 1, True, Mock(), self.folder)
        self.bot.send_audio.assert_called_once()

    def test_direct_telegram_sender_never_compresses_audio_to_fit(self):
        source = self.folder / 'source.mp3'
        source.write_bytes(b'0123456789')
        compression = Path(self.app.DOWNLOAD_FOLDER) / 'compressed'
        compression.mkdir()
        output = compression / 'compressed.mp3'
        output.write_bytes(b'mp3')
        with patch.object(self.app, 'MAX_FILE_SIZE_BYTES', 5), \
                patch.object(self.app, 'compress_media') as compress:
            with self.assertRaises(ValueError):
                self.app.send_downloaded_file(str(source), {'title': 'Track'}, 1, True, Mock(), self.folder)
        compress.assert_not_called()
        self.assertEqual(self.app.get_traffic_store().snapshot()['telegram_bytes'], 0)


if __name__ == '__main__':
    unittest.main()
