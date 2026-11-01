import importlib.util
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_downloads import FakeBot, FakeYDL, ROOT
import download_pipeline as pipeline
from link_bundle import LinkBundle
from r2_storage import StorageError, StoredFile


class LinkBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def add(self, bundle, index, title='Track'):
        filename = self.root / f'{index}.mp3'
        filename.write_bytes(str(index).encode())
        bundle.add(filename, {'title': title, 'playlist': 'Album'})

    def test_album_zip_preserves_order_and_file_contents(self):
        bundle = LinkBundle(self.root, 10000)
        for index in (1, 2, 3):
            self.add(bundle, index, '../../Bad: title')
        archive, name = bundle.finalize()
        self.assertEqual(name, 'Album.zip')
        with zipfile.ZipFile(archive) as output:
            self.assertEqual([output.read(path) for path in output.namelist()], [b'1', b'2', b'3'])
            self.assertTrue(all('/' not in path and '\\' not in path for path in output.namelist()))

    def test_single_file_does_not_need_zip(self):
        bundle = LinkBundle(self.root, 10000)
        self.add(bundle, 1)
        filename, name = bundle.finalize()
        self.assertEqual(filename.suffix, '.mp3')
        self.assertEqual(name, 'Track.mp3')

    def test_progress_limit_stops_download_even_without_known_total_size(self):
        bundle = LinkBundle(self.root, 10)
        bundle.progress_hook({'status': 'downloading', 'filename': 'first', 'downloaded_bytes': 7})
        with self.assertRaises(pipeline.RequestAbortedError):
            bundle.progress_hook({'status': 'downloading', 'filename': 'second', 'downloaded_bytes': 4})

    def test_completed_album_size_limit_aborts_instead_of_continuing(self):
        bundle = LinkBundle(self.root, 1)
        self.add(bundle, 1)
        with self.assertRaises(pipeline.RequestAbortedError):
            self.add(bundle, 2)


class RecordingBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.links = []
        self.fail_link = False

    def send_message(self, chat_id, text, **kwargs):
        if 'disable_web_page_preview' in kwargs:
            if self.fail_link:
                raise RuntimeError('Telegram link delivery failed')
            self.links.append(kwargs['reply_markup'])
        return super().send_message(chat_id, text, **kwargs)


class LocalStorage:
    def __init__(self):
        self.settings = SimpleNamespace(max_file_bytes=2_000_000_000,
                                        max_storage_bytes=8_000_000_000,
                                        max_objects=200, ttl_seconds=21600)
        self.uploaded = []
        self.zip_entries = []
        self.revoke = Mock()
        self.fail_upload = False

    def check_connection(self):
        return {'bytes': 0, 'objects': 0}

    def upload(self, filename, name, progress_callback=None, user_id=None, user_limit_bytes=None):
        if self.fail_upload:
            raise StorageError('R2 offline')
        self.uploaded.append((name, Path(filename).stat().st_size))
        if progress_callback:
            progress_callback(Path(filename).stat().st_size, Path(filename).stat().st_size)
        if Path(filename).suffix == '.zip':
            with zipfile.ZipFile(filename) as archive:
                self.zip_entries = archive.namelist()
        return StoredFile('dl06/test', 'https://download.example.com/test',
                          9999999999, Path(filename).stat().st_size, name)


class LinkBotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = RecordingBot()
        config = SimpleNamespace(TOKEN='123:test', ADMIN_ID=1,
                                 FFMPEG_PATH=str(ROOT / 'ffmpeg-master-latest-win64-gpl-shared/bin/ffmpeg.exe'))
        spec = importlib.util.spec_from_file_location('dl06_links_test', ROOT / 'DL-06.py')
        cls.app = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'config': config}), patch('telebot.TeleBot', return_value=cls.bot):
            spec.loader.exec_module(cls.app)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.app.DOWNLOAD_FOLDER = self.temporary.name
        self.stats_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.stats_temporary.cleanup)
        self.app.TRAFFIC_STATE_FILE = str(Path(self.stats_temporary.name) / 'traffic.sqlite3')
        self.bot.sent.clear()
        self.bot.statuses.clear()
        self.bot.links.clear()
        self.bot.fail_link = False
        self.storage = LocalStorage()

    def run_link(self, url='https://soundcloud.com/user/sets/album'):
        with patch.object(pipeline, 'YoutubeDL', FakeYDL), \
                patch.object(self.app, 'get_link_storage', return_value=self.storage):
            self.app.download_content(url, 1, 'audio', 'link')
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_album_becomes_one_zip_and_one_link(self):
        FakeYDL.scenario = lambda ydl: {
            '_type': 'playlist', 'entries': [ydl.item(index) for index in (1, 2, 3)]}
        self.run_link()
        self.assertEqual(len(self.storage.uploaded), 1)
        self.assertEqual(len(self.storage.zip_entries), 3)
        self.assertEqual(len(self.bot.links), 1)
        self.assertFalse(self.bot.sent)
        self.assertIn('Файлов по ссылке: 3', self.bot.statuses[-1])

    def test_file_over_telegram_limit_is_uploaded_without_compression(self):
        def scenario(ydl):
            filename = Path(ydl.params['paths']['home']) / 'large.mp4'
            with filename.open('wb') as output:
                output.truncate(80_000_001)
            info = {'id': 'large', 'filepath': str(filename), 'title': 'Large video'}
            ydl.processor.callback(info)
            return info

        FakeYDL.scenario = scenario
        with patch.object(self.app, 'compress_media', side_effect=AssertionError('Must not compress')):
            self.run_link('https://example.com/video')
        self.assertEqual(self.storage.uploaded[0][1], 80_000_001)
        self.assertEqual(len(self.bot.links), 1)

    def test_upload_failure_still_removes_all_local_files(self):
        FakeYDL.scenario = lambda ydl: ydl.item(1)
        self.storage.fail_upload = True
        self.run_link()
        self.assertFalse(self.bot.links)
        self.assertIn('Файлов по ссылке: 0', self.bot.statuses[-1])

    def test_failed_telegram_link_revokes_uploaded_object(self):
        FakeYDL.scenario = lambda ydl: ydl.item(1)
        self.bot.fail_link = True
        self.run_link()
        self.storage.revoke.assert_called_once()
        self.assertIn('Файлов по ссылке: 0', self.bot.statuses[-1])

    def test_request_size_abort_stops_playlist_and_cleans_everything(self):
        self.storage.settings.max_file_bytes = 6
        attempts = []

        def scenario(ydl):
            entries = []
            for index in (1, 2, 3):
                attempts.append(index)
                entries.append(ydl.item(index))
            return {'_type': 'playlist', 'entries': entries}

        FakeYDL.scenario = scenario
        self.run_link()
        self.assertEqual(attempts, [1, 2])
        self.assertFalse(self.storage.uploaded)


if __name__ == '__main__':
    unittest.main()
