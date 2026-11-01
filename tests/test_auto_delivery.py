import importlib.util
import json
import sys
import tempfile
import functools
import http.server
import threading
import subprocess
import wave
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_downloads import ROOT, FakeYDL, FakeBot
from test_links import LocalStorage
import download_pipeline as pipeline
from media_policy import is_short_video, readable_filename, RequestBudget
from request_choices import ChoiceStore, ChoiceError


class DeliveryBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.deliveries = []
        self.documents = []
        self.cloud_messages = []
        self.answer_callback_query = Mock()
        self.reply_to = Mock(return_value=SimpleNamespace(message_id=50))

    def send_audio(self, chat_id, content, **kwargs):
        if kwargs.get('thumbnail'):
            kwargs['_thumbnail_bytes'] = kwargs['thumbnail'].read()
        self.deliveries.append(('audio', Path(content.name).name, kwargs, content.read()))
        self.sent.append(kwargs['title'])

    def send_video(self, chat_id, content, **kwargs):
        self.deliveries.append(('video', Path(content.name).name, kwargs, content.read()))
        self.sent.append('video')

    def send_document(self, chat_id, content, **kwargs):
        self.documents.append((Path(content.name).name, content.read(), kwargs))

    def send_message(self, chat_id, text, **kwargs):
        if kwargs.get('disable_web_page_preview'):
            self.cloud_messages.append((text, kwargs))
        return super().send_message(chat_id, text, **kwargs)


class AutoDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = DeliveryBot()
        config = SimpleNamespace(TOKEN='123:test', ADMIN_ID=1,
                                 FFMPEG_PATH=str(ROOT/'ffmpeg-master-latest-win64-gpl-shared/bin/ffmpeg.exe'))
        spec = importlib.util.spec_from_file_location('dl06_auto_test', ROOT/'DL-06.py')
        cls.app = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'config': config}), patch('telebot.TeleBot', return_value=cls.bot):
            spec.loader.exec_module(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.downloads = self.root/'downloads'
        self.downloads.mkdir()
        self.app.DOWNLOAD_FOLDER = str(self.downloads)
        self.app.TRAFFIC_STATE_FILE = str(self.root/'traffic.sqlite3')
        self.app.ALLOWED_USER_IDS = {'1', '2'}
        self.app.CHOICES = ChoiceStore()
        self.app.ACTIVE_DOWNLOAD_USERS.clear()
        for items in (self.bot.deliveries, self.bot.documents, self.bot.cloud_messages,
                      self.bot.sent, self.bot.statuses):
            items.clear()
        self.storage = LocalStorage()
        self.upload = self.storage.upload = Mock(wraps=self.storage.upload)
        self.cloud = patch.object(self.app, 'get_link_storage', return_value=self.storage).start()
        self.addCleanup(patch.stopall)
        patch.object(self.app, 'MAX_FILE_SIZE_BYTES', 10).start()

    def item(self, ydl, index, size=5, ext='flac', title=None):
        path = Path(ydl.params['paths']['home'])/f'{index}.{ext}'
        path.write_bytes(bytes([index])*size)
        info = {'id': str(index), 'filepath': str(path), 'title': title or f'Трек {index}',
                'artist': 'Исполнитель', 'album': 'Мой альбом',
                'playlist_id': 'album', 'playlist_index': index}
        ydl.processor.callback(info)
        return info

    def run_media(self, *, url='https://soundcloud.com/artist/song', mode='audio',
                  delivery='auto', user=2, audio_format='original'):
        with patch.object(pipeline, 'YoutubeDL', FakeYDL):
            self.app.download_content(url, 2, mode, delivery, audio_format, user)
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_small_original_audio_uses_telegram_without_any_r2_calls(self):
        options = []
        def scenario(ydl):
            options.append(ydl.params)
            return self.item(ydl, 1)
        FakeYDL.scenario = scenario
        with patch.object(self.app, 'compress_media') as compress:
            self.run_media()
        self.cloud.assert_not_called()
        compress.assert_not_called()
        self.assertEqual(self.bot.deliveries[0][1], 'Исполнитель — Трек 1.flac')
        self.assertEqual(options[0]['postprocessors'][0]['preferredcodec'], 'best')
        self.assertNotIn('preferredquality', options[0]['postprocessors'][0])

    def test_large_music_stays_original_and_records_personal_owner_quota(self):
        FakeYDL.scenario = lambda ydl: self.item(ydl, 1, size=12)
        with patch.object(self.app, 'compress_media') as compress:
            self.run_media()
        compress.assert_not_called()
        self.assertFalse(self.bot.deliveries)
        self.assertEqual(self.upload.call_args.kwargs['user_id'], '2')
        self.assertEqual(self.upload.call_args.kwargs['user_limit_bytes'], 2_000_000_000)
        text, kwargs = self.bot.cloud_messages[0]
        self.assertIn('Трек превышает', text)
        self.assertIn('Исполнитель — Трек 1.flac', text)
        self.assertEqual(kwargs['reply_markup'].keyboard[0][0].text, '⬇️ Скачать трек')

    def test_mixed_album_sends_two_tracks_and_one_link_in_order(self):
        FakeYDL.scenario = lambda ydl: {'entries': [self.item(ydl, i, size=size)
                                                   for i, size in enumerate((5,12,7),1)]}
        self.run_media()
        self.assertEqual(self.bot.sent, ['Трек 1', 'Трек 3'])
        self.assertEqual(len(self.storage.uploaded), 1)
        self.assertIn('Трек 2.flac', self.bot.cloud_messages[0][0])
        self.assertIn('В Telegram: 2. По ссылкам: 1.', self.bot.statuses[-1])

    def test_explicit_zip_keeps_cyrillic_names_and_uploads_once(self):
        FakeYDL.scenario = lambda ydl: {'entries': [self.item(ydl, i) for i in (1,2)]}
        self.run_media(delivery='zip')
        self.assertEqual(self.upload.call_count, 1)
        self.assertFalse(self.bot.deliveries)
        self.assertEqual(self.storage.zip_entries,
                         ['001 - Исполнитель — Трек 1.flac', '002 - Исполнитель — Трек 2.flac'])
        self.assertEqual(self.storage.uploaded[0][0], 'Мой альбом.zip')

    def test_admin_has_no_personal_quota_but_still_uses_the_storage_layer(self):
        FakeYDL.scenario = lambda ydl: self.item(ydl, 1, size=12)
        self.run_media(user=1)
        self.assertIsNone(self.upload.call_args.kwargs['user_limit_bytes'])
        self.assertEqual(self.upload.call_args.kwargs['user_id'], '1')

    def test_long_video_is_not_compressed_and_is_linked(self):
        FakeYDL.scenario = lambda ydl: self.item(ydl, 1, size=12, ext='mp4', title='Большое видео')
        with patch.object(self.app, 'compress_media') as compress:
            self.run_media(url='https://www.youtube.com/watch?v=dQw4w9WgXcQ', mode='video')
        compress.assert_not_called()
        self.assertIn('Видео превышает', self.bot.cloud_messages[0][0])
        self.assertIn('Большое видео.mp4', self.bot.cloud_messages[0][0])

    def test_short_video_is_compressed_for_telegram_and_does_not_touch_r2(self):
        FakeYDL.scenario = lambda ydl: self.item(ydl, 1, size=12, ext='mp4', title='Короткое видео')
        def compress(filename, audio_only, max_output_bytes, temporary_parent):
            self.assertFalse(audio_only)
            folder = Path(temporary_parent)/'compressed'
            folder.mkdir()
            path = folder/'internal.mp4'
            path.write_bytes(b'mp4')
            return str(path), str(folder)
        with patch.object(self.app, 'compress_media', side_effect=compress):
            self.run_media(url='https://www.tiktok.com/@artist/video/123', mode='video')
        self.cloud.assert_not_called()
        self.assertEqual(self.bot.deliveries[0][1], 'Исполнитель — Короткое видео.mp4')
        self.assertEqual(self.app.get_traffic_store().snapshot()['telegram_bytes'], 3)

    def test_failed_short_video_compression_falls_back_to_original_link_and_cleans_partials(self):
        FakeYDL.scenario = lambda ydl: self.item(ydl, 1, size=12, ext='mp4')
        def compress(filename, *args, **kwargs):
            (Path(kwargs['temporary_parent'])/'failed.part').write_bytes(b'partial')
            raise self.app.CompressionError('too low quality')
        with patch.object(self.app, 'compress_media', side_effect=compress):
            self.run_media(url='https://www.youtube.com/shorts/abcdefghijk', mode='video')
        self.assertEqual(self.storage.uploaded[0][1], 12)

    def test_failed_cloud_upload_does_not_prevent_next_album_track(self):
        self.storage.fail_upload = True
        FakeYDL.scenario = lambda ydl: {'entries': [self.item(ydl, 1, size=12), self.item(ydl, 2)]}
        self.run_media()
        self.assertEqual(self.bot.sent, ['Трек 2'])
        self.assertFalse(self.bot.cloud_messages)

    def test_original_flac_rejected_by_audio_api_falls_back_to_document_without_cloud(self):
        exception = self.app.telebot.apihelper.ApiTelegramException('sendAudio', Mock(),
                    {'ok':False, 'error_code':400, 'description':'Bad Request: unsupported audio format'})
        FakeYDL.scenario = lambda ydl: self.item(ydl, 1)
        with patch.object(self.bot, 'send_audio', side_effect=exception):
            self.run_media()
        self.cloud.assert_not_called()
        self.assertEqual(self.bot.documents[0][0], 'Исполнитель — Трек 1.flac')
        self.assertEqual(self.bot.documents[0][1], b'\1'*5)
        self.assertEqual(self.app.get_traffic_store().snapshot()['telegram_bytes'], 5)

    def test_transport_failure_is_not_retried_as_a_document(self):
        FakeYDL.scenario = lambda ydl: self.item(ydl, 1)
        with patch.object(self.bot, 'send_audio', side_effect=TimeoutError('network')):
            self.run_media()
        self.assertFalse(self.bot.documents)
        self.cloud.assert_not_called()

    def test_inline_album_selection_is_owned_and_cannot_enqueue_twice(self):
        item = self.app.CHOICES.create(2, 2, 'https://soundcloud.com/user/sets/album')
        self.app.CHOICES.prepare(item.key, album=True, title='Альбом', count=2)
        self.app.CHOICES.bind(item.key, 50)
        def call(user, action):
            return SimpleNamespace(id='callback', from_user=SimpleNamespace(id=user),
                message=SimpleNamespace(chat=SimpleNamespace(id=2), message_id=50),
                data=f'choose:{item.key}:{action}')
        with patch.object(self.app, 'enqueue_download') as enqueue:
            self.app.choose_download(call(1, 'original'))
            enqueue.assert_not_called()
            self.app.choose_download(call(2, 'original'))
            enqueue.assert_not_called()
            self.app.choose_download(call(2, 'zip'))
            self.app.choose_download(call(2, 'zip'))
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs['audio_format'], 'original')
        self.assertEqual(enqueue.call_args.kwargs['delivery_mode'], 'zip')

    def test_audio_choice_probes_once_and_does_not_download_before_selection(self):
        item = self.app.CHOICES.create(2, 2, 'https://soundcloud.com/artist/song')
        message = SimpleNamespace(chat=SimpleNamespace(id=2))
        with patch.object(self.app, 'probe_audio', return_value=(False,'Песня',1,None)), \
                patch.object(self.app, 'download_content') as download:
            self.app.prepare_audio_choice(item, message)
        download.assert_not_called()
        keyboard = self.bot.reply_to.call_args.kwargs['reply_markup']
        labels = [row[0].text for row in keyboard.keyboard]
        self.assertEqual(labels, ['Исходный формат', 'MP3', 'Отмена'])
        for row in keyboard.keyboard:
            self.assertLessEqual(len(row[0].callback_data.encode()),64)
            self.assertNotIn('https://', row[0].callback_data)

    def test_download_queue_blocks_duplicate_user_and_releases_user_after_failure(self):
        self.app.ACTIVE_DOWNLOAD_USERS.add('2')
        with patch.object(self.app.DOWNLOAD_EXECUTOR,'submit') as submit:
            self.assertFalse(self.app.enqueue_download('url',2,'video',user_id=2))
            submit.assert_not_called()
        self.app.ACTIVE_DOWNLOAD_USERS.clear()
        with patch.object(self.app.DOWNLOAD_EXECUTOR,'submit',side_effect=lambda run:run()), \
                patch.object(self.app,'download_content',side_effect=RuntimeError('worker failed')):
            self.assertTrue(self.app.enqueue_download('url',2,'video',user_id=2))
        self.assertNotIn('2',self.app.ACTIVE_DOWNLOAD_USERS)

    def test_one_track_spotify_album_still_offers_album_delivery(self):
        with patch.object(self.app,'spotify_reference',return_value=('album','testid')), \
                patch.object(self.app,'get_spotify_tracks',return_value=[{'name':'One','album':{'name':'Album'}}]):
            album,title,count,tracks = self.app.probe_audio('https://spotify.link/short')
        self.assertTrue(album)
        self.assertEqual(count,1)

    def test_soundcloud_drm_has_specific_message_instead_of_invalid_link(self):
        downloader = Mock()
        downloader.__enter__ = Mock(return_value=downloader)
        downloader.__exit__ = Mock(return_value=False)
        downloader.extract_info.side_effect = RuntimeError(
            'ERROR: [soundcloud] 252642814: This video is DRM protected')
        with patch.object(self.app,'YoutubeDL',return_value=downloader):
            with self.assertRaises(ChoiceError) as failure:
                self.app.probe_audio('https://soundcloud.com/yg400/left-right-1')
        text = str(failure.exception)
        self.assertIn('SoundCloud',text)
        self.assertIn('DRM',text)
        self.assertIn('Ссылка распознана',text)
        self.assertNotIn('Проверьте ссылку',text)

    def test_metadata_cookie_attempt_can_succeed_after_initial_drm_error(self):
        first,second = Mock(),Mock()
        for downloader in (first,second):
            downloader.__enter__ = Mock(return_value=downloader)
            downloader.__exit__ = Mock(return_value=False)
        first.extract_info.side_effect = RuntimeError('This video is DRM protected')
        second.extract_info.return_value = {'title':'Available original','formats':[{'ext':'flac'}]}
        with patch.object(self.app,'get_music_cookie_sources',return_value=[('first',{}),('second',{})]), \
                patch.object(self.app,'YoutubeDL',side_effect=[first,second]):
            result = self.app.probe_audio('https://soundcloud.com/artist/song')
        self.assertEqual(result[:3],(False,'Available original',1))


class NativeAudioIntegrationTests(unittest.TestCase):
    def setUp(self):
        AutoDeliveryTests.setUpClass()
        self.fixture = AutoDeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app, self.bot = self.fixture.app, self.fixture.bot
        patch.object(self.app, 'MAX_FILE_SIZE_BYTES', 49_000_000).start()
        self.media = self.fixture.root/'media'
        self.media.mkdir()
        wav = self.media/'source.wav'
        with wave.open(str(wav),'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b'\0\0'*8000)
        self.source = self.media/'source.flac'
        for command in ([self.app.FFMPEG_PATH,'-v','error','-y','-i',str(wav),str(self.source)],
                        [self.app.FFMPEG_PATH,'-v','error','-y','-f','lavfi','-i',
                         'color=c=red:s=400x400','-frames:v','1',str(self.media/'cover.jpg')]):
            subprocess.run(command,check=True,capture_output=True,timeout=20)
        from test_ytdlp_integration import QuietHandler
        server = http.server.ThreadingHTTPServer(('127.0.0.1',0),
                    functools.partial(QuietHandler,directory=str(self.media)))
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        from yt_dlp.extractor.common import InfoExtractor
        from yt_dlp import YoutubeDL
        fixture = self
        class NativeIE(InfoExtractor):
            _VALID_URL = r'dl06native:(?P<id>test)'
            def _real_extract(self,url):
                return {'id':'native','title':'Мой трек','track':'Мой трек','artist':'Артист',
                        'ext':fixture.source.suffix[1:],'url':base+'/'+fixture.source.name,
                        'thumbnail':base+'/cover.jpg','duration':0.5}
        def make_downloader(options):
            downloader = YoutubeDL(options,auto_init=False)
            downloader.add_info_extractor(NativeIE())
            downloader.add_default_info_extractors()
            return downloader
        patch.object(pipeline,'YoutubeDL',side_effect=make_downloader).start()

    def test_original_flac_keeps_audio_frames_and_sends_cover_with_cyrillic_metadata(self):
        from mutagen.flac import FLAC
        original_md5 = FLAC(self.source).info.md5_signature
        self.app.download_content('dl06native:test',2,'audio','auto','original',2)
        self.fixture.cloud.assert_not_called()
        delivery = self.bot.deliveries[0]
        self.assertEqual(delivery[1],'Артист — Мой трек.flac')
        audio = FLAC(io.BytesIO(delivery[3]))
        self.assertEqual(audio.info.md5_signature,original_md5)
        self.assertEqual(audio['artist'],['Артист'])
        self.assertEqual(audio['title'],['Мой трек'])
        self.assertTrue(audio.pictures)
        self.assertTrue(delivery[2]['_thumbnail_bytes'].startswith(b'\xff\xd8'))
        self.assertEqual(list(self.fixture.downloads.iterdir()),[])

    def test_mp3_choice_really_converts_flac_and_keeps_cover(self):
        from mutagen.mp3 import MP3
        self.app.download_content('dl06native:test',2,'audio','auto','mp3',2)
        delivery = self.bot.deliveries[0]
        self.assertEqual(delivery[1],'Артист — Мой трек.mp3')
        self.assertGreater(MP3(io.BytesIO(delivery[3])).info.length,0.4)
        self.assertTrue(delivery[2]['_thumbnail_bytes'].startswith(b'\xff\xd8'))
        self.fixture.cloud.assert_not_called()
        self.assertEqual(list(self.fixture.downloads.iterdir()),[])

    def test_original_opus_is_delivered_as_m4a_music_with_cover(self):
        from mutagen.mp4 import MP4
        opus = self.media/'source.opus'
        subprocess.run([self.app.FFMPEG_PATH,'-v','error','-i',str(self.source),
                        '-c:a','libopus','-b:a','128k',str(opus)],capture_output=True,check=True,timeout=20)
        self.source = opus
        self.app.download_content('dl06native:test',2,'audio','auto','original',2)
        delivery = self.bot.deliveries[0]
        self.assertEqual(delivery[1],'Артист — Мой трек.m4a')
        audio = MP4(io.BytesIO(delivery[3]))
        self.assertGreater(audio.info.length,0.4)
        self.assertEqual(audio['\xa9ART'],['Артист'])
        self.assertTrue(audio['covr'])
        self.assertTrue(delivery[2]['_thumbnail_bytes'].startswith(b'\xff\xd8'))
        self.fixture.cloud.assert_not_called()
        self.assertEqual(list(self.fixture.downloads.iterdir()),[])


class ChoiceAndPolicyTests(unittest.TestCase):
    def test_expiry_cancellation_and_message_binding(self):
        now = [0]
        store = ChoiceStore(clock=lambda:now[0])
        item = store.create(2,2,'url')
        store.bind(item.key,50)
        with self.assertRaises(ChoiceError):
            store.choose(item.key,2,2,51,'original')
        now[0] = 901
        with self.assertRaises(ChoiceError):
            store.choose(item.key,2,2,50,'original')
        item = store.create(2,2,'url')
        store.bind(item.key,50)
        self.assertEqual(store.choose(item.key,2,2,50,'cancel')[1],'cancel')
        with self.assertRaises(ChoiceError):
            store.choose(item.key,2,2,50,'original')

    def test_clip_detection_does_not_treat_every_short_duration_as_a_short(self):
        self.assertTrue(is_short_video('https://www.instagram.com/reel/abc/',{}))
        self.assertTrue(is_short_video('https://www.youtube.com/shorts/abc',{}))
        self.assertFalse(is_short_video('https://www.youtube.com/watch?v=abc',{'duration':20}))

    def test_safe_names_keep_unicode_and_cannot_escape_directories(self):
        name = readable_filename('x.flac',{'artist':'Мой артист','title':'../Трек: Один.flac'})
        self.assertIn('Мой артист',name)
        self.assertIn('Трек_ Один',name)
        self.assertNotIn('/',name)
        self.assertNotIn('\\',name)

    def test_request_budget_applies_to_auto_delivery_too(self):
        budget = RequestBudget(max_bytes=10)
        budget.observe({'status':'downloading','filename':'a','downloaded_bytes':7})
        with self.assertRaises(pipeline.RequestAbortedError):
            budget.observe({'status':'downloading','filename':'b','downloaded_bytes':4})


if __name__ == '__main__':
    unittest.main()
