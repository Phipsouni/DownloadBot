r"""Запуск: .venv\Scripts\python.exe -m unittest discover -s tests -v"""

import importlib.util
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cookie_scope
import download_pipeline as pipeline
import music_sources


class FakeYDL:
    scenario = None

    def __init__(self, params):
        self.params = params
        self.cookiejar = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def add_post_processor(self, processor, when):
        assert when == 'after_move'
        self.processor = processor

    def extract_info(self, url, download=True):
        return type(self).scenario(self)

    def item(self, index, identifier=None):
        folder = Path(self.params['paths']['home'])
        filename = folder / f'{index}.mp3'
        filename.write_bytes(b'audio')
        (folder / f'{index}.part').write_bytes(b'partial')
        info = {'id': identifier or str(index), 'title': f'Track {index}',
                'playlist_id': 'album', 'playlist_index': index, 'filepath': str(filename)}
        self.processor.callback(info)
        return info


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.request = self.root / 'request'
        self.request.mkdir()
        self.mock_ydl = patch.object(pipeline, 'YoutubeDL', FakeYDL)
        self.mock_ydl.start()
        self.addCleanup(self.mock_ydl.stop)
        self.addCleanup(self.temporary.cleanup)

    def run_download(self, on_file, sources=None,
                     url='https://soundcloud.com/artist/sets/album', on_retry=None):
        report = pipeline.download_and_deliver(
            url, {}, on_file, sources or [('anonymous', {})], Mock(),
            str(self.request), str(self.root), on_retry=on_retry)
        self.assertEqual(list(self.request.iterdir()), [])
        return report

    def test_album_delivers_each_file_before_downloading_the_next(self):
        events = []

        def scenario(ydl):
            entries = []
            for index in range(1, 4):
                events.append(f'download {index}')
                entries.append(ydl.item(index))
            return {'_type': 'playlist', 'entries': entries}

        FakeYDL.scenario = scenario
        report = self.run_download(lambda filename, info: events.append(f'send {info["id"]}'))
        self.assertEqual(events, ['download 1', 'send 1', 'download 2', 'send 2',
                                  'download 3', 'send 3'])
        self.assertEqual((report.sent, report.failed), (3, 0))

    def test_album_continues_past_drm_and_reports_the_actual_reason(self):
        def scenario(ydl):
            ydl.params['logger'].error('ERROR: [soundcloud] 1: This video is DRM protected')
            return {'_type':'playlist','entries':[None,ydl.item(2)]}
        FakeYDL.scenario = scenario
        sent = []
        report = self.run_download(lambda filename,info:sent.append(info['id']))
        self.assertEqual(sent,['2'])
        self.assertEqual((report.sent,report.failed),(1,1))
        self.assertIn('DRM',report.errors[0])
        self.assertIn('yt-dlp не поддерживает',report.errors[0])

    def test_telegram_failure_does_not_stop_album_and_cleans_partial_files(self):
        FakeYDL.scenario = lambda ydl: {
            '_type': 'playlist', 'entries': [ydl.item(index) for index in range(1, 4)]}
        sent = []

        def deliver(filename, info):
            self.assertTrue(Path(filename).exists())
            if info['id'] == '2':
                raise RuntimeError('Telegram upload failed')
            sent.append(info['id'])

        report = self.run_download(deliver)
        self.assertEqual(sent, ['1', '3'])
        self.assertEqual((report.sent, report.failed), (2, 1))
        self.assertIn('Telegram upload failed', report.errors[0])

    def test_exception_before_filename_is_known_cleans_everything(self):
        def scenario(ydl):
            (Path(ydl.params['paths']['home']) / 'orphan.webm.part').write_bytes(b'partial')
            raise RuntimeError('download interrupted')

        FakeYDL.scenario = scenario
        report = self.run_download(Mock())
        self.assertEqual((report.sent, report.failed), (0, 1))

    def test_auth_retry_does_not_resend_already_delivered_tracks(self):
        attempts = []

        def scenario(ydl):
            attempts.append(ydl.cookiejar)
            first = ydl.item(1)
            if ydl.cookiejar is None:
                ydl.params['logger'].error('HTTP Error 403: cookies required')
                return {'_type': 'playlist', 'entries': [first, None]}
            return {'_type': 'playlist', 'entries': [first, ydl.item(2)]}

        FakeYDL.scenario = scenario
        sent = []
        report = self.run_download(lambda filename, info: sent.append(info['id']), [
            ('anonymous', {}), ('cookies', {'_scoped_cookie_file': 'test.txt',
                                         '_cookie_site': 'youtube'})])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sent, ['1', '2'])
        self.assertEqual((report.sent, report.failed), (2, 0))
        self.assertFalse(report.errors)

    def test_nested_playlist_and_oversize_skip_are_counted(self):
        FakeYDL.scenario = lambda ydl: {
            '_type': 'playlist', 'entries': [
                {'_type': 'playlist', 'entries': [ydl.item(1), None]},
                {'id': 'oversize', 'title': 'Too large'}]}
        report = self.run_download(Mock())
        self.assertEqual((report.sent, report.failed), (1, 2))

    def test_repeated_track_at_different_positions_is_preserved(self):
        FakeYDL.scenario = lambda ydl: {
            '_type': 'playlist', 'entries': [ydl.item(1, 'same'), ydl.item(2, 'same')]}
        report = self.run_download(Mock())
        self.assertEqual((report.sent, report.failed), (2, 0))

    def test_cleanup_refuses_download_root_or_outside_folder(self):
        for path in (self.root, self.root.parent):
            with self.assertRaises(ValueError):
                pipeline.cleanup_folder(path, self.root)
        self.assertTrue(self.root.is_dir())

    def test_terminal_colors_are_removed(self):
        self.assertEqual(pipeline.plain_error('\x1b[0;31mERROR\x1b[0m [0;94mtext[0m'),
                         'ERROR text')

    def test_tiktok_bad_page_retries_with_another_http_profile(self):
        profiles = []

        def scenario(ydl):
            profiles.append(str(ydl.params.get('impersonate', 'default')))
            if len(profiles) == 1:
                (Path(ydl.params['paths']['home']) / 'bad-response.part').write_bytes(b'partial')
                ydl.params['logger'].error('Unexpected response from webpage request')
                return None
            return ydl.item(1)

        FakeYDL.scenario = scenario
        on_retry = Mock()
        report = self.run_download(Mock(), url='https://www.tiktok.com/@user/video/123',
                                   on_retry=on_retry)
        self.assertEqual(profiles, ['default', 'chrome-146'])
        self.assertEqual((report.sent, report.failed), (1, 0))
        self.assertFalse(report.errors)
        on_retry.assert_called_once_with(2, 3)

    def test_tiktok_retries_are_bounded_and_every_attempt_is_cleaned(self):
        attempts = []

        def scenario(ydl):
            attempts.append(ydl.params['paths']['home'])
            (Path(attempts[-1]) / 'partial.mp4.part').write_bytes(b'partial')
            ydl.params['logger'].error('Unable to extract universal data for rehydration')
            return None

        FakeYDL.scenario = scenario
        report = self.run_download(Mock(), url='https://vm.tiktok.com/test/')
        self.assertEqual(len(attempts), 3)
        self.assertEqual((report.sent, report.failed), (0, 1))
        self.assertTrue(all(not Path(path).exists() for path in attempts))

    def test_similar_error_from_other_site_does_not_use_tiktok_retries(self):
        attempts = []

        def scenario(ydl):
            attempts.append(ydl)
            ydl.params['logger'].error('Unexpected response from webpage request')
            return None

        FakeYDL.scenario = scenario
        self.run_download(Mock(), sources=[('anonymous', {}), ('cookies', {})])
        self.assertEqual(len(attempts), 1)

    def test_tiktok_upload_failure_does_not_redownload_or_resend(self):
        attempts = []

        def scenario(ydl):
            attempts.append(ydl)
            return ydl.item(1)

        FakeYDL.scenario = scenario
        report = self.run_download(Mock(side_effect=RuntimeError('upload failed')),
                                   url='https://www.tiktok.com/@user/video/123')
        self.assertEqual(len(attempts), 1)
        self.assertEqual((report.sent, report.failed), (0, 1))

    def test_tiktok_fallback_does_not_resend_completed_items(self):
        attempts = []

        def scenario(ydl):
            attempts.append(ydl)
            first = ydl.item(1)
            if len(attempts) == 1:
                ydl.params['logger'].error('Unexpected response from webpage request')
                return {'_type': 'playlist', 'entries': [first, None]}
            return {'_type': 'playlist', 'entries': [first, ydl.item(2)]}

        FakeYDL.scenario = scenario
        sent = []
        report = self.run_download(lambda filename, info: sent.append(info['id']),
                                   url='https://www.tiktok.com/@user/video/123')
        self.assertEqual(sent, ['1', '2'])
        self.assertEqual((report.sent, report.failed), (2, 0))

    def test_tiktok_attempts_preserve_cookie_scope_without_changing_input(self):
        cookie_options = {'_scoped_cookie_file': 'tiktok.txt', '_cookie_site': 'tiktok'}
        attempts = pipeline.get_download_attempts(
            'https://www.tiktok.com/@user/video/123', [('anonymous', {}), ('cookies', cookie_options)])
        self.assertEqual(len(attempts), 6)
        self.assertNotIn('impersonate', cookie_options)
        self.assertEqual(attempts[3][1]['_cookie_site'], 'tiktok')
        self.assertEqual(attempts[5][1]['_scoped_cookie_file'], 'tiktok.txt')
        self.assertEqual(pipeline.get_download_attempts(
            'https://tiktok.com.evil.test/video/123', [('anonymous', {})]), [('anonymous', {})])


class SpotifyTests(unittest.TestCase):
    def setUp(self):
        self.track = {'name': 'Example Song', 'type': 'track', 'duration_ms': 180000,
                      'artists': [{'name': 'Example Artist'}]}
        self.match = {'title': 'Example Song', 'artists': [{'name': 'Example Artist'}],
                      'duration_seconds': 181, 'videoId': 'abcdefghijk'}

    def test_song_matches_artist_title_and_duration(self):
        client = Mock()
        client.search.return_value = [self.match]
        url, metadata = music_sources.find_spotify_audio(self.track, client)
        self.assertEqual(url, 'https://www.youtube.com/watch?v=abcdefghijk')
        self.assertEqual(metadata['artist'], 'Example Artist')

    def test_covers_remixes_wrong_artist_and_short_previews_are_rejected(self):
        variants = [dict(self.match, title='Example Song (Cover)'),
                    dict(self.match, title='Example Song (Remix)'),
                    dict(self.match, artists=[{'name': 'Someone Else'}]),
                    dict(self.match, duration_seconds=30)]
        for candidate in variants:
            with self.subTest(candidate=candidate):
                self.assertEqual(music_sources.match_score(self.track, candidate), 0)

    def test_original_remix_is_allowed(self):
        track = dict(self.track, name='Example Song (Remix)')
        candidate = dict(self.match, title='Example Song (Remix)')
        self.assertGreater(music_sources.match_score(track, candidate), 0.9)

    def test_no_match_is_an_explicit_error(self):
        client = Mock()
        client.search.return_value = []
        with self.assertRaises(music_sources.MusicSourceError):
            music_sources.find_spotify_audio(self.track, client)

    def test_localized_and_embedded_spotify_links(self):
        self.assertEqual(music_sources.spotify_reference(
            'https://open.spotify.com/intl-ru/album/abc123?si=test'), ('album', 'abc123'))
        self.assertEqual(music_sources.spotify_reference(
            'https://open.spotify.com/embed/track/abc123'), ('track', 'abc123'))
        self.assertFalse(music_sources.is_spotify_url('https://open.spotify.com.evil.test/track/a'))

    def test_contributor_profiles_from_playlists_are_supported(self):
        track = dict(self.track, artists=[{'profile': {'name': 'Example Artist'}}])
        self.assertGreater(music_sources.match_score(track, self.match), 0.9)

    def test_album_uses_paginated_album_tracks_method(self):
        client = Mock()
        client.album_tracks.return_value = {'items': [self.track, self.track]}
        with patch('SpotipyFree.Spotify', return_value=client):
            tracks = music_sources.get_spotify_tracks('https://open.spotify.com/album/abc')
        self.assertEqual(len(tracks), 2)
        client.album_tracks.assert_called_once_with('abc')
        client.album.assert_not_called()


class CookieTests(unittest.TestCase):
    def test_yandex_session_is_limited_to_music_and_other_cookies_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / 'cookies.txt'
            filename.write_text(
                '# Netscape HTTP Cookie File\n'
                '.yandex.ru\tTRUE\t/\tTRUE\t2147483647\tSession_id\ttest-session\n'
                '.google.com\tTRUE\t/\tTRUE\t2147483647\tsecret\tother-site\n', encoding='utf-8')
            before = filename.read_bytes()
            jar = cookie_scope.load_scoped_cookie_file(str(filename), 'yandex_music')
            cookies = list(jar)
            self.assertEqual(len(cookies), 1)
            self.assertEqual(cookies[0].domain, '.music.yandex.ru')
            self.assertEqual(filename.read_bytes(), before)
            self.assertIn('Session_id=test-session', jar.get_cookie_header('https://api.music.yandex.ru/'))
            self.assertFalse(jar.get_cookie_header('https://mail.yandex.ru/'))
            self.assertFalse(jar.get_cookie_header('https://youtube.com/'))

    def test_yandex_domain_detection_does_not_accept_lookalikes(self):
        self.assertEqual(cookie_scope.site_for_url('https://music.yandex.ru/album/1'), 'yandex_music')
        self.assertIsNone(cookie_scope.site_for_url('https://music.yandex.ru.evil.test/album/1'))


class FakeBot:
    def __init__(self):
        self.sent = []
        self.statuses = []
        self.fail_title = None
        self.fail_status_edits = False

    def message_handler(self, *args, **kwargs):
        return lambda function: function

    callback_query_handler = message_handler

    def send_message(self, chat_id, text, **kwargs):
        self.statuses.append(text)
        return SimpleNamespace(message_id=1)

    def edit_message_text(self, text, *args, **kwargs):
        if self.fail_status_edits:
            raise RuntimeError('status unavailable')
        self.statuses.append(text)

    def send_audio(self, chat_id, content, **kwargs):
        assert content.read()
        if kwargs['title'] == self.fail_title:
            raise RuntimeError('Telegram upload failed')
        self.sent.append(kwargs['title'])

    def send_video(self, chat_id, content, **kwargs):
        assert content.read()
        self.sent.append('video')


class BotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = FakeBot()
        config = SimpleNamespace(TOKEN='123:test', ADMIN_ID=1,
                                 FFMPEG_PATH=str(ROOT / 'ffmpeg-master-latest-win64-gpl-shared/bin/ffmpeg.exe'))
        spec = importlib.util.spec_from_file_location('dl06_under_test', ROOT / 'DL-06.py')
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'config': config}), patch('telebot.TeleBot', return_value=cls.bot):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.bot.sent.clear()
        self.bot.statuses.clear()
        self.bot.fail_title = None
        self.bot.fail_status_edits = False
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.module.DOWNLOAD_FOLDER = self.temporary.name
        self.stats_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.stats_temporary.cleanup)
        self.module.TRAFFIC_STATE_FILE = str(Path(self.stats_temporary.name) / 'traffic.sqlite3')
        cloud = patch.object(self.module, 'get_link_storage',
                             side_effect=self.module.StorageError('Test cloud is not configured'))
        cloud.start()
        self.addCleanup(cloud.stop)

    def test_album_continues_after_send_failure_and_request_folder_is_removed(self):
        FakeYDL.scenario = lambda ydl: {
            '_type': 'playlist', 'entries': [ydl.item(index) for index in range(1, 4)]}
        self.bot.fail_title = 'Track 2'
        with patch.object(pipeline, 'YoutubeDL', FakeYDL):
            self.module.download_content('https://soundcloud.com/artist/sets/album', 1, 'audio')
        self.assertEqual(self.bot.sent, ['Track 1', 'Track 3'])
        self.assertIn('Отправлено: 2. Не удалось: 1', self.bot.statuses[-1])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_status_edit_error_does_not_prevent_delivery_or_cleanup(self):
        FakeYDL.scenario = lambda ydl: ydl.item(1)
        self.bot.fail_status_edits = True
        with patch.object(pipeline, 'YoutubeDL', FakeYDL):
            self.module.download_content('https://soundcloud.com/artist/track', 1, 'audio')
        self.assertEqual(self.bot.sent, ['Track 1'])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_yandex_without_cookies_has_setup_message_and_no_orphan_files(self):
        self.module.download_content('https://music.yandex.ru/album/1', 1, 'audio')
        self.assertIn('site_cookies/yandex_music.txt', self.bot.statuses[-1])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_yandex_preview_is_not_sent_as_full_song(self):
        filename = Path(self.temporary.name) / 'preview.mp3'
        filename.write_bytes(b'preview')
        info = {'title': 'Preview', 'extractor_key': 'YandexMusicTrack', 'duration': 180}
        with patch.object(self.module, 'get_media_duration', return_value=30):
            with self.assertRaises(music_sources.MusicSourceError):
                self.module.send_downloaded_file(str(filename), info, 1, True,
                                                 Mock(), self.temporary.name)
        self.assertFalse(self.bot.sent)

    def test_video_delivery_still_uses_send_video(self):
        FakeYDL.scenario = lambda ydl: ydl.item(1)
        with patch.object(pipeline, 'YoutubeDL', FakeYDL):
            self.module.download_content('https://example.com/video', 1, 'video')
        self.assertEqual(self.bot.sent, ['video'])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_large_music_is_uploaded_without_compression(self):
        from test_links import LocalStorage
        storage = LocalStorage()
        FakeYDL.scenario = lambda ydl: ydl.item(1)

        def fail_compression(filename, audio_only, temporary_parent):
            folder = Path(temporary_parent) / 'compress-partial'
            folder.mkdir()
            (folder / 'partial.mp3').write_bytes(b'partial')
            raise self.module.CompressionError('ffmpeg failed')

        with patch.object(pipeline, 'YoutubeDL', FakeYDL), \
                patch.object(self.module, 'MAX_FILE_SIZE_BYTES', 1), \
                patch.object(self.module, 'get_link_storage', return_value=storage), \
                patch.object(self.module, 'compress_media', side_effect=fail_compression) as compress:
            self.module.download_content('https://soundcloud.com/artist/track', 1, 'audio')
        self.assertFalse(self.bot.sent)
        compress.assert_not_called()
        self.assertEqual(len(storage.uploaded), 1)
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_telegram_rate_limit_retries_file_from_the_beginning(self):
        ApiTelegramException = self.module.telebot.apihelper.ApiTelegramException
        filename = Path(self.temporary.name) / 'track.mp3'
        filename.write_bytes(b'full audio')
        error = ApiTelegramException('sendAudio', Mock(), {
            'ok': False, 'error_code': 429, 'description': 'Too many requests',
            'parameters': {'retry_after': 1}})
        reads = []

        def send_audio(chat_id, content, **kwargs):
            reads.append(content.read())
            if len(reads) == 1:
                raise error

        with patch.object(self.bot, 'send_audio', side_effect=send_audio), \
                patch.object(self.module.time, 'sleep') as sleep:
            self.module.send_downloaded_file(str(filename), {'title': 'Song'}, 1,
                                             True, Mock(), self.temporary.name)
        self.assertEqual(reads, [b'full audio', b'full audio'])
        sleep.assert_called_once_with(2)
        self.assertEqual(self.module.get_traffic_store().snapshot()['telegram_bytes'], len(b'full audio'))

    def test_spotify_continues_after_unmatched_track_and_forces_audio(self):
        tracks = [{'name': f'Spotify {index}'} for index in range(1, 4)]
        FakeYDL.scenario = lambda ydl: ydl.item(1)

        def match(track, client):
            if track['name'] == 'Spotify 2':
                raise music_sources.MusicSourceError('No matching recording')
            return ('https://www.youtube.com/watch?v=abcdefghijk',
                    {'title': track['name'], 'track': track['name'], 'artist': 'Artist'})

        with patch.object(pipeline, 'YoutubeDL', FakeYDL), \
                patch.object(self.module, 'get_spotify_tracks', return_value=tracks), \
                patch.object(self.module, 'find_spotify_audio', side_effect=match), \
                patch.object(self.module, 'music_search_client', return_value=nullcontext(Mock())):
            self.module.download_content('https://open.spotify.com/album/abc', 1, 'video')
        self.assertEqual(self.bot.sent, ['Spotify 1', 'Spotify 3'])
        self.assertIn('Отправлено: 2. Не удалось: 1', self.bot.statuses[-1])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_tiktok_bad_page_has_readable_message_after_retries(self):
        def scenario(ydl):
            ydl.params['logger'].error(
                '\x1b[0;31mERROR:\x1b[0m Unexpected response from webpage request; '
                'please report this issue on https://github.com/yt-dlp/yt-dlp/issues')
            return None

        FakeYDL.scenario = scenario
        with patch.object(pipeline, 'YoutubeDL', FakeYDL):
            self.module.download_content('https://www.tiktok.com/@user/video/123', 1, 'video')
        self.assertIn('TikTok не отдал данные ролика', self.bot.statuses[-1])
        self.assertNotIn('please report', self.bot.statuses[-1])
        self.assertNotIn('\x1b', self.bot.statuses[-1])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
