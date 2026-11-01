"""Настоящие yt-dlp/ffmpeg; локальный HTTP-сервер, без Telegram и внешних сайтов."""

import functools
import http.server
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yt_dlp import YoutubeDL
from yt_dlp.extractor.common import InfoExtractor
import download_pipeline as pipeline
from traffic_stats import TrafficStats, DownloadTraffic


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class RealDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.request = self.downloads / 'request'
        self.request.mkdir()
        with wave.open(str(self.media / 'sample.wav'), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b'\0\0' * 4000)
        handler = functools.partial(QuietHandler, directory=str(self.media))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'

        class TestAlbumIE(InfoExtractor):
            _VALID_URL = r'dl06test:(?P<id>good|missing)'

            def _real_extract(self, url):
                missing = self._match_id(url) == 'missing'
                return self.playlist_result([
                    {'id': str(index), 'title': f'Track {index}', 'ext': 'wav',
                     'url': f'{base}/missing.wav' if missing and index == 2
                     else f'{base}/sample.wav'} for index in range(1, 4)], 'album')

        def make_downloader(options):
            downloader = YoutubeDL(options, auto_init=False)
            downloader.add_info_extractor(TestAlbumIE())
            downloader.add_default_info_extractors()
            return downloader

        self.patch = patch.object(pipeline, 'YoutubeDL', side_effect=make_downloader)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def download(self, url, deliver, progress_hook=None, postprocessor_hook=None):
        options = {
            'format': 'bestaudio/best', 'quiet': True, 'noprogress': True,
            'retries': 0, 'socket_timeout': 5,
            'ffmpeg_location': str(ROOT / 'ffmpeg-master-latest-win64-gpl-shared/bin/ffmpeg.exe'),
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'},
                {'key': 'FFmpegMetadata'},
            ],
            'progress_hooks': [progress_hook] if progress_hook else [],
            'postprocessor_hooks': [postprocessor_hook] if postprocessor_hook else [],
        }
        result = pipeline.download_and_deliver(
            url, options, deliver, [('anonymous', {})], Mock(),
            str(self.request), str(self.downloads))
        self.assertEqual(list(self.request.iterdir()), [])
        return result

    def test_real_postprocessing_sends_mp3_and_continues_after_http_404(self):
        sent = []

        def deliver(filename, info):
            path = Path(filename)
            self.assertEqual(path.suffix, '.mp3')
            self.assertGreater(path.stat().st_size, 0)
            for previous in sent:
                self.assertFalse(previous[1].exists())
            sent.append((info['id'], path))

        result = self.download('dl06test:missing', deliver)
        self.assertEqual([item[0] for item in sent], ['1', '3'])
        self.assertEqual((result.sent, result.failed), (2, 1))

    def test_real_media_progress_counts_source_bytes_once_before_mp3_conversion(self):
        store = TrafficStats(self.root / 'traffic.sqlite3')
        tracker = DownloadTraffic(lambda size: store.add('source_bytes', size))
        result = self.download('dl06test:good', Mock(), tracker.observe)
        tracker.flush()
        self.assertEqual(result.sent, 3)
        self.assertEqual(store.snapshot()['source_bytes'], (self.media / 'sample.wav').stat().st_size * 3)

    def test_real_postprocessor_hooks_announce_audio_and_metadata_processing(self):
        stages = []
        self.download('dl06test:good', Mock(), postprocessor_hook=stages.append)
        started = [entry['postprocessor'] for entry in stages if entry['status'] == 'started']
        self.assertTrue(any('ExtractAudio' in name for name in started))
        self.assertTrue(any('Metadata' in name for name in started))

    def test_real_album_continues_after_upload_exception(self):
        attempted = []

        def deliver(filename, info):
            attempted.append(info['id'])
            if info['id'] == '2':
                raise RuntimeError('upload failed')

        result = self.download('dl06test:good', deliver)
        self.assertEqual(attempted, ['1', '2', '3'])
        self.assertEqual((result.sent, result.failed), (2, 1))

    def test_real_progress_budget_aborts_and_cleans_partial_download(self):
        def limited_progress(data):
            if (data.get('downloaded_bytes') or 0) > 1000:
                raise pipeline.RequestAbortedError('Request budget exceeded')

        with self.assertRaises(pipeline.RequestAbortedError):
            self.download('dl06test:good', Mock(), progress_hook=limited_progress)
        self.assertEqual(list(self.request.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
